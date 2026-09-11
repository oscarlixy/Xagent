from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RawAuthor:
    id: str
    username: str
    display_name: str | None = None


@dataclass(frozen=True)
class RawPost:
    id: str
    author: RawAuthor
    text: str
    created_at: datetime
    source_url: str
    conversation_id: str | None = None
    in_reply_to_post_id: str | None = None
    references: list[dict[str, Any]] | None = None
    entities: dict[str, Any] | None = None
    media: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class RejectedItem:
    identifier: str | None
    reason: str


@dataclass(frozen=True)
class SourcePage:
    posts: tuple[RawPost, ...] = ()
    rejected_items: tuple[RejectedItem, ...] = ()
    next_token: str | None = None
