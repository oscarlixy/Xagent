from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from x_digest.models.content import PostState


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    """Base class for all persisted records."""


class XList(Base):
    __tablename__ = "x_lists"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform_list_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=1440, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    latest_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_seen_post_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.now, onupdate=datetime.now
    )

    memberships: Mapped[list[PostListMembership]] = relationship(back_populates="x_list")


class Author(Base):
    __tablename__ = "authors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform_author_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    profile_image_url: Mapped[str | None] = mapped_column(String(2048))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    posts: Mapped[list[Post]] = relationship(back_populates="author")


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("platform", "platform_post_id", name="uq_post_platform_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_post_id: Mapped[str] = mapped_column(String(32), nullable=False)
    author_id: Mapped[str] = mapped_column(ForeignKey("authors.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(32))
    in_reply_to_post_id: Mapped[str | None] = mapped_column(String(32))
    references_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    entities_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    media_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)

    author: Mapped[Author] = relationship(back_populates="posts")
    memberships: Mapped[list[PostListMembership]] = relationship(back_populates="post")
    state: Mapped[PostState | None] = relationship(back_populates="post", uselist=False)


class PostListMembership(Base):
    __tablename__ = "post_list_memberships"
    __table_args__ = (UniqueConstraint("post_id", "list_id", name="uq_post_list_membership"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), nullable=False)
    list_id: Mapped[str] = mapped_column(ForeignKey("x_lists.id"), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)

    post: Mapped[Post] = relationship(back_populates="memberships")
    x_list: Mapped[XList] = relationship(back_populates="memberships")
