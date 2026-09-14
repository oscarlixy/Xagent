import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from x_digest.models import Base, Post, XList
from x_digest.repositories.lists import upsert_list
from x_digest.services.ingestion import IngestionService
from x_digest.sources.fake import FakeXSource
from x_digest.sources.types import RawAuthor, RawPost, SourcePage


def test_fake_source_sync_is_idempotent_and_advances_watermark() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime.now(UTC)

    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="list-a", name="AI news")
        list_id = x_list.id

    page = SourcePage(
        posts=(
            RawPost(
                id="101",
                author=RawAuthor(id="author-1", username="author"),
                text="first post",
                created_at=now - timedelta(minutes=2),
                source_url="https://x.com/author/status/101",
            ),
            RawPost(
                id="102",
                author=RawAuthor(id="author-2", username="other"),
                text="second post",
                created_at=now,
                source_url="https://x.com/other/status/102",
            ),
        ),
        next_token=None,
    )
    service = IngestionService(session_factory=session_factory, source=FakeXSource({None: page}))

    first = asyncio.run(service.sync_list(list_id))
    second = asyncio.run(service.sync_list(list_id))

    assert first.status == "succeeded"
    assert first.posts_created == 2
    assert second.posts_created == 0
    assert second.duplicates == 2
    with session_factory() as session:
        x_list = session.get(XList, list_id)
        assert x_list is not None
        assert x_list.latest_seen_post_id == "102"
        assert len(session.scalars(select(Post)).all()) == 2


def test_source_fetch_happens_outside_an_active_database_transaction() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        list_id = x_list.id

    active_transactions = 0

    def begin(*_args: object) -> None:
        nonlocal active_transactions
        active_transactions += 1

    def end(*_args: object) -> None:
        nonlocal active_transactions
        active_transactions -= 1

    event.listen(engine, "begin", begin)
    event.listen(engine, "commit", end)
    event.listen(engine, "rollback", end)

    class TransactionAwareSource:
        async def fetch_page(
            self, *, list_id: str, pagination_token: str | None, max_results: int
        ) -> SourcePage:
            assert list_id == "123456789"
            assert active_transactions == 0
            return SourcePage()

    result = asyncio.run(
        IngestionService(
            session_factory=session_factory, source=TransactionAwareSource()
        ).sync_list(list_id)
    )

    assert result.status == "succeeded"
    assert result.pages_fetched == 1
