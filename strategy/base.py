"""Sleeve protocol.  PLAN.md 4.2.

The contract that makes backtest, shadow and live share ONE code path:

    C4.2a  `desired_state` is PURE -- a deterministic function of its inputs.
           No I/O, no clock reads (time is in the snapshot), no unseeded random.
    C4.2b  Sleeves never call a VenueClient.  Only the executor does.
    C4.2c  Every DesiredQuote carries its rationale.  An order whose reasoning
           cannot be reconstructed later is a bug.

A sleeve declares what it WANTS.  The executor diffs that against reality and
decides what to send -- which is what lets the same sleeve run in shadow mode
against live data without a single branch inside the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from core.models import Event, Market, Series, Side


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Everything a sleeve is allowed to see.  Time is IN here, never read live."""

    now_us: int
    markets: tuple[Market, ...]
    events: dict[str, Event] = field(default_factory=dict)
    series: dict[str, Series] = field(default_factory=dict)
    bankroll_cents: int = 0
    positions: dict[str, int] = field(default_factory=dict)      # ticker -> net contracts
    settled_counts: dict[str, int] = field(default_factory=dict)  # sleeve -> settlements

    def series_for(self, m: Market) -> Series | None:
        return self.series.get(m.series_ticker)

    def event_for(self, m: Market) -> Event | None:
        return self.events.get(m.event_ticker)


@dataclass(frozen=True, slots=True)
class DesiredQuote:
    """One resting order the sleeve wants to exist."""

    ticker: str
    side: Side
    price_cents: int
    size: int
    post_only: bool = True
    # Multi-leg sleeves (S2, S3) need their legs bound together as ONE risk
    # object.  Carrying it in `rationale` and lifting it back out by string key
    # is how a leg silently loses its partner and becomes a naked directional
    # bet wearing an arbitrage's clothes.
    structure_id: str | None = None
    rationale: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str, int]:
        """Identity for diffing: one quote per (ticker, side, price)."""
        return (self.ticker, self.side.value, self.price_cents)


@dataclass(frozen=True, slots=True)
class Decision:
    """A model probability the sleeve emitted -- ACTED ON OR NOT.

    Un-acted decisions are what make calibration measurable without survivorship
    bias (PLAN.md 6.3), so they are recorded too.
    """

    ticker: str
    market_price: float
    p_model: float
    raw_edge: float
    shrunk_edge: float
    acted: bool
    category: str = ""


@dataclass(frozen=True, slots=True)
class DesiredState:
    quotes: tuple[DesiredQuote, ...] = ()
    decisions: tuple[Decision, ...] = ()
    rationale: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Sleeve(Protocol):
    id: str
    gate: int          # executor refuses LIVE orders below 4 (I5)

    def desired_state(self, snapshot: MarketSnapshot) -> DesiredState: ...
