"""TMDB enrichment fields on titles (poster, tmdb_id, status)

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-28

Posters/metadata are fetched OFFLINE by the worker's enricher, never live
in the search/index path (CLAUDE.md golden rule 1). A newly resolved title
starts 'pending'; the enricher sets it to 'done' (poster found) or 'nomatch'
(searched, absent from TMDB).

Deliberately UPGRADE-ONLY for new content: every title that already exists
when this migration runs is set to 'skip', so the enricher touches only
titles created from here on. No 1.7M-row backfill sweep.
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("titles", sa.Column("tmdb_id", sa.BigInteger(), nullable=True))
    op.add_column("titles", sa.Column("poster_url", sa.Text(), nullable=True))
    op.add_column(
        "titles",
        sa.Column(
            "enrich_status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "titles",
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_titles_enrich_status",
        "titles",
        "enrich_status IN ('pending', 'done', 'nomatch', 'skip')",
    )
    # Upcoming-only: retire every pre-existing title so the enricher never
    # sweeps the historical index. New inserts take the 'pending' default.
    op.execute("UPDATE titles SET enrich_status = 'skip'")
    # The enricher polls for pending rows; a partial index keeps that O(pending)
    # even as the 'done'/'skip' rows grow into the millions.
    op.create_index(
        "ix_titles_enrich_pending",
        "titles",
        ["id"],
        postgresql_where=sa.text("enrich_status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_titles_enrich_pending", table_name="titles")
    op.drop_constraint("ck_titles_enrich_status", "titles", type_="check")
    op.drop_column("titles", "enriched_at")
    op.drop_column("titles", "enrich_status")
    op.drop_column("titles", "poster_url")
    op.drop_column("titles", "tmdb_id")
