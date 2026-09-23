import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import Session, sessionmaker

from x_digest.models import Base, Post, PostListMembership, SyncRejection, SyncRun, XList
from x_digest.repositories.lists import upsert_list
from x_digest.services.ingestion import IngestionService
from x_digest.services.oauth_client import OAuthClientError
from x_digest.sources.errors import AuthenticationError, RateLimitError, UpstreamError
from x_digest.sources.fake import FakeXSource
from x_digest.sources.types import RawAuthor, RawPost, RejectedItem, SourcePage

NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


def _page(
    *posts: RawPost,
    next_token: str | None = None,
    rejected_items: tuple[RejectedItem, ...] = (),
    returned_count: int | None = None,
    head_post_id: str | None = None,
) -> SourcePage:
    return SourcePage(
        posts=posts,
        rejected_items=rejected_items,
        next_token=next_token,
        returned_count=(
            len(posts) + len(rejected_items) if returned_count is None else returned_count
        ),
        head_post_id=head_post_id or (posts[0].id if posts else None),
    )


def _new_list(
    *,
    observed_head: str | None = None,
    latest_seen_at: datetime | None = None,
) -> tuple[sessionmaker[Session], str]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        x_list.latest_observed_post_id = observed_head
        x_list.latest_seen_at = latest_seen_at
        x_list.latest_seen_post_id = "stored-watermark" if latest_seen_at else None
        list_id = x_list.id
    return factory, list_id


def _service(
    factory: sessionmaker[Session],
    source: FakeXSource,
    *,
    max_pages: int = 6,
    max_posts: int = 50,
    page_size: int = 10,
) -> IngestionService:
    return IngestionService(
        session_factory=factory,
        source=source,
        max_pages=max_pages,
        max_posts=max_posts,
        page_size=page_size,
        overlap=timedelta(minutes=10),
        backfill_window=timedelta(hours=24),
        clock=lambda: NOW,
    )


def _stored_platform_ids(factory: sessionmaker[Session]) -> set[str]:
    with factory() as session:
        return {post.platform_post_id for post in session.scalars(select(Post))}


def test_initialized_unchanged_list_stops_after_one_result_probe() -> None:
    factory, list_id = _new_list(
        observed_head="head", latest_seen_at=NOW - timedelta(hours=1)
    )
    source = FakeXSource({None: _page(_raw_post("head", NOW), head_post_id="head")})
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    assert [call[2] for call in source.calls] == [1]
    assert result.posts_returned == 1
    assert result.stop_reason == "unchanged"
    assert result.truncated is False


def test_uninitialized_list_starts_with_ten_results_and_skips_old_posts() -> None:
    factory, list_id = _new_list()
    source = FakeXSource(
        {
            None: _page(
                _raw_post("new", NOW - timedelta(hours=1)),
                _raw_post("old", NOW - timedelta(hours=25)),
                head_post_id="new",
            )
        }
    )
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    assert [call[2] for call in source.calls] == [10]
    assert result.stop_reason == "time_window_reached"
    assert _stored_platform_ids(factory) == {"new"}


def test_incremental_thirty_posts_returns_at_most_forty_one_items() -> None:
    factory, list_id = _new_list(
        observed_head="old-head", latest_seen_at=NOW - timedelta(hours=2)
    )
    new_posts = tuple(
        _raw_post(f"new-{index:02d}", NOW - timedelta(minutes=index))
        for index in range(30)
    )
    old = _raw_post("old", NOW - timedelta(hours=3))
    source = FakeXSource(
        {
            None: _page(new_posts[0], next_token="p1", returned_count=1),
            "p1": _page(*new_posts[1:11], next_token="p2", returned_count=10),
            "p2": _page(*new_posts[11:21], next_token="p3", returned_count=10),
            "p3": _page(*new_posts[21:30], old, next_token="p4", returned_count=10),
        }
    )
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    assert [call[2] for call in source.calls] == [1, 10, 10, 10]
    assert result.posts_returned == 31
    assert result.posts_returned <= 41
    assert result.truncated is False
    assert result.stop_reason == "watermark_reached"


def test_remaining_budget_reduces_requested_page_size() -> None:
    factory, list_id = _new_list()
    source = FakeXSource(
        {None: _page(_raw_post("head", NOW), next_token="p2", returned_count=7)}
    )
    service = _service(factory, source, max_posts=7, page_size=10)
    asyncio.run(service.sync_list(list_id))

    assert [call[2] for call in source.calls] == [7]


def test_mixed_page_crossing_recent_watermark_does_not_fetch_next_page() -> None:
    factory, list_id = _new_list(latest_seen_at=NOW - timedelta(minutes=30))
    source = FakeXSource(
        {
            None: _page(
                _raw_post("new", NOW),
                _raw_post("old", NOW - timedelta(minutes=41)),
                next_token="p2",
            ),
            "p2": _page(_raw_post("must-not-fetch", NOW - timedelta(hours=1))),
        }
    )
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    assert [call[1] for call in source.calls] == [None]
    assert result.stop_reason == "watermark_reached"


def test_naive_watermark_is_normalized_against_aware_clock() -> None:
    factory, list_id = _new_list(
        latest_seen_at=(NOW - timedelta(minutes=30)).replace(tzinfo=None)
    )
    source = FakeXSource({None: _page(_raw_post("old", NOW - timedelta(minutes=41)))})
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    assert result.stop_reason == "watermark_reached"


def test_repeated_pagination_token_stops_at_page_limit_as_truncated() -> None:
    factory, list_id = _new_list()
    source = FakeXSource(
        {
            None: _page(_raw_post("head", NOW), next_token="repeat"),
            "repeat": _page(_raw_post("again", NOW), next_token="repeat"),
        }
    )
    result = asyncio.run(_service(factory, source, max_pages=3).sync_list(list_id))

    assert result.pages_fetched == 3
    assert result.truncated is True
    assert result.stop_reason == "budget_exhausted"


def test_rejected_only_pages_with_next_tokens_are_bounded() -> None:
    factory, list_id = _new_list()
    rejected = (RejectedItem(identifier="bad", reason="invalid_item"),)
    source = FakeXSource(
        {
            None: _page(rejected_items=rejected, next_token="p2", returned_count=1),
            "p2": _page(rejected_items=rejected, next_token="p3", returned_count=1),
            "p3": _page(rejected_items=rejected, next_token="p4", returned_count=1),
        }
    )
    result = asyncio.run(_service(factory, source, max_pages=3).sync_list(list_id))

    assert result.pages_fetched == 3
    assert result.truncated is True


def test_empty_page_with_next_token_stops_as_provider_exhausted() -> None:
    factory, list_id = _new_list()
    source = FakeXSource({None: _page(next_token="p2", returned_count=0)})
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    assert len(source.calls) == 1
    assert result.stop_reason == "provider_exhausted"
    assert result.truncated is False


def test_budget_truncation_advances_head_and_next_run_uses_unchanged_probe() -> None:
    factory, list_id = _new_list()
    pages: dict[str | None, SourcePage] = {}
    for page_index in range(5):
        token = None if page_index == 0 else f"p{page_index}"
        next_token = f"p{page_index + 1}"
        posts = tuple(
            _raw_post(
                f"post-{page_index * 10 + offset:02d}",
                NOW - timedelta(minutes=page_index * 10 + offset),
            )
            for offset in range(10)
        )
        pages[token] = _page(*posts, next_token=next_token, returned_count=10)
    source = FakeXSource(pages)
    first = asyncio.run(_service(factory, source).sync_list(list_id))

    source.pages = {
        None: _page(
            _raw_post("post-00", NOW), returned_count=1, head_post_id="post-00"
        )
    }
    second = asyncio.run(_service(factory, source).sync_list(list_id))

    assert first.truncated is True
    assert first.stop_reason == "budget_exhausted"
    assert second.stop_reason == "unchanged"
    assert second.posts_returned == 1


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


def test_sync_stops_when_any_valid_post_crosses_watermark_overlap() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        x_list.latest_seen_at = now
        x_list.latest_seen_post_id = "watermark"
        list_id = x_list.id

    source = FakeXSource(
        {
            None: SourcePage(
                posts=(_raw_post("new", now + timedelta(minutes=1)),), next_token="p2"
            ),
            "p2": SourcePage(
                posts=(_raw_post("overlap", now - timedelta(minutes=11)),),
                next_token="p3",
            ),
            "p3": SourcePage(posts=(_raw_post("too-old", now - timedelta(days=1)),)),
        }
    )

    result = asyncio.run(
        IngestionService(
            session_factory=session_factory,
            source=source,
            overlap=timedelta(minutes=10),
        ).sync_list(list_id)
    )

    assert result.pages_fetched == 2
    assert [call[1] for call in source.calls] == [None, "p2"]
    with session_factory() as session:
        assert {post.platform_post_id for post in session.scalars(select(Post))} == {"new"}


def test_rejected_only_page_does_not_trigger_watermark_stop() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        x_list.latest_seen_at = now
        list_id = x_list.id

    source = FakeXSource(
        {
            None: SourcePage(
                rejected_items=(RejectedItem(identifier="hidden", reason="provider_rejected"),),
                next_token="p2",
            ),
            "p2": SourcePage(posts=(_raw_post("old", now - timedelta(hours=1)),)),
        }
    )

    result = asyncio.run(
        IngestionService(
            session_factory=session_factory,
            source=source,
            overlap=timedelta(minutes=10),
        ).sync_list(list_id)
    )

    assert result.pages_fetched == 2
    assert [call[1] for call in source.calls] == [None, "p2"]


def test_old_overlap_page_does_not_move_watermark_backwards() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        x_list.latest_seen_at = now
        x_list.latest_seen_post_id = "watermark"
        list_id = x_list.id

    result = asyncio.run(
        IngestionService(
            session_factory=session_factory,
            source=FakeXSource(
                {None: SourcePage(posts=(_raw_post("old", now - timedelta(hours=1)),))}
            ),
        ).sync_list(list_id)
    )

    assert result.status == "succeeded"
    with session_factory() as session:
        x_list = session.get(XList, list_id)
        assert x_list is not None
        assert x_list.latest_seen_post_id == "watermark"


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


def test_cancelled_second_page_retains_rows_but_advances_no_cursors() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        list_id = x_list.id

    class CancelledSource:
        calls = 0

        async def fetch_page(
            self, *, list_id: str, pagination_token: str | None, max_results: int
        ) -> SourcePage:
            self.calls += 1
            if self.calls == 1:
                return _page(
                    _raw_post("committed", NOW),
                    next_token="p2",
                    head_post_id="committed",
                )
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
        assert run.posts_created == 1
        assert run.truncated is False
        assert run.stop_reason is None
        assert x_list.latest_observed_post_id is None
        assert x_list.latest_seen_at is None
        assert x_list.latest_seen_post_id is None
        assert {post.platform_post_id for post in session.scalars(select(Post))} == {
            "committed"
        }


def test_second_page_failure_retains_rows_but_advances_no_cursors() -> None:
    factory, list_id = _new_list()

    class FailingSecondPageSource:
        calls = 0

        async def fetch_page(
            self, *, list_id: str, pagination_token: str | None, max_results: int
        ) -> SourcePage:
            self.calls += 1
            if self.calls == 1:
                return _page(
                    _raw_post("committed", NOW),
                    next_token="p2",
                    head_post_id="committed",
                )
            raise UpstreamError()

    with pytest.raises(UpstreamError):
        asyncio.run(
            IngestionService(
                session_factory=factory,
                source=FailingSecondPageSource(),
                clock=lambda: NOW,
            ).sync_list(list_id)
        )

    with factory() as session:
        run = session.scalar(select(SyncRun))
        x_list = session.get(XList, list_id)
        assert run is not None
        assert x_list is not None
        assert run.status == "failed"
        assert run.posts_created == 1
        assert run.posts_returned == 1
        assert run.truncated is False
        assert run.stop_reason is None
        assert x_list.latest_observed_post_id is None
        assert x_list.latest_seen_at is None
        assert x_list.latest_seen_post_id is None
        assert {post.platform_post_id for post in session.scalars(select(Post))} == {
            "committed"
        }


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
        assert run.posts_returned == 1
        assert x_list.latest_seen_at is None
        assert session.scalars(select(Post)).all() == []


def test_rejected_page_is_partial_records_rejection_and_advances_cursors() -> None:
    factory, list_id = _new_list()
    source = FakeXSource(
        {
            None: _page(
                _raw_post("101", NOW),
                rejected_items=(RejectedItem(identifier="102", reason="provider_rejected"),),
                returned_count=2,
                head_post_id="101",
            )
        }
    )
    result = asyncio.run(_service(factory, source).sync_list(list_id))

    with factory() as session:
        run = session.scalar(select(SyncRun))
        rejection = session.scalar(select(SyncRejection))
        x_list = session.get(XList, list_id)
        assert run is not None
        assert rejection is not None
        assert x_list is not None
        assert result.status == "partial"
        assert result.rejected == 1
        assert (rejection.item_identifier, rejection.reason) == (
            "102",
            "provider_rejected",
        )
        assert x_list.latest_observed_post_id == "101"
        assert x_list.latest_seen_post_id == "101"


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
        assert run.truncated is False
        assert run.stop_reason is None
        assert x_list.latest_observed_post_id is None
        assert x_list.latest_seen_at is None
        assert x_list.latest_seen_post_id is None


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


def test_stale_run_reconciliation_selects_one_deterministic_oldest_batch() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    now = datetime.now()
    stale_batch_limit = 100
    with session_factory.begin() as session:
        x_list = upsert_list(session, platform_list_id="123456789", name="AI news")
        list_id = x_list.id
        older_ids = [f"older-{index:03d}" for index in range(stale_batch_limit - 1)]
        session.add_all(
            SyncRun(
                id=run_id,
                list_id=list_id,
                status="running",
                started_at=now - timedelta(hours=3),
            )
            for run_id in older_ids
        )
        session.add_all(
            SyncRun(
                id=run_id,
                list_id=list_id,
                status="running",
                started_at=now - timedelta(hours=2),
            )
            for run_id in ("boundary-c", "boundary-b", "boundary-a")
        )
        session.add(
            SyncRun(id="recent", list_id=list_id, status="running", started_at=now)
        )

    asyncio.run(
        IngestionService(
            session_factory=session_factory, source=FakeXSource({None: SourcePage()})
        ).sync_list(list_id)
    )

    initial_ids = [*older_ids, "boundary-a", "boundary-b", "boundary-c", "recent"]
    with session_factory() as session:
        runs = {
            run.id: run
            for run in session.scalars(select(SyncRun).where(SyncRun.id.in_(initial_ids)))
        }

    assert all(runs[run_id].status == "failed" for run_id in older_ids)
    assert runs["boundary-a"].status == "failed"
    assert runs["boundary-a"].error_code == "ingestion_failed"
    assert runs["boundary-b"].status == "running"
    assert runs["boundary-c"].status == "running"
    assert runs["recent"].status == "running"


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


def _raw_post(post_id: str, created_at: datetime) -> RawPost:
    return RawPost(
        id=post_id,
        author=RawAuthor(id=f"author-{post_id}", username=f"author_{post_id}"),
        text=f"post {post_id}",
        created_at=created_at,
        source_url=f"https://x.com/author_{post_id}/status/{post_id}",
    )
