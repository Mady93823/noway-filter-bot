"""Re-run the parser over already-indexed rows. No Telegram calls.

Every file row keeps its raw_file_name and caption, so a parser upgrade
can be applied to the whole index locally - no channel re-walk, no rate
limits, and telegram_file_id/uid are never touched so nothing an
indexed file needs to stay sendable is at risk.

    uv run python -m worker.reparse            # apply
    uv run python -m worker.reparse --dry-run  # report only

Admins normally run it from the bot instead (/reparse), which drives this
same function from inside the worker and reports progress in /stats - a
run over lakhs of files takes long enough that a silent CLI looks hung.

Titles left with no files afterwards are deleted, so the identity split
(one row per season) actually shows up instead of leaving empty shells.

A final pass repairs display titles that a fuzzy match captured. Re-parsing
alone cannot undo those: merge_metadata only ever lengthens a display
title, so a row renamed to "Ep 10 Bang Bang" would keep that name forever
even once the parser stops producing it.
"""

import argparse
import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import delete, func, select

from shared.db.engine import dispose_engine, get_session_factory
from shared.db.models import File, Title
from shared.parsing.filename import parse_media, strip_leading_article
from worker.resolver import resolve_title

logger = logging.getLogger(__name__)

BATCH = 500

# (phase, done, total, stats) - called once per committed batch, so what a
# caller publishes is always a state the database has actually reached.
ProgressHook = Callable[[str, int, int, dict[str, int]], Awaitable[None]]
CancelHook = Callable[[], Awaitable[bool]]


async def _repair_display_titles(
    session_factory,
    dry_run: bool,
    on_progress: ProgressHook | None = None,
    stats: dict[str, int] | None = None,
) -> int:
    """Reset display titles that no longer spell out their own canonical.

    The rule mirrors the guard now in titles_repo.merge_metadata: a
    display title may only be a longer spelling of the canonical text, so
    "swat" -> "Swati" is legitimate and survives, while "bang bang" ->
    "Ep 10 Bang Bang" is debris a fuzzy match dragged in and is reset to
    the canonical name. Comparison is case-insensitive because display is
    title-cased and canonical is not.
    """
    renamed = 0
    seen = 0
    last_id = 0
    async with session_factory() as session:
        total = await session.scalar(select(func.count()).select_from(Title)) or 0
    while True:
        async with session_factory() as session:
            async with session.begin():
                titles = (
                    await session.scalars(
                        select(Title)
                        .where(Title.id > last_id)
                        .order_by(Title.id)
                        .limit(BATCH)
                    )
                ).all()
                if not titles:
                    return renamed
                for title in titles:
                    last_id = title.id
                    seen += 1
                    # The canonical is article-stripped, so a legit display
                    # ("The Wicked Within") won't start with it directly -
                    # strip the display's own leading article before the
                    # check, or every "The …"/"A …" title would be reset.
                    display_key = strip_leading_article(title.display_title.lower())
                    if display_key.startswith(title.canonical_title.lower()):
                        continue
                    renamed += 1
                    if not dry_run:
                        # Only reached when the display is debris a fuzzy
                        # match dragged in ("Ep 10 Bang Bang"); the canonical
                        # is the clean fallback. A real "The …" display was
                        # already kept by the article-aware check above.
                        title.display_title = title.canonical_title.title()
                if dry_run:
                    await session.rollback()
        if on_progress is not None:
            merged = dict(stats or {})
            merged["renamed"] = renamed
            await on_progress("titles", seen, total, merged)


async def reparse(
    dry_run: bool = False,
    on_progress: ProgressHook | None = None,
    should_cancel: CancelHook | None = None,
) -> dict[str, int]:
    session_factory = get_session_factory()
    stats = {
        "files": 0,
        "moved": 0,
        "titles_before": 0,
        "titles_after": 0,
        "orphans": 0,
        "renamed": 0,
        "total": 0,
        "cancelled": 0,
    }

    async with session_factory() as session:
        stats["titles_before"] = await session.scalar(
            select(func.count()).select_from(Title)
        )
        # Counted up front purely so progress can be a percentage. An
        # exact count of a large table costs one seq scan, once - cheap
        # next to the per-file work that follows.
        stats["total"] = (
            await session.scalar(select(func.count()).select_from(File)) or 0
        )

    if on_progress is not None:
        await on_progress("files", 0, stats["total"], stats)

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
        # Both hooks run between batches, never inside the transaction: the
        # batch is already committed, so what is reported is durable and a
        # cancel stops on a clean boundary rather than tearing one open.
        if on_progress is not None:
            await on_progress("files", stats["files"], stats["total"], stats)
        if should_cancel is not None and await should_cancel():
            stats["cancelled"] = 1
            logger.info("reparse cancelled after %s files", stats["files"])
            break

    if not stats["cancelled"]:
        # Skipped on cancel: it is a second full-table pass, and an admin
        # who just asked for a stop should get one. The orphan sweep below
        # still runs - a title with zero files left is junk either way, and
        # leaving it behind is exactly the stale name the re-parse was for.
        stats["renamed"] = await _repair_display_titles(
            session_factory, dry_run, on_progress, stats
        )

    if on_progress is not None:
        await on_progress("cleanup", 0, 0, stats)

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
        f"{stats['moved']} · display titles repaired: {stats['renamed']} · "
        f"titles {stats['titles_before']} -> "
        f"{stats['titles_after']} (orphans removed: {stats['orphans']})"
    )


if __name__ == "__main__":
    asyncio.run(main())
