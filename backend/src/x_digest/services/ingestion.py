import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from x_digest.models import Author, SyncRejection, SyncRun, XList
from x_digest.repositories.jobs import (
    acquire_sync_lease,
    advance_list_cursors,
    begin_sync_run,
    finish_sync_run,
    release_sync_lease,
    renew_sync_lease,
    reserve_sync_post_budget,
    settle_sync_post_budget,
)
from x_digest.repositories.posts import AuthorInput, PostInput, attach_to_list, upsert_post
from x_digest.services.oauth_client import OAuthClientError
from x_digest.sources.base import XSource
from x_digest.sources.errors import XSourceError
from x_digest.sources.types import RawPost

_SAFE_SOURCE_ERROR_CODES = frozenset(
    {
        "x_authorization_failed",
        "x_rate_limited",
        "x_upstream_unavailable",
        "x_response_invalid",
        "x_request_invalid",
    }
)
_SAFE_OAUTH_ERROR_CODES = frozenset(
    {
        "oauth_not_configured",
        "oauth_authorization_failed",
        "oauth_upstream_unavailable",
        "oauth_token_invalid",
    }
)
_GENERIC_FAILURE_CODE = "ingestion_failed"
STALE_RUNNING_RUN_THRESHOLD = timedelta(hours=1)
MAX_STALE_RUNS_PER_RECONCILIATION = 100

StopReason = Literal[
    "unchanged",
    "watermark_reached",
    "time_window_reached",
    "provider_exhausted",
    "budget_exhausted",
    "daily_budget_exhausted",
]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class SyncResult:
    run_id: str
    status: Literal["succeeded", "partial", "failed"]
    pages_fetched: int
    posts_seen: int
    posts_created: int
    duplicates: int
    rejected: int
    posts_returned: int
    truncated: bool
    stop_reason: StopReason


class IngestionService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        source: XSource,
        max_pages: int = 6,
        max_posts: int = 50,
        max_posts_per_day: int = 10_000,
        page_size: int = 10,
        overlap: timedelta = timedelta(minutes=10),
        backfill_window: timedelta = timedelta(hours=24),
        clock: Clock | None = None,
    ) -> None:
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        if max_posts <= 0:
            raise ValueError("max_posts must be positive")
        if max_posts_per_day <= 0:
            raise ValueError("max_posts_per_day must be positive")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        if overlap < timedelta(0):
            raise ValueError("overlap must not be negative")
        if backfill_window <= timedelta(0):
            raise ValueError("backfill_window must be positive")
        self._session_factory = session_factory
        self._source = source
        self._max_pages = max_pages
        self._max_posts = max_posts
        self._max_posts_per_day = max_posts_per_day
        self._page_size = page_size
        self._overlap = overlap
        self._backfill_window = backfill_window
        self._clock = clock or (lambda: datetime.now(UTC))

    async def sync_list(self, list_id: str) -> SyncResult:
        run_now = _as_utc(self._clock())
        with self._session_factory.begin() as session:
            lease_owner = acquire_sync_lease(
                session,
                list_id=list_id,
                acquired_at=run_now,
            )
        try:
            return await self._sync_list_locked(
                list_id,
                run_now=run_now,
                lease_owner=lease_owner,
            )
        finally:
            with self._session_factory.begin() as session:
                release_sync_lease(session, list_id=list_id, owner_id=lease_owner)

    async def _sync_list_locked(
        self, list_id: str, *, run_now: datetime, lease_owner: str
    ) -> SyncResult:
        with self._session_factory() as session:
            x_list = session.get(XList, list_id)
            if x_list is None:
                raise ValueError(f"Unknown List: {list_id}")
            platform_list_id = x_list.platform_list_id
            stored_head = x_list.latest_observed_post_id
            initialized = stored_head is not None
            day_start = run_now.replace(hour=0, minute=0, second=0, microsecond=0)
            posts_returned_today = session.scalar(
                select(func.coalesce(func.sum(SyncRun.posts_reserved), 0)).where(
                    SyncRun.list_id == list_id,
                    SyncRun.started_at >= day_start,
                    SyncRun.started_at < day_start + timedelta(days=1),
                )
            )
            daily_remaining = max(
                self._max_posts_per_day - int(posts_returned_today or 0), 0
            )
            recovery_cutoff = run_now - self._backfill_window
            watermark_cutoff = (
                _as_utc(x_list.latest_seen_at) - self._overlap
                if x_list.latest_seen_at is not None
                else None
            )
            if watermark_cutoff is not None and watermark_cutoff >= recovery_cutoff:
                effective_cutoff = watermark_cutoff
                cutoff_reason: StopReason = "watermark_reached"
            else:
                effective_cutoff = recovery_cutoff
                cutoff_reason = "time_window_reached"

        self._reconcile_stale_runs(list_id)
        with self._session_factory.begin() as session:
            sync_run = begin_sync_run(session, list_id=list_id, started_at=run_now)
            run_id = sync_run.id

        result = _MutableSyncResult(run_id=run_id)
        if daily_remaining == 0:
            result.stop_reason = "daily_budget_exhausted"
            return self._complete_run(list_id, result)
        effective_max_posts = min(self._max_posts, daily_remaining)
        minimum_results = getattr(self._source, "minimum_results", 1)
        if effective_max_posts < minimum_results:
            result.stop_reason = "daily_budget_exhausted"
            return self._complete_run(list_id, result)
        pagination_token: str | None = None
        try:
            while (
                result.pages_fetched < self._max_pages
                and result.posts_returned < effective_max_posts
            ):
                first_page = result.pages_fetched == 0
                request_size = min(
                    self._page_size,
                    effective_max_posts - result.posts_returned,
                )
                if request_size < minimum_results:
                    result.stop_reason = "budget_exhausted"
                    result.truncated = True
                    break
                with self._session_factory.begin() as session:
                    renew_sync_lease(
                        session,
                        list_id=list_id,
                        owner_id=lease_owner,
                        renewed_at=_as_utc(self._clock()),
                    )
                    reserved_request_size = reserve_sync_post_budget(
                        session,
                        list_id=list_id,
                        sync_run_id=run_id,
                        day_start=day_start,
                        requested=request_size,
                        minimum=minimum_results,
                        daily_limit=self._max_posts_per_day,
                    )
                if reserved_request_size == 0:
                    result.stop_reason = "daily_budget_exhausted"
                    break
                page = await self._source.fetch_page(
                    list_id=platform_list_id,
                    pagination_token=pagination_token,
                    max_results=reserved_request_size,
                    start_time=effective_cutoff,
                )
                returned_count = (
                    page.returned_count
                    if page.returned_count is not None
                    else len(page.posts) + len(page.rejected_items)
                )
                if returned_count < 0:
                    raise ValueError("Source returned a negative item count")
                # Provider reads are incurred before the page transaction commits, so retain
                # this accounting even if persistence fails and the run becomes failed.
                result.posts_returned += returned_count
                crossed_effective_cutoff = any(
                    _as_utc(post.created_at) < effective_cutoff for post in page.posts
                )
                with self._session_factory.begin() as session:
                    x_list = session.get(XList, list_id)
                    if x_list is None:  # pragma: no cover - list deletion is externally coordinated
                        raise ValueError(f"Unknown List: {list_id}")
                    page_result = _MutableSyncResult(
                        run_id=run_id,
                        pages_fetched=1,
                        latest_observed_post_id=page.head_post_id if first_page else None,
                    )
                    page_result.rejected = len(page.rejected_items)
                    if page_result.rejected:
                        page_result.status = "partial"
                    for raw_post in page.posts:
                        if _as_utc(raw_post.created_at) >= effective_cutoff:
                            self._store_post(session, x_list, raw_post, page_result)
                    candidate = _merge_results(result, page_result)
                    durable_run = session.get(SyncRun, run_id)
                    if durable_run is not None:  # pragma: no branch - the run was just created
                        settle_sync_post_budget(
                            session,
                            sync_run=durable_run,
                            reserved=reserved_request_size,
                            returned=returned_count,
                        )
                        for rejected in page.rejected_items:
                            session.add(
                                SyncRejection(
                                    sync_run_id=run_id,
                                    item_identifier=rejected.identifier,
                                    reason=rejected.reason,
                                )
                            )
                        self._record_counts(durable_run, candidate)
                result = candidate
                if first_page and initialized and page.head_post_id == stored_head:
                    result.stop_reason = "unchanged"
                elif crossed_effective_cutoff:
                    result.stop_reason = cutoff_reason
                elif page.next_token is None or returned_count == 0:
                    result.stop_reason = "provider_exhausted"
                elif (
                    result.posts_returned >= effective_max_posts
                    or result.pages_fetched >= self._max_pages
                ):
                    result.stop_reason = "budget_exhausted"
                    result.truncated = True
                else:
                    pagination_token = page.next_token
                    continue
                break
        except asyncio.CancelledError:
            self._best_effort_finish_failed(result, error_code=_GENERIC_FAILURE_CODE)
            raise
        except Exception as error:
            self._best_effort_finish_failed(result, error_code=_safe_failure_code(error))
            raise

        try:
            return self._complete_run(list_id, result)
        except asyncio.CancelledError:
            self._best_effort_finish_failed(result, error_code=_GENERIC_FAILURE_CODE)
            raise
        except Exception as error:
            self._best_effort_finish_failed(result, error_code=_safe_failure_code(error))
            raise

    def _complete_run(self, list_id: str, result: "_MutableSyncResult") -> SyncResult:
        with self._session_factory.begin() as session:
            x_list = session.get(XList, list_id)
            durable_run = session.get(SyncRun, result.run_id)
            if x_list is None or durable_run is None:
                raise ValueError(f"Unknown List: {list_id}")
            if result.stop_reason is None:
                raise ValueError("Sync run completed without a stop reason")
            finish_sync_run(session, sync_run=durable_run, status=result.status)
            advance_list_cursors(
                session,
                x_list=x_list,
                sync_run=durable_run,
                latest_observed_post_id=result.latest_observed_post_id,
                latest_seen_at=result.latest_seen_at,
                latest_seen_post_id=result.latest_seen_post_id,
            )
            self._record_counts(durable_run, result)
            return SyncResult(
                run_id=result.run_id,
                status=result.status,
                pages_fetched=result.pages_fetched,
                posts_seen=result.posts_seen,
                posts_created=result.posts_created,
                duplicates=result.duplicates,
                rejected=result.rejected,
                posts_returned=result.posts_returned,
                truncated=result.truncated,
                stop_reason=result.stop_reason,
            )

    def _reconcile_stale_runs(self, list_id: str) -> None:
        cutoff = datetime.now() - STALE_RUNNING_RUN_THRESHOLD
        with self._session_factory.begin() as session:
            stale_runs = session.scalars(
                select(SyncRun).where(
                    SyncRun.list_id == list_id,
                    SyncRun.status == "running",
                    SyncRun.started_at <= cutoff,
                )
                .order_by(SyncRun.started_at, SyncRun.id)
                .limit(MAX_STALE_RUNS_PER_RECONCILIATION)
            ).all()
            for sync_run in stale_runs:
                finish_sync_run(session, sync_run=sync_run, status="failed")
                sync_run.error_code = _GENERIC_FAILURE_CODE

    def _best_effort_finish_failed(
        self, result: "_MutableSyncResult", *, error_code: str
    ) -> None:
        try:
            self._finish_failed_run(result, error_code=error_code)
        except BaseException:
            return

    def _finish_failed_run(self, result: "_MutableSyncResult", *, error_code: str) -> None:
        with self._session_factory.begin() as session:
            sync_run = session.get(SyncRun, result.run_id)
            if sync_run is None:  # pragma: no cover - run is created before fetching pages
                return
            finish_sync_run(session, sync_run=sync_run, status="failed")
            sync_run.error_code = error_code
            self._record_counts(sync_run, result)
            sync_run.truncated = False
            sync_run.stop_reason = None

    @staticmethod
    def _record_counts(sync_run: SyncRun, result: "_MutableSyncResult") -> None:
        sync_run.pages_fetched = result.pages_fetched
        sync_run.posts_seen = result.posts_seen
        sync_run.posts_created = result.posts_created
        sync_run.duplicates = result.duplicates
        sync_run.rejected = result.rejected
        sync_run.posts_returned = result.posts_returned
        sync_run.truncated = result.truncated
        sync_run.stop_reason = result.stop_reason

    @staticmethod
    def _store_post(
        session: Session, x_list: XList, raw_post: RawPost, result: "_MutableSyncResult"
    ) -> None:
        cached_author = session.scalar(
            select(Author).where(Author.platform_author_id == raw_post.author.id)
        )
        username = raw_post.author.username or (
            cached_author.username if cached_author is not None else raw_post.author.id
        )
        display_name = (
            raw_post.author.display_name
            if raw_post.author.display_name is not None
            else cached_author.display_name if cached_author is not None else None
        )
        post, created = upsert_post(
            session,
            PostInput(
                platform_post_id=raw_post.id,
                author=AuthorInput(
                    platform_author_id=raw_post.author.id,
                    username=username,
                    display_name=display_name,
                ),
                text=raw_post.text,
                created_at=raw_post.created_at,
                source_url=raw_post.source_url,
                conversation_id=raw_post.conversation_id,
                in_reply_to_post_id=raw_post.in_reply_to_post_id,
                references_json=raw_post.references,
                entities_json=raw_post.entities,
                media_json=raw_post.media,
            ),
        )
        attach_to_list(session, post.id, x_list.id)
        result.posts_seen += 1
        if created:
            result.posts_created += 1
        else:
            result.duplicates += 1
        if result.latest_seen_at is None or (raw_post.created_at, raw_post.id) > (
            result.latest_seen_at,
            result.latest_seen_post_id or "",
        ):
            result.latest_seen_at = raw_post.created_at
            result.latest_seen_post_id = raw_post.id


@dataclass
class _MutableSyncResult:
    run_id: str
    status: Literal["succeeded", "partial", "failed"] = "succeeded"
    pages_fetched: int = 0
    posts_seen: int = 0
    posts_created: int = 0
    duplicates: int = 0
    rejected: int = 0
    posts_returned: int = 0
    truncated: bool = False
    stop_reason: StopReason | None = None
    latest_observed_post_id: str | None = None
    latest_seen_at: datetime | None = None
    latest_seen_post_id: str | None = None


def _merge_results(result: _MutableSyncResult, page: _MutableSyncResult) -> _MutableSyncResult:
    latest_seen_at = result.latest_seen_at
    latest_seen_post_id = result.latest_seen_post_id
    if page.latest_seen_at is not None and (
        latest_seen_at is None
        or (page.latest_seen_at, page.latest_seen_post_id or "")
        > (latest_seen_at, latest_seen_post_id or "")
    ):
        latest_seen_at = page.latest_seen_at
        latest_seen_post_id = page.latest_seen_post_id
    return _MutableSyncResult(
        run_id=result.run_id,
        status="partial" if "partial" in {result.status, page.status} else "succeeded",
        pages_fetched=result.pages_fetched + page.pages_fetched,
        posts_seen=result.posts_seen + page.posts_seen,
        posts_created=result.posts_created + page.posts_created,
        duplicates=result.duplicates + page.duplicates,
        rejected=result.rejected + page.rejected,
        posts_returned=result.posts_returned + page.posts_returned,
        truncated=result.truncated or page.truncated,
        stop_reason=page.stop_reason or result.stop_reason,
        latest_observed_post_id=(
            page.latest_observed_post_id or result.latest_observed_post_id
        ),
        latest_seen_at=latest_seen_at,
        latest_seen_post_id=latest_seen_post_id,
    )


def _safe_failure_code(error: Exception) -> str:
    if isinstance(error, XSourceError) and error.code in _SAFE_SOURCE_ERROR_CODES:
        return error.code
    if isinstance(error, OAuthClientError) and error.code in _SAFE_OAUTH_ERROR_CODES:
        return error.code
    return _GENERIC_FAILURE_CODE


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
