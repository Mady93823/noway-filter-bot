"""Tiny health endpoint for an uptime monitor and Docker healthchecks.

Deliberately stdlib asyncio rather than aiohttp/FastAPI: a liveness probe
should not drag a web framework into a bot process. It answers one route
and never blocks the event loop.

"Alive" here means the process can still reach what it needs - Postgres
and Redis - not merely that Python is running. A bot whose database is
gone is down, whatever the process table says.
"""

import asyncio
import json
import logging

from sqlalchemy import text

from shared.db.engine import get_session_factory
from shared.redis_client import get_redis

logger = logging.getLogger(__name__)

_REQUEST_LIMIT = 4096  # a probe's request line is tiny; cap the read


async def _probe() -> dict[str, bool]:
    checks = {"db": False, "redis": False}
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["db"] = True
    except Exception as exc:
        logger.warning("health: db unreachable: %s", exc)
    try:
        checks["redis"] = bool(await get_redis().ping())
    except Exception as exc:
        logger.warning("health: redis unreachable: %s", exc)
    return checks


def _response(status: str, body: dict) -> bytes:
    payload = json.dumps(body).encode()
    return (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + payload


async def start_health_server(port: int, service: str) -> asyncio.Server:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.read(_REQUEST_LIMIT)
            checks = await _probe()
            ok = all(checks.values())
            body = {"service": service, "ok": ok, **checks}
            # 503 so a monitor (or `docker compose ps`) sees the failure,
            # instead of a cheerful 200 with ok:false buried in the body.
            writer.write(
                _response("200 OK" if ok else "503 Service Unavailable", body)
            )
            await writer.drain()
        except Exception as exc:
            logger.warning("health request failed: %s", exc)
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    logger.info("health endpoint listening on :%d", port)
    return server
