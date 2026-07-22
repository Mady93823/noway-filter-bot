"""Search query parsing - the deterministic fast path (docs.md section 8).

Reuses the exact same tokenizer and dictionaries as the indexing parser,
so a query decomposes identically to the filenames it should match:
year regex, bidirectional language dictionary, optional quality tokens,
remainder = title.
"""

from shared.parsing.filename import ParsedMedia, parse_media


def normalize_query(raw: str) -> str:
    """Canonical form used for hashing/caching: lowercase, single spaces."""
    return " ".join(raw.lower().split())


def parse_query(text: str) -> ParsedMedia:
    """'swati 1997 tamil 720p' -> title='swati', year=1997,
    languages=('tamil',), quality='720p'."""
    return parse_media(text)
