from fastapi.testclient import TestClient
from pydantic import SecretStr

from x_digest.config import Settings
from x_digest.main import create_app

TEST_TOKEN = SecretStr("test-only-internal-api-token-0123456789abcdef")


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
