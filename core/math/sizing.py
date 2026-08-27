"""Kelly sizing under estimation error.  PLAN.md section 2.2--2.4  [CANONICAL]

The governing facts, all verified by simulation (research/quant/quant_research.py
and research/08 section 4.1):

    g(c * f*) / g*  =  2c - c^2          exact in the small-edge limit

  * Half Kelly gives 75% of the growth rate at half the log-wealth volatility.
  * DOUBLE KELLY GIVES EXACTLY ZERO GROWTH.  Beyond that, negative -- ruin.
  * The curve is a downward parabola: underbetting is second-order cheap,
    overbetting is catastrophic.

That last point is the whole case for shrinking your edge before sizing.  If your
true edge is half your estimate, then full-Kelly-on-your-estimate is
double-Kelly-on-truth, which earns NOTHING.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.math.contracts import FeeSpec, edge

# PLAN.md canonical defaults (section 2.2, appendix A).
LAMBDA_DEFAULT: float = 0.5      # shrinkage; see caveat below
KELLY_MULTIPLE: float = 0.25     # quarter Kelly on the shrunk edge
POSITION_CAP: float = 0.02       # 2% of bankroll; 1% during Gate 4


def kelly_fraction(p: float, price: float) -> float:
    """Full-Kelly fraction of bankroll for a binary at `price` with true prob `p`.

    f* = (p - price) / (1 - price).  Returns 0 for non-positive edge.
    """
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0,1), got {price!r}")
    if p <= price:
        return 0.0
    return (p - price) / (1.0 - price)


def growth(f: float, price: float, p: float) -> float:
    """Expected log growth per bet at stake fraction `f`.

    g(f) = p*ln(1 + f*(1-price)/price) + (1-p)*ln(1-f)
    """
    if f <= 0.0:
        return 0.0
    if f >= 1.0:
        return float("-inf")
    up = 1.0 + f * (1.0 - price) / price
    return p * math.log(up) + (1.0 - p) * math.log(1.0 - f)


def growth_ratio(c: float) -> float:
    """Fraction of maximum growth retained at `c` times full Kelly: 2c - c^2.

    Small-edge limit.  c=0.5 -> 0.75, c=1 -> 1.0, c=2 -> 0.0.
    """
    return 2.0 * c - c * c


def position_fraction(
    p_model: float,
    price: float,
    spec: FeeSpec,
    *,
    is_maker: bool,
    lam: float = LAMBDA_DEFAULT,
    kelly_mult: float = KELLY_MULTIPLE,
    cap: float = POSITION_CAP,
) -> float:
    """THE sizing function.  Every sleeve calls this.  Never bypass it.

    PLAN.md invariant I2: sizing NEVER uses a raw model edge.  The edge is
    shrunk by `lam` first, and `lam` should come from the fitted per-category
    hierarchical edge model (PLAN.md 2.3b), not from the 0.5 default.
    """
    raw = edge(p_model, price, spec, is_maker=is_maker)
    if raw <= 0.0:
        return 0.0
    p_shrunk = price + lam * raw          # I2: shrink BEFORE sizing
    f_star = kelly_fraction(p_shrunk, price)
    return max(0.0, min(kelly_mult * f_star, cap))


# --------------------------------------------------------------------------- #
# Drawdown.  PLAN.md 2.4.
# --------------------------------------------------------------------------- #
def prob_ever_drawdown(x: float, kelly_multiple: float) -> float:
    """P(bankroll ever touches x * B0) ~= x^(2/m - 1) at Kelly multiple m.

    Continuous approximation (Thorp 2006); Monte Carlo agrees within ~3 points
    at a 2,000-bet horizon.  Full Kelly halves your bankroll 50% of the time;
    quarter Kelly, 0.8%.
    """
    if not 0.0 < x < 1.0:
        raise ValueError(f"x must be in (0,1), got {x!r}")
    if kelly_multiple <= 0.0:
        raise ValueError("kelly_multiple must be positive")
    return x ** (2.0 / kelly_multiple - 1.0)


# --------------------------------------------------------------------------- #
# Shrinkage.  PLAN.md 2.3 / 2.3b.
# --------------------------------------------------------------------------- #
def shrinkage_factor(sigma_edge: float, sigma_noise: float) -> float:
    """lambda = sigma_e^2 / (sigma_e^2 + sigma_n^2).

    Regression to the mean: your best guess of the true edge given an estimate.
    Equal dispersion and noise gives exactly 0.5 -- which is where the default
    comes from, and why it is a theorem rather than humility.

    NOTE (PLAN.md 2.3): the popular argument "parameter uncertainty implies
    shrinkage" is WRONG for log utility -- E[log W] is linear in the outcome
    probability, so a correct posterior uses the unshrunk posterior MEAN.  The
    real mechanisms are SELECTION (you trade where your model disagrees most,
    which is where it is most likely wrong) and INDUCED CORRELATION from a
    shared edge parameter.  This function models the first.
    """
    if sigma_edge < 0 or sigma_noise < 0:
        raise ValueError("dispersions must be non-negative")
    denom = sigma_edge ** 2 + sigma_noise ** 2
    if denom == 0.0:
        return 0.0
    return sigma_edge ** 2 / denom


@dataclass(frozen=True, slots=True)
class EmpiricalBayesEdge:
    """James-Stein shrinkage of per-category edge multipliers.  PLAN.md 2.3b.

    `beta_c` is the coefficient of the forecast-encompassing regression

        logit(P(y=1)) = logit(m) + beta_c * (logit(q) - logit(m)) + alpha_c

    beta_c = 0 -> your disagreement with the market is pure noise (no edge).
    beta_c = 1 -> your forecast is right and the market is wrong.

    This IS the lambda of I2, estimated per category instead of guessed.
    """

    grand_mean: float
    tau2: float
    betas: tuple[float, ...]
    weights: tuple[float, ...]

    @classmethod
    def fit(cls, beta_hat: list[float], se: list[float],
            *, eps: float = 1e-9) -> "EmpiricalBayesEdge":
        if len(beta_hat) != len(se):
            raise ValueError("beta_hat and se must be the same length")
        if not beta_hat:
            raise ValueError("need at least one category")
        if any(s <= 0 for s in se):
            raise ValueError("standard errors must be positive")

        prec = [1.0 / (s * s) for s in se]
        mu0 = sum(b * w for b, w in zip(beta_hat, prec)) / sum(prec)
        tau2 = max(
            sum((b - mu0) ** 2 - s * s for b, s in zip(beta_hat, se)) / len(beta_hat),
            eps,
        )
        weights = tuple(tau2 / (tau2 + s * s) for s in se)
        betas = tuple(w * b + (1.0 - w) * mu0 for w, b in zip(weights, beta_hat))
        return cls(grand_mean=mu0, tau2=tau2, betas=betas, weights=weights)
