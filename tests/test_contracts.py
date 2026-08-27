"""T-002 acceptance: reproduces the PLAN.md section 2.1 fee table exactly."""

from __future__ import annotations

import math

import pytest

from core.math.contracts import (
    KALSHI_HISTORICALLY_FEE_FREE,
    FeeSpec,
    edge,
    fee,
    fee_death_zone_boundary,
    fee_ratio,
    in_fee_death_zone,
    sd,
    should_post_not_cross,
    taker_fee_equals_half_tick,
    variance,
)

KALSHI_MAKER_SERIES = FeeSpec.kalshi("quadratic_with_maker_fees", 1.0)
KALSHI_PLAIN = FeeSpec.kalshi("quadratic", 1.0)
PM = FeeSpec.polymarket_us()

# PLAN.md section 2.1 -- break-even edge in CENTS, hold-to-settlement, one fee.
# (price, kalshi_taker, kalshi_maker, pmus_taker, pmus_maker)
FEE_TABLE = [
    (0.05, 0.33, 0.08, 0.29, -0.06),
    (0.10, 0.63, 0.16, 0.54, -0.11),
    (0.20, 1.12, 0.28, 0.96, -0.20),
    (0.30, 1.47, 0.37, 1.26, -0.26),
    (0.40, 1.68, 0.42, 1.44, -0.30),
    (0.50, 1.75, 0.44, 1.50, -0.31),
    (0.60, 1.68, 0.42, 1.44, -0.30),
    (0.70, 1.47, 0.37, 1.26, -0.26),
    (0.80, 1.12, 0.28, 0.96, -0.20),
    (0.90, 0.63, 0.16, 0.54, -0.11),
    (0.95, 0.33, 0.08, 0.29, -0.06),
]


@pytest.mark.parametrize("price,k_taker,k_maker,pm_taker,pm_maker", FEE_TABLE)
def test_fee_table_matches_plan(price, k_taker, k_maker, pm_taker, pm_maker):
    """Every cell of the canonical table, to the cent it is quoted at."""
    assert fee(price, KALSHI_MAKER_SERIES, is_maker=False) * 100 == pytest.approx(k_taker, abs=0.005)
    assert fee(price, KALSHI_MAKER_SERIES, is_maker=True) * 100 == pytest.approx(k_maker, abs=0.005)
    assert fee(price, PM, is_maker=False) * 100 == pytest.approx(pm_taker, abs=0.005)
    assert fee(price, PM, is_maker=True) * 100 == pytest.approx(pm_maker, abs=0.005)


def test_kalshi_makers_pay_zero_on_the_default_fee_type():
    """research/06 K2: maker fees apply to only 130 of 13,486 series.

    On the other 99% -- fee_type 'quadratic' -- makers pay NOTHING, not 25%.
    Assuming a flat 0.0175 maker rate overstates cost across almost the whole
    venue.
    """
    for price in (0.05, 0.25, 0.50, 0.75, 0.95):
        assert fee(price, KALSHI_PLAIN, is_maker=True) == 0.0
        assert fee(price, KALSHI_PLAIN, is_maker=False) > 0.0


def test_combo_maker_ratio_is_half_not_a_quarter():
    combo = FeeSpec.kalshi("quadratic_with_combo_maker_fees", 1.0)
    taker = fee(0.5, combo, is_maker=False)
    assert fee(0.5, combo, is_maker=True) == pytest.approx(0.5 * taker)
    assert fee(0.5, KALSHI_MAKER_SERIES, is_maker=True) == pytest.approx(
        0.25 * fee(0.5, KALSHI_MAKER_SERIES, is_maker=False)
    )


def test_zero_multiplier_would_cost_nothing():
    """The MECHANIC works; whether any series currently uses it is a live question.

    research/06 K3 reported 14 fee-free series. Re-checked 2026-08-26: none
    exist today. The constant is retained as historical only -- see
    tests/test_kalshi_client_live.py for what is actually true now.
    """
    free = FeeSpec.kalshi("quadratic", 0.0)
    assert len(KALSHI_HISTORICALLY_FEE_FREE) == 14
    for price in (0.05, 0.50, 0.95):
        assert fee(price, free, is_maker=False) == 0.0
        assert fee(price, free, is_maker=True) == 0.0


def test_mlb_half_multiplier():
    half = FeeSpec.kalshi("quadratic", 0.5)
    assert fee(0.5, half, is_maker=False) == pytest.approx(
        0.5 * fee(0.5, KALSHI_PLAIN, is_maker=False)
    )


def test_polymarket_maker_is_a_rebate_everywhere():
    """PM-US makers are PAID. The fee is negative across the whole price range."""
    for price in [i / 100 for i in range(1, 100)]:
        assert fee(price, PM, is_maker=True) < 0.0


def test_fee_is_symmetric_and_peaks_at_half():
    for price in (0.05, 0.2, 0.35):
        assert fee(price, KALSHI_PLAIN, is_maker=False) == pytest.approx(
            fee(1.0 - price, KALSHI_PLAIN, is_maker=False)
        )
    peak = fee(0.50, KALSHI_PLAIN, is_maker=False)
    for price in (0.4, 0.45, 0.55, 0.6):
        assert fee(price, KALSHI_PLAIN, is_maker=False) < peak


# PLAN.md section 2.1 -- taker fee as % of capital at risk.
DEATH_ZONE = [(0.05, 6.65), (0.10, 6.30), (0.30, 4.90), (0.50, 3.50),
              (0.70, 2.10), (0.90, 0.70), (0.95, 0.35)]


@pytest.mark.parametrize("price,pct", DEATH_ZONE)
def test_fee_ratio_table(price, pct):
    assert fee_ratio(price, KALSHI_PLAIN, is_maker=False) * 100 == pytest.approx(pct, abs=0.01)


def test_fee_ratio_is_linear_and_decreasing_not_explosive():
    """The algebra that an earlier draft of PLAN.md got wrong.

    fee/price = theta*(1-p) -- LINEAR and DECREASING, running 6.65% at 5c to
    3.50% at 50c.  The fee does not blow up on cheap contracts; the
    favourite-longshot bias does.
    """
    ratios = [fee_ratio(p, KALSHI_PLAIN, is_maker=False)
              for p in (0.05, 0.10, 0.30, 0.50, 0.90)]
    assert ratios == sorted(ratios, reverse=True)          # monotone decreasing
    assert ratios[0] / ratios[-1] < 10                     # shallow, not explosive
    for p in (0.05, 0.3, 0.7):
        assert fee_ratio(p, KALSHI_PLAIN, is_maker=False) == pytest.approx(
            0.07 * (1 - p)
        )


def test_fee_death_zone_boundary_is_43c_not_13c():
    """R2.1b with a 0.04 limit excludes below 42.9c for a Kalshi taker.

    An earlier draft claimed 'roughly below 13c' -- wrong arithmetic, caught by
    this test.  1 - 0.04/0.07 = 0.4286.
    """
    boundary = fee_death_zone_boundary(KALSHI_PLAIN, is_maker=False, limit=0.04)
    assert boundary == pytest.approx(0.4286, abs=0.001)
    assert in_fee_death_zone(boundary - 0.01, KALSHI_PLAIN, is_maker=False)
    assert not in_fee_death_zone(boundary + 0.01, KALSHI_PLAIN, is_maker=False)
    # cheap contracts are inside it, mid-and-above are not
    assert in_fee_death_zone(0.05, KALSHI_PLAIN, is_maker=False)
    assert not in_fee_death_zone(0.50, KALSHI_PLAIN, is_maker=False)
    assert not in_fee_death_zone(0.90, KALSHI_PLAIN, is_maker=False)


def test_fee_death_zone_never_binds_for_a_zero_fee_maker():
    """S1 quotes 70-95c as a maker on quadratic series -- the rule is vacuous there."""
    assert fee_death_zone_boundary(KALSHI_PLAIN, is_maker=True) == 0.0
    assert not in_fee_death_zone(0.02, KALSHI_PLAIN, is_maker=True)
    # and Polymarket makers are paid, so it never binds there either
    assert fee_death_zone_boundary(PM, is_maker=True) == 0.0


def test_maker_taker_crossover_roots():
    """research/07 9.1: taker fee equals a half-tick at p = 0.0774 and 0.9226."""
    lo, hi = taker_fee_equals_half_tick()
    assert lo == pytest.approx(0.0774, abs=0.0005)
    assert hi == pytest.approx(0.9226, abs=0.0005)
    # between the roots the fee exceeds the entire half-spread of a 1c market
    assert should_post_not_cross(0.50)
    assert should_post_not_cross(0.20)
    # outside them the flat tick dominates and crossing is comparatively cheap
    assert not should_post_not_cross(0.03)
    assert not should_post_not_cross(0.97)


def test_variance_is_bernoulli_and_time_free():
    """R2.4b: total remaining variance is p(1-p) with NO time term."""
    assert variance(0.5) == pytest.approx(0.25)
    assert sd(0.5) == pytest.approx(0.5)
    assert variance(0.9) == pytest.approx(0.09)
    # symmetric
    assert variance(0.1) == pytest.approx(variance(0.9))


def test_edge_subtracts_the_fee():
    e = edge(0.55, 0.50, KALSHI_PLAIN, is_maker=False)
    assert e == pytest.approx(0.05 - 0.0175)
    # a maker on a zero-maker-fee series keeps the whole gross edge
    assert edge(0.55, 0.50, KALSHI_PLAIN, is_maker=True) == pytest.approx(0.05)


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5, math.nan])
def test_rejects_prices_outside_the_open_interval(bad):
    with pytest.raises(ValueError):
        fee(bad, KALSHI_PLAIN, is_maker=False)
