"""Group keyword filters: a keyword in the chat gets a canned reply.

Ordering matters. This responder is registered BEFORE the search
catch-all and both match plain group text, so on no match it calls
message.continue_propagation() to hand the message on - without that,
adding one filter to a group would silently kill search there.

Matching happens in the bot, not in SQL: the group's whole keyword set
is cached in Redis (golden rule 4 - never a module-level dict), so a
group message costs one Redis GET rather than a query. Writes delete the
key, so a new filter is live immediately; the TTL is only a backstop.
"""

import json
import logging
import re

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.types import Message

from bot.ephemeral import expire_in_group
from bot.handlers.groups import touch_group
from shared.config import get_settings
from shared.db.engine import get_session_factory
from shared.db.repos import filters as filters_repo
from shared.db.repos import groups as groups_repo
from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "gfilters:"
_PUNCTUATION_RE = re.compile(r"[^a-z0-9]+")
MAX_KEYWORD_LENGTH = 64


def normalize(text: str) -> str:
    """Lowercase, punctuation to spaces, collapsed. Applied to both the
    stored keyword and the incoming message so they compare the same."""
    return _PUNCTUATION_RE.sub(" ", text.lower()).strip()


def match_keyword(text: str, keywords: list[str]) -> str | None:
    """First keyword present as a whole word (or phrase). None if no hit.

    Padding both sides with spaces makes 'her' miss 'here' while still
    letting multi-word keywords match, with no regex per keyword.
    """
    haystack = f" {normalize(text)} "
    for keyword in keywords:
        if f" {keyword} " in haystack:
            return keyword
    return None


async def _load_filters(group_id: int) -> dict[str, str]:
    redis = get_redis()
    key = f"{_CACHE_PREFIX}{group_id}"
    cached = await redis.get(key)
    if cached is not None:
        return json.loads(cached)

    session_factory = get_session_factory()
    async with session_factory() as session:
        mapping = await filters_repo.filters_for_group(session, group_id)
    # Cache the empty case too, or every message in a filterless group
    # (most of them) becomes a database query.
    await redis.set(key, json.dumps(mapping), ex=get_settings().filter_cache_ttl)
    return mapping


async def _invalidate(group_id: int) -> None:
    await get_redis().delete(f"{_CACHE_PREFIX}{group_id}")


async def _is_group_admin(client: Client, message: Message) -> bool:
    if message.from_user is None:
        return False
    if message.from_user.id in get_settings().admin_ids:
        return True
    member = await client.get_chat_member(message.chat.id, message.from_user.id)
    return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)


def _split_command(message: Message) -> tuple[str, str]:
    """('keyword', 'reply body') from '/filter keyword reply body…'.

    A quoted first argument keeps multi-word keywords possible:
        /filter "new movies" ask in @somechannel
    """
    body = (message.text or "").split(maxsplit=1)
    if len(body) < 2:
        return "", ""
    rest = body[1].strip()
    if rest.startswith('"') and '"' in rest[1:]:
        end = rest.index('"', 1)
        return normalize(rest[1:end]), rest[end + 1 :].strip()
    parts = rest.split(maxsplit=1)
    return normalize(parts[0]), (parts[1].strip() if len(parts) > 1 else "")


async def _reply(message: Message, *args, **kwargs) -> None:
    """Reply, and let it self-delete - every handler here is group-side.

    Wrapping rather than calling reply_text directly means no future
    handler in this module can quietly leave a permanent message behind
    in someone's group.
    """
    sent = await message.reply_text(*args, **kwargs)
    await expire_in_group(message, sent)


def register_group_filter_handlers(app: Client) -> None:
    group_only = filters.group

    @app.on_message(group_only & filters.command("filter"))
    async def _on_add(client: Client, message: Message) -> None:
        if not await _is_group_admin(client, message):
            await _reply(message,"🛑 Group admins only.")
            return

        keyword, reply = _split_command(message)
        if keyword and not reply and message.reply_to_message:
            reply = message.reply_to_message.text or ""
        if not keyword or not reply:
            await _reply(message,
                "Usage: <code>/filter &lt;keyword&gt; &lt;reply&gt;</code>\n"
                'Multi-word keyword: <code>/filter "new movies" …</code>\n'
                "Or reply to a message with <code>/filter &lt;keyword&gt;</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        if len(keyword) > MAX_KEYWORD_LENGTH:
            await _reply(message,
                f"🛑 Keyword too long (max {MAX_KEYWORD_LENGTH} characters)."
            )
            return

        existing = await _load_filters(message.chat.id)
        limit = get_settings().max_filters_per_group
        if keyword not in existing and len(existing) >= limit:
            await _reply(message,
                f"🛑 This group is at the {limit}-filter limit. "
                "Remove one with <code>/stop &lt;keyword&gt;</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            # filters.group_id references groups(id), so the group has to
            # be on record before its first filter.
            await groups_repo.upsert_group(session, message.chat.id, message.chat.title)
            await filters_repo.add_filter(session, message.chat.id, keyword, reply)
        await _invalidate(message.chat.id)

        verb = "Updated" if keyword in existing else "Saved"
        await _reply(message,
            f"✅ {verb} filter <code>{keyword}</code>.", parse_mode=ParseMode.HTML
        )

    @app.on_message(group_only & filters.command("filters"))
    async def _on_list(client: Client, message: Message) -> None:
        mapping = await _load_filters(message.chat.id)
        if not mapping:
            await _reply(message,
                "📭 No filters here yet. Add one with "
                "<code>/filter &lt;keyword&gt; &lt;reply&gt;</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        lines = [f"🛠 <b>{len(mapping)} filter(s)</b>", ""]
        lines += [f"▫️ <code>{keyword}</code>" for keyword in sorted(mapping)]
        await _reply(message,"\n".join(lines), parse_mode=ParseMode.HTML)

    @app.on_message(group_only & filters.command("stop"))
    async def _on_stop(client: Client, message: Message) -> None:
        if not await _is_group_admin(client, message):
            await _reply(message,"🛑 Group admins only.")
            return
        keyword, _ = _split_command(message)
        if not keyword:
            await _reply(message,
                "Usage: <code>/stop &lt;keyword&gt;</code>", parse_mode=ParseMode.HTML
            )
            return

        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            removed = await filters_repo.delete_filter(
                session, message.chat.id, keyword
            )
        await _invalidate(message.chat.id)
        await _reply(message,
            f"🗑 Removed <code>{keyword}</code>."
            if removed
            else f"🤷 No filter called <code>{keyword}</code>.",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(group_only & filters.command("stopall"))
    async def _on_stop_all(client: Client, message: Message) -> None:
        if not await _is_group_admin(client, message):
            await _reply(message,"🛑 Group admins only.")
            return
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            count = await filters_repo.delete_all_filters(session, message.chat.id)
        await _invalidate(message.chat.id)
        await _reply(message,f"🗑 Removed {count} filter(s).")

    @app.on_message(
        group_only
        & filters.text
        & ~filters.me
        & ~filters.bot
        & ~filters.forwarded
        & ~filters.via_bot
    )
    async def _on_group_text(client: Client, message: Message) -> None:
        await touch_group(message.chat)

        text = message.text or ""
        if text.startswith("/"):
            message.continue_propagation()

        mapping = await _load_filters(message.chat.id)
        keyword = match_keyword(text, list(mapping)) if mapping else None
        if keyword is None:
            # Not a filter - let the search catch-all have it.
            message.continue_propagation()

        await _reply(message,mapping[keyword], quote=True)
