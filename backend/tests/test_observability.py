from datetime import UTC, datetime

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
                debug_metadata={
                    "stages": {
                        "ingestion": {
                            "counts": {"posts_created": 1},
                            "duration_ms": 12,
                            "error_code": None,
                        },
                        "summary": {"counts": {"created": 1}, "duration_ms": 8, "error_code": None},
                        "links": {
                            "counts": {
                                "failed": 1,
                                "provider_payload": {"api_key": "nested-secret"},
                            },
                            "duration_ms": 1,
                            "error_code": "link_failed",
                        },
                    },
                    "provider_payload": {"api_key": "super-secret", "raw": "do not expose"},
                },
            )
        )
        session.commit()

        snapshot = status_snapshot(session)

    assert snapshot["latest_run"]["status"] == "partial"
    assert snapshot["latest_run"]["stages"]["links"] == {
        "counts": {"failed": 1},
        "duration_ms": 1,
        "last_error": "link_failed",
    }
    assert "provider_payload" not in str(snapshot)
    assert "super-secret" not in str(snapshot)
    assert "nested-secret" not in str(snapshot)
