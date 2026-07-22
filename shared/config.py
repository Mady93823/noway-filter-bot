"""Typed application settings loaded from environment / .env.

Never read os.environ directly anywhere else in the codebase.
"""

import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class DbSettings(BaseSettings):
    """Database-only settings.

    Importable without Telegram credentials so alembic migrations can run
    with nothing but DATABASE_URL configured.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://nowaybot:nowaybot@localhost:5433/nowaybot"


class Settings(DbSettings):
    """Full settings for the bot and worker services."""

    api_id: int
    api_hash: str
    bot_token: str

    redis_url: str = "redis://localhost:6380/0"

    # NoDecode: parsed by the validator below (accepts JSON list or comma-separated).
    source_channel_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    backfill_batch_size: int = Field(default=100, ge=1, le=200)
    base_batch_delay: float = Field(default=2.5, gt=0)
    # How often the worker DMs indexing progress to admins ("indexed x/x").
    progress_report_interval: int = Field(default=90, ge=10)
    fuzzy_threshold: float = Field(default=0.45, gt=0, lt=1)
    job_poll_interval: int = Field(default=15, ge=1)

    search_page_size: int = Field(default=10, ge=1, le=50)
    search_cache_ttl: int = Field(default=300, ge=10)
    search_max_results: int = Field(default=50, ge=1, le=500)
    # Looser than fuzzy_threshold on purpose: identity merge must never
    # conflate swati/swathi, but search recall should still surface them
    # both for a near-miss query.
    search_fuzzy_threshold: float = Field(default=0.3, gt=0, lt=1)
    # Looser again, and for a different job: "did you mean" only runs
    # after search has already returned nothing, where a loose guess
    # beats a dead end. Never used to decide what search itself returns.
    suggest_threshold: float = Field(default=0.15, gt=0, lt=1)
    # Distinct users who must ask for the same missing title within 24h
    # before it is reported to the log channel. Minimum 2 - reporting
    # every single failed search would bury the channel in typos.
    missing_threshold: int = Field(default=3, ge=2, le=100)
    # How long anything the bot posts in a GROUP survives before it is
    # deleted. Groups are the main surface here and result cards pile up
    # fast. PM is never auto-cleared - that is the user's own archive.
    group_message_ttl: int = Field(default=300, ge=30, le=86400)

    # Group keyword filters. The cache TTL only bounds staleness after a
    # missed invalidation - every add/remove clears its group's key
    # outright, so a change is visible immediately.
    filter_cache_ttl: int = Field(default=600, ge=10)
    # A cap keeps per-message matching bounded no matter what a group does.
    max_filters_per_group: int = Field(default=200, ge=1, le=5000)

    # Liveness endpoint. Each service sets its own port in compose.
    health_port: int = Field(default=8080, ge=1, le=65535)
    # Seconds the same fatal error stays deduped, so a crash loop DMs
    # admins once rather than thousands of times.
    alert_cooldown: int = Field(default=900, ge=30)

    @field_validator("source_channel_ids", "admin_ids", mode="before")
    @classmethod
    def _parse_id_list(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            if value.startswith("["):
                return json.loads(value)
            return [int(part) for part in value.replace(" ", "").split(",") if part]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_db_settings() -> DbSettings:
    return DbSettings()
