"""Rate governor: the parts provable without a live Redis.

The token-bucket timing itself is exercised by scripts/smoke_governor.py
against real Redis - Lua execution can't be faithfully faked. What lives
here is the wiring that would break silently: that wrapping a client
routes every send through acquire(), that the chat id is pulled from
either positional or keyword form, and that a Redis outage fails OPEN
rather than wedging a send.
"""

import asyncio
import time

from shared.ratelimit import RateGovernor, install_governor


class _RecordingGovernor:
    """Stands in for the real governor: records what acquire saw."""

    def __init__(self):
        self.calls = []

    async def acquire(self, chat_id=None):
        self.calls.append(chat_id)


class _FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **k):
        self.sent.append(("msg", chat_id, text))
        return "ok"

    async def send_cached_media(self, chat_id, file_id, **k):
        self.sent.append(("media", chat_id, file_id))

    async def edit_message_text(self, chat_id, mid, text, **k):
        self.sent.append(("edit", chat_id, mid))

    async def delete_messages(self, chat_id, mid, **k):
        self.sent.append(("del", chat_id, mid))


def test_install_wraps_and_still_delivers():
    client = _FakeClient()
    gov = _RecordingGovernor()
    original = client.send_message
    install_governor(client, gov)
    assert client.send_message is not original, "method should be wrapped"

    result = asyncio.run(client.send_message(123, "hi"))
    assert result == "ok", "wrapper must return the underlying result"
    assert client.sent == [("msg", 123, "hi")], "underlying send must still run"
    assert gov.calls == [123], "acquire must see the chat id"


def test_chat_id_read_from_keyword_too():
    # Pyrogram's Message.reply_* delegate with chat_id as a keyword.
    client = _FakeClient()
    gov = _RecordingGovernor()
    install_governor(client, gov)
    asyncio.run(client.send_message(chat_id=-100987, text="x"))
    assert gov.calls == [-100987]


def test_every_send_surface_is_governed():
    client = _FakeClient()
    gov = _RecordingGovernor()
    install_governor(client, gov)
    asyncio.run(client.send_cached_media(1, "fid"))
    asyncio.run(client.edit_message_text(2, 9, "t"))
    asyncio.run(client.delete_messages(3, 9))
    assert gov.calls == [1, 2, 3], "media, edit and delete all pass through"


class _Boom:
    """A Redis whose script always raises - simulates an outage."""

    def register_script(self, _lua):
        async def _raise(**_k):
            raise RuntimeError("redis down")

        return _raise


def test_acquire_fails_open_on_redis_error():
    gov = RateGovernor(_Boom(), global_rate=1, global_capacity=1)
    start = time.monotonic()
    asyncio.run(gov.acquire(5))
    # Returns promptly instead of hanging or raising: a Redis blip must
    # never stop the bot sending.
    assert time.monotonic() - start < 0.2


class _Script:
    """A script that returns a fixed wait in ms for the first N calls."""

    def __init__(self, waits):
        self._waits = list(waits)

    async def __call__(self, **_k):
        return self._waits.pop(0) if self._waits else 0


class _FakeRedis:
    def __init__(self, script):
        self._script = script

    def register_script(self, _lua):
        return self._script


def test_allowed_immediately_returns():
    gov = RateGovernor(_FakeRedis(_Script([0])))
    start = time.monotonic()
    asyncio.run(gov.acquire(7))
    assert time.monotonic() - start < 0.1


def test_waits_then_proceeds_and_never_loops_forever():
    # Script keeps asking for another 20ms; max_wait caps the total so a
    # permanently-starved bucket can't block a send indefinitely.
    gov = RateGovernor(_FakeRedis(_Script([20] * 1000)), max_wait=0.1)
    start = time.monotonic()
    asyncio.run(gov.acquire(7))
    elapsed = time.monotonic() - start
    # It did wait (not an instant return) but gave up near max_wait rather
    # than looping forever. The cap is checked before the final sleep, so
    # actual elapsed lands just under max_wait, never far past it.
    assert 0.03 <= elapsed < 0.4, f"{elapsed*1000:.0f}ms"
