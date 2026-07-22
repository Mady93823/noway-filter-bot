"""Re-run the parser over already-indexed rows. No Telegram calls.

Every file row keeps its raw_file_name and caption, so a parser upgrade
can be applied to the whole index locally - no channel re-walk, no rate
limits, and telegram_file_id/uid are never touched so nothing an
indexed file needs to stay sendable is at risk.

    uv run python -m worker.reparse            # apply
    uv run python -m worker.reparse --dry-run  # report only

Titles left with no files afterwards are deleted, so the identity split
(one row per season) actually shows up instead of leaving empty shells.
"""

import argparse
import asyncio
import logging

from sqlalchemy import delete, func, select

from shared.db.engine import dispose_engine, get_session_factory
from shared.db.models import File, Title
from shared.parsing.filename import parse_media
from worker.resolver import resolve_title

logger = logging.getLogger(__name__)

BATCH = 500


async def reparse(dry_run: bool = False) -> dict[str, int]:
    session_factory = get_session_factory()
    stats = {"files": 0, "moved": 0, "titles_before": 0, "titles_after": 0, "orphans": 0}

    async with session_factory() as session:
        stats["titles_before"] = await session.scalar(
            select(func.count()).select_from(Title)
        )

    last_id = 0
    while True:
        async with session_factory() as session:
            async with session.begin():
                files = (
                    await session.scalars(
                        select(File)
                        .where(File.id > last_id)
                        .order_by(File.id)
                        .limit(BATCH)
                    )
                ).all()
                if not files:
                    break
                for file in files:
                    last_id = file.id
                    stats["files"] += 1
                    parsed = parse_media(file.raw_file_name, file.caption)
                    if not parsed.title_guess:
                        continue
                    title = await resolve_title(session, parsed)
                    if title.id != file.title_id:
                        stats["moved"] += 1
                        if not dry_run:
                            file.title_id = title.id
                    if not dry_run:
                        file.quality = parsed.quality
                        file.languages = list(parsed.languages)
                        file.episodes = parsed.episodes
                if dry_run:
                    # Nothing may persist on a dry run - not even the title
                    # rows resolve_title created while probing.
                    await session.rollback()

    async with session_factory() as session:
        async with session.begin():
            orphan_ids = (
                await session.scalars(
                    select(Title.id).where(
                        ~select(File.id).where(File.title_id == Title.id).exists()
                    )
                )
            ).all()
            stats["orphans"] = len(orphan_ids)
            if orphan_ids and not dry_run:
                await session.execute(delete(Title).where(Title.id.in_(orphan_ids)))
        stats["titles_after"] = await session.scalar(
            select(func.count()).select_from(Title)
        )
    return stats


async def main() -> None:
    parser = argparse.ArgumentParser(description="Re-parse the existing index")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    stats = await reparse(args.dry_run)
    await dispose_engine()
    print(
        f"{'DRY RUN - ' if args.dry_run else ''}"
        f"files reparsed: {stats['files']} · moved to another title: "
        f"{stats['moved']} · titles {stats['titles_before']} -> "
        f"{stats['titles_after']} (orphans removed: {stats['orphans']})"
    )


if __name__ == "__main__":
    asyncio.run(main())
