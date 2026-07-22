"""The access gate: who may receive a file, and for how long.

One clock, two ways to wind it. Completing a shortlink sets
users.access_until to now + access_hours; an admin grant sets it to
now + 30d (or 1y, or 4h). Everything - delivery, /myplan, /stats -
reads that single field, so verification and premium can never disagree
about whether someone is allowed a file.

The verification round trip needs no web server, which is what keeps the
deferred streaming server out of this feature entirely:

    1. user taps a file while the gate is on
    2. bot mints a single-use token and builds
       https://t.me/<bot>?start=verify_<token>
    3. that Telegram URL is what gets shortened, so the ad gate leads
       back into the bot
    4. user completes it, Telegram opens the bot with the payload, the
       token is redeemed exactly once and the clock starts

Tokens live in Redis with a TTL (golden rule 4 - never a dict): they are
short-lived, single-use, and must be redeemable on whichever instance
the user happens to land on. Redemption deletes the key first and acts
on the result, so two taps of the same link cannot both grant time.
"""

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from shared.db.repos import users as users_repo
from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_TOKEN_PREFIX = "verify:"
# Long enough to survive the ad gate and a slow reader, short enough that
# an abandoned link cannot be redeemed hours later.
_TOKEN_TTL = 1800
_TOKEN_BYTES = 12

_DURATION_RE = re.compile(r"^(\d+)\s*([hdwmy])$", re.IGNORECASE)
# Months and years are approximated deliberately: calendar-exact
# arithmetic would need another dependency for a value nobody checks to
# the day.
_UNIT_SECONDS = {
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "m": 2592000,  # 30 days
    "y": 31536000,  # 365 days
}


def parse_duration(text: str) -> timedelta | None:
    """'4h' / '30d' / '2w' / '6m' / '1y' -> timedelta. None if unparseable."""
    match = _DURATION_RE.match(text.strip())
    if not match:
        return None
    amount = int(match.group(1))
    if amount <= 0:
        return None
    return timedelta(seconds=amount * _UNIT_SECONDS[match.group(2).lower()])


def format_remaining(expiry: datetime | None) -> str | None:
    """'2 days, 3 hours' left on the clock, or None when it has run out."""
    if expiry is None:
        return None
    remaining = expiry - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        return None

    days, rest = divmod(int(remaining.total_seconds()), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    # Minutes only matter when there is little enough left to care.
    if minutes and not days:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return ", ".join(parts) or "under a minute"


async def has_access(session, user_id: int) -> bool:
    expiry = await users_repo.get_access_until(session, user_id)
    return expiry is not None and expiry > datetime.now(timezone.utc)


async def mint_token(user_id: int) -> str:
    """A single-use verification token bound to one user."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    await get_redis().set(f"{_TOKEN_PREFIX}{token}", str(user_id), ex=_TOKEN_TTL)
    return token


async def redeem_token(token: str, user_id: int) -> bool:
    """Burn a token for this user. False if unknown, expired, or not theirs.

    Deletes first and decides afterwards: two taps racing on the same
    link must not both grant time, and only one of them can win the DEL.
    """
    redis = get_redis()
    key = f"{_TOKEN_PREFIX}{token}"
    owner = await redis.get(key)
    if owner is None:
        return False
    if not await redis.delete(key):
        return False  # another request burned it first
    if owner != str(user_id):
        # Someone forwarded their link. The token is spent either way -
        # it was already deleted - but it grants nothing here.
        logger.info("verify token for %s redeemed by %s; refused", owner, user_id)
        return False
    return True
