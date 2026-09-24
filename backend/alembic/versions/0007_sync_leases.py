"""Serialize X synchronization per List.

Revision ID: 0007_sync_leases
Revises: 0006_cost_bounded_ingestion
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_sync_leases"
down_revision: str | None = "0006_cost_bounded_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sync_runs",
        sa.Column("posts_reserved", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("UPDATE sync_runs SET posts_reserved = posts_returned")
    op.create_table(
        "sync_leases",
        sa.Column(
            "list_id",
            sa.String(length=36),
            sa.ForeignKey("x_lists.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sync_leases")
    op.drop_column("sync_runs", "posts_reserved")
