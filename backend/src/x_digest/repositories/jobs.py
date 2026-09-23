from datetime import UTC, datetime

from sqlalchemy.orm import Session

from x_digest.models import SyncRun, XList


def begin_sync_run(session: Session, *, list_id: str) -> SyncRun:
    sync_run = SyncRun(list_id=list_id, status="running")
    session.add(sync_run)
    session.flush()
    return sync_run


def finish_sync_run(session: Session, *, sync_run: SyncRun, status: str) -> SyncRun:
    sync_run.status = status
    sync_run.finished_at = datetime.now().astimezone()
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


def advance_list_watermark(
    session: Session,
    *,
    x_list: XList,
    sync_run: SyncRun,
    latest_seen_at: datetime,
    latest_seen_post_id: str,
) -> bool:
    """Compatibility wrapper until the ingestion service adopts both cursors."""
    _, watermark_advanced = advance_list_cursors(
        session,
        x_list=x_list,
        sync_run=sync_run,
        latest_observed_post_id=None,
        latest_seen_at=latest_seen_at,
        latest_seen_post_id=latest_seen_post_id,
    )
    return watermark_advanced


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
