"""Keyword matching for group filters - pure logic, no Telegram, no DB."""

from bot.handlers.group_filters import match_keyword, normalize


def test_normalize_folds_case_and_punctuation():
    assert normalize("  New MOVIES!!  ") == "new movies"
    assert normalize("Spider-Man: No Way Home") == "spider man no way home"


def test_whole_word_match_only():
    """'her' must not fire on 'here' - the classic filter-bot annoyance."""
    assert match_keyword("is she here yet", ["her"]) is None
    assert match_keyword("give it to her", ["her"]) == "her"


def test_multi_word_keyword():
    keywords = ["new movies"]
    assert match_keyword("any new movies today?", keywords) == "new movies"
    assert match_keyword("new films today", keywords) is None


def test_punctuation_around_keyword_still_matches():
    assert match_keyword("rules?", ["rules"]) == "rules"
    assert match_keyword("(rules)", ["rules"]) == "rules"


def test_first_keyword_wins_and_empty_set_is_no_match():
    assert match_keyword("read the rules and faq", ["rules", "faq"]) == "rules"
    assert match_keyword("anything at all", []) is None
