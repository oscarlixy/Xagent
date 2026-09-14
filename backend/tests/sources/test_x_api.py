import asyncio
import importlib.util
import io
from collections.abc import Awaitable, Callable
from contextlib import redirect_stdout
from pathlib import Path

import httpx
import pytest

from x_digest.sources.errors import (
    AuthenticationError,
    InvalidRequestError,
    InvalidResponseError,
    UpstreamError,
)
from x_digest.sources.x_api import XApiSource

LIST_ID = "123456789"
API_URL = f"https://api.x.com/2/lists/{LIST_ID}/tweets"
SECRET = "test-oauth-access-token-must-not-leak"


class TokenService:
    def __init__(self, token: str = SECRET) -> None:
        self.token = token
        self.calls = 0

    async def access_token(self) -> str:
        self.calls += 1
        return self.token


def _response_payload() -> dict[str, object]:
    return {
        "data": [
            {
                "id": "tweet-1",
                "author_id": "author-1",
                "text": "A repost record is still a record",
                "created_at": "2026-09-12T08:00:00.000Z",
                "conversation_id": "conversation-1",
                "entities": {"urls": []},
                "referenced_tweets": [{"type": "retweeted", "id": "original-1"}],
            }
        ],
        "includes": {"users": [{"id": "author-1", "username": "alice", "name": "Alice"}]},
        "meta": {"next_token": "next-page"},
    }


def _source(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
    max_attempts: int = 3,
) -> tuple[XApiSource, TokenService, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    token_service = TokenService()
    return (
        XApiSource(
            token_service=token_service,
            http_client=client,
            sleeper=sleeper,
            max_attempts=max_attempts,
        ),
        token_service,
        client,
    )


def test_fetch_page_uses_oauth_and_exact_list_query_without_repost_lookups() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert token_service.calls == 1
        requests.append(request)
        return httpx.Response(200, json=_response_payload(), headers={"x-request-id": "request-1"})

    source, token_service, client = _source(handler)
    try:
        page = asyncio.run(
            source.fetch_page(
                list_id=LIST_ID, pagination_token="page-before", max_results=25
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert token_service.calls == 1
    assert len(requests) == 1
    assert str(requests[0].url.copy_with(query=None)) == API_URL
    assert dict(requests[0].url.params) == {
        "max_results": "25",
        "pagination_token": "page-before",
        "tweet.fields": "author_id,created_at,conversation_id,entities,referenced_tweets",
        "expansions": "author_id",
        "user.fields": "username,name",
    }
    assert requests[0].headers["authorization"] == f"Bearer {SECRET}"
    assert page.next_token == "next-page"
    assert page.request_id == "request-1"
    assert len(page.posts) == 1
    assert page.posts[0].author.username == "alice"
    assert page.posts[0].references == [{"type": "retweeted", "id": "original-1"}]
    assert page.posts[0].source_url == "https://x.com/alice/status/tweet-1"


def test_fetch_page_accepts_valid_data_and_converts_provider_item_errors_safely() -> None:
    payload = _response_payload()
    payload["errors"] = [
        {"value": "tweet-rejected", "detail": "provider detail containing " + SECRET},
        {"detail": "another provider detail"},
    ]
    source, _tokens, client = _source(lambda _request: httpx.Response(200, json=payload))
    try:
        page = asyncio.run(
            source.fetch_page(list_id=LIST_ID, pagination_token=None, max_results=1)
        )
    finally:
        asyncio.run(client.aclose())

    assert len(page.posts) == 1
    assert page.rejected_items[0].identifier == "tweet-rejected"
    assert [item.reason for item in page.rejected_items] == [
        "provider_rejected",
        "provider_rejected",
    ]
    assert SECRET not in repr(page.rejected_items)


def test_fetch_page_accepts_omitted_data_with_safe_provider_item_errors() -> None:
    source, _tokens, client = _source(
        lambda _request: httpx.Response(
            200,
            json={
                "errors": [{"value": "tweet-rejected", "detail": "provider detail " + SECRET}],
                "meta": {"result_count": 0},
            },
        )
    )
    try:
        page = asyncio.run(
            source.fetch_page(list_id=LIST_ID, pagination_token=None, max_results=1)
        )
    finally:
        asyncio.run(client.aclose())

    assert page.posts == ()
    assert page.rejected_items[0].identifier == "tweet-rejected"
    assert page.rejected_items[0].reason == "provider_rejected"
    assert SECRET not in repr(page.rejected_items)


@pytest.mark.parametrize("list_id", ["../../users/me", "123?max_results=100", "123#fragment"])
def test_fetch_page_rejects_non_numeric_list_ids_before_token_or_http_request(list_id: str) -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an invalid List ID must not make an X request")

    source, token_service, client = _source(unexpected_request)
    try:
        with pytest.raises(InvalidRequestError):
            asyncio.run(source.fetch_page(list_id=list_id, pagination_token=None, max_results=1))
    finally:
        asyncio.run(client.aclose())

    assert token_service.calls == 0


def test_fetch_page_rejects_malformed_provider_payload_without_body_details() -> None:
    source, _tokens, client = _source(
        lambda _request: httpx.Response(200, text='{"data": "bad ' + SECRET + '"}')
    )
    try:
        with pytest.raises(InvalidResponseError) as raised:
            asyncio.run(
                source.fetch_page(list_id=LIST_ID, pagination_token=None, max_results=1)
            )
    finally:
        asyncio.run(client.aclose())

    assert raised.value.status_code == 502
    assert raised.value.code == "x_response_invalid"
    assert SECRET not in str(raised.value)


def test_fetch_page_retries_rate_limit_using_retry_after_and_preserves_request_id() -> None:
    attempts = 0
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                text=SECRET,
                headers={"retry-after": "2", "x-request-id": "slow-1"},
            )
        return httpx.Response(200, json=_response_payload(), headers={"x-request-id": "success-1"})

    source, _tokens, client = _source(handler, sleeper=sleeper)
    try:
        page = asyncio.run(
            source.fetch_page(list_id=LIST_ID, pagination_token=None, max_results=1)
        )
    finally:
        asyncio.run(client.aclose())

    assert attempts == 2
    assert delays == [2.0]
    assert page.request_id == "success-1"


@pytest.mark.parametrize("retry_after", ["NaN", "inf", "-1", "not-a-number"])
def test_fetch_page_uses_bounded_backoff_for_invalid_retry_after(retry_after: str) -> None:
    attempts = 0
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": retry_after})
        return httpx.Response(200, json=_response_payload())

    source, _tokens, client = _source(handler, sleeper=sleeper)
    try:
        asyncio.run(source.fetch_page(list_id=LIST_ID, pagination_token=None, max_results=1))
    finally:
        asyncio.run(client.aclose())

    assert delays == [0.5]


def test_fetch_page_retries_5xx_with_bounded_exponential_backoff() -> None:
    attempts = 0
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, text="provider body " + SECRET)
        return httpx.Response(200, json=_response_payload())

    source, _tokens, client = _source(handler, sleeper=sleeper)
    try:
        asyncio.run(source.fetch_page(list_id=LIST_ID, pagination_token=None, max_results=1))
    finally:
        asyncio.run(client.aclose())

    assert attempts == 3
    assert delays == [0.5, 1.0]


def test_fetch_page_redacts_provider_body_after_retries_are_exhausted() -> None:
    source, _tokens, client = _source(
        lambda _request: httpx.Response(503, text="provider body " + SECRET), max_attempts=1
    )
    try:
        with pytest.raises(UpstreamError) as raised:
            asyncio.run(
                source.fetch_page(list_id=LIST_ID, pagination_token=None, max_results=1)
            )
    finally:
        asyncio.run(client.aclose())

    assert raised.value.status_code == 503
    assert raised.value.code == "x_upstream_unavailable"
    assert SECRET not in str(raised.value)


def test_fetch_page_fails_authentication_without_retrying_or_leaking_body() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="provider body " + SECRET)

    source, _tokens, client = _source(handler)
    try:
        with pytest.raises(AuthenticationError) as raised:
            asyncio.run(
                source.fetch_page(list_id=LIST_ID, pagination_token=None, max_results=1)
            )
    finally:
        asyncio.run(client.aclose())

    assert calls == 1
    assert raised.value.status_code == 401
    assert raised.value.code == "x_authorization_failed"
    assert SECRET not in str(raised.value)


def test_preflight_outputs_only_safe_json_and_uses_one_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = Path(__file__).parents[2] / "scripts" / "check_x_access.py"
    spec = importlib.util.spec_from_file_location("check_x_access", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class ProbeSource:
        calls: list[tuple[str, str | None, int]] = []

        async def fetch_page(
            self, *, list_id: str, pagination_token: str | None, max_results: int
        ) -> object:
            self.calls.append((list_id, pagination_token, max_results))
            from x_digest.sources.types import RejectedItem, SourcePage

            return SourcePage(
                request_id="request-only",
                posts=(),
                rejected_items=(
                    RejectedItem(identifier="tweet-rejected", reason="provider_rejected"),
                ),
            )

        async def aclose(self) -> None:
            return None

    class ProbeClient:
        async def aclose(self) -> None:
            return None

    probe_source = ProbeSource()
    monkeypatch.setattr(module, "_build_source", lambda _settings: (probe_source, ProbeClient()))
    output = io.StringIO()
    with redirect_stdout(output):
        code = module.main(["--list-id", LIST_ID, "--confirm"])

    assert code == 0
    assert output.getvalue() == '{"status":"ok","count":0,"request_id":"request-only"}\n'
    assert SECRET not in output.getvalue()
    assert probe_source.calls == [(LIST_ID, None, 1)]


def test_preflight_rejects_invalid_list_id_before_constructing_oauth_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = Path(__file__).parents[2] / "scripts" / "check_x_access.py"
    spec = importlib.util.spec_from_file_location("check_x_access_invalid_list", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(
        module,
        "_build_source",
        lambda _settings: (_ for _ in ()).throw(
            AssertionError("OAuth source must not be constructed")
        ),
    )
    output = io.StringIO()
    with redirect_stdout(output), pytest.raises(SystemExit):
        module.main(["--list-id", "../../users/me", "--confirm"])

    assert output.getvalue() == ""


def test_preflight_failure_output_is_equally_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    script = Path(__file__).parents[2] / "scripts" / "check_x_access.py"
    spec = importlib.util.spec_from_file_location("check_x_access_failure", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FailingProbeSource:
        async def fetch_page(
            self, *, list_id: str, pagination_token: str | None, max_results: int
        ) -> object:
            raise UpstreamError(request_id="request-failed")

        async def aclose(self) -> None:
            return None

    class ProbeClient:
        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        module, "_build_source", lambda _settings: (FailingProbeSource(), ProbeClient())
    )
    output = io.StringIO()
    with redirect_stdout(output):
        code = module.main(["--list-id", LIST_ID, "--confirm"])

    assert code == 1
    assert output.getvalue() == '{"status":"error","count":0,"request_id":"request-failed"}\n'
    assert SECRET not in output.getvalue()
