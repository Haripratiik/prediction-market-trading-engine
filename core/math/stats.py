"""Validation statistics.  PLAN.md section 2.5, research/08  [CANONICAL]

Implemented in-house deliberately.  Verified on this machine (Windows 11,
Python 3.13.5, NumPy 2.x): `confseq` cannot pip-install (no Windows wheels above
cp310; `np.float_` removed in NumPy 2), and statsmodels/scipy have ZERO
alpha-spending, always-valid or e-value functionality.  research/08 section 10.

The load-bearing fact: naive continuous monitoring does not converge to any
error rate.  Verified at 40,000 reps, P(ever falsely reject) reaches 0.363 by
100 observations, 0.525 by 1,000, 0.647 by 10,000 and 0.739 by 100,000 --
climbing toward 1.0 by the law of the iterated logarithm.  Hence the e-process.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.special import betaln  # type: ignore[import-untyped]
from scipy.stats import norm  # type: ignore[import-untyped]

# --------------------------------------------------------------------------- #
# Fixed-sample size.  PLAN.md 2.5.
# --------------------------------------------------------------------------- #
def sample_size(price: float, edge: float, *, alpha: float = 0.05,
                power: float = 0.80) -> int:
    """Settlements needed for a one-sided binomial test of H0: win rate = price.

    n = [ (z_alpha*sqrt(c(1-c)) + z_beta*sqrt(p(1-p))) / e ]^2

    Favourites are kinder to statisticians as well as bettors: outcome variance
    p(1-p) shrinks at the extremes, so a 3c edge at 85c confirms in ~823
    settlements versus ~1,715 at 50c.
    """
    if not 0.0 < price < 1.0:
        raise ValueError("price must be in (0,1)")
    if edge <= 0.0:
        raise ValueError("edge must be positive")
    p1 = price + edge
    if not 0.0 < p1 < 1.0:
        raise ValueError("price + edge must be in (0,1)")
    za = norm.ppf(1.0 - alpha)
    zb = norm.ppf(power)
    num = za * math.sqrt(price * (1.0 - price)) + zb * math.sqrt(p1 * (1.0 - p1))
    return math.ceil((num / edge) ** 2)


def markets_to_beat_market(delta: float) -> int:
    """N >= 4 / delta^2 -- settlements to beat the market at t = 2.

    `delta` is your typical disagreement |q - m|.  Derivation (research/08 1.1):
    if you are calibrated, E[d_i] = -delta_i^2 and sd(d_i) <= |delta_i|, so the
    t-statistic is at least delta*sqrt(N).

    A 5-point disagreement needs 1,600 settled markets.  A 2-point one, 10,000.
    """
    if delta <= 0.0:
        raise ValueError("delta must be positive")
    return math.ceil(4.0 / (delta * delta))


def kl_divergence(q: float, m: float) -> float:
    """KL(q || m) for Bernoulli -- simultaneously THREE things (research/08 0):

      (A) your Kelly growth rate,
      (B) your log-score edge over the market,
      (C) the growth rate of the e-process that proves you have edge.

    Your growth rate, your forecasting skill, and your statistical evidence are
    the same number.
    """
    if not 0.0 < q < 1.0 or not 0.0 < m < 1.0:
        raise ValueError("q and m must be in (0,1)")
    return q * math.log(q / m) + (1.0 - q) * math.log((1.0 - q) / (1.0 - m))


def markets_to_prove_edge(q: float, m: float, *, alpha: float = 0.05) -> int:
    """N ~ log(1/alpha) / KL(q||m).  q=0.55 vs m=0.50 needs ~598 settlements."""
    kl = kl_divergence(q, m)
    if kl <= 0.0:
        raise ValueError("no edge: KL is zero")
    return math.ceil(math.log(1.0 / alpha) / kl)


def growth_per_unit_time(q: float, m: float, hours_to_resolution: float) -> float:
    """KL(q||m) / T.  PLAN.md 2.3a -- capital velocity.

    Prediction-market collateral is locked until resolution, so the growth rate
    per unit of TIME is what ranks opportunities.  A 2% edge resolving in a week
    dominates a 6% edge resolving in a year.
    """
    if hours_to_resolution <= 0.0:
        raise ValueError("hours_to_resolution must be positive")
    return kl_divergence(q, m) / hours_to_resolution


# --------------------------------------------------------------------------- #
# Intervals.
# --------------------------------------------------------------------------- #
def wilson_interval(successes: int, n: int,
                    *, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval -- the fixed-n confidence interval for a rate.

    Use the confidence SEQUENCE (below) for anything you monitor continuously.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be in [0, n]")
    z = norm.ppf(1.0 - alpha / 2.0)
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


# --------------------------------------------------------------------------- #
# Anytime-valid inference.  PLAN.md R2.5b.
# --------------------------------------------------------------------------- #
def log_e_beta_binomial(successes: int, n: int, p0: float,
                        *, a: float = 1.0, b: float = 1.0) -> float:
    """log of the growth-rate-optimal e-value for H0: p <= p0.

    Mixes over the unknown alternative with a Beta(a,b) prior.  Four lines, and
    it is the recommended default for binary outcomes.

    By Ville's inequality, P(exists t : E_t >= 1/alpha) <= alpha, so
    "reject when the e-process ever exceeds 1/alpha" is an ANYTIME-VALID
    level-alpha test: optional stopping and optional continuation are free.

    Verified: E[E_t] <= 1 at every horizon; realized false-positive rate 0.041
    against a 0.05 target, versus 0.739-and-climbing for naive monitoring.
    """
    if n < 0 or not 0 <= successes <= n:
        raise ValueError("need 0 <= successes <= n")
    if not 0.0 < p0 < 1.0:
        raise ValueError("p0 must be in (0,1)")
    return (
        betaln(a + successes, b + n - successes)
        - betaln(a, b)
        - successes * math.log(p0)
        - (n - successes) * math.log1p(-p0)
    )


def e_value_beta_binomial(successes: int, n: int, p0: float,
                          *, a: float = 1.0, b: float = 1.0) -> float:
    """The e-value itself.  Reject H0 when this exceeds 1/alpha (20 at alpha=5%)."""
    return math.exp(log_e_beta_binomial(successes, n, p0, a=a, b=b))


def e_to_p(e_value: float) -> float:
    """p = 1/E is a valid p-value (Markov's inequality)."""
    if e_value <= 0.0:
        raise ValueError("e_value must be positive")
    return min(1.0, 1.0 / e_value)


def combine_e_values(e_values: list[float], *, independent: bool = False) -> float:
    """Combine evidence across sleeves.

    Independent:            product
    ARBITRARY dependence:   MEAN -- valid with no correction at all.

    The average being valid under arbitrary dependence has no p-value analogue,
    and it is the single most practically important reason to use e-values in
    trading, where sleeves share market exposure in ways you cannot model.
    """
    if not e_values:
        raise ValueError("need at least one e-value")
    if independent:
        return math.prod(e_values)
    return sum(e_values) / len(e_values)


def e_bh(e_values: list[float], *, alpha: float = 0.05) -> list[int]:
    """e-BH: FDR control across K sleeves under ARBITRARY dependence.

    Sort descending, take k* = max{k : k*e_[k]/K >= 1/alpha}, reject the k*
    largest.  Unlike BH on p-values (which needs PRDS), no correction is needed.

    Returns the indices of the rejected hypotheses, in the original order.
    """
    if not e_values:
        return []
    k_total = len(e_values)
    order = sorted(range(k_total), key=lambda i: -e_values[i])
    k_star = 0
    for k in range(1, k_total + 1):
        if k * e_values[order[k - 1]] / k_total >= 1.0 / alpha:
            k_star = k
    return sorted(order[:k_star])


# --------------------------------------------------------------------------- #
# Scoring.  research/08 section 1.
# --------------------------------------------------------------------------- #
def brier_score(forecasts: list[float], outcomes: list[int]) -> float:
    """Mean squared error of probabilistic forecasts.  Lower is better."""
    if len(forecasts) != len(outcomes):
        raise ValueError("forecasts and outcomes must be the same length")
    if not forecasts:
        raise ValueError("need at least one observation")
    return sum((q - y) ** 2 for q, y in zip(forecasts, outcomes)) / len(forecasts)


def log_score(forecasts: list[float], outcomes: list[int],
              *, clip: float = 1e-6) -> float:
    """Mean negative log-likelihood.  Clip before scoring.

    Log score is unbounded, so a single blown market otherwise dominates a year
    of results.  Clip to your minimum tradeable price (~1c on both venues).
    """
    if len(forecasts) != len(outcomes):
        raise ValueError("forecasts and outcomes must be the same length")
    if not forecasts:
        raise ValueError("need at least one observation")
    total = 0.0
    for q, y in zip(forecasts, outcomes):
        qc = min(max(q, clip), 1.0 - clip)
        total -= math.log(qc) if y else math.log1p(-qc)
    return total / len(forecasts)


def brier_skill_vs_market(model: list[float], market: list[float],
                          outcomes: list[int]) -> float:
    """1 - BS_model/BS_market.  Positive means you beat the price as a forecast.

    PLAN.md KPI #1 and the primary continuous metric: it converges far faster
    than P&L.  A sleeve whose model Brier is worse than the market price's Brier
    has negative expected edge regardless of its P&L to date.
    """
    bs_model = brier_score(model, outcomes)
    bs_market = brier_score(market, outcomes)
    if bs_market == 0.0:
        raise ValueError("market Brier is zero; skill score undefined")
    return 1.0 - bs_model / bs_market


def spiegelhalter_z(forecasts: list[float], outcomes: list[int]) -> float:
    """Bin-free calibration test.  ~N(0,1) under calibration.

    Z = sum (y-q)(1-2q) / sqrt( sum (1-2q)^2 q(1-q) )

    Preferred over ECE, which is BROKEN: a perfectly calibrated forecaster shows
    ECE 0.10 at n=100 and 0.047 at n=500 purely from binning noise.  Never
    report ECE (research/08 1.2).
    """
    if len(forecasts) != len(outcomes):
        raise ValueError("forecasts and outcomes must be the same length")
    num = sum((y - q) * (1.0 - 2.0 * q) for q, y in zip(forecasts, outcomes))
    den = math.sqrt(
        sum((1.0 - 2.0 * q) ** 2 * q * (1.0 - q) for q in forecasts)
    )
    if den == 0.0:
        raise ValueError("degenerate forecasts (all exactly 0.5)")
    return num / den


@dataclass(frozen=True, slots=True)
class PeekingPenalty:
    """Verified type-I error of naive continuous monitoring, by horizon.

    40,000 reps, Bernoulli(0.5), two-sided nominal 5%.  It does NOT plateau --
    by the law of the iterated logarithm it climbs toward 1.0.
    """

    TABLE: tuple[tuple[int, float], ...] = (
        (10, 0.1583),
        (100, 0.3630),
        (1_000, 0.5250),
        (10_000, 0.6472),
        (100_000, 0.7389),
    )
