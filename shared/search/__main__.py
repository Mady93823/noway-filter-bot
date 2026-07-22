"""Manual search CLI for development.

Usage:
    uv run python -m shared.search "swati 1997 tamil"
    uv run python -m shared.search "swati 1997 tamil" --cursor "<token>"
"""

import argparse
import asyncio

from shared.db.engine import dispose_engine, get_session_factory
from shared.redis_client import close_redis
from shared.search.service import search


def _size_label(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


async def _run(query: str, cursor: str | None) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        page = await search(session, query, cursor)

    if page.expired:
        print("cursor expired - search again")
    elif not page.results:
        print("no results")
    for result in page.results:
        year = f" ({result.year})" if result.year else ""
        languages = ", ".join(result.languages) or "unknown"
        print(f"{result.display_title}{year} [{languages}]")
        for variant in result.variants:
            print(f"   {variant.quality or 'unknown'} - {_size_label(variant.file_size)}")
    print(f"total={page.total} next_cursor={page.next_cursor}")

    await close_redis()
    await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--cursor", default=None)
    args = parser.parse_args()
    asyncio.run(_run(args.query, args.cursor))


if __name__ == "__main__":
    main()
