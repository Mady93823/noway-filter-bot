"""The word-subset guard that keeps two different films apart.

Real case that started this: title 208's canonical was "karuppu pulsar"
and it had swallowed the four real "Karuppu" (2026) files, because
similarity("karuppu", "karuppu pulsar") ~= 0.53 clears the 0.45 fuzzy
floor. The guess "karuppu" is a strict word-subset of "karuppu pulsar",
so the merge is a different-identity collision, not a typo.
"""

from worker.resolver import _distinct_named_thing


def test_short_name_absorbed_into_superset_is_rejected():
    # The exact production bug, both directions of who was indexed first.
    assert _distinct_named_thing("karuppu", "karuppu pulsar")
    assert _distinct_named_thing("karuppu pulsar", "karuppu")


def test_two_word_franchise_names_stay_separate():
    assert _distinct_named_thing("iron man", "iron man 3")
    assert _distinct_named_thing("vikram", "vikram vedha")


def test_long_title_truncation_still_merges():
    # Dropping a trailing word from a long name is what fuzzy is FOR.
    assert not _distinct_named_thing(
        "spider man no way", "spider man no way home"
    )


def test_same_word_completion_is_not_a_subset():
    # "swat" -> "swati" is one token growing, never a word-subset.
    assert not _distinct_named_thing("swat", "swati")


def test_identical_titles_are_not_distinct():
    assert not _distinct_named_thing("karuppu", "karuppu")


def test_unrelated_overlap_is_not_a_subset():
    # Shared word but neither is a subset of the other -> normal trigram
    # ranking decides, the guard stays out of it.
    assert not _distinct_named_thing("karuppu petti", "karuppu aadu")
