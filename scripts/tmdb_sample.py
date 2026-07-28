"""Sample a channel's recent media and probe TMDB matching quality.

Two jobs, one tool:

  1. Pull the last N media messages from a channel (id, filename, caption)
     so we can see what the raw data actually looks like before designing
     enrichment.
  2. With --tmdb, run each sample through the REAL production parser
     (shared.parsing.filename.parse_media) to get title/year, query TMDB
     /search/movie, and print the best match + poster URL. This is the
     experiment: how well does "our parsed guess -> TMDB" actually land.

Why a known latest id is required
---------------------------------
A bot token cannot read channel history (get_chat_history is blocked
server-side for bots, over Bot API and MTProto alike). So there is no
"give me the last 100" call. Instead we walk message ids DESCENDING from
a ceiling and fetch them by id (get_messages accepts <=200 ids/call,
which is exactly how the backfill worker reads a channel).

Get the ceiling id for free: open the channel, click its newest post,
"Copy Message Link" -> t.me/c/<internal>/<ID>. That trailing <ID> is the
latest message id. Pass it as --latest-id. Or pass --probe to have the
bot post a throwaway message, read its id, and delete it (bot must have
post rights; subscribers may briefly see it).

TMDB rule reminder (CLAUDE.md golden rule 1)
--------------------------------------------
This script is an OFFLINE experiment, not the runtime path. Production
must never call TMDB live in search/index. The intended shape is: worker
resolves a title once -> fetches poster + tmdb_id once -> stores it ->
search reads the stored poster. This script only measures match quality
to justify that pipeline; it does not wire TMDB into the bot.

Usage
-----
  python -m scripts.tmdb_sample --channel -1004478862246 --latest-id 5321
  python -m scripts.tmdb_sample --channel -1004475836877 --latest-id 900 --tmdb
  python -m scripts.tmdb_sample --channel -1004478862246 --probe --count 50 --tmdb

Reads TMDB_BEARER (v4 Read Access Token) or TMDB_API_KEY (v3) from .env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.parsing.filename import ParsedMedia, parse_media
from shared.telegram.client import create_client

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"
# w342 is the sweet spot for a Telegram search-card thumbnail: sharp on a
# phone, ~30-50 KB, cheap to fetch and cache. Bump to w500 for a hero.
POSTER_SIZE = "w342"
GET_MESSAGES_MAX = 200  # Telegram hard cap on ids per get_messages call


class _TmdbSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    tmdb_bearer: str = ""
    tmdb_api_key: str = ""


@dataclass
class Sample:
    message_id: int
    file_name: str | None
    caption: str | None
    file_size: int | None
    mime_type: str | None


def _extract_media(message):
    return message.document or message.video or message.audio


def _caption_line(caption: str | None) -> str:
    if not caption:
        return ""
    return caption.strip().splitlines()[0] if caption.strip() else ""


async def _resolve_ceiling(app, channel: int, latest_id: int | None, probe: bool) -> int:
    if latest_id is not None:
        return latest_id
    if probe:
        # Post -> read id -> delete. The id of a fresh post is the current
        # ceiling; the message is removed immediately after.
        msg = await app.send_message(channel, "⁣")  # invisible separator char
        top = msg.id
        try:
            await app.delete_messages(channel, msg.id)
        except Exception as exc:  # deletion is best-effort; id is what we need
            print(f"  (probe posted id {top} but delete failed: {exc})")
        return top
    raise SystemExit(
        "Need a ceiling message id. Pass --latest-id <N> (copy the newest "
        "post's message link; the trailing number is N) or --probe."
    )


async def collect_samples(channel: int, latest_id: int | None, probe: bool, count: int) -> list[Sample]:
    samples: list[Sample] = []
    app = create_client("tmdb_sample")
    async with app:
        top = await _resolve_ceiling(app, channel, latest_id, probe)
        print(f"channel {channel}: walking down from message id {top}, want {count} media")
        while top > 0 and len(samples) < count:
            low = max(1, top - GET_MESSAGES_MAX + 1)
            ids = list(range(low, top + 1))
            messages = await app.get_messages(channel, ids)
            # Descending so "last N" means newest first.
            for message in sorted(
                (m for m in messages if m and not getattr(m, "empty", False)),
                key=lambda m: m.id,
                reverse=True,
            ):
                media = _extract_media(message)
                if media is None:
                    continue
                samples.append(
                    Sample(
                        message_id=message.id,
                        file_name=getattr(media, "file_name", None),
                        caption=message.caption,
                        file_size=getattr(media, "file_size", None),
                        mime_type=getattr(media, "mime_type", None),
                    )
                )
                if len(samples) >= count:
                    break
            top = low - 1
    return samples


def _tmdb_get(path: str, params: dict, settings: _TmdbSettings) -> dict:
    """Blocking GET, run via asyncio.to_thread. Bearer (v4) preferred."""
    query = dict(params)
    headers = {"accept": "application/json"}
    if settings.tmdb_bearer:
        headers["Authorization"] = f"Bearer {settings.tmdb_bearer}"
    elif settings.tmdb_api_key:
        query["api_key"] = settings.tmdb_api_key
    else:
        raise SystemExit("No TMDB_BEARER or TMDB_API_KEY in .env")
    url = f"{TMDB_BASE}{path}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


async def tmdb_lookup(parsed: ParsedMedia, settings: _TmdbSettings, media_type: str) -> dict | None:
    if not parsed.title_guess:
        return None
    params = {"query": parsed.title_guess, "include_adult": "false", "language": "en-US"}
    if parsed.year:
        # TV and movie name the year filter differently: first_air_year vs
        # primary_release_year (both tighter than the loose `year`).
        params["first_air_date_year" if media_type == "tv" else "primary_release_year"] = str(parsed.year)
    data = await asyncio.to_thread(_tmdb_get, f"/search/{media_type}", params, settings)
    results = data.get("results") or []
    return _normalize(results[0], media_type) if results else None


def _normalize(result: dict, media_type: str) -> dict:
    """Flatten movie vs tv field-name differences into one shape."""
    if media_type == "tv":
        result = dict(result)
        result["title"] = result.get("name")
        result["original_title"] = result.get("original_name")
        result["release_date"] = result.get("first_air_date")
    return result


def _poster_url(poster_path: str | None) -> str | None:
    return f"{TMDB_IMAGE_BASE}/{POSTER_SIZE}{poster_path}" if poster_path else None


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", type=int, required=True, help="channel id, e.g. -1004478862246")
    parser.add_argument("--latest-id", type=int, default=None, help="newest message id (ceiling)")
    parser.add_argument("--probe", action="store_true", help="post+delete a msg to learn the ceiling")
    parser.add_argument("--count", type=int, default=100, help="how many media samples to pull")
    parser.add_argument("--tmdb", action="store_true", help="also run each sample through TMDB search")
    parser.add_argument("--type", choices=("movie", "tv"), default="movie",
                        help="TMDB endpoint: movie (default) or tv for series channels")
    parser.add_argument("--out", type=Path, default=None, help="write full JSON dump here")
    args = parser.parse_args()

    samples = await collect_samples(args.channel, args.latest_id, args.probe, args.count)
    print(f"\ncollected {len(samples)} media samples\n" + "=" * 70)

    settings = _TmdbSettings() if args.tmdb else None
    dump: list[dict] = []
    hits = 0
    for sample in samples:
        line = _caption_line(sample.caption)
        print(f"\n#{sample.message_id}  {sample.file_name or '(no filename)'}")
        if line:
            print(f"    caption: {line}")
        record: dict = {"sample": asdict(sample)}
        if args.tmdb and settings is not None:
            parsed = parse_media(sample.file_name, sample.caption)
            record["parsed"] = {"title": parsed.title_guess, "year": parsed.year,
                                "languages": list(parsed.languages), "season": parsed.season}
            print(f"    parsed:  title={parsed.title_guess!r} year={parsed.year} "
                  f"langs={list(parsed.languages)} season={parsed.season}")
            try:
                match = await tmdb_lookup(parsed, settings, args.type)
            except Exception as exc:
                print(f"    TMDB error: {exc}")
                match = None
            if match:
                hits += 1
                record["tmdb"] = {
                    "id": match.get("id"), "title": match.get("title"),
                    "original_title": match.get("original_title"),
                    "release_date": match.get("release_date"),
                    "original_language": match.get("original_language"),
                    "vote_average": match.get("vote_average"),
                    "overview": match.get("overview"),
                    "poster_url": _poster_url(match.get("poster_path")),
                }
                print(f"    TMDB:    [{match.get('id')}] {match.get('title')} "
                      f"({(match.get('release_date') or '????')[:4]}) "
                      f"lang={match.get('original_language')} "
                      f"vote={match.get('vote_average')}")
                print(f"    poster:  {_poster_url(match.get('poster_path'))}")
            else:
                record["tmdb"] = None
                print("    TMDB:    no match")
        dump.append(record)

    if args.tmdb:
        print("\n" + "=" * 70)
        print(f"TMDB match rate: {hits}/{len(samples)} "
              f"({100 * hits / len(samples):.0f}%)" if samples else "no samples")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
