"""T-057: the supervisor keeps the pipeline running unattended.

The binding constraint on this project stopped being code and became CALENDAR
TIME -- 134 settlements cannot demonstrate an edge smaller than 17 percentage
points, and mark-outs sat at n=3 for a day.  Calendar time only accrues if the
thing runs while nobody is watching, so these tests are about SURVIVING, not
about doing anything clever.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from scripts.operate import Supervisor, Task


@pytest.fixture()
def run_dir():
    d = tempfile.mkdtemp(prefix="pm-operate-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def sup(run_dir, **kw) -> Supervisor:
    return Supervisor(db_path=":memory:", run_dir=run_dir,
                      bankroll_cents=1_000_000, **kw)


# --------------------------------------------------------------------------- #
# Failure isolation -- the property the whole design exists for
# --------------------------------------------------------------------------- #
def test_one_failing_task_does_not_stop_the_others(run_dir):
    """A settlement poll that 429s must NOT take the tape down with it.

    The tape is the only part of this archive that cannot be backfilled: a
    quote not recorded at 14:03:22 is gone forever, while a settlement missed
    now is still there in an hour.  So the cheap task failing must never cost
    us the expensive one.
    """
    ran: list[str] = []

    def boom() -> str:
        raise RuntimeError("venue said no")

    def fine() -> str:
        ran.append("ok")
        return "ok"

    s = sup(run_dir)
    s.tasks = [Task("bad", 0.0, boom), Task("good", 0.0, fine)]
    s.step()

    assert s.tasks[0].errors == 1
    assert "venue said no" in s.tasks[0].last_error
    assert ran == ["ok"], "a healthy task was skipped because another failed"


def test_a_task_that_fails_forever_never_blocks_the_loop(run_dir):
    s = sup(run_dir)
    s.tasks = [Task("bad", 0.0, lambda: (_ for _ in ()).throw(ValueError("x")))]
    for _ in range(5):
        s.step()
    assert s.tasks[0].errors == 5 and s.tasks[0].runs == 0


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
def test_each_task_keeps_its_own_cadence(run_dir):
    """Settlements every 15 min and a trading cycle every minute must not be
    forced onto one clock -- polling settlements 15x more often than needed is
    rate limit spent on nothing."""
    calls = {"fast": 0, "slow": 0}
    s = sup(run_dir)
    s.tasks = [
        Task("fast", 0.0, lambda: (calls.__setitem__("fast", calls["fast"] + 1), "")[1]),
        Task("slow", 9_999.0, lambda: (calls.__setitem__("slow", calls["slow"] + 1), "")[1]),
    ]
    for _ in range(4):
        s.step()
    assert calls["fast"] == 4
    assert calls["slow"] == 1, "a long-cadence task ran more than once"


def test_a_task_that_has_never_run_is_due_against_any_clock():
    """The regression that made the suite depend on system uptime.

    `time.monotonic()` has an arbitrary origin; on Windows it is seconds since
    boot. With `last_run` defaulting to 0.0, `due(now)` asked whether the
    MACHINE had been up longer than the cadence, so a supervisor started on a
    fresh boot skipped its 1800s universe sweep for the first half hour, and
    this file's cadence test passed or failed depending on the host's uptime.
    """
    t = Task("never", 1800.0, lambda: "")
    for clock in (0.0, 1.0, 300.0, 1799.0, 9_509.0, 1e9):
        assert t.due(clock), f"a task that has never run must be due at {clock}"


def test_a_task_is_due_only_after_its_interval_elapses():
    t = Task("x", 10.0, lambda: "")
    t.last_run = 100.0
    assert not t.due(105.0)
    assert t.due(110.0)


# --------------------------------------------------------------------------- #
# I9 -- the kill file stops everything
# --------------------------------------------------------------------------- #
def test_a_kill_file_stops_every_task_immediately(run_dir):
    """I9 is a promise about ANY state, and 'running unattended overnight' is
    the state where an operator most needs it to hold."""
    ran: list[str] = []
    s = sup(run_dir)
    s.tasks = [Task("t", 0.0, lambda: (ran.append("x"), "")[1])]

    s.step()
    assert ran == ["x"]

    (run_dir / "KILL").write_text("stop", encoding="utf-8")
    s.step()
    s.step()
    assert ran == ["x"], "a task ran while the kill file was present"
    assert s.killed()


def test_the_kill_file_is_read_from_the_configured_run_dir(run_dir):
    """A KILL in the wrong directory is not a kill switch.  The supervisor and
    the executor must agree on where it lives."""
    s = sup(run_dir)
    assert not s.killed()
    (Path(run_dir) / "KILL").write_text("", encoding="utf-8")
    assert s.killed()


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_build_registers_every_pipeline_stage(run_dir):
    s = sup(run_dir)
    s.build(cycle_s=60, settle_s=900, universe_s=1800, marks_s=300)
    assert {t.name for t in s.tasks} == {"settlements", "cycle", "marks", "universe"}


def test_settlements_run_more_often_than_the_universe_sweep(run_dir):
    """Settlements unblock every scored KPI and cost one paged request; the
    universe sweep is 26 seconds and 109,000 markets.  Their cadences should
    reflect that, not be equal."""
    s = sup(run_dir)
    s.build(cycle_s=60, settle_s=900, universe_s=1800, marks_s=300)
    by = {t.name: t.every_s for t in s.tasks}
    assert by["cycle"] < by["marks"] < by["settlements"] < by["universe"]


def test_the_report_names_every_task_and_its_error_count(run_dir):
    s = sup(run_dir)
    s.tasks = [Task("a", 0.0, lambda: ""), Task("b", 0.0, lambda: "")]
    s.step()
    r = s.report()
    assert "a 1ok/0err" in r and "b 1ok/0err" in r
