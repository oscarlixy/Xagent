from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from x_digest.models.content import Digest
from x_digest.models.source import Base, XList, new_id


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    list_id: Mapped[str] = mapped_column(ForeignKey("x_lists.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    posts_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    posts_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    posts_returned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))
    debug_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    x_list: Mapped[XList] = relationship()
    rejections: Mapped[list[SyncRejection]] = relationship(
        back_populates="sync_run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SyncRejection(Base):
    __tablename__ = "sync_rejections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    sync_run_id: Mapped[str] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False
    )
    item_identifier: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)

    sync_run: Mapped[SyncRun] = relationship(back_populates="rejections")


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (UniqueConstraint("digest_id", "channel", name="uq_digest_delivery_channel"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    digest_id: Mapped[str] = mapped_column(ForeignKey("digests.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_receipt: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.now, onupdate=datetime.now
    )

    digest: Mapped[Digest] = relationship()
