"""Offline TMDB enrichment: give newly-resolved titles a poster + tmdb_id.

Runs entirely OUTSIDE the search/index hot path (CLAUDE.md golden rule 1):
the worker polls titles the resolver marked 'pending', fetches from TMDB in
a gentle background trickle, and writes the poster URL onto the title. Search
later reads that stored URL - it never calls TMDB itself.

Match method, proven against a 200-file channel sample (see
memory/tmdb-enrichment-findings.md):
  - endpoint by identity: a title with a season is a series (/search/tv),
    otherwise a movie (/search/movie);
  - query WITH the year, and if that returns nothing, retry WITHOUT it -
    TMDB's year filter is strict and rejects ~80% of otherwise-valid hits;
  - guard the no-year fallback with a title-similarity floor, so a loose
    first result ("gunche" -> "Colony") is rejected rather than stored.

A transient network error is never written back: the title stays 'pending'
and the next sweep retries it. Only a real answer ('done' with a match, or
'nomatch' when TMDB genuinely has nothing) is committed.
"""

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher

import httpx

from shared.config import Settings
from shared.db.engine import get_session_factory
from shared.db.models import Title
from shared.db.repos import titles as titles_repo

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"
# w342: sharp on a phone, ~30-50 KB, cheap to cache. Bump to w500 for a hero.
POSTER_SIZE = "w342"

# Terminal statuses written back to titles.enrich_status.
STATUS_DONE = "done"
STATUS_NOMATCH = "nomatch"


@dataclass(frozen=True)
class EnrichResult:
    status: str
    tmdb_id: int | None = None
    poster_url: str | None = None


def media_type(title: Title) -> str:
    """A season means a series; no season means a movie."""
    return "tv" if title.season is not None else "movie"


def result_title(result: dict, media_type: str) -> str:
    """TMDB names the display field differently for movie vs tv."""
    key = "name" if media_type == "tv" else "title"
    return result.get(key) or ""


def poster_url(poster_path: str | None) -> str | None:
    return f"{TMDB_IMAGE_BASE}/{POSTER_SIZE}{poster_path}" if poster_path else None


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def decide(title: Title, result: dict | None, min_similarity: float) -> EnrichResult:
    """Turn a TMDB search result into what to store. Pure - unit-testable.

    A missing result, or one whose title is too dissimilar to our guess,
    is a 'nomatch'. A confident match is 'done' with its tmdb_id and poster
    (poster may still be None if TMDB has the film but no artwork yet).
    """
    if result is None:
        return EnrichResult(STATUS_NOMATCH)
    name = result_title(result, media_type(title))
    if not name or _similar(title.canonical_title, name) < min_similarity:
        return EnrichResult(STATUS_NOMATCH)
    return EnrichResult(
        STATUS_DONE,
        tmdb_id=result.get("id"),
        poster_url=poster_url(result.get("poster_path")),
    )


def make_client(settings: Settings) -> httpx.AsyncClient:
    """HTTP client pre-loaded with TMDB auth. Bearer (v4) preferred."""
    headers = {"accept": "application/json"}
    if settings.tmdb_bearer:
        headers["Authorization"] = f"Bearer {settings.tmdb_bearer}"
    return httpx.AsyncClient(base_url=TMDB_BASE, headers=headers, timeout=15.0)


async def _search_once(
    client: httpx.AsyncClient, mt: str, query: str, year: int | None, settings: Settings
) -> dict | None:
    params: dict[str, str] = {
        "query": query,
        "include_adult": "false",
        "language": settings.tmdb_language,
    }
    if not settings.tmdb_bearer and settings.tmdb_api_key:
        params["api_key"] = settings.tmdb_api_key
    if year is not None:
        params["first_air_date_year" if mt == "tv" else "primary_release_year"] = str(year)
    response = await client.get(f"/search/{mt}", params=params)
    response.raise_for_status()
    results = response.json().get("results") or []
    return results[0] if results else None


async def search(
    client: httpx.AsyncClient, mt: str, query: str, year: int | None, settings: Settings
) -> dict | None:
    """Year-filtered search, falling back to no-year when it comes up empty."""
    hit = await _search_once(client, mt, query, year, settings)
    if hit is None and year is not None:
        hit = await _search_once(client, mt, query, None, settings)
    return hit


async def enrich_title(
    client: httpx.AsyncClient, title: Title, settings: Settings
) -> EnrichResult:
    mt = media_type(title)
    result = await search(client, mt, title.canonical_title, title.year, settings)
    return decide(title, result, settings.tmdb_min_similarity)


async def run_batch(client: httpx.AsyncClient, settings: Settings) -> int:
    """Enrich one batch of pending titles. Returns how many were processed.

    A network error aborts the batch (raised to the caller to back off);
    the untouched titles simply stay 'pending' for the next sweep.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        pending = await titles_repo.list_pending_enrichment(
            session, settings.enrich_batch_size
        )
    if not pending:
        return 0

    for title in pending:
        outcome = await enrich_title(client, title, settings)
        async with session_factory() as session:
            async with session.begin():
                await titles_repo.set_enrichment(
                    session,
                    title.id,
                    status=outcome.status,
                    tmdb_id=outcome.tmdb_id,
                    poster_url=outcome.poster_url,
                )
        if outcome.status == STATUS_DONE and outcome.poster_url:
            logger.info(
                "enriched title %s (%r): poster saved %s",
                title.id,
                title.canonical_title,
                outcome.poster_url,
            )
        else:
            logger.debug(
                "enriched title %s (%r) -> %s",
                title.id,
                title.canonical_title,
                outcome.status,
            )
    return len(pending)
