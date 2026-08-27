"""T-003 acceptance: reproduces the PLAN.md section 2.2 growth table and 2.4 drawdowns."""

from __future__ import annotations

import pytest

from core.math.contracts import FeeSpec
from core.math.sizing import (
    EmpiricalBayesEdge,
    growth,
    growth_ratio,
    kelly_fraction,
    position_fraction,
    prob_ever_drawdown,
    shrinkage_factor,
)

KALSHI_PLAIN = FeeSpec.kalshi("quadratic", 1.0)

# Reference bet from PLAN.md 2.2: price 50c, estimated edge 5c -> f* = 10%.
PRICE, P_EST, P_HALF = 0.50, 0.55, 0.525
F_STAR = 0.10

# (kelly_multiple, growth in bp if true edge = 5c, growth in bp if true edge = 2.5c)
GROWTH_TABLE = [
    (0.25, 21.9, 9.4),
    (0.50, 37.5, 12.5),
    (1.00, 50.1, -0.1),
    (1.50, 37.4, -38.2),
]


def test_reference_kelly_is_ten_percent():
    assert kelly_fraction(P_EST, PRICE) == pytest.approx(F_STAR)


@pytest.mark.parametrize("mult,bp_true,bp_half", GROWTH_TABLE)
def test_growth_table_matches_plan(mult, bp_true, bp_half):
    """Both columns of the canonical growth table, in basis points per bet."""
    g_true = growth(mult * F_STAR, PRICE, P_EST) * 1e4
    g_half = growth(mult * F_STAR, PRICE, P_HALF) * 1e4
    assert g_true == pytest.approx(bp_true, abs=0.15)
    assert g_half == pytest.approx(bp_half, abs=0.15)


def test_double_kelly_gives_zero_growth():
    """The single most important number in sizing: growth vanishes at 2x Kelly."""
    assert growth(2.0 * F_STAR, PRICE, P_EST) * 1e4 == pytest.approx(-1.4, abs=0.2)
    # and beyond it, ruin
    assert growth(2.4 * F_STAR, PRICE, P_EST) < 0.0


def test_growth_peaks_at_full_kelly():
    best = max(
        (growth(m * F_STAR, PRICE, P_EST), m)
        for m in [i / 100 for i in range(1, 200)]
    )
    assert best[1] == pytest.approx(1.0, abs=0.02)


def test_half_kelly_gives_three_quarters_of_growth():
    """g(c f*)/g* = 2c - c^2, the exact small-edge form."""
    assert growth_ratio(0.5) == pytest.approx(0.75)
    assert growth_ratio(1.0) == pytest.approx(1.0)
    assert growth_ratio(2.0) == pytest.approx(0.0)
    # and the exact computation agrees closely at a realistic edge
    ratio = growth(0.5 * F_STAR, PRICE, P_EST) / growth(F_STAR, PRICE, P_EST)
    assert ratio == pytest.approx(0.75, abs=0.01)


def test_quarter_of_estimate_is_half_kelly_on_a_halved_truth():
    """PLAN.md 2.2: the reason the doctrine is quarter Kelly on a shrunk edge.

    If the true edge is half the estimate, quarter-of-estimate lands on true
    half Kelly -- 75% of available growth at half the volatility.
    """
    true_f_star = kelly_fraction(P_HALF, PRICE)
    quarter_of_estimate = 0.25 * F_STAR
    assert quarter_of_estimate == pytest.approx(0.5 * true_f_star, rel=0.01)


# PLAN.md 2.4 -- analytic hitting probabilities.
DRAWDOWN_TABLE = [
    (1.00, 0.50, 0.10),    # full Kelly:    P(-50%) 50%,   P(-90%) 10%
    (0.50, 0.125, 0.001),  # half Kelly:    12.5%,         0.1%
    (0.25, 0.0078, 0.0),   # quarter Kelly: 0.8%,          ~0
]


@pytest.mark.parametrize("mult,p_half,p_ninety", DRAWDOWN_TABLE)
def test_drawdown_table(mult, p_half, p_ninety):
    assert prob_ever_drawdown(0.5, mult) == pytest.approx(p_half, abs=0.001)
    assert prob_ever_drawdown(0.1, mult) == pytest.approx(p_ninety, abs=0.001)


def test_position_fraction_respects_the_cap():
    """I2 + the 2% cap. A large edge is truncated, never scaled through."""
    f = position_fraction(0.95, 0.50, KALSHI_PLAIN, is_maker=True, cap=0.02)
    assert f == pytest.approx(0.02)


def test_position_fraction_is_zero_without_edge():
    assert position_fraction(0.50, 0.50, KALSHI_PLAIN, is_maker=False) == 0.0
    assert position_fraction(0.45, 0.50, KALSHI_PLAIN, is_maker=False) == 0.0
    # a gross edge smaller than the taker fee is still no edge
    assert position_fraction(0.505, 0.50, KALSHI_PLAIN, is_maker=False) == 0.0


def test_position_fraction_is_monotone_in_edge():
    fs = [
        position_fraction(p, 0.50, KALSHI_PLAIN, is_maker=True, cap=1.0)
        for p in (0.52, 0.55, 0.60, 0.70)
    ]
    assert fs == sorted(fs)
    assert all(f > 0 for f in fs)


def test_shrinking_reduces_size():
    """Sizing on the raw edge is strictly larger -- which is the mistake I2 bans."""
    shrunk = position_fraction(0.60, 0.50, KALSHI_PLAIN, is_maker=True, lam=0.5, cap=1.0)
    raw = position_fraction(0.60, 0.50, KALSHI_PLAIN, is_maker=True, lam=1.0, cap=1.0)
    assert shrunk < raw


def test_flagship_sleeve_stake_is_far_inside_quarter_kelly():
    """PLAN.md 2.8: buy at 85c with true p=88%; a 2% stake is ~0.11x Kelly."""
    spec = FeeSpec.kalshi("quadratic_with_maker_fees", 1.0)
    from core.math.contracts import edge as _edge

    net = _edge(0.88, 0.85, spec, is_maker=True)
    assert net * 100 == pytest.approx(2.78, abs=0.01)
    full = net / (1.0 - 0.85)
    assert full == pytest.approx(0.185, abs=0.001)
    assert 0.02 / full == pytest.approx(0.11, abs=0.005)


def test_shrinkage_factor_is_one_half_at_equal_dispersion():
    assert shrinkage_factor(0.03, 0.03) == pytest.approx(0.5)
    assert shrinkage_factor(0.03, 0.02) == pytest.approx(0.692, abs=0.001)
    assert shrinkage_factor(0.02, 0.04) == pytest.approx(0.20, abs=0.001)


def test_empirical_bayes_shrinks_noisy_categories_harder():
    """PLAN.md 2.3b: partial pooling pulls imprecise categories toward the mean."""
    eb = EmpiricalBayesEdge.fit(
        beta_hat=[0.56, 0.08, -0.18, 0.87],
        se=[0.16, 0.34, 0.35, 0.37],
    )
    # the precise category keeps more of its own estimate than the noisy ones
    assert eb.weights[0] > eb.weights[1]
    assert eb.weights[0] > eb.weights[3]
    # every shrunk estimate lies between its raw value and the grand mean
    for raw, shrunk in zip([0.56, 0.08, -0.18, 0.87], eb.betas):
        assert min(raw, eb.grand_mean) - 1e-9 <= shrunk <= max(raw, eb.grand_mean) + 1e-9
    # the wildly negative category is pulled back toward the pool
    assert eb.betas[2] > -0.18
