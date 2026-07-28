"""The season switcher on a series title card.

Each season is its own title row (identity includes season). When a
show-name search returns several of them on one page, the title card
grows a season chip row so a user can jump S1 -> S2 without going back
to the results list.
"""

from bot import ui
from shared.search.cache import encode_cursor, query_hash
from shared.search.service import FileVariant, TitleResult

CURSOR = encode_cursor(query_hash("wednesday"), 0)


def _variant(vid: int) -> FileVariant:
    return FileVariant(
        file_db_id=vid,
        quality="1080p WEB-DL",
        file_size=150_000_000,
        telegram_file_id=f"fid{vid}",
        languages=("english",),
    )


def _season(title_id: int, season: int) -> TitleResult:
    return TitleResult(
        title_id=title_id,
        display_title=f"Wednesday",
        year=2022,
        languages=("english",),
        variants=(_variant(title_id * 10),),
        season=season,
        canonical_title="wednesday",
    )


def _movie(title_id: int) -> TitleResult:
    return TitleResult(
        title_id=title_id,
        display_title="Skyfall",
        year=2012,
        languages=("english",),
        variants=(_variant(title_id * 10),),
        canonical_title="skyfall",
    )


def _flat(keyboard):
    return [b for row in keyboard.inline_keyboard for b in row]


def test_siblings_group_by_show_and_need_a_season():
    s1, s2 = _season(1, 1), _season(2, 2)
    other = _season(3, 1)  # a different show, same "season 1"
    other = TitleResult(**{**other.__dict__, "canonical_title": "loki"})
    results = (s1, s2, other)
    assert ui.season_siblings(results, s1) == ((1, 1), (2, 2))
    # the unrelated show sees only itself -> no switcher
    assert ui.season_siblings(results, other) == ()


def test_single_season_is_not_a_switcher():
    s1 = _season(1, 1)
    assert ui.season_siblings((s1,), s1) == ()


def test_movie_never_has_season_siblings():
    m = _movie(5)
    assert ui.season_siblings((m, _movie(6)), m) == ()


def test_season_chips_render_active_and_links():
    s1, s2 = _season(1, 1), _season(2, 2)
    seasons = ui.season_siblings((s1, s2), s1)
    _, keyboard = ui.build_title(s1, CURSOR, seasons=seasons)
    flat = _flat(keyboard)
    texts = [b.text for b in flat]
    data = [b.callback_data for b in flat]

    # current season is the active (green) chip and inert
    assert "🟢 S1" in texts
    active = next(b for b in flat if b.text == "🟢 S1")
    assert active.callback_data == ui.NOOP_CALLBACK
    # the other season is a deep link that opens its own title card
    assert "📺 S2" in texts
    assert f"t:2:{CURSOR}" in data
    # callbacks stay within Telegram's budget
    assert all(len(d.encode()) <= 64 for d in data if d)


def test_no_seasons_argument_means_no_switcher():
    s1 = _season(1, 1)
    _, keyboard = ui.build_title(s1, CURSOR)  # seasons defaults to ()
    assert not any("S1" in b.text or "S2" in b.text for b in _flat(keyboard))
