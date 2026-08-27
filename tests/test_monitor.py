"""Monitor acceptance tests.  PLAN.md section 12 (KPIs) and 6.6 (alerts).

Three properties are load-bearing here and the rest of the file exists to support
them:

  1. Every KPI survives an EMPTY database.  The first run of the digest happens
     before any data exists, and a monitor that crashes on day zero is a monitor
     nobody turns on.
  2. The statistics are correct in SIGN and in WIDTH, not merely non-crashing.  A
     Brier skill that never goes negative, or a CI that never excludes zero,
     would pass a smoke test and still let a dead sleeve through the gate.
  3. No alert path can emit key material.  That one is not a statistic, it is a
     safety property, so it is tested against the rendered string, the serialized
     dict AND the webhook payload.

No test here makes a network call: the webhook sink is exercised with `httpx.post`
monkeypatched to a recorder, and with it monkeypatched to explode.
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pytest

from core.db import Database
from core.models import Market, Side, Venue
from monitor.alerts import (
    REDACTED,
    Alert,
    AlertKind,
    Alerts,
    ConsoleAlerter,
    MemoryAlerter,
    Severity,
    WebhookAlerter,
    alert_json,
    default_alerts,
    redact,
)
from monitor.kpi import (
    LAMBDA_HALT_BELOW,
    brier_skill_vs_market,
    capacity_utilization,
    e_process_status,
    fill_quality,
    lambda_hat,
    markouts,
    net_edge_with_ci,
    non_edge_income,
    orphan_loss_ratio,
    settled_decisions,
    sleeve_ids,
    sleeve_report,
)
from monitor.main import build_digest, main, render_digest, render_sleeve
from shadow.engine import ShadowExecutor, ShadowOrder

T0 = 1_700_000_000_000_000
SEC = 1_000_000


@pytest.fixture()
def db():
    with Database(":memory:") as d:
        yield d


@pytest.fixture()
def db_dir():
    """A scratch directory for the tests that need a real file on disk.

    Deliberately NOT pytest's `tmp_path`: its base directory is unreadable under
    this machine's sandbox, and a monitor test suite that cannot run is worth
    nothing.  `mkdtemp` is portable and cleans up after itself.
    """
    path = tempfile.mkdtemp(prefix="pm-monitor-")
    try:
        yield Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Fixture builders -- raw SQL so the tests control every timestamp exactly.
# --------------------------------------------------------------------------- #
def add_decision(db, *, sleeve="S1", ticker="KXA-1", market_price=0.5, p_model=0.5,
                 acted=True, at_us=T0, venue="kalshi"):
    with db.tx() as c:
        c.execute(
            """INSERT INTO decisions
               (decided_at_us, sleeve_id, venue, ticker, market_price, p_model,
                raw_edge, shrunk_edge, acted, preregistration_id)
               VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
            (at_us, sleeve, venue, ticker, market_price, p_model,
             p_model - market_price, 0.5 * (p_model - market_price), int(acted)),
        )


def add_settlement(db, ticker, outcome, *, at_us=T0 + 3600 * SEC, venue="kalshi"):
    with db.tx() as c:
        c.execute(
            """INSERT OR REPLACE INTO settlements
               (venue, ticker, settled_at_us, outcome, voided)
               VALUES (?,?,?,?,0)""",
            (venue, ticker, at_us, int(outcome)),
        )


def add_scored_batch(db, sleeve, rows, *, acted=True, prefix="KX"):
    """rows = [(market_price, p_model, outcome)] -> one settled decision each."""
    with db.tx() as c:
        for i, (mp, pm, y) in enumerate(rows):
            ticker = f"{prefix}{sleeve}-{i}"
            c.execute(
                """INSERT INTO decisions
                   (decided_at_us, sleeve_id, venue, ticker, market_price, p_model,
                    raw_edge, shrunk_edge, acted, preregistration_id)
                   VALUES (?,?,'kalshi',?,?,?,?,?,?,NULL)""",
                (T0 + i, sleeve, ticker, mp, pm, pm - mp, 0.5 * (pm - mp), int(acted)),
            )
            c.execute(
                """INSERT OR REPLACE INTO settlements
                   (venue, ticker, settled_at_us, outcome, voided)
                   VALUES ('kalshi',?,?,?,0)""",
                (ticker, T0 + i + 3600 * SEC, int(y)),
            )


def add_order(db, *, coid, sleeve="S1", ticker="KXA-1", side="yes", price=40,
              size=100, mode="live", state="open", at_us=T0, rationale=None):
    with db.tx() as c:
        c.execute(
            """INSERT OR REPLACE INTO orders
               (client_order_id, created_at_us, sleeve_id, structure_id, venue, ticker,
                side, price_cents, size, post_only, mode, venue_order_id, state,
                rationale_json, updated_at_us)
               VALUES (?,?,?,NULL,'kalshi',?,?,?,?,1,?,NULL,?,?,?)""",
            (coid, at_us, sleeve, ticker, side, price, size, mode, state,
             json.dumps(rationale or {"why": "test"}), at_us),
        )


def add_fill(db, *, coid, price=40, size=100, at_us=T0, fee_cents=0, is_maker=1,
             terminal=1, fill_id=None):
    with db.tx() as c:
        c.execute(
            """INSERT INTO fills
               (filled_at_us, client_order_id, venue_fill_id, price_cents, size,
                fee_cents, is_maker, terminal)
               VALUES (?,?,?,?,?,?,?,?)""",
            (at_us, coid, fill_id or f"vf-{coid}-{at_us}", price, size, fee_cents,
             is_maker, terminal),
        )


def add_snapshot(db, ticker, *, at_us, bid=44, ask=46, bid_size=500.0,
                 ask_size=500.0, volume=0.0):
    db.append_markets(
        [Market(venue=Venue.KALSHI, ticker=ticker, yes_bid=bid, yes_ask=ask,
                yes_bid_size=bid_size, yes_ask_size=ask_size, volume=volume)],
        observed_at_us=at_us,
    )


def add_trade(db, ticker, *, at_us, price, size, taker_side, trade_id=None):
    with db.tx() as c:
        c.execute(
            """INSERT INTO trades
               (trade_id, ticker, traded_at_us, yes_price_cents, size, taker_side, is_block)
               VALUES (?,?,?,?,?,?,0)""",
            (trade_id or f"t-{ticker}-{at_us}-{price}", ticker, at_us, price, size,
             taker_side),
        )


# --------------------------------------------------------------------------- #
# 1. The empty database.  Every KPI, no crashes, no ZeroDivisionError.
# --------------------------------------------------------------------------- #
def test_every_kpi_is_sane_on_an_empty_database(db):
    """Day zero is the first time anyone runs this.  Nothing may raise."""
    bs = brier_skill_vs_market(db, "S1")
    assert bs.n == 0 and bs.skill is None and not bs.beats_market

    ne = net_edge_with_ci(db, "S1")
    assert ne.n == 0 and ne.net_edge is None and ne.ci_low is None
    assert ne.fee_per_contract == 0.0          # not a ZeroDivisionError
    assert not ne.excludes_zero and ne.ci_width is None

    mo = markouts(db, "S1")
    assert set(mo) and all(m.n == 0 and m.mean_cents is None for m in mo.values())

    fq = fill_quality(db, "S1")
    assert fq.live_orders == 0 and fq.live_fill_rate is None and fq.ratio is None
    assert fq.maker_share is None and fq.taker_slippage_cents is None
    assert not fq.within_bracket

    lh = lambda_hat(db, "S1")
    assert lh.n == 0 and lh.beta is None and not lh.halt and not lh.credibly_positive

    ol = orphan_loss_ratio(db, "S1")
    assert ol.available and ol.structures == 0
    assert ol.ratio is None and not ol.breaches_target

    ni = non_edge_income(db)
    assert ni.total_cents == 0.0 and ni.per_unit_capital is None

    cap = capacity_utilization(db, "S1")
    assert cap.n == 0 and cap.mean_utilization is None and not cap.freeze

    ep = e_process_status(db, "S1")
    assert ep.n == 0 and ep.e_value is None and not ep.reject


def test_sleeve_report_and_digest_survive_an_empty_database(db):
    report = sleeve_report(db, "S1")
    assert report["sleeve_id"] == "S1"
    assert set(report) == {
        "sleeve_id", "brier_skill", "net_edge", "markouts", "fill_quality",
        "lambda_hat", "orphan_loss", "non_edge_income", "capacity", "e_process",
    }
    assert "sleeve S1" in render_sleeve(report)

    assert sleeve_ids(db) == []
    digest = build_digest(db)
    assert digest["sleeves"] == []
    assert "no sleeve has recorded" in render_digest(digest)


def test_non_edge_income_never_divides_by_zero_capital(db):
    add_order(db, coid="o1")
    add_fill(db, coid="o1", fee_cents=-250)      # signed: negative = rebate
    assert non_edge_income(db, capital_cents=0).per_unit_capital is None
    ni = non_edge_income(db, capital_cents=100_000, reward_cents=500.0,
                         interest_cents=1_000.0)
    assert ni.rebate_cents == 250.0
    assert ni.total_cents == 1_750.0
    assert ni.per_unit_capital == pytest.approx(0.0175)


# --------------------------------------------------------------------------- #
# 2. Brier skill vs market -- the primary metric.  PLAN.md 12 KPI 1.
# --------------------------------------------------------------------------- #
def _informative(n=100, *, good=True):
    """Alternating outcomes at a 50c price; the model either knows or is backwards."""
    rows = []
    for i in range(n):
        y = i % 2
        p = (0.9 if y else 0.1) if good else (0.1 if y else 0.9)
        rows.append((0.5, p, y))
    return rows


def test_brier_skill_is_positive_when_the_model_beats_the_price(db):
    add_scored_batch(db, "GOOD", _informative(good=True))
    bs = brier_skill_vs_market(db, "GOOD")
    assert bs.n == 100
    assert bs.brier_model == pytest.approx(0.01)
    assert bs.brier_market == pytest.approx(0.25)
    assert bs.skill == pytest.approx(0.96)
    assert bs.beats_market


def test_brier_skill_is_negative_when_the_model_loses_to_the_price(db):
    """The sufficient stop condition: worse than a freely available forecast."""
    add_scored_batch(db, "BAD", _informative(good=False))
    bs = brier_skill_vs_market(db, "BAD")
    assert bs.brier_model == pytest.approx(0.81)
    assert bs.skill == pytest.approx(1.0 - 0.81 / 0.25)
    assert bs.skill < 0
    assert not bs.beats_market


def test_unacted_decisions_are_scored_too(db):
    """PLAN.md 6.3 -- scoring only what you traded measures the filter, not the model."""
    add_scored_batch(db, "MIX", _informative(good=True), acted=False)
    assert brier_skill_vs_market(db, "MIX").n == 100
    assert brier_skill_vs_market(db, "MIX", acted_only=True).n == 0


def test_a_decision_made_after_settlement_is_not_scored(db):
    """I6: a restart against a stale universe must not manufacture skill."""
    add_settlement(db, "KXLATE", 1, at_us=T0)
    add_decision(db, sleeve="LATE", ticker="KXLATE", p_model=0.99, at_us=T0 + SEC)
    assert settled_decisions(db, "LATE") == []
    add_decision(db, sleeve="LATE", ticker="KXLATE", p_model=0.99, at_us=T0 - SEC)
    assert len(settled_decisions(db, "LATE")) == 1


def test_voided_markets_are_excluded(db):
    add_decision(db, sleeve="V", ticker="KXV", p_model=0.9)
    with db.tx() as c:
        c.execute(
            """INSERT INTO settlements (venue, ticker, settled_at_us, outcome, voided)
               VALUES ('kalshi','KXV',?,1,1)""",
            (T0 + SEC,),
        )
    assert settled_decisions(db, "V") == []


# --------------------------------------------------------------------------- #
# 3. Net edge and its interval.  PLAN.md 12 KPI 2 -- the CI decides.
# --------------------------------------------------------------------------- #
def _coinflips(n, win_rate, price=0.5, edge=0.1):
    """n acted decisions at `price`, taking the YES side, winning `win_rate` of them."""
    wins = round(n * win_rate)
    return [(price, price + edge, 1 if i < wins else 0) for i in range(n)]


def test_net_edge_point_estimate_is_win_rate_minus_price_minus_fee(db):
    add_scored_batch(db, "E", _coinflips(100, 0.60))
    add_order(db, coid="e1", sleeve="E")
    add_fill(db, coid="e1", size=100, fee_cents=100)      # 1c/contract of fee
    ne = net_edge_with_ci(db, "E")
    assert ne.n == 100 and ne.wins == 60
    assert ne.win_rate == pytest.approx(0.60)
    assert ne.price_implied == pytest.approx(0.50)
    assert ne.fee_per_contract == pytest.approx(0.01)
    assert ne.net_edge == pytest.approx(0.09)


def test_the_ci_widens_with_fewer_samples(db):
    add_scored_batch(db, "SMALL", _coinflips(40, 0.60), prefix="A")
    add_scored_batch(db, "BIG", _coinflips(400, 0.60), prefix="B")
    small = net_edge_with_ci(db, "SMALL")
    big = net_edge_with_ci(db, "BIG")
    # identical point estimate, very different evidence
    assert small.net_edge == pytest.approx(big.net_edge, abs=1e-9)
    assert small.ci_width > big.ci_width
    assert small.ci_width / big.ci_width > 2.5


def test_the_ci_excludes_zero_only_with_enough_evidence(db):
    add_scored_batch(db, "SMALL", _coinflips(40, 0.60), prefix="A")
    add_scored_batch(db, "BIG", _coinflips(400, 0.60), prefix="B")
    assert not net_edge_with_ci(db, "SMALL").excludes_zero
    assert net_edge_with_ci(db, "BIG").excludes_zero


def test_a_no_edge_sleeve_never_excludes_zero_however_many_samples(db):
    """Win rate exactly at the price: more data must tighten AROUND zero."""
    add_scored_batch(db, "FLAT", _coinflips(2_000, 0.50))
    ne = net_edge_with_ci(db, "FLAT")
    assert ne.net_edge == pytest.approx(0.0, abs=1e-9)
    assert ne.ci_low < 0 < ne.ci_high
    assert not ne.excludes_zero


def test_the_no_side_is_priced_at_one_minus_the_market(db):
    """p_model below the price means we buy NO, and NO costs 1 - price."""
    add_scored_batch(db, "NO", [(0.70, 0.40, 0)] * 1)
    ne = net_edge_with_ci(db, "NO")
    assert ne.wins == 1                                   # outcome NO, we were long NO
    assert ne.price_implied == pytest.approx(0.30)


def test_rebates_raise_net_edge(db):
    """fee_cents is signed; a rebating venue must not be charged a fee."""
    add_scored_batch(db, "R", _coinflips(100, 0.60))
    add_order(db, coid="r1", sleeve="R")
    add_fill(db, coid="r1", size=100, fee_cents=-125)
    ne = net_edge_with_ci(db, "R")
    assert ne.fee_per_contract == pytest.approx(-0.0125)
    assert ne.net_edge == pytest.approx(0.1125)


# --------------------------------------------------------------------------- #
# 4. Mark-outs.  PLAN.md 12 KPI 3.
# --------------------------------------------------------------------------- #
def test_markouts_measure_the_fair_price_move_after_a_fill(db):
    add_order(db, coid="m1", ticker="KXM", side="yes", price=40)
    add_fill(db, coid="m1", price=40, at_us=T0)
    add_snapshot(db, "KXM", at_us=T0 + 1 * SEC, bid=44, ask=46)     # fair 45
    out = markouts(db, "S1", (1 * SEC,))
    assert out[1 * SEC].n == 1
    assert out[1 * SEC].mean_cents == pytest.approx(5.0)


def test_markout_sign_flips_for_the_no_side(db):
    """A rising fair value hurts a long-NO fill.

    NOTE THE TWO CONVENTIONS.  `orders.price_cents` is YES-referenced, so this
    order sits at a YES price of 40.  `fills.price_cents` is SIDE-referenced
    (that is what `OMS.position` and `execution/fillfeed.py` store), so the SAME
    price is written as 60 -- the cost of the NO contract.  Writing 40 in both
    places, as this fixture used to, is not a NO fill at 40; it is a fill that
    does not correspond to the order above it.
    """
    add_order(db, coid="m2", ticker="KXN", side="no", price=40)
    add_fill(db, coid="m2", price=60, at_us=T0)
    add_snapshot(db, "KXN", at_us=T0 + 1 * SEC, bid=44, ask=46)
    assert markouts(db, "S1", (1 * SEC,))[1 * SEC].mean_cents == pytest.approx(-5.0)


def test_markouts_count_unobserved_horizons_instead_of_guessing(db):
    add_order(db, coid="m3", ticker="KXM", side="yes", price=40)
    add_fill(db, coid="m3", price=40, at_us=T0)
    out = markouts(db, "S1", (1 * SEC, 300 * SEC))
    assert all(m.n == 0 and m.n_unobserved == 1 for m in out.values())


def test_non_terminal_fills_are_not_marked_out(db):
    """R5b -- MATCHED can later FAIL, so a non-terminal fill is a claim."""
    add_order(db, coid="m4", ticker="KXM", side="yes", price=40)
    add_fill(db, coid="m4", price=40, at_us=T0, terminal=0)
    add_snapshot(db, "KXM", at_us=T0 + 1 * SEC)
    assert markouts(db, "S1", (1 * SEC,))[1 * SEC].n == 0


def test_max_staleness_rejects_a_distant_snapshot(db):
    """The 1s horizon must not silently resolve to an hour-old sweep.

    This is now the DEFAULT, not an opt-in.  The universe sweep records roughly
    one observation per market, so an unbounded "first snapshot at or after
    t+h" resolves every horizon from 1s to 30m to the SAME row -- and KPI 3's
    decay curve, the thing that distinguishes real maker edge from adverse
    selection, silently becomes one number repeated five times.

    An unmeasurable horizon must report itself unmeasured.
    """
    add_order(db, coid="m5", ticker="KXM", side="yes", price=40)
    add_fill(db, coid="m5", price=40, at_us=T0)
    add_snapshot(db, "KXM", at_us=T0 + 3600 * SEC)

    default = markouts(db, "S1", (1 * SEC,))[1 * SEC]
    assert default.n == 0 and default.n_unobserved == 1

    # Widening the budget past the gap accepts the same snapshot, which proves
    # the rejection is the staleness rule and not a missing row.
    wide = markouts(db, "S1", (1 * SEC,), max_staleness_us=7200 * SEC)
    assert wide[1 * SEC].n == 1


# --------------------------------------------------------------------------- #
# 5. Fill quality.  PLAN.md 12 KPI 4.
# --------------------------------------------------------------------------- #
def test_fill_quality_compares_live_against_the_recomputed_shadow_bracket(db):
    # shadow: two orders, one of which the tape trades through
    ex = ShadowExecutor(db)
    for i, ticker in enumerate(("KXS-1", "KXS-2")):
        ex.submit(ShadowOrder.create(
            sleeve_id="S1", ticker=ticker, side=Side.YES, price_cents=40, size=100,
            queue_ahead=0.0, book_bid=40, book_ask=42, rationale={"why": "t"},
            decided_at_us=T0 + i,
        ))
    add_trade(db, "KXS-1", at_us=T0 + 10 * SEC, price=39, size=500, taker_side="no")

    # live: two orders, one filled
    add_order(db, coid="L1", mode="live", ticker="KXL-1")
    add_order(db, coid="L2", mode="live", ticker="KXL-2")
    add_fill(db, coid="L1", price=40, size=100)

    fq = fill_quality(db, "S1")
    assert fq.live_orders == 2 and fq.live_filled == 1
    assert fq.live_fill_rate == pytest.approx(0.5)
    assert fq.shadow_orders == 2
    assert fq.shadow_fill_rate_pessimistic == pytest.approx(0.5)
    assert fq.ratio == pytest.approx(1.0)
    assert fq.within_bracket


def test_the_shadow_bracket_is_ordered(db):
    """R6.7d: report the bracket, never a point.  A print AT our price only
    fills us under the optimistic model."""
    ex = ShadowExecutor(db)
    ex.submit(ShadowOrder.create(
        sleeve_id="S1", ticker="KXS-3", side=Side.YES, price_cents=40, size=100,
        queue_ahead=0.0, book_bid=40, book_ask=42, rationale={"why": "t"},
        decided_at_us=T0,
    ))
    add_trade(db, "KXS-3", at_us=T0 + SEC, price=40, size=500, taker_side="no")
    fq = fill_quality(db, "S1")
    assert fq.shadow_fill_rate_pessimistic == 0.0
    assert fq.shadow_fill_rate_optimistic == 1.0


def test_queue_position_is_recovered_from_the_persisted_rationale(db):
    """Without queue_ahead the pessimistic model degenerates into the optimistic one."""
    ex = ShadowExecutor(db)
    ex.submit(ShadowOrder.create(
        sleeve_id="S1", ticker="KXS-4", side=Side.YES, price_cents=40, size=100,
        queue_ahead=10_000.0, book_bid=40, book_ask=42, rationale={"why": "t"},
        decided_at_us=T0,
    ))
    add_trade(db, "KXS-4", at_us=T0 + SEC, price=39, size=500, taker_side="no")
    assert fill_quality(db, "S1").shadow_fill_rate_pessimistic == 0.0


def test_maker_share_and_taker_slippage(db):
    add_order(db, coid="t1", side="yes", price=40, mode="live")
    add_fill(db, coid="t1", price=42, size=100, is_maker=0)      # paid 2c worse
    add_order(db, coid="t2", side="yes", price=40, mode="live")
    add_fill(db, coid="t2", price=40, size=100, is_maker=1)
    fq = fill_quality(db, "S1")
    assert fq.maker_share == pytest.approx(0.5)
    assert fq.taker_slippage_cents == pytest.approx(2.0)


def test_taker_slippage_sign_flips_for_the_no_side(db):
    """A HIGHER yes-price means a CHEAPER NO contract.

    The order rests at a YES price of 40; the fill is stored SIDE-referenced, so
    filling at a YES price of 42 is written as 58.
    """
    add_order(db, coid="t3", side="no", price=40, mode="live")
    add_fill(db, coid="t3", price=58, size=100, is_maker=0)
    assert fill_quality(db, "S1").taker_slippage_cents == pytest.approx(-2.0)


# --------------------------------------------------------------------------- #
# 6. lambda_hat.  PLAN.md 2.3b / 12 KPI 5.
# --------------------------------------------------------------------------- #
def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _logit(p):
    return np.log(p / (1.0 - p))


def synth_lambda(db, sleeve, true_beta, n, *, seed=20260826, spread=1.0):
    """Data generated FROM the encompassing model, so the target is known exactly.

        logit P(y=1) = logit(m) + beta * ( logit(q) - logit(m) )
    """
    rng = np.random.default_rng(seed)
    m = rng.uniform(0.25, 0.75, n)
    x = rng.normal(0.0, spread, n)                 # our disagreement, in log-odds
    q = _sigmoid(_logit(m) + x)
    y = (rng.random(n) < _sigmoid(_logit(m) + true_beta * x)).astype(int)
    add_scored_batch(db, sleeve, list(zip(m.tolist(), q.tolist(), y.tolist())),
                     prefix=f"L{sleeve}")
    return m, q, y


def test_lambda_hat_recovers_a_known_slope(db):
    synth_lambda(db, "HALF", 0.45, 4_000)
    lh = lambda_hat(db, "HALF")
    assert lh.n == 4_000 and lh.sufficient
    assert lh.beta == pytest.approx(0.45, abs=0.10)
    assert lh.se is not None and lh.se < 0.10
    assert lh.ci_low < 0.45 < lh.ci_high
    assert lh.credibly_positive
    assert not lh.halt


def test_lambda_hat_recovers_a_second_known_slope(db):
    """One recovered slope could be luck; the estimator has to track the target."""
    synth_lambda(db, "ONE", 1.0, 4_000, seed=7)
    lh = lambda_hat(db, "ONE")
    assert lh.beta == pytest.approx(1.0, abs=0.15)


def test_lambda_hat_near_zero_means_the_disagreement_is_noise(db):
    """beta = 0: PLAN.md 2.3b says stop trading that category."""
    synth_lambda(db, "NOISE", 0.0, 4_000, seed=99)
    lh = lambda_hat(db, "NOISE")
    assert lh.beta == pytest.approx(0.0, abs=0.10)
    assert not lh.credibly_positive
    assert lh.halt                       # R2.3a: below 0.30 with enough samples


def test_lambda_hat_halts_only_once_the_sample_floor_is_met(db):
    """R2.3a fits at >= 100 settlements.  Halting on nine would be a coin flip."""
    synth_lambda(db, "TINY", 0.0, 40, seed=5)
    lh = lambda_hat(db, "TINY")
    assert lh.n == 40 and not lh.sufficient and not lh.halt
    assert "floor" in lh.note
    assert lambda_hat(db, "TINY", min_n=10).halt is True


def test_lambda_hat_reports_a_degenerate_design_instead_of_a_number(db):
    """A sleeve that never disagrees with the price has no slope to estimate."""
    add_scored_batch(db, "SAME", [(0.5, 0.5, i % 2) for i in range(200)])
    lh = lambda_hat(db, "SAME")
    assert lh.n == 200 and lh.beta is None and not lh.halt
    assert "degenerate" in lh.note


def test_lambda_hat_survives_perfect_separation(db):
    """Small separated samples send the MLE to infinity; it must not escape."""
    rows = [(0.5, 0.9, 1) for _ in range(20)] + [(0.5, 0.1, 0) for _ in range(20)]
    add_scored_batch(db, "SEP", rows)
    lh = lambda_hat(db, "SEP", min_n=10)
    assert lh.beta is None or math.isfinite(lh.beta)


def test_lambda_hat_is_encompassing_not_correlational(db):
    """A model that merely copies the price scores 0, not 1: there is no
    disagreement for the price to be wrong about."""
    rng = np.random.default_rng(3)
    m = rng.uniform(0.2, 0.8, 500)
    y = (rng.random(500) < m).astype(int)
    add_scored_batch(db, "COPY", list(zip(m.tolist(), m.tolist(), y.tolist())))
    assert lambda_hat(db, "COPY").beta is None      # zero-variance design


# --------------------------------------------------------------------------- #
# 7-8. Orphan losses, capacity.
# --------------------------------------------------------------------------- #
def test_orphan_loss_reports_the_missing_table_instead_of_raising(db):
    """A pre-v2 database file has no `structures` table.

    The digest runs against whatever database it is pointed at, including an
    archived one.  Degrading to available=False is right; raising in the middle
    of the daily report is not.
    """
    with db.tx() as c:
        c.execute("DROP TABLE structures")
    ol = orphan_loss_ratio(db, "S2")
    assert not ol.available and ol.ratio is None


def test_orphan_loss_ratio_when_structures_exist(db):
    # `structures` ships in the DDL as of schema v2 -- do not re-create it.
    # Columns are named explicitly: the shipped table has more of them than this
    # test cares about, and positional VALUES would silently shift on the next
    # column added.
    with db.tx() as c:
        c.executemany(
            """INSERT INTO structures
               (structure_id, sleeve_id, kind, created_at_us, legs_json, n_legs,
                target_margin_cents, state, realized_margin_cents, rationale_json)
               VALUES (?,?,'dutch_book',?,'[]',2,?,?,?,'{}')""",
            [
                ("s1", "S2", T0, 1_000.0, "complete", 1_000.0),
                ("s2", "S2", T0, 1_000.0, "orphaned", -300.0),
                ("s3", "S2", T0, 1_000.0, "orphaned", 50.0),   # unwound at a profit
            ],
        )
    ol = orphan_loss_ratio(db, "S2")
    assert ol.available and ol.structures == 3 and ol.orphaned == 2
    assert ol.orphan_loss_cents == pytest.approx(300.0)
    assert ol.gross_margin_cents == pytest.approx(3_000.0)
    assert ol.ratio == pytest.approx(0.10)
    assert not ol.breaches_target


def test_capacity_uses_the_touch_depth_as_of_the_order(db):
    add_snapshot(db, "KXC", at_us=T0 - SEC, bid_size=500.0, ask_size=200.0)
    add_snapshot(db, "KXC", at_us=T0 + 10 * SEC, bid_size=1.0)   # after: must be ignored
    add_order(db, coid="c1", ticker="KXC", side="yes", size=100, at_us=T0)
    cap = capacity_utilization(db, "S1")
    assert cap.n == 1 and cap.mean_utilization == pytest.approx(0.2)
    assert not cap.freeze


def test_capacity_uses_the_ask_queue_for_a_resting_no_bid(db):
    """A resting NO bid sits on the YES ask -- a YES taker is what fills it."""
    add_snapshot(db, "KXC2", at_us=T0 - SEC, bid_size=500.0, ask_size=200.0)
    add_order(db, coid="c2", ticker="KXC2", side="no", size=100, at_us=T0)
    assert capacity_utilization(db, "S1").mean_utilization == pytest.approx(0.5)


def test_capacity_freeze_trips_at_the_configured_threshold(db):
    add_snapshot(db, "KXC3", at_us=T0 - SEC, bid_size=100.0)
    add_order(db, coid="c3", ticker="KXC3", side="yes", size=60, at_us=T0)
    cap = capacity_utilization(db, "S1")
    assert cap.freeze_at == pytest.approx(0.50)
    assert cap.mean_utilization == pytest.approx(0.6) and cap.freeze


def test_taker_volume_share(db):
    add_snapshot(db, "KXV1", at_us=T0 - SEC, volume=1_000.0)
    add_order(db, coid="v1", ticker="KXV1", mode="live")
    add_fill(db, coid="v1", size=50, is_maker=0)
    assert capacity_utilization(db, "S1").taker_volume_share == pytest.approx(0.05)


# --------------------------------------------------------------------------- #
# The e-process.  PLAN.md R2.5b -- fires only on real evidence.
# --------------------------------------------------------------------------- #
def test_the_e_process_does_not_fire_at_the_null(db):
    """1,000 observations at exactly break-even.  Naive monitoring would have
    falsely rejected by now with probability ~0.525 (research/08)."""
    add_scored_batch(db, "NULL", _coinflips(1_000, 0.50))
    ep = e_process_status(db, "NULL")
    assert ep.n == 1_000 and ep.p0 == pytest.approx(0.5)
    assert ep.e_value < ep.threshold
    assert not ep.reject


def test_the_e_process_does_not_fire_on_a_small_lucky_run(db):
    """18 of 30 is a 60% win rate -- the point estimate a P&L review would love."""
    add_scored_batch(db, "LUCKY", _coinflips(30, 0.60))
    ep = e_process_status(db, "LUCKY")
    assert ep.successes == 18
    assert not ep.reject


def test_the_e_process_is_stricter_than_the_fixed_sample_interval(db):
    """The point of R2.5b, in one assertion.

    At n=150 and a 60% win rate the Wilson interval already excludes zero -- and
    an operator who has been watching that interval every day did not run one
    test, they ran 150.  The e-process, which is valid at every stopping time,
    has not fired yet.  Measured: E = 2.05 against a threshold of 20.
    """
    add_scored_batch(db, "PEEK", _coinflips(150, 0.60))
    assert net_edge_with_ci(db, "PEEK").excludes_zero
    ep = e_process_status(db, "PEEK")
    assert 1.0 < ep.e_value < ep.threshold
    assert not ep.reject


def test_the_e_process_fires_on_real_evidence(db):
    add_scored_batch(db, "REAL", _coinflips(1_000, 0.65))
    ep = e_process_status(db, "REAL")
    assert ep.successes == 650
    assert ep.e_value > ep.threshold
    assert ep.reject
    assert ep.p_value < 0.05


def test_the_e_process_null_includes_fees(db):
    """Break-even is price PLUS fee; a fee must make the null harder to reject."""
    add_scored_batch(db, "FEE", _coinflips(400, 0.56))
    before = e_process_status(db, "FEE")
    add_order(db, coid="f1", sleeve="FEE")
    add_fill(db, coid="f1", size=100, fee_cents=400)         # 4c/contract
    after = e_process_status(db, "FEE")
    assert after.p0 == pytest.approx(0.54)
    assert after.log_e < before.log_e


def test_the_e_value_never_overflows(db):
    """Overwhelming evidence must report inf, not raise OverflowError."""
    add_scored_batch(db, "HUGE", _coinflips(20_000, 0.95))
    ep = e_process_status(db, "HUGE")
    assert ep.reject and ep.p_value == 0.0


# --------------------------------------------------------------------------- #
# Alerts.  PLAN.md 6.6 -- the safety property.
# --------------------------------------------------------------------------- #
SECRET = "hunter2-super-secret-value"
PEM = ("-----BEGIN RSA PRIVATE KEY-----\n"
       "MIIEowIBAAKCAQEAxxxxSECRETMATERIALxxxx\n"
       "-----END RSA PRIVATE KEY-----")


def test_alerts_never_emit_secrets(db):
    alert = Alert(
        AlertKind.DISCONNECT, Severity.CRITICAL,
        f"auth failed with api_key={SECRET} and body {PEM}",
        {
            "api_key": SECRET,
            "private_key_path": "C:/keys/prod.pem",
            "nested": {"session_token": SECRET, "ticker": "KXA-1"},
            "list": [{"authorization": f"Bearer {SECRET}"}],
            "size": 100,
        },
    )
    for blob in (alert.render(), alert_json(alert), json.dumps(alert.to_dict())):
        assert SECRET not in blob
        assert "PRIVATE KEY" not in blob
        assert REDACTED in blob
    # non-secret context survives -- an alert scrubbed to uselessness is not an alert
    assert alert.context["nested"]["ticker"] == "KXA-1"
    assert alert.context["size"] == 100


def test_redaction_is_by_key_name_and_by_value_shape():
    assert redact({"api_key": "abc"})["api_key"] == REDACTED
    assert redact({"note": PEM})["note"] == REDACTED
    assert redact({"note": f"Bearer {SECRET}"})["note"] == f"Bearer {REDACTED}"
    assert redact({"ticker": "KXA-1"})["ticker"] == "KXA-1"
    assert redact([{"secret": "x"}, 3, None]) == [{"secret": REDACTED}, 3, None]


def test_the_console_sink_cannot_print_a_secret(capsys):
    ConsoleAlerter().send(Alert(AlertKind.FILL, Severity.INFO, "ok",
                                {"private_key": SECRET}))
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert REDACTED in captured.out


def test_webhook_alerter_no_ops_when_the_env_var_is_unset(monkeypatch):
    """The normal state in backtest, shadow and CI.  It must not raise, and it
    must not reach the network."""
    import httpx

    monkeypatch.delenv("PM_ALERT_WEBHOOK", raising=False)

    def explode(*a, **k):
        raise AssertionError("an unconfigured alerter attempted a network call")

    monkeypatch.setattr(httpx, "post", explode)
    monkeypatch.setattr(httpx.Client, "request", explode)

    hook = WebhookAlerter()
    assert not hook.enabled and hook.url is None
    assert hook.send(Alert(AlertKind.FILL, Severity.INFO, "ignored")) is False
    assert hook.sent == 0 and hook.failures == 0
    assert "unset" in hook.describe()
    assert all(not isinstance(s, WebhookAlerter) for s in default_alerts().sinks)


def test_webhook_alerter_never_echoes_its_own_url(monkeypatch):
    """The webhook URL is itself a bearer credential."""
    monkeypatch.setenv("PM_ALERT_WEBHOOK", f"https://hooks.example/{SECRET}")
    hook = WebhookAlerter()
    assert hook.enabled
    assert SECRET not in hook.describe()


def test_webhook_payload_is_redacted(monkeypatch):
    """Stubbed transport -- no socket is opened."""
    import httpx

    posted: list[dict] = []

    class FakeResponse:
        status_code = 200

    def fake_post(url, *, json=None, timeout=None):
        posted.append({"url": url, "json": json})
        return FakeResponse()

    monkeypatch.setenv("PM_ALERT_WEBHOOK", "https://hooks.example/abc")
    monkeypatch.setattr(httpx, "post", fake_post)

    hook = WebhookAlerter()
    assert hook.send(Alert(AlertKind.STATE_DRIFT, Severity.CRITICAL,
                           "drift", {"api_key": SECRET})) is True
    assert hook.sent == 1
    assert SECRET not in json.dumps(posted[0]["json"])


def test_webhook_transport_failure_is_swallowed(monkeypatch):
    """Monitoring must never take down the process it monitors."""
    import httpx

    monkeypatch.setenv("PM_ALERT_WEBHOOK", "https://hooks.example/abc")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(
        httpx.ConnectError("down")))
    hook = WebhookAlerter()
    assert hook.send(Alert(AlertKind.FILL, Severity.INFO, "x")) is False
    assert hook.failures == 1


def test_every_alertable_event_has_a_named_method():
    """PLAN.md 6.6: fills, limit breaches, disconnects, 429s, drift, drawdown."""
    sink = MemoryAlerter()
    alerts = Alerts([sink])
    alerts.fill(sleeve_id="S1", ticker="KXA-1", side="yes", price_cents=40,
                size=10, is_maker=True)
    alerts.limit_breach(sleeve_id="S1", reason="position_cap", detail="2.00 > 1.00")
    alerts.disconnect(venue="kalshi", detail="ws closed")
    alerts.rate_limited(venue="kalshi", endpoint="/orders", retry_after_s=1.5)
    alerts.state_drift(venue="kalshi", ticker="KXA-1", local=10, remote=12)
    alerts.drawdown_rung(drawdown=0.31, action="halt_worst_sleeve_by_edge_ci")

    kinds = [a.kind for a in sink.alerts]
    assert kinds == [AlertKind.FILL, AlertKind.LIMIT_BREACH, AlertKind.DISCONNECT,
                     AlertKind.RATE_LIMIT, AlertKind.STATE_DRIFT, AlertKind.DRAWDOWN]
    # drift halts a venue and needs human acknowledgement (6.6) -- never merely INFO
    drift = sink.alerts[4]
    assert drift.severity is Severity.CRITICAL and drift.context["delta"] == 2
    assert sink.alerts[5].severity is Severity.CRITICAL      # 0.31 is past the 0.30 rung
    assert sink.alerts[3].context["status"] == 429


def test_a_shallow_drawdown_rung_is_a_warning_not_a_page():
    sink = MemoryAlerter()
    Alerts([sink]).drawdown_rung(drawdown=0.10, action="mandatory_written_review")
    assert sink.alerts[0].severity is Severity.WARN


def test_fan_out_counts_accepting_sinks():
    a, b = MemoryAlerter(), MemoryAlerter()
    assert Alerts([a, b]).fill(sleeve_id="S1", ticker="K", side="yes",
                               price_cents=1, size=1, is_maker=True) == 2


# --------------------------------------------------------------------------- #
# The digest CLI.
# --------------------------------------------------------------------------- #
def test_digest_runs_on_an_empty_database(db_dir, capsys):
    path = db_dir / "empty.db"
    assert main(["--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "DAILY DIGEST" in out
    assert "no sleeve has recorded" in out


def test_digest_renders_every_kpi_for_a_populated_database(db_dir, capsys):
    path = db_dir / "full.db"
    with Database(path) as d:
        add_scored_batch(d, "S1", _coinflips(400, 0.60))
        add_order(d, coid="d1", sleeve="S1", ticker="KXD", mode="live")
        add_fill(d, coid="d1", price=42, size=100, fee_cents=50, is_maker=0)
        add_snapshot(d, "KXD", at_us=T0 - SEC, bid_size=500.0, volume=10_000.0)
        add_snapshot(d, "KXD", at_us=T0 + 1 * SEC, bid=44, ask=46)
    assert main(["--db", str(path), "--capital", "100000"]) == 0
    out = capsys.readouterr().out
    for label in ("brier skill", "net edge", "mark-outs", "fill quality",
                  "lambda_hat", "orphan loss", "non-edge income",
                  "capacity utilization", "e-process"):
        assert label in out
    assert "sleeve S1" in out


def test_digest_emits_valid_json(db_dir, capsys):
    path = db_dir / "j.db"
    with Database(path) as d:
        add_scored_batch(d, "S1", _coinflips(120, 0.60))
    assert main(["--db", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["decisions"] == 120
    assert payload["sleeves"][0]["net_edge"]["n"] == 120


def test_digest_can_be_restricted_to_one_sleeve(db_dir, capsys):
    path = db_dir / "s.db"
    with Database(path) as d:
        add_scored_batch(d, "S1", _coinflips(10, 0.60), prefix="A")
        add_scored_batch(d, "S2", _coinflips(10, 0.60), prefix="B")
        assert sleeve_ids(d) == ["S1", "S2"]
    assert main(["--db", str(path), "--sleeve", "S2"]) == 0
    out = capsys.readouterr().out
    assert "sleeve S2" in out and "sleeve S1" not in out


def test_missing_values_render_as_dashes_not_zeros(db):
    """A KPI printed as 0.0000 on no data is how a dead sleeve stays live.

    Every headline statistic must be a dash on an empty database.  (The fee line
    is genuinely 0.00 -- no fills means no fees, which is a fact, not a gap.)
    """
    text = render_sleeve(sleeve_report(db, "S1"))
    for headline in (
        "brier skill vs market  --",
        "net edge / settlement  --",
        "CI[--, --]",
        "lambda_hat             --",
        "orphan loss ratio      --",
        "non-edge income        -- per unit capital",
        "capacity utilization   mean --  max --",
        "e-process (anytime)    E=--",
    ):
        assert headline in text, headline
    assert "+    1.0s  --" in text          # every mark-out horizon too
