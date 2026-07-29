"""The bot's playful "genz" voice - all opt-in behind /funmode.

Every function takes a `fun` flag and, when it is False, returns exactly
what the plain default voice would say (usually by delegating straight to
bot.ui). So a caller wires funmode in ONCE - fetch the flag, pass it down -
and never branches on it itself. When fun is True the copy is swapped for a
random pick from a small pool, so the same action reads a little different
each time and the bot feels alive rather than canned.

Pure module: it builds strings and picks from lists, it never touches the
network or the database. The one stateful flourish (streaks) lives
elsewhere; this stays trivially unit-testable.
"""

import random
import re
from html import escape

from bot import ui

# --- reactions -------------------------------------------------------------

# Dropped onto the user's own search message when funmode is on. Kept to
# emoji Telegram actually allows as message reactions.
_SEARCH_REACTIONS = ("🔥", "👀", "🎬", "🍿", "⚡", "💯", "😎", "🫡")


def search_reaction() -> str:
    """A random emoji to react to a search with. Caller reacts only in funmode."""
    return random.choice(_SEARCH_REACTIONS)


# --- no results ------------------------------------------------------------

_NO_RESULTS_ROASTS = (
    "💀 nah that one ain't in the vault chief",
    "🤨 bro searched for a fever dream",
    "😭 that title left me on read — got nothing",
    "🫥 the vault stared back. empty.",
)

_GIBBERISH_ROASTS = (
    "💀 bro just mashed the keyboard fr",
    "🤖 beep boop that's not a movie",
    "🥴 respectfully… what",
)

# Vowel-less or symbol-heavy junk: "asdfgh", "!!!", "xkcdqz". A real short
# title ("up", "her", "1917") always clears this.
_VOWELS = set("aeiou")


def is_gibberish(query: str) -> bool:
    letters = [c for c in query.lower() if c.isalpha()]
    if len(letters) >= 4 and not (_VOWELS & set(letters)):
        return True  # four+ letters, not one vowel
    stripped = re.sub(r"\s+", "", query)
    if len(stripped) >= 3 and sum(c.isalnum() for c in stripped) / len(stripped) < 0.4:
        return True  # mostly punctuation
    return False


def no_results(query: str, fun: bool) -> str:
    if not fun:
        return ui.no_results_text(query)
    roast = random.choice(
        _GIBBERISH_ROASTS if is_gibberish(query) else _NO_RESULTS_ROASTS
    )
    return (
        f"{roast}\n"
        f"<i>“{escape(query)}”</i>\n\n"
        "<blockquote>💡 <b>try again bestie</b>\n"
        "🎬 <code>movie name</code>\n"
        "🎬 <code>movie name 2019</code>\n"
        "🎬 <code>movie name tamil</code></blockquote>\n\n"
        "<i>🔤 check spelling — or it just ain't dropped yet 🤷</i>"
    )


# --- conversational replies ------------------------------------------------

_GREETINGS = (
    "yooo {m} 👋",
    "ayy {m} 🫡",
    "wsg {m} 😎",
)

_THANKS = (
    "np king 👑",
    "anytime bestie 🫶",
    "slay — enjoy 🍿",
    "gotchu 🤝",
)


def greeting(mention: str, fun: bool) -> str:
    if not fun:
        return ui.greeting_text(mention)
    head = random.choice(_GREETINGS).format(m=mention)
    return (
        f"{head}\n\n"
        "<blockquote>🎬 drop a <b>movie name</b> and I pull every quality "
        "I got.</blockquote>\n\n"
        "🎯 <b>sharpest</b>   <code>name year language</code>\n"
        "<code>swati 1997 tamil</code>"
    )


def thanks(fun: bool) -> str:
    if not fun:
        return ui.thanks_text()
    return f"🤗  <b>{random.choice(_THANKS)}</b>\n\n<i>🍿 send another name whenever</i>"


def chat_help(fun: bool) -> str:
    if not fun:
        return ui.chat_help_text()
    return (
        "🎬  <b>just send the movie name bestie</b>\n"
        "<i>that's the whole cheat code</i>\n\n"
        "<blockquote>🎯 <b>sharpest</b>   <code>name year language</code>\n"
        "<code>swati 1997 tamil</code></blockquote>\n\n"
        "💎 add a quality to lock in — <code>1080p</code>\n"
        "🔁 or send just <code>1080p</code> / <code>tamil</code> right after "
        "a search to filter what you found."
    )


# --- flair on real results -------------------------------------------------


def count_vibe(total: int, fun: bool) -> str | None:
    """A cheeky one-liner appended to the results counter, or None.

    None in plain mode and for the unremarkable middle - the vibe only
    fires at the extremes where it actually lands (one perfect hit, or a
    genuinely stacked pile)."""
    if not fun:
        return None
    if total == 1:
        return "bullseye 🎯"
    if total >= 200:
        return "we COOKED 🍳"
    if total >= 50:
        return "the whole stack is here 😮‍💨"
    return None


_DELIVERY_HYPE = (
    "W pick 🔥",
    "elite taste fr",
    "certified banger 🎬",
    "go crazy 🍿",
    "sheeeesh 😮‍💨",
)


def delivery_hype(fun: bool) -> str | None:
    """Extra hype line for a delivered file's caption, or None in plain mode."""
    return random.choice(_DELIVERY_HYPE) if fun else None


# --- easter eggs -----------------------------------------------------------

# Matched on the normalised query only when a search found NOTHING, so a
# real film of the same name is never hidden by the gag.
_EASTER_EGGS = {
    "rickroll": "😏 never gonna give you up, never gonna let you down 🎶",
    "never gonna give you up": "😏 you just got rickrolled 🎶",
    "thanos": "🫰 perfectly balanced, as all things should be",
    "konami": "⬆️⬆️⬇️⬇️⬅️➡️⬅️➡️🅱️🅰️ 😎",
    "69": "nice 😏",
    "404": "🤖 title not found… literally",
}


def easter_egg(query: str) -> str | None:
    """A gag reply for a known trigger, or None. Funmode + no-results only."""
    key = re.sub(r"\s+", " ", query.strip().lower())
    return _EASTER_EGGS.get(key)
