from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from x_digest.models import SyncLease, SyncRun, XList
from x_digest.models.source import new_id

SYNC_LEASE_STALE_AFTER = timedelta(hours=1)


class ConcurrentSyncError(RuntimeError):
    pass


def acquire_sync_lease(
    session: Session, *, list_id: str, acquired_at: datetime
) -> str:
    if session.get(XList, list_id) is None:
        raise ValueError(f"Unknown List: {list_id}")
    existing = session.get(SyncLease, list_id)
    if existing is not None:
        if _as_utc(existing.acquired_at) > _as_utc(acquired_at) - SYNC_LEASE_STALE_AFTER:
            raise ConcurrentSyncError(f"Sync already running for List: {list_id}")
        session.delete(existing)
        session.flush()

    owner_id = new_id()
    try:
        session.add(
            SyncLease(
                list_id=list_id,
                owner_id=owner_id,
                acquired_at=_as_utc(acquired_at),
            )
        )
        session.flush()
    except IntegrityError:
        raise ConcurrentSyncError(f"Sync already running for List: {list_id}") from None
    return owner_id


def release_sync_lease(session: Session, *, list_id: str, owner_id: str) -> None:
    lease = session.get(SyncLease, list_id)
    if lease is not None and lease.owner_id == owner_id:
        session.delete(lease)
        session.flush()


def renew_sync_lease(
    session: Session, *, list_id: str, owner_id: str, renewed_at: datetime
) -> None:
    result = session.execute(
        update(SyncLease)
        .where(SyncLease.list_id == list_id, SyncLease.owner_id == owner_id)
        .values(acquired_at=_as_utc(renewed_at))
    )
    if getattr(result, "rowcount", 0) != 1:
        raise ConcurrentSyncError(f"Sync lease lost for List: {list_id}")


def reserve_sync_post_budget(
    session: Session,
    *,
    list_id: str,
    sync_run_id: str,
    day_start: datetime,
    requested: int,
    minimum: int,
    daily_limit: int,
) -> int:
    locked_list_id = session.scalar(
        select(XList.id).where(XList.id == list_id).with_for_update()
    )
    sync_run = session.get(SyncRun, sync_run_id)
    if locked_list_id is None or sync_run is None:
        raise ValueError(f"Unknown sync run for List: {list_id}")
    reserved_today = session.scalar(
        select(func.coalesce(func.sum(SyncRun.posts_reserved), 0)).where(
            SyncRun.list_id == list_id,
            SyncRun.started_at >= _as_utc(day_start),
            SyncRun.started_at < _as_utc(day_start) + timedelta(days=1),
        )
    )
    remaining = max(daily_limit - int(reserved_today or 0), 0)
    if remaining < minimum:
        return 0
    granted = min(requested, remaining)
    sync_run.posts_reserved += granted
    session.flush()
    return granted


def settle_sync_post_budget(
    session: Session, *, sync_run: SyncRun, reserved: int, returned: int
) -> None:
    if returned < 0 or returned > reserved:
        raise ValueError("Source returned more items than the reserved request budget")
    sync_run.posts_reserved -= reserved - returned
    session.flush()


def begin_sync_run(
    session: Session, *, list_id: str, started_at: datetime | None = None
) -> SyncRun:
    sync_run = SyncRun(
        list_id=list_id,
        status="running",
        started_at=started_at or datetime.now(UTC),
    )
    session.add(sync_run)
    session.flush()
    return sync_run


def finish_sync_run(session: Session, *, sync_run: SyncRun, status: str) -> SyncRun:
    sync_run.status = status
    sync_run.finished_at = datetime.now(UTC)
    session.flush()
    return sync_run


def advance_list_cursors(
    session: Session,
    *,
    x_list: XList,
    sync_run: SyncRun,
    latest_observed_post_id: str | None,
    latest_seen_at: datetime | None,
    latest_seen_post_id: str | None,
) -> tuple[bool, bool]:
    if sync_run.status not in {"succeeded", "partial"}:
        return False, False

    observed_advanced = latest_observed_post_id is not None
    if observed_advanced:
        x_list.latest_observed_post_id = latest_observed_post_id

    watermark_advanced = False
    if latest_seen_at is not None and latest_seen_post_id is not None:
        candidate = (_as_utc(latest_seen_at), latest_seen_post_id)
        stored = (
            (_as_utc(x_list.latest_seen_at), x_list.latest_seen_post_id or "")
            if x_list.latest_seen_at is not None
            else None
        )
        if stored is None or candidate > stored:
            x_list.latest_seen_at = latest_seen_at
            x_list.latest_seen_post_id = latest_seen_post_id
            watermark_advanced = True
    session.flush()
    return observed_advanced, watermark_advanced


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
