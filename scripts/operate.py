"""Run the whole pipeline unattended.  T-057.

    python -m scripts.operate                     # run until stopped
    python -m scripts.operate --duration 3600     # or for a fixed spell
    python -m scripts.operate --once              # one pass of every task

WHY THIS EXISTS
---------------
Every piece of this system already worked in isolation and none of it ran on its
own.  The recorder had to be started by hand, settlements were ingested when
somebody remembered, and mark-outs were computed once.  The result was an
archive that could answer almost nothing: 128 settlements is not enough to
demonstrate any edge smaller than 17 percentage points, and mark-outs sat at
n=3.  The binding constraint on this project stopped being code a while ago and
became CALENDAR TIME, and calendar time only accrues if the thing runs while
nobody is watching.

Each task keeps its OWN schedule and its OWN database connection, and a failure
in one never stops the others -- a settlement poll that 429s must not take the
tape down with it, because the tape is the part that cannot be backfilled.

WHAT IT DOES NOT DO
-------------------
It never sends an order to a real venue.  `--mode` is fixed to shadow, and the
runner refuses live independently (I5).  The KILL file stops everything within
one poll, from any state.
"""

from __future__ import annotations

import argparse
import signal
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.config import load_settings
from core.db import Database
from core.models import RunMode, now_us

KILL_FILENAME = "KILL"


@dataclass
class Task:
    """One periodic job.  Owns its cadence, its errors, and its own connection."""

    name: str
    every_s: float
    run: Callable[[], str]
    last_run: float = 0.0
    runs: int = 0
    errors: int = 0
    last_error: str = ""

    def due(self, now: float) -> bool:
        return now - self.last_run >= self.every_s


@dataclass
class Supervisor:
    db_path: str
    run_dir: Path
    bankroll_cents: int
    tasks: list[Task] = field(default_factory=list)
    _stop: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def request_stop(self, *_: object) -> None:
        self._stop = True

    def killed(self) -> bool:
        return (self.run_dir / KILL_FILENAME).exists()

    # ------------------------------------------------------------------ tasks
    def task_universe(self) -> str:
        """Full universe sweep.  Slow and broad -- the denominator for everything."""
        from recorder.main import Recorder
        from venues.kalshi.client import PROD_BASE, KalshiClient

        with Database(self.db_path) as db:
            rec = Recorder(db, KalshiClient(base_url=PROD_BASE))
            n_ev, n_mk = rec.cycle()
            return f"{n_ev:,} events / {n_mk:,} markets"

    def task_settlements(self) -> str:
        """Outcomes.  Without these NOTHING can be scored -- no Brier, no edge CI,
        no e-process, no gate.  This is the single highest-value periodic task."""
        from recorder.settlements import SettlementRecorder
        from venues.kalshi.client import PROD_BASE, KalshiClient

        with Database(self.db_path) as db:
            rec = SettlementRecorder(db, KalshiClient(base_url=PROD_BASE),
                                     portfolio=False)
            polled, written = rec.cycle()
            return f"polled {polled}, wrote {written}"

    def task_cycle(self) -> str:
        """One shadow trading cycle: decide, risk-check, record, ingest fills."""
        from runner import Runner
        from strategy.s2_shortbasket import S2ShortBasket
        from strategy.s3_linked_rv import S3LinkedRV

        with Database(self.db_path) as db:
            runner = Runner(db=db, settings=load_settings(),
                            sleeves=[S2ShortBasket(), S3LinkedRV()],
                            mode=RunMode.SHADOW,
                            bankroll_cents=self.bankroll_cents,
                            run_dir=self.run_dir)
            runner.cycle()
            s = runner.stats
            return (f"quoted={s.quoted} placed={s.placed} denied={s.denied} "
                    f"fills={s.fills}")

    def task_marks(self) -> str:
        """Persist mark-outs as they mature.  They can only be measured while a
        quote near the horizon still exists, so late is the same as never."""
        from monitor.marks import MarkRecorder

        with Database(self.db_path) as db:
            return f"wrote {MarkRecorder(db).cycle()}"

    # ------------------------------------------------------------------- loop
    def build(self, *, cycle_s: float, settle_s: float,
              universe_s: float, marks_s: float) -> None:
        self.tasks = [
            Task("settlements", settle_s, self.task_settlements),
            Task("cycle", cycle_s, self.task_cycle),
            Task("marks", marks_s, self.task_marks),
            Task("universe", universe_s, self.task_universe),
        ]

    def step(self) -> None:
        now = time.monotonic()
        for t in self.tasks:
            if self._stop or self.killed() or not t.due(now):
                continue
            t.last_run = now
            started = time.monotonic()
            try:
                detail = t.run()
                t.runs += 1
                print(f"[{t.name:11s}] {detail}  ({time.monotonic()-started:.1f}s)",
                      flush=True)
            except Exception as exc:                     # noqa: BLE001
                t.errors += 1
                t.last_error = f"{type(exc).__name__}: {exc}"
                # Isolated on purpose: one task's outage must not stop the tape.
                print(f"[{t.name:11s}] ERROR {t.last_error}", flush=True)
                if t.errors <= 2:
                    traceback.print_exc()

    def report(self) -> str:
        return " | ".join(f"{t.name} {t.runs}ok/{t.errors}err" for t in self.tasks)

    def run(self, *, duration: float | None, once: bool) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        started = time.monotonic()
        print(f"[operate] started; KILL file = {self.run_dir / KILL_FILENAME}",
              flush=True)
        if once:
            for t in self.tasks:
                t.last_run = -1e9
            self.step()
        else:
            while not self._stop:
                if self.killed():
                    print("[operate] KILL present -- stopping", flush=True)
                    break
                if duration is not None and time.monotonic() - started >= duration:
                    break
                self.step()
                time.sleep(1.0)
        print(f"[operate] stopped. {self.report()}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="run the whole pipeline unattended")
    ap.add_argument("--db", default="data/pm.db")
    ap.add_argument("--run-dir", default=".")
    ap.add_argument("--bankroll", type=float, default=10_000.0, help="dollars")
    ap.add_argument("--cycle-interval", type=float, default=60.0)
    ap.add_argument("--settle-interval", type=float, default=900.0)
    ap.add_argument("--universe-interval", type=float, default=1800.0)
    ap.add_argument("--marks-interval", type=float, default=300.0)
    ap.add_argument("--duration", type=float, default=None, help="seconds")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)

    sup = Supervisor(db_path=args.db, run_dir=Path(args.run_dir),
                     bankroll_cents=int(args.bankroll * 100))
    sup.build(cycle_s=args.cycle_interval, settle_s=args.settle_interval,
              universe_s=args.universe_interval, marks_s=args.marks_interval)
    sup.run(duration=args.duration, once=args.once)
    with Database(args.db) as db:
        print(f"[operate] db: {db.counts()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
