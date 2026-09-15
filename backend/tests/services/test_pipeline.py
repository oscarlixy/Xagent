import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from x_digest.models import Base, Link, Post, Summary, SyncRun, Thread, ThreadPost, XList
from x_digest.observability import status_snapshot
from x_digest.repositories.lists import upsert_list
from x_digest.services.ingestion import IngestionService
from x_digest.services.pipeline import PipelineService
from x_digest.services.summarization import DeterministicSummarizer
from x_digest.sources.fake import FakeXSource
from x_digest.sources.types import RawAuthor, RawPost, RejectedItem, SourcePage


@dataclass
class FlakyLinks:
    failures_remaining: int = 0

    def process(self, post: Post) -> None:
        if self.failures_remaining and post.platform_post_id == "101":
            self.failures_remaining -= 1
            raise RuntimeError("temporary link failure")


@dataclass
class FailOnceSummarizer:
    failures_remaining: int = 1
    model_name: str = "fake-summary-v1"

    async def summarize(self, content):
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("temporary provider failure")
        return await DeterministicSummarizer().summarize(content)


def make_pipeline(
    *,
    posts: tuple[RawPost, ...],
    rejected_items: tuple[RejectedItem, ...] = (),
    max_items: int = 100,
    links: FlakyLinks | None = None,
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="list-a", name="AI news")
        list_id = x_list.id
    ingestion = IngestionService(
        session_factory=session_factory,
        source=FakeXSource({None: SourcePage(posts=posts, rejected_items=rejected_items)}),
    )
    service = PipelineService(
        ingestion=ingestion,
        session_factory=session_factory,
        summarizer=DeterministicSummarizer(),
        llm_max_items_per_run=max_items,
        link_processor=links.process if links else None,
    )
    return session_factory, list_id, service


def raw_post(
    post_id: str,
    *,
    author_id: str | None = None,
    conversation_id: str | None = None,
) -> RawPost:
    return RawPost(
        id=post_id,
        author=RawAuthor(id=author_id or f"author-{post_id}", username="author"),
        text=f"Post {post_id} is ready for an offline digest.",
        created_at=datetime.now(UTC),
        source_url=f"https://x.com/author/status/{post_id}",
        conversation_id=conversation_id,
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


def test_provider_rejections_remain_partial_through_pipeline_and_withhold_watermark() -> None:
    session_factory, list_id, service = make_pipeline(
        posts=(raw_post("101"),),
        rejected_items=(RejectedItem(identifier="102", reason="provider_rejected"),),
    )

    result = asyncio.run(service.run_list_pipeline(list_id))

    assert result.status == "partial"
    assert result.stages["ingestion"].counts == {
        "pages_fetched": 1,
        "posts_seen": 1,
        "posts_created": 1,
        "duplicates": 0,
        "rejected": 1,
    }
    assert result.stages["ingestion"].last_error == "ingestion_partial"
    with session_factory() as session:
        run = session.get(SyncRun, result.run_id)
        x_list = session.get(XList, list_id)
        assert run is not None
        assert x_list is not None
        assert run.status == "partial"
        assert run.rejected == 1
        assert run.error_code == "ingestion_partial"
        assert x_list.latest_seen_at is None
        assert x_list.latest_seen_post_id is None
        snapshot = status_snapshot(session)
        assert snapshot["latest_run"]["stages"]["ingestion"]["last_error"] == (
            "ingestion_partial"
        )


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


def test_multi_post_conversation_persists_one_thread_and_one_summary() -> None:
    session_factory, list_id, service = make_pipeline(
        posts=(
            raw_post("101", author_id="author-a", conversation_id="conversation-a"),
            raw_post("102", author_id="author-a", conversation_id="conversation-a"),
        )
    )

    result = asyncio.run(service.run_list_pipeline(list_id))

    assert result.stages["processing"].counts["threads_created"] == 1
    assert result.stages["summary"].counts["created"] == 1
    with session_factory() as session:
        assert len(session.scalars(select(Thread)).all()) == 1
        assert len(session.scalars(select(ThreadPost)).all()) == 2
        summary = session.scalar(select(Summary))
        assert summary is not None
        assert len(summary.source_ids) == 2


def test_standalone_thread_uses_portable_platform_post_id_fallback() -> None:
    platform_post_id = "post-123456789012345678901234567"
    session_factory, list_id, service = make_pipeline(posts=(raw_post(platform_post_id),))

    asyncio.run(service.run_list_pipeline(list_id))

    with session_factory() as session:
        thread = session.scalar(select(Thread))
        assert thread is not None
        assert thread.conversation_id == platform_post_id
        assert len(thread.conversation_id) <= 32


def test_failed_summary_is_retried_without_losing_ingested_post() -> None:
    session_factory, list_id, service = make_pipeline(posts=(raw_post("101"),))
    service._summarizer = FailOnceSummarizer()  # type: ignore[assignment]

    first = asyncio.run(service.run_list_pipeline(list_id))
    second = asyncio.run(service.run_list_pipeline(list_id))

    assert first.status == "partial"
    assert first.stages["summary"].counts["failed"] == 1
    assert second.stages["summary"].counts["created"] == 1
    with session_factory() as session:
        assert len(session.scalars(select(Post)).all()) == 1
        assert len(session.scalars(select(Summary)).all()) == 1


def test_persisted_failed_summary_is_retried_as_current_work() -> None:
    session_factory, list_id, service = make_pipeline(posts=(raw_post("101"),))

    asyncio.run(service.run_list_pipeline(list_id))
    with session_factory.begin() as session:
        summary = session.scalar(select(Summary))
        assert summary is not None
        summary.status = "failed"
        summary.failure_code = "provider_unavailable"
    result = asyncio.run(service.run_list_pipeline(list_id))

    assert result.stages["summary"].counts == {"selected": 1, "created": 1, "failed": 0}
    with session_factory() as session:
        summary = session.scalar(select(Summary))
        assert summary is not None
        assert summary.status == "succeeded"


def test_changed_thread_content_creates_current_summary_work() -> None:
    session_factory, list_id, service = make_pipeline(posts=(raw_post("101"),))

    asyncio.run(service.run_list_pipeline(list_id))
    with session_factory.begin() as session:
        post = session.scalar(select(Post))
        assert post is not None
        post.text = "The post changed and requires a new deterministic summary."
    result = asyncio.run(service.run_list_pipeline(list_id))

    assert result.stages["summary"].counts == {"selected": 1, "created": 1, "failed": 0}
    with session_factory() as session:
        assert len(session.scalars(select(Summary)).all()) == 2


def test_pipeline_refreshes_finished_at_and_logs_correlated_partial_result(
    monkeypatch, caplog
) -> None:
    import x_digest.services.ingestion as ingestion_module

    session_factory, list_id, service = make_pipeline(
        posts=(raw_post("101"),), links=FlakyLinks(failures_remaining=1)
    )
    original_finish = ingestion_module.finish_sync_run

    def backdated_finish(*args, **kwargs):
        run = original_finish(*args, **kwargs)
        run.finished_at = datetime(2000, 1, 1, tzinfo=UTC)
        return run

    monkeypatch.setattr(ingestion_module, "finish_sync_run", backdated_finish)
    with caplog.at_level(logging.INFO, logger="x_digest.services.pipeline"):
        result = asyncio.run(service.run_list_pipeline(list_id))

    with session_factory() as session:
        run = session.get(SyncRun, result.run_id)
        assert run is not None
        assert run.finished_at is not None
        assert run.finished_at.year != 2000
        snapshot = status_snapshot(session)
        assert snapshot["latest_run"]["id"] == result.run_id
        assert snapshot["latest_run"]["stages"]["links"]["counts"]["failed"] == 1
    events = [json.loads(record.message) for record in caplog.records]
    partial_event = next(event for event in events if event["event"] == "pipeline.completed")
    assert partial_event["run_id"] == result.run_id
    assert partial_event["list_id"] == list_id
    assert partial_event["duration_ms"] >= 0
    assert partial_event["counts"]["links"]["failed"] == 1
    assert partial_event["error_code"] == "link_processing_failed"
    assert all(
        {"run_id", "list_id", "duration_ms", "counts", "error_code"} <= event.keys()
        for event in events
    )
