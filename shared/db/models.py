"""SQLAlchemy 2.0 models - the single source of truth for the schema.

Kept in sync with migrations/ (initial migration is hand-written because
autogenerate cannot emit CREATE EXTENSION pg_trgm).
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JobStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERRORED = "errored"


class Title(Base):
    """One row per resolved movie identity (docs.md section 5).

    canonical_title is lowercase-normalized for matching; display_title is
    the most complete raw title seen so far (docs.md section 7).
    """

    __tablename__ = "titles"
    __table_args__ = (
        # season is part of the IDENTITY: "Wednesday S01" and "Wednesday
        # S02" are two titles, not two quality variants of one.
        UniqueConstraint(
            "canonical_title",
            "year",
            "season",
            name="uq_titles_canonical_year_season",
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "ix_titles_canonical_trgm",
            "canonical_title",
            postgresql_using="gin",
            postgresql_ops={"canonical_title": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    canonical_title: Mapped[str] = mapped_column(Text, nullable=False)
    display_title: Mapped[str] = mapped_column(Text, nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    # NULL for movies; set for one season of a series.
    season: Mapped[int | None] = mapped_column(Integer)
    languages: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class File(Base):
    """One row per quality/size variant of a title.

    telegram_file_uid (Telegram file_unique_id) carries the UNIQUE
    constraint - dedup is atomic at the DB level, never a pre-check scan.
    telegram_file_id is the bot-scoped send handle used for delivery.
    """

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("titles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    telegram_file_uid: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    telegram_file_id: Mapped[str] = mapped_column(Text, nullable=False)
    raw_file_name: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    quality: Mapped[str | None] = mapped_column(Text)
    # Episode label of THIS variant ("E01-E04", "E12", "Part 1"). Never
    # part of the title identity - a variant tag, like quality.
    episodes: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(Text)
    # Audio languages of THIS variant. titles.languages stays the union
    # across variants (search matching); display uses this per-file list.
    languages: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    source_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IndexProgress(Base):
    """One row per channel backfill job - what makes indexing resumable.

    Checkpointed every batch; on worker startup every 'running' row is
    resumed automatically from last_processed_message_id (golden rule 7).
    """

    __tablename__ = "index_progress"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'paused', 'completed', 'errored')",
            name="ck_index_progress_status",
        ),
    )

    channel_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    last_processed_message_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    target_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'running'")
    )
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_banned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Why and when, so /banned reports more than a bare id.
    banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ban_reason: Mapped[str | None] = mapped_column(Text)
    # ONE access clock for both routes in: completing a shortlink sets it
    # to now + access_hours, an admin grant sets it to now + 30d/1y. Every
    # check reads this single field, so the two can never disagree about
    # whether someone is allowed a file. NULL = never had access.
    access_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    # False once the bot is removed. The row and its filters survive, so
    # re-adding the bot restores the group's setup instead of making
    # admins rebuild it - a kick is often accidental.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Filter(Base):
    """Group keyword filters - one table for ALL groups (never per-group)."""

    __tablename__ = "filters"
    __table_args__ = (
        UniqueConstraint("group_id", "keyword", name="uq_filters_group_keyword"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    keyword: Mapped[str] = mapped_column(Text, nullable=False)
    reply: Mapped[dict] = mapped_column(JSONB, nullable=False)


class BotSetting(Base):
    """Runtime config an admin sets from chat, not from the environment.

    Anything an admin can change while the bot is running lives here -
    log channel, shortener credentials, whether the gate is on, how many
    hours a verification buys. It has to be in Postgres rather than .env
    because a /setlog must take effect without a redeploy, and both the
    bot and the worker have to see the same value (golden rule 4).

    Deployment-time facts - API keys, database URLs, admin ids - stay in
    the environment. The split is "who changes it and when", not "is it
    a secret".
    """

    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
