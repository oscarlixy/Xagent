from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from x_digest.models import NotificationDelivery, Summary, SyncRun, XList

_SAFE_STAGE_COUNTS = {
    "ingestion": {
        "pages_fetched",
        "posts_seen",
        "posts_created",
        "duplicates",
        "rejected",
        "posts_returned",
    },
    "processing": {"processed", "threads_created", "thread_posts_created"},
    "links": {"selected", "processed", "failed"},
    "summary": {"selected", "created", "failed"},
}
_SAFE_ERROR_CODES = {
    "ingestion_partial",
    "link_failed",
    "link_processing_failed",
    "summary_processing_failed",
}
_SAFE_SYNC_ERROR_CODES = _SAFE_ERROR_CODES | {
    "ingestion_failed",
    "x_authorization_failed",
    "x_rate_limited",
    "x_upstream_unavailable",
    "x_response_invalid",
    "x_request_invalid",
    "oauth_not_configured",
    "oauth_authorization_failed",
    "oauth_upstream_unavailable",
    "oauth_token_invalid",
}
_SAFE_STOP_REASONS = {
    "unchanged",
    "watermark_reached",
    "time_window_reached",
    "provider_exhausted",
    "budget_exhausted",
}
_STALE_GRACE = timedelta(minutes=5)


def status_snapshot(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Return safe operational data for the future authenticated status route."""

    checked_at = _utc(now or datetime.now(UTC))
    latest_run = session.scalar(select(SyncRun).order_by(desc(SyncRun.started_at)))
    latest_summary = session.scalar(select(Summary).order_by(desc(Summary.created_at)))
    deliveries = Counter(session.scalars(select(NotificationDelivery.status)).all())
    return {
        "latest_run": _run_snapshot(latest_run),
        "latest_summary": (
            {"id": latest_summary.id, "status": latest_summary.status}
            if latest_summary is not None
            else None
        ),
        "deliveries": dict(deliveries),
        "sync_health": _sync_health(session, latest_run, checked_at),
    }


def _sync_health(session: Session, latest_run: SyncRun | None, now: datetime) -> dict[str, Any]:
    enabled_lists = session.scalars(
        select(XList).where(XList.enabled.is_(True)).order_by(XList.id)
    ).all()
    completed_runs = session.scalars(
        select(SyncRun)
        .where(
            SyncRun.finished_at.is_not(None),
            SyncRun.status.in_(("succeeded", "partial")),
        )
        .order_by(SyncRun.finished_at.desc())
    ).all()
    latest_completed_by_list: dict[str, SyncRun] = {}
    for run in completed_runs:
        latest_completed_by_list.setdefault(run.list_id, run)

    stale_list_count = 0
    for x_list in enabled_lists:
        completed = latest_completed_by_list.get(x_list.id)
        if completed is None or completed.finished_at is None:
            stale_list_count += 1
            continue
        stale_after = (
            _run_finished_at(completed)
            + timedelta(minutes=x_list.sync_interval_minutes)
            + _STALE_GRACE
        )
        if now > stale_after:
            stale_list_count += 1

    latest_completed = max(
        latest_completed_by_list.values(),
        key=_run_finished_at,
        default=None,
    )
    last_attempt_at = None
    if latest_run is not None:
        last_attempt_at = _utc(latest_run.finished_at or latest_run.started_at)
    return {
        "enabled_list_count": len(enabled_lists),
        "stale_list_count": stale_list_count,
        "stale": stale_list_count > 0,
        "status": latest_run.status if latest_run is not None else None,
        "last_attempt_at": last_attempt_at,
        "last_completed_at": (
            _utc(latest_completed.finished_at)
            if latest_completed is not None and latest_completed.finished_at is not None
            else None
        ),
        "last_error": (
            latest_run.error_code
            if latest_run is not None and latest_run.error_code in _SAFE_SYNC_ERROR_CODES
            else None
        ),
    }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _run_finished_at(run: SyncRun) -> datetime:
    assert run.finished_at is not None
    return _utc(run.finished_at)


def _run_snapshot(run: SyncRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    stages = (run.debug_metadata or {}).get("stages", {})
    safe_stages = {}
    for name, allowed_counts in _SAFE_STAGE_COUNTS.items():
        values = stages.get(name)
        if not isinstance(values, dict):
            continue
        counts = values.get("counts", {})
        safe_counts = (
            {
                key: value
                for key, value in counts.items()
                if key in allowed_counts and isinstance(value, int) and not isinstance(value, bool)
            }
            if isinstance(counts, dict)
            else {}
        )
        duration = values.get("duration_ms", 0)
        error_code = values.get("error_code")
        truncated = values.get("truncated")
        stop_reason = values.get("stop_reason")
        safe_stages[name] = {
            "counts": safe_counts,
            "duration_ms": duration if isinstance(duration, int) and duration >= 0 else 0,
            "last_error": error_code if error_code in _SAFE_ERROR_CODES else None,
            "truncated": truncated if isinstance(truncated, bool) else False,
            "stop_reason": stop_reason if stop_reason in _SAFE_STOP_REASONS else None,
        }
    return {
        "id": run.id,
        "list_id": run.list_id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "posts_returned": run.posts_returned,
        "truncated": run.truncated,
        "stop_reason": run.stop_reason if run.stop_reason in _SAFE_STOP_REASONS else None,
        "stages": safe_stages,
    }
