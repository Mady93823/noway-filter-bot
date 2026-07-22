"""per-file audio languages

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-22

One Hindi+English variant must not mark every variant of the title as
Hindi. titles.languages remains the union (search matching); this
column records what each file actually carries.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "files",
        sa.Column(
            "languages",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("files", "languages")
