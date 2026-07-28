"""The gate that decides whether a results card is sent as a photo.

Only a lone hit with a stored poster may become a photo card; a
multi-result list must stay text (it has no single poster, and Telegram
cannot edit a text list into a photo).
"""

from bot.ui import results_photo_url
from shared.search.service import SearchPage, TitleResult


def _result(title_id: int, poster: str | None) -> TitleResult:
    return TitleResult(
        title_id=title_id,
        display_title=f"Title {title_id}",
        year=2026,
        languages=(),
        variants=(),
        poster_url=poster,
    )


def _page(results, total) -> SearchPage:
    return SearchPage(results=tuple(results), total=total, next_cursor=None)


def test_lone_hit_with_poster_sends_photo():
    page = _page([_result(1, "https://image.tmdb.org/t/p/w342/x.jpg")], total=1)
    assert results_photo_url(page) == "https://image.tmdb.org/t/p/w342/x.jpg"


def test_lone_hit_without_poster_stays_text():
    assert results_photo_url(_page([_result(1, None)], total=1)) is None


def test_multi_result_never_a_photo_even_if_first_has_poster():
    page = _page([_result(1, "https://poster/1.jpg"), _result(2, None)], total=2)
    assert results_photo_url(page) is None


def test_empty_page_is_text():
    assert results_photo_url(_page([], total=0)) is None
