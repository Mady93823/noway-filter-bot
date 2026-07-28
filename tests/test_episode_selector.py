"""The episode picker on a series title card.

A season indexed as one file per episode used to render as a flat wall of
buttons with no way to jump to a given episode. The episode chip row fixes
that: tap E13 and the variant list filters to it, exactly like the
resolution/audio chips already do.
"""

from bot import ui
from shared.search.cache import encode_cursor, query_hash
from shared.search.service import FileVariant, TitleResult

CURSOR = encode_cursor(query_hash("wednesday"), 0)


def _ep_variant(vid: int, episode: str, quality: str) -> FileVariant:
    return FileVariant(
        file_db_id=vid,
        quality=quality,
        file_size=150_000_000,
        telegram_file_id=f"fid{vid}",
        languages=("english",),
        episodes=episode,
    )


def _series() -> TitleResult:
    # Three episodes, two of them with two resolutions - so the episode
    # axis and the resolution axis are both real choices.
    return TitleResult(
        title_id=7,
        display_title="Wednesday",
        year=2022,
        languages=("english",),
        season=1,
        variants=(
            _ep_variant(101, "E01", "720p WEB-DL"),
            _ep_variant(102, "E01", "1080p WEB-DL"),
            _ep_variant(103, "E02", "720p WEB-DL"),
            _ep_variant(104, "E03", "1080p WEB-DL"),
        ),
    )


def _flat(keyboard):
    return [b for row in keyboard.inline_keyboard for b in row]


def test_variant_episodes_are_distinct_and_in_order():
    assert ui._variant_episodes(_series()) == ["E01", "E02", "E03"]


def test_episode_chips_appear_with_all_eps_first():
    _, keyboard = ui.build_title(_series(), CURSOR)
    flat = _flat(keyboard)
    # "All eps" + one chip per episode, the first chip active (unfiltered).
    assert "🟢 All eps" in [b.text for b in flat]
    assert any(b.text == "📺 E01" for b in flat)
    assert any(b.text == "📺 E03" for b in flat)
    # episode chips carry the index, not the label, in their callback
    data = [b.callback_data for b in flat]
    assert f"t:7:{CURSOR}:-:-:0:0" in data  # E01 -> index 0
    assert f"t:7:{CURSOR}:-:-:0:2" in data  # E03 -> index 2


def test_selecting_an_episode_filters_the_files():
    text, keyboard = ui.build_title(_series(), CURSOR, episode=0)  # E01
    data = [b.callback_data for b in _flat(keyboard)]
    gets = [d for d in data if d.startswith("get:")]
    assert gets == ["get:101", "get:102"]  # only E01's two files
    assert "<b>Files</b>   2" in text
    assert "<b>Filter</b>   E01" in text
    # the active episode chip is the green one
    assert "🟢 E01" in [b.text for b in _flat(keyboard)]


def test_episode_and_resolution_filters_compose():
    # E01 at 1080p -> exactly one file, and each chip must carry the other
    # filter through so neither tap widens the other.
    text, keyboard = ui.build_title(_series(), CURSOR, episode=0, quality="1080p")
    data = [b.callback_data for b in _flat(keyboard)]
    assert [d for d in data if d.startswith("get:")] == ["get:102"]
    # a resolution chip keeps the episode index; an episode chip keeps :1080p
    assert f"t:7:{CURSOR}:-:1080p:0:0" in data       # 1080p chip, still E01
    assert f"t:7:{CURSOR}:-:1080p:0:1" in data       # E02 chip, still 1080p


def test_out_of_range_episode_index_falls_back_to_all():
    text, keyboard = ui.build_title(_series(), CURSOR, episode=99)
    gets = [d for d in _flat(keyboard) if d.callback_data.startswith("get:")]
    assert len(gets) == 4  # every file, no filter applied
    assert "<b>Filter</b>" not in text


def test_movie_has_no_episode_chips():
    movie = TitleResult(
        title_id=9,
        display_title="Skyfall",
        year=2012,
        languages=("english",),
        variants=(_ep_variant(201, None, "1080p"),),  # episodes None
    )
    _, keyboard = ui.build_title(movie, CURSOR)
    assert not any("eps" in b.text for b in _flat(keyboard))


def test_episode_callbacks_stay_within_budget():
    _, keyboard = ui.build_title(_series(), CURSOR, episode=0, quality="1080p")
    data = [b.callback_data for b in _flat(keyboard) if b.callback_data]
    assert all(len(d.encode()) <= 64 for d in data)
