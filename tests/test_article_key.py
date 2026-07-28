"""Leading-article normalization for the matching key.

The recurring bug: "The Wicked Within" (2015) got fuzzy-merged into a
pre-existing "A Wicked Within", and a search for the real name then found
no exact match. Keying both off "wicked within" makes article variants
resolve as one title by exact match, and makes the search land.
"""

from shared.parsing.filename import strip_leading_article


def test_the_a_an_are_stripped():
    assert strip_leading_article("the wicked within") == "wicked within"
    assert strip_leading_article("a wicked within") == "wicked within"
    assert strip_leading_article("an american tail") == "american tail"


def test_article_variants_share_one_key():
    # The whole point: the/a/none collapse to the same canonical.
    assert (
        strip_leading_article("the wicked within")
        == strip_leading_article("a wicked within")
        == strip_leading_article("wicked within")
    )


def test_no_leading_article_is_unchanged():
    assert strip_leading_article("wicked within") == "wicked within"
    assert strip_leading_article("game of thrones") == "game of thrones"


def test_a_bare_article_is_a_real_one_word_title():
    # "A" (2018) and "The" as whole titles must not vanish to empty.
    assert strip_leading_article("the") == "the"
    assert strip_leading_article("a") == "a"
    assert strip_leading_article("an") == "an"


def test_only_the_leading_article_goes():
    # An article deeper in the title is part of the name.
    assert strip_leading_article("dude wheres the car") == "dude wheres the car"
    # Doubled leading article: only the first is peeled, the rest is the name.
    assert strip_leading_article("the a team") == "a team"
