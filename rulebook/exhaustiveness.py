"""The MECE gate.  T-050b.  The single most important guard in the RV sleeves.

Kalshi's `mutually_exclusive` flag guarantees **at most one** leg resolves YES.
It says NOTHING about whether **at least one** does, and there is no
exhaustiveness field anywhere in the API.

That gap is not academic.  33 live events flagged `mutually_exclusive` price
below `sum(YES ask) = 0.90`, showing apparent margins up to **+87c**:

    LA-01 Republican nominee?     2 candidates listed, sum(ask) = 0.125
    Who will the next Pope be?    7 candidates listed, sum(ask) = 0.282
    Brendan Sorsby's Next Team   32 teams listed,     sum(ask) = 0.320

None are arbitrage.  They are races whose listed outcomes do not cover the
outcome space -- no "Other"/"None" leg.  Buy every leg and you collect $0
whenever the winner is unlisted.  A naive scanner ranks these as its BEST
opportunities: of 47 fee-profitable taker structures found live, 33 were this
trap (research/05 F1).

The asymmetry that follows is the whole design of sleeve S2:

    BUY  the basket  -> UNSAFE, requires verified exhaustiveness
    SELL the basket  -> SAFE, liability capped at $1 regardless
                        (and non-exhaustiveness makes it BETTER)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from core.models import Event, Market

# A leg whose subtitle matches these is an explicit catch-all, which is what
# makes a candidate list exhaustive.  Rare and inconsistent on Kalshi -- detect,
# never assume (research/06 section 2.1).
CATCH_ALL_PATTERNS = (
    r"\bother\b",
    r"\bnone\b",
    r"\bany other\b",
    r"\bneither\b",
    r"\bno one\b",
    r"\bnobody\b",
    r"\ball others\b",
    r"\bsomeone else\b",
    r"\bfield\b",
)
_CATCH_ALL = re.compile("|".join(CATCH_ALL_PATTERNS), re.IGNORECASE)

# Below this, a flagged-MECE book is almost certainly not exhaustive.
# Chosen from the live distribution: genuinely exhaustive books cluster at
# sum(bid) just under 1.0, while the trap cases sit far below.
MIN_SUM_BID_FOR_EXHAUSTIVE = 0.80


class Verdict(StrEnum):
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


@dataclass(frozen=True, slots=True)
class MeceCheck:
    """Result of the five-condition MECE test (PLAN.md 3.2)."""

    verdict: Verdict
    reasons: tuple[str, ...]
    sum_bid: float
    sum_ask: float
    n_legs: int
    has_catch_all: bool
    all_legs_restable: bool
    # Read from the exchange flag, never inferred.  Carried here because
    # `safe_to_sell` depends on it and previously could not see it.
    mutually_exclusive: bool = False

    @property
    def safe_to_buy(self) -> bool:
        """Buying the basket requires VERIFIED exhaustiveness."""
        return self.verdict is Verdict.VERIFIED

    @property
    def safe_to_sell(self) -> bool:
        """Selling is capped at $1 liability -- ONLY under mutual exclusivity.

        The docstring here already said this rule; the code did not implement it,
        and that gap put real money at risk.  Without mutual exclusivity, an
        n-leg short basket is not capped at $1 -- it is capped at $n, because
        nothing stops every leg resolving YES at once.

        The case that caught it, live: `KXBTCD-26AUG2817` ("BTC price on Aug 28")
        is flagged `mutually_exclusive = 0` and lists 50 NESTED THRESHOLD
        markets -- "BTC above $65,999.99", "above $66,499.99", and so on.  They
        are not alternatives, they are a ladder, and `sum(YES bid) = $25.64` is
        perfectly consistent rather than an overround.  S2 sized a 21-leg short
        against it, collecting $11.06 for up to $21 of liability, and reported a
        `margin` of $10.01 per contract on an instrument that pays at most $1.
        An arbitrage that claims ten times the maximum payout is not an
        arbitrage; the constraint that made it look like one was never checked.

        Exhaustiveness genuinely does not matter for the short side -- an
        unlisted winner means nobody collects, which is BETTER for the seller.
        Mutual exclusivity is a different property, and it is load-bearing.
        """
        return (self.mutually_exclusive
                and self.n_legs >= 2
                and self.all_legs_restable)


def has_catch_all_leg(markets: list[Market]) -> bool:
    """Detect an explicit Other/None outcome by regex on the market title."""
    return any(_CATCH_ALL.search(m.title or "") for m in markets)


def check_mece(
    event: Event,
    markets: list[Market],
    *,
    min_sum_bid: float = MIN_SUM_BID_FOR_EXHAUSTIVE,
) -> MeceCheck:
    """The five-condition gate.  Returns a verdict, never a bare bool.

    1. Mutual exclusivity  -- read the exchange flag, do not infer it.
    2. Exhaustiveness      -- NOT promised by the flag; inferred here and
                              escalated to a human when uncertain.
    3. Same settlement source and deadline across legs.
    4. Identical void clauses  -> NEEDS_HUMAN (requires reading the rules text).
    5. Every leg has a real bid -- a leg nobody bids cannot be rested into.
    """
    reasons: list[str] = []
    quoted = [m for m in markets if m.has_ask]
    n = len(quoted)

    sum_ask = sum((m.yes_ask or 0) / 100.0 for m in quoted)
    sum_bid = sum((m.yes_bid or 0) / 100.0 for m in quoted)
    catch_all = has_catch_all_leg(quoted)
    restable = n >= 2 and all(m.has_bid for m in quoted)

    if n < 2:
        return MeceCheck(Verdict.REJECTED, ("fewer than 2 quoted legs",),
                         sum_bid, sum_ask, n, catch_all, restable,
                         bool(event.mutually_exclusive))

    # 1 -- mutual exclusivity
    if not event.mutually_exclusive:
        reasons.append("event is not flagged mutually_exclusive")
    if event.collateral_return_type and event.collateral_return_type != "MECNET":
        reasons.append(
            f"collateral_return_type is {event.collateral_return_type!r}, expected MECNET"
        )

    # 2 -- exhaustiveness.  THE trap.
    if sum_bid < min_sum_bid and not catch_all:
        reasons.append(
            f"sum(YES bid) = {sum_bid:.3f} < {min_sum_bid:.2f} with no catch-all leg: "
            "the listed outcomes almost certainly do not cover the outcome space"
        )

    # 3 -- settlement sources.  NOT a rejection on Kalshi, and this took a live
    # screen to establish: `settlement_sources` is an EVENT-level list of
    # ACCEPTABLE/FALLBACK sources ("we may use any of these"), not a per-leg
    # assignment.  All legs of an event necessarily share it, so per-leg source
    # divergence is not detectable here at all.  "Who will the next Pope be?"
    # lists 14 news outlets; that is one settlement rule, not 14.
    #
    # A long fallback list IS an ambiguity worth a human reading the rules text,
    # so it is recorded as a note rather than a veto.
    notes: list[str] = []
    if len({s.name for s in event.settlement_sources if s.name}) > 3:
        notes.append(
            f"{len(event.settlement_sources)} fallback settlement sources listed; "
            "confirm which one governs before BUYING this structure"
        )

    # 5 -- restability
    if not restable:
        n_bidless = sum(1 for m in quoted if not m.has_bid)
        reasons.append(f"{n_bidless} of {n} legs have no bid at all")

    if reasons:
        return MeceCheck(Verdict.REJECTED, tuple(reasons),
                         sum_bid, sum_ask, n, catch_all, restable,
                         bool(event.mutually_exclusive))

    # 4 -- void clauses need the rules text and a HUMAN.  Never auto-VERIFY.
    #
    # `Event.exhaustive_verified` is that human verdict, recorded in
    # event_snapshots.  Without reading it here there was NO code path by which
    # a review could ever reach a sleeve, so `safe_to_buy` was permanently False
    # and the long direction was dead code.  Mechanical checks gate WHAT may be
    # reviewed; only the recorded human decision promotes to VERIFIED.
    if event.exhaustive_verified:
        return MeceCheck(
            Verdict.VERIFIED,
            tuple(notes + ["exhaustiveness verified by human review"]),
            sum_bid, sum_ask, n, catch_all, restable,
            bool(event.mutually_exclusive),
        )

    return MeceCheck(
        Verdict.NEEDS_HUMAN,
        tuple(
            notes
            + ["mechanical checks pass; void/cancellation clauses require human review "
               "before this event may be BOUGHT (selling is already safe)"]
        ),
        sum_bid, sum_ask, n, catch_all, restable,
        bool(event.mutually_exclusive),
    )


def screen_events(
    events: list[Event], markets_by_event: dict[str, list[Market]]
) -> dict[str, MeceCheck]:
    """Run the gate across a universe.  Returns one verdict per event ticker."""
    return {
        e.event_ticker: check_mece(e, markets_by_event.get(e.event_ticker, []))
        for e in events
    }
