"""Bidirectional language dictionary behavior."""

from shared.parsing.languages import (
    aliases_for,
    canonical_language,
    languages_in_text,
)


def test_alias_to_canonical():
    assert canonical_language("tam") == "tamil"
    assert canonical_language("tel") == "telugu"
    assert canonical_language("hin") == "hindi"
    assert canonical_language("mal") == "malayalam"
    assert canonical_language("kan") == "kannada"
    assert canonical_language("eng") == "english"


def test_canonical_maps_to_itself():
    assert canonical_language("tamil") == "tamil"
    assert canonical_language("TAMIL") == "tamil"


def test_reverse_direction():
    assert "tam" in aliases_for("tamil")
    assert "eng" in aliases_for("english")


def test_unknown_token():
    assert canonical_language("klingon") is None


def test_languages_in_text_dedup_and_order():
    found = languages_in_text("Tamil + Telugu + tam dubbed")
    assert found == ["tamil", "telugu"]
