"""Dev tool: dump every file's name + caption from a channel to JSONL.

Not a pytest file. Feeds real-world naming patterns back into parser
development: each row carries the raw strings plus what parse_media
currently makes of them, so weak spots are easy to grep.

Usage:
    uv run python tests/extract_channel_files.py <channel_id> <last_message_id>
    uv run python tests/extract_channel_files.py -1001234567890 5000 --out dump.jsonl

The bot must be an admin in the channel (same requirement as indexing).
"""

import argparse
import asyncio
import json
from dataclasses import asdict

from pyrogram.errors import FloodWait

from shared.parsing.filename import parse_media
from shared.telegram.client import create_client

BATCH = 100


def _media_of(message):
    return message.document or message.video or message.audio


async def _run(channel_id: int, last_id: int, out_path: str) -> None:
    app = create_client("nowaybot-extract")
    rows = 0
    await app.start()
    try:
        with open(out_path, "w", encoding="utf-8") as out:
            for start in range(1, last_id + 1, BATCH):
                ids = list(range(start, min(start + BATCH, last_id + 1)))
                while True:
                    try:
                        messages = await app.get_messages(channel_id, ids)
                        break
                    except FloodWait as exc:
                        print(f"FloodWait {exc.value}s - honoring")
                        await asyncio.sleep(float(exc.value) + 1)
                for message in messages or []:
                    if message is None or getattr(message, "empty", False):
                        continue
                    media = _media_of(message)
                    if media is None:
                        continue
                    file_name = getattr(media, "file_name", None)
                    parsed = parse_media(file_name, message.caption)
                    out.write(
                        json.dumps(
                            {
                                "message_id": message.id,
                                "file_name": file_name,
                                "caption": message.caption,
                                "parsed": asdict(parsed),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    rows += 1
                print(f"scanned up to {ids[-1]}/{last_id} · {rows} files")
                await asyncio.sleep(1.0)
    finally:
        await app.stop()
    print(f"done: {rows} files -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel_id", type=int)
    parser.add_argument("last_message_id", type=int)
    parser.add_argument("--out", default="channel_dump.jsonl")
    args = parser.parse_args()
    asyncio.run(_run(args.channel_id, args.last_message_id, args.out))


if __name__ == "__main__":
    main()
