"""The S3 link graph -- which markets are logically bound to which, and how.

PLAN.md 3.3; research/06 sections 3 and 3.2.

Markets that are logically related are priced on SEPARATE BOOKS with no
cross-margining (research/06 section 8: netting exists only inside one market
and inside a MECNET event -- a DIRECNET threshold ladder nets NOTHING), so their
prices routinely violate the constraint that binds them.  Unlike a forecast the
constraint is PROVABLE; unlike an equity pair it is GUARANTEED to converge at
settlement (PLAN.md W4).

FOUR RELATIONS, IN DECREASING ORDER OF CONFIDENCE
-------------------------------------------------
    L1 Identity     P(A) = P(B)                    same event listed twice
    L2 Implication  A subset B  =>  P(A) <= P(B)   "wins by 10+" vs "wins"
    L3 Partition    sum(P(A_i)) = P(B)             monthly buckets vs the quarter
    L4 Bounded      |P(A) - P(B)| <= k             consecutive thresholds

L1 and L2 are hard logical constraints.  L4 needs an ASSUMPTION about `k`, so it
is genuinely statistical rather than arbitrage: a losing L4 costs a full $1 per
contract, not zero.  It is therefore sized at half (PLAN.md 3.3, and see
`strategy.s3_linked_rv.S3Config.l4_size_multiplier`).

THE PRIMARY TARGET IS THE THRESHOLD LADDER
------------------------------------------
`KXFED-27APR` lists 18 CUMULATIVE thresholds with `mutually_exclusive = false`
and `collateral_return_type = DIRECNET`.  Its structure is MONOTONICITY
(`P(>0.00) >= P(>0.25) >= ...`), not sum-to-one.  Monotonicity violation between
adjacent strikes is the cleanest L2 link on the venue and needs no forecasting
whatsoever.  Measured live, the richest hunting ground is the ladder series:
`KXMIDTERMVOTETURN` (502 events), `KXMIDTERMMOV` (475), `KXNCAAF1H` (206),
`KXMLBINNINGWIN` (136), `KXNCAAFTOTAL` (78), `KXNCAAFSPREAD` (78).

WARNING (research/06 section 3): only 7 series MIX both shapes -- `KXPRIMARYMOV`,
`KXPSAVERT`, `KXGOVSENDIFF`, `KXSTARSHIPSPACE`, `KXSCFI`, `KXMLBSS`,
`KXHEISMANSPECIAL` -- so `mutually_exclusive` is read PER EVENT here and is never
cached per series.

DISCOVERY IS SEEDED FROM `GET /milestones`, NOT FROM TITLES
-----------------------------------------------------------
Across 12,000 live events, title matching found ONE real duplicate and NINE false
positives (research/06 section 3.1).  `GET /milestones` publishes
`primary_event_tickers[]` and `related_event_tickers[]`, grouping events ACROSS
series that resolve off one real-world occurrence -- Kalshi's own correlated-event
index, and largely absent from community tooling.  It yields CANDIDATES (pairs
worth a rulebook read), never links: the endpoint says "these are related", not
"P(A) <= P(B)".

NOTHING HERE IS TRADEABLE UNTIL A HUMAN VERIFIES IT
---------------------------------------------------
`equivalence_status` starts at NEEDS_HUMAN and only `LinkRegistry` can promote it,
bound to the exact `rules_hash` of every leg.  Correlation-of-definition risk is
the top loss driver in this family (PLAN.md C4): a link whose rulebooks do not
actually agree is a directional bet wearing a hedge costume.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from core.models import Event, Market
from rulebook.exhaustiveness import Verdict

__all__ = [
    "MIN_NET_CENTS",
    "SAME_UNDERLYING_SERIES",
    "KNOWN_DUPLICATE_EVENTS",
    "Ladder",
    "LadderDirection",
    "LadderScan",
    "Link",
    "LinkCandidate",
    "LinkRegistry",
    "LinkSource",
    "LinkType",
    "Milestone",
    "Strike",
    "StrikeKind",
    "Verdict",
    "Violation",
    "bounded_link",
    "candidates_from_milestones",
    "candidates_from_same_underlying",
    "detect_ladders",
    "identity_link",
    "implication_link",
    "ladder_bounded_links",
    "ladder_implication_links",
    "ladder_links_from_markets",
    "ladder_direction",
    "parse_strike",
    "partition_link",
]


# --------------------------------------------------------------------------- #
# Taxonomy
# --------------------------------------------------------------------------- #
class LinkType(StrEnum):
    IDENTITY = "L1"
    IMPLICATION = "L2"
    PARTITION = "L3"
    BOUNDED = "L4"


class LinkSource(StrEnum):
    """Where the link came from.  Provenance is auditable, never decorative."""

    LADDER = "ladder"                    # strike codes parsed inside ONE event
    MILESTONE = "milestone"              # GET /milestones correlated-event index
    SAME_UNDERLYING = "same_underlying"   # curated cross-series pairs
    MANUAL = "manual"


# PLAN.md 3.3 `MIN_NET[link.type]`, converted from dollars to cents.  The weaker
# the constraint, the more net margin it must clear before it is worth the two
# legs of execution risk.
MIN_NET_CENTS: Final[dict[LinkType, float]] = {
    LinkType.IDENTITY: 1.0,
    LinkType.IMPLICATION: 1.0,
    LinkType.PARTITION: 1.5,
    LinkType.BOUNDED: 2.0,
}


# --------------------------------------------------------------------------- #
# Strike parsing -- the ladder detector's foundation
# --------------------------------------------------------------------------- #
class StrikeKind(StrEnum):
    THRESHOLD = "T"     # cumulative one-sided condition; ladder is MONOTONE
    BUCKET = "B"        # a between-range; the ticker carries the MIDPOINT
    OTHER = "?"         # a code we do not recognise -- never laddered


# Kalshi market tickers are `<event_ticker>-<strike_code>`, e.g.
#   KXNCAAFTOTAL-25AUG23MICHOSU-T45.5   threshold at 45.5
#   KXHIGHNY-26AUG26-B84.5              bucket whose MIDPOINT is 84.5
#   KXFED-27APR-T4.25                   threshold at 4.25
#   KXNCAAFSPREAD-25SEP06ALAWIS-T-3.5   NEGATIVE threshold -- the value carries
#                                       its own '-', which is why this is a
#                                       regex and not `ticker.rsplit("-")`.
# The letter prefix is mandatory, which is what keeps an event suffix like
# `-26AUG26` (leading digit) from being mistaken for a strike.
_STRIKE_RE: Final[re.Pattern[str]] = re.compile(
    r"-(?P<code>[A-Z]{1,2})(?P<value>-?\d+(?:\.\d+)?)$"
)
_STRIKE_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<code>[A-Z]{1,2})(?P<value>-?\d+(?:\.\d+)?)$"
)

_KIND_BY_CODE: Final[dict[str, StrikeKind]] = {
    "T": StrikeKind.THRESHOLD,
    "B": StrikeKind.BUCKET,
}

_NUM = r"-?[\d,]+(?:\.\d+)?"
_TITLE_RANGE_RE: Final[re.Pattern[str]] = re.compile(
    rf"\bbetween\s+\$?({_NUM})\s+and\s+\$?({_NUM})", re.IGNORECASE
)
_TITLE_OR_MORE_RE: Final[re.Pattern[str]] = re.compile(
    rf"\$?({_NUM})\s*(?:%|percent)?\s+or\s+"
    r"(?:more|above|higher|greater|less|below|lower|fewer)\b",
    re.IGNORECASE,
)
_TITLE_COMPARATIVE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:above|over|below|under|more\s+than|less\s+than|greater\s+than|"
    rf"fewer\s+than|at\s+least|at\s+most|exceeds?)\s+\$?({_NUM})",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Strike:
    """One rung of a ladder: the market plus the number that orders it."""

    ticker: str
    kind: StrikeKind
    value: float
    code: str = ""          # raw suffix as it appeared, e.g. "T45.5"
    from_title: bool = False


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def parse_strike(
    ticker: str, *, event_ticker: str = "", title: str = ""
) -> Strike | None:
    """Extract the ordering number from a Kalshi market ticker, or its title.

    The event-ticker prefix is preferred when known, because Kalshi guarantees
    `market_ticker == event_ticker + "-" + strike_code`; the trailing regex is
    the fallback for snapshots that did not carry the event.
    """
    suffix = ""
    if event_ticker and ticker.startswith(event_ticker + "-"):
        suffix = ticker[len(event_ticker) + 1:]
        m = _STRIKE_SUFFIX_RE.match(suffix)
    else:
        m = _STRIKE_RE.search(ticker)
    if m is not None:
        value = _to_float(m.group("value"))
        if value is not None:
            code = m.group("code")
            return Strike(
                ticker=ticker,
                kind=_KIND_BY_CODE.get(code, StrikeKind.OTHER),
                value=value,
                code=code + m.group("value"),
            )
    return _parse_strike_from_title(ticker, title)


def _parse_strike_from_title(ticker: str, title: str) -> Strike | None:
    """Fallback for tickers with no strike code.  Ranges become MIDPOINTS, which
    is the same convention Kalshi's own `B` codes use."""
    if not title:
        return None
    rng = _TITLE_RANGE_RE.search(title)
    if rng is not None:
        lo, hi = _to_float(rng.group(1)), _to_float(rng.group(2))
        if lo is not None and hi is not None:
            return Strike(ticker, StrikeKind.BUCKET, (lo + hi) / 2.0,
                          code="", from_title=True)
    for pattern in (_TITLE_OR_MORE_RE, _TITLE_COMPARATIVE_RE):
        hit = pattern.search(title)
        if hit is not None:
            value = _to_float(hit.group(1))
            if value is not None:
                return Strike(ticker, StrikeKind.THRESHOLD, value,
                              code="", from_title=True)
    return None


# --------------------------------------------------------------------------- #
# Ladder direction
# --------------------------------------------------------------------------- #
class LadderDirection(StrEnum):
    ABOVE = "above"     # P DECREASES with the strike: P(>40) >= P(>45)
    BELOW = "below"     # P INCREASES with the strike: P(<40) <= P(<45)


# Order matters: the negated comparatives must be tested BEFORE the plain ones,
# because "no more than 45" is an UPPER bound and contains the substring
# "more than".  Getting this backwards inverts every L2 link on the ladder and
# turns a hedge into a doubled directional bet.
_DIRECTION_PATTERNS: Final[tuple[tuple[re.Pattern[str], LadderDirection], ...]] = (
    (re.compile(r"\bno[t]?\s+(?:more|higher|greater)\s+than\b", re.I), LadderDirection.BELOW),
    (re.compile(r"\bno[t]?\s+(?:less|fewer|lower)\s+than\b", re.I), LadderDirection.ABOVE),
    (re.compile(r"\b(?:above|over|greater\s+than|more\s+than|at\s+least|"
                r"higher\s+than|exceeds?|exceeding)\b", re.I), LadderDirection.ABOVE),
    (re.compile(r"\bor\s+(?:more|above|higher|greater)\b", re.I), LadderDirection.ABOVE),
    (re.compile(r"\b(?:below|under|less\s+than|fewer\s+than|at\s+most|"
                r"lower\s+than)\b", re.I), LadderDirection.BELOW),
    (re.compile(r"\bor\s+(?:less|below|lower|fewer)\b", re.I), LadderDirection.BELOW),
    (re.compile(r"(?:>=|≥|>)"), LadderDirection.ABOVE),
    (re.compile(r"(?:<=|≤|<)"), LadderDirection.BELOW),
)


def _direction_of_title(title: str) -> LadderDirection | None:
    for pattern, direction in _DIRECTION_PATTERNS:
        if pattern.search(title or ""):
            return direction
    return None


def ladder_direction(titles: Iterable[str]) -> LadderDirection | None:
    """The unanimous direction stated by the titles, or None.

    DELIBERATELY NOT INFERRED FROM PRICES.  If the direction were read off the
    observed ordering, every ladder would be "correctly ordered" by construction
    and the sleeve's entire signal would vanish.  A ladder whose titles do not
    state a direction is simply not laddered here.
    """
    found = {d for d in (_direction_of_title(t) for t in titles) if d is not None}
    if len(found) == 1:
        return found.pop()
    return None


# --------------------------------------------------------------------------- #
# Ladders
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Ladder:
    """A cumulative threshold ladder inside ONE event, strikes ascending."""

    event_ticker: str
    series_ticker: str
    direction: LadderDirection
    strikes: tuple[Strike, ...]

    def implication_pairs(
        self, *, adjacent_only: bool = True
    ) -> tuple[tuple[Strike, Strike], ...]:
        """Ordered (subset, superset) pairs, i.e. pairs where P(a) <= P(b).

        For an ABOVE ladder the HIGHER strike is the tighter condition, so it is
        the subset.  For a BELOW ladder it is the lower strike.  Adjacent pairs
        are the default because they are the cleanest and the most likely to be
        mispriced against each other (PLAN.md 3.3).
        """
        out: list[tuple[Strike, Strike]] = []
        n = len(self.strikes)
        for i in range(n):
            hi_range = range(i + 1, i + 2) if adjacent_only else range(i + 1, n)
            for j in hi_range:
                if j >= n:
                    break
                lo, hi = self.strikes[i], self.strikes[j]
                if self.direction is LadderDirection.ABOVE:
                    out.append((hi, lo))       # P(>hi) <= P(>lo)
                else:
                    out.append((lo, hi))       # P(<lo) <= P(<hi)
        return tuple(out)


@dataclass(frozen=True, slots=True)
class LadderScan:
    """What `detect_ladders` found, and why it refused the rest.

    The rejections are as operationally important as the acceptances: a ladder
    series that silently stops being detected looks exactly like a quiet market.
    """

    ladders: tuple[Ladder, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()      # (event_ticker, reason)

    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, reason in self.skipped:
            counts[reason] = counts.get(reason, 0) + 1
        return counts


def detect_ladders(
    markets: Sequence[Market],
    events: Mapping[str, Event],
    *,
    min_strikes: int = 2,
    require_direcnet: bool = True,
) -> LadderScan:
    """Find cumulative threshold ladders in a market snapshot.

    The `mutually_exclusive` flag is read PER EVENT and the event must be
    present: 7 series mix both shapes, so a per-series cache is wrong
    (research/06 section 3).  A MECE bracket set is NOT a ladder -- its legs are
    disjoint ranges that sum to 1, and P is not monotone in a bucket midpoint.
    """
    by_event: dict[str, list[Market]] = {}
    for m in markets:
        if m.event_ticker:
            by_event.setdefault(m.event_ticker, []).append(m)

    ladders: list[Ladder] = []
    skipped: list[tuple[str, str]] = []

    for event_ticker, legs in sorted(by_event.items()):
        ev = events.get(event_ticker)
        if ev is None:
            skipped.append((event_ticker, "event not in snapshot (flag unreadable)"))
            continue
        if ev.mutually_exclusive:
            skipped.append((event_ticker, "mutually_exclusive: bracket set, not a ladder"))
            continue
        # DIRECNET is what says "these legs do not net against each other", which
        # is precisely the no-cross-margining premise of the sleeve.  An empty
        # field is tolerated (some snapshots do not carry it); a DIFFERENT value
        # is not.
        crt = ev.collateral_return_type
        if require_direcnet and crt and crt != "DIRECNET":
            skipped.append((event_ticker, f"collateral_return_type {crt!r}, expected DIRECNET"))
            continue

        parsed = [
            parse_strike(m.ticker, event_ticker=event_ticker, title=m.title)
            for m in legs
        ]
        if any(s is None for s in parsed):
            skipped.append((event_ticker, "not every leg carries a parseable strike"))
            continue
        strikes = [s for s in parsed if s is not None]

        kinds = {s.kind for s in strikes}
        if kinds != {StrikeKind.THRESHOLD}:
            found = sorted(k.value for k in kinds)
            skipped.append((event_ticker, f"strike kinds {found} are not all thresholds"))
            continue
        if len(strikes) < min_strikes:
            skipped.append((event_ticker, f"fewer than {min_strikes} strikes"))
            continue
        if len({s.value for s in strikes}) != len(strikes):
            skipped.append((event_ticker, "duplicate strike values: the ladder cannot be ordered"))
            continue

        direction = ladder_direction(m.title for m in legs)
        if direction is None:
            skipped.append((event_ticker, "no title states an unambiguous ladder direction"))
            continue

        ladders.append(Ladder(
            event_ticker=event_ticker,
            series_ticker=legs[0].series_ticker,
            direction=direction,
            strikes=tuple(sorted(strikes, key=lambda s: s.value)),
        ))

    return LadderScan(tuple(ladders), tuple(skipped))


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Violation:
    """A constraint breach expressed as the two-leg trade that captures it."""

    link_id: str
    link_type: LinkType
    size: float          # dollars per contract BEYOND the constraint's bound
    sell_ticker: str     # the rich leg -- sell YES here
    buy_ticker: str      # the cheap leg -- buy YES here


@dataclass(frozen=True, slots=True)
class Link:
    """One logical constraint between markets.

    `tickers` is in a canonical order that depends on the type:

        L1  (a, b)                       interchangeable
        L2  (subset, superset)           P(subset) <= P(superset)
        L3  (whole, part_0, ..., part_n) sum(parts) = whole
        L4  (a, b)                       |P(a) - P(b)| <= k_bound
    """

    link_id: str
    link_type: LinkType
    tickers: tuple[str, ...]
    source: LinkSource = LinkSource.MANUAL
    equivalence_status: Verdict = Verdict.NEEDS_HUMAN
    rules_hashes: tuple[str, ...] = ()
    k_bound: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        n = len(self.tickers)
        if self.link_type is LinkType.PARTITION:
            if n < 3:
                raise ValueError("an L3 partition needs a whole and >= 2 parts")
        elif n != 2:
            raise ValueError(f"{self.link_type} is a two-leg relation, got {n} legs")
        if len(set(self.tickers)) != n:
            raise ValueError("a link may not repeat a ticker")
        if self.link_type is LinkType.BOUNDED:
            if self.k_bound is None or not 0.0 <= self.k_bound < 1.0:
                raise ValueError("an L4 link needs k_bound in [0,1)")
        elif self.k_bound is not None:
            raise ValueError("k_bound is meaningful only for L4")

    # ------------------------------------------------------------------ shape
    @property
    def is_two_leg(self) -> bool:
        return len(self.tickers) == 2

    @property
    def subset(self) -> str:
        if self.link_type is not LinkType.IMPLICATION:
            raise ValueError("subset/superset are defined only for L2")
        return self.tickers[0]

    @property
    def superset(self) -> str:
        if self.link_type is not LinkType.IMPLICATION:
            raise ValueError("subset/superset are defined only for L2")
        return self.tickers[1]

    @property
    def bound(self) -> float:
        """Slack the constraint permits, in dollars.  Only L4 has any."""
        return self.k_bound if self.link_type is LinkType.BOUNDED and self.k_bound else 0.0

    @property
    def min_net_cents(self) -> float:
        return MIN_NET_CENTS[self.link_type]

    def with_status(self, status: Verdict) -> "Link":
        return Link(
            link_id=self.link_id, link_type=self.link_type, tickers=self.tickers,
            source=self.source, equivalence_status=status,
            rules_hashes=self.rules_hashes, k_bound=self.k_bound,
            detail=dict(self.detail),
        )

    # -------------------------------------------------------------- violation
    def violation(self, prices: Mapping[str, float]) -> Violation | None:
        """The two-leg trade implied by a breach, or None if the prices are legal.

        `prices` are YES probabilities in dollars.  L3 has a well-defined error
        but no TWO-leg trade, so it returns None here -- use `partition_error`.
        """
        if not self.is_two_leg:
            return None
        a, b = self.tickers
        if a not in prices or b not in prices:
            return None
        pa, pb = prices[a], prices[b]

        if self.link_type is LinkType.IMPLICATION:
            # subset priced above superset: sell the subset, buy the superset.
            # Settlement pays -1{subset} + 1{superset} >= 0, always.
            excess = pa - pb
            if excess <= 0.0:
                return None
            return Violation(self.link_id, self.link_type, excess, a, b)

        delta = pa - pb
        excess = abs(delta) - self.bound
        if excess <= 0.0:
            return None
        sell, buy = (a, b) if delta > 0 else (b, a)
        return Violation(self.link_id, self.link_type, excess, sell, buy)

    def partition_error(self, prices: Mapping[str, float]) -> float | None:
        """sum(parts) - whole, in dollars.  Signed.  L3 only."""
        if self.link_type is not LinkType.PARTITION:
            return None
        if any(t not in prices for t in self.tickers):
            return None
        whole, *parts = self.tickers
        return sum(prices[p] for p in parts) - prices[whole]


def _rules_hashes(
    tickers: Sequence[str], markets_by_ticker: Mapping[str, Market]
) -> tuple[str, ...]:
    return tuple(
        (markets_by_ticker[t].rules_hash if t in markets_by_ticker else "")
        for t in tickers
    )


def identity_link(
    a: str, b: str, *, markets_by_ticker: Mapping[str, Market] | None = None,
    source: LinkSource = LinkSource.MANUAL, detail: dict[str, Any] | None = None,
) -> Link:
    return Link(
        link_id=f"L1|{a}|{b}", link_type=LinkType.IDENTITY, tickers=(a, b),
        source=source, rules_hashes=_rules_hashes((a, b), markets_by_ticker or {}),
        detail=detail or {},
    )


def implication_link(
    subset: str, superset: str, *,
    markets_by_ticker: Mapping[str, Market] | None = None,
    source: LinkSource = LinkSource.MANUAL, detail: dict[str, Any] | None = None,
) -> Link:
    return Link(
        link_id=f"L2|{subset}|{superset}", link_type=LinkType.IMPLICATION,
        tickers=(subset, superset), source=source,
        rules_hashes=_rules_hashes((subset, superset), markets_by_ticker or {}),
        detail=detail or {},
    )


def bounded_link(
    a: str, b: str, k: float, *,
    markets_by_ticker: Mapping[str, Market] | None = None,
    source: LinkSource = LinkSource.MANUAL, detail: dict[str, Any] | None = None,
) -> Link:
    return Link(
        link_id=f"L4|{a}|{b}", link_type=LinkType.BOUNDED, tickers=(a, b),
        source=source, k_bound=k,
        rules_hashes=_rules_hashes((a, b), markets_by_ticker or {}),
        detail=detail or {},
    )


def partition_link(
    whole: str, parts: Sequence[str], *,
    markets_by_ticker: Mapping[str, Market] | None = None,
    source: LinkSource = LinkSource.MANUAL, detail: dict[str, Any] | None = None,
) -> Link:
    """sum(parts) = whole.

    The canonical instance is `KXFEDDECISION` (5 exclusive MECNET brackets)
    against `KXFED` (18 cumulative DIRECNET thresholds): a bracket is exactly the
    difference of two adjacent thresholds (research/06 section 3.1).
    """
    tickers = (whole, *parts)
    return Link(
        link_id="L3|" + "|".join(tickers), link_type=LinkType.PARTITION,
        tickers=tickers, source=source,
        rules_hashes=_rules_hashes(tickers, markets_by_ticker or {}),
        detail=detail or {},
    )


def ladder_implication_links(
    ladder: Ladder,
    markets_by_ticker: Mapping[str, Market] | None = None,
    *,
    adjacent_only: bool = True,
) -> tuple[Link, ...]:
    """The monotonicity constraints of one ladder, as L2 links."""
    return tuple(
        implication_link(
            sub.ticker, sup.ticker,
            markets_by_ticker=markets_by_ticker,
            source=LinkSource.LADDER,
            detail={
                "event_ticker": ladder.event_ticker,
                "series_ticker": ladder.series_ticker,
                "direction": ladder.direction.value,
                "subset_strike": sub.value,
                "superset_strike": sup.value,
            },
        )
        for sub, sup in ladder.implication_pairs(adjacent_only=adjacent_only)
    )


def ladder_bounded_links(
    ladder: Ladder,
    k: float,
    markets_by_ticker: Mapping[str, Market] | None = None,
) -> tuple[Link, ...]:
    """Adjacent-strike L4 bounds: the gap between consecutive rungs is at most k.

    This is the ASSUMPTION half of the taxonomy.  The pair already carries a hard
    L2 (monotonicity); L4 adds "and the two are close", which is a forecast about
    how much probability mass sits between the strikes.  A wrong L4 costs the
    full $1 per contract, which is why it trades at half size.
    """
    out: list[Link] = []
    for lo, hi in zip(ladder.strikes, ladder.strikes[1:]):
        out.append(bounded_link(
            lo.ticker, hi.ticker, k,
            markets_by_ticker=markets_by_ticker,
            source=LinkSource.LADDER,
            detail={
                "event_ticker": ladder.event_ticker,
                "series_ticker": ladder.series_ticker,
                "direction": ladder.direction.value,
                "strikes": (lo.value, hi.value),
            },
        ))
    return tuple(out)


def ladder_links_from_markets(
    markets: Sequence[Market],
    events: Mapping[str, Event],
    *,
    adjacent_only: bool = True,
    k_bound: float | None = None,
    require_direcnet: bool = True,
) -> tuple[tuple[Link, ...], LadderScan]:
    """Snapshot -> ladder L2 links (plus optional L4s).  Pure; no I/O."""
    scan = detect_ladders(markets, events, require_direcnet=require_direcnet)
    by_ticker = {m.ticker: m for m in markets}
    links: list[Link] = []
    for ladder in scan.ladders:
        links.extend(ladder_implication_links(
            ladder, by_ticker, adjacent_only=adjacent_only))
        if k_bound is not None:
            links.extend(ladder_bounded_links(ladder, k_bound, by_ticker))
    return tuple(links), scan


# --------------------------------------------------------------------------- #
# Human sign-off.  PLAN.md 3.3 / C4 -- the moat, and a HARD gate.
# --------------------------------------------------------------------------- #
@dataclass
class LinkRegistry:
    """Which links a human has actually read the rulebooks for.

    An approval is bound to the exact `rules_hash` of every leg.  Any change in
    either rulebook invalidates it and forces re-review, because the failure mode
    this guards against is not "the price moved" but "the settlement source
    changed and my two legs no longer describe the same world".
    """

    approvals: dict[str, tuple[str, ...]] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    def approve(self, link_id: str, rules_hashes: Sequence[str],
                *, note: str = "") -> None:
        self.approvals[link_id] = tuple(rules_hashes)
        if note:
            self.notes[link_id] = note

    def approve_link(self, link: Link, *, note: str = "") -> None:
        self.approve(link.link_id, link.rules_hashes, note=note)

    def revoke(self, link_id: str) -> None:
        self.approvals.pop(link_id, None)
        self.notes.pop(link_id, None)

    def is_stale(self, link: Link) -> bool:
        """Approved once, but at least one rulebook has changed since."""
        known = self.approvals.get(link.link_id)
        return known is not None and known != link.rules_hashes

    def status_for(self, link: Link) -> Verdict:
        known = self.approvals.get(link.link_id)
        if known is None or known != link.rules_hashes:
            return Verdict.NEEDS_HUMAN
        return Verdict.VERIFIED

    def apply(self, links: Iterable[Link]) -> tuple[Link, ...]:
        """Stamp every link with its current verdict.  Never promotes blindly."""
        return tuple(link.with_status(self.status_for(link)) for link in links)


# --------------------------------------------------------------------------- #
# Discovery.  GET /milestones, and the curated same-underlying pairs.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LinkCandidate:
    """Two events worth a rulebook read.  NOT a link and NOT tradeable.

    The milestone index says "these resolve off one real-world occurrence".  It
    does not say which of L1/L2/L3/L4 binds them, and it certainly does not say
    the rulebooks agree.  Turning a candidate into a Link is the human step.
    """

    event_a: str
    event_b: str
    source: LinkSource = LinkSource.MILESTONE
    reason: str = ""
    category: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.event_a, self.event_b)


@dataclass(frozen=True, slots=True)
class Milestone:
    """One `GET /milestones` row.  research/06 section 3.2."""

    milestone_id: str = ""
    title: str = ""
    category: str = ""
    primary_event_tickers: tuple[str, ...] = ()
    related_event_tickers: tuple[str, ...] = ()

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Milestone":
        return cls(
            milestone_id=str(raw.get("id") or raw.get("milestone_id") or ""),
            title=str(raw.get("title") or ""),
            category=str(raw.get("category") or ""),
            primary_event_tickers=tuple(raw.get("primary_event_tickers") or ()),
            related_event_tickers=tuple(raw.get("related_event_tickers") or ()),
        )

    @property
    def event_tickers(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for t in (*self.primary_event_tickers, *self.related_event_tickers):
            if t:
                seen.setdefault(t, None)
        return tuple(seen)


def candidates_from_milestones(
    milestones: Iterable[Milestone], *, max_events_per_milestone: int = 12
) -> tuple[LinkCandidate, ...]:
    """Seed the graph from Kalshi's own correlated-event index.

    NOT from title similarity: across 12,000 live events that found ONE real
    duplicate and NINE false positives (research/06 section 3.1).

    A milestone with many events would emit O(n^2) candidates for a human to
    read; past the cap it is recorded as a single wide-group candidate-free skip
    rather than flooding the review queue.
    """
    out: dict[tuple[str, str], LinkCandidate] = {}
    for ms in milestones:
        tickers = ms.event_tickers
        if len(tickers) < 2 or len(tickers) > max_events_per_milestone:
            continue
        ordered = sorted(tickers)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                out.setdefault((a, b), LinkCandidate(
                    event_a=a, event_b=b, source=LinkSource.MILESTONE,
                    reason=f"milestone {ms.title!r}" if ms.title else "milestone",
                    category=ms.category,
                ))
    return tuple(out[k] for k in sorted(out))


# The premier same-underlying pairs: one event, two shapes, two independent
# books (research/06 section 3.1).  Keyed by series; the event suffix must match.
SAME_UNDERLYING_SERIES: Final[tuple[tuple[str, str, str], ...]] = (
    ("KXFED", "KXFEDDECISION",
     "18 cumulative DIRECNET thresholds vs 5 exclusive MECNET brackets; "
     "a bracket is the difference of two adjacent thresholds"),
    ("KXINXDIRY", "KXINXY",
     "same index, same timestamp: DIRECNET thresholds vs MECNET ranges"),
)

# The single genuine intra-Kalshi duplicate found across all 12,000 open events:
# the same Oscar category listed twice on two independent books.
KNOWN_DUPLICATE_EVENTS: Final[tuple[tuple[str, str], ...]] = (
    ("KXOSCARMAH-27", "KXOSCARVIS-27"),
)

# Families that overlap without being identical -- worth a read, never assumed.
OVERLAPPING_FAMILIES: Final[dict[str, tuple[str, ...]]] = {
    "cpi": ("KXUSCPIYEAR", "KXCPI", "KXCPIYOY", "KXLCPIMAXYOY", "KXHIGHINFLATION"),
    "rate_path": ("KXFEDCHGCOUNT", "KXRATECUTCOUNT", "KXRATECUT",
                  "KXEMERCUTS", "KXLARGECUT", "KXZERORATE"),
}


def _split_event(event_ticker: str) -> tuple[str, str]:
    series, _, suffix = event_ticker.partition("-")
    return series, suffix


def candidates_from_same_underlying(
    event_tickers: Iterable[str],
) -> tuple[LinkCandidate, ...]:
    """Pair up the curated two-shape series wherever both list the same suffix."""
    by_series: dict[str, dict[str, str]] = {}
    for t in event_tickers:
        series, suffix = _split_event(t)
        if suffix:
            by_series.setdefault(series, {})[suffix] = t

    out: dict[tuple[str, str], LinkCandidate] = {}
    for series_a, series_b, why in SAME_UNDERLYING_SERIES:
        a_map, b_map = by_series.get(series_a, {}), by_series.get(series_b, {})
        for suffix in sorted(set(a_map) & set(b_map)):
            first, second = sorted((a_map[suffix], b_map[suffix]))
            out.setdefault((first, second), LinkCandidate(
                event_a=first, event_b=second,
                source=LinkSource.SAME_UNDERLYING, reason=why,
            ))

    known = {t for t in event_tickers}
    for a, b in KNOWN_DUPLICATE_EVENTS:
        if a in known and b in known:
            out.setdefault((a, b), LinkCandidate(
                event_a=a, event_b=b, source=LinkSource.SAME_UNDERLYING,
                reason="same Oscar category listed twice on independent books",
            ))
    return tuple(out[k] for k in sorted(out))
