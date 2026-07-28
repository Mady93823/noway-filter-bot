"""Deduped posters table; titles.tmdb_id becomes an FK; drop titles.poster_url

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-28

0006 stored the poster URL directly on every title. That copies the same
artwork onto each season of a series and each size variant of a film, and
across the whole index that is a lot of duplicated text. This migration
normalizes: posters live once per TMDB entity in a new `posters` table,
keyed by tmdb_id, and titles point at them through titles.tmdb_id.

The handful of titles already enriched under 0006 keep their artwork - the
existing (tmdb_id, poster_url) pairs are lifted into posters before the
column is dropped. The enrich-everything sweep (flipping historical titles
back to 'pending') is deliberately NOT done here: it runs as a separate
step AFTER the article-fix reparse, so the sweep does not fetch posters for
title rows the reparse is about to merge away.
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "posters",
        sa.Column("tmdb_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("poster_url", sa.Text(), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("vote", sa.Float(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_posters_media_type", "posters", "media_type IN ('movie', 'tv')"
    )
    # Lift the posters 0006 already fetched into the deduped table. media_type
    # is derived from identity (a title with a season is a series); overview
    # and vote were never stored under 0006, so they start NULL and the next
    # sweep fills them. DISTINCT ON collapses seasons/variants that share a
    # tmdb_id, preferring a row that actually has a poster_url.
    op.execute(
        """
        INSERT INTO posters (tmdb_id, media_type, poster_url)
        SELECT DISTINCT ON (tmdb_id)
               tmdb_id,
               CASE WHEN season IS NOT NULL THEN 'tv' ELSE 'movie' END,
               poster_url
        FROM titles
        WHERE tmdb_id IS NOT NULL
        ORDER BY tmdb_id, (poster_url IS NULL)
        """
    )
    op.drop_column("titles", "poster_url")
    op.create_index("ix_titles_tmdb_id", "titles", ["tmdb_id"])
    op.create_foreign_key(
        "fk_titles_tmdb_id_posters",
        "titles",
        "posters",
        ["tmdb_id"],
        ["tmdb_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_titles_tmdb_id_posters", "titles", type_="foreignkey")
    op.drop_index("ix_titles_tmdb_id", table_name="titles")
    op.add_column("titles", sa.Column("poster_url", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE titles t
        SET poster_url = p.poster_url
        FROM posters p
        WHERE t.tmdb_id = p.tmdb_id
        """
    )
    op.drop_table("posters")
