"""CLI to create/restart a backfill job without the bot running.

Usage:
    python -m worker.enqueue --channel-id -1001234567890 --last-message-id 54321

The running worker picks the job up on its next poll.
"""

import argparse
import asyncio

from shared.db.engine import dispose_engine, get_session_factory
from shared.db.repos import progress as progress_repo


async def _run(channel_id: int, last_message_id: int) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session, session.begin():
        await progress_repo.upsert_job(session, channel_id, last_message_id)
    await dispose_engine()
    print(f"job upserted: channel={channel_id} target={last_message_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-id", type=int, required=True)
    parser.add_argument(
        "--last-message-id",
        type=int,
        required=True,
        help="Latest message id in the channel (the backfill upper bound)",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.channel_id, args.last_message_id))


if __name__ == "__main__":
    main()
