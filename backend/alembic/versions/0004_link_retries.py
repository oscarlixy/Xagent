"""Persist bounded link retry state."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_link_retries"
down_revision: str | None = "0003_digest_windows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "links",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("links", sa.Column("next_attempt_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("links", "next_attempt_at")
    op.drop_column("links", "attempt_count")
