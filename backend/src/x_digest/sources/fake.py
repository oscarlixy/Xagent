from x_digest.sources.types import SourcePage


class FakeXSource:
    """Deterministic in-memory source used for local development and tests."""

    def __init__(self, pages: dict[str | None, SourcePage]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str | None, int]] = []

    async def fetch_page(
        self, *, list_id: str, pagination_token: str | None, max_results: int
    ) -> SourcePage:
        self.calls.append((list_id, pagination_token, max_results))
        return self.pages.get(pagination_token, SourcePage())
