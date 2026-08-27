"""T-004 acceptance: reproduces the PLAN.md section 2.5 sample-size table and
verifies the anytime-valid machinery."""

from __future__ import annotations

import math

import numpy as np
import pytest

from core.math.stats import (
    brier_score,
    brier_skill_vs_market,
    combine_e_values,
    e_bh,
    e_to_p,
    e_value_beta_binomial,
    growth_per_unit_time,
    kl_divergence,
    log_score,
    markets_to_beat_market,
    markets_to_prove_edge,
    sample_size,
    spiegelhalter_z,
    wilson_interval,
)

# PLAN.md section 2.5 -- (price, edge, n at 80% power, n at 95% power)
SAMPLE_TABLE = [
    (0.50, 0.01, 15_454, 27_050),
    (0.50, 0.02, 3_862, 6_758),
    (0.50, 0.03, 1_715, 3_001),
    (0.50, 0.05, 616, 1_077),
    (0.85, 0.02, 1_894, 3_252),
    (0.85, 0.03, 823, 1_398),
    (0.15, 0.03, 921, 1_652),
]


@pytest.mark.parametrize("price,edge,n80,n95", SAMPLE_TABLE)
def test_sample_size_table(price, edge, n80, n95):
    """Within 1% of the canonical table (T-004 acceptance criterion)."""
    assert sample_size(price, edge, power=0.80) == pytest.approx(n80, rel=0.01)
    assert sample_size(price, edge, power=0.95) == pytest.approx(n95, rel=0.01)


def test_favourites_need_fewer_samples_than_coin_flips():
    """Outcome variance p(1-p) shrinks at the extremes -- a second, independent
    reason S1 trades the 70-95c band."""
    assert sample_size(0.85, 0.03) < sample_size(0.50, 0.03)


# research/08 1.1 -- N >= 4/delta^2
BEAT_MARKET = [(0.02, 10_000), (0.03, 4_444), (0.05, 1_600), (0.10, 400)]


@pytest.mark.parametrize("delta,n", BEAT_MARKET)
def test_markets_to_beat_market(delta, n):
    assert markets_to_beat_market(delta) == pytest.approx(n, rel=0.01)


# research/08 section 0 -- N ~ log(1/alpha)/KL
PROVE_EDGE = [(0.52, 3_744), (0.55, 598), (0.60, 149)]


@pytest.mark.parametrize("q,n", PROVE_EDGE)
def test_markets_to_prove_edge(q, n):
    assert markets_to_prove_edge(q, 0.50) == pytest.approx(n, rel=0.01)


def test_kl_is_zero_only_when_you_agree_with_the_market():
    assert kl_divergence(0.5, 0.5) == pytest.approx(0.0)
    assert kl_divergence(0.6, 0.5) > 0.0
    assert kl_divergence(0.4, 0.5) > 0.0


def test_kl_equals_kelly_growth_rate():
    """research/08 identity (A): KL(q||m) IS the Kelly growth rate.

    Betting fraction q of bankroll on YES at price m in a complete market.
    """
    q, m = 0.60, 0.50
    realised = q * math.log(q / m) + (1 - q) * math.log((1 - q) / (1 - m))
    assert kl_divergence(q, m) == pytest.approx(realised)


def test_capital_velocity_ranks_fast_small_edges_over_slow_large_ones():
    """PLAN.md 2.3a: a 2% edge in a week beats a 6% edge in a year."""
    fast = growth_per_unit_time(0.52, 0.50, hours_to_resolution=24 * 7)
    slow = growth_per_unit_time(0.56, 0.50, hours_to_resolution=24 * 365)
    assert fast > slow


def test_wilson_interval_brackets_the_rate():
    lo, hi = wilson_interval(55, 100)
    assert lo < 0.55 < hi
    assert 0.0 <= lo and hi <= 1.0
    # tightens with n
    lo2, hi2 = wilson_interval(550, 1000)
    assert (hi2 - lo2) < (hi - lo)


# --------------------------------------------------------------------------- #
# Anytime-valid inference.
# --------------------------------------------------------------------------- #
def test_e_value_grows_under_a_real_edge():
    """A genuine 60% win rate against a 50% null drives the e-value up."""
    e_small = e_value_beta_binomial(60, 100, 0.5)
    e_big = e_value_beta_binomial(600, 1000, 0.5)
    assert e_big > e_small
    assert e_big > 20.0    # 1/alpha at 5%


def test_e_value_stays_small_under_the_null():
    assert e_value_beta_binomial(500, 1000, 0.5) < 20.0


def test_e_process_controls_type_one_error_under_continuous_monitoring():
    """The property that makes this the right tool (PLAN.md R2.5b).

    Ville: P(exists t : E_t >= 1/alpha) <= alpha, EVEN THOUGH we look after
    every single observation. Naive continuous monitoring reaches 0.36 by 100
    observations and climbs toward 1.0.
    """
    rng = np.random.default_rng(7)
    n_paths, horizon, alpha = 3000, 500, 0.05
    threshold = 1.0 / alpha
    draws = rng.random((n_paths, horizon)) < 0.5     # H0 is TRUE
    successes = np.cumsum(draws, axis=1)

    ever_rejected = np.zeros(n_paths, dtype=bool)
    for t in range(1, horizon + 1):
        logs = np.array([
            e_value_beta_binomial(int(s), t, 0.5) for s in successes[:, t - 1]
        ])
        ever_rejected |= logs >= threshold

    false_positive = ever_rejected.mean()
    assert false_positive <= alpha + 0.02, f"anytime validity broken: {false_positive}"


def test_naive_repeated_testing_blows_past_nominal_alpha():
    """The counterexample the e-process exists to fix."""
    from scipy.stats import norm as _norm

    rng = np.random.default_rng(11)
    n_paths, horizon = 4000, 500
    draws = rng.random((n_paths, horizon)) < 0.5
    cum = np.cumsum(draws, axis=1)
    z_crit = _norm.ppf(0.95)

    ever = np.zeros(n_paths, dtype=bool)
    for t in range(25, horizon + 1, 25):
        phat = cum[:, t - 1] / t
        z = (phat - 0.5) / math.sqrt(0.25 / t)
        ever |= z > z_crit
    # inflated well beyond the nominal 5%
    assert ever.mean() > 0.12


def test_e_to_p_is_a_valid_p_value():
    assert e_to_p(20.0) == pytest.approx(0.05)
    assert e_to_p(0.5) == 1.0


def test_e_values_average_under_arbitrary_dependence():
    """The superpower with no p-value analogue (PLAN.md R2.5c)."""
    assert combine_e_values([2.0, 8.0]) == pytest.approx(5.0)
    assert combine_e_values([2.0, 8.0], independent=True) == pytest.approx(16.0)


def test_e_bh_rejects_the_strong_sleeves_only():
    rejected = e_bh([100.0, 50.0, 1.0, 0.2], alpha=0.05)
    assert 0 in rejected and 1 in rejected
    assert 3 not in rejected


def test_e_bh_rejects_nothing_when_no_evidence():
    assert e_bh([1.0, 0.9, 0.8], alpha=0.05) == []


# --------------------------------------------------------------------------- #
# Scoring.
# --------------------------------------------------------------------------- #
def test_brier_and_log_score_reward_the_truth():
    outcomes = [1, 0, 1, 0]
    good = [0.9, 0.1, 0.8, 0.2]
    bad = [0.2, 0.8, 0.3, 0.7]
    assert brier_score(good, outcomes) < brier_score(bad, outcomes)
    assert log_score(good, outcomes) < log_score(bad, outcomes)


def test_log_score_clips_so_one_blown_market_cannot_dominate():
    assert math.isfinite(log_score([0.0], [1]))


def test_brier_skill_is_positive_only_when_you_beat_the_price():
    outcomes = [1, 1, 0, 0, 1, 0]
    market = [0.5] * 6
    better = [0.8, 0.7, 0.3, 0.2, 0.9, 0.1]
    worse = [0.2, 0.3, 0.7, 0.8, 0.1, 0.9]
    assert brier_skill_vs_market(better, market, outcomes) > 0
    assert brier_skill_vs_market(worse, market, outcomes) < 0


def test_spiegelhalter_z_is_small_when_calibrated():
    rng = np.random.default_rng(3)
    q = rng.beta(2, 2, size=4000)
    y = (rng.random(4000) < q).astype(int)
    z = spiegelhalter_z(list(q), list(y))
    assert abs(z) < 3.0


def test_spiegelhalter_z_detects_overconfidence():
    """Forecasts pushed toward the extremes should register as miscalibrated."""
    rng = np.random.default_rng(5)
    q_true = rng.beta(2, 2, size=6000)
    y = (rng.random(6000) < q_true).astype(int)
    # report probabilities more extreme than they should be
    q_over = np.clip(0.5 + 1.6 * (q_true - 0.5), 0.01, 0.99)
    z = spiegelhalter_z(list(q_over), list(y))
    assert abs(z) > 3.0
