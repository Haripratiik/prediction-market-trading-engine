"""Multi-leg structure lifecycle.  T-052.  PLAN.md 3.2, 5, 10.3, 12 KPI 6.

S2 and S3 do not emit orders.  They emit STRUCTURES: two or more legs that are
one risk object, whose margin exists only while every leg is on.  This module
owns the part of that object's life the sleeve cannot see, because the sleeve is
pure and this depends on what actually filled.

THE FAILURE MODE THIS MODULE EXISTS FOR
---------------------------------------
One leg fills and the other does not.  What was designed as a hedged structure
worth a few cents per contract is now a naked directional position at FULL size,
sized by a hedged-risk budget that no longer applies.  A 60c/55c linked-RV pair
is designed to earn 4.15c per contract; the same trade with only the sell leg on
is short 100 contracts of YES at 60c -- $60 of collateral per contract and an
unbounded-in-practice mark.  The designed edge is 4.15c; one cent of adverse
move is 24% of it.  That asymmetry is why PLAN.md 3.2 calls orphan risk "the
sleeve's only real risk" and gives it its own KPI.

STATE MACHINE (the DDL's states, PLAN.md 5)

    forming ---- every leg filled to target -------------> complete
       |                                                      |
       |-- deadline passed, SOME legs filled ---> orphaned    |
       |-- deadline passed, NO leg filled ------> closed      |
                                                              +--> unwinding --> closed

ONCE ORPHANED, ALWAYS ORPHANED.  `monitor.kpi.orphan_loss_ratio` counts rows
whose `state = 'orphaned'`; moving a structure to `unwinding` or `closed` after
its unwind would delete it from KPI 6's numerator and silently drive the ratio
to zero -- the exact statistic going quiet at the exact moment it has something
to report.  So an orphan keeps `state = 'orphaned'` for the rest of its life;
`closed_at_us`, `realized_margin_cents` and `rationale_json['unwind']` record
what happened to it.  `unwinding` is for a structure being flattened that never
orphaned (a complete structure taken off before settlement).

TWO CONVENTIONS THAT ARE NOT NEGOTIABLE
---------------------------------------
1. PRICES ARE YES-REFERENCED -- IN THIS MODULE AND IN `orders`, BUT NOT IN THE
   `fills` COLUMN THIS MODULE READS.  A leg with `side = NO` at
   `orders.price_cents = p` is a resting YES ask at p: it collects p and locks
   100 - p of collateral.  `fills.price_cents` is SIDE-REFERENCED instead --
   `execution.fillfeed._stored_price_cents` writes a NO fill at 100 - p and
   `execution.oms.OMS.position` reads it back the same way -- so every fill
   price entering this module goes through `fill_yes_price_cents()` first.

   Capital comes from `risk.engine.per_contract_cost_cents`, which is THE place
   that rule lives; `leg_cost_cents()` below is its float twin and is tested
   against it at every integer price.  S2 rests NO legs at LOW yes-prices, so
   confusing the two conventions on those legs mis-states the basis by
   (100 - 2p) and the collateral by up to 20x.

2. COMPLETION COMES FROM TERMINAL FILLS (I4 / R5b).  Never a counter, never the
   order's `state`, never the venue's positions endpoint.  A non-terminal fill
   is a claim, not a fact -- Polymarket's MATCHED can later FAIL -- and a
   structure marked complete on a fill that unwinds is a structure nobody is
   watching while it is naked.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not send anything.  `unwind_plan()` and `completion_plan()` return the
decision as DATA -- which legs to close, at what price, as maker or taker, and
what the margin becomes.  The executor is the only component allowed to talk to
a venue (C4.2b), and it is also the only one that enforces risk (I3), so a
module that sent its own unwinds would bypass both.

KNOWN DISAGREEMENT IN THE REST OF THE REPO (reported, not fixed here)
--------------------------------------------------------------------
`execution/executor.py::_to_venue_side` still reads an ORDER's `price_cents` as
the NO price and sends `("ask", 100 - p)` for a NO quote, which contradicts
`risk/engine.py::per_contract_cost_cents`, `execution/fillfeed.py`,
`shadow/engine.py::counterfactual_fill` and both sleeves -- all of which treat
`orders.price_cents` as the YES price on both sides.  An S2 leg resting at a YES
price of 5c reaches the venue as a YES ask at 95c under that mapping, which is a
different order entirely.  This module does not depend on it, because it prices
from `orders`/`fills` rather than from the wire, but the report stands.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from core.config import StructureLimits
from core.db import Database
from core.math.contracts import FeeSpec, fee
from core.models import Market, OrderRequest, OrderState, Side, Venue, now_us
from risk.engine import per_contract_cost_cents
from strategy.base import DesiredQuote

# The overwhelmingly common Kalshi series: 13,353 of 13,486 are plain
# `quadratic`, where a MAKER PAYS ZERO and a taker pays 0.07*p*(1-p)
# (core/math/contracts.py, research/06 section 4).  Used only when the caller
# supplies no per-ticker spec; it is the conservative default for the maker side
# and the correct one for the taker side that an unwind actually pays.
DEFAULT_FEE_SPEC = FeeSpec.kalshi("quadratic", 1.0)

# PLAN.md 3.2 step 4 / config/risk.yaml `structures.leg_timeout_seconds`.
DEFAULT_LEG_TIMEOUT_S = 900


class StructureState(StrEnum):
    """The `structures.state` domain, verbatim from the DDL (core/db.py)."""

    FORMING = "forming"
    COMPLETE = "complete"
    ORPHANED = "orphaned"
    UNWINDING = "unwinding"
    CLOSED = "closed"


class StructureKind(StrEnum):
    """The `structures.kind` domain, verbatim from the DDL (core/db.py)."""

    DUTCH_BOOK = "dutch_book"
    SHORT_BASKET = "short_basket"
    LINKED_RV = "linked_rv"
    HEDGE = "hedge"


class UnwindAction(StrEnum):
    NOTHING_FILLED = "nothing_filled"     # no leg ever filled: cancel and forget
    UNWIND_MAKER = "unwind_maker"         # exposure inside budget: post, do not cross
    UNWIND_TAKER = "unwind_taker"         # PLAN.md 10.3.2: cross to flatten, do not wait
    BLOCKED = "blocked"                   # no book to exit into -- page a human (6.6)


class CompletionAction(StrEnum):
    COMPLETE_AS_TAKER = "complete_as_taker"
    WAIT = "wait"                         # margin does not survive a taker completion
    NOT_APPLICABLE = "not_applicable"     # already complete, or nothing filled yet


# --------------------------------------------------------------------------- #
# Money.  Every number below is CENTS, YES-referenced.
# --------------------------------------------------------------------------- #
def leg_cost_cents(side: Side, yes_price_cents: float) -> float:
    """Capital locked per contract.  The float twin of the ONE canonical rule.

    `risk.engine.per_contract_cost_cents` is that rule and this must agree with
    it at every integer price on both sides -- which is asserted directly, at
    every price in 1..99, in tests/test_structures.py.  The float form exists
    because an average fill price is not an integer: two partial fills at 5c and
    6c average 5.5c, and rounding that to 5c before costing it understates the
    collateral on a leg that locks 94.5c.
    """
    p = float(yes_price_cents)
    return max(0.0, p if side is Side.YES else 100.0 - p)


def fill_yes_price_cents(side: Side, stored_price_cents: float) -> float:
    """`fills.price_cents` -> a YES price.  The inverse of the ONE conversion.

    `execution.fillfeed._stored_price_cents` writes a NO fill SIDE-REFERENCED at
    100 - p, and `execution.oms.OMS.position` reads it back with exactly this
    rule.  Both columns cannot be assumed to share a convention: `orders` is
    YES-referenced and `fills` is not, and reading a NO fill as a YES price puts
    the entry basis out by (100 - 2p) -- 90 cents on the 5c longshot legs S2
    rests, which is most of the contract.
    """
    p = float(stored_price_cents)
    return p if side is Side.YES else 100.0 - p


def leg_fee_cents(yes_price_cents: float, spec: FeeSpec, *, is_maker: bool) -> float:
    """Fee in cents per contract, from `core.math.contracts.fee`.

    The Kalshi fee is theta*p*(1-p), symmetric under p -> 1-p, so a leg is
    charged off its YES price whichever side of the book it sits on.  Prices are
    clamped into the fee model's open interval (0,1): a fill can be reported at
    the boundary and a ValueError from a fee lookup is not an acceptable way for
    an orphan sweep to end.
    """
    p = min(99.0, max(1.0, float(yes_price_cents))) / 100.0
    return 100.0 * fee(p, spec, is_maker=is_maker)


def signed_contracts(side: Side, size: int) -> int:
    """YES-signed exposure: +size long YES, -size short YES (PLAN.md 0.3)."""
    return int(size) if side is Side.YES else -int(size)


def matched_baskets(statuses: Sequence[LegStatus]) -> int:
    """How many COMPLETE baskets the fills actually formed.

    The smallest leg is the binding one: a structure with legs filled 100 and 40
    is hedged as designed on 40 baskets and holds 60 contracts of naked
    directional risk.  Reading "incomplete" as "orphaned" without this would
    flatten those 40 -- crossing two spreads and paying two taker fees to give
    up a locked positive margin.
    """
    if not statuses:
        return 0
    return min(s.effective_size for s in statuses)


def closing_side(side: Side) -> Side:
    """The side of the order that FLATTENS a leg opened on `side`.

    A long YES is closed by resting a YES ask, which in this system's notation
    is `Side.NO` at the YES price being asked.  A short YES is closed by buying
    YES back, which is `Side.YES`.  The prices stay YES-referenced on both.
    """
    return Side.NO if side is Side.YES else Side.YES


# --------------------------------------------------------------------------- #
# Legs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Leg:
    """One leg as the structure was DESIGNED.  `legs_json` is a list of these."""

    ticker: str
    side: Side
    target_size: int
    price_cents: int                      # YES-referenced
    venue: Venue = Venue.KALSHI

    @property
    def key(self) -> tuple[str, str]:
        return (self.ticker, self.side.value)

    @property
    def cost_cents(self) -> int:
        """Collateral the full leg locks, from the canonical integer rule."""
        return per_contract_cost_cents(self.side, self.price_cents) * self.target_size

    def as_json(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "side": self.side.value,
            "target_size": self.target_size,
            "price_cents": self.price_cents,
            "venue": self.venue.value,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "Leg":
        return cls(
            ticker=str(raw["ticker"]),
            side=Side(raw["side"]),
            target_size=int(raw["target_size"]),
            price_cents=int(raw["price_cents"]),
            venue=Venue(raw.get("venue", Venue.KALSHI.value)),
        )

    @classmethod
    def from_quote(cls, quote: DesiredQuote, *, venue: Venue = Venue.KALSHI) -> "Leg":
        return cls(quote.ticker, quote.side, quote.size, quote.price_cents, venue)


@dataclass(frozen=True, slots=True)
class LegStatus:
    """What a leg has ACTUALLY done, reconstructed from fills (I4)."""

    leg: Leg
    filled_size: int = 0                  # terminal fills only
    unconfirmed_size: int = 0             # non-terminal: a claim, not a fact
    fee_cents: int = 0                    # actual, signed (negative = rebate)
    notional_cents: float = 0.0           # sum(yes_price * size) over terminal fills
    settlement_voided: bool = False
    order_ids: tuple[str, ...] = ()
    open_order_ids: tuple[str, ...] = ()

    @property
    def avg_price_cents(self) -> float:
        """YES-referenced average entry.  Falls back to the design price.

        `notional_cents` is already converted out of the side-referenced
        `fills.price_cents` (see `fill_yes_price_cents`), so this agrees with
        `OMS.position(ticker).avg_price_cents` on the same rows.

        The fallback is the DESIGN price, never zero: an average of nothing is
        undefined, and zero would report a leg acquired for free.
        """
        if self.filled_size <= 0:
            return float(self.leg.price_cents)
        return self.notional_cents / float(self.filled_size)

    @property
    def is_filled(self) -> bool:
        """Filled to target AND still a real position.

        A voided market returns the stake: the contracts are gone, so the leg is
        not on however many fills it reported.  Treating a voided leg as filled
        would mark a structure complete whose hedge no longer exists.
        """
        return (
            not self.settlement_voided
            and self.filled_size >= self.leg.target_size
            and self.leg.target_size > 0
        )

    @property
    def has_position(self) -> bool:
        return self.filled_size > 0 and not self.settlement_voided

    @property
    def effective_size(self) -> int:
        """Contracts actually held.  A voided market holds none, whatever filled."""
        return 0 if self.settlement_voided else self.filled_size

    @property
    def signed_size(self) -> int:
        return signed_contracts(self.leg.side, self.effective_size)

    @property
    def exposure_cents(self) -> float:
        """Collateral this leg's ACTUAL fill locks, at its actual average price."""
        if not self.has_position:
            return 0.0
        return leg_cost_cents(self.leg.side, self.avg_price_cents) * self.effective_size

    def naked_size(self, matched: int) -> int:
        """Contracts on this leg that NO other leg offsets.

        `matched` complete baskets are hedged exactly as designed and are worth
        keeping; only the residual above them is the directional position the
        structure never intended to hold.
        """
        return max(0, self.effective_size - matched)

    def naked_exposure_cents(self, matched: int) -> float:
        return leg_cost_cents(self.leg.side, self.avg_price_cents) * self.naked_size(matched)


# --------------------------------------------------------------------------- #
# Intents and rows
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class StructureIntent:
    """A structure the sleeve wants to exist, ready to persist."""

    structure_id: str
    sleeve_id: str
    kind: StructureKind
    legs: tuple[Leg, ...]
    target_margin_cents: float = 0.0      # TOTAL cents for the structure, not per contract
    designed_fee_cents: float = 0.0       # TOTAL modeled entry fee, for slippage accounting
    unwind_deadline_us: int | None = None
    event_ticker: str | None = None
    rationale: dict[str, Any] = field(default_factory=dict)

    @property
    def n_legs(self) -> int:
        return len(self.legs)

    @property
    def size(self) -> int:
        """One "basket": the smallest leg, since that is what can complete."""
        return min((leg.target_size for leg in self.legs), default=0)

    @classmethod
    def from_quotes(
        cls,
        quotes: Sequence[DesiredQuote],
        *,
        sleeve_id: str,
        now: int,
        kind: StructureKind | None = None,
        venue: Venue = Venue.KALSHI,
        default_timeout_s: int = DEFAULT_LEG_TIMEOUT_S,
    ) -> "StructureIntent":
        """Rebuild the structure from the legs a sleeve emitted.

        This reads `DesiredQuote.structure_id` FIRST and the rationale key second,
        because S2 and S3 currently populate only the rationale -- see the report
        in the module header.  Lifting a structure id back out of a free-form
        dict by string key is how a leg silently loses its partner, so the
        fallback is deliberate, narrow, and asserts agreement across the legs.
        """
        if not quotes:
            raise ValueError("a structure needs at least one leg")
        ids = {_structure_id_of(q) for q in quotes}
        if len(ids) != 1:
            raise ValueError(f"legs disagree on structure_id: {sorted(ids)}")
        sid = ids.pop()
        if not sid:
            raise ValueError(
                "no structure_id on the quotes: neither DesiredQuote.structure_id "
                "nor rationale['structure_id'] is set, so these legs cannot be "
                "bound into one risk object"
            )

        legs = tuple(Leg.from_quote(q, venue=venue) for q in quotes)
        size = min(leg.target_size for leg in legs)
        rationale: dict[str, Any] = {}
        for q in quotes:
            rationale.update(q.rationale)

        deadline = rationale.get("unwind_deadline_us")
        if deadline is None:
            timeout_s = int(rationale.get("leg_timeout_seconds", default_timeout_s))
            deadline = now + timeout_s * 1_000_000

        return cls(
            structure_id=sid,
            sleeve_id=sleeve_id,
            kind=kind or _infer_kind(rationale),
            legs=legs,
            target_margin_cents=_per_contract_cents(
                rationale, "net_cents", "margin") * size,
            designed_fee_cents=_per_contract_cents(
                rationale, "fee_cents", "fees") * size,
            unwind_deadline_us=int(deadline),
            event_ticker=rationale.get("event_ticker"),
            rationale=rationale,
        )


@dataclass(frozen=True, slots=True)
class StructureRecord:
    """One row of `structures`, decoded."""

    structure_id: str
    created_at_us: int
    sleeve_id: str
    kind: str
    event_ticker: str | None
    legs: tuple[Leg, ...]
    n_legs: int
    state: StructureState
    target_margin_cents: float
    realized_margin_cents: float | None
    unwind_deadline_us: int | None
    closed_at_us: int | None
    rationale: dict[str, Any]

    @property
    def designed_fee_cents(self) -> float:
        return float(self.rationale.get("designed_fee_cents", 0.0) or 0.0)

    @property
    def is_terminal(self) -> bool:
        return self.state is StructureState.CLOSED or self.closed_at_us is not None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "StructureRecord":
        return cls(
            structure_id=row["structure_id"],
            created_at_us=int(row["created_at_us"]),
            sleeve_id=row["sleeve_id"],
            kind=row["kind"],
            event_ticker=row["event_ticker"],
            legs=tuple(Leg.from_json(d) for d in json.loads(row["legs_json"] or "[]")),
            n_legs=int(row["n_legs"]),
            state=StructureState(row["state"]),
            target_margin_cents=float(row["target_margin_cents"] or 0.0),
            realized_margin_cents=(
                None if row["realized_margin_cents"] is None
                else float(row["realized_margin_cents"])
            ),
            unwind_deadline_us=(
                None if row["unwind_deadline_us"] is None
                else int(row["unwind_deadline_us"])
            ),
            closed_at_us=(
                None if row["closed_at_us"] is None else int(row["closed_at_us"])
            ),
            rationale=json.loads(row["rationale_json"] or "{}"),
        )


@dataclass(frozen=True, slots=True)
class OpenResult:
    """What `open_with_intents` actually wrote.  Send ONLY `new_order_ids`."""

    structure_id: str
    created: bool
    new_order_ids: tuple[str, ...] = ()
    replayed_order_ids: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Plans -- returned as DATA.  Nothing here sends an order (C4.2b).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class UnwindLeg:
    """One closing order, priced, with the money it realizes."""

    ticker: str
    side: Side                            # the CLOSING side
    size: int
    price_cents: int                      # YES-referenced exit
    post_only: bool
    entry_price_cents: float              # YES-referenced average entry
    entry_fee_cents: float                # actual, signed
    exit_fee_cents: float                 # modeled at the exit price
    pnl_cents: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "side": self.side.value,
            "size": self.size,
            "price_cents": self.price_cents,
            "post_only": self.post_only,
            "entry_price_cents": round(self.entry_price_cents, 4),
            "entry_fee_cents": round(self.entry_fee_cents, 4),
            "exit_fee_cents": round(self.exit_fee_cents, 4),
            "pnl_cents": round(self.pnl_cents, 4),
        }


@dataclass(frozen=True, slots=True)
class UnwindPlan:
    """What to do with an orphan's filled legs.  A decision, not an action."""

    structure_id: str
    sleeve_id: str
    action: UnwindAction
    legs: tuple[UnwindLeg, ...] = ()
    cancel_order_ids: tuple[str, ...] = ()
    blocked_tickers: tuple[str, ...] = ()
    target_margin_cents: float = 0.0
    realized_margin_cents: float = 0.0
    naked_exposure_cents: float = 0.0
    orphan_budget_cents: float = 0.0
    exceeds_orphan_budget: bool = False
    matched_baskets: int = 0              # complete baskets that DID form
    surviving_margin_cents: float = 0.0   # the designed margin those still earn

    @property
    def loss_cents(self) -> float:
        """The designed margin, minus what survives, minus the naked outcome.

        This is the economic cost of the orphan: edge forgone plus money lost.
        It is NOT what KPI 6 sums -- `monitor.kpi.orphan_loss_ratio` takes the
        negative part of `realized_margin_cents` alone, i.e. money actually lost.
        Both are reported because they answer different questions: "what did leg
        risk cost us" and "what share of the edge did it eat".
        """
        return (self.target_margin_cents
                - self.surviving_margin_cents
                - self.realized_margin_cents)

    @property
    def is_priced(self) -> bool:
        return self.action is not UnwindAction.BLOCKED

    def as_dict(self) -> dict[str, Any]:
        return {
            "structure_id": self.structure_id,
            "sleeve_id": self.sleeve_id,
            "action": self.action.value,
            "legs": [leg.as_dict() for leg in self.legs],
            "cancel_order_ids": list(self.cancel_order_ids),
            "blocked_tickers": list(self.blocked_tickers),
            "target_margin_cents": round(self.target_margin_cents, 4),
            "realized_margin_cents": round(self.realized_margin_cents, 4),
            "loss_cents": round(self.loss_cents, 4),
            "naked_exposure_cents": round(self.naked_exposure_cents, 4),
            "orphan_budget_cents": round(self.orphan_budget_cents, 4),
            "exceeds_orphan_budget": self.exceeds_orphan_budget,
            "matched_baskets": self.matched_baskets,
            "surviving_margin_cents": round(self.surviving_margin_cents, 4),
        }


@dataclass(frozen=True, slots=True)
class CompletionLeg:
    ticker: str
    side: Side
    size: int
    price_cents: int                      # YES-referenced taker price
    limit_cents: int | None               # the sleeve's published worst price
    within_limit: bool


@dataclass(frozen=True, slots=True)
class CompletionPlan:
    """Finish a half-filled structure as a taker, if the margin survives.

    PLAN.md 3.2 step 3.  S3 publishes `max_taker_buy_cents` and
    `min_taker_sell_cents` with the quotes precisely so this call is arithmetic
    rather than a re-derivation under time pressure, and so "it will probably
    converge anyway" is an inadmissible answer.
    """

    structure_id: str
    sleeve_id: str
    action: CompletionAction
    completion: float                     # filled legs / n legs
    legs: tuple[CompletionLeg, ...] = ()
    threshold: float = 0.6
    reason: str = ""


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
@dataclass
class StructureStore:
    """The `structures` table, plus the lifecycle that keeps it honest."""

    db: Database
    venue: Venue = Venue.KALSHI
    limits: StructureLimits = field(default_factory=StructureLimits)

    # ------------------------------------------------------------------ open
    def open(self, intent: StructureIntent, *, at_us: int | None = None) -> bool:
        """Write the structure row.  True when it is NEW.

        Returns False on a re-emission.  S2 and S3 both mint deterministic,
        PRICE-BEARING ids (`_structure_id` / `f"{id}:{link_id}@{sell}/{buy}"`),
        so a sleeve that still wants the same structure next cycle produces the
        same id -- and re-opening it must NOT reset the deadline.  A deadline
        that refreshes every cycle is a deadline that never fires, which turns
        the orphan timeout off exactly while the structure is failing to fill.
        """
        with self.db.tx() as c:
            return self._insert(c, intent, at_us=at_us)

    def open_with_intents(
        self,
        intent: StructureIntent,
        requests: Sequence[OrderRequest],
        *,
        at_us: int | None = None,
    ) -> OpenResult:
        """Structure row AND every leg's order intent, in ONE transaction.

        The ordering guarantee the OMS gives for a single order (the intent row
        exists before the network call) is not enough for a structure: an order
        that reaches a venue carrying a `structure_id` no row knows about is a
        leg with no lifecycle manager, no deadline and no unwind -- exactly the
        naked position this module exists to prevent.  Writing both sides in one
        transaction means there is no window in which that can be true.

        Returns the ids that were NEW.  Those are the ones that may be sent; the
        replayed ones must not be, for the T-041 reason.  Note that the intents
        are already recorded, so these must be handed to the executor's send
        path rather than to `Executor.submit()`, which would record them a
        second time, see a conflict and report REPLAY.
        """
        mismatched = [
            r.client_order_id for r in requests
            if r.structure_id != intent.structure_id
        ]
        if mismatched:
            raise ValueError(
                f"orders {mismatched} do not carry structure_id "
                f"{intent.structure_id!r}; a leg that loses its structure id is "
                "a naked directional bet wearing an arbitrage's clothes"
            )
        ts = now_us() if at_us is None else at_us
        new: list[str] = []
        replayed: list[str] = []
        with self.db.tx() as c:
            created = self._insert(c, intent, at_us=ts)
            for req in requests:
                cur = c.execute(
                    """INSERT INTO orders
                       (client_order_id, created_at_us, sleeve_id, structure_id, venue,
                        ticker, side, price_cents, size, post_only, mode, venue_order_id,
                        state, rationale_json, updated_at_us)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(client_order_id) DO NOTHING""",
                    (
                        req.client_order_id, ts, req.sleeve_id, req.structure_id,
                        req.venue.value, req.ticker, req.side.value, req.price_cents,
                        req.size, int(req.post_only), req.mode.value, None,
                        OrderState.PENDING.value,
                        json.dumps(req.rationale, default=str),
                        ts,
                    ),
                )
                (new if cur.rowcount == 1 else replayed).append(req.client_order_id)
        return OpenResult(intent.structure_id, created, tuple(new), tuple(replayed))

    def _insert(self, c: sqlite3.Connection, intent: StructureIntent,
                *, at_us: int | None) -> bool:
        ts = now_us() if at_us is None else at_us
        rationale = {
            **intent.rationale,
            "designed_fee_cents": intent.designed_fee_cents,
            "opened_at_us": ts,
        }
        cur = c.execute(
            """INSERT INTO structures
               (structure_id, created_at_us, sleeve_id, kind, event_ticker,
                legs_json, n_legs, state, target_margin_cents,
                realized_margin_cents, unwind_deadline_us, closed_at_us,
                rationale_json)
               VALUES (?,?,?,?,?,?,?,?,?,NULL,?,NULL,?)
               ON CONFLICT(structure_id) DO NOTHING""",
            (
                intent.structure_id, ts, intent.sleeve_id,
                StructureKind(intent.kind).value, intent.event_ticker,
                json.dumps([leg.as_json() for leg in intent.legs]),
                intent.n_legs, StructureState.FORMING.value,
                float(intent.target_margin_cents), intent.unwind_deadline_us,
                json.dumps(rationale, default=str),
            ),
        )
        return cur.rowcount == 1

    # ------------------------------------------------------------------ read
    def get(self, structure_id: str) -> StructureRecord | None:
        row = self.db.conn.execute(
            "SELECT * FROM structures WHERE structure_id = ?", (structure_id,)
        ).fetchone()
        return StructureRecord.from_row(row) if row else None

    def by_state(self, *states: StructureState,
                 sleeve_id: str | None = None) -> list[StructureRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if states:
            clauses.append(f"state IN ({','.join('?' * len(states))})")
            params.extend(StructureState(s).value for s in states)
        if sleeve_id is not None:
            clauses.append("sleeve_id = ?")
            params.append(sleeve_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.conn.execute(
            f"SELECT * FROM structures{where} ORDER BY created_at_us, structure_id",
            params,
        ).fetchall()
        return [StructureRecord.from_row(r) for r in rows]

    def legs_from_orders(self, structure_id: str) -> tuple[Leg, ...]:
        """Rebuild the design from the order rows.

        The recovery path: a structure row lost, or an order carrying a
        structure_id written before the structure row existed.  Legs are keyed
        on (ticker, side), so a re-quote of the same leg at a new price collapses
        into one leg at the price of the first order.
        """
        rows = self.db.conn.execute(
            """SELECT ticker, side, venue, size, price_cents
               FROM orders WHERE structure_id = ?
               ORDER BY created_at_us, client_order_id""",
            (structure_id,),
        ).fetchall()
        seen: dict[tuple[str, str], Leg] = {}
        for r in rows:
            key = (r["ticker"], r["side"])
            if key in seen:
                continue
            seen[key] = Leg(r["ticker"], Side(r["side"]), int(r["size"] or 0),
                            int(r["price_cents"]), Venue(r["venue"]))
        return tuple(seen.values())

    # ------------------------------------------------------- leg completion
    def leg_status(self, structure_id: str,
                   legs: Sequence[Leg] | None = None) -> tuple[LegStatus, ...]:
        """Per-leg truth, aggregated over TERMINAL FILLS ONLY (I4).

        Every number here comes from rows that were persisted before anything
        acted on them.  Nothing consults the order's `state`: an order can be
        marked filled and later be found not to have been, and a structure whose
        completion is read off a status flag is a structure that can believe it
        is hedged while it is naked.
        """
        rec = self.get(structure_id)
        design = tuple(legs) if legs is not None else (
            rec.legs if rec is not None else self.legs_from_orders(structure_id)
        )
        if not design:
            return ()

        rows = self.db.conn.execute(
            """SELECT o.ticker AS ticker,
                      o.side   AS side,
                      o.client_order_id AS coid,
                      o.state  AS ostate,
                      COALESCE(SUM(CASE WHEN f.terminal = 1
                                        THEN f.size END), 0) AS filled,
                      COALESCE(SUM(CASE WHEN f.terminal = 0
                                        THEN f.size END), 0) AS unconfirmed,
                      COALESCE(SUM(CASE WHEN f.terminal = 1
                                        THEN f.fee_cents END), 0) AS fee,
                      -- `fills.price_cents` is SIDE-referenced while everything
                      -- downstream of here is YES-referenced, so the conversion
                      -- happens once, at the read, and never again.
                      COALESCE(SUM(CASE WHEN f.terminal = 1
                                        THEN (CASE WHEN o.side = 'yes'
                                                   THEN f.price_cents
                                                   ELSE 100 - f.price_cents END)
                                             * f.size END), 0) AS notional
               FROM orders o
               LEFT JOIN fills f ON f.client_order_id = o.client_order_id
               WHERE o.structure_id = ?
               GROUP BY o.client_order_id""",
            (structure_id,),
        ).fetchall()

        agg: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            slot = agg.setdefault(
                (r["ticker"], r["side"]),
                {"filled": 0, "unconfirmed": 0, "fee": 0, "notional": 0.0,
                 "orders": [], "open": []},
            )
            slot["filled"] += int(r["filled"] or 0)
            slot["unconfirmed"] += int(r["unconfirmed"] or 0)
            slot["fee"] += int(r["fee"] or 0)
            slot["notional"] += float(r["notional"] or 0.0)
            slot["orders"].append(r["coid"])
            if OrderState(r["ostate"]) in (OrderState.PENDING, OrderState.OPEN,
                                           OrderState.PARTIAL):
                slot["open"].append(r["coid"])

        voided = self._voided_tickers([leg.ticker for leg in design])
        out: list[LegStatus] = []
        for leg in design:
            slot = agg.get(leg.key, {})
            out.append(LegStatus(
                leg=leg,
                filled_size=int(slot.get("filled", 0)),
                unconfirmed_size=int(slot.get("unconfirmed", 0)),
                fee_cents=int(slot.get("fee", 0)),
                notional_cents=float(slot.get("notional", 0.0)),
                settlement_voided=leg.ticker in voided,
                order_ids=tuple(slot.get("orders", ())),
                open_order_ids=tuple(slot.get("open", ())),
            ))
        return tuple(out)

    def _voided_tickers(self, tickers: Sequence[str]) -> frozenset[str]:
        """Markets the venue voided.  A voided contract returns the stake.

        A leg whose market was voided is NOT a position, however many fills it
        reported: the money came back and the hedge it was providing is gone.
        Counting it as filled would mark a structure complete whose other leg is
        now, in fact, naked.
        """
        if not tickers:
            return frozenset()
        rows = self.db.conn.execute(
            f"""SELECT ticker FROM settlements
                WHERE voided = 1 AND venue = ?
                  AND ticker IN ({','.join('?' * len(tickers))})""",
            [self.venue.value, *tickers],
        ).fetchall()
        return frozenset(r["ticker"] for r in rows)

    # ------------------------------------------------------- the state machine
    def is_orphaned(self, statuses: Sequence[LegStatus], deadline_us: int | None,
                    *, now: int) -> bool:
        """UNBALANCED legs and the deadline is PAST.  PLAN.md 3.2 step 4.

        Strictly past: at exactly the deadline the structure still has its full
        timeout, and a `>=` here would orphan a structure one microsecond early
        -- which in a maker book is the difference between being filled and
        paying a spread to flatten a position you were about to hedge for free.

        The test is UNBALANCED, not merely incomplete, and the difference is
        money.  Both legs filled 40 of 100 is a smaller structure, hedged
        exactly as designed and earning its margin on those 40; declaring it an
        orphan would cross two spreads and pay two taker fees to give up a
        locked positive margin.  Nothing filled is not naked either -- it is
        unfilled, and unfilled is free.  The orphan is the residual that no
        other leg offsets, and it is the only exposure the sleeve never sized.
        """
        if deadline_us is None or now <= deadline_us:
            return False
        return matched_baskets(statuses) < max(
            (s.effective_size for s in statuses), default=0)

    def refresh(
        self,
        structure_id: str,
        *,
        now: int | None = None,
        books: Mapping[str, Market] | None = None,
        bankroll_cents: int = 0,
        fee_specs: Mapping[str, FeeSpec] | None = None,
    ) -> StructureRecord | None:
        """Advance one structure to the state its FILLS say it is in.

        Idempotent: calling it twice changes nothing the second time.  When a
        structure orphans and `books` are supplied, the mark is priced and
        written in the same step, because PLAN.md 10.3 step 3 wants the realized
        loss logged AT detection -- an orphan sitting in the table with a NULL
        margin is invisible to KPI 6, which is the moment the KPI most needs to
        speak up.
        """
        rec = self.get(structure_id)
        if rec is None or rec.is_terminal:
            # Its life is over.  A fill arriving after an unwind is a NEW
            # position, not a late completion of a structure already accounted
            # for, and quietly folding it back in would double-count the margin.
            return rec
        ts = now_us() if now is None else now
        statuses = self.leg_status(structure_id, rec.legs)
        if not statuses:
            return rec

        if all(s.is_filled for s in statuses):
            if rec.state is StructureState.COMPLETE:
                return rec
            realized = self.realized_complete_margin(rec, statuses)
            return self._set_state(
                rec, StructureState.COMPLETE,
                realized_margin_cents=realized,
                note={"completed_at_us": ts,
                      "price_slippage_cents": round(
                          _price_slippage_cents(statuses), 4)},
            )

        if rec.state in (StructureState.ORPHANED, StructureState.UNWINDING):
            return rec

        if self.is_orphaned(statuses, rec.unwind_deadline_us, now=ts):
            mark: float | None = None
            note: dict[str, Any] = {
                "orphaned_at_us": ts,
                "legs_filled": sum(1 for s in statuses if s.is_filled),
                "n_legs": len(statuses),
            }
            if books is not None:
                plan = self.unwind_plan(
                    structure_id, books=books, bankroll_cents=bankroll_cents,
                    fee_specs=fee_specs, now=ts, statuses=statuses, record=rec,
                )
                if plan.is_priced:
                    mark = plan.realized_margin_cents
                note["unwind"] = plan.as_dict()
            return self._set_state(rec, StructureState.ORPHANED,
                                   realized_margin_cents=mark, note=note)

        deadline = rec.unwind_deadline_us
        if deadline is not None and ts > deadline:
            # Past the deadline and BALANCED: either nothing filled (never a
            # risk object) or every leg filled equally below target (a smaller
            # structure, hedged as designed).  Closed rather than left forming
            # forever, so the sweep does not reconsider it every cycle.
            matched = matched_baskets(statuses)
            size = _basket_size(rec)
            fraction = (matched / size) if size > 0 else 0.0
            return self._set_state(
                rec, StructureState.CLOSED,
                realized_margin_cents=(
                    0.0 if matched == 0
                    else self.realized_hedged_margin(rec, statuses,
                                                     fraction=fraction)),
                closed_at_us=ts,
                note={"reason": "expired_unfilled" if matched == 0
                      else "expired_balanced",
                      "matched_baskets": matched},
            )
        return rec

    def sweep(
        self,
        *,
        now: int | None = None,
        sleeve_id: str | None = None,
        books: Mapping[str, Market] | None = None,
        bankroll_cents: int = 0,
        fee_specs: Mapping[str, FeeSpec] | None = None,
    ) -> list[StructureRecord]:
        """Refresh every live structure.  Returns only the ones that CHANGED."""
        ts = now_us() if now is None else now
        live = self.by_state(StructureState.FORMING, StructureState.COMPLETE,
                             sleeve_id=sleeve_id)
        changed: list[StructureRecord] = []
        for rec in live:
            after = self.refresh(rec.structure_id, now=ts, books=books,
                                 bankroll_cents=bankroll_cents, fee_specs=fee_specs)
            if after is not None and after.state is not rec.state:
                changed.append(after)
        return changed

    # ----------------------------------------------------------- economics
    def realized_hedged_margin(self, rec: StructureRecord,
                               statuses: Sequence[LegStatus],
                               *, fraction: float = 1.0) -> float:
        """What the HEDGED part of a structure actually earned, in total cents.

        The settlement half of a structure's payoff does not depend on how it was
        filled -- one leg of a MECE basket wins either way -- so the only things
        that move the realized margin away from the designed one are the prices
        the legs actually got and the fees actually charged:

            realized = fraction * (target + designed_fee)
                       + price_slippage - actual_fee

        Both corrections are real money read off the `fills` rows, which is what
        makes "realized versus modeled margin" (PLAN.md 12) a measurement rather
        than a restatement of the model.  `fraction` is the share of the designed
        basket count that actually formed: a structure that filled 40 of 100 on
        every leg earned 40% of the design and paid 100% of the fees on those 40.
        """
        actual_fee = float(sum(s.fee_cents for s in statuses))
        return (fraction * (rec.target_margin_cents + rec.designed_fee_cents)
                + _price_slippage_cents(statuses)
                - actual_fee)

    def realized_complete_margin(self, rec: StructureRecord,
                                 statuses: Sequence[LegStatus]) -> float:
        """Every leg filled to target: the whole design, realized."""
        return self.realized_hedged_margin(rec, statuses, fraction=1.0)

    def unwind_plan(
        self,
        structure_id: str,
        *,
        books: Mapping[str, Market],
        bankroll_cents: int = 0,
        limits: StructureLimits | None = None,
        fee_spec: FeeSpec | None = None,
        fee_specs: Mapping[str, FeeSpec] | None = None,
        now: int | None = None,
        statuses: Sequence[LegStatus] | None = None,
        record: StructureRecord | None = None,
    ) -> UnwindPlan:
        """What to do with the filled legs of an orphan.  PURE -- it sends nothing.

        The decision has two parts and PLAN.md 3.2 step 4 / 10.3 step 2 fix both:

          WHICH legs      exactly the ones holding a position.  A leg that never
                          filled has nothing to close; its resting orders are
                          cancelled instead, which is why `cancel_order_ids` is
                          part of the plan rather than an afterthought.
          MAKER or TAKER  maker while the residual directional exposure is inside
                          `max_orphan_exposure_fraction` of bankroll, taker the
                          moment it is not: "cross the spread to flatten.  Do not
                          wait."  Posting is cheaper and slower, and slower is
                          only acceptable while the position is small.

        Exit prices are YES-referenced and read off the touch: closing a long YES
        means selling YES, so a maker posts at `yes_ask` and a taker hits
        `yes_bid`; closing a short YES is the mirror.  A leg with no usable side
        of the book cannot be priced at all -- the plan says BLOCKED rather than
        inventing a number, because a fabricated exit price would flow straight
        into KPI 6 as a fabricated loss.
        """
        rec = record if record is not None else self.get(structure_id)
        if rec is None:
            raise KeyError(f"unknown structure {structure_id!r}")
        lim = limits or self.limits
        stats = tuple(statuses) if statuses is not None else self.leg_status(
            structure_id, rec.legs)
        spec_default = fee_spec or DEFAULT_FEE_SPEC
        specs = dict(fee_specs or {})

        matched = matched_baskets(stats)
        size = _basket_size(rec)
        surviving = rec.target_margin_cents * ((matched / size) if size > 0 else 0.0)
        naked = [s for s in stats if s.naked_size(matched) > 0]
        # Every resting order on the structure: the design is not going to
        # complete, so a leg still trying to fill would re-open the exposure the
        # unwind is closing.  An order that already filled is terminal and has
        # nothing left to cancel.
        cancels = tuple(coid for s in stats for coid in s.open_order_ids)
        exposure = float(sum(s.naked_exposure_cents(matched) for s in stats))
        budget = float(bankroll_cents) * lim.max_orphan_exposure_fraction
        over = exposure > budget

        if not naked:
            return UnwindPlan(
                structure_id=rec.structure_id, sleeve_id=rec.sleeve_id,
                action=UnwindAction.NOTHING_FILLED, cancel_order_ids=cancels,
                target_margin_cents=rec.target_margin_cents,
                realized_margin_cents=0.0, orphan_budget_cents=budget,
                matched_baskets=matched, surviving_margin_cents=surviving,
            )

        legs: list[UnwindLeg] = []
        blocked: list[str] = []
        for s in naked:
            book = books.get(s.leg.ticker)
            exit_px, is_taker = _exit_price(book, s.leg.side, prefer_taker=over)
            if exit_px is None:
                blocked.append(s.leg.ticker)
                continue
            spec = specs.get(s.leg.ticker, spec_default)
            n = s.naked_size(matched)
            entry = s.avg_price_cents
            exit_fee = leg_fee_cents(exit_px, spec, is_maker=not is_taker) * n
            # Only the naked contracts' share of the entry fee belongs to the
            # unwind; the fee on the matched baskets was paid for margin we keep.
            entry_fee = float(s.fee_cents) * (n / s.effective_size)
            pnl = (float(signed_contracts(s.leg.side, n)) * (float(exit_px) - entry)
                   - entry_fee - exit_fee)
            legs.append(UnwindLeg(
                ticker=s.leg.ticker,
                side=closing_side(s.leg.side),
                size=n,
                price_cents=int(exit_px),
                post_only=not is_taker,
                entry_price_cents=entry,
                entry_fee_cents=entry_fee,
                exit_fee_cents=exit_fee,
                pnl_cents=pnl,
            ))

        if blocked:
            action = UnwindAction.BLOCKED
        elif over:
            action = UnwindAction.UNWIND_TAKER
        else:
            action = UnwindAction.UNWIND_MAKER

        return UnwindPlan(
            structure_id=rec.structure_id,
            sleeve_id=rec.sleeve_id,
            action=action,
            legs=tuple(legs),
            cancel_order_ids=cancels,
            blocked_tickers=tuple(blocked),
            target_margin_cents=rec.target_margin_cents,
            realized_margin_cents=float(sum(leg.pnl_cents for leg in legs)),
            naked_exposure_cents=exposure,
            orphan_budget_cents=budget,
            exceeds_orphan_budget=over,
            matched_baskets=matched,
            surviving_margin_cents=surviving,
        )

    def completion_plan(
        self,
        structure_id: str,
        *,
        books: Mapping[str, Market],
        threshold: float | None = None,
        statuses: Sequence[LegStatus] | None = None,
    ) -> CompletionPlan:
        """Cross the spread to FINISH a half-filled structure.  PLAN.md 3.2 step 3.

        Preferred to an unwind whenever it is available: completing keeps the
        designed margin, unwinding pays a spread to give it up.  It is admissible
        only while the remaining legs can be taken at prices the sleeve already
        certified preserve the margin -- `max_taker_buy_cents` for a leg we must
        buy, `min_taker_sell_cents` for one we must sell.  Without a published
        limit the answer is WAIT: a completion at an unchecked price is just a
        new directional trade at the worst possible moment.
        """
        rec = self.get(structure_id)
        if rec is None:
            raise KeyError(f"unknown structure {structure_id!r}")
        stats = tuple(statuses) if statuses is not None else self.leg_status(
            structure_id, rec.legs)
        thresh = (rec.rationale.get("completion_taker_threshold", 0.6)
                  if threshold is None else threshold)
        thresh = float(thresh)

        done = [s for s in stats if s.is_filled]
        todo = [s for s in stats if not s.is_filled]
        completion = (len(done) / len(stats)) if stats else 0.0
        if not todo or not done:
            return CompletionPlan(
                rec.structure_id, rec.sleeve_id, CompletionAction.NOT_APPLICABLE,
                completion, threshold=thresh,
                reason="complete" if not todo else "no leg has filled",
            )
        if completion < thresh:
            return CompletionPlan(
                rec.structure_id, rec.sleeve_id, CompletionAction.WAIT, completion,
                threshold=thresh,
                reason=f"completion {completion:.2f} below {thresh:.2f}",
            )

        max_buy = rec.rationale.get("max_taker_buy_cents")
        min_sell = rec.rationale.get("min_taker_sell_cents")
        legs: list[CompletionLeg] = []
        for s in todo:
            book = books.get(s.leg.ticker)
            need = s.leg.target_size - s.filled_size
            # We still have to acquire this leg, so we pay the aggressive side:
            # a YES leg lifts the ask, a NO leg (a YES ask we owe) hits the bid.
            if s.leg.side is Side.YES:
                px = book.yes_ask if book is not None and book.has_ask else None
                limit = None if max_buy is None else int(max_buy)
                ok = px is not None and limit is not None and px <= limit
            else:
                px = book.yes_bid if book is not None and book.has_bid else None
                limit = None if min_sell is None else int(min_sell)
                ok = px is not None and limit is not None and px >= limit
            if px is None:
                return CompletionPlan(
                    rec.structure_id, rec.sleeve_id, CompletionAction.WAIT,
                    completion, threshold=thresh,
                    reason=f"no book on {s.leg.ticker}",
                )
            legs.append(CompletionLeg(s.leg.ticker, s.leg.side, need, int(px),
                                      limit, bool(ok)))

        if all(leg.within_limit for leg in legs):
            return CompletionPlan(
                rec.structure_id, rec.sleeve_id, CompletionAction.COMPLETE_AS_TAKER,
                completion, tuple(legs), thresh,
                "taker completion preserves the structure margin",
            )
        return CompletionPlan(
            rec.structure_id, rec.sleeve_id, CompletionAction.WAIT, completion,
            tuple(legs), thresh,
            "taker completion would not preserve the structure margin",
        )

    # -------------------------------------------------------------- closing
    def record_unwind(self, plan: UnwindPlan, *, at_us: int | None = None,
                      closed: bool = True) -> StructureRecord | None:
        """Persist an unwind's outcome.  An orphan STAYS `orphaned`.

        See the module header: KPI 6 sums rows whose state is `orphaned`, so
        moving one to `closed` on the way out would erase it from the numerator.
        The end of its life is recorded in `closed_at_us` instead, and the plan
        itself is kept in the rationale so the post-mortem reads one row.
        """
        rec = self.get(plan.structure_id)
        if rec is None:
            return None
        ts = now_us() if at_us is None else at_us
        if rec.state is StructureState.ORPHANED:
            state = StructureState.ORPHANED
        else:
            state = StructureState.CLOSED if closed else StructureState.UNWINDING
        return self._set_state(
            rec, state,
            realized_margin_cents=plan.realized_margin_cents,
            closed_at_us=ts if closed else None,
            note={"unwind": plan.as_dict(), "unwound_at_us": ts},
        )

    def close(self, structure_id: str, *, realized_margin_cents: float | None = None,
              at_us: int | None = None, reason: str = "") -> StructureRecord | None:
        """End a structure's life.  An orphan keeps its state (see `record_unwind`)."""
        rec = self.get(structure_id)
        if rec is None:
            return None
        ts = now_us() if at_us is None else at_us
        state = (StructureState.ORPHANED if rec.state is StructureState.ORPHANED
                 else StructureState.CLOSED)
        note = {"closed_reason": reason} if reason else {}
        return self._set_state(rec, state,
                               realized_margin_cents=realized_margin_cents,
                               closed_at_us=ts, note=note)

    def _set_state(
        self,
        rec: StructureRecord,
        state: StructureState,
        *,
        realized_margin_cents: float | None = None,
        closed_at_us: int | None = None,
        note: Mapping[str, Any] | None = None,
    ) -> StructureRecord:
        rationale = {**rec.rationale, **(dict(note) if note else {})}
        with self.db.tx() as c:
            c.execute(
                """UPDATE structures SET
                     state = ?,
                     realized_margin_cents = COALESCE(?, realized_margin_cents),
                     closed_at_us = COALESCE(?, closed_at_us),
                     rationale_json = ?
                   WHERE structure_id = ?""",
                (
                    StructureState(state).value,
                    realized_margin_cents,
                    closed_at_us,
                    json.dumps(rationale, default=str),
                    rec.structure_id,
                ),
            )
        after = self.get(rec.structure_id)
        assert after is not None
        return after

    # ----------------------------------------------------------------- stats
    def counts_by_state(self, *, sleeve_id: str | None = None) -> dict[str, int]:
        sql = "SELECT state, COUNT(*) AS n FROM structures"
        params: list[Any] = []
        if sleeve_id:
            sql += " WHERE sleeve_id = ?"
            params.append(sleeve_id)
        sql += " GROUP BY state"
        return {r["state"]: r["n"] for r in self.db.conn.execute(sql, params)}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _structure_id_of(quote: DesiredQuote) -> str:
    """`DesiredQuote.structure_id` first, the rationale key second."""
    return str(quote.structure_id or quote.rationale.get("structure_id") or "")


def _basket_size(rec: StructureRecord) -> int:
    """The designed basket count: the smallest leg, since that is what binds."""
    return min((leg.target_size for leg in rec.legs), default=0)


def _infer_kind(rationale: Mapping[str, Any]) -> StructureKind:
    if "link_type" in rationale or "link_id" in rationale:
        return StructureKind.LINKED_RV
    if rationale.get("is_arbitrage"):
        return StructureKind.DUTCH_BOOK
    if "direction" in rationale:
        return StructureKind.SHORT_BASKET
    return StructureKind.HEDGE


def _per_contract_cents(rationale: Mapping[str, Any], cents_key: str,
                        dollars_key: str) -> float:
    """Per-contract cents from whichever unit the sleeve happened to publish.

    S3 publishes `net_cents` / `fee_cents` in CENTS; S2 publishes `margin` /
    `fees` in DOLLARS (`short_basket_margin` works in dollars per contract).
    Reading either as the other is a 100x error in KPI 6's denominator, so the
    unit is decided by the key name rather than by inspection of the value.
    """
    if cents_key in rationale:
        try:
            return float(rationale[cents_key])
        except (TypeError, ValueError):
            return 0.0
    if dollars_key in rationale:
        try:
            return 100.0 * float(rationale[dollars_key])
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _price_slippage_cents(statuses: Sequence[LegStatus]) -> float:
    """Total cents gained (or lost) versus the quoted leg prices.

    YES-signed, so one expression covers both sides: buying YES below the quote
    and selling YES above it both come out positive.
    """
    total = 0.0
    for s in statuses:
        if not s.has_position:
            continue
        total += float(s.signed_size) * (float(s.leg.price_cents) - s.avg_price_cents)
    return total


def _exit_price(book: Market | None, side: Side, *,
                prefer_taker: bool) -> tuple[int | None, bool]:
    """(YES-referenced exit price, is_taker) for flattening a leg on `side`.

    Closing a long YES sells YES: a maker posts at the ask, a taker hits the bid.
    Closing a short YES buys YES: a maker posts at the bid, a taker lifts the ask.
    When the preferred side of the book is empty the other is used and the flag
    reports what it actually is, because a plan that says "maker" while pricing
    off the far touch would book a spread it is not going to get.
    """
    if book is None:
        return None, prefer_taker
    if side is Side.YES:
        maker_px = book.yes_ask if book.has_ask else None
        taker_px = book.yes_bid if book.has_bid else None
    else:
        maker_px = book.yes_bid if book.has_bid else None
        taker_px = book.yes_ask if book.has_ask else None

    first, second = (taker_px, maker_px) if prefer_taker else (maker_px, taker_px)
    if first is not None:
        return int(first), prefer_taker
    if second is not None:
        return int(second), not prefer_taker
    return None, prefer_taker
