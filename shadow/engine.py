"""Shadow engine.  T-044.  The only mechanism that validates edge without capital.

Neither venue offers a simulator that reproduces real liquidity: Kalshi's demo
has quotes but ZERO volume (verified: 202 of 1000 demo markets show a two-sided
quote, none has traded), and Polymarket US has no sandbox at all.  So shadow mode
is not a convenience -- it is the validator, and it has to be built.

It runs the IDENTICAL sleeve code path as live.  The only difference is that the
executor records orders instead of sending them, and fills are reconstructed
afterwards from the recorded tape.

THE FILL RULE, and why it is deliberately pessimistic
-----------------------------------------------------
You never actually joined the queue, so you must assume you were behind EVERY
contract resting at your price when you decided.  A resting buy-YES at price p is
filled only once cumulative sell-YES volume at prices <= p exceeds the size that
was displayed ahead of you.

That systematically UNDER-counts fills, which is the correct direction of error
for a go/no-go decision: touch-fill overstates fill speed ~1.6x while
trade-through understates it ~2.4x, and a strategy that is only profitable under
the optimistic bound does not exist.

Kalshi labels `taker_side` on the public tape, so trade direction is GROUND TRUTH
here -- no Lee-Ready, no BVC.  (On Polymarket, feed-inferred direction agrees with
on-chain truth only ~59% of the time, flipping the sign of the effective
half-spread 67% of the time.)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from core.db import Database
from core.models import RunMode, Side, now_us


class FillModel(StrEnum):
    """PLAN.md 6.7 -- report ALL THREE.  Never a point estimate.

    The middle model was missing here while `backtest/fills.py` had it, and the
    gap was not cosmetic.  `PESSIMISTIC` counts only volume that traded STRICTLY
    THROUGH our price and then ALSO makes us queue behind everything displayed
    at it.  On a market whose prints all land at a single price -- which is most
    of an illiquid prediction market, and every one of the arbitrage baskets
    this engine selects -- the strict inequality matches NOTHING, so the
    pessimistic column reads a structural, permanent ZERO.

    Measured on a live resting order (KXTESTMATCH ... -ENG, a YES ask at 74c
    with 428 contracts subsequently taking YES at exactly 74c):

        pessimistic  0 / 92 contracts     (volume through price = 0)
        optimistic  92 / 92 contracts     (volume at price   = 427.7)

    Nothing between them, and PLAN.md R6.7a says to GATE on the pessimistic
    column.  A gate criterion that is structurally zero never promotes anything,
    and it fails silently -- it looks exactly like a strategy that found no
    fills.  REALISTIC is the estimate that actually corresponds to resting a
    maker order: volume at OR through our price, minus the queue ahead of us.
    """

    PESSIMISTIC = "pessimistic"   # only volume THROUGH our price, behind the queue
    REALISTIC = "realistic"       # volume AT or through, behind the queue  <- the maker case
    OPTIMISTIC = "optimistic"     # any qualifying print fills us, queue ignored


@dataclass(frozen=True, slots=True)
class ShadowOrder:
    """An order the sleeve wanted, recorded instead of sent."""

    client_order_id: str
    sleeve_id: str
    ticker: str
    side: Side
    price_cents: int
    size: int
    decided_at_us: int
    # book state AT DECISION TIME -- this is what makes the fill model honest
    queue_ahead: float          # size displayed at our price when we decided
    book_bid: int | None
    book_ask: int | None
    rationale: dict
    structure_id: str | None = None
    mode: RunMode = RunMode.SHADOW

    @classmethod
    def create(cls, *, sleeve_id: str, ticker: str, side: Side, price_cents: int,
               size: int, queue_ahead: float, book_bid: int | None,
               book_ask: int | None, rationale: dict,
               decided_at_us: int | None = None,
               client_order_id: str | None = None,
               structure_id: str | None = None,
               mode: RunMode = RunMode.SHADOW) -> "ShadowOrder":
        if not rationale:
            raise ValueError("rationale is mandatory (PLAN.md C4.2c)")
        return cls(
            # Minting a UUID here unconditionally meant an order routed through
            # the executor got TWO ids -- the idempotency key already written by
            # `record_intent`, and a second one invented here -- so every shadow
            # send wrote an untracked duplicate row.
            client_order_id=client_order_id or str(uuid.uuid4()),
            structure_id=structure_id,
            mode=mode,
            sleeve_id=sleeve_id,
            ticker=ticker,
            side=side,
            price_cents=price_cents,
            size=size,
            decided_at_us=decided_at_us or now_us(),
            queue_ahead=queue_ahead,
            book_bid=book_bid,
            book_ask=book_ask,
            rationale=rationale,
        )


@dataclass(frozen=True, slots=True)
class ShadowFill:
    client_order_id: str
    ticker: str
    filled_size: float
    price_cents: int
    first_fill_at_us: int | None
    model: FillModel
    volume_through: float
    queue_ahead: float

    @property
    def filled(self) -> bool:
        return self.filled_size > 0


@dataclass
class ShadowExecutor:
    """Records orders.  NEVER touches a venue -- asserted by tests."""

    db: Database
    orders: list[ShadowOrder] = field(default_factory=list)

    def submit(self, order: ShadowOrder) -> None:
        """Record the order.  NEVER touches a venue.

        `INSERT OR REPLACE` was wrong here in two ways.  It DELETEs and re-INSERTs,
        so it wiped `structure_id` and any state the OMS had already advanced --
        a filled order could be quietly reset to 'open' by a replayed submit, and
        the row's foreign keys went with it.  And it hardcoded mode='shadow', so a
        BACKTEST order was mislabelled as shadow in the one table the KPIs read.
        """
        self.orders.append(order)
        with self.db.tx() as c:
            c.execute(
                """INSERT INTO orders
                   (client_order_id, created_at_us, sleeve_id, structure_id, venue,
                    ticker, side, price_cents, size, post_only, mode, venue_order_id,
                    state, rationale_json, updated_at_us)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(client_order_id) DO UPDATE SET
                       state         = CASE WHEN orders.state = 'pending'
                                            THEN 'open' ELSE orders.state END,
                       structure_id  = COALESCE(orders.structure_id, excluded.structure_id),
                       -- The BOOK CONTEXT must survive the conflict path.  The
                       -- executor writes its intent row first (no book context
                       -- yet), then submits here; preserving the existing
                       -- rationale wholesale dropped `queue_ahead` on every
                       -- order routed through the executor, so every shadow
                       -- fill was computed as though nothing was queued ahead
                       -- of us -- silently the most OPTIMISTIC queue possible,
                       -- in the model whose entire job is to be conservative.
                       rationale_json = excluded.rationale_json,
                       updated_at_us = excluded.updated_at_us""",
                (
                    order.client_order_id, order.decided_at_us, order.sleeve_id,
                    order.structure_id,
                    "kalshi", order.ticker, order.side.value, order.price_cents,
                    order.size, 1, order.mode.value, None, "open",
                    json.dumps({
                        **order.rationale,
                        "queue_ahead": order.queue_ahead,
                        "book_bid": order.book_bid,
                        "book_ask": order.book_ask,
                    }),
                    order.decided_at_us,
                ),
            )


def counterfactual_fill(
    db: Database,
    order: ShadowOrder,
    *,
    model: FillModel = FillModel.PESSIMISTIC,
    horizon_us: int | None = None,
) -> ShadowFill:
    """Would this resting order have filled, given what the tape actually did?

    A resting BUY YES at price p is filled by someone SELLING YES at <= p.  On
    Kalshi's tape that is a trade whose `taker_side` is 'no' (the taker bought NO,
    i.e. sold YES) at `yes_price_cents <= p`.

    Symmetrically, a resting BUY NO at YES-price p (i.e. we bid 100-p for NO) is
    filled by a taker who bought YES at `yes_price_cents >= p`.
    """
    t0 = order.decided_at_us
    t1 = (t0 + horizon_us) if horizon_us else None

    # PESSIMISTIC alone uses a STRICT inequality -- it counts only volume that
    # traded through our price and refuses to credit anything that printed at
    # it.  REALISTIC and OPTIMISTIC are inclusive; they differ from each other
    # in whether the queue ahead of us is charged.
    inclusive = model is not FillModel.PESSIMISTIC
    if order.side is Side.YES:
        # we rest a YES bid; a taker selling YES lifts it
        taker = "no"
        price_clause = ("yes_price_cents <= ?" if inclusive
                        else "yes_price_cents < ?")
    else:
        # we rest a YES ask; a taker buying YES lifts it
        taker = "yes"
        price_clause = ("yes_price_cents >= ?" if inclusive
                        else "yes_price_cents > ?")

    params: list = [order.ticker, t0, order.price_cents, taker]
    time_clause = "AND traded_at_us <= ?" if t1 else ""
    if t1:
        params.append(t1)

    rows = db.conn.execute(
        f"""SELECT traded_at_us, size, yes_price_cents FROM trades
            WHERE ticker = ? AND traded_at_us > ? AND {price_clause}
              AND taker_side = ? {time_clause}
            ORDER BY traded_at_us""",
        params,
    ).fetchall()

    through = sum((r["size"] or 0.0) for r in rows)

    if model is FillModel.OPTIMISTIC:
        # touch fill: any qualifying print fills us, queue ignored
        filled = min(float(order.size), through) if through > 0 else 0.0
        first = rows[0]["traded_at_us"] if rows else None
        return ShadowFill(order.client_order_id, order.ticker, filled,
                          order.price_cents, first, model, through, order.queue_ahead)

    # PESSIMISTIC and REALISTIC both sit behind everything displayed at our
    # price; they differ only in which prints were counted above.
    remaining = order.queue_ahead
    filled = 0.0
    first: int | None = None
    for r in rows:
        vol = r["size"] or 0.0
        if remaining > 0:
            consumed = min(remaining, vol)
            remaining -= consumed
            vol -= consumed
        if vol > 0 and filled < order.size:
            take = min(vol, order.size - filled)
            if first is None:
                first = r["traded_at_us"]
            filled += take
    return ShadowFill(order.client_order_id, order.ticker, filled, order.price_cents,
                      first, model, through, order.queue_ahead)


def fill_rate(db: Database, orders: list[ShadowOrder], *,
              model: FillModel = FillModel.PESSIMISTIC,
              horizon_us: int | None = None) -> dict[str, float]:
    """Aggregate fill statistics -- the Gate 3 acceptance number."""
    if not orders:
        return {"orders": 0, "filled": 0, "fill_rate": 0.0, "avg_fill_fraction": 0.0}
    fills = [counterfactual_fill(db, o, model=model, horizon_us=horizon_us)
             for o in orders]
    n_filled = sum(1 for f in fills if f.filled)
    frac = sum(f.filled_size / o.size for f, o in zip(fills, orders)) / len(orders)
    return {
        "orders": len(orders),
        "filled": n_filled,
        "fill_rate": n_filled / len(orders),
        "avg_fill_fraction": frac,
    }


# A reference quote may be at most this much LATER than the horizon it is
# standing in for.  Without a bound, "the first snapshot at or after t+h" is
# whatever exists -- and the universe sweep records roughly one observation per
# market, so every horizon from 1s to 30m resolves to the SAME row and the decay
# curve KPI 3 exists to measure collapses into a single repeated number.
# Half the horizon keeps the reference nearer to its own horizon than to the
# next one up, which is the weakest bound that still separates the five.
def _staleness_budget(horizon_us: int) -> int:
    return max(horizon_us // 2, 1_000_000)


def markout(db: Database, ticker: str, at_us: int, price_cents: int,
            side: Side, *, horizon_us: int, venue: str = "kalshi",
            max_staleness_us: int | None = None) -> float | None:
    """Signed mark-out in cents at `horizon_us` after a (hypothetical) fill.

    The maker's per-contract P&L at horizon h.  Healthy is positive at 1s decaying
    to a positive plateau; positive at 1s but negative at settlement means you are
    capturing spread on the wrong side of the favourite-longshot bias.

    Returns None when no quote exists close enough to the horizon to stand in for
    it.  Returning None is the honest answer -- a mark-out computed against a
    quote hours past its horizon measures the passage of time, not the trade.
    """
    budget = (_staleness_budget(horizon_us) if max_staleness_us is None
              else max_staleness_us)
    target = at_us + horizon_us
    row = db.conn.execute(
        """SELECT yes_bid, yes_ask FROM market_snapshots
           WHERE venue = ? AND ticker = ?
             AND observed_at_us >= ? AND observed_at_us <= ?
           ORDER BY observed_at_us LIMIT 1""",
        (venue, ticker, target, target + budget),
    ).fetchone()
    if row is None or row["yes_bid"] is None or row["yes_ask"] is None:
        return None
    fair = (row["yes_bid"] + row["yes_ask"]) / 2.0
    return (fair - price_cents) if side is Side.YES else (price_cents - fair)
