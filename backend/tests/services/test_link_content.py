import gzip
import socket

from x_digest.security.network import ResolvedTarget
from x_digest.services.link_content import HttpResponse, LinkContentFetcher


def public_resolver(host: str, port: int, *, type: socket.SocketKind):
    assert type == socket.SOCK_STREAM
    address = "93.184.216.34" if host == "example.com" else "93.184.216.35"
    return [(socket.AF_INET, type, 6, "", (address, port))]


class StubRequester:
    def __init__(self, responses: dict[str, HttpResponse | Exception]) -> None:
        self.responses = responses
        self.targets: list[ResolvedTarget] = []

    def __call__(
        self, target: ResolvedTarget, max_bytes: int, timeout_seconds: float
    ) -> HttpResponse:
        self.targets.append(target)
        response = self.responses[target.url]
        if isinstance(response, Exception):
            raise response
        return response


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_fetch_extracts_readable_html_and_title() -> None:
    requester = StubRequester(
        {
            "https://example.com/article": HttpResponse(
                status_code=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head><title>  Useful &amp; Clear </title>"
                    b"<script>ignore me</script></head><body><nav>Menu</nav>"
                    b"<article><h1>Heading</h1><p>First paragraph.</p>"
                    b"<p>Second paragraph.</p></article></body></html>"
                ),
            )
        }
    )

    result = LinkContentFetcher(resolver=public_resolver, requester=requester).fetch(
        "https://example.com/article"
    )

    assert result.status == "fetched"
    assert result.title == "Useful & Clear"
    assert result.text == "Heading First paragraph. Second paragraph."
    assert result.final_url == "https://example.com/article"


def test_fetch_revalidates_redirect_target() -> None:
    requester = StubRequester(
        {
            "https://example.com/start": HttpResponse(
                status_code=302,
                headers={"location": "http://127.0.0.1/private"},
                body=b"",
            )
        }
    )

    result = LinkContentFetcher(resolver=public_resolver, requester=requester).fetch(
        "https://example.com/start"
    )

    assert result.status == "blocked"
    assert result.error_code == "blocked_ip"
    assert [target.ip for target in requester.targets] == ["93.184.216.34"]


def test_fetch_rejects_non_text_content() -> None:
    requester = StubRequester(
        {
            "https://example.com/file": HttpResponse(
                status_code=200,
                headers={"content-type": "application/pdf"},
                body=b"%PDF",
            )
        }
    )

    result = LinkContentFetcher(resolver=public_resolver, requester=requester).fetch(
        "https://example.com/file"
    )

    assert result.status == "unsupported"
    assert result.error_code == "unsupported_content_type"


def test_fetch_limits_decompressed_content() -> None:
    requester = StubRequester(
        {
            "https://example.com/large": HttpResponse(
                status_code=200,
                headers={"content-type": "text/plain", "content-encoding": "gzip"},
                body=gzip.compress(b"a" * 100),
            )
        }
    )

    result = LinkContentFetcher(
        resolver=public_resolver,
        requester=requester,
        max_decompressed_bytes=32,
    ).fetch("https://example.com/large")

    assert result.status == "too_large"
    assert result.error_code == "decompressed_body_too_large"


def test_fetch_returns_sanitized_network_failure() -> None:
    requester = StubRequester(
        {"https://example.com/private": TimeoutError("secret upstream detail")}
    )

    result = LinkContentFetcher(resolver=public_resolver, requester=requester).fetch(
        "https://example.com/private"
    )

    assert result.status == "failed"
    assert result.error_code == "request_failed"


def test_redirects_share_one_total_timeout_budget() -> None:
    clock = ManualClock()

    def slow_redirect(
        target: ResolvedTarget, max_bytes: int, timeout_seconds: float
    ) -> HttpResponse:
        del target, max_bytes, timeout_seconds
        clock.value = 11.0
        return HttpResponse(
            status_code=302,
            headers={"location": "https://other.example/next"},
            body=b"",
        )

    result = LinkContentFetcher(
        resolver=public_resolver,
        requester=slow_redirect,
        timeout_seconds=10,
        clock=clock,
    ).fetch("https://example.com/start")

    assert result.status == "failed"
    assert result.error_code == "request_timeout"
