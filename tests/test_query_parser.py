"""Search query parsing - must decompose exactly like the indexing parser."""

from shared.search.query import normalize_query, parse_query


def test_full_pattern():
    parsed = parse_query("swati 1997 tamil")
    assert parsed.title_guess == "swati"
    assert parsed.year == 1997
    assert parsed.languages == ("tamil",)
    assert parsed.quality is None


def test_title_only():
    parsed = parse_query("ponniyin selvan")
    assert parsed.title_guess == "ponniyin selvan"
    assert parsed.year is None
    assert parsed.languages == ()


def test_alias_language_and_quality_filter():
    parsed = parse_query("swati 1997 tam 720p")
    assert parsed.title_guess == "swati"
    assert parsed.languages == ("tamil",)
    assert parsed.quality == "720p"


def test_year_like_title_query():
    parsed = parse_query("1917")
    assert parsed.title_guess == "1917"
    assert parsed.year is None


def test_normalize_query():
    assert normalize_query("  Swati   1997  TAMIL ") == "swati 1997 tamil"
