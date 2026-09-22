import socket

import pytest

from x_digest.security.network import NetworkPolicyError, resolve_public_url


def resolver_for(*addresses: str):
    def resolve(host: str, port: int, *, type: socket.SocketKind):
        assert host
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                type,
                6,
                "",
                (address, port),
            )
            for address in addresses
        ]

    return resolve


@pytest.mark.parametrize(
    ("url", "addresses", "error_code"),
    [
        ("http://127.0.0.1/admin", ("127.0.0.1",), "blocked_ip"),
        ("http://service.local/data", ("93.184.216.34",), "blocked_host"),
        (
            "http://user:secret@example.com/",
            ("93.184.216.34",),
            "credentials_not_allowed",
        ),
        ("file:///etc/passwd", (), "unsupported_scheme"),
        (
            "https://example.com/article",
            ("93.184.216.34", "10.0.0.7"),
            "blocked_ip",
        ),
    ],
)
def test_resolve_public_url_rejects_unsafe_targets(
    url: str, addresses: tuple[str, ...], error_code: str
) -> None:
    with pytest.raises(NetworkPolicyError) as exc_info:
        resolve_public_url(url, resolver=resolver_for(*addresses))

    assert exc_info.value.error_code == error_code


def test_resolve_public_url_returns_a_pinned_public_target() -> None:
    target = resolve_public_url(
        "https://Example.com:8443/article?q=1",
        resolver=resolver_for("93.184.216.34"),
    )

    assert target.url == "https://Example.com:8443/article?q=1"
    assert target.host == "example.com"
    assert target.port == 8443
    assert target.ip == "93.184.216.34"
