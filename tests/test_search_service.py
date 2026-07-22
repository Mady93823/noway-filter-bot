"""Pure parts of the search service: cursor codec + variant filtering."""

from shared.search.cache import decode_cursor, encode_cursor, query_hash
from shared.search.service import variant_language_ok, variant_matches


def test_cursor_roundtrip():
    qhash = query_hash("swati 1997 tamil")
    cursor = encode_cursor(qhash, 20)
    assert decode_cursor(cursor) == (qhash, 20)


def test_cursor_garbage_rejected():
    assert decode_cursor("nonsense") is None
    assert decode_cursor("shorthash:5") is None
    assert decode_cursor("abcdefabcdef:notanumber") is None
    assert decode_cursor("") is None


def test_query_hash_stable_and_short():
    first = query_hash("swati 1997 tamil")
    assert first == query_hash("swati 1997 tamil")
    assert len(first) == 12
    # qhash + ':' + offset must stay well under Telegram's 64-byte
    # callback_data limit.
    assert len(encode_cursor(first, 999999)) < 64


def test_variant_matches_resolution():
    assert variant_matches("720p WEB-DL", "720p")
    assert not variant_matches("1080p BluRay", "720p")
    assert variant_matches("720p WEB-DL", "720p web-dl")
    assert not variant_matches(None, "720p")


def test_variant_language_filter():
    # query names hindi: english-only variant hidden, hindi+english kept
    assert not variant_language_ok(("english",), ("hindi",))
    assert variant_language_ok(("hindi", "english"), ("hindi",))
    # no language in query: everything passes
    assert variant_language_ok(("english",), ())
    # pre-migration rows without recorded languages are never hidden
    assert variant_language_ok((), ("hindi",))
