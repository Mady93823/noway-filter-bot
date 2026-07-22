"""season on titles, episode label on files

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-22

Series were collapsing: with identity keyed on (canonical_title, year)
only, every season of a show became quality variants of a single title
(real data: 16 Wednesday files under one row). Season joins the identity
key; the episode range stays on the file as a variant label.
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("titles", sa.Column("season", sa.Integer(), nullable=True))
    op.add_column("files", sa.Column("episodes", sa.Text(), nullable=True))
    op.drop_constraint("uq_titles_canonical_year", "titles", type_="unique")
    # NULLS NOT DISTINCT so two rows with the same name and unknown year
    # still conflict (a plain UNIQUE treats NULLs as always distinct).
    op.execute(
        "ALTER TABLE titles ADD CONSTRAINT uq_titles_canonical_year_season "
        "UNIQUE NULLS NOT DISTINCT (canonical_title, year, season)"
    )


def downgrade() -> None:
    op.drop_constraint("uq_titles_canonical_year_season", "titles", type_="unique")
    op.execute(
        "ALTER TABLE titles ADD CONSTRAINT uq_titles_canonical_year "
        "UNIQUE NULLS NOT DISTINCT (canonical_title, year)"
    )
    op.drop_column("files", "episodes")
    op.drop_column("titles", "season")
