"""The /stats re-parse block: the numbers an admin decides on.

This is the part that was missing when a real run "felt stuck" and got
killed, so the tests are about whether the display can be trusted: a
percentage that matches the counters, an ETA that only appears once
there is something to base it on, and no half-finished state rendering
as a finished one.
"""

import time

from bot.handlers.admin import _duration, _reparse_lines
from shared import reparse_state


def _state(**overrides) -> dict:
    """A read() result with everything defaulted, like the real one."""
    state = {
        "status": reparse_state.IDLE,
        "phase": "",
        "dry_run": False,
        "cancel": False,
        "error": "",
        "total": 0,
        "done": 0,
        "files": 0,
        "moved": 0,
        "renamed": 0,
        "orphans": 0,
        "titles_before": 0,
        "titles_after": 0,
        "requested_at": 0.0,
        "started_at": 0.0,
        "updated_at": 0.0,
        "finished_at": 0.0,
    }
    state.update(overrides)
    return state


def test_never_run_adds_nothing_to_stats():
    # /stats is read constantly; a job nobody ever started is not news.
    assert _reparse_lines(_state()) == []


def test_queued_says_so_without_inventing_a_percentage():
    text = "\n".join(_reparse_lines(_state(status=reparse_state.REQUESTED)))
    assert "Queued" in text
    assert "%" not in text


def test_running_percentage_follows_the_counters():
    text = "\n".join(
        _reparse_lines(
            _state(
                status=reparse_state.RUNNING,
                phase="files",
                done=5000,
                total=10000,
                started_at=time.time() - 60,
            )
        )
    )
    assert "50.0%" in text
    assert "5,000</b> / 10,000" in text


def test_progress_bar_fills_with_progress():
    def bar(done: int) -> str:
        lines = _reparse_lines(
            _state(
                status=reparse_state.RUNNING,
                phase="files",
                done=done,
                total=100,
                started_at=time.time() - 10,
            )
        )
        return lines[2]

    assert bar(0).count("█") == 0
    assert bar(50).count("█") == 5
    assert bar(100).count("█") == 10


def test_zero_total_does_not_divide_by_zero():
    # An empty index is a legitimate state, not a crash in /stats.
    text = "\n".join(
        _reparse_lines(
            _state(status=reparse_state.RUNNING, phase="files", total=0, done=0)
        )
    )
    assert "0.0%" in text


def test_eta_waits_until_there_is_evidence_for_one():
    """No files done yet means no rate, and a made-up ETA is worse than none."""
    text = "\n".join(
        _reparse_lines(
            _state(
                status=reparse_state.RUNNING,
                phase="files",
                done=0,
                total=10000,
                started_at=time.time() - 30,
            )
        )
    )
    assert "left" not in text


def test_eta_appears_once_files_are_moving():
    text = "\n".join(
        _reparse_lines(
            _state(
                status=reparse_state.RUNNING,
                phase="files",
                done=100,
                total=200,
                started_at=time.time() - 100,
            )
        )
    )
    assert "left" in text


def test_a_pending_cancel_is_visible_while_it_drains():
    text = "\n".join(
        _reparse_lines(
            _state(
                status=reparse_state.RUNNING,
                phase="files",
                done=10,
                total=100,
                cancel=True,
                started_at=time.time() - 5,
            )
        )
    )
    assert "stopping" in text


def test_dry_run_is_labelled_so_results_are_not_believed():
    text = "\n".join(
        _reparse_lines(
            _state(status=reparse_state.RUNNING, phase="files", dry_run=True)
        )
    )
    assert "dry run" in text


def test_finished_reports_the_title_delta():
    text = "\n".join(
        _reparse_lines(
            _state(
                status=reparse_state.DONE,
                files=184320,
                moved=431,
                renamed=17,
                orphans=52,
                titles_before=20114,
                titles_after=20062,
                finished_at=time.time() - 120,
            )
        )
    )
    assert "Finished" in text
    assert "20,114 → <b>20,062</b>" in text
    assert "52 empty removed" in text


def test_a_cancelled_run_never_reads_as_a_finished_one():
    text = "\n".join(
        _reparse_lines(
            _state(status=reparse_state.CANCELLED, files=900, finished_at=time.time())
        )
    )
    assert "Stopped by admin" in text
    assert "Finished" not in text


def test_failure_shows_the_reason_html_escaped():
    text = "\n".join(
        _reparse_lines(
            _state(
                status=reparse_state.FAILED,
                error="ValueError: bad <tag> in name",
                finished_at=time.time(),
            )
        )
    )
    assert "Failed" in text
    # A raw "<" here would break the whole /stats message, not just this line.
    assert "&lt;tag&gt;" in text
    assert "<tag>" not in text


def test_duration_reads_naturally_at_each_scale():
    assert _duration(45) == "45s"
    assert _duration(90) == "1m 30s"
    assert _duration(3700) == "1h 1m"
    # Clocks can go backwards between two processes; never print "-3s".
    assert _duration(-5) == "0s"
