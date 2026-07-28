"""Pure decision logic of the TMDB enricher - no network, no DB.

The network calls (search/_search_once) are integration territory; what
matters to get right in isolation is the media-type routing, the poster
URL construction, and the similarity guard that decides whether a TMDB
result is trustworthy enough to store.
"""

from types import SimpleNamespace

from worker.enrich import (
    STATUS_DONE,
    STATUS_NOMATCH,
    decide,
    media_type,
    poster_url,
    result_title,
)


def _title(canonical: str, *, season: int | None = None, year: int | None = None):
    # Only the fields decide()/media_type() read.
    return SimpleNamespace(canonical_title=canonical, season=season, year=year)


def test_media_type_from_season():
    assert media_type(_title("wednesday", season=1)) == "tv"
    assert media_type(_title("skyfall")) == "movie"


def test_result_title_picks_the_right_field():
    assert result_title({"title": "Skyfall"}, "movie") == "Skyfall"
    assert result_title({"name": "Wednesday"}, "tv") == "Wednesday"
    # Wrong field for the type -> empty, which decide() treats as no match.
    assert result_title({"name": "Wednesday"}, "movie") == ""


def test_poster_url_construction():
    assert poster_url("/abc.jpg") == "https://image.tmdb.org/t/p/w342/abc.jpg"
    assert poster_url(None) is None


def test_decide_none_result_is_nomatch():
    assert decide(_title("skyfall"), None, 0.6).status == STATUS_NOMATCH


def test_decide_confident_match_is_done_with_poster():
    result = {"id": 37724, "title": "Skyfall", "poster_path": "/x.jpg"}
    out = decide(_title("skyfall"), result, 0.6)
    assert out.status == STATUS_DONE
    assert out.tmdb_id == 37724
    assert out.poster_url == "https://image.tmdb.org/t/p/w342/x.jpg"


def test_decide_matched_but_no_artwork_is_still_done():
    # TMDB has the film but no poster yet: still a real match, poster None.
    result = {"id": 1, "title": "Skyfall", "poster_path": None}
    out = decide(_title("skyfall"), result, 0.6)
    assert out.status == STATUS_DONE
    assert out.poster_url is None


def test_decide_rejects_dissimilar_first_result():
    # The real production risk: no-year fallback returns a loose first hit.
    # "gunche" must NOT be stored as "Colony".
    result = {"id": 99, "title": "Colony", "poster_path": "/c.jpg"}
    assert decide(_title("gunche"), result, 0.6).status == STATUS_NOMATCH


def test_decide_series_uses_name_field():
    result = {"id": 119051, "name": "Wednesday", "poster_path": "/w.jpg"}
    out = decide(_title("wednesday", season=1), result, 0.6)
    assert out.status == STATUS_DONE
    assert out.tmdb_id == 119051
