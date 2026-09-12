import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from x_digest.config import Settings
from x_digest.main import create_app
from x_digest.models import Base, OAuthCredential

INTERNAL_TOKEN = "test-only-internal-api-token-0123456789abcdef"
FERNET_KEY = SecretStr("MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
NOW = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _settings(*, configured: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        internal_api_token=SecretStr(INTERNAL_TOKEN),
        x_client_id="test-client-id" if configured else None,
        x_oauth_redirect_uri="https://reader.test/api/x/callback" if configured else None,
        x_token_encryption_key=FERNET_KEY if configured else None,
    )


def _request(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    configured: bool = True,
    headers: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[httpx.Response, sessionmaker[Session]]:
    factory = _session_factory()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(
        settings=_settings(configured=configured),
        session_factory=factory,
        oauth_http_client=http_client,
        oauth_clock=lambda: NOW,
    )
    try:
        response = TestClient(app).post(
            "/api/x/oauth/callback",
            headers=headers,
            json=payload or {"code": "test-code-secret", "verifier": "test-verifier-secret"},
        )
    finally:
        asyncio.run(http_client.aclose())
    return response, factory


def _success(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "test-access-secret",
            "refresh_token": "test-refresh-secret",
            "expires_in": 3600,
            "scope": "tweet.read users.read list.read offline.access",
        },
    )


def test_callback_requires_the_internal_bearer_token() -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unauthorized callbacks must not call X")

    response, _factory = _request(unexpected_request)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_callback_returns_empty_204_and_persists_only_encrypted_tokens() -> None:
    response, factory = _request(
        _success,
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    )

    assert response.status_code == 204
    assert response.content == b""
    assert "test-access-secret" not in response.text
    assert "test-refresh-secret" not in response.text
    assert "test-code-secret" not in response.text
    assert "test-verifier-secret" not in response.text
    with factory() as session:
        stored = session.get(OAuthCredential, "x")
        assert stored is not None
        assert b"test-access-secret" not in stored.encrypted_access_token
        assert b"test-refresh-secret" not in stored.encrypted_refresh_token


def test_callback_rejects_extra_json_fields_without_contacting_x() -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid callbacks must not call X")

    response, _factory = _request(
        unexpected_request,
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
        payload={
            "code": "test-code-secret",
            "verifier": "test-verifier-secret",
            "access_token": "injected-secret",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "injected-secret" not in str(response.json())


def test_callback_returns_safe_503_when_oauth_is_not_configured() -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unconfigured callbacks must not call X")

    response, _factory = _request(
        unexpected_request,
        configured=False,
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "oauth_not_configured"
    assert response.json()["message"] == "X OAuth is not configured"


@pytest.mark.parametrize("status_code", [400, 503])
def test_callback_never_returns_upstream_body_or_request_secrets(status_code: int) -> None:
    response, _factory = _request(
        lambda _request: httpx.Response(
            status_code,
            text="upstream test-code-secret test-verifier-secret test-refresh-secret",
        ),
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    )

    assert response.status_code == status_code
    assert response.json()["code"] in {
        "oauth_authorization_failed",
        "oauth_upstream_unavailable",
    }
    assert "upstream test-code-secret" not in str(response.json())
    assert "test-code-secret" not in str(response.json())
    assert "test-verifier-secret" not in str(response.json())
    assert "test-refresh-secret" not in str(response.json())
