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
from shared.parsing.filename import ParsedMedia, strip_leading_article


def _display_from_guess(guess: str) -> str:
    return guess.title()


def _distinct_named_thing(guess: str, canonical: str) -> bool:
    """True when a fuzzy hit is a different title, not a typo/truncation.

    Trigram similarity happily merges a name into a superset of its own
    words: "karuppu" vs "karuppu pulsar" scores ~0.53 and clears the 0.45
    floor, yet they are two different films - the shorter one ended up
    hidden inside the longer, wearing the longer one's name. Guard it by
    WORDS, not characters: when one title's tokens are a strict subset of
    the other's AND the short side is one or two words, an added word is a
    new identity ("vikram" -> "vikram vedha", "iron man" -> "iron man 3").

    Dropping a trailing word from a LONG title ("spider man no way" for
    "spider man no way home") is a real truncation and must still merge,
    so the guard only fires when the shorter side is <= 2 tokens. A same-
    word completion ("swat" -> "swati") is not a subset at the token level
    and is untouched.
    """
    g = frozenset(guess.split())
    c = frozenset(canonical.split())
    if g == c or not (g <= c or c <= g):
        return False
    return min(len(g), len(c)) <= 2


async def resolve_title(session: AsyncSession, parsed: ParsedMedia) -> Title:
    guess = parsed.title_guess.lower().strip()
    # The MATCHING key drops a leading the/a/an so "the wicked within" and
    # "a wicked within" resolve to one canonical instead of fuzzy-colliding
    # into whichever was indexed first. The display keeps the full name.
    canonical = strip_leading_article(guess)
    # Season is carried through every lookup: name matching alone must
    # never merge two seasons of one show into a single title.
    title = await titles_repo.find_exact(session, canonical, parsed.year, parsed.season)
    if title is None:
        title = await titles_repo.find_fuzzy(
            session,
            canonical,
            parsed.year,
            get_settings().fuzzy_threshold,
            parsed.season,
        )
        # A fuzzy hit that is only a word-superset/subset of the guess is a
        # different title; fall through to create the guess as its own row.
        if title is not None and _distinct_named_thing(canonical, title.canonical_title):
            title = None
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
        canonical=canonical,
        display=_display_from_guess(guess),
        year=parsed.year,
        languages=list(parsed.languages),
        season=parsed.season,
    )
