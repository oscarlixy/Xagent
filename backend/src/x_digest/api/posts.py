from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from x_digest.api.auth import require_internal_token
from x_digest.api.errors import APIError
from x_digest.api.schemas import (
    AuthorResponse,
    PostPageResponse,
    PostResponse,
    PostStateResponse,
    PostStateUpdate,
)
from x_digest.models import Author, Post, PostListMembership, PostState, Summary

router = APIRouter(prefix="/api/posts", dependencies=[Depends(require_internal_token)])


def _session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


class PostService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find(
        self,
        *,
        list_id: str | None,
        topic: str | None,
        author: str | None,
        from_at: datetime | None,
        to_at: datetime | None,
        state: Literal["read", "saved", "ignored"] | None,
        cursor: str | None,
        limit: int,
    ) -> PostPageResponse:
        if from_at is not None and to_at is not None:
            try:
                reversed_window = from_at > to_at
            except TypeError as error:
                raise APIError(
                    422,
                    "invalid_time_window",
                    "Start time must not be after end time",
                ) from error
            if reversed_window:
                raise APIError(
                    422,
                    "invalid_time_window",
                    "Start time must not be after end time",
                )
        statement = select(Post).join(Author)
        if list_id is not None:
            statement = statement.join(PostListMembership).where(
                PostListMembership.list_id == list_id
            )
        if author is not None:
            statement = statement.where(Author.username == author)
        if from_at is not None:
            statement = statement.where(Post.created_at >= from_at)
        if to_at is not None:
            statement = statement.where(Post.created_at <= to_at)
        if state is not None:
            state_column = {
                "read": PostState.is_read,
                "saved": PostState.is_saved,
                "ignored": PostState.is_ignored,
            }[state]
            statement = statement.join(PostState).where(state_column.is_(True))
        if cursor is not None:
            cursor_time, cursor_id = _decode_cursor(cursor)
            statement = statement.where(
                or_(
                    Post.created_at < cursor_time,
                    and_(Post.created_at == cursor_time, Post.id < cursor_id),
                )
            )
        posts = self._session.scalars(
            statement.distinct().order_by(Post.created_at.desc(), Post.id.desc())
        ).all()
        topics_by_post = self._topics_by_post()
        if topic is not None:
            posts = [post for post in posts if topic in topics_by_post.get(post.id, set())]
        selected = posts[: limit + 1]
        has_more = len(selected) > limit
        selected = selected[:limit]
        next_cursor = _encode_cursor(selected[-1]) if has_more and selected else None
        return PostPageResponse(
            items=[self._response(post, topics_by_post.get(post.id, set())) for post in selected],
            next_cursor=next_cursor,
        )

    def update_state(self, post_id: str, payload: PostStateUpdate) -> PostStateResponse:
        if self._session.get(Post, post_id) is None:
            raise APIError(404, "post_not_found", "Post not found")
        post_state = self._session.get(PostState, post_id)
        if post_state is None:
            post_state = PostState(post_id=post_id)
            self._session.add(post_state)
        values = payload.model_dump(exclude_unset=True)
        if "read" in values:
            post_state.is_read = values["read"]
        if "saved" in values:
            post_state.is_saved = values["saved"]
        if "ignored" in values:
            post_state.is_ignored = values["ignored"]
        self._session.commit()
        self._session.refresh(post_state)
        return _state_response(post_state)

    def _topics_by_post(self) -> dict[str, set[str]]:
        topics: dict[str, set[str]] = {}
        for summary in self._session.scalars(select(Summary).where(Summary.status == "succeeded")):
            for source_id in summary.source_ids:
                topics.setdefault(source_id, set()).update(summary.topics)
        return topics

    @staticmethod
    def _response(post: Post, topics: set[str]) -> PostResponse:
        return PostResponse(
            id=post.id,
            platform=post.platform,
            platform_post_id=post.platform_post_id,
            text=post.text,
            created_at=post.created_at,
            source_url=post.source_url,
            author=AuthorResponse.model_validate(post.author),
            state=_state_response(post.state),
            topics=sorted(topics),
        )


def _state_response(state: PostState | None) -> PostStateResponse:
    return PostStateResponse(
        read=state.is_read if state is not None else False,
        saved=state.is_saved if state is not None else False,
        ignored=state.is_ignored if state is not None else False,
    )


def _encode_cursor(post: Post) -> str:
    raw = json.dumps([post.created_at.isoformat(), post.id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], str):
            raise ValueError
        return datetime.fromisoformat(value[0]), value[1]
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise APIError(422, "invalid_cursor", "Cursor is invalid") from error


@router.get("", response_model=PostPageResponse)
def list_posts(
    list_id: str | None = None,
    topic: str | None = None,
    author: str | None = None,
    from_at: datetime | None = Query(default=None, alias="from"),
    to_at: datetime | None = Query(default=None, alias="to"),
    state: Literal["read", "saved", "ignored"] | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(_session),
) -> PostPageResponse:
    return PostService(session).find(
        list_id=list_id,
        topic=topic,
        author=author,
        from_at=from_at,
        to_at=to_at,
        state=state,
        cursor=cursor,
        limit=limit,
    )


@router.post("/{post_id}/state", response_model=PostStateResponse)
def update_post_state(
    post_id: str, payload: PostStateUpdate, session: Session = Depends(_session)
) -> PostStateResponse:
    return PostService(session).update_state(post_id, payload)
