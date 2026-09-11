"""Create the initial X Digest schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "x_lists",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("platform_list_id", sa.String(length=32), nullable=False, unique=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("sync_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("latest_seen_at", sa.DateTime(timezone=True)),
        sa.Column("latest_seen_post_id", sa.String(length=32)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "authors",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("platform_author_id", sa.String(length=32), nullable=False, unique=True),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255)),
        sa.Column("profile_image_url", sa.String(length=2048)),
        sa.Column("metadata_json", sa.JSON()),
    )
    op.create_table(
        "posts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("platform_post_id", sa.String(length=32), nullable=False),
        sa.Column("author_id", sa.String(length=36), sa.ForeignKey("authors.id"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("conversation_id", sa.String(length=32)),
        sa.Column("in_reply_to_post_id", sa.String(length=32)),
        sa.Column("references_json", sa.JSON()),
        sa.Column("entities_json", sa.JSON()),
        sa.Column("media_json", sa.JSON()),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("platform", "platform_post_id", name="uq_post_platform_id"),
    )
    op.create_table(
        "post_list_memberships",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("post_id", sa.String(length=36), sa.ForeignKey("posts.id"), nullable=False),
        sa.Column("list_id", sa.String(length=36), sa.ForeignKey("x_lists.id"), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("post_id", "list_id", name="uq_post_list_membership"),
    )
    op.create_table(
        "threads",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=32)),
        sa.Column("author_id", sa.String(length=36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_threads_conversation_id", "threads", ["conversation_id"])
    op.create_index("ix_threads_author_id", "threads", ["author_id"])
    op.create_table(
        "thread_posts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("thread_id", sa.String(length=36), sa.ForeignKey("threads.id"), nullable=False),
        sa.Column("post_id", sa.String(length=36), sa.ForeignKey("posts.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("thread_id", "post_id", name="uq_thread_post"),
    )
    op.create_table(
        "links",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("post_id", sa.String(length=36), sa.ForeignKey("posts.id"), nullable=False),
        sa.Column("original_url", sa.String(length=2048), nullable=False),
        sa.Column("canonical_url", sa.String(length=2048), nullable=False),
        sa.Column("final_url", sa.String(length=2048)),
        sa.Column("fetch_status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=1024)),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("error_code", sa.String(length=128)),
        sa.UniqueConstraint("post_id", "canonical_url", name="uq_post_canonical_link"),
    )
    op.create_table(
        "summaries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("content_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("prompt_version", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("regeneration_request_id", sa.String(length=255)),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("key_points", sa.JSON(), nullable=False),
        sa.Column("topics", sa.JSON(), nullable=False),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("source_ids", sa.JSON(), nullable=False),
        sa.Column("token_usage", sa.JSON()),
        sa.Column("estimated_cost", sa.Float()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_code", sa.String(length=128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("importance BETWEEN 1 AND 5", name="ck_summary_importance"),
        sa.UniqueConstraint(
            "content_fingerprint",
            "model",
            "prompt_version",
            "generation",
            name="uq_summary_generation",
        ),
        sa.UniqueConstraint("regeneration_request_id", name="uq_summary_regeneration_request"),
    )
    op.create_table(
        "digests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("window_key", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("rendered_content", sa.JSON()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("window_key", "version", name="uq_digest_version"),
    )
    op.create_table(
        "digest_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("digest_id", sa.String(length=36), sa.ForeignKey("digests.id"), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("topic", sa.String(length=128)),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.JSON()),
        sa.UniqueConstraint("digest_id", "source_type", "source_id", name="uq_digest_item"),
    )
    op.create_table(
        "post_states",
        sa.Column("post_id", sa.String(length=36), sa.ForeignKey("posts.id"), primary_key=True),
        sa.Column("is_read", sa.Boolean(), nullable=False),
        sa.Column("is_saved", sa.Boolean(), nullable=False),
        sa.Column("is_ignored", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("list_id", sa.String(length=36), sa.ForeignKey("x_lists.id"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("pages_fetched", sa.Integer(), nullable=False),
        sa.Column("posts_seen", sa.Integer(), nullable=False),
        sa.Column("posts_created", sa.Integer(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=128)),
        sa.Column("debug_metadata", sa.JSON()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("digest_id", sa.String(length=36), sa.ForeignKey("digests.id"), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("provider_receipt", sa.String(length=1024)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("digest_id", "channel", name="uq_digest_delivery_channel"),
    )


def downgrade() -> None:
    op.drop_table("notification_deliveries")
    op.drop_table("sync_runs")
    op.drop_table("post_states")
    op.drop_table("digest_items")
    op.drop_table("digests")
    op.drop_table("summaries")
    op.drop_table("links")
    op.drop_table("thread_posts")
    op.drop_index("ix_threads_author_id", table_name="threads")
    op.drop_index("ix_threads_conversation_id", table_name="threads")
    op.drop_table("threads")
    op.drop_table("post_list_memberships")
    op.drop_table("posts")
    op.drop_table("authors")
    op.drop_table("x_lists")
