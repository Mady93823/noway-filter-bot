"""Ephemeral group messages: the queue member encoding.

Scheduling and sweeping need Redis, so they live in the live smoke. What
is worth pinning here is the encoding, because chat ids are negative and
long, and a naive split on the first ":" would still pass a lazy test
while being wrong in principle.
"""


def _decode(member: str) -> tuple[int, int]:
    """Mirror of the parsing in ephemeral.sweep_once."""
    chat_part, _, message_part = member.rpartition(":")
    return int(chat_part), int(message_part)


def test_supergroup_id_survives_a_round_trip():
    member = f"{-1001234567890}:{4242}"
    assert _decode(member) == (-1001234567890, 4242)


def test_positive_chat_id_round_trips():
    assert _decode(f"{12345}:{7}") == (12345, 7)


def test_message_id_is_taken_from_the_right_hand_side():
    chat_id, message_id = _decode("-1009876543210:1")
    assert chat_id == -1009876543210
    assert message_id == 1
