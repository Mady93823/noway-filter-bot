"""Pyrogram (Pyrofork) client factory.

One bot token, multiple named sessions: the worker session handles
channel-post updates + backfill; the bot session handles user chats.
file_id values are bot-scoped, and both sessions share the same bot
identity, so files indexed by the worker are sendable by the bot.

Bot API token only - never a user session (golden rule 5).
"""

from pathlib import Path

from pyrogram import Client

from shared.config import get_settings

SESSIONS_DIR = Path("sessions")


def create_client(session_name: str) -> Client:
    settings = get_settings()
    SESSIONS_DIR.mkdir(exist_ok=True)
    return Client(
        name=session_name,
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        workdir=str(SESSIONS_DIR),
    )
