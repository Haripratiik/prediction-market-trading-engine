"""Acceptance for backtest/validate.py -- the harness that must be able to say NO.

Every test here asks one question: could this harness be talked into reporting an
edge that is not there?  The four ways it could, and a section each:

  * it scores LEGS instead of STRUCTURES, and mutual exclusivity manufactures a
    win rate out of arithmetic (`independence`)
  * it counts a partial fill as a locked basket, or prices the orphan for free
    (`orphan`)
  * it reads the future -- a book from after the decision instant, or a fill
    from a stretch of tape the recorder was silent for (`pointintime`)
  * it compares the strategy against nothing, so any number looks like edge
    (`control`)

Numeric expectations are written as the arithmetic that produces them, never as
a constant copied out of a previous run.

pytest's `tmp_path` raises PermissionError (WinError 5) on this machine, so every
fixture that needs a file uses `tempfile.mkdtemp()` with explicit cleanup.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence

import pytest

from backtest.fills import ALL_MODELS, FillModel
from backtest.validate import (
    Candidate,
    ReadOnlyArchive,
    StructureOutcome,
    ValidationConfig,
    bid_census,
    book_as_of,
    build_control,
    choose_decision_times,
    difference_ci,
    flow_stats,
    independence_unit,
    joint_fill_table,
    leg_order,
    locked_size_for,
    markout_cents,
    mean_ci,
    orphan_stats,
    run_validation,
    scan,
    settled_pnl_cents,
    simulate_structure,
    summarise,
    tape_window,
)
from core.db import Database
from core.math.contracts import FeeSpec, fee
from core.models import Event, Market, Series
from strategy.base import MarketSnapshot
from strategy.s2_shortbasket import S2Config, S2ShortBasket

T0 = 1_700_000_000_000_000
MINUTE = 60_000_000
HOUR = 3_600_000_000


# --------------------------------------------------------------------------- #
# Fixtures -- a synthetic archive whose arithmetic can be done by hand
# --------------------------------------------------------------------------- #
def _make_db(
    path: str,
    *,
    n_legs: int = 3,
    asks: Sequence[int] = (50, 35, 20),
    spread: int = 2,
    ask_size: float = 100.0,
    bid_size: float = 500.0,
    prints: Sequence[tuple[int, int, float, str]] = (),
    fee_type: str = "quadratic",
    mutually_exclusive: bool = True,
    settle: dict[str, int] | None = None,
    n_steps: int = 2,
    step_us: int = MINUTE,
) -> Database:
    """One MECE event with `n_legs` legs, a tape, and optional settlements.

    `asks` are the YES asks; the bid sits `spread` cents below.  With
    `spread = 2` the sleeve rests one tick inside at `ask - 1`, so the resting
    price of leg i is `asks[i] - 1` and `sum(rest px)` is `sum(asks) - n_legs`.
    Choosing asks that sum above 100 + n_legs is what makes the structure clear
    the margin gate.

    `prints` are absolute `(offset_us, yes_price_cents, size, taker_side)`
    tuples applied to leg 0..k in order of `leg_index` supplied by the caller
    through `prints_for`.
    """
    db = Database(path)
    db.upsert_series(
        [Series(ticker="KXS", fee_type=fee_type, fee_multiplier=1.0)],
        observed_at_us=T0 - HOUR,
    )
    tickers = [f"KXE-L{i}" for i in range(n_legs)]
    for step in range(n_steps):
        obs = T0 + step * step_us
        db.append_markets(
            [
                Market(
                    ticker=t,
                    event_ticker="KXE",
                    series_ticker="KXS",
                    title=f"leg {i}",
                    status="active",
                    yes_bid=asks[i] - spread,
                    yes_ask=asks[i],
                    yes_bid_size=bid_size,
                    yes_ask_size=ask_size,
                    volume=10_000.0,
                    volume_24h=10_000.0,
                    open_interest=10_000.0,
                    # 2 days, not 30: the sleeve also gates on ROLC
                    # (margin/capital * 365/days >= 0.15), and a 2.00c margin on
                    # 1.98 dollars of collateral held for a month annualises to
                    # 0.123 -- below the floor, so a 30-day fixture would be
                    # rejected for a reason that has nothing to do with fills.
                    close_at_us=T0 + 2 * 24 * HOUR,
                )
                for i, t in enumerate(tickers)
            ],
            observed_at_us=obs,
        )
        db.append_events(
            [
                Event(
                    event_ticker="KXE",
                    series_ticker="KXS",
                    category="Test",
                    title="a three-way race",
                    mutually_exclusive=mutually_exclusive,
                    collateral_return_type="MECNET",
                )
            ],
            observed_at_us=obs,
        )
    with db.tx() as c:
        for k, (offset, price, size, side) in enumerate(prints):
            leg = k % n_legs
            c.execute(
                "INSERT INTO trades(trade_id, ticker, traded_at_us, yes_price_cents,"
                " size, taker_side) VALUES (?,?,?,?,?,?)",
                (f"tr-{k:04d}", tickers[leg], T0 + offset, price, size, side),
            )
        for ticker, outcome in (settle or {}).items():
            c.execute(
                "INSERT INTO settlements(venue, ticker, settled_at_us, outcome, voided)"
                " VALUES ('kalshi', ?, ?, ?, 0)",
                (ticker, T0 + 10 * HOUR, outcome),
            )
    return db


def _prints_on(leg: int, n_legs: int, *, offset: int, price: int, size: float,
               side: str = "yes") -> list[tuple[int, int, float, str]]:
    """Prints landing on exactly one leg.  Padding keeps the round-robin honest."""
    out: list[tuple[int, int, float, str]] = []
    for i in range(n_legs):
        if i == leg:
            out.append((offset, price, size, side))
        else:
            out.append((offset, 1, 0.0, "no"))       # a zero-size no-op print
    return out


@pytest.fixture
def workdir() -> Iterator[str]:
    """`tmp_path` raises PermissionError (WinError 5) here; mkdtemp does not."""
    directory = tempfile.mkdtemp(prefix="pm-validate-")
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def cfg() -> ValidationConfig:
    return ValidationConfig(
        decision_times=(T0,),
        max_book_staleness_us=None,
        fill_horizon_us=30 * MINUTE,
        control_multiple=2,
        s2=S2Config(min_leg_depth=10.0, min_volume_24h=1.0),
    )


def _candidate(db: Database, cfg: ValidationConfig, at_us: int = T0) -> Candidate:
    real, _pool, _stats = scan(db, at_us, cfg, tape_end_us=at_us + 10 * HOUR)
    assert real, "fixture was supposed to produce a gate-passing structure"
    return real[0]


# --------------------------------------------------------------------------- #
# The independence unit -- requirement 1, the whole reason this file exists
# --------------------------------------------------------------------------- #
def test_the_per_leg_win_rate_of_a_complete_short_basket_is_forced_by_arithmetic():
    """A 4-leg short 'wins' 3 of 4 legs with no skill, so per-leg scoring is a lie.

    In a mutually-exclusive set exactly one leg resolves YES.  Short every leg
    and n-1 of the n short positions collect $1 whatever anybody believed about
    anything.  Scored per leg that is a 75% hit rate on a 4-leg basket and 90%
    on a 10-leg basket -- monotone in leg count, and containing exactly zero
    information about whether the basket made money.  Money is only made or lost
    at the STRUCTURE level, which is why every statistic in the harness is
    computed there.
    """
    for n in (2, 3, 4, 5, 10):
        o = StructureOutcome(
            cohort="real", model=FillModel.PESSIMISTIC, event_ticker="E",
            decided_at_us=T0, n_legs=n, size=1, margin_cents=1.0,
            filled=tuple([1.0] * n), matched=1.0, target=1.0, legs_touched=n,
            net_cents=-42.0, resolution="flatten", flatten_cents=-42.0,
            complete_cents=None, orphan_leg_cost_cents=tuple([0.0] * n),
            first_fill_at_us=T0,
        )
        u = independence_unit([o])
        assert u.mechanical_leg_win_rate == pytest.approx((n - 1) / n)
        # the structure lost 42 cents; the leg-level number says it won n-1 of n
        assert u.structure_win_rate == 0.0


def test_scoring_legs_instead_of_structures_shrinks_the_interval_by_root_n():
    """A leg-scored CI is ~sqrt(legs/structure) too narrow, so a gate fires early.

    Counting one structure's outcome once per leg leaves the MEAN alone and
    divides the standard error by sqrt(legs per structure).  On 4-leg baskets
    that is a 2x narrower interval, which is the difference between "includes
    zero" and "promote this sleeve".

    The measured ratio is 2.10 rather than exactly 2.00 because the sample sd
    carries the n-1 correction: replicating 8 samples four times gives
    sd_32/sd_8 = sqrt(4*7/31) = 0.950, so the widths differ by 2/0.950.
    """
    n_legs = 4
    outcomes = [
        StructureOutcome(
            cohort="real", model=FillModel.PESSIMISTIC, event_ticker=f"E{i}",
            decided_at_us=T0, n_legs=n_legs, size=1, margin_cents=1.0,
            filled=tuple([1.0] * n_legs), matched=1.0, target=1.0,
            legs_touched=n_legs, net_cents=float(v), resolution="flatten",
            flatten_cents=float(v), complete_cents=None,
            orphan_leg_cost_cents=tuple([0.0] * n_legs), first_fill_at_us=T0,
        )
        for i, v in enumerate([-10, 12, -8, 14, -6, 16, -4, 18])
    ]
    u = independence_unit(outcomes)
    assert u.variance_inflation == pytest.approx(n_legs)
    honest = u.honest_ci.high - u.honest_ci.low
    fake = u.leg_scored_ci.high - u.leg_scored_ci.low
    assert u.honest_ci.mean == pytest.approx(u.leg_scored_ci.mean)
    n_struct = len(outcomes)
    bessel = (n_legs * (n_struct - 1) / (n_legs * n_struct - 1)) ** 0.5
    assert honest / fake == pytest.approx((n_legs ** 0.5) / bessel, rel=1e-6)
    assert honest / fake > 2.0


def test_one_structure_contributes_exactly_one_sample_however_many_legs_it_has():
    """Re-quoting a 12-leg event must not buy twelve degrees of freedom.

    `settlements` in backtest/engine.py counts one sample per TICKER.  For S2
    that is twelve samples for one event and one decision, and the CI a
    promotion gate reads is then computed on an n that never existed.
    """
    big = StructureOutcome(
        cohort="real", model=FillModel.PESSIMISTIC, event_ticker="E",
        decided_at_us=T0, n_legs=12, size=1, margin_cents=1.0,
        filled=tuple([1.0] * 12), matched=1.0, target=1.0, legs_touched=12,
        net_cents=5.0, resolution="flatten", flatten_cents=5.0,
        complete_cents=None, orphan_leg_cost_cents=tuple([0.0] * 12),
        first_fill_at_us=T0,
    )
    col = summarise("real", FillModel.PESSIMISTIC, [big])
    assert col.per_structure.n == 1
    assert col.independence.n_structures == 1
    assert col.independence.n_legs == 12


# --------------------------------------------------------------------------- #
# Joint fill -- requirement 2
# --------------------------------------------------------------------------- #
def test_a_basket_needs_every_leg_and_one_missing_leg_makes_it_an_orphan(workdir, cfg):
    """n simultaneous maker fills is a conjunction; n-1 of them is a naked bet.

    The fixture fills leg 0 alone.  A harness that called that a filled
    structure would report the basket's locked margin on a position that has no
    hedge and no cap other than the $1 the single short leg can lose.
    """
    db = _make_db(
        os.path.join(workdir, "a.db"),
        asks=(50, 35, 20),
        prints=_prints_on(0, 3, offset=MINUTE, price=60, size=5000.0, side="yes"),
    )
    try:
        cand = _candidate(db, cfg)
        out = simulate_structure(db, cand, FillModel.REALISTIC, cfg)
        assert out.legs_touched == 1
        assert out.orphaned and not out.all_filled
        assert out.matched == 0.0, "no basket is locked while a leg is missing"
        table = {r.bucket: r for r in joint_fill_table([out])}
        assert table["3"].p_all == 0.0
        assert table["3"].p_orphan_given_any == 1.0
    finally:
        db.close()


def test_every_leg_filling_locks_the_designed_margin_and_nothing_more(workdir, cfg):
    """A complete basket earns its margin exactly -- not the sum of leg premia.

    asks (50, 35, 20) with a 2c spread rest at (49, 34, 19), so sum(px) = 1.02
    and, at zero maker fee on the 'quadratic' fee type, margin = 2.00 cents per
    basket.  With `matched` baskets locked the structure's P&L is
    matched * 2.00 cents and the orphan bill is zero.
    """
    prints: list[tuple[int, int, float, str]] = []
    for leg in range(3):
        prints.extend(_prints_on(leg, 3, offset=MINUTE, price=99, size=5000.0,
                                 side="yes"))
    db = _make_db(os.path.join(workdir, "b.db"), asks=(50, 35, 20), prints=prints)
    try:
        cand = _candidate(db, cfg)
        assert cand.prices_cents == (49, 34, 19)
        assert cand.margin_cents == pytest.approx(49 + 34 + 19 - 100)
        out = simulate_structure(db, cand, FillModel.OPTIMISTIC, cfg)
        assert out.all_filled and not out.orphaned
        assert out.matched == float(cand.size)
        assert out.net_cents == pytest.approx(out.matched * cand.margin_cents)
    finally:
        db.close()


def test_a_dead_book_and_a_deep_queue_are_reported_as_different_failures(workdir):
    """"Nobody traded" and "we were behind 300 contracts" need different fixes.

    A leg nobody lifted is a dead book and no order placement changes it.  A leg
    with flow that never cleared the displayed queue is at least addressable, by
    quoting earlier or accepting a worse price.  Reporting one pooled fill rate
    hides which of the two the strategy is actually up against.
    """
    cfg = ValidationConfig(decision_times=(T0,), max_book_staleness_us=None,
                           fill_horizon_us=30 * MINUTE,
                           s2=S2Config(min_leg_depth=10.0))
    # A 1-cent spread means every leg JOINS the ask rather than improving it,
    # so all three queue behind the 4000 displayed contracts.  Only leg 0 sees
    # any prints, and only 12 contracts of them.
    db = _make_db(
        os.path.join(workdir, "flow.db"), asks=(51, 35, 20), spread=1,
        ask_size=4000.0,
        prints=_prints_on(0, 3, offset=MINUTE, price=99, size=12.0, side="yes"),
    )
    try:
        cand = _candidate(db, cfg)
        out = simulate_structure(db, cand, FillModel.PESSIMISTIC, cfg)
        f = flow_stats([out])
        assert f.n_legs == 3
        assert f.legs_zero_flow == 2                # the two silent legs
        assert f.legs_flow_beats_queue == 0         # 12 contracts < 4000 queued
        assert out.legs_touched == 0, "queued out is still a non-fill"
        assert f.median_queue_ahead == pytest.approx(4000.0)
        assert f.median_credited == pytest.approx(0.0)   # 2 of 3 legs saw nothing
    finally:
        db.close()


def test_joint_fill_is_reported_per_leg_count_because_it_is_mechanically_n_dependent():
    """Pooling leg counts hides that a 12-leg basket essentially never completes.

    P(all n fill) is a conjunction, so it falls in n for any fill process that is
    not perfectly correlated across legs.  A single pooled number over a mixed
    population is an average of two different difficulties and comparing it
    across cohorts compares leg-count mixes, not edge.
    """
    def out(n: int, touched: int) -> StructureOutcome:
        return StructureOutcome(
            cohort="real", model=FillModel.PESSIMISTIC, event_ticker="E",
            decided_at_us=T0, n_legs=n, size=1, margin_cents=1.0,
            filled=tuple([1.0] * touched + [0.0] * (n - touched)),
            matched=0.0, target=1.0, legs_touched=touched, net_cents=0.0,
            resolution="flatten", flatten_cents=0.0, complete_cents=None,
            orphan_leg_cost_cents=tuple([0.0] * n), first_fill_at_us=T0,
        )
    rows = {r.bucket: r for r in joint_fill_table(
        [out(3, 3), out(3, 1), out(4, 2), out(7, 7), out(9, 0)])}
    assert rows["3"].p_all == pytest.approx(0.5)
    assert rows["4"].p_all == 0.0
    assert rows["5+"].p_all == pytest.approx(0.5)       # the 7-leg one filled
    assert rows["5+"].n_none == 1                       # the 9-leg one did not
    assert rows["all"].n_structures == 5


# --------------------------------------------------------------------------- #
# Orphan cost -- requirement 3
# --------------------------------------------------------------------------- #
def test_unwinding_one_orphan_leg_costs_the_spread_plus_the_taker_fee(workdir):
    """The orphan bill is real money and must be charged at the taker fee.

    Short YES at p, flatten by BUYING yes at the ask: cost per contract is
    (ask - p) + 100*fee(ask/100, taker).  With ask = 50 and p = 49 on a
    fee_multiplier 1.0 quadratic series that is
    1 + 100*0.07*0.50*0.50 = 1 + 1.75 = 2.75 cents -- against a 2.00 cent
    basket margin.  One cross erases the whole structure's designed profit.
    """
    cfg = ValidationConfig(
        decision_times=(T0,), max_book_staleness_us=None,
        fill_horizon_us=30 * MINUTE, s2=S2Config(min_leg_depth=10.0),
    )
    db = _make_db(
        os.path.join(workdir, "c.db"), asks=(50, 35, 20),
        prints=_prints_on(0, 3, offset=MINUTE, price=99, size=5000.0, side="yes"),
    )
    try:
        cand = _candidate(db, cfg)
        out = simulate_structure(db, cand, FillModel.REALISTIC, cfg)
        expected = (50 - 49) + 100.0 * fee(0.50, FeeSpec.kalshi("quadratic", 1.0),
                                           is_maker=False)
        assert expected == pytest.approx(2.75)
        assert out.orphan_leg_cost_cents[0] == pytest.approx(expected)
        stats = orphan_stats([out])
        assert stats.n_orphans == 1
        assert stats.median_cross_cents == pytest.approx(expected)
        # the ratio the strategy lives or dies by
        assert stats.cross_over_margin == pytest.approx(expected / cand.margin_cents)
        assert stats.cross_over_margin > 1.0
    finally:
        db.close()


def test_a_partially_filled_structure_is_never_credited_with_a_locked_margin(workdir, cfg):
    """`matched` is min over legs, so one filled leg locks zero baskets.

    Crediting max(filled) or sum(filled) would book the arbitrage margin on a
    position that is a naked short.  That is the accounting error that turns
    S2's designed +1c into a live -104.7c, which is what the one orphaned
    structure in the recorded archive actually did.
    """
    db = _make_db(
        os.path.join(workdir, "d.db"), asks=(50, 35, 20),
        prints=_prints_on(0, 3, offset=MINUTE, price=99, size=5000.0, side="yes"),
    )
    try:
        cand = _candidate(db, cfg)
        out = simulate_structure(db, cand, FillModel.REALISTIC, cfg)
        assert out.matched == 0.0, "min over legs is zero while a leg is empty"
        assert out.target > 0.0, "one leg did fill"
        assert out.net_cents < 0.0, "an orphan cannot be profit"
        # crediting max(filled) instead would have booked target*margin
        assert out.net_cents < out.target * cand.margin_cents
    finally:
        db.close()


def test_the_orphan_is_resolved_with_the_cheaper_of_flatten_and_complete(workdir, cfg):
    """The strategy is handed a prescient executor and must still be judged.

    An orphan can be closed two ways: flatten the filled leg by crossing its
    ask, or complete the basket by crossing the missing legs' bids.  The harness
    credits `max(flatten, complete)` so that a negative result cannot be blamed
    on a naive unwind policy.  If the edge dies with the better remedy applied
    on every single structure, no execution policy rescues it.
    """
    db = _make_db(
        os.path.join(workdir, "e.db"), asks=(50, 35, 20),
        prints=_prints_on(0, 3, offset=MINUTE, price=99, size=5000.0, side="yes"),
    )
    try:
        cand = _candidate(db, cfg)
        out = simulate_structure(db, cand, FillModel.REALISTIC, cfg)
        assert out.flatten_cents is not None and out.complete_cents is not None
        assert out.net_cents == pytest.approx(max(out.flatten_cents,
                                                  out.complete_cents))
        assert out.resolution in {"flatten", "complete"}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Point in time -- the harness must not read the future
# --------------------------------------------------------------------------- #
def test_the_bulk_point_in_time_read_agrees_with_the_audited_accessor(workdir):
    """`book_as_of` is an optimisation of `Database.latest_market`, not a rewrite.

    The per-ticker accessor is the one backtest/leakage.py audits.  Replacing it
    with a window function for speed is only safe if the two return the same
    row for every ticker at every instant, so that equality is pinned here.
    """
    db = _make_db(os.path.join(workdir, "f.db"), asks=(50, 35, 20), n_steps=5)
    try:
        for step in range(6):
            at = T0 + step * MINUTE
            bulk = book_as_of(db, at)
            for ticker in ("KXE-L0", "KXE-L1", "KXE-L2"):
                row = db.latest_market(ticker, as_of_us=at)
                if row is None:
                    assert ticker not in bulk
                    continue
                market, obs = bulk[ticker]
                assert obs == row["observed_at_us"]
                assert market.yes_ask == row["yes_ask"]
                assert market.yes_bid == row["yes_bid"]
    finally:
        db.close()


def test_a_snapshot_recorded_after_the_decision_instant_is_never_visible(workdir):
    """One row from the future is enough to invent an edge that does not exist.

    The fixture moves the book on step 1.  A read as of step 0 that returned the
    step-1 quote would let the sleeve price a basket at prices nobody was
    showing when it decided.
    """
    path = os.path.join(workdir, "g.db")
    db = _make_db(path, asks=(50, 35, 20), n_steps=1)
    try:
        db.append_markets(
            [Market(ticker="KXE-L0", event_ticker="KXE", series_ticker="KXS",
                    status="active", yes_bid=90, yes_ask=92, yes_bid_size=500.0,
                    yes_ask_size=100.0, volume_24h=10_000.0,
                    close_at_us=T0 + 2 * 24 * HOUR)],
            observed_at_us=T0 + MINUTE,
        )
        now = book_as_of(db, T0)
        later = book_as_of(db, T0 + MINUTE)
        assert now["KXE-L0"][0].yes_ask == 50
        assert later["KXE-L0"][0].yes_ask == 92
    finally:
        db.close()


def test_decision_instants_outside_the_recorded_tape_window_are_refused(workdir):
    """Zero fills from a silent recorder is CENSORING, not an unfillable market.

    The archive under test snapshots for hours before the trade recorder starts.
    An order rested in that gap reports a clean 0% fill rate, and a harness that
    accepted it would conclude that maker fills never happen for a reason that
    has nothing to do with the market.
    """
    db = _make_db(
        os.path.join(workdir, "h.db"), asks=(50, 35, 20), n_steps=6,
        step_us=10 * MINUTE,
        # two prints, so the tape window has a positive width: with a single
        # print lo == hi and NOTHING is inside it, which is a different
        # (also honest) refusal from the one under test here
        prints=[(15 * MINUTE, 99, 10.0, "yes"), (45 * MINUTE, 99, 10.0, "yes")],
    )
    try:
        lo, hi, n = tape_window(db)
        assert n > 0 and lo is not None and hi is not None
        chosen = choose_decision_times(
            db, ValidationConfig(min_sweep_rows=1, max_decision_times=10))
        assert chosen, "some sweep should sit inside the tape window"
        assert all(lo <= t < hi for t in chosen)
        assert T0 not in chosen, "the pre-tape sweep must be refused"
    finally:
        db.close()


def test_an_archive_with_no_tape_at_all_reports_nothing_rather_than_zero_edge(workdir):
    """No prints means fills are unmeasurable, which is not the same as no fills."""
    db = _make_db(os.path.join(workdir, "i.db"), asks=(50, 35, 20), prints=())
    try:
        assert tape_window(db) == (None, None, 0)
        assert choose_decision_times(db, ValidationConfig()) == ()
        rep = run_validation(db, ValidationConfig())
        assert rep.verdict()[0] == "UNDECIDABLE"
        assert any("no snapshot sweep" in w for w in rep.warnings)
        assert isinstance(rep.report(), str)
    finally:
        db.close()


def test_a_markout_is_refused_when_no_quote_lands_near_its_own_horizon(workdir):
    """A mark read hours past its horizon measures time passing, not the trade.

    shadow/engine.py bounds staleness at half the horizon for exactly this
    reason; the adverse-fill gate must see None, not a stale quote, or its rate
    drifts toward 0.5 -- which is the failure signature the gate exists to catch.
    """
    db = _make_db(os.path.join(workdir, "j.db"), asks=(50, 35, 20), n_steps=2,
                  step_us=5 * MINUTE)
    try:
        # a 5-minute horizon allows a reference at most 2.5 minutes late, and
        # the T0 + 5 min snapshot is exactly on time: mid = (48 + 50)/2 = 49,
        # so a short resting at 49 marks out flat
        assert markout_cents(db, "KXE-L0", T0, 49, horizon_us=5 * MINUTE) == \
            pytest.approx(49 - (48 + 50) / 2.0)
        # a 60-minute horizon admits a reference no later than T0 + 90 min, and
        # the archive has nothing there at all
        assert markout_cents(db, "KXE-L0", T0, 49, horizon_us=60 * MINUTE) is None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The gate: what the harness will and will not call a candidate
# --------------------------------------------------------------------------- #
def test_a_book_that_is_not_flagged_mutually_exclusive_is_never_a_candidate(workdir, cfg):
    """Without mutual exclusivity an n-leg short is capped at $n, not $1.

    `KXBTCD-26AUG2817` in the live archive lists 50 NESTED THRESHOLD markets and
    is flagged mutually_exclusive = 0.  S2 once sized a 21-leg short against it
    and reported a margin ten times the maximum payout of the instrument.
    """
    db = _make_db(os.path.join(workdir, "k.db"), asks=(50, 35, 20),
                  mutually_exclusive=False)
    try:
        real, pool, stats = scan(db, T0, cfg, tape_end_us=T0 + HOUR)
        assert real == [] and pool == []
        assert stats.events_flagged_me == 0
    finally:
        db.close()


def test_a_basket_priced_below_par_fails_the_margin_gate_and_feeds_the_control(workdir, cfg):
    """sum(rest px) <= 1 is not arbitrage, it is a paid-for directional short.

    asks (40, 30, 20) rest at (39, 29, 19) = 0.87, so the 'margin' is -13 cents
    per basket.  It must land in the control pool, never in the real cohort.
    """
    db = _make_db(os.path.join(workdir, "l.db"), asks=(40, 30, 20))
    try:
        real, pool, stats = scan(db, T0, cfg, tape_end_us=T0 + HOUR)
        assert real == []
        assert len(pool) == 1
        assert pool[0].margin_cents == pytest.approx(39 + 29 + 19 - 100)
        assert stats.failed_margin == 1 and stats.passed_gate == 0
    finally:
        db.close()


def test_a_maker_fee_series_raises_the_hurdle_that_a_zero_fee_series_does_not(workdir, cfg):
    """13,385 of 13,518 series charge makers nothing; the other 133 are the trap.

    On 'quadratic' the maker fee is 0 and a 1c gross margin is a 1c net margin.
    On 'quadratic_with_maker_fees' the same basket pays
    0.25 * 0.07 * sum p(1-p) per basket, which at rest prices (49, 34, 19) is
    0.0175 * (0.2499 + 0.2244 + 0.1539) = 0.011 dollars = 1.10 cents -- more
    than the whole 2.00 cent gross margin leaves after two legs.
    """
    free = _make_db(os.path.join(workdir, "m0.db"), asks=(50, 35, 20),
                    fee_type="quadratic")
    paid = _make_db(os.path.join(workdir, "m1.db"), asks=(50, 35, 20),
                    fee_type="quadratic_with_maker_fees")
    try:
        c_free = _candidate(free, cfg)
        c_paid = _candidate(paid, cfg)
        assert c_free.margin_cents == pytest.approx(2.0)
        by_hand = 2.0 - 100.0 * sum(
            0.25 * 0.07 * (p / 100.0) * (1.0 - p / 100.0) for p in (49, 34, 19)
        )
        assert c_paid.margin_cents == pytest.approx(by_hand)
        assert c_paid.margin_cents < c_free.margin_cents
    finally:
        free.close()
        paid.close()


def test_the_control_cohort_is_sized_by_the_sleeves_own_rule(workdir, cfg):
    """A control sized differently from the real cohort is not a control.

    `locked_size_for` restates `S2ShortBasket._locked_size`; if the two ever
    diverge the null comparison silently compares two different order sizes,
    and order size drives fill probability directly.
    """
    db = _make_db(os.path.join(workdir, "n.db"), asks=(50, 35, 20))
    try:
        book = book_as_of(db, T0)
        legs = [book[f"KXE-L{i}"][0] for i in range(3)]
        snap = MarketSnapshot(now_us=T0, markets=tuple(legs),
                              bankroll_cents=cfg.bankroll_cents)
        capital = 3 - (49 + 34 + 19) / 100.0
        sleeve = S2ShortBasket(cfg=cfg.s2)
        assert locked_size_for(legs, capital, cfg.bankroll_cents, cfg.s2) == \
            sleeve._locked_size(legs, capital, snap)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Control -- requirement 5
# --------------------------------------------------------------------------- #
def test_the_control_matches_the_real_cohorts_leg_counts(workdir):
    """P(all n fill) falls in n, so an unmatched control compares difficulties.

    Without leg-count matching a 3-leg real cohort against a 12-leg control
    would 'beat' it on joint fill for purely combinatorial reasons and the
    harness would report edge where there is only arithmetic.
    """
    cfg = ValidationConfig(decision_times=(T0,), max_book_staleness_us=None,
                           control_multiple=4, s2=S2Config(min_leg_depth=10.0))
    good = _make_db(os.path.join(workdir, "o0.db"), asks=(50, 35, 20))
    try:
        real, _p, _s = scan(good, T0, cfg, tape_end_us=T0 + HOUR)
        assert real
        pool = [
            Candidate(cohort="control-pool", decided_at_us=T0, event_ticker="X",
                      legs=tuple(f"X-{i}" for i in range(6)),
                      prices_cents=(20, 20, 20, 20, 20, 20), size=0,
                      sum_px_cents=120.0, margin_cents=-40.0,
                      fee_spec=FeeSpec.kalshi("quadratic", 1.0),
                      book_bid=(19,) * 6, book_ask=(20,) * 6,
                      book_ask_size=(100.0,) * 6, staleness_us=(0,) * 6)
        ]
        control = build_control(good, pool, real, cfg)
        assert control
        assert {c.n_legs for c in control} == {c.n_legs for c in real}
        assert len(control) == len(real) * cfg.control_multiple
    finally:
        good.close()


def test_the_control_is_reproducible_from_its_seed(workdir):
    """A control that changes between runs cannot falsify anything."""
    cfg = ValidationConfig(decision_times=(T0,), max_book_staleness_us=None,
                           control_multiple=3, control_seed=7,
                           s2=S2Config(min_leg_depth=10.0))
    db = _make_db(os.path.join(workdir, "p.db"), asks=(50, 35, 20))
    try:
        real, _p, _s = scan(db, T0, cfg, tape_end_us=T0 + HOUR)
        pool = [
            Candidate(cohort="control-pool", decided_at_us=T0, event_ticker=f"X{k}",
                      legs=tuple(f"X{k}-{i}" for i in range(5)),
                      prices_cents=(10, 20, 30, 15, 12), size=0,
                      sum_px_cents=87.0, margin_cents=-13.0,
                      fee_spec=FeeSpec.kalshi("quadratic", 1.0),
                      book_bid=(9, 19, 29, 14, 11), book_ask=(10, 20, 30, 15, 12),
                      book_ask_size=(100.0,) * 5, staleness_us=(0,) * 5)
            for k in range(4)
        ]
        a = build_control(db, pool, real, cfg)
        b = build_control(db, pool, real, cfg)
        assert [c.legs for c in a] == [c.legs for c in b]
    finally:
        db.close()


def test_a_difference_that_straddles_zero_is_reported_as_no_edge():
    """'Real beats control' is a claim about a difference, so it needs its CI.

    Two samples with the same mean and any spread produce an interval covering
    zero.  The harness must call that indistinguishable rather than reading the
    sign of the point estimate.
    """
    a = [1.0, -1.0, 2.0, -2.0, 3.0, -3.0]
    b = [1.5, -1.5, 2.5, -2.5, 3.5, -3.5]
    d = difference_ci(a, b)
    assert d.low < 0.0 < d.high
    assert not d.excludes_zero


def test_a_real_cohort_that_only_matches_the_null_cannot_be_called_an_edge(workdir):
    """The end-to-end null test, on an archive where nothing fills.

    With no tape, real and control both score exactly zero, the difference is
    zero, and the only honest verdict is UNDECIDABLE -- never "no losses, so
    promote".
    """
    db = _make_db(os.path.join(workdir, "q.db"), asks=(50, 35, 20), prints=())
    try:
        rep = run_validation(db, ValidationConfig(
            decision_times=(T0,), max_book_staleness_us=None,
            s2=S2Config(min_leg_depth=10.0)))
        v, reasons = rep.verdict()
        assert v == "UNDECIDABLE"
        assert any("structure" in r for r in reasons)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# P&L accounting
# --------------------------------------------------------------------------- #
def test_settlement_pnl_pays_one_dollar_on_exactly_one_leg_of_a_mece_set(workdir, cfg):
    """Held to settlement, a complete short basket returns sum(px) - $1, no more.

    Short YES leg i at p_i collects p_i and pays 100 only if leg i resolves YES.
    Over a mutually-exclusive set with a listed winner that is
    sum(p) - 100 = margin cents per basket -- so the arbitrage claim is exactly
    the claim that every leg filled, and nothing else.
    """
    db = _make_db(
        os.path.join(workdir, "r.db"), asks=(50, 35, 20),
        prints=[p for leg in range(3)
                for p in _prints_on(leg, 3, offset=MINUTE, price=99, size=5000.0,
                                    side="yes")],
        settle={"KXE-L0": 1, "KXE-L1": 0, "KXE-L2": 0},
    )
    try:
        cand = _candidate(db, cfg)
        out = simulate_structure(db, cand, FillModel.OPTIMISTIC, cfg)
        assert out.all_filled
        pnl = settled_pnl_cents(cand, out, {
            "KXE-L0": (T0, 1, False), "KXE-L1": (T0, 0, False),
            "KXE-L2": (T0, 0, False),
        })
        assert pnl is not None
        # every leg filled `size` contracts, so the basket count is `size`
        assert pnl == pytest.approx(out.matched * (49 + 34 + 19 - 100))
    finally:
        db.close()


def test_settlement_pnl_is_refused_when_any_leg_of_the_basket_is_unsettled(workdir, cfg):
    """A partially settled basket has an unknown winner, so it has no P&L yet.

    Reporting one would mean guessing which leg pays $1, and the guess is worth
    100 cents per contract.  None is the honest answer and the report says the
    number cannot be computed.
    """
    db = _make_db(os.path.join(workdir, "s.db"), asks=(50, 35, 20),
                  prints=[p for leg in range(3)
                          for p in _prints_on(leg, 3, offset=MINUTE, price=99,
                                              size=5000.0, side="yes")])
    try:
        cand = _candidate(db, cfg)
        out = simulate_structure(db, cand, FillModel.OPTIMISTIC, cfg)
        partial = {"KXE-L0": (T0, 1, False)}          # legs 1 and 2 not settled
        assert settled_pnl_cents(cand, out, partial) is None
        assert settled_pnl_cents(cand, out, {}) is None
    finally:
        db.close()


def test_a_voided_leg_makes_the_basket_unpriceable_rather_than_free_money(workdir, cfg):
    """A void returns the stake; treating it as a $1 collection is invented P&L."""
    db = _make_db(os.path.join(workdir, "t.db"), asks=(50, 35, 20),
                  prints=[p for leg in range(3)
                          for p in _prints_on(leg, 3, offset=MINUTE, price=99,
                                              size=5000.0, side="yes")])
    try:
        cand = _candidate(db, cfg)
        out = simulate_structure(db, cand, FillModel.OPTIMISTIC, cfg)
        voided = {"KXE-L0": (T0, 1, True), "KXE-L1": (T0, 0, False),
                  "KXE-L2": (T0, 0, False)}
        assert settled_pnl_cents(cand, out, voided) is None
    finally:
        db.close()


def test_the_three_fill_models_never_disagree_about_which_way_is_more_generous(workdir, cfg):
    """PLAN.md 6.7: pessimistic <= realistic <= optimistic, on contracts filled.

    The bracket only bounds anything if it is ordered.  Checked on the structure
    aggregate rather than on one order, because the harness's unit is the
    structure and an aggregation could in principle invert what the per-order
    invariant guarantees.
    """
    db = _make_db(
        os.path.join(workdir, "u.db"), asks=(50, 35, 20), ask_size=40.0,
        prints=[p for leg in range(3)
                for p in _prints_on(leg, 3, offset=MINUTE, price=49, size=60.0,
                                    side="yes")],
    )
    try:
        cand = _candidate(db, cfg)
        totals = [
            sum(simulate_structure(db, cand, m, cfg).filled) for m in ALL_MODELS
        ]
        assert totals[0] <= totals[1] <= totals[2]
    finally:
        db.close()


def test_a_zero_size_structure_is_never_counted_as_a_locked_basket(workdir):
    """Size floored to zero by depth or capital means no position at all."""
    cfg = ValidationConfig(decision_times=(T0,), max_book_staleness_us=None,
                           s2=S2Config(min_leg_depth=1.0, max_depth_fraction=0.0))
    db = _make_db(os.path.join(workdir, "v.db"), asks=(50, 35, 20), bid_size=10.0)
    try:
        real, _p, _s = scan(db, T0, cfg, tape_end_us=T0 + HOUR)
        assert real == [], "a size-zero structure is not a structure"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def test_a_single_structure_never_produces_a_confidence_interval():
    """One sample has no spread; pretending otherwise promotes on n = 1."""
    one = mean_ci([3.0])
    assert one.n == 1 and one.low == one.high == 3.0
    assert not one.excludes_zero
    assert mean_ci([]).n == 0


def test_the_interval_is_the_normal_approximation_written_out_by_hand():
    """z = 1.959963984540054, half-width = z*sd/sqrt(n).  No hidden estimator."""
    xs = [1.0, 2.0, 3.0, 4.0]
    got = mean_ci(xs)
    import statistics as st
    half = 1.959963984540054 * st.stdev(xs) / (len(xs) ** 0.5)
    assert got.mean == pytest.approx(2.5)
    assert got.low == pytest.approx(2.5 - half)
    assert got.high == pytest.approx(2.5 + half)


# --------------------------------------------------------------------------- #
# Read-only access -- the live recorder owns data/pm.db
# --------------------------------------------------------------------------- #
def test_the_archive_handle_cannot_write_to_the_file_it_reads(workdir):
    """A live recorder owns the archive; the harness must be unable to touch it.

    `Database.__init__` runs `migrate()`, which writes.  `ReadOnlyArchive` opens
    with SQLite's `mode=ro`, so an accidental INSERT raises instead of corrupting
    a recording session.
    """
    path = os.path.join(workdir, "w.db")
    db = _make_db(path, asks=(50, 35, 20))
    db.close()
    with ReadOnlyArchive(path) as archive:
        assert archive.conn.execute(
            "SELECT COUNT(*) n FROM market_snapshots").fetchone()["n"] > 0
        with pytest.raises(sqlite3.OperationalError):
            archive.conn.execute(
                "INSERT INTO settlements(venue, ticker, settled_at_us, outcome)"
                " VALUES ('kalshi','X',1,1)")
        with pytest.raises(sqlite3.OperationalError):
            archive.conn.execute("DELETE FROM trades")


def test_a_point_in_time_read_without_an_as_of_is_rejected_outright(workdir):
    """A read with no `as_of_us` is look-ahead by default, so it must not exist."""
    path = os.path.join(workdir, "x.db")
    db = _make_db(path, asks=(50, 35, 20))
    db.close()
    with ReadOnlyArchive(path) as archive:
        with pytest.raises(ValueError):
            archive.latest_market("KXE-L0")
        assert archive.latest_market("KXE-L0", as_of_us=T0) is not None


def test_the_archive_handle_reads_the_same_rows_a_database_handle_does(workdir):
    """The read-only shim is a Database substitute, not a different reader."""
    path = os.path.join(workdir, "y.db")
    db = _make_db(path, asks=(50, 35, 20))
    expected = book_as_of(db, T0)
    db.close()
    with ReadOnlyArchive(path) as archive:
        got = book_as_of(archive, T0)
        assert sorted(got) == sorted(expected)
        for t in got:
            assert got[t][0].yes_ask == expected[t][0].yes_ask
        assert archive.get_series("KXS") is not None


# --------------------------------------------------------------------------- #
# The census and the end-to-end report
# --------------------------------------------------------------------------- #
def test_the_census_separates_the_liquidity_fantasy_from_the_real_opportunity(workdir):
    """A scan without the depth filter is pricing fills nobody is offering.

    research/05 4.3: the liquidity filter took 3,793 maker "opportunities" down
    to 504.  The census must report BOTH numbers, because the unfiltered one is
    the headline that made a naive scan call 78% of MECE events profitable.
    """
    cfg = ValidationConfig(decision_times=(T0,), max_book_staleness_us=None,
                           s2=S2Config(min_leg_depth=1_000_000.0))
    db = _make_db(os.path.join(workdir, "z.db"), asks=(50, 35, 20), bid_size=500.0)
    try:
        c = bid_census(db, T0, cfg)
        assert c.n_events == 1
        assert c.n_maker_profitable == 1        # margin 2.00c clears 0.50c
        assert c.n_liquid == 0                  # 500 contracts < the 1e6 floor
        assert c.n_maker_profitable_liquid == 0
    finally:
        db.close()


def test_the_full_report_renders_and_is_plain_ascii(workdir):
    """A report nobody can read on a Windows console is a report nobody reads."""
    db = _make_db(
        os.path.join(workdir, "aa.db"), asks=(50, 35, 20),
        prints=[p for leg in range(3)
                for p in _prints_on(leg, 3, offset=MINUTE, price=99, size=5000.0,
                                    side="yes")],
    )
    try:
        rep = run_validation(db, ValidationConfig(
            decision_times=(T0,), max_book_staleness_us=None,
            control_multiple=1, s2=S2Config(min_leg_depth=10.0)))
        text = rep.report()
        text.encode("ascii")            # raises if a unicode character crept in
        for heading in ("JOINT FILL PROBABILITY", "ORPHAN COST",
                        "NET P&L PER STRUCTURE", "INDEPENDENCE UNIT", "VERDICT"):
            assert heading in text
        blob = rep.as_dict()
        assert blob["verdict"] == rep.verdict()[0]
        assert set(blob["real"]) <= {m.value for m in ALL_MODELS}
    finally:
        db.close()


def test_two_runs_over_the_same_archive_produce_the_same_report(workdir):
    """A number that cannot be reproduced is not evidence.

    The control cohort is the only randomness in the harness and it is seeded,
    so the whole report is a pure function of (archive, config).
    """
    db = _make_db(
        os.path.join(workdir, "ab.db"), asks=(50, 35, 20),
        prints=[p for leg in range(3)
                for p in _prints_on(leg, 3, offset=MINUTE, price=60, size=800.0,
                                    side="yes")],
    )
    cfg = ValidationConfig(decision_times=(T0,), max_book_staleness_us=None,
                           control_multiple=2, s2=S2Config(min_leg_depth=10.0))
    try:
        assert run_validation(db, cfg).report() == run_validation(db, cfg).report()
    finally:
        db.close()


def test_the_verdict_is_undecidable_rather_than_pass_on_a_one_structure_sample(workdir):
    """A harness that cannot say 'the data does not support a conclusion' will
    always find one.

    One structure is not a sample, however profitable it looks.  The verdict
    must refuse rather than read the sign of a mean computed on n = 1 -- which
    is the shape of every "the backtest says it works" argument worth
    distrusting.
    """
    db = _make_db(
        os.path.join(workdir, "ac.db"), asks=(50, 35, 20),
        prints=[p for leg in range(3)
                for p in _prints_on(leg, 3, offset=MINUTE, price=99, size=5000.0,
                                    side="yes")],
    )
    try:
        rep = run_validation(db, ValidationConfig(
            decision_times=(T0,), max_book_staleness_us=None,
            control_multiple=1, s2=S2Config(min_leg_depth=10.0)))
        pess = rep.real[FillModel.PESSIMISTIC]
        assert pess.n_structures == 1
        v, reasons = rep.verdict()
        assert v == "UNDECIDABLE"
        assert any("30" in r for r in reasons)
    finally:
        db.close()


def test_a_cohort_that_loses_with_an_interval_excluding_zero_is_reported_refuted():
    """"The strategy loses money" is a finding, not an absence of one.

    Folding a decisively negative interval into UNDECIDABLE would hide the one
    answer this harness exists to be able to give.  Thirty structures each
    losing about ten cents, with a spread small enough that the interval clears
    zero, is a refutation and has to be named as one.
    """
    outcomes = [
        StructureOutcome(
            cohort="real", model=FillModel.PESSIMISTIC, event_ticker=f"E{i}",
            decided_at_us=T0, n_legs=3, size=1, margin_cents=1.0,
            filled=(1.0, 1.0, 0.0), matched=0.0, target=1.0, legs_touched=2,
            net_cents=-10.0 + (i % 3), resolution="flatten",
            flatten_cents=-10.0 + (i % 3), complete_cents=None,
            orphan_leg_cost_cents=(2.5, 2.5, 0.0), first_fill_at_us=T0,
        )
        for i in range(40)
    ]
    col = summarise("real", FillModel.PESSIMISTIC, outcomes)
    assert col.per_structure.excludes_zero and col.per_structure.high < 0.0
    from backtest.validate import ValidationReport
    rep = ValidationReport(
        config={"min_legs": 3, "min_margin": 0.005}, tape_lo=T0, tape_hi=T0 + HOUR,
        n_trades=10, scans=(), census=None,
        real={FillModel.PESSIMISTIC: col}, control={},
        settled=(0, 0, 0.0), n_settlements_archive=0, warnings=(),
    )
    assert rep.verdict()[0] == "EDGE REFUTED"


def test_a_resting_short_leg_queues_behind_the_displayed_ask_when_it_joins_it(workdir, cfg):
    """Quoting AT the touch means queueing behind everything displayed there.

    With a 1-cent spread the sleeve joins the ask instead of improving it, so
    `queue_ahead` is the displayed ask size -- and a fill model that ignored it
    would hand the strategy the front of a queue it never joined.
    """
    joined = _make_db(os.path.join(workdir, "ad.db"), asks=(51, 35, 20), spread=1,
                      ask_size=250.0)
    inside = _make_db(os.path.join(workdir, "ae.db"), asks=(51, 35, 20), spread=3,
                      ask_size=250.0)
    try:
        c_join = _candidate(joined, cfg)
        c_inside = _candidate(inside, cfg)
        assert c_join.prices_cents[0] == 51        # joined the ask
        assert c_inside.prices_cents[0] == 50      # stepped inside by a tick
        assert leg_order(c_join, 0).queue_ahead == pytest.approx(250.0)
        assert leg_order(c_inside, 0).queue_ahead == 0.0
    finally:
        joined.close()
        inside.close()
