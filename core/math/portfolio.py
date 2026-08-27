"""Portfolio construction for correlated binaries.  PLAN.md 2.6--2.7  [CANONICAL]

Two facts drive everything here:

1.  PHI SYSTEMATICALLY UNDERSTATES DEPENDENCE.  At p_X=0.05, p_Y=0.60 with a
    genuine latent rho of 0.70, phi = 0.1818 while its structural ceiling is
    0.1873 -- phi sits at 97% of maximum while reading as "basically
    independent".  Any risk model built on a phi matrix understates
    concentration risk.  Use tetrachoric.

2.  CORRELATION BITES FAR HARDER THAN IT LOOKS.  A latent rho of 0.4 -- an
    observed phi of only 0.26 -- cuts per-market Kelly by 44% and growth to 41%
    of the independent case.  Combined with (1), this is the single largest
    sizing error available in this asset class.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq  # type: ignore[import-untyped]
from scipy.stats import norm  # type: ignore[import-untyped]

from core.math.contracts import FeeSpec, fee

# --------------------------------------------------------------------------- #
# Diversification.  PLAN.md 2.7.
# --------------------------------------------------------------------------- #
def n_effective(n: int, rho: float) -> float:
    """Effective independent bets: N / (1 + (N-1)*rho).

    Diversification SATURATES at 1/rho: at rho = 0.1 you can never exceed 10
    effective bets no matter how many tickers you hold.  More tickers inside one
    theme does not help; more THEMES does.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    if not -1.0 < rho <= 1.0:
        raise ValueError("rho must be in (-1,1]")
    denom = 1.0 + (n - 1) * rho
    if denom <= 0.0:
        return float(n)
    return n / denom


def n_effective_ceiling(rho: float) -> float:
    """lim N->inf of n_effective = 1/rho."""
    if rho <= 0.0:
        return float("inf")
    return 1.0 / rho


# --------------------------------------------------------------------------- #
# Hedging.  PLAN.md 2.6.
# --------------------------------------------------------------------------- #
def hedge_ratio(rho: float, sigma_a: float, sigma_b: float) -> float:
    """Minimum-variance hedge ratio h* = rho * sigma_A / sigma_B."""
    if sigma_b <= 0.0:
        raise ValueError("sigma_b must be positive")
    return rho * sigma_a / sigma_b


def variance_removed(rho: float) -> float:
    """Fraction of variance a minimum-variance hedge removes: rho^2.

    PLAN.md R2.6a: DO NOT HEDGE BELOW rho = 0.8.  At rho = 0.5 a hedge removes
    only 25% of variance while costing a full extra leg of fees and spread.
    Diversification across uncorrelated themes is cheaper.
    """
    return rho * rho


def residual_sd_fraction(rho: float) -> float:
    """Residual sd as a fraction of unhedged: sqrt(1 - rho^2)."""
    return math.sqrt(max(0.0, 1.0 - rho * rho))


# --------------------------------------------------------------------------- #
# Binary correlation.  research/08 section 3.
# --------------------------------------------------------------------------- #
def phi_max(p_x: float, p_y: float) -> float:
    """Prentice upper bound on the phi coefficient given the marginals.

    |phi| = 1 is attainable ONLY when p_X == p_Y.  This is why phi reads low for
    markets at different prices even when they are strongly dependent.
    """
    for p in (p_x, p_y):
        if not 0.0 < p < 1.0:
            raise ValueError("marginals must be in (0,1)")
    qx, qy = 1.0 - p_x, 1.0 - p_y
    return min(math.sqrt(p_x * qy / (qx * p_y)), math.sqrt(p_y * qx / (qy * p_x)))


def phi_min(p_x: float, p_y: float) -> float:
    """Prentice lower bound.

    The negative side is brutal: two markets each at p = 0.02 cannot have binary
    correlation below about -0.0204, in ANY distribution.  You cannot hedge
    longshots against each other -- diversification across themes is the only
    variance reduction available in the tails.
    """
    for p in (p_x, p_y):
        if not 0.0 < p < 1.0:
            raise ValueError("marginals must be in (0,1)")
    qx, qy = 1.0 - p_x, 1.0 - p_y
    return max(-math.sqrt(p_x * p_y / (qx * qy)), -math.sqrt(qx * qy / (p_x * p_y)))


def _bvn_cdf(h: float, k: float, r: float) -> float:
    """P(Z1 <= h, Z2 <= k) for a standard bivariate normal with correlation r.

    64-node Gauss-Legendre on the 1-D representation.  ~16us and more accurate
    than scipy's multivariate_normal.cdf, which matters because this sits inside
    a root-find (research/08 3.1).
    """
    if r == 0.0:
        return float(norm.cdf(h) * norm.cdf(k))
    r = float(np.clip(r, -0.999999999, 0.999999999))
    x, w = np.polynomial.legendre.leggauss(64)
    t = 0.5 * r * (x + 1.0)
    weights = 0.5 * r * w
    one = 1.0 - t * t
    integ = np.exp(-(h * h - 2.0 * t * h * k + k * k) / (2.0 * one)) / np.sqrt(one)
    return float(norm.cdf(h) * norm.cdf(k) + np.sum(weights * integ) / (2.0 * math.pi))


def phi_from_latent(rho: float, p_x: float, p_y: float) -> float:
    """Observed phi implied by a latent-normal correlation rho.

    Shows the attenuation directly: at p=0.05 a latent rho of 0.5 shows up as
    phi = 0.204.
    """
    zx, zy = norm.ppf(1.0 - p_x), norm.ppf(1.0 - p_y)
    p11 = 1.0 - norm.cdf(zx) - norm.cdf(zy) + _bvn_cdf(zx, zy, rho)
    qx, qy = 1.0 - p_x, 1.0 - p_y
    return (p11 - p_x * p_y) / math.sqrt(p_x * qx * p_y * qy)


def tetrachoric(p_x: float, p_y: float, p_11: float) -> float:
    """Latent-normal correlation implied by a 2x2 table.  Olsson's MLE.

    Use this instead of phi.  With 100 settled events the pairwise SE is ~0.14,
    so you cannot distinguish rho=0.3 from rho=0.5 -- shrink and cluster rather
    than trusting raw pairwise estimates (PLAN.md R2.7e).
    """
    for p in (p_x, p_y):
        if not 0.0 < p < 1.0:
            raise ValueError("marginals must be in (0,1)")
    h, k = norm.ppf(1.0 - p_x), norm.ppf(1.0 - p_y)

    def f(r: float) -> float:
        return 1.0 - norm.cdf(h) - norm.cdf(k) + _bvn_cdf(h, k, r) - p_11

    lo, hi = -0.999999, 0.999999
    if f(lo) * f(hi) > 0:      # boundary case (a zero cell); clamp
        return lo if f(lo) > 0 else hi
    return float(brentq(f, lo, hi, xtol=1e-10))


def haldane_correct(n11: int, n10: int, n01: int, n00: int) -> tuple[float, ...]:
    """Add 0.5 to every cell.  A single zero cell otherwise drives the MLE to +-1.

    Verified: table (20,0,5,25) gives +0.9990 uncorrected, 0.9665 with Haldane.
    """
    return (n11 + 0.5, n10 + 0.5, n01 + 0.5, n00 + 0.5)


# --------------------------------------------------------------------------- #
# Dutch books.  PLAN.md 2.6, and the S2 sleeve.
# --------------------------------------------------------------------------- #
def dutch_book_fee_hurdle(n_outcomes: int, spec: FeeSpec, *, is_maker: bool,
                          book_total: float = 0.97) -> float:
    """Total fees to trade all N legs of a near-complete book.

    A 5-outcome Kalshi Dutch book needs sum(px) < 0.9453 as taker but < 0.9863
    as maker -- the maker window is ~4 points wider, which is the difference
    between "almost never" and "regularly" given that live sum(ask) clusters
    just above 1.00 (measured p25 = 1.040).
    """
    if n_outcomes < 2:
        raise ValueError("need at least 2 outcomes")
    p = book_total / n_outcomes
    return n_outcomes * fee(p, spec, is_maker=is_maker)


def dutch_book_margin(prices: list[float], spec: FeeSpec, *,
                      is_maker: bool) -> float:
    """Locked profit per basket from BUYING every outcome: 1 - sum(px) - fees.

    WARNING (PLAN.md 3.2 / research/05 F1): this is only a real arbitrage if the
    outcome set is EXHAUSTIVE.  Kalshi's `mutually_exclusive` flag guarantees at
    most one YES and says NOTHING about at least one.  33 live events price
    below sum(bid) = 0.90 with no Other/None leg -- buying every leg returns $0
    whenever the winner is unlisted.  Gate on exhaustiveness separately.
    """
    if len(prices) < 2:
        raise ValueError("need at least 2 legs")
    fees = sum(fee(p, spec, is_maker=is_maker) for p in prices)
    return 1.0 - sum(prices) - fees


def short_basket_margin(bids: list[float], spec: FeeSpec, *,
                        is_maker: bool) -> float:
    """Locked profit per basket from SELLING every outcome: sum(bid) - 1 - fees.

    THIS IS THE STRUCTURALLY SAFE DIRECTION (PLAN.md 3.2).  Max liability is $1
    regardless of exhaustiveness, because at most one leg can resolve YES --
    and non-exhaustiveness makes it BETTER, since an unlisted winner leaves
    every leg worthless and you keep the whole premium.
    """
    if len(bids) < 2:
        raise ValueError("need at least 2 legs")
    fees = sum(fee(b, spec, is_maker=is_maker) for b in bids)
    return sum(bids) - 1.0 - fees


# --------------------------------------------------------------------------- #
# Kelly across mutually exclusive outcomes.  research/08 section 4.3.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MutuallyExclusiveKelly:
    """Smoczynski-Tomkins closed-form Kelly for a mutually exclusive outcome set.

    Two verified surprises:

      1. Optimal Kelly BUYS OUTCOMES WITH NEGATIVE EXPECTED VALUE.  They are
         hedges: they raise wealth in states where the main bet loses, and log
         utility values that more than their EV cost.
      2. The naive per-outcome approach captures only 65.2% of optimal growth,
         staking 14% of bankroll where the optimum stakes 50%.

    Uniqueness requires sum(prices) > 1 -- the overround is what pins the
    solution down, which is exactly the regime Kalshi is in (median 1.15).
    """

    stakes: tuple[float, ...]      # contracts per $1 payout, per outcome
    bet_set: tuple[int, ...]       # indices actually bought
    total_staked: float            # fraction of bankroll at risk
    growth: float                  # expected log growth

    @classmethod
    def solve(cls, prices: list[float], probs: list[float],
              *, wealth: float = 1.0) -> "MutuallyExclusiveKelly":
        if len(prices) != len(probs):
            raise ValueError("prices and probs must be the same length")
        if len(prices) < 2:
            raise ValueError("need at least 2 outcomes")
        if any(not 0.0 < p < 1.0 for p in prices):
            raise ValueError("prices must be in (0,1)")
        if abs(sum(probs) - 1.0) > 1e-6:
            raise ValueError("probs must sum to 1")

        n = len(prices)
        order = sorted(range(n), key=lambda i: -probs[i] / prices[i])

        chosen: list[int] = []
        sum_pi = 0.0
        sum_p = 0.0
        for i in order:
            reservation = (1.0 - sum_pi) / (1.0 - sum_p)
            if probs[i] / prices[i] <= reservation:
                break
            chosen.append(i)
            sum_pi += probs[i]
            sum_p += prices[i]

        stakes = [0.0] * n
        if chosen:
            leftover = 1.0 - sum_pi
            denom = 1.0 - sum_p
            reservation = leftover / denom if denom > 0 else 0.0
            for i in chosen:
                stakes[i] = wealth * (probs[i] / prices[i] - reservation)

        staked = sum(prices[i] * stakes[i] for i in range(n))
        growth = sum(
            probs[i] * math.log(wealth + stakes[i] - staked)
            for i in range(n)
            if wealth + stakes[i] - staked > 0
        )
        return cls(
            stakes=tuple(stakes),
            bet_set=tuple(sorted(chosen)),
            total_staked=staked,
            growth=growth,
        )
