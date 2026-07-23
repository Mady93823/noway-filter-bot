"""Deterministic search service (docs.md section 8).

parse -> exact title match -> trigram fallback -> one result per movie
with quality variants grouped. Result id lists and cursors live in
Redis with a TTL; an expired cursor returns an 'expired' page instead
of silently re-running a stale query.

If the query names a quality (e.g. '... 720p'), variants are filtered
to it and titles left with no matching variant are dropped from that
page.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.db.repos import files as files_repo
from shared.db.repos import titles as titles_repo
from shared.search import cache
from shared.search.query import normalize_query, parse_query


@dataclass(frozen=True)
class FileVariant:
    file_db_id: int
    quality: str | None
    file_size: int | None
    telegram_file_id: str
    languages: tuple[str, ...] = ()
    episodes: str | None = None


@dataclass(frozen=True)
class TitleResult:
    title_id: int
    display_title: str
    year: int | None
    languages: tuple[str, ...]
    variants: tuple[FileVariant, ...]
    season: int | None = None


@dataclass(frozen=True)
class SearchPage:
    results: tuple[TitleResult, ...]
    total: int
    next_cursor: str | None
    expired: bool = False
    # Render metadata: lets the bot build prev/next buttons and a page
    # counter without re-deriving cache internals.
    qhash: str | None = None
    offset: int = 0
    query: str = ""
    # How many of `total` actually contain what the user typed. The rest
    # only cleared the trigram floor - "game over" for "game of thrones" -
    # and the card marks them off rather than passing them off as equals.
    # None means "no ladder ran", and renders as one undivided list.
    strong_total: int | None = None


@dataclass(frozen=True)
class Suggestion:
    """A "did you mean" candidate for a query that matched nothing."""

    title_id: int
    display_title: str
    year: int | None


_EMPTY_PAGE = SearchPage(results=(), total=0, next_cursor=None)
_EXPIRED_PAGE = SearchPage(results=(), total=0, next_cursor=None, expired=True)


def refinement_of(raw_query: str) -> str | None:
    """'1080p' or 'tamil' alone -> that text, meaning "narrow my last search".

    A message is a refinement only when it parses to pure metadata with
    no title left over, which is exactly the case the search service
    already treats as an empty query. Anything with a title in it is a
    new search, so "swati tamil" can never be mistaken for a refinement.

    Returns None when the message is not a bare modifier.
    """
    normalized = normalize_query(raw_query)
    if not normalized:
        return None
    parsed = parse_query(normalized)
    if parsed.title_guess:
        return None
    if parsed.quality or parsed.languages:
        return normalized
    return None


async def suggest(
    session: AsyncSession, raw_query: str, limit: int = 3
) -> tuple[Suggestion, ...]:
    """Closest titles to a query that found nothing.

    Only reached on a dead end, so a loose match beats no answer - see
    titles_repo.nearest_titles for why the threshold is below search's.
    """
    normalized = normalize_query(raw_query)
    parsed = parse_query(normalized) if normalized else None
    if parsed is None or not parsed.title_guess:
        return ()
    titles = await titles_repo.nearest_titles(
        session,
        parsed.title_guess,
        floor=get_settings().suggest_threshold,
        limit=limit,
    )
    return tuple(
        Suggestion(title_id=title.id, display_title=title.display_title, year=title.year)
        for title in titles
    )


def variant_matches(variant_quality: str | None, wanted_quality: str) -> bool:
    """True when every requested quality word appears in the variant label."""
    if not variant_quality:
        return False
    variant_words = set(variant_quality.lower().split())
    return all(word in variant_words for word in wanted_quality.lower().split())


def variant_language_ok(
    variant_languages: tuple[str, ...], wanted: tuple[str, ...]
) -> bool:
    """Language filter for one variant. Lenient on missing data: a variant
    with no recorded languages is never hidden (pre-migration rows)."""
    if not wanted or not variant_languages:
        return True
    return any(language in variant_languages for language in wanted)


async def search(
    session: AsyncSession, raw_query: str, cursor: str | None = None
) -> SearchPage:
    settings = get_settings()

    if cursor is not None:
        decoded = cache.decode_cursor(cursor)
        if decoded is None:
            return _EXPIRED_PAGE
        qhash, offset = decoded
        cached = await cache.load_results(qhash)
        if cached is None:
            # TTL passed - tell the caller to ask the user to search again
            # rather than guessing at a stale result set.
            return _EXPIRED_PAGE
        normalized, title_ids, strong = cached
        parsed = parse_query(normalized)
    else:
        normalized = normalize_query(raw_query)
        parsed = parse_query(normalized) if normalized else None
        if parsed is None or not parsed.title_guess:
            return _EMPTY_PAGE
        qhash = cache.query_hash(normalized)
        offset = 0
        title_ids, strong = await titles_repo.search_title_ids(
            session,
            guess=parsed.title_guess,
            year=parsed.year,
            languages=parsed.languages,
            threshold=settings.search_fuzzy_threshold,
            limit=settings.search_max_results,
            season=parsed.season,
        )
        if not title_ids:
            return _EMPTY_PAGE
        await cache.store_results(qhash, normalized, title_ids, strong)

    page_ids = title_ids[offset : offset + settings.search_page_size]
    titles = await titles_repo.load_titles_ordered(session, page_ids)
    variants_by_title = await files_repo.files_for_titles(session, page_ids)

    results: list[TitleResult] = []
    for title in titles:
        variants = tuple(
            FileVariant(
                file_db_id=file.id,
                quality=file.quality,
                file_size=file.file_size,
                telegram_file_id=file.telegram_file_id,
                languages=tuple(file.languages),
                episodes=file.episodes,
            )
            for file in variants_by_title.get(title.id, [])
            if (
                parsed.quality is None
                or variant_matches(file.quality, parsed.quality)
            )
            and variant_language_ok(tuple(file.languages), parsed.languages)
        )
        if not variants:
            continue
        results.append(
            TitleResult(
                title_id=title.id,
                display_title=title.display_title,
                year=title.year,
                languages=tuple(title.languages),
                variants=variants,
                season=title.season,
            )
        )

    next_offset = offset + settings.search_page_size
    next_cursor = (
        cache.encode_cursor(qhash, next_offset)
        if next_offset < len(title_ids)
        else None
    )
    return SearchPage(
        results=tuple(results),
        total=len(title_ids),
        next_cursor=next_cursor,
        qhash=qhash,
        offset=offset,
        query=normalized,
        strong_total=strong,
    )
