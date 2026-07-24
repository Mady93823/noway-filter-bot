"""The delivered-file lifetime: warning text, expiry text, queue encoding.

The scheduling and sweeping themselves need a live Redis and a live
client, so they belong to the smoke run. What is worth pinning here is
everything that is pure: the two strings a user actually reads, and the
three-field member encoding - chat ids are negative in groups and BOTH
message ids sit on the right, so a split from the left would still pass
a lazy test while being wrong in principle.
"""

import asyncio

from bot import ui
from shared.db.repos.titles import merge_metadata


def _decode(member: str) -> tuple[int, int, int]:
    """Mirror of the parsing in ephemeral._sweep_deliveries."""
    head, _, notice_part = member.rpartition(":")
    chat_part, _, file_part = head.rpartition(":")
    return int(chat_part), int(file_part), int(notice_part)


def test_delivery_member_round_trips_a_negative_chat_id():
    assert _decode(f"{-1001234567890}:{4242}:{4243}") == (
        -1001234567890,
        4242,
        4243,
    )


def test_delivery_member_round_trips_a_pm_chat_id():
    assert _decode("12345:7:8") == (12345, 7, 8)


def test_missing_notice_encodes_as_zero():
    # The warning failed to send; the file must still expire.
    assert _decode("12345:7:0") == (12345, 7, 0)


def test_warning_states_the_real_minutes_and_the_way_out():
    text = ui.delivery_warning_text(600)
    assert "10 MINUTE" in text
    assert "FORWARD IT NOW" in text
    # Alarming without the escape hatch would just be alarming.
    assert "Search again" in text


def test_warning_is_singular_for_one_minute():
    text = ui.delivery_warning_text(60)
    assert "1 MINUTE</b>" in text
    assert "MINUTES" not in text


def test_expired_text_says_what_happened_and_what_to_do():
    text = ui.delivery_expired_text()
    assert "deleted" in text
    assert "Search again" in text


class _Title:
    """Stands in for the ORM row: merge_metadata only touches these two."""

    def __init__(self, canonical: str, display: str):
        self.canonical_title = canonical
        self.display_title = display
        self.languages: list[str] = []


def _merge(title: _Title, candidate: str) -> None:
    # merge_metadata never awaits the session, so None is safe here.
    asyncio.run(
        merge_metadata(None, title, languages=(), display_candidate=candidate)
    )


def test_prepended_debris_cannot_rename_a_title():
    """The "Ep 10 Bang Bang" failure: longer is not the same as better."""
    title = _Title("bang bang", "Bang Bang")
    _merge(title, "Ep 10 Bang Bang")
    assert title.display_title == "Bang Bang"


def test_a_longer_spelling_of_the_same_name_still_wins():
    """The rule the guard must not break: truncated names get completed."""
    title = _Title("swat", "Swat")
    _merge(title, "Swati")
    assert title.display_title == "Swati"


def test_a_shorter_candidate_never_wins():
    title = _Title("bang bang", "Bang Bang")
    _merge(title, "Bang")
    assert title.display_title == "Bang Bang"
