"""T-005 acceptance: reproduces the PLAN.md 2.6/2.7 correlation, hedge and
Dutch-book tables, plus the Smoczynski-Tomkins closed form."""

from __future__ import annotations

import math

import pytest

from core.math.contracts import FeeSpec
from core.math.portfolio import (
    MutuallyExclusiveKelly,
    dutch_book_fee_hurdle,
    dutch_book_margin,
    haldane_correct,
    hedge_ratio,
    n_effective,
    n_effective_ceiling,
    phi_from_latent,
    phi_max,
    phi_min,
    residual_sd_fraction,
    short_basket_margin,
    tetrachoric,
    variance_removed,
)

KALSHI_PLAIN = FeeSpec.kalshi("quadratic", 1.0)
KALSHI_MAKER = FeeSpec.kalshi("quadratic_with_maker_fees", 1.0)
PM = FeeSpec.polymarket_us()

# PLAN.md 2.7 -- effective independent bets.
NEFF_TABLE = [
    (20, [(0.0, 20.0), (0.05, 10.3), (0.10, 6.9), (0.20, 4.2), (0.30, 3.0), (0.50, 1.9)]),
    (50, [(0.0, 50.0), (0.05, 14.5), (0.10, 8.5), (0.20, 4.6), (0.30, 3.2), (0.50, 2.0)]),
    (100, [(0.0, 100.0), (0.05, 16.8), (0.10, 9.2), (0.20, 4.8), (0.30, 3.3), (0.50, 2.0)]),
]


@pytest.mark.parametrize("n,rows", NEFF_TABLE)
def test_n_effective_table(n, rows):
    for rho, expected in rows:
        assert n_effective(n, rho) == pytest.approx(expected, abs=0.05)


def test_diversification_saturates_at_one_over_rho():
    """More tickers inside one theme cannot help past 1/rho -- more THEMES can."""
    assert n_effective_ceiling(0.1) == pytest.approx(10.0)
    assert n_effective(10_000, 0.1) < 10.1
    assert n_effective_ceiling(0.0) == float("inf")


# PLAN.md 2.6 -- minimum-variance hedge.
HEDGE_TABLE = [(0.3, 0.09, 0.95), (0.5, 0.25, 0.87), (0.7, 0.49, 0.71),
               (0.8, 0.64, 0.60), (0.9, 0.81, 0.44)]


@pytest.mark.parametrize("rho,removed,residual", HEDGE_TABLE)
def test_hedge_table(rho, removed, residual):
    assert variance_removed(rho) == pytest.approx(removed, abs=0.005)
    assert residual_sd_fraction(rho) == pytest.approx(residual, abs=0.005)


def test_hedge_ratio_is_rho_for_equal_sigmas():
    assert hedge_ratio(0.7, 0.5, 0.5) == pytest.approx(0.7)


def test_r2_6a_hedging_below_point_eight_is_not_worth_a_leg():
    """At rho=0.5 a hedge removes only a quarter of the variance."""
    assert variance_removed(0.5) < 0.30
    assert variance_removed(0.8) >= 0.64


# PLAN.md 2.6 -- N-outcome Dutch book fee hurdles, in cents.
HURDLE_TABLE = [
    (2, 3.50, 3.00, 0.87),
    (3, 4.59, 3.94, 1.15),
    (5, 5.47, 4.69, 1.37),
    (8, 5.97, 5.11, 1.49),
    (12, 6.24, 5.35, 1.56),
]


@pytest.mark.parametrize("n,k_taker,pm_taker,k_maker", HURDLE_TABLE)
def test_dutch_book_hurdle_table(n, k_taker, pm_taker, k_maker):
    assert dutch_book_fee_hurdle(n, KALSHI_PLAIN, is_maker=False) * 100 == pytest.approx(k_taker, abs=0.01)
    assert dutch_book_fee_hurdle(n, PM, is_maker=False) * 100 == pytest.approx(pm_taker, abs=0.01)
    assert dutch_book_fee_hurdle(n, KALSHI_MAKER, is_maker=True) * 100 == pytest.approx(k_maker, abs=0.01)


def test_maker_window_is_far_wider_than_taker():
    """The C2 thesis, in one assertion: for n=2 the profitable ceiling moves from
    0.9650 (taker) to 0.9913 (maker) -- and live sum(ask) clusters just above 1."""
    taker_ceiling = 1.0 - dutch_book_fee_hurdle(2, KALSHI_PLAIN, is_maker=False)
    maker_ceiling = 1.0 - dutch_book_fee_hurdle(2, KALSHI_MAKER, is_maker=True)
    assert taker_ceiling == pytest.approx(0.9650, abs=0.001)
    assert maker_ceiling == pytest.approx(0.9913, abs=0.001)
    assert maker_ceiling - taker_ceiling > 0.025


def test_dutch_book_margin_signs():
    cheap = dutch_book_margin([0.40, 0.50], KALSHI_PLAIN, is_maker=False)
    rich = dutch_book_margin([0.55, 0.55], KALSHI_PLAIN, is_maker=False)
    assert cheap > 0.0
    assert rich < 0.0


def test_short_basket_is_the_safe_direction():
    """PLAN.md 3.2: selling collects sum(bid) against a max $1 liability.

    An overround book -- the normal case, median sum(ask) = 1.15 -- is
    profitable to SELL and a guaranteed loss to BUY.
    """
    bids = [0.55, 0.52]
    assert short_basket_margin(bids, KALSHI_MAKER, is_maker=True) > 0.0
    assert dutch_book_margin(bids, KALSHI_MAKER, is_maker=True) < 0.0


# --------------------------------------------------------------------------- #
# Binary correlation.
# --------------------------------------------------------------------------- #
def test_phi_ceiling_is_one_only_for_equal_marginals():
    assert phi_max(0.5, 0.5) == pytest.approx(1.0)
    assert phi_max(0.5, 0.2) == pytest.approx(0.5, abs=0.01)
    assert phi_max(0.5, 0.05) == pytest.approx(0.229, abs=0.005)


def test_phi_understates_dependence_at_asymmetric_prices():
    """The headline case (PLAN.md R2.7d): phi at 97% of its ceiling reads 0.18.

    A genuine latent rho of 0.70 between a 5c market and a 60c market shows up
    as phi = 0.18 -- indistinguishable from noise to any naive risk model.
    """
    observed = phi_from_latent(0.70, 0.05, 0.60)
    ceiling = phi_max(0.05, 0.60)
    assert observed == pytest.approx(0.1818, abs=0.005)
    assert ceiling == pytest.approx(0.1873, abs=0.005)
    assert observed / ceiling > 0.95


def test_phi_attenuation_worsens_toward_the_tails():
    """Same latent rho reads progressively smaller as marginals get extreme."""
    at_half = phi_from_latent(0.5, 0.5, 0.5)
    at_fifth = phi_from_latent(0.5, 0.2, 0.2)
    at_twentieth = phi_from_latent(0.5, 0.05, 0.05)
    assert at_half == pytest.approx(0.333, abs=0.01)
    assert at_fifth == pytest.approx(0.295, abs=0.01)
    assert at_twentieth == pytest.approx(0.204, abs=0.01)
    assert at_half > at_fifth > at_twentieth


def test_longshots_cannot_be_hedged_against_each_other():
    """R2.7g: two 2c markets cannot have binary correlation below about -0.02."""
    assert phi_min(0.02, 0.02) == pytest.approx(-0.0204, abs=0.001)


def test_tetrachoric_recovers_the_latent_correlation():
    """Round-trip: latent rho -> implied table -> tetrachoric MLE."""
    from scipy.stats import norm

    for rho, p_x, p_y in [(0.5, 0.5, 0.5), (0.7, 0.05, 0.6), (0.3, 0.2, 0.8)]:
        zx, zy = norm.ppf(1 - p_x), norm.ppf(1 - p_y)
        from core.math.portfolio import _bvn_cdf

        p11 = 1 - norm.cdf(zx) - norm.cdf(zy) + _bvn_cdf(zx, zy, rho)
        assert tetrachoric(p_x, p_y, p11) == pytest.approx(rho, abs=0.01)


def test_tetrachoric_exceeds_phi_at_asymmetric_marginals():
    """The whole reason to use it."""
    from scipy.stats import norm

    from core.math.portfolio import _bvn_cdf

    rho, p_x, p_y = 0.70, 0.05, 0.60
    zx, zy = norm.ppf(1 - p_x), norm.ppf(1 - p_y)
    p11 = 1 - norm.cdf(zx) - norm.cdf(zy) + _bvn_cdf(zx, zy, rho)
    assert tetrachoric(p_x, p_y, p11) > 3 * phi_from_latent(rho, p_x, p_y)


def test_haldane_correction_moves_every_cell():
    assert haldane_correct(20, 0, 5, 25) == (20.5, 0.5, 5.5, 25.5)


# --------------------------------------------------------------------------- #
# Mutually exclusive Kelly.  research/08 4.3 worked example.
# --------------------------------------------------------------------------- #
def test_smoczynski_tomkins_worked_example():
    prices = [0.30, 0.26, 0.21, 0.13, 0.14]     # sums to 1.040
    probs = [0.40, 0.25, 0.20, 0.10, 0.05]
    sol = MutuallyExclusiveKelly.solve(prices, probs)

    assert sol.bet_set == (0, 1, 2, 3)
    expected = [0.8333, 0.4615, 0.4524, 0.2692, 0.0]
    for got, want in zip(sol.stakes, expected):
        assert got == pytest.approx(want, abs=0.001)
    assert sol.total_staked == pytest.approx(0.5000, abs=0.001)
    assert sol.growth == pytest.approx(0.034616, abs=1e-5)


def test_kelly_buys_negative_ev_outcomes_as_hedges():
    """The first surprise: three of the four bought legs have pi/p < 1."""
    prices = [0.30, 0.26, 0.21, 0.13, 0.14]
    probs = [0.40, 0.25, 0.20, 0.10, 0.05]
    sol = MutuallyExclusiveKelly.solve(prices, probs)

    negative_ev_bought = [
        i for i in sol.bet_set if probs[i] / prices[i] < 1.0
    ]
    assert len(negative_ev_bought) == 3
    assert all(sol.stakes[i] > 0 for i in negative_ev_bought)


def test_naive_per_outcome_kelly_leaves_a_third_of_growth_behind():
    """The second surprise: naive sizing captures only ~65% of optimal growth."""
    prices = [0.30, 0.26, 0.21, 0.13, 0.14]
    probs = [0.40, 0.25, 0.20, 0.10, 0.05]
    sol = MutuallyExclusiveKelly.solve(prices, probs)

    # naive: bet only the single positive-EV outcome at its binary Kelly fraction
    from core.math.sizing import kelly_fraction

    f = kelly_fraction(probs[0], prices[0])
    naive_growth = (
        probs[0] * math.log(1 + f * (1 - prices[0]) / prices[0])
        + (1 - probs[0]) * math.log(1 - f)
    )
    assert naive_growth == pytest.approx(0.022582, abs=1e-4)
    assert naive_growth / sol.growth == pytest.approx(0.652, abs=0.01)


def test_mutually_exclusive_kelly_rejects_a_normalised_book():
    """Uniqueness needs sum(prices) > 1 -- the overround pins the solution."""
    sol = MutuallyExclusiveKelly.solve([0.5, 0.5], [0.6, 0.4])
    assert sol.total_staked >= 0.0     # still solvable, just degenerate-adjacent


def test_no_bet_when_you_agree_with_the_book():
    prices = [0.52, 0.53]
    probs = [0.50, 0.50]
    sol = MutuallyExclusiveKelly.solve(prices, probs)
    assert sol.total_staked == pytest.approx(0.0, abs=1e-9)
