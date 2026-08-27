"""Weather research harness acceptance.  research/13.

Most of this file exists to keep a NEGATIVE result from quietly turning into a
trading strategy.  The measured finding is that the market's implied forecast
beats the best free forecast we can build, so the expensive failure mode here is
not a wrong number -- it is someone deleting the `quotes = ()` and shipping a
sleeve against an edge nobody demonstrated.

The rest pins the two unit traps that each manufactured large fake profits
during the research: the midnight-LST climate day, and METAR degrees Celsius.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest

from core.math.contracts import FeeSpec
from core.math.stats import markets_to_beat_market
from core.models import Market
from strategy.base import DesiredState, MarketSnapshot, Sleeve
from strategy.weather import (
    EDGE_DEMONSTRATED,
    FORECAST_ERROR_SD_F,
    MARKET_IMPLIED_SD_F,
    MEASURED_DISAGREEMENT_PP,
    MEDIAN_TRADEABLE_SPREAD_CENTS,
    SETTLEMENT_STATION,
    SURPRISING_STATIONS,
    Bucket,
    ForecastPoint,
    TempPartition,
    WeatherHarness,
    bucket_probabilities,
    climate_day_window_us,
    degrees_for_bucket_move,
    dominated_buckets,
    fit_implied_gaussian,
    forecast_error_sd,
    implied_distribution,
    lead_hours_to_event,
    market_implied_sd,
    normal_cdf,
    parse_bucket,
    partition_from_markets,
    required_taker_edge,
    settlement_bounds_from_metar_c,
)

NOW = 1_756_000_000_000_000
HOUR = 3_600_000_000

QUADRATIC = FeeSpec.kalshi("quadratic", 1.0)


# --------------------------------------------------------------------------- #
# Fixtures -- built here, never loaded from data/pm.db (a live recorder owns it).
# --------------------------------------------------------------------------- #
def leg(ticker: str, title: str, bid: int, ask: int, *,
        event: str = "KXHIGHTSFO-26AUG27", series: str = "KXHIGHTSFO",
        close_us: int = NOW + 30 * HOUR) -> Market:
    return Market(
        ticker=ticker, event_ticker=event, series_ticker=series, title=title,
        yes_bid=bid, yes_ask=ask, yes_bid_size=500.0, yes_ask_size=500.0,
        close_at_us=close_us,
    )


def sfo_legs(quotes: list[tuple[int, int]] | None = None) -> list[Market]:
    """The real KXHIGHTSFO-26AUG27 geometry and top-of-book, from our snapshots."""
    q = quotes or [(14, 15), (19, 21), (33, 34), (24, 25), (11, 12), (3, 4)]
    spec = [
        ("KXHIGHTSFO-26AUG27-T71", "Will the maximum temperature be <71 on Aug 27, 2026?"),
        ("KXHIGHTSFO-26AUG27-B71.5", "Will the maximum temperature be 71-72 on Aug 27, 2026?"),
        ("KXHIGHTSFO-26AUG27-B73.5", "Will the maximum temperature be 73-74 on Aug 27, 2026?"),
        ("KXHIGHTSFO-26AUG27-B75.5", "Will the maximum temperature be 75-76 on Aug 27, 2026?"),
        ("KXHIGHTSFO-26AUG27-B77.5", "Will the maximum temperature be 77-78 on Aug 27, 2026?"),
        ("KXHIGHTSFO-26AUG27-T78", "Will the maximum temperature be >78 on Aug 27, 2026?"),
    ]
    return [leg(t, ti, b, a) for (t, ti), (b, a) in zip(spec, q)]


def snapshot(markets: list[Market], now_us: int = NOW) -> MarketSnapshot:
    return MarketSnapshot(now_us=now_us, markets=tuple(markets))


# --------------------------------------------------------------------------- #
# The guard rail.  If any of these fail, an unproven edge is being traded.
# --------------------------------------------------------------------------- #
def test_the_harness_emits_no_quotes_because_no_weather_edge_was_demonstrated() -> None:
    """The whole point.  research/13 section 4 found the market's implied forecast
    BEATS the best free forecast at matched lead, so any resting order here is a
    bet on an edge we looked for and did not find."""
    h = WeatherHarness(forecasts={
        "KXHIGHTSFO-26AUG27": ForecastPoint("KXHIGHTSFO-26AUG27", 74.0)
    })
    state = h.desired_state(snapshot(sfo_legs()))
    assert state.quotes == ()
    assert isinstance(state, DesiredState)


def test_no_quotes_even_when_the_forecast_screams_that_a_bucket_is_mispriced() -> None:
    """A 6 F disagreement is the loudest signal this model can produce -- it moves
    the modal bucket by tens of points.  It must still buy nothing: the size of a
    disagreement is not evidence about its direction, and section 3 measured our
    forecast to be the less accurate of the two."""
    h = WeatherHarness(forecasts={
        "KXHIGHTSFO-26AUG27": ForecastPoint("KXHIGHTSFO-26AUG27", 68.0)
    })
    state = h.desired_state(snapshot(sfo_legs()))
    assert state.quotes == ()
    assert any(d.raw_edge > 0.10 for d in state.decisions), (
        "fixture should produce a large apparent edge, otherwise this proves nothing"
    )


def test_every_decision_is_recorded_as_not_acted_on() -> None:
    """Un-acted decisions are what make calibration measurable without
    survivorship bias (PLAN.md 6.3).  A harness that only logged the trades it
    liked would prove its own edge by construction."""
    h = WeatherHarness(forecasts={
        "KXHIGHTSFO-26AUG27": ForecastPoint("KXHIGHTSFO-26AUG27", 74.0)
    })
    decisions = h.desired_state(snapshot(sfo_legs())).decisions
    assert len(decisions) == 6
    assert all(d.acted is False for d in decisions)
    assert all(d.category == "Climate and Weather" for d in decisions)


def test_the_harness_sits_below_the_gate_at_which_the_executor_will_send_orders() -> None:
    """Belt and braces for invariant I5: even a caller that ignored the empty
    quote tuple could not send an order, because gate 0 is below the executor's
    LIVE threshold of 4."""
    assert WeatherHarness().gate < 4
    assert EDGE_DEMONSTRATED is False


def test_the_harness_satisfies_the_sleeve_protocol_so_it_shares_one_code_path() -> None:
    """Backtest, shadow and live must run the same object (PLAN.md 4.2).  A
    research harness that did not conform would need a parallel harness runner,
    and the two would drift."""
    assert isinstance(WeatherHarness(), Sleeve)


def test_desired_state_is_pure_and_reads_time_only_from_the_snapshot() -> None:
    """C4.2a.  A weather sleeve that re-derived 'now' from the clock would, in a
    backtest, silently price against a forecast issued after the market closed."""
    h = WeatherHarness(forecasts={
        "KXHIGHTSFO-26AUG27": ForecastPoint("KXHIGHTSFO-26AUG27", 74.5)
    })
    snap = snapshot(sfo_legs())
    a = h.desired_state(snap)
    b = h.desired_state(snap)
    assert [(d.ticker, d.p_model) for d in a.decisions] == \
           [(d.ticker, d.p_model) for d in b.decisions]
    early = h.desired_state(snapshot(sfo_legs(), now_us=NOW - 20 * HOUR))
    assert early.decisions[0].p_model != a.decisions[0].p_model, (
        "moving the snapshot clock must change the lead and therefore the model"
    )


# --------------------------------------------------------------------------- #
# Settlement.  Getting the station or the window wrong voids every number.
# --------------------------------------------------------------------------- #
def test_the_four_cities_that_settle_on_a_surprising_station_are_recorded() -> None:
    """Fetching a gridpoint forecast for 'Chicago' lands on O'Hare, which settles
    nothing -- Kalshi uses Midway.  Same for Houston (Hobby), New York (Central
    Park) and Austin (Bergstrom).  Four of 23 cities, i.e. 17% of the corpus
    would be forecast against the wrong sensor by the obvious default."""
    assert SETTLEMENT_STATION["KXHIGHCHI"] == ("CLIMDW", "KMDW", "max")
    assert SETTLEMENT_STATION["KXHIGHTHOU"] == ("CLIHOU", "KHOU", "max")
    assert SETTLEMENT_STATION["KXHIGHNY"] == ("CLINYC", "KNYC", "max")
    assert SETTLEMENT_STATION["KXHIGHAUS"] == ("CLIAUS", "KAUS", "max")
    assert set(SURPRISING_STATIONS) == {"CLIMDW", "CLIHOU", "CLINYC", "CLIAUS"}


def test_every_series_has_both_a_high_and_a_low_and_a_valid_element() -> None:
    """46 series over 23 stations.  A missing pair means a city whose low market
    would be priced with the high market's error model."""
    stations = {v[0] for v in SETTLEMENT_STATION.values()}
    assert len(stations) == 23
    assert len(SETTLEMENT_STATION) == 46
    for cli in stations:
        elements = {v[2] for v in SETTLEMENT_STATION.values() if v[0] == cli}
        assert elements == {"max", "min"}, cli


def test_the_climate_day_runs_midnight_to_midnight_local_standard_time() -> None:
    """Kalshi closes these at 01:00 local DAYLIGHT time, i.e. midnight local
    STANDARD time.  Computing the daily extreme over the local calendar day
    instead changes the answer on 11.1-11.9% of station-days for the MINIMUM
    (research/13 section 1.3) -- the minimum sits near the boundary."""
    start, end = climate_day_window_us("CLISFO", date(2026, 8, 26))
    assert end - start == 24 * HOUR
    # Pacific standard is UTC-8, so midnight LST is 08:00Z even during PDT.
    assert start == int(datetime(2026, 8, 26, 8, 0, tzinfo=UTC).timestamp() * 1e6)


def test_phoenix_does_not_observe_dst_so_its_climate_day_is_local_midnight() -> None:
    """Arizona is the one station where local standard and local calendar agree
    all year.  A hard-coded 'add one hour during summer' would be wrong here."""
    start, _ = climate_day_window_us("CLIPHX", date(2026, 8, 26))
    assert start == int(datetime(2026, 8, 26, 7, 0, tzinfo=UTC).timestamp() * 1e6)


# --------------------------------------------------------------------------- #
# The METAR unit trap.  This one cost $209,300 of imaginary profit.
# --------------------------------------------------------------------------- #
def test_metar_celsius_never_proves_a_bucket_dead_that_actually_won() -> None:
    """THE regression.  api.weather.gov reported KLAX at 31 C on 2026-08-26.
    Converted naively that is 87.8 F, which 'proves' the 86-87 bucket worthless.
    The settlement value was 87 F and that bucket WON at 99c.  The naive
    conversion produced 204 phantom dead buckets worth $209,300 of premium."""
    lo, hi = settlement_bounds_from_metar_c(31.0)
    assert lo == 87 and hi == 89
    assert lo <= 87 <= hi, "the true settlement value must lie inside the bracket"
    # The bound must NOT kill the winning bucket.
    winner = Bucket("KXHIGHLAX-26AUG26-B86.5", 85.5, 87.5)
    part = TempPartition("KXHIGHLAX-26AUG26", "KXHIGHLAX", (winner,), ())
    assert dominated_buckets(part, float(lo), "max") == ()
    # ...whereas the naive conversion would have.
    assert 31.0 * 9 / 5 + 32 == pytest.approx(87.8)
    assert dominated_buckets(part, 87.8, "max") == (winner.ticker,)


def test_a_whole_degree_celsius_reading_is_one_point_eight_fahrenheit_wide() -> None:
    """The bracket is a physical fact about the encoding, not a safety margin:
    a METAR of 20 C means anything in [19.5, 20.5] C.  Any inequality-based
    signal must respect it or it is trading rounding noise."""
    lo, hi = settlement_bounds_from_metar_c(20.0)
    assert hi - lo == 2
    assert lo <= 68 <= hi


def test_the_precise_t_group_narrows_the_bracket() -> None:
    """The hourly METAR remark carries 0.1 C, worth +/- 0.18 F instead of
    +/- 0.9 F.  Present on only ~5% of observations, so it tightens the bound
    when available and must never be assumed."""
    wide = settlement_bounds_from_metar_c(31.0)
    tight = settlement_bounds_from_metar_c(31.0, precise=True)
    assert (tight[1] - tight[0]) < (wide[1] - wide[0])


def test_dominated_buckets_mirrors_correctly_for_minimum_temperature_markets() -> None:
    """A running MINIMUM can only fall, so for a low market the dead buckets are
    the ones ABOVE the bound.  Getting the inequality backwards would sell the
    only legs that can still win."""
    buckets = (Bucket("lo", float("-inf"), 57.5), Bucket("mid", 57.5, 59.5),
               Bucket("hi", 59.5, float("inf")))
    part = TempPartition("E", "KXLOWTPHIL", buckets, ())
    assert dominated_buckets(part, 58.0, "min") == ("hi",)
    assert dominated_buckets(part, 58.0, "max") == ("lo",)


def test_dominated_buckets_rejects_an_element_it_does_not_understand() -> None:
    """Silently treating 'average' as 'max' would invert the inequality."""
    part = TempPartition("E", "KXHIGHTSFO", (Bucket("a", 1.0, 2.0),), ())
    with pytest.raises(ValueError):
        dominated_buckets(part, 1.5, "average")


# --------------------------------------------------------------------------- #
# Bucket geometry and the exhaustiveness gap.
# --------------------------------------------------------------------------- #
def test_bucket_titles_parse_into_the_continuous_intervals_settlement_compares() -> None:
    """Titles talk in integers, settlement compares a reported value, so '71-72'
    must become [70.5, 72.5) or the boundary cases price wrong."""
    assert parse_bucket("t", "Will the maximum temperature be 71-72 on Aug 27, 2026?") == \
        Bucket("t", 70.5, 72.5)
    assert parse_bucket("t", "Will the maximum temperature be <71 on Aug 27, 2026?") == \
        Bucket("t", float("-inf"), 70.5)
    assert parse_bucket("t", "Will the maximum temperature be >78 on Aug 27, 2026?") == \
        Bucket("t", 78.5, float("inf"))
    assert parse_bucket("t", "Will the Fed cut rates?") is None


def test_both_open_tails_are_named_dash_T_so_the_title_not_the_ticker_decides() -> None:
    """`-T71` and `-T78` are both tails of the same event; only the title says
    which side.  Parsing the ticker suffix would put both on the same end and
    silently produce a non-exhaustive partition."""
    lo = parse_bucket("KXHIGHTSFO-26AUG27-T71",
                      "Will the maximum temperature be <71 on Aug 27, 2026?")
    hi = parse_bucket("KXHIGHTSFO-26AUG27-T78",
                      "Will the maximum temperature be >78 on Aug 27, 2026?")
    assert lo is not None and hi is not None
    assert lo.lo == float("-inf") and hi.hi == float("inf")


def test_a_partition_with_a_gap_is_rejected_rather_than_silently_traded() -> None:
    """Contiguity is the exhaustiveness claim.  A gap means some settlement value
    resolves every leg NO -- the F1 trap that rulebook/exhaustiveness.py exists
    for, and the reason the long basket is unsafe."""
    legs = sfo_legs()
    broken = list(legs)
    broken[3] = leg("KXHIGHTSFO-26AUG27-B99.5",
                    "Will the maximum temperature be 99-100 on Aug 27, 2026?", 24, 25)
    assert partition_from_markets("E", "KXHIGHTSFO", broken) is None
    assert partition_from_markets("E", "KXHIGHTSFO", legs) is not None


def test_a_partition_without_both_open_tails_is_rejected() -> None:
    """Bounded ends leave the outcome space uncovered on at least one side."""
    bounded = [m for m in sfo_legs() if not m.ticker.endswith(("T71", "T78"))]
    assert partition_from_markets("E", "KXHIGHTSFO", bounded) is None


def test_the_real_sfo_partition_tiles_the_line_and_normalises_to_one() -> None:
    """The corpus fixture: every one of 92 events had exactly 6 contiguous legs."""
    p = partition_from_markets("KXHIGHTSFO-26AUG27", "KXHIGHTSFO", sfo_legs())
    assert p is not None
    assert p.cuts == (70.5, 72.5, 74.5, 76.5, 78.5)
    probs = implied_distribution(p)
    assert probs is not None
    assert sum(probs) == pytest.approx(1.0)
    assert p.element == "max"


# --------------------------------------------------------------------------- #
# Distributions.
# --------------------------------------------------------------------------- #
def test_bucket_probabilities_are_a_proper_distribution() -> None:
    """If these did not sum to one the implied sd, and therefore every
    comparison in research/13, would be measuring the wrong thing."""
    q = bucket_probabilities((70.5, 72.5, 74.5, 76.5, 78.5), 74.0, 2.0)
    assert len(q) == 6
    assert sum(q) == pytest.approx(1.0)
    assert all(x > 0 for x in q)


def test_normal_cdf_matches_known_values() -> None:
    """The whole error model rests on this one function."""
    assert normal_cdf(0.0) == pytest.approx(0.5)
    assert normal_cdf(1.0) == pytest.approx(0.8413447, abs=1e-6)
    assert normal_cdf(74.0, 74.0, 2.0) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        normal_cdf(0.0, 0.0, 0.0)


def test_fitting_recovers_a_gaussian_that_was_put_in() -> None:
    """Identifiability check.  If the fit could not recover a known sd, the
    measured 'market implied sd 1.95 F' would be an artefact of the optimiser
    rather than a fact about the book."""
    cuts = (70.5, 72.5, 74.5, 76.5, 78.5)
    truth = bucket_probabilities(cuts, 74.3, 1.95)
    fit = fit_implied_gaussian(cuts, truth)
    assert fit.mu == pytest.approx(74.3, abs=0.10)
    assert fit.sd == pytest.approx(1.95, abs=0.10)
    assert fit.kl < 1e-3
    assert fit.identifiable


def test_a_tail_dominated_partition_is_flagged_unidentifiable_not_reported() -> None:
    """When nearly all mass sits in an open tail, mu runs off to infinity.  Live
    example: the fit put KXLOWTPHIL's implied mean at 94.7 F for a MINIMUM
    temperature, with sd 14.8 F.  Reporting that as an implied forecast would
    have produced a 28 F 'edge' out of a degenerate optimisation."""
    cuts = (57.5, 59.5, 61.5, 63.5, 65.5)
    tail_heavy = (0.005, 0.005, 0.005, 0.005, 0.005, 0.975)
    fit = fit_implied_gaussian(cuts, tail_heavy)
    assert not fit.identifiable
    centred = fit_implied_gaussian(cuts, (0.02, 0.10, 0.30, 0.36, 0.17, 0.05))
    assert centred.identifiable


def test_the_fit_is_deterministic_because_desired_state_must_be_replayable() -> None:
    """C4.2a again.  A stochastic optimiser inside a sleeve makes a backtest
    unreproducible and an audit impossible."""
    cuts = (70.5, 72.5, 74.5, 76.5, 78.5)
    probs = (0.14, 0.19, 0.33, 0.24, 0.07, 0.03)
    a = fit_implied_gaussian(cuts, probs)
    b = fit_implied_gaussian(cuts, probs)
    assert (a.mu, a.sd, a.kl) == (b.mu, b.sd, b.kl)


def test_implied_mae_is_the_gaussian_mean_absolute_deviation() -> None:
    """research/09 used MAE = 0.798 * sd to compare against published NWS skill.
    The constant is sqrt(2/pi), not a fudge."""
    fit = fit_implied_gaussian((70.5, 72.5, 74.5, 76.5, 78.5),
                               bucket_probabilities((70.5, 72.5, 74.5, 76.5, 78.5), 74.5, 2.0))
    assert fit.implied_mae == pytest.approx(fit.sd * math.sqrt(2 / math.pi))


def test_implied_distribution_rejects_a_book_whose_mids_do_not_look_like_one() -> None:
    """A book summing far from 1.0 is either a ladder of nested thresholds or a
    stale quote.  Normalising it anyway is how S2 once sized a 21-leg short
    against an instrument that pays at most $1."""
    p = partition_from_markets("E", "KXHIGHTSFO",
                               sfo_legs([(90, 92)] * 6))
    assert p is not None
    assert implied_distribution(p) is None


# --------------------------------------------------------------------------- #
# The measured constants -- these ARE the finding.
# --------------------------------------------------------------------------- #
def test_the_market_is_sharper_than_the_best_free_forecast_at_matched_lead() -> None:
    """THE RESULT (research/13 section 4).  Market implied sd 1.95 F at 24-36h to
    close against 2.19-2.38 F for the best free blend interpolated to the same
    lead.  While this holds there is nothing to trade, and if it ever stops
    holding this test is the alarm."""
    market = MARKET_IMPLIED_SD_F[(24.0, 36.0)]
    assert market == pytest.approx(1.954, abs=0.01)
    lead = lead_hours_to_event(30.0)          # 30h to close -> ~20h to the max
    assert lead == pytest.approx(20.0)
    free = forecast_error_sd("max", lead)
    assert free > market, "if this ever inverts, re-run research/13 before trading"
    assert free - market == pytest.approx(0.31, abs=0.05)


def test_the_market_is_sharper_than_every_single_free_product_day_ahead() -> None:
    """Not just the blend: NBM alone is 2.45 F and Open-Meteo best_match 2.79 F
    against the market's 1.95 F.  'Blend the models yourself' is not a way out --
    NBM already IS NOAA's calibrated blend."""
    market = MARKET_IMPLIED_SD_F[(24.0, 36.0)]
    for source in ("nbm", "best_match", "blend"):
        key = (source, "24-39h", "max")
        if key in FORECAST_ERROR_SD_F:
            assert FORECAST_ERROR_SD_F[key] > market, source


def test_forecast_error_grows_monotonically_with_lead_time() -> None:
    """A model whose error shrank with lead would be reading the future.  Also
    guards the interpolation from producing a non-monotone curve."""
    sds = [forecast_error_sd("max", h) for h in (0, 3, 10, 20, 31, 50, 80)]
    assert sds == sorted(sds)
    assert sds[0] == pytest.approx(2.06)


def test_the_market_sharpens_monotonically_as_close_approaches() -> None:
    """0.57 F inside three hours against 1.95 F a day out.  Inside six hours the
    modal bucket is at 0.975, which is the latency game and not ours."""
    sds = [market_implied_sd(h) for h in (1.0, 8.0, 15.0, 20.0, 30.0)]
    assert sds == sorted(sds)
    assert market_implied_sd(1.0) == pytest.approx(0.570)


def test_lead_to_the_event_is_about_ten_hours_shorter_than_lead_to_the_close() -> None:
    """Close is 01:00 LST the following day; the maximum lands mid-afternoon.
    Comparing a forecast at lead h against a market at h hours-to-close would
    handicap the forecast by ten hours and flatter the market."""
    assert lead_hours_to_event(30.0) == pytest.approx(20.0)
    assert lead_hours_to_event(2.0) == 0.0


def test_makers_pay_no_fee_on_these_series_so_the_entire_hurdle_is_adverse_selection() -> None:
    """Temperature series are fee_type 'quadratic', fee_multiplier 1.0, and
    13,353 of 13,486 such series charge makers ZERO.  The reason not to quote
    here is not the fee -- it is that the forecast is worse than the price."""
    from core.math.contracts import fee
    assert fee(0.30, QUADRATIC, is_maker=True) == 0.0
    assert fee(0.30, QUADRATIC, is_maker=False) > 0.0


def test_the_taker_hurdle_reproduces_the_research_table() -> None:
    """research/13 TABLE C: 1.63pp at 10c rising to 2.75pp at 50c, on the
    measured 2.0c median spread.  These are the numbers an edge must clear."""
    assert MEDIAN_TRADEABLE_SPREAD_CENTS == 2.0
    assert required_taker_edge(0.10, QUADRATIC) == pytest.approx(0.0163, abs=1e-4)
    assert required_taker_edge(0.30, QUADRATIC) == pytest.approx(0.0247, abs=1e-4)
    assert required_taker_edge(0.50, QUADRATIC) == pytest.approx(0.0275, abs=1e-4)


def test_the_taker_hurdle_in_degrees_is_about_two_tenths_of_a_fahrenheit() -> None:
    """research/13 TABLE D.  0.17-0.22 F is the whole prize, and section 3
    measured us 0.24-0.43 F BEHIND -- larger than the prize, opposite sign."""
    sd = MARKET_IMPLIED_SD_F[(18.0, 24.0)]
    need_20c = degrees_for_bucket_move(required_taker_edge(0.20, QUADRATIC), sd)
    need_40c = degrees_for_bucket_move(required_taker_edge(0.40, QUADRATIC), sd)
    assert need_20c == pytest.approx(0.17, abs=0.03)
    assert need_40c == pytest.approx(0.22, abs=0.03)
    assert need_40c > need_20c


def test_a_one_degree_error_moves_a_bucket_by_about_twelve_points() -> None:
    """The sensitivity that makes this category look tractable and is exactly why
    it is not: 0.25 F is worth 3pp, which is inside the disagreement between two
    reasonable post-processings of the same public data."""
    sd = MARKET_IMPLIED_SD_F[(18.0, 24.0)]
    assert degrees_for_bucket_move(0.0122, sd) == pytest.approx(0.10, abs=0.02)
    assert degrees_for_bucket_move(0.1191, sd) == pytest.approx(1.00, abs=0.05)
    assert degrees_for_bucket_move(0.0, sd) == 0.0


def test_the_question_is_decidable_in_about_a_hundred_settlements() -> None:
    """research/09 5.3 said ~10,000 settled markets, 'that is years', assuming a
    2pp disagreement.  The MEASURED disagreement is 18.8pp, and N >= 4/delta^2
    scales inverse-square, so it is 114 settlements -- about 2.5 days of all 46
    listed city-days.  The harness exists to produce exactly those rows."""
    assert markets_to_beat_market(MEASURED_DISAGREEMENT_PP) == 114
    assert markets_to_beat_market(0.02) == 10_000


# --------------------------------------------------------------------------- #
# Assessment behaviour.
# --------------------------------------------------------------------------- #
def test_an_event_with_no_forecast_produces_no_decisions_rather_than_a_guess() -> None:
    """A missing forecast must not silently become 'the partition centre'.  That
    would score a made-up number against reality and pollute the calibration
    record the harness exists to build."""
    state = WeatherHarness().desired_state(snapshot(sfo_legs()))
    assert state.decisions == ()
    assert state.rationale["events_assessed"] == 1


def test_the_assessment_reports_a_sharpness_deficit_in_degrees() -> None:
    """The deficit is `our error sd - the market's implied sd`.  It is the single
    number that decides whether there is anything to trade, so it must be
    computed and carried, not inferred later from a probability comparison."""
    h = WeatherHarness(forecasts={
        "KXHIGHTSFO-26AUG27": ForecastPoint("KXHIGHTSFO-26AUG27", 74.0)
    })
    p = partition_from_markets("KXHIGHTSFO-26AUG27", "KXHIGHTSFO", sfo_legs())
    assert p is not None
    a = h.assess(p, snapshot(sfo_legs()))
    assert a is not None
    assert a.element == "max"
    assert a.forecast_sd is not None and a.sharpness_deficit_f is not None
    assert a.sharpness_deficit_f == pytest.approx(a.forecast_sd - a.implied.sd)
    assert 0.0 < a.max_abs_disagreement < 1.0


def test_on_a_typical_day_the_market_is_sharper_than_our_forecast() -> None:
    """The corpus finding, applied to one event.  A partition carrying the median
    implied sd of 1.95 F must come out with a POSITIVE deficit -- the market
    claiming more accuracy than we can deliver at that lead."""
    cuts = (70.5, 72.5, 74.5, 76.5, 78.5)
    typical = bucket_probabilities(cuts, 74.5, MARKET_IMPLIED_SD_F[(24.0, 36.0)])
    quotes = [(max(int(q * 100) - 1, 1), int(q * 100) + 1) for q in typical]
    h = WeatherHarness(forecasts={
        "KXHIGHTSFO-26AUG27": ForecastPoint("KXHIGHTSFO-26AUG27", 74.5)
    })
    p = partition_from_markets("KXHIGHTSFO-26AUG27", "KXHIGHTSFO", sfo_legs(quotes))
    assert p is not None
    a = h.assess(p, snapshot(sfo_legs(quotes)))
    assert a is not None
    assert a.implied.sd == pytest.approx(1.95, abs=0.15)
    assert a.sharpness_deficit_f is not None and a.sharpness_deficit_f > 0
    assert a.forecast_is_sharper is False


def test_a_wide_market_day_can_look_beatable_and_must_still_not_be_traded() -> None:
    """THE RESIDUAL, kept as a test so it is not forgotten.  Implied sd varies by
    event (p10/p50/p90 = 1.57 / 1.87 / 2.54 F at 18-36h), and on 13-21% of events
    it exceeds our best CONSTANT forecast sd -- the real KXHIGHTSFO-26AUG27 book
    implies 2.70 F, a marine-layer coin flip.  That is a sharpness comparison,
    not an accuracy one, and our constant sd is exactly what is most wrong on
    such days.  research/13 section 6 item 7: untested, n = 38, no power.  So
    even here the harness quotes nothing."""
    h = WeatherHarness(forecasts={
        "KXHIGHTSFO-26AUG27": ForecastPoint("KXHIGHTSFO-26AUG27", 74.0)
    })
    p = partition_from_markets("KXHIGHTSFO-26AUG27", "KXHIGHTSFO", sfo_legs())
    assert p is not None
    a = h.assess(p, snapshot(sfo_legs()))
    assert a is not None
    assert a.implied.sd == pytest.approx(2.70, abs=0.10)
    assert a.forecast_is_sharper is True, "this fixture is the residual case"
    assert h.desired_state(snapshot(sfo_legs())).quotes == ()


def test_the_rationale_records_why_there_are_no_quotes() -> None:
    """C4.2c: an order whose reasoning cannot be reconstructed is a bug, and so
    is a REFUSAL whose reasoning cannot be reconstructed."""
    state = WeatherHarness().desired_state(snapshot(sfo_legs()))
    assert state.rationale["mode"] == "research_harness"
    assert state.rationale["edge_demonstrated"] is False
    assert "1.95" in state.rationale["reason_no_quotes"]


def test_markets_from_other_categories_are_ignored_entirely() -> None:
    """The harness keys off the settlement map, not off a title regex.  A
    non-weather market whose title happens to contain a range must not be
    parsed as a temperature bucket."""
    other = Market(ticker="KXBTC-1", event_ticker="KXBTC", series_ticker="KXBTC",
                   title="Will the price be 71-72 thousand?", yes_bid=40, yes_ask=41)
    state = WeatherHarness().desired_state(snapshot(sfo_legs() + [other]))
    assert state.rationale["events_assessed"] == 1


# --------------------------------------------------------------------------- #
# Live -- network, excluded from the default run.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_acis_still_reproduces_a_settled_kalshi_temperature_market() -> None:
    """The settlement mapping is the foundation of research/13.  If ACIS ever
    stops matching, every measured number in that note is void."""
    import json
    import urllib.request

    body = json.dumps({"sid": "KPHL", "sdate": "2026-08-26",
                       "edate": "2026-08-26", "elems": "maxt,mint"}).encode()
    req = urllib.request.Request(
        "https://data.rcc-acis.org/StnData", data=body,
        headers={"Content-Type": "application/json", "User-Agent": "pm-research"})
    with urllib.request.urlopen(req, timeout=60) as fh:
        data = json.load(fh)
    # KXHIGHPHIL-26AUG26-B85.5 ("85-86") settled YES.
    assert float(data["data"][0][1]) == 86.0
