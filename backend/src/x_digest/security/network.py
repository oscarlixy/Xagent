from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

type Resolver = Callable[..., Sequence[tuple[Any, ...]]]
DEFAULT_RESOLVER = cast(Resolver, socket.getaddrinfo)


class NetworkPolicyError(ValueError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class ResolvedTarget:
    url: str
    scheme: str
    host: str
    port: int
    ip: str


def resolve_public_url(
    url: str, *, resolver: Resolver = DEFAULT_RESOLVER
) -> ResolvedTarget:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise NetworkPolicyError("unsupported_scheme")
    if parsed.username is not None or parsed.password is not None:
        raise NetworkPolicyError("credentials_not_allowed")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise NetworkPolicyError("missing_host")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise NetworkPolicyError("blocked_host")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise NetworkPolicyError("invalid_port") from exc

    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        addresses: tuple[str, ...] = (str(literal_address),)
    else:
        try:
            answers = resolver(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise NetworkPolicyError("dns_failed") from exc
        addresses = tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))
    if not addresses:
        raise NetworkPolicyError("dns_failed")
    try:
        parsed_addresses = tuple(ipaddress.ip_address(address) for address in addresses)
    except ValueError as exc:
        raise NetworkPolicyError("dns_failed") from exc
    if any(not address.is_global for address in parsed_addresses):
        raise NetworkPolicyError("blocked_ip")

    return ResolvedTarget(
        url=url,
        scheme=parsed.scheme.lower(),
        host=host,
        port=port,
        ip=str(parsed_addresses[0]),
    )
