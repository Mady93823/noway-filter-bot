"""AroLinks-style URL shortener client.

This is the only outbound HTTP call in the project, and it is worth
being explicit about why it does not violate golden rule 1. That rule
bans external *metadata* lookups (TMDB/IMDB/OMDb) from the indexing and
search paths, because they make the core of the bot depend on someone
else's uptime. This call is neither: it resolves no title data, and it
runs only when a gated user taps a file. Search and indexing never
touch it.

API shape (per the provider's docs):

    GET {base}?api={token}&url={encoded}&format=text   -> the short URL
    GET {base}?api={token}&url={encoded}               -> JSON

TEXT is used because the documented failure mode is "if an error occurs,
it will not output anything" - an empty body is unambiguous, whereas the
JSON path needs a status field parsed before it means anything. Both are
handled anyway, since providers of this family disagree about which they
actually return.

Failure is deliberately loud rather than permissive: if the shortener is
down the user is told to retry and the error is logged. The tempting
alternative - handing out the unshortened link - would silently switch
the gate off for everyone the moment the provider had an outage, and
could be triggered on purpose.
"""

import json
import logging
from urllib.parse import quote

import httpx

from shared.settings_store import shortener_config

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


class ShortenerError(RuntimeError):
    """The shortener could not produce a link. Never means "allow through"."""


async def shorten(target_url: str, alias: str | None = None) -> str:
    """Shorten target_url. Raises ShortenerError on any failure."""
    token, base = await shortener_config()
    if not token:
        raise ShortenerError("no shortener API token configured")

    params = f"api={quote(token, safe='')}&url={quote(target_url, safe='')}&format=text"
    if alias:
        params += f"&alias={quote(alias, safe='')}"
    url = f"{base}?{params}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ShortenerError(f"shortener unreachable: {exc}") from exc

    if response.status_code != 200:
        raise ShortenerError(f"shortener returned HTTP {response.status_code}")

    body = response.text.strip()
    if not body:
        # The documented error signal for format=text.
        raise ShortenerError("shortener returned an empty response")

    # Some providers ignore format=text and answer JSON regardless.
    if body.startswith("{"):
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise ShortenerError("shortener returned unparseable JSON") from exc
        if payload.get("status") == "success" and payload.get("shortenedUrl"):
            return str(payload["shortenedUrl"])
        raise ShortenerError(str(payload.get("message") or "shortener error"))

    if not body.startswith("http"):
        raise ShortenerError(f"shortener returned a non-URL: {body[:80]}")
    return body
