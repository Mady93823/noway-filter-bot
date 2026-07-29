"""Filename/caption parser (docs.md section 6, steps 1-5).

Deterministic, local-only: normalize -> extract year -> extract
language(s) -> extract quality -> remainder is the title guess.
The same logic backs the search fast path, so indexing and search
always agree on how a string decomposes.

Core rule, learned from a real channel dump: release names read
"Title . Year . Metadata - GROUP", so the title is the token run
BEFORE the first metadata marker (year/resolution/source/codec).
Unknown tokens after that point are debris (release groups, platform
tags, bitrates) and are dropped; languages are still harvested from
the whole string because multi-audio lists sit late in the name.

Series markers (S02, S01E12, EP01, EP 10, [E01 - 04], PART 1) are metadata
markers too, but they are *kept* as structured fields instead of being
thrown away: the season joins the title's identity (S01 and S02 are
different things, not quality variants of one thing) and the episode
range labels the individual file.
"""

import re
from dataclasses import dataclass
from urllib.parse import unquote

from shared.parsing.languages import (
    TOKEN_TO_LANGUAGE,
    languages_in_text,
    strip_subtitle_segments,
)
from shared.parsing.quality import (
    BIGRAM_SOURCE_TOKENS,
    resolution_label,
    source_label,
)

_EXTENSION_RE = re.compile(
    r"\.(mkv|mp4|avi|webm|mov|wmv|flv|m4v|ts|m2ts)$", re.IGNORECASE
)
# Leading uploader / release-group tags: "[MW] Avengers", "{CF}.Movie",
# "@Channel Movie". Peeled off the FRONT before tokenizing so the tag's
# letters ("mw", "cf") don't survive as title words and split one film
# into a dozen near-duplicate canonical rows. Only a LEADING run is
# stripped - a bracket group later in the name is ordinary metadata the
# debris-drop already handles, and a bracket is capped at 15 chars so a
# genuine "[Director's Cut]"-length note in front is left intact. Real
# titles effectively never open with a bracket/handle tag.
_LEADING_TAG_RE = re.compile(
    r"^(?:\s*(?:\[[^\]]{1,15}\]|\{[^}]{1,15}\}|@[A-Za-z0-9_]+)\s*[-._]*)+"
)
# ':' and ';' are separators too - "Spider-Man: No.Way.Home" must
# canonicalize the same whether or not the uploader kept the colon.
_SPLIT_RE = re.compile(r"[.\-_\s()\[\]{}+,!|~#&*%:;]+")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_CHANNELS_RE = re.compile(r"^\d(?:\.\d)?ch$")  # 2ch / 6ch / 5.1ch
# Fused audio-codec tokens the splitter produces: ddp5, dd5, aac2, aac5…
_CODEC_RE = re.compile(r"^(?:dd|ddp|aac|eac3|ac3|dts|opus)\d*$")
# Bitrate/size debris: 192kbps, 640k, 700mb, 4gb, 60fps
_RATE_RE = re.compile(r"^\d+(?:\.\d+)?(?:k|kbps|mbps|mb|gb|fps)$")

# Series markers. Deliberately anchored and digit-bounded so ordinary
# title words can never be mistaken for them.
_SEASON_EPISODE_RE = re.compile(r"^s(\d{1,2})e(\d{1,3})(?:e(\d{1,3}))?$")  # s01e12, s02e08e21
_SEASON_RE = re.compile(r"^s(\d{1,2})$")  # s01, s2
_EPISODE_RE = re.compile(r"^e(?:p|pisode)?(\d{1,3})$")  # e01, ep01
_BARE_NUMBER_RE = re.compile(r"^\d{1,3}$")
# "EP 10" / "Episode 10" - the marker word and its number arrive as two
# separate tokens, so _EPISODE_RE (which needs them fused) never fires and
# both leak into the title. A bare "e" is deliberately NOT here: it is a
# plausible title token on its own, and the fused forms already cover E01.
_EPISODE_WORDS = frozenset({"ep", "eps", "epi", "episode", "episodes"})
# "part001"/"part002" are multipart FILE SPLITS of one movie, never a
# season part - the splitter keeps them fused, so they never reach the
# season logic (which only reads a separate "part" token + number).
_PART_WORD = "part"

# Purely technical tokens that never belong to a title. Kept conservative:
# words that could plausibly appear in a real movie title stay out.
_JUNK_TOKENS = frozenset(
    {
        "x264", "x265", "h264", "h265", "hevc", "avc", "av1",
        "aac", "ac3", "eac3", "ddp", "dts", "truehd", "atmos", "opus",
        "esub", "esubs", "msub", "msubs", "subs", "softsub", "hardsub",
        "10bit", "8bit", "hdr", "hdr10", "sdr",
        "amzn", "nf", "dsnp", "zee5", "sonyliv", "hotstar", "jiohotstar",
        "mkv", "mp4", "avi", "hq", "org", "untouched", "remastered",
        "multi", "dual", "audio", "proper", "uncut",
        "combined", "dub", "dubbed", "imax",
    }
)


@dataclass(frozen=True)
class ParsedMedia:
    """Result of parsing one file's name + caption.

    season identifies the title alongside name+year; episodes is a label
    for THIS file only ("E01-E04", "E12", "Part 1") and never affects
    which title the file belongs to.
    """

    title_guess: str
    year: int | None
    languages: tuple[str, ...]
    quality: str | None
    season: int | None = None
    episodes: str | None = None


def tokenize(text: str) -> list[str]:
    return [token for token in _SPLIT_RE.split(text.lower()) if token]


# Leading articles collapsed for the MATCHING key only. "the wicked within",
# "a wicked within" and "wicked within" are the same film wearing different
# articles; keying them all to "wicked within" makes them resolve by exact
# match instead of fuzzy guesswork, and makes a search for the real name
# land instead of trigram-scattering. The display title keeps its article.
_LEADING_ARTICLES = frozenset({"the", "a", "an"})


def strip_leading_article(title: str) -> str:
    """Drop a leading the/a/an from a title's matching key.

    Never empties a title: a bare "The" or "A" is a real one-word name
    ("A", the 2018 film) and is left untouched. Only a leading article in
    front of MORE title is removed.
    """
    parts = title.split()
    if len(parts) > 1 and parts[0] in _LEADING_ARTICLES:
        return " ".join(parts[1:])
    return title


def _extract_series(tokens: list[str], consumed: list[bool]) -> tuple[
    int | None, str | None, int | None
]:
    """Pull season/episode/part markers out of the token stream.

    Returns (season, episodes_label, first_marker_index). Consumed tokens
    are marked so they cannot leak into the title or the language scan.
    """
    season: int | None = None
    first: int | None = None
    last: int | None = None
    part: int | None = None
    marker_index: int | None = None

    def _mark(i: int) -> None:
        nonlocal marker_index
        consumed[i] = True
        marker_index = i if marker_index is None else min(marker_index, i)

    index = 0
    while index < len(tokens):
        if consumed[index]:
            index += 1
            continue
        token = tokens[index]

        match = _SEASON_EPISODE_RE.match(token)
        if match:
            season = season or int(match.group(1))
            episode = int(match.group(2))
            first = episode if first is None else min(first, episode)
            last = episode if last is None else max(last, episode)
            # Double-episode file glued into one token ("s02e08e21"):
            # without this the trailing "e21" fails the single-episode
            # regex and the whole marker leaks into the title.
            if match.group(3):
                second = int(match.group(3))
                first = min(first, second)
                last = max(last, second)
            _mark(index)
            index += 1
            continue

        match = _SEASON_RE.match(token)
        if match:
            season = season or int(match.group(1))
            _mark(index)
            index += 1
            continue

        # "season 2" spelled out
        if (
            token in ("season", "seasons")
            and index + 1 < len(tokens)
            and _BARE_NUMBER_RE.match(tokens[index + 1])
        ):
            season = season or int(tokens[index + 1])
            _mark(index)
            _mark(index + 1)
            index += 2
            continue

        # Spelled-out episode marker, the same two-token shape as
        # "season 2" above. Handled together with the fused form so the
        # "[E01 - 04]" range logic below covers "EP 01 - 04" as well.
        spelled = (
            token in _EPISODE_WORDS
            and index + 1 < len(tokens)
            and not consumed[index + 1]
            and _BARE_NUMBER_RE.match(tokens[index + 1])
        )
        match = _EPISODE_RE.match(token)
        if match or spelled:
            if spelled:
                _mark(index)  # the word itself
                index += 1
                episode = int(tokens[index])
            else:
                episode = int(match.group(1))
            _mark(index)
            index += 1
            # "[E01 - 04]" and "E05-08" both split into <ep-token> <number>
            if index < len(tokens) and not consumed[index]:
                if _BARE_NUMBER_RE.match(tokens[index]):
                    end = int(tokens[index])
                    if end > episode:
                        _mark(index)
                        index += 1
                        first = episode if first is None else min(first, episode)
                        last = end if last is None else max(last, end)
                        continue
            first = episode if first is None else min(first, episode)
            last = episode if last is None else max(last, episode)
            continue

        # A bare "PART 1" only counts as a season part when a season was
        # actually named - otherwise it is a movie's own word ("Part 2").
        if (
            token == _PART_WORD
            and season is not None
            and index + 1 < len(tokens)
            and _BARE_NUMBER_RE.match(tokens[index + 1])
        ):
            part = int(tokens[index + 1])
            _mark(index)
            _mark(index + 1)
            index += 2
            continue

        index += 1

    if first is not None and last is not None and last != first:
        episodes = f"E{first:02d}-E{last:02d}"
    elif first is not None:
        episodes = f"E{first:02d}"
    elif part is not None:
        episodes = f"Part {part}"
    else:
        episodes = None
    return season, episodes, marker_index


def parse_media(file_name: str | None, caption: str | None = None) -> ParsedMedia:
    """Parse a Telegram media post into title/year/languages/quality.

    The filename is the primary source; when it is missing the first
    caption line stands in. Caption text is always scanned for languages
    because captions often carry the language when the filename doesn't.
    """
    primary = (file_name or "").strip()
    if not primary and caption:
        # Caption stands in for the filename: strip subtitle lists first
        # so "… English ESub" can't leak a subtitle language into tokens.
        stripped = strip_subtitle_segments(caption).strip()
        primary = stripped.splitlines()[0] if stripped else ""

    if "%" in primary:
        primary = unquote(primary)  # "The%20Dark%20Knight" happens
    primary = _EXTENSION_RE.sub("", primary)
    # Peel leading uploader/group tags ("[MW]", "@FBM") before tokenizing.
    # Guard: never let the strip empty the name - a file that is ONLY a tag
    # ("[MW].mkv") keeps its original text rather than parsing to nothing.
    stripped_lead = _LEADING_TAG_RE.sub("", primary).strip()
    if stripped_lead:
        primary = stripped_lead
    tokens = tokenize(primary)

    consumed = [False] * len(tokens)
    resolution: str | None = None
    source: str | None = None
    # First metadata marker position: title tokens must come before it.
    cut = len(tokens)

    # Bigram pass first, so "web dl" is claimed before the single-token
    # pass can misread its halves.
    for i in range(len(tokens) - 1):
        if consumed[i] or consumed[i + 1]:
            continue
        label = BIGRAM_SOURCE_TOKENS.get((tokens[i], tokens[i + 1]))
        if label is not None:
            source = source or label
            consumed[i] = consumed[i + 1] = True
            cut = min(cut, i)

    # Year: the LAST year-looking token wins, so a title like "1917 2019"
    # keeps 1917 as text and takes 2019 as the year.
    year: int | None = None
    year_index: int | None = None
    for i, token in enumerate(tokens):
        if not consumed[i] and _YEAR_RE.match(token):
            year_index = i
    if year_index is not None:
        year = int(tokens[year_index])
        consumed[year_index] = True
        cut = min(cut, year_index)

    # Series pass before the title scan: S/E markers are metadata, so they
    # move the cut too - that is what drops trailing episode NAMES
    # ("... S01E12 A New Knot") from the series identity.
    season, episodes, series_index = _extract_series(tokens, consumed)
    if series_index is not None:
        cut = min(cut, series_index)

    languages: list[str] = []
    title_tokens: list[tuple[int, str]] = []
    for i, token in enumerate(tokens):
        if consumed[i]:
            continue
        language = TOKEN_TO_LANGUAGE.get(token)
        if language is not None:
            if language not in languages:
                languages.append(language)
            continue
        label = resolution_label(token)
        if label is not None:
            resolution = resolution or label
            cut = min(cut, i)
            continue
        label = source_label(token)
        if label is not None:
            source = source or label
            cut = min(cut, i)
            continue
        if _CODEC_RE.match(token) or _RATE_RE.match(token) or _CHANNELS_RE.match(token):
            cut = min(cut, i)
            continue
        if token in _JUNK_TOKENS or token.startswith("@"):
            continue
        title_tokens.append((i, token))

    # Debris drop: unknown tokens at/after the first metadata marker are
    # release groups, platform tags, or codec fragments - never title.
    kept = [token for i, token in title_tokens if i < cut]
    # Safety net: a name like "PSA.2026.mkv" would cut everything; fall
    # back to the old keep-all behavior rather than lose the title.
    if not kept and title_tokens:
        kept = [token for _, token in title_tokens]

    # A bare "1917.mkv": the year-like token IS the title.
    if not kept and year is not None:
        kept = [str(year)]
        year = None

    if caption:
        # Subtitle lists (💬 line, "ESub"/"Subs: ..." tails) are stripped
        # first - only audio languages may reach the index.
        caption_scan = strip_subtitle_segments(caption)
        for language in languages_in_text(caption_scan):
            if language not in languages:
                languages.append(language)
        # Resolution/source fallback: truncated filenames often lose the
        # quality tail that the caption still carries.
        if resolution is None or source is None:
            caption_tokens = tokenize(caption_scan)
            for i, token in enumerate(caption_tokens):
                if resolution is None:
                    resolution = resolution_label(token)
                if source is None:
                    source = source_label(token) or BIGRAM_SOURCE_TOKENS.get(
                        (token, caption_tokens[i + 1])
                        if i + 1 < len(caption_tokens)
                        else ("", "")
                    )

    quality = " ".join(part for part in (resolution, source) if part) or None
    return ParsedMedia(
        title_guess=" ".join(kept),
        year=year,
        languages=tuple(languages),
        quality=quality,
        season=season,
        episodes=episodes,
    )
