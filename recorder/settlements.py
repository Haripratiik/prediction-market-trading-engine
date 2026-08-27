"""Settlement recorder.  Populates the `settlements` table (PLAN.md section 5).

This is the link between "we placed orders" and "we know whether we were right".
Nothing else writes `settlements`, and without it there is no realised P&L
(`runner._settled_pnl_cents`), no Brier skill (KPI 1), no calibration
(`monitor.kpi.settled_decisions`), and no sleeve can clear G4, whose exit
criterion is literally `live_settlements >= 200`.

TWO SOURCES, AND THE DIFFERENCE IS THE WHOLE DESIGN
---------------------------------------------------
MARKET data -- `client.market_result(ticker)`.  Needs NO credentials, and is the
    ONLY source that works in shadow mode: in shadow no order was ever sent, so
    the account settled nothing and the portfolio feed is empty BY CONSTRUCTION.
PORTFOLIO data -- `client.iter_settlements()`.  The account's own settled
    positions, for live/demo.  It carries a real `settled_time`, which market
    data as exposed by `market_result()` does not.

When both exist they are CROSS-CHECKED.  A disagreement is reported loudly and
NEITHER is written: one of the two is a parsing bug, and writing either would
put a wrong outcome into every downstream P&L, Brier and gate decision.  A row
that is missing announces itself (the position stays open); a row that is wrong
does not.

VOID IS NOT NO
--------------
A voided/cancelled market returns every position AT COST.  It is not a NO
resolution, it is not a loss, and scoring it as a NO biases every Brier score
in the same direction on every void -- a silent, systematic calibration error.
`voided` is therefore the flag every consumer must branch on BEFORE reading
`outcome`, because the schema's `outcome` column is NOT NULL and a void has to
store *something* there (this module stores 0).  All current readers do branch
correctly: `monitor.kpi.settled_decisions` filters `s.voided = 0`,
`runner._settled_pnl_cents` skips `row["voided"]`, `backtest.engine` skips
`is_void` before scoring.

MEASURED AGAINST THE LIVE PUBLIC API (2026-08-26, 21,000 settled markets)
------------------------------------------------------------------------
  * a settled market reports `status='finalized'`, NOT 'settled'
  * `result` was 'no' 12,830 / 'yes' 7,971 / **'scalar' 199** (0.95%)
  * ZERO markets carried result 'void'/'canceled' in that sample
`result='scalar'` is a pro-rata payout (e.g. "resolves to the fair market
price" when a listed player is scratched; one such market settled at $0.16).
It is neither YES, nor NO, nor void-at-cost, and the `settlements` schema cannot
represent it -- so it is REFUSED and reported, never guessed at.

WHAT IS POLLED
--------------
Only tickers we have exposure to or recorded a decision about, taken from
`fills`/`orders`/`decisions`, ordered so real exposure is polled first.  The
venue lists 106,000+ markets; polling all of them is not an option and the
token bucket in `venues.kalshi.client` would (correctly) throttle it to a crawl.
Tickers already in `settlements` are never polled again -- which is both the
rate-limit saving and the idempotency guarantee.

    python -m recorder.settlements --once
    python -m recorder.settlements --interval 300 --limit 200
"""

from __future__ import annotations

import argparse
import signal
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from core.config import KalshiCredentials, load_settings
from core.db import Database
from core.models import Venue, now_us, parse_iso
from venues.kalshi.client import PROD_BASE, KalshiClient, KalshiError

KILL_FILE = Path("KILL")

# `Market.status` vocabulary (research/06 section 7):
#   initialized -> active -> closed -> determined -> finalized
#   disputed -> amended RESTARTS the settlement timer.
# Only the terminal states are recorded.  A `determined` market has a result
# that can still be disputed and amended, and rows here are immutable -- so
# recording one early is how a wrong outcome becomes permanent.
SETTLED_STATUSES = frozenset({"finalized", "settled"})

# Void vocabulary.  `market_result()` already maps the `result` field; these
# cover the case where the STATUS itself says the market paid nobody.
VOID_STATUSES = frozenset({"voided", "canceled", "cancelled"})

# Field names the portfolio feed might use for the result and the timestamp.
# UNVERIFIED: this machine's demo account has zero settlements, so the row shape
# could not be confirmed against a real response.  Parsed defensively for that
# reason -- an unrecognised shape yields None and is reported, never guessed.
_PORTFOLIO_RESULT_KEYS = ("market_result", "result", "outcome")
_PORTFOLIO_TIME_KEYS = ("settled_time", "settled_ts", "settlement_ts",
                        "determined_time", "settled_at")
_PORTFOLIO_VOID_RESULTS = frozenset({"void", "voided", "canceled", "cancelled"})


def describe(outcome: int | None, voided: bool) -> str:
    """Human-readable verdict.  VOID is deliberately not spelled like NO."""
    if voided:
        return "VOID"
    if outcome is None:
        return "UNRESOLVED"
    return "YES" if outcome == 1 else "NO"


class SettlementClient(Protocol):
    """The slice of a venue client this recorder needs.

    Narrow on purpose: a test double implements two methods and no socket is
    ever opened.
    """

    def market_result(self, ticker: str) -> tuple[str, int | None, bool]: ...

    def iter_settlements(self, **params: Any) -> Iterator[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class Settlement:
    """One resolved market, before it is written."""

    venue: str
    ticker: str
    settled_at_us: int
    outcome: int | None          # 1 = YES, 0 = NO, None when voided
    voided: bool
    source: str = "market"       # market | portfolio | market+portfolio

    @property
    def outcome_column(self) -> int:
        """What goes in the NOT NULL `outcome` column.

        A void HAS no outcome, but the column cannot be NULL, so it stores 0 --
        which makes a void row byte-identical to a NO row apart from `voided`.
        That is exactly why the flag is checked first everywhere.
        """
        if self.voided or self.outcome is None:
            return 0
        return int(self.outcome)

    def verdict(self) -> tuple[int | None, bool]:
        """(outcome, voided), with the void case collapsed so it compares equal
        regardless of what the padding `outcome` happens to be."""
        return (None, True) if self.voided else (self.outcome, False)

    def __str__(self) -> str:
        return f"{self.ticker} {describe(self.outcome, self.voided)} ({self.source})"


@dataclass(frozen=True, slots=True)
class Conflict:
    """Two sources that cannot both be right.  Never resolved silently."""

    ticker: str
    a_source: str
    a_outcome: int | None
    a_voided: bool
    b_source: str
    b_outcome: int | None
    b_voided: bool

    def __str__(self) -> str:
        return (
            f"SETTLEMENT CONFLICT {self.ticker}: "
            f"{self.a_source}={describe(self.a_outcome, self.a_voided)} vs "
            f"{self.b_source}={describe(self.b_outcome, self.b_voided)} "
            f"-- NOTHING WRITTEN; one of the two is a bug"
        )


@dataclass
class SettlementStats:
    cycles: int = 0
    polled: int = 0
    written: int = 0
    already_recorded: int = 0
    still_open: int = 0
    unresolvable: int = 0
    conflicts: int = 0
    not_found: int = 0
    errors: int = 0
    started_us: int = field(default_factory=now_us)

    def report(self) -> str:
        mins = (now_us() - self.started_us) / 60_000_000
        return (
            f"cycles={self.cycles} polled={self.polled:,} written={self.written:,} "
            f"known={self.already_recorded:,} open={self.still_open:,} "
            f"unresolvable={self.unresolvable} conflicts={self.conflicts} "
            f"missing={self.not_found} errors={self.errors} uptime={mins:.1f}m"
        )


class SettlementRecorder:
    """Discovers settled markets and writes them to `settlements`, idempotently.

    Rows are IMMUTABLE.  A settlement is written once and never updated, so a
    re-run cannot duplicate a row (UNIQUE(venue, ticker)) and cannot flip one
    (no UPDATE path exists).  A later poll that disagrees with a stored row is
    reported as a conflict instead -- if the outcome of a market we already
    booked P&L against has changed, that is a human's problem, not something to
    paper over.
    """

    def __init__(
        self,
        db: Database,
        client: SettlementClient,
        *,
        venue: Venue = Venue.KALSHI,
        limit: int = 200,
        portfolio: bool = True,
        portfolio_max_pages: int = 20,
        sleep_between: float = 0.05,
    ) -> None:
        self.db = db
        self.client = client
        self.venue = Venue(venue).value
        self.limit = max(1, int(limit))
        self.portfolio = portfolio
        self.portfolio_max_pages = portfolio_max_pages
        self.sleep_between = sleep_between
        self.stats = SettlementStats()
        self.conflicts: list[Conflict] = []
        self.unresolvable: list[tuple[str, str]] = []    # (ticker, why)
        self._offset = 0                                 # rotation, anti-starvation
        self._stop = False

    def request_stop(self, *_: object) -> None:
        self._stop = True

    # ------------------------------------------------------------ candidates
    def recorded(self) -> set[str]:
        """Tickers already settled.  Never polled again -- see the class doc."""
        return {
            r["ticker"]
            for r in self.db.conn.execute(
                "SELECT ticker FROM settlements WHERE venue = ?", (self.venue,)
            )
        }

    def candidates(self) -> list[str]:
        """Tickers we have exposure to or recorded a decision about, unsettled.

        Ordered by how much a wrong answer would cost: markets we actually hold
        (a fill exists) first, then markets we merely quoted, then markets we
        only formed an opinion about.  Un-acted decisions are included on
        purpose -- scoring only the trades you took measures your execution
        filter, not your model (PLAN.md 6.3).
        """
        rows = self.db.conn.execute(
            """SELECT u.ticker AS ticker, MAX(u.pri) AS pri, MAX(u.t) AS last_us
                 FROM (
                       SELECT o.ticker AS ticker, 3 AS pri, f.filled_at_us AS t
                         FROM fills f
                         JOIN orders o ON o.client_order_id = f.client_order_id
                        WHERE o.venue = ?
                       UNION ALL
                       SELECT ticker AS ticker, 2 AS pri, created_at_us AS t
                         FROM orders WHERE venue = ?
                       UNION ALL
                       SELECT ticker AS ticker, 1 AS pri, decided_at_us AS t
                         FROM decisions
                        WHERE venue IS NULL OR venue = ?
                      ) AS u
                WHERE u.ticker IS NOT NULL AND u.ticker <> ''
                  AND u.ticker NOT IN (SELECT ticker FROM settlements WHERE venue = ?)
                GROUP BY u.ticker
                ORDER BY pri DESC, last_us DESC""",
            (self.venue, self.venue, self.venue, self.venue),
        ).fetchall()
        return [str(r["ticker"]) for r in rows]

    def poll_list(self, portfolio_tickers: list[str] | None = None) -> list[str]:
        """The tickers this cycle will actually poll, at most `limit` of them.

        Portfolio tickers come first: those are settled positions of real money.
        The rest rotates across cycles, so a candidate set larger than `limit`
        is covered eventually instead of the tail starving forever behind a
        fixed sort order.
        """
        known = self.recorded()
        out: list[str] = []
        seen: set[str] = set()
        for t in portfolio_tickers or []:
            if t and t not in known and t not in seen:
                seen.add(t)
                out.append(t)

        pending = [t for t in self.candidates() if t not in seen]
        if pending:
            start = self._offset % len(pending)
            rotated = pending[start:] + pending[:start]
            room = max(0, self.limit - len(out))
            take = rotated[:room]
            out.extend(take)
            self._offset = (start + len(take)) % len(pending)
        return out[: self.limit]

    # -------------------------------------------------------- market source
    def market_settlement(self, ticker: str) -> Settlement | None:
        """The shadow-mode source.  None when the market has not finally settled.

        Returns None (and records it as unresolvable) for a market that IS final
        but whose result cannot be expressed as a binary outcome -- MEASURED:
        `result='scalar'`, 199 of 21,000 live settled markets.  Note that
        `market_result()` reports that case as `(status, None, False)`, which is
        indistinguishable from an open market by outcome alone; only the status
        separates them.
        """
        status, outcome, voided = self.client.market_result(ticker)
        status = str(status or "").lower()

        if voided or status in VOID_STATUSES:
            return Settlement(self.venue, ticker, self.settled_at_from_snapshot(ticker),
                              None, True, "market")
        if status not in SETTLED_STATUSES:
            self.stats.still_open += 1
            return None
        if outcome is None:
            self._note_unresolvable(
                ticker,
                f"status={status!r} is final but the result is not yes/no/void "
                f"(scalar or unknown); the settlements schema cannot hold it",
            )
            return None
        return Settlement(self.venue, ticker, self.settled_at_from_snapshot(ticker),
                          int(outcome), False, "market")

    def settled_at_from_snapshot(self, ticker: str) -> int:
        """Best available settlement time for a MARKET-data settlement.

        `market_result()` returns only (status, outcome, voided) -- the
        `settlement_ts` the API actually carries is discarded before we see it
        -- so this uses the market's own close time from our snapshots, falling
        back to the observation time.

        Close time is deliberately preferred over "now": it is a LOWER bound on
        the true settlement (settlement follows close by
        `settlement_timer_seconds`, 30s-3600s), and the calibration join guards
        look-ahead with `decided_at_us <= settled_at_us` (I6).  Dating a
        settlement at the moment we happened to poll -- which can be hours late
        if this process was down -- would let a decision made AFTER the market
        actually resolved be scored, manufacturing skill out of nothing.
        """
        ts = now_us()
        row = self.db.latest_market(ticker, venue=self.venue)
        if row is not None and row["close_at_us"] is not None:
            close = int(row["close_at_us"])
            if 0 < close <= ts:
                return close
        return ts

    # ----------------------------------------------------- portfolio source
    def portfolio_settlements(self) -> dict[str, Settlement]:
        """The account's own settled positions.  Empty in shadow, by construction.

        No credentials means `_request` raises before any socket is opened, so
        the shadow path costs nothing and is not an error -- it disables the
        cross-check and says so once.
        """
        if not self.portfolio:
            return {}
        out: dict[str, Settlement] = {}
        try:
            for raw in self.client.iter_settlements(max_pages=self.portfolio_max_pages):
                s = self.parse_portfolio_row(raw)
                if s is not None:
                    out.setdefault(s.ticker, s)
        except KalshiError as exc:
            self.portfolio = False
            print(
                f"[settle] portfolio settlements unavailable ({exc}); "
                f"continuing on MARKET data only -- this is the shadow-mode path",
                file=sys.stderr, flush=True,
            )
            return {}
        return out

    def parse_portfolio_row(self, raw: dict[str, Any]) -> Settlement | None:
        """Parse one `/portfolio/settlements` row.

        Defensive about field names on purpose: the row shape is UNVERIFIED
        against a real response (the demo account here has settled nothing), and
        Kalshi's fixed-point migration renames fields with `_dollars`/`_fp`
        suffixes.  An unrecognised result is reported, never assumed to be NO.
        """
        ticker = str(raw.get("ticker") or "").strip()
        if not ticker:
            return None

        result = ""
        for key in _PORTFOLIO_RESULT_KEYS:
            if raw.get(key):
                result = str(raw[key]).strip().lower()
                break

        settled_at: int | None = None
        for key in _PORTFOLIO_TIME_KEYS:
            if raw.get(key):
                settled_at = parse_iso(str(raw[key]))
                if settled_at is not None:
                    break
        if settled_at is None:
            settled_at = now_us()

        if result in _PORTFOLIO_VOID_RESULTS:
            return Settlement(self.venue, ticker, settled_at, None, True, "portfolio")
        if result == "yes":
            return Settlement(self.venue, ticker, settled_at, 1, False, "portfolio")
        if result == "no":
            return Settlement(self.venue, ticker, settled_at, 0, False, "portfolio")

        self._note_unresolvable(
            ticker, f"portfolio result {result!r} is not yes/no/void")
        return None

    # ------------------------------------------------------------ combining
    def combine(self, market: Settlement | None,
                portfolio: Settlement | None) -> Settlement | None:
        """Cross-check the two sources.  A disagreement writes NOTHING."""
        if market is None:
            return portfolio
        if portfolio is None:
            return market
        if market.verdict() != portfolio.verdict():
            self._note_conflict(market, portfolio)
            return None
        # Agreed.  Keep the portfolio's timestamp: it is a real settlement time
        # rather than the close-time lower bound inferred from snapshots.
        return replace(market, settled_at_us=portfolio.settled_at_us,
                       source="market+portfolio")

    # -------------------------------------------------------------- writing
    def record(self, s: Settlement) -> str:
        """Write one settlement.  Returns inserted | duplicate | conflict.

        The row is immutable: an existing row is never updated, so re-running
        cannot flip an outcome that P&L has already been booked against.  A
        stored row that disagrees with a fresh observation is a conflict and is
        reported.
        """
        with self.db.tx() as c:
            if self._disagrees_with_stored(c, s):
                return "conflict"
            cur = c.execute(
                """INSERT INTO settlements (venue, ticker, settled_at_us, outcome, voided)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(venue, ticker) DO NOTHING""",
                (s.venue, s.ticker, int(s.settled_at_us), s.outcome_column,
                 int(s.voided)),
            )
            if cur.rowcount == 1:
                return "inserted"
            # Lost the race to another writer between the read and the insert.
            # DO NOTHING swallowed it, so re-read: a row that won the race with a
            # DIFFERENT verdict is still a conflict and must not pass as a
            # harmless duplicate.
            if self._disagrees_with_stored(c, s):
                return "conflict"
        return "duplicate"

    def _disagrees_with_stored(self, c: sqlite3.Connection, s: Settlement) -> bool:
        """True (and reported) when a stored row contradicts `s`."""
        row = c.execute(
            "SELECT outcome, voided FROM settlements WHERE venue = ? AND ticker = ?",
            (s.venue, s.ticker),
        ).fetchone()
        if row is None:
            return False
        stored: tuple[int | None, bool] = (
            (None, True) if row["voided"] else (int(row["outcome"]), False)
        )
        if stored == s.verdict():
            return False
        self._note_conflict(
            Settlement(s.venue, s.ticker, 0, stored[0], stored[1], "recorded"), s)
        return True

    # ---------------------------------------------------------------- cycle
    # ------------------------------------------------------------------ bulk
    #: `/markets?tickers=` accepts a comma-separated batch.  100 keeps the URL
    #: comfortably short and costs one request per hundred markets.
    BULK_BATCH: int = 100

    def bulk_candidates(self, limit: int) -> list[str]:
        """Tickers from OUR OWN ARCHIVE that have no settlement row yet.

        The per-ticker path exists to settle what we TRADED.  This exists to
        settle everything we have ever SEEN, which is a different and much
        larger set -- and for calibration it is the right one: any settled
        market is a labelled example, whether or not we had a position in it.

        `status=settled` cannot be used to enumerate these.  MEASURED: 40 pages
        of that endpoint returned 8,000 markets of which **4** were non-MVE with
        volume and a yes/no result -- it is almost entirely parlay shards.  Our
        own recorded universe is the better list, and a real settled market
        reports `status='finalized'`, not `'settled'`.
        """
        rows = self.db.conn.execute(
            """SELECT m.ticker FROM market_snapshots m
               JOIN (SELECT ticker, MAX(observed_at_us) AS t FROM market_snapshots
                     GROUP BY ticker) l
                 ON m.ticker = l.ticker AND m.observed_at_us = l.t
               WHERE m.ticker NOT LIKE 'KXMVE%'
                 AND m.volume_24h > 0
                 AND m.ticker NOT IN (SELECT ticker FROM settlements)
               ORDER BY m.volume_24h DESC
               LIMIT ?""", (limit,)).fetchall()
        return [r["ticker"] for r in rows]

    def bulk_cycle(self, *, limit: int = 2000) -> tuple[int, int]:
        """Resolve many markets per request.  Returns (polled, written).

        One request settles up to `BULK_BATCH` markets, against one request per
        market on the per-ticker path -- the difference between minutes and
        hours for the same answer.
        """
        tickers = self.bulk_candidates(limit)
        polled = written = 0
        for i in range(0, len(tickers), self.BULK_BATCH):
            if self._stop:
                break
            chunk = tickers[i:i + self.BULK_BATCH]
            try:
                data = self.client._request(
                    "GET", "/markets",
                    params={"tickers": ",".join(chunk), "_cb": uuid.uuid4().hex})
            except Exception as exc:                    # noqa: BLE001
                self.stats.errors += 1
                print(f"[settle] bulk batch failed: {exc}", flush=True)
                continue
            polled += len(chunk)
            for m in data.get("markets") or []:
                s = self._from_market_payload(m)
                if s is None:
                    continue
                if self.record(s) == "inserted":
                    written += 1
            time.sleep(self.sleep_between)
        self.stats.polled += polled
        self.stats.written += written
        return polled, written

    def _from_market_payload(self, m: dict[str, Any]) -> "Settlement | None":
        """A `/markets` row -> a Settlement, or None if it is not resolvable.

        A `scalar` result is a PRO-RATA payout -- neither YES, nor NO, nor
        void-at-cost -- and the settlements schema cannot hold it, so it is
        refused rather than mis-scored as a NO.  Measured at ~0.95% of settled
        markets, and scoring one as NO is a silent, systematic calibration error.
        """
        ticker = str(m.get("ticker") or "")
        status = str(m.get("status") or "").lower()
        result = str(m.get("result") or "").lower()
        if not ticker or status not in ("finalized", "settled"):
            return None
        if result in ("void", "voided", "canceled", "cancelled"):
            outcome, voided = None, True
        elif result == "yes":
            outcome, voided = 1, False
        elif result == "no":
            outcome, voided = 0, False
        else:
            self.stats.unresolvable += 1        # scalar / unknown
            return None
        settled_at = (parse_iso(str(m.get("settlement_ts")))
                      if m.get("settlement_ts")
                      else None) or self.settled_at_from_snapshot(ticker)
        venue = getattr(self.venue, "value", self.venue)
        return Settlement(ticker=ticker, venue=venue,
                          settled_at_us=settled_at, outcome=outcome,
                          voided=voided, source="market")

    def cycle(self) -> tuple[int, int]:
        """One pass.  Returns (polled, written)."""
        port = self.portfolio_settlements()
        todo = self.poll_list(sorted(port))
        written = 0

        for ticker in todo:
            if self._stop:
                break
            self.stats.polled += 1
            try:
                market = self.market_settlement(ticker)
            except KalshiError as exc:
                # One dead ticker must never stop settlement ingestion for the
                # rest: an unsettled book is invisible P&L.
                if exc.status == 404:
                    self.stats.not_found += 1
                    print(f"[settle] {ticker}: 404 -- market no longer served",
                          file=sys.stderr, flush=True)
                else:
                    self.stats.errors += 1
                    print(f"[settle] {ticker}: {exc}", file=sys.stderr, flush=True)
                continue
            except Exception as exc:                      # keep the loop alive
                self.stats.errors += 1
                print(f"[settle] {ticker}: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
                continue

            final = self.combine(market, port.get(ticker))
            if final is None:
                continue
            outcome = self.record(final)
            if outcome == "inserted":
                written += 1
                self.stats.written += 1
            elif outcome == "duplicate":
                self.stats.already_recorded += 1
            if self.sleep_between > 0:
                time.sleep(self.sleep_between)

        self.stats.cycles += 1
        return len(todo), written

    def run(self, *, interval: float | None, once: bool = False) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        while not self._stop:
            if KILL_FILE.exists():        # I9: the lowest-tech switch always works
                print("[settle] KILL file present -- stopping", flush=True)
                break
            t0 = time.monotonic()
            try:
                polled, written = self.cycle()
                print(
                    f"[settle] polled {polled} / wrote {written} in "
                    f"{time.monotonic() - t0:.1f}s | {self.stats.report()}",
                    flush=True,
                )
            except KalshiError as exc:
                self.stats.errors += 1
                print(f"[settle] API error: {exc}", file=sys.stderr, flush=True)
            except Exception as exc:      # keep the recorder alive
                self.stats.errors += 1
                print(f"[settle] {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)

            if once or interval is None:
                break
            slept = 0.0
            while slept < interval and not self._stop and not KILL_FILE.exists():
                time.sleep(min(1.0, interval - slept))
                slept += 1.0

        print(f"[settle] stopped. {self.stats.report()}", flush=True)
        self.print_problems()

    def print_problems(self) -> None:
        """Say the quiet parts out loud, at the end, where they get read."""
        for c in self.conflicts:
            print(f"[settle] {c}", file=sys.stderr, flush=True)
        for ticker, why in self.unresolvable:
            print(f"[settle] UNRESOLVABLE {ticker}: {why}",
                  file=sys.stderr, flush=True)

    # --------------------------------------------------------------- notes
    def _note_conflict(self, a: Settlement, b: Settlement) -> None:
        c = Conflict(a.ticker, a.source, a.outcome, a.voided,
                     b.source, b.outcome, b.voided)
        self.conflicts.append(c)
        self.stats.conflicts += 1
        print(f"[settle] {c}", file=sys.stderr, flush=True)

    def _note_unresolvable(self, ticker: str, why: str) -> None:
        self.unresolvable.append((ticker, why))
        self.stats.unresolvable += 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def credentials_match(creds: KalshiCredentials, base_url: str) -> bool:
    """True when these credentials belong to the exchange we are reading.

    A demo key against a prod universe (or the reverse) would poll DEMO tickers
    for markets recorded from PROD and cross-check prod settlements against a
    play-money account.  Both answers would be garbage, and garbage that looks
    like data is worse than no data.
    """
    return bool(creds.is_complete) and creds.base_url == base_url.rstrip("/")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Kalshi settlement recorder")
    ap.add_argument("--db", default="data/pm.db")
    ap.add_argument("--base-url", default=PROD_BASE)
    ap.add_argument("--interval", type=float, default=None,
                    help="seconds between sweeps; omit for a single pass")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--limit", type=int, default=200,
                    help="tickers polled per sweep (rotates across sweeps)")
    ap.add_argument("--no-portfolio", action="store_true",
                    help="market data only, even if credentials are present")
    ap.add_argument("--sleep", type=float, default=0.05,
                    help="pause between market polls, on top of the token bucket")
    args = ap.parse_args(argv)

    settings = load_settings()
    signer = None
    if not args.no_portfolio:
        if credentials_match(settings.kalshi, args.base_url):
            signer = settings.kalshi.signer()
        elif settings.kalshi.is_complete:
            print(
                f"[settle] credentials are for {settings.kalshi.env} "
                f"({settings.kalshi.base_url}) but market data is being read from "
                f"{args.base_url}; portfolio cross-check DISABLED",
                file=sys.stderr, flush=True,
            )
        else:
            print(f"[settle] no credentials ({settings.kalshi.describe()}); "
                  f"market data only -- the shadow-mode path", flush=True)

    with Database(args.db) as db, KalshiClient(base_url=args.base_url,
                                               signer=signer) as client:
        rec = SettlementRecorder(db, client, limit=args.limit,
                                 portfolio=signer is not None,
                                 sleep_between=args.sleep)
        rec.run(interval=args.interval, once=args.once)
        print(f"[settle] db now holds: {db.counts()}", flush=True)
        if rec.conflicts:
            print(f"[settle] {len(rec.conflicts)} CONFLICT(S) -- nothing was written "
                  f"for those tickers; resolve by hand", file=sys.stderr, flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
