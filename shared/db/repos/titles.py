"""Title lookups and creation - local resolution only, no external APIs.

Fuzzy matching uses pg_trgm: the % operator rides the GIN index to
prefilter candidates, then similarity() enforces our stricter threshold.
"""

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Title


async def _align_trigram_threshold(session: AsyncSession, threshold: float) -> None:
    """Make the % operator as strict as the similarity() filter that follows.

    % tests against the pg_trgm.similarity_threshold GUC (0.3 by default),
    not against our threshold - so left alone, the GIN index hands back
    every row scoring above 0.3 and the recheck throws most of them away.
    Measured at 100k titles, on a query whose leading token was common to
    the whole corpus: the index returned all 100,000 rows and the query
    took 391ms. Aligned, the index returns 41k, the result set is
    identical, and the query takes 186ms.

    SET LOCAL, so it reverts when the session's transaction ends and can
    never leak into another request on a pooled connection.
    """
    # SET takes no bind parameters; a float cannot carry an injection.
    await session.execute(
        text(f"SET LOCAL pg_trgm.similarity_threshold = {float(threshold)}")
    )


async def find_exact(
    session: AsyncSession, canonical: str, year: int | None, season: int | None = None
) -> Title | None:
    query = select(Title).where(Title.canonical_title == canonical)
    # Season is matched strictly (NULL == NULL): a season-2 file must
    # never land on the season-1 row, and never on the movie row.
    query = query.where(Title.season.is_not_distinct_from(season))
    if year is not None:
        # Accept an exact-year row or a year-unknown row; prefer exact year.
        query = query.where(or_(Title.year == year, Title.year.is_(None)))
        query = query.order_by(Title.year.is_(None))
    else:
        query = query.order_by(Title.id)
    return await session.scalar(query.limit(1))


async def find_fuzzy(
    session: AsyncSession,
    guess: str,
    year: int | None,
    threshold: float,
    season: int | None = None,
) -> Title | None:
    await _align_trigram_threshold(session, threshold)
    similarity = func.similarity(Title.canonical_title, guess)
    query = (
        select(Title)
        # % prefilter uses the trigram GIN index; similarity() then applies
        # the configured (stricter) confidence threshold.
        .where(Title.canonical_title.op("%")(guess))
        .where(similarity > threshold)
        # Same strictness as find_exact: fuzzy name matching must not
        # bridge two different seasons.
        .where(Title.season.is_not_distinct_from(season))
        .order_by(similarity.desc())
        .limit(1)
    )
    if year is not None:
        query = query.where(or_(Title.year == year, Title.year.is_(None)))
    return await session.scalar(query)


async def search_title_ids(
    session: AsyncSession,
    *,
    guess: str,
    year: int | None,
    languages: tuple[str, ...],
    threshold: float,
    limit: int,
    season: int | None = None,
) -> list[int]:
    """Deterministic search ladder (docs.md section 8), all index-backed:

    1. exact canonical match
    2. substring containment (the trigram GIN index accelerates ILIKE) -
       partial queries work: 'wednesday' finds every Wednesday pack and
       'sheep detective' finds 'the sheep detectives'
    3. whole-string trigram similarity - typo tolerance

    Returns an ordered, de-duplicated id list - the thing cached in
    Redis for pagination.
    """

    def _apply_filters(query):
        if year is not None:
            query = query.where(or_(Title.year == year, Title.year.is_(None)))
        if languages:
            query = query.where(Title.languages.overlap(list(languages)))
        # Only narrows when the user actually named a season ("wednesday
        # s2"); a bare "wednesday" still returns every season.
        if season is not None:
            query = query.where(Title.season == season)
        return query

    ids: list[int] = []

    def _absorb(found) -> None:
        for title_id in found:
            if title_id not in ids:
                ids.append(title_id)

    exact_query = (
        _apply_filters(select(Title.id).where(Title.canonical_title == guess))
        .order_by(Title.id)
        .limit(limit)
    )
    _absorb((await session.scalars(exact_query)).all())

    if len(ids) < limit and len(guess) >= 3:
        pattern = (
            "%"
            + guess.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        substring_query = (
            _apply_filters(
                select(Title.id).where(
                    Title.canonical_title.ilike(pattern, escape="\\")
                )
            )
            # Shortest matching title first: for the words the user gave,
            # the tightest title is the best interpretation.
            .order_by(func.char_length(Title.canonical_title), Title.id)
            .limit(limit)
        )
        _absorb((await session.scalars(substring_query)).all())

    if len(ids) < limit:
        await _align_trigram_threshold(session, threshold)
        similarity = func.similarity(Title.canonical_title, guess)
        fuzzy_query = (
            _apply_filters(
                select(Title.id)
                .where(Title.canonical_title.op("%")(guess))
                .where(similarity > threshold)
            )
            .order_by(similarity.desc(), Title.id)
            .limit(limit)
        )
        _absorb((await session.scalars(fuzzy_query)).all())

    return ids[:limit]


async def nearest_titles(
    session: AsyncSession, guess: str, *, floor: float, limit: int
) -> list[Title]:
    """Closest titles for a query that matched nothing - "did you mean".

    Runs at a deliberately lower floor than search itself: this only ever
    fires when the normal ladder returned zero rows, so the choice is
    between a loose guess and a dead end. Aligning the GUC keeps even
    this loose match on the GIN index instead of a sequential scan.
    """
    await _align_trigram_threshold(session, floor)
    similarity = func.similarity(Title.canonical_title, guess)
    query = (
        select(Title)
        .where(Title.canonical_title.op("%")(guess))
        .where(similarity > floor)
        .order_by(similarity.desc(), Title.id)
        .limit(limit)
    )
    return list((await session.scalars(query)).all())


async def load_titles_ordered(session: AsyncSession, ids: list[int]) -> list[Title]:
    """Fetch titles by id, preserving the given (relevance) order."""
    if not ids:
        return []
    rows = (await session.scalars(select(Title).where(Title.id.in_(ids)))).all()
    by_id = {title.id: title for title in rows}
    return [by_id[title_id] for title_id in ids if title_id in by_id]


async def get_title(session: AsyncSession, title_id: int) -> Title | None:
    return await session.get(Title, title_id)


async def get_or_create(
    session: AsyncSession,
    *,
    canonical: str,
    display: str,
    year: int | None,
    languages: list[str],
    season: int | None = None,
) -> Title:
    """Insert a new canonical title; on a concurrent duplicate, fetch it.

    Race-free via the (canonical_title, year, season) unique constraint -
    no scan-before-insert.
    """
    insert_stmt = (
        pg_insert(Title)
        .values(
            canonical_title=canonical,
            display_title=display,
            year=year,
            season=season,
            languages=languages,
        )
        .on_conflict_do_nothing(constraint="uq_titles_canonical_year_season")
        .returning(Title)
    )
    title = await session.scalar(insert_stmt)
    if title is not None:
        return title
    result = await session.scalar(
        select(Title)
        .where(Title.canonical_title == canonical)
        .where(Title.year.is_not_distinct_from(year))
        .where(Title.season.is_not_distinct_from(season))
        .limit(1)
    )
    assert result is not None  # conflict implies the row exists
    return result


async def merge_metadata(
    session: AsyncSession,
    title: Title,
    *,
    languages: tuple[str, ...],
    display_candidate: str | None,
) -> None:
    """Enrich an existing title from a newly indexed file (docs.md section 7).

    Languages are unioned; the display title upgrades to the most complete
    string seen. Year is deliberately never rewritten here - flipping a
    NULL year could collide with another (canonical, year) row.
    """
    merged = list(title.languages)
    for language in languages:
        if language not in merged:
            merged.append(language)
    if merged != list(title.languages):
        title.languages = merged
    if display_candidate and len(display_candidate) > len(title.display_title):
        title.display_title = display_candidate
