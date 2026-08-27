"""S2 acceptance.

The tests that matter here are not "does the arithmetic work" -- they are "does
the sleeve refuse the trade that looks best and is worst".  Of the 47
fee-profitable taker structures found on the live venue, 33 were the F1
exhaustiveness trap (research/05 4.2).  A scanner that ranks by apparent margin
puts those at the TOP.  So most of this file is about what S2 declines to do.
"""

from __future__ import annotations

import math

import pytest

from core.math.contracts import FeeSpec
from core.math.portfolio import dutch_book_fee_hurdle, short_basket_margin
from core.models import Event, Market, Series, Side
from rulebook.exhaustiveness import MeceCheck, Verdict, check_mece
from strategy.base import MarketSnapshot
from strategy.s2_shortbasket import (
    MIN_ARBITRAGE_LEGS,
    UNKNOWN_SERIES_FEE_SPEC,
    Direction,
    S2Config,
    S2ShortBasket,
    annualized_rolc,
    devigged_probs,
    locked_capital,
    max_sum_px_for_long,
    min_sum_px_for_short,
    rest_price_short_cents,
)

NOW = 1_700_000_000_000_000
HOUR = 3_600_000_000
DAY = 24 * HOUR

# 13,353 of 13,486 series: MAKERS PAY ZERO (research/06 section 4).
QUADRATIC = FeeSpec.kalshi("quadratic", 1.0)
# The 130 that do charge makers, at 0.25x base.  This is the spec the maker
# column of the research/05 4.5 fee table is computed against.
MAKER_FEE = FeeSpec.kalshi("quadratic_with_maker_fees", 1.0)


# --------------------------------------------------------------------------- #
# Fixtures -- built here, never loaded from data/pm.db
# --------------------------------------------------------------------------- #
def mk_leg(i: int, bid: int, ask: int, *, event: str = "KXA", **kw) -> Market:
    kw.setdefault("yes_bid_size", 500.0)
    kw.setdefault("yes_ask_size", 500.0)
    kw.setdefault("volume_24h", 100.0)
    kw.setdefault("close_at_us", NOW + 30 * DAY)
    return Market(
        ticker=f"{event}-{i}",
        event_ticker=event,
        series_ticker="KXA",
        title=f"Outcome {i}",
        yes_bid=bid,
        yes_ask=ask,
        **kw,
    )


def mk_event(ticker: str = "KXA", **kw) -> Event:
    kw.setdefault("mutually_exclusive", True)
    kw.setdefault("collateral_return_type", "MECNET")
    kw.setdefault("title", "Who will win?")
    return Event(event_ticker=ticker, **kw)


def mk_snapshot(markets: list[Market], *, series: Series | None = None,
                events: list[Event] | None = None, **kw) -> MarketSnapshot:
    kw.setdefault("bankroll_cents", 1_000_000)          # $10,000
    evs = events or [mk_event(t) for t in sorted({m.event_ticker for m in markets})]
    return MarketSnapshot(
        now_us=NOW,
        markets=tuple(markets),
        series={"KXA": series or Series(ticker="KXA")},
        events={e.event_ticker: e for e in evs},
        **kw,
    )


def sleeve(**cfg) -> S2ShortBasket:
    return S2ShortBasket(cfg=S2Config(**cfg))


def uniform(n: int, bid: int, ask: int, *, event: str = "KXA", **kw) -> list[Market]:
    return [mk_leg(i, bid, ask, event=event, **kw) for i in range(n)]


def verified_checker(event: Event, markets: list[Market]) -> MeceCheck:
    """A human verdict that exhaustiveness holds.  Nothing in the code base can
    produce this today -- see `test_the_gate_never_self_verifies`."""
    real = check_mece(event, markets)
    return MeceCheck(Verdict.VERIFIED, ("human-verified (test fixture)",),
                     real.sum_bid, real.sum_ask, real.n_legs,
                     real.has_catch_all, real.all_legs_restable,
                     # A human verdict certifies EXHAUSTIVENESS; mutual
                     # exclusivity is still the exchange flag, carried through
                     # from the real check rather than assumed.
                     real.mutually_exclusive)


# --------------------------------------------------------------------------- #
# 1.  The exhaustiveness asymmetry -- the whole point of the sleeve
# --------------------------------------------------------------------------- #
def test_the_pope_book_is_never_bought_but_may_be_sold():
    """`KXNEWPOPE-70`: 7 legs, sum(ask) = 0.282, no Other leg (research/05 4.2).

    A naive scanner calls this a +72c arbitrage.  It returns $0 whenever the new
    Pope is not one of the seven listed cardinals.
    """
    legs = uniform(7, 3, 4)                       # sum(ask) = 0.28, sum(bid) = 0.21
    ev = mk_event(title="Who will the next Pope be?")
    check = check_mece(ev, legs)

    assert check.verdict is Verdict.REJECTED
    assert not check.safe_to_buy                  # buying is the trap
    assert check.safe_to_sell                     # selling is capped at $1 regardless

    # even with the long basket explicitly enabled, it is never bought
    s = sleeve(allow_long_basket=True)
    st = s.evaluate(ev, legs, mk_snapshot(legs, events=[ev]))
    assert st is None or st.direction is not Direction.LONG


def test_a_non_exhaustive_but_overround_book_is_sold():
    """Non-exhaustiveness makes the SHORT better, not worse.

    sum(bid) = 0.56 fails the exhaustiveness gate, so this can never be bought.
    But an unlisted winner leaves every leg worthless and the whole premium is
    kept, so the short is strictly safer than on an exhaustive book.
    """
    legs = uniform(7, 8, 17)                      # sum(bid) 0.56, sum(ask) 1.19
    ev = mk_event()
    check = check_mece(ev, legs)
    assert not check.safe_to_buy and check.safe_to_sell

    st = sleeve().evaluate(ev, legs, mk_snapshot(legs, events=[ev]))
    assert st is not None
    assert st.direction is Direction.SHORT
    assert st.locked and st.is_arbitrage
    assert st.size > 0
    assert st.margin == pytest.approx(0.12, abs=1e-9)     # 7 x 16c - $1, no maker fee


def test_the_gate_never_self_verifies():
    """S2's long path is dead by construction, and that is the design.

    `check_mece` tops out at NEEDS_HUMAN because condition 4 (identical void
    clauses) requires reading the rules text.  If this ever starts returning
    VERIFIED on its own, the long basket silently goes live.
    """
    legs = uniform(5, 19, 21)                     # a tight, plausible book
    assert check_mece(mk_event(), legs).verdict is not Verdict.VERIFIED


def test_long_basket_needs_a_verified_verdict():
    legs = uniform(5, 17, 19)                     # rest bids at 18c -> sum 0.90
    ev = mk_event()
    snap = mk_snapshot(legs, events=[ev])

    blocked = sleeve(allow_long_basket=True)
    assert blocked.evaluate(ev, legs, snap) is None

    allowed = S2ShortBasket(cfg=S2Config(allow_long_basket=True),
                            mece_check=verified_checker)
    st = allowed.evaluate(ev, legs, snap)
    assert st is not None and st.direction is Direction.LONG
    assert st.sum_px == pytest.approx(0.90)
    assert st.margin == pytest.approx(0.10)       # maker fees are zero here


def test_long_basket_stays_off_by_default_even_when_verified():
    """`allow_long_basket` is a second, independent switch: a VERIFIED verdict
    alone does not turn the unsafe direction on."""
    legs = uniform(5, 17, 19)
    ev = mk_event()
    s = S2ShortBasket(mece_check=verified_checker)          # default config
    assert s.evaluate(ev, legs, mk_snapshot(legs, events=[ev])) is None


# --------------------------------------------------------------------------- #
# 2.  Restability -- a leg nobody bids cannot be rested into
# --------------------------------------------------------------------------- #
def test_a_leg_with_no_bid_blocks_the_structure():
    legs = [mk_leg(0, 40, 42), mk_leg(1, 40, 42), mk_leg(2, 0, 35)]
    ev = mk_event()
    check = check_mece(ev, legs)
    assert not check.safe_to_sell
    assert any("no bid at all" in r for r in check.reasons)
    assert sleeve().evaluate(ev, legs, mk_snapshot(legs, events=[ev])) is None


def test_a_bidless_leg_blocks_even_a_spectacular_looking_book():
    """Resting at 1c on a market nobody bids is the liquidity fantasy that made
    a naive scan report 78% of MECE events as profitable (research/05 4.3)."""
    legs = [mk_leg(0, 60, 62), mk_leg(1, 55, 57), mk_leg(2, 50, 52),
            mk_leg(3, None, 99)]                  # sum(ask) = 2.70, and worthless
    ev = mk_event()
    assert not check_mece(ev, legs).all_legs_restable
    assert sleeve().evaluate(ev, legs, mk_snapshot(legs, events=[ev])) is None


def test_no_quotes_are_emitted_for_a_bidless_book():
    legs = [mk_leg(0, 40, 42), mk_leg(1, 40, 42), mk_leg(2, 0, 35)]
    st = sleeve().desired_state(mk_snapshot(legs))
    assert st.quotes == ()


# --------------------------------------------------------------------------- #
# 3.  n == 2 is market making, not arbitrage
# --------------------------------------------------------------------------- #
def test_two_legs_are_tagged_market_making_not_arbitrage():
    """357 of the 504 liquidity-filtered maker structures live are n == 2
    (research/05 4.4).  Their margin needs BOTH legs to fill."""
    legs = [mk_leg(0, 40, 44), mk_leg(1, 55, 59)]
    st = sleeve().evaluate(mk_event(), legs, mk_snapshot(legs))
    assert st is not None
    assert st.direction is Direction.MARKET_MAKING
    assert st.is_market_making
    assert not st.is_arbitrage
    assert not st.locked
    assert st.size == 0
    assert any("both-fill risk" in r for r in st.reasons)


def test_two_legs_are_reported_for_s6_but_never_quoted_by_s2():
    legs = [mk_leg(0, 40, 44), mk_leg(1, 55, 59)]
    st = sleeve().desired_state(mk_snapshot(legs))
    assert st.quotes == ()
    assert st.rationale["market_making_candidates"] == ("KXA",)
    assert st.rationale["locked_arbitrage"] == 0


def test_genuine_arbitrage_begins_at_three_legs():
    assert MIN_ARBITRAGE_LEGS == 3
    three = uniform(3, 40, 42)                    # rest 41c x3 -> sum 1.23
    st = sleeve().evaluate(mk_event(), three, mk_snapshot(three))
    assert st is not None and st.is_arbitrage and st.n_legs == 3


# --------------------------------------------------------------------------- #
# 4.  The fee hurdle table -- research/05 4.5, reproduced exactly
# --------------------------------------------------------------------------- #
def test_five_outcome_hurdles_match_the_measured_table():
    """A 5-outcome Kalshi Dutch book: sum(px) < 0.9453 taker, < 0.9863 maker."""
    assert max_sum_px_for_long(5, QUADRATIC, is_maker=False) == pytest.approx(0.9453, abs=5e-5)
    assert max_sum_px_for_long(5, MAKER_FEE, is_maker=True) == pytest.approx(0.9863, abs=5e-5)


@pytest.mark.parametrize("n,taker,maker", [
    (2, 0.9650, 0.9913),
    (3, 0.9541, 0.9885),
    (5, 0.9453, 0.9863),
    (8, 0.9403, 0.9851),
    (12, 0.9376, 0.9844),
])
def test_the_whole_fee_hurdle_table(n, taker, maker):
    assert max_sum_px_for_long(n, QUADRATIC, is_maker=False) == pytest.approx(taker, abs=5e-5)
    assert max_sum_px_for_long(n, MAKER_FEE, is_maker=True) == pytest.approx(maker, abs=5e-5)


def test_the_maker_window_is_wider_which_is_correction_c2():
    """~4 points wider at n = 5 -- the difference between 'almost never' and
    'regularly' given sum(ask) clusters just above 1.00."""
    widening = (max_sum_px_for_long(5, MAKER_FEE, is_maker=True)
                - max_sum_px_for_long(5, QUADRATIC, is_maker=False))
    assert widening == pytest.approx(0.041, abs=0.002)


def test_makers_pay_zero_on_the_ordinary_series():
    """13,353 of 13,486 series are plain `quadratic`: the maker hurdle is
    exactly zero there, wider still than the table's maker column."""
    assert dutch_book_fee_hurdle(5, QUADRATIC, is_maker=True) == 0.0
    assert max_sum_px_for_long(5, QUADRATIC, is_maker=True) == 1.0
    assert min_sum_px_for_short(5, QUADRATIC, is_maker=True) == 1.0


def test_the_short_hurdle_mirrors_the_long_hurdle():
    for n in (2, 3, 5, 8, 12):
        long_gap = 1.0 - max_sum_px_for_long(n, MAKER_FEE, is_maker=True, book_total=1.0)
        short_gap = min_sum_px_for_short(n, MAKER_FEE, is_maker=True) - 1.0
        assert long_gap == pytest.approx(short_gap)


# --------------------------------------------------------------------------- #
# 5.  short_basket_margin is positive exactly when sum(bid) > 1 + fees
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("n", [2, 3, 5, 8, 12])
@pytest.mark.parametrize("spec,is_maker", [(QUADRATIC, False), (MAKER_FEE, True),
                                           (QUADRATIC, True)])
def test_short_margin_positive_exactly_above_one_plus_fees(n, spec, is_maker):
    for k in range(80, 141):
        total = k / 100.0                          # sum(bid) from 0.80 to 1.40
        bids = [total / n] * n
        margin = short_basket_margin(bids, spec, is_maker=is_maker)
        # fees evaluated at the SAME uniform price, so the hurdle is exact
        hurdle = 1.0 + dutch_book_fee_hurdle(n, spec, is_maker=is_maker,
                                             book_total=total)
        assert (margin > 0.0) == (total > hurdle), (n, total, margin, hurdle)
        assert margin == pytest.approx(total - hurdle)


def test_selling_a_book_at_par_loses_exactly_the_fees():
    bids = [0.2] * 5
    assert short_basket_margin(bids, QUADRATIC, is_maker=False) == pytest.approx(
        -dutch_book_fee_hurdle(5, QUADRATIC, is_maker=False, book_total=1.0))


def test_the_sleeve_respects_the_short_hurdle_on_a_maker_fee_series():
    """Straddle `min_sum_px_for_short` on a series that actually charges makers."""
    series = Series(ticker="KXA", fee_type="quadratic_with_maker_fees",
                    fee_multiplier=1.0)
    hurdle = min_sum_px_for_short(5, MAKER_FEE, is_maker=True)
    assert 1.0 < hurdle < 1.02

    s = sleeve(min_margin=1e-9)
    soon = {"close_at_us": NOW + 7 * DAY}          # keep ROLC out of the way
    below = uniform(5, 19, 21, **soon)             # rest 20c x5 -> sum 1.00 < hurdle
    above = uniform(5, 20, 22, **soon)             # rest 21c x5 -> sum 1.05 > hurdle

    st_below = s.evaluate(mk_event(), below, mk_snapshot(below, series=series))
    assert st_below is None or not st_below.locked

    st_above = s.evaluate(mk_event(), above, mk_snapshot(above, series=series))
    assert st_above is not None and st_above.locked and st_above.size > 0
    # exactly the overround minus the five maker fees this series does charge
    assert st_above.margin == pytest.approx(
        1.05 - min_sum_px_for_short(5, MAKER_FEE, is_maker=True, book_total=1.05))


# --------------------------------------------------------------------------- #
# 6.  Return on locked capital
# --------------------------------------------------------------------------- #
def test_rolc_rejects_capital_tied_up_too_long_for_too_little():
    """3 legs resting at 35/35/34 -> +4c on $1.96 of collateral.  Over a year
    that is 2%, and the capital belongs in S1 instead (PLAN.md 3.2)."""
    legs = [mk_leg(0, 34, 36, close_at_us=NOW + 365 * DAY),
            mk_leg(1, 34, 36, close_at_us=NOW + 365 * DAY),
            mk_leg(2, 33, 35, close_at_us=NOW + 365 * DAY)]
    st = sleeve().evaluate(mk_event(), legs, mk_snapshot(legs))
    assert st is not None
    assert st.margin == pytest.approx(0.04)
    assert st.rolc == pytest.approx(0.0204, abs=1e-3)
    assert st.size == 0                            # rejected: nothing to quote
    assert any("ROLC" in r for r in st.reasons)
    assert sleeve().desired_state(mk_snapshot(legs)).quotes == ()


def test_the_same_structure_is_accepted_when_it_settles_sooner():
    legs = [mk_leg(0, 34, 36, close_at_us=NOW + 30 * DAY),
            mk_leg(1, 34, 36, close_at_us=NOW + 30 * DAY),
            mk_leg(2, 33, 35, close_at_us=NOW + 30 * DAY)]
    st = sleeve().evaluate(mk_event(), legs, mk_snapshot(legs))
    assert st is not None and st.size > 0
    assert st.rolc > 0.15


def test_rolc_uses_total_locked_collateral_not_the_leg_price():
    margin, days = 0.04, 365.0
    assert annualized_rolc(margin, 1.96, days) == pytest.approx(0.0204, abs=1e-3)
    assert annualized_rolc(margin, 1.96, 30.0) == pytest.approx(0.248, abs=1e-3)
    # a structure that immobilises nothing has an unbounded return
    assert annualized_rolc(margin, 0.0, days) == math.inf
    with pytest.raises(ValueError):
        annualized_rolc(margin, 1.0, 0.0)


def test_locked_capital_is_unnetted_by_default():
    """research/05 F4 flags MECNET netting as unverified against the margin
    endpoints, so assuming it would overstate capacity ~n-fold."""
    px = [0.41, 0.41, 0.34]
    assert locked_capital(px, direction=Direction.SHORT, netted=False) == pytest.approx(1.84)
    assert locked_capital(px, direction=Direction.SHORT, netted=True) == 0.0
    assert locked_capital(px, direction=Direction.LONG, netted=False) == pytest.approx(1.16)
    assert sleeve().cfg.assume_mecnet_netting is False


# --------------------------------------------------------------------------- #
# 7.  Sizing: depth and lockup, never Kelly, on a locked structure
# --------------------------------------------------------------------------- #
def test_size_is_capped_by_touch_depth():
    legs = uniform(3, 40, 42, yes_bid_size=50.0)   # 20% of 50 = 10
    st = sleeve().evaluate(mk_event(), legs, mk_snapshot(legs))
    assert st is not None and st.size == 10


def test_size_is_capped_at_five_percent_of_bankroll():
    legs = uniform(3, 40, 42, yes_bid_size=100_000.0)
    snap = mk_snapshot(legs, bankroll_cents=1_000_000)
    st = sleeve().evaluate(mk_event(), legs, snap)
    assert st is not None
    budget = 0.05 * snap.bankroll_cents
    assert st.size * st.capital_per_basket * 100.0 <= budget
    assert st.size == int(budget // round(st.capital_per_basket * 100.0))


def test_the_sleeve_total_is_capped_at_fifteen_percent():
    """PLAN.md section 9 `structures.max_sleeve_total_fraction`."""
    legs: list[Market] = []
    events = []
    for e in range(4):
        tick = f"KX{e}"
        legs += [mk_leg(i, 40, 42, event=tick, yes_bid_size=100_000.0) for i in range(3)]
        events.append(mk_event(tick))
    snap = MarketSnapshot(
        now_us=NOW, markets=tuple(legs), bankroll_cents=1_000_000,
        series={"KXA": Series(ticker="KXA")},
        events={e.event_ticker: e for e in events},
    )
    st = sleeve().desired_state(snap)
    assert st.rationale["structures_found"] == 4
    assert st.rationale["structures_quoted"] == 3          # the 4th does not fit
    assert st.rationale["committed_cents"] <= st.rationale["sleeve_budget_cents"]


def test_max_structures_bounds_the_order_count():
    legs: list[Market] = []
    events = []
    for e in range(5):
        tick = f"KX{e}"
        legs += [mk_leg(i, 40, 42, event=tick, yes_bid_size=60.0) for i in range(3)]
        events.append(mk_event(tick))
    snap = MarketSnapshot(
        now_us=NOW, markets=tuple(legs), bankroll_cents=1_000_000,
        series={"KXA": Series(ticker="KXA")},
        events={e.event_ticker: e for e in events},
    )
    st = S2ShortBasket(cfg=S2Config(max_structures=2)).desired_state(snap)
    assert st.rationale["structures_quoted"] == 2
    assert len(st.quotes) == 6


# --------------------------------------------------------------------------- #
# 8.  Emission: one structure_id per basket, YES-referenced prices
# --------------------------------------------------------------------------- #
def test_every_leg_carries_the_same_structure_id():
    legs = uniform(4, 30, 34)
    st = sleeve().desired_state(mk_snapshot(legs))
    ids = {q.rationale["structure_id"] for q in st.quotes}
    assert len(st.quotes) == 4
    assert len(ids) == 1
    sid = ids.pop()
    assert sid.startswith("S2-short-KXA-")
    assert {q.rationale["leg_index"] for q in st.quotes} == {0, 1, 2, 3}
    assert all(tuple(q.rationale["legs"]) == tuple(m.ticker for m in legs)
               for q in st.quotes)


def test_short_legs_are_no_side_at_a_yes_referenced_price():
    """PLAN.md 0.3: prices are YES-referenced throughout.  Resting a YES ask at
    41c IS bidding 59c for NO -- one order, two descriptions."""
    legs = uniform(3, 40, 42)
    st = sleeve().desired_state(mk_snapshot(legs))
    for q in st.quotes:
        assert q.side is Side.NO
        assert q.post_only                          # entry is always maker (I1/C2)
        assert q.price_cents == 41 == rest_price_short_cents(legs[0])
        assert q.rationale["price_convention"] == "yes_referenced"
        assert q.rationale["no_price_cents"] == 59


def test_we_join_the_ask_when_the_spread_is_one_tick():
    """Stepping inside a 1c spread would cross the bid and be post-only rejected."""
    assert rest_price_short_cents(mk_leg(0, 40, 41)) == 41
    assert rest_price_short_cents(mk_leg(0, 40, 42)) == 41
    assert rest_price_short_cents(mk_leg(0, 40, 60)) == 59


def test_the_rationale_documents_bounded_partial_fill_risk():
    """Selling k of N legs leaves max liability $1 and keeps k premiums; worst
    case is $1 - premium (research/06 2.4)."""
    legs = uniform(3, 40, 42)
    q = sleeve().desired_state(mk_snapshot(legs)).quotes[0]
    assert q.rationale["max_liability_per_basket"] == 1.0
    assert q.rationale["worst_case_partial"] == pytest.approx(0.59)   # $1 - 41c
    assert q.rationale["leg_timeout_seconds"] == 900
    assert q.rationale["completion_taker_threshold"] == 0.6
    assert q.rationale["mece_verdict"] in {v.value for v in Verdict}
    assert q.rationale["safe_to_sell"] is True


def test_decisions_are_recorded_even_when_nothing_is_quoted():
    """Un-acted decisions make calibration measurable without survivorship bias
    (PLAN.md 6.3) -- and here they are the audit trail of a refused trade."""
    legs = [mk_leg(0, 34, 36, close_at_us=NOW + 365 * DAY),
            mk_leg(1, 34, 36, close_at_us=NOW + 365 * DAY),
            mk_leg(2, 33, 35, close_at_us=NOW + 365 * DAY)]
    st = sleeve().desired_state(mk_snapshot(legs))
    assert st.quotes == ()
    assert len(st.decisions) == 3
    assert all(not d.acted for d in st.decisions)
    assert all(d.shrunk_edge == pytest.approx(0.5 * d.raw_edge) for d in st.decisions)


# --------------------------------------------------------------------------- #
# 9.  Partial / unlocked structures
# --------------------------------------------------------------------------- #
def test_a_partial_short_sells_only_the_rich_legs():
    """One leg fails the depth filter, so the basket cannot lock.  What remains
    is directional, and the legs Mutually-Exclusive Kelly DECLINES to buy are
    exactly the ones worth selling.

    `allow_partial` is OFF by default -- this path is directional, not
    arbitrage -- so it must be asked for explicitly.
    """
    legs = [mk_leg(0, 30, 34), mk_leg(1, 30, 34), mk_leg(2, 30, 34),
            mk_leg(3, 10, 14, yes_bid_size=5.0)]
    st = sleeve(allow_partial=True).evaluate(mk_event(), legs, mk_snapshot(legs))
    assert st is not None
    assert st.direction is Direction.SHORT
    assert not st.locked
    assert not st.is_arbitrage                     # unlocked is never arbitrage
    assert st.legs == ("KXA-0", "KXA-1", "KXA-2")  # the illiquid leg is dropped
    assert st.size > 0
    assert "partial short" in st.reasons[0]


def test_a_partial_short_emits_only_its_own_legs():
    legs = [mk_leg(0, 30, 34), mk_leg(1, 30, 34), mk_leg(2, 30, 34),
            mk_leg(3, 10, 14, yes_bid_size=5.0)]
    st = sleeve(allow_partial=True).desired_state(mk_snapshot(legs))
    assert {q.ticker for q in st.quotes} == {"KXA-0", "KXA-1", "KXA-2"}
    assert st.rationale["partial_directional"] == 1
    assert st.rationale["locked_arbitrage"] == 0
    assert all(q.rationale["locked"] is False for q in st.quotes)


def test_partial_sizing_can_be_disabled():
    legs = [mk_leg(0, 30, 34), mk_leg(1, 30, 34), mk_leg(2, 30, 34),
            mk_leg(3, 10, 14, yes_bid_size=5.0)]
    assert sleeve(allow_partial=False).evaluate(
        mk_event(), legs, mk_snapshot(legs)) is None


def test_devigging_removes_the_overround():
    probs = devigged_probs([0.41, 0.41, 0.34])
    assert sum(probs) == pytest.approx(1.0)
    assert probs[0] == pytest.approx(0.41 / 1.16)


# --------------------------------------------------------------------------- #
# 10.  Fees
# --------------------------------------------------------------------------- #
def test_an_unknown_series_is_priced_pessimistically():
    """Assuming the fee-free 99% for a series we never looked up is exactly how
    a 0.5c margin becomes a loss (research/06 section 4)."""
    legs = uniform(3, 40, 42)
    snap = MarketSnapshot(now_us=NOW, markets=tuple(legs), bankroll_cents=1_000_000,
                          events={"KXA": mk_event()})       # no series cache
    assert sleeve().fee_spec(legs, snap) == UNKNOWN_SERIES_FEE_SPEC
    assert UNKNOWN_SERIES_FEE_SPEC.fee_type == "quadratic_with_maker_fees"


def test_the_worst_leg_fee_governs_the_whole_structure():
    legs = uniform(3, 40, 42)
    cheap = mk_snapshot(legs, series=Series(ticker="KXA"))
    dear = mk_snapshot(legs, series=Series(ticker="KXA",
                                           fee_type="quadratic_with_maker_fees"))
    s = sleeve()
    assert s.fee_spec(legs, cheap).fee_type == "quadratic"
    assert s.fee_spec(legs, dear).fee_type == "quadratic_with_maker_fees"
    lo = s.evaluate(mk_event(), legs, cheap)
    hi = s.evaluate(mk_event(), legs, dear)
    assert lo is not None and hi is not None
    assert hi.margin < lo.margin                    # fees come out of the margin


# --------------------------------------------------------------------------- #
# 11.  Purity (C4.2a) -- what lets backtest, shadow and live share one path
# --------------------------------------------------------------------------- #
def test_desired_state_is_deterministic():
    legs = uniform(4, 30, 34) + uniform(3, 40, 42, event="KXB")
    snap = MarketSnapshot(
        now_us=NOW, markets=tuple(legs), bankroll_cents=1_000_000,
        series={"KXA": Series(ticker="KXA")},
        events={"KXA": mk_event("KXA"), "KXB": mk_event("KXB")},
    )
    s = sleeve()
    a, b = s.desired_state(snap), s.desired_state(snap)
    assert [q.key() for q in a.quotes] == [q.key() for q in b.quotes]
    assert [q.size for q in a.quotes] == [q.size for q in b.quotes]
    assert [q.rationale["structure_id"] for q in a.quotes] == \
           [q.rationale["structure_id"] for q in b.quotes]
    assert a.rationale == b.rationale


def test_structure_ids_are_content_addressed_not_random():
    """A backtest replaying the same snapshot must reproduce the same id, so the
    id cannot be a uuid."""
    legs = uniform(3, 40, 42)
    one = sleeve().evaluate(mk_event(), legs, mk_snapshot(legs))
    two = S2ShortBasket().evaluate(mk_event(), legs, mk_snapshot(legs))
    assert one is not None and two is not None
    assert one.structure_id == two.structure_id
    # a different book is a different structure
    other = sleeve().evaluate(mk_event(), uniform(3, 41, 43),
                              mk_snapshot(uniform(3, 41, 43)))
    assert other is not None and other.structure_id != one.structure_id


def test_sleeve_makes_no_network_call(monkeypatch):
    import httpx

    def explode(*a, **k):
        raise AssertionError("a sleeve must never touch the network (C4.2b)")

    monkeypatch.setattr(httpx.Client, "request", explode)
    legs = uniform(3, 40, 42)
    sleeve().desired_state(mk_snapshot(legs))


def test_sleeve_does_not_read_the_clock(monkeypatch):
    """Time comes from the snapshot -- otherwise a backtest silently uses NOW."""
    import time as _time

    monkeypatch.setattr(_time, "time_ns",
                        lambda: (_ for _ in ()).throw(AssertionError("clock read")))
    legs = uniform(3, 40, 42)
    st = sleeve().desired_state(mk_snapshot(legs))
    assert st.quotes


def test_sleeve_conforms_to_the_protocol():
    from strategy.base import Sleeve

    s = sleeve()
    assert isinstance(s, Sleeve)
    assert s.id == "S2"
    assert s.gate < 4                               # I5: not clear for live orders


def test_the_directional_partial_path_is_off_by_default():
    """The book's stated purpose is ARBITRAGE, and a partial short is not one.

    Selling a SUBSET of a mutually-exclusive set pays only if the winner falls
    outside the subset -- a directional bet sized by Kelly, with no locked
    margin.  It is a legitimate strategy and it must be asked for.

    It also publishes `margin = sum(px) - 1`, negative by construction on this
    path, which is copied into `structures.target_margin_cents`; KPI 6 counts
    only positive targets in its denominator, so a directional structure
    disappears from the orphan-loss ratio while still able to contribute losses.
    """
    legs = [mk_leg(0, 30, 34), mk_leg(1, 30, 34), mk_leg(2, 30, 34),
            mk_leg(3, 10, 14, yes_bid_size=5.0)]
    assert sleeve().evaluate(mk_event(), legs, mk_snapshot(legs)) is None
    assert sleeve(allow_partial=True).evaluate(
        mk_event(), legs, mk_snapshot(legs)) is not None


# --------------------------------------------------------------------------- #
# Collateral return -- pinned to Kalshi's own published worked example
# --------------------------------------------------------------------------- #
def test_netted_collateral_matches_kalshis_published_example():
    """Kalshi's "Collateral Return" article, verbatim:

        NO at 60c + NO at 70c -> invested $1.30, at least one pays $1,
        so collateral is $0.30, not $1.30.

    Two NO legs at 60c and 70c are short YES at 40c and 30c, so `prices` (which
    are YES-referenced) are 0.40 and 0.30 and `1 - sum(px)` = 0.30.  Encoding
    their example here means a future refactor of the collateral rule fails
    against the venue's own documentation rather than against our reading of it.
    """
    from strategy.s2_shortbasket import Direction, locked_capital

    assert locked_capital([0.40, 0.30], direction=Direction.SHORT,
                          netted=True) == pytest.approx(0.30)
    # ...and the un-netted default is the sum of per-leg costs, 0.60 + 0.70
    assert locked_capital([0.40, 0.30], direction=Direction.SHORT,
                          netted=False) == pytest.approx(1.30)


def test_a_profitable_basket_nets_to_zero_collateral():
    """The consequence that changes the strategy's economics.

    Once `sum(px) > 1` the basket is a locked profit, so there is nothing left
    to collateralise -- capital stops binding and joint FILL probability becomes
    the only constraint that matters.
    """
    from strategy.s2_shortbasket import Direction, locked_capital

    assert locked_capital([0.74, 0.13, 0.16], direction=Direction.SHORT,
                          netted=True) == 0.0
    # un-netted, the same basket appears to lock $1.97 per contract
    assert locked_capital([0.74, 0.13, 0.16], direction=Direction.SHORT,
                          netted=False) == pytest.approx(1.97)
