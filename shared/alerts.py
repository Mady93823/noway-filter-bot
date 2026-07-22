"""Fatal-error alerting: tell an admin when something actually breaks.

Two rules, both from how these bots fail in practice:

1. Alerting must never be able to kill the thing it is reporting on.
   Every send is individually guarded; a FloodWait, or an admin who never
   opened the bot, cannot take the process down with it.
2. A crash loop must not become a DM flood. A dedupe key claimed in Redis
   with SET NX EX means the same failure alerts once per cooldown, no
   matter how often it repeats or how many instances hit it.
"""

import logging

from shared.config import get_settings
from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_DEDUPE_PREFIX = "alert:"


async def notify_admins(client, text: str, dedupe_key: str | None = None) -> None:
    """DM every admin and mirror it to the log channel.

    Never raises - the caller is already in trouble.

    The channel copy rides the same dedupe claim as the DMs, so a crash
    loop costs one channel post per cooldown rather than thousands, and
    an admin reading either place sees the same set of failures.
    """
    if dedupe_key is not None:
        try:
            claimed = await get_redis().set(
                f"{_DEDUPE_PREFIX}{dedupe_key}",
                "1",
                ex=get_settings().alert_cooldown,
                nx=True,
            )
            if not claimed:
                return
        except Exception:
            # Redis being down is itself worth knowing about - fall
            # through and send rather than swallowing the error we were
            # called about.
            logger.warning("alert dedupe unavailable; sending anyway")

    for admin_id in get_settings().admin_ids:
        try:
            await client.send_message(admin_id, text)
        except Exception as exc:
            logger.warning("alert to %s failed: %s", admin_id, exc)

    # Imported here rather than at module scope: logchannel reads
    # settings_store, which imports the DB models, and alerts is imported
    # by both entrypoints before the engine exists.
    from shared import logchannel

    await logchannel.log_event(
        client, logchannel.ERROR, "Alert", {"Detail": text}
    )
