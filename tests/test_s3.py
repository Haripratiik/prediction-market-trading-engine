"""S3 acceptance -- linked-market relative value.  PLAN.md 3.3.

The properties pinned here are the ones whose failure is silent and expensive:

  * a monotonicity violation in a threshold ladder is DETECTED (the primary
    target: KXMIDTERMVOTETURN 502 events, KXMIDTERMMOV 475, KXNCAAF1H 206, ...)
  * a correctly ordered ladder produces NO trade -- otherwise the sleeve is a
    spread-capture bot wearing an arbitrage label
  * an unVERIFIED link is NEVER traded, and a Link cannot self-certify (C4)
  * the maker/taker economics reproduce, including that the double-taker version
    of the canonical 60c/55c structure is a LOSS once it crosses a book (C2)
  * L4 is sized at half, because its constraint is an assumption and a wrong one
    costs the full $1
  * the sleeve is PURE (C4.2a) -- no clock, no I/O, deterministic

Fixtures are built here.  Nothing touches data/pm.db or the network.
"""

from __future__ import annotations

from unittest import mock

import pytest

from core.math.contracts import FeeSpec, KalshiFeeType
from core.models import Event, Market, Series, Side
from rulebook.exhaustiveness import Verdict
from rulebook.links import (
    MIN_NET_CENTS,
    Ladder,
    LadderDirection,
    Link,
    LinkRegistry,
    LinkSource,
    LinkType,
    Milestone,
    StrikeKind,
    bounded_link,
    candidates_from_milestones,
    candidates_from_same_underlying,
    detect_ladders,
    identity_link,
    implication_link,
    ladder_direction,
    parse_strike,
    partition_link,
)
from strategy.base import MarketSnapshot, Sleeve
from strategy.s3_linked_rv import (
    S3Config,
    S3LinkedRV,
    maker_taker_comparison,
    structure_margin,
    taker_completion_limits,
)

# Time is IN the snapshot (C4.2a), so it is a constant here, not a clock read.
NOW_US = 1_800_000_000_000_000
HOUR_US = 3_600_000_000
DAY_US = 24 * HOUR_US

LADDER_EVENT = "KXNCAAFTOTAL-25AUG23MICHOSU"
LADDER_SERIES = "KXNCAAFTOTAL"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def mk_market(
    ticker: str,
    event_ticker: str,
    *,
    bid: int,
    ask: int,
    title: str,
    bid_size: float = 500.0,
    ask_size: float = 500.0,
    close_us: int = NOW_US + 30 * DAY_US,
    series_ticker: str = LADDER_SERIES,
    rules_hash: str = "rules-v1",
) -> Market:
    return Market(
        ticker=ticker,
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        title=title,
        yes_bid=bid,
        yes_ask=ask,
        yes_bid_size=bid_size,
        yes_ask_size=ask_size,
        volume_24h=1000.0,
        close_at_us=close_us,
        rules_hash=rules_hash,
    )


def ladder_event(*, mutually_exclusive: bool = False,
                 collateral_return_type: str = "DIRECNET",
                 event_ticker: str = LADDER_EVENT) -> Event:
    return Event(
        event_ticker=event_ticker,
        series_ticker=LADDER_SERIES,
        category="Sports",
        title="Michigan vs Ohio State total points",
        mutually_exclusive=mutually_exclusive,
        collateral_return_type=collateral_return_type,
    )


def ladder_markets(quotes: dict[str, tuple[int, int]], **kw) -> list[Market]:
    """`{"40.5": (bid, ask), ...}` -> the KXNCAAFTOTAL threshold ladder."""
    return [
        mk_market(
            f"{LADDER_EVENT}-T{strike}", LADDER_EVENT,
            bid=bid, ask=ask,
            title=f"Will the total be more than {strike} points?",
            **kw,
        )
        for strike, (bid, ask) in quotes.items()
    ]


# P(>45.5) = 0.61 > P(>40.5) = 0.56 -- IMPOSSIBLE: the tighter condition cannot
# be likelier than the looser one it implies.  This is the whole sleeve.
VIOLATING = {"40.5": (55, 57), "45.5": (60, 62), "50.5": (30, 32)}
ORDERED = {"40.5": (70, 72), "45.5": (50, 52), "50.5": (30, 32)}


def snapshot(
    quotes: dict[str, tuple[int, int]] = VIOLATING,
    *,
    now_us: int = NOW_US,
    bankroll_cents: int = 1_000_000,
    fee_type: KalshiFeeType = "quadratic",
    event: Event | None = None,
    **market_kw,
) -> MarketSnapshot:
    ev = event or ladder_event()
    markets = ladder_markets(quotes, **market_kw)
    series = Series(ticker=LADDER_SERIES, fee_type=fee_type)
    return MarketSnapshot(
        now_us=now_us,
        markets=tuple(markets),
        events={ev.event_ticker: ev},
        series={LADDER_SERIES: series},
        bankroll_cents=bankroll_cents,
    )


def verified_sleeve(
    snap: MarketSnapshot,
    cfg: S3Config | None = None,
    *,
    only: LinkType | None = None,
) -> S3LinkedRV:
    """Build the sleeve and have the 'human' sign off on what it detected.

    This is the real workflow: the detector proposes, the registry disposes.
    """
    sleeve = S3LinkedRV(cfg=cfg or S3Config())
    links, _ = sleeve.resolve_links(snap)
    for link in links:
        if only is None or link.link_type is only:
            sleeve.registry.approve_link(link, note="test fixture")
    return sleeve


def structure_ids(state) -> set[str]:
    return {q.rationale["structure_id"] for q in state.quotes}


# --------------------------------------------------------------------------- #
# 1. Ladder parsing -- real Kalshi ticker shapes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "ticker, event_ticker, kind, value, code",
    [
        # the primary target series
        ("KXNCAAFTOTAL-25AUG23MICHOSU-T45.5", "KXNCAAFTOTAL-25AUG23MICHOSU",
         StrikeKind.THRESHOLD, 45.5, "T45.5"),
        # bucket midpoint -- parsed, but NOT a monotone ladder rung
        ("KXHIGHNY-26AUG26-B84.5", "", StrikeKind.BUCKET, 84.5, "B84.5"),
        # the 18-rung Fed ladder
        ("KXFED-27APR-T4.25", "KXFED-27APR", StrikeKind.THRESHOLD, 4.25, "T4.25"),
        # NEGATIVE strike: the value carries its own '-', which is why this is a
        # regex and not a rsplit
        ("KXNCAAFSPREAD-25SEP06ALAWIS-T-3.5", "", StrikeKind.THRESHOLD, -3.5, "T-3.5"),
        # integer strike
        ("KXMLBINNINGWIN-25AUG26NYYBOS-T1", "", StrikeKind.THRESHOLD, 1.0, "T1"),
        # a series whose name ends in a digit
        ("KXNCAAF1H-25SEP06MICHOSU-T17.5", "", StrikeKind.THRESHOLD, 17.5, "T17.5"),
    ],
)
def test_parse_strike_real_ticker_shapes(ticker, event_ticker, kind, value, code):
    s = parse_strike(ticker, event_ticker=event_ticker)
    assert s is not None
    assert (s.kind, s.value, s.code) == (kind, value, code)
    assert s.from_title is False


@pytest.mark.parametrize(
    "ticker",
    [
        "KXINXDIRY-27DEC31H1600",   # event ticker, no strike code
        "KXHIGHNY-26AUG26",         # date suffix must not read as a strike
        "KXOSCARVIS-27",            # year suffix
        "KXFED",                    # bare series
    ],
)
def test_parse_strike_rejects_non_strike_suffixes(ticker):
    assert parse_strike(ticker) is None


def test_parse_strike_falls_back_to_the_title():
    s = parse_strike("KXCPI-26AUG", title="Will CPI be 3.1% or more?")
    assert s is not None and s.kind is StrikeKind.THRESHOLD
    assert s.value == 3.1 and s.from_title

    rng = parse_strike("KXHIGHNY-26AUG26", title="Will the high be between 84 and 85?")
    assert rng is not None and rng.kind is StrikeKind.BUCKET
    assert rng.value == 84.5      # midpoint, matching Kalshi's own B convention


def test_ladder_direction_reads_negated_comparatives_correctly():
    # "no more than" is an UPPER bound.  Reading it as ABOVE would invert every
    # L2 on the ladder and turn a hedge into a doubled directional bet.
    assert ladder_direction(["Will the total be no more than 45.5?"]) is LadderDirection.BELOW
    assert ladder_direction(["Will the total be more than 45.5?"]) is LadderDirection.ABOVE
    assert ladder_direction(["45.5 or more"]) is LadderDirection.ABOVE
    assert ladder_direction(["Under 45.5"]) is LadderDirection.BELOW
    # conflicting or absent -> no ladder, rather than a guess
    assert ladder_direction(["above 40", "below 50"]) is None
    assert ladder_direction(["Michigan vs Ohio State"]) is None


def test_direction_is_never_inferred_from_prices():
    """A ladder with no directional wording is refused even though its prices
    descend perfectly.  Inferring direction from the observed ordering would make
    every ladder 'correctly ordered' by construction and delete the signal."""
    ev = ladder_event()
    markets = [
        mk_market(f"{LADDER_EVENT}-T{s}", LADDER_EVENT, bid=b, ask=a, title=f"{s}")
        for s, (b, a) in ORDERED.items()
    ]
    scan = detect_ladders(markets, {ev.event_ticker: ev})
    assert scan.ladders == ()
    assert "no title states an unambiguous ladder direction" in scan.reason_counts()


# --------------------------------------------------------------------------- #
# 2. Ladder detection -- read `mutually_exclusive` PER EVENT
# --------------------------------------------------------------------------- #
def test_direcnet_threshold_ladder_is_detected():
    ev = ladder_event()
    scan = detect_ladders(ladder_markets(VIOLATING), {ev.event_ticker: ev})
    assert len(scan.ladders) == 1
    ladder = scan.ladders[0]
    assert ladder.direction is LadderDirection.ABOVE
    assert [s.value for s in ladder.strikes] == [40.5, 45.5, 50.5]


def test_mece_bracket_event_is_not_a_ladder():
    """research/06 section 3: only 7 series mix both shapes, so the flag is read
    per event.  A MECNET bracket set sums to 1; it is not monotone."""
    ev = ladder_event(mutually_exclusive=True, collateral_return_type="MECNET")
    scan = detect_ladders(ladder_markets(VIOLATING), {ev.event_ticker: ev})
    assert scan.ladders == ()
    assert "mutually_exclusive: bracket set, not a ladder" in scan.reason_counts()


def test_bucket_codes_do_not_form_a_monotone_ladder():
    """P is not monotone in a bucket midpoint -- disjoint ranges do not nest."""
    ev = ladder_event(event_ticker="KXHIGHNY-26AUG26")
    markets = [
        mk_market(f"KXHIGHNY-26AUG26-B{v}", "KXHIGHNY-26AUG26",
                  bid=20, ask=22, title=f"Will the high be between {v - 0.5} and {v + 0.5}?")
        for v in (84.5, 85.5, 86.5)
    ]
    scan = detect_ladders(markets, {ev.event_ticker: ev})
    assert scan.ladders == ()
    assert any("not all thresholds" in r for r in scan.reason_counts())


def test_event_absent_from_snapshot_is_refused():
    """The flag cannot be cached per series, so an unreadable flag is a refusal."""
    scan = detect_ladders(ladder_markets(VIOLATING), {})
    assert scan.ladders == ()
    assert "event not in snapshot (flag unreadable)" in scan.reason_counts()


def test_implication_pairs_orient_by_direction():
    rungs = [parse_strike(f"{LADDER_EVENT}-T{v}") for v in ("40.5", "45.5")]
    assert all(r is not None for r in rungs)
    strikes = tuple(r for r in rungs if r is not None)

    above = Ladder(LADDER_EVENT, LADDER_SERIES, LadderDirection.ABOVE, strikes)
    (subset, superset), = above.implication_pairs()
    assert subset.value == 45.5 and superset.value == 40.5   # P(>45.5) <= P(>40.5)

    below = Ladder(LADDER_EVENT, LADDER_SERIES, LadderDirection.BELOW, above.strikes)
    (subset, superset), = below.implication_pairs()
    assert subset.value == 40.5 and superset.value == 45.5   # P(<40.5) <= P(<45.5)


def test_non_adjacent_pairs_are_available_but_off_by_default():
    """Adjacent strikes are the default because they are the likeliest to be
    mispriced against each other; the full transitive closure is opt-in."""
    ev = ladder_event()
    ladder, = detect_ladders(ladder_markets(VIOLATING), {ev.event_ticker: ev}).ladders
    assert len(ladder.implication_pairs()) == 2                       # 3 rungs
    assert len(ladder.implication_pairs(adjacent_only=False)) == 3    # C(3,2)


# --------------------------------------------------------------------------- #
# 3. The core signal
# --------------------------------------------------------------------------- #
def test_monotonicity_violation_produces_a_two_leg_structure():
    snap = snapshot(VIOLATING)
    state = verified_sleeve(snap).desired_state(snap)

    assert len(state.quotes) == 2, "one violation -> exactly two legs"
    sell, buy = state.quotes
    assert sell.rationale["structure_id"] == buy.rationale["structure_id"]
    assert sell.rationale["link_type"] == LinkType.IMPLICATION.value

    # sell the subset (the impossibly rich tighter condition), buy the superset
    assert sell.ticker == f"{LADDER_EVENT}-T45.5"
    assert buy.ticker == f"{LADDER_EVENT}-T40.5"

    # MAKER on both legs: join the ask to sell, join the bid to buy.  Neither
    # order crosses, and both capture a half-spread the taker would surrender.
    assert (sell.price_cents, buy.price_cents) == (62, 55)
    assert sell.post_only and buy.post_only
    assert sell.rationale["gross_cents"] == pytest.approx(7.0)
    assert sell.rationale["mid_violation_cents"] == pytest.approx(5.0)
    assert state.rationale["structures"] == 1


def test_sell_leg_is_side_no_at_the_YES_price():
    """The easiest thing in this codebase to get wrong.  `price_cents` is always
    YES-referenced: a Side.NO quote at YES-price p is a resting YES ASK at p
    (shadow/engine.py), NOT a bid of 100-p."""
    snap = snapshot(VIOLATING)
    sell, buy = verified_sleeve(snap).desired_state(snap).quotes
    assert sell.side is Side.NO and sell.price_cents == 62      # not 38
    assert buy.side is Side.YES and buy.price_cents == 55


def test_correctly_ordered_ladder_produces_no_trade():
    snap = snapshot(ORDERED)
    state = verified_sleeve(snap).desired_state(snap)
    assert state.quotes == ()
    assert state.rationale["skipped"]["prices satisfy the constraint"] == 2
    assert state.rationale["ladders_detected"] == 1   # detected, just not violated


def test_structure_publishes_the_two_leg_unwind_discipline():
    """'Complete as taker if the margin survives, else unwind inside the timeout.'
    Both halves of that sentence are published with the order so the executor
    never re-derives them under time pressure -- and so 'it will probably
    converge anyway' is not an available answer."""
    snap = snapshot(VIOLATING)
    sell, _ = verified_sleeve(snap).desired_state(snap).quotes
    r = sell.rationale
    assert r["leg_timeout_seconds"] == 900                        # config/risk.yaml
    assert r["unwind_deadline_us"] == NOW_US + 900 * 1_000_000
    assert r["max_taker_buy_cents"] is not None
    assert r["min_taker_sell_cents"] is not None
    # completing as a taker must be strictly worse than our resting price
    assert r["max_taker_buy_cents"] > 55
    assert r["min_taker_sell_cents"] < 62


def test_legs_that_settle_far_apart_are_refused():
    """An L1/L2 payoff is only guaranteed if BOTH legs are held to settlement."""
    ev = ladder_event()
    markets = ladder_markets(VIOLATING)
    markets[1] = markets[1].model_copy(update={"close_at_us": NOW_US + 90 * DAY_US})
    snap = MarketSnapshot(
        now_us=NOW_US, markets=tuple(markets), events={ev.event_ticker: ev},
        bankroll_cents=1_000_000,
    )
    state = verified_sleeve(snap).desired_state(snap)
    assert state.quotes == ()
    assert "legs settle too far apart to hold both" in state.rationale["skipped"]


def test_rolc_hurdle_rejects_capital_locked_for_years():
    snap = snapshot(VIOLATING, close_us=NOW_US + 5 * 365 * DAY_US)
    state = verified_sleeve(snap).desired_state(snap)
    assert state.quotes == ()
    assert "annualized ROLC below hurdle" in state.rationale["skipped"]


def test_min_net_gate_blocks_a_structure_that_does_not_clear_the_hurdle():
    snap = snapshot(VIOLATING)
    cfg = S3Config(min_net_cents={**MIN_NET_CENTS, LinkType.IMPLICATION: 8.0})
    state = verified_sleeve(snap, cfg).desired_state(snap)
    assert state.quotes == ()
    assert any("below 8.00c" in k for k in state.rationale["skipped"])


def test_maker_fee_series_reduces_the_net_but_still_clears():
    """Only ~130 of 13,486 series charge makers anything; on those the same
    structure keeps 0.85c less."""
    free = snapshot(VIOLATING, fee_type="quadratic")
    paid = snapshot(VIOLATING, fee_type="quadratic_with_maker_fees")
    free_sell, _ = verified_sleeve(free).desired_state(free).quotes
    paid_sell, _ = verified_sleeve(paid).desired_state(paid).quotes
    assert free_sell.rationale["fee_cents"] == 0.0
    assert paid_sell.rationale["fee_cents"] > 0.0
    assert paid_sell.rationale["net_cents"] < free_sell.rationale["net_cents"]


def test_decisions_are_recorded_whether_acted_on_or_not():
    """PLAN.md 6.3 -- un-acted decisions are what make the hit rate measurable
    without survivorship bias."""
    snap = snapshot(VIOLATING)
    cfg = S3Config(min_net_cents={**MIN_NET_CENTS, LinkType.IMPLICATION: 8.0})
    blocked = verified_sleeve(snap, cfg).desired_state(snap)
    assert blocked.quotes == ()
    assert len(blocked.decisions) == 1
    d = blocked.decisions[0]
    assert d.acted is False
    assert d.raw_edge == pytest.approx(0.05)
    # no forecast is involved: the constraint itself caps the rich leg
    assert d.p_model == pytest.approx(0.56)
    assert d.market_price == pytest.approx(0.61)


# --------------------------------------------------------------------------- #
# 4. The hard equivalence gate (C4)
# --------------------------------------------------------------------------- #
def test_unverified_link_is_never_traded():
    snap = snapshot(VIOLATING)
    sleeve = S3LinkedRV()                       # empty registry: nothing signed off
    state = sleeve.desired_state(snap)
    assert state.quotes == ()
    assert state.rationale["links_verified"] == 0
    assert any("not VERIFIED" in k for k in state.rationale["skipped"])
    # the violation was still SEEN -- the gate blocks the trade, not the detection
    assert state.rationale["ladders_detected"] == 1


def test_a_link_cannot_self_certify():
    """A hand-built Link marked VERIFIED is still downgraded.  The registry is
    the single source of truth; otherwise the gate is a default, not a gate."""
    snap = snapshot(VIOLATING)
    forged = Link(
        link_id="L2|forged", link_type=LinkType.IMPLICATION,
        tickers=(f"{LADDER_EVENT}-T45.5", f"{LADDER_EVENT}-T40.5"),
        equivalence_status=Verdict.VERIFIED,
    )
    sleeve = S3LinkedRV(links=(forged,), cfg=S3Config(auto_detect_ladders=False))
    resolved, _ = sleeve.resolve_links(snap)
    assert [lk.equivalence_status for lk in resolved] == [Verdict.NEEDS_HUMAN]
    assert sleeve.desired_state(snap).quotes == ()


def test_a_changed_rulebook_invalidates_an_existing_approval():
    """PLAN.md 3.3: any change in either market's rules_hash forces re-review.
    The failure guarded against is not 'the price moved' but 'the settlement
    source changed and my two legs no longer describe the same world'."""
    snap = snapshot(VIOLATING)
    sleeve = verified_sleeve(snap)
    assert len(sleeve.desired_state(snap).quotes) == 2

    amended = snapshot(VIOLATING, rules_hash="rules-v2")
    links, _ = sleeve.resolve_links(amended)
    link = next(lk for lk in links if lk.tickers[0].endswith("T45.5"))
    assert sleeve.registry.is_stale(link)
    assert link.equivalence_status is Verdict.NEEDS_HUMAN
    assert sleeve.desired_state(amended).quotes == ()


def test_registry_revoke_stops_trading_immediately():
    snap = snapshot(VIOLATING)
    sleeve = verified_sleeve(snap)
    link_id = next(iter(sleeve.registry.approvals))
    sleeve.registry.revoke(link_id)
    assert sleeve.desired_state(snap).quotes == ()


# --------------------------------------------------------------------------- #
# 5. The economics table (C2) -- maker discipline IS the sleeve
# --------------------------------------------------------------------------- #
def test_maker_taker_economics_table_reproduces():
    """PLAN.md 3.3, computed rather than quoted, on the canonical 60c/55c
    implication violation with a 5c gross."""
    table = maker_taker_comparison()

    maker = table["double_maker"]
    assert maker.gross_cents == pytest.approx(5.0)
    assert maker.fee_cents == pytest.approx(0.85, abs=0.01)      # 0.25x the taker
    assert maker.net_cents == pytest.approx(4.1, abs=0.05)
    assert maker.edge_to_cost == pytest.approx(4.8, abs=0.1)     # the 4.8:1 ratio

    # At the reference prices alone the taker clears a bare 1.5c against a 3.41c
    # hurdle -- but a taker cannot transact at reference prices.
    ref = table["double_taker_at_reference"]
    assert ref.fee_cents == pytest.approx(3.41, abs=0.01)
    assert ref.net_cents == pytest.approx(1.5, abs=0.1)

    # Crossing one cent of spread on each leg turns it into a LOSS.  This is the
    # entire case for maker-first execution.
    crossed = table["double_taker_after_crossing"]
    assert crossed.net_cents < 0.0
    assert maker.net_cents > 2 * ref.net_cents


def test_maker_fee_is_exactly_zero_on_the_13353_plain_quadratic_series():
    q = FeeSpec.kalshi("quadratic", 1.0)
    m = structure_margin(sell_price_cents=60, buy_price_cents=55,
                         sell_spec=q, buy_spec=q)
    assert m.fee_cents == 0.0 and m.net_cents == 5.0
    # and the legs do NOT net: DIRECNET locks collateral on both
    assert m.locked_cents == 95.0


def test_fee_is_symmetric_so_the_sell_leg_prices_off_its_YES_price():
    q = FeeSpec.kalshi("quadratic_with_maker_fees", 1.0)
    a = structure_margin(sell_price_cents=60, buy_price_cents=55,
                         sell_spec=q, buy_spec=q)
    b = structure_margin(sell_price_cents=45, buy_price_cents=40,
                         sell_spec=q, buy_spec=q)
    # fee(p) = theta*p*(1-p) is symmetric under p -> 1-p
    assert a.fee_cents == pytest.approx(b.fee_cents)


def test_taker_completion_limits_bracket_the_maker_prices():
    q = FeeSpec.kalshi("quadratic", 1.0)
    max_buy, min_sell = taker_completion_limits(
        sell_price_cents=60, buy_price_cents=55, sell_spec=q, buy_spec=q)
    assert max_buy is not None and min_sell is not None
    assert 55 < max_buy < 60      # room to pay up, but not past the short
    assert 55 < min_sell < 60


# --------------------------------------------------------------------------- #
# 6. L4 -- statistical, not arbitrage, and therefore half size
# --------------------------------------------------------------------------- #
def test_l4_is_sized_at_half_relative_to_l1_and_l2():
    sleeve = S3LinkedRV()
    common = dict(sell_depth=500.0, buy_depth=500.0,
                  locked_cents=93.0, bankroll_cents=1_000_000)
    hard = sleeve.structure_size(link_type=LinkType.IMPLICATION, **common)
    identity = sleeve.structure_size(link_type=LinkType.IDENTITY, **common)
    soft = sleeve.structure_size(link_type=LinkType.BOUNDED, **common)
    assert hard == identity == 100
    assert soft == 50


def test_l4_is_sized_at_half_end_to_end():
    snap = snapshot(VIOLATING)
    l2 = verified_sleeve(snap, only=LinkType.IMPLICATION)
    cfg = S3Config(ladder_k_bound=0.01)
    l4 = verified_sleeve(snap, cfg, only=LinkType.BOUNDED)

    pair = {f"{LADDER_EVENT}-T45.5", f"{LADDER_EVENT}-T40.5"}

    def size_for(state) -> int:
        legs = [q for q in state.quotes if {q.ticker, q.rationale["pair_ticker"]} == pair]
        assert len(legs) == 2
        assert legs[0].size == legs[1].size, "both legs of a structure size alike"
        return legs[0].size

    hard = size_for(l2.desired_state(snap))
    soft = size_for(l4.desired_state(snap))
    assert hard == 100 and soft == 50


def test_l4_bound_absorbs_small_gaps():
    """|P(A) - P(B)| <= k is an ASSUMPTION.  Inside k there is no edge at all,
    and the bound is subtracted from the gross rather than ignored."""
    a, b = f"{LADDER_EVENT}-T40.5", f"{LADDER_EVENT}-T45.5"
    prices = {a: 0.56, b: 0.61}                    # gap of 5c

    assert bounded_link(a, b, 0.10).violation(prices) is None      # inside the bound
    v = bounded_link(a, b, 0.01).violation(prices)
    assert v is not None
    assert v.size == pytest.approx(0.04)           # only the excess is edge
    assert (v.sell_ticker, v.buy_ticker) == (b, a)  # sell rich, buy cheap


def test_l4_carries_a_higher_net_hurdle_than_l1_and_l2():
    assert MIN_NET_CENTS[LinkType.BOUNDED] > MIN_NET_CENTS[LinkType.IMPLICATION]
    assert MIN_NET_CENTS[LinkType.IDENTITY] == MIN_NET_CENTS[LinkType.IMPLICATION]


def test_only_one_structure_rests_per_ticker_pair():
    """A ladder pair carries both a hard L2 and a soft L4.  Resting both would
    quietly double the size on the same two books."""
    snap = snapshot(VIOLATING)
    sleeve = verified_sleeve(snap, S3Config(ladder_k_bound=0.01))
    state = sleeve.desired_state(snap)
    pairs = [frozenset((q.ticker, q.rationale["pair_ticker"])) for q in state.quotes]
    assert len(set(pairs)) == len(pairs) // 2
    assert "superseded by a richer structure on the same pair" in state.rationale["skipped"]


# --------------------------------------------------------------------------- #
# 7. Link algebra
# --------------------------------------------------------------------------- #
def test_identity_link_sells_the_rich_side():
    v = identity_link("A", "B").violation({"A": 0.62, "B": 0.55})
    assert v is not None and (v.sell_ticker, v.buy_ticker) == ("A", "B")
    assert v.size == pytest.approx(0.07)
    assert identity_link("A", "B").violation({"A": 0.55, "B": 0.55}) is None


def test_implication_only_fires_in_one_direction():
    link = implication_link("SUB", "SUP")            # P(SUB) <= P(SUP)
    assert link.violation({"SUB": 0.40, "SUP": 0.90}) is None
    v = link.violation({"SUB": 0.90, "SUP": 0.40})
    assert v is not None and (v.sell_ticker, v.buy_ticker) == ("SUB", "SUP")


def test_partition_error_is_signed_and_not_a_two_leg_trade():
    link = partition_link("Q", ["M1", "M2", "M3"])
    prices = {"Q": 0.60, "M1": 0.25, "M2": 0.25, "M3": 0.20}
    assert link.partition_error(prices) == pytest.approx(0.10)
    assert link.violation(prices) is None            # >2 legs: no two-leg trade


def test_multi_leg_links_are_recorded_but_not_traded():
    """The two-leg unwind protocol does not describe a 3-leg structure and its
    orphan risk is a different shape, so L3 stops at the link graph."""
    snap = snapshot(VIOLATING)
    l3 = partition_link(f"{LADDER_EVENT}-T40.5",
                        [f"{LADDER_EVENT}-T45.5", f"{LADDER_EVENT}-T50.5"])
    sleeve = S3LinkedRV(links=(l3,), cfg=S3Config(auto_detect_ladders=False))
    sleeve.registry.approve_link(l3)
    state = sleeve.desired_state(snap)
    assert state.quotes == ()
    assert "multi-leg structure needs the n-leg protocol" in state.rationale["skipped"]


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(link_id="x", link_type=LinkType.IMPLICATION, tickers=("A", "B", "C")),
        dict(link_id="x", link_type=LinkType.PARTITION, tickers=("A", "B")),
        dict(link_id="x", link_type=LinkType.BOUNDED, tickers=("A", "B")),
        dict(link_id="x", link_type=LinkType.IDENTITY, tickers=("A", "A")),
        dict(link_id="x", link_type=LinkType.IDENTITY, tickers=("A", "B"), k_bound=0.1),
    ],
)
def test_malformed_links_are_rejected_at_construction(kwargs):
    with pytest.raises(ValueError):
        Link(**kwargs)


# --------------------------------------------------------------------------- #
# 8. Discovery -- milestones, never title similarity
# --------------------------------------------------------------------------- #
def test_milestones_seed_candidates_across_series():
    """research/06 section 3.2.  Title matching found 1 real duplicate and 9
    false positives across 12,000 events; this is Kalshi's own index."""
    ms = Milestone.from_api({
        "title": "Strait of Hormuz", "category": "Elections",
        "primary_event_tickers": ["KXHORMUZNORM-26MAR17"],
        "related_event_tickers": [
            "KXHORMUZNORM-26MAR17", "KXHORMUZWEEKLY-26AUG23",
            "KXHORMUZMAX-26AUG23", "KXMAXSHIPSHORMUZ-26AUG31",
        ],
    })
    assert len(ms.event_tickers) == 4            # the duplicate is folded away
    cands = candidates_from_milestones([ms])
    assert len(cands) == 6                       # C(4,2)
    assert all(c.event_a != c.event_b for c in cands)
    assert all(c.source is LinkSource.MILESTONE for c in cands)
    # a candidate is NOT a link: the endpoint says "related", not "P(A) <= P(B)"
    assert not any(isinstance(c, Link) for c in cands)


def test_milestone_candidates_are_deterministic_and_deduplicated():
    ms = Milestone(title="t", related_event_tickers=("B", "A", "B"))
    once = candidates_from_milestones([ms, ms])
    assert len(once) == 1 and once[0].key == ("A", "B")


def test_same_underlying_pairs_are_matched_on_the_event_suffix():
    events = [
        "KXFED-27APR", "KXFEDDECISION-27APR", "KXFEDDECISION-28JAN",
        "KXINXDIRY-27DEC31H1600", "KXINXY-27DEC31H1600",
        "KXOSCARVIS-27", "KXOSCARMAH-27",
    ]
    keys = {c.key for c in candidates_from_same_underlying(events)}
    assert ("KXFED-27APR", "KXFEDDECISION-27APR") in keys
    assert ("KXINXDIRY-27DEC31H1600", "KXINXY-27DEC31H1600") in keys
    assert ("KXOSCARMAH-27", "KXOSCARVIS-27") in keys
    # KXFEDDECISION-28JAN has no KXFED-28JAN partner listed
    assert not any("28JAN" in k[0] or "28JAN" in k[1] for k in keys)


# --------------------------------------------------------------------------- #
# 9. Purity (C4.2a)
# --------------------------------------------------------------------------- #
def test_sleeve_satisfies_the_sleeve_protocol():
    s = S3LinkedRV()
    assert isinstance(s, Sleeve)
    assert (s.id, s.gate) == ("S3", 2)


def test_identical_snapshot_gives_identical_output():
    snap = snapshot(VIOLATING)
    sleeve = verified_sleeve(snap)
    a = sleeve.desired_state(snap)
    b = sleeve.desired_state(snap)
    assert a.quotes == b.quotes
    assert a.decisions == b.decisions
    assert a.rationale == b.rationale
    assert structure_ids(a) == structure_ids(b)


def test_two_sleeves_built_the_same_way_agree():
    """No hidden per-instance state -- structure ids are derived, never counted."""
    snap = snapshot(VIOLATING)
    assert (verified_sleeve(snap).desired_state(snap).quotes
            == verified_sleeve(snap).desired_state(snap).quotes)


def test_sleeve_never_reads_a_clock():
    """Time is IN the snapshot.  `Market.hours_to_close` falls back to
    `core.models.now_us` when `now` is omitted, so a single missed argument would
    make the sleeve non-reproducible in backtest -- and silently so."""
    snap = snapshot(VIOLATING)
    sleeve = verified_sleeve(snap)
    boom = mock.Mock(side_effect=AssertionError("sleeve read the wall clock"))
    with mock.patch("core.models.now_us", boom), mock.patch("time.time_ns", boom):
        state = sleeve.desired_state(snap)
    assert len(state.quotes) == 2


def test_time_comes_from_the_snapshot():
    """Advance only `now_us` and the final-hour gate engages -- proof the clock
    is an input rather than an ambient read."""
    snap = snapshot(VIOLATING)
    sleeve = verified_sleeve(snap)
    assert len(sleeve.desired_state(snap).quotes) == 2

    late = snapshot(VIOLATING, now_us=NOW_US + 30 * DAY_US - HOUR_US // 2)
    state = sleeve.desired_state(late)
    assert state.quotes == ()
    assert "inside the final hour" in state.rationale["skipped"]


def test_bankroll_scales_size_not_prices():
    small = snapshot(VIOLATING, bankroll_cents=10_000)
    sleeve = verified_sleeve(small)
    quotes = sleeve.desired_state(small).quotes
    assert len(quotes) == 2
    # 5% of $100 = $5 against 93c of locked capital per structure
    assert quotes[0].size == 5
    assert (quotes[0].price_cents, quotes[1].price_cents) == (62, 55)


def test_zero_bankroll_produces_no_quotes_rather_than_a_zero_size_order():
    snap = snapshot(VIOLATING, bankroll_cents=0)
    state = verified_sleeve(snap).desired_state(snap)
    assert state.quotes == ()
    assert "size rounds to zero" in state.rationale["skipped"]


def test_registry_is_the_only_promoter():
    reg = LinkRegistry()
    link = implication_link("A", "B")
    assert reg.status_for(link) is Verdict.NEEDS_HUMAN
    reg.approve_link(link)
    assert reg.status_for(link) is Verdict.VERIFIED
    assert reg.apply([link])[0].equivalence_status is Verdict.VERIFIED
