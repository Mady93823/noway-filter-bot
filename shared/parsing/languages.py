"""Bidirectional language dictionary (docs.md section 6, step 3).

Maps canonical language names to every alias seen in filenames/captions
and back. Used by both the indexing parser and (later) the search parser
so the same query token resolves the same way in both paths.
"""

import re

LANGUAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "tamil": ("tamil", "tam", "tml"),
    "telugu": ("telugu", "tel", "tlg"),
    "hindi": ("hindi", "hin", "hnd"),
    "malayalam": ("malayalam", "mal", "mala"),
    "kannada": ("kannada", "kan", "knd"),
    "english": ("english", "eng", "en"),
    "bengali": ("bengali", "ben", "bangla"),
    "marathi": ("marathi", "mar"),
    "punjabi": ("punjabi", "pun", "panjabi"),
    "gujarati": ("gujarati", "guj"),
    "korean": ("korean", "kor"),
    "japanese": ("japanese", "jpn", "jap"),
    "chinese": ("chinese", "chi", "zho"),
    "french": ("french", "fre", "fra"),
    "spanish": ("spanish", "spa", "esp"),
    "italian": ("italian", "ita"),
    "german": ("german", "ger", "deu"),
    "portuguese": ("portuguese", "por"),
    "russian": ("russian", "rus"),
    "arabic": ("arabic", "ara"),
    "turkish": ("turkish", "tur"),
    "urdu": ("urdu", "urd"),
}

# Reverse index: any alias (including the canonical name itself) -> canonical.
TOKEN_TO_LANGUAGE: dict[str, str] = {
    alias: canonical
    for canonical, aliases in LANGUAGE_ALIASES.items()
    for alias in aliases
}

_WORD_RE = re.compile(r"[a-z]+")

# Subtitle lists must never be indexed as audio languages. Two caption
# conventions cover the wild: a 💬-prefixed line, and a "Subs:"/"ESub"
# style keyword. Everything from the marker to end-of-line is dropped,
# plus one immediately preceding word ("English ESub" names a subtitle
# language, not an audio track).
_SUBTITLE_EMOJI_RE = re.compile(r"💬[^\n]*")
_SUBTITLE_KEYWORD_RE = re.compile(
    r"(?:\b[a-z]{2,}[ \t,+&/-]{1,3})?\b(?:e-?subs?|m-?subs?|subs?|subtitles?|"
    r"subbed|softsubs?|hardsubs?)\b[^\n]*",
    re.IGNORECASE,
)


def strip_subtitle_segments(text: str) -> str:
    """Remove subtitle-language lists from a caption before language scan."""
    return _SUBTITLE_KEYWORD_RE.sub("", _SUBTITLE_EMOJI_RE.sub("", text))


def canonical_language(token: str) -> str | None:
    """Resolve a single token to its canonical language, or None."""
    return TOKEN_TO_LANGUAGE.get(token.lower())


def aliases_for(canonical: str) -> tuple[str, ...]:
    """The reverse direction of the dictionary."""
    return LANGUAGE_ALIASES.get(canonical.lower(), ())


def languages_in_tokens(tokens: list[str]) -> list[str]:
    """Ordered, de-duplicated canonical languages found in tokens."""
    found: list[str] = []
    for token in tokens:
        language = TOKEN_TO_LANGUAGE.get(token)
        if language is not None and language not in found:
            found.append(language)
    return found


def languages_in_text(text: str) -> list[str]:
    """Scan free text (captions) for language mentions."""
    return languages_in_tokens(_WORD_RE.findall(text.lower()))
