"""Backfill historical 1-minute OHLC from the public candlesticks endpoint.  T-058.

    python -m recorder.history --status settled --limit 500
    python -m recorder.history --tickers KXMLBGAME-26AUG271305COLWSH-WSH

WHY THIS IS THE MOST VALUABLE RECORDER
--------------------------------------
Every other recorder here is point-in-time: to obtain 1,600 settled markets you
must WAIT for 1,600 markets to settle.  At the observed ~100 settlements/day
that is sixteen days before an edge smaller than 5 percentage points can even be
tested, and `markets_to_beat_market` scales inverse-square, so 2pp needs a
hundred days.  Calendar time was the binding constraint on this whole project.

`/series/{series}/markets/{ticker}/candlesticks` removes it:

  * PUBLIC and unauthenticated -- verified against production, no account.
  * Works on ALREADY-SETTLED markets -- verified 4/4 on settled tickers.
  * Returns BOTH sides of the book per minute (bid and ask, OHLC each), plus
    traded volume and open interest.

So the same 1,600 settlements can be fetched in an afternoon.  Combined with the
`settlements` table, each backfilled market is a complete labelled example: the
full price path the market believed, and the outcome that actually occurred.

WHAT A CANDLE CANNOT DO
-----------------------
A candle is an aggregate over a minute with NO QUEUE SIZES, so it cannot support
a counterfactual fill -- you cannot ask "would my resting order have been hit"
of an OHLC bar.  `market_snapshots` and `trades` remain the source for anything
execution-shaped.  Candles answer the CALIBRATION question ("what did the market
think at time t, and what happened"), which is a different and cheaper question.
"""

from __future__ import annotations

import argparse
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from core.db import Database
from core.models import now_us
from venues.kalshi.client import PROD_BASE, KalshiClient, KalshiError

#: Kalshi rejects windows wider than this for 1-minute candles, so a long-lived
#: market has to be walked in chunks rather than asked for in one call.
MAX_WINDOW_S: int = 7 * 86_400


def _cents(value: Any) -> int | None:
    """Fixed-point dollar string -> integer cents.  None when absent.

    The wire format is `"0.7700"`, NOT an integer -- the same trap that made a
    docs-faithful WebSocket parser read empty books (venues/kalshi/ws.py).
    """
    if value in (None, ""):
        return None
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


@dataclass
class HistoryStats:
    markets: int = 0
    candles: int = 0
    written: int = 0
    errors: int = 0
    skipped: int = 0
    started_us: int = field(default_factory=now_us)

    def report(self) -> str:
        mins = max((now_us() - self.started_us) / 60_000_000, 1e-9)
        return (f"markets={self.markets} candles={self.candles} "
                f"written={self.written} skipped={self.skipped} "
                f"errors={self.errors} uptime={mins:.1f}m")


@dataclass
class HistoryRecorder:
    """Fetches candlestick history and stores it append-only."""

    db: Database
    client: KalshiClient
    period_minutes: int = 1
    sleep_between: float = 0.15
    stats: HistoryStats = field(default_factory=HistoryStats)
    _stop: bool = False

    def request_stop(self, *_: object) -> None:
        self._stop = True

    # ------------------------------------------------------------------ fetch
    def fetch(self, ticker: str, series: str, *,
              start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        """One market's candles over a window, walked in <=MAX_WINDOW_S chunks."""
        out: list[dict[str, Any]] = []
        lo = start_ts
        while lo < end_ts and not self._stop:
            hi = min(lo + MAX_WINDOW_S, end_ts)
            params = {
                "start_ts": lo,
                "end_ts": hi,
                "period_interval": self.period_minutes,
                # The public host is CloudFront and serves cached bodies with an
                # `age` of up to 13s; a unique parameter forces a fresh read.
                # Harmless for history, and it keeps one code path.
                "_cb": uuid.uuid4().hex,
            }
            data = self.client._request(
                "GET", f"/series/{series}/markets/{ticker}/candlesticks",
                params=params)
            out.extend(data.get("candlesticks") or [])
            lo = hi
            if lo < end_ts:
                time.sleep(self.sleep_between)
        return out

    # ------------------------------------------------------------------ store
    def store(self, ticker: str, series: str,
              candles: Iterable[dict[str, Any]]) -> int:
        rows = []
        for c in candles:
            bid = c.get("yes_bid") or {}
            ask = c.get("yes_ask") or {}
            price = c.get("price") or {}
            rows.append((
                ticker, series, int(c.get("end_period_ts") or 0),
                self.period_minutes,
                _cents(bid.get("open_dollars")), _cents(bid.get("high_dollars")),
                _cents(bid.get("low_dollars")), _cents(bid.get("close_dollars")),
                _cents(ask.get("open_dollars")), _cents(ask.get("high_dollars")),
                _cents(ask.get("low_dollars")), _cents(ask.get("close_dollars")),
                _cents(price.get("close_dollars") or price.get("previous_dollars")),
                float(c.get("volume_fp") or 0.0),
                float(c.get("open_interest_fp") or 0.0),
            ))
        if not rows:
            return 0
        with self.db.tx() as conn:
            before = conn.total_changes
            # OR IGNORE: history is immutable, so a re-fetch of an overlapping
            # window must be a no-op rather than a rewrite.
            conn.executemany(
                """INSERT OR IGNORE INTO candles
                   (ticker, series_ticker, end_period_ts, period_minutes,
                    yes_bid_open, yes_bid_high, yes_bid_low, yes_bid_close,
                    yes_ask_open, yes_ask_high, yes_ask_low, yes_ask_close,
                    price_close, volume, open_interest)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
            return conn.total_changes - before

    # ---------------------------------------------------------------- targets
    def settled_targets(self, limit: int) -> list[tuple[str, str, int]]:
        """(ticker, series, settled_ts) for settled markets we have not stored.

        Settled markets are the valuable ones: each is a labelled example, so
        the price path is a FEATURE and the outcome is the LABEL.
        """
        rows = self.db.conn.execute(
            """SELECT s.ticker AS ticker, m.series_ticker AS series,
                      s.settled_at_us AS settled
               FROM settlements s
               JOIN (SELECT ticker, series_ticker FROM market_snapshots
                     WHERE series_ticker != '' GROUP BY ticker) m
                 ON m.ticker = s.ticker
               WHERE s.voided = 0
                 AND s.ticker NOT IN (SELECT DISTINCT ticker FROM candles)
               LIMIT ?""", (limit,)).fetchall()
        return [(r["ticker"], r["series"], int(r["settled"] // 1_000_000))
                for r in rows]

    def iter_targets(self, tickers: list[str]) -> Iterator[tuple[str, str, int]]:
        for t in tickers:
            row = self.db.conn.execute(
                """SELECT series_ticker, close_at_us FROM market_snapshots
                   WHERE ticker = ? AND series_ticker != ''
                   ORDER BY observed_at_us DESC LIMIT 1""", (t,)).fetchone()
            if row is None:
                self.stats.skipped += 1
                continue
            end = int((row["close_at_us"] or now_us()) // 1_000_000)
            yield t, row["series_ticker"], end

    # ------------------------------------------------------------------- run
    def backfill(self, targets: Iterable[tuple[str, str, int]],
                 *, lookback_days: float = 3.0) -> int:
        written = 0
        for ticker, series, end_ts in targets:
            if self._stop:
                break
            start_ts = int(end_ts - lookback_days * 86_400)
            try:
                candles = self.fetch(ticker, series,
                                     start_ts=start_ts, end_ts=end_ts + 3600)
            except KalshiError as exc:
                self.stats.errors += 1
                print(f"[history] {ticker}: {exc}", flush=True)
                continue
            self.stats.markets += 1
            self.stats.candles += len(candles)
            n = self.store(ticker, series, candles)
            self.stats.written += n
            written += n
            time.sleep(self.sleep_between)
        return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="backfill 1-minute candle history")
    ap.add_argument("--db", default="data/pm.db")
    ap.add_argument("--limit", type=int, default=200,
                    help="settled markets to backfill this run")
    ap.add_argument("--lookback-days", type=float, default=3.0)
    ap.add_argument("--period", type=int, default=1, help="candle minutes")
    ap.add_argument("--tickers", default="", help="comma-separated, overrides --limit")
    args = ap.parse_args(argv)

    with Database(args.db) as db:
        rec = HistoryRecorder(db, KalshiClient(base_url=PROD_BASE),
                              period_minutes=args.period)
        signal.signal(signal.SIGINT, rec.request_stop)
        signal.signal(signal.SIGTERM, rec.request_stop)

        if args.tickers:
            targets = list(rec.iter_targets(
                [t.strip() for t in args.tickers.split(",") if t.strip()]))
        else:
            targets = rec.settled_targets(args.limit)
        print(f"[history] {len(targets)} market(s) to backfill", flush=True)
        rec.backfill(targets, lookback_days=args.lookback_days)
        print(f"[history] {rec.stats.report()}", flush=True)

        total = db.conn.execute("SELECT COUNT(*) c FROM candles").fetchone()["c"]
        mkts = db.conn.execute(
            "SELECT COUNT(DISTINCT ticker) c FROM candles").fetchone()["c"]
        print(f"[history] candles table: {total:,} rows over {mkts:,} markets",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
