from datetime import timedelta

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from x_digest import main as main_module
from x_digest.config import Settings
from x_digest.main import create_app

TEST_TOKEN = SecretStr("test-only-internal-api-token-0123456789abcdef")


def test_ingestion_cost_defaults_are_conservative() -> None:
    settings = Settings(_env_file=None)
    assert settings.x_page_size == 10
    assert settings.x_max_posts_per_sync == 50
    assert settings.x_max_pages_per_sync == 6
    assert settings.x_ingestion_backfill_hours == 24
    assert settings.x_ingestion_overlap_minutes == 10


def test_web_pipeline_receives_bounded_ingestion_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def capture_ingestion(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(main_module, "IngestionService", capture_ingestion)
    session_factory = sessionmaker(create_engine("sqlite+pysqlite:///:memory:"))
    main_module.create_app(
        settings=Settings(_env_file=None),
        session_factory=session_factory,
        _validate_token_on_create=False,
    )

    assert {
        key: captured[key]
        for key in ("page_size", "max_posts", "max_pages", "overlap", "backfill_window")
    } == {
        "page_size": 10,
        "max_posts": 50,
        "max_pages": 6,
        "overlap": timedelta(minutes=10),
        "backfill_window": timedelta(hours=24),
    }


def test_liveness_does_not_require_provider_credentials() -> None:
    response = TestClient(create_app(settings=Settings(internal_api_token=TEST_TOKEN))).get(
        "/health/live"
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_database_failure() -> None:
    app = create_app(
        database_url="postgresql+psycopg://unavailable/x_digest",
        settings=Settings(internal_api_token=TEST_TOKEN),
    )

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["code"] == "database_unavailable"
