import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import sessionmaker

from x_digest.models import Base, Post, PostListMembership, SyncRun, XList
from x_digest.repositories.lists import upsert_list
from x_digest.services.ingestion import IngestionService
from x_digest.services.oauth_client import OAuthClientError
from x_digest.sources.errors import AuthenticationError, RateLimitError, UpstreamError
from x_digest.sources.fake import FakeXSource
from x_digest.sources.types import RawAuthor, RawPost, RejectedItem, SourcePage


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


def test_cancelled_source_finalizes_the_durable_run_as_failed_without_watermark() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        list_id = x_list.id

    class CancelledSource:
        async def fetch_page(
            self, *, list_id: str, pagination_token: str | None, max_results: int
        ) -> SourcePage:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            IngestionService(
                session_factory=session_factory, source=CancelledSource()
            ).sync_list(list_id)
        )

    with session_factory() as session:
        run = session.scalar(select(SyncRun))
        x_list = session.get(XList, list_id)
        assert run is not None
        assert x_list is not None
        assert run.status == "failed"
        assert run.finished_at is not None
        assert run.error_code == "ingestion_failed"
        assert x_list.latest_seen_at is None


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (AuthenticationError(), "x_authorization_failed"),
        (RateLimitError(), "x_rate_limited"),
        (UpstreamError(), "x_upstream_unavailable"),
        (
            OAuthClientError(503, "oauth_upstream_unavailable", "provider body test-access-token"),
            "oauth_upstream_unavailable",
        ),
        (RuntimeError("provider body test-access-token"), "ingestion_failed"),
    ],
)
def test_failed_source_persists_only_safe_error_codes(
    error: Exception, expected_code: str
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        list_id = x_list.id

    class FailingSource:
        async def fetch_page(
            self, *, list_id: str, pagination_token: str | None, max_results: int
        ) -> SourcePage:
            raise error

    with pytest.raises(type(error)):
        asyncio.run(
            IngestionService(
                session_factory=session_factory, source=FailingSource()
            ).sync_list(list_id)
        )

    with session_factory() as session:
        run = session.scalar(select(SyncRun))
        assert run is not None
        assert run.status == "failed"
        assert run.finished_at is not None
        assert run.error_code == expected_code
        assert "test-access-token" not in str(run.debug_metadata)


def test_rolled_back_page_does_not_inflate_durable_run_counts() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        list_id = x_list.id

    commits = 0

    def fail_page_commit(*_args: object) -> None:
        nonlocal commits
        commits += 1
        if commits == 3:
            raise RuntimeError("forced page commit rollback")

    event.listen(engine, "commit", fail_page_commit)
    source = FakeXSource(
        {
            None: SourcePage(
                posts=(
                    RawPost(
                        id="101",
                        author=RawAuthor(id="author-1", username="author"),
                        text="first post",
                        created_at=now,
                        source_url="https://x.com/author/status/101",
                    ),
                )
            )
        }
    )
    try:
        with pytest.raises(RuntimeError, match="forced page commit rollback"):
            asyncio.run(
                IngestionService(session_factory=session_factory, source=source).sync_list(list_id)
            )
    finally:
        event.remove(engine, "commit", fail_page_commit)

    with session_factory() as session:
        run = session.scalar(select(SyncRun))
        x_list = session.get(XList, list_id)
        assert run is not None
        assert x_list is not None
        assert run.status == "failed"
        assert (run.pages_fetched, run.posts_seen, run.posts_created, run.duplicates) == (
            0,
            0,
            0,
            0,
        )
        assert x_list.latest_seen_at is None
        assert session.scalars(select(Post)).all() == []


def test_rejected_page_is_partial_and_does_not_advance_the_watermark() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        list_id = x_list.id

    source = FakeXSource(
        {
            None: SourcePage(
                posts=(
                    RawPost(
                        id="101",
                        author=RawAuthor(id="author-1", username="author"),
                        text="first post",
                        created_at=now,
                        source_url="https://x.com/author/status/101",
                    ),
                ),
                rejected_items=(RejectedItem(identifier="102", reason="provider_rejected"),),
            )
        }
    )

    result = asyncio.run(
        IngestionService(session_factory=session_factory, source=source).sync_list(list_id)
    )

    with session_factory() as session:
        run = session.scalar(select(SyncRun))
        x_list = session.get(XList, list_id)
        assert run is not None
        assert x_list is not None
        assert result.status == "partial"
        assert result.rejected == 1
        assert run.status == "partial"
        assert run.rejected == 1
        assert x_list.latest_seen_at is None


def test_final_completion_commit_failure_marks_the_durable_run_failed() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        list_id = x_list.id

    def fail_final_completion(session: object) -> None:
        dirty = getattr(session, "dirty")
        if any(
            isinstance(value, SyncRun) and value.status in {"succeeded", "partial"}
            for value in dirty
        ):
            raise RuntimeError("forced final completion rollback")

    event.listen(session_factory.class_, "before_commit", fail_final_completion)
    source = FakeXSource({None: _single_post_page(now)})
    try:
        with pytest.raises(RuntimeError, match="forced final completion rollback"):
            asyncio.run(
                IngestionService(
                    session_factory=session_factory, source=source
                ).sync_list(list_id)
            )
    finally:
        event.remove(session_factory.class_, "before_commit", fail_final_completion)

    with session_factory() as session:
        run = session.scalar(select(SyncRun))
        x_list = session.get(XList, list_id)
        assert run is not None
        assert x_list is not None
        assert run.status == "failed"
        assert run.finished_at is not None
        assert run.error_code == "ingestion_failed"
        assert (run.pages_fetched, run.posts_created) == (1, 1)
        assert x_list.latest_seen_at is None


def test_missing_list_during_completion_marks_the_durable_run_failed() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        list_id = x_list.id

    class DeletingCompletionService(IngestionService):
        def _complete_run(self, list_id: str, result: object) -> object:
            with session_factory.begin() as session:
                x_list = session.get(XList, list_id)
                assert x_list is not None
                session.execute(
                    delete(PostListMembership).where(PostListMembership.list_id == list_id)
                )
                session.delete(x_list)
            return super()._complete_run(list_id, result)

    with pytest.raises(ValueError, match="Unknown List"):
        asyncio.run(
            DeletingCompletionService(
                session_factory=session_factory, source=FakeXSource({None: _single_post_page(now)})
            ).sync_list(list_id)
        )

    with session_factory() as session:
        run = session.scalar(select(SyncRun))
        assert run is not None
        assert run.status == "failed"
        assert run.finished_at is not None
        assert run.error_code == "ingestion_failed"


def test_cancelled_completion_marks_the_durable_run_failed_and_reraises() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        list_id = x_list.id

    class CancelledCompletionService(IngestionService):
        def _complete_run(self, list_id: str, result: object) -> object:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            CancelledCompletionService(
                session_factory=session_factory, source=FakeXSource({None: _single_post_page(now)})
            ).sync_list(list_id)
        )

    with session_factory() as session:
        run = session.scalar(select(SyncRun))
        assert run is not None
        assert run.status == "failed"
        assert run.finished_at is not None
        assert run.error_code == "ingestion_failed"


def test_later_same_list_sync_reconciles_only_stale_running_runs_after_cleanup_failure() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        list_id = x_list.id

    def fail_completion_and_cleanup(session: object) -> None:
        dirty = getattr(session, "dirty")
        if any(
            isinstance(value, SyncRun) and value.status in {"succeeded", "partial", "failed"}
            for value in dirty
        ):
            raise RuntimeError("forced completion and cleanup rollback")

    event.listen(session_factory.class_, "before_commit", fail_completion_and_cleanup)
    try:
        with pytest.raises(RuntimeError, match="forced completion and cleanup rollback"):
            asyncio.run(
                IngestionService(
                    session_factory=session_factory,
                    source=FakeXSource({None: _single_post_page(now)}),
                ).sync_list(list_id)
            )
    finally:
        event.remove(session_factory.class_, "before_commit", fail_completion_and_cleanup)

    with session_factory.begin() as session:
        abandoned = session.scalar(select(SyncRun))
        assert abandoned is not None
        assert abandoned.status == "running"
        abandoned_id = abandoned.id
        abandoned.started_at = datetime.now() - timedelta(hours=2)
        active = SyncRun(list_id=list_id, status="running", started_at=datetime.now())
        session.add(active)
        session.flush()
        active_id = active.id

    asyncio.run(
        IngestionService(
            session_factory=session_factory, source=FakeXSource({None: SourcePage()})
        ).sync_list(list_id)
    )

    with session_factory() as session:
        abandoned = session.get(SyncRun, abandoned_id)
        active = session.get(SyncRun, active_id)
        assert abandoned is not None
        assert active is not None
        assert abandoned.status == "failed"
        assert abandoned.error_code == "ingestion_failed"
        assert active.status == "running"


def _single_post_page(now: datetime) -> SourcePage:
    return SourcePage(
        posts=(
            RawPost(
                id="101",
                author=RawAuthor(id="author-1", username="author"),
                text="first post",
                created_at=now,
                source_url="https://x.com/author/status/101",
            ),
        )
    )
