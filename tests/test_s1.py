"""S1 acceptance: universe filters, recalibration, join-don't-penny, and purity."""

from __future__ import annotations

import pytest

from core.models import Event, Market, Series, Side, now_us
from strategy.base import MarketSnapshot
from strategy.s1_structural import (
    THETA_BY_HORIZON,
    S1Config,
    S1Structural,
    horizon_bucket,
    recalibrate,
)

NOW = 1_700_000_000_000_000
HOUR = 3_600_000_000


def mk_market(**kw) -> Market:
    kw.setdefault("ticker", "KXA-1")
    kw.setdefault("event_ticker", "KXA")
    kw.setdefault("series_ticker", "KXA")
    kw.setdefault("yes_bid", 84)
    kw.setdefault("yes_ask", 86)
    kw.setdefault("yes_bid_size", 1000.0)
    kw.setdefault("volume_24h", 500.0)
    kw.setdefault("close_at_us", NOW + 48 * HOUR)
    kw.setdefault("rules_hash", "reviewed-hash")
    return Market(**kw)


def mk_snapshot(markets=None, **kw) -> MarketSnapshot:
    markets = markets or (mk_market(),)
    kw.setdefault("now_us", NOW)
    kw.setdefault("bankroll_cents", 1_000_000)      # $10,000
    kw.setdefault("series", {"KXA": Series(ticker="KXA", fee_type="quadratic",
                                           fee_multiplier=1.0)})
    kw.setdefault("events", {"KXA": Event(event_ticker="KXA", category="Politics",
                                          title="Will the bill pass?")})
    return MarketSnapshot(markets=tuple(markets), **kw)


def sleeve(**cfg) -> S1Structural:
    s = S1Structural(cfg=S1Config(**cfg))
    s.reviewed_rules.add("reviewed-hash")
    return s


# --------------------------------------------------------------------------- #
# Recalibration
# --------------------------------------------------------------------------- #
def test_theta_one_is_the_identity():
    for p in (0.1, 0.5, 0.85):
        assert recalibrate(p, 1.0) == pytest.approx(p)


def test_theta_above_one_pushes_favourites_up():
    """theta > 1 means the market is UNDERCONFIDENT -- favourites are cheap."""
    assert recalibrate(0.70, 1.32) > 0.70
    assert recalibrate(0.30, 1.32) < 0.30       # and longshots are rich
    assert recalibrate(0.50, 1.32) == pytest.approx(0.50)   # the fixed point


def test_horizon_buckets_and_the_bias_collapsing_at_expiry():
    assert horizon_bucket(0.5) == "under_1h"
    assert horizon_bucket(12) == "under_1d"
    assert horizon_bucket(24 * 3) == "under_1w"
    assert horizon_bucket(24 * 45) == "over_1m"
    # the documented shape: the bias grows with time to expiry
    assert THETA_BY_HORIZON["under_1h"] < THETA_BY_HORIZON["over_1m"]
    assert THETA_BY_HORIZON["under_1h"] == pytest.approx(0.99, abs=0.02)


# --------------------------------------------------------------------------- #
# Universe filters
# --------------------------------------------------------------------------- #
def test_accepts_a_liquid_favourite():
    s = sleeve()
    ok, why = s.in_universe(mk_market(), mk_snapshot())
    assert ok, why


@pytest.mark.parametrize("bid,ask,reason", [
    (40, 42, "favourite band"),      # too cheap
    (96, 98, "favourite band"),      # too rich
])
def test_rejects_outside_the_favourite_band(bid, ask, reason):
    ok, why = sleeve().in_universe(mk_market(yes_bid=bid, yes_ask=ask), mk_snapshot())
    assert not ok and reason in why


def test_rejects_inside_the_final_hour():
    """The bias collapses at expiry -- calibration slope goes to 0.99, and the
    Yogi Berra effect makes closing-day maker losses match taker losses."""
    m = mk_market(close_at_us=NOW + HOUR // 2)
    ok, why = sleeve().in_universe(m, mk_snapshot())
    assert not ok and "final hour" in why


def test_rejects_beyond_ninety_days():
    m = mk_market(close_at_us=NOW + 100 * 24 * HOUR)
    ok, why = sleeve().in_universe(m, mk_snapshot())
    assert not ok and "90d" in why


def test_rejects_thin_depth():
    ok, why = sleeve().in_universe(mk_market(yes_bid_size=10.0), mk_snapshot())
    assert not ok and "depth" in why


def test_rejects_untraded_markets():
    ok, why = sleeve().in_universe(mk_market(volume_24h=0.0), mk_snapshot())
    assert not ok and "volume" in why


def test_rejects_unreviewed_rules():
    """I7 -- no market enters any universe until its rules are reviewed."""
    s = S1Structural()                       # nothing reviewed
    ok, why = s.in_universe(mk_market(), mk_snapshot())
    assert not ok and "rules not reviewed" in why


def test_rejects_a_one_sided_book():
    ok, why = sleeve().in_universe(mk_market(yes_bid=0), mk_snapshot())
    assert not ok


# --------------------------------------------------------------------------- #
# Execution: join, don't penny
# --------------------------------------------------------------------------- #
def test_joins_the_bid_on_a_small_edge():
    """Unless the edge is >= 3c, improving must double or triple fill probability
    to break even -- and the whole front-to-back queue is worth 0.21-0.26 ticks."""
    s = sleeve()
    m = mk_market(yes_bid=84, yes_ask=88)
    assert s.quote_price(m, edge_cents=1.5) == 84


def test_improves_only_on_a_large_edge():
    s = sleeve()
    m = mk_market(yes_bid=84, yes_ask=88)
    assert s.quote_price(m, edge_cents=4.0) == 85


def test_never_improves_into_a_one_tick_spread():
    s = sleeve()
    m = mk_market(yes_bid=84, yes_ask=85)
    assert s.quote_price(m, edge_cents=10.0) == 84


# --------------------------------------------------------------------------- #
# Quoting
# --------------------------------------------------------------------------- #
def test_emits_a_post_only_quote_when_the_edge_clears():
    s = sleeve()
    # push theta high so the recalibrated edge is unambiguous
    s.theta_by_horizon["under_1w"] = 2.5
    st = s.desired_state(mk_snapshot())
    assert st.quotes
    q = st.quotes[0]
    assert q.post_only and q.side is Side.YES
    assert q.rationale["p_model"] > q.rationale["mid"]


def test_no_quote_when_the_market_is_already_fair():
    """theta = 1 means no disagreement, so no edge and no order."""
    s = sleeve()
    s.theta_by_horizon = {k: 1.0 for k in s.theta_by_horizon}
    assert s.desired_state(mk_snapshot()).quotes == ()


def test_size_is_capped_by_touch_depth():
    """Capacity: never rest more than 20% of visible depth."""
    s = sleeve()
    s.theta_by_horizon["under_1w"] = 3.0
    m = mk_market(yes_bid_size=50.0)          # depth cap = 10 contracts
    st = s.desired_state(mk_snapshot(markets=[m]))
    if st.quotes:
        assert st.quotes[0].size <= 10


def test_decisions_are_recorded_even_when_not_acted_on():
    """Un-acted decisions are what make calibration measurable without
    survivorship bias (PLAN.md 6.3)."""
    s = sleeve()
    s.theta_by_horizon = {k: 1.0 for k in s.theta_by_horizon}
    st = s.desired_state(mk_snapshot())
    assert st.decisions
    assert all(not d.acted for d in st.decisions)
    assert st.quotes == ()


def test_quotes_are_ranked_by_shrunk_edge():
    s = sleeve()
    s.theta_by_horizon["under_1w"] = 2.0
    markets = [mk_market(ticker=f"KXA-{i}", yes_bid=80 + i, yes_ask=82 + i)
               for i in range(5)]
    st = s.desired_state(mk_snapshot(markets=markets))
    edges = [q.rationale["shrunk_edge"] for q in st.quotes]
    assert edges == sorted(edges, reverse=True)


def test_shrinkage_is_applied_before_sizing():
    """I2 -- sizing NEVER uses a raw model edge."""
    s = sleeve()
    s.theta_by_horizon["under_1w"] = 2.0
    st = s.desired_state(mk_snapshot())
    if st.quotes:
        r = st.quotes[0].rationale
        assert r["shrunk_edge"] == pytest.approx(0.5 * r["raw_edge"], rel=1e-6)


# --------------------------------------------------------------------------- #
# Purity (C4.2a)
# --------------------------------------------------------------------------- #
def test_desired_state_is_deterministic():
    """The property that lets backtest, shadow and live share one code path."""
    s = sleeve()
    s.theta_by_horizon["under_1w"] = 2.0
    snap = mk_snapshot()
    a, b = s.desired_state(snap), s.desired_state(snap)
    assert [q.key() for q in a.quotes] == [q.key() for q in b.quotes]
    assert [q.size for q in a.quotes] == [q.size for q in b.quotes]


def test_sleeve_makes_no_network_call(monkeypatch):
    import httpx

    def explode(*a, **k):
        raise AssertionError("a sleeve must never touch the network (C4.2b)")

    monkeypatch.setattr(httpx.Client, "request", explode)
    s = sleeve()
    s.theta_by_horizon["under_1w"] = 2.0
    s.desired_state(mk_snapshot())


def test_sleeve_does_not_read_the_clock(monkeypatch):
    """Time comes from the snapshot -- otherwise a backtest silently uses NOW."""
    import time as _time

    monkeypatch.setattr(_time, "time_ns",
                        lambda: (_ for _ in ()).throw(AssertionError("clock read")))
    s = sleeve()
    s.theta_by_horizon["under_1w"] = 2.0
    s.desired_state(mk_snapshot())
