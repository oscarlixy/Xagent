from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

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
LIST_ID_PATTERN = re.compile(r"^[0-9]{1,19}$")
SAFE_PROVIDER_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

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
        source_mode: Literal["recent_search", "list_posts"] = "recent_search",
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if source_mode not in {"recent_search", "list_posts"}:
            raise ValueError("source_mode must be recent_search or list_posts")
        self._token_service = token_service
        self._http_client = http_client or httpx.AsyncClient(timeout=10.0)
        self._owns_http_client = http_client is None
        self._sleeper = sleeper or asyncio.sleep
        self._max_attempts = max_attempts
        self._api_base_url = api_base_url.rstrip("/")
        self._source_mode = source_mode

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    @property
    def minimum_results(self) -> int:
        return 10 if self._source_mode == "recent_search" else 1

    async def fetch_page(
        self,
        *,
        list_id: str,
        pagination_token: str | None,
        max_results: int,
        start_time: datetime | None = None,
    ) -> SourcePage:
        if (
            not is_valid_x_list_id(list_id)
            or not self.minimum_results <= max_results <= 100
        ):
            raise InvalidRequestError()
        access_token = await self._token_service.access_token()
        params = {
            "max_results": str(max_results),
            "post.fields": "author_id,created_at,conversation_id,entities,referenced_posts",
        }
        if pagination_token is not None:
            params["pagination_token"] = pagination_token
        if self._source_mode == "recent_search":
            params["query"] = f"list:{list_id} -is:retweet -is:reply -is:nullcast"
        if start_time is not None and self._source_mode == "recent_search":
            params["start_time"] = (
                start_time.astimezone(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
        url = (
            f"{self._api_base_url}/tweets/search/recent"
            if self._source_mode == "recent_search"
            else f"{self._api_base_url}/lists/{list_id}/tweets"
        )

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
            delay = float(retry_after)
        except ValueError:
            return _backoff_delay(attempt)
        if math.isfinite(delay) and delay >= 0:
            return min(delay, MAX_RETRY_DELAY_SECONDS)
    return _backoff_delay(attempt)


def is_valid_x_list_id(value: str) -> bool:
    return LIST_ID_PATTERN.fullmatch(value) is not None


def _safe_identifier(value: object, *, max_length: int) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= max_length:
        return None
    return value if SAFE_PROVIDER_IDENTIFIER_PATTERN.fullmatch(value) is not None else None


def _parse_page(response: httpx.Response, *, request_id: str | None) -> SourcePage:
    try:
        payload = response.json()
    except ValueError:
        raise InvalidResponseError(request_id=request_id) from None
    if not isinstance(payload, Mapping):
        raise InvalidResponseError(request_id=request_id)

    data = payload.get("data", [])
    if not isinstance(data, list):
        raise InvalidResponseError(request_id=request_id)
    returned_count = len(data)
    first_item = data[0] if data else None
    raw_head = first_item.get("id") if isinstance(first_item, Mapping) else None
    head_post_id = _safe_identifier(raw_head, max_length=32)
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
        post = _parse_post(item)
        if post is None:
            identifier = item.get("id") if isinstance(item, Mapping) else None
            rejected.append(
                RejectedItem(
                    identifier=_safe_identifier(identifier, max_length=64),
                    reason="invalid_item",
                )
            )
        else:
            posts.append(post)
    for error in errors:
        identifier = error.get("value") if isinstance(error, Mapping) else None
        rejected.append(
            RejectedItem(
                identifier=_safe_identifier(identifier, max_length=64),
                reason="provider_rejected",
            )
        )
    return SourcePage(
        posts=tuple(posts),
        rejected_items=tuple(rejected),
        next_token=next_token,
        request_id=request_id,
        returned_count=returned_count,
        head_post_id=head_post_id,
    )


def _parse_post(item: object) -> RawPost | None:
    if not isinstance(item, Mapping):
        return None
    post_id = _safe_identifier(item.get("id"), max_length=32)
    author_id = item.get("author_id")
    text = item.get("text")
    created_at = item.get("created_at")
    conversation_id = item.get("conversation_id")
    entities = item.get("entities")
    references = item.get("referenced_posts", item.get("referenced_tweets"))
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
    author = RawAuthor(id=author_id)
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
        source_url=f"https://x.com/i/web/status/{post_id}",
        conversation_id=conversation_id,
        references=parsed_references,
        entities=entities,
    )


def _parse_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)
