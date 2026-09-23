from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from x_digest.models import Base, SyncRun
from x_digest.observability import status_snapshot
from x_digest.repositories.lists import upsert_list


def test_status_snapshot_exposes_stage_metrics_without_provider_payloads_or_secrets() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        x_list = upsert_list(session, platform_list_id="list-a", name="AI news")
        session.add(
            SyncRun(
                list_id=x_list.id,
                status="partial",
                finished_at=datetime.now(UTC),
                posts_returned=2,
                truncated=True,
                stop_reason="budget_exhausted",
                debug_metadata={
                    "stages": {
                        "ingestion": {
                            "counts": {"posts_created": 1, "posts_returned": 2},
                            "duration_ms": 12,
                            "error_code": None,
                            "truncated": True,
                            "stop_reason": "budget_exhausted",
                        },
                        "summary": {"counts": {"created": 1}, "duration_ms": 8, "error_code": None},
                        "links": {
                            "counts": {
                                "failed": 1,
                                "provider_payload": {"api_key": "nested-secret"},
                            },
                            "duration_ms": 1,
                            "error_code": "link_failed",
                            "truncated": "not-a-boolean",
                            "stop_reason": "provider-secret-reason",
                        },
                    },
                    "provider_payload": {"api_key": "super-secret", "raw": "do not expose"},
                },
            )
        )
        session.commit()

        snapshot = status_snapshot(session)

    assert snapshot["latest_run"]["status"] == "partial"
    assert snapshot["latest_run"]["posts_returned"] == 2
    assert snapshot["latest_run"]["truncated"] is True
    assert snapshot["latest_run"]["stop_reason"] == "budget_exhausted"
    assert snapshot["latest_run"]["stages"]["ingestion"] == {
        "counts": {"posts_created": 1, "posts_returned": 2},
        "duration_ms": 12,
        "last_error": None,
        "truncated": True,
        "stop_reason": "budget_exhausted",
    }
    assert snapshot["latest_run"]["stages"]["links"] == {
        "counts": {"failed": 1},
        "duration_ms": 1,
        "last_error": "link_failed",
        "truncated": False,
        "stop_reason": None,
    }
    assert "provider_payload" not in str(snapshot)
    assert "super-secret" not in str(snapshot)
    assert "nested-secret" not in str(snapshot)


def test_status_snapshot_normalizes_unknown_top_level_stop_reason() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        x_list = upsert_list(session, platform_list_id="list-a", name="AI news")
        session.add(
            SyncRun(
                list_id=x_list.id,
                status="succeeded",
                stop_reason="provider-secret-reason",
            )
        )
        session.commit()

        snapshot = status_snapshot(session)

    assert snapshot["latest_run"]["stop_reason"] is None
    assert "provider-secret-reason" not in str(snapshot)


def test_status_snapshot_marks_overdue_syncs_stale_and_exposes_safe_failure() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    with Session(engine) as session:
        x_list = upsert_list(
            session,
            platform_list_id="list-a",
            name="AI news",
            sync_interval_minutes=60,
        )
        session.add(
            SyncRun(
                list_id=x_list.id,
                status="partial",
                started_at=now - timedelta(hours=3),
                finished_at=now - timedelta(hours=2),
                error_code="summary_processing_failed",
            )
        )
        session.commit()

        snapshot = status_snapshot(session, now=now)

    assert snapshot["sync_health"] == {
        "enabled_list_count": 1,
        "stale_list_count": 1,
        "stale": True,
        "status": "partial",
        "last_attempt_at": now - timedelta(hours=2),
        "last_completed_at": now - timedelta(hours=2),
        "last_error": "summary_processing_failed",
    }


def test_status_snapshot_treats_recent_completed_sync_as_fresh() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    with Session(engine) as session:
        x_list = upsert_list(
            session,
            platform_list_id="list-a",
            name="AI news",
            sync_interval_minutes=60,
        )
        session.add(
            SyncRun(
                list_id=x_list.id,
                status="succeeded",
                started_at=now - timedelta(minutes=20),
                finished_at=now - timedelta(minutes=15),
            )
        )
        session.commit()

        snapshot = status_snapshot(session, now=now)

    assert snapshot["sync_health"]["stale"] is False
    assert snapshot["sync_health"]["stale_list_count"] == 0
