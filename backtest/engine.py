"""Event-driven replay over recorded snapshots and the public tape.  T-030.

WHAT THIS IS AND IS NOT
-----------------------
It is a replay of `market_snapshots` and `trades` (core/db.py) that runs the
IDENTICAL sleeve code path as shadow and live (strategy/base.py C4.2a), feeds it
MarketSnapshot objects built ONLY from point-in-time data, and reports the
result under all three fill models side by side (PLAN.md 6.7).

It is NOT a P&L forecast.  R6.7a: gate-promotion decisions read the PESSIMISTIC
column and nothing else.  The other two columns exist to show how wide the
fill-model bracket is -- ~3.9x on real data -- so that a result which survives
only at the optimistic end is visibly worthless rather than quietly reported.

DETERMINISM (the T-030 acceptance criterion)
--------------------------------------------
Same inputs -> byte-identical `digest()`.  Concretely that forbids, and this
module contains none of: `now_us()` (time comes from the snapshot), `uuid4()`
(order ids are positional), unseeded randomness, set iteration, and reliance on
SQLite's unordered row delivery (every query carries an ORDER BY).

FEES ARE PER VENUE **AND** PER ERA
----------------------------------
Kalshi fees are per SERIES (13,353 of 13,486 series charge makers zero; 130
charge 0.25x base; 3 charge 0.50x -- core/math/contracts.py), so the default fee
comes from the series cache.  But a fee SCHEDULE also changes over time, and
PLAN.md 6.7 is explicit that pre-2026 Polymarket data is a zero-fee world which
must not be used to justify a 2026 strategy.  `FeeSchedule` lets a run pin the
regime in force at trade time; when it does, it overrides the series cache,
because the cached series row describes TODAY's regime, not the tape's.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from backtest.fills import (
    ALL_MODELS,
    AdverseFillCheck,
    FillModel,
    RestingOrder,
    SimFill,
    TapeTrade,
    adverse_fill_gate,
    assert_ordering,
    simulate_maker_all,
    with_markout,
)
from core.db import Database
from core.math.contracts import FeeSpec, fee
from core.models import Event, Market, Series, SettlementSource, Side, Venue
from strategy.base import MarketSnapshot, Sleeve

HOUR_US = 3_600_000_000
MINUTE_US = 60_000_000


# --------------------------------------------------------------------------- #
# Fees: per venue AND per era
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FeeEra:
    """The fee regime a venue was operating under from `effective_from_us`."""

    venue: str
    effective_from_us: int
    spec: FeeSpec
    label: str = ""


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Era overrides, most recent applicable era wins.

    Empty (the default) means "trust the per-series cache", which is right for
    Kalshi where the fee genuinely is a per-series property.  Populate it when
    replaying a tape from a different regime -- that is the whole point of
    modelling fees per era.
    """

    eras: tuple[FeeEra, ...] = ()

    def spec_for(
        self, *, venue: str, at_us: int, series: Series | None, default: FeeSpec
    ) -> FeeSpec:
        best: FeeEra | None = None
        for era in self.eras:
            if era.venue != venue or era.effective_from_us > at_us:
                continue
            if best is None or era.effective_from_us > best.effective_from_us:
                best = era
        if best is not None:
            return best.spec
        if series is not None:
            return series.fee_spec
        return default


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    venue: str = Venue.KALSHI.value
    start_us: int | None = None
    end_us: int | None = None
    #: minimum spacing between decision points.  0 = decide on every recorded
    #: observation.  Raising it thins the timeline; it never adds information.
    min_step_us: int = 0
    max_steps: int | None = None
    bankroll_cents: int = 1_000_000
    #: how long a resting order is left in the book before it is treated as
    #: cancelled.  R6.7d: a cancel is CENSORING, not a non-fill -- what we report
    #: is how much of this horizon's flow reached us, never "it did not fill".
    fill_horizon_us: int = 6 * HOUR_US
    #: mark-out horizon for the R6.7c adverse-fill gate (KPI 3 uses 5m and 1h).
    markout_horizon_us: int = 5 * MINUTE_US
    fees: FeeSchedule = FeeSchedule()
    default_fee_spec: FeeSpec = FeeSpec.kalshi("quadratic", 1.0)
    #: NEVER set False in a real run.  It exists so backtest/leakage.py can prove
    #: that dropping voided/delisted markets changes the answer, which is why
    #: PLAN.md 6.7 lists their inclusion as a leakage checklist item.
    include_voided: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "min_step_us": self.min_step_us,
            "max_steps": self.max_steps,
            "bankroll_cents": self.bankroll_cents,
            "fill_horizon_us": self.fill_horizon_us,
            "markout_horizon_us": self.markout_horizon_us,
            "include_voided": self.include_voided,
            "default_fee_spec": [
                self.default_fee_spec.venue,
                self.default_fee_spec.fee_type,
                self.default_fee_spec.fee_multiplier,
            ],
            "eras": [
                [e.venue, e.effective_from_us, e.spec.venue, e.spec.fee_type,
                 e.spec.fee_multiplier, e.label]
                for e in sorted(self.fees.eras, key=lambda e: (e.venue, e.effective_from_us))
            ],
        }


# --------------------------------------------------------------------------- #
# Point-in-time reader.  R5a: read the latest row with observed_at_us <= t.
# --------------------------------------------------------------------------- #


def _market_from_row(row: sqlite3.Row) -> Market:
    try:
        venue = Venue(row["venue"])
    except ValueError:                       # a venue we do not model yet
        venue = Venue.KALSHI
    return Market(
        venue=venue,
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
    try:
        venue = Venue(row["venue"])
    except ValueError:
        venue = Venue.KALSHI
    raw = json.loads(row["settlement_sources_json"] or "[]")
    return Event(
        venue=venue,
        event_ticker=row["event_ticker"],
        series_ticker=row["series_ticker"] or "",
        category=row["category"] or "",
        title=row["title"] or "",
        mutually_exclusive=bool(row["mutually_exclusive"]),
        collateral_return_type=row["collateral_return_type"] or "",
        settlement_sources=tuple(SettlementSource(**s) for s in raw),
        exhaustive_verified=bool(row["exhaustive_verified"]),
    )


@dataclass
class SnapshotReader:
    """Every market read goes through `Database.latest_market(as_of_us=...)`.

    That is deliberate and is what backtest/leakage.py asserts on: it is THE
    anti-look-ahead accessor (core/db.py), and a reader that hand-rolls its own
    SQL is a reader nobody can audit.  It costs one indexed lookup per ticker
    per step, which the `ix_ms_ticker` covering index makes cheap.
    """

    db: Database
    venue: str = Venue.KALSHI.value
    include_voided: bool = True
    _tickers: tuple[str, ...] | None = field(default=None, init=False, repr=False)

    def tickers(self) -> tuple[str, ...]:
        """Every ticker ever observed on this venue, sorted.

        Sorted because dict/row order must never reach a result (T-030), and
        UNFILTERED by status because silently dropping voided or delisted
        markets is itself a leakage failure (PLAN.md 6.7 leakage checklist) --
        survivorship is look-ahead wearing a different hat.
        """
        if self._tickers is None:
            rows = self.db.conn.execute(
                "SELECT DISTINCT ticker FROM market_snapshots WHERE venue = ? "
                "ORDER BY ticker",
                (self.venue,),
            ).fetchall()
            names = [r["ticker"] for r in rows]
            if not self.include_voided:
                dropped = self._voided_tickers()
                names = [t for t in names if t not in dropped]
            self._tickers = tuple(names)
        return self._tickers

    def _voided_tickers(self) -> frozenset[str]:
        rows = self.db.conn.execute(
            """SELECT ticker FROM settlements WHERE venue = ? AND voided = 1
               UNION
               SELECT ticker FROM market_snapshots
               WHERE venue = ? AND status IN ('voided', 'delisted')""",
            (self.venue, self.venue),
        ).fetchall()
        return frozenset(r["ticker"] for r in rows)

    def timeline(self, cfg: BacktestConfig) -> tuple[int, ...]:
        rows = self.db.conn.execute(
            """SELECT DISTINCT observed_at_us FROM market_snapshots
               WHERE venue = ? AND observed_at_us >= ? AND observed_at_us <= ?
               ORDER BY observed_at_us""",
            (self.venue,
             cfg.start_us if cfg.start_us is not None else -(1 << 62),
             cfg.end_us if cfg.end_us is not None else (1 << 62)),
        ).fetchall()
        out: list[int] = []
        last: int | None = None
        for r in rows:
            t = int(r["observed_at_us"])
            if last is not None and cfg.min_step_us > 0 and t - last < cfg.min_step_us:
                continue
            out.append(t)
            last = t
            if cfg.max_steps is not None and len(out) >= cfg.max_steps:
                break
        return tuple(out)

    def markets_as_of(self, as_of_us: int) -> tuple[Market, ...]:
        out: list[Market] = []
        for ticker in self.tickers():
            row = self.db.latest_market(ticker, as_of_us=as_of_us, venue=self.venue)
            if row is not None:
                out.append(_market_from_row(row))
        return tuple(out)

    def events_as_of(self, as_of_us: int, event_tickers: Sequence[str]) -> dict[str, Event]:
        out: dict[str, Event] = {}
        for et in sorted({e for e in event_tickers if e}):
            row = self.db.conn.execute(
                """SELECT * FROM event_snapshots
                   WHERE venue = ? AND event_ticker = ? AND observed_at_us <= ?
                   ORDER BY observed_at_us DESC LIMIT 1""",
                (self.venue, et, as_of_us),
            ).fetchone()
            if row is not None:
                out[et] = _event_from_row(row)
        return out

    def series_map(self, series_tickers: Sequence[str]) -> dict[str, Series]:
        out: dict[str, Series] = {}
        for st in sorted({s for s in series_tickers if s}):
            s = self.db.get_series(st)
            if s is not None:
                out[st] = s
        return out

    def tape(self, ticker: str, start_us: int, end_us: int) -> tuple[TapeTrade, ...]:
        rows = self.db.conn.execute(
            """SELECT traded_at_us, yes_price_cents, size, taker_side FROM trades
               WHERE ticker = ? AND traded_at_us > ? AND traded_at_us <= ?
               ORDER BY traded_at_us, trade_id""",
            (ticker, start_us, end_us),
        ).fetchall()
        return tuple(
            TapeTrade(
                traded_at_us=int(r["traded_at_us"] or 0),
                yes_price_cents=int(r["yes_price_cents"] or 0),
                size=float(r["size"] or 0.0),
                taker_side=str(r["taker_side"] or ""),
            )
            for r in rows
            if r["traded_at_us"] is not None and r["yes_price_cents"] is not None
        )

    def settlements(self) -> dict[str, tuple[int, int, bool]]:
        """ticker -> (settled_at_us, outcome, voided).  Evaluation data only."""
        rows = self.db.conn.execute(
            """SELECT ticker, settled_at_us, outcome, voided FROM settlements
               WHERE venue = ? ORDER BY ticker""",
            (self.venue,),
        ).fetchall()
        return {
            r["ticker"]: (int(r["settled_at_us"]), int(r["outcome"]), bool(r["voided"]))
            for r in rows
        }

    def mark_at(self, ticker: str, at_us: int) -> float | None:
        """Fair price (cents) at or after `at_us`.  Forward-looking BY DESIGN:
        a mark-out is evaluation, never an input to a decision."""
        row = self.db.conn.execute(
            """SELECT yes_bid, yes_ask FROM market_snapshots
               WHERE venue = ? AND ticker = ? AND observed_at_us >= ?
               ORDER BY observed_at_us LIMIT 1""",
            (self.venue, ticker, at_us),
        ).fetchone()
        if row is None or row["yes_bid"] is None or row["yes_ask"] is None:
            return None
        return (row["yes_bid"] + row["yes_ask"]) / 2.0


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


def _z95() -> float:
    return 1.959963984540054          # scipy.stats.norm.ppf(0.975), frozen


@dataclass(frozen=True, slots=True)
class ModelResult:
    """One column of the R6.7d bracket."""

    model: FillModel
    orders: int
    filled_orders: int
    filled_contracts: float
    settlements: int                  # settled FILLS -- the independence unit
    voided_settlements: int
    open_contracts: float
    gross_pnl_cents: float
    fee_cents: float
    net_pnl_cents: float
    net_edge_per_settlement: float    # dollars per contract, net of fees
    edge_ci_low: float
    edge_ci_high: float
    #: calibration sufficient statistics.  actual vs price-implied wins; the
    #: z-score is what makes a look-ahead strategy visible (backtest/leakage.py).
    implied_wins: float
    actual_wins: float
    implied_var: float
    adverse: AdverseFillCheck

    @property
    def edge_ci_excludes_zero(self) -> bool:
        """G2's actual exit criterion.  The CI, never the point estimate."""
        return self.settlements > 1 and (self.edge_ci_low > 0.0 or self.edge_ci_high < 0.0)

    @property
    def calibration_z(self) -> float:
        """(realized wins - price-implied wins) / sd.  ~0 for a market-neutral
        sleeve.  A number no honest strategy reaches means look-ahead."""
        if self.implied_var <= 0.0:
            return 0.0
        return (self.actual_wins - self.implied_wins) / (self.implied_var ** 0.5)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.value,
            "orders": self.orders,
            "filled_orders": self.filled_orders,
            "filled_contracts": self.filled_contracts,
            "settlements": self.settlements,
            "voided_settlements": self.voided_settlements,
            "open_contracts": self.open_contracts,
            "gross_pnl_cents": self.gross_pnl_cents,
            "fee_cents": self.fee_cents,
            "net_pnl_cents": self.net_pnl_cents,
            "net_edge_per_settlement": self.net_edge_per_settlement,
            "edge_ci_low": self.edge_ci_low,
            "edge_ci_high": self.edge_ci_high,
            "implied_wins": self.implied_wins,
            "actual_wins": self.actual_wins,
            "implied_var": self.implied_var,
            "adverse_rate": self.adverse.rate,
            "adverse_n": self.adverse.n,
            "adverse_passed": self.adverse.passed,
        }


def _normalise(value: Any) -> Any:
    """Round floats before hashing so -0.0 and 1e-17 noise cannot flip a digest."""
    if isinstance(value, float):
        r = round(value, 9)
        return 0.0 if r == 0.0 else r
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_normalise(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class BacktestResult:
    config: dict[str, Any]
    steps: int
    orders: int
    by_model: dict[FillModel, ModelResult]

    @property
    def pessimistic(self) -> ModelResult:
        """R6.7a.  The ONLY column a gate decision is allowed to read."""
        return self.by_model[FillModel.PESSIMISTIC]

    def as_dict(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "steps": self.steps,
            "orders": self.orders,
            "by_model": {m.value: self.by_model[m].as_dict()
                         for m in ALL_MODELS if m in self.by_model},
        }

    def digest(self) -> str:
        """T-030: same inputs -> same 64 hex chars.  This is the determinism test."""
        blob = json.dumps(_normalise(self.as_dict()), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def report(self) -> str:
        """The bracket, side by side.  R6.7d: never publish a point estimate."""
        head = (f"{'':<22}" + "".join(f"{m.value:>16}" for m in ALL_MODELS
                                      if m in self.by_model))
        rows = [
            ("orders", lambda r: f"{r.orders:d}"),
            ("filled orders", lambda r: f"{r.filled_orders:d}"),
            ("filled contracts", lambda r: f"{r.filled_contracts:.1f}"),
            ("settlements", lambda r: f"{r.settlements:d}"),
            ("voided", lambda r: f"{r.voided_settlements:d}"),
            ("net P&L (cents)", lambda r: f"{r.net_pnl_cents:.1f}"),
            ("fees (cents)", lambda r: f"{r.fee_cents:.1f}"),
            ("net edge ($/settle)", lambda r: f"{r.net_edge_per_settlement:.4f}"),
            ("edge CI low", lambda r: f"{r.edge_ci_low:.4f}"),
            ("edge CI high", lambda r: f"{r.edge_ci_high:.4f}"),
            ("CI excludes zero", lambda r: "yes" if r.edge_ci_excludes_zero else "no"),
            ("adverse-fill rate", lambda r: f"{r.adverse.rate:.3f} (n={r.adverse.n})"),
        ]
        lines = [head]
        for label, fmt in rows:
            line = f"{label:<22}"
            for m in ALL_MODELS:
                if m in self.by_model:
                    line += f"{fmt(self.by_model[m]):>16}"
            lines.append(line)
        lines.append("")
        lines.append("gate decisions read the PESSIMISTIC column only (PLAN.md R6.7a)")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class GateVerdict:
    passed: bool
    reasons: tuple[str, ...]

    def __str__(self) -> str:
        head = "G2 PASS" if self.passed else "G2 FAIL"
        return head + ("" if not self.reasons else "\n  - " + "\n  - ".join(self.reasons))


def gate2_verdict(
    result: BacktestResult, *, min_settlements: int = 1000
) -> GateVerdict:
    """PLAN.md section 8 G2, reading the pessimistic column ONLY (R6.7a).

    Covers the two criteria a backtest can decide by itself.  The rest of G2 --
    pre-registration, walk-forward, capacity -- is process, not arithmetic, and
    is checked in the gate document.
    """
    r = result.pessimistic
    reasons: list[str] = []
    if r.settlements < min_settlements:
        reasons.append(
            f"simulated_settlements {r.settlements} < {min_settlements}"
        )
    if not r.edge_ci_excludes_zero:
        reasons.append(
            f"net edge CI [{r.edge_ci_low:.4f}, {r.edge_ci_high:.4f}] includes zero "
            f"under pessimistic fills"
        )
    elif r.edge_ci_low <= 0.0:
        reasons.append("net edge CI excludes zero but on the WRONG side (edge is negative)")
    if not r.adverse.passed:
        reasons.append(f"adverse-fill gate (R6.7c): {r.adverse.verdict}")
    return GateVerdict(not reasons, tuple(reasons))


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #


@dataclass
class BacktestEngine:
    db: Database
    sleeve: Sleeve
    cfg: BacktestConfig = BacktestConfig()
    reader: SnapshotReader = field(init=False)

    def __post_init__(self) -> None:
        self.reader = SnapshotReader(
            self.db, venue=self.cfg.venue, include_voided=self.cfg.include_voided
        )

    # ---------------------------------------------------------------- snapshots
    def snapshot_at(
        self,
        at_us: int,
        *,
        positions: dict[str, int] | None = None,
        settled_counts: dict[str, int] | None = None,
    ) -> MarketSnapshot:
        markets = self.reader.markets_as_of(at_us)
        return MarketSnapshot(
            now_us=at_us,
            markets=markets,
            events=self.reader.events_as_of(at_us, [m.event_ticker for m in markets]),
            series=self._era_adjusted(
                self.reader.series_map([m.series_ticker for m in markets]), at_us
            ),
            bankroll_cents=self.cfg.bankroll_cents,
            positions=dict(positions or {}),
            settled_counts=dict(settled_counts or {}),
        )

    def _era_adjusted(self, series: dict[str, Series], at_us: int) -> dict[str, Series]:
        """Rewrite the cached series with the fee regime in force at `at_us`.

        The sleeve prices off `snapshot.series[...].fee_spec`, so an era that
        only reached the P&L accounting would let a strategy DECIDE under one fee
        world and be SETTLED in another.  Both have to see the same schedule.
        """
        if not self.cfg.fees.eras:
            return series
        out: dict[str, Series] = {}
        for key, s in series.items():
            spec = self.cfg.fees.spec_for(
                venue=self.cfg.venue, at_us=at_us, series=s,
                default=self.cfg.default_fee_spec,
            )
            if spec.venue == "kalshi" and (
                spec.fee_type != s.fee_type or spec.fee_multiplier != s.fee_multiplier
            ):
                s = s.model_copy(update={"fee_type": spec.fee_type,
                                         "fee_multiplier": spec.fee_multiplier})
            out[key] = s
        return out

    def snapshots(self) -> Iterator[MarketSnapshot]:
        """Point-in-time view of every decision instant, with no position feedback.

        backtest/leakage.py consumes this to audit what the sleeve is allowed to
        see, which is why it must not depend on simulated fills.
        """
        for t in self.reader.timeline(self.cfg):
            yield self.snapshot_at(t)

    # ------------------------------------------------------------------- replay
    def _queue_ahead(self, market: Market, side: Side, price_cents: int) -> float:
        """Displayed size resting at our price when we decided.

        The archive is top-of-book, so we only know the queue AT the touch.  A
        quote away from the touch reports 0 known size, which is optimistic --
        and is exactly why R6.7e says calibrate queue position from realized
        fills (T-044b) rather than believing displayed depth.
        """
        if side is Side.YES:
            return float(market.yes_bid_size) if market.yes_bid == price_cents else 0.0
        return float(market.yes_ask_size) if market.yes_ask == price_cents else 0.0

    def _replay(self) -> tuple[tuple[RestingOrder, ...], dict[FillModel, list[SimFill]]]:
        timeline = self.reader.timeline(self.cfg)
        settlements = self.reader.settlements()
        orders: list[RestingOrder] = []
        fills: dict[FillModel, list[SimFill]] = {m: [] for m in ALL_MODELS}
        # (time, ticker, signed contracts) -- credited progressively so that the
        # position a sleeve sees at step k never contains flow that happened
        # after k.  Crediting a whole horizon at its first print would leak.
        pos_events: list[tuple[int, str, float]] = []
        settle_events: list[int] = []
        market_by_ticker: dict[str, Market] = {}

        for step, t in enumerate(timeline):
            positions: dict[str, int] = {}
            for when, ticker, delta in pos_events:
                if when <= t:
                    positions[ticker] = positions.get(ticker, 0) + int(delta)
            positions = {k: v for k, v in sorted(positions.items()) if v != 0}
            settled = sum(1 for when in settle_events if when <= t)

            snap = self.snapshot_at(
                t, positions=positions, settled_counts={self.sleeve.id: settled}
            )
            market_by_ticker.update({m.ticker: m for m in snap.markets})
            state = self.sleeve.desired_state(snap)

            for j, quote in enumerate(state.quotes):
                market = market_by_ticker.get(quote.ticker)
                if market is None:
                    continue
                side = quote.side
                order = RestingOrder(
                    # positional, not uuid4: a backtest that cannot be re-run to
                    # the same bytes is not evidence (T-030)
                    order_id=f"{self.sleeve.id}-{step:06d}-{j:03d}",
                    ticker=quote.ticker,
                    side=side,
                    price_cents=quote.price_cents,
                    size=quote.size,
                    placed_at_us=t,
                    queue_ahead=self._queue_ahead(market, side, quote.price_cents),
                    book_bid=market.yes_bid,
                    book_ask=market.yes_ask,
                )
                orders.append(order)
                tape = self.reader.tape(order.ticker, t, t + self.cfg.fill_horizon_us)
                sim = simulate_maker_all(order, tape, horizon_us=self.cfg.fill_horizon_us)
                assert_ordering(sim)          # PLAN.md 6.7, checked on every order

                for model, f in sim.items():
                    if f.filled and f.first_fill_at_us is not None:
                        mark = self.reader.mark_at(
                            f.ticker, f.first_fill_at_us + self.cfg.markout_horizon_us
                        )
                        if mark is None:
                            f = with_markout(f, None)
                        else:
                            signed = (mark - f.price_cents) if side is Side.YES \
                                else (f.price_cents - mark)
                            f = with_markout(f, signed)
                    fills[model].append(f)

                # Position and settlement feedback come from the PESSIMISTIC
                # column alone (R6.7a).  Using each model's own fills would make
                # the three columns describe three different order streams, and
                # then the bracket would no longer bound anything.
                pess = sim[FillModel.PESSIMISTIC]
                prev = 0.0
                for when, cum in pess.path:
                    delta = cum - prev
                    prev = cum
                    if delta > 0:
                        pos_events.append(
                            (when, order.ticker, delta if side is Side.YES else -delta)
                        )
                if pess.filled and pess.first_fill_at_us is not None:
                    s = settlements.get(order.ticker)
                    if s is not None:
                        # max(): a settlement cannot be counted before the fill
                        # that created the position, or a horizon that runs past
                        # the cut would leak into the count the sleeve sees.
                        settle_events.append(max(s[0], pess.first_fill_at_us))

        return tuple(orders), fills

    def orders(self) -> tuple[RestingOrder, ...]:
        """The order stream alone -- the sleeve's pure signal output.

        Isolated because it is the right object for a look-ahead test: it depends
        on decisions, not on how generously we then chose to fill them.
        """
        return self._replay()[0]

    # ---------------------------------------------------------------------- run
    def run(self) -> BacktestResult:
        orders, fills = self._replay()
        settlements = self.reader.settlements()
        series_of: dict[str, str] = {}
        for row in self.db.conn.execute(
            """SELECT DISTINCT ticker, series_ticker FROM market_snapshots
               WHERE venue = ? ORDER BY ticker""",
            (self.cfg.venue,),
        ).fetchall():
            series_of[row["ticker"]] = row["series_ticker"] or ""

        by_model: dict[FillModel, ModelResult] = {}
        for model in ALL_MODELS:
            by_model[model] = self._score(
                model, fills[model], len(orders), settlements, series_of
            )
        return BacktestResult(
            config=self.cfg.as_dict(),
            steps=len(self.reader.timeline(self.cfg)),
            orders=len(orders),
            by_model=by_model,
        )

    def _fee_spec(self, ticker: str, at_us: int, series_of: dict[str, str]) -> FeeSpec:
        st = series_of.get(ticker, "")
        series = self.reader.series_map([st]).get(st) if st else None
        return self.cfg.fees.spec_for(
            venue=self.cfg.venue, at_us=at_us, series=series,
            default=self.cfg.default_fee_spec,
        )

    def _score(
        self,
        model: FillModel,
        fills: Sequence[SimFill],
        n_orders: int,
        settlements: dict[str, tuple[int, int, bool]],
        series_of: dict[str, str],
    ) -> ModelResult:
        filled = [f for f in fills if f.filled]
        gross = 0.0
        fees_total = 0.0
        implied_wins = 0.0
        actual_wins = 0.0
        implied_var = 0.0
        open_contracts = 0.0
        voided_tickers: set[str] = set()
        # ONE sample per settled MARKET, not per fill.  Re-quoting the same
        # market across steps produces many fills that all resolve on one
        # outcome; counting each as a draw inflates n and shrinks the very CI
        # that G2 is decided on.  The market outcome is the independence unit.
        # ticker -> [size, gross, fee, p*size, size that paid out]
        #
        # The last slot is SIZE-WEIGHTED, not a bool.  `p_implied` below is the
        # size-weighted average payout probability of everything held on this
        # ticker, so the realized quantity it is compared against has to be the
        # size-weighted fraction of contracts that actually paid.  A bool
        # `won_by_ticker[ticker] = won` records only the LAST fill, so a sleeve
        # holding both sides of one market had half its outcomes overwritten and
        # `calibration_z` -- the only leakage detector that still fires on a
        # cheat baked in at construction time -- moved when nothing but the
        # order the quotes were emitted in had changed.  For a one-sided
        # position this is 1.0 or 0.0, i.e. exactly the old value.
        per_ticker: dict[str, list[float]] = {}

        for f in filled:
            s = settlements.get(f.ticker)
            if s is None:
                open_contracts += f.filled_size          # still open at the cut
                continue
            _settled_at, outcome, is_void = s
            if is_void:
                # Stake returned.  Counted, never silently dropped: excluding
                # voids is survivorship, and survivorship is look-ahead.
                voided_tickers.add(f.ticker)
                continue

            won = (outcome == 1) if f.side is Side.YES else (outcome == 0)
            payout = 100.0 if won else 0.0
            at_us = f.first_fill_at_us if f.first_fill_at_us is not None else 0
            spec = self._fee_spec(f.ticker, at_us, series_of)
            px = min(max(f.avg_cost_cents / 100.0, 0.01), 0.99)
            fee_per_contract = fee(px, spec, is_maker=f.is_maker) * 100.0

            g = f.filled_size * (payout - f.avg_cost_cents)
            fc = f.filled_size * fee_per_contract
            gross += g
            fees_total += fc

            acc = per_ticker.setdefault(f.ticker, [0.0, 0.0, 0.0, 0.0, 0.0])
            acc[0] += f.filled_size
            acc[1] += g
            acc[2] += fc
            acc[3] += px * f.filled_size
            acc[4] += f.filled_size if won else 0.0

        samples: list[float] = []
        for ticker in sorted(per_ticker):
            size, g, fc, p_size, won_size = per_ticker[ticker]
            if size <= 0:
                continue
            samples.append((g - fc) / size / 100.0)
            p_implied = p_size / size
            implied_wins += p_implied
            actual_wins += won_size / size
            implied_var += p_implied * (1.0 - p_implied)

        voided = len(voided_tickers)
        n = len(samples)
        if n:
            mean = sum(samples) / n
        else:
            mean = 0.0
        if n > 1:
            var = sum((x - mean) ** 2 for x in samples) / (n - 1)
            half = _z95() * (var / n) ** 0.5
            ci_low, ci_high = mean - half, mean + half
        else:
            ci_low = ci_high = mean

        return ModelResult(
            model=model,
            orders=n_orders,
            filled_orders=len(filled),
            filled_contracts=sum(f.filled_size for f in filled),
            settlements=n,
            voided_settlements=voided,
            open_contracts=open_contracts,
            gross_pnl_cents=gross,
            fee_cents=fees_total,
            net_pnl_cents=gross - fees_total,
            net_edge_per_settlement=mean,
            edge_ci_low=ci_low,
            edge_ci_high=ci_high,
            implied_wins=implied_wins,
            actual_wins=actual_wins,
            implied_var=implied_var,
            adverse=adverse_fill_gate(fills),
        )
