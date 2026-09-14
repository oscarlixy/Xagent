import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from x_digest.models import SyncRun, XList
from x_digest.repositories.jobs import advance_list_watermark, begin_sync_run, finish_sync_run
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


@dataclass(frozen=True)
class SyncResult:
    run_id: str
    status: Literal["succeeded", "partial", "failed"]
    pages_fetched: int
    posts_seen: int
    posts_created: int
    duplicates: int
    rejected: int


class IngestionService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        source: XSource,
        max_pages: int = 5,
        max_posts: int = 500,
    ) -> None:
        self._session_factory = session_factory
        self._source = source
        self._max_pages = max_pages
        self._max_posts = max_posts

    async def sync_list(self, list_id: str) -> SyncResult:
        with self._session_factory() as session:
            x_list = session.get(XList, list_id)
            if x_list is None:
                raise ValueError(f"Unknown List: {list_id}")
            platform_list_id = x_list.platform_list_id

        self._reconcile_stale_runs(list_id)
        with self._session_factory.begin() as session:
            sync_run = begin_sync_run(session, list_id=list_id)
            run_id = sync_run.id

        result = _MutableSyncResult(run_id=run_id)
        pagination_token: str | None = None
        try:
            while result.pages_fetched < self._max_pages and result.posts_seen < self._max_posts:
                page = await self._source.fetch_page(
                    list_id=platform_list_id,
                    pagination_token=pagination_token,
                    max_results=min(100, self._max_posts - result.posts_seen),
                )
                with self._session_factory.begin() as session:
                    x_list = session.get(XList, list_id)
                    if x_list is None:  # pragma: no cover - list deletion is externally coordinated
                        raise ValueError(f"Unknown List: {list_id}")
                    page_result = _MutableSyncResult(run_id=run_id, pages_fetched=1)
                    page_result.rejected = len(page.rejected_items)
                    if page_result.rejected:
                        page_result.status = "partial"
                    for raw_post in page.posts:
                        if result.posts_seen + page_result.posts_seen >= self._max_posts:
                            break
                        self._store_post(session, x_list, raw_post, page_result)
                    candidate = _merge_results(result, page_result)
                    durable_run = session.get(SyncRun, run_id)
                    if durable_run is not None:  # pragma: no branch - the run was just created
                        self._record_counts(durable_run, candidate)
                result = candidate
                if page.next_token is None:
                    break
                pagination_token = page.next_token
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
            finish_sync_run(session, sync_run=durable_run, status=result.status)
            if result.status == "succeeded" and result.latest_seen_at is not None:
                advance_list_watermark(
                    session,
                    x_list=x_list,
                    sync_run=durable_run,
                    latest_seen_at=result.latest_seen_at,
                    latest_seen_post_id=result.latest_seen_post_id or "",
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

    @staticmethod
    def _record_counts(sync_run: SyncRun, result: "_MutableSyncResult") -> None:
        sync_run.pages_fetched = result.pages_fetched
        sync_run.posts_seen = result.posts_seen
        sync_run.posts_created = result.posts_created
        sync_run.duplicates = result.duplicates
        sync_run.rejected = result.rejected

    @staticmethod
    def _store_post(
        session: Session, x_list: XList, raw_post: RawPost, result: "_MutableSyncResult") -> None:
        post, created = upsert_post(
            session,
            PostInput(
                platform_post_id=raw_post.id,
                author=AuthorInput(
                    platform_author_id=raw_post.author.id,
                    username=raw_post.author.username,
                    display_name=raw_post.author.display_name,
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
        latest_seen_at=latest_seen_at,
        latest_seen_post_id=latest_seen_post_id,
    )


def _safe_failure_code(error: Exception) -> str:
    if isinstance(error, XSourceError) and error.code in _SAFE_SOURCE_ERROR_CODES:
        return error.code
    if isinstance(error, OAuthClientError) and error.code in _SAFE_OAUTH_ERROR_CODES:
        return error.code
    return _GENERIC_FAILURE_CODE
