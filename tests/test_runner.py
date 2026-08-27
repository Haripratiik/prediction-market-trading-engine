"""The loop's own invariants.  T-054.

These are the things that only break once the loop RUNS -- state that must
survive a cycle boundary, and links between tables that no single component
owns.  Every test here corresponds to a defect that was actually present.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from core.config import load_settings
from core.db import Database
from core.models import Fill, OrderRequest, RunMode, Side, Venue, now_us
from execution.oms import OMS, new_client_order_id
from runner import Runner
from strategy.base import Decision, DesiredQuote, DesiredState

BANKROLL = 1_000_000        # $10,000 in cents


# pytest's `tmp_path` cannot be used on this machine: its basetemp under
# C:\Users\...\AppData\Local\Temp\pytest-of-harie raises PermissionError
# [WinError 5].  Manage the directory ourselves.
@pytest.fixture()
def workdir():
    d = tempfile.mkdtemp(prefix="pm-runner-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def db(workdir):
    with Database(workdir / "t.db") as handle:
        yield handle


class StubSleeve:
    """Emits a fixed desired state.  The loop under test, not the strategy."""

    id = "S1"
    gate = 5

    def __init__(self, quotes, decisions=()):
        self._quotes = tuple(quotes)
        self._decisions = tuple(decisions)

    def desired_state(self, snapshot) -> DesiredState:
        return DesiredState(quotes=self._quotes, decisions=self._decisions,
                            rationale={"stub": True})


def make_runner(db, workdir, sleeves, **kw) -> Runner:
    return Runner(db=db, settings=load_settings(), sleeves=list(sleeves),
                  mode=RunMode.SHADOW, bankroll_cents=BANKROLL,
                  run_dir=workdir, **kw)


def quote(ticker="KXA-1", side=Side.YES, price=50, size=10) -> DesiredQuote:
    return DesiredQuote(ticker=ticker, side=side, price_cents=price, size=size,
                        rationale={"why": "test"})


# --------------------------------------------------------------------------- #
# Exposure must ACCUMULATE across cycles
# --------------------------------------------------------------------------- #
def test_resting_orders_count_as_deployed_capital(db, workdir):
    """The regression: the loop built a blank PortfolioState every cycle.

    Full bankroll, zero exposure, nothing deployed -- so every limit in PLAN.md
    section 9 was evaluated against an empty book no matter what was actually
    resting.  The same quotes were re-approved indefinitely and gross deployment
    could grow without bound while every check reported plenty of room.
    """
    r = make_runner(db, workdir, [StubSleeve([])])
    assert r.portfolio_state().gross_cents == 0

    oms = OMS(db, venue=Venue.KALSHI)
    req = OrderRequest(
        client_order_id=new_client_order_id(), sleeve_id="S1", venue=Venue.KALSHI,
        ticker="KXA-1", side=Side.YES, price_cents=40, size=100, post_only=True,
        mode=RunMode.SHADOW, rationale={"why": "test"},
    )
    oms.record_intent(req)

    # 100 contracts at 40c = 4,000 cents locked, visible to the NEXT cycle.
    assert r.portfolio_state().gross_cents == 4_000


def test_a_resting_no_leg_is_costed_at_the_complement(db, workdir):
    """A NO leg at YES-price 5c locks 95c, not 5c.

    This is the same rule the risk engine got wrong; the loop reconstructs
    exposure independently, so it can get it wrong independently too.
    """
    r = make_runner(db, workdir, [StubSleeve([])])
    oms = OMS(db, venue=Venue.KALSHI)
    oms.record_intent(OrderRequest(
        client_order_id=new_client_order_id(), sleeve_id="S1", venue=Venue.KALSHI,
        ticker="KXB-1", side=Side.NO, price_cents=5, size=100, post_only=True,
        mode=RunMode.SHADOW, rationale={"why": "test"},
    ))
    assert r.portfolio_state().gross_cents == 9_500


def test_exposure_is_grouped_into_themes(db, workdir):
    r = make_runner(db, workdir, [StubSleeve([])])
    r.risk.theme_of = {"KXA-1": "election", "KXA-2": "election"}
    oms = OMS(db, venue=Venue.KALSHI)
    for t in ("KXA-1", "KXA-2"):
        oms.record_intent(OrderRequest(
            client_order_id=new_client_order_id(), sleeve_id="S1",
            venue=Venue.KALSHI, ticker=t, side=Side.YES, price_cents=50,
            size=10, post_only=True, mode=RunMode.SHADOW, rationale={"w": 1},
        ))
    st = r.portfolio_state()
    assert st.exposure_by_theme == {"election": 1_000}


def test_cash_falls_as_capital_is_deployed(db, workdir):
    r = make_runner(db, workdir, [StubSleeve([])])
    before = r.portfolio_state().cash_cents
    OMS(db, venue=Venue.KALSHI).record_intent(OrderRequest(
        client_order_id=new_client_order_id(), sleeve_id="S1", venue=Venue.KALSHI,
        ticker="KXA-1", side=Side.YES, price_cents=50, size=100, post_only=True,
        mode=RunMode.SHADOW, rationale={"w": 1},
    ))
    assert r.portfolio_state().cash_cents == before - 5_000


# --------------------------------------------------------------------------- #
# Settled P&L is DERIVED, not stored
# --------------------------------------------------------------------------- #
def _settle(db, ticker, outcome, *, voided=0, at_us=None):
    with db.tx() as c:
        c.execute(
            "INSERT OR REPLACE INTO settlements "
            "(venue, ticker, settled_at_us, outcome, voided) VALUES (?,?,?,?,?)",
            (Venue.KALSHI.value, ticker, at_us or now_us(), outcome, voided),
        )


def _fill(db, ticker, side, price, size):
    oms = OMS(db, venue=Venue.KALSHI)
    coid = new_client_order_id()
    oms.record_intent(OrderRequest(
        client_order_id=coid, sleeve_id="S1", venue=Venue.KALSHI, ticker=ticker,
        side=side, price_cents=price, size=size, post_only=True,
        mode=RunMode.SHADOW, rationale={"w": 1},
    ))
    oms.record_fill(Fill(
        client_order_id=coid, venue_fill_id=f"vf-{coid[:8]}", filled_at_us=now_us(),
        price_cents=price, size=size, fee_cents=0, is_maker=True, terminal=True,
    ))
    return coid


def test_a_long_that_resolves_yes_earns_the_complement(db, workdir):
    """100 contracts bought at 40c and resolving YES pays 100c: +6,000 cents."""
    r = make_runner(db, workdir, [StubSleeve([])])
    _fill(db, "KXW-1", Side.YES, 40, 100)
    _settle(db, "KXW-1", outcome=1)
    assert r._settled_pnl_cents() == 6_000


def test_a_long_that_resolves_no_loses_what_it_paid(db, workdir):
    r = make_runner(db, workdir, [StubSleeve([])])
    _fill(db, "KXW-2", Side.YES, 40, 100)
    _settle(db, "KXW-2", outcome=0)
    assert r._settled_pnl_cents() == -4_000


def test_a_short_that_resolves_no_keeps_the_premium(db, workdir):
    """Buying NO at 60c is a short YES at 40c.  A NO resolution pays 100c on the
    NO contract, i.e. +4,000 cents on 100 -- the SAME signed expression."""
    r = make_runner(db, workdir, [StubSleeve([])])
    _fill(db, "KXW-3", Side.NO, 60, 100)
    _settle(db, "KXW-3", outcome=0)
    assert r._settled_pnl_cents() == 4_000


def test_a_voided_market_pays_nobody(db, workdir):
    r = make_runner(db, workdir, [StubSleeve([])])
    _fill(db, "KXW-4", Side.YES, 40, 100)
    _settle(db, "KXW-4", outcome=1, voided=1)
    assert r._settled_pnl_cents() == 0


def test_unsettled_positions_contribute_nothing(db, workdir):
    """P&L is realised, not marked.  An open position is not a profit."""
    r = make_runner(db, workdir, [StubSleeve([])])
    _fill(db, "KXW-5", Side.YES, 40, 100)
    assert r._settled_pnl_cents() == 0


def test_day_pnl_excludes_settlements_older_than_a_day(db, workdir):
    r = make_runner(db, workdir, [StubSleeve([])])
    _fill(db, "KXW-6", Side.YES, 40, 100)
    _settle(db, "KXW-6", outcome=1, at_us=now_us() - 2 * 86_400_000_000)
    assert r._settled_pnl_cents() == 6_000        # all time
    assert r._day_pnl_cents() == 0                # but not today


# --------------------------------------------------------------------------- #
# Decisions: recorded with category, and joined to the orders they caused
# --------------------------------------------------------------------------- #
def test_every_decision_is_recorded_with_its_category(db, workdir):
    """R2.3a fits beta_c PER CATEGORY and removes categories whose posterior beta
    is not credibly above zero.  A decisions table that drops the category makes
    that rule unimplementable, however good the estimator is."""
    decisions = [
        Decision(ticker="KXA-1", market_price=0.50, p_model=0.55, raw_edge=0.05,
                 shrunk_edge=0.02, acted=True, category="Politics"),
        Decision(ticker="KXB-1", market_price=0.30, p_model=0.28, raw_edge=-0.02,
                 shrunk_edge=-0.01, acted=False, category="Economics"),
    ]
    r = make_runner(db, workdir, [StubSleeve([], decisions)])
    r._record_decisions("S1", StubSleeve([], decisions).desired_state(None))

    rows = {row["ticker"]: row["category"]
            for row in db.conn.execute("SELECT ticker, category FROM decisions")}
    assert rows == {"KXA-1": "Politics", "KXB-1": "Economics"}


def test_unacted_decisions_are_recorded_too(db, workdir):
    """Recording only what you traded is survivorship bias: calibration measured
    on acted decisions alone cannot see the edges you correctly declined."""
    decisions = [
        Decision(ticker="KXA-1", market_price=0.5, p_model=0.9, raw_edge=0.4,
                 shrunk_edge=0.2, acted=False, category="Politics"),
    ]
    r = make_runner(db, workdir, [StubSleeve([], decisions)])
    r._record_decisions("S1", StubSleeve([], decisions).desired_state(None))
    row = db.conn.execute("SELECT acted FROM decisions").fetchone()
    assert row["acted"] == 0


def test_orders_are_joined_back_to_the_decision_that_caused_them(db, workdir):
    """Without the link, decisions match outcomes on (venue, ticker) alone --
    wrong the moment one ticker is quoted twice -- and per-decision fee and
    slippage attribution is impossible at any size."""
    decisions = [Decision(ticker="KXA-1", market_price=0.5, p_model=0.55,
                          raw_edge=0.05, shrunk_edge=0.02, acted=True,
                          category="Politics")]
    r = make_runner(db, workdir, [StubSleeve([], decisions)])
    ids = r._record_decisions("S1", StubSleeve([], decisions).desired_state(None))
    assert ids == {"KXA-1": 1}

    oms = OMS(db, venue=Venue.KALSHI)
    coid = new_client_order_id()
    oms.record_intent(OrderRequest(
        client_order_id=coid, sleeve_id="S1", venue=Venue.KALSHI, ticker="KXA-1",
        side=Side.YES, price_cents=50, size=10, post_only=True,
        mode=RunMode.SHADOW, rationale={"w": 1},
    ))
    r._link_decisions((coid,), ids)

    row = db.conn.execute(
        "SELECT decision_id FROM orders WHERE client_order_id = ?", (coid,)
    ).fetchone()
    assert row["decision_id"] == 1


def test_linking_ignores_orders_with_no_matching_decision(db, workdir):
    r = make_runner(db, workdir, [StubSleeve([])])
    r._link_decisions(("does-not-exist",), {"KXA-1": 1})     # must not raise


# --------------------------------------------------------------------------- #
# The loop refuses to start on a self-inconsistent risk config
# --------------------------------------------------------------------------- #
def test_runner_refuses_an_unreachable_risk_limit(db, workdir):
    """A limit that cannot be cleared at ANY portfolio the other limits permit is
    an outage that looks exactly like a strategy finding nothing (errata E4)."""
    settings = load_settings()
    # The limit models are frozen, which is the point -- a risk limit must not be
    # mutable at runtime.  Build a modified copy instead.
    bad_risk = settings.risk.model_copy(
        update={"theme": settings.risk.theme.model_copy(update={"min_n_eff": 999.0})}
    )
    bad = settings.model_copy(update={"risk": bad_risk})
    with pytest.raises(ValueError, match="UNREACHABLE"):
        Runner(db=db, settings=bad, sleeves=[StubSleeve([])],
               mode=RunMode.SHADOW, bankroll_cents=BANKROLL, run_dir=workdir)


# --------------------------------------------------------------------------- #
# The kill switch stops the loop
# --------------------------------------------------------------------------- #
def test_a_kill_file_stops_the_cycle_before_any_order(db, workdir):
    r = make_runner(db, workdir, [StubSleeve([quote()])])
    (Path(workdir) / "KILL").write_text("halt", encoding="utf-8")
    r.cycle()
    assert r.stats.placed == 0
    assert r._stop is True


def test_the_loop_places_through_the_executor_not_around_it(db, workdir):
    """Every order must carry an idempotency key and a persisted rationale,
    which is what going through the executor guarantees."""
    r = make_runner(db, workdir, [StubSleeve([quote(size=10)])])
    r.cycle()
    rows = db.conn.execute(
        "SELECT client_order_id, rationale_json, mode FROM orders"
    ).fetchall()
    assert len(rows) == 1
    assert len(rows[0]["client_order_id"]) >= 32          # a UUID, not a counter
    assert rows[0]["rationale_json"] not in ("", "{}")
    assert rows[0]["mode"] == RunMode.SHADOW.value


def test_a_second_identical_cycle_places_nothing_new(db, workdir):
    """The diff is the point: an unchanged quote produces NO action.

    Re-posting a resting order costs 10 rate-limit tokens and donates queue
    position -- a quoter that re-sends its book each tick pays both taxes on
    every cycle and ends up permanently behind the ones that do not.
    """
    r = make_runner(db, workdir, [StubSleeve([quote(size=10)])])
    r.cycle()
    first = r.stats.placed
    r.cycle()
    assert first == 1
    assert r.stats.placed == 1, "the second cycle re-sent an unchanged quote"
    assert r.stats.unchanged >= 1
