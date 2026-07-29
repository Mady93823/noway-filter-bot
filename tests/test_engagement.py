"""Engagement (streaks / milestones) - the pure milestone mapping.

The streak/download counters themselves are Redis-backed and best-effort,
so they are exercised in integration; here we pin the pure line mapping,
which is what decides whether a given count says anything at all.
"""

from bot import engagement


def test_milestone_lines_fire_on_exact_counts():
    assert engagement.milestone_line(1) is not None
    assert engagement.milestone_line(10) is not None
    assert engagement.milestone_line(100) is not None
    assert engagement.milestone_line(1000) is not None


def test_non_milestone_counts_say_nothing():
    assert engagement.milestone_line(2) is None
    assert engagement.milestone_line(11) is None
    assert engagement.milestone_line(0) is None
    assert engagement.milestone_line(999) is None
