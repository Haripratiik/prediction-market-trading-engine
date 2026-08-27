"""Persist mark-outs as they mature.  T-056.  PLAN.md 12 KPI 3.

A mark-out is the signed fair-price move after one of our fills.  It is the
earliest warning a maker gets, because fill likelihood correlates NEGATIVELY
with post-fill returns (PLAN.md 3.4): the quote that is filling well is the one
to be suspicious of.  Positive at 1s decaying to a positive plateau is healthy;
positive at 1s and negative at the long horizon means the spread capture is
being handed straight back to informed flow.

WHY THIS IS A RECORDER AND NOT A QUERY
--------------------------------------
`core/db.py` says of the `marks` table: mark-outs are "persisted rather than
recomputed, because recomputing it later from snapshots gives a DIFFERENT
answer -- the snapshot history is far too sparse to resolve those horizons."
That is not a performance note.  A horizon can only be measured while a quote
near it still exists in the archive, and the recorder keeps a ~400-ticker
watchlist at 5-second cadence against a 106,000-market universe.  Ask an hour
later and the nearest quote is minutes away from the horizon it is standing in
for; the number that comes back is the passage of time, not the trade.

So each horizon is recorded ONCE, as soon as it matures, and never revised.

WHAT "MATURE" MEANS
-------------------
A horizon h on a fill at t0 is measurable only after t0 + h has passed AND a
quote exists close enough to t0 + h to stand in for it.  Both conditions are
checked here; a horizon that cannot be measured is left unwritten rather than
written wrong, so a missing row means "not measurable", never "zero".
"""

from __future__ import annotations

import argparse
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Final

from core.db import Database
from core.models import Side, Venue, now_us

#: Horizons in microseconds.  1s / 5s / 60s / 5m / 30m -- the decay curve.
DEFAULT_HORIZONS_US: Final[tuple[int, ...]] = (
    1_000_000, 5_000_000, 60_000_000, 300_000_000, 1_800_000_000,
)


def staleness_budget_us(horizon_us: int) -> int:
    """How far past its own horizon a reference quote may be.

    Half the horizon, floored at one second -- the weakest bound that still
    keeps a reference nearer to its own horizon than to the next one up.  Mirrors
    `shadow.engine._staleness_budget`; the two must agree or the persisted marks
    and the on-the-fly ones would disagree about the same fill.
    """
    return max(horizon_us // 2, 1_000_000)


@dataclass(frozen=True, slots=True)
class Mark:
    """One matured mark-out, ready to persist."""

    client_order_id: str
    ticker: str
    venue: str
    filled_at_us: int
    horizon_us: int
    markout_cents: float
    ref_mid: float
    stale_us: int


@dataclass
class MarkStats:
    scanned: int = 0
    written: int = 0
    duplicates: int = 0
    immature: int = 0          # the horizon has not elapsed yet
    unobserved: int = 0        # elapsed, but no quote close enough to it
    started_us: int = field(default_factory=now_us)

    def report(self) -> str:
        mins = max((now_us() - self.started_us) / 60_000_000, 1e-9)
        return (f"scanned={self.scanned} written={self.written} "
                f"dup={self.duplicates} immature={self.immature} "
                f"unobserved={self.unobserved} uptime={mins:.1f}m")


@dataclass
class MarkRecorder:
    """Computes and persists mark-outs for fills whose horizons have matured."""

    db: Database
    horizons_us: tuple[int, ...] = DEFAULT_HORIZONS_US
    venue: Venue = Venue.KALSHI
    stats: MarkStats = field(default_factory=MarkStats)
    _stop: bool = False

    def request_stop(self, *_: object) -> None:
        self._stop = True

    # ------------------------------------------------------------------ read
    def pending(self, *, limit: int = 5000) -> list[tuple[str, str, int, int, Side]]:
        """(client_order_id, ticker, filled_at_us, yes_price_cents, side) per fill.

        `fills.price_cents` is SIDE-referenced and `orders.price_cents` is
        YES-referenced (see `monitor/kpi.py`), and a mark-out is YES-referenced
        by construction -- so the conversion happens HERE, in SQL, once.
        """
        rows = self.db.conn.execute(
            """SELECT f.client_order_id AS coid, o.ticker AS ticker,
                      f.filled_at_us AS at_us, o.side AS side,
                      CASE WHEN o.side = 'no' THEN 100 - f.price_cents
                           ELSE f.price_cents END AS yes_px
               FROM fills f
               JOIN orders o ON o.client_order_id = f.client_order_id
               WHERE f.terminal = 1 AND o.venue = ?
               ORDER BY f.filled_at_us
               LIMIT ?""",
            (self.venue.value, limit),
        ).fetchall()
        return [(r["coid"], r["ticker"], int(r["at_us"]), int(r["yes_px"]),
                 Side(str(r["side"]))) for r in rows]

    def already_recorded(self) -> set[tuple[str, int]]:
        return {
            (r["client_order_id"], int(r["horizon_us"]))
            for r in self.db.conn.execute(
                "SELECT client_order_id, horizon_us FROM marks")
        }

    def reference_quote(self, ticker: str, target_us: int,
                        budget_us: int) -> tuple[float, int] | None:
        """(mid, staleness) of the first quote at or after `target_us`, if close.

        Returns None when nothing lands inside the budget -- which is the honest
        answer, and the reason a missing row must never be read as a zero.
        """
        row = self.db.conn.execute(
            """SELECT yes_bid, yes_ask, observed_at_us FROM market_snapshots
               WHERE venue = ? AND ticker = ?
                 AND observed_at_us >= ? AND observed_at_us <= ?
               ORDER BY observed_at_us LIMIT 1""",
            (self.venue.value, ticker, target_us, target_us + budget_us),
        ).fetchone()
        if row is None or row["yes_bid"] is None or row["yes_ask"] is None:
            return None
        mid = (row["yes_bid"] + row["yes_ask"]) / 2.0
        return mid, int(row["observed_at_us"]) - target_us

    # --------------------------------------------------------------- compute
    def mature(self, *, now: int | None = None) -> list[Mark]:
        """Every mark-out that can be measured now and has not been recorded."""
        ts = now_us() if now is None else now
        seen = self.already_recorded()
        out: list[Mark] = []

        for coid, ticker, at_us, yes_px, side in self.pending():
            for h in self.horizons_us:
                self.stats.scanned += 1
                if (coid, h) in seen:
                    self.stats.duplicates += 1
                    continue
                target = at_us + h
                if target > ts:
                    self.stats.immature += 1        # come back later
                    continue
                ref = self.reference_quote(ticker, target, staleness_budget_us(h))
                if ref is None:
                    self.stats.unobserved += 1
                    continue
                mid, stale = ref
                # Signed FOR US: a rising fair value helps a long YES and hurts
                # a long NO.  Same convention as `shadow.engine.markout`.
                signed = (mid - yes_px) if side is Side.YES else (yes_px - mid)
                out.append(Mark(
                    client_order_id=coid, ticker=ticker, venue=self.venue.value,
                    filled_at_us=at_us, horizon_us=h,
                    markout_cents=round(signed, 4), ref_mid=mid, stale_us=stale,
                ))
        return out

    # ----------------------------------------------------------------- write
    def persist(self, marks: list[Mark]) -> int:
        """Idempotent by UNIQUE(client_order_id, horizon_us).

        A matured horizon is written ONCE and never revised -- re-deriving it
        later against a sparser archive would silently change a published KPI.
        """
        if not marks:
            return 0
        with self.db.tx() as c:
            before = c.total_changes
            c.executemany(
                """INSERT OR IGNORE INTO marks
                   (client_order_id, ticker, venue, filled_at_us, horizon_us,
                    markout_cents, ref_mid, stale_us)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [(m.client_order_id, m.ticker, m.venue, m.filled_at_us,
                  m.horizon_us, m.markout_cents, m.ref_mid, m.stale_us)
                 for m in marks],
            )
            written = c.total_changes - before
        self.stats.written += written
        return written

    def cycle(self, *, now: int | None = None) -> int:
        return self.persist(self.mature(now=now))

    # ---------------------------------------------------------------- report
    def curve(self) -> dict[int, tuple[int, float | None]]:
        """horizon -> (n, mean cents).  The decay curve, from persisted rows."""
        out: dict[int, tuple[int, float | None]] = {}
        for h in self.horizons_us:
            row = self.db.conn.execute(
                "SELECT COUNT(*) n, AVG(markout_cents) m FROM marks "
                "WHERE horizon_us = ? AND venue = ?", (h, self.venue.value),
            ).fetchone()
            n = int(row["n"] or 0)
            out[h] = (n, float(row["m"]) if n else None)
        return out

    def run(self, *, interval: float | None, once: bool = False) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        while not self._stop:
            t0 = time.monotonic()
            n = self.cycle()
            print(f"[marks] wrote {n} in {time.monotonic()-t0:.2f}s | "
                  f"{self.stats.report()}", flush=True)
            if once or interval is None:
                break
            slept = 0.0
            while slept < interval and not self._stop:
                time.sleep(min(1.0, interval - slept))
                slept += 1.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="persist mark-outs as they mature")
    ap.add_argument("--db", default="data/pm.db")
    ap.add_argument("--interval", type=float, default=None)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)

    with Database(args.db) as db:
        rec = MarkRecorder(db)
        rec.run(interval=args.interval, once=args.once)
        print("[marks] decay curve:", flush=True)
        for h, (n, mean) in rec.curve().items():
            shown = f"{mean:+.3f}c" if mean is not None else "--"
            print(f"  {h/1e6:8.1f}s  n={n:<5d} {shown}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
