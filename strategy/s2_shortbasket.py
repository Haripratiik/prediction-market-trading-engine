"""S2 -- the intra-event Dutch book, and it is a SHORT sleeve.

THE ASYMMETRY THAT DEFINES THIS SLEEVE (PLAN.md 3.2, research/06 section 2.2)
-----------------------------------------------------------------------------
Kalshi's `mutually_exclusive` flag guarantees AT MOST ONE leg resolves YES.  It
does NOT guarantee at least one does, and there is no exhaustiveness field
anywhere in the API.  So the two directions are not mirror images:

    BUY  the basket  (pay sum(ask), collect $1 if a listed leg wins)
        -> UNSAFE.  Returns $0 when the winner is unlisted.  Requires an
           INDEPENDENTLY VERIFIED exhaustiveness verdict, never the exchange
           flag.  Measured: 33 flagged-MECE events price below sum(ask) = 0.90
           with apparent margins up to +87c, and NONE are arbitrage.  Of the 47
           fee-profitable taker structures found live, 33 were this trap
           (research/05 F1).

    SELL the basket  (collect sum(bid), pay AT MOST $1)
        -> SAFE.  Liability is capped at $1 by mutual exclusivity alone, and
           non-exhaustiveness makes the trade BETTER: an unlisted winner leaves
           every leg worthless and the whole premium is kept.

The short side is also where the density is: median sum(ask) = 1.15 against
median sum(bid) = 0.88.  Books are overround, and overround is collected by
SELLING.  Hence: S2's primary direction is SHORT, and the long basket is off by
default behind a VERIFIED gate that nothing currently satisfies.

WHY PARTIAL FILLS ARE SURVIVABLE HERE (research/06 section 2.4)
---------------------------------------------------------------
Selling k of N legs leaves a short YES position on a subset.  Max liability is
STILL $1 -- only one leg can win -- and the k premiums are already collected.
Worst case is `$1 - premium_collected`, i.e. an ordinary sold-longshot outcome,
not a wipeout.  Contrast the long basket, where a single unfilled leg destroys
the structure and converts locked arbitrage into a naked directional bet.

This is also why S2-short and S1 are the same edge: selling overpriced YES on
longshot legs IS the favourite-longshot trade, executed at basket granularity
with the MECE structure supplying a hard $1 liability cap per event.

MAKER-FIRST, AND WHAT "RESTABLE" MEANS
--------------------------------------
Correction C2: the maker window is ~4 points wider than the taker window, which
is the difference between "almost never" and "regularly".  So the sleeve rests
rather than crosses.  But a leg with NO bid is not restable -- pricing it as if
you could rest at 1c on a market nobody bids is the liquidity fantasy that made
a naive scan report 78% of MECE events as profitable (research/05 4.3).  That
condition lives in `rulebook.exhaustiveness.check_mece` and is consumed here
through `MeceCheck.safe_to_sell`.

n == 2 IS NOT ARBITRAGE
-----------------------
For a two-outcome event a "maker Dutch book" is resting inside the spread on
both sides.  The margin is realised only if BOTH legs fill, so it carries
both-fill risk: that is market making, sleeve S6, and it is tagged as such here
and never counted as S2 arbitrage.  Genuine S2 begins at n >= 3 (research/05
4.4: 357 of 504 liquidity-filtered maker structures are n == 2).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from core.math.contracts import KALSHI_MAKER_RATIO, FeeSpec, edge, fee
from core.math.portfolio import (
    MutuallyExclusiveKelly,
    dutch_book_fee_hurdle,
    dutch_book_margin,
    short_basket_margin,
)
from core.math.sizing import KELLY_MULTIPLE, LAMBDA_DEFAULT, position_fraction
from core.models import Event, Market, Side
from rulebook.exhaustiveness import MeceCheck, check_mece
from strategy.base import Decision, DesiredQuote, DesiredState, MarketSnapshot

TICK_CENTS: int = 1

# research/05 4.4: below this leg count a "Dutch book" is two-sided quoting.
MIN_ARBITRAGE_LEGS: int = 3

# The reference book total at which research/05 4.5 tabulates the fee hurdles.
# Reproducing that table exactly is a regression test, so it is a named constant.
DUTCH_BOOK_REFERENCE_TOTAL: float = 0.97

# A leg whose series is missing from the snapshot is priced PESSIMISTICALLY.
# 130 of 13,486 series charge maker fees; assuming the free 99% for a series we
# have not actually looked up is exactly the optimism that turns a 0.5c margin
# into a loss (research/06 section 4).
UNKNOWN_SERIES_FEE_SPEC: FeeSpec = FeeSpec.kalshi("quadratic_with_maker_fees", 1.0)

MeceChecker = Callable[[Event, list[Market]], MeceCheck]


class Direction(StrEnum):
    SHORT = "short"                  # rest asks, collect the overround.  PRIMARY.
    LONG = "long"                    # rest bids, needs VERIFIED exhaustiveness.
    MARKET_MAKING = "market_making"  # n == 2: both-fill risk, accounted to S6.


# --------------------------------------------------------------------------- #
# Economics.  Thin wrappers that name the hurdle in the units the sleeve thinks
# in, so the fee table of research/05 4.5 is directly assertable.
# --------------------------------------------------------------------------- #
def max_sum_px_for_long(
    n_outcomes: int,
    spec: FeeSpec,
    *,
    is_maker: bool,
    book_total: float = DUTCH_BOOK_REFERENCE_TOTAL,
) -> float:
    """Largest `sum(px)` at which BUYING all N legs still profits.

    research/05 4.5, n = 5: 0.9453 as taker, 0.9863 as maker.  Note the maker
    column of that table is computed for `quadratic_with_maker_fees` (0.25x
    base); on the plain `quadratic` fee type that ~99% of series carry, makers
    pay ZERO and the hurdle is 1.0 exactly.
    """
    return 1.0 - dutch_book_fee_hurdle(n_outcomes, spec, is_maker=is_maker,
                                       book_total=book_total)


def min_sum_px_for_short(
    n_outcomes: int,
    spec: FeeSpec,
    *,
    is_maker: bool,
    book_total: float = 1.0,
) -> float:
    """Smallest `sum(px)` at which SELLING all N legs still profits.

    The mirror of `max_sum_px_for_long`.  Fees are referenced at a book total of
    1.0 rather than 0.97 because a short candidate by construction sits at or
    above par, so that is where its legs actually price.
    """
    return 1.0 + dutch_book_fee_hurdle(n_outcomes, spec, is_maker=is_maker,
                                       book_total=book_total)


def locked_capital(prices: Sequence[float], *, direction: Direction,
                   netted: bool) -> float:
    """Dollars tied up per basket until settlement.

    LONG: you pay for every leg, so `sum(px)`.

    SHORT un-netted: a short YES at p posts (1 - p) of collateral per leg, so
    `n - sum(px)`.  SHORT netted: MECNET assesses collateral on the worst-case
    event outcome, and only one leg can pay, so it collapses to `1 - sum(px)`.

    THE NETTED FORMULA IS NOW DOCUMENTED BY KALSHI, not merely inferred.
    Kalshi's "Collateral Return" help article states the rule as

        collateral = total invested - guaranteed payout

    and gives a worked example: NO at 60c plus NO at 70c invests $1.30, at least
    one of which must pay $1, so collateral is **$0.30, not $1.30**.  For k legs
    of a mutually-exclusive set, at most one can resolve YES, so at least k-1 of
    the NO legs pay:

        invested          = sum_i (1 - p_i) = k - sum(px)
        guaranteed payout = k - 1
        collateral        = (k - sum(px)) - (k - 1) = 1 - sum(px)

    which is exactly the expression below.  Note the consequence: on a basket
    that is actually profitable (`sum(px) > 1`) the collateral floors to ZERO --
    capital stops being the binding constraint entirely, and joint fill
    probability becomes the only thing that matters.

    *** AND THE NETTING CARRIES A TRAP THAT MAY BE FATAL TO THIS SLEEVE. ***

    The same article warns, verbatim: "Enabling this feature may make you unable
    to sell positions for which you've already had collateral returned," and
    "Once this is initialized, there is no way to retroactively enable or
    disable collateral return for a given event."  The setting latches "at the
    first moment a user places their first order in a given event" -- BEFORE any
    trade fills, so it cannot be probed by testing the water.

    Read that against this sleeve's measured behaviour: 69.8% of fill episodes
    are ORPHANS, and an orphan's only remedy is to SELL the filled legs.  A
    netting mode that can prevent selling removes the exit from the failure mode
    that happens seven times out of ten, converting a hedged 3c arbitrage into a
    naked directional position held to settlement at up to $1 per contract.

    So the two effects point in OPPOSITE directions and must not be traded off
    casually: netting takes collateral to zero (good, unbounded ROLC) and may
    take the unwind path with it (potentially ruinous).  Establishing which
    positions become unsellable, and when, is a HARD PREREQUISITE to enabling
    `assume_mecnet_netting` -- not a sizing refinement.

    Un-netted remains the DEFAULT anyway, because the article is documentation
    and not a measurement against this account.  Over-stating collateral only
    ever sizes us DOWN; under-stating it could over-size into a margin call, so
    the conservative direction is the correct default until one real fill
    confirms it via `positions()`'s `event_exposure_dollars`.  Flipping
    `S2Config.assume_mecnet_netting` is then a one-line change.
    """
    total = sum(prices)
    if direction is Direction.LONG:
        return total
    if netted:
        return max(0.0, 1.0 - total)
    return len(prices) - total


def annualized_rolc(margin: float, capital: float, days: float) -> float:
    """Annualised return on locked capital.  PLAN.md 3.2 / section 9.

    A locked structure is near-riskless, so the binding question is not "is the
    edge real" but "is this the best use of the cents until settlement".  A
    +0.5c margin held for a year is worse than leaving the capital in S1.

    PLAN.md writes this as `margin / days * 365 / avg_price`; `avg_price` there
    is the per-leg shorthand for the same quantity this computes exactly, as
    total capital actually locked by the whole basket.
    """
    if days <= 0.0:
        raise ValueError("days must be positive")
    if capital <= 0.0:
        return math.inf          # nothing tied up: the return is unbounded
    return (margin / capital) * (365.0 / days)


def devigged_probs(mids: Sequence[float]) -> tuple[float, ...]:
    """Consensus probabilities implied by the book with the overround removed.

    The normalisation is what makes the legs comparable: on a 14%-overround book
    every leg looks rich in absolute terms, and only the RELATIVE richness says
    which legs to sell first.
    """
    total = sum(mids)
    if total <= 0.0:
        raise ValueError("mids must sum to a positive number")
    return tuple(m / total for m in mids)


def rest_price_short_cents(m: Market) -> int:
    """Where to rest a YES ask.  Improve only when the spread allows it.

    Mirror of the long-side rule in PLAN.md 3.2: step inside by one tick when
    the spread is wider than a tick, otherwise join the ask.  Stepping inside a
    1c spread would cross the bid, which post-only would reject anyway.
    """
    assert m.yes_bid is not None and m.yes_ask is not None
    px = m.yes_ask - TICK_CENTS if (m.yes_ask - m.yes_bid) > TICK_CENTS else m.yes_ask
    return min(99, max(1, px))


def rest_price_long_cents(m: Market) -> int:
    """Where to rest a YES bid.  PLAN.md 3.2 `s2_scan`."""
    assert m.yes_bid is not None and m.yes_ask is not None
    px = m.yes_bid + TICK_CENTS if (m.yes_ask - m.yes_bid) > TICK_CENTS else m.yes_bid
    return min(99, max(1, px))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class S2Config:
    # structure shape
    min_legs: int = MIN_ARBITRAGE_LEGS
    max_legs: int = 40
    # economics
    min_margin: float = 0.005          # $/contract.  PLAN.md 3.2 MIN_MARGIN.
    # liquidity filter -- research/05 4.3, the filter that took 3,793 maker
    # "opportunities" down to 504 real ones
    min_leg_depth: float = 20.0        # contracts at the touch
    max_leg_spread_cents: int = 10
    min_hours_to_close: float = 1.0
    min_volume_24h: float = 1.0
    # sizing.  PLAN.md section 9 `structures:`
    max_depth_fraction: float = 0.20   # max_resting_fraction_of_touch_depth
    max_structure_fraction: float = 0.05
    max_sleeve_fraction: float = 0.15
    min_rolc: float = 0.15
    assume_mecnet_netting: bool = False   # research/05 F4: unverified
    # unlocked / partial sizing
    lam: float = LAMBDA_DEFAULT
    kelly_mult: float = KELLY_MULTIPLE
    # OFF by default.  The partial path is explicitly directional -- it sells a
    # SUBSET of a mutually-exclusive set, which pays only if the winner falls
    # outside the subset, and is sized by Kelly rather than by a locked margin.
    # That is a legitimate strategy and it is NOT arbitrage, so it does not
    # belong on by default in a book whose stated purpose is arbitrage.
    #
    # It also publishes `margin = sum(px) - 1`, which is NEGATIVE by construction
    # on this path (a partial basket sums below 1), and that value is copied
    # into `structures.target_margin_cents` -- where KPI 6 counts only positive
    # targets in its denominator, so a directional structure silently vanishes
    # from the orphan-loss ratio while still able to contribute losses to it.
    #
    # Measured: with live quotes and this ON, the only structure S2 opened was a
    # 6-leg WNBA short at sum_px = 0.91, i.e. a -10.1c/contract "margin".
    allow_partial: bool = False
    # the long basket is a separate, far more dangerous trade
    allow_long_basket: bool = False
    # execution policy handed to the executor via the rationale.  PLAN.md 3.2.
    leg_timeout_seconds: int = 900
    completion_taker_threshold: float = 0.6
    max_orphan_exposure_fraction: float = 0.005
    max_structures: int = 20


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Structure:
    """One multi-leg intent.  The `structure_id` is what the executor tracks."""

    structure_id: str
    event_ticker: str
    direction: Direction
    locked: bool
    legs: tuple[str, ...]              # tickers, in quote order
    prices_cents: tuple[int, ...]      # YES-REFERENCED resting prices
    size: int
    sum_px: float
    gross: float
    fees: float
    margin: float
    hurdle_sum_px: float
    capital_per_basket: float
    days_to_settle: float
    rolc: float
    mece: MeceCheck
    reasons: tuple[str, ...] = ()

    @property
    def n_legs(self) -> int:
        return len(self.legs)

    @property
    def is_arbitrage(self) -> bool:
        """True only for a locked n >= 3 basket.  n == 2 is never arbitrage."""
        return (
            self.locked
            and self.n_legs >= MIN_ARBITRAGE_LEGS
            and self.direction is not Direction.MARKET_MAKING
        )

    @property
    def is_market_making(self) -> bool:
        return self.direction is Direction.MARKET_MAKING

    @property
    def worst_case_partial(self) -> float:
        """Worst-case loss per basket if only ONE short leg ever fills.

        `$1 - premium_collected`, taking the least premium available -- an
        ordinary sold-longshot outcome (research/06 2.4).  Undefined (0.0) for
        the long basket, where an unfilled leg destroys the structure instead.
        """
        if self.direction is Direction.LONG or not self.prices_cents:
            return 0.0
        return 1.0 - min(self.prices_cents) / 100.0


def _structure_id(event_ticker: str, direction: Direction,
                  legs: Sequence[tuple[str, int]]) -> str:
    """Deterministic id.  NOT a uuid: C4.2a forbids unseeded randomness, and a
    backtest replaying the same snapshot must produce the same id."""
    body = "|".join(f"{t}@{p}" for t, p in legs)
    digest = hashlib.sha256(f"{event_ticker}:{direction.value}:{body}".encode()).hexdigest()
    return f"S2-{direction.value}-{event_ticker}-{digest[:8]}"


# --------------------------------------------------------------------------- #
# The sleeve
# --------------------------------------------------------------------------- #
@dataclass
class S2ShortBasket:
    """Rest asks across a mutually-exclusive outcome set and collect the overround."""

    id: str = "S2"
    gate: int = 2
    cfg: S2Config = field(default_factory=S2Config)
    # Injectable so the VERIFIED long path is testable.  Defaults to the real
    # gate, which today can never return VERIFIED -- see `_evaluate_long`.
    mece_check: MeceChecker = field(default=check_mece)

    # ------------------------------------------------------------------- fees
    def fee_spec(self, legs: Sequence[Market], snapshot: MarketSnapshot) -> FeeSpec:
        """The most expensive fee treatment across the legs.

        One spec governs the whole structure because the basket is priced as a
        unit; taking the worst leg's treatment can only understate the margin.
        """
        specs = [
            (snapshot.series_for(m).fee_spec if snapshot.series_for(m)
             else UNKNOWN_SERIES_FEE_SPEC)
            for m in legs
        ]
        if not specs:
            return UNKNOWN_SERIES_FEE_SPEC
        return max(
            specs,
            key=lambda s: (KALSHI_MAKER_RATIO.get(s.fee_type, 1.0) * s.fee_multiplier,
                           s.fee_multiplier),
        )

    # -------------------------------------------------------------- liquidity
    def leg_is_liquid(self, m: Market, snapshot: MarketSnapshot) -> tuple[bool, str]:
        """research/05 4.3.  Bid depth is the reference even though we rest an
        ASK: the buyers are what a seller actually gets lifted by."""
        if m.yes_bid_size < self.cfg.min_leg_depth:
            return False, "leg depth below minimum"
        spread = m.spread_cents
        if spread is None or spread > self.cfg.max_leg_spread_cents:
            return False, "leg spread too wide"
        hrs = m.hours_to_close(now=snapshot.now_us)
        if hrs is None:
            return False, "leg has no close time"
        if hrs < self.cfg.min_hours_to_close:
            return False, "leg inside the final hour"
        if m.volume_24h < self.cfg.min_volume_24h:
            return False, "leg has no recent volume"
        return True, ""

    # --------------------------------------------------------------- evaluate
    def evaluate(
        self, event: Event, markets: Sequence[Market], snapshot: MarketSnapshot
    ) -> Structure | None:
        """Judge one event.  Returns None when it is not a candidate at all."""
        check = self.mece_check(event, list(markets))

        # THE GATE.  `safe_to_sell` already carries mutual exclusivity plus a
        # real bid on every leg; `safe_to_buy` requires a VERIFIED exhaustiveness
        # verdict, which the exchange flag never supplies.
        if not check.safe_to_sell:
            return None

        legs = [m for m in markets if m.has_two_sided_quote]
        if len(legs) != check.n_legs:
            # A locked or crossed leg (ask <= bid) means the book the gate saw is
            # not the book we would price against.  Refuse rather than reconcile.
            return None

        n = len(legs)
        if n == 2:
            return self._market_making_stub(event, legs, snapshot, check)
        if n < self.cfg.min_legs or n > self.cfg.max_legs:
            return None

        short = self._evaluate_short(event, legs, snapshot, check)
        if short is not None:
            return short
        return self._evaluate_long(event, legs, snapshot, check)

    # ---------------------------------------------------------------- n == 2
    def _market_making_stub(
        self, event: Event, legs: Sequence[Market], snapshot: MarketSnapshot,
        check: MeceCheck,
    ) -> Structure:
        """A two-outcome event, priced but NEVER quoted by S2.

        Its margin is real but conditional on BOTH legs filling, which makes it
        two-sided quoting rather than locked arbitrage.  It is surfaced with
        size 0 so S6 can pick it up and so the count is auditable.
        """
        spec = self.fee_spec(legs, snapshot)
        px = [rest_price_short_cents(m) for m in legs]
        dollars = [p / 100.0 for p in px]
        margin = short_basket_margin(dollars, spec, is_maker=True)
        return Structure(
            structure_id=_structure_id(
                event.event_ticker, Direction.MARKET_MAKING,
                [(m.ticker, p) for m, p in zip(legs, px, strict=True)],
            ),
            event_ticker=event.event_ticker,
            direction=Direction.MARKET_MAKING,
            locked=False,
            legs=tuple(m.ticker for m in legs),
            prices_cents=tuple(px),
            size=0,
            sum_px=sum(dollars),
            gross=sum(dollars) - 1.0,
            fees=sum(dollars) - 1.0 - margin,
            margin=margin,
            hurdle_sum_px=min_sum_px_for_short(2, spec, is_maker=True),
            capital_per_basket=locked_capital(
                dollars, direction=Direction.SHORT,
                netted=self.cfg.assume_mecnet_netting),
            days_to_settle=self._days_to_settle(legs, snapshot) or 0.0,
            rolc=0.0,
            mece=check,
            reasons=("n == 2 is two-sided quoting with both-fill risk, "
                     "accounted to S6 and never to S2 (PLAN.md 3.2)",),
        )

    # ----------------------------------------------------------------- SHORT
    def _evaluate_short(
        self, event: Event, legs: Sequence[Market], snapshot: MarketSnapshot,
        check: MeceCheck,
    ) -> Structure | None:
        spec = self.fee_spec(legs, snapshot)
        n = len(legs)
        px = [rest_price_short_cents(m) for m in legs]
        dollars = [p / 100.0 for p in px]
        liquid = [self.leg_is_liquid(m, snapshot)[0] for m in legs]
        days = self._days_to_settle(legs, snapshot)

        # --- locked basket: every leg restable, liquid, and the sum clears ---
        if all(liquid) and days is not None:
            margin = short_basket_margin(dollars, spec, is_maker=True)
            hurdle = min_sum_px_for_short(n, spec, is_maker=True)
            if margin >= self.cfg.min_margin:
                capital = locked_capital(
                    dollars, direction=Direction.SHORT,
                    netted=self.cfg.assume_mecnet_netting)
                rolc = annualized_rolc(margin, capital, days)
                if rolc < self.cfg.min_rolc:
                    return Structure(
                        structure_id=_structure_id(
                            event.event_ticker, Direction.SHORT,
                            [(m.ticker, p) for m, p in zip(legs, px, strict=True)]),
                        event_ticker=event.event_ticker,
                        direction=Direction.SHORT, locked=True,
                        legs=tuple(m.ticker for m in legs),
                        prices_cents=tuple(px), size=0,
                        sum_px=sum(dollars), gross=sum(dollars) - 1.0,
                        fees=sum(dollars) - 1.0 - margin, margin=margin,
                        hurdle_sum_px=hurdle, capital_per_basket=capital,
                        days_to_settle=days, rolc=rolc, mece=check,
                        reasons=(f"ROLC {rolc:.3f} below {self.cfg.min_rolc:.2f}: "
                                 "the capital is better used by S1",),
                    )
                size = self._locked_size(legs, capital, snapshot)
                return Structure(
                    structure_id=_structure_id(
                        event.event_ticker, Direction.SHORT,
                        [(m.ticker, p) for m, p in zip(legs, px, strict=True)]),
                    event_ticker=event.event_ticker,
                    direction=Direction.SHORT, locked=True,
                    legs=tuple(m.ticker for m in legs),
                    prices_cents=tuple(px), size=size,
                    sum_px=sum(dollars), gross=sum(dollars) - 1.0,
                    fees=sum(dollars) - 1.0 - margin, margin=margin,
                    hurdle_sum_px=hurdle, capital_per_basket=capital,
                    days_to_settle=days, rolc=rolc, mece=check,
                    reasons=() if size > 0 else ("size floored to zero by depth "
                                                 "or capital cap",),
                )

        # --- partial / unlocked: directional, so Kelly governs, not depth ---
        if not self.cfg.allow_partial or days is None:
            return None
        return self._evaluate_partial_short(event, legs, snapshot, check, spec,
                                            px, liquid, days)

    def _evaluate_partial_short(
        self, event: Event, legs: Sequence[Market], snapshot: MarketSnapshot,
        check: MeceCheck, spec: FeeSpec, px: Sequence[int],
        liquid: Sequence[bool], days: float,
    ) -> Structure | None:
        """Sell the legs the market prices richest, sized by Kelly.

        WHICH legs: `MutuallyExclusiveKelly.solve` greedily admits outcomes whose
        `pi/p` beats the reservation rate, and it is generous -- verified surprise
        (1) is that it BUYS negative-EV outcomes as hedges.  So the legs it
        DECLINES are strictly the overpriced ones, and its complement is exactly
        the set worth selling.  Note the degenerate cases are both correct: on a
        book de-vigged from its own prices every leg is declined when sum > 1
        (sell the lot) and every leg is admitted when sum < 1 (sell nothing) --
        the latter being the F1 trap, which the exhaustiveness gate has already
        stopped from being BOUGHT.

        HOW MUCH: a short of subset K is exactly ONE binary bet.  It pays
        `sum_K(px)` unless the winner is in K, in which case it pays
        `sum_K(px) - 1`.  So it is a long NO at price `1 - sum_K(px)` with model
        probability `1 - sum_K(pi)`, and the canonical sizer applies unchanged
        (I2: never bypass `position_fraction`).
        """
        mids = [m.mid for m in legs]
        if any(v is None for v in mids):
            return None
        pi = devigged_probs([v for v in mids if v is not None])
        dollars = [p / 100.0 for p in px]

        mek = MutuallyExclusiveKelly.solve(prices=list(dollars), probs=list(pi))
        rich = [
            i for i in range(len(legs))
            if i not in mek.bet_set and liquid[i]
        ]
        if len(rich) < self.cfg.min_legs:
            return None

        sub_px = [dollars[i] for i in rich]
        sub_pi = sum(pi[i] for i in rich)
        p_k = sum(sub_px)
        if not 0.0 < p_k < 1.0:
            return None    # p_k >= 1 would have been locked above

        # Fold every leg's fee into the aggregate price.  `position_fraction`
        # charges one fee at `price`, so the surcharge is the REST of the legs'
        # fees; without it a 9-leg partial is priced as if it paid one fee.
        no_price = 1.0 - p_k
        leg_fees = sum(fee(p, spec, is_maker=True) for p in sub_px)
        surcharge = max(0.0, leg_fees - fee(no_price, spec, is_maker=True))
        eff_price = no_price + surcharge
        if not 0.0 < eff_price < 1.0:
            return None

        frac = position_fraction(
            1.0 - sub_pi, eff_price, spec, is_maker=True,
            lam=self.cfg.lam, kelly_mult=self.cfg.kelly_mult,
            cap=self.cfg.max_structure_fraction,
        )
        if frac <= 0.0:
            return None

        margin = short_basket_margin(sub_px, spec, is_maker=True) \
            if len(sub_px) >= 2 else 0.0
        capital = locked_capital(sub_px, direction=Direction.SHORT,
                                 netted=self.cfg.assume_mecnet_netting)
        # Two independent caps: Kelly bounds the RISK (loss per basket is
        # 1 - p_k), the collateral bounds the LOCKUP.  Both bind.
        risk_cents = max(1, round(no_price * 100.0))
        cap_cents = max(1, round(capital * 100.0))
        budget = int(snapshot.bankroll_cents * frac)
        size = min(budget // risk_cents,
                   int(snapshot.bankroll_cents * self.cfg.max_structure_fraction)
                   // cap_cents)
        depth_cap = int(min(legs[i].yes_bid_size for i in rich)
                        * self.cfg.max_depth_fraction)
        size = max(0, min(size, depth_cap))

        sub_legs = [legs[i] for i in rich]
        sub_cents = [px[i] for i in rich]
        return Structure(
            structure_id=_structure_id(
                event.event_ticker, Direction.SHORT,
                [(m.ticker, p) for m, p in zip(sub_legs, sub_cents, strict=True)]),
            event_ticker=event.event_ticker,
            direction=Direction.SHORT,
            locked=False,
            legs=tuple(m.ticker for m in sub_legs),
            prices_cents=tuple(sub_cents),
            size=size,
            sum_px=p_k,
            gross=p_k - sub_pi,
            fees=leg_fees,
            margin=margin,
            hurdle_sum_px=min_sum_px_for_short(max(2, len(sub_px)), spec,
                                               is_maker=True),
            capital_per_basket=capital,
            days_to_settle=days,
            rolc=0.0,      # not locked: the return is not a certainty to annualise
            mece=check,
            reasons=(f"partial short: {len(rich)} of {len(legs)} legs are rich "
                     "relative to the de-vigged book",),
        )

    # ------------------------------------------------------------------ LONG
    def _evaluate_long(
        self, event: Event, legs: Sequence[Market], snapshot: MarketSnapshot,
        check: MeceCheck,
    ) -> Structure | None:
        """Buying the basket.  Blocked unless exhaustiveness is VERIFIED.

        `check_mece` never returns VERIFIED today -- its best verdict is
        NEEDS_HUMAN, because condition 4 (identical void clauses) needs a human
        to read the rules text.  That is deliberate and this path is therefore
        dead by construction until a human verdict exists.  It is written out
        anyway so that the gate is a gate and not an absence.
        """
        if not self.cfg.allow_long_basket or not check.safe_to_buy:
            return None
        if not all(self.leg_is_liquid(m, snapshot)[0] for m in legs):
            return None
        days = self._days_to_settle(legs, snapshot)
        if days is None:
            return None

        spec = self.fee_spec(legs, snapshot)
        n = len(legs)
        px = [rest_price_long_cents(m) for m in legs]
        dollars = [p / 100.0 for p in px]
        margin = dutch_book_margin(dollars, spec, is_maker=True)
        if margin < self.cfg.min_margin:
            return None

        capital = locked_capital(dollars, direction=Direction.LONG, netted=False)
        rolc = annualized_rolc(margin, capital, days)
        if rolc < self.cfg.min_rolc:
            return None
        size = self._locked_size(legs, capital, snapshot)
        return Structure(
            structure_id=_structure_id(
                event.event_ticker, Direction.LONG,
                [(m.ticker, p) for m, p in zip(legs, px, strict=True)]),
            event_ticker=event.event_ticker,
            direction=Direction.LONG, locked=True,
            legs=tuple(m.ticker for m in legs),
            prices_cents=tuple(px), size=size,
            sum_px=sum(dollars), gross=1.0 - sum(dollars),
            fees=1.0 - sum(dollars) - margin, margin=margin,
            hurdle_sum_px=max_sum_px_for_long(n, spec, is_maker=True),
            capital_per_basket=capital, days_to_settle=days, rolc=rolc,
            mece=check,
        )

    # ---------------------------------------------------------------- sizing
    def _locked_size(self, legs: Sequence[Market], capital: float,
                     snapshot: MarketSnapshot) -> int:
        """Depth and capital lockup, NOT Kelly.

        A locked structure is near-riskless, so a growth-optimal fraction is the
        wrong question -- there is no variance to trade off against.  What binds
        is how much of the touch you can rest into and how much of the bankroll
        the basket immobilises (PLAN.md 3.2, section 9).
        """
        depth_cap = int(min(m.yes_bid_size for m in legs) * self.cfg.max_depth_fraction)
        cap_cents = max(1, round(capital * 100.0))
        budget = int(snapshot.bankroll_cents * self.cfg.max_structure_fraction)
        return max(0, min(depth_cap, budget // cap_cents))

    @staticmethod
    def _days_to_settle(legs: Sequence[Market], snapshot: MarketSnapshot) -> float | None:
        hours = [m.hours_to_close(now=snapshot.now_us) for m in legs]
        if any(h is None for h in hours):
            return None
        soonest = min(h for h in hours if h is not None)
        return max(soonest / 24.0, 1.0 / 24.0)     # floor at an hour

    # ----------------------------------------------------------- desired_state
    def desired_state(self, snapshot: MarketSnapshot) -> DesiredState:
        by_event: dict[str, list[Market]] = {}
        for m in snapshot.markets:
            if m.event_ticker:
                by_event.setdefault(m.event_ticker, []).append(m)

        structures: list[Structure] = []
        skipped: dict[str, int] = {}
        for ticker in sorted(by_event):
            event = snapshot.events.get(ticker)
            if event is None:
                skipped["no event metadata"] = skipped.get("no event metadata", 0) + 1
                continue
            s = self.evaluate(event, by_event[ticker], snapshot)
            if s is None:
                skipped["not a candidate"] = skipped.get("not a candidate", 0) + 1
                continue
            structures.append(s)

        # Deterministic ordering: richest margin first, id as the tiebreak.
        structures.sort(key=lambda s: (-s.margin, s.structure_id))

        quotes: list[DesiredQuote] = []
        decisions: list[Decision] = []
        committed_cents = 0
        sleeve_budget = int(snapshot.bankroll_cents * self.cfg.max_sleeve_fraction)
        emitted: list[Structure] = []
        market_making: list[str] = []

        for s in structures:
            if s.is_market_making:
                market_making.append(s.event_ticker)
            tradeable = (
                not s.is_market_making
                and s.size > 0
                and len(emitted) < self.cfg.max_structures
            )
            cost = int(round(s.capital_per_basket * 100.0)) * s.size
            if tradeable and committed_cents + cost > sleeve_budget:
                tradeable = False       # section 9: 15% of bankroll in S2 total

            decisions.extend(self._decisions_for(s, snapshot, acted=tradeable))
            if not tradeable:
                continue
            committed_cents += cost
            emitted.append(s)
            quotes.extend(self._quotes_for(s, snapshot))

        return DesiredState(
            quotes=tuple(quotes),
            decisions=tuple(decisions),
            rationale={
                "sleeve": self.id,
                "events_considered": len(by_event),
                "structures_found": len(structures),
                "structures_quoted": len(emitted),
                "locked_arbitrage": sum(1 for s in emitted if s.is_arbitrage),
                "partial_directional": sum(1 for s in emitted if not s.locked),
                # n == 2 belongs to S6.  Reported, never traded here.
                "market_making_candidates": tuple(sorted(market_making)),
                "committed_cents": committed_cents,
                "sleeve_budget_cents": sleeve_budget,
                "skipped": skipped,
            },
        )

    # --------------------------------------------------------------- emission
    def _quotes_for(self, s: Structure,
                    snapshot: MarketSnapshot) -> list[DesiredQuote]:
        """One DesiredQuote per leg, all carrying the SAME `structure_id`.

        PRICE CONVENTION: `price_cents` is YES-referenced throughout this system
        (PLAN.md 0.3).  A SHORT leg is `side = NO` at the YES price we are asking
        -- i.e. we bid `100 - price_cents` for NO, which is the same order as
        resting a YES ask at `price_cents` (see shadow/engine.py).
        """
        markets = {m.ticker: m for m in snapshot.markets}
        base = {
            "sleeve": self.id,
            "structure_id": s.structure_id,
            "direction": s.direction.value,
            "locked": s.locked,
            "is_arbitrage": s.is_arbitrage,
            "event_ticker": s.event_ticker,
            "n_legs": s.n_legs,
            "legs": s.legs,
            "price_convention": "yes_referenced",
            "sum_px": round(s.sum_px, 4),
            "hurdle_sum_px": round(s.hurdle_sum_px, 4),
            "margin": round(s.margin, 5),
            "fees": round(s.fees, 5),
            "capital_per_basket": round(s.capital_per_basket, 4),
            "days_to_settle": round(s.days_to_settle, 3),
            "rolc": (None if math.isinf(s.rolc) else round(s.rolc, 4)),
            "mece_verdict": s.mece.verdict.value,
            "safe_to_sell": s.mece.safe_to_sell,
            "safe_to_buy": s.mece.safe_to_buy,
            "sum_bid": round(s.mece.sum_bid, 4),
            "sum_ask": round(s.mece.sum_ask, 4),
            "has_catch_all": s.mece.has_catch_all,
            # Bounded, not catastrophic: only one leg can win, so liability is
            # capped at $1 however many legs fill (research/06 2.4).
            "max_liability_per_basket": 1.0,
            "worst_case_partial": round(s.worst_case_partial, 4),
            # PLAN.md 3.2 execution protocol -- the executor owns the lifecycle.
            "leg_timeout_seconds": self.cfg.leg_timeout_seconds,
            "completion_taker_threshold": self.cfg.completion_taker_threshold,
            "max_orphan_exposure_fraction": self.cfg.max_orphan_exposure_fraction,
        }
        side = Side.NO if s.direction is Direction.SHORT else Side.YES
        out: list[DesiredQuote] = []
        for i, (ticker, price) in enumerate(zip(s.legs, s.prices_cents, strict=True)):
            m = markets.get(ticker)
            out.append(DesiredQuote(
                ticker=ticker,
                side=side,
                price_cents=price,
                size=s.size,
                post_only=True,     # I1 / C2: entry is always maker
                # The TYPED field, not just the rationale.  Carried only in the
                # rationale it never reached `orders.structure_id`, so every leg
                # landed with a NULL structure and a 30-leg basket was
                # indistinguishable from 30 unrelated shorts.  Leg tracking,
                # orphan detection and KPI 6 all key on that column.
                structure_id=s.structure_id,
                rationale={
                    **base,
                    "leg_index": i,
                    "rest_yes_price_cents": price,
                    "no_price_cents": 100 - price,
                    "book_bid": None if m is None else m.yes_bid,
                    "book_ask": None if m is None else m.yes_ask,
                },
            ))
        return out

    def _decisions_for(self, s: Structure, snapshot: MarketSnapshot,
                       *, acted: bool) -> list[Decision]:
        """One Decision per leg, acted or not.

        Un-acted decisions are what make calibration measurable without
        survivorship bias (PLAN.md 6.3) -- and for this sleeve they are also the
        audit trail showing WHY a tempting F1-trap event was never bought.
        """
        markets = {m.ticker: m for m in snapshot.markets}
        legs = [markets[t] for t in s.legs if t in markets]
        if len(legs) != s.n_legs:
            return []
        mids = [m.mid for m in legs]
        if any(v is None for v in mids):
            return []
        pi = devigged_probs([v for v in mids if v is not None])
        spec = self.fee_spec(legs, snapshot)
        event = snapshot.event_for(legs[0])

        out: list[Decision] = []
        for m, prob, price in zip(legs, pi, s.prices_cents, strict=True):
            px = price / 100.0
            if s.direction is Direction.LONG:
                raw = edge(prob, px, spec, is_maker=True)
            else:
                # Selling YES at px is buying NO at (1 - px); `fee` is symmetric
                # in price, so this is the exact per-leg edge of the short.
                raw = edge(1.0 - prob, 1.0 - px, spec, is_maker=True)
            out.append(Decision(
                ticker=m.ticker,
                market_price=m.mid or 0.0,
                p_model=prob,
                raw_edge=raw,
                shrunk_edge=self.cfg.lam * raw,
                acted=acted,
                category=(event.category if event else ""),
            ))
        return out
