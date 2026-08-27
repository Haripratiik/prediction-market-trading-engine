"""Universe recorder.  T-014.

The appreciating asset: neither venue serves historical order-book depth, so
every hour this does not run is a backtest you can never do (PLAN.md 6.1, 7.2).

Runs against the PUBLIC API with no credentials, which is what makes shadow mode
free. Enumerates via `/events?with_nested_markets=true` -- NOT `/markets`, which
returns 99.3% parlay shards (research/05 F2).

    python -m recorder.main --once
    python -m recorder.main --interval 300
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.db import Database
from core.models import now_us
from venues.kalshi.client import PROD_BASE, KalshiClient, KalshiError

KILL_FILE = Path("KILL")


@dataclass
class RecorderStats:
    cycles: int = 0
    events_seen: int = 0
    markets_seen: int = 0
    errors: int = 0
    started_us: int = field(default_factory=now_us)

    def report(self) -> str:
        mins = (now_us() - self.started_us) / 60_000_000
        return (
            f"cycles={self.cycles} events={self.events_seen:,} "
            f"markets={self.markets_seen:,} errors={self.errors} "
            f"uptime={mins:.1f}m"
        )


class Recorder:
    def __init__(self, db: Database, client: KalshiClient, *, max_pages: int = 400) -> None:
        self.db = db
        self.client = client
        self.max_pages = max_pages
        self.stats = RecorderStats()
        self._stop = False

    def request_stop(self, *_: object) -> None:
        self._stop = True

    def refresh_series(self) -> int:
        """`/series` returns the whole ~13.5k map in one call.  Cache it."""
        series = self.client.list_series()
        return self.db.upsert_series(series)

    def cycle(self) -> tuple[int, int]:
        """One full universe sweep, written as an append-only snapshot."""
        ts = now_us()
        events, markets = self.client.fetch_universe(max_pages=self.max_pages)
        self.db.append_events(events, observed_at_us=ts)
        self.db.append_markets(markets, observed_at_us=ts)

        # rules text is deduplicated by hash, so this is cheap after the first pass
        for m in markets:
            if m.rules_primary and m.rules_hash:
                self.db.store_rules("kalshi", m.ticker, m.rules_hash, m.rules_primary)

        self.stats.cycles += 1
        self.stats.events_seen += len(events)
        self.stats.markets_seen += len(markets)
        return len(events), len(markets)

    def run(self, *, interval: float | None, once: bool = False) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        n = self.refresh_series()
        print(f"[recorder] series cached: {n:,}", flush=True)

        while not self._stop:
            if KILL_FILE.exists():          # I9: the lowest-tech switch always works
                print("[recorder] KILL file present -- stopping", flush=True)
                break
            t0 = time.monotonic()
            try:
                ev, mk = self.cycle()
                print(
                    f"[recorder] {ev:,} events / {mk:,} markets in "
                    f"{time.monotonic()-t0:.1f}s | {self.stats.report()}",
                    flush=True,
                )
            except KalshiError as exc:
                self.stats.errors += 1
                print(f"[recorder] API error: {exc}", file=sys.stderr, flush=True)
            except Exception as exc:        # keep the recorder alive
                self.stats.errors += 1
                print(f"[recorder] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

            if once or interval is None:
                break
            slept = 0.0
            while slept < interval and not self._stop and not KILL_FILE.exists():
                time.sleep(min(1.0, interval - slept))
                slept += 1.0

        print(f"[recorder] stopped. {self.stats.report()}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Kalshi universe recorder")
    ap.add_argument("--db", default="data/pm.db")
    ap.add_argument("--base-url", default=PROD_BASE)
    ap.add_argument("--interval", type=float, default=None,
                    help="seconds between sweeps; omit for a single pass")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--max-pages", type=int, default=400)
    args = ap.parse_args(argv)

    with Database(args.db) as db, KalshiClient(base_url=args.base_url) as client:
        rec = Recorder(db, client, max_pages=args.max_pages)
        rec.run(interval=args.interval, once=args.once)
        stats = db.universe_stats()
        print(f"[recorder] db now holds: {db.counts()}", flush=True)
        print(f"[recorder] universe: {stats}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
