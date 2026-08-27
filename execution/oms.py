"""Order management.  T-041.  PLAN.md 5, 6.4, I4.

The OMS owns the `orders` and `fills` tables and nothing else.  It exists to make
two guarantees that no amount of care in the executor could provide on its own:

IDEMPOTENCY (PLAN.md 0.3 "Order identity", T-041)
-------------------------------------------------
`client_order_id` is a UUIDv4 minted BEFORE the send and is the idempotency key.
`record_intent()` writes `state='pending'` and returns whether the row was NEW.
The insert is `ON CONFLICT DO NOTHING` against a PRIMARY KEY, so the "have I
already sent this?" question is answered by the database's own uniqueness
constraint rather than by a lookup the caller could race or forget.  A replay
therefore returns False and the executor stops -- there is no code path in which
the same `client_order_id` reaches a venue twice.

The ordering matters as much as the key: the intent row is written BEFORE the
network call.  Crash between the two and the row survives as `pending` with no
`venue_order_id`, which is precisely the signature `reconcile()` looks for.  Do
it the other way round and a crash mid-send leaves an order resting at the venue
that no local record mentions -- unfindable, uncancellable by the executor, and
still exposed.

POSITION FROM FILLS (I4 / R5b)
------------------------------
`position()` is a SQL aggregate over terminal fills joined to their orders.  It
is never a counter, never cached, and never read from the venue's positions
endpoint (which lags fills by ~1s, PLAN.md 6.4).  A counter drifts silently on
every dropped websocket message; an aggregate over rows that were persisted
before anything acted on them cannot.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.db import Database
from core.models import Fill, OrderRequest, OrderState, Position, RunMode, Side, Venue, now_us

# States in which an order may still be resting at the venue.  PENDING is in the
# list deliberately: a send whose response never arrived may well have landed.
OPEN_STATES: tuple[OrderState, ...] = (
    OrderState.PENDING,
    OrderState.OPEN,
    OrderState.PARTIAL,
)

TERMINAL_STATES: tuple[OrderState, ...] = (
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
)


class RestingOrderSource(Protocol):
    """The slice of a venue client that reconciliation needs."""

    def resting_orders(self, **params: Any) -> list[dict[str, Any]]: ...


def new_client_order_id() -> str:
    """UUIDv4, minted BEFORE the send (PLAN.md 0.3).  The idempotency key."""
    return str(uuid.uuid4())


def _num(raw: Any, default: float = 0.0) -> float:
    """Kalshi returns fixed-point DOLLAR/COUNT STRINGS ('1.00', '0.6720')."""
    if raw is None:
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class OrderRecord:
    """One row of `orders`, plus the terminal fill total for that order."""

    client_order_id: str
    created_at_us: int
    sleeve_id: str
    structure_id: str | None
    venue: Venue
    ticker: str
    side: Side
    price_cents: int
    size: int
    post_only: bool
    mode: RunMode
    venue_order_id: str | None
    state: OrderState
    rationale: dict[str, Any]
    updated_at_us: int
    filled_size: int = 0

    @property
    def remaining(self) -> int:
        """What is still resting.  A partial fill leaves the residual on the book
        holding its queue position (PLAN.md 6.4) -- so the diff must compare
        against THIS, not against `size`."""
        return max(0, self.size - self.filled_size)

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    def key(self) -> tuple[str, str, int]:
        """Same identity the sleeve's `DesiredQuote.key()` uses, so they diff."""
        return (self.ticker, self.side.value, self.price_cents)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OrderRecord":
        keys = row.keys()
        return cls(
            client_order_id=row["client_order_id"],
            created_at_us=row["created_at_us"],
            sleeve_id=row["sleeve_id"],
            structure_id=row["structure_id"],
            venue=Venue(row["venue"]),
            ticker=row["ticker"],
            side=Side(row["side"]),
            price_cents=row["price_cents"],
            size=row["size"],
            post_only=bool(row["post_only"]),
            mode=RunMode(row["mode"]),
            venue_order_id=row["venue_order_id"],
            state=OrderState(row["state"]),
            rationale=json.loads(row["rationale_json"] or "{}"),
            updated_at_us=row["updated_at_us"],
            filled_size=int(row["filled_size"] or 0) if "filled_size" in keys else 0,
        )


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Local versus venue.  PLAN.md 6.6: on drift, halt the venue and page.

    `adopted` is not cosmetic -- it is the crash-mid-send recovery (T-041).  An
    order we wrote as `pending` and never got an ack for, which the venue is in
    fact resting, is reunited with its `venue_order_id` here.  Until that
    happens the executor cannot cancel it, because it has nothing to cancel by.
    """

    checked_at_us: int
    local_open: int
    venue_resting: int
    missing_at_venue: tuple[str, ...] = ()      # client_order_ids we think rest
    unknown_at_venue: tuple[str, ...] = ()      # venue order ids we never wrote
    adopted: tuple[str, ...] = ()               # pending locally, resting at venue
    size_drift: tuple[tuple[str, int, float], ...] = ()   # coid, local, venue

    @property
    def is_clean(self) -> bool:
        """The `and a successful reconciliation` half of PLAN.md 10.6."""
        return not (
            self.missing_at_venue
            or self.unknown_at_venue
            or self.adopted
            or self.size_drift
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at_us": self.checked_at_us,
            "local_open": self.local_open,
            "venue_resting": self.venue_resting,
            "missing_at_venue": list(self.missing_at_venue),
            "unknown_at_venue": list(self.unknown_at_venue),
            "adopted": list(self.adopted),
            "size_drift": [list(t) for t in self.size_drift],
            "is_clean": self.is_clean,
        }

    def __str__(self) -> str:
        if self.is_clean:
            return f"clean: {self.local_open} local / {self.venue_resting} venue"
        return (
            f"DRIFT: missing_at_venue={len(self.missing_at_venue)} "
            f"unknown_at_venue={len(self.unknown_at_venue)} "
            f"adopted={len(self.adopted)} size_drift={len(self.size_drift)}"
        )


# --------------------------------------------------------------------------- #
# OMS
# --------------------------------------------------------------------------- #
@dataclass
class OMS:
    """Order lifecycle over a `Database`.  The DB is the truth (I4)."""

    db: Database
    venue: Venue = Venue.KALSHI
    _replays: list[str] = field(default_factory=list, repr=False)

    # --------------------------------------------------------------- intents
    def record_intent(self, req: OrderRequest) -> bool:
        """Write `state='pending'` BEFORE any network call.

        Returns True when this `client_order_id` is NEW and the caller may send.
        Returns False when a row already exists -- i.e. this is a REPLAY, and
        sending again would double the position.  The caller must treat False as
        "already handled" and do nothing.

        The uniqueness test is the PRIMARY KEY itself rather than a prior SELECT,
        so two threads (or a retry loop racing its own earlier attempt) cannot
        both observe "absent" and both send.
        """
        ts = now_us()
        with self.db.tx() as c:
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
                    # C4.2c: the rationale is persisted WITH the order, in the same
                    # statement.  An order whose reasoning cannot be reconstructed
                    # later is a bug, so there is no window in which one exists.
                    json.dumps(req.rationale, default=str),
                    ts,
                ),
            )
            inserted = cur.rowcount == 1
        if not inserted:
            self._replays.append(req.client_order_id)
        return inserted

    @property
    def replays(self) -> tuple[str, ...]:
        """client_order_ids that were replayed and therefore NOT re-sent."""
        return tuple(self._replays)

    # ------------------------------------------------------------------- acks
    def record_ack(
        self,
        client_order_id: str,
        venue_order_id: str | None = None,
        state: OrderState = OrderState.OPEN,
        *,
        mode: RunMode | None = None,
        rationale_extra: dict[str, Any] | None = None,
    ) -> bool:
        """Record what the venue said.  Returns False if the order is unknown.

        `venue_order_id` is COALESCEd: an ack that omits it (shadow, or a venue
        reply that only carries a status) must never erase an id we already hold,
        because that id is the only handle the kill path has for cancelling.

        `mode` re-asserts the run mode.  It is needed because
        `shadow.engine.ShadowExecutor.submit()` writes the row itself with a
        hardcoded `mode='shadow'`, which would mislabel a BACKTEST order.
        """
        ts = now_us()
        rec = self.get(client_order_id)
        if rec is None:
            return False
        rationale_json: str | None = None
        if rationale_extra:
            rationale_json = json.dumps({**rec.rationale, **rationale_extra}, default=str)
        with self.db.tx() as c:
            c.execute(
                """UPDATE orders SET
                     venue_order_id = COALESCE(?, venue_order_id),
                     state          = ?,
                     mode           = COALESCE(?, mode),
                     rationale_json = COALESCE(?, rationale_json),
                     updated_at_us  = ?
                   WHERE client_order_id = ?""",
                (
                    venue_order_id,
                    OrderState(state).value,
                    mode.value if mode is not None else None,
                    rationale_json,
                    ts,
                    client_order_id,
                ),
            )
        return True

    def record_reject(self, client_order_id: str, reason: str,
                      **detail: Any) -> bool:
        """A venue rejection is INFORMATION and is kept as such (PLAN.md 6.4).

        The reason lands in the order's own rationale so the post-mortem reads
        one row, not a row plus a log file that may have rotated away.
        """
        return self.record_ack(
            client_order_id,
            None,
            OrderState.REJECTED,
            rationale_extra={"reject_reason": reason, **detail},
        )

    def mark_cancelled(self, client_order_id: str, *, reason: str = "") -> bool:
        extra = {"cancel_reason": reason} if reason else None
        return self.record_ack(client_order_id, None, OrderState.CANCELLED,
                               rationale_extra=extra)

    # ------------------------------------------------------------------ fills
    def record_fill(self, fill: Fill) -> bool:
        """Persist one fill.  Returns False if it was already known.

        Deduped on `venue_fill_id` by a UNIQUE constraint, not by a set in
        memory: fills arrive over a websocket that reconnects, and a REST
        backfill after a reconnect re-delivers everything in the window.  Double
        counting there corrupts the position permanently, and the position is
        what every risk limit is measured against.
        """
        if not fill.venue_fill_id:
            raise ValueError("venue_fill_id is the dedupe key and cannot be empty")
        if self.get(fill.client_order_id) is None:
            raise ValueError(
                f"fill {fill.venue_fill_id} references unknown order "
                f"{fill.client_order_id}; the intent must be recorded first (I4)"
            )
        with self.db.tx() as c:
            cur = c.execute(
                """INSERT INTO fills
                   (filled_at_us, client_order_id, venue_fill_id, price_cents, size,
                    fee_cents, is_maker, terminal)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(venue_fill_id) DO NOTHING""",
                (
                    fill.filled_at_us, fill.client_order_id, fill.venue_fill_id,
                    fill.price_cents, fill.size, fill.fee_cents,
                    int(fill.is_maker), int(fill.terminal),
                ),
            )
            inserted = cur.rowcount == 1
        if inserted:
            self._refresh_order_state(fill.client_order_id)
        return inserted

    def _refresh_order_state(self, client_order_id: str) -> None:
        """Derive open/partial/filled from the fills, not from an event counter."""
        rec = self.get(client_order_id)
        if rec is None or rec.state in (OrderState.CANCELLED, OrderState.REJECTED):
            return
        # Only TERMINAL fills move an order's state: Polymarket can report a
        # MATCHED fill that later FAILs, and an order marked filled on a fill
        # that unwinds is an order the executor stops managing while it still
        # rests (core/models.py Fill.terminal).
        if rec.filled_size <= 0:
            return
        state = OrderState.FILLED if rec.filled_size >= rec.size else OrderState.PARTIAL
        self.record_ack(client_order_id, None, state)

    def fills_for(self, client_order_id: str) -> list[Fill]:
        rows = self.db.conn.execute(
            "SELECT * FROM fills WHERE client_order_id = ? ORDER BY filled_at_us",
            (client_order_id,),
        ).fetchall()
        return [
            Fill(
                client_order_id=r["client_order_id"],
                venue_fill_id=r["venue_fill_id"],
                filled_at_us=r["filled_at_us"],
                price_cents=r["price_cents"],
                size=r["size"],
                fee_cents=r["fee_cents"],
                is_maker=bool(r["is_maker"]),
                terminal=bool(r["terminal"]),
            )
            for r in rows
        ]

    # -------------------------------------------------------------- positions
    def position(self, ticker: str, *, venue: Venue | None = None) -> Position:
        """Net exposure in `ticker`, computed from TERMINAL FILLS ONLY (I4/R5b).

        Sides are normalised to YES (PLAN.md 0.3): a NO contract bought at q is a
        short YES at 100-q, so it carries a negative signed size and a
        YES-referenced price of 100-q.  Without that normalisation a hedged
        two-sided quoter looks like two positions instead of none, and every cap
        measured against it is wrong.
        """
        v = (venue or self.venue).value
        rows = self.db.conn.execute(
            """SELECT o.side AS side, f.price_cents AS price, f.size AS size
               FROM fills f JOIN orders o ON o.client_order_id = f.client_order_id
               WHERE o.ticker = ? AND o.venue = ? AND f.terminal = 1""",
            (ticker, v),
        ).fetchall()

        net = 0
        basis = 0.0                     # sum(yes_price * signed_size)
        for r in rows:
            is_yes = Side(r["side"]) is Side.YES
            size = int(r["size"])
            signed = size if is_yes else -size
            yes_price = float(r["price"]) if is_yes else 100.0 - float(r["price"])
            net += signed
            basis += yes_price * signed
        avg = (basis / net) if net else 0.0
        return Position(
            venue=Venue(v),
            ticker=ticker,
            net_contracts=net,
            avg_price_cents=avg,
        )

    def positions(self, *, venue: Venue | None = None,
                  nonzero_only: bool = True) -> dict[str, Position]:
        v = (venue or self.venue).value
        tickers = [
            r["ticker"]
            for r in self.db.conn.execute(
                """SELECT DISTINCT o.ticker AS ticker
                   FROM fills f JOIN orders o ON o.client_order_id = f.client_order_id
                   WHERE o.venue = ? AND f.terminal = 1""",
                (v,),
            ).fetchall()
        ]
        out: dict[str, Position] = {}
        for t in tickers:
            p = self.position(t, venue=Venue(v))
            if p.net_contracts or not nonzero_only:
                out[t] = p
        return out

    def realized_fees_cents(self, *, sleeve_id: str | None = None) -> int:
        """Signed: negative is a rebate received (core/models.py Fill.fee_cents)."""
        sql = ["SELECT COALESCE(SUM(f.fee_cents), 0) AS fees FROM fills f",
               "JOIN orders o ON o.client_order_id = f.client_order_id",
               "WHERE f.terminal = 1"]
        params: list[Any] = []
        if sleeve_id:
            sql.append("AND o.sleeve_id = ?")
            params.append(sleeve_id)
        row = self.db.conn.execute(" ".join(sql), params).fetchone()
        return int(row["fees"] or 0)

    # ----------------------------------------------------------------- reads
    _SELECT = """
        SELECT o.*, COALESCE(t.filled, 0) AS filled_size
        FROM orders o
        LEFT JOIN (SELECT client_order_id, SUM(size) AS filled
                   FROM fills WHERE terminal = 1
                   GROUP BY client_order_id) t
          ON t.client_order_id = o.client_order_id
    """

    def get(self, client_order_id: str) -> OrderRecord | None:
        row = self.db.conn.execute(
            f"{self._SELECT} WHERE o.client_order_id = ?", (client_order_id,)
        ).fetchone()
        return OrderRecord.from_row(row) if row else None

    def open_orders(
        self,
        *,
        sleeve_id: str | None = None,
        venue: Venue | None = None,
        ticker: str | None = None,
        states: Sequence[OrderState] = OPEN_STATES,
    ) -> list[OrderRecord]:
        """Everything that may still be resting.  The left side of the diff."""
        clauses = [f"o.state IN ({','.join('?' * len(states))})"]
        params: list[Any] = [OrderState(s).value for s in states]
        if sleeve_id is not None:
            clauses.append("o.sleeve_id = ?")
            params.append(sleeve_id)
        if venue is not None:
            clauses.append("o.venue = ?")
            params.append(Venue(venue).value)
        if ticker is not None:
            clauses.append("o.ticker = ?")
            params.append(ticker)
        rows = self.db.conn.execute(
            f"{self._SELECT} WHERE {' AND '.join(clauses)} ORDER BY o.created_at_us",
            params,
        ).fetchall()
        return [OrderRecord.from_row(r) for r in rows]

    def orders_for_sleeve(self, sleeve_id: str, *, limit: int = 500) -> list[OrderRecord]:
        rows = self.db.conn.execute(
            f"{self._SELECT} WHERE o.sleeve_id = ? ORDER BY o.created_at_us DESC LIMIT ?",
            (sleeve_id, limit),
        ).fetchall()
        return [OrderRecord.from_row(r) for r in rows]

    # ------------------------------------------------------------ reconcile
    def reconcile(
        self,
        client: RestingOrderSource,
        *,
        venue: Venue | None = None,
        adopt: bool = True,
    ) -> DriftReport:
        """Compare local open orders against the venue's resting orders.

        Reports drift; it does NOT silently repair it, because PLAN.md 6.6 wants
        a human acknowledgement on drift.  The one exception is ADOPTION: an
        order we wrote as `pending` that the venue is resting under our own
        `client_order_id` is unambiguously ours, and reuniting it with its
        `venue_order_id` is what makes it cancellable again.  Everything else --
        an order we believe rests and the venue does not (it may have filled, or
        never landed), or an order at the venue we have no record of -- is
        reported and left alone.
        """
        v = venue or self.venue
        local = {r.client_order_id: r for r in self.open_orders(venue=v)}
        by_venue_id = {
            r.venue_order_id: r for r in local.values() if r.venue_order_id
        }

        remote = list(client.resting_orders())
        matched: set[str] = set()
        unknown: list[str] = []
        adopted: list[str] = []
        size_drift: list[tuple[str, int, float]] = []

        for o in remote:
            coid = o.get("client_order_id") or ""
            oid = o.get("order_id") or ""
            rec = local.get(coid) or by_venue_id.get(oid)
            if rec is None:
                unknown.append(oid or coid or "<unidentified>")
                continue
            matched.add(rec.client_order_id)

            if adopt and (rec.state is OrderState.PENDING or not rec.venue_order_id):
                # Crash-mid-send recovery (T-041): the send DID land.
                self.record_ack(rec.client_order_id, oid or None, OrderState.OPEN,
                                rationale_extra={"adopted_by_reconcile": True})
                adopted.append(rec.client_order_id)

            remaining = _num(o.get("remaining_count"), default=float(rec.remaining))
            if abs(remaining - rec.remaining) > 1e-9:
                size_drift.append((rec.client_order_id, rec.remaining, remaining))

        missing = tuple(sorted(set(local) - matched))
        return DriftReport(
            checked_at_us=now_us(),
            local_open=len(local),
            venue_resting=len(remote),
            missing_at_venue=missing,
            unknown_at_venue=tuple(unknown),
            adopted=tuple(adopted),
            size_drift=tuple(size_drift),
        )

    # ------------------------------------------------------------------ stats
    def counts_by_state(self, *, sleeve_id: str | None = None) -> dict[str, int]:
        sql = "SELECT state, COUNT(*) AS n FROM orders"
        params: list[Any] = []
        if sleeve_id:
            sql += " WHERE sleeve_id = ?"
            params.append(sleeve_id)
        sql += " GROUP BY state"
        return {r["state"]: r["n"] for r in self.db.conn.execute(sql, params)}
