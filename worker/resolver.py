"""Local title resolution (docs.md sections 6-7). Zero external APIs.

Exact match -> pg_trgm fuzzy match above the confidence threshold ->
create a new canonical title from the guess itself. Every new clean
filename improves the canonical pool that future truncated variants
fuzzy-match against.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.db.models import Title
from shared.db.repos import titles as titles_repo
from shared.parsing.filename import ParsedMedia


def _display_from_guess(guess: str) -> str:
    return guess.title()


async def resolve_title(session: AsyncSession, parsed: ParsedMedia) -> Title:
    guess = parsed.title_guess.lower().strip()
    # Season is carried through every lookup: name matching alone must
    # never merge two seasons of one show into a single title.
    title = await titles_repo.find_exact(session, guess, parsed.year, parsed.season)
    if title is None:
        title = await titles_repo.find_fuzzy(
            session,
            guess,
            parsed.year,
            get_settings().fuzzy_threshold,
            parsed.season,
        )
    if title is not None:
        await titles_repo.merge_metadata(
            session,
            title,
            languages=parsed.languages,
            display_candidate=_display_from_guess(guess),
        )
        return title
    return await titles_repo.get_or_create(
        session,
        canonical=guess,
        display=_display_from_guess(guess),
        year=parsed.year,
        languages=list(parsed.languages),
        season=parsed.season,
    )
