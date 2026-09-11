from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True)
class ExtractedLink:
    original_url: str
    canonical_url: str


def canonicalize_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    hostname = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port and parsed.port not in {80, 443} else ""
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.startswith("utm_")
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme.lower(), f"{hostname}{port}", parsed.path or "/", query, ""))


def extract_links(_text: str, entities: list[dict[str, str]]) -> tuple[ExtractedLink, ...]:
    links: list[ExtractedLink] = []
    seen: set[str] = set()
    for entity in entities:
        original_url = entity.get("expanded_url") or entity.get("url")
        if not original_url:
            continue
        canonical_url = canonicalize_url(original_url)
        if canonical_url is None or canonical_url in seen:
            continue
        seen.add(canonical_url)
        links.append(ExtractedLink(original_url=original_url, canonical_url=canonical_url))
    return tuple(links)
