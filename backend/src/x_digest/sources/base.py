from typing import Protocol

from x_digest.sources.types import SourcePage


class XSource(Protocol):
    async def fetch_page(
        self, *, list_id: str, pagination_token: str | None, max_results: int
    ) -> SourcePage: ...
