"""S3 -- linked-market relative value.  The core statistical-arbitrage sleeve.

PLAN.md 3.3, with corrections C1-C5 from 3.0.

THESIS.  Markets that are logically related are priced on separate books with no
cross-margining, so their prices routinely violate the constraint that binds
them.  Unlike a forecast the constraint is PROVABLE; unlike equity pairs trading
it is GUARANTEED to converge at settlement (W4).  Nothing here forecasts
anything: the sleeve enforces the market's own internal consistency.

MAKER DISCIPLINE IS THE ENTIRE VIABILITY OF THE SLEEVE (C2)
------------------------------------------------------------
A 5c implication violation between legs priced 60c and 55c:

    double MAKER   gross 5.00c   fee 0.85c   net  4.15c     4.9:1 edge:cost
    double TAKER   gross 5.00c   fee 3.41c   net  1.59c     0.5:1
    double TAKER, actually crossing a 2c-wide book on each leg:
                   gross 3.00c   fee 3.42c   net -0.42c     A LOSS

The taker cannot transact at the reference prices -- it must lift the ask and
hit the bid, which surrenders a half-spread per leg on top of a fee four times
larger.  `maker_taker_comparison()` computes all three rows rather than quoting
them.  So the correct formulation is NOT "detect an arbitrage and hit it" but:

    post resting orders that only ever fill at prices which complete a
    profitable structure

which converts a latency race (which a retail VPS loses -- C1: 7 executable
single-market episodes across 3,042 NBA markets in a month, median duration 3.6
seconds) into a patience-and-inventory game.

THE HARD GATE (C4)
------------------
A link trades only when `equivalence_status == VERIFIED`.  The dominant risk in
this family is not market risk but CORRELATION-OF-DEFINITION risk: a link whose
rulebooks do not actually agree is a directional bet wearing a hedge costume,
and it is the top loss driver in the whole strategy family.  The gate is checked
before anything else, including before the prices are even read.

TWO-LEG EXECUTION DISCIPLINE
----------------------------
Rest both legs.  On a single-leg fill either complete the other as a taker if the
margin survives -- which is why every structure publishes `max_taker_buy_cents`
and `min_taker_sell_cents`, the worst completion prices that still clear the
hurdle -- or unwind inside `leg_timeout_seconds` (900s, config/risk.yaml).
NEVER carry a naked leg past the timeout because "it will probably converge
anyway": that is how a relative-value book becomes a directional book.

PURITY (C4.2a).  `desired_state` is a deterministic function of the snapshot.
Time comes from `snapshot.now_us`; the sleeve never reads a clock, never does
I/O, and never calls a VenueClient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from core.math.contracts import FeeSpec, fee
from core.models import Market, Side
from rulebook.exhaustiveness import Verdict
from rulebook.links import (
    MIN_NET_CENTS,
    LadderScan,
    Link,
    LinkRegistry,
    LinkType,
    Violation,
    ladder_links_from_markets,
)
from strategy.base import Decision, DesiredQuote, DesiredState, MarketSnapshot

HOURS_PER_YEAR: Final[float] = 365.0 * 24.0


# --------------------------------------------------------------------------- #
# Structure economics
# --------------------------------------------------------------------------- #
def leg_fee_cents(price_cents: int, spec: FeeSpec, *, is_maker: bool) -> float:
    """Fee in cents for one leg quoted at a YES price of `price_cents`.

    The Kalshi fee is theta * p * (1-p), which is SYMMETRIC under p -> 1-p, so
    selling YES at p costs exactly what buying YES at p costs.  That is why both
    legs of a structure can be priced off their YES prices without tracking which
    side of the book we are on (research/06 section 4).
    """
    if not 1 <= price_cents <= 99:
        raise ValueError(f"price_cents must be in 1..99, got {price_cents}")
    return 100.0 * fee(price_cents / 100.0, spec, is_maker=is_maker)


@dataclass(frozen=True, slots=True)
class MarginBreakdown:
    """Per-contract economics of one two-leg structure, in cents."""

    gross_cents: float
    fee_cents: float
    net_cents: float
    locked_cents: float

    @property
    def edge_to_cost(self) -> float:
        """Ratio the plan quotes as 4.8:1 for the canonical maker structure."""
        return self.net_cents / self.fee_cents if self.fee_cents > 0 else float("inf")

    @property
    def rolc(self) -> float:
        """Return on locked capital, undiscounted."""
        return self.net_cents / self.locked_cents if self.locked_cents > 0 else 0.0

    def annualized_rolc(self, hours_to_settlement: float) -> float:
        """The hurdle that stops the sleeve tying up capital for a year to earn 3c.

        Linear (not compounded) annualisation: these are one-shot structures, not
        a reinvested series, so compounding would overstate them.
        """
        if hours_to_settlement <= 0.0:
            return float("inf")
        return self.rolc * (HOURS_PER_YEAR / hours_to_settlement)


def structure_margin(
    *,
    sell_price_cents: int,
    buy_price_cents: int,
    sell_spec: FeeSpec,
    buy_spec: FeeSpec,
    sell_is_maker: bool = True,
    buy_is_maker: bool = True,
    bound_cents: float = 0.0,
) -> MarginBreakdown:
    """Sell YES on the rich leg, buy YES on the cheap leg.

    Payoff at settlement is `-1{sell} + 1{buy}`.  For L1 that is identically 0
    and for L2 (sell = subset, buy = superset) it is >= 0, so the entry credit
    `sell - buy` IS the locked profit.  For L4 the relation is an assumption
    worth at most `bound_cents`, and the residual `-1{sell and not buy}` is a
    real $1 loss -- hence subtracting the bound from the gross, and hence half
    sizing.

    `locked_cents` assumes NO netting between the legs: (100 - sell) collateral
    on the short plus `buy` paid on the long.  research/06 section 8 -- netting
    exists inside one market and inside a MECNET event; a DIRECNET ladder nets
    nothing, and the MECNET behaviour is unverified on a funded account
    (PLAN.md T-050c).  Assuming netting we have not measured would understate
    capital by an order of magnitude.
    """
    fees = (leg_fee_cents(sell_price_cents, sell_spec, is_maker=sell_is_maker)
            + leg_fee_cents(buy_price_cents, buy_spec, is_maker=buy_is_maker))
    gross = float(sell_price_cents - buy_price_cents) - bound_cents
    return MarginBreakdown(
        gross_cents=gross,
        fee_cents=fees,
        net_cents=gross - fees,
        locked_cents=float((100 - sell_price_cents) + buy_price_cents),
    )


def maker_taker_comparison(
    *,
    sell_price_cents: int = 60,
    buy_price_cents: int = 55,
    spec: FeeSpec | None = None,
    cross_cents: int = 1,
) -> dict[str, MarginBreakdown]:
    """PLAN.md 3.3's economics table, computed instead of quoted.

    The default spec is `quadratic_with_maker_fees` -- the CONSERVATIVE case.
    Only ~130 of 13,486 Kalshi series charge makers anything at all; on the other
    13,353 the maker fee is exactly zero and the maker structure keeps the whole
    5c.  The plan's 0.85c maker hurdle is 0.25x the 3.41c taker hurdle, i.e. this
    fee type.

    `cross_cents` is the adverse half-spread a taker pays per leg.  The plan
    describes the double-taker version as a loss, which it is as soon as the
    taker has to actually cross a book: at the reference prices alone it clears
    a bare 1.59c, and one cent of spread per leg wipes that out.
    """
    spec = spec or FeeSpec.kalshi("quadratic_with_maker_fees", 1.0)
    return {
        "double_maker": structure_margin(
            sell_price_cents=sell_price_cents, buy_price_cents=buy_price_cents,
            sell_spec=spec, buy_spec=spec,
            sell_is_maker=True, buy_is_maker=True,
        ),
        "double_taker_at_reference": structure_margin(
            sell_price_cents=sell_price_cents, buy_price_cents=buy_price_cents,
            sell_spec=spec, buy_spec=spec,
            sell_is_maker=False, buy_is_maker=False,
        ),
        "double_taker_after_crossing": structure_margin(
            sell_price_cents=sell_price_cents - cross_cents,
            buy_price_cents=buy_price_cents + cross_cents,
            sell_spec=spec, buy_spec=spec,
            sell_is_maker=False, buy_is_maker=False,
        ),
    }


def taker_completion_limits(
    *,
    sell_price_cents: int,
    buy_price_cents: int,
    sell_spec: FeeSpec,
    buy_spec: FeeSpec,
    bound_cents: float = 0.0,
    min_net_cents: float = 0.0,
) -> tuple[int | None, int | None]:
    """Worst prices at which a taker can still COMPLETE a half-filled structure.

    Returns `(max_taker_buy_cents, min_taker_sell_cents)`:

      * the sell leg filled as maker -> how high we may pay to lift the other ask
      * the buy leg filled as maker  -> how low we may sell to hit the other bid

    This is the arithmetic behind "complete as taker if the margin survives".
    Publishing it with the quotes is what lets the executor make that call in
    milliseconds without re-deriving the structure, and what makes "it will
    probably converge anyway" an inadmissible answer.
    """
    max_buy: int | None = None
    for b in range(1, 100):
        m = structure_margin(
            sell_price_cents=sell_price_cents, buy_price_cents=b,
            sell_spec=sell_spec, buy_spec=buy_spec,
            sell_is_maker=True, buy_is_maker=False, bound_cents=bound_cents,
        )
        if m.net_cents >= min_net_cents:
            max_buy = b if max_buy is None else max(max_buy, b)

    min_sell: int | None = None
    for s in range(1, 100):
        m = structure_margin(
            sell_price_cents=s, buy_price_cents=buy_price_cents,
            sell_spec=sell_spec, buy_spec=buy_spec,
            sell_is_maker=False, buy_is_maker=True, bound_cents=bound_cents,
        )
        if m.net_cents >= min_net_cents:
            min_sell = s if min_sell is None else min(min_sell, s)

    return max_buy, min_sell


# --------------------------------------------------------------------------- #
# Sleeve
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class S3Config:
    # --- universe -----------------------------------------------------------
    min_hours_to_close: float = 1.0
    # An L1/L2 payoff is only guaranteed if BOTH legs are held to settlement.
    # Legs that settle far apart leave a naked position between the two dates,
    # which is the same failure the leg timeout exists to prevent -- only slower
    # and unavoidable.  Reject the link rather than discover it later.
    max_close_gap_hours: float = 24.0
    # --- signal -------------------------------------------------------------
    min_net_cents: dict[LinkType, float] = field(
        default_factory=lambda: dict(MIN_NET_CENTS)
    )
    min_annualized_rolc: float = 0.15          # config/risk.yaml structures
    # --- sizing -------------------------------------------------------------
    max_depth_fraction: float = 0.20           # risk.yaml capacity
    max_per_structure_fraction: float = 0.05   # risk.yaml structures
    l4_size_multiplier: float = 0.5            # PLAN.md 3.3: L4 is sized at half
    # --- execution ----------------------------------------------------------
    leg_timeout_seconds: int = 900             # risk.yaml structures
    # JOIN the touch, never penny.  A structure's whole margin is 1-5c; giving a
    # cent away to improve the queue surrenders a fifth of it for a fill-
    # probability gain that measures 0.21-0.26 ticks of queue value (S1's
    # `quote_price` reasoning, applied to a far thinner edge).
    improve_touch: bool = False
    max_structures: int = 20
    # --- discovery ----------------------------------------------------------
    auto_detect_ladders: bool = True
    adjacent_only: bool = True                 # adjacent strikes are the cleanest
    ladder_k_bound: float | None = None        # set to emit L4s alongside the L2s


@dataclass(frozen=True, slots=True)
class StructurePlan:
    """A complete two-leg structure the sleeve wants to exist."""

    structure_id: str
    link_id: str
    link_type: LinkType
    sell_ticker: str
    buy_ticker: str
    sell_price_cents: int
    buy_price_cents: int
    size: int
    margin: MarginBreakdown
    mid_violation_cents: float
    hours_to_settlement: float
    max_taker_buy_cents: int | None = None
    min_taker_sell_cents: int | None = None


@dataclass
class S3LinkedRV:
    """Trade provable inconsistencies between logically linked markets."""

    id: str = "S3"
    gate: int = 2                                   # PLAN.md 3.3 gate_entry: G2
    cfg: S3Config = field(default_factory=S3Config)
    links: tuple[Link, ...] = ()
    registry: LinkRegistry = field(default_factory=LinkRegistry)

    # ---------------------------------------------------------------- linking
    def resolve_links(
        self, snapshot: MarketSnapshot
    ) -> tuple[tuple[Link, ...], LadderScan]:
        """Explicit links plus ladders derived from the snapshot, then stamped.

        Ladder derivation is pure -- it reads only the snapshot -- so the primary
        target of the sleeve needs no external link file.  It still cannot trade
        until the registry promotes it: strike parsing can be wrong (a "below"
        ladder read as "above" inverts every constraint), and that is exactly the
        class of error the human gate exists for.

        The registry stamp is applied LAST and unconditionally, so a Link handed
        in already marked VERIFIED is still downgraded unless the registry knows
        it.  A link object may not self-certify -- that is what makes the gate a
        gate rather than a default.
        """
        found: dict[str, Link] = {}
        scan = LadderScan()
        if self.cfg.auto_detect_ladders:
            derived, scan = ladder_links_from_markets(
                snapshot.markets, snapshot.events,
                adjacent_only=self.cfg.adjacent_only,
                k_bound=self.cfg.ladder_k_bound,
            )
            for link in derived:
                found[link.link_id] = link
        # explicit links win: a hand-curated relation outranks a parsed one
        for link in self.links:
            found[link.link_id] = link
        ordered = tuple(found[k] for k in sorted(found))
        return self.registry.apply(ordered), scan

    # -------------------------------------------------------------- economics
    @staticmethod
    def _fee_spec(m: Market, snapshot: MarketSnapshot) -> FeeSpec:
        s = snapshot.series_for(m)
        return s.fee_spec if s else FeeSpec.kalshi("quadratic", 1.0)

    def maker_prices(self, sell: Market, buy: Market) -> tuple[int, int]:
        """Resting prices that are makers on BOTH legs.

        Joining the ask to sell and the bid to buy is simultaneously the most
        profitable and the only maker-legal choice: a sell posted at or above the
        ask cannot cross the bid, and a buy posted at or below the bid cannot
        cross the ask.  The taker's disadvantage -- surrendering a half-spread on
        each leg -- becomes the maker's second source of margin.
        """
        assert sell.yes_ask is not None and sell.yes_bid is not None
        assert buy.yes_bid is not None and buy.yes_ask is not None
        sell_px, buy_px = sell.yes_ask, buy.yes_bid
        if self.cfg.improve_touch:
            if sell.yes_ask - sell.yes_bid > 1:
                sell_px = sell.yes_ask - 1
            if buy.yes_ask - buy.yes_bid > 1:
                buy_px = buy.yes_bid + 1
        return sell_px, buy_px

    def structure_size(
        self,
        *,
        link_type: LinkType,
        sell_depth: float,
        buy_depth: float,
        locked_cents: float,
        bankroll_cents: int,
    ) -> int:
        """Contracts, capped by visible depth and by the per-structure cap.

        L4 is halved because it is not an arbitrage: its constraint is an
        assumption about `k`, and a wrong one costs the full $1 per contract
        (PLAN.md 3.3 -- "treat as directional and size at half cap").
        """
        depth_cap = int(min(sell_depth, buy_depth) * self.cfg.max_depth_fraction)
        budget = int(bankroll_cents * self.cfg.max_per_structure_fraction)
        capital_cap = budget // max(1, int(round(locked_cents)))
        size = min(depth_cap, capital_cap)
        if link_type is LinkType.BOUNDED:
            size = int(size * self.cfg.l4_size_multiplier)
        return max(0, size)

    # ---------------------------------------------------------------- signal
    def evaluate(
        self, link: Link, snapshot: MarketSnapshot,
        by_ticker: dict[str, Market],
    ) -> tuple[StructurePlan | None, str, Violation | None]:
        """One link -> a structure, or a reason there is none."""
        # C4 HARD GATE, checked before the prices are even read.  A link whose
        # rulebooks have not been confirmed to agree is not a hedge.
        if link.equivalence_status is not Verdict.VERIFIED:
            return None, f"equivalence {link.equivalence_status.value}, not VERIFIED", None
        if not link.is_two_leg:
            # An L3 partition is a >=3-leg structure; the two-leg unwind protocol
            # below does not describe it and its orphan risk is a different shape.
            return None, "multi-leg structure needs the n-leg protocol", None

        markets = [by_ticker.get(t) for t in link.tickers]
        if any(m is None for m in markets):
            return None, "leg missing from snapshot", None
        legs: list[Market] = [m for m in markets if m is not None]
        if not all(m.has_two_sided_quote for m in legs):
            return None, "a leg has no two-sided quote", None

        prices = {m.ticker: m.mid for m in legs if m.mid is not None}
        violation = link.violation(prices)
        if violation is None:
            return None, "prices satisfy the constraint", None

        sell = by_ticker[violation.sell_ticker]
        buy = by_ticker[violation.buy_ticker]

        hours = [m.hours_to_close(now=snapshot.now_us) for m in (sell, buy)]
        if any(h is None for h in hours):
            return None, "no close time", violation
        h_sell, h_buy = float(hours[0] or 0.0), float(hours[1] or 0.0)
        if min(h_sell, h_buy) < self.cfg.min_hours_to_close:
            return None, "inside the final hour", violation
        if abs(h_sell - h_buy) > self.cfg.max_close_gap_hours:
            return None, "legs settle too far apart to hold both", violation

        sell_px, buy_px = self.maker_prices(sell, buy)
        if sell_px <= buy_px:
            return None, "no maker price pair with positive gross", violation
        # `has_ask` admits an ask of 100, which is not a price on the 1..99 grid
        # the fee model is defined on -- and a leg quoted at 100 is untradeable
        # anyway.
        if not (1 <= buy_px <= 99 and 1 <= sell_px <= 99):
            return None, "maker price outside the 1..99 grid", violation

        sell_spec = self._fee_spec(sell, snapshot)
        buy_spec = self._fee_spec(buy, snapshot)
        bound_cents = 100.0 * link.bound
        margin = structure_margin(
            sell_price_cents=sell_px, buy_price_cents=buy_px,
            sell_spec=sell_spec, buy_spec=buy_spec,
            sell_is_maker=True, buy_is_maker=True, bound_cents=bound_cents,
        )
        min_net = self.cfg.min_net_cents.get(link.link_type,
                                             MIN_NET_CENTS[link.link_type])
        if margin.net_cents < min_net:
            return None, f"net {margin.net_cents:.2f}c below {min_net:.2f}c", violation

        # Capital is locked until the LATER leg settles, so that is the horizon
        # the return-on-locked-capital hurdle must use.
        horizon = max(h_sell, h_buy)
        if margin.annualized_rolc(horizon) < self.cfg.min_annualized_rolc:
            return None, "annualized ROLC below hurdle", violation

        size = self.structure_size(
            link_type=link.link_type,
            sell_depth=sell.yes_ask_size, buy_depth=buy.yes_bid_size,
            locked_cents=margin.locked_cents,
            bankroll_cents=snapshot.bankroll_cents,
        )
        if size < 1:
            return None, "size rounds to zero", violation

        max_buy, min_sell = taker_completion_limits(
            sell_price_cents=sell_px, buy_price_cents=buy_px,
            sell_spec=sell_spec, buy_spec=buy_spec, bound_cents=bound_cents,
        )
        plan = StructurePlan(
            # Deterministic and price-bearing: an identical snapshot yields an
            # identical id (C4.2a), and a re-quote at a different price is a
            # different structure rather than a silent amendment.
            structure_id=f"{self.id}:{link.link_id}@{sell_px}/{buy_px}",
            link_id=link.link_id,
            link_type=link.link_type,
            sell_ticker=sell.ticker,
            buy_ticker=buy.ticker,
            sell_price_cents=sell_px,
            buy_price_cents=buy_px,
            size=size,
            margin=margin,
            mid_violation_cents=100.0 * violation.size,
            hours_to_settlement=horizon,
            max_taker_buy_cents=max_buy,
            min_taker_sell_cents=min_sell,
        )
        return plan, "", violation

    # -------------------------------------------------------------- execution
    def quotes_for(self, plan: StructurePlan, snapshot: MarketSnapshot
                   ) -> tuple[DesiredQuote, DesiredQuote]:
        """Both legs, sharing a `structure_id`, both post-only.

        SIDE CONVENTION, and it is the easiest thing in this codebase to get
        wrong: `price_cents` is always YES-referenced.  A `Side.NO` quote at
        YES-price p is a resting YES ASK at p -- shadow/engine.py, "a resting BUY
        NO at YES-price p is filled by a taker who bought YES at
        yes_price_cents >= p".  So the SELL leg is Side.NO at the YES price we
        want to sell at, NOT at 100 - p.
        """
        shared = {
            "sleeve": self.id,
            # DesiredQuote has no structure_id field (OrderRequest does), so the
            # two legs are bound together here.  The executor must treat them as
            # one unit: a fill on one arms the timeout on the other.
            "structure_id": plan.structure_id,
            "link_id": plan.link_id,
            "link_type": plan.link_type.value,
            "gross_cents": round(plan.margin.gross_cents, 3),
            "fee_cents": round(plan.margin.fee_cents, 3),
            "net_cents": round(plan.margin.net_cents, 3),
            "locked_cents": round(plan.margin.locked_cents, 3),
            "mid_violation_cents": round(plan.mid_violation_cents, 3),
            "annualized_rolc": round(
                plan.margin.annualized_rolc(plan.hours_to_settlement), 4),
            # The two-leg discipline, published with the order so the executor
            # never has to re-derive it under time pressure.
            "leg_timeout_seconds": self.cfg.leg_timeout_seconds,
            "unwind_deadline_us": snapshot.now_us
            + self.cfg.leg_timeout_seconds * 1_000_000,
            "max_taker_buy_cents": plan.max_taker_buy_cents,
            "min_taker_sell_cents": plan.min_taker_sell_cents,
        }
        # `structure_id` goes in the TYPED field as well as the rationale.
        # Carried only in the rationale it never reached `orders.structure_id`,
        # so every leg landed with a NULL structure and the two legs of a hedge
        # were indistinguishable from two unrelated directional bets.  Leg
        # tracking, orphan detection and KPI 6 all key on that column.
        sell_leg = DesiredQuote(
            ticker=plan.sell_ticker,
            side=Side.NO,                       # rest a YES ask at this YES price
            price_cents=plan.sell_price_cents,
            size=plan.size,
            post_only=True,
            structure_id=plan.structure_id,
            rationale={**shared, "leg": "sell_yes", "pair_ticker": plan.buy_ticker},
        )
        buy_leg = DesiredQuote(
            ticker=plan.buy_ticker,
            side=Side.YES,
            price_cents=plan.buy_price_cents,
            size=plan.size,
            post_only=True,
            structure_id=plan.structure_id,
            rationale={**shared, "leg": "buy_yes", "pair_ticker": plan.sell_ticker},
        )
        return sell_leg, buy_leg

    def desired_state(self, snapshot: MarketSnapshot) -> DesiredState:
        by_ticker = {m.ticker: m for m in snapshot.markets}
        links, scan = self.resolve_links(snapshot)

        plans: list[StructurePlan] = []
        decisions: list[Decision] = []
        skipped: dict[str, int] = {}

        for link in links:
            plan, reason, violation = self.evaluate(link, snapshot, by_ticker)
            if plan is not None:
                plans.append(plan)
            else:
                skipped[reason] = skipped.get(reason, 0) + 1

            # Record every link that had a readable violation, ACTED ON OR NOT.
            # Un-acted decisions are what make the sleeve's hit rate measurable
            # without survivorship bias (PLAN.md 6.3).
            if violation is None:
                continue
            sell = by_ticker.get(violation.sell_ticker)
            if sell is None or sell.mid is None:
                continue
            ev = snapshot.event_for(sell)
            decisions.append(Decision(
                ticker=violation.sell_ticker,
                market_price=sell.mid,
                # No forecast is involved: the constraint itself says the rich
                # leg is worth at most this much.
                p_model=max(0.0, sell.mid - violation.size),
                raw_edge=violation.size,
                shrunk_edge=(plan.margin.net_cents / 100.0) if plan else 0.0,
                acted=plan is not None,
                category=(ev.category if ev else ""),
            ))

        plans.sort(key=lambda p: (-p.margin.net_cents, p.structure_id))
        # One structure per unordered ticker pair.  A ladder pair can carry both
        # a hard L2 and a soft L4, and resting both would quietly double the size
        # on the same two books while the risk engine sees two unrelated orders.
        # Keep the richest; the sort above has already ranked them.
        best: dict[frozenset[str], StructurePlan] = {}
        deduped: list[StructurePlan] = []
        for plan in plans:
            pair = frozenset((plan.sell_ticker, plan.buy_ticker))
            if pair in best:
                skipped["superseded by a richer structure on the same pair"] = (
                    skipped.get("superseded by a richer structure on the same pair", 0) + 1
                )
                continue
            best[pair] = plan
            deduped.append(plan)

        quotes: list[DesiredQuote] = []
        for plan in deduped[: self.cfg.max_structures]:
            quotes.extend(self.quotes_for(plan, snapshot))

        return DesiredState(
            quotes=tuple(quotes),
            decisions=tuple(decisions),
            rationale={
                "sleeve": self.id,
                "links_considered": len(links),
                "links_verified": sum(
                    1 for lk in links
                    if lk.equivalence_status is Verdict.VERIFIED
                ),
                "structures": min(len(deduped), self.cfg.max_structures),
                "skipped": skipped,
                "ladders_detected": len(scan.ladders),
                "ladders_skipped": scan.reason_counts(),
            },
        )
