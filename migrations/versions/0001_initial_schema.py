"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-21

Hand-written (autogenerate cannot emit CREATE EXTENSION). Keep in sync
with shared/db/models.py.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "titles",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("canonical_title", sa.Text(), nullable=False),
        sa.Column("display_title", sa.Text(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column(
            "languages",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "canonical_title",
            "year",
            name="uq_titles_canonical_year",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_titles_canonical_trgm",
        "titles",
        ["canonical_title"],
        postgresql_using="gin",
        postgresql_ops={"canonical_title": "gin_trgm_ops"},
    )

    op.create_table(
        "files",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_file_uid", sa.Text(), nullable=False),
        sa.Column("telegram_file_id", sa.Text(), nullable=False),
        sa.Column("raw_file_name", sa.Text(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("quality", sa.Text(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("source_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["title_id"], ["titles.id"], ondelete="CASCADE"),
        # THE dedup rule: same Telegram file can never be indexed twice.
        sa.UniqueConstraint("telegram_file_uid", name="uq_files_telegram_file_uid"),
    )
    op.create_index("ix_files_title_id", "files", ["title_id"])

    op.create_table(
        "index_progress",
        sa.Column("channel_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column(
            "last_processed_message_id",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("target_message_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'running'")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('running', 'paused', 'completed', 'errored')",
            name="ck_index_progress_status",
        ),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column(
            "first_seen",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("is_banned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.create_table(
        "groups",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("settings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "filters",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column("reply", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("group_id", "keyword", name="uq_filters_group_keyword"),
    )
    op.create_index("ix_filters_group_id", "filters", ["group_id"])


def downgrade() -> None:
    op.drop_table("filters")
    op.drop_table("groups")
    op.drop_table("users")
    op.drop_table("index_progress")
    op.drop_table("files")
    op.drop_table("titles")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
