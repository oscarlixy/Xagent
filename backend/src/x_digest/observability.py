from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from x_digest.models import NotificationDelivery, Summary, SyncRun


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
    safe_stages = {
        name: {
            "counts": values.get("counts", {}),
            "duration_ms": values.get("duration_ms", 0),
            "last_error": values.get("error_code"),
        }
        for name, values in stages.items()
        if isinstance(values, dict)
    }
    return {
        "id": run.id,
        "list_id": run.list_id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "stages": safe_stages,
    }
