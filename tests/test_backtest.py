"""T-030 / T-031 / T-032 acceptance for backtest/.

A backtest is never a measurement.  It is an ARGUMENT for spending money, and
every test in this file asks the same question of it: could this harness talk us
into a trade that does not exist?

Four ways it could, and one section each:

  * the fill model quietly hands us liquidity the market would not have
    (`ordering`, plus the R6.7d bracket that has to bound the damage)
  * the strategy read the answer before it traded (`leakage` -- the section that
    matters most: a detector that cannot catch a KNOWN cheat is worthless)
  * the number cannot be reproduced, so nobody can check it (`determinism`)
  * the cost of trading was left out (`fees`), or the run was empty and reported
    as a clean zero rather than as nothing at all (`empty`)

Every numeric expectation below is written as the arithmetic that produces it,
never as a constant lifted out of a previous run.
"""

from __future__ import annotations

import itertools
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import pytest

from backtest import fills as fills_module
from backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    FeeEra,
    FeeSchedule,
    gate2_verdict,
)
from backtest.fills import (
    ALL_MODELS,
    FillModel,
    FillParams,
    RestingOrder,
    TapeTrade,
    generosity,
    ladder_from_book,
    ordering_violations,
    simulate_maker_all,
    simulate_taker_all,
)
from backtest.leakage import (
    LEAKAGE_CHECKS,
    LabelSpan,
    LookAheadSleeve,
    check_impossible_performance,
    check_purge_embargo,
    check_signal_timestamps,
    check_sleeve_purity,
    contiguous_blocks,
    look_ahead_factory,
    purge_embargo_experiment,
    purge_embargo_mask,
    roc_auc,
    run_leakage_suite,
)
from core.db import Database
from core.math.contracts import KALSHI_BASE_TAKER, KALSHI_MAKER_RATIO, FeeSpec
from core.models import Market, Series, Side
from strategy.base import DesiredQuote, DesiredState, MarketSnapshot

T0 = 1_700_000_000_000_000
HOUR = 3_600_000_000
MINUTE = 60_000_000


# --------------------------------------------------------------------------- #
# Fixtures: a synthetic archive we can do the arithmetic on by hand
# --------------------------------------------------------------------------- #
def build_db(
    *,
    n_markets: int = 4,
    n_steps: int = 1,
    bid: int = 25,
    ask: int = 27,
    bid_size: float = 0.0,
    prints: Sequence[tuple[int, float, str]] = ((-1, 4000.0, "no"),),
    fee_types: Sequence[str] = ("quadratic",),
    resolves_yes: Callable[[int], bool] | None = None,
    voided: Sequence[str] = (),
) -> Database:
    """An in-memory Kalshi archive with a quote, a tape and a settlement.

    `prints` are (cents relative to `bid`, size, taker_side) and are replayed
    once per observation, ten minutes after it.  Market i is assigned to series
    `KXS{i % len(fee_types)}`, which is what makes per-series fees testable.

    Everything is constant across time on purpose: a fixture whose prices drift
    hides arithmetic errors inside plausible-looking noise.
    """
    resolves = resolves_yes if resolves_yes is not None else (lambda i: i % 4 == 0)
    db = Database(":memory:")
    for k, fee_type in enumerate(fee_types):
        db.upsert_series(
            [Series(ticker=f"KXS{k}", fee_type=fee_type, fee_multiplier=1.0)],
            observed_at_us=T0 - HOUR,
        )
    tickers = [f"KXM-{i:02d}" for i in range(n_markets)]
    for step in range(n_steps):
        db.append_markets(
            [
                Market(
                    ticker=ticker,
                    event_ticker=f"KXE-{i // 4:02d}",
                    series_ticker=f"KXS{i % len(fee_types)}",
                    status="active",
                    yes_bid=bid,
                    yes_ask=ask,
                    yes_bid_size=bid_size,
                    yes_ask_size=bid_size,
                    close_at_us=T0 + 100 * HOUR,
                )
                for i, ticker in enumerate(tickers)
            ],
            observed_at_us=T0 + step * HOUR,
        )
    tape = [
        (f"{ticker}-{step}-{j}", ticker, T0 + step * HOUR + 10 * MINUTE,
         bid + offset, size, taker_side, 0)
        for ticker in tickers
        for step in range(n_steps)
        for j, (offset, size, taker_side) in enumerate(prints)
    ]
    settled = [
        ("kalshi", ticker, T0 + (n_steps + 4) * HOUR,
         1 if resolves(i) else 0, 1 if ticker in voided else 0)
        for i, ticker in enumerate(tickers)
    ]
    with db.tx() as c:
        c.executemany(
            """INSERT OR IGNORE INTO trades
               (trade_id, ticker, traded_at_us, yes_price_cents, size, taker_side, is_block)
               VALUES (?,?,?,?,?,?,?)""",
            tape,
        )
        c.executemany(
            """INSERT OR IGNORE INTO settlements
               (venue, ticker, settled_at_us, outcome, voided) VALUES (?,?,?,?,?)""",
            settled,
        )
    return db


#: 48 markets quoted 25/27, a quarter of which resolve YES, with flow deep enough
#: that every resting bid fills in full under all three models.  Priced FAIRLY:
#: 25c on a 25%-chance contract, so gross P&L is exactly zero and anything the
#: engine reports as P&L is fees, and anything it reports as calibration skew is
#: a bug or a cheat.
FAIR = dict(
    n_markets=48,
    n_steps=6,
    bid=25,
    ask=27,
    bid_size=0.0,
    prints=((-1, 4000.0, "no"),),
    resolves_yes=lambda i: i % 4 == 0,
)

#: A deep displayed queue plus flow AT the touch -- the only shape that pulls the
#: three fill models apart.  Used wherever a test would otherwise be vacuous.
SEPARATING = dict(
    n_markets=4,
    n_steps=1,
    bid=25,
    ask=27,
    bid_size=400.0,
    prints=((-1, 450.0, "no"), (0, 100.0, "no")),
)
SEPARATING_SIZE = 500
SEPARATING_CFG = BacktestConfig(fill_horizon_us=30 * MINUTE)


@dataclass
class BidSleeve:
    """Honest.  Rests YES at the displayed bid on every two-sided market.

    Pure in the C4.2a sense: it touches nothing but the snapshot it was handed.
    """

    id: str = "HONEST"
    gate: int = 0
    size: int = 10

    def desired_state(self, snapshot: MarketSnapshot) -> DesiredState:
        return DesiredState(
            quotes=tuple(
                DesiredQuote(ticker=m.ticker, side=Side.YES, price_cents=m.yes_bid,
                             size=self.size, rationale={"sleeve": self.id})
                for m in snapshot.markets
                if m.has_two_sided_quote and m.yes_bid is not None
            ),
            rationale={"sleeve": self.id},
        )


@dataclass
class TwoSidedSleeve:
    """Rests BOTH legs of every market.  `yes_first` flips the emission order
    only -- the fills, and therefore the money, are identical either way."""

    id: str = "BOTH"
    gate: int = 0
    size: int = 10
    yes_first: bool = True

    def desired_state(self, snapshot: MarketSnapshot) -> DesiredState:
        quotes: list[DesiredQuote] = []
        for m in snapshot.markets:
            if not m.has_two_sided_quote:
                continue
            yes = DesiredQuote(ticker=m.ticker, side=Side.YES, price_cents=m.yes_bid,
                               size=self.size, rationale={"leg": "yes"})
            no = DesiredQuote(ticker=m.ticker, side=Side.NO, price_cents=m.yes_ask,
                              size=self.size, rationale={"leg": "no"})
            quotes.extend([yes, no] if self.yes_first else [no, yes])
        return DesiredState(quotes=tuple(quotes), rationale={"sleeve": self.id})


@dataclass
class BakedInCheatSleeve:
    """DELIBERATELY CHEATS, but reads nothing at run time.

    The winners were handed to it at construction, so it performs no I/O, it
    repeats, and its order stream does not move when the future is deleted.  Two
    of the three leakage detectors are structurally blind to it.  It exists to
    prove the third one is load-bearing.
    """

    winners: frozenset[str]
    id: str = "BAKED"
    gate: int = 0
    size: int = 10

    def desired_state(self, snapshot: MarketSnapshot) -> DesiredState:
        return DesiredState(
            quotes=tuple(
                DesiredQuote(ticker=m.ticker, side=Side.YES, price_cents=m.yes_bid,
                             size=self.size, rationale={"cheat": "baked in"})
                for m in snapshot.markets
                if m.has_two_sided_quote and m.ticker in self.winners
            ),
            rationale={"cheat": True},
        )


def winners_of(db: Database) -> frozenset[str]:
    rows = db.conn.execute("SELECT ticker FROM settlements WHERE outcome = 1").fetchall()
    return frozenset(r["ticker"] for r in rows)


def run(db: Database, sleeve: object, cfg: BacktestConfig | None = None):
    return BacktestEngine(db=db, sleeve=sleeve, cfg=cfg or BacktestConfig()).run()


# --------------------------------------------------------------------------- #
# 1.  The bracket is ordered.  PLAN.md 6.7 / R6.7d.
#
# If the three columns are not ordered they do not bracket anything, and the
# "pessimistic" number a gate promotes on is just a fourth opinion.
# --------------------------------------------------------------------------- #
_GRID_TAPES: dict[str, tuple[tuple[int, float, str], ...]] = {
    "silent": (),
    "trade_through": ((-1, 3.0, "no"), (1, 3.0, "yes")),
    "heavy_touch": ((0, 500.0, "no"), (0, 500.0, "yes")),
    "mixed": ((0, 40.0, "no"), (-1, 40.0, "no"), (0, 40.0, "yes"), (1, 40.0, "yes")),
    "wrong_taker_side": ((-5, 900.0, "yes"), (5, 900.0, "no")),
    "simultaneous": ((-1, 5.0, "no"), (0, 5.0, "no"), (1, 5.0, "yes"), (0, 5.0, "yes")),
    "beyond_horizon": ((-1, 999.0, "no"),),
    "zero_size_prints": ((-1, 0.0, "no"), (0, 0.0, "no")),
}


def test_the_bracket_is_ordered_on_every_resting_order_in_a_wide_maker_grid():
    """The whole R6.7d apparatus is worth nothing if the bounds can cross.

    An inverted bracket means the column a promotion decision reads is not the
    conservative one, so a strategy could clear G2 on liquidity the pessimistic
    model was supposed to have denied it.  The invariant is claimed to be
    structural, so it is checked over a grid rather than on one lucky tape.
    """
    checked = 0
    for side, price, size, queue, (label, rows) in itertools.product(
        (Side.YES, Side.NO), (1, 5, 40, 50, 60, 99), (0, 1, 7, 100),
        (0.0, 0.5, 3.0, 250.0), _GRID_TAPES.items(),
    ):
        offset = 24 * HOUR if label == "beyond_horizon" else 0
        trades = tuple(
            TapeTrade(traded_at_us=T0 + offset + i + 1, yes_price_cents=price + d,
                      size=sz, taker_side=taker)
            for i, (d, sz, taker) in enumerate(rows)
            if 0 <= price + d <= 100
        )
        order = RestingOrder(
            order_id=f"{side.value}-{price}-{size}-{queue}-{label}",
            ticker="KXM-00", side=side, price_cents=price, size=size,
            placed_at_us=T0, queue_ahead=queue, book_bid=price, book_ask=price + 1,
        )
        sim = simulate_maker_all(order, trades, horizon_us=6 * HOUR)
        assert ordering_violations(sim) == ()
        pess, real, opt = (generosity(sim[m]) for m in ALL_MODELS)
        assert pess <= real + 1e-9
        assert real <= opt + 1e-9
        checked += 1
    assert checked == 2 * 6 * 4 * 4 * len(_GRID_TAPES)


def test_the_bracket_is_ordered_on_every_crossing_order_in_a_wide_taker_grid():
    """Takers always get their size, so for them the bracket runs on COST.

    A crossing model that reports the optimistic price as dearer than the
    pessimistic one would make crossing look expensive exactly where it is
    cheap, which is how a maker-only doctrine gets justified by arithmetic
    instead of by evidence.
    """
    books = (
        (None, 0.0, None, 0.0),          # no book at all
        (40, 10.0, 42, 10.0),
        (40, 0.0, 42, 5.0),              # one-sided displayed size
        (1, 1e6, 100, 1e6),              # the extremes of the tick grid
        (50, 2.0, 51, 2.0),              # thinner than any order below
    )
    checked = 0
    for side, price, size, (bid, bid_size, ask, ask_size) in itertools.product(
        (Side.YES, Side.NO), (1, 5, 40, 50, 60, 99), (0, 1, 7, 100), books,
    ):
        depth = ladder_from_book(side, yes_bid=bid, yes_bid_size=bid_size,
                                 yes_ask=ask, yes_ask_size=ask_size)
        order = RestingOrder(order_id=f"cross-{side.value}-{price}-{size}",
                             ticker="KXM-00", side=side, price_cents=price,
                             size=size, placed_at_us=T0)
        sim = simulate_taker_all(order, depth)
        assert ordering_violations(sim) == ()
        pess, real, opt = (generosity(sim[m]) for m in ALL_MODELS)
        assert pess <= real + 1e-9
        assert real <= opt + 1e-9
        checked += 1
    assert checked == 2 * 6 * 4 * len(books)


_ENGINE_FIXTURES: dict[str, tuple[dict, int, BacktestConfig]] = {
    "flat_book_deep_flow": (
        dict(bid_size=0.0, prints=((-1, 4000.0, "no"),)), 10, BacktestConfig()),
    "deep_queue_and_touch": (
        dict(bid_size=400.0, prints=((-1, 450.0, "no"), (0, 100.0, "no"))),
        500, BacktestConfig(fill_horizon_us=30 * MINUTE)),
    "touch_flow_only": (
        dict(bid_size=120.0, prints=((0, 300.0, "no"),)),
        50, BacktestConfig(fill_horizon_us=30 * MINUTE)),
    "wrong_side_flow": (
        dict(bid_size=10.0, prints=((-1, 900.0, "yes"), (1, 900.0, "no"))),
        50, BacktestConfig()),
    "no_tape_at_all": (dict(bid_size=10.0, prints=()), 50, BacktestConfig()),
    "trickle": (
        dict(bid_size=90.0, prints=((-1, 30.0, "no"), (0, 20.0, "no"))),
        40, BacktestConfig(fill_horizon_us=30 * MINUTE)),
}


@pytest.mark.parametrize("name", sorted(_ENGINE_FIXTURES))
def test_the_three_columns_describe_one_order_stream_and_are_ordered(name):
    """Property, not anecdote: the bracket holds end to end on every fixture.

    It also has to be the SAME order stream in all three columns.  If the
    generosity of the fill model fed back into what the sleeve decided to quote,
    the three columns would describe three different strategies and the spread
    between them would bound nothing at all.
    """
    kwargs, size, cfg = _ENGINE_FIXTURES[name]
    db = build_db(n_markets=6, n_steps=4, **kwargs)
    result = run(db, BidSleeve(size=size), cfg)

    assert {result.by_model[m].orders for m in ALL_MODELS} == {result.orders}
    filled = [result.by_model[m].filled_orders for m in ALL_MODELS]
    contracts = [result.by_model[m].filled_contracts for m in ALL_MODELS]
    assert filled == sorted(filled), (name, filled)
    assert contracts == sorted(contracts), (name, contracts)


def test_a_deep_queue_with_touch_flow_separates_all_three_models_strictly():
    """Without this the ordering tests above could pass on three equal columns.

    Real money question: how much of the reported fill is a modelling choice?
    Here the optimistic model hands us 10x the contracts the pessimistic one
    does off the identical tape -- which is the 3.9x bracket Lo/MacKinlay/Zhang
    measured, made concrete.
    """
    db = build_db(**SEPARATING)
    result = run(db, BidSleeve(size=SEPARATING_SIZE), SEPARATING_CFG)

    n_orders = SEPARATING["n_markets"] * SEPARATING["n_steps"]
    through, at_touch = 450.0, 100.0
    displayed_queue = SEPARATING["bid_size"]
    size = float(SEPARATING_SIZE)

    # pessimistic: full displayed queue ahead of us, no credit for touch prints
    pess = through - displayed_queue
    # realistic: half the queue is assumed stale, and we take our proportional
    # share of anything that prints AT our price
    half_queue = 0.5 * displayed_queue
    real = through + at_touch * size / (size + half_queue) - half_queue
    # optimistic: no queue, every print at our price is ours -- so the order caps
    opt = size

    assert result.by_model[FillModel.PESSIMISTIC].filled_contracts == pytest.approx(
        n_orders * pess)
    assert result.by_model[FillModel.REALISTIC].filled_contracts == pytest.approx(
        n_orders * real)
    assert result.by_model[FillModel.OPTIMISTIC].filled_contracts == pytest.approx(
        n_orders * opt)
    assert pess < real < opt


def test_a_more_generous_fill_model_makes_a_losing_strategy_lose_more():
    """The ordering invariant is over CONTRACTS, and P&L does not inherit it.

    Buy a contract that always loses and the optimistic model -- the generous
    one -- books the largest loss, so the P&L columns run the OTHER way.  That
    matters for real money: "read the pessimistic column" (R6.7a) is
    conservative about fill quantity, not about profit, and on a negative-edge
    sleeve the pessimistic column is the FLATTERING one.
    """
    winners = run(build_db(resolves_yes=lambda i: True, **SEPARATING),
                  BidSleeve(size=SEPARATING_SIZE), SEPARATING_CFG)
    losers = run(build_db(resolves_yes=lambda i: False, **SEPARATING),
                 BidSleeve(size=SEPARATING_SIZE), SEPARATING_CFG)

    win_pnl = [winners.by_model[m].net_pnl_cents for m in ALL_MODELS]
    lose_pnl = [losers.by_model[m].net_pnl_cents for m in ALL_MODELS]

    # bought at 25c: a YES that settles pays 100c, one that does not pays 0
    edge_per_contract = 100 - SEPARATING["bid"]
    for model in ALL_MODELS:
        held = winners.by_model[model].filled_contracts
        assert winners.by_model[model].net_pnl_cents == pytest.approx(
            held * edge_per_contract)
        assert losers.by_model[model].net_pnl_cents == pytest.approx(
            -held * SEPARATING["bid"])

    assert win_pnl == sorted(win_pnl)                 # generous model, more profit
    assert lose_pnl == sorted(lose_pnl, reverse=True)  # generous model, more loss


def test_the_engine_aborts_a_run_whose_bracket_has_inverted(monkeypatch):
    """`assert_ordering` is called on every order; prove it is not decorative.

    A refactor can break a proof.  If the check were only in a docstring, an
    inverted bracket would be published as a result rather than raised as a bug.
    """
    monkeypatch.setitem(
        fills_module.PARAMS,
        FillModel.PESSIMISTIC,
        FillParams(queue_factor=0.0, touch_credit=True,
                   taker_penalty_cents=0.0, taker_at_best=False),
    )
    db = build_db(**SEPARATING)
    with pytest.raises(AssertionError, match="ordering violated"):
        run(db, BidSleeve(size=SEPARATING_SIZE), SEPARATING_CFG)


# --------------------------------------------------------------------------- #
# 2.  The leakage suite catches a known cheat.  PLAN.md R11a.
#
# This is the most important section in the file.  Everything else measures how
# good a strategy looks; this measures whether the harness is entitled to an
# opinion at all.
# --------------------------------------------------------------------------- #
def test_the_backtest_makes_the_cheating_sleeve_look_spectacular():
    """Motivation for everything below: the P&L alone will never object.

    `LookAheadSleeve` reads `settlements` and quotes only the markets it already
    knows resolve YES.  The engine dutifully reports a 100% hit rate on a book
    priced at 25c.  Nothing in the result object says "impossible" -- only the
    leakage suite does.
    """
    db = build_db(**FAIR)
    cheat = LookAheadSleeve(db=db)
    column = run(db, cheat).pessimistic

    n_winners = len(winners_of(db))
    contracts = n_winners * FAIR["n_steps"] * cheat.size
    assert column.filled_contracts == pytest.approx(contracts)
    assert column.actual_wins == pytest.approx(column.settlements)   # never wrong
    assert column.net_pnl_cents == pytest.approx(contracts * (100 - FAIR["bid"]))


def test_the_leakage_suite_fails_the_sleeve_that_reads_the_settlements_table():
    """R11a, and the reason backtest/leakage.py exists.

    A leakage detector that cannot catch a cheat we WROTE is not evidence about
    the cheats we did not write.  All three independent detectors have to fire,
    and nothing else may fire -- a suite that fails everything is as useless as
    one that fails nothing.
    """
    db = build_db(**FAIR)
    report = run_leakage_suite(db, LookAheadSleeve(db=db), BacktestConfig(),
                               sleeve_factory=look_ahead_factory)

    assert not report.passed
    assert tuple(f.check for f in report.findings) == LEAKAGE_CHECKS
    assert report.failed_checks() == frozenset(
        {"sleeve_purity", "signal_timestamps", "impossible_performance"})


def test_sleeve_purity_catches_the_cheater_by_the_sql_it_executes():
    """Detector 1.  C4.2a says `desired_state` is pure, so ANY query it issues
    is proof of look-ahead regardless of which table it hit.  Cheapest and most
    general of the three: it does not need the cheat to be profitable to fire."""
    db = build_db(**FAIR)
    finding = check_sleeve_purity(db, LookAheadSleeve(db=db), BacktestConfig())
    assert not finding.passed
    assert "settlements" in finding.detail


def test_deleting_the_future_moves_the_cheaters_order_stream_and_not_an_honest_one():
    """Detector 2.  What the world looked like at t cannot depend on t+1.

    Run the same decisions against a database truncated at the decision cut: an
    honest sleeve fingerprints identically, the cheater cannot, because the rows
    it was reading are gone.
    """
    db = build_db(**FAIR)
    cheating = check_signal_timestamps(db, LookAheadSleeve(db=db), BacktestConfig(),
                                       sleeve_factory=look_ahead_factory)
    honest = check_signal_timestamps(db, BidSleeve(), BacktestConfig())

    assert not cheating.passed
    assert "reading the future" in cheating.detail
    assert honest.passed


def test_the_truncation_check_cannot_be_disarmed_by_omitting_the_factory():
    """A safety check that a forgotten argument switches off is not a check.

    A sleeve holding its own Database handle keeps reading the ORIGINAL database
    unless it is rebuilt against the truncated clone.  Omitting `sleeve_factory`
    used to make this check report PASS on a deliberate look-ahead cheat -- one
    missing keyword argument between a leaking strategy and a G2 promotion, and
    the report said PASS rather than "not checked".

    It now FAILS instead, and the detail says why.  Failing closed is the only
    defensible default: an unverifiable claim is not a verified one.
    """
    db = build_db(**FAIR)
    cheat = LookAheadSleeve(db=db)

    omitted = check_signal_timestamps(db, cheat, BacktestConfig())
    assert not omitted.passed
    assert "sleeve_factory" in omitted.detail

    # With the factory supplied it fails for the REAL reason -- the look-ahead.
    supplied = check_signal_timestamps(db, cheat, BacktestConfig(),
                                       sleeve_factory=look_ahead_factory)
    assert not supplied.passed


def test_a_stateless_sleeve_still_runs_without_a_factory():
    """The guard must not force a factory on sleeves that hold no database --
    most sleeves read only the snapshot they are handed."""
    db = build_db(**FAIR)
    assert check_signal_timestamps(db, BidSleeve(), BacktestConfig()).passed


def test_calibration_alone_catches_a_cheat_that_never_touches_the_database():
    """Detector 3, tested in the one case where it is the ONLY line of defence.

    `BakedInCheatSleeve` was handed the answers at construction: it issues no
    SQL and its decisions do not move when the future is deleted, so detectors 1
    and 2 both pass it.  Only realized-versus-implied wins says anything, and
    what it says is 6 standard deviations, which is the arithmetic below.
    """
    db = build_db(**FAIR)
    winners = winners_of(db)
    sleeve = BakedInCheatSleeve(winners=winners)
    report = run_leakage_suite(db, sleeve, BacktestConfig(),
                               sleeve_factory=lambda _db: BakedInCheatSleeve(winners=winners))

    assert report.failed_checks() == frozenset({"impossible_performance"})

    column = run(db, sleeve).pessimistic
    n = len(winners)
    price = FAIR["bid"] / 100.0
    assert column.settlements == n
    assert column.actual_wins == pytest.approx(float(n))       # every one a winner
    assert column.implied_wins == pytest.approx(n * price)     # the book said 25%
    assert column.implied_var == pytest.approx(n * price * (1 - price))
    expected_z = (n - n * price) / (n * price * (1 - price)) ** 0.5
    assert column.calibration_z == pytest.approx(expected_z)
    assert expected_z > 4.0                                    # the suite's threshold


def test_an_honest_sleeve_passes_every_check_in_the_leakage_suite():
    """The control.  A suite that red-flags everything cannot promote anything,
    which in practice means it gets switched off.

    The fixture is priced fairly -- 25c on a contract that resolves YES a
    quarter of the time -- so a sleeve that lifts every bid must come out at
    exactly zero calibration skew.  Any other answer is the harness inventing
    signal.
    """
    db = build_db(**FAIR)
    report = run_leakage_suite(db, BidSleeve(), BacktestConfig())

    assert report.passed, report.report()
    assert tuple(f.check for f in report.findings) == LEAKAGE_CHECKS

    column = run(db, BidSleeve()).pessimistic
    price = FAIR["bid"] / 100.0
    assert column.settlements == FAIR["n_markets"]
    assert column.implied_wins == pytest.approx(FAIR["n_markets"] * price)
    assert column.actual_wins == pytest.approx(FAIR["n_markets"] * price)
    assert column.calibration_z == pytest.approx(0.0)
    assert check_impossible_performance(run(db, BidSleeve())).passed


def test_the_calibration_statistic_does_not_depend_on_quote_emission_order():
    """Regression (see the report): the leakage backstop used to be reorderable.

    Both sleeves rest both legs of every market and get byte-identical fills;
    only the order the quotes come out of `desired_state` in differs.  If the
    realized-wins accumulator keys on ticker alone it keeps whichever leg
    happened to be written last, and `calibration_z` -- the one detector that
    still fires on a cheat baked in at construction -- swings with it.
    """
    yes_first = build_db(n_markets=8, n_steps=1, bid_size=0.0,
                         prints=((-1, 4000.0, "no"), (3, 4000.0, "yes")),
                         resolves_yes=lambda i: i < 6)
    no_first = build_db(n_markets=8, n_steps=1, bid_size=0.0,
                        prints=((-1, 4000.0, "no"), (3, 4000.0, "yes")),
                        resolves_yes=lambda i: i < 6)
    a = run(yes_first, TwoSidedSleeve(yes_first=True))
    b = run(no_first, TwoSidedSleeve(yes_first=False))

    assert a.pessimistic.gross_pnl_cents == pytest.approx(b.pessimistic.gross_pnl_cents)
    assert a.pessimistic.actual_wins == pytest.approx(b.pessimistic.actual_wins)
    assert a.pessimistic.calibration_z == pytest.approx(b.pessimistic.calibration_z)
    # equal size on both legs of every market, exactly one of which pays out
    assert a.pessimistic.actual_wins == pytest.approx(0.5 * a.pessimistic.settlements)


# --------------------------------------------------------------------------- #
# 3.  Determinism.  T-030.
#
# A backtest that cannot be re-run to the same bytes is not evidence; it is an
# anecdote that happened once on somebody's laptop.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def disk_db():
    """An on-disk database in a hand-managed scratch directory.

    pytest's `tmp_path` raises WinError 5 on this machine, so the directory is
    created and removed explicitly.  On disk rather than in memory on purpose:
    SQLite delivers rows from a file in whatever order it likes, and that is
    precisely the non-determinism T-030 is about.
    """
    directory = tempfile.mkdtemp(prefix="pm-backtest-")
    db: Database | None = None
    try:
        db = Database(os.path.join(directory, "pm.db"))
        source = build_db(n_markets=6, n_steps=3, bid_size=50.0,
                          prints=((-1, 60.0, "no"), (0, 40.0, "no")),
                          fee_types=("quadratic", "quadratic_with_maker_fees"),
                          resolves_yes=lambda i: i % 3 == 0)
        source.conn.backup(db.conn)
        source.close()
        yield db
    finally:
        if db is not None:
            db.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_two_runs_over_the_same_database_are_byte_identical(disk_db):
    """T-030's acceptance criterion.  Same inputs -> same 64 hex characters.

    Without it nobody can reproduce the number a promotion decision was made on,
    and "the backtest said so" stops being checkable by anyone but its author.
    """
    first = run(disk_db, BidSleeve(size=20))
    second = run(disk_db, BidSleeve(size=20))

    assert first.digest() == second.digest()
    assert len(first.digest()) == 64
    assert first.as_dict() == second.as_dict()
    assert first.report() == second.report()


def test_a_second_database_built_from_the_same_recipe_digests_the_same(disk_db):
    """Reproducibility has to survive the database being rebuilt, not just the
    run being repeated -- otherwise the digest is fingerprinting rowids."""
    rebuilt = build_db(n_markets=6, n_steps=3, bid_size=50.0,
                       prints=((-1, 60.0, "no"), (0, 40.0, "no")),
                       fee_types=("quadratic", "quadratic_with_maker_fees"),
                       resolves_yes=lambda i: i % 3 == 0)
    try:
        assert run(disk_db, BidSleeve(size=20)).digest() == run(
            rebuilt, BidSleeve(size=20)).digest()
    finally:
        rebuilt.close()


def test_the_digest_moves_when_a_single_trade_is_added_to_the_tape():
    """A digest that never changes is a constant, not a fingerprint.

    The added print is 100 contracts of trade-through flow, which is exactly the
    amount by which the pessimistic column's fills must grow: it clears the
    displayed queue barrier one for one.
    """
    db = build_db(**SEPARATING)
    before = run(db, BidSleeve(size=SEPARATING_SIZE), SEPARATING_CFG)
    added = 100.0
    with db.tx() as c:
        c.execute(
            """INSERT INTO trades
               (trade_id, ticker, traded_at_us, yes_price_cents, size, taker_side, is_block)
               VALUES ('extra', 'KXM-00', ?, ?, ?, 'no', 0)""",
            (T0 + 12 * MINUTE, SEPARATING["bid"] - 1, added),
        )
    after = run(db, BidSleeve(size=SEPARATING_SIZE), SEPARATING_CFG)

    assert before.digest() != after.digest()
    assert after.pessimistic.filled_contracts == pytest.approx(
        before.pessimistic.filled_contracts + added)


def test_order_ids_are_positional_so_the_same_run_names_the_same_orders(disk_db):
    """`uuid4()` in the order id would break the digest for no benefit.

    Ids are `<sleeve>-<step>-<index within step>`, so two runs agree on which
    order is which and a diff between two backtests is readable.
    """
    sleeve = BidSleeve(size=20)
    first = [o.order_id for o in BacktestEngine(db=disk_db, sleeve=sleeve).orders()]
    second = [o.order_id for o in BacktestEngine(db=disk_db, sleeve=sleeve).orders()]

    assert first == second
    assert first[0] == f"{sleeve.id}-000000-000"
    assert first[1] == f"{sleeve.id}-000000-001"
    assert len(set(first)) == len(first)


# --------------------------------------------------------------------------- #
# 4.  Purging and embargoing.  PLAN.md 6.7, research/08 section 6.4.
#
# In prediction markets label overlap is structural: you trade at t and learn the
# answer at settlement, so neighbouring observations share most of their outcome.
# Naive cross-validation turns that overlap into edge that is not there.
# --------------------------------------------------------------------------- #
LEAK_N, LEAK_H, LEAK_BLOCK = 80, 10, 20
LEAK_EMBARGO = 2 * LEAK_H
#: strictly less than the horizon, so a predictor whose nearest surviving
#: training neighbour is a full label-span away is forced to abstain
LEAK_RADIUS = LEAK_H - 1
LEAK_SPANS = [LabelSpan(i, i + LEAK_H) for i in range(LEAK_N)]


def test_purging_drops_exactly_the_training_rows_whose_label_overlaps_the_test():
    """The mask is the whole mechanism, so it is asserted element by element.

    Test block is 20..39, and with H=10 its labels are decided over [20, 49).
    Purge therefore has to remove every observation whose own span [j, j+10)
    touches that: j >= 11 (its span reaches past 20) and j <= 48 (it starts
    before 49).  An off-by-one here leaves one leaked row per fold, which is all
    it takes to manufacture the edge the next test measures.
    """
    test = np.arange(20, 40)
    purged = np.flatnonzero(purge_embargo_mask(LEAK_SPANS, test))
    embargoed = np.flatnonzero(
        purge_embargo_mask(LEAK_SPANS, test, embargo_us=LEAK_EMBARGO))
    symmetric = np.flatnonzero(purge_embargo_mask(
        LEAK_SPANS, test, embargo_us=LEAK_EMBARGO, embargo_before_us=LEAK_EMBARGO))

    assert purged.tolist() == list(range(0, 11)) + list(range(49, LEAK_N))
    # embargo drops a further 2H of observations STARTING after the span ends
    assert embargoed.tolist() == list(range(0, 11)) + list(range(69 + 1, LEAK_N))
    # ... and the symmetric version drops the 2H that END just before it starts
    assert symmetric.tolist() == list(range(70, LEAK_N))

    for survivors in (purged, embargoed, symmetric):
        gap = int(np.min(np.abs(test[:, None] - survivors[None, :])))
        assert gap >= LEAK_H          # no shared noise left with the test block
        assert gap > LEAK_RADIUS      # so the predictor below must abstain


def _leaky_auc(seed: int, *, purge: bool) -> float:
    """Nearest-neighbour-in-time predictor on a fixture with NO real signal.

    Label of observation i is `1{sum of iid noise over [i, i+H) > 0}`, so
    neighbours one step apart share H-1 of their H noise terms and their labels
    are nearly the same coin.  Copying the neighbour's label is therefore an
    excellent in-sample predictor and a worthless out-of-sample one -- which is
    exactly the shape of a real overlapping-label leak.
    """
    rng = np.random.default_rng(seed)
    cumulative = np.concatenate([[0.0], np.cumsum(rng.standard_normal(LEAK_N + LEAK_H))])
    idx = np.arange(LEAK_N)
    y = ((cumulative[idx + LEAK_H] - cumulative[idx]) > 0).astype(int)

    predictions: list[np.ndarray] = []
    truths: list[np.ndarray] = []
    for test in contiguous_blocks(LEAK_N, LEAK_BLOCK):
        if purge:
            mask = purge_embargo_mask(LEAK_SPANS, test, embargo_us=LEAK_EMBARGO,
                                      embargo_before_us=LEAK_EMBARGO)
        else:
            mask = np.ones(LEAK_N, dtype=bool)
            mask[test] = False
        train = np.flatnonzero(mask)
        if not len(train):
            continue
        k = np.searchsorted(train, test)
        left = train[np.clip(k - 1, 0, len(train) - 1)]
        right = train[np.clip(k, 0, len(train) - 1)]
        nearest = np.where(np.abs(test - left) <= np.abs(test - right), left, right)
        distance = np.abs(test - nearest)
        predictions.append(np.where(distance <= LEAK_RADIUS, y[nearest].astype(float), 0.5))
        truths.append(y[test])
    return roc_auc(np.concatenate(truths), np.concatenate(predictions))


def test_a_leaked_label_manufactures_edge_that_purge_and_embargo_erase():
    """The finding, on a fixture whose true out-of-sample AUC is 0.500 by
    construction: naive CV pays you for noise, purging plus embargo does not.

    An AUC of 0.64 on a signal that does not exist is larger than any edge this
    project expects to find, so a research pipeline without purging will fund
    fictional strategies before it funds a real one.

    The purged number is EXACTLY 0.500, not approximately: the previous test
    shows every surviving training row is further away than the abstention
    radius, so every prediction is the tie value 0.5, and Mann-Whitney AUC over
    a completely tied score is 0.5 by definition.
    """
    seeds = range(100)
    naive = [_leaky_auc(s, purge=False) for s in seeds]
    purged = [_leaky_auc(s, purge=True) for s in seeds]

    assert all(auc == 0.5 for auc in purged)
    assert float(np.mean(naive)) > 0.58
    assert float(np.mean(naive)) - float(np.mean(purged)) > 0.05


def test_the_shipped_purge_embargo_check_reproduces_its_own_finding():
    """The repo's own experiment, run as the machinery every model here will be
    cross-validated with.  Seeded, so this is a fixed number and not a coin.

    It also has to fail LOUDLY if the fixture stops leaking -- a purge routine
    validated against data with nothing to remove has been validated against
    nothing.
    """
    finding = check_purge_embargo()
    assert finding.passed, finding.detail

    measured = purge_embargo_experiment(reps=120)
    assert measured.naive > 0.53                                  # the leak is present
    assert measured.purged < measured.naive                       # purging removes it
    assert abs(measured.purged_embargoed_symmetric - 0.5) < 0.02  # nothing left over
    assert measured.embargo == 2 * measured.horizon


# --------------------------------------------------------------------------- #
# 5.  Fees are per venue AND per series.  research/06 section 4.
#
# 13,353 of 13,486 Kalshi series charge makers nothing and 130 charge 0.25x base.
# A backtest that applies one number to all of them is wrong on both cohorts.
# --------------------------------------------------------------------------- #
def maker_fee_cents(price_cents: int, fee_type: str, multiplier: float = 1.0) -> float:
    """The fee model, recomputed here rather than imported, so the test cannot
    agree with the engine by sharing its mistake."""
    price = price_cents / 100.0
    theta = KALSHI_BASE_TAKER * multiplier * KALSHI_MAKER_RATIO[fee_type]
    return theta * price * (1.0 - price) * 100.0


def test_a_maker_fee_series_and_a_plain_quadratic_series_disagree_on_the_pnl():
    """Acceptance: fees are applied per series and CHANGE the answer.

    The fixture is priced fairly, so gross P&L is exactly zero on both runs and
    the entire difference in net P&L is the fee.  This is the whole maker-first
    doctrine in one number: on the 130 series that charge makers, resting a
    fairly-priced quote is a guaranteed slow loss.
    """
    free = run(build_db(fee_types=("quadratic",), **FAIR), BidSleeve())
    charged = run(build_db(fee_types=("quadratic_with_maker_fees",), **FAIR), BidSleeve())

    assert free.pessimistic.gross_pnl_cents == pytest.approx(0.0)
    assert charged.pessimistic.gross_pnl_cents == pytest.approx(0.0)
    assert free.pessimistic.fee_cents == pytest.approx(0.0)
    assert charged.pessimistic.fee_cents > 0.0
    assert free.pessimistic.net_pnl_cents != charged.pessimistic.net_pnl_cents
    assert free.digest() != charged.digest()


def test_the_maker_fee_charged_is_the_quadratic_fee_recomputed_by_hand():
    """0.07 base x 0.25 maker ratio x p(1-p), per contract, on every fill.

    Asserted against arithmetic done here rather than against whatever the
    engine printed -- a fee test that copies the engine's output tests nothing
    except that the engine is consistent with itself.
    """
    db = build_db(fee_types=("quadratic_with_maker_fees",), **FAIR)
    column = run(db, BidSleeve()).pessimistic

    contracts = FAIR["n_markets"] * FAIR["n_steps"] * BidSleeve().size
    per_contract = maker_fee_cents(FAIR["bid"], "quadratic_with_maker_fees")
    assert column.filled_contracts == pytest.approx(contracts)
    assert column.fee_cents == pytest.approx(contracts * per_contract)
    assert column.net_pnl_cents == pytest.approx(-contracts * per_contract)


def test_fees_are_charged_per_series_so_the_free_half_of_the_book_pays_nothing():
    """Per SERIES, not per run.  Markets alternate between a free series and a
    maker-fee one, so exactly half the contracts may be charged.  Applying one
    blended rate would land on the same total here by accident, which is why the
    single-series test above pins the rate itself."""
    db = build_db(fee_types=("quadratic", "quadratic_with_maker_fees"), **FAIR)
    column = run(db, BidSleeve()).pessimistic

    charged_contracts = FAIR["n_markets"] // 2 * FAIR["n_steps"] * BidSleeve().size
    per_contract = maker_fee_cents(FAIR["bid"], "quadratic_with_maker_fees")
    assert column.fee_cents == pytest.approx(charged_contracts * per_contract)


def test_a_fee_era_overrides_a_series_cache_that_describes_the_wrong_decade():
    """`series_cache` is upserted, so it carries TODAY's regime, not the tape's.

    Replaying 2024 flow under 2026 fees is a small, real look-ahead, and pinning
    the era is the fix.  Here the cache says makers are free and the era says
    they are not -- and the era has to win, or a pre-fee tape would be used to
    justify a post-fee strategy.
    """
    db = build_db(fee_types=("quadratic",), **FAIR)
    era = FeeSchedule(eras=(FeeEra(venue="kalshi", effective_from_us=T0 - 10 * HOUR,
                                   spec=FeeSpec.kalshi("quadratic_with_maker_fees", 1.0),
                                   label="test era"),))
    cached = run(db, BidSleeve())
    pinned = run(db, BidSleeve(), BacktestConfig(fees=era))

    contracts = FAIR["n_markets"] * FAIR["n_steps"] * BidSleeve().size
    assert cached.pessimistic.fee_cents == pytest.approx(0.0)
    assert pinned.pessimistic.fee_cents == pytest.approx(
        contracts * maker_fee_cents(FAIR["bid"], "quadratic_with_maker_fees"))
    assert cached.digest() != pinned.digest()


def test_an_era_that_begins_after_the_tape_ends_is_not_applied_to_it():
    """The mirror: a regime that had not started yet must not be charged, or
    every historical study silently pays a fee nobody was paying."""
    db = build_db(fee_types=("quadratic",), **FAIR)
    future = FeeSchedule(eras=(FeeEra(venue="kalshi", effective_from_us=T0 + 999 * HOUR,
                                      spec=FeeSpec.kalshi("quadratic_with_maker_fees", 1.0)),))
    assert run(db, BidSleeve(), BacktestConfig(fees=future)
               ).pessimistic.fee_cents == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 6.  Nothing in, nothing out.
#
# The failure mode being prevented is a run over a window that recorded no data
# reporting a clean zero and a passing shape, which reads like a flat strategy
# rather than like an absent one.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def empty_db():
    with Database(":memory:") as db:
        yield db


def test_an_empty_database_produces_an_empty_result_rather_than_raising(empty_db):
    """No markets, no tape, no settlements.  Every column is present and empty."""
    result = run(empty_db, BidSleeve())

    assert result.steps == 0
    assert result.orders == 0
    assert set(result.by_model) == set(ALL_MODELS)
    for model in ALL_MODELS:
        column = result.by_model[model]
        assert column.orders == 0
        assert column.filled_orders == 0
        assert column.filled_contracts == 0
        assert column.settlements == 0
        assert column.net_pnl_cents == 0
        assert column.fee_cents == 0
        assert column.open_contracts == 0
        assert not column.edge_ci_excludes_zero
        assert column.calibration_z == 0.0


def test_an_empty_run_still_digests_and_still_prints(empty_db):
    """The report is what a human reads before promoting; it must not depend on
    there having been trades."""
    result = run(empty_db, BidSleeve())
    assert len(result.digest()) == 64
    assert result.digest() == run(empty_db, BidSleeve()).digest()
    assert [m.value for m in ALL_MODELS] == list(result.as_dict()["by_model"])
    assert "PESSIMISTIC" in result.report()


def test_an_empty_backtest_cannot_pass_the_promotion_gate(empty_db):
    """The point of the previous two tests.  Zero P&L is not a flat strategy, it
    is no evidence, and G2 has to say so out loud -- including that the
    adverse-fill calibration never got a sample."""
    verdict = gate2_verdict(run(empty_db, BidSleeve()))
    assert not verdict.passed
    assert any("simulated_settlements 0" in r for r in verdict.reasons)
    assert any("includes zero" in r for r in verdict.reasons)
    assert any("insufficient sample" in r for r in verdict.reasons)


def test_the_leakage_suite_survives_an_empty_database(empty_db):
    """It runs the engine too, so it inherits every empty-input edge case.  A
    crash here would be indistinguishable from "no leakage found"."""
    report = run_leakage_suite(empty_db, BidSleeve(), BacktestConfig(), purge_reps=30)
    assert tuple(f.check for f in report.findings) == LEAKAGE_CHECKS
    assert report.passed, report.report()


def test_a_database_with_quotes_but_no_tape_reports_orders_and_no_fills():
    """The half-empty case, which is what a thin market actually looks like.

    R6.7d: an order that never met flow is CENSORED, not evidence of a
    non-fill -- so it must show up as an order with nothing behind it, never be
    dropped from the count.
    """
    db = build_db(n_markets=3, n_steps=2, prints=())
    result = run(db, BidSleeve())

    assert result.orders == 3 * 2
    for model in ALL_MODELS:
        assert result.by_model[model].orders == result.orders
        assert result.by_model[model].filled_orders == 0
        assert result.by_model[model].settlements == 0
