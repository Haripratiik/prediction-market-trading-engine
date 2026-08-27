"""Fill ingestion -- the only thing that closes the loop.  PLAN.md 6.4, 7.2, I4.

`OMS.record_fill()` existed and nothing called it.  The engine placed orders and
never learned whether they filled, so `position()`, realized fees, mark-outs and
every KPI downstream of them were permanently empty -- not wrong, EMPTY, which is
worse, because an empty risk measurement reads as "no exposure" to every cap.

This module is the missing caller, and it has two paths behind one interface:

  LIVE / PAPER   `VenueFillFeed` pages `client.iter_fills()` from a persisted
                 high-water mark, maps each venue fill back onto OUR
                 `client_order_id`, and hands it to `OMS.record_fill()`.
  SHADOW / BACKTEST
                 `ShadowFillFeed` materialises counterfactual fills from the
                 recorded trade tape via `shadow.engine.counterfactual_fill` and
                 writes them into the SAME `fills` table.

Same table, same reader, same KPIs.  That is the whole point of PLAN.md 7.2: if
shadow P&L came out of a different pipe than live P&L, a shadow result would
prove nothing about the live one.

THREE THINGS THAT ARE EASY TO GET WRONG AND EXPENSIVE WHEN YOU DO
-----------------------------------------------------------------
1. PRICE REFERENCE, AND IT IS NOT UNIFORM ACROSS THE TWO COLUMNS.

     `orders.price_cents`  YES-REFERENCED on both sides (PLAN.md 0.3, and stated
                           in strategy/s2, strategy/s3, risk/engine,
                           backtest/fills, execution/structures, monitor/kpi).
                           A NO quote at YES-price p rests a YES ask at p and
                           locks 100 - p of capital.
     `fills.price_cents`   SIDE-REFERENCED.  `execution.oms.OMS.position` reads
                           it as `yes = 100 - price` for a NO order, and
                           `runner.py` then locks `100 - avg` against the short.
                           That chain is only correct if what we store is the
                           price paid for the side we bought.

   So a NO fill at YES-price p is a SHORT YES of the same size and is stored at
   100 - p.  `_stored_price_cents` is the ONE place the conversion happens and
   both paths go through it.  Store it YES-referenced instead and the basis is
   wrong by (100 - 2p) while the capital charged for a short-basket leg collapses
   by up to 20x -- the exact failure `risk.engine.per_contract_cost_cents` exists
   to prevent.  The sign of a position never depends on the price at all: it
   comes from `orders.side`, which is why that is the one field of a venue
   payload we refuse to trust.

   REPORTED, NOT FIXED (nothing outside this module was touched).  Two readers
   join `fills.price_cents` to a YES-referenced number and are therefore wrong by
   (100 - 2p) on NO fills now that fills exist at all:
   `monitor.kpi._taker_slippage` differences it against `orders.price_cents`, and
   the mark-out query above `monitor.kpi.markouts` feeds it to
   `shadow.engine.markout`, which is YES-referenced by construction.  Both are
   one-line fixes in files this module does not own.  The alternative -- storing
   fills YES-referenced -- fixes those two and breaks `OMS.position` and the
   capital arithmetic in `runner.py` instead, which is the strictly worse trade.

2. IDENTITY, NOT SEQUENCE.  Dedupe is on the venue's own fill id, never on "have
   I seen this many fills".  A websocket reconnect plus a REST backfill
   re-delivers a whole window, and double-counting a fill corrupts the position
   permanently.  Replaying a page is therefore a NO-OP, and the tests prove it.

3. FEES ARE SIGNED AND PER SERIES.  `core.math.contracts.fee` is
   `theta * p * (1-p)` with theta read from the market's SERIES row -- a maker on
   a plain `quadratic` series (13,353 of 13,518) pays exactly ZERO, and a
   Polymarket maker is paid a REBATE, which is a NEGATIVE fee.  Taking an
   absolute value anywhere here turns a credit into a charge.

ORDERING: fills are written BEFORE the high-water mark advances, for the same
reason `OMS.record_intent` writes before the send.  Crash in between and the next
poll re-reads a window we already stored, which dedupe makes free.  Do it the
other way round and the crash silently eats a fill -- and a fill you never
ingested is a position you do not know you have.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.db import Database
from core.math.contracts import FeeSpec
from core.math.contracts import fee as fee_per_contract_dollars
from core.models import (Fill, OrderState, RunMode, Side, Venue, now_us,
                          parse_iso)
from execution.executor import PAPERLESS_MODES
from execution.oms import OMS, OrderRecord
from shadow.engine import FillModel, ShadowOrder, counterfactual_fill

# `schema_meta` is a key/value table that already exists; the high-water mark
# lives there rather than in a new table so that it commits to the SAME database
# file as the fills it describes.  A mark stored anywhere else can survive a
# database that was restored from a backup, and then it skips real fills.
HIGH_WATER_KEY = "fillfeed.high_water_us"

# Deterministic id prefix for a materialised counterfactual fill.  It carries the
# CUMULATIVE filled size (see `ShadowFillFeed.poll`), which is what makes
# re-running the materialisation idempotent AND incremental at the same time.
SHADOW_FILL_PREFIX = "shadow"

# Float slop guard.  0.07 * 0.4 * 0.6 * 100 * 100 is 168 in exact arithmetic and
# 168.00000000000003 in binary floating point; without this every such fee would
# round UP a whole cent.
_EPS = 1e-9

# ---------------------------------------------------------------------------- #
# Kalshi fill payload field names.
#
# UNVERIFIED.  `GET /portfolio/fills` needs credentials, so none of this was
# checked against a real response -- it is reconstructed from the V1 shape in the
# unofficial API docs plus the V2 conventions this repo already relies on
# elsewhere (fixed-point DOLLAR strings such as "0.6720", `_fp` counts; see
# `core.models.Market.from_api`).  Each field is therefore a CANDIDATE LIST, and
# a payload that matches none of them is reported as malformed rather than
# silently read as a zero.
# ---------------------------------------------------------------------------- #
FILL_ID_FIELDS: tuple[str, ...] = ("trade_id", "fill_id", "venue_fill_id", "id")
ORDER_ID_FIELDS: tuple[str, ...] = ("order_id", "venue_order_id")
CLIENT_ID_FIELDS: tuple[str, ...] = ("client_order_id", "client_id")
COUNT_FIELDS: tuple[str, ...] = ("count", "count_fp", "size", "filled_count", "quantity")
TIME_FIELDS: tuple[str, ...] = ("created_time", "created_ts", "filled_at", "ts", "trade_time")
YES_PRICE_FIELDS: tuple[str, ...] = ("yes_price_dollars", "yes_price")
NO_PRICE_FIELDS: tuple[str, ...] = ("no_price_dollars", "no_price")
FEE_FIELDS: tuple[str, ...] = ("fee_cents", "fee_dollars", "fee")
TAKER_FIELDS: tuple[str, ...] = ("is_taker", "taker")
MAKER_FIELDS: tuple[str, ...] = ("is_maker", "maker")


class FillSource(Protocol):
    """The slice of a venue client that ingestion needs.

    Deliberately one method: a fill feed that could also place or cancel orders
    would be a fill feed that can lose money.
    """

    def iter_fills(self, **params: Any) -> Iterable[Mapping[str, Any]]: ...


# --------------------------------------------------------------------------- #
# Parsing helpers.  Every one of them is total: an unreadable field yields None,
# never a plausible-looking zero.
# --------------------------------------------------------------------------- #
def _first_present(raw: Mapping[str, Any], keys: tuple[str, ...]) -> tuple[str, Any] | None:
    for k in keys:
        v = raw.get(k)
        if v is not None and v != "":
            return k, v
    return None


def _to_float(raw: Any) -> float | None:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _price_field_cents(key: str, raw: Any) -> int | None:
    """One price field -> integer cents.

    Kalshi V2 sends prices as fixed-point DOLLAR strings ("0.6720"); the V1 shape
    sent integer cents (67).  Both are in circulation, so the units are decided
    per field rather than assumed:

      * a `_dollars` suffix is dollars, always;
      * otherwise a value with a fractional part is dollars ("0.6720" -> 67c);
      * otherwise it is already cents (67 -> 67c).

    The one ambiguous value is a bare 1, which is either 1 cent or $1.00.  It is
    read as 1 CENT, because $1.00 is not a tradeable price (the grid is 1..99)
    and a 1-cent fill very much is.
    """
    v = _to_float(raw)
    if v is None:
        return None
    if key.endswith("_dollars") or not float(v).is_integer():
        return int(round(v * 100.0))
    return int(v)


def _yes_price_cents(raw: Mapping[str, Any]) -> int | None:
    """The fill price, YES-REFERENCED, exactly as the venue reports it.

    Falls back to the NO price when only that is present: a NO price of q is a
    YES price of 100 - q, which is the same identity the rest of the system
    uses (`risk.engine.per_contract_cost_cents`).
    """
    hit = _first_present(raw, YES_PRICE_FIELDS)
    if hit is not None:
        return _price_field_cents(*hit)
    hit = _first_present(raw, NO_PRICE_FIELDS)
    if hit is not None:
        no_cents = _price_field_cents(*hit)
        if no_cents is not None:
            return 100 - no_cents
    return None


def _count(raw: Mapping[str, Any]) -> int | None:
    """Contracts filled.  Counts are fixed-point ("10.00" is TEN, not a thousand)."""
    hit = _first_present(raw, COUNT_FIELDS)
    if hit is None:
        return None
    v = _to_float(hit[1])
    return None if v is None else int(round(v))


def _timestamp_us(raw: Mapping[str, Any]) -> int | None:
    """Epoch MICROSECONDS.  Accepts ISO-8601 or an epoch in s / ms / us.

    The magnitude test is a guess about the payload, so it is written to fail
    safe: an epoch-seconds value read as microseconds would land in 1970 and drag
    the high-water mark backwards, which is why the mark never moves back.
    """
    hit = _first_present(raw, TIME_FIELDS)
    if hit is None:
        return None
    _, value = hit
    if isinstance(value, str) and not value.replace(".", "").isdigit():
        return parse_iso(value)
    v = _to_float(value)
    if v is None:
        return None
    if v < 1e11:            # seconds
        return int(v * 1_000_000)
    if v < 1e14:            # milliseconds
        return int(v * 1_000)
    return int(v)           # already microseconds


def _payload_fee_cents(raw: Mapping[str, Any]) -> int | None:
    """A fee the VENUE reported, if it reports one at all.

    Venue truth beats our model of venue truth, so this wins when present.  It is
    signed and it is NOT abs()'d: Kalshi's own schedule has a rebate leg, and a
    rebate recorded as a charge is a two-sided error in P&L.
    """
    hit = _first_present(raw, FEE_FIELDS)
    if hit is None:
        return None
    key, value = hit
    v = _to_float(value)
    if v is None:
        return None
    if key == "fee_cents":
        return int(round(v))
    return int(round(v * 100.0))        # dollars


def _is_maker(raw: Mapping[str, Any], order: OrderRecord) -> bool:
    """Maker or taker.  Decides whether the fee is charged at all on Kalshi.

    Falls back to the order's own `post_only` flag: an order the venue accepted
    as post-only CANNOT have taken liquidity, so that is a sound fallback rather
    than an optimistic one.
    """
    hit = _first_present(raw, TAKER_FIELDS)
    if hit is not None:
        return not _truthy(hit[1])
    hit = _first_present(raw, MAKER_FIELDS)
    if hit is not None:
        return _truthy(hit[1])
    return bool(order.post_only)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "t", "y")


def _stored_price_cents(side: Side, yes_price_cents: int) -> int:
    """YES-referenced -> what `fills.price_cents` must hold.  THE conversion.

    Both paths funnel through this one function, which is the point: the venue
    reports a YES price and a counterfactual is decided at a YES price, so if the
    two agreed on nothing else they would still agree here.

    `OMS.position` re-derives the YES price as `100 - price` for a NO order and
    hands the result to `runner.py`, which locks `100 - avg` of capital against a
    short.  Store a NO fill YES-referenced and that chain double-inverts: the
    position's basis is wrong by (100 - 2p) cents and the capital charged for a
    short basket leg collapses to the 20x under-count that
    `risk.engine.per_contract_cost_cents` was written to stop.
    """
    return yes_price_cents if side is Side.YES else 100 - yes_price_cents


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class IngestReport:
    """What one poll did.  Same shape of answer as `execution.oms.DriftReport`.

    `unmatched` and `malformed` are NOT errors to be swallowed.  A fill we cannot
    tie to a local order is a position we are carrying and cannot see, which is
    the PLAN.md 6.6 drift condition -- so the high-water mark refuses to advance
    past one, and every later poll re-offers it until the order is adopted.
    """

    scanned: int = 0
    recorded: int = 0
    duplicates: int = 0
    contracts: int = 0
    fees_cents: int = 0                     # SIGNED: negative is a rebate received
    high_water_us: int = 0
    unmatched: tuple[str, ...] = ()
    malformed: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not (self.unmatched or self.malformed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "recorded": self.recorded,
            "duplicates": self.duplicates,
            "contracts": self.contracts,
            "fees_cents": self.fees_cents,
            "high_water_us": self.high_water_us,
            "unmatched": list(self.unmatched),
            "malformed": list(self.malformed),
            "is_clean": self.is_clean,
        }

    def __str__(self) -> str:
        base = (f"scanned={self.scanned} recorded={self.recorded} "
                f"dup={self.duplicates} contracts={self.contracts} "
                f"fees={self.fees_cents}c")
        if self.is_clean:
            return base
        return (f"{base} UNMATCHED={len(self.unmatched)} "
                f"MALFORMED={len(self.malformed)}")


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #
@dataclass
class FillFeed(ABC):
    """One interface, two implementations (PLAN.md 7.2).

    Everything shared lives here -- the fee model, the series lookup and the
    high-water mark -- so that a shadow fill and a live fill are priced by the
    SAME code.  A fee model that differed between the two would make every shadow
    P&L incomparable with the live one it is supposed to predict.
    """

    oms: OMS
    venue: Venue | None = None
    default_fee_spec: FeeSpec | None = None
    _series_cache: dict[str, FeeSpec] = field(default_factory=dict, repr=False)

    #: Namespaces the high-water mark.  The two feeds must NOT share one: a
    #: shadow pass marking the tape it consumed would otherwise push the live
    #: resume point forward and skip real fills on the next live poll.
    feed_kind: str = "base"

    @property
    def db(self) -> Database:
        return self.oms.db

    @property
    def venue_id(self) -> Venue:
        return self.venue or self.oms.venue

    @abstractmethod
    def poll(self, **kwargs: Any) -> IngestReport:
        """Ingest whatever is newly available.  Idempotent by construction."""

    # ------------------------------------------------------------------- fees
    def fee_spec_for(self, ticker: str) -> FeeSpec:
        """The SERIES fee spec for a market.  research/06 section 4.

        Fees are per series, not per venue: `fee_type` decides whether a MAKER
        pays anything at all (13,353 of 13,518 series say no) and
        `fee_multiplier` halves it on the 19 MLB derivative series.  A constant
        here would over-charge almost every maker fill in the book and under-
        charge the ~130 series that do bill makers.
        """
        cached = self._series_cache.get(ticker)
        if cached is not None:
            return cached
        spec = self._resolve_fee_spec(ticker)
        self._series_cache[ticker] = spec
        return spec

    def _resolve_fee_spec(self, ticker: str) -> FeeSpec:
        if self.venue_id is not Venue.KALSHI:
            return self._fallback_spec()
        row = self.db.latest_market(ticker, venue=self.venue_id.value)
        candidates: list[str] = []
        if row is not None and row["series_ticker"]:
            candidates.append(str(row["series_ticker"]))
        # A Kalshi market ticker is `SERIES-EXPIRY-STRIKE`, so the prefix is the
        # series.  This is the fallback for a fill on a market we never
        # snapshotted -- which is exactly what happens after a restart.
        if "-" in ticker:
            candidates.append(ticker.split("-", 1)[0])
        for cand in candidates:
            series = self.db.get_series(cand)
            if series is not None:
                return series.fee_spec
        return self._fallback_spec()

    def _fallback_spec(self) -> FeeSpec:
        """What to charge when the series is unknown.

        The default is the FULL taker rate, never zero.  An unknown fee that is
        assumed free flatters every backtest that touches an un-cached market;
        one that is assumed expensive only ever under-states edge.
        """
        if self.default_fee_spec is not None:
            return self.default_fee_spec
        if self.venue_id is Venue.POLYMARKET_US:
            return FeeSpec.polymarket_us()
        return FeeSpec.kalshi("quadratic", 1.0)

    def fee_cents(self, ticker: str, yes_price_cents: int, size: int,
                  *, is_maker: bool) -> int:
        """Signed fee in whole cents for one fill.

        `fee = theta * p * (1-p)` is SYMMETRIC in p and 1-p, so the YES/NO price
        reference cannot change the magnitude -- only `is_maker` and the series
        can.  Rounding is always toward +infinity, which makes a charge round UP
        (Kalshi rounds fees up per fill, research/06 section 4.3) and a REBATE
        round toward zero.  Both directions are the conservative one.
        """
        p = yes_price_cents / 100.0
        if not 0.0 < p < 1.0 or size <= 0:
            return 0
        spec = self.fee_spec_for(ticker)
        per_contract = fee_per_contract_dollars(p, spec, is_maker=is_maker)
        return math.ceil(per_contract * size * 100.0 - _EPS)

    # ------------------------------------------------------- high-water mark
    @property
    def high_water_key(self) -> str:
        return f"{HIGH_WATER_KEY}.{self.feed_kind}.{self.venue_id.value}"

    @property
    def high_water_us(self) -> int:
        row = self.db.conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (self.high_water_key,)
        ).fetchone()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def set_high_water(self, value: int) -> int:
        """Advance the resume point.  MONOTONE -- it can never move backwards.

        A mark that moved backwards would be harmless (dedupe absorbs a replay);
        a mark that moved FORWARD past a fill we did not store would not be, so
        the only guarantee worth enforcing here is that it never advances past
        unfinished work.  Callers pass a value already clamped to that.
        """
        current = self.high_water_us
        if value <= current:
            return current
        with self.db.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
                (self.high_water_key, str(int(value))),
            )
        return int(value)


# --------------------------------------------------------------------------- #
# LIVE / PAPER
# --------------------------------------------------------------------------- #
@dataclass
class VenueFillFeed(FillFeed):
    """Pages the venue's own fill history into the OMS.

    The venue is the authority on WHAT filled; we remain the authority on WHOSE
    order it was.  So the payload is read only for the fill id, price, size,
    timestamp and maker flag -- the SIDE always comes from our own order row.
    That is deliberate: `side` on a Kalshi fill describes the venue's single YES
    book ("bid"/"ask") while ours describes a buy of a named outcome, and a feed
    that trusted the payload's side would invert every NO fill the first time the
    two vocabularies drifted apart.
    """

    client: FillSource | None = None
    use_min_ts: bool = True
    feed_kind: str = "venue"

    def poll(self, *, page_size: int = 200, max_pages: int = 200,
             **params: Any) -> IngestReport:
        """Read forward from the high-water mark and record everything new.

        Replaying a page is a no-op: `OMS.record_fill` dedupes on the venue's own
        fill id via a UNIQUE constraint, so the second pass counts duplicates and
        moves no position.
        """
        if self.client is None:
            raise ValueError("VenueFillFeed needs a client; use ShadowFillFeed in "
                             "shadow or backtest mode")

        query = dict(params)
        start_us = self.high_water_us
        if self.use_min_ts and start_us > 0 and "min_ts" not in query:
            # Kalshi's window filter is in epoch SECONDS.  FLOOR it: asking for
            # one second too much re-delivers fills we already have (free), while
            # asking for one too little loses a fill inside the same second.
            query["min_ts"] = start_us // 1_000_000

        recorded = duplicates = scanned = contracts = fees = 0
        unmatched: list[str] = []
        malformed: list[str] = []
        newest_done = start_us
        oldest_open: int | None = None

        by_venue_id = self._venue_order_index()

        for raw in self.client.iter_fills(page_size=page_size, max_pages=max_pages,
                                          **query):
            scanned += 1
            parsed = self._parse(raw, by_venue_id)
            if parsed is None:
                malformed.append(self._describe(raw))
                continue
            if isinstance(parsed, str):
                unmatched.append(parsed)
                ts = _timestamp_us(raw)
                if ts is not None:
                    oldest_open = ts if oldest_open is None else min(oldest_open, ts)
                continue
            fill = parsed
            if self.oms.record_fill(fill):
                recorded += 1
                contracts += fill.size
                fees += fill.fee_cents
            else:
                duplicates += 1
            newest_done = max(newest_done, fill.filled_at_us)

        # Never advance past a fill still waiting for its order to be adopted.
        if oldest_open is not None:
            newest_done = min(newest_done, oldest_open - 1)
        high_water = self.set_high_water(newest_done)

        return IngestReport(
            scanned=scanned, recorded=recorded, duplicates=duplicates,
            contracts=contracts, fees_cents=fees, high_water_us=high_water,
            unmatched=tuple(unmatched), malformed=tuple(malformed),
        )

    # ------------------------------------------------------------- internals
    def _venue_order_index(self) -> dict[str, str]:
        """venue_order_id -> client_order_id, over ALL orders, not just open ones.

        A fill routinely arrives for an order the venue has already closed, and a
        late fill can arrive for one we marked cancelled, so restricting this to
        resting orders would drop exactly the fills that matter most.
        """
        rows = self.db.conn.execute(
            """SELECT client_order_id, venue_order_id FROM orders
               WHERE venue = ? AND venue_order_id IS NOT NULL""",
            (self.venue_id.value,),
        ).fetchall()
        return {r["venue_order_id"]: r["client_order_id"] for r in rows}

    def _parse(self, raw: Mapping[str, Any],
               by_venue_id: Mapping[str, str]) -> Fill | str | None:
        """One venue payload -> one `Fill`.

        Three outcomes, all of them explicit: a `Fill` we can record, the fill id
        alone when the fill is real but we cannot say whose order it was, and
        None when the payload is unreadable.  Neither failure is a silent drop --
        both surface in the report, because an unrecorded fill is an unrecorded
        position.
        """
        hit = _first_present(raw, FILL_ID_FIELDS)
        if hit is None:
            return None                      # no dedupe key -> unrecordable
        venue_fill_id = str(hit[1])

        yes_price = _yes_price_cents(raw)
        size = _count(raw)
        if yes_price is None or size is None or size <= 0:
            return None

        order = self._match(raw, by_venue_id)
        if order is None:
            return venue_fill_id

        maker = _is_maker(raw, order)
        fee = _payload_fee_cents(raw)
        if fee is None:
            fee = self.fee_cents(order.ticker, yes_price, size, is_maker=maker)

        return Fill(
            client_order_id=order.client_order_id,
            venue_fill_id=venue_fill_id,
            filled_at_us=_timestamp_us(raw) or now_us(),
            # YES-referenced on the wire, SIDE-referenced in the table.
            price_cents=_stored_price_cents(order.side, yes_price),
            size=size,
            fee_cents=fee,
            is_maker=maker,
            terminal=True,
        )

    def _match(self, raw: Mapping[str, Any],
               by_venue_id: Mapping[str, str]) -> OrderRecord | None:
        """Venue fill -> OUR order.  Our own id first, the venue's id second."""
        hit = _first_present(raw, CLIENT_ID_FIELDS)
        if hit is not None:
            rec = self.oms.get(str(hit[1]))
            if rec is not None:
                return rec
        hit = _first_present(raw, ORDER_ID_FIELDS)
        if hit is not None:
            coid = by_venue_id.get(str(hit[1]))
            if coid:
                return self.oms.get(coid)
        return None

    @staticmethod
    def _describe(raw: Mapping[str, Any]) -> str:
        hit = _first_present(raw, FILL_ID_FIELDS)
        if hit is not None:
            return str(hit[1])
        return f"<no id: {sorted(raw)[:6]}>"


# --------------------------------------------------------------------------- #
# SHADOW / BACKTEST
# --------------------------------------------------------------------------- #
@dataclass
class ShadowFillFeed(FillFeed):
    """Materialises counterfactual fills into the real `fills` table.

    Shadow mode already knew how to ASK whether an order would have filled
    (`shadow.engine.counterfactual_fill`); nothing wrote the answer down.  Until
    it does, shadow positions are empty and no KPI that reads a position -- P&L,
    mark-out, orphan-leg ratio, every risk cap -- means anything in shadow.

    IDEMPOTENT AND INCREMENTAL AT ONCE.  Each pass recomputes the CUMULATIVE
    counterfactual fill from the tape and writes only the delta over what is
    already stored, under an id that names that cumulative total.  Re-running on
    an unchanged tape recomputes the same total, mints the same id, and the UNIQUE
    constraint absorbs it.  Re-running on a longer tape yields a larger total, a
    new id and a delta row.  A sequence-numbered id would double-count on the
    first replay; a size-independent id would never see the second fill.
    """

    # QUEUE-CONSERVATIVE, deliberately, and this default matters.
    #
    # PESSIMISTIC counts only volume that traded STRICTLY THROUGH our price.  On
    # the illiquid mutually-exclusive baskets this engine actually selects, every
    # print lands AT a single price, so that model materialises a structural
    # ZERO -- no fills, no positions, no P&L, no calibration, ever, and it fails
    # silently because it looks identical to a strategy that found nothing.
    #
    # REALISTIC is the model that corresponds to what we actually do: rest a
    # maker order and wait behind the queue displayed ahead of us.  It is the
    # central estimate, not the flattering one -- `queue_ahead` still has to
    # clear before a single contract is credited.
    #
    # Reporting still shows all three (PLAN.md 6.7); this is only the model whose
    # fills are MATERIALISED into the ledger.
    model: FillModel = FillModel.REALISTIC
    horizon_us: int | None = None
    modes: tuple[RunMode, ...] = PAPERLESS_MODES
    feed_kind: str = "shadow"

    def poll(self, *, sleeve_id: str | None = None, ticker: str | None = None,
             **_: Any) -> IngestReport:
        recorded = duplicates = scanned = contracts = fees = 0
        newest = self.high_water_us

        for rec, horizon_us in self._orders_in_scope(sleeve_id, ticker):
            if rec.mode not in self.modes:
                continue                     # a live order is not ours to invent
            scanned += 1
            fill = self._counterfactual(rec, horizon_us=horizon_us)
            if fill is None:
                continue
            if self.oms.record_fill(fill):
                recorded += 1
                contracts += fill.size
                fees += fill.fee_cents
            else:
                duplicates += 1
            newest = max(newest, fill.filled_at_us)

        return IngestReport(
            scanned=scanned, recorded=recorded, duplicates=duplicates,
            contracts=contracts, fees_cents=fees,
            high_water_us=self.set_high_water(newest),
        )

    def _orders_in_scope(
        self, sleeve_id: str | None, ticker: str | None
    ) -> list[tuple[OrderRecord, int | None]]:
        """Orders that were RESTING at some point, with the window they rested for.

        Iterating `open_orders()` alone is why the real database held 0 fills
        against 892,000 tape prints.  The runner polls at the top of a cycle and
        the diff then cancels re-priced quotes further down the SAME cycle, so a
        shadow order got about one poll before disappearing from the query --
        median measured lifetime 153 seconds, against a tape that needs minutes
        to reach a resting price.  Forcing every historical order back to `open`
        recovered 14 real fills the ledger had silently dropped.

        The window is the fix for BOTH directions of that error.  A cancelled
        order is evaluated over exactly the interval it was actually resting, so
        it can still be filled by prints that arrived before the cancel -- and an
        order can no longer claim a print that landed after it was gone, which
        the unbounded horizon happily allowed.
        """
        # Reuse the OMS's own SELECT: it carries the computed `filled_size`, and
        # a bare `SELECT *` silently reports every order as unfilled, which
        # would re-record the same contracts on every poll.
        sql = [self.oms._SELECT, "WHERE o.venue = ?"]
        params: list[Any] = [self.venue_id.value]
        if sleeve_id:
            sql.append("AND o.sleeve_id = ?")
            params.append(sleeve_id)
        if ticker:
            sql.append("AND o.ticker = ?")
            params.append(ticker)
        # `cancelled` is included so an order's final resting window is scored
        # once; `record_fill` dedupes on the venue fill id, so re-scoring an
        # already-scored window is a no-op rather than a double count.
        sql.append("AND o.state IN ('pending','open','partial','cancelled','filled')")
        rows = self.db.conn.execute(" ".join(sql), tuple(params)).fetchall()

        out: list[tuple[OrderRecord, int | None]] = []
        for row in rows:
            rec = OrderRecord.from_row(row)
            if rec.state in (OrderState.OPEN, OrderState.PENDING,
                             OrderState.PARTIAL):
                out.append((rec, self.horizon_us))      # still resting: up to now
                continue
            lifetime = int(row["updated_at_us"] or 0) - rec.created_at_us
            if lifetime <= 0:
                continue                                # cancelled before it rested
            if self.horizon_us is not None:
                lifetime = min(lifetime, self.horizon_us)
            out.append((rec, lifetime))
        return out

    def _counterfactual(self, rec: OrderRecord,
                        *, horizon_us: int | None = None) -> Fill | None:
        shadow = counterfactual_fill(
            self.db, self._as_shadow_order(rec),
            model=self.model,
            horizon_us=self.horizon_us if horizon_us is None else horizon_us,
        )
        # NEVER round a fill up.  Claiming a contract the tape does not support is
        # the one error a pessimistic fill model exists to prevent.
        cumulative = min(rec.size, int(math.floor(shadow.filled_size + _EPS)))
        delta = cumulative - rec.filled_size
        if delta <= 0:
            return None

        # `orders.price_cents` is YES-REFERENCED on both sides (PLAN.md 0.3), so
        # this needs no conversion on the way in -- and exactly the same
        # conversion as a venue fill on the way out.
        yes_price = rec.price_cents
        is_maker = bool(rec.post_only)
        return Fill(
            client_order_id=rec.client_order_id,
            venue_fill_id=f"{SHADOW_FILL_PREFIX}:{rec.client_order_id}:{cumulative}",
            # `first_fill_at_us` is the first print that reached us.  On an
            # incremental pass it still names the FIRST fill rather than this
            # increment's, which is the honest bound available from the tape.
            filled_at_us=shadow.first_fill_at_us or rec.created_at_us,
            # A resting order fills at ITS OWN price, never the taker's -- taking
            # the taker's price would manufacture free basis on every fill.
            price_cents=_stored_price_cents(rec.side, yes_price),
            size=delta,
            fee_cents=self.fee_cents(rec.ticker, yes_price, delta, is_maker=is_maker),
            is_maker=is_maker,
            terminal=True,
        )

    def _as_shadow_order(self, rec: OrderRecord) -> ShadowOrder:
        """Rebuild the decision-time order the fill model needs.

        The book state at decision time was persisted into the order's rationale
        by `shadow.engine.ShadowExecutor.submit`, which is what makes this
        reconstructable after a restart at all.

        `price_cents` passes through UNCONVERTED.  `orders.price_cents` is
        YES-referenced on both sides (PLAN.md 0.3; `strategy/s3_linked_rv.py`
        "the SELL leg is Side.NO at the YES price we want to sell at, NOT at
        100 - p"), and `counterfactual_fill` reads its NO branch the same way
        ("a resting BUY NO at YES-price p").  Mirroring it here would query the
        tape at the wrong level entirely -- and one-sidedly, so a NO leg would
        either never fill or fill on prints that never reached us.

        `created_at_us` stands in for the decision time: `record_intent` stamps
        it before the ShadowOrder is built, so it is the earliest defensible
        anchor and cannot let the model see a print it decided after.
        """
        rationale = dict(rec.rationale) or {"reconstructed_by": "fillfeed"}
        return ShadowOrder.create(
            client_order_id=rec.client_order_id,
            sleeve_id=rec.sleeve_id,
            ticker=rec.ticker,
            side=rec.side,
            price_cents=rec.price_cents,
            size=rec.size,
            queue_ahead=float(rationale.get("queue_ahead") or 0.0),
            book_bid=rationale.get("book_bid"),
            book_ask=rationale.get("book_ask"),
            rationale=rationale,
            decided_at_us=rec.created_at_us,
            structure_id=rec.structure_id,
            mode=rec.mode,
        )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def fill_feed_for(mode: RunMode, oms: OMS, *, client: FillSource | None = None,
                  **kwargs: Any) -> FillFeed:
    """The right feed for a run mode.  ONE place decides, so the two paths cannot
    drift apart.

    BACKTEST and SHADOW materialise from the tape; PAPER and LIVE read the venue.
    The split is `execution.executor.PAPERLESS_MODES` itself rather than a copy of
    it -- a mode that never sends an order can never have a venue fill to read.
    """
    if mode in PAPERLESS_MODES:
        return ShadowFillFeed(oms=oms, **kwargs)
    if client is None:
        raise ValueError(f"mode {mode.value} needs a venue client to read fills from")
    return VenueFillFeed(oms=oms, client=client, **kwargs)
