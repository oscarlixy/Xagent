import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from x_digest.models import Base, Link, Post, Summary
from x_digest.repositories.lists import upsert_list
from x_digest.services.ingestion import IngestionService
from x_digest.services.pipeline import PipelineService
from x_digest.services.summarization import DeterministicSummarizer
from x_digest.sources.fake import FakeXSource
from x_digest.sources.types import RawAuthor, RawPost, SourcePage


@dataclass
class FlakyLinks:
    failures_remaining: int = 0

    def process(self, post: Post) -> None:
        if self.failures_remaining and post.platform_post_id == "101":
            self.failures_remaining -= 1
            raise RuntimeError("temporary link failure")


def make_pipeline(
    *, posts: tuple[RawPost, ...], max_items: int = 100, links: FlakyLinks | None = None
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="list-a", name="AI news")
        list_id = x_list.id
    ingestion = IngestionService(
        session_factory=session_factory,
        source=FakeXSource({None: SourcePage(posts=posts)}),
    )
    service = PipelineService(
        ingestion=ingestion,
        session_factory=session_factory,
        summarizer=DeterministicSummarizer(),
        llm_max_items_per_run=max_items,
        link_processor=links.process if links else None,
    )
    return session_factory, list_id, service


def raw_post(post_id: str) -> RawPost:
    return RawPost(
        id=post_id,
        author=RawAuthor(id=f"author-{post_id}", username="author"),
        text=f"Post {post_id} is ready for an offline digest.",
        created_at=datetime.now(UTC),
        source_url=f"https://x.com/author/status/{post_id}",
        entities={"urls": [{"expanded_url": f"https://example.com/{post_id}"}]},
    )


def test_newly_ingested_post_reaches_persisted_summary() -> None:
    session_factory, list_id, service = make_pipeline(posts=(raw_post("101"),))

    result = asyncio.run(service.run_list_pipeline(list_id))

    assert result.status == "succeeded"
    assert result.stages["ingestion"].counts["posts_created"] == 1
    assert result.stages["processing"].counts["processed"] == 1
    assert result.stages["summary"].counts["created"] == 1
    with session_factory() as session:
        assert len(session.scalars(select(Summary)).all()) == 1


def test_failed_link_processing_does_not_block_summary() -> None:
    session_factory, list_id, service = make_pipeline(
        posts=(raw_post("101"),), links=FlakyLinks(failures_remaining=1)
    )

    result = asyncio.run(service.run_list_pipeline(list_id))

    assert result.status == "partial"
    assert result.stages["links"].counts["failed"] == 1
    assert result.stages["summary"].counts["created"] == 1
    with session_factory() as session:
        assert len(session.scalars(select(Summary)).all()) == 1
        assert len(session.scalars(select(Link)).all()) == 1


def test_pipeline_enforces_summary_item_cap_per_run() -> None:
    session_factory, list_id, service = make_pipeline(
        posts=(raw_post("101"), raw_post("102"), raw_post("103")), max_items=2
    )

    result = asyncio.run(service.run_list_pipeline(list_id))

    assert result.stages["summary"].counts == {"selected": 2, "created": 2, "failed": 0}
    with session_factory() as session:
        assert len(session.scalars(select(Summary)).all()) == 2


def test_retryable_link_work_is_picked_up_by_next_pipeline_run() -> None:
    session_factory, list_id, service = make_pipeline(
        posts=(raw_post("101"),), links=FlakyLinks(failures_remaining=1)
    )

    first = asyncio.run(service.run_list_pipeline(list_id))
    second = asyncio.run(service.run_list_pipeline(list_id))

    assert first.stages["links"].counts["failed"] == 1
    assert second.stages["links"].counts["processed"] == 1
    with session_factory() as session:
        assert session.scalar(select(Link.fetch_status)) == "pending"
