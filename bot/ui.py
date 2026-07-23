"""Presentation layer - every user-visible string and keyboard in one place.

Results are TWO levels, not one. A page of 10 titles can hold well over
a hundred quality variants between them; rendering every variant as its
own button made the card unreadable and blew past what Telegram will
draw. So level 1 lists titles (one button each, page_size rows), level 2
opens a single title and lists its variants.

Level 2 paginates too, for the same reason level 1 does: one fully
indexed season is dozens of files, and dumping them all into a single
keyboard is exactly the wall of buttons the two-level split existed to
prevent. Audio and resolution chips sit above the list so narrowing is
always cheaper than paging.

Callback data stays tiny (Telegram caps it at 64 bytes):
    nav:<qhash>:<offset>                  turn a results page
    t:<title_id>:<qhash>:<offset>         open one title (offset = page to return to)
    t:<title_id>:<qhash>:<offset>:<lang>  same, audio-filtered ('hin', 'tam', ...)
    t:<id>:<qhash>:<offset>:<lang>:<res>:<page>
                                          full state; '-' means that
                                          filter is off, page indexes the
                                          title's own variant list
    get:<file_db_id>                      deliver one quality variant
    dym:<title_id>                        search a "did you mean" suggestion
    hlp / abt / hom                       start-menu navigation
    nop                                   inert label (the page counter)
    x                                     close (delete) a results message

HTML parse mode everywhere; user-supplied text is always escaped.
Telegram HTML is a small set - b, i, u, s, code, blockquote, spoiler, a.
No <br>, no nesting of blockquote, no custom emoji without Premium.
"""

from html import escape
from math import ceil

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from shared.parsing.languages import aliases_for
from shared.parsing.quality import RESOLUTION_TOKENS
from shared.search.cache import encode_cursor
from shared.search.service import SearchPage, Suggestion, TitleResult

# Wide enough for a full variant row ("E01-E04 · 1080p WEB-DL · 2.96 GB ·
# hin+eng" is 44); Telegram wraps rather than clipping, and cutting the
# audio codes off the end was worse than a two-line button.
MAX_BUTTON_TEXT = 48
MAX_LANGS_SHOWN = 3
MAX_QUALITIES_SHOWN = 4
CHIPS_PER_ROW = 3

# A title is one row per FILE, and a fully indexed season is routinely 40+
# files - which drew a keyboard nobody could use and, past 100 rows, one
# Telegram refuses to draw at all. Variants paginate like the results list
# does; a screenful at a time.
VARIANTS_PER_PAGE = 8

# Placeholder for "no filter" in a positional callback field. No language
# code or resolution label is ever "-", so it can never collide.
NO_FILTER = "-"

# Canonical resolutions ("1080p", "720p", ...) as the parser emits them.
# Chips filter on resolution alone so "1080p WEB-DL" and "1080p BluRay"
# collapse into one chip instead of two near-identical ones.
_RESOLUTIONS = frozenset(RESOLUTION_TOKENS.values())

# Keycap digits for the results list. Telegram renders these as single
# glyphs, which reads far better than "1." and survives truncation.
_INDEX_MARKS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")

# A button that exists to be looked at, not pressed (the page counter).
# It needs its own token: reusing "x" made tapping the counter delete the
# whole results message.
NOOP_CALLBACK = "nop"

# Separates titles that really contain the query from ones that merely
# scored above the trigram floor. Without it a search for "game of
# thrones" lists its one true hit and then "game over", "the hating game"
# and "the key game" in the same voice, and the reader has no way to know
# which is which.
CLOSE_MATCH_DIVIDER = "<i>─────  🤔 close matches  ─────</i>"


def format_size(size: int | None) -> str:
    if not size:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit != "GB" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def start_text(mention: str) -> str:
    return (
        f"👋 Hey {mention}!\n\n"
        "🎬 I'm your <b>movie vault</b>. Send me a movie name and I'll dig "
        "out every quality variant I've indexed — instantly.\n\n"
        "✨ <b>Best results:</b> <code>name year language</code>\n"
        "   e.g. <code>swati 1997 tamil</code>\n\n"
        "👇 Take a look around:"
    )


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔍 How to search", callback_data="hlp"),
                InlineKeyboardButton("ℹ️ About", callback_data="abt"),
            ]
        ]
    )


def help_text() -> str:
    return (
        "🔍 <b>How to search</b>\n\n"
        "Just type what you're looking for:\n\n"
        "▫️ <code>swati</code> — title only\n"
        "▫️ <code>swati 1997</code> — pin the year\n"
        "▫️ <code>swati 1997 tamil</code> — pin the language\n"
        "▫️ <code>swati 1997 tamil 720p</code> — pin the quality\n\n"
        "💡 Language shortcuts work too: <code>tam</code>, <code>tel</code>, "
        "<code>hin</code>, <code>mal</code>, <code>kan</code>…\n\n"
        "Each result lists every quality variant — tap one and the file "
        "lands in your chat. 📥"
    )


def about_text() -> str:
    return (
        "ℹ️ <b>About this bot</b>\n\n"
        "⚡ Lightning search over a self-hosted index\n"
        "🗂 Every quality variant preserved — nothing merged away\n"
        "🔒 No third-party metadata APIs, no tracking\n\n"
        "Built for speed. Type a movie name to begin."
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data="hom")]]
    )


def no_results_text(query: str) -> str:
    return (
        f"😕 Nothing found for <b>{escape(query)}</b>\n\n"
        "💡 Try the full pattern: <code>name year language</code>\n"
        "   e.g. <code>swati 1997 tamil</code>\n\n"
        "Check the spelling — or the movie may not be indexed yet."
    )


def expired_text() -> str:
    return "⌛ These results expired. Send your search again 🔁"


def build_suggestions(
    query: str, suggestions: tuple[Suggestion, ...]
) -> tuple[str, InlineKeyboardMarkup]:
    """"Nothing found - did you mean ...?" with one button per candidate.

    The button carries only the title id; tapping it runs a real search
    for that title rather than opening it directly, so the result gets a
    proper cursor and the same keyboard as any other search.
    """
    lines = [
        f"😕 Nothing found for <b>{escape(query)}</b>",
        "",
        "<i>🤔 Did you mean one of these?</i>",
    ]
    rows = [
        [
            InlineKeyboardButton(
                _truncate(
                    f"🎬 {item.display_title}"
                    + (f" ({item.year})" if item.year else "")
                ),
                callback_data=f"dym:{item.title_id}",
            )
        ]
        for item in suggestions
    ]
    rows.append([InlineKeyboardButton("✖️ Close", callback_data="x")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_gate(short_url: str, hours: int) -> tuple[str, InlineKeyboardMarkup]:
    """The unlock card shown when a gated user taps a file.

    It states the reward before the ask - "how long do I get" is the only
    question that matters to someone looking at an ad gate, and burying
    it under instructions is what makes these feel like a scam.
    """
    text = (
        "🔒 <b>One quick step</b>\n\n"
        f"Open the link below and finish it — you'll get <b>unlimited "
        f"files for {hours} hour{'s' if hours != 1 else ''}</b>.\n\n"
        "<blockquote>1️⃣  Tap <b>Unlock</b>\n"
        "2️⃣  Wait for the countdown, press <i>continue</i>\n"
        "3️⃣  You land back here — tap your file again</blockquote>\n\n"
        "<i>⏳ The link is valid for 30 minutes.</i>"
    )
    rows = [
        [InlineKeyboardButton("🔓 Unlock", url=short_url)],
        [InlineKeyboardButton("💎 My plan", callback_data="plan")],
    ]
    return text, InlineKeyboardMarkup(rows)


def access_granted_text(hours: int, until_text: str) -> str:
    return (
        "✅ <b>You're in!</b>\n\n"
        f"🎬 Unlimited files for the next <b>{hours} hour"
        f"{'s' if hours != 1 else ''}</b>.\n"
        f"⏳ <b>Expires in</b> {until_text}\n\n"
        "<i>Tap your file again — or search for something new.</i>"
    )


def plan_text(remaining: str | None) -> str:
    """/myplan. Says what to do next in both states, not just the status."""
    if remaining is None:
        return (
            "💤 <b>No active access</b>\n\n"
            "Tap any file and you'll get a one-step unlock link.\n"
            "<i>Searching is always free.</i>"
        )
    return (
        "💎 <b>Access active</b>\n\n"
        f"⏳ <b>Time left</b>   {remaining}\n\n"
        "<i>Unlimited files until then. Enjoy 🎬</i>"
    )


def premium_granted_text(remaining: str) -> str:
    """Sent to the user when an admin grants them access unprompted."""
    return (
        "💎 <b>Premium activated</b>\n\n"
        f"⏳ <b>Time left</b>   {remaining}\n\n"
        "<i>Unlimited files, no unlock links. Enjoy 🎬</i>"
    )


def verify_failed_text() -> str:
    """A token that was already used, expired, or belongs to someone else.

    All three collapse into one message on purpose: telling a stranger
    which of those it was only helps them work out what to try next.
    """
    return (
        "⚠️ <b>That link didn't work</b>\n\n"
        "It may have expired or already been used.\n"
        "<i>Tap any file again to get a fresh one.</i>"
    )


def plan_alert(remaining: str | None) -> str:
    """Same status as /myplan, sized for a callback alert popup.

    Telegram truncates these hard, so it says the one fact and stops.
    """
    if remaining is None:
        return "💤 No active access yet — tap a file to unlock."
    return f"💎 Access active · {remaining} left"


def gate_unavailable_text() -> str:
    """Shortener failed. Never silently lets the file through.

    Failing open would switch the gate off for everyone the moment the
    provider had an outage - and could be induced on purpose.
    """
    return (
        "⚠️ <b>Couldn't create your unlock link</b>\n\n"
        "The link service isn't responding right now. "
        "Please try again in a minute — the admins have been notified."
    )


def greeting_text(mention: str) -> str:
    """Only ever sent when the index had no film by that name.

    Worth restating because "Hello" and "Hi" are both real film titles -
    a greeting reply that pre-empted the search would hide them.
    """
    return (
        f"👋 Hey {mention}!\n\n"
        "🎬 Send me a <b>movie name</b> and I'll pull every quality "
        "variant I've got.\n\n"
        "✨ <i>Sharpest results:</i> <code>name year language</code>\n"
        "   e.g. <code>swati 1997 tamil</code>"
    )


def thanks_text() -> str:
    return "🤗 Anytime! Send another name whenever you need one 🎬"


def chat_help_text() -> str:
    """Reply to "how do i use this" and to messages that are pure filler.

    Separate from the /help card on purpose: someone who typed "bro send
    movie" needs one example, not the full command reference.
    """
    return (
        "🎬 <b>Just send the movie name</b> — that's the whole trick.\n\n"
        "✨ <b>Sharpest:</b> <code>name year language</code>\n"
        "   <code>swati 1997 tamil</code>\n\n"
        "🎚 Add a quality to narrow it: <code>1080p</code>\n"
        "💬 Or send just <code>1080p</code> / <code>tamil</code> right "
        "after a search to filter what you already found."
    )


def _lang_codes(languages: tuple[str, ...]) -> str:
    """Compact per-variant audio tag: 'hin+eng', 'tam+tel+1'."""
    codes = []
    for language in languages[:2]:
        aliases = aliases_for(language)
        codes.append(aliases[1] if len(aliases) > 1 else language[:3])
    extra = len(languages) - 2
    return "+".join(codes) + (f"+{extra}" if extra > 0 else "")


def _truncate(label: str) -> str:
    if len(label) > MAX_BUTTON_TEXT:
        return label[: MAX_BUTTON_TEXT - 1] + "…"
    return label


def _variant_button(variant, show_languages: bool = True) -> InlineKeyboardButton:
    # Episode label leads: for a series it is what tells two variants
    # apart, so it must survive the length truncation.
    parts = [
        piece
        for piece in (
            variant.episodes,
            variant.quality or "Original",
            format_size(variant.file_size),
            _lang_codes(variant.languages)
            if show_languages and variant.languages
            else None,
        )
        if piece
    ]
    label = "📥 " + " · ".join(parts)
    return InlineKeyboardButton(
        _truncate(label), callback_data=f"get:{variant.file_db_id}"
    )


def season_label(season: int | None) -> str:
    """' · Season 2' for a series row, empty for a movie."""
    return f" · Season {season}" if season is not None else ""


def _languages_line(languages: tuple[str, ...]) -> str:
    if not languages:
        return "language n/a"
    shown = languages[:MAX_LANGS_SHOWN]
    extra = len(languages) - len(shown)
    return " · ".join(shown) + (f" +{extra}" if extra else "")


def _files_word(count: int) -> str:
    return f"{count} file{'s' if count != 1 else ''}"


def _headline(result: TitleResult) -> str:
    """'<b>Title</b> <i>(2025)</i> · 🎞 <i>Season 2</i>', escaped."""
    parts = [f"<b>{escape(result.display_title)}</b>"]
    if result.year:
        parts.append(f"<i>({result.year})</i>")
    if result.season is not None:
        parts.append(f"·  🎞 <i>Season {result.season}</i>")
    return " ".join(parts)


def _index_mark(index: int) -> str:
    """Keycap digit for a list position, so text and button pair visually.

    The eye jumps from 3️⃣ in the list straight to the 3️⃣ button without
    reading either. Only 1-10 have keycaps; search_page_size may legally
    go to 50, so anything past the table falls back to a plain number
    rather than rendering as tofu.
    """
    return _INDEX_MARKS[index - 1] if 1 <= index <= len(_INDEX_MARKS) else f"{index}."


def _title_line(index: int, result: TitleResult) -> str:
    # An untagged title says nothing useful with "🗣 language n/a" - the
    # row is tidier carrying only the file count it can actually vouch for.
    meta = [f"💾 {_files_word(len(result.variants))}"]
    if result.languages:
        meta.insert(0, f"🗣 {escape(_languages_line(result.languages))}")
    return (
        f"{_index_mark(index)}  {_headline(result)}\n"
        f"       <i>{'   ·   '.join(meta)}</i>"
    )


def _title_button(index: int, result: TitleResult, cursor: str) -> InlineKeyboardButton:
    # The mark is the only anchor back to the numbered text list, so it
    # leads and the title gets truncated instead.
    tail = f" ({result.year})" if result.year else ""
    tail += f" S{result.season}" if result.season is not None else ""
    label = _truncate(f"{_index_mark(index)} {result.display_title}{tail}")
    return InlineKeyboardButton(
        label, callback_data=f"t:{result.title_id}:{cursor}"
    )


def _variant_languages(result: TitleResult) -> list[str]:
    """Audio languages actually present on this title's files, in order."""
    seen: list[str] = []
    for variant in result.variants:
        for language in variant.languages:
            if language not in seen:
                seen.append(language)
    return seen


def short_code(language: str) -> str:
    """'hindi' -> 'hin'. The chip callback carries this, not the full name."""
    aliases = aliases_for(language)
    return aliases[1] if len(aliases) > 1 else language[:3]


def _variant_has_language(variant, language: str) -> bool:
    # Lenient the same way the search service is: a file with no recorded
    # audio is never hidden, because "unknown" is not "not it".
    return not variant.languages or language in variant.languages


def resolution_of(quality: str | None) -> str | None:
    """'1080p WEB-DL' -> '1080p'. None when the label carries no resolution."""
    if not quality:
        return None
    return next((word for word in quality.split() if word in _RESOLUTIONS), None)


def _variant_resolutions(result: TitleResult) -> list[str]:
    """Resolutions present on this title's files, best first.

    Descending because that is the order people scan for: someone opening
    a title is far more often after the 1080p than the 360p.
    """
    seen = {
        resolution
        for resolution in (resolution_of(v.quality) for v in result.variants)
        if resolution
    }
    return sorted(seen, key=lambda label: int(label.rstrip("pk")), reverse=True)


def title_callback(
    title_id: int,
    cursor: str,
    *,
    language: str | None = None,
    quality: str | None = None,
    page: int = 0,
) -> str:
    """Callback data for one state of the title view.

    Emits the shortest form that expresses the state, so the common cases
    stay far inside Telegram's 64-byte budget and older 4-/5-field buttons
    still round-trip:

        t:<id>:<qhash>:<off>                      everything, first page
        t:<id>:<qhash>:<off>:<lang>               one audio language
        t:<id>:<qhash>:<off>:<lang>:<qual>:<page> full state, '-' = unset
    """
    code = short_code(language) if language else NO_FILTER
    if quality is None and page == 0:
        if language is None:
            return f"t:{title_id}:{cursor}"
        return f"t:{title_id}:{cursor}:{code}"
    return f"t:{title_id}:{cursor}:{code}:{quality or NO_FILTER}:{page}"


def _chip_rows(chips: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [chips[i : i + CHIPS_PER_ROW] for i in range(0, len(chips), CHIPS_PER_ROW)]


def _language_chips(
    result: TitleResult, cursor: str, active: str | None, quality: str | None
) -> list[list[InlineKeyboardButton]]:
    """Audio filter row(s). Empty when the title has nothing to choose between.

    Every chip carries the quality filter through unchanged - switching
    audio must not silently widen the resolution the user already picked -
    and resets to the first page, because the old page number means
    nothing once the list behind it changed.
    """
    languages = _variant_languages(result)
    if len(languages) < 2:
        return []

    chips = [
        InlineKeyboardButton(
            "🟢 All" if active is None else "🌐 All",
            callback_data=title_callback(result.title_id, cursor, quality=quality),
        )
    ]
    for language in languages:
        mark = "🟢 " if language == active else ""
        chips.append(
            InlineKeyboardButton(
                _truncate(f"{mark}{language}"),
                callback_data=title_callback(
                    result.title_id, cursor, language=language, quality=quality
                ),
            )
        )
    return _chip_rows(chips)


def _quality_chips(
    result: TitleResult, cursor: str, active: str | None, language: str | None
) -> list[list[InlineKeyboardButton]]:
    """Resolution filter row(s), the counterpart to the audio chips.

    This is the lever that makes a 60-file season usable: one tap turns
    eight pages of mixed rips into the two the user actually wanted.
    """
    resolutions = _variant_resolutions(result)
    if len(resolutions) < 2:
        return []

    chips = [
        InlineKeyboardButton(
            "🟢 Any" if active is None else "🎚 Any",
            callback_data=title_callback(result.title_id, cursor, language=language),
        )
    ]
    for resolution in resolutions:
        mark = "🟢 " if resolution == active else ""
        chips.append(
            InlineKeyboardButton(
                _truncate(f"{mark}{resolution}"),
                callback_data=title_callback(
                    result.title_id, cursor, language=language, quality=resolution
                ),
            )
        )
    return _chip_rows(chips)


def _quality_line(variants) -> str:
    qualities = list(dict.fromkeys(v.quality for v in variants if v.quality))
    if not qualities:
        return ""
    shown = qualities[:MAX_QUALITIES_SHOWN]
    extra = len(qualities) - len(shown)
    return " · ".join(shown) + (f" +{extra}" if extra else "")


def build_title(
    result: TitleResult,
    cursor: str,
    *,
    show_back: bool = True,
    language: str | None = None,
    quality: str | None = None,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """One title's file picker: filtered by audio/resolution, one page at a time.

    cursor is always the page this title was opened from - the chips need
    it even when there is no results list to go back to (a lone hit).
    """
    variants = [
        variant
        for variant in result.variants
        if (language is None or _variant_has_language(variant, language))
        and (quality is None or resolution_of(variant.quality) == quality)
    ]
    if not variants:  # a chip combination no file actually satisfies
        variants = list(result.variants)
        language = quality = None

    # Clamped rather than trusted: a stale keyboard can name a page that
    # the current filter no longer has.
    pages = max(1, ceil(len(variants) / VARIANTS_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    shown = variants[page * VARIANTS_PER_PAGE : (page + 1) * VARIANTS_PER_PAGE]

    # Everything the user needs to choose, in one quoted block - Telegram
    # draws it with a coloured bar, which separates it from the buttons
    # far better than blank lines do.
    facts = []
    if result.languages:  # an "Audio: n/a" row is worse than no row
        facts.append(f"🗣 <b>Audio</b>   {escape(_languages_line(result.languages))}")
    quality_summary = _quality_line(variants)
    if quality_summary:
        facts.append(f"🎚 <b>Quality</b>   {escape(quality_summary)}")
    facts.append(f"💾 <b>Files</b>   {len(variants)}")
    active_filters = [
        label
        for label in (
            f"{escape(language)} audio" if language else None,
            escape(quality) if quality else None,
        )
        if label
    ]
    if active_filters:
        facts.append(f"🔎 <b>Filter</b>   {'   ·   '.join(active_filters)}")

    lines = [
        f"🎬 {_headline(result)}",
        "",
        "<blockquote>" + "\n".join(facts) + "</blockquote>",
        "",
        "<i>👇 Tap a file — it lands right here in this chat</i>",
    ]

    # When every listed variant carries the same audio, the block above
    # already said it - repeating it on each button only eats the space
    # the episode/quality labels need.
    show_languages = len({variant.languages for variant in shown}) > 1
    rows = _quality_chips(result, cursor, quality, language)
    rows += _language_chips(result, cursor, language, quality)
    rows += [[_variant_button(variant, show_languages)] for variant in shown]

    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "⬅️",
                    callback_data=title_callback(
                        result.title_id, cursor,
                        language=language, quality=quality, page=page - 1,
                    ),
                )
            )
        nav.append(
            InlineKeyboardButton(f"📄 {page + 1} / {pages}", callback_data=NOOP_CALLBACK)
        )
        if page + 1 < pages:
            nav.append(
                InlineKeyboardButton(
                    "➡️",
                    callback_data=title_callback(
                        result.title_id, cursor,
                        language=language, quality=quality, page=page + 1,
                    ),
                )
            )
        rows.append(nav)

    footer: list[InlineKeyboardButton] = []
    if show_back and cursor:
        footer.append(
            InlineKeyboardButton("⬅️ Results", callback_data=f"nav:{cursor}")
        )
    footer.append(InlineKeyboardButton("✖️ Close", callback_data="x"))
    rows.append(footer)

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_results(page: SearchPage, page_size: int) -> tuple[str, InlineKeyboardMarkup]:
    """Results message + keyboard. Caller guarantees page.results non-empty.

    A lone hit skips the list entirely and opens straight into its
    variants - making the user tap through a one-item menu is noise.
    """
    current = page.offset // page_size + 1
    pages = max(1, ceil(page.total / page_size))
    cursor = encode_cursor(page.qhash, page.offset) if page.qhash else ""

    if page.total == 1 and len(page.results) == 1:
        return build_title(page.results[0], cursor, show_back=False)

    # Where the real matches stop. None (no ladder ran) means "all real".
    strong = page.total if page.strong_total is None else page.strong_total
    close = page.total - strong

    if strong == 0:
        counter = f"🤔 <b>{close}</b> close match{'es' if close != 1 else ''}"
    else:
        counter = f"✨ <b>{strong}</b> match{'es' if strong != 1 else ''}"
        if close:
            counter += f"   ·   🤔 <b>{close}</b> close"
    if pages > 1:
        counter += f"   ·   📄 page <b>{current}</b> of <b>{pages}</b>"

    rows: list[list[InlineKeyboardButton]] = []
    entries: list[str] = []
    for index, result in enumerate(page.results, start=1):
        # Absolute rank, not the position on this page: the boundary can
        # fall anywhere, including exactly on a page break.
        if page.offset + index - 1 == strong and strong > 0:
            entries.append(CLOSE_MATCH_DIVIDER)
            rows.append(
                [
                    InlineKeyboardButton(
                        "🤔 Close matches below", callback_data=NOOP_CALLBACK
                    )
                ]
            )
        entries.append(_title_line(index, result))
        rows.append([_title_button(index, result, cursor)])

    # The list goes in a quoted block: Telegram draws a coloured bar down
    # the side, which separates ten two-line entries from the header and
    # the buttons far better than blank lines manage. Entries are spaced
    # inside it so each title reads as its own block, not a wall.
    lines = [
        f"🔎  <b>{escape(page.query)}</b>",
        f"<i>{counter}</i>",
        "",
        "<blockquote>" + "\n\n".join(entries) + "</blockquote>",
        "",
        "<i>👇 Tap a title to open its files</i>",
    ]

    nav: list[InlineKeyboardButton] = []
    if page.qhash and page.offset > 0:
        prev_cursor = encode_cursor(page.qhash, max(0, page.offset - page_size))
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"nav:{prev_cursor}"))
    if pages > 1:
        nav.append(
            InlineKeyboardButton(
                f"📄 {current} / {pages}", callback_data=NOOP_CALLBACK
            )
        )
    if page.next_cursor:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"nav:{page.next_cursor}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✖️ Close", callback_data="x")])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def delivery_caption(
    display_title: str,
    year: int | None,
    languages: tuple[str, ...] | list[str],
    quality: str | None,
    file_size: int | None,
    season: int | None = None,
    episodes: str | None = None,
) -> str:
    year_part = f" ({year})" if year else ""
    year_part += season_label(season)
    parts = [f"🎬 <b>{escape(display_title)}</b>{escape(year_part)}"]
    details = " · ".join(
        piece
        for piece in (
            escape(episodes) if episodes else None,
            escape(quality) if quality else None,
            format_size(file_size) if file_size else None,
            escape(", ".join(languages)) if languages else None,
        )
        if piece
    )
    if details:
        parts.append(f"📦 {details}")
    parts.append("⚡ Enjoy!")
    return "\n".join(parts)
