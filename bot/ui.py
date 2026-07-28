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

Every card follows the same shape, so a user learns the layout once:

    <icon>  <b>WHAT THIS IS</b>
    <i>the one line that sets expectations</i>

    <blockquote>the facts, one per line: glyph, label, value</blockquote>

    <i>what to do next</i>

The blockquote is doing real work, not decoration: Telegram draws a
coloured bar down its left edge, which separates content from chrome far
better than blank lines. Every card ends on an action, because a card
that only reports leaves the user guessing.

Rule lines (━) appear ONLY where a card has two halves the blockquote bar
cannot bracket on its own. Anywhere else they are noise, and noise is
what makes a "designed" bot feel cheap.

The emoji vocabulary is fixed - one meaning each, so a glyph carries
information instead of decoration:

    🎬 title      💎 quality/tier   🎧 audio     📦 size      📥 delivery
    🔍 search     📂 file count     🎯 filter    📺 series    📄 page
    👑 premium    🎉 success        ⚠️ error     🔒/🔓 gate    ⏳ time
    🟢 the ACTIVE filter chip, and nothing else

One caveat that has bitten before: callback ALERTS (`callback.answer(...,
show_alert=True)`) render as plain text, so the strings that feed them -
expired_text, plan_alert - must carry no tags at all.
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
CLOSE_MATCH_DIVIDER = "<i>━━━━━━━  🤔  close names  ━━━━━━━</i>"

# Separator inside one fact's value ("tamil • english"). A bullet reads as
# "and also", which is what a language or quality list actually means; the
# heavier "   ·   " stays reserved for separating whole facts.
_DOT = " • "

# Resolution tier glyphs. Someone scanning eight rows reads the glyph well
# before the number, so the ladder has to be legible at a glance: a crown
# for the best thing on offer, a phone for the small one.
_TIER_BY_RESOLUTION = {
    "2160p": "👑",
    "1440p": "💎",
    "1080p": "💎",
    "720p": "⚡",
    "576p": "📱",
    "480p": "📱",
    "360p": "📱",
    "240p": "📱",
}

# For a label with no resolution in it at all ("BluRay", "Original", None).
_TIER_FALLBACK = "🎞"

# NOTE: no country flags on the audio chips. Half this index is Indian
# audio, and 🇮🇳 next to tamil, telugu, hindi, malayalam and kannada
# labels five different choices with one identical glyph - the exact
# opposite of what a chip row is for. The three-letter code the bot
# already teaches (tam / tel / hin) distinguishes every one of them and
# works for a language no flag table would have covered.


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
    # TODO(sticker): a short welcome sticker sent immediately before this
    # card would land well - it is the one moment in the flow with no
    # latency to cover and nothing else competing for attention.
    return (
        f"✨  <b>Welcome, {mention}</b>\n"
        "<i>your private cinema — one search away</i>\n\n"
        "<blockquote>🍿 <b>PERSONAL MOVIE LIBRARY</b>\n"
        "⚡ <b>Instant search</b>   ·   no commands to learn\n"
        "💎 <b>Every quality</b>   ·   nothing merged away\n"
        "🎧 <b>Every audio track</b>   ·   pick your language\n"
        "📥 <b>Instant delivery</b>   ·   right here in this chat"
        "</blockquote>\n\n"
        "🔍 <b>Try it now</b>\n"
        "<code>swati 1997 tamil</code>\n\n"
        "<i>👇 Have a look around</i>"
    )


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔍  How to Search", callback_data="hlp"),
                InlineKeyboardButton("✨  About", callback_data="abt"),
            ]
        ]
    )


def help_text() -> str:
    return (
        "🔍  <b>HOW TO SEARCH</b>\n"
        "<i>no commands, no syntax to remember</i>\n\n"
        "<blockquote>🎬 <code>avatar</code>\n"
        "🎬 <code>avatar 2009</code>\n"
        "🎬 <code>avatar hindi</code>\n"
        "🎬 <code>avatar 1080p</code></blockquote>\n\n"
        "🎯 <b>Stack them for the sharpest hit</b>\n"
        "<code>swati 1997 tamil 720p</code>\n\n"
        "🎧 <b>Language shortcuts</b>\n"
        "<code>tam</code>  <code>tel</code>  <code>hin</code>  "
        "<code>mal</code>  <code>kan</code>  <code>eng</code>\n\n"
        "🔁 <b>Refine what you already found</b>\n"
        "Send just <code>1080p</code> or <code>tamil</code> straight after "
        "a search and the list narrows.\n\n"
        "<i>📥 Tap any file — it lands right here in this chat</i>"
    )


def about_text() -> str:
    return (
        "✨  <b>ABOUT THIS BOT</b>\n"
        "<i>built for speed, not for show</i>\n\n"
        "<blockquote>⚡ <b>Fast</b>   ·   search over a self-hosted index\n"
        "💎 <b>Complete</b>   ·   every quality variant kept\n"
        "🎧 <b>Multilingual</b>   ·   audio resolved locally\n"
        "🔒 <b>Private</b>   ·   no third-party APIs, no tracking"
        "</blockquote>\n\n"
        "<i>🎬 Type a movie name to begin</i>"
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️  Back", callback_data="hom")]]
    )


def no_results_text(query: str) -> str:
    return (
        "😔  <b>Couldn't find anything</b>\n"
        f"<i>for “{escape(query)}”</i>\n\n"
        "<blockquote>💡 <b>Try one of these</b>\n"
        "🎬 <code>movie name</code>\n"
        "🎬 <code>movie name 1997</code>\n"
        "🎬 <code>movie name tamil</code></blockquote>\n\n"
        "<i>🔤 Check the spelling — or it may not be indexed yet</i>"
    )


def expired_text() -> str:
    """Plain text: this one is shown in a callback ALERT, where no HTML
    renders and tags would be printed literally."""
    return "⌛ These results expired.\n\n🔁 Just send your search again."


def build_suggestions(
    query: str, suggestions: tuple[Suggestion, ...]
) -> tuple[str, InlineKeyboardMarkup]:
    """"Nothing found - did you mean ...?" with one button per candidate.

    The button carries only the title id; tapping it runs a real search
    for that title rather than opening it directly, so the result gets a
    proper cursor and the same keyboard as any other search.
    """
    lines = [
        "😔  <b>Couldn't find anything</b>",
        f"<i>for “{escape(query)}”</i>",
        "",
        "<blockquote>🤔 <b>Did you mean…</b></blockquote>",
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
    rows.append([InlineKeyboardButton("✖️  Close", callback_data="x")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_gate(short_url: str, hours: int) -> tuple[str, InlineKeyboardMarkup]:
    """The unlock card shown when a gated user taps a file.

    It states the reward before the ask - "how long do I get" is the only
    question that matters to someone looking at an ad gate, and burying
    it under instructions is what makes these feel like a scam.
    """
    plural = "s" if hours != 1 else ""
    text = (
        "👑  <b>VIP ACCESS</b>\n"
        f"<i>one quick step — then {hours} hour{plural} of everything</i>\n\n"
        "<blockquote>✨ <b>Unlimited downloads</b>\n"
        "⚡ <b>Instant delivery</b>\n"
        "💎 <b>The full HD collection</b>\n"
        f"⏳ <b>Valid for</b>   {hours} hour{plural}</blockquote>\n\n"
        "🔓 <b>How it works</b>\n"
        "1️⃣  Tap <b>Unlock Now</b> below\n"
        "2️⃣  Wait for the countdown, press <i>continue</i>\n"
        "3️⃣  You land back here — tap your file again\n\n"
        "<i>⏳ The link stays valid for 30 minutes</i>"
    )
    rows = [
        [InlineKeyboardButton("🚀  Unlock Now", url=short_url)],
        [InlineKeyboardButton("👑  My Plan", callback_data="plan")],
    ]
    return text, InlineKeyboardMarkup(rows)


def access_granted_text(hours: int, until_text: str) -> str:
    # TODO(sticker): a celebration sticker belongs here - this is the one
    # card in the whole flow the user genuinely earned.
    return (
        "🎉  <b>ACCESS GRANTED</b>\n"
        "<i>you're in — the whole library is open</i>\n\n"
        f"<blockquote>💎 <b>Unlimited files</b>   next {hours} hour"
        f"{'s' if hours != 1 else ''}\n"
        f"⏳ <b>Expires in</b>   {until_text}</blockquote>\n\n"
        "<i>👇 Tap your file again — or search for something new</i>"
    )


def plan_text(remaining: str | None) -> str:
    """/myplan. Says what to do next in both states, not just the status."""
    if remaining is None:
        return (
            "💤  <b>NO ACTIVE PLAN</b>\n"
            "<i>searching is free — downloading takes one tap</i>\n\n"
            "<blockquote>🔓 <b>Unlock</b>   tap any file for a one-step "
            "link\n"
            "🔍 <b>Search</b>   free, always</blockquote>\n\n"
            "<i>👇 Send a movie name to get started</i>"
        )
    return (
        "👑  <b>PLAN ACTIVE</b>\n"
        "<i>everything unlocked</i>\n\n"
        f"<blockquote>⏳ <b>Time left</b>   {remaining}\n"
        "💎 <b>Downloads</b>   unlimited</blockquote>\n\n"
        "<i>🍿 Enjoy — search for anything</i>"
    )


def premium_granted_text(remaining: str) -> str:
    """Sent to the user when an admin grants them access unprompted."""
    return (
        "🎉  <b>PREMIUM ACTIVATED</b>\n"
        "<i>a gift from the admins</i>\n\n"
        f"<blockquote>⏳ <b>Time left</b>   {remaining}\n"
        "💎 <b>Downloads</b>   unlimited\n"
        "🚫 <b>Unlock links</b>   none, no waiting</blockquote>\n\n"
        "<i>🍿 Enjoy!</i>"
    )


def verify_failed_text() -> str:
    """A token that was already used, expired, or belongs to someone else.

    All three collapse into one message on purpose: telling a stranger
    which of those it was only helps them work out what to try next.
    """
    return (
        "⚠️  <b>That link didn't work</b>\n\n"
        "<blockquote>It may have expired, or it was already "
        "used.</blockquote>\n\n"
        "<i>💡 Tap any file again — a fresh link takes a second</i>"
    )


def plan_alert(remaining: str | None) -> str:
    """Same status as /myplan, sized for a callback alert popup.

    Telegram truncates these hard, so it says the one fact and stops.
    """
    if remaining is None:
        return "💤 No active plan yet — tap a file to unlock."
    return f"👑 Plan active · {remaining} left"


def gate_unavailable_text() -> str:
    """Shortener failed. Never silently lets the file through.

    Failing open would switch the gate off for everyone the moment the
    provider had an outage - and could be induced on purpose.
    """
    return (
        "⚠️  <b>Couldn't create your unlock link</b>\n\n"
        "<blockquote>The link service isn't responding right now. The "
        "admins have already been told.</blockquote>\n\n"
        "<i>💡 Try again in a minute — nothing is lost</i>"
    )


def greeting_text(mention: str) -> str:
    """Only ever sent when the index had no film by that name.

    Worth restating because "Hello" and "Hi" are both real film titles -
    a greeting reply that pre-empted the search would hide them.
    """
    return (
        f"👋  <b>Hey {mention}</b>\n\n"
        "<blockquote>🎬 Send me a <b>movie name</b> and I'll pull every "
        "quality variant I've got.</blockquote>\n\n"
        "🎯 <b>Sharpest</b>   <code>name year language</code>\n"
        "<code>swati 1997 tamil</code>"
    )


def thanks_text() -> str:
    return "🤗  <b>Anytime!</b>\n\n<i>🍿 Send another name whenever you need one</i>"


def chat_help_text() -> str:
    """Reply to "how do i use this" and to messages that are pure filler.

    Separate from the /help card on purpose: someone who typed "bro send
    movie" needs one example, not the full command reference.
    """
    return (
        "🎬  <b>Just send the movie name</b>\n"
        "<i>that's the whole trick</i>\n\n"
        "<blockquote>🎯 <b>Sharpest</b>   <code>name year language</code>\n"
        "<code>swati 1997 tamil</code></blockquote>\n\n"
        "💎 Add a quality to narrow it — <code>1080p</code>\n"
        "🔁 Or send just <code>1080p</code> / <code>tamil</code> right "
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


def _tier_icon(quality: str | None) -> str:
    """Glyph for a quality label, chosen by the resolution inside it.

    Keyed off resolution_of() so "1080p WEB-DL" and "1080p BluRay" get the
    same glyph - the rip source is not a quality tier.
    """
    return _TIER_BY_RESOLUTION.get(resolution_of(quality) or "", _TIER_FALLBACK)


def _lang_chip_code(language: str) -> str:
    """'tamil' -> 'Tam'. The badge an inactive audio chip wears.

    Title case, not upper: a row of TAM TEL HIN reads as shouting next to
    the lowercase language names everywhere else in the card.
    """
    return short_code(language).title()


def _variant_button(variant, show_languages: bool = True) -> InlineKeyboardButton:
    # Episode label leads: for a series it is what tells two variants
    # apart, so it must survive the length truncation. The tier glyph sits
    # ahead of it, so even a clipped row still announces its quality.
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
    label = f"{_tier_icon(variant.quality)} " + " · ".join(parts)
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
    return _DOT.join(shown) + (f" +{extra}" if extra else "")


def _files_word(count: int) -> str:
    return f"{count} file{'s' if count != 1 else ''}"


def _headline(result: TitleResult) -> str:
    """'<b>Title</b> <i>(2025)</i> · 📺 <i>Season 2</i>', escaped."""
    parts = [f"<b>{escape(result.display_title)}</b>"]
    if result.year:
        parts.append(f"<i>({result.year})</i>")
    if result.season is not None:
        parts.append(f"·  📺 <i>Season {result.season}</i>")
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
    # The file count leads because it is the promise this row makes -
    # "there is something to choose from in here". An untagged title says
    # nothing useful with "🎧 language n/a", so that half is simply gone.
    meta = [f"💎 {_files_word(len(result.variants))}"]
    if result.languages:
        meta.append(f"🎧 {escape(_languages_line(result.languages))}")
    # The meta line is indented under its keycap so the eye reads each
    # entry as one card instead of a run of alternating lines.
    return (
        f"{_index_mark(index)}  {_headline(result)}\n"
        f"      <i>{'   ·   '.join(meta)}</i>"
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


def _variant_episodes(result: TitleResult) -> list[str]:
    """Episode labels present on this title's files, in listing order.

    files_for_titles already returns variants episode-ascending, so this is
    E01-E04 then E05-E08 then ... - the order a season reads in. The INDEX
    into this list is what an episode chip's callback carries.
    """
    seen: list[str] = []
    for variant in result.variants:
        if variant.episodes and variant.episodes not in seen:
            seen.append(variant.episodes)
    return seen


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
    episode: int | None = None,
) -> str:
    """Callback data for one state of the title view.

    Emits the shortest form that expresses the state, so the common cases
    stay far inside Telegram's 64-byte budget and older 4-/5-field buttons
    still round-trip:

        t:<id>:<qhash>:<off>                          everything, first page
        t:<id>:<qhash>:<off>:<lang>                   one audio language
        t:<id>:<qhash>:<off>:<lang>:<qual>:<page>     audio+resolution+page
        t:<id>:<qhash>:<off>:<lang>:<qual>:<page>:<ep>
                                                      full state, '-' = unset.
                                                      ep is an INDEX into the
                                                      title's episode list (not
                                                      the label), so a long
                                                      "E01-E04" costs one digit.
    """
    code = short_code(language) if language else NO_FILTER
    if quality is None and page == 0 and episode is None:
        if language is None:
            return f"t:{title_id}:{cursor}"
        return f"t:{title_id}:{cursor}:{code}"
    base = f"t:{title_id}:{cursor}:{code}:{quality or NO_FILTER}:{page}"
    # Episode index is appended only when set, so every non-series callback
    # stays byte-identical to before this field existed.
    return base if episode is None else f"{base}:{episode}"


def _chip_rows(chips: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [chips[i : i + CHIPS_PER_ROW] for i in range(0, len(chips), CHIPS_PER_ROW)]


def _episode_chips(
    result: TitleResult,
    cursor: str,
    active: int | None,
    language: str | None,
    quality: str | None,
) -> list[list[InlineKeyboardButton]]:
    """Episode picker row(s) for a series. Empty for a movie or a title
    with a single episode/pack - nothing to switch between.

    This is what turns a 40-file season from a flat wall of buttons into
    something navigable: one tap jumps straight to E13 instead of paging.
    Mirrors the audio/resolution chip rows exactly - 🟢 marks the active
    one, and every chip carries the current audio+resolution filters
    through unchanged so picking an episode never widens them.
    """
    episodes = _variant_episodes(result)
    if len(episodes) < 2:
        return []

    chips = [
        InlineKeyboardButton(
            "🟢 All eps" if active is None else "📺 All eps",
            callback_data=title_callback(
                result.title_id, cursor, language=language, quality=quality
            ),
        )
    ]
    for index, label in enumerate(episodes):
        mark = "🟢 " if index == active else "📺 "
        chips.append(
            InlineKeyboardButton(
                _truncate(f"{mark}{label}"),
                callback_data=title_callback(
                    result.title_id,
                    cursor,
                    language=language,
                    quality=quality,
                    episode=index,
                ),
            )
        )
    return _chip_rows(chips)


def _language_chips(
    result: TitleResult,
    cursor: str,
    active: str | None,
    quality: str | None,
    episode: int | None,
) -> list[list[InlineKeyboardButton]]:
    """Audio filter row(s). Empty when the title has nothing to choose between.

    Every chip carries the quality filter through unchanged - switching
    audio must not silently widen the resolution the user already picked -
    and resets to the first page, because the old page number means
    nothing once the list behind it changed.

    🟢 is reserved for the ACTIVE chip, here and in the resolution row.
    That green dot IS the state readout: one per row, and the user knows
    what they are looking at without a header saying so. The active chip
    spells its language out in full; the rest wear their three-letter code
    (Tam, Tel, Hin), which is what keeps a six-language row scannable.
    """
    languages = _variant_languages(result)
    if len(languages) < 2:
        return []

    chips = [
        InlineKeyboardButton(
            # Inactive form carries the same glyph as the card's Audio
            # row, so the two chip rows label themselves without a header.
            "🟢 All" if active is None else "🎧 All",
            callback_data=title_callback(
                result.title_id, cursor, quality=quality, episode=episode
            ),
        )
    ]
    for language in languages:
        label = (
            f"🟢 {language}" if language == active else _lang_chip_code(language)
        )
        chips.append(
            InlineKeyboardButton(
                _truncate(label),
                callback_data=title_callback(
                    result.title_id,
                    cursor,
                    language=language,
                    quality=quality,
                    episode=episode,
                ),
            )
        )
    return _chip_rows(chips)


def _quality_chips(
    result: TitleResult,
    cursor: str,
    active: str | None,
    language: str | None,
    episode: int | None,
) -> list[list[InlineKeyboardButton]]:
    """Resolution filter row(s), the counterpart to the audio chips.

    This is the lever that makes a 60-file season usable: one tap turns
    eight pages of mixed rips into the two the user actually wanted. The
    tier glyph on each inactive chip (👑 💎 ⚡ 📱) is the same one its
    files carry below, so the row and the list read as one ladder.
    """
    resolutions = _variant_resolutions(result)
    if len(resolutions) < 2:
        return []

    chips = [
        InlineKeyboardButton(
            "🟢 Any" if active is None else "🎚 Any",
            callback_data=title_callback(
                result.title_id, cursor, language=language, episode=episode
            ),
        )
    ]
    for resolution in resolutions:
        mark = "🟢 " if resolution == active else f"{_tier_icon(resolution)} "
        chips.append(
            InlineKeyboardButton(
                _truncate(f"{mark}{resolution}"),
                callback_data=title_callback(
                    result.title_id,
                    cursor,
                    language=language,
                    quality=resolution,
                    episode=episode,
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
    return _DOT.join(shown) + (f" +{extra}" if extra else "")


def build_title(
    result: TitleResult,
    cursor: str,
    *,
    show_back: bool = True,
    language: str | None = None,
    quality: str | None = None,
    page: int = 0,
    episode: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """One title's file picker: filtered by episode/audio/resolution, one
    page at a time.

    cursor is always the page this title was opened from - the chips need
    it even when there is no results list to go back to (a lone hit).
    episode is an INDEX into _variant_episodes(result); None means "all".
    """
    episodes = _variant_episodes(result)
    active_episode = (
        episodes[episode] if episode is not None and 0 <= episode < len(episodes) else None
    )
    variants = [
        variant
        for variant in result.variants
        if (language is None or _variant_has_language(variant, language))
        and (quality is None or resolution_of(variant.quality) == quality)
        and (active_episode is None or variant.episodes == active_episode)
    ]
    if not variants:  # a chip combination no file actually satisfies
        variants = list(result.variants)
        language = quality = active_episode = None
        episode = None

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
        facts.append(f"🎧 <b>Audio</b>   {escape(_languages_line(result.languages))}")
    quality_summary = _quality_line(variants)
    if quality_summary:
        facts.append(f"💎 <b>Quality</b>   {escape(quality_summary)}")
    facts.append(f"📂 <b>Files</b>   {len(variants)}")
    active_filters = [
        label
        for label in (
            escape(active_episode) if active_episode else None,
            f"{escape(language)} audio" if language else None,
            escape(quality) if quality else None,
        )
        if label
    ]
    if active_filters:
        facts.append(f"🎯 <b>Filter</b>   {'   ·   '.join(active_filters)}")

    # The rule line earns its place on this card and nowhere else: there
    # is a masthead and a body here, and the blockquote bar only brackets
    # the body.
    lines = [
        "🍿  <b>MOVIE DETAILS</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🎬  {_headline(result)}",
        "",
        "<blockquote>" + "\n".join(facts) + "</blockquote>",
        "",
        "<i>👇 Pick a version — it lands right here in this chat 📥</i>",
    ]

    # When every listed variant carries the same audio, the block above
    # already said it - repeating it on each button only eats the space
    # the episode/quality labels need.
    show_languages = len({variant.languages for variant in shown}) > 1
    # Episode row first: for a series it is the primary axis, the one that
    # answers "which episode" before "which quality".
    rows = _episode_chips(result, cursor, episode, language, quality)
    rows += _quality_chips(result, cursor, quality, language, episode)
    rows += _language_chips(result, cursor, language, quality, episode)
    rows += [[_variant_button(variant, show_languages)] for variant in shown]

    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "◀️",
                    callback_data=title_callback(
                        result.title_id, cursor,
                        language=language, quality=quality, page=page - 1,
                        episode=episode,
                    ),
                )
            )
        nav.append(
            InlineKeyboardButton(
                f"📄  {page + 1} / {pages}", callback_data=NOOP_CALLBACK
            )
        )
        if page + 1 < pages:
            nav.append(
                InlineKeyboardButton(
                    "▶️",
                    callback_data=title_callback(
                        result.title_id, cursor,
                        language=language, quality=quality, page=page + 1,
                        episode=episode,
                    ),
                )
            )
        rows.append(nav)

    footer: list[InlineKeyboardButton] = []
    if show_back and cursor:
        footer.append(
            InlineKeyboardButton("↩️  Results", callback_data=f"nav:{cursor}")
        )
    footer.append(InlineKeyboardButton("✖️  Close", callback_data="x"))
    rows.append(footer)

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def results_photo_url(page: SearchPage) -> str | None:
    """Poster to send the results card as a photo, or None to send text.

    Only a LONE hit qualifies: build_results opens a single result straight
    into its detail card (no list), so there is no text results-list that a
    photo would have to cross-edit into. A multi-result list stays text -
    ten posters cannot share one card, and Telegram cannot edit a text
    message into a photo one anyway.
    """
    if page.total == 1 and len(page.results) == 1:
        return page.results[0].poster_url
    return None


def build_results(page: SearchPage, page_size: int) -> tuple[str, InlineKeyboardMarkup]:
    """Results message + keyboard. Caller guarantees page.results non-empty.

    A lone hit skips the list entirely and opens straight into its
    variants - making the user tap through a one-item menu is noise.
    """
    # NOTE(progressive reveal - NOT implemented here, and deliberately so):
    # the search handler could send one placeholder and edit it through
    #     "🔍  Searching…"  ->  "📂  Opening the library…"  ->  this card
    # which reads as motion without pretending to animate anything. It
    # costs two edit_text calls and belongs in the handler, not in this
    # module - ui.py returns strings, it never touches the API.
    current = page.offset // page_size + 1
    pages = max(1, ceil(page.total / page_size))
    cursor = encode_cursor(page.qhash, page.offset) if page.qhash else ""

    if page.total == 1 and len(page.results) == 1:
        return build_title(page.results[0], cursor, show_back=False)

    # Where the real matches stop. None (no ladder ran) means "all real".
    strong = page.total if page.strong_total is None else page.strong_total
    close = page.total - strong

    if strong == 0:
        # Says the important half first. These only ever appear when
        # nothing actually contained the query, so leading with the count
        # would present near-misses as if they were results.
        counter = (
            "🤔 <b>No exact match</b>   ·   "
            f"{close} close name{'s' if close != 1 else ''}"
        )
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
                        "🤔  ·  close names below  ·  🤔",
                        callback_data=NOOP_CALLBACK,
                    )
                ]
            )
        entries.append(_title_line(index, result))
        rows.append([_title_button(index, result, cursor)])

    # The list goes in a quoted block: Telegram draws a coloured bar down
    # the side, which separates ten two-line entries from the header and
    # the buttons far better than blank lines manage. Entries are spaced
    # inside it so each title reads as its own card, not a wall.
    lines = [
        f"🔍  <b>{escape(page.query)}</b>",
        f"<i>{counter}</i>",
        "",
        "<blockquote>" + "\n\n".join(entries) + "</blockquote>",
        "",
        "<i>👇 Tap a title — every quality variant is inside</i>",
    ]

    nav: list[InlineKeyboardButton] = []
    if page.qhash and page.offset > 0:
        prev_cursor = encode_cursor(page.qhash, max(0, page.offset - page_size))
        nav.append(InlineKeyboardButton("◀️  Prev", callback_data=f"nav:{prev_cursor}"))
    if pages > 1:
        nav.append(
            InlineKeyboardButton(
                f"📄  {current} / {pages}", callback_data=NOOP_CALLBACK
            )
        )
    if page.next_cursor:
        nav.append(
            InlineKeyboardButton("Next  ▶️", callback_data=f"nav:{page.next_cursor}")
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✖️  Close", callback_data="x")])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def delivery_warning_text(ttl_seconds: int) -> str:
    """Loud notice sent right after a delivered file.

    The file really is deleted, so this cannot read like a footnote - a
    user who scrolls past it loses what they came for. Telegram HTML has
    no colour, so "red" is carried by 🔴/❗️ and bold; the escape hatch
    (search again) is stated too, so the warning informs instead of just
    alarming. This is the one card that is deliberately NOT pretty.
    """
    minutes = max(1, ttl_seconds // 60)
    plural = "S" if minutes != 1 else ""
    return (
        f"🔴 <b>THIS FILE DELETES IN {minutes} MINUTE{plural}</b> 🔴\n\n"
        "❗️ <b>FORWARD IT NOW</b> — to <b>Saved Messages</b>, or to any "
        "chat you like.\n\n"
        "<blockquote>🚫 <b>Do not just leave it here.</b>\n"
        f"⏳ In <b>{minutes} minute{plural.lower()}</b> it is removed from "
        "this chat.\n"
        "✅ Your forwarded copy is yours to keep — forever.</blockquote>\n\n"
        "<i>🔁 Lost it? Search again and download it any time.</i>"
    )


def delivery_expired_text() -> str:
    """Replaces the warning once the file is gone. Says what to do next."""
    return (
        "🗑  <b>Your file was deleted</b>\n\n"
        "<blockquote>⏳ The time window closed and the file was removed "
        "from this chat.</blockquote>\n\n"
        "<i>💡 Search again — you can download it right away, any time.</i>"
    )


def delivery_caption(
    display_title: str,
    year: int | None,
    languages: tuple[str, ...] | list[str],
    quality: str | None,
    file_size: int | None,
    season: int | None = None,
    episodes: str | None = None,
) -> str:
    """The caption on the delivered file itself.

    One fact per line, each behind its own glyph. This caption is read on
    a file bubble that is already busy with a filename and a progress bar,
    and a single dot-separated run of details just disappears into that.
    """
    # TODO(sticker): a "🍿 enjoy" sticker could follow the file here - but
    # only ever AFTER the expiry warning, never between file and warning.
    year_part = f" ({year})" if year else ""
    year_part += season_label(season)
    parts = [
        "🎉  <b>Download Ready</b>",
        "",
        f"🎬 <b>{escape(display_title)}</b>{escape(year_part)}",
    ]
    if episodes:
        parts.append(f"📺 {escape(episodes)}")
    if quality:
        parts.append(f"{_tier_icon(quality)} {escape(quality)}")
    if languages:
        parts.append(f"🎧 {escape(_DOT.join(languages))}")
    if file_size:
        parts.append(f"📦 {format_size(file_size)}")
    parts.append("")
    parts.append("🍿 <i>Enjoy your movie!</i>")
    return "\n".join(parts)
