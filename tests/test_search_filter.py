"""What counts as a search query.

Regression: the worker runs a second Pyrogram session on the same bot
token, so its admin progress DMs came back to the bot session as normal
messages and got searched ("Nothing found for ... Indexing 94/94").
"""

from types import SimpleNamespace

import pytest
from pyrogram.enums import ChatType

from bot.handlers.search import QUERY_FILTER


def _message(
    text: str = "swati 1997 tamil",
    *,
    chat_type: ChatType = ChatType.PRIVATE,
    is_self: bool = False,
    is_bot: bool = False,
    outgoing: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(type=chat_type),
        from_user=SimpleNamespace(is_self=is_self, is_bot=is_bot),
        outgoing=outgoing,
        forward_origin=None,
        via_bot=None,
    )


async def _accepts(message) -> bool:
    return await QUERY_FILTER(None, message)


@pytest.mark.asyncio
async def test_normal_user_query_accepted():
    assert await _accepts(_message())


@pytest.mark.asyncio
async def test_group_query_accepted():
    assert await _accepts(_message(chat_type=ChatType.GROUP))


@pytest.mark.asyncio
async def test_own_progress_message_ignored():
    progress = "Indexing -1004456107119\nindexed 94/94 (100.0%)"
    assert not await _accepts(_message(progress, is_self=True, is_bot=True))


@pytest.mark.asyncio
async def test_outgoing_message_ignored():
    assert not await _accepts(_message("Backfill complete", outgoing=True))


@pytest.mark.asyncio
async def test_other_bot_ignored():
    assert not await _accepts(_message(chat_type=ChatType.GROUP, is_bot=True))


@pytest.mark.asyncio
async def test_commands_ignored():
    assert not await _accepts(_message("/stats"))
