"""Enforce one thread per conversation and author."""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_thread_identity"
down_revision: str | None = "0004_link_retries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("threads") as batch_op:
        batch_op.create_unique_constraint(
            "uq_thread_conversation_author", ["conversation_id", "author_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("threads") as batch_op:
        batch_op.drop_constraint("uq_thread_conversation_author", type_="unique")
