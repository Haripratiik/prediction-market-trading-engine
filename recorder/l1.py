"""High-frequency L1 + trade-tape recorder.  NO CREDENTIALS REQUIRED.

Kalshi's WebSocket needs auth even for public channels, and `/markets/{t}/orderbook`
(full L2 depth) needs auth.  But two things are open to anyone:

    top of book   yes_bid / yes_ask / sizes, from `/markets?tickers=a,b,c`
    trade tape    `/markets/trades`, WITH `taker_side` labelled

That second one matters more than it looks.  Kalshi tells you which side was the
taker, so you never need Lee-Ready trade-sign inference -- which misclassifies
10-20% of trades on equities and is barely better than a coin flip on Polymarket
(~59% agreement with on-chain truth).  Combined with L1 quotes, this is enough to:

  * detect trade-THROUGH events, the honest maker-fill condition (research/07 3.6)
  * measure realized spread and mark-outs
  * estimate Kyle's lambda by regressing price change on signed order flow
  * run shadow mode end-to-end

What it is NOT: full L2 depth.  Queue position beyond the touch is invisible, so
fill models must stay queue-conservative until an authenticated account exists.

    python -m recorder.l1 --interval 5 --limit 400
"""

from __future__ import annotations

import argparse
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from core.db import Database
from core.models import Market, now_us
from venues.kalshi.client import PROD_BASE, KalshiClient, KalshiError

KILL_FILE = Path("KILL")
BATCH = 100          # tickers per request


@dataclass
class L1Stats:
    polls: int = 0
    quotes: int = 0
    trades: int = 0
    errors: int = 0
    started_us: int = field(default_factory=now_us)

    def report(self) -> str:
        mins = max((now_us() - self.started_us) / 60_000_000, 1e-9)
        return (
            f"polls={self.polls} quotes={self.quotes:,} trades={self.trades:,} "
            f"errors={self.errors} rate={self.quotes/mins:,.0f} quotes/min"
        )


def build_watchlist(
    db: Database,
    *,
    limit: int = 400,
    max_spread_cents: int = 10,
    min_depth_contracts: float = 20.0,
    min_hours: float = 1.0,
    max_hours: float = 720.0,
    include_families: bool = True,
) -> list[str]:
    """Pick the markets actually worth recording.

    84.8% of markets have zero 24h volume and the top 200 carry 62.7% of it, so
    recording everything at high frequency wastes most of the budget.  This
    selects liquid, tight, not-about-to-close markets -- the S1/S2 universe.

    `include_families` then pulls in the RELATED events on each selected game,
    and that is not a nicety -- without it a whole class of arbitrage is
    untestable by construction.

    Kalshi lists several logically-linked events per game under a shared key:
    `KXATPMATCH-<game>` (who wins) and `KXATPEXACTMATCH-<game>` (the exact score)
    are separate events bound by a hard identity --

        P(player wins) == sum over the exact scores in which that player wins

    -- and nothing in the matching engine enforces it.  Ranking purely by volume
    picks up the moneyline and drops the exact-score legs, so the two sides get
    recorded on completely different clocks.  MEASURED consequence: a scan found
    two apparent cross-market arbitrages worth 3.33c and 0.38c, and both
    dissolved on inspection -- the moneyline had 1,071 observations while its
    exact-score legs had 2, taken **11.9 hours apart**.  That is not an
    opportunity, it is a comparison between a live price and yesterday's.

    Recording the family together is the only way to tell those two apart.
    """
    now = now_us()
    rows = db.conn.execute(
        """SELECT m.ticker, m.yes_bid, m.yes_ask, m.yes_bid_size, m.volume_24h, m.close_at_us
           FROM market_snapshots m
           JOIN (SELECT ticker, MAX(observed_at_us) AS t
                 FROM market_snapshots GROUP BY ticker) latest
             ON m.ticker = latest.ticker AND m.observed_at_us = latest.t
           WHERE m.yes_bid IS NOT NULL AND m.yes_ask IS NOT NULL
             AND m.yes_bid >= 1 AND m.yes_ask > m.yes_bid
             AND m.volume_24h > 0
           ORDER BY m.volume_24h DESC"""
    ).fetchall()

    picked: list[str] = []
    for r in rows:
        if r["yes_ask"] - r["yes_bid"] > max_spread_cents:
            continue
        if (r["yes_bid_size"] or 0.0) < min_depth_contracts:
            continue
        close = r["close_at_us"]
        if close is not None:
            hrs = (close - now) / 3_600_000_000.0
            if not (min_hours <= hrs <= max_hours):
                continue
        picked.append(r["ticker"])
        if len(picked) >= limit:
            break

    if include_families:
        picked = _with_linked_families(db, picked, limit=limit)
    return picked


def is_game_key(key: str) -> bool:
    """Is this event-ticker tail a specific CONTEST, or just a date?

    `26AUG27ZHEPRI` names a match (date + two player codes) and its family is
    real.  `26AUG28` names a day, and treating it as a family makes
    `KXFEDDECISION-26SEP` a sibling of `KXTRUMPUFC-26SEP` -- markets with no
    logical relation whatsoever.  Measured: the naive rule produced 44 "families"
    of which the large ones were all pure date collisions.

    A contest key carries participant codes after the date, so it is longer and
    ends in letters.
    """
    if len(key) < 10:
        return False
    return key[-4:].isalpha()


def _with_linked_families(db: Database, picked: list[str],
                          *, limit: int) -> list[str]:
    """Add every market that shares a GAME KEY with something already picked.

    The game key is the event ticker minus its series prefix:
    `KXATPMATCH-26AUG27ZHEPRI` -> `26AUG27ZHEPRI`, which is also the key of
    `KXATPEXACTMATCH-26AUG27ZHEPRI`.  1,643 of 7,061 game keys in the archive
    carry two or more market types, so this is a real and common structure.

    Companions are appended AFTER the volume-ranked selection and the cap still
    applies, so this trades some breadth for the ability to compare linked
    markets on one clock.  Breadth we have; synchronised pairs we did not.
    """
    if not picked:
        return picked
    # Reserve room for companions BEFORE the volume ranking consumes the cap --
    # otherwise the family logic runs against a full list and adds nothing,
    # which is exactly what it did on the first attempt (1 companion added).
    core_n = max(1, int(limit * 0.75))
    picked = picked[:core_n]
    chosen = set(picked)
    holes = ",".join("?" * len(picked))
    keys = {
        r["event_ticker"].partition("-")[2]
        for r in db.conn.execute(
            f"""SELECT DISTINCT event_ticker FROM market_snapshots
                WHERE ticker IN ({holes}) AND event_ticker != ''""", tuple(picked))
        if is_game_key(r["event_ticker"].partition("-")[2])
    }
    if not keys:
        return picked

    out = list(picked)
    for key in keys:
        if len(out) >= limit:
            break
        for r in db.conn.execute(
            """SELECT m.ticker FROM market_snapshots m
               JOIN (SELECT ticker, MAX(observed_at_us) AS t FROM market_snapshots
                     GROUP BY ticker) l
                 ON m.ticker = l.ticker AND m.observed_at_us = l.t
               WHERE m.event_ticker LIKE ?
                 AND m.status = 'active'
                 AND m.yes_bid IS NOT NULL AND m.yes_ask IS NOT NULL""",
            ("%-" + key,),
        ):
            t = r["ticker"]
            if t not in chosen:
                chosen.add(t)
                out.append(t)
                if len(out) >= limit:
                    break
    return out


class L1Recorder:
    def __init__(self, db: Database, client: KalshiClient, tickers: list[str]) -> None:
        self.db = db
        self.client = client
        self.tickers = tickers
        self.stats = L1Stats()
        self._stop = False
        self._trade_cursor: str | None = None
        self._series_tickers: set[str] | None = None
        # An insertion-ORDERED dict, not a set.  Truncating a set keeps an
        # ARBITRARY subset -- `list(a_set)[-200_000:]` is not "the most recent
        # 200k", because set iteration order is unspecified -- so a recent
        # trade_id could be evicted while an ancient one survived.  Here that
        # only inflates a counter (`trades.trade_id` is a PRIMARY KEY and the
        # insert is OR IGNORE), but the same pattern in a consumer without a
        # uniqueness constraint would emit duplicate events.
        self._seen_trades: dict[str, None] = {}

    def request_stop(self, *_: object) -> None:
        self._stop = True

    def _series_of(self, raw: dict) -> str:
        """Resolve the SERIES for a market.  `/markets` does not return one.

        Without this, every L1-recorded row carried `series_ticker = ''`, so
        `MarketSnapshot.series_for()` returned None and every sleeve fell back to
        the default fee spec -- the one where MAKERS PAY ZERO.  24 of the 103
        live series are not that spec (21 charge a maker 0.25x base, 3 carry a
        0.5 multiplier), so edge was overstated on roughly 23% of the live
        universe, silently and always in our favour.  It is also exactly the
        rows the sleeves act on: L1 records the watchlist, and the freshness
        filter means the watchlist is the only thing quotable.

        Resolution is the LONGEST dash-prefix of the event ticker present in
        `series_cache`.  The naive first-segment rule matches 13,923 of 13,954
        events; the 31 misses are per-entity series like `KXMLBWINS-ATH-26`,
        whose series really is `KXMLBWINS-ATH`.
        """
        event = str(raw.get("event_ticker") or "")
        if not event:
            return ""
        if self._series_tickers is None:
            self._series_tickers = {
                r["ticker"] for r in self.db.conn.execute(
                    "SELECT ticker FROM series_cache")
            }
        parts = event.split("-")
        for n in range(len(parts), 0, -1):
            candidate = "-".join(parts[:n])
            if candidate in self._series_tickers:
                return candidate
        return parts[0]

    def poll_quotes(self) -> int:
        """One L1 sweep of the watchlist, written as append-only snapshots."""
        ts = now_us()
        out: list[Market] = []
        for i in range(0, len(self.tickers), BATCH):
            chunk = self.tickers[i : i + BATCH]
            # CACHE-BUST.  MEASURED: `api.elections.kalshi.com` is CloudFront,
            # and a plain request is served from cache with an `age` header of
            # up to 13 seconds -- which is what the "15-second refresh grid"
            # seen in the archive actually was.  It is the CDN's TTL, not the
            # venue's book.  Polling faster than the TTL just re-reads the same
            # cached object, which is why 5s polling looked 3x redundant.
            #
            # A unique query parameter forces `x-cache: Miss` on every request.
            # Measured on a live market (KXBTC15M, ~1,000 trades/min): 4 state
            # changes per 30 polls with the buster versus 2 without -- twice the
            # resolution, for free.  It is NOT a substitute for the WebSocket;
            # the origin has its own cadence and this still undersamples a
            # millisecond book badly.
            #
            # The signature covers `{timestamp}{method}{path}` and EXCLUDES the
            # query string (venues/kalshi/auth.py), so this cannot break auth.
            params = {"tickers": ",".join(chunk), "_cb": uuid.uuid4().hex}
            data = self.client._request("GET", "/markets", params=params)
            out.extend(
                Market.from_api(m, series_ticker=self._series_of(m))
                for m in data.get("markets", [])
            )
        if out:
            self.db.append_markets(out, observed_at_us=ts)
        self.stats.polls += 1
        self.stats.quotes += len(out)
        return len(out)

    def poll_trades(self, *, pages: int = 6) -> int:
        """Pull the public tape.  `taker_side` is labelled -- no sign inference.

        NEWEST FIRST, every poll, stopping as soon as we recognise a trade.

        The cursor used to PERSIST across polls.  Kalshi returns the tape
        newest-first and the cursor pages backwards, so each call walked further
        into the past and the recorder never saw a new print.  Measured
        consequence: the tape covered 01:24-03:28 while the quote recorder
        covered 03:28-04:09 -- **1.4 minutes of overlap** across a 5-hour run.

        That is not a coverage inconvenience, it is the reason no fill estimate
        in this system can be trusted: a counterfactual fill needs a print that
        happened WHILE a quote was resting, and with disjoint windows there are
        almost none.  Every fill rate measured so far is an optimistic bound
        that assumes we always win the queue.

        Restarting from the head each poll and stopping at the first already-seen
        trade_id gives continuous forward coverage with no gap and no re-fetch.
        """
        rows: list[tuple] = []
        cursor = None                 # ALWAYS start from the head of the tape
        caught_up = False
        for _ in range(pages):
            params: dict[str, object] = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = self.client._request("GET", "/markets/trades", params=params)
            batch = data.get("trades") or []
            for t in batch:
                tid = str(t.get("trade_id") or "")
                if not tid:
                    continue
                if tid in self._seen_trades:
                    caught_up = True      # reached what we already hold
                    continue
                self._seen_trades[tid] = None
                rows.append(
                    (
                        tid,
                        t.get("ticker") or "",
                        _parse_iso_us(t.get("created_time")),
                        _cents(t.get("yes_price_dollars")),
                        _num(t.get("count_fp")),
                        t.get("taker_side") or "",
                        int(bool(t.get("is_block_trade"))),
                    )
                )
            if caught_up:
                break                     # the rest of this page is history
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        self._trade_cursor = cursor
        if rows:
            with self.db.tx() as c:
                c.executemany(
                    """INSERT OR IGNORE INTO trades
                       (trade_id, ticker, traded_at_us, yes_price_cents, size,
                        taker_side, is_block)
                       VALUES (?,?,?,?,?,?,?)""",
                    rows,
                )
        # keep the dedupe set bounded
        if len(self._seen_trades) > 400_000:
            # dict preserves insertion order, so this really is the newest half
            keep = list(self._seen_trades)[-200_000:]
            self._seen_trades = dict.fromkeys(keep)
        self.stats.trades += len(rows)
        return len(rows)

    def run(self, *, interval: float, duration: float | None = None) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        t_end = time.monotonic() + duration if duration else None

        print(f"[l1] watching {len(self.tickers)} markets every {interval}s", flush=True)
        while not self._stop:
            if KILL_FILE.exists():
                print("[l1] KILL file present -- stopping", flush=True)
                break
            if t_end and time.monotonic() >= t_end:
                break
            t0 = time.monotonic()
            try:
                q = self.poll_quotes()
                tr = self.poll_trades()
                if self.stats.polls % 10 == 1:
                    print(
                        f"[l1] {q} quotes / {tr} new trades in {time.monotonic()-t0:.2f}s"
                        f" | {self.stats.report()}",
                        flush=True,
                    )
            except KalshiError as exc:
                self.stats.errors += 1
                print(f"[l1] API error: {exc}", flush=True)
            except Exception as exc:
                self.stats.errors += 1
                print(f"[l1] {type(exc).__name__}: {exc}", flush=True)

            slept = time.monotonic() - t0
            while slept < interval and not self._stop:
                time.sleep(min(0.5, interval - slept))
                slept = time.monotonic() - t0
        print(f"[l1] stopped. {self.stats.report()}", flush=True)


def _cents(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(round(float(str(raw)) * 100))
    except (TypeError, ValueError):
        return None


def _num(raw: object) -> float:
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return 0.0


def _parse_iso_us(ts: object) -> int | None:
    from core.models import parse_iso

    return parse_iso(str(ts)) if ts else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Kalshi L1 + trade-tape recorder")
    ap.add_argument("--db", default="data/pm.db")
    ap.add_argument("--base-url", default=PROD_BASE)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--limit", type=int, default=400, help="watchlist size")
    ap.add_argument("--duration", type=float, default=None, help="seconds to run")
    args = ap.parse_args(argv)

    with Database(args.db) as db, KalshiClient(base_url=args.base_url) as client:
        watch = build_watchlist(db, limit=args.limit)
        if not watch:
            print("[l1] empty watchlist -- run `python -m recorder.main --once` first")
            return 1
        rec = L1Recorder(db, client, watch)
        rec.run(interval=args.interval, duration=args.duration)
        print(f"[l1] db now holds: {db.counts()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
