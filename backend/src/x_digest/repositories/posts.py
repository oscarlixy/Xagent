from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from x_digest.models import Author, Post, PostListMembership
from x_digest.repositories.transactions import ensure_sqlite_outer_transaction


@dataclass(frozen=True)
class AuthorInput:
    platform_author_id: str
    username: str
    display_name: str | None = None
    profile_image_url: str | None = None
    metadata_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class PostInput:
    platform_post_id: str
    author: AuthorInput
    text: str
    created_at: datetime
    source_url: str
    platform: str = "x"
    conversation_id: str | None = None
    in_reply_to_post_id: str | None = None
    references_json: list[dict[str, Any]] | None = None
    entities_json: dict[str, Any] | None = None
    media_json: list[dict[str, Any]] | None = None


def upsert_post(session: Session, post_input: PostInput) -> tuple[Post, bool]:
    ensure_sqlite_outer_transaction(session)
    post_query = select(Post).where(
        Post.platform == post_input.platform,
        Post.platform_post_id == post_input.platform_post_id,
    )
    post = session.scalar(post_query)
    if post is not None:
        return post, False

    author = session.scalar(
        select(Author).where(Author.platform_author_id == post_input.author.platform_author_id)
    )
    if author is None:
        author = Author(
            platform_author_id=post_input.author.platform_author_id,
            username=post_input.author.username,
            display_name=post_input.author.display_name,
            profile_image_url=post_input.author.profile_image_url,
            metadata_json=post_input.author.metadata_json,
        )
        try:
            with session.begin_nested():
                session.add(author)
                session.flush()
        except IntegrityError:
            author = session.scalar(
                select(Author).where(
                    Author.platform_author_id == post_input.author.platform_author_id
                )
            )
            if author is None:
                raise
    else:
        author.username = post_input.author.username
        author.display_name = post_input.author.display_name
        author.profile_image_url = post_input.author.profile_image_url
        author.metadata_json = post_input.author.metadata_json

    post = Post(
        platform=post_input.platform,
        platform_post_id=post_input.platform_post_id,
        author_id=author.id,
        text=post_input.text,
        created_at=post_input.created_at,
        source_url=post_input.source_url,
        conversation_id=post_input.conversation_id,
        in_reply_to_post_id=post_input.in_reply_to_post_id,
        references_json=post_input.references_json,
        entities_json=post_input.entities_json,
        media_json=post_input.media_json,
    )
    try:
        with session.begin_nested():
            session.add(post)
            session.flush()
    except IntegrityError:
        winner = session.scalar(post_query)
        if winner is None:
            raise
        return winner, False
    return post, True


def attach_to_list(session: Session, post_id: str, list_id: str) -> bool:
    ensure_sqlite_outer_transaction(session)
    membership = session.scalar(
        select(PostListMembership).where(
            PostListMembership.post_id == post_id,
            PostListMembership.list_id == list_id,
        )
    )
    if membership is not None:
        return False
    try:
        with session.begin_nested():
            session.add(PostListMembership(post_id=post_id, list_id=list_id))
            session.flush()
    except IntegrityError:
        if session.scalar(
            select(PostListMembership).where(
                PostListMembership.post_id == post_id,
                PostListMembership.list_id == list_id,
            )
        ) is None:
            raise
        return False
    return True
