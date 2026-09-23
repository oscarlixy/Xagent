"""Persist cost-bounded ingestion state."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_cost_bounded_ingestion"
down_revision: str | None = "0005_thread_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("x_lists", sa.Column("latest_observed_post_id", sa.String(length=32)))
    op.add_column(
        "sync_runs",
        sa.Column("posts_returned", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "sync_runs",
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("sync_runs", sa.Column("stop_reason", sa.String(length=64)))
    op.create_table(
        "sync_rejections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "sync_run_id",
            sa.String(length=36),
            sa.ForeignKey("sync_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_identifier", sa.String(length=64)),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sync_rejections")
    op.drop_column("sync_runs", "stop_reason")
    op.drop_column("sync_runs", "truncated")
    op.drop_column("sync_runs", "posts_returned")
    op.drop_column("x_lists", "latest_observed_post_id")
