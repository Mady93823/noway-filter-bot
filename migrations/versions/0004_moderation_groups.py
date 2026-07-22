"""ban metadata on users, active flag on groups

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-22

users.is_banned existed since 0001 but nothing ever set or read it.
Ban enforcement needs to report *why* and *when*, and group bookkeeping
needs to survive the bot being removed from a chat without dropping that
group's filters.
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("banned_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("ban_reason", sa.Text(), nullable=True))
    op.add_column(
        "groups",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("groups", "is_active")
    op.drop_column("users", "ban_reason")
    op.drop_column("users", "banned_at")
