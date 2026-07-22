"""Access clock + admin-editable runtime settings.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One clock for shortlink verification AND admin-granted premium, so
    # no code path has to decide which of two fields wins.
    op.add_column(
        "users", sa.Column("access_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "bot_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("bot_settings")
    op.drop_column("users", "access_until")
