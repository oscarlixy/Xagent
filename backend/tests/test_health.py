from fastapi.testclient import TestClient

from x_digest.main import create_app


def test_liveness_does_not_require_provider_credentials() -> None:
    response = TestClient(create_app()).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_database_failure() -> None:
    app = create_app(database_url="postgresql+psycopg://unavailable/x_digest")

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["code"] == "database_unavailable"
