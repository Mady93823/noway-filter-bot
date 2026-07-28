"""Queue the whole historical index for TMDB enrichment.

0006 enriched only upcoming titles; every title that predated it was set to
'skip'. This flips those (and past 'nomatch' misses the article-fix reparse
may now resolve) back to 'pending', so the running worker's enrich_dispatcher
sweeps them into the deduped posters table.

    python -m worker.enrich_all             # queue the sweep
    python -m worker.enrich_all --dry-run   # count what would be queued

RUN ORDER MATTERS: run this AFTER the article-fix reparse has finished, or the
sweep will fetch posters for title rows the reparse is about to merge away.
The actual fetching is done by the worker in the background - this only sets
the flag; nothing here calls TMDB.
"""

import argparse
import asyncio

from sqlalchemy import func, select

from shared.db.engine import dispose_engine, get_session_factory
from shared.db.models import Title
from shared.db.repos import titles as titles_repo


async def main() -> None:
    parser = argparse.ArgumentParser(description="Queue every title for enrichment")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    session_factory = get_session_factory()
    async with session_factory() as session:
        eligible = await session.scalar(
            select(func.count())
            .select_from(Title)
            .where(Title.enrich_status.in_(("skip", "nomatch")))
        )
        if args.dry_run:
            print(f"DRY RUN - would queue {eligible:,} titles for enrichment")
        else:
            async with session.begin():
                flipped = await titles_repo.mark_all_pending(session)
            print(f"queued {flipped:,} titles for enrichment (was skip/nomatch)")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
