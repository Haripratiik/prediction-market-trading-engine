"""Three fill models, always reported side by side.  PLAN.md 6.7, T-031.

WHY THREE AND NOT ONE
---------------------
Lo, MacKinlay & Zhang (JFE 2002) reconstructed hypothetical fills two ways and
compared them against actual executions: touch-fill overstates fill speed ~1.6x,
trade-through understates it ~2.4x, and the two bounds sit ~3.9x apart
(research/07 section 9.4).  A point estimate inside a 3.9x bracket is not a
measurement, so PLAN.md R6.7d requires the bracket.  R6.7a then says gate
promotion reads the PESSIMISTIC column only -- the other two exist to bound
fill-model uncertainty, not to flatter a strategy.  A strategy that is profitable
only at the optimistic bound does not exist.

THE ORDERING INVARIANT IS STRUCTURAL, NOT INCIDENTAL
---------------------------------------------------
Every model is expressed as the same walk over the same tape with two knobs:

    barrier  = queue_factor * queue_ahead      (contracts that must clear first)
    credit   = 1.0 for a print THROUGH our price
             + touch_share for a print AT our price

    filled(t) = clip(cumulative_credit(t) - barrier, 0, size)

`barrier` is non-increasing and `credit` non-decreasing across
PESSIMISTIC -> REALISTIC -> OPTIMISTIC, and `filled` is monotone in both, so
`pessimistic <= realistic <= optimistic` holds on EVERY tape by construction
rather than by fixture luck.  The test suite still checks it on every fixture,
because a refactor can break a proof.

CONCRETE RULES (research/07 section 9.4, adopted verbatim)
  1. A resting bid at p fills pessimistically only on a print strictly BELOW p --
     a print AT p means someone ahead of us filled.
  2. Displayed depth OVER-estimates fill probability (icebergs: ~9.3% of
     submitted and 15.9% of executed size elsewhere is hidden), so the realistic
     model discounts the visible queue rather than trusting it.
  3. Cancellations are censoring, not non-fills -- this module never observes a
     cancel, it reports how much of the horizon's flow reached us.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from core.models import Side

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class FillModel(StrEnum):
    """PLAN.md 6.7.  Report all three; gate on PESSIMISTIC (R6.7a)."""

    PESSIMISTIC = "pessimistic"
    REALISTIC = "realistic"
    OPTIMISTIC = "optimistic"


#: Canonical reporting order.  Also the direction the ordering invariant runs in.
ALL_MODELS: Final[tuple[FillModel, ...]] = (
    FillModel.PESSIMISTIC,
    FillModel.REALISTIC,
    FillModel.OPTIMISTIC,
)


@dataclass(frozen=True, slots=True)
class FillParams:
    """The two knobs that generate all three models.  See the module docstring.

    `queue_factor` is the fraction of the DISPLAYED queue ahead that is assumed
    real.  1.0 = we sat behind every displayed contract (pessimistic); 0.5 = half
    the displayed queue cancels or is stale before it trades (realistic); 0.0 =
    we were at the front (optimistic).  R6.7e forbids trusting displayed depth,
    so the realistic discount is a modelling assumption to be CALIBRATED against
    realized fills (T-044b), never a constant to be believed.
    """

    queue_factor: float
    touch_credit: bool          # do prints AT our price reach us at all?
    taker_penalty_cents: float  # extra cost per contract when crossing
    taker_at_best: bool         # optimistic: whole order at the best displayed level


PARAMS: Final[dict[FillModel, FillParams]] = {
    # trade-through only, behind the FULL resting queue, plus a tick of penalty
    # when crossing.  This is the column gates read.
    FillModel.PESSIMISTIC: FillParams(
        queue_factor=1.0, touch_credit=False, taker_penalty_cents=1.0, taker_at_best=False
    ),
    # trade-through fills proportional to modelled queue position; prints at our
    # price fill us in proportion to our share of the level.
    FillModel.REALISTIC: FillParams(
        queue_factor=0.5, touch_credit=True, taker_penalty_cents=0.0, taker_at_best=False
    ),
    # touch fill: any print at our price fills us, queue ignored.
    FillModel.OPTIMISTIC: FillParams(
        queue_factor=0.0, touch_credit=True, taker_penalty_cents=0.0, taker_at_best=True
    ),
}

#: Cost per contract charged beyond the last recorded depth level.  Recorded
#: depth is only what we archived (L1 on Kalshi), not the whole book, so the
#: overflow has to cost SOMETHING or a taker model reads as free size.
OVERFLOW_PENALTY_CENTS: Final[float] = 1.0

#: R6.7c.  Realized adverse-fill rates on CME futures (April 2024): ES 81.5%,
#: NQ 65.8%, CL 82.9%, ZN 88.8%.  Two-thirds to nine-tenths of maker fills are
#: immediately adverse.  A simulator producing ~50% is handing you fills the real
#: market would not have.
ADVERSE_FILL_BAND: Final[tuple[float, float]] = (0.66, 0.89)


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TapeTrade:
    """One public print.  Mirrors the `trades` table (core/db.py).

    `taker_side` is LABELLED by Kalshi, so trade direction is ground truth here
    -- no Lee-Ready, no BVC.  (On Polymarket feed-inferred direction agrees with
    on-chain truth only ~59% of the time, which flips the sign of the effective
    half-spread 67% of the time -- research/07 section 7.)
    """

    traded_at_us: int
    yes_price_cents: int
    size: float
    taker_side: str             # "yes" | "no"


@dataclass(frozen=True, slots=True)
class DepthLevel:
    """One level of recorded depth, priced in the currency of the side BOUGHT.

    Buying NO at a YES-bid of 62c costs 38c, so the caller converts before
    handing the ladder over (`ladder_from_book` does it).
    """

    price_cents: float
    size: float


@dataclass(frozen=True, slots=True)
class RestingOrder:
    """An order the sleeve wanted, plus the book state AT DECISION TIME.

    `price_cents` is YES-referenced throughout the system (PLAN.md 0.3), for both
    sides.  A NO order at YES-price p means we pay 100-p for NO.

    `queue_ahead` is what makes the fill model honest: it is the size displayed
    at our price when we decided, i.e. what has to clear before flow reaches us.
    """

    order_id: str
    ticker: str
    side: Side
    price_cents: int
    size: int
    placed_at_us: int
    queue_ahead: float = 0.0
    book_bid: int | None = None
    book_ask: int | None = None

    @property
    def cost_price_cents(self) -> float:
        """Cash paid per contract for the side we are buying."""
        return float(self.price_cents) if self.side is Side.YES else 100.0 - self.price_cents


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SimFill:
    """What one order got under one fill model.

    `path` is the cumulative fill curve -- (time, cumulative contracts).  It
    exists so a caller can ask "how much of this order was filled AS OF t?"
    without looking at the rest of the horizon.  That is not a nicety: crediting
    a whole horizon's fill at its first print leaks future tape into the position
    the sleeve sees at the next decision (backtest/leakage.py checks for it).
    """

    order_id: str
    ticker: str
    side: Side
    model: FillModel
    is_maker: bool
    filled_size: float
    avg_cost_cents: float          # cash per contract for the side bought
    price_cents: int               # the order's YES-referenced limit, for context
    first_fill_at_us: int | None
    queue_ahead: float
    volume_credited: float
    path: tuple[tuple[int, float], ...] = ()
    markout_cents: float | None = None   # signed; positive = the mark moved our way

    @property
    def filled(self) -> bool:
        return self.filled_size > 0.0

    @property
    def cost_cents(self) -> float:
        return self.filled_size * self.avg_cost_cents

    def filled_as_of(self, at_us: int) -> float:
        """Contracts filled at or before `at_us`.  Zero before the first print."""
        total = 0.0
        for t, cum in self.path:
            if t > at_us:
                break
            total = cum
        return total


def with_markout(fill: SimFill, markout_cents: float | None) -> SimFill:
    """Attach the post-fill mark.  Separate from simulation on purpose: the mark
    is EVALUATION data (it looks forward), the fill decision is not."""
    return replace(fill, markout_cents=markout_cents)


# --------------------------------------------------------------------------- #
# Maker fills
# --------------------------------------------------------------------------- #


def _eligible(order: RestingOrder, trade: TapeTrade) -> tuple[bool, bool]:
    """(reaches_us_through, reaches_us_at_touch) for one print.

    A resting BUY YES at p is filled by someone SELLING YES at <= p, which on
    Kalshi's tape is a print with taker_side='no' (the taker bought NO).  A
    resting BUY NO at YES-price p is filled by a taker who bought YES at >= p.
    """
    if order.side is Side.YES:
        if trade.taker_side != "no":
            return False, False
        return trade.yes_price_cents < order.price_cents, (
            trade.yes_price_cents == order.price_cents
        )
    if trade.taker_side != "yes":
        return False, False
    return trade.yes_price_cents > order.price_cents, (
        trade.yes_price_cents == order.price_cents
    )


def simulate_maker_fill(
    order: RestingOrder,
    trades: Iterable[TapeTrade],
    model: FillModel,
    *,
    horizon_us: int | None = None,
    params: FillParams | None = None,
) -> SimFill:
    """Replay the tape against one resting order under one fill model."""
    p = params if params is not None else PARAMS[model]
    size = float(order.size)

    barrier = p.queue_factor * max(order.queue_ahead, 0.0)
    if not p.touch_credit:
        touch_share = 0.0
    elif barrier <= 0.0:
        touch_share = 1.0
    else:
        # our proportional position in the level: we own `size` of `size+barrier`
        touch_share = size / (size + barrier)

    t_end = order.placed_at_us + horizon_us if horizon_us is not None else None
    # stable sort: identical timestamps keep tape order, so the result does not
    # depend on how the rows came out of SQLite (T-030 determinism)
    stream = sorted(trades, key=lambda t: t.traded_at_us)

    credited = 0.0
    filled = 0.0
    first: int | None = None
    path: list[tuple[int, float]] = []

    for tr in stream:
        if tr.traded_at_us <= order.placed_at_us:
            continue                       # we were not resting yet
        if t_end is not None and tr.traded_at_us > t_end:
            break
        through, at_touch = _eligible(order, tr)
        credit = tr.size if through else (tr.size * touch_share if at_touch else 0.0)
        if credit <= 0.0:
            continue
        credited += credit
        new = min(max(credited - barrier, 0.0), size)
        if new > filled:
            if first is None:
                first = tr.traded_at_us
            filled = new
            path.append((tr.traded_at_us, filled))

    return SimFill(
        order_id=order.order_id,
        ticker=order.ticker,
        side=order.side,
        model=model,
        is_maker=True,
        filled_size=filled,
        avg_cost_cents=order.cost_price_cents,
        price_cents=order.price_cents,
        first_fill_at_us=first,
        queue_ahead=order.queue_ahead,
        volume_credited=credited,
        path=tuple(path),
    )


def simulate_maker_all(
    order: RestingOrder,
    trades: Sequence[TapeTrade],
    *,
    horizon_us: int | None = None,
) -> dict[FillModel, SimFill]:
    """All three columns for one order.  R6.7d: report the bracket, never a point."""
    return {
        m: simulate_maker_fill(order, trades, m, horizon_us=horizon_us)
        for m in ALL_MODELS
    }


# --------------------------------------------------------------------------- #
# Taker fills
# --------------------------------------------------------------------------- #


def ladder_from_book(
    side: Side,
    *,
    yes_bid: int | None,
    yes_bid_size: float,
    yes_ask: int | None,
    yes_ask_size: float,
) -> tuple[DepthLevel, ...]:
    """Convert an L1 book into a cost ladder for the side being BOUGHT.

    Buying YES lifts the ask.  Buying NO hits the bid and costs 100 - yes_bid.
    Kalshi's archived feed is top-of-book, so this is one level; the taker walk
    handles deeper ladders unchanged when L2 becomes available.
    """
    if side is Side.YES:
        if yes_ask is None or not 0 < yes_ask <= 100 or yes_ask_size <= 0:
            return ()
        return (DepthLevel(float(yes_ask), float(yes_ask_size)),)
    if yes_bid is None or yes_bid < 1 or yes_bid_size <= 0:
        return ()
    return (DepthLevel(100.0 - yes_bid, float(yes_bid_size)),)


def simulate_taker_fill(
    order: RestingOrder,
    depth: Sequence[DepthLevel],
    model: FillModel,
    *,
    params: FillParams | None = None,
    overflow_penalty_cents: float = OVERFLOW_PENALTY_CENTS,
) -> SimFill:
    """Cross the spread against recorded depth.

    pessimistic  walk the ladder with full slippage, plus one tick of penalty
    realistic    walk the ladder with full slippage
    optimistic   the whole order at the best displayed level

    Size is the same in all three (we crossed; we got filled) -- what differs is
    the price.  So for takers the ordering invariant runs on COST, not quantity,
    which is what `generosity` normalises.
    """
    p = params if params is not None else PARAMS[model]
    size = float(order.size)
    levels = sorted(depth, key=lambda d: d.price_cents)

    if not levels or size <= 0:
        return SimFill(
            order_id=order.order_id, ticker=order.ticker, side=order.side, model=model,
            is_maker=False, filled_size=0.0, avg_cost_cents=0.0,
            price_cents=order.price_cents, first_fill_at_us=None,
            queue_ahead=order.queue_ahead, volume_credited=0.0, path=(),
        )

    if p.taker_at_best:
        cost = size * levels[0].price_cents
    else:
        cost = 0.0
        remaining = size
        for lvl in levels:
            take = min(remaining, lvl.size)
            cost += take * lvl.price_cents
            remaining -= take
            if remaining <= 0.0:
                break
        if remaining > 0.0:
            cost += remaining * (levels[-1].price_cents + overflow_penalty_cents)
        cost += size * p.taker_penalty_cents

    return SimFill(
        order_id=order.order_id, ticker=order.ticker, side=order.side, model=model,
        is_maker=False, filled_size=size, avg_cost_cents=cost / size,
        price_cents=order.price_cents, first_fill_at_us=order.placed_at_us,
        queue_ahead=order.queue_ahead, volume_credited=size,
        path=((order.placed_at_us, size),),
    )


def simulate_taker_all(
    order: RestingOrder,
    depth: Sequence[DepthLevel],
    *,
    overflow_penalty_cents: float = OVERFLOW_PENALTY_CENTS,
) -> dict[FillModel, SimFill]:
    return {
        m: simulate_taker_fill(
            order, depth, m, overflow_penalty_cents=overflow_penalty_cents
        )
        for m in ALL_MODELS
    }


# --------------------------------------------------------------------------- #
# The invariant
# --------------------------------------------------------------------------- #


def generosity(fill: SimFill) -> float:
    """How much the fill model gave us.  Monotone across the three models.

    Makers are measured in contracts (more fills = a more generous model).
    Takers always get their size, so they are measured in negative cash (a
    cheaper execution = a more generous model).  Mixing the two units is fine
    because the invariant is only ever compared WITHIN one order.
    """
    return fill.filled_size if fill.is_maker else -fill.cost_cents


def ordering_violations(
    fills: Mapping[FillModel, SimFill], *, tol: float = 1e-9
) -> tuple[str, ...]:
    """PLAN.md 6.7: pessimistic <= realistic <= optimistic.  Empty tuple = clean."""
    out: list[str] = []
    present = [m for m in ALL_MODELS if m in fills]
    for lo, hi in zip(present, present[1:], strict=False):
        a, b = fills[lo], fills[hi]
        if generosity(a) > generosity(b) + tol:
            out.append(
                f"{a.order_id}: {lo.value} ({generosity(a):.6f}) > "
                f"{hi.value} ({generosity(b):.6f})"
            )
        # a more generous model can never fill LATER than a less generous one
        if a.first_fill_at_us is not None and b.first_fill_at_us is not None:
            if b.first_fill_at_us > a.first_fill_at_us:
                out.append(
                    f"{a.order_id}: {hi.value} filled at {b.first_fill_at_us} after "
                    f"{lo.value} at {a.first_fill_at_us}"
                )
        elif a.first_fill_at_us is not None and b.first_fill_at_us is None:
            out.append(f"{a.order_id}: {lo.value} filled but {hi.value} did not")
    return tuple(out)


def assert_ordering(fills: Mapping[FillModel, SimFill]) -> None:
    """Raise if the bracket is inverted.  Cheap enough to run on every order."""
    bad = ordering_violations(fills)
    if bad:
        raise AssertionError("fill-model ordering violated (PLAN.md 6.7): " + "; ".join(bad))


# --------------------------------------------------------------------------- #
# The calibration gate.  PLAN.md R6.7c.
# --------------------------------------------------------------------------- #


def adverse_fill_rate(
    fills: Iterable[SimFill], *, exclude_flat: bool = True
) -> float:
    """Fraction of fills immediately followed by an adverse move.

    "Adverse" means the mark went AGAINST the side we took: `markout_cents < 0`.
    Flat marks are excluded by default because the quantity CME reports is the
    direction of the NEXT MOVE -- on a 1c tick most short-horizon marks are
    unchanged, and counting them as favourable drags the rate toward 0.5, which
    is exactly the failure signature this gate exists to catch.

    Returns 0.0 when nothing is measurable, which `adverse_fill_gate` reports as
    insufficient sample rather than as a pass.
    """
    total = 0
    adverse = 0
    for f in fills:
        if not f.filled or f.markout_cents is None:
            continue
        if exclude_flat and f.markout_cents == 0.0:
            continue
        total += 1
        if f.markout_cents < 0.0:
            adverse += 1
    return adverse / total if total else 0.0


@dataclass(frozen=True, slots=True)
class AdverseFillCheck:
    rate: float
    n: int
    passed: bool
    verdict: str


def adverse_fill_gate(
    fills: Iterable[SimFill],
    *,
    band: tuple[float, float] = ADVERSE_FILL_BAND,
    min_n: int = 30,
    exclude_flat: bool = True,
) -> AdverseFillCheck:
    """R6.7c.  A realistic simulator lands in 66-89%.

    Below the band the simulator is too generous -- it is handing you fills the
    real market would not have, and every downstream number is fiction.  Above
    it, the marks are being measured over the wrong horizon or the quotes really
    are toxic; either way it is not a green light.
    """
    measurable = [
        f for f in fills
        if f.filled and f.markout_cents is not None
        and not (exclude_flat and f.markout_cents == 0.0)
    ]
    n = len(measurable)
    rate = adverse_fill_rate(measurable, exclude_flat=exclude_flat)
    lo, hi = band
    if n < min_n:
        return AdverseFillCheck(rate, n, False,
                                f"insufficient sample: {n} measurable fills < {min_n}")
    if rate < lo:
        return AdverseFillCheck(
            rate, n, False,
            f"adverse-fill rate {rate:.3f} below {lo:.2f}: the fill model is too "
            f"generous (R6.7c -- ~50% means fills the real market would not have given)",
        )
    if rate > hi:
        return AdverseFillCheck(
            rate, n, False,
            f"adverse-fill rate {rate:.3f} above {hi:.2f}: marks are measured over the "
            f"wrong horizon, or the quotes are toxic",
        )
    return AdverseFillCheck(rate, n, True,
                            f"adverse-fill rate {rate:.3f} within {lo:.2f}-{hi:.2f}")
