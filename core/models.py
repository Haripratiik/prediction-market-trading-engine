"""Domain models.  PLAN.md section 0.3 conventions + section 5 data model.

Conventions enforced here:
  * prices are integer CENTS (1..99) in storage, floats only at the math boundary
  * money is integer cents
  * time is UTC epoch-MICROSECONDS as int
  * side is YES-referenced internally
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.math.contracts import FeeSpec, KalshiFeeType


# --------------------------------------------------------------------------- #
# Time.  PLAN.md 0.3: UTC, microsecond precision, stored as int.
# --------------------------------------------------------------------------- #
def now_us() -> int:
    """Current UTC time in epoch microseconds."""
    return time.time_ns() // 1_000


def to_us(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetimes are forbidden (PLAN.md 0.3)")
    return int(dt.timestamp() * 1_000_000)


def from_us(us: int) -> datetime:
    return datetime.fromtimestamp(us / 1_000_000, tz=UTC)


def parse_iso(ts: str | None) -> int | None:
    """Parse an ISO-8601 timestamp (Kalshi uses trailing Z) to epoch micros."""
    if not ts:
        return None
    try:
        return to_us(datetime.fromisoformat(ts.replace("Z", "+00:00")))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Venue(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET_US = "polymarket_us"
    FORECASTEX = "forecastex"
    MANIFOLD = "manifold"


class Side(StrEnum):
    """YES-referenced internally (PLAN.md 0.3)."""

    YES = "yes"
    NO = "no"


class OrderState(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class RunMode(StrEnum):
    """I5: only LIVE sends orders, and only from a sleeve at Gate >= 4."""

    BACKTEST = "backtest"
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE = "live"


Cents = Annotated[int, Field(ge=1, le=99)]


# --------------------------------------------------------------------------- #
# Series -- the fee / prohibition / settlement-source map.
# --------------------------------------------------------------------------- #
class SettlementSource(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str | None = None
    url: str | None = None


class Series(BaseModel):
    """One Kalshi series.  Carries everything needed to price and vet its markets.

    `GET /series` returns all ~13.5k in a single response and ignores `limit`,
    so this is cached once per session (PLAN.md 6.1).
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    title: str = ""
    category: str = ""
    fee_type: KalshiFeeType = "quadratic"
    fee_multiplier: float = 1.0
    contract_terms_url: str | None = None
    settlement_sources: tuple[SettlementSource, ...] = ()
    additional_prohibitions: tuple[str, ...] = ()

    @field_validator("fee_type", mode="before")
    @classmethod
    def _default_fee_type(cls, v: Any) -> Any:
        return v or "quadratic"

    @property
    def fee_spec(self) -> FeeSpec:
        """The FeeSpec every pricing call needs.  research/06 K2."""
        return FeeSpec.kalshi(self.fee_type, self.fee_multiplier)

    @property
    def is_fee_free(self) -> bool:
        """True when this series carries a zero fee multiplier.

        research/06 K3 reported 14 such series. Re-checked live 2026-08-26:
        there are currently NONE. The mechanic is real; the cohort is not.
        """
        return self.fee_multiplier == 0.0

    @property
    def charges_maker_fees(self) -> bool:
        """Only ~130 of 13.5k series do."""
        return self.fee_type != "quadratic" and self.fee_multiplier > 0.0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Series":
        return cls(
            ticker=raw.get("ticker", ""),
            title=raw.get("title") or "",
            category=raw.get("category") or "",
            fee_type=raw.get("fee_type") or "quadratic",
            fee_multiplier=float(raw.get("fee_multiplier") or 1.0),
            contract_terms_url=raw.get("contract_terms_url"),
            settlement_sources=tuple(
                SettlementSource(**s) if isinstance(s, dict) else SettlementSource(name=str(s))
                for s in (raw.get("settlement_sources") or [])
            ),
            additional_prohibitions=tuple(raw.get("additional_prohibitions") or []),
        )


# --------------------------------------------------------------------------- #
# Event / Market
# --------------------------------------------------------------------------- #
class Event(BaseModel):
    """A Kalshi event grouping one or more markets.

    `mutually_exclusive` is the exchange's own flag and means AT MOST ONE leg
    resolves YES.  It does NOT promise at least one does -- see
    `exhaustive_verified`, which is OUR separate verdict (PLAN.md 3.2 / F1).
    """

    model_config = ConfigDict(frozen=True)

    venue: Venue = Venue.KALSHI
    event_ticker: str
    series_ticker: str = ""
    category: str = ""
    title: str = ""
    mutually_exclusive: bool = False
    collateral_return_type: str = ""
    settlement_sources: tuple[SettlementSource, ...] = ()
    exhaustive_verified: bool = False

    @property
    def is_mec_netted(self) -> bool:
        """MECNET implies collateral is assessed on worst-case event outcome."""
        return self.collateral_return_type == "MECNET"


class Market(BaseModel):
    """A single binary contract with its top-of-book quote."""

    model_config = ConfigDict(frozen=True)

    venue: Venue = Venue.KALSHI
    ticker: str
    event_ticker: str = ""
    series_ticker: str = ""
    title: str = ""
    status: str = ""
    yes_bid: int | None = None          # cents
    yes_ask: int | None = None          # cents
    yes_bid_size: float = 0.0           # contracts
    yes_ask_size: float = 0.0
    volume: float = 0.0
    volume_24h: float = 0.0
    open_interest: float = 0.0
    close_at_us: int | None = None
    rules_primary: str = ""
    rules_hash: str = ""

    @property
    def has_bid(self) -> bool:
        """A yes_bid of 0 means NOBODY IS BIDDING, not "bidding zero".

        This distinction is load-bearing for the RV sleeves: a leg with no bid
        cannot be rested into, and treating it as restable at 1c is exactly the
        liquidity fantasy that made a naive scan report 78% of MECE events as
        profitable (research/05 section 4.3).
        """
        return self.yes_bid is not None and self.yes_bid >= 1

    @property
    def has_ask(self) -> bool:
        """A yes_ask of 100 means NOBODY IS OFFERING -- the mirror of has_bid.

        The old bound admitted 100, which is outside the 1..99 tick grid and
        outside `core.math.contracts.fee`'s 0 < p < 1 domain, so any sleeve that
        priced directly off `yes_ask` raised ValueError on such a market.  The
        RV sleeves read this on every leg.
        """
        return self.yes_ask is not None and 1 <= self.yes_ask <= 99

    @property
    def has_two_sided_quote(self) -> bool:
        if not (self.has_bid and self.has_ask):
            return False
        assert self.yes_bid is not None and self.yes_ask is not None
        return self.yes_ask > self.yes_bid

    @property
    def mid(self) -> float | None:
        if not self.has_two_sided_quote:
            return None
        assert self.yes_bid is not None and self.yes_ask is not None
        return (self.yes_bid + self.yes_ask) / 200.0

    @property
    def spread_cents(self) -> int | None:
        if not self.has_two_sided_quote:
            return None
        assert self.yes_bid is not None and self.yes_ask is not None
        return self.yes_ask - self.yes_bid

    def hours_to_close(self, *, now: int | None = None) -> float | None:
        if self.close_at_us is None:
            return None
        ref = now if now is not None else now_us()
        return (self.close_at_us - ref) / 3_600_000_000.0

    @staticmethod
    def _cents(raw: Any) -> int | None:
        """Kalshi returns fixed-point dollar STRINGS ('0.6720').  Never float-parse
        into a price without rounding to the cent grid (research/06 section 13)."""
        if raw is None:
            return None
        try:
            dollars = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return int(round(dollars * 100))

    @staticmethod
    def _num(raw: Any) -> float:
        if raw is None:
            return 0.0
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def from_api(cls, raw: dict[str, Any], *, series_ticker: str = "") -> "Market":
        import hashlib

        rules = str(raw.get("rules_primary") or "")
        return cls(
            ticker=raw.get("ticker", ""),
            event_ticker=raw.get("event_ticker") or "",
            series_ticker=series_ticker,
            title=raw.get("title") or "",
            status=raw.get("status") or "",
            yes_bid=cls._cents(raw.get("yes_bid_dollars")),
            yes_ask=cls._cents(raw.get("yes_ask_dollars")),
            yes_bid_size=cls._num(raw.get("yes_bid_size_fp")),
            yes_ask_size=cls._num(raw.get("yes_ask_size_fp")),
            volume=cls._num(raw.get("volume_fp")),
            volume_24h=cls._num(raw.get("volume_24h_fp")),
            open_interest=cls._num(raw.get("open_interest_fp")),
            close_at_us=parse_iso(raw.get("close_time")),
            rules_primary=rules,
            rules_hash=hashlib.sha256(rules.encode()).hexdigest()[:32] if rules else "",
        )


# --------------------------------------------------------------------------- #
# Orders and fills
# --------------------------------------------------------------------------- #
class OrderRequest(BaseModel):
    """What a sleeve wants.  The executor turns this into a venue call.

    `rationale` is mandatory (PLAN.md C4.2c): an order whose reasoning cannot be
    reconstructed later is a bug.
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    sleeve_id: str
    venue: Venue
    ticker: str
    side: Side
    price_cents: Cents
    size: int = Field(gt=0)
    post_only: bool = True              # I1: maker by default
    mode: RunMode = RunMode.SHADOW
    structure_id: str | None = None
    rationale: dict[str, Any] = Field(default_factory=dict)

    @field_validator("rationale")
    @classmethod
    def _require_rationale(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not v:
            raise ValueError("rationale is mandatory (PLAN.md C4.2c)")
        return v


class Fill(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_order_id: str
    venue_fill_id: str
    filled_at_us: int
    price_cents: int
    size: int
    fee_cents: int                       # signed: negative = rebate received
    is_maker: bool
    terminal: bool = True                # PM: MATCHED can later FAIL


class Position(BaseModel):
    """Signed net exposure.  I4: derived from terminal fills, never a counter."""

    model_config = ConfigDict(frozen=True)

    venue: Venue
    ticker: str
    net_contracts: int                   # positive = long YES, negative = long NO
    avg_price_cents: float = 0.0

    @property
    def side(self) -> Side | None:
        if self.net_contracts == 0:
            return None
        return Side.YES if self.net_contracts > 0 else Side.NO
