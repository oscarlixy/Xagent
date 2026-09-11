from datetime import datetime

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


def advance_list_watermark(
    session: Session,
    *,
    x_list: XList,
    sync_run: SyncRun,
    latest_seen_at: datetime,
    latest_seen_post_id: str,
) -> bool:
    if sync_run.status != "succeeded":
        return False
    x_list.latest_seen_at = latest_seen_at
    x_list.latest_seen_post_id = latest_seen_post_id
    session.flush()
    return True
