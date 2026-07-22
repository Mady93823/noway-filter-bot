"""Pure UI rendering: labels, keyboards, callback-data budget, HTML escaping."""

from bot import ui
from shared.search.cache import encode_cursor, query_hash
from shared.search.service import FileVariant, SearchPage, TitleResult


def _variant(
    vid: int,
    quality: str | None,
    size: int | None,
    languages: tuple[str, ...] = (),
) -> FileVariant:
    return FileVariant(
        file_db_id=vid,
        quality=quality,
        file_size=size,
        telegram_file_id=f"fid{vid}",
        languages=languages,
    )


def _page() -> SearchPage:
    qhash = query_hash("swati tamil")
    results = (
        TitleResult(
            title_id=1,
            display_title="Swati <1997>",
            year=1997,
            languages=("tamil",),
            variants=(
                _variant(11, "480p", 400_000_000, ("english",)),
                _variant(12, "720p WEB-DL", 900_000_000, ("hindi", "english")),
            ),
        ),
        TitleResult(
            title_id=2,
            display_title="Swathi",
            year=2005,
            languages=("tamil", "telugu"),
            variants=(_variant(21, None, None),),
        ),
    )
    return SearchPage(
        results=results,
        total=25,
        next_cursor=encode_cursor(qhash, 20),
        qhash=qhash,
        offset=10,
        query="swati tamil",
    )


def test_format_size():
    assert ui.format_size(None) == "?"
    assert ui.format_size(0) == "?"
    assert ui.format_size(400_000_000) == "381 MB"
    assert ui.format_size(1_800_000_000) == "1.68 GB"


def test_build_results_lists_titles_not_variants():
    page = _page()
    text, keyboard = ui.build_results(page, page_size=10)

    assert "page <b>2</b> of <b>3</b>" in text
    assert "<b>25</b> matches" in text
    # HTML injection from a hostile display title must be escaped
    assert "Swati &lt;1997&gt;" in text
    assert "<b>Swati &lt;1997&gt;</b>" in text
    # file counts belong on the list; the files themselves do not
    assert "2 files" in text and "1 file" in text

    flat = [button for row in keyboard.inline_keyboard for button in row]
    data = [button.callback_data for button in flat if button.callback_data]

    # every callback fits Telegram's 64-byte budget
    assert all(len(d.encode()) <= 64 for d in data)
    # one button per TITLE - the variants live behind them, not on the page
    cursor = encode_cursor(query_hash("swati tamil"), 10)
    assert [d for d in data if d.startswith("t:")] == [
        f"t:1:{cursor}",
        f"t:2:{cursor}",
    ]
    assert not any(d.startswith("get:") for d in data)
    # nav row: prev goes back to offset 0, next uses the service cursor
    prev = next(d for d in data if d.startswith("nav:") and d.endswith(":0"))
    assert prev == f"nav:{encode_cursor(query_hash('swati tamil'), 0)}"
    assert any(d.startswith("nav:") and d.endswith(":20") for d in data)
    # close button always present
    assert "x" in data
    # the page counter is a label - it must NOT reuse the close token,
    # or tapping it would delete the results it is describing
    assert "📄 2 / 3" in [b.text for b in flat]
    assert ui.NOOP_CALLBACK in data and ui.NOOP_CALLBACK != "x"
    # title buttons carry the keycap that indexes the text list
    assert [b.text for b in flat if b.callback_data.startswith("t:")] == [
        "1️⃣ Swati <1997> (1997)",
        "2️⃣ Swathi (2005)",
    ]
    # the list is quoted so Telegram draws its coloured bar down the side
    assert "<blockquote>" in text and "</blockquote>" in text
    assert "1️⃣" in text and "2️⃣" in text
    assert all(len(b.text) <= ui.MAX_BUTTON_TEXT for b in flat)


def test_build_title_lists_every_variant():
    result = _page().results[0]
    cursor = encode_cursor(query_hash("swati tamil"), 10)
    text, keyboard = ui.build_title(result, cursor)

    assert "<b>Swati &lt;1997&gt;</b>" in text
    # facts sit in one blockquote above the buttons
    assert "<blockquote>" in text and "</blockquote>" in text
    assert "<b>Files</b>   2" in text
    assert "480p" in text and "720p WEB-DL" in text  # quality summary

    flat = [button for row in keyboard.inline_keyboard for button in row]
    data = [b.callback_data for b in flat]
    assert [d for d in data if d.startswith("get:")] == ["get:11", "get:12"]
    assert f"nav:{cursor}" in data  # back to the results list
    assert "x" in data
    assert all(len(b.text) <= ui.MAX_BUTTON_TEXT for b in flat)
    # per-variant audio languages shown as short codes on the button
    assert any("eng" in b.text and "480p" in b.text for b in flat)
    assert any("hin+eng" in b.text for b in flat)


def test_single_audio_title_has_no_chips_and_no_button_codes():
    """One language across the files: nothing to filter, nothing to repeat."""
    result = TitleResult(
        title_id=3,
        display_title="Solo",
        year=2020,
        languages=("tamil",),
        variants=(
            _variant(31, "480p", 400_000_000, ("tamil",)),
            _variant(32, "720p", 900_000_000, ("tamil",)),
        ),
    )
    _, keyboard = ui.build_title(result, encode_cursor(query_hash("solo"), 0))
    flat = [b for row in keyboard.inline_keyboard for b in row]

    assert not any(b.callback_data.startswith("t:") for b in flat)
    assert not any("tam" in b.text for b in flat)


def test_language_chips_filter_the_variant_list():
    result = _page().results[0]  # english variant + hindi/english variant
    cursor = encode_cursor(query_hash("swati tamil"), 10)

    _, keyboard = ui.build_title(result, cursor)
    chips = [
        b
        for row in keyboard.inline_keyboard
        for b in row
        if b.callback_data.startswith("t:")
    ]
    assert [b.callback_data for b in chips] == [
        f"t:1:{cursor}",  # All
        f"t:1:{cursor}:eng",
        f"t:1:{cursor}:hin",
    ]
    assert chips[0].text == "🟢 All"  # unfiltered: All is the active chip

    text, keyboard = ui.build_title(result, cursor, language="hindi")
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert [d for d in data if d.startswith("get:")] == ["get:12"]
    assert "<b>Filter</b>   hindi audio" in text
    active = next(
        b
        for row in keyboard.inline_keyboard
        for b in row
        if b.callback_data == f"t:1:{cursor}:hin"
    )
    assert active.text == "🟢 hindi"


def test_unknown_audio_variant_survives_a_language_filter():
    """No recorded audio is not the same as 'not that language'."""
    result = TitleResult(
        title_id=4,
        display_title="Mixed",
        year=None,
        languages=("hindi",),
        variants=(
            _variant(41, "480p", 100, ("hindi",)),
            _variant(42, "720p", 200, ("english",)),
            _variant(43, "1080p", 300),  # languages unknown
        ),
    )
    _, keyboard = ui.build_title(
        result, encode_cursor(query_hash("mixed"), 0), language="hindi"
    )
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert [d for d in data if d.startswith("get:")] == ["get:41", "get:43"]


def test_lone_hit_skips_the_list():
    """One match would make a one-row menu - open its files directly."""
    page = _page()
    only = SearchPage(
        results=page.results[:1],
        total=1,
        next_cursor=None,
        qhash=page.qhash,
        offset=0,
        query=page.query,
    )
    text, keyboard = ui.build_results(only, page_size=10)
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]

    assert [d for d in data if d.startswith("get:")] == ["get:11", "get:12"]
    assert not any(d.startswith("nav:") for d in data)  # nothing to go back to
    assert "Tap a title" not in text
    # chips still work from a lone hit: they need the cursor even with no
    # results list behind them
    assert f"t:1:{encode_cursor(page.qhash, 0)}:hin" in data


def test_first_page_has_no_prev():
    page = _page()
    first = SearchPage(
        results=page.results,
        total=25,
        next_cursor=page.next_cursor,
        qhash=page.qhash,
        offset=0,
        query=page.query,
    )
    _, keyboard = ui.build_results(first, page_size=10)
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert not any(d == f"nav:{encode_cursor(page.qhash, 0)}" for d in data)


def test_delivery_caption_escapes_and_includes_details():
    caption = ui.delivery_caption(
        "Swati <b>", 1997, ("tamil",), "720p WEB-DL", 900_000_000
    )
    assert "Swati &lt;b&gt;" in caption
    assert "720p WEB-DL" in caption
    assert "858 MB" in caption
    assert "tamil" in caption


def test_no_results_text_escapes_query():
    assert "&lt;i&gt;" in ui.no_results_text("<i>weird</i>")
