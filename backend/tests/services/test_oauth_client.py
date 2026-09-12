import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from x_digest.models import Base
from x_digest.services.oauth_client import OAuthClientError, XOAuthClient
from x_digest.services.oauth_tokens import OAuthTokenSet, TokenVault

FERNET_KEY = SecretStr("MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
NOW = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
TOKEN_URL = "https://tokens.test/oauth2/token"


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _token_payload(
    *, access_token: str = "test-access-alpha", refresh_token: str = "test-refresh-alpha"
) -> dict[str, object]:
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": 3600,
        "scope": "tweet.read users.read list.read offline.access",
    }


def _client(
    session_factory: sessionmaker[Session],
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    now: datetime = NOW,
) -> tuple[XOAuthClient, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        XOAuthClient(
            session_factory=session_factory,
            client_id="test-client-id",
            redirect_uri="https://reader.test/api/x/callback",
            encryption_key=FERNET_KEY,
            http_client=http_client,
            clock=lambda: now,
            token_url=TOKEN_URL,
        ),
        http_client,
    )


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode(), keep_blank_values=True)


def _seed(
    session_factory: sessionmaker[Session],
    *,
    now: datetime = NOW,
    access_token: str = "test-access-alpha",
    refresh_token: str = "test-refresh-alpha",
) -> None:
    with session_factory.begin() as session:
        TokenVault(session=session, encryption_key=FERNET_KEY).store(
            provider="x",
            token_payload=_token_payload(
                access_token=access_token,
                refresh_token=refresh_token,
            ),
            now=now,
        )


def test_exchange_code_posts_the_pkce_form_and_persists_valid_tokens(
    session_factory: sessionmaker[Session],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_token_payload())

    service, http_client = _client(session_factory, handler)
    try:
        credential = asyncio.run(
            service.exchange_code(code="test-code-secret", verifier="test-verifier-secret")
        )
    finally:
        asyncio.run(http_client.aclose())

    assert credential == OAuthTokenSet(
        access_token="test-access-alpha",
        refresh_token="test-refresh-alpha",
        access_expires_at=NOW + timedelta(hours=1),
        scope="tweet.read users.read list.read offline.access",
    )
    assert len(requests) == 1
    assert requests[0].url == httpx.URL(TOKEN_URL)
    assert requests[0].headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert _form(requests[0]) == {
        "grant_type": ["authorization_code"],
        "client_id": ["test-client-id"],
        "code": ["test-code-secret"],
        "redirect_uri": ["https://reader.test/api/x/callback"],
        "code_verifier": ["test-verifier-secret"],
    }


def test_access_token_does_not_refresh_before_the_margin(
    session_factory: sessionmaker[Session],
) -> None:
    _seed(session_factory)

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("valid credentials must not call the token endpoint")

    service, http_client = _client(
        session_factory,
        unexpected_request,
        now=NOW + timedelta(minutes=58, seconds=59),
    )
    try:
        token = asyncio.run(service.access_token())
    finally:
        asyncio.run(http_client.aclose())

    assert token == "test-access-alpha"


def test_access_token_refreshes_once_at_the_margin(
    session_factory: sessionmaker[Session],
) -> None:
    _seed(session_factory)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_token_payload(
                access_token="test-access-beta",
                refresh_token="test-refresh-beta",
            ),
        )

    service, http_client = _client(session_factory, handler, now=NOW + timedelta(minutes=59))
    try:
        first, second = asyncio.run(_concurrent_tokens(service))
    finally:
        asyncio.run(http_client.aclose())

    assert first == "test-access-beta"
    assert second == "test-access-beta"
    assert len(requests) == 1
    assert _form(requests[0]) == {
        "grant_type": ["refresh_token"],
        "client_id": ["test-client-id"],
        "refresh_token": ["test-refresh-alpha"],
    }


def test_concurrent_client_instances_share_one_process_refresh_lock(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'oauth.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    _seed(factory)
    request_count = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json=_token_payload(
                access_token="test-access-beta",
                refresh_token="test-refresh-beta",
            ),
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    clients = [
        XOAuthClient(
            session_factory=factory,
            client_id="test-client-id",
            redirect_uri="https://reader.test/api/x/callback",
            encryption_key=FERNET_KEY,
            http_client=http_client,
            clock=lambda: NOW + timedelta(minutes=59),
            token_url=TOKEN_URL,
        )
        for _index in range(2)
    ]
    try:
        first, second = asyncio.run(_tokens_from_distinct_clients(clients))
    finally:
        asyncio.run(http_client.aclose())

    assert first == "test-access-beta"
    assert second == "test-access-beta"
    assert request_count == 1


async def _concurrent_tokens(service: XOAuthClient) -> tuple[str, str]:
    first, second = await asyncio.gather(service.access_token(), service.access_token())
    return first, second


async def _tokens_from_distinct_clients(clients: list[XOAuthClient]) -> tuple[str, str]:
    first, second = await asyncio.gather(clients[0].access_token(), clients[1].access_token())
    return first, second


@pytest.mark.parametrize(
    ("response", "expected_status", "expected_code"),
    [
        (
            httpx.Response(400, text="test-code-secret upstream detail"),
            400,
            "oauth_authorization_failed",
        ),
        (
            httpx.Response(401, text="test-code-secret upstream detail"),
            400,
            "oauth_authorization_failed",
        ),
        (
            httpx.Response(403, text="test-code-secret upstream detail"),
            400,
            "oauth_authorization_failed",
        ),
        (
            httpx.Response(503, text="test-code-secret upstream detail"),
            503,
            "oauth_upstream_unavailable",
        ),
    ],
)
def test_exchange_maps_http_failures_to_safe_codes(
    session_factory: sessionmaker[Session],
    response: httpx.Response,
    expected_status: int,
    expected_code: str,
) -> None:
    service, http_client = _client(session_factory, lambda _request: response)
    try:
        with pytest.raises(OAuthClientError) as raised:
            asyncio.run(
                service.exchange_code(code="test-code-secret", verifier="test-verifier-secret")
            )
    finally:
        asyncio.run(http_client.aclose())

    assert raised.value.status_code == expected_status
    assert raised.value.code == expected_code
    assert "test-code-secret" not in str(raised.value)
    assert "upstream detail" not in str(raised.value)


def test_exchange_maps_network_failure_without_exposing_the_exception(
    session_factory: sessionmaker[Session],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("test-code-secret connection detail", request=request)

    service, http_client = _client(session_factory, handler)
    try:
        with pytest.raises(OAuthClientError) as raised:
            asyncio.run(
                service.exchange_code(code="test-code-secret", verifier="test-verifier-secret")
            )
    finally:
        asyncio.run(http_client.aclose())

    assert raised.value.status_code == 503
    assert raised.value.code == "oauth_upstream_unavailable"
    assert "test-code-secret" not in str(raised.value)
    assert "connection detail" not in str(raised.value)


def test_malformed_refresh_payload_preserves_the_previous_credential(
    session_factory: sessionmaker[Session],
) -> None:
    _seed(session_factory)
    service, http_client = _client(
        session_factory,
        lambda _request: httpx.Response(200, json={"access_token": "replacement-only"}),
        now=NOW + timedelta(minutes=59),
    )
    try:
        with pytest.raises(OAuthClientError) as raised:
            asyncio.run(service.access_token())
    finally:
        asyncio.run(http_client.aclose())

    assert raised.value.status_code == 502
    assert raised.value.code == "oauth_token_invalid"
    with session_factory() as session:
        assert TokenVault(session=session, encryption_key=FERNET_KEY).load(
            provider="x"
        ) == OAuthTokenSet(
            access_token="test-access-alpha",
            refresh_token="test-refresh-alpha",
            access_expires_at=NOW + timedelta(hours=1),
            scope="tweet.read users.read list.read offline.access",
        )


@pytest.mark.parametrize(
    ("client_id", "redirect_uri", "encryption_key"),
    [
        (None, "https://reader.test/api/x/callback", FERNET_KEY),
        ("test-client-id", None, FERNET_KEY),
        ("test-client-id", "https://reader.test/api/x/callback", None),
    ],
)
def test_missing_oauth_configuration_returns_a_safe_error(
    session_factory: sessionmaker[Session],
    client_id: str | None,
    redirect_uri: str | None,
    encryption_key: SecretStr | None,
) -> None:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(AssertionError("must not call X"))
        )
    )
    service = XOAuthClient(
        session_factory=session_factory,
        client_id=client_id,
        redirect_uri=redirect_uri,
        encryption_key=encryption_key,
        http_client=http_client,
        clock=lambda: NOW,
        token_url=TOKEN_URL,
    )
    try:
        with pytest.raises(OAuthClientError) as raised:
            asyncio.run(service.exchange_code(code="test-code", verifier="test-verifier"))
    finally:
        asyncio.run(http_client.aclose())

    assert raised.value.status_code == 503
    assert raised.value.code == "oauth_not_configured"
