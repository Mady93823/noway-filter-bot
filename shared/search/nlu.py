"""Conversational query understanding - local, deterministic, no APIs.

People do not type "swati 1997 tamil". They type "bro send swati movie
plz". The filename parser cannot help here: it strips *release*
metadata (codecs, resolutions, release groups), so conversational filler
survives it and goes straight into the trigram match, where "bro send
plz" is noise that drags the similarity score down.

The safety rule that shapes this whole module: **cleaning and intent are
fallbacks, never gates.** Real titles collide with every word list here -
"Hello" is a 2017 Telugu film, "Up" is a Pixar one, "Scary Movie" is a
franchise. So the caller always searches first and only consults this
module when the index came back empty. Nothing in here can hide a film
that actually exists.

Golden rule 1 holds: no external API, no model call. Word lists and
regexes only.
"""

import re
from dataclasses import dataclass
from enum import Enum

from shared.parsing.filename import tokenize

# Request scaffolding. Conservative on purpose - a word only belongs here
# if dropping it from a real title would still leave that title findable,
# because the caller retries with the untouched text anyway.
_FILLER = frozenset(
    {
        # address / politeness
        "bro", "bruh", "bhai", "boss", "sir", "madam", "mam", "dear",
        "anna", "please", "plz", "pls", "plss", "kindly", "kripya",
        # asking
        "send", "sent", "share", "sharing", "upload", "give", "gimme",
        "want", "wanted", "need", "needed", "looking", "searching",
        "requesting", "request", "req", "asking", "provide", "post",
        # possession
        "have", "has", "having", "got", "available", "avail", "ava",
        "anyone", "anybody", "someone", "somebody",
        # deixis that only ever wraps the real query
        "me", "myself", "us", "plzz", "pleaseee",
        # media nouns - "movie"/"film" are the single most common filler
        # word in this domain, and the raw-text retry covers Scary Movie.
        "movie", "movies", "film", "films", "cinema", "picture", "padam",
        "link", "links", "file", "files", "copy", "print", "version",
    }
)

# Never removed from a query - "The Ring", "It", "Up" are all real
# titles. These exist only to answer "is what survived filler-stripping
# actually a title, or just grammar?", which is how "send me the movie
# bro" avoids collapsing to a search for "the".
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "for", "to", "in", "on", "is", "are",
        "do", "does", "did", "can", "you", "u", "i", "my", "any",
        "some", "that", "this", "it", "one", "here", "there", "and",
    }
)

_GREETINGS = frozenset(
    {
        "hi", "hii", "hiii", "hello", "helo", "hey", "heyy", "yo",
        "hai", "hlo", "namaste", "namaskaram", "vanakkam", "salaam",
        "assalamualaikum", "hola", "gm", "gn",
    }
)

_THANKS = frozenset(
    {
        "thanks", "thank", "thanku", "thankyou", "thnx", "thx", "ty",
        "tq", "nandri", "dhanyavad", "shukriya", "tnx",
    }
)

_HELP = frozenset({"help", "howto", "usage", "guide", "commands", "start"})

# "how do i search", "how to use this bot" - the words vary, the shape
# does not: an interrogative followed by a use/search verb.
_HELP_PHRASE_RE = re.compile(
    r"\bhow\s+(?:do\s+i|to|can\s+i|does?)\b.*\b(?:use|search|find|work|get)\b"
)


class Intent(Enum):
    """What a message that found nothing was probably trying to do."""

    SEARCH = "search"
    GREETING = "greeting"
    THANKS = "thanks"
    HELP = "help"


@dataclass(frozen=True)
class CleanedQuery:
    """text is what to search; changed says whether anything was removed.

    changed exists so the caller can skip a pointless second search when
    cleaning was a no-op - the retry only makes sense if the two strings
    actually differ.
    """

    text: str
    changed: bool


def _content_tokens(tokens: list[str]) -> list[str]:
    """Tokens that could carry a title: no filler, no bare grammar."""
    return [
        token for token in tokens if token not in _FILLER and token not in _STOPWORDS
    ]


def clean_query(raw: str) -> CleanedQuery:
    """Drop request scaffolding, keep everything that could be a title.

    Never returns something with no title in it: if all that survives is
    filler and grammar ("send me the movie bro" -> "the"), the original
    is handed back so the caller's normal no-results path runs instead
    of searching for a stray article.

    Stopwords are only consulted for that decision, never removed - the
    returned text keeps them, because "The Ring" needs its "the".
    """
    tokens = tokenize(raw)
    kept = [token for token in tokens if token not in _FILLER]
    if not _content_tokens(kept) or len(kept) == len(tokens):
        return CleanedQuery(raw.strip(), changed=False)
    return CleanedQuery(" ".join(kept), changed=True)


def detect_intent(raw: str) -> Intent:
    """Classify a message the index could not answer.

    Deliberately strict: a message only reads as conversational when it
    is *entirely* made of greeting/thanks/filler words. "hello world" is
    a search, "hello" alone is not - and even "hello" only lands here
    after the index has already said it has no such film.
    """
    text = raw.lower().strip()
    if _HELP_PHRASE_RE.search(text):
        return Intent.HELP

    tokens = _content_tokens(tokenize(raw))
    if not tokens:
        # Pure scaffolding ("send me the movie bro") - the user wants the
        # bot, not a specific film.
        return Intent.HELP
    if all(token in _GREETINGS for token in tokens):
        return Intent.GREETING
    if all(token in _THANKS for token in tokens):
        return Intent.THANKS
    if all(token in _HELP for token in tokens):
        return Intent.HELP
    return Intent.SEARCH
