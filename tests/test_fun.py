"""Fun (genz voice) copy module - pure, so fully unit-testable."""

from bot import fun
from bot import ui


def test_fun_off_delegates_to_plain_voice():
    # With fun off, every copy function must return exactly the default.
    assert fun.no_results("q", False) == ui.no_results_text("q")
    assert fun.thanks(False) == ui.thanks_text()
    assert fun.chat_help(False) == ui.chat_help_text()
    assert fun.greeting("bob", False) == ui.greeting_text("bob")


def test_count_vibe_only_at_the_extremes_and_only_in_fun():
    assert fun.count_vibe(1, True) is not None
    assert fun.count_vibe(250, True) is not None
    assert fun.count_vibe(50, True) is not None
    assert fun.count_vibe(10, True) is None  # unremarkable middle
    assert fun.count_vibe(1, False) is None  # off = never


def test_delivery_hype_gated_on_fun():
    assert fun.delivery_hype(False) is None
    assert fun.delivery_hype(True) is not None


def test_easter_eggs_match_normalised_triggers():
    assert fun.easter_egg("thanos") is not None
    assert fun.easter_egg("  THANOS  ") is not None  # case + space folded
    assert fun.easter_egg("a real movie") is None


def test_gibberish_flags_vowelless_and_symbol_junk():
    assert fun.is_gibberish("sdfghjk") is True  # no vowel
    assert fun.is_gibberish("!!!") is True  # symbols
    assert fun.is_gibberish("asdfgh") is False  # has a vowel
    assert fun.is_gibberish("up") is False  # real short title
    assert fun.is_gibberish("1917") is False


def test_fun_no_results_escapes_the_query():
    out = fun.no_results("<script>", True)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_search_reaction_is_a_known_emoji():
    assert fun.search_reaction() in fun._SEARCH_REACTIONS
