"""Text search - the bot's main job.

Any plain text message (PM or group) is a query. Groups stay quiet on
no-hit searches so the bot never spams a busy chat; PMs always get an
answer.

Never a query: anything the bot itself said. The worker runs a SECOND
Pyrogram session on the same token, so its admin progress DMs
("Indexing ... 94/94") arrive at this session as ordinary incoming
messages - without a self/bot guard the bot searches its own status
reports and answers "Nothing found for. Indexing ...". Other bots are
excluded for the same reason.
"""

import logging

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import Message

from bot import guards, ui
from bot.ephemeral import expire_in_group
from shared import logchannel
from shared.config import get_settings
from shared.db.engine import get_session_factory
from shared.demand import record_miss
from shared.logchannel import log_event
from shared.search import cache
from shared.search.nlu import Intent, clean_query, detect_intent
from shared.search.service import SearchPage, refinement_of, search, suggest

logger = logging.getLogger(__name__)

async def _is_plain_text(_, __, message: Message) -> bool:
    # async on purpose: a sync predicate would make Pyrogram hand the
    # whole filter chain to a thread executor.
    return bool(message.text) and not message.text.startswith("/")


_plain_text = filters.create(_is_plain_text)

# Module-level so it can be exercised directly in tests. The ban guard is
# deliberately NOT part of it: this stays a pure predicate over the
# message, and guards.not_banned (which reads Redis) is combined in at
# registration instead.
QUERY_FILTER = (
    (filters.private | filters.group)
    & _plain_text
    & ~filters.forwarded
    & ~filters.via_bot
    # me: our own messages, echoed back from the worker's session.
    # bot: any other bot's chatter in a group.
    & ~filters.me
    & ~filters.bot
)


async def _resolve_query(session, message: Message, raw: str) -> tuple[SearchPage, str]:
    """Run the search ladder for one message. Returns (page, query used).

    Three attempts, most literal first:

    1. A bare modifier ("1080p", "tamil") refines whatever this user
       searched last - it finds nothing on its own, so there is nothing
       to lose by reading it as a refinement.
    2. The text with conversational filler removed ("bro send swati plz"
       -> "swati"), because filler drags the trigram score down.
    3. The untouched text, but only when step 2 actually changed
       something. This is what makes filler-stripping safe: a real title
       built from filler words ("Scary Movie") still gets searched
       verbatim before anyone is told it does not exist.
    """
    scope = cache.conversation_scope(message.chat.id, message.from_user.id)

    modifier = refinement_of(raw)
    if modifier is not None:
        previous = await cache.load_last_query(scope)
        if previous:
            refined = f"{previous} {modifier}"
            page = await search(session, refined)
            if page.results:
                return page, refined
        # Nothing to narrow: fall through so a bare "1080p" takes the
        # ordinary no-results path instead of silently doing nothing.

    cleaned = clean_query(raw)
    page = await search(session, cleaned.text)
    if not page.results and cleaned.changed:
        return await search(session, raw), raw
    return page, cleaned.text


def register_search_handlers(app: Client) -> None:
    @app.on_message(QUERY_FILTER & guards.not_banned)
    async def _on_query(client: Client, message: Message) -> None:
        query = (message.text or "").strip()
        if len(query) < 2:
            return

        # Classify once: the same verdict decides whether a group hears
        # back at all, and how a PM miss is answered.
        intent = detect_intent(query)
        is_group = message.chat.type != ChatType.PRIVATE

        session_factory = get_session_factory()
        async with session_factory() as session:
            page, used = await _resolve_query(session, message, query)
            # Suggestions cost a query, so only pay for them on a miss
            # someone is waiting on: every PM, but a group only when the
            # message is a real search attempt rather than chit-chat.
            suggestions = (
                await suggest(session, query)
                if not page.results and (not is_group or intent is Intent.SEARCH)
                else ()
            )

        if page.results:
            # Remembered only on success: refining a search that found
            # nothing would just narrow an empty set.
            await cache.store_last_query(
                cache.conversation_scope(message.chat.id, message.from_user.id), used
            )
            text, keyboard = ui.build_results(page, get_settings().search_page_size)
            sent = await message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard, quote=True
            )
            # In a group the card is temporary; the file itself is
            # delivered in PM and stays there.
            await expire_in_group(message, sent)
            return

        # Demand is counted for group misses too: a title people keep
        # asking for in a busy group is exactly the signal worth having.
        if intent is Intent.SEARCH:
            count = await record_miss(
                used, message.from_user.id, get_settings().missing_threshold
            )
            if count is not None:
                await log_event(
                    client,
                    logchannel.MISSING,
                    "Title repeatedly requested but not indexed",
                    {
                        "Query": used,
                        "Distinct users (24h)": count,
                        "Last asked by": f"{message.from_user.mention} "
                        f"({message.from_user.id})",
                        "Chat": message.chat.title or "PM",
                    },
                )

        if is_group:
            # A group only ever hears back about a genuine search, and the
            # reply self-deletes in a few minutes - greetings and chit-chat
            # stay silent so a busy chat is never filled with bot noise.
            if intent is not Intent.SEARCH:
                return
            if suggestions:
                text, keyboard = ui.build_suggestions(query, suggestions)
                sent = await message.reply_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=keyboard, quote=True
                )
            else:
                sent = await message.reply_text(
                    ui.no_results_text(query), parse_mode=ParseMode.HTML, quote=True
                )
            await expire_in_group(message, sent)
            return

        # PM: an answer always goes out. Suggestions first; otherwise read
        # the message as conversation now that the index is confirmed empty.
        if suggestions:
            text, keyboard = ui.build_suggestions(query, suggestions)
            await message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard, quote=True
            )
            return

        if intent is Intent.GREETING:
            reply = ui.greeting_text(message.from_user.mention)
        elif intent is Intent.THANKS:
            reply = ui.thanks_text()
        elif intent is Intent.HELP:
            reply = ui.chat_help_text()
        else:
            reply = ui.no_results_text(query)
        await message.reply_text(reply, parse_mode=ParseMode.HTML, quote=True)
