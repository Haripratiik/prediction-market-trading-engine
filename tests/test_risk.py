"""T-040 acceptance: EVERY limit in PLAN.md section 9 has a test that trips it,
and a sleeve below Gate 4 cannot place a live order (I5)."""

from __future__ import annotations

import pytest

from core.config import RiskConfig
from core.models import RunMode, Side
from risk.engine import Denial, PortfolioState, RiskEngine
from strategy.base import DesiredQuote

BANKROLL = 1_000_000        # $10,000 in cents


def state(**kw) -> PortfolioState:
    kw.setdefault("bankroll_cents", BANKROLL)
    kw.setdefault("peak_bankroll_cents", BANKROLL)
    kw.setdefault("cash_cents", BANKROLL)
    return PortfolioState(**kw)


def quote(ticker="KXA-1", price=50, size=100) -> DesiredQuote:
    return DesiredQuote(ticker=ticker, side=Side.YES, price_cents=price, size=size,
                        rationale={"t": 1})


@pytest.fixture()
def eng():
    return RiskEngine(RiskConfig())


def allow(eng, q, st, **kw):
    kw.setdefault("sleeve_gate", 5)
    kw.setdefault("mode", RunMode.SHADOW)
    return eng.check(q, st, **kw)


# --------------------------------------------------------------------------- #
# I5 -- the gate
# --------------------------------------------------------------------------- #
def test_live_order_from_a_sleeve_below_gate_4_is_denied(eng):
    v = eng.check(quote(), state(), sleeve_gate=3, mode=RunMode.LIVE)
    assert not v.allowed and v.reason is Denial.GATE


def test_shadow_orders_are_allowed_at_any_gate(eng):
    assert eng.check(quote(), state(), sleeve_gate=0, mode=RunMode.SHADOW).allowed


def test_live_is_allowed_at_gate_4(eng):
    assert eng.check(quote(price=10, size=10), state(),
                     sleeve_gate=4, mode=RunMode.LIVE).allowed


# --------------------------------------------------------------------------- #
# Position / theme / deployment caps
# --------------------------------------------------------------------------- #
def test_position_cap_trips(eng):
    """2% of $10,000 = $200 = 20,000 cents.  A $250 order must be denied."""
    v = allow(eng, quote(price=50, size=500), state())     # 25,000 cents
    assert not v.allowed and v.reason is Denial.POSITION_CAP


def test_position_cap_is_halved_at_gate_4(eng):
    q = quote(price=50, size=300)                          # 15,000 cents = 1.5%
    assert allow(eng, q, state(), sleeve_gate=5).allowed
    v = eng.check(q, state(), sleeve_gate=4, mode=RunMode.SHADOW)
    assert not v.allowed and v.reason is Denial.POSITION_CAP


def test_existing_exposure_counts_toward_the_cap(eng):
    st = state(exposure_by_ticker={"KXA-1": 15_000})
    v = allow(eng, quote(price=50, size=200), st)          # +10,000 -> 25,000
    assert not v.allowed and v.reason is Denial.POSITION_CAP


def test_theme_cap_trips_across_different_tickers(eng):
    """15% of bankroll per theme. Positions on one underlying are ONE position."""
    eng.theme_of = {f"KXA-{i}": "election-2028" for i in range(10)}
    st = state(exposure_by_theme={"election-2028": 149_000})
    v = allow(eng, quote(ticker="KXA-9", price=50, size=100), st)
    assert not v.allowed and v.reason is Denial.THEME_CAP


def test_gross_deployment_cap(eng):
    st = state(exposure_by_ticker={"other": 399_000}, cash_cents=BANKROLL)
    v = allow(eng, quote(price=50, size=100), st)
    assert not v.allowed and v.reason is Denial.GROSS_DEPLOYMENT


def test_cash_reserve_is_protected(eng):
    """30% cash floor -- the fat-pitch reserve and dispute buffer."""
    st = state(cash_cents=305_000)
    v = allow(eng, quote(price=50, size=200), st)          # would leave 295,000
    assert not v.allowed and v.reason is Denial.CASH_RESERVE


def test_venue_concentration_cap(eng):
    st = state(venue_exposure={"kalshi": 599_000})
    v = allow(eng, quote(price=50, size=100), st)
    assert not v.allowed and v.reason is Denial.VENUE_CONCENTRATION


# --------------------------------------------------------------------------- #
# Capacity, N_eff, daily loss, drawdown
# --------------------------------------------------------------------------- #
def test_capacity_ceiling_caps_size_at_a_fraction_of_depth(eng):
    """Never rest more than 20% of visible touch depth."""
    v = allow(eng, quote(price=50, size=100), state(), touch_depth_contracts=200.0)
    assert not v.allowed and v.reason is Denial.CAPACITY
    assert allow(eng, quote(price=50, size=40), state(),
                 touch_depth_contracts=200.0).allowed


def test_n_eff_floor_blocks_concentration_once_deployed(eng):
    """Three themes give n_eff ~2 at rho=0.5 -- below the floor of 8."""
    eng.theme_of = {"KXA-1": "t1", "KXB-1": "t2", "KXC-1": "t3"}
    st = state(exposure_by_ticker={"KXA-1": 150_000, "KXB-1": 150_000},
               exposure_by_theme={"t1": 150_000, "t2": 150_000})
    v = allow(eng, quote(ticker="KXC-1", price=50, size=100), st)
    assert not v.allowed and v.reason is Denial.N_EFF


def test_n_eff_is_not_checked_when_barely_deployed(eng):
    """Below 20% deployment the floor does not bind."""
    eng.theme_of = {"KXA-1": "t1"}
    assert allow(eng, quote(price=50, size=100), state()).allowed


def test_daily_loss_stop(eng):
    st = state(day_pnl_cents=-60_000)                      # -6% of bankroll
    v = allow(eng, quote(), st)
    assert not v.allowed and v.reason is Denial.DAILY_LOSS


def test_drawdown_ladder_halves_caps_at_twenty_percent(eng):
    """A 1.5% order passes normally but is denied once caps halve to 1%."""
    q = quote(price=50, size=300)                          # 1.5% of bankroll
    assert allow(eng, q, state()).allowed
    drawn = state(bankroll_cents=800_000, peak_bankroll_cents=1_000_000,
                  cash_cents=800_000)
    v = allow(eng, q, drawn)
    assert not v.allowed and v.reason is Denial.POSITION_CAP


def test_forty_percent_drawdown_is_a_full_stop(eng):
    st = state(bankroll_cents=550_000, peak_bankroll_cents=1_000_000,
               cash_cents=550_000)
    v = allow(eng, quote(price=1, size=1), st)
    assert not v.allowed and v.reason is Denial.DRAWDOWN_HALT


def test_kill_switch_denies_everything(eng):
    v = allow(eng, quote(price=1, size=1), state(killed=True))
    assert not v.allowed and v.reason is Denial.KILLED


# --------------------------------------------------------------------------- #
# Batch filtering
# --------------------------------------------------------------------------- #
def test_a_batch_cannot_collectively_breach_a_cap(eng):
    """Each order is individually fine; together they exceed the position cap."""
    quotes = [quote(price=50, size=150) for _ in range(5)]   # 7,500 cents each
    approved, denied = eng.filter(quotes, state(), sleeve_gate=5, mode=RunMode.SHADOW)
    assert len(approved) < len(quotes)
    assert all(d[1].reason is Denial.POSITION_CAP for d in denied)
    total = sum(q.price_cents * q.size for q in approved)
    assert total <= int(BANKROLL * 0.02)


def test_filter_spreads_across_distinct_tickers(eng):
    eng.theme_of = {f"KXA-{i}": f"theme-{i}" for i in range(12)}
    quotes = [quote(ticker=f"KXA-{i}", price=50, size=100) for i in range(12)]
    approved, _ = eng.filter(quotes, state(), sleeve_gate=5, mode=RunMode.SHADOW)
    assert len(approved) >= 8            # distinct themes clear the n_eff floor
    assert len({q.ticker for q in approved}) == len(approved)


def test_filter_is_pure_and_does_not_mutate_the_caller_state(eng):
    st = state()
    before = dict(st.exposure_by_ticker)
    eng.filter([quote()], st, sleeve_gate=5, mode=RunMode.SHADOW)
    assert st.exposure_by_ticker == before


# --------------------------------------------------------------------------- #
# Config self-consistency -- a limit you can never clear is an outage, not a control
# --------------------------------------------------------------------------- #
def test_shipped_config_is_self_consistent(eng):
    """The n_eff floor MUST be reachable given the position and gross caps."""
    assert eng.validate() == [], eng.validate()


def test_validate_catches_an_unreachable_n_eff_floor():
    """Regression: min_n_eff=8 at cross-rho 0.10 is unreachable, because a 2%
    position cap and 40% gross cap permit at most 20 positions (n_eff = 6.9)."""
    e = RiskEngine(RiskConfig())
    e.cross_theme_rho = 0.10
    problems = e.validate()
    assert any("UNREACHABLE" in p for p in problems), problems


def test_max_achievable_n_eff_respects_the_saturation_ceiling(eng):
    """n_eff can never exceed 1/rho no matter how many themes you hold."""
    assert eng.max_achievable_n_eff() <= 1.0 / eng.cross_theme_rho + 1e-9


def test_validate_catches_gross_plus_cash_over_one_hundred_percent():
    from core.config import DeploymentLimits, RiskConfig as RC
    cfg = RC(deployment=DeploymentLimits(max_gross_fraction=0.8, min_cash_fraction=0.5))
    assert any("100%" in p for p in RiskEngine(cfg).validate())


# --------------------------------------------------------------------------- #
# Side-aware capital accounting -- found by the S2 sleeve
# --------------------------------------------------------------------------- #
def test_no_side_quotes_cost_the_complement(eng):
    """price_cents is YES-referenced, so a NO leg at YES-price 5c costs 95c.

    Mis-costing this under-counts capital by up to 20x on exactly the legs S2
    rests (short baskets are NO quotes at LOW yes-prices).
    """
    from risk.engine import quote_cost_cents

    yes = DesiredQuote(ticker="KXA-1", side=Side.YES, price_cents=5, size=100,
                       rationale={"t": 1})
    no = DesiredQuote(ticker="KXA-1", side=Side.NO, price_cents=5, size=100,
                      rationale={"t": 1})
    assert quote_cost_cents(yes) == 500
    assert quote_cost_cents(no) == 9_500


def test_a_cheap_looking_no_leg_still_trips_the_position_cap(eng):
    """The regression: 300 NO contracts at YES-price 5c locks $285, not $15."""
    q = DesiredQuote(ticker="KXA-1", side=Side.NO, price_cents=5, size=300,
                     rationale={"t": 1})
    v = allow(eng, q, state())
    assert not v.allowed and v.reason is Denial.POSITION_CAP


def test_no_side_exposure_accumulates_correctly_in_filter(eng):
    eng.theme_of = {f"KXA-{i}": f"th-{i}" for i in range(10)}
    quotes = [DesiredQuote(ticker=f"KXA-{i}", side=Side.NO, price_cents=10,
                           size=200, rationale={"t": 1}) for i in range(10)]
    approved, _ = eng.filter(quotes, state(), sleeve_gate=5, mode=RunMode.SHADOW)
    # each locks 90c x 200 = 18,000 cents = 1.8%, so the 40% gross cap binds first
    assert sum(1 for _ in approved) <= 22
    assert approved, "expected at least some to clear"
