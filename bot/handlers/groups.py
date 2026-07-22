"""Group membership bookkeeping.

The groups table existed from migration 0001 but nothing ever wrote to
it. Two ways a row appears:

1. The bot is added to a chat - the reliable path.
2. Lazily, the first time an already-present group is active. Groups
   that predate this bookkeeping, or joins the bot missed while it was
   offline, get picked up without anyone re-adding it.

The lazy path is throttled through Redis so it costs one round trip per
group message and one INSERT per group per day - never a write per
message.
"""

import logging

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import Chat, Message

from bot.ephemeral import expire_in_group
from shared.db.engine import get_session_factory
from shared.db.repos import groups as groups_repo
from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_SEEN_PREFIX = "grp:seen:"
_SEEN_TTL = 86_400  # one day

_WELCOME = (
    "👋 <b>Thanks for the add!</b>\n\n"
    "🎬 Send any movie name here and I'll dig out every quality variant "
    "I've indexed.\n\n"
    "✨ <b>Best results:</b> <code>name year language</code>\n"
    "🛠 Group admins: <code>/filter</code>, <code>/filters</code>, "
    "<code>/stop</code> for keyword replies."
)


async def _record(group_id: int, title: str | None) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session, session.begin():
        await groups_repo.upsert_group(session, group_id, title)


async def touch_group(chat: Chat) -> None:
    """Lazily record an active group, at most once a day per group."""
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    redis = get_redis()
    # set(nx) is the throttle AND the claim in one round trip: only the
    # caller that actually sets the key does the write.
    if not await redis.set(f"{_SEEN_PREFIX}{chat.id}", "1", ex=_SEEN_TTL, nx=True):
        return
    await _record(chat.id, chat.title)


def register_group_handlers(app: Client) -> None:
    @app.on_message(filters.new_chat_members)
    async def _on_added(client: Client, message: Message) -> None:
        me = client.me.id
        if not any(member.id == me for member in message.new_chat_members):
            return  # somebody else joined; not our bookkeeping
        await _record(message.chat.id, message.chat.title)
        logger.info("added to group %s (%s)", message.chat.id, message.chat.title)
        sent = await message.reply_text(_WELCOME, parse_mode=ParseMode.HTML)
        # Same rule as every other group message. Worth knowing: this is
        # the one whose usefulness outlives the window, since it is the
        # setup note for admins - raise GROUP_MESSAGE_TTL if that bites.
        await expire_in_group(message, sent)

    @app.on_message(filters.left_chat_member)
    async def _on_removed(client: Client, message: Message) -> None:
        if message.left_chat_member.id != client.me.id:
            return
        # Deactivate, never delete: filters cascade off this row and a
        # kick is often accidental. Re-adding restores the setup.
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            await groups_repo.set_active(session, message.chat.id, False)
        logger.info("removed from group %s", message.chat.id)
