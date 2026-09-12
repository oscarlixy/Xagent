from datetime import UTC, datetime

from sqlalchemy import DateTime, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from x_digest.models.source import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class OAuthCredential(Base):
    """Encrypted OAuth tokens stored once per provider."""

    __tablename__ = "oauth_credentials"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    encrypted_access_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_refresh_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
