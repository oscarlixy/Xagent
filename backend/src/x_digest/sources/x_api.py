from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from x_digest.sources.errors import (
    AuthenticationError,
    InvalidRequestError,
    InvalidResponseError,
    RateLimitError,
    UpstreamError,
)
from x_digest.sources.types import RawAuthor, RawPost, RejectedItem, SourcePage

DEFAULT_X_API_BASE_URL = "https://api.x.com/2"
MAX_RETRY_DELAY_SECONDS = 60.0

type Sleeper = Callable[[float], Awaitable[None]]


class AccessTokenService(Protocol):
    async def access_token(self) -> str: ...


class XApiSource:
    """Official X List source using a user OAuth access token."""

    def __init__(
        self,
        *,
        token_service: AccessTokenService,
        http_client: httpx.AsyncClient | None = None,
        sleeper: Sleeper | None = None,
        max_attempts: int = 3,
        api_base_url: str = DEFAULT_X_API_BASE_URL,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._token_service = token_service
        self._http_client = http_client or httpx.AsyncClient(timeout=10.0)
        self._owns_http_client = http_client is None
        self._sleeper = sleeper or asyncio.sleep
        self._max_attempts = max_attempts
        self._api_base_url = api_base_url.rstrip("/")

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def fetch_page(
        self, *, list_id: str, pagination_token: str | None, max_results: int
    ) -> SourcePage:
        if not list_id or not 1 <= max_results <= 100:
            raise InvalidRequestError()
        access_token = await self._token_service.access_token()
        params = {
            "max_results": str(max_results),
            "tweet.fields": "author_id,created_at,conversation_id,entities,referenced_tweets",
            "expansions": "author_id",
            "user.fields": "username,name",
        }
        if pagination_token is not None:
            params["pagination_token"] = pagination_token
        url = f"{self._api_base_url}/lists/{list_id}/tweets"

        for attempt in range(self._max_attempts):
            try:
                response = await self._http_client.get(
                    url, params=params, headers={"Authorization": f"Bearer {access_token}"}
                )
            except httpx.RequestError:
                if attempt == self._max_attempts - 1:
                    raise UpstreamError() from None
                await self._sleeper(_backoff_delay(attempt))
                continue

            request_id = response.headers.get("x-request-id")
            if 200 <= response.status_code < 300:
                return _parse_page(response, request_id=request_id)
            if response.status_code in {401, 403}:
                raise AuthenticationError(request_id=request_id)
            if response.status_code == 429:
                if attempt == self._max_attempts - 1:
                    raise RateLimitError(request_id=request_id)
                await self._sleeper(_retry_after_delay(response, attempt))
                continue
            if response.status_code >= 500:
                if attempt == self._max_attempts - 1:
                    raise UpstreamError(request_id=request_id)
                await self._sleeper(_backoff_delay(attempt))
                continue
            raise UpstreamError(request_id=request_id)

        raise UpstreamError()  # pragma: no cover - loop always returns or raises


def _backoff_delay(attempt: int) -> float:
    return float(min(0.5 * (2**attempt), MAX_RETRY_DELAY_SECONDS))


def _retry_after_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0.0), MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    return _backoff_delay(attempt)


def _parse_page(response: httpx.Response, *, request_id: str | None) -> SourcePage:
    try:
        payload = response.json()
    except ValueError:
        raise InvalidResponseError(request_id=request_id) from None
    if not isinstance(payload, Mapping):
        raise InvalidResponseError(request_id=request_id)

    data = payload.get("data")
    if not isinstance(data, list):
        raise InvalidResponseError(request_id=request_id)
    users = _users_by_id(payload.get("includes"), request_id=request_id)
    errors = payload.get("errors", [])
    if not isinstance(errors, list):
        raise InvalidResponseError(request_id=request_id)
    meta = payload.get("meta", {})
    if not isinstance(meta, Mapping):
        raise InvalidResponseError(request_id=request_id)
    next_token = meta.get("next_token")
    if next_token is not None and not isinstance(next_token, str):
        raise InvalidResponseError(request_id=request_id)

    posts: list[RawPost] = []
    rejected: list[RejectedItem] = []
    for item in data:
        post = _parse_post(item, users)
        if post is None:
            identifier = item.get("id") if isinstance(item, Mapping) else None
            rejected.append(
                RejectedItem(
                    identifier=identifier if isinstance(identifier, str) else None,
                    reason="invalid_item",
                )
            )
        else:
            posts.append(post)
    for error in errors:
        identifier = error.get("value") if isinstance(error, Mapping) else None
        rejected.append(
            RejectedItem(
                identifier=identifier if isinstance(identifier, str) else None,
                reason="provider_rejected",
            )
        )
    return SourcePage(
        posts=tuple(posts),
        rejected_items=tuple(rejected),
        next_token=next_token,
        request_id=request_id,
    )


def _users_by_id(includes: object, *, request_id: str | None) -> dict[str, RawAuthor]:
    if includes is None:
        return {}
    if not isinstance(includes, Mapping):
        raise InvalidResponseError(request_id=request_id)
    users = includes.get("users", [])
    if not isinstance(users, list):
        raise InvalidResponseError(request_id=request_id)
    result: dict[str, RawAuthor] = {}
    for user in users:
        if not isinstance(user, Mapping):
            continue
        user_id = user.get("id")
        username = user.get("username")
        name = user.get("name")
        if (
            isinstance(user_id, str)
            and isinstance(username, str)
            and (name is None or isinstance(name, str))
        ):
            result[user_id] = RawAuthor(id=user_id, username=username, display_name=name)
    return result


def _parse_post(item: object, users: Mapping[str, RawAuthor]) -> RawPost | None:
    if not isinstance(item, Mapping):
        return None
    post_id = item.get("id")
    author_id = item.get("author_id")
    text = item.get("text")
    created_at = item.get("created_at")
    conversation_id = item.get("conversation_id")
    entities = item.get("entities")
    references = item.get("referenced_tweets")
    if not (
        isinstance(post_id, str)
        and isinstance(author_id, str)
        and isinstance(text, str)
        and isinstance(created_at, str)
        and (conversation_id is None or isinstance(conversation_id, str))
        and (entities is None or isinstance(entities, dict))
        and (references is None or isinstance(references, list))
    ):
        return None
    author = users.get(author_id)
    if author is None:
        return None
    try:
        created = _parse_rfc3339(created_at)
    except ValueError:
        return None
    parsed_references: list[dict[str, Any]] | None = None
    if references is not None:
        if not all(isinstance(reference, Mapping) for reference in references):
            return None
        parsed_references = [dict(reference) for reference in references]
    return RawPost(
        id=post_id,
        author=author,
        text=text,
        created_at=created,
        source_url=f"https://x.com/{author.username}/status/{post_id}",
        conversation_id=conversation_id,
        references=parsed_references,
        entities=entities,
    )


def _parse_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)
