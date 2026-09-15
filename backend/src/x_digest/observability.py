from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from x_digest.models import NotificationDelivery, Summary, SyncRun

_SAFE_STAGE_COUNTS = {
    "ingestion": {"pages_fetched", "posts_seen", "posts_created", "duplicates", "rejected"},
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


def status_snapshot(session: Session) -> dict[str, Any]:
    """Return safe operational data for the future authenticated status route."""

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
    }


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
        safe_stages[name] = {
            "counts": safe_counts,
            "duration_ms": duration if isinstance(duration, int) and duration >= 0 else 0,
            "last_error": error_code if error_code in _SAFE_ERROR_CODES else None,
        }
    return {
        "id": run.id,
        "list_id": run.list_id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "stages": safe_stages,
    }
