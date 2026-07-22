"""Access clock: duration parsing, remaining-time wording, token masking.

Only the pure functions live here. Granting, redeeming, and the gate
itself talk to Postgres and Redis, so they are covered by the live smoke
script instead of by mocks that would only assert our own assumptions.
"""

from datetime import datetime, timedelta, timezone

from bot.access import format_remaining, parse_duration
from shared.settings_store import mask_token


def test_every_unit_parses():
    assert parse_duration("6h") == timedelta(hours=6)
    assert parse_duration("30d") == timedelta(days=30)
    assert parse_duration("2w") == timedelta(weeks=2)
    # Month and year are deliberately approximate - 30 and 365 days.
    assert parse_duration("1m") == timedelta(days=30)
    assert parse_duration("1y") == timedelta(days=365)


def test_parsing_is_forgiving_about_case_and_spacing():
    assert parse_duration("12H") == timedelta(hours=12)
    assert parse_duration(" 3 d ") == timedelta(days=3)


def test_nonsense_durations_are_rejected():
    # Rejected rather than defaulted: silently granting a year because
    # an admin typed "1x" is the kind of mistake nobody notices.
    for bad in ("", "x", "1x", "d5", "-3d", "0h", "99"):
        assert parse_duration(bad) is None, bad


def test_remaining_reads_naturally():
    now = datetime.now(timezone.utc)
    assert format_remaining(now + timedelta(days=2, hours=3)) == "2 days, 3 hours"
    assert format_remaining(now + timedelta(hours=1, minutes=30)) == (
        "1 hour, 30 minutes"
    )
    # Minutes are dropped once there are days left - nobody reads
    # "9 days, 4 hours, 12 minutes".
    assert format_remaining(now + timedelta(days=9, hours=4, minutes=12)) == (
        "9 days, 4 hours"
    )


def test_expired_and_unset_both_read_as_no_access():
    now = datetime.now(timezone.utc)
    assert format_remaining(None) is None
    assert format_remaining(now - timedelta(seconds=1)) is None


def test_token_is_never_shown_in_full():
    # Deliberately not a real-looking key: a 40-hex literal in a public
    # repo gets flagged by secret scanners and copied by readers.
    token = "EXAMPLE-NOT-A-REAL-TOKEN-0000-abcd"
    masked = mask_token(token)
    assert masked.endswith(token[-4:])
    assert token not in masked
    assert len(masked) < len(token)
    # A short value must not leak either.
    assert "abc" not in mask_token("abc")
