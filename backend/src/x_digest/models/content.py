from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from x_digest.models.source import Base, Post, new_id


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str | None] = mapped_column(String(32), index=True)
    author_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)

    posts: Mapped[list[ThreadPost]] = relationship(back_populates="thread")


class ThreadPost(Base):
    __tablename__ = "thread_posts"
    __table_args__ = (UniqueConstraint("thread_id", "post_id", name="uq_thread_post"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"), nullable=False)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    thread: Mapped[Thread] = relationship(back_populates="posts")
    post: Mapped[Post] = relationship()


class Link(Base):
    __tablename__ = "links"
    __table_args__ = (UniqueConstraint("post_id", "canonical_url", name="uq_post_canonical_link"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), nullable=False)
    original_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    final_url: Mapped[str | None] = mapped_column(String(2048))
    fetch_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    title: Mapped[str | None] = mapped_column(String(1024))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(128))

    post: Mapped[Post] = relationship()


class Summary(Base):
    __tablename__ = "summaries"
    __table_args__ = (
        UniqueConstraint(
            "content_fingerprint",
            "model",
            "prompt_version",
            "generation",
            name="uq_summary_generation",
        ),
        UniqueConstraint("regeneration_request_id", name="uq_summary_regeneration_request"),
        CheckConstraint("importance BETWEEN 1 AND 5", name="ck_summary_importance"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    content_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    regeneration_request_id: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_points: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    topics: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    estimated_cost: Mapped[float | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(32), default="succeeded", nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)


class Digest(Base):
    __tablename__ = "digests"
    __table_args__ = (UniqueConstraint("window_key", "version", name="uq_digest_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    window_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    rendered_content: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="succeeded", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)

    items: Mapped[list[DigestItem]] = relationship(back_populates="digest")


class DigestItem(Base):
    __tablename__ = "digest_items"
    __table_args__ = (
        UniqueConstraint("digest_id", "source_type", "source_id", name="uq_digest_item"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    digest_id: Mapped[str] = mapped_column(ForeignKey("digests.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    topic: Mapped[str | None] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    digest: Mapped[Digest] = relationship(back_populates="items")


class PostState(Base):
    __tablename__ = "post_states"

    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), primary_key=True)
    is_read: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_saved: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_ignored: Mapped[bool] = mapped_column(default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)

    post: Mapped[Post] = relationship(back_populates="state")
