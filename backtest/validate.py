"""Does the S2 short-basket statistical arbitrage actually make money?

This module answers ONE question and refuses to answer it with a single number:
does resting maker asks across a mutually-exclusive Kalshi outcome set earn
money after fees, queue position, orphan risk and adverse selection?  The
answer this harness is designed to be able to produce is NO.

WHY THIS EXISTS SEPARATELY FROM backtest/engine.py
--------------------------------------------------
`backtest/engine.py::_score` aggregates P&L PER TICKER -- "the market outcome is
the independence unit".  For a single-market directional sleeve that is right.
For S2 it is wrong, and wrong in the direction that manufactures edge.

In a mutually-exclusive set exactly one leg resolves YES.  A short basket over
n legs therefore collects $1 on n-1 legs and pays $1 on one, BY CONSTRUCTION.
Score that per leg and a 4-leg short "wins" 3 times out of 4 with a 75% hit
rate before anybody has had a single correct thought.  Score n legs as n
independent samples and the standard error shrinks by sqrt(n), so the
confidence interval that a promotion gate reads is roughly sqrt(n) times too
narrow.  That is how a noise process reported a 77.8% win rate here.

THE INDEPENDENCE UNIT IS THE STRUCTURE.  One event, one draw, one sample.
Every statistic in this file is computed over structures.  `IndependenceUnit`
computes the leg-level number alongside it purely to show how large the
illusion is.

WHAT IS MEASURED, AND WHAT THE DATA CAN AND CANNOT SUPPORT
----------------------------------------------------------
  1. Margin census      -- how many flagged-MECE events clear the maker short
                           margin gate, and by how much.  Priced from the
                           recorded book, not from a fantasy of resting at 1c
                           on a market nobody bids (research/05 4.3).
  2. Joint fill         -- an n-leg structure needs n SIMULTANEOUS maker fills.
                           P(all legs fill) and the orphan rate
                           P(some but not all) are measured over the recorded
                           tape under all three fill models.
  3. Orphan cost        -- what it costs to resolve a partial fill, against the
                           margin the structure was opened for.  Both remedies
                           are priced: FLATTEN the overhang (cross the ask) and
                           COMPLETE the basket (cross the bid on the missing
                           legs).  The strategy is credited with the better one.
  4. Net P&L            -- per STRUCTURE, after fees, as a three-model bracket.
                           Never a point estimate (PLAN.md R6.7d).
  5. Null control       -- the identical pipeline over random structures built
                           from ME events that FAIL the margin gate, leg-count
                           matched so that the mechanical n-dependence of joint
                           fill cannot masquerade as edge.  If the real cohort
                           does not beat the control there is no edge.

Everything is read-only.  `ReadOnlyArchive` opens SQLite with `mode=ro` and has
no write path at all, which is what lets this run against a database a live
recorder is appending to.  It never calls `Database.migrate`.

DETERMINISM
-----------
Same archive plus same config -> same report.  No `now_us()`, no `uuid4()`, no
unseeded randomness (the control cohort draws from `random.Random(seed)`), and
every query carries an ORDER BY.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backtest.fills import (
    ADVERSE_FILL_BAND,
    ALL_MODELS,
    AdverseFillCheck,
    FillModel,
    RestingOrder,
    SimFill,
    TapeTrade,
    adverse_fill_gate,
    simulate_maker_fill,
    with_markout,
)
from core.math.contracts import FeeSpec, fee
from core.models import Event, Market, Series, SettlementSource, Side
from rulebook.exhaustiveness import check_mece
from strategy.base import MarketSnapshot
from strategy.s2_shortbasket import (
    Direction,
    S2Config,
    S2ShortBasket,
    Structure,
    rest_price_short_cents,
)

MINUTE_US = 60_000_000
HOUR_US = 3_600_000_000

#: scipy.stats.norm.ppf(0.975), frozen so a report is reproducible offline.
Z95 = 1.959963984540054

#: A leg whose series is missing is priced pessimistically -- the same rule the
#: sleeve uses (strategy/s2_shortbasket.py UNKNOWN_SERIES_FEE_SPEC).
UNKNOWN_SERIES_FEE_SPEC: FeeSpec = FeeSpec.kalshi("quadratic_with_maker_fees", 1.0)


# --------------------------------------------------------------------------- #
# Read-only archive access
# --------------------------------------------------------------------------- #


class ReadOnlyArchive:
    """SELECT-only handle on a recorded archive.

    `core.db.Database.__init__` runs `migrate()`, which writes.  A live recorder
    owns `data/pm.db`, so this class exists to make writing impossible rather
    than merely unintended: the connection is opened with SQLite's `mode=ro`
    URI flag, and the class exposes no method that mutates.

    It duck-types the three members of `Database` that point-in-time reading
    needs -- `conn`, `latest_market`, `get_series` -- so the same code can run
    against a real `Database` in tests and against the live file in production.
    """

    def __init__(self, path: str | Path, *, venue: str = "kalshi") -> None:
        self.path = str(path)
        self.venue = venue
        if self.path == ":memory:":
            raise ValueError("ReadOnlyArchive needs a file; use Database(':memory:') in tests")
        uri = "file:" + Path(self.path).as_posix() + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row

    def latest_market(self, ticker: str, *, as_of_us: int | None = None,
                      venue: str = "kalshi") -> sqlite3.Row | None:
        """The anti-look-ahead accessor, mirrored from `core.db.Database`."""
        if as_of_us is None:
            raise ValueError("as_of_us is mandatory here: a read without it is look-ahead")
        return self.conn.execute(
            """SELECT * FROM market_snapshots
               WHERE venue = ? AND ticker = ? AND observed_at_us <= ?
               ORDER BY observed_at_us DESC LIMIT 1""",
            (venue, ticker, as_of_us),
        ).fetchone()

    def get_series(self, ticker: str) -> Series | None:
        row = self.conn.execute(
            "SELECT * FROM series_cache WHERE ticker = ?", (ticker,)
        ).fetchone()
        if row is None:
            return None
        return Series(
            ticker=row["ticker"],
            title=row["title"] or "",
            category=row["category"] or "",
            fee_type=row["fee_type"],
            fee_multiplier=row["fee_multiplier"],
            contract_terms_url=row["contract_terms_url"],
            settlement_sources=tuple(
                SettlementSource(**s)
                for s in json.loads(row["settlement_sources_json"] or "[]")
            ),
            additional_prohibitions=tuple(json.loads(row["prohibitions_json"] or "[]")),
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ReadOnlyArchive":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    """Every knob that can move a number, in one auditable object."""

    venue: str = "kalshi"
    #: explicit decision instants; empty means "choose them from the archive".
    decision_times: tuple[int, ...] = ()
    max_decision_times: int = 6
    #: a decision instant must be a sweep covering at least this many tickers,
    #: otherwise the "universe" it prices is a handful of watchlist names.
    min_sweep_rows: int = 200
    #: a leg whose most recent snapshot is older than this at decision time is
    #: DROPPED.  Quoting off an eight-hour-old book is not a maker strategy, it
    #: is a guess, and counting its fills would flatter the harness.
    max_book_staleness_us: int | None = 15 * MINUTE_US
    #: how long the order rests.  Default is the sleeve's own leg timeout
    #: (S2Config.leg_timeout_seconds = 900 s).
    fill_horizon_us: int = 900_000_000
    #: mark-out horizon for the R6.7c adverse-fill gate.  A maker whose fills are
    #: NOT mostly adverse is being handed liquidity the real market would not
    #: have given -- realized CME maker fills are 66-89% adverse.
    markout_horizon_us: int = 5 * MINUTE_US
    bankroll_cents: int = 1_000_000
    #: control cohort: how many random structures per real structure, and the
    #: seed that makes them reproducible.
    control_multiple: int = 5
    control_seed: int = 20260827
    s2: S2Config = field(default_factory=S2Config)

    def as_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "decision_times": list(self.decision_times),
            "max_decision_times": self.max_decision_times,
            "min_sweep_rows": self.min_sweep_rows,
            "max_book_staleness_us": self.max_book_staleness_us,
            "fill_horizon_us": self.fill_horizon_us,
            "markout_horizon_us": self.markout_horizon_us,
            "bankroll_cents": self.bankroll_cents,
            "control_multiple": self.control_multiple,
            "control_seed": self.control_seed,
            "min_margin": self.s2.min_margin,
            "min_legs": self.s2.min_legs,
            "min_leg_depth": self.s2.min_leg_depth,
            "max_leg_spread_cents": self.s2.max_leg_spread_cents,
        }


# --------------------------------------------------------------------------- #
# Point-in-time reading
# --------------------------------------------------------------------------- #


def _market_from_row(row: sqlite3.Row) -> Market:
    return Market(
        ticker=row["ticker"],
        event_ticker=row["event_ticker"] or "",
        series_ticker=row["series_ticker"] or "",
        title=row["title"] or "",
        status=row["status"] or "",
        yes_bid=row["yes_bid"],
        yes_ask=row["yes_ask"],
        yes_bid_size=row["yes_bid_size"] or 0.0,
        yes_ask_size=row["yes_ask_size"] or 0.0,
        volume=row["volume"] or 0.0,
        volume_24h=row["volume_24h"] or 0.0,
        open_interest=row["open_interest"] or 0.0,
        close_at_us=row["close_at_us"],
        rules_hash=row["rules_hash"] or "",
    )


def _event_from_row(row: sqlite3.Row) -> Event:
    raw = json.loads(row["settlement_sources_json"] or "[]")
    return Event(
        event_ticker=row["event_ticker"],
        series_ticker=row["series_ticker"] or "",
        category=row["category"] or "",
        title=row["title"] or "",
        mutually_exclusive=bool(row["mutually_exclusive"]),
        collateral_return_type=row["collateral_return_type"] or "",
        settlement_sources=tuple(SettlementSource(**s) for s in raw),
        exhaustive_verified=bool(row["exhaustive_verified"]),
    )


def book_as_of(
    archive: Any, at_us: int, *, venue: str = "kalshi",
    max_staleness_us: int | None = None,
) -> dict[str, tuple[Market, int]]:
    """ticker -> (Market, observed_at_us) for the latest row with obs <= at_us.

    One windowed query instead of one lookup per ticker, because the universe is
    118k tickers and the per-ticker version costs minutes.  The predicate is the
    same predicate `Database.latest_market` uses -- `observed_at_us <= at_us`,
    latest wins -- and `test_validate.py` pins the two against each other on a
    fixture so this optimisation cannot silently drift into look-ahead.
    """
    lo = -(1 << 62) if max_staleness_us is None else at_us - max_staleness_us
    rows = archive.conn.execute(
        """SELECT ticker, event_ticker, series_ticker, title, status,
                  yes_bid, yes_ask, yes_bid_size, yes_ask_size,
                  volume, volume_24h, open_interest, close_at_us, rules_hash,
                  observed_at_us
           FROM (
             SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY ticker ORDER BY observed_at_us DESC) AS rn
             FROM market_snapshots
             WHERE venue = ? AND observed_at_us <= ? AND observed_at_us >= ?
           )
           WHERE rn = 1
           ORDER BY ticker""",
        (venue, at_us, lo),
    ).fetchall()
    return {r["ticker"]: (_market_from_row(r), int(r["observed_at_us"])) for r in rows}


def events_as_of(archive: Any, at_us: int, *, venue: str = "kalshi") -> dict[str, Event]:
    """event_ticker -> Event, latest row observed at or before `at_us`."""
    rows = archive.conn.execute(
        """SELECT * FROM (
             SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY event_ticker ORDER BY observed_at_us DESC) AS rn
             FROM event_snapshots
             WHERE venue = ? AND observed_at_us <= ?
           )
           WHERE rn = 1
           ORDER BY event_ticker""",
        (venue, at_us),
    ).fetchall()
    return {r["event_ticker"]: _event_from_row(r) for r in rows}


def tape_window(archive: Any) -> tuple[int | None, int | None, int]:
    """(first print, last print, number of prints).  The measurable window.

    A decision taken outside this window cannot have its fills measured: an
    order resting where no tape was recorded reports zero fills, and zero fills
    from a silent recorder is CENSORING, not evidence of an unfillable market.
    """
    row = archive.conn.execute(
        "SELECT MIN(traded_at_us) a, MAX(traded_at_us) b, COUNT(*) n FROM trades"
    ).fetchone()
    if row is None or row["n"] == 0:
        return None, None, 0
    return int(row["a"]), int(row["b"]), int(row["n"])


def sweep_times(archive: Any, cfg: ValidationConfig) -> tuple[tuple[int, int], ...]:
    """Candidate decision instants: (timestamp, tickers covered by that sweep)."""
    rows = archive.conn.execute(
        """SELECT observed_at_us t, COUNT(*) n FROM market_snapshots
           WHERE venue = ? GROUP BY observed_at_us HAVING n >= ?
           ORDER BY observed_at_us""",
        (cfg.venue, cfg.min_sweep_rows),
    ).fetchall()
    return tuple((int(r["t"]), int(r["n"])) for r in rows)


def choose_decision_times(archive: Any, cfg: ValidationConfig) -> tuple[int, ...]:
    """Decision instants that lie inside the tape window, evenly spread.

    Refusing instants outside the tape window is the single most important
    honesty rule in this module.  The recorded archive here starts snapshotting
    5.4 hours before the trade recorder starts; every order placed in that gap
    would report a clean zero fill rate for a reason that has nothing to do with
    the market.
    """
    if cfg.decision_times:
        return tuple(sorted(cfg.decision_times))
    lo, hi, n = tape_window(archive)
    if lo is None or hi is None or n == 0:
        return ()
    eligible = [(t, n) for t, n in sweep_times(archive, cfg) if lo <= t < hi]
    if not eligible:
        return ()
    usable = [t for t, _ in eligible]
    if len(usable) <= cfg.max_decision_times:
        return tuple(usable)
    # The WIDEST sweep inside the window is always included.  This archive is
    # two full universe sweeps plus a 400-ticker watchlist polled every 5 s; an
    # evenly-spaced sample lands almost entirely on the watchlist and would
    # report "no candidates" for a reason that is about the recorder, not the
    # market.  Widest-plus-spread keeps both the census and the time spread.
    widest = max(eligible, key=lambda r: (r[1], -r[0]))[0]
    k = max(1, cfg.max_decision_times - 1)
    step = (len(usable) - 1) / k if k > 1 else len(usable) - 1
    picked = {widest}
    picked.update(usable[int(round(i * step))] for i in range(k))
    return tuple(sorted(picked)[: cfg.max_decision_times])


def tape_for(archive: Any, ticker: str, start_us: int, end_us: int) -> tuple[TapeTrade, ...]:
    rows = archive.conn.execute(
        """SELECT traded_at_us, yes_price_cents, size, taker_side FROM trades
           WHERE ticker = ? AND traded_at_us > ? AND traded_at_us <= ?
           ORDER BY traded_at_us, trade_id""",
        (ticker, start_us, end_us),
    ).fetchall()
    return tuple(
        TapeTrade(
            traded_at_us=int(r["traded_at_us"]),
            yes_price_cents=int(r["yes_price_cents"]),
            size=float(r["size"] or 0.0),
            taker_side=str(r["taker_side"] or ""),
        )
        for r in rows
        if r["traded_at_us"] is not None and r["yes_price_cents"] is not None
    )


def markout_cents(archive: Any, ticker: str, at_us: int, price_cents: int, *,
                  horizon_us: int, venue: str = "kalshi") -> float | None:
    """Signed mark-out for a SHORT YES position, positive = the mark moved our way.

    The reference quote must be no more than half a horizon late
    (`shadow/engine.py::_staleness_budget`).  Without that bound "the first
    snapshot at or after t+h" is whatever exists, and on an archive whose
    universe sweeps are hours apart every horizon resolves to the same row --
    which measures the passage of time, not the trade.  Returning None is the
    honest answer, and the adverse-fill gate counts it as unmeasurable rather
    than as a pass.
    """
    budget = max(horizon_us // 2, 1_000_000)
    target = at_us + horizon_us
    row = archive.conn.execute(
        """SELECT yes_bid, yes_ask FROM market_snapshots
           WHERE venue = ? AND ticker = ?
             AND observed_at_us >= ? AND observed_at_us <= ?
           ORDER BY observed_at_us LIMIT 1""",
        (venue, ticker, target, target + budget),
    ).fetchone()
    if row is None or row["yes_bid"] is None or row["yes_ask"] is None:
        return None
    fair = (row["yes_bid"] + row["yes_ask"]) / 2.0
    return float(price_cents) - fair


def settlements_map(archive: Any, *, venue: str = "kalshi") -> dict[str, tuple[int, int, bool]]:
    rows = archive.conn.execute(
        """SELECT ticker, settled_at_us, outcome, voided FROM settlements
           WHERE venue = ? ORDER BY ticker""",
        (venue,),
    ).fetchall()
    return {r["ticker"]: (int(r["settled_at_us"]), int(r["outcome"]), bool(r["voided"]))
            for r in rows}


# --------------------------------------------------------------------------- #
# Candidate structures
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Candidate:
    """One short-basket intent, priced from a point-in-time book.

    `cohort` is "real" for a structure the sleeve would actually have quoted and
    "control" for a random structure built over an event that FAILED the margin
    gate.  Both run the identical downstream pipeline; that is the whole point.
    """

    cohort: str
    decided_at_us: int
    event_ticker: str
    legs: tuple[str, ...]
    prices_cents: tuple[int, ...]          # YES-referenced resting asks
    size: int
    sum_px_cents: float
    margin_cents: float                    # per basket, net of maker fees
    fee_spec: FeeSpec
    book_bid: tuple[int | None, ...]
    book_ask: tuple[int | None, ...]
    book_ask_size: tuple[float, ...]
    staleness_us: tuple[int, ...]

    @property
    def n_legs(self) -> int:
        return len(self.legs)

    def maker_fee_cents(self, price_cents: int) -> float:
        return 100.0 * fee(price_cents / 100.0, self.fee_spec, is_maker=True)

    def taker_fee_cents(self, price_cents: float) -> float:
        px = min(max(price_cents / 100.0, 0.01), 0.99)
        return 100.0 * fee(px, self.fee_spec, is_maker=False)


def _snapshot_for(event: Event, legs: Sequence[Market], at_us: int,
                  series: dict[str, Series], bankroll_cents: int) -> MarketSnapshot:
    return MarketSnapshot(
        now_us=at_us,
        markets=tuple(legs),
        events={event.event_ticker: event},
        series=series,
        bankroll_cents=bankroll_cents,
    )


def _series_for_legs(archive: Any, legs: Sequence[Market],
                     cache: dict[str, Series | None]) -> dict[str, Series]:
    out: dict[str, Series] = {}
    for m in legs:
        st = m.series_ticker
        if not st:
            continue
        if st not in cache:
            cache[st] = archive.get_series(st)
        s = cache[st]
        if s is not None:
            out[st] = s
    return out


def locked_size_for(legs: Sequence[Market], capital: float, bankroll_cents: int,
                    cfg: S2Config) -> int:
    """The sleeve's locked-size rule, restated so the control cohort uses it too.

    Depth and capital lockup, NOT Kelly -- `strategy/s2_shortbasket.py`
    `_locked_size`.  `test_validate.py` asserts this reproduces the sleeve's own
    method exactly, so the control cohort cannot be quietly sized differently
    from the real one.
    """
    depth_cap = int(min(m.yes_bid_size for m in legs) * cfg.max_depth_fraction)
    cap_cents = max(1, round(capital * 100.0))
    budget = int(bankroll_cents * cfg.max_structure_fraction)
    return max(0, min(depth_cap, budget // cap_cents))


def _candidate_from_structure(s: Structure, at_us: int, cohort: str,
                              legs: Sequence[Market], spec: FeeSpec,
                              staleness: dict[str, int]) -> Candidate:
    by_ticker = {m.ticker: m for m in legs}
    ordered = [by_ticker[t] for t in s.legs]
    return Candidate(
        cohort=cohort,
        decided_at_us=at_us,
        event_ticker=s.event_ticker,
        legs=s.legs,
        prices_cents=s.prices_cents,
        size=s.size,
        sum_px_cents=100.0 * s.sum_px,
        margin_cents=100.0 * s.margin,
        fee_spec=spec,
        book_bid=tuple(m.yes_bid for m in ordered),
        book_ask=tuple(m.yes_ask for m in ordered),
        book_ask_size=tuple(float(m.yes_ask_size) for m in ordered),
        staleness_us=tuple(staleness.get(m.ticker, 0) for m in ordered),
    )


@dataclass(frozen=True, slots=True)
class ScanStats:
    """What the scan saw, so an empty candidate list is never a silent zero."""

    decided_at_us: int
    forward_tape_us: int
    events_flagged_me: int
    events_with_book: int
    events_dropped_stale: int
    events_two_sided_ge_min: int
    passed_gate: int
    failed_margin: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "decided_at_us": self.decided_at_us,
            "forward_tape_us": self.forward_tape_us,
            "events_flagged_me": self.events_flagged_me,
            "events_with_book": self.events_with_book,
            "events_dropped_stale": self.events_dropped_stale,
            "events_two_sided_ge_min": self.events_two_sided_ge_min,
            "passed_gate": self.passed_gate,
            "failed_margin": self.failed_margin,
        }


def scan(
    archive: Any, at_us: int, cfg: ValidationConfig, *, tape_end_us: int,
) -> tuple[list[Candidate], list[Candidate], ScanStats]:
    """Price every flagged-MECE event at `at_us`.  Returns (real, fail_pool, stats).

    `real` are the structures `S2ShortBasket.evaluate` would actually quote:
    locked, n >= min_legs, size > 0, margin >= min_margin.  Running the sleeve
    itself rather than a re-implementation is deliberate -- a harness that
    validates a paraphrase of the strategy validates nothing.

    `fail_pool` are the events that cleared MECE and liquidity but whose short
    margin missed the gate.  They are the raw material for the null control.
    """
    sleeve = S2ShortBasket(cfg=cfg.s2)
    book = book_as_of(archive, at_us, venue=cfg.venue,
                      max_staleness_us=cfg.max_book_staleness_us)
    book_all = (book if cfg.max_book_staleness_us is None
                else book_as_of(archive, at_us, venue=cfg.venue))
    events = events_as_of(archive, at_us, venue=cfg.venue)

    by_event: dict[str, list[Market]] = {}
    stale_by_event: dict[str, dict[str, int]] = {}
    for ticker in sorted(book):
        market, obs = book[ticker]
        if market.event_ticker:
            by_event.setdefault(market.event_ticker, []).append(market)
            stale_by_event.setdefault(market.event_ticker, {})[ticker] = at_us - obs
    seen_any: set[str] = {
        m.event_ticker for m, _ in book_all.values() if m.event_ticker
    }

    me_events = sorted(et for et, e in events.items() if e.mutually_exclusive)
    series_cache: dict[str, Series | None] = {}
    real: list[Candidate] = []
    pool: list[Candidate] = []
    n_book = n_stale = n_two_sided = n_pass = n_fail = 0

    for et in me_events:
        legs = by_event.get(et)
        if not legs:
            if et in seen_any:
                n_stale += 1
            continue
        n_book += 1
        quoted = [m for m in legs if m.has_two_sided_quote]
        if len(quoted) < cfg.s2.min_legs:
            continue
        n_two_sided += 1
        event = events[et]
        series = _series_for_legs(archive, quoted, series_cache)
        snap = _snapshot_for(event, quoted, at_us, series, cfg.bankroll_cents)
        spec = sleeve.fee_spec(quoted, snap)
        s = sleeve.evaluate(event, quoted, snap)
        stale = stale_by_event.get(et, {})

        tradeable = (
            s is not None
            and s.locked
            and s.direction is Direction.SHORT
            and s.n_legs >= cfg.s2.min_legs
            and s.size > 0
            and s.margin >= cfg.s2.min_margin
        )
        if tradeable and s is not None:
            n_pass += 1
            real.append(_candidate_from_structure(s, at_us, "real", quoted, spec, stale))
            continue

        # Control raw material: a genuinely restable MECE book whose maker short
        # does NOT clear the margin gate.  `check_mece` is the same gate the
        # sleeve applies, so the control differs from the real cohort in exactly
        # one property -- the margin -- and in nothing else.
        check = check_mece(event, list(quoted))
        if not check.safe_to_sell:
            continue
        px = [rest_price_short_cents(m) for m in quoted]
        maker_fees = sum(100.0 * fee(p / 100.0, spec, is_maker=True) for p in px)
        margin_cents = sum(px) - 100.0 - maker_fees
        if margin_cents >= 100.0 * cfg.s2.min_margin:
            continue
        n_fail += 1
        pool.append(Candidate(
            cohort="control-pool",
            decided_at_us=at_us,
            event_ticker=et,
            legs=tuple(m.ticker for m in quoted),
            prices_cents=tuple(px),
            size=0,
            sum_px_cents=float(sum(px)),
            margin_cents=margin_cents,
            fee_spec=spec,
            book_bid=tuple(m.yes_bid for m in quoted),
            book_ask=tuple(m.yes_ask for m in quoted),
            book_ask_size=tuple(float(m.yes_ask_size) for m in quoted),
            staleness_us=tuple(stale.get(m.ticker, 0) for m in quoted),
        ))

    stats = ScanStats(
        decided_at_us=at_us,
        forward_tape_us=max(0, tape_end_us - at_us),
        events_flagged_me=len(me_events),
        events_with_book=n_book,
        events_dropped_stale=n_stale,
        events_two_sided_ge_min=n_two_sided,
        passed_gate=n_pass,
        failed_margin=n_fail,
    )
    return real, pool, stats


def build_control(
    archive: Any, pool: Sequence[Candidate], real: Sequence[Candidate],
    cfg: ValidationConfig,
) -> list[Candidate]:
    """Random structures from the failed-margin pool, leg-count matched.

    LEG-COUNT MATCHING IS NOT COSMETIC.  P(all n legs fill) falls roughly
    geometrically in n, so a control whose leg counts differ from the real
    cohort's compares two different mechanical difficulties and calls the
    difference edge.  Each control draws an n from the real cohort's empirical
    leg-count distribution and then a random n-subset of a random pool event
    that has at least n legs.
    """
    if not pool or not real:
        return []
    rng = random.Random(cfg.control_seed)
    want_ns = [c.n_legs for c in real]
    by_n: dict[int, list[Candidate]] = {}
    for c in pool:
        by_n.setdefault(c.n_legs, []).append(c)
    sizes = sorted(by_n)
    out: list[Candidate] = []
    target = len(real) * cfg.control_multiple
    for k in range(target):
        n = want_ns[k % len(want_ns)]
        eligible = [m for m in sizes if m >= n]
        if not eligible:
            continue
        src = rng.choice(by_n[rng.choice(eligible)])
        idx = sorted(rng.sample(range(src.n_legs), n))
        legs = [src.legs[i] for i in idx]
        px = [src.prices_cents[i] for i in idx]
        maker_fees = sum(src.maker_fee_cents(p) for p in px)
        # capital: un-netted short collateral, n - sum(px), matching
        # strategy/s2_shortbasket.py::locked_capital(netted=False)
        capital = (n * 100.0 - sum(px)) / 100.0
        cap_cents = max(1, round(capital * 100.0))
        budget = int(cfg.bankroll_cents * cfg.s2.max_structure_fraction)
        depth = min(src.book_ask_size[i] for i in idx)
        size = max(0, min(int(depth * cfg.s2.max_depth_fraction), budget // cap_cents))
        out.append(Candidate(
            cohort="control",
            decided_at_us=src.decided_at_us,
            event_ticker=src.event_ticker,
            legs=tuple(legs),
            prices_cents=tuple(px),
            size=size,
            sum_px_cents=float(sum(px)),
            margin_cents=sum(px) - 100.0 - maker_fees,
            fee_spec=src.fee_spec,
            book_bid=tuple(src.book_bid[i] for i in idx),
            book_ask=tuple(src.book_ask[i] for i in idx),
            book_ask_size=tuple(src.book_ask_size[i] for i in idx),
            staleness_us=tuple(src.staleness_us[i] for i in idx),
        ))
    return out


# --------------------------------------------------------------------------- #
# Fill simulation and structure-level P&L
# --------------------------------------------------------------------------- #


def leg_order(cand: Candidate, i: int) -> RestingOrder:
    """One resting NO order at the YES-referenced ask.

    `queue_ahead` mirrors `BacktestEngine._queue_ahead`: the displayed size at
    our price when we decided, and zero when we quote away from the touch --
    which is OPTIMISTIC and is exactly why R6.7e says to calibrate queue
    position from realized fills rather than believe displayed depth.
    """
    price = cand.prices_cents[i]
    ask = cand.book_ask[i]
    queue = cand.book_ask_size[i] if ask == price else 0.0
    return RestingOrder(
        order_id=f"{cand.event_ticker}-{cand.decided_at_us}-{i:03d}",
        ticker=cand.legs[i],
        side=Side.NO,
        price_cents=price,
        size=max(cand.size, 0),
        placed_at_us=cand.decided_at_us,
        queue_ahead=queue,
        book_bid=cand.book_bid[i],
        book_ask=ask,
    )


@dataclass(frozen=True, slots=True)
class StructureOutcome:
    """What one structure did under one fill model.  ONE SAMPLE.

    `net_cents` is the structure's whole P&L: locked baskets at their designed
    margin, plus whatever the residual cost to resolve.  It is the only quantity
    downstream statistics are computed over, because the structure is the
    independence unit.
    """

    cohort: str
    model: FillModel
    event_ticker: str
    decided_at_us: int
    n_legs: int
    size: int
    margin_cents: float
    filled: tuple[float, ...]
    matched: float                  # complete baskets = min over legs
    target: float                   # max over legs -- what completing would reach
    legs_touched: int
    net_cents: float
    resolution: str                 # none | complete | flatten | unresolvable
    flatten_cents: float | None
    complete_cents: float | None
    orphan_leg_cost_cents: tuple[float, ...]
    first_fill_at_us: int | None
    #: the per-leg SimFills, mark-out attached.  Kept so the R6.7c adverse-fill
    #: gate can be run over exactly the fills this P&L was computed from.
    leg_fills: tuple[SimFill, ...] = ()

    @property
    def all_filled(self) -> bool:
        return self.legs_touched == self.n_legs and self.n_legs > 0

    @property
    def any_filled(self) -> bool:
        return self.legs_touched > 0

    @property
    def orphaned(self) -> bool:
        """Some legs filled, not all.  The single most expensive RV failure."""
        return 0 < self.legs_touched < self.n_legs


def _unwind_book(archive: Any, ticker: str, at_us: int,
                 venue: str) -> tuple[int | None, int | None]:
    row = archive.latest_market(ticker, as_of_us=at_us, venue=venue)
    if row is None:
        return None, None
    return row["yes_bid"], row["yes_ask"]


def simulate_structure(
    archive: Any, cand: Candidate, model: FillModel, cfg: ValidationConfig,
) -> StructureOutcome:
    """Rest every leg, replay the tape, then price the residual both ways.

    THE ACCOUNTING, in cents, for a short basket resting at YES-prices p_i:

      matched = min_i filled_i        -- complete baskets, liability capped at $1
      target  = max_i filled_i        -- what crossing the laggards would reach

      FLATTEN   matched*margin
                + sum_i (filled_i - matched) * [(p_i - ask_i) - takerfee(ask_i)]
                - sum_i (filled_i - matched) * makerfee(p_i)
      COMPLETE  target*(sum p - 100)
                - sum_i [ filled_i*makerfee(p_i)
                        + (target - filled_i)*((p_i - bid_i) + takerfee(bid_i)) ]

    The strategy is credited with `max(FLATTEN, COMPLETE)`, i.e. with a perfectly
    prescient executor.  That is deliberate: if the edge dies even when the
    orphan is resolved optimally, no execution policy saves it.
    """
    n = cand.n_legs
    horizon_end = cand.decided_at_us + cfg.fill_horizon_us
    filled: list[float] = []
    firsts: list[int] = []
    sims: list[SimFill] = []
    for i in range(n):
        order = leg_order(cand, i)
        tape = tape_for(archive, cand.legs[i], cand.decided_at_us, horizon_end)
        sim: SimFill = simulate_maker_fill(order, tape, model,
                                           horizon_us=cfg.fill_horizon_us)
        filled.append(sim.filled_size)
        if sim.first_fill_at_us is not None:
            firsts.append(sim.first_fill_at_us)
            sim = with_markout(sim, markout_cents(
                archive, cand.legs[i], sim.first_fill_at_us, cand.prices_cents[i],
                horizon_us=cfg.markout_horizon_us, venue=cfg.venue,
            ))
        sims.append(sim)

    matched = min(filled) if filled else 0.0
    target = max(filled) if filled else 0.0
    touched = sum(1 for f in filled if f > 0.0)

    flatten: float | None = matched * cand.margin_cents
    orphan_costs: list[float] = []
    for i in range(n):
        over = filled[i] - matched
        _bid, ask = _unwind_book(archive, cand.legs[i], horizon_end, cfg.venue)
        if ask is None or not 1 <= ask <= 99:
            ask = cand.book_ask[i]
        if over <= 0.0:
            orphan_costs.append(0.0)
            continue
        if ask is None or not 1 <= ask <= 99:
            flatten = None                       # cannot price the exit at all
            orphan_costs.append(float("nan"))
            continue
        per = (ask - cand.prices_cents[i]) + cand.taker_fee_cents(float(ask))
        orphan_costs.append(per)
        if flatten is not None:
            flatten -= over * (per + cand.maker_fee_cents(cand.prices_cents[i]))

    complete: float | None = target * (cand.sum_px_cents - 100.0)
    for i in range(n):
        short_by = target - filled[i]
        complete -= filled[i] * cand.maker_fee_cents(cand.prices_cents[i])
        if short_by <= 0.0:
            continue
        bid, _ask = _unwind_book(archive, cand.legs[i], horizon_end, cfg.venue)
        if bid is None or bid < 1:
            bid = cand.book_bid[i]
        if bid is None or bid < 1:
            complete = None                      # no bid: the leg is not crossable
            break
        complete -= short_by * ((cand.prices_cents[i] - bid) + cand.taker_fee_cents(float(bid)))

    if target <= 0.0:
        net, resolution = 0.0, "none"
    elif flatten is None and complete is None:
        net, resolution = 0.0, "unresolvable"
    elif complete is None or (flatten is not None and flatten >= complete):
        net, resolution = float(flatten), "flatten"
    else:
        net, resolution = float(complete), "complete"

    return StructureOutcome(
        cohort=cand.cohort,
        model=model,
        event_ticker=cand.event_ticker,
        decided_at_us=cand.decided_at_us,
        n_legs=n,
        size=cand.size,
        margin_cents=cand.margin_cents,
        filled=tuple(filled),
        matched=matched,
        target=target,
        legs_touched=touched,
        net_cents=net,
        resolution=resolution,
        flatten_cents=flatten,
        complete_cents=complete,
        orphan_leg_cost_cents=tuple(orphan_costs),
        first_fill_at_us=min(firsts) if firsts else None,
        leg_fills=tuple(sims),
    )


def settled_pnl_cents(
    cand: Candidate, outcome: StructureOutcome, settlements: dict[str, tuple[int, int, bool]],
) -> float | None:
    """P&L if the filled legs are HELD to settlement instead of unwound.

    Per contract short YES on leg i: we collected p_i and pay 100 only if leg i
    resolves YES.  So pnl_i = filled_i * (p_i - 100*outcome_i) - maker fees.
    Returns None unless every leg of the structure has a non-voided settlement,
    because a partially-settled basket has an unknown winner and reporting it
    would be a guess dressed as a measurement.
    """
    total = 0.0
    for i, ticker in enumerate(cand.legs):
        s = settlements.get(ticker)
        if s is None or s[2]:
            return None
        total += outcome.filled[i] * (cand.prices_cents[i] - 100.0 * s[1])
        total -= outcome.filled[i] * cand.maker_fee_cents(cand.prices_cents[i])
    return total


# --------------------------------------------------------------------------- #
# Statistics -- always over STRUCTURES
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Interval:
    n: int
    mean: float
    sd: float
    low: float
    high: float

    @property
    def excludes_zero(self) -> bool:
        return self.n > 1 and (self.low > 0.0 or self.high < 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {"n": self.n, "mean": self.mean, "sd": self.sd,
                "low": self.low, "high": self.high,
                "excludes_zero": self.excludes_zero}


def mean_ci(samples: Sequence[float]) -> Interval:
    """Normal-approximation 95% CI of the mean.  n is the number of SAMPLES."""
    n = len(samples)
    if n == 0:
        return Interval(0, 0.0, 0.0, 0.0, 0.0)
    mean = sum(samples) / n
    if n == 1:
        return Interval(1, mean, 0.0, mean, mean)
    sd = statistics.stdev(samples)
    half = Z95 * sd / math.sqrt(n)
    return Interval(n, mean, sd, mean - half, mean + half)


def difference_ci(a: Sequence[float], b: Sequence[float]) -> Interval:
    """95% CI for mean(a) - mean(b), unequal variances (Welch standard error).

    This is the null-control test.  "Real beats control" is a claim about a
    DIFFERENCE, and a difference reported without its interval is an anecdote:
    with 69 real structures against 345 controls the sampling noise on the gap
    is larger than any margin S2 is designed to earn.
    """
    if len(a) < 2 or len(b) < 2:
        return Interval(min(len(a), len(b)), 0.0, 0.0, 0.0, 0.0)
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    diff = ma - mb
    return Interval(min(len(a), len(b)), diff, se, diff - Z95 * se, diff + Z95 * se)


@dataclass(frozen=True, slots=True)
class JointFillRow:
    bucket: str
    n_structures: int
    n_all: int
    n_any: int
    n_none: int

    @property
    def p_all(self) -> float:
        return self.n_all / self.n_structures if self.n_structures else 0.0

    @property
    def p_orphan(self) -> float:
        """P(some but not all legs fill), over ALL structures."""
        return (self.n_any - self.n_all) / self.n_structures if self.n_structures else 0.0

    @property
    def p_orphan_given_any(self) -> float:
        return (self.n_any - self.n_all) / self.n_any if self.n_any else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"bucket": self.bucket, "n_structures": self.n_structures,
                "n_all": self.n_all, "n_any": self.n_any, "n_none": self.n_none,
                "p_all": self.p_all, "p_orphan": self.p_orphan,
                "p_orphan_given_any": self.p_orphan_given_any}


def _bucket(n: int) -> str:
    return str(n) if n <= 4 else "5+"


def joint_fill_table(outcomes: Sequence[StructureOutcome]) -> tuple[JointFillRow, ...]:
    """P(all legs fill) and the orphan rate, bucketed by leg count.

    Bucketed because the quantity is mechanically n-dependent: n simultaneous
    maker fills is a conjunction, and reporting one pooled number over a mixed
    leg-count population hides that a 12-leg basket essentially never completes.
    """
    buckets: dict[str, list[StructureOutcome]] = {}
    for o in outcomes:
        buckets.setdefault(_bucket(o.n_legs), []).append(o)
    order = ["2", "3", "4", "5+"]
    rows: list[JointFillRow] = []
    for b in order:
        group = buckets.get(b)
        if not group:
            continue
        rows.append(JointFillRow(
            bucket=b,
            n_structures=len(group),
            n_all=sum(1 for o in group if o.all_filled),
            n_any=sum(1 for o in group if o.any_filled),
            n_none=sum(1 for o in group if not o.any_filled),
        ))
    if outcomes:
        rows.append(JointFillRow(
            bucket="all",
            n_structures=len(outcomes),
            n_all=sum(1 for o in outcomes if o.all_filled),
            n_any=sum(1 for o in outcomes if o.any_filled),
            n_none=sum(1 for o in outcomes if not o.any_filled),
        ))
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class OrphanStats:
    n_orphans: int
    n_priced_legs: int
    median_cross_cents: float | None
    mean_cross_cents: float | None
    p90_cross_cents: float | None
    median_margin_cents: float | None
    cross_over_margin: float | None
    n_flatten: int
    n_complete: int
    n_unresolvable: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_orphans": self.n_orphans,
            "n_priced_legs": self.n_priced_legs,
            "median_cross_cents": self.median_cross_cents,
            "mean_cross_cents": self.mean_cross_cents,
            "p90_cross_cents": self.p90_cross_cents,
            "median_margin_cents": self.median_margin_cents,
            "cross_over_margin": self.cross_over_margin,
            "n_flatten": self.n_flatten,
            "n_complete": self.n_complete,
            "n_unresolvable": self.n_unresolvable,
        }


def orphan_stats(outcomes: Sequence[StructureOutcome]) -> OrphanStats:
    """Cost to resolve a partial fill, against the margin it was opened for.

    `median_cross_cents` is per CONTRACT per LEG -- what it costs to cross one
    leg.  `median_margin_cents` is per BASKET.  The ratio is the number that
    decides the strategy: crossing one leg once must cost less than the margin
    the whole basket was designed to earn, or a single orphan erases many
    complete baskets.
    """
    orphans = [o for o in outcomes if o.orphaned]
    costs: list[float] = []
    for o in outcomes:
        for c in o.orphan_leg_cost_cents:
            if c != 0.0 and not math.isnan(c):
                costs.append(c)
    margins = [o.margin_cents for o in outcomes if o.n_legs > 0]
    med_cost = statistics.median(costs) if costs else None
    med_margin = statistics.median(margins) if margins else None
    # Only meaningful against a POSITIVE designed margin.  A control structure's
    # margin is negative by construction, and "cross costs -0.02x the margin"
    # is a division artefact, not a measurement.
    ratio = (med_cost / med_margin
             if (med_cost is not None and med_margin is not None and med_margin > 0.0)
             else None)
    return OrphanStats(
        n_orphans=len(orphans),
        n_priced_legs=len(costs),
        median_cross_cents=med_cost,
        mean_cross_cents=(sum(costs) / len(costs)) if costs else None,
        p90_cross_cents=(sorted(costs)[int(0.9 * (len(costs) - 1))] if costs else None),
        median_margin_cents=med_margin,
        cross_over_margin=ratio,
        n_flatten=sum(1 for o in outcomes if o.resolution == "flatten"),
        n_complete=sum(1 for o in outcomes if o.resolution == "complete"),
        n_unresolvable=sum(1 for o in outcomes if o.resolution == "unresolvable"),
    )


@dataclass(frozen=True, slots=True)
class FlowStats:
    """WHY a leg did or did not fill: flow against queue.  The causal story.

    Two independent ways a maker leg fails, and they need separating because
    they have different fixes:

      NO FLOW      nobody lifted the ask at all in the whole horizon.  Nothing
                   about order placement changes this -- the book is dead.
      QUEUED OUT   flow arrived but less of it than was displayed ahead of us.
                   This one is at least addressable, by quoting earlier or
                   accepting a worse price.

    `legs_flow_beats_queue` is the count where credited volume exceeded the
    displayed queue, i.e. where a fill was even arithmetically possible.
    """

    n_legs: int
    legs_zero_flow: int
    legs_flow_beats_queue: int
    median_credited: float | None
    median_queue_ahead: float | None
    median_order_size: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_legs": self.n_legs,
            "legs_zero_flow": self.legs_zero_flow,
            "legs_flow_beats_queue": self.legs_flow_beats_queue,
            "median_credited": self.median_credited,
            "median_queue_ahead": self.median_queue_ahead,
            "median_order_size": self.median_order_size,
        }


def flow_stats(outcomes: Sequence[StructureOutcome]) -> FlowStats:
    fills = [f for o in outcomes for f in o.leg_fills]
    credited = [f.volume_credited for f in fills]
    queues = [f.queue_ahead for f in fills]
    sizes = [float(o.size) for o in outcomes for _ in o.leg_fills]
    return FlowStats(
        n_legs=len(fills),
        legs_zero_flow=sum(1 for c in credited if c <= 0.0),
        legs_flow_beats_queue=sum(1 for f in fills
                                  if f.volume_credited > f.queue_ahead),
        median_credited=statistics.median(credited) if credited else None,
        median_queue_ahead=statistics.median(queues) if queues else None,
        median_order_size=statistics.median(sizes) if sizes else None,
    )


@dataclass(frozen=True, slots=True)
class IndependenceUnit:
    """The leg-vs-structure comparison.  Requirement 1, made numeric.

    `mechanical_leg_win_rate` is what a per-leg scorer reports on a COMPLETE
    short basket over a MECE set with a listed winner: n-1 of the n short legs
    pay $1 and one does not, so the "win rate" is sum(n_i - 1) / sum(n_i) and
    contains exactly zero information about profit.  `structure_win_rate` is the
    fraction of STRUCTURES with positive net P&L, which is the real quantity.

    `variance_inflation` is legs/structures: treating legs as independent
    shrinks the standard error by sqrt of it, so a leg-scored CI is about
    sqrt(legs/structures) times too narrow.  `leg_scored_ci` shows it by
    PSEUDO-REPLICATION -- attributing the structure's outcome to each of its
    legs, which is exactly what a per-ticker scorer does to a basket that
    resolves on one shared event.
    """

    n_structures: int
    n_legs: int
    legs_per_structure: float
    mechanical_leg_win_rate: float
    structure_win_rate: float
    variance_inflation: float
    honest_ci: Interval
    leg_scored_ci: Interval

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_structures": self.n_structures,
            "n_legs": self.n_legs,
            "legs_per_structure": self.legs_per_structure,
            "mechanical_leg_win_rate": self.mechanical_leg_win_rate,
            "structure_win_rate": self.structure_win_rate,
            "variance_inflation": self.variance_inflation,
            "honest_ci": self.honest_ci.as_dict(),
            "leg_scored_ci": self.leg_scored_ci.as_dict(),
        }


def independence_unit(outcomes: Sequence[StructureOutcome]) -> IndependenceUnit:
    live = [o for o in outcomes if o.any_filled]
    n_struct = len(live)
    n_legs = sum(o.n_legs for o in live)
    mech = (sum(o.n_legs - 1 for o in live) / n_legs) if n_legs else 0.0
    wins = sum(1 for o in live if o.net_cents > 0.0)
    per_structure = [o.net_cents for o in live]
    # The leg-scored fiction: one structure's outcome counted once per leg, as
    # a per-ticker scorer does when n legs settle on one shared event.  Same
    # mean at equal leg counts; standard error sqrt(legs/structures) too small.
    per_leg = [o.net_cents for o in live for _ in range(o.n_legs)]
    return IndependenceUnit(
        n_structures=n_struct,
        n_legs=n_legs,
        legs_per_structure=(n_legs / n_struct) if n_struct else 0.0,
        mechanical_leg_win_rate=mech,
        structure_win_rate=(wins / n_struct) if n_struct else 0.0,
        variance_inflation=(n_legs / n_struct) if n_struct else 0.0,
        honest_ci=mean_ci(per_structure),
        leg_scored_ci=mean_ci(per_leg),
    )


@dataclass(frozen=True, slots=True)
class ModelColumn:
    """One fill model's structure-level result for one cohort."""

    cohort: str
    model: FillModel
    n_structures: int
    n_complete: int
    n_orphan: int
    n_nofill: int
    matched_baskets: float
    designed_margin_cents: float
    net_cents: float
    per_structure: Interval
    per_structure_filled: Interval
    win_rate: float
    joint_fill: tuple[JointFillRow, ...]
    orphans: OrphanStats
    independence: IndependenceUnit
    adverse: AdverseFillCheck
    flow: FlowStats
    #: the raw per-structure net P&L samples, kept so the control comparison is
    #: a difference of SAMPLES rather than a difference of two rounded means.
    net_samples: tuple[float, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "cohort": self.cohort,
            "model": self.model.value,
            "n_structures": self.n_structures,
            "n_complete": self.n_complete,
            "n_orphan": self.n_orphan,
            "n_nofill": self.n_nofill,
            "matched_baskets": self.matched_baskets,
            "designed_margin_cents": self.designed_margin_cents,
            "net_cents": self.net_cents,
            "per_structure": self.per_structure.as_dict(),
            "per_structure_filled": self.per_structure_filled.as_dict(),
            "win_rate": self.win_rate,
            "joint_fill": [r.as_dict() for r in self.joint_fill],
            "orphans": self.orphans.as_dict(),
            "independence": self.independence.as_dict(),
            "adverse_rate": self.adverse.rate,
            "adverse_n": self.adverse.n,
            "adverse_passed": self.adverse.passed,
            "adverse_verdict": self.adverse.verdict,
            "flow": self.flow.as_dict(),
        }


def summarise(cohort: str, model: FillModel,
              outcomes: Sequence[StructureOutcome]) -> ModelColumn:
    live = [o for o in outcomes if o.any_filled]
    every_fill = [f for o in outcomes for f in o.leg_fills]
    return ModelColumn(
        cohort=cohort,
        model=model,
        n_structures=len(outcomes),
        n_complete=sum(1 for o in outcomes if o.all_filled),
        n_orphan=sum(1 for o in outcomes if o.orphaned),
        n_nofill=sum(1 for o in outcomes if not o.any_filled),
        matched_baskets=sum(o.matched for o in outcomes),
        designed_margin_cents=sum(o.matched * o.margin_cents for o in outcomes),
        net_cents=sum(o.net_cents for o in outcomes),
        per_structure=mean_ci([o.net_cents for o in outcomes]),
        per_structure_filled=mean_ci([o.net_cents for o in live]),
        win_rate=(sum(1 for o in live if o.net_cents > 0.0) / len(live)) if live else 0.0,
        joint_fill=joint_fill_table(outcomes),
        orphans=orphan_stats(outcomes),
        independence=independence_unit(outcomes),
        adverse=adverse_fill_gate(every_fill, band=ADVERSE_FILL_BAND),
        flow=flow_stats(outcomes),
        net_samples=tuple(o.net_cents for o in outcomes),
    )


# --------------------------------------------------------------------------- #
# The sum(bid) census -- an independent cross-check of the whole premise
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BidCensus:
    """How many flagged-MECE books can be shorted by CROSSING, net of fees.

    This is the premise check.  A maker short only ever earns more than a taker
    short; if no book anywhere is short-profitable even at the bid, the entire
    edge rests on maker fills happening, which is precisely what the joint-fill
    measurement then has to establish.
    """

    at_us: int
    n_events: int
    n_sum_bid_gt_1: int
    n_taker_profitable: int
    n_maker_profitable: int
    n_liquid: int
    n_maker_profitable_liquid: int
    median_sum_bid: float | None
    median_sum_ask: float | None
    median_maker_margin_cents: float | None
    median_maker_margin_liquid_cents: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at_us": self.at_us, "n_events": self.n_events,
            "n_sum_bid_gt_1": self.n_sum_bid_gt_1,
            "n_taker_profitable": self.n_taker_profitable,
            "n_maker_profitable": self.n_maker_profitable,
            "n_liquid": self.n_liquid,
            "n_maker_profitable_liquid": self.n_maker_profitable_liquid,
            "median_sum_bid": self.median_sum_bid,
            "median_sum_ask": self.median_sum_ask,
            "median_maker_margin_cents": self.median_maker_margin_cents,
            "median_maker_margin_liquid_cents": self.median_maker_margin_liquid_cents,
        }


def bid_census(archive: Any, at_us: int, cfg: ValidationConfig) -> BidCensus:
    """sum(bid) and sum(rest-ask) net of fees over every flagged-MECE event.

    TAKER short: hit every bid.  Proceeds sum(bid_i), liability at most $1, and
    a taker fee at each bid.  Profitable iff sum(bid) - 1 - sum(takerfee) > 0.

    MAKER short: rest at `rest_price_short_cents` (ask - 1 tick when the spread
    allows, else join the ask).  Profitable iff sum(px) - 1 - sum(makerfee) > 0,
    and on the plain `quadratic` fee type that 13,385 of 13,518 series carry the
    maker fee is exactly zero, so the hurdle is sum(px) > 1.
    """
    book = book_as_of(archive, at_us, venue=cfg.venue)
    events = events_as_of(archive, at_us, venue=cfg.venue)
    sleeve = S2ShortBasket(cfg=cfg.s2)
    by_event: dict[str, list[Market]] = {}
    for ticker in sorted(book):
        m, _ = book[ticker]
        if m.event_ticker:
            by_event.setdefault(m.event_ticker, []).append(m)

    series_cache: dict[str, Series | None] = {}
    sum_bids: list[float] = []
    sum_asks: list[float] = []
    maker_margins: list[float] = []
    liquid_margins: list[float] = []
    n = n_gt1 = n_taker = n_maker = n_liquid = n_maker_liquid = 0
    for et in sorted(by_event):
        e = events.get(et)
        if e is None or not e.mutually_exclusive:
            continue
        legs = [m for m in by_event[et] if m.has_two_sided_quote]
        if len(legs) < cfg.s2.min_legs:
            continue
        n += 1
        series = _series_for_legs(archive, legs, series_cache)
        snap = _snapshot_for(e, legs, at_us, series, cfg.bankroll_cents)
        spec = sleeve.fee_spec(legs, snap)
        bids = [m.yes_bid or 0 for m in legs]
        asks = [m.yes_ask or 0 for m in legs]
        px = [rest_price_short_cents(m) for m in legs]
        sb, sa = sum(bids) / 100.0, sum(asks) / 100.0
        sum_bids.append(sb)
        sum_asks.append(sa)
        if sb > 1.0:
            n_gt1 += 1
        taker_fees = sum(fee(min(max(b / 100.0, 0.01), 0.99), spec, is_maker=False)
                         for b in bids)
        if sb - 1.0 - taker_fees > 0.0:
            n_taker += 1
        maker_fees = sum(fee(p / 100.0, spec, is_maker=True) for p in px)
        margin = sum(px) / 100.0 - 1.0 - maker_fees
        maker_margins.append(100.0 * margin)
        if margin >= cfg.s2.min_margin:
            n_maker += 1
        # research/05 4.3: the liquidity filter is what took 3,793 maker
        # "opportunities" down to 504.  Reporting the unfiltered count alone is
        # the naive scan that called 78% of MECE events profitable.
        liquid = all(
            m.yes_bid_size >= cfg.s2.min_leg_depth
            and (m.spread_cents or 99) <= cfg.s2.max_leg_spread_cents
            and (m.hours_to_close(now=at_us) or -1.0) >= cfg.s2.min_hours_to_close
            and m.volume_24h >= cfg.s2.min_volume_24h
            for m in legs
        )
        if liquid:
            n_liquid += 1
            liquid_margins.append(100.0 * margin)
            if margin >= cfg.s2.min_margin:
                n_maker_liquid += 1
    return BidCensus(
        at_us=at_us, n_events=n, n_sum_bid_gt_1=n_gt1,
        n_taker_profitable=n_taker, n_maker_profitable=n_maker,
        n_liquid=n_liquid, n_maker_profitable_liquid=n_maker_liquid,
        median_sum_bid=statistics.median(sum_bids) if sum_bids else None,
        median_sum_ask=statistics.median(sum_asks) if sum_asks else None,
        median_maker_margin_cents=(statistics.median(maker_margins)
                                   if maker_margins else None),
        median_maker_margin_liquid_cents=(statistics.median(liquid_margins)
                                          if liquid_margins else None),
    )


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ValidationReport:
    config: dict[str, Any]
    tape_lo: int | None
    tape_hi: int | None
    n_trades: int
    scans: tuple[ScanStats, ...]
    census: BidCensus | None
    real: dict[FillModel, ModelColumn]
    control: dict[FillModel, ModelColumn]
    settled: tuple[int, int, float]        # (structures priced, legs settled, cents)
    n_settlements_archive: int
    warnings: tuple[str, ...]

    # ------------------------------------------------------------- the verdict
    def verdict(self) -> tuple[str, tuple[str, ...]]:
        """One of four answers, read from the PESSIMISTIC column (R6.7a).

            EDGE SUPPORTED       positive CI over structures AND beats the null
            EDGE REFUTED         the CI excludes zero on the LOSING side
            NO EDGE DEMONSTRATED enough structures, interval covers zero
            UNDECIDABLE          the archive cannot answer the question

        UNDECIDABLE is a first-class answer.  A harness that cannot say "the
        data does not support a conclusion" will always find one.

        The sample size that governs is the number of STRUCTURES, not the
        number that filled: a structure whose legs never filled returns exactly
        zero and is a real observation of the strategy, not missing data.  The
        count of FILLED structures is reported separately because it is what
        bounds any statement about orphan cost or adverse selection.
        """
        reasons: list[str] = []
        pess = self.real.get(FillModel.PESSIMISTIC)
        real = self.real.get(FillModel.REALISTIC)
        if pess is None or pess.n_structures == 0:
            return "UNDECIDABLE", ("no candidate structures inside the tape window",)
        n = pess.n_structures
        live = pess.independence.n_structures
        if live < 30:
            reasons.append(
                f"only {live} of {n} structure(s) saw ANY fill under pessimistic "
                f"fills, so orphan cost and adverse selection are measured on a "
                f"sample too small to carry a conclusion of their own"
            )
        for label, col in (("pessimistic", pess), ("realistic", real)):
            if col is None:
                continue
            ci = col.per_structure
            if ci.excludes_zero and ci.low > 0.0:
                reasons.append(f"{label}: per-structure CI is positive and excludes zero")
            elif ci.excludes_zero:
                reasons.append(f"{label}: per-structure CI EXCLUDES ZERO ON THE LOSING SIDE")
            else:
                reasons.append(
                    f"{label}: per-structure net "
                    f"[{ci.low:.3f}, {ci.high:.3f}] cents includes zero"
                )
        ctrl = self.control.get(FillModel.PESSIMISTIC)
        beats_control = False
        if ctrl is not None and ctrl.n_structures:
            d = difference_ci(pess.net_samples, ctrl.net_samples)
            beats_control = d.excludes_zero and d.low > 0.0
            reasons.append(
                f"real minus control (pessimistic): {d.mean:+.3f} cents per structure, "
                f"95% CI [{d.low:+.3f}, {d.high:+.3f}] -- "
                + ("the real cohort beats the null" if beats_control
                   else "indistinguishable from the null")
            )
        else:
            reasons.append("no control cohort could be built: the comparison is missing")
        if not pess.adverse.passed:
            reasons.append(f"adverse-fill gate (R6.7c): {pess.adverse.verdict}")

        ci = pess.per_structure
        if n < 30:
            reasons.append(
                f"only {n} candidate structure(s) in total; 30 is the bare minimum "
                f"for a mean and an interval to mean anything"
            )
            return "UNDECIDABLE", tuple(reasons)
        if ci.excludes_zero and ci.low > 0.0 and beats_control:
            return "EDGE SUPPORTED", tuple(reasons)
        if ci.excludes_zero and ci.high < 0.0:
            return "EDGE REFUTED", tuple(reasons)
        return "NO EDGE DEMONSTRATED", tuple(reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "tape_lo": self.tape_lo,
            "tape_hi": self.tape_hi,
            "n_trades": self.n_trades,
            "scans": [s.as_dict() for s in self.scans],
            "census": self.census.as_dict() if self.census else None,
            "real": {m.value: c.as_dict() for m, c in self.real.items()},
            "control": {m.value: c.as_dict() for m, c in self.control.items()},
            "settled": list(self.settled),
            "n_settlements_archive": self.n_settlements_archive,
            "warnings": list(self.warnings),
            "verdict": self.verdict()[0],
        }

    # -------------------------------------------------------------- rendering
    def report(self) -> str:
        out: list[str] = []
        add = out.append
        add("=" * 78)
        add("S2 SHORT-BASKET VALIDATION -- structure-level, three fill models")
        add("=" * 78)

        add("")
        add("0. WHAT THE ARCHIVE CAN SUPPORT")
        add("-" * 78)
        if self.tape_lo is None:
            add("  no trades recorded: fills cannot be measured at all")
        else:
            span_h = (self.tape_hi - self.tape_lo) / HOUR_US
            add(f"  tape: {self.n_trades} prints over {span_h:.2f} h "
                f"[{self.tape_lo} .. {self.tape_hi}]")
        add(f"  decision instants used: {len(self.scans)} "
            f"(only sweeps INSIDE the tape window are eligible)")
        for s in self.scans:
            add(f"    t={s.decided_at_us}  forward tape {s.forward_tape_us / 60_000_000:.1f} min"
                f"  ME events {s.events_flagged_me}"
                f"  with fresh book {s.events_with_book}"
                f"  dropped stale {s.events_dropped_stale}"
                f"  >= {self.config['min_legs']} two-sided legs {s.events_two_sided_ge_min}"
                f"  PASSED gate {s.passed_gate}  failed margin {s.failed_margin}")
        for w in self.warnings:
            add(f"  WARNING: {w}")

        if self.census is not None:
            c = self.census
            add("")
            add("1. THE PREMISE -- is any MECE book short-profitable at all?")
            add("-" * 78)
            add(f"  at t={c.at_us}, {c.n_events} flagged-MECE events with "
                f">= {self.config['min_legs']} two-sided legs")
            add(f"  median sum(YES bid) = {c.median_sum_bid}   "
                f"median sum(YES ask) = {c.median_sum_ask}")
            add(f"  sum(bid) > 1.00 (before fees) : {c.n_sum_bid_gt_1}"
                f"  ({100.0 * c.n_sum_bid_gt_1 / c.n_events:.2f}%)"
                if c.n_events else "  sum(bid) > 1.00: n/a")
            add(f"  TAKER short profitable, sum(bid) - 1 - sum(taker fee) > 0 : "
                f"{c.n_taker_profitable}")
            add(f"  MAKER short clears margin gate ({self.config['min_margin']:.3f} $/basket) : "
                f"{c.n_maker_profitable}   <- BEFORE the liquidity filter")
            add(f"  ... of which pass depth/spread/volume/time filters : "
                f"{c.n_maker_profitable_liquid}   (liquid books at all: {c.n_liquid})")
            if c.median_maker_margin_cents is not None:
                add(f"  median maker margin: {c.median_maker_margin_cents:.3f} c/basket "
                    f"unfiltered, "
                    + (f"{c.median_maker_margin_liquid_cents:.3f} c/basket among liquid"
                       if c.median_maker_margin_liquid_cents is not None else "n/a liquid"))
            add("  derivation: maker fee = theta*p*(1-p) with theta = 0 on the plain")
            add("  'quadratic' fee type, so the maker hurdle is sum(rest px) > 1 exactly.")
            add("  The gap between the two MAKER lines is the whole of research/05 4.3:")
            add("  a scan that skips the liquidity filter is pricing fills nobody offers.")

        for name, cols in (("REAL (passed the margin gate)", self.real),
                           ("CONTROL (random legs, failed the margin gate)", self.control)):
            add("")
            add(f"2. JOINT FILL PROBABILITY -- {name}")
            add("-" * 78)
            if not cols:
                add("  no structures")
                continue
            for model in ALL_MODELS:
                col = cols.get(model)
                if col is None or col.n_structures == 0:
                    continue
                add(f"  {model.value}:")
                add(f"    {'legs':>6} {'structs':>9} {'P(all fill)':>13} "
                    f"{'P(orphan)':>11} {'P(orphan|any)':>15}")
                for r in col.joint_fill:
                    add(f"    {r.bucket:>6} {r.n_structures:>9d} {r.p_all:>13.4f} "
                        f"{r.p_orphan:>11.4f} {r.p_orphan_given_any:>15.4f}")

        add("")
        add("2b. WHY THE LEGS DO NOT FILL -- taker flow against the displayed queue")
        add("-" * 78)
        add(f"  {'cohort/model':<26}{'legs':>7}{'no flow':>10}{'flow>queue':>12}"
            f"{'med flow':>11}{'med queue':>11}{'med size':>10}")
        for name, cols in (("real", self.real), ("control", self.control)):
            for model in ALL_MODELS:
                col = cols.get(model)
                if col is None or col.flow.n_legs == 0:
                    continue
                f = col.flow
                add(f"  {name + '/' + model.value:<26}{f.n_legs:>7d}"
                    f"{f.legs_zero_flow:>10d}{f.legs_flow_beats_queue:>12d}"
                    f"{(f.median_credited or 0.0):>11.1f}"
                    f"{(f.median_queue_ahead or 0.0):>11.1f}"
                    f"{(f.median_order_size or 0.0):>10.1f}")
        add("  'no flow' legs are dead books: no order placement fixes them.")
        add("  'flow>queue' is where a fill was even arithmetically possible.")

        add("")
        add("3. ORPHAN COST -- cents to cross ONE leg vs the whole basket's margin")
        add("-" * 78)
        for name, cols in (("real", self.real), ("control", self.control)):
            for model in ALL_MODELS:
                col = cols.get(model)
                if col is None or col.n_structures == 0:
                    continue
                o = col.orphans
                if o.n_priced_legs == 0:
                    add(f"  {name}/{model.value}: no orphan legs to price "
                        f"({col.n_orphan} orphan structures)")
                    continue
                add(f"  {name}/{model.value}: {o.n_orphans} orphan structures, "
                    f"{o.n_priced_legs} orphan legs priced")
                add(f"    cross one leg: median {o.median_cross_cents:.3f}c  "
                    f"mean {o.mean_cross_cents:.3f}c  p90 {o.p90_cross_cents:.3f}c")
                if o.cross_over_margin is not None:
                    add(f"    basket margin: median {o.median_margin_cents:.3f}c  "
                        f"-> one cross costs {o.cross_over_margin:.2f}x the whole margin")
                else:
                    add(f"    basket margin: median {o.median_margin_cents:.3f}c "
                        f"(not positive: the ratio is undefined for this cohort)")
                add(f"    resolution chosen: flatten {o.n_flatten}, "
                    f"complete {o.n_complete}, unresolvable {o.n_unresolvable}")
        add("  derivation, per contract on an orphaned leg we are SHORT yes at p:")
        add("    cost = (ask_at_unwind - p) + 100*fee(ask/100, taker)")
        add("  read from the point-in-time book at decision time + rest horizon.")

        add("")
        add("4. NET P&L PER STRUCTURE (cents), after fees -- THE BRACKET")
        add("-" * 78)
        add(f"  {'cohort':<9}{'model':<14}{'structs':>8}{'complete':>9}{'orphan':>8}"
            f"{'nofill':>8}{'locked c':>11}{'orphan c':>11}{'net c':>11}"
            f"{'mean c':>10}{'CI low':>10}{'CI high':>10}")
        for name, cols in (("real", self.real), ("control", self.control)):
            for model in ALL_MODELS:
                col = cols.get(model)
                if col is None:
                    continue
                ci = col.per_structure
                add(f"  {name:<9}{model.value:<14}{col.n_structures:>8d}"
                    f"{col.n_complete:>9d}{col.n_orphan:>8d}{col.n_nofill:>8d}"
                    f"{col.designed_margin_cents:>11.2f}"
                    f"{col.net_cents - col.designed_margin_cents:>11.2f}"
                    f"{col.net_cents:>11.2f}{ci.mean:>10.4f}"
                    f"{ci.low:>10.4f}{ci.high:>10.4f}")
        add("  gate decisions read the PESSIMISTIC row only (PLAN.md R6.7a).")
        add("  net = 'locked' + 'orphan', exactly:")
        add("    locked = sum over structures of matched_baskets * designed margin")
        add("    orphan = the residual resolved at max(FLATTEN, COMPLETE), i.e. the")
        add("             strategy is credited with the BETTER remedy every time.")
        add("  matched_baskets = min over legs of filled contracts: only that many")
        add("  baskets are actually locked, however much any single leg filled.")
        add("")
        add("  4b. REAL MINUS CONTROL -- the null test, with its interval")
        for model in ALL_MODELS:
            r, c = self.real.get(model), self.control.get(model)
            if r is None or c is None or not r.n_structures or not c.n_structures:
                continue
            d = difference_ci(r.net_samples, c.net_samples)
            add(f"    {model.value:<12} {d.mean:+9.4f} c/structure  "
                f"95% CI [{d.low:+9.4f}, {d.high:+9.4f}]  "
                + ("BEATS NULL" if (d.excludes_zero and d.low > 0.0) else "no edge"))
        add("")
        add("  4c. ADVERSE SELECTION (R6.7c) -- realized maker fills run 66-89% adverse")
        for name, cols in (("real", self.real), ("control", self.control)):
            for model in ALL_MODELS:
                col = cols.get(model)
                if col is None or col.n_structures == 0:
                    continue
                a = col.adverse
                add(f"    {name}/{model.value:<12} rate {a.rate:.3f} (n={a.n})  "
                    f"{'PASS' if a.passed else 'FAIL'}: {a.verdict}")

        add("")
        add("5. THE INDEPENDENCE UNIT -- why per-leg scoring manufactures a win rate")
        add("-" * 78)
        for name, cols in (("real", self.real), ("control", self.control)):
            for model in ALL_MODELS:
                col = cols.get(model)
                if col is None or col.independence.n_structures == 0:
                    continue
                u = col.independence
                add(f"  {name}/{model.value}: {u.n_structures} structures, "
                    f"{u.n_legs} legs ({u.legs_per_structure:.2f} legs/structure)")
                add(f"    per-leg 'win rate' forced by mutual exclusivity: "
                    f"{u.mechanical_leg_win_rate:.4f}   "
                    f"(sum(n-1)/sum(n): arithmetic, not skill)")
                add(f"    STRUCTURE win rate (net > 0):        "
                    f"{u.structure_win_rate:.4f}")
                add(f"    honest CI over structures : "
                    f"[{u.honest_ci.low:.4f}, {u.honest_ci.high:.4f}] "
                    f"width {u.honest_ci.high - u.honest_ci.low:.4f}")
                add(f"    leg-scored CI (WRONG)     : "
                    f"[{u.leg_scored_ci.low:.4f}, {u.leg_scored_ci.high:.4f}] "
                    f"width {u.leg_scored_ci.high - u.leg_scored_ci.low:.4f}")
                add(f"    variance inflation legs/structures = {u.variance_inflation:.2f}, "
                    f"so a leg-scored CI is ~{math.sqrt(u.variance_inflation):.2f}x too narrow")

        add("")
        add("6. SETTLEMENT-BASED P&L (held to settlement rather than unwound)")
        add("-" * 78)
        n_priced, n_legs_settled, cents = self.settled
        add(f"  settlements recorded anywhere in the archive: {self.n_settlements_archive}")
        add(f"  legs OF THE SCANNED STRUCTURES that have one: {n_legs_settled}")
        add(f"  structures whose EVERY leg has a non-voided settlement: {n_priced}")
        if n_priced:
            add(f"  total settled P&L: {cents:.2f} cents")
        else:
            add("  CANNOT BE COMPUTED on this archive.  A short basket's P&L is only")
            add("  realised when its event resolves; with no fully-settled structure")
            add("  there is no realised return to report, and any number claiming to")
            add("  be one would be a mark, not a settlement.")

        add("")
        add("7. VERDICT")
        add("-" * 78)
        v, reasons = self.verdict()
        add(f"  {v}")
        for r in reasons:
            add(f"    - {r}")
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def _iter_outcomes(archive: Any, cands: Iterable[Candidate], model: FillModel,
                   cfg: ValidationConfig) -> Iterator[StructureOutcome]:
    for c in cands:
        yield simulate_structure(archive, c, model, cfg)


def run_validation(archive: Any, cfg: ValidationConfig | None = None) -> ValidationReport:
    """Scan, simulate, score, control.  The whole harness in one call."""
    cfg = cfg or ValidationConfig()
    lo, hi, n_trades = tape_window(archive)
    warnings: list[str] = []

    times = choose_decision_times(archive, cfg)
    if not times:
        warnings.append(
            "no snapshot sweep lies inside the recorded tape window, so no order "
            "can have its fills measured; every fill rate below would be censoring"
        )
    if lo is not None and hi is not None:
        sweeps = [t for t, _ in sweep_times(archive, cfg)]
        outside = [t for t in sweeps if t < lo]
        if outside:
            warnings.append(
                f"{len(outside)} snapshot sweep(s) predate the first recorded print "
                f"by up to {(lo - min(outside)) / HOUR_US:.2f} h -- orders placed there "
                f"would report zero fills because the RECORDER was silent, not the market"
            )

    real: list[Candidate] = []
    pool: list[Candidate] = []
    scans: list[ScanStats] = []
    for t in times:
        r, p, st = scan(archive, t, cfg, tape_end_us=hi if hi is not None else t)
        real.extend(r)
        pool.extend(p)
        scans.append(st)
        if st.forward_tape_us < cfg.fill_horizon_us:
            warnings.append(
                f"decision at {t} has only {st.forward_tape_us / 60_000_000:.1f} min of "
                f"forward tape against a {cfg.fill_horizon_us / 60_000_000:.1f} min rest "
                f"horizon: fills are TRUNCATED, so P(all fill) is a lower bound"
            )

    control = build_control(archive, pool, real, cfg)

    real_cols: dict[FillModel, ModelColumn] = {}
    ctrl_cols: dict[FillModel, ModelColumn] = {}
    settled_map = settlements_map(archive, venue=cfg.venue)
    settled_priced = 0
    settled_cents = 0.0
    for model in ALL_MODELS:
        r_out = list(_iter_outcomes(archive, real, model, cfg))
        c_out = list(_iter_outcomes(archive, control, model, cfg))
        real_cols[model] = summarise("real", model, r_out)
        ctrl_cols[model] = summarise("control", model, c_out)
        if model is FillModel.PESSIMISTIC:
            for cand, out in zip(real, r_out, strict=True):
                pnl = settled_pnl_cents(cand, out, settled_map)
                if pnl is not None:
                    settled_priced += 1
                    settled_cents += pnl

    n_legs_settled = sum(1 for c in real for t in c.legs if t in settled_map)
    census = bid_census(archive, times[-1], cfg) if times else None
    if not real:
        warnings.append(
            "zero structures passed the margin gate at the eligible decision "
            "instants; the P&L columns below are empty by construction"
        )
    if real and not control:
        warnings.append("control cohort is empty: the null comparison is unavailable")

    return ValidationReport(
        config=cfg.as_dict(),
        tape_lo=lo, tape_hi=hi, n_trades=n_trades,
        scans=tuple(scans), census=census,
        real=real_cols, control=ctrl_cols,
        settled=(settled_priced, n_legs_settled, settled_cents),
        n_settlements_archive=len(settled_map),
        warnings=tuple(warnings),
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="data/pm.db", help="archive path (opened READ-ONLY)")
    ap.add_argument("--horizon-minutes", type=float, default=15.0)
    ap.add_argument("--max-decision-times", type=int, default=6)
    ap.add_argument("--staleness-minutes", type=float, default=15.0,
                    help="drop a leg whose book is older than this; 0 disables")
    ap.add_argument("--control-multiple", type=int, default=5)
    ap.add_argument("--at", type=int, action="append", default=None,
                    help="explicit decision instant (us); repeatable")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the report")
    args = ap.parse_args(argv)

    cfg = ValidationConfig(
        decision_times=tuple(args.at or ()),
        max_decision_times=args.max_decision_times,
        max_book_staleness_us=(None if args.staleness_minutes <= 0
                               else int(args.staleness_minutes * MINUTE_US)),
        fill_horizon_us=int(args.horizon_minutes * MINUTE_US),
        control_multiple=args.control_multiple,
    )
    with ReadOnlyArchive(args.db) as archive:
        rep = run_validation(archive, cfg)
    if args.json:
        print(json.dumps(rep.as_dict(), indent=2, sort_keys=True))
    else:
        print(rep.report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
