"""Persist canonical digest window boundaries."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_digest_windows"
down_revision: str | None = "0002_oauth_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("digests", sa.Column("window_start_at", sa.DateTime(timezone=True)))
    op.add_column("digests", sa.Column("window_end_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("digests", "window_end_at")
    op.drop_column("digests", "window_start_at")
