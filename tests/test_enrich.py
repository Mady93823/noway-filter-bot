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
    result_year,
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


def test_result_year_parses_by_media_type():
    assert result_year({"release_date": "2012-10-26"}, "movie") == 2012
    assert result_year({"first_air_date": "2022-11-23"}, "tv") == 2022
    # Blank / missing / wrong-field-for-type -> None (no basis to reject).
    assert result_year({"release_date": ""}, "movie") is None
    assert result_year({"first_air_date": "2022-11-23"}, "movie") is None


def test_decide_rejects_wrong_year_edition():
    # Our "Skyfall" is 2012; a result dated 1999 is a different film reusing
    # the name - reject rather than store its poster.
    result = {"id": 5, "title": "Skyfall", "poster_path": "/x.jpg", "release_date": "1999-01-01"}
    assert decide(_title("skyfall", year=2012), result, 0.6).status == STATUS_NOMATCH


def test_decide_allows_one_year_of_slack():
    # Festival vs wide-release drift of a single year still matches.
    result = {"id": 6, "title": "Skyfall", "poster_path": "/x.jpg", "release_date": "2013-01-01"}
    assert decide(_title("skyfall", year=2012), result, 0.6).status == STATUS_DONE


def test_decide_no_year_either_side_does_not_reject():
    # Our year unknown: year check has nothing to compare, similarity decides.
    result = {"id": 7, "title": "Skyfall", "poster_path": "/x.jpg", "release_date": "1999-01-01"}
    assert decide(_title("skyfall"), result, 0.6).status == STATUS_DONE


def test_decide_carries_poster_metadata():
    result = {
        "id": 37724,
        "title": "Skyfall",
        "poster_path": "/x.jpg",
        "release_date": "2012-10-26",
        "overview": "Bond investigates an attack on MI6.",
        "vote_average": 7.5,
    }
    out = decide(_title("skyfall", year=2012), result, 0.6)
    assert out.status == STATUS_DONE
    assert out.media_type == "movie"
    assert out.overview.startswith("Bond")
    assert out.vote == 7.5
