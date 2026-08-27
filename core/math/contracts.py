"""Contract algebra and the venue fee model.  PLAN.md section 2.1  [CANONICAL]

A binary contract pays $1 on YES and $0 on NO.  Everything here is dollars per
contract, with price in (0, 1).

The single most important structural fact (PLAN.md 2.1, research/07 section 0):
THREE separate quantities all scale as p(1-p) --- they are all second moments of
a Bernoulli, so this is structural rather than coincidence:

    remaining settlement variance   p(1-p)
    Glosten-Milgrom break-even      4*mu*p(1-p) / (1 - mu^2 (2p-1)^2)
    venue fee                       feeRate * p(1-p)

Consequence: quote spreads proportional to p(1-p), and normalise every risk, fee
and signal measurement by p(1-p) or work in log-odds.  Basis points are the WRONG
unit here -- relative spread varies 50x across the book purely mechanically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

Venue = Literal["kalshi", "polymarket_us", "forecastex"]

# --------------------------------------------------------------------------- #
# Kalshi fees are PER SERIES.  research/06 section 4, measured across all 13,486
# series via the public API:
#     fee_type = "quadratic"                       13,353 series -> MAKERS PAY ZERO
#     fee_type = "quadratic_with_maker_fees"          130 series -> maker = 0.25x base
#     fee_type = "quadratic_with_combo_maker_fees"      3 series -> maker = 0.50x base
#     fee_multiplier: 1.0 on 13,499 | 0.5 on 19 (MLB) | 0.0 on NONE
#         (research/06 reported 14 fee-free series; re-checked live 2026-08-26,
#          there are none -- see KALSHI_HISTORICALLY_FEE_FREE below)
#
# WARNING: the 0.07 base coefficient is the one number not confirmed against
# Kalshi's own fee-schedule PDF (HTTP 429 on every fetch across three research
# passes).  Verify before sizing.  research/06 section 4.
# --------------------------------------------------------------------------- #
KALSHI_BASE_TAKER: Final[float] = 0.07

KalshiFeeType = Literal[
    "quadratic",
    "quadratic_with_maker_fees",
    "quadratic_with_combo_maker_fees",
    "flat",
]

KALSHI_MAKER_RATIO: Final[dict[str, float]] = {
    "quadratic": 0.0,
    "quadratic_with_maker_fees": 0.25,
    "quadratic_with_combo_maker_fees": 0.50,
    "flat": 0.0,
}

# Non-Kalshi venues use a flat coefficient.  Negative = rebate paid to you.
THETA_FLAT: Final[dict[tuple[str, bool], float]] = {
    ("polymarket_us", False): 0.06,
    ("polymarket_us", True): -0.0125,
    ("forecastex", False): 0.0,   # $0.01 embedded in the spread, modelled separately
    ("forecastex", True): 0.0,
}

# HISTORICAL, NOT CURRENT.  research/06 section 4.2 reported these 14 series as
# carrying fee_multiplier = 0.  Re-checked against the live API on 2026-08-26:
# ZERO series now have a zero multiplier -- the distribution is {1.0: 13499,
# 0.5: 19}.  The waivers were either withdrawn or were never there.
#
# Do NOT plan around a "fee-free corner" without re-verifying first.  The live
# check is `tests/test_kalshi_client_live.py::test_fee_multiplier_distribution`,
# which asserts what is actually true today and will fail loudly if it changes.
KALSHI_HISTORICALLY_FEE_FREE: Final[frozenset[str]] = frozenset(
    {
        "KXBTCY", "KXETHY", "KXGDPYEAR", "KXLAYOFFSYINFO", "KXCITRINI", "KXDOED",
        "KXELECTIRAN", "KXEXPAND", "KXGAMBLINGREPEAL", "KXGREENLAND",
        "KXIRANDEMOCRACY", "KXNEXTIRANLEADER", "KXPAHLAVIHEAD", "KXTRUMPOUT",
    }
)


@dataclass(frozen=True, slots=True)
class FeeSpec:
    """Per-series fee parameters, read from the Kalshi `/series` cache.

    For non-Kalshi venues only `venue` is consulted.
    """

    venue: Venue
    fee_type: KalshiFeeType = "quadratic"
    fee_multiplier: float = 1.0

    @classmethod
    def kalshi(cls, fee_type: KalshiFeeType, fee_multiplier: float) -> "FeeSpec":
        return cls("kalshi", fee_type, fee_multiplier)

    @classmethod
    def polymarket_us(cls) -> "FeeSpec":
        return cls("polymarket_us")


def _check_price(price: float) -> None:
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0,1), got {price!r}")


def fee(price: float, spec: FeeSpec, *, is_maker: bool) -> float:
    """Fee in dollars per contract.  Parabolic in price, peaking at 0.50.

    Negative return values are rebates (Polymarket US makers).
    """
    _check_price(price)
    if spec.venue == "kalshi":
        base = KALSHI_BASE_TAKER * spec.fee_multiplier
        ratio = KALSHI_MAKER_RATIO[spec.fee_type] if is_maker else 1.0
        theta = base * ratio
    else:
        theta = THETA_FLAT[(spec.venue, is_maker)]
    return theta * price * (1.0 - price)


def edge(p_model: float, price: float, spec: FeeSpec, *, is_maker: bool) -> float:
    """Expected value per contract, held to settlement.

    This is the raw edge.  NEVER size on it directly -- shrink it first
    (PLAN.md invariant I2, core.math.sizing.position_fraction).
    """
    _check_price(price)
    return p_model - price - fee(price, spec, is_maker=is_maker)


def variance(p: float) -> float:
    """TOTAL remaining settlement variance of a binary.

    Closed form, model-free, and with NO dependence on time to expiry: since
    p_T^2 = p_T for a martingale converging to {0,1}, Var(p_T|F_t) = p(1-p).

    This is why inventory risk in a binary does NOT decay as expiry approaches
    (PLAN.md R2.4b) -- you settle at 0 or 1, never at the mid.
    """
    return p * (1.0 - p)


def sd(p: float) -> float:
    """Per-contract standard deviation: ~0.50 at p=0.5, ten times a typical edge."""
    return math.sqrt(variance(p))


def fee_ratio(price: float, spec: FeeSpec, *, is_maker: bool) -> float:
    """Fee as a fraction of capital at risk.

    NOTE the algebra, because it is easy to get wrong: fee/price = theta*(1-p),
    which is LINEAR AND DECREASING in price, not explosive at the low end.  It
    runs 6.65% at 5c to 3.50% at 50c for a Kalshi taker -- a shallow slope.

    So the fee does not "blow up" on cheap contracts the way the phrase
    'fee death zone' suggests.  What actually makes cheap contracts lethal is
    the favourite-longshot bias (sub-10c buyers lose >60% of stake), not the
    fee ratio.  Keep the two arguments separate.
    """
    return fee(price, spec, is_maker=is_maker) / price


def fee_death_zone_boundary(spec: FeeSpec, *, is_maker: bool,
                            limit: float = 0.04) -> float:
    """Price below which fee/stake exceeds `limit`.  Solves theta*(1-p) = limit.

    Returns 0.0 when the fee can never exceed the limit (e.g. a zero-fee maker
    series, where the ratio is identically 0).
    """
    if spec.venue == "kalshi":
        theta = (KALSHI_BASE_TAKER * spec.fee_multiplier
                 * (KALSHI_MAKER_RATIO[spec.fee_type] if is_maker else 1.0))
    else:
        theta = THETA_FLAT[(spec.venue, is_maker)]
    if theta <= 0.0:
        return 0.0                      # rebates and zero fees never bind
    boundary = 1.0 - limit / theta
    return max(0.0, boundary)


def in_fee_death_zone(price: float, spec: FeeSpec, *, is_maker: bool,
                      limit: float = 0.04) -> bool:
    """PLAN.md R2.1b.  True when the fee exceeds `limit` of the stake.

    With the default 0.04 limit and Kalshi taker fees the boundary is 42.9c --
    i.e. the rule excludes ALL taker entries below mid, which is consistent with
    the maker-first doctrine but far broader than a 'cheap contracts only'
    screen.  For a maker on a zero-fee series it never binds.
    """
    return fee_ratio(price, spec, is_maker=is_maker) > limit


# --------------------------------------------------------------------------- #
# The maker/taker crossover.  research/07 section 9.1.
# --------------------------------------------------------------------------- #
def taker_fee_equals_half_tick(tick: float = 0.01,
                               base: float = KALSHI_BASE_TAKER) -> tuple[float, float]:
    """Prices where the Kalshi taker fee equals half a tick.

    Solving  base*p(1-p) = tick/2  gives p = 0.0774 and p = 0.9226 at the
    default 1c tick.

    BETWEEN those roots the taker fee exceeds the ENTIRE half-spread of a
    1-tick market.  Outside them the flat tick dominates and the fee is nearly
    free -- so the maker/taker policy must be a function of price level, never a
    global constant.
    """
    # base*p - base*p^2 = tick/2  ->  p^2 - p + tick/(2*base) = 0
    c = tick / (2.0 * base)
    disc = 1.0 - 4.0 * c
    if disc < 0:
        raise ValueError("no real crossover for these parameters")
    root = math.sqrt(disc)
    return (1.0 - root) / 2.0, (1.0 + root) / 2.0


def should_post_not_cross(price: float) -> bool:
    """True when the taker fee exceeds a half-tick, i.e. crossing is expensive.

    research/07 section 9.1: at p=0.50 in a 1-tick market crossing costs 2.25c
    while posting is a net CREDIT of 0.06c -- you need 2.31c of edge (4.6% of
    price) before crossing beats a certain fill.
    """
    lo, hi = taker_fee_equals_half_tick()
    return lo < price < hi
