"""No-arbitrage ACROSS events, not just within one.  T-060.

Every constraint tested elsewhere in this project lives inside a single event:
a MECE basket sums to $1, a ladder is monotone, a board of markets on one game
admits a consistent distribution (`rulebook.jointarb`). None of them can see an
inconsistency between two SEPARATE events that happen to price the same world.

Kalshi lists exactly such a pair for weather:

    KXHIGHLAX-26AUG26      "Will the maximum temperature be 84-85 deg?"
    KXTEMPLAXH-26AUG2616   "Will the temp in Los Angeles be above 83.99 deg
                            at 4pm EDT?"

Different events, different series, separate order books -- and bound together
by something that cannot fail: the day's MAXIMUM is at least the reading at any
hour inside that day. So for every threshold X,

    P(daily_max >= X)  >=  P(temp_at_4pm >= X)

and the mirror for the daily minimum, which is at most any hour's reading:

    P(daily_min <= X)  >=  P(temp_at_4pm <= X)

THE HEDGE
---------
The daily market is a MECE partition into buckets, so `daily_max >= X` is a
BASKET of buckets rather than one contract. Buy every bucket that overlaps
[X, inf) and sell the hourly:

    temp >= X   -> max >= X -> exactly one bought bucket pays $1, owe $1 -> 0
    temp <  X, max >= X     -> a bought bucket pays $1, owe nothing     -> +1
    max  <  X               -> temp <= max < X, nothing pays, owe none  ->  0

Payoff is never negative, so if the basket costs less than the hourly bid the
difference is riskless. Note the basket must include any bucket that STRADDLES
X: dropping it would leave the max-in-that-bucket state unhedged while the
hourly still pays, turning a riskless trade into a losing one.

WHAT WOULD MAKE A HIT SPURIOUS
------------------------------
The two series must read the same instrument. `KXHIGHNY` and `KXTEMPNYCH` are
both "New York" but a daily high from Central Park and an hourly from LaGuardia
are different numbers, and the implication would not hold. A violation found
here is a candidate to verify against the settlement rules, never a filled
trade on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

#: A bucket with no upper edge, used for the ">89 deg" tail.
INF = 10_000


@dataclass(frozen=True, slots=True)
class Bucket:
    """One leg of a daily high/low partition, in whole degrees, inclusive."""

    ticker: str
    lo: int
    hi: int
    bid_cents: int
    ask_cents: int


@dataclass(frozen=True, slots=True)
class Threshold:
    """One hourly leg: pays if the reading at that hour is >= `at_least`."""

    ticker: str
    at_least: int
    bid_cents: int
    ask_cents: int


@dataclass(frozen=True, slots=True)
class CrossViolation:
    city: str
    kind: str                       # "high" or "low"
    threshold: int
    hourly: str
    basket: tuple[str, ...]
    basket_cost_cents: int
    hourly_bid_cents: int

    @property
    def edge_cents(self) -> int:
        """Riskless cents per contract, before fees."""
        return self.hourly_bid_cents - self.basket_cost_cents


# --------------------------------------------------------------------------- #
# Titles, not tickers.  Ticker suffixes on these series encode the same number
# three different ways (B82.5, T82, T83.99); the prose says what it means.
# --------------------------------------------------------------------------- #
_RANGE = re.compile(r"be\s+(-?\d+)\s*-\s*(-?\d+)\s*.?\s*(?:deg|$|on)", re.I)
_BELOW = re.compile(r"be\s*<\s*(-?\d+)", re.I)
_ABOVE = re.compile(r"be\s*>\s*(-?\d+)", re.I)
_HOURLY = re.compile(r"above\s+(-?\d+)(?:\.(\d+))?\s*.?\s*(?:deg|on)", re.I)


def parse_bucket(title: str) -> tuple[int, int] | None:
    """`(lo, hi)` in whole degrees, inclusive, or None if not a bucket title."""
    m = _RANGE.search(title)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _BELOW.search(title)
    if m:
        return -INF, int(m.group(1)) - 1          # "<82" -> max is 81 or less
    m = _ABOVE.search(title)
    if m:
        return int(m.group(1)) + 1, INF           # ">89" -> max is 90 or more
    return None


def parse_threshold(title: str) -> int | None:
    """Smallest whole degree that satisfies "above 83.99 deg", i.e. 84."""
    m = _HOURLY.search(title)
    if not m:
        return None
    whole = int(m.group(1))
    # Readings are whole degrees, so "above 83.99" and "above 83" both mean 84.
    return whole + 1


# --------------------------------------------------------------------------- #
def covers_upward(buckets: Iterable[Bucket], at_least: int) -> bool:
    """Do these buckets TILE [at_least, inf) with no gap?

    The hedge is only riskless if every state where the daily market can settle
    at or above X is bought. A bucket that exists on the venue but is missing
    from this list -- unquoted at that minute, or simply not fetched -- leaves a
    hole, and the trade loses $1 whenever the day's max lands in it.

    This is the guard for the failure that produced 80 cent "arbitrage" here:
    a basket assembled only from whatever happened to be quoted looks cheap
    precisely because it is incomplete.
    """
    spans = sorted(((b.lo, b.hi) for b in buckets if b.hi >= at_least))
    if not spans:
        return False
    reach = min(spans[0][0], at_least)
    if reach > at_least:
        return False
    for lo, hi in spans:
        if lo > reach + 1:
            return False                      # gap between buckets
        reach = max(reach, hi)
    return reach >= INF


def covers_downward(buckets: Iterable[Bucket], at_most: int) -> bool:
    """Mirror of `covers_upward` for the daily minimum, tiling (-inf, at_most]."""
    spans = sorted(((b.lo, b.hi) for b in buckets if b.lo <= at_most),
                   reverse=True)
    if not spans:
        return False
    reach = max(spans[0][1], at_most)
    if reach < at_most:
        return False
    for lo, hi in spans:
        if hi < reach - 1:
            return False
        reach = min(reach, lo)
    return reach <= -INF


def check_high(city: str, buckets: Iterable[Bucket], hourly: Iterable[Threshold]
               ) -> list[CrossViolation]:
    """Daily maximum against one hour's reading.

    For each hourly threshold, the hedging basket is every bucket that overlaps
    [X, inf). Buying it costs the sum of the asks we would actually pay.
    """
    buckets = list(buckets)
    out: list[CrossViolation] = []
    for h in hourly:
        basket = [b for b in buckets if b.hi >= h.at_least]
        if not covers_upward(basket, h.at_least):
            continue                          # incomplete hedge, not an edge
        cost = sum(b.ask_cents for b in basket)
        if h.bid_cents > cost:
            out.append(CrossViolation(city, "high", h.at_least, h.ticker,
                                      tuple(b.ticker for b in basket),
                                      cost, h.bid_cents))
    return out


def check_low(city: str, buckets: Iterable[Bucket], hourly: Iterable[Threshold]
              ) -> list[CrossViolation]:
    """Daily minimum against one hour's reading.

    The daily minimum is at most any hour's reading, so `min <= X - 1` is
    implied by `temp <= X - 1`, which is the NO side of the hourly. Selling
    that NO earns `100 - ask` rather than the YES bid.
    """
    buckets = list(buckets)
    out: list[CrossViolation] = []
    for h in hourly:
        cutoff = h.at_least - 1                   # temp <= cutoff is the NO leg
        basket = [b for b in buckets if b.lo <= cutoff]
        if not covers_downward(basket, cutoff):
            continue                          # incomplete hedge, not an edge
        cost = sum(b.ask_cents for b in basket)
        no_bid = 100 - h.ask_cents
        if no_bid > cost:
            out.append(CrossViolation(city, "low", cutoff, h.ticker,
                                      tuple(b.ticker for b in basket),
                                      cost, no_bid))
    return out


# --------------------------------------------------------------------------- #
# The same idea in sport.  A knockout cup tie is priced as two events:
#
#     KXEFLCUPGAME-26AUG26BRABUR-{BRA,BUR,TIE}   the 90 minute result
#     KXEFLCUPADVANCE-26AUG26BRABUR-{BRA,BUR}    who goes through
#
# A draw after 90 minutes goes to extra time or penalties, so exactly one side
# still advances. That gives a two-sided sandwich on every team:
#
#     P(win in 90)  <=  P(advance)  <=  P(win in 90) + P(draw)
#
# The left half is "winning the match means going through". The right half is
# "you cannot go through if you lost in regulation".
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class TieLegs:
    """One team's quotes on both events, in cents."""

    team: str
    win_bid: int
    win_ask: int
    draw_bid: int
    draw_ask: int
    adv_bid: int
    adv_ask: int


def check_advance(fixture: str, legs: Iterable[TieLegs]) -> list[CrossViolation]:
    """Both halves of the sandwich, priced at the touch we would really trade."""
    out: list[CrossViolation] = []
    for g in legs:
        # advance is too CHEAP against the win: buy advance, sell the win.
        if g.win_bid > g.adv_ask:
            out.append(CrossViolation(fixture, "advance-under", 0,
                                      f"{g.team}:win", (f"{g.team}:adv",),
                                      g.adv_ask, g.win_bid))
        # advance is too DEAR against win-or-draw: buy both results, sell it.
        hedge = g.win_ask + g.draw_ask
        if g.adv_bid > hedge:
            out.append(CrossViolation(fixture, "advance-over", 0,
                                      f"{g.team}:adv",
                                      (f"{g.team}:win", "draw"),
                                      hedge, g.adv_bid))
    return out
