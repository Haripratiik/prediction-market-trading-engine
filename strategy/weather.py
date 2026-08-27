"""RESEARCH HARNESS -- Kalshi daily temperature markets.  research/13.

THIS MODULE DELIBERATELY EMITS NO QUOTES.  `desired_state` returns
`quotes = ()` unconditionally and every `Decision` carries `acted = False`.

WHY, in money terms
-------------------
The thesis was: Kalshi lists daily high/low temperature as a mutually exclusive,
exhaustive 6-bucket partition; NWS/NOAA publish free forecasts; if the free
forecast beats the market's implied distribution that is a statistical edge that
needs no speed.  We tested it and it is not there.  research/13 section 4, all
measured on this machine:

    market implied sd, 24-36h to close (lead-to-max ~14-26h)   1.95 F
    best free blend, lead-interpolated to the SAME lead        2.19 - 2.32 F
    NOAA NBM alone, day-ahead, out-of-sample debiased          2.45 F
    Open-Meteo best_match, day-ahead, OOS debiased             2.79 F

The market's point forecast is 11-16% MORE accurate than the best forecast we
can assemble from free public data, at matched lead.  A taker needs to beat the
market by 0.17-0.22 F to clear fee plus half-spread; we are behind by
0.24-0.43 F.  Trading this is not a small edge, it is a measured negative one.

The model-free version of the same idea -- the running max-so-far is a HARD
LOWER BOUND, so any bucket below it is worth exactly zero -- fired ZERO times in
1,591 synchronous checks across 92 events.  See `dominated_buckets`, which is
implemented here because it is the only structure in the category that could
ever justify a quote, and because getting its units wrong manufactured $209,300
of imaginary profit on the first attempt (research/13 section 5.1).

So the harness records what a sleeve WOULD have thought, settles it later, and
costs nothing.  research/13 section 4.2: at the measured 18.8pp typical
disagreement, `markets_to_beat_market` says 114 settlements decide it.  Those
rows are what this module exists to produce.

WHAT WOULD HAVE TO CHANGE before any of this ships a quote
----------------------------------------------------------
    1.  `EDGE_DEMONSTRATED` flips to True, which requires >= 114 settled
        decisions whose Brier beats the market's on the SAME events, and
    2.  a non-Gaussian error model, because the market's implied density is
        peaked-and-fat and a Gaussian misprices mode against shoulders on every
        market before skill even enters (research/13 section 2.2), and
    3.  `gate` is raised to 4, which the executor enforces (invariant I5).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from core.math.contracts import FeeSpec, edge, fee
from core.math.sizing import LAMBDA_DEFAULT
from core.models import Market
from strategy.base import Decision, DesiredState, MarketSnapshot

# --------------------------------------------------------------------------- #
# THE GOVERNING FLAG.  research/13 section 7.
# --------------------------------------------------------------------------- #
EDGE_DEMONSTRATED: Final[bool] = False
"""No weather edge has been demonstrated.  While False this module never quotes."""


# --------------------------------------------------------------------------- #
# Settlement.  research/13 section 1.  Getting ANY of this wrong makes every
# number downstream meaningless, so it is data, not a guess.
# --------------------------------------------------------------------------- #
Element = str  # "max" | "min"

SETTLEMENT_STATION: Final[dict[str, tuple[str, str, Element]]] = {
    # series ticker -> (CLI product id, ICAO station, element)
    # Read verbatim from `rules_docs` in data/pm.db, 2026-08-27.
    "KXHIGHTATL": ("CLIATL", "KATL", "max"), "KXLOWTATL": ("CLIATL", "KATL", "min"),
    "KXHIGHAUS": ("CLIAUS", "KAUS", "max"),  "KXLOWTAUS": ("CLIAUS", "KAUS", "min"),
    "KXHIGHTBOS": ("CLIBOS", "KBOS", "max"), "KXLOWTBOS": ("CLIBOS", "KBOS", "min"),
    "KXHIGHTDC": ("CLIDCA", "KDCA", "max"),  "KXLOWTDC": ("CLIDCA", "KDCA", "min"),
    "KXHIGHDEN": ("CLIDEN", "KDEN", "max"),  "KXLOWTDEN": ("CLIDEN", "KDEN", "min"),
    "KXHIGHTDAL": ("CLIDFW", "KDFW", "max"), "KXLOWTDAL": ("CLIDFW", "KDFW", "min"),
    "KXHIGHTEWR": ("CLIEWR", "KEWR", "max"), "KXLOWTEWR": ("CLIEWR", "KEWR", "min"),
    "KXHIGHTHOU": ("CLIHOU", "KHOU", "max"), "KXLOWTHOU": ("CLIHOU", "KHOU", "min"),
    "KXHIGHTLV": ("CLILAS", "KLAS", "max"),  "KXLOWTLV": ("CLILAS", "KLAS", "min"),
    "KXHIGHLAX": ("CLILAX", "KLAX", "max"),  "KXLOWTLAX": ("CLILAX", "KLAX", "min"),
    "KXHIGHCHI": ("CLIMDW", "KMDW", "max"),  "KXLOWTCHI": ("CLIMDW", "KMDW", "min"),
    "KXHIGHMIA": ("CLIMIA", "KMIA", "max"),  "KXLOWTMIA": ("CLIMIA", "KMIA", "min"),
    "KXHIGHTMIN": ("CLIMSP", "KMSP", "max"), "KXLOWTMIN": ("CLIMSP", "KMSP", "min"),
    "KXHIGHTNOLA": ("CLIMSY", "KMSY", "max"), "KXLOWTNOLA": ("CLIMSY", "KMSY", "min"),
    "KXHIGHNY": ("CLINYC", "KNYC", "max"),   "KXLOWTNYC": ("CLINYC", "KNYC", "min"),
    "KXHIGHTOKC": ("CLIOKC", "KOKC", "max"), "KXLOWTOKC": ("CLIOKC", "KOKC", "min"),
    "KXHIGHPHIL": ("CLIPHL", "KPHL", "max"), "KXLOWTPHIL": ("CLIPHL", "KPHL", "min"),
    "KXHIGHTPHX": ("CLIPHX", "KPHX", "max"), "KXLOWTPHX": ("CLIPHX", "KPHX", "min"),
    "KXHIGHTSATX": ("CLISAT", "KSAT", "max"), "KXLOWTSATX": ("CLISAT", "KSAT", "min"),
    "KXHIGHTSAN": ("CLISAN", "KSAN", "max"), "KXLOWTSAN": ("CLISAN", "KSAN", "min"),
    "KXHIGHTSEA": ("CLISEA", "KSEA", "max"), "KXLOWTSEA": ("CLISEA", "KSEA", "min"),
    "KXHIGHTSFO": ("CLISFO", "KSFO", "max"), "KXLOWTSFO": ("CLISFO", "KSFO", "min"),
    "KXHIGHTTTN": ("CLITTN", "KTTN", "max"), "KXLOWTTTN": ("CLITTN", "KTTN", "min"),
}

# The four cities where the obvious guess is the WRONG station.  A gridpoint
# forecast fetched for "Chicago" lands on O'Hare and settles nothing.
SURPRISING_STATIONS: Final[dict[str, str]] = {
    "CLIMDW": "Chicago settles on MIDWAY, not O'Hare",
    "CLIHOU": "Houston settles on HOBBY, not Bush/IAH",
    "CLINYC": "New York settles on CENTRAL PARK, not JFK/LGA/EWR",
    "CLIAUS": "Austin settles on BERGSTROM, not Camp Mabry",
}

IANA_TZ: Final[dict[str, str]] = {
    "CLIATL": "America/New_York", "CLIAUS": "America/Chicago",
    "CLIBOS": "America/New_York", "CLIDCA": "America/New_York",
    "CLIDEN": "America/Denver", "CLIDFW": "America/Chicago",
    "CLIEWR": "America/New_York", "CLIHOU": "America/Chicago",
    "CLILAS": "America/Los_Angeles", "CLILAX": "America/Los_Angeles",
    "CLIMDW": "America/Chicago", "CLIMIA": "America/New_York",
    "CLIMSP": "America/Chicago", "CLIMSY": "America/Chicago",
    "CLINYC": "America/New_York", "CLIOKC": "America/Chicago",
    "CLIPHL": "America/New_York", "CLIPHX": "America/Phoenix",
    "CLISAN": "America/Los_Angeles", "CLISAT": "America/Chicago",
    "CLISEA": "America/Los_Angeles", "CLISFO": "America/Los_Angeles",
    "CLITTN": "America/New_York",
}

SETTLEMENT_SOURCE_PER_API: Final[str] = "The Weather Company"
SETTLEMENT_SOURCE_PER_RULEBOOK: Final[str] = "National Weather Service"
"""These disagree.  research/13 section 1.2 -- the API names the publisher, the
certified GLOBALTEMPERATURE terms name the agency.  All 7 settled markets in our
corpus matched the NWS-derived value; that is 7 observations, not a guarantee."""


# --------------------------------------------------------------------------- #
# Measured constants.  Every one of these is a number from research/13, not a
# guess, and each carries where it came from.
# --------------------------------------------------------------------------- #
MARKET_IMPLIED_SD_F: Final[dict[tuple[float, float], float]] = {
    # (hours-to-close lower, upper) -> median Gaussian-KL-fitted sd, in F.
    # research/13 TABLE B, 625 synchronous partitions across 81 events.
    (0.0, 3.0): 0.570,
    (6.0, 12.0): 0.972,
    (12.0, 18.0): 1.804,
    (18.0, 24.0): 1.907,
    (24.0, 36.0): 1.954,
    (36.0, 60.0): 1.981,
}

FORECAST_ERROR_SD_F: Final[dict[tuple[str, str, Element], float]] = {
    # (source, lead bucket, element) -> out-of-sample debiased error sd, in F.
    # research/13 TABLE A.  2,713 station-days, 23 CLI stations, 2026-05-01..08-26,
    # scored against ACIS station values over the midnight-LST climate day.
    ("nbm", "0-6h", "max"): 2.06, ("nbm", "0-6h", "min"): 2.05,
    ("nbm", "24-39h", "max"): 2.45, ("nbm", "24-39h", "min"): 2.33,
    ("nbm", "48-63h", "max"): 2.77, ("nbm", "48-63h", "min"): 2.52,
    ("nbm", "72-87h", "max"): 3.12, ("nbm", "72-87h", "min"): 2.65,
    ("best_match", "0-6h", "max"): 1.27, ("best_match", "0-6h", "min"): 1.39,
    ("best_match", "24-39h", "max"): 2.79, ("best_match", "24-39h", "min"): 2.38,
    ("best_match", "48-63h", "max"): 3.49, ("best_match", "48-63h", "min"): 2.79,
    ("best_match", "72-87h", "max"): 3.92, ("best_match", "72-87h", "min"): 2.96,
    # In-sample-optimal two-model blend.  OPTIMISTIC -- the weight was fitted on
    # the same days it is scored on, so a live blend is worse than this.
    ("blend", "24-39h", "max"): 2.38, ("blend", "24-39h", "min"): 2.48,
}

BLEND_WEIGHT_NBM: Final[dict[Element, float]] = {"max": 0.65, "min": 0.70}

# Anchors for interpolating forecast error sd to an arbitrary lead.  Forecast
# error grows roughly linearly across day 1, so linear in lead is defensible --
# but it IS an interpolation (research/13 section 6, item 2).
LEAD_ANCHORS_F: Final[dict[Element, tuple[tuple[float, float], tuple[float, float]]]] = {
    "max": ((3.0, 2.06), (31.0, 2.38)),
    "min": ((3.0, 2.05), (31.0, 2.48)),
}

MEDIAN_TRADEABLE_SPREAD_CENTS: Final[float] = 2.0
"""Median bid-ask across buckets priced 3-97c at 18-36h.  research/13 TABLE C."""

MEAN_ABS_DEV_OVER_SD: Final[float] = 0.7978845608028654
"""E|X| / sd for a zero-mean Gaussian = sqrt(2/pi).  Converts implied sd to MAE."""

MEASURED_DISAGREEMENT_PP: Final[float] = 0.188
"""Median max|q - m| between the day-ahead blend and the market, 38 events at
18-36h to close.  research/13 section 4.2.  Feeds markets_to_beat_market."""

MARKET_GAUSSIAN_MISFIT_NATS: Final[float] = 0.024
"""Median KL(market || best-fit Gaussian) at 24-36h.  The market's implied shape
is peaked-and-fat; 606 of 625 observations exceed the modal probability any
Gaussian with their own fitted sd allows.  research/13 section 2.2."""

MIN_INTERIOR_MASS_FOR_FIT: Final[float] = 0.5
"""Below this the Gaussian fit is unidentifiable: when nearly all mass sits in an
open tail, mu runs off to infinity and sd with it.  One live example put
KXLOWTPHIL's implied mean at 94.7 F for a MINIMUM temperature.  Such partitions
are flagged, never fitted and then trusted."""


# --------------------------------------------------------------------------- #
# Bucket geometry.  A Kalshi temperature partition is six slots over INTEGER
# degrees F: two open tails and four 2-degree interior buckets.
# --------------------------------------------------------------------------- #
_RANGE = re.compile(r"be\s+(-?\d+)\s*-\s*(-?\d+)")
_LT = re.compile(r"be\s+<\s*(-?\d+)")
_GT = re.compile(r"be\s+>\s*(-?\d+)")


@dataclass(frozen=True, slots=True)
class Bucket:
    """One leg, as a half-open interval on the CONTINUOUS temperature line.

    The market titles talk in integers ("71-72", "<71", ">78") but settlement
    compares a reported value, so the usable representation is the continuous
    interval whose integer content matches: "71-72" -> [70.5, 72.5).
    """

    ticker: str
    lo: float
    hi: float

    @property
    def lowest_int(self) -> float:
        return self.lo + 0.5 if self.lo != float("-inf") else float("-inf")

    @property
    def highest_int(self) -> float:
        return self.hi - 0.5 if self.hi != float("inf") else float("inf")

    def contains(self, value: float) -> bool:
        return self.lo <= value < self.hi


def parse_bucket(ticker: str, title: str | None) -> Bucket | None:
    """Continuous interval for a temperature leg, from its title.  None if not one.

    Parsed from the TITLE rather than the ticker suffix on purpose: both tails
    are named `-T<n>` and only the title says which side it is.
    """
    if not title:
        return None
    m = _RANGE.search(title)
    if m:
        return Bucket(ticker, int(m.group(1)) - 0.5, int(m.group(2)) + 0.5)
    m = _LT.search(title)
    if m:
        return Bucket(ticker, float("-inf"), int(m.group(1)) - 0.5)
    m = _GT.search(title)
    if m:
        return Bucket(ticker, int(m.group(1)) + 0.5, float("inf"))
    return None


@dataclass(frozen=True, slots=True)
class TempPartition:
    """A verified-contiguous temperature partition and its top-of-book."""

    event_ticker: str
    series_ticker: str
    buckets: tuple[Bucket, ...]
    markets: tuple[Market, ...]

    @property
    def cuts(self) -> tuple[float, ...]:
        """The interior cut points -- one fewer than the bucket count."""
        return tuple(b.hi for b in self.buckets[:-1])

    @property
    def station(self) -> tuple[str, str, Element] | None:
        return SETTLEMENT_STATION.get(self.series_ticker)

    @property
    def element(self) -> Element | None:
        st = self.station
        return st[2] if st else None


def partition_from_markets(
    event_ticker: str, series_ticker: str, markets: list[Market]
) -> TempPartition | None:
    """Build a partition, or None if the legs do not actually tile the line.

    Contiguity is CHECKED, not assumed.  A partition with a gap is not
    exhaustive, and buying every leg of a non-exhaustive book is the F1 trap
    that `rulebook/exhaustiveness.py` exists to reject.
    """
    pairs = []
    for m in markets:
        b = parse_bucket(m.ticker, m.title)
        if b is None:
            return None
        pairs.append((b, m))
    if len(pairs) < 3:
        return None
    pairs.sort(key=lambda p: (p[0].lo if p[0].lo != float("-inf") else -1e18))
    buckets = tuple(p[0] for p in pairs)
    if buckets[0].lo != float("-inf") or buckets[-1].hi != float("inf"):
        return None
    for a, b in zip(buckets[:-1], buckets[1:]):
        if abs(a.hi - b.lo) > 1e-9:
            return None
    return TempPartition(event_ticker, series_ticker, buckets,
                         tuple(p[1] for p in pairs))


# --------------------------------------------------------------------------- #
# Distributions.  Pure maths, no scipy -- `desired_state` must stay cheap.
# --------------------------------------------------------------------------- #
def normal_cdf(x: float, mu: float = 0.0, sd: float = 1.0) -> float:
    if sd <= 0.0:
        raise ValueError(f"sd must be positive, got {sd!r}")
    return 0.5 * (1.0 + math.erf((x - mu) / (sd * math.sqrt(2.0))))


def bucket_probabilities(cuts: tuple[float, ...], mu: float,
                         sd: float) -> tuple[float, ...]:
    """Gaussian mass in each of the len(cuts)+1 slots.  Sums to 1 by construction."""
    prev = 0.0
    out = []
    for c in cuts:
        cur = normal_cdf(c, mu, sd)
        out.append(cur - prev)
        prev = cur
    out.append(1.0 - prev)
    return tuple(out)


def implied_distribution(p: TempPartition) -> tuple[float, ...] | None:
    """Normalised mid prices -- the market's implied distribution, or None.

    Normalising by sum(mid) removes the overround.  The books are close to fair
    here: median sum(bid) runs 0.95-1.01 across horizons (research/13 TABLE B),
    unlike the general Kalshi book whose median sum(ask) is 1.15.
    """
    mids = []
    for m in p.markets:
        mid = m.mid
        if mid is None:
            return None
        mids.append(mid)
    total = sum(mids)
    if not 0.5 < total <= 1.6:
        return None
    return tuple(x / total for x in mids)


def interior_mass(probs: tuple[float, ...]) -> float:
    """Probability NOT in the two open tails.  Drives fit identifiability."""
    return sum(probs[1:-1])


@dataclass(frozen=True, slots=True)
class ImpliedGaussian:
    mu: float
    sd: float
    kl: float
    identifiable: bool

    @property
    def implied_mae(self) -> float:
        """MAE the market is claiming for its own point forecast, in F."""
        return MEAN_ABS_DEV_OVER_SD * self.sd


def fit_implied_gaussian(cuts: tuple[float, ...],
                         probs: tuple[float, ...]) -> ImpliedGaussian:
    """Fit (mu, sd) by minimising KL(market || Gaussian).  Pure and deterministic.

    Coarse grid then successive refinement -- no optimiser, so the result is a
    function of its inputs alone (C4.2a) and reproduces bit-for-bit in backtest.

    The returned `kl` is not a diagnostic to ignore: at 24-36h its median is
    0.024 nats, which is the market telling you its density is NOT Gaussian
    (research/13 section 2.2).  Trading a Gaussian against it misprices the
    modal bucket on every market.
    """
    if len(probs) != len(cuts) + 1:
        raise ValueError("probs must have exactly one more element than cuts")
    if not cuts:
        raise ValueError("need at least one cut point")

    def score(mu: float, sd: float) -> float:
        q = bucket_probabilities(cuts, mu, sd)
        return -sum(p * math.log(max(qi, 1e-12)) for p, qi in zip(probs, q))

    centre = 0.5 * (cuts[0] + cuts[-1])
    span = max(cuts[-1] - cuts[0], 2.0)
    best_mu, best_sd = centre, 2.0
    lo_mu, hi_mu = centre - 2.0 * span, centre + 2.0 * span
    lo_sd, hi_sd = 0.2, 8.0 * span
    for _ in range(6):
        best = float("inf")
        step_mu = (hi_mu - lo_mu) / 40.0
        step_sd = (hi_sd - lo_sd) / 40.0
        for i in range(41):
            mu = lo_mu + i * step_mu
            for j in range(41):
                sd = lo_sd + j * step_sd
                if sd <= 0.05:
                    continue
                s = score(mu, sd)
                if s < best:
                    best, best_mu, best_sd = s, mu, sd
        lo_mu, hi_mu = best_mu - 2 * step_mu, best_mu + 2 * step_mu
        lo_sd, hi_sd = max(0.05, best_sd - 2 * step_sd), best_sd + 2 * step_sd

    q = bucket_probabilities(cuts, best_mu, best_sd)
    kl = sum(p * math.log(p / max(qi, 1e-12)) for p, qi in zip(probs, q) if p > 0)
    return ImpliedGaussian(best_mu, best_sd, kl,
                           interior_mass(probs) >= MIN_INTERIOR_MASS_FOR_FIT)


# --------------------------------------------------------------------------- #
# The error model, and what it costs to disagree.
# --------------------------------------------------------------------------- #
def forecast_error_sd(element: Element, lead_hours: float) -> float:
    """Best free-forecast error sd at `lead_hours`, in F.  research/13 TABLE E.

    Linear interpolation between the measured anchors, clamped outside them.
    This is the number a forecast distribution must be widened by; using the
    ensemble spread instead would be using the model's opinion of its own
    accuracy, which is not the same thing and is usually optimistic.
    """
    if element not in LEAD_ANCHORS_F:
        raise ValueError(f"element must be 'max' or 'min', got {element!r}")
    (l0, s0), (l1, s1) = LEAD_ANCHORS_F[element]
    if lead_hours <= l0:
        return s0
    if lead_hours >= l1:
        # Beyond the day-ahead anchor, fall back on the measured day-2/day-3 row.
        if lead_hours >= 60.0:
            return FORECAST_ERROR_SD_F[("nbm", "72-87h", element)]
        if lead_hours >= 40.0:
            return FORECAST_ERROR_SD_F[("nbm", "48-63h", element)]
        return s1
    return s0 + (s1 - s0) * (lead_hours - l0) / (l1 - l0)


def market_implied_sd(hours_to_close: float) -> float:
    """Median implied sd at this horizon, in F.  research/13 TABLE B."""
    for (lo, hi), sd in MARKET_IMPLIED_SD_F.items():
        if lo <= hours_to_close < hi:
            return sd
    return MARKET_IMPLIED_SD_F[(24.0, 36.0)] if hours_to_close >= 36.0 else 0.570


def lead_hours_to_event(hours_to_close: float) -> float:
    """Hours from now until the extreme actually occurs.

    Close is 01:00 local standard time on the following day and the maximum
    lands mid-afternoon, so the lead to the EVENT is about ten hours shorter
    than the lead to the close.  Comparing a forecast at lead h against a market
    at h hours-to-close without this shift handicaps the forecast by ~10 hours
    and makes the market look better than it is (research/13 section 4).
    """
    return max(0.0, hours_to_close - 10.0)


def required_taker_edge(price: float, spec: FeeSpec,
                        spread_cents: float = MEDIAN_TRADEABLE_SPREAD_CENTS) -> float:
    """Edge in probability needed before CROSSING is worth it: fee + half-spread.

    research/13 TABLE C: 1.63pp at 10c rising to 2.75pp at 50c.  Note the maker
    column is exactly zero -- these series are `fee_type = "quadratic"`, so
    makers pay nothing and the entire hurdle is adverse selection.
    """
    return fee(price, spec, is_maker=False) + spread_cents / 200.0


def degrees_for_bucket_move(move: float, sd: float, width: float = 2.0,
                            *, search_halfwidth: float = 3.0) -> float:
    """Smallest shift of the mean, in F, that moves SOME bucket by `move`.

    The answer depends on where the bucket sits relative to the mean, so this
    takes the most favourable placement -- an upper bound on your luck.
    research/13 TABLE D: 0.17 F at a 20c bucket, 0.22 F at 40c.
    """
    if move <= 0.0:
        return 0.0

    def best_move(delta: float) -> float:
        out = 0.0
        steps = 240
        for i in range(steps + 1):
            centre = -search_halfwidth + 2.0 * search_halfwidth * i / steps
            a, b = centre - width / 2.0, centre + width / 2.0
            base = normal_cdf(b, 0.0, sd) - normal_cdf(a, 0.0, sd)
            shifted = normal_cdf(b, delta, sd) - normal_cdf(a, delta, sd)
            out = max(out, abs(shifted - base))
        return out

    lo, hi = 0.0, 8.0
    for _ in range(48):
        mid = 0.5 * (lo + hi)
        if best_move(mid) < move:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# The climate day, and the model-free bound.  research/13 sections 1.3 and 5.
# --------------------------------------------------------------------------- #
def climate_day_window_us(cli: str, day: date) -> tuple[int, int]:
    """[start, end) of the midnight-LST climate day, in UTC epoch microseconds.

    Midnight LOCAL STANDARD time, not local calendar midnight.  During DST those
    differ by an hour, and recomputing the daily extreme over the wrong window
    changes the answer on 11.1-11.9% of station-days FOR THE MINIMUM
    (research/13 section 1.3).  The maximum happens mid-afternoon and forgives
    the error; the minimum happens near the boundary and does not.
    """
    tz = ZoneInfo(IANA_TZ[cli])
    standard = datetime(day.year, 1, 15, 12, tzinfo=tz).utcoffset()
    if standard is None:  # pragma: no cover -- ZoneInfo always supplies one
        raise ValueError(f"no UTC offset for {cli}")
    start = (datetime(day.year, day.month, day.day) - standard).replace(tzinfo=UTC)
    return int(start.timestamp() * 1e6), int((start + timedelta(days=1)).timestamp() * 1e6)


def settlement_bounds_from_metar_c(celsius: float,
                                   *, precise: bool = False) -> tuple[int, int]:
    """Guaranteed [lowest, highest] integer F the settlement value can round to.

    THE BUG THIS EXISTS TO PREVENT.  `api.weather.gov` reports temperature in
    whole degrees Celsius taken from the METAR.  Convert naively and KLAX on
    2026-08-26 reads 31 C -> 87.8 F, "proving" the 86-87 bucket dead.  The
    settlement value was 87 F and that bucket WON, bid at 99c.  Across the
    corpus the naive conversion manufactured 204 phantom dead buckets worth
    $209,300 of imaginary premium (research/13 section 5.1).

    A whole-degree C reading is 1.8 F wide, so only the bracketed range is
    known.  `precise=True` is for the `Txxxxxxxx` METAR remark group, which
    carries 0.1 C and narrows the bracket to +/- 0.18 F.
    """
    half = 0.05 if precise else 0.5
    lo = 1.8 * celsius + 32.0 - 1.8 * half
    hi = 1.8 * celsius + 32.0 + 1.8 * half
    return math.floor(lo + 0.5), math.ceil(hi - 0.5)


def dominated_buckets(p: TempPartition, bound: float, element: Element) -> tuple[str, ...]:
    """Legs that are already worth EXACTLY zero, given a provable running bound.

    For a `max` market the running maximum can only rise, so every bucket
    entirely below it is dead; for a `min` market, mirror.  No forecast, no
    distribution, no calibration -- an inequality.  It is the only structure in
    this category that could ever justify a quote.

    IT NEVER FIRES.  Zero hits in 1,591 synchronous checks across 92 events
    (research/13 section 5).  `bound` MUST come from
    `settlement_bounds_from_metar_c`, never from a raw unit conversion.
    """
    if element not in ("max", "min"):
        raise ValueError(f"element must be 'max' or 'min', got {element!r}")
    out = []
    for b in p.buckets:
        dead = b.highest_int < bound if element == "max" else b.lowest_int > bound
        if dead:
            out.append(b.ticker)
    return tuple(out)


# --------------------------------------------------------------------------- #
# The harness.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ForecastPoint:
    """A point forecast for one event's settlement value, plus its provenance.

    Passed IN, never fetched.  `desired_state` is pure (C4.2a): a sleeve that
    reaches for the network cannot be replayed, and a weather sleeve that
    re-fetches during a backtest silently reads tomorrow's forecast.
    """

    event_ticker: str
    value_f: float
    source: str = "blend"
    lead_hours: float | None = None


@dataclass(frozen=True, slots=True)
class WeatherAssessment:
    """Everything the harness concluded about one event.  Recorded, never traded."""

    event_ticker: str
    series_ticker: str
    element: Element
    hours_to_close: float
    market_probs: tuple[float, ...]
    implied: ImpliedGaussian
    forecast_f: float | None
    forecast_sd: float | None
    model_probs: tuple[float, ...] | None
    max_abs_disagreement: float
    sharpness_deficit_f: float | None
    dominated: tuple[str, ...] = ()

    @property
    def forecast_is_sharper(self) -> bool:
        """True only if our error sd is SMALLER than the market's implied sd.

        Measured across the corpus this is false: 1.95 F implied against
        2.19-2.38 F achievable at matched lead.  A model that is wider than the
        market cannot beat it, however much the two disagree.
        """
        return self.sharpness_deficit_f is not None and self.sharpness_deficit_f < 0.0


@dataclass
class WeatherHarness:
    """RESEARCH ONLY.  Conforms to `Sleeve`; emits decisions and NEVER quotes.

    `gate = 0` keeps it below the executor's LIVE threshold of 4 (invariant I5),
    so even a caller that ignored `EDGE_DEMONSTRATED` could not send an order.
    """

    id: str = "weather"
    gate: int = 0
    forecasts: dict[str, ForecastPoint] = field(default_factory=dict)
    lam: float = LAMBDA_DEFAULT
    fee_spec: FeeSpec = field(default_factory=lambda: FeeSpec.kalshi("quadratic", 1.0))
    category: str = "Climate and Weather"

    # -- assessment ------------------------------------------------------- #
    def assess(self, p: TempPartition, snapshot: MarketSnapshot) -> WeatherAssessment | None:
        probs = implied_distribution(p)
        if probs is None:
            return None
        st = p.station
        if st is None:
            return None
        element = st[2]

        closes = [m.close_at_us for m in p.markets if m.close_at_us]
        hours = ((min(closes) - snapshot.now_us) / 1e6 / 3600.0) if closes else 0.0
        implied = fit_implied_gaussian(p.cuts, probs)

        fp = self.forecasts.get(p.event_ticker)
        model_probs: tuple[float, ...] | None = None
        fsd: float | None = None
        deficit: float | None = None
        if fp is not None:
            lead = fp.lead_hours if fp.lead_hours is not None else lead_hours_to_event(hours)
            fsd = forecast_error_sd(element, lead)
            model_probs = bucket_probabilities(p.cuts, fp.value_f, fsd)
            # Positive means the market claims to be sharper than we can be.
            deficit = fsd - implied.sd

        disagreement = (
            max(abs(a - b) for a, b in zip(model_probs, probs)) if model_probs else 0.0
        )
        return WeatherAssessment(
            event_ticker=p.event_ticker, series_ticker=p.series_ticker,
            element=element, hours_to_close=hours, market_probs=probs,
            implied=implied, forecast_f=fp.value_f if fp else None,
            forecast_sd=fsd, model_probs=model_probs,
            max_abs_disagreement=disagreement, sharpness_deficit_f=deficit,
        )

    def partitions(self, snapshot: MarketSnapshot) -> list[TempPartition]:
        by_event: dict[str, list[Market]] = {}
        for m in snapshot.markets:
            if m.series_ticker in SETTLEMENT_STATION:
                by_event.setdefault(m.event_ticker, []).append(m)
        out = []
        for ev, legs in sorted(by_event.items()):
            p = partition_from_markets(ev, legs[0].series_ticker, legs)
            if p is not None:
                out.append(p)
        return out

    # -- Sleeve protocol -------------------------------------------------- #
    def desired_state(self, snapshot: MarketSnapshot) -> DesiredState:
        """PURE.  Returns decisions for calibration and, always, ZERO quotes.

        The `quotes = ()` is not an oversight and not a config default.  There
        is no measured edge to trade (research/13 section 4), so a quote here
        would be a bet on an edge we looked for and did not find.
        """
        decisions: list[Decision] = []
        assessed = 0
        sharper = 0
        for p in self.partitions(snapshot):
            a = self.assess(p, snapshot)
            if a is None:
                continue
            assessed += 1
            if a.forecast_is_sharper:
                sharper += 1
            if a.model_probs is None:
                continue
            for bucket, market, q in zip(p.buckets, p.markets, a.model_probs):
                price = market.mid
                if price is None or not 0.0 < price < 1.0:
                    continue
                raw = edge(q, price, self.fee_spec, is_maker=False)
                decisions.append(
                    Decision(
                        ticker=bucket.ticker,
                        market_price=price,
                        p_model=q,
                        raw_edge=raw,
                        shrunk_edge=self.lam * raw,
                        acted=False,          # ALWAYS.  Nothing here is tradeable.
                        category=self.category,
                    )
                )
        return DesiredState(
            quotes=(),                        # ALWAYS.  See the module docstring.
            decisions=tuple(decisions),
            rationale={
                "sleeve": self.id,
                "mode": "research_harness",
                "edge_demonstrated": EDGE_DEMONSTRATED,
                "events_assessed": assessed,
                "events_where_our_forecast_is_sharper": sharper,
                "reason_no_quotes": (
                    "research/13: market implied sd 1.95 F at 24-36h beats the best "
                    "free forecast (2.19-2.38 F) at matched lead; taker hurdle is "
                    "0.17-0.22 F and the gap runs the wrong way"
                ),
                "settlements_to_decide": MEASURED_DISAGREEMENT_PP,
            },
        )
