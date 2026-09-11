from x_digest.services.link_extraction import extract_links


def test_extract_links_prefers_expanded_url_and_removes_tracking_parameters() -> None:
    links = extract_links(
        "Read https://t.co/example",
        [{"url": "https://t.co/example", "expanded_url": "https://Example.com/path?utm_source=x&id=7#part"}],
    )

    assert [link.canonical_url for link in links] == ["https://example.com/path?id=7"]


def test_extract_links_rejects_non_http_urls_and_deduplicates() -> None:
    links = extract_links(
        "",
        [
            {"expanded_url": "javascript:alert(1)"},
            {"expanded_url": "https://example.com/article"},
            {"expanded_url": "https://example.com/article#fragment"},
        ],
    )

    assert [link.canonical_url for link in links] == ["https://example.com/article"]
