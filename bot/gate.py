"""The gate in front of file delivery.

Composes the three pieces that have to agree before a file goes out:
bot/access.py (is the clock still running), shared/settings_store.py (is
the gate even switched on), and shared/shortener.py (turn the verify
deep link into an ad link). Kept apart from access.py so that module
stays pure clock-and-token logic with no Telegram or UI in it.

Fails closed, on purpose. If the shortener is unreachable the user is
told to retry and an admin is alerted - the file is not released. The
alternative silently disables monetisation for everyone the moment the
provider hiccups, and anyone who noticed could induce it deliberately.

Search is never gated. Only delivery is: someone has to be able to find
out the bot has what they want before being asked for anything.
"""

import logging

from pyrogram.enums import ParseMode

from bot import access, ui
from shared import logchannel
from shared.alerts import notify_admins
from shared.db.engine import get_session_factory
from shared.logchannel import log_event
from shared.settings_store import access_hours, gate_enabled
from shared.shortener import ShortenerError, shorten

logger = logging.getLogger(__name__)


async def blocked(client, user, message=None) -> bool:
    """True when the tap was intercepted; the unlock card has been sent.

    False means "carry on and deliver" - either the gate is off, or this
    user's clock is still running.
    """
    if not await gate_enabled():
        return False

    session_factory = get_session_factory()
    async with session_factory() as session:
        if await access.has_access(session, user.id):
            return False

    hours = await access_hours()
    token = await access.mint_token(user.id)
    target = f"https://t.me/{client.me.username}?start=verify_{token}"

    try:
        short_url = await shorten(target)
    except ShortenerError as exc:
        logger.warning("gate could not shorten for %s: %s", user.id, exc)
        await client.send_message(
            user.id, ui.gate_unavailable_text(), parse_mode=ParseMode.HTML
        )
        # Deduped by the alerts layer, so a dead provider costs one DM
        # per cooldown rather than one per blocked tap.
        await notify_admins(
            client,
            f"⚠️ Shortener failed - deliveries are gated and failing.\n{exc}",
            dedupe_key="shortener",
        )
        await log_event(
            client,
            logchannel.ERROR,
            "Shortener failure",
            {"User": user.id, "Error": str(exc)},
        )
        return True

    text, keyboard = ui.build_gate(short_url, hours)
    await client.send_message(
        user.id,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    return True
