"""Lakh-scale load rehearsal for the index and the search path.

What this proves, and what it does not
--------------------------------------
It answers the question that actually decides whether this bot survives
a real channel: does search stay fast, and do the indexes stay honest,
once titles and files hold hundreds of thousands of rows? Everything
that degrades at scale on the storage side - the trigram GIN index, the
3-rung ladder, per-title variant grouping, deep pagination - runs
against real Postgres here.

It does NOT rehearse Telegram: no FloodWait, no get_messages pacing.
That half needs a real channel and can only be measured for real.

Every row is tagged with a fixed prefix, so --cleanup removes exactly
what this created and can never touch a genuinely indexed file.

    uv run python scripts/loadtest.py --rows 100000
    uv run python scripts/loadtest.py --report-only
    uv run python scripts/loadtest.py --cleanup
"""

import argparse
import asyncio
import random
import statistics
import time

from sqlalchemy import delete, func, select, text

from shared.db.engine import dispose_engine, get_session_factory
from shared.db.models import File, Title
from shared.redis_client import close_redis, get_redis
from shared.search.service import search

TAG = "loadtest"
BATCH = 5_000
QUALITIES = ["480p HDRip", "720p WEB-DL", "1080p WEB-DL", "1080p BluRay", "2160p"]
LANGUAGES = [["tamil"], ["telugu"], ["hindi", "english"], ["malayalam"], ["kannada"]]
# Deliberately repetitive: a corpus of unique nonsense would give trigram
# search no real competition and make it look faster than it is.
WORDS = [
    "kadhal", "veeran", "raja", "anbu", "thala", "vetri", "maaran",
    "puli", "kaadu", "mazhai", "nila", "aruvi", "thendral", "kanaa",
]


def _title_name(index: int) -> str:
    random.seed(index)
    return " ".join(random.choice(WORDS) for _ in range(random.randint(1, 3)))


async def seed(rows: int) -> None:
    session_factory = get_session_factory()
    started = time.monotonic()
    made = 0

    while made < rows:
        size = min(BATCH, rows - made)
        titles = []
        for offset in range(size):
            index = made + offset
            name = _title_name(index)
            titles.append(
                {
                    # The tag lives in the canonical title, so cleanup is a
                    # prefix match rather than a guess about which rows
                    # belong to the test.
                    "canonical_title": f"{TAG} {name} {index}",
                    "display_title": f"{TAG.title()} {name.title()} {index}",
                    "year": 1980 + (index % 46),
                    "season": None,
                    "languages": LANGUAGES[index % len(LANGUAGES)],
                }
            )
        async with session_factory() as session, session.begin():
            result = await session.execute(
                Title.__table__.insert().returning(Title.id), titles
            )
            title_ids = [row[0] for row in result]

            files = []
            for position, title_id in enumerate(title_ids):
                index = made + position
                # 1-4 variants per title: grouping and the per-title file
                # query are a large part of what this measures.
                for variant in range((index % 4) + 1):
                    files.append(
                        {
                            "title_id": title_id,
                            "telegram_file_uid": f"{TAG}-{index}-{variant}",
                            "telegram_file_id": f"{TAG}-fid-{index}-{variant}",
                            "raw_file_name": f"{_title_name(index)}.{1980 + index % 46}.mkv",
                            "caption": None,
                            "quality": QUALITIES[variant % len(QUALITIES)],
                            "episodes": None,
                            "file_size": 400_000_000 + variant * 700_000_000,
                            "mime_type": "video/x-matroska",
                            "languages": LANGUAGES[index % len(LANGUAGES)],
                            "source_channel_id": -1001234567890,
                            "source_message_id": index,
                        }
                    )
            await session.execute(File.__table__.insert(), files)

        made += size
        elapsed = time.monotonic() - started
        print(f"  seeded {made:,}/{rows:,} titles  ({made / elapsed:,.0f} rows/s)")

    # Without this the planner is still working from pre-load statistics
    # and every timing below is a lie.
    async with session_factory() as session, session.begin():
        await session.execute(text("ANALYZE titles"))
        await session.execute(text("ANALYZE files"))
    print(f"  seeded in {time.monotonic() - started:,.1f}s, statistics refreshed")


async def measure(samples: int = 40) -> None:
    session_factory = get_session_factory()
    redis = get_redis()

    async def timed(label: str, queries: list[str]) -> None:
        timings = []
        hits = 0
        for query in queries:
            # Clear the cache every time: this measures Postgres, not Redis.
            keys = await redis.keys("search:*")
            if keys:
                await redis.delete(*keys)
            async with session_factory() as session:
                started = time.perf_counter()
                page = await search(session, query)
                timings.append((time.perf_counter() - started) * 1000)
            hits += page.total
        timings.sort()
        p50 = statistics.median(timings)
        p95 = timings[max(0, int(len(timings) * 0.95) - 1)]
        print(
            f"  {label:<22} p50 {p50:7.1f} ms   p95 {p95:7.1f} ms   "
            f"max {max(timings):7.1f} ms   ({hits:,} hits)"
        )

    exact = [f"{TAG} {_title_name(i)} {i}" for i in range(samples)]
    partial = [_title_name(i) for i in range(samples)]
    typos = [_title_name(i).replace("a", "e", 1) for i in range(samples)]

    await timed("exact title", exact)
    await timed("partial (substring)", partial)
    await timed("typo (trigram)", typos)


async def report() -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        titles = await session.scalar(select(func.count(Title.id)))
        files = await session.scalar(select(func.count(File.id)))
        synthetic = await session.scalar(
            select(func.count(Title.id)).where(Title.canonical_title.like(f"{TAG} %"))
        )
        size = await session.scalar(
            text("SELECT pg_size_pretty(pg_database_size(current_database()))")
        )
        index_size = await session.scalar(
            text("SELECT pg_size_pretty(pg_relation_size('ix_titles_canonical_trgm'))")
        )
    print(
        f"  titles {titles:,} ({synthetic:,} synthetic) · files {files:,} · "
        f"database {size} · trigram index {index_size}"
    )


async def cleanup() -> None:
    session_factory = get_session_factory()
    async with session_factory() as session, session.begin():
        # files would cascade from titles, but delete them explicitly so
        # the reported count is measured rather than assumed.
        removed_files = await session.execute(
            delete(File).where(File.telegram_file_uid.like(f"{TAG}-%"))
        )
        removed_titles = await session.execute(
            delete(Title).where(Title.canonical_title.like(f"{TAG} %"))
        )
    redis = get_redis()
    keys = await redis.keys("search:*")
    if keys:
        await redis.delete(*keys)
    print(
        f"  removed {removed_titles.rowcount:,} synthetic titles and "
        f"{removed_files.rowcount:,} files"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000, help="synthetic titles")
    parser.add_argument("--cleanup", action="store_true", help="remove synthetic rows")
    parser.add_argument("--report-only", action="store_true", help="counts and sizes")
    args = parser.parse_args()

    if args.cleanup:
        print("cleanup:")
        await cleanup()
    elif args.report_only:
        print("index:")
        await report()
    else:
        print(f"seeding {args.rows:,} titles:")
        await seed(args.rows)
        print("index:")
        await report()
        print("search latency (cold cache, Postgres only):")
        await measure()
        print("\nDone. Remove the synthetic rows with: --cleanup")

    await close_redis()
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
