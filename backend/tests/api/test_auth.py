from __future__ import annotations

import hmac

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from x_digest.config import Settings
from x_digest.main import create_app
from x_digest.models import Base

TOKEN = "test-only-internal-api-token-0123456789abcdef"


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _app(*, token: str | None = TOKEN):
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        internal_api_token=SecretStr(token) if token is not None else None,
    )
    return create_app(settings=settings, session_factory=_session_factory())


def test_health_endpoints_stay_anonymous() -> None:
    app = _app()

    assert TestClient(app).get("/health/live").status_code == 200
    assert TestClient(app).get("/health/ready").status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "json", "headers"),
    [
        ("GET", "/api/lists", None, None),
        (
            "POST",
            "/api/lists",
            {"platform_list_id": "123", "name": "AI", "sync_interval_minutes": 60},
            None,
        ),
        ("PATCH", "/api/lists/missing", {"name": "AI"}, None),
        ("DELETE", "/api/lists/missing", None, None),
        ("GET", "/api/posts", None, None),
        ("POST", "/api/posts/missing/state", {"read": True}, None),
        ("GET", "/api/digests", None, None),
        ("GET", "/api/digests/missing", None, None),
        ("POST", "/api/digests/missing/regenerate", None, None),
        ("POST", "/api/jobs/sync/missing", None, None),
        (
            "POST",
            "/api/posts/missing/summaries/regenerate",
            None,
            {"Idempotency-Key": "summary-request-1"},
        ),
        ("GET", "/api/status", None, None),
    ],
)
def test_every_api_route_rejects_missing_bearer_token(
    method: str,
    path: str,
    json: dict[str, object] | None,
    headers: dict[str, str] | None,
) -> None:
    response = TestClient(_app()).request(method, path, json=json, headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert response.json()["message"] == "Authentication required"
    assert response.json()["request_id"]


def test_valid_bearer_token_is_compared_in_constant_time(monkeypatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real_compare_digest = hmac.compare_digest

    def recording_compare_digest(received: bytes, expected: bytes) -> bool:
        calls.append((received, expected))
        return real_compare_digest(received, expected)

    monkeypatch.setattr("x_digest.api.auth.hmac.compare_digest", recording_compare_digest)

    response = TestClient(_app()).get("/api/lists", headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200
    assert calls == [(TOKEN.encode(), TOKEN.encode())]


def test_wrong_bearer_token_is_rejected() -> None:
    response = TestClient(_app()).get(
        "/api/lists", headers={"Authorization": "Bearer definitely-wrong-token"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


@pytest.mark.parametrize("token", [None, "short-token"])
def test_non_test_startup_rejects_missing_or_short_internal_token(token: str | None) -> None:
    with pytest.raises(RuntimeError, match="INTERNAL_API_TOKEN"):
        _app(token=token)


def test_api_validation_errors_use_safe_error_contract() -> None:
    response = TestClient(_app()).post(
        "/api/lists",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"platform_list_id": "123", "name": "", "unexpected": "secret"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert response.json()["message"] == "Request validation failed"
    assert response.json()["request_id"]
    assert "secret" not in str(response.json())


def test_api_does_not_enable_permissive_cors() -> None:
    response = TestClient(_app()).options(
        "/api/lists",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "access-control-allow-origin" not in response.headers
