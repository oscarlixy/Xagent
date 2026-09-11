from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ThreadPostView:
    id: str
    author_id: str
    created_at: datetime
    conversation_id: str | None
    in_reply_to_post_id: str | None


@dataclass(frozen=True)
class ThreadGroup:
    id: str
    posts: tuple[ThreadPostView, ...]


def assemble_threads(posts: Sequence[ThreadPostView]) -> tuple[ThreadGroup, ...]:
    """Group only explicit conversation metadata; never infer from posting time."""

    grouped: dict[tuple[str, str], list[ThreadPostView]] = {}
    for post in posts:
        conversation_id = post.conversation_id or post.id
        grouped.setdefault((conversation_id, post.author_id), []).append(post)

    return tuple(
        ThreadGroup(
            id=f"{conversation_id}:{author_id}",
            posts=tuple(sorted(group, key=lambda post: (post.created_at, post.id))),
        )
        for (conversation_id, author_id), group in sorted(grouped.items())
    )
