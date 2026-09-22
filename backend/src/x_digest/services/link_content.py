from __future__ import annotations

import http.client
import re
import socket
import ssl
import time
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal, Protocol
from urllib.parse import urljoin, urlsplit

from x_digest.security.network import (
    DEFAULT_RESOLVER,
    NetworkPolicyError,
    ResolvedTarget,
    Resolver,
    resolve_public_url,
)

FetchStatus = Literal["fetched", "unsupported", "blocked", "failed", "too_large"]


@dataclass(frozen=True)
class LinkContentResult:
    status: FetchStatus
    final_url: str
    title: str | None = None
    text: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


type Requester = Callable[[ResolvedTarget, int, float], HttpResponse]


class LinkFetcher(Protocol):
    def fetch(self, url: str) -> LinkContentResult: ...


class _BodyTooLarge(Exception):
    pass


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, target: ResolvedTarget, timeout: float) -> None:
        super().__init__(target.host, target.port, timeout=timeout)
        self._pinned_ip = target.ip
        self._source_address: tuple[str, int] | None = None

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self._source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, target: ResolvedTarget, timeout: float) -> None:
        context = ssl.create_default_context()
        super().__init__(
            target.host,
            target.port,
            timeout=timeout,
            context=context,
        )
        self._pinned_ip = target.ip
        self._source_address: tuple[str, int] | None = None
        self._ssl_context = context

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self._source_address
        )
        self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)


class _ReadableHTMLParser(HTMLParser):
    _ignored_tags = {"footer", "header", "nav", "noscript", "script", "style", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._body_depth = 0
        self._ignored_depth = 0
        self._title_depth = 0
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "body":
            self._body_depth += 1
        if tag in self._ignored_tags:
            self._ignored_depth += 1
        if tag == "title":
            self._title_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag in self._ignored_tags and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "body" and self._body_depth:
            self._body_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._title_depth:
            self.title_parts.append(data)
        if self._body_depth:
            self.body_parts.append(data)


class LinkContentFetcher:
    def __init__(
        self,
        *,
        resolver: Resolver = DEFAULT_RESOLVER,
        requester: Requester | None = None,
        max_redirects: int = 3,
        timeout_seconds: float = 10.0,
        max_compressed_bytes: int = 2 * 1024 * 1024,
        max_decompressed_bytes: int = 5 * 1024 * 1024,
        max_text_chars: int = 200_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolver = resolver
        self._requester = requester or _request
        self._max_redirects = max_redirects
        self._timeout_seconds = timeout_seconds
        self._max_compressed_bytes = max_compressed_bytes
        self._max_decompressed_bytes = max_decompressed_bytes
        self._max_text_chars = max_text_chars
        self._clock = clock

    def fetch(self, url: str) -> LinkContentResult:
        current_url = url
        visited: set[str] = set()
        deadline = self._clock() + self._timeout_seconds
        for redirect_count in range(self._max_redirects + 1):
            if current_url in visited:
                return _failure("failed", current_url, "redirect_loop")
            visited.add(current_url)
            try:
                target = resolve_public_url(current_url, resolver=self._resolver)
            except NetworkPolicyError as exc:
                return _failure("blocked", current_url, exc.error_code)
            try:
                remaining_seconds = deadline - self._clock()
                if remaining_seconds <= 0:
                    return _failure("failed", current_url, "request_timeout")
                response = self._requester(
                    target, self._max_compressed_bytes, remaining_seconds
                )
            except _BodyTooLarge:
                return _failure("too_large", current_url, "compressed_body_too_large")
            except Exception:
                return _failure("failed", current_url, "request_failed")

            headers = {key.lower(): value for key, value in response.headers.items()}
            if response.status_code in {301, 302, 303, 307, 308}:
                location = headers.get("location")
                if not location:
                    return _failure("failed", current_url, "redirect_missing_location")
                if redirect_count == self._max_redirects:
                    return _failure("failed", current_url, "too_many_redirects")
                current_url = urljoin(current_url, location)
                continue
            if response.status_code < 200 or response.status_code >= 300:
                return _failure("failed", current_url, "http_status")
            if len(response.body) > self._max_compressed_bytes:
                return _failure("too_large", current_url, "compressed_body_too_large")
            return self._extract(current_url, headers, response.body)
        return _failure("failed", current_url, "too_many_redirects")  # pragma: no cover

    def _extract(
        self, final_url: str, headers: Mapping[str, str], compressed_body: bytes
    ) -> LinkContentResult:
        try:
            body = _decompress(
                compressed_body,
                headers.get("content-encoding", ""),
                self._max_decompressed_bytes,
            )
        except _BodyTooLarge:
            return _failure("too_large", final_url, "decompressed_body_too_large")
        except (ValueError, zlib.error):
            return _failure("failed", final_url, "invalid_content_encoding")

        content_type, _, parameters = headers.get("content-type", "").partition(";")
        content_type = content_type.strip().lower()
        if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
            return _failure("unsupported", final_url, "unsupported_content_type")
        charset = "utf-8"
        match = re.search(r"charset\s*=\s*['\"]?([^\s;'\"]+)", parameters, re.I)
        if match:
            charset = match.group(1)
        try:
            decoded = body.decode(charset, errors="replace")
        except LookupError:
            decoded = body.decode("utf-8", errors="replace")

        title: str | None = None
        if content_type in {"text/html", "application/xhtml+xml"}:
            parser = _ReadableHTMLParser()
            parser.feed(decoded)
            parser.close()
            title = _normalize_text(" ".join(parser.title_parts)) or None
            text = _normalize_text(" ".join(parser.body_parts))
        else:
            text = _normalize_text(decoded)
        if not text:
            return _failure("unsupported", final_url, "no_useful_text")
        return LinkContentResult(
            status="fetched",
            final_url=final_url,
            title=title,
            text=text[: self._max_text_chars],
        )


def _request(target: ResolvedTarget, max_bytes: int, timeout_seconds: float) -> HttpResponse:
    connection_type = (
        _PinnedHTTPSConnection if target.scheme == "https" else _PinnedHTTPConnection
    )
    connection = connection_type(target, timeout_seconds)
    parsed = urlsplit(target.url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "text/html, application/xhtml+xml, text/plain;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": "XDigestLinkFetcher/1.0",
            },
        )
        response = connection.getresponse()
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise _BodyTooLarge
        return HttpResponse(
            status_code=response.status,
            headers={key.lower(): value for key, value in response.getheaders()},
            body=body,
        )
    finally:
        connection.close()


def _decompress(body: bytes, content_encoding: str, max_bytes: int) -> bytes:
    encoding = content_encoding.strip().lower()
    if not encoding or encoding == "identity":
        if len(body) > max_bytes:
            raise _BodyTooLarge
        return body
    if encoding in {"gzip", "x-gzip"}:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decompressor = zlib.decompressobj()
    else:
        raise ValueError("unsupported encoding")
    decoded = decompressor.decompress(body, max_bytes + 1)
    if len(decoded) > max_bytes or decompressor.unconsumed_tail:
        raise _BodyTooLarge
    decoded += decompressor.flush(max_bytes + 1 - len(decoded))
    if len(decoded) > max_bytes:
        raise _BodyTooLarge
    return decoded


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _failure(status: FetchStatus, final_url: str, error_code: str) -> LinkContentResult:
    return LinkContentResult(status=status, final_url=final_url, error_code=error_code)
