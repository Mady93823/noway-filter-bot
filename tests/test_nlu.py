"""Conversational layer: filler stripping, intent, refinement detection.

The theme of these tests is that none of it may ever hide a real film.
Every word list here collides with an actual title, so the cases that
matter most are the ones proving a collision still gets searched.
"""

from shared.search.nlu import Intent, clean_query, detect_intent
from shared.search.service import refinement_of


def test_filler_is_stripped_from_a_request():
    cleaned = clean_query("bro send swati movie plz")
    assert cleaned.text == "swati"
    assert cleaned.changed is True


def test_cleaning_a_plain_query_changes_nothing():
    cleaned = clean_query("swati 1997 tamil")
    assert cleaned.text == "swati 1997 tamil"
    # changed=False is what tells the caller not to bother re-searching
    # the identical string a second time.
    assert cleaned.changed is False


def test_all_filler_input_is_handed_back_untouched():
    # "send me the movie bro" has no title in it. Returning "" would
    # search for nothing; returning the original lets the miss path run.
    cleaned = clean_query("send me the movie bro")
    assert cleaned.text == "send me the movie bro"
    assert cleaned.changed is False


def test_a_title_made_of_filler_words_is_still_searchable():
    # "Scary Movie" is a real franchise: cleaning strips "movie", but it
    # reports changed=True, which is exactly the flag that makes the
    # handler retry the untouched text before giving up.
    cleaned = clean_query("scary movie")
    assert cleaned.text == "scary"
    assert cleaned.changed is True


def test_greeting_alone_is_a_greeting():
    assert detect_intent("hi") is Intent.GREETING
    assert detect_intent("hello bro") is Intent.GREETING


def test_greeting_word_with_a_title_is_a_search():
    # "Hello" is a 2017 Telugu film and "Hello World" a real title - a
    # greeting only counts when the whole message is greeting words, and
    # even then only after the index came back empty.
    assert detect_intent("hello world") is Intent.SEARCH
    assert detect_intent("hi nanna") is Intent.SEARCH


def test_thanks_and_help_are_recognised():
    assert detect_intent("thanks bro") is Intent.THANKS
    assert detect_intent("tq") is Intent.THANKS
    assert detect_intent("how do i search here") is Intent.HELP
    assert detect_intent("bro send movie plz") is Intent.HELP


def test_an_ordinary_query_is_a_search():
    assert detect_intent("swati 1997 tamil") is Intent.SEARCH
    assert detect_intent("wednesday s2") is Intent.SEARCH


def test_bare_modifier_is_a_refinement():
    assert refinement_of("1080p") == "1080p"
    assert refinement_of("tamil") == "tamil"
    assert refinement_of("720p tamil") == "720p tamil"


def test_anything_with_a_title_is_not_a_refinement():
    # The distinction that keeps refinement safe: leftover title text
    # means a new search, never a filter on the previous one.
    assert refinement_of("swati tamil") is None
    assert refinement_of("wednesday") is None
    assert refinement_of("") is None
