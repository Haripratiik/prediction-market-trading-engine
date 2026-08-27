"""T-052 acceptance -- multi-leg structure lifecycle.  PLAN.md 3.2, 5, 10.3, 12.

Every property here costs money when it fails, and the amounts are not close to
each other:

  * a structure that reaches `complete` on a fill that later unwinds is a book
    that believes it is hedged while it is naked at full size
  * an orphan detected LATE is a directional position nobody sized, carried past
    the timeout that exists to stop exactly that
  * an orphan detected EARLY pays a spread to flatten a leg that was about to be
    hedged for free
  * a NO leg costed at p instead of 100 - p under-counts collateral by up to 20x
    on precisely the legs S2 rests (risk/engine.py::per_contract_cost_cents)
  * an orphan whose row stops saying `orphaned` disappears from KPI 6's
    numerator, and the one statistic that measures this failure mode goes quiet
    at the exact moment it has something to report

The last test in the file is the one that matters most: `monitor.kpi` is already
written and tested against a column contract, and this module is the only writer
of that table.  If the two disagree, KPI 6 reports confident zeros forever.

Nothing here reaches a venue and nothing uses pytest's `tmp_path` -- its base
directory raises WinError 5 on this machine, so the one test that needs a real
file on disk builds its own directory with `mkdtemp` and removes it.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.config import StructureLimits
from core.db import Database
from core.math.contracts import FeeSpec, fee
from core.models import (
    Fill,
    Market,
    OrderRequest,
    OrderState,
    RunMode,
    Side,
    Venue,
)
from execution.oms import OMS, new_client_order_id
from execution.structures import (
    DEFAULT_FEE_SPEC,
    CompletionAction,
    Leg,
    LegStatus,
    StructureIntent,
    StructureKind,
    StructureState,
    StructureStore,
    UnwindAction,
    closing_side,
    leg_cost_cents,
    leg_fee_cents,
)
from monitor.kpi import orphan_loss_ratio
from risk.engine import per_contract_cost_cents
from strategy.base import DesiredQuote

# Time is a constant, never a clock read: every deadline assertion in this file
# is about an exact microsecond, and a real clock would make them approximate.
T0 = 1_800_000_000_000_000
SEC = 1_000_000
TIMEOUT_US = 900 * SEC              # config/risk.yaml structures.leg_timeout_seconds

SID = "S3:KXA-T60|KXA-T55@60/55"
SELL = "KXA-T60"                    # the rich leg: we sell YES at 60
BUY = "KXA-T55"                     # the cheap leg: we buy YES at 55
BANKROLL = 1_000_000                # $10,000 in cents

# 130 of 13,486 Kalshi series charge makers anything at all.  This is that
# minority, used wherever an entry fee must be non-zero for the arithmetic to be
# interesting; the exit side of an unwind pays the taker rate on any series.
MAKER_FEE_SPEC = FeeSpec.kalshi("quadratic_with_maker_fees", 1.0)


# --------------------------------------------------------------------------- #
# Fixtures and builders
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db():
    with Database(":memory:") as d:
        yield d


@pytest.fixture()
def oms(db):
    return OMS(db)


@pytest.fixture()
def store(db):
    return StructureStore(db)


@pytest.fixture()
def db_dir():
    """A scratch directory for the one test that needs a database file.

    Deliberately NOT pytest's `tmp_path`: its base under
    C:\\Users\\harie\\AppData\\Local\\Temp\\pytest-of-harie raises
    PermissionError (WinError 5) on this machine, and a persistence test that
    cannot create a file proves nothing about persistence.
    """
    path = tempfile.mkdtemp(prefix="pm-structures-")
    try:
        yield Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


def mk_market(ticker: str, *, bid: int | None = None, ask: int | None = None,
              size: float = 500.0) -> Market:
    return Market(ticker=ticker, yes_bid=bid, yes_ask=ask,
                  yes_bid_size=size, yes_ask_size=size)


def order(structure_id: str | None, ticker: str, side: Side, price: int, size: int,
          *, sleeve: str = "S3", coid: str | None = None) -> OrderRequest:
    return OrderRequest(
        client_order_id=coid or new_client_order_id(),
        sleeve_id=sleeve,
        venue=Venue.KALSHI,
        ticker=ticker,
        side=side,
        price_cents=price,
        size=size,
        mode=RunMode.SHADOW,
        structure_id=structure_id,
        rationale={"why": "structure leg", "structure_id": structure_id},
    )


def fill_order(oms: OMS, coid: str, *, price: int, size: int, fee_cents: int = 0,
               terminal: bool = True, is_maker: bool = True, at_us: int = T0) -> None:
    """Record a fill at a YES price, stored the way `fillfeed` stores it.

    `price` is YES-referenced, as every price in these tests is.  What lands in
    `fills.price_cents` is SIDE-referenced -- 100 - p on a NO leg -- because
    that is what `execution.fillfeed._stored_price_cents` writes and what
    `execution.oms.OMS.position` reads back.  Writing the YES price here would
    make the fixtures agree with the module and disagree with the database.
    """
    side = oms.get(coid).side
    oms.record_fill(Fill(
        client_order_id=coid,
        venue_fill_id=f"F-{uuid.uuid4()}",
        filled_at_us=at_us,
        price_cents=price if side is Side.YES else 100 - price,
        size=size,
        fee_cents=fee_cents,
        is_maker=is_maker,
        terminal=terminal,
    ))


def rv_intent(*, sell_px: int = 60, buy_px: int = 55, size: int = 100,
              deadline_us: int | None = T0 + TIMEOUT_US,
              target_cents: float | None = None,
              designed_fee_cents: float | None = None,
              sleeve: str = "S3", sid: str = SID) -> StructureIntent:
    """The canonical PLAN.md 3.3 pair: sell YES at 60, buy YES at 55.

    The margin is COMPUTED from `core.math.contracts.fee` at the two leg prices
    rather than asserted, so the target the KPI divides by is the same number the
    sleeve would have quoted.
    """
    entry_fee = size * (leg_fee_cents(sell_px, MAKER_FEE_SPEC, is_maker=True)
                        + leg_fee_cents(buy_px, MAKER_FEE_SPEC, is_maker=True))
    gross = float(sell_px - buy_px) * size
    return StructureIntent(
        structure_id=sid,
        sleeve_id=sleeve,
        kind=StructureKind.LINKED_RV,
        legs=(
            # PLAN.md 3.3: the SELL leg is Side.NO at the YES price we sell at,
            # NOT at 100 - p.  It is a resting YES ask.
            Leg(SELL, Side.NO, size, sell_px),
            Leg(BUY, Side.YES, size, buy_px),
        ),
        target_margin_cents=(gross - entry_fee) if target_cents is None
        else target_cents,
        designed_fee_cents=entry_fee if designed_fee_cents is None
        else designed_fee_cents,
        unwind_deadline_us=deadline_us,
        rationale={"link_type": "L2", "link_id": "KXA-T60->KXA-T55"},
    )


def open_rv(store: StructureStore, intent: StructureIntent | None = None,
            **kw: Any) -> tuple[StructureIntent, dict[str, str]]:
    """Open a two-leg structure and its intents atomically.  Returns the coids."""
    intent = intent or rv_intent(**kw)
    reqs = [
        order(intent.structure_id, leg.ticker, leg.side, leg.price_cents,
              leg.target_size, sleeve=intent.sleeve_id)
        for leg in intent.legs
    ]
    store.open_with_intents(intent, reqs, at_us=T0)
    return intent, {r.ticker: r.client_order_id for r in reqs}


# =========================================================================== #
# Birth -- a structure and its legs are born together
# =========================================================================== #
def test_a_structure_and_its_leg_intents_are_written_in_one_transaction(store, db):
    """An order at a venue whose structure no row knows about has no lifecycle.

    Nothing would arm its deadline, nothing would detect its orphan, nothing
    would unwind it.  That is a naked position with no owner, so the structure
    row and every leg intent must land together or not at all.
    """
    intent, coids = open_rv(store)
    assert store.get(intent.structure_id) is not None
    rows = db.conn.execute(
        "SELECT COUNT(*) n FROM orders WHERE structure_id = ?", (SID,)
    ).fetchone()["n"]
    assert rows == 2
    assert set(coids) == {SELL, BUY}


def test_a_failure_mid_batch_leaves_neither_the_structure_nor_a_single_leg(store, db):
    """The other half of atomicity, which is the half that actually bites.

    A partial write here is the worst outcome available: one leg recorded and
    sendable, its partner missing, and a structure row asserting a hedge that was
    never placed.
    """
    intent = rv_intent()
    good = order(SID, SELL, Side.NO, 60, 100)

    class Exploding:
        """An OrderRequest whose serialization blows up, as a dropped link would."""

        client_order_id = "boom"
        structure_id = SID

        @property
        def rationale(self) -> dict[str, Any]:
            raise RuntimeError("serialization failed mid-batch")

        def __getattr__(self, name: str) -> Any:
            return getattr(good, name)

    with pytest.raises(RuntimeError):
        store.open_with_intents(intent, [good, Exploding()], at_us=T0)

    assert store.get(SID) is None
    assert db.conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 0
    assert db.conn.execute("SELECT COUNT(*) n FROM structures").fetchone()["n"] == 0


def test_an_order_that_does_not_carry_the_structure_id_is_refused(store):
    """A leg that loses its structure id is a naked bet in arbitrage clothing."""
    intent = rv_intent()
    stray = order(None, BUY, Side.YES, 55, 100)
    with pytest.raises(ValueError, match="structure_id"):
        store.open_with_intents(intent, [stray], at_us=T0)


def test_the_structure_row_carries_every_leg_so_the_design_can_be_rebuilt(store):
    """`legs_json` is the design of record: ticker, side, size and YES price."""
    intent, _ = open_rv(store)
    rec = store.get(intent.structure_id)
    assert rec.n_legs == 2
    assert rec.legs == intent.legs
    assert rec.state is StructureState.FORMING
    assert rec.legs[0].side is Side.NO and rec.legs[0].price_cents == 60


def test_reopening_the_same_structure_does_not_reset_its_unwind_deadline(store):
    """S2 and S3 mint deterministic, price-bearing ids, so re-emission is normal.

    A deadline refreshed on every cycle is a deadline that never fires, which
    switches the orphan timeout off for exactly as long as the structure keeps
    failing to fill -- i.e. for the whole time it is dangerous.
    """
    intent, _ = open_rv(store)
    later = rv_intent(deadline_us=T0 + 10 * TIMEOUT_US)
    assert store.open(later, at_us=T0 + SEC) is False
    assert store.get(SID).unwind_deadline_us == intent.unwind_deadline_us


def test_a_structure_written_here_survives_a_reopen_of_the_database(db_dir, store):
    """The DB is the truth (I4): a restart must not lose an armed deadline."""
    path = db_dir / "pm.db"
    with Database(path) as d:
        s = StructureStore(d)
        intent, _ = open_rv(s)
    with Database(path) as d:
        rec = StructureStore(d).get(intent.structure_id)
    assert rec is not None
    assert rec.legs == intent.legs
    assert rec.unwind_deadline_us == intent.unwind_deadline_us
    assert rec.target_margin_cents == pytest.approx(intent.target_margin_cents)


# =========================================================================== #
# Completion -- driven by fills, never by a counter (I4)
# =========================================================================== #
def test_a_fully_filled_structure_reaches_complete(store, oms):
    """Both legs on at target size: the hedge designed for actually exists."""
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)
    assert store.refresh(SID, now=T0 + SEC).state is StructureState.FORMING
    fill_order(oms, coids[BUY], price=55, size=100)

    rec = store.refresh(SID, now=T0 + 2 * SEC)
    assert rec.state is StructureState.COMPLETE
    assert rec.realized_margin_cents is not None


def test_a_partially_filled_leg_does_not_complete_the_structure(store, oms):
    """Half a leg is half a hedge, and the unhedged half is at full size."""
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)
    fill_order(oms, coids[BUY], price=55, size=40)
    assert store.refresh(SID, now=T0 + SEC).state is StructureState.FORMING
    statuses = store.leg_status(SID)
    assert [s.filled_size for s in statuses] == [100, 40]
    assert [s.is_filled for s in statuses] == [True, False]


def test_completion_is_measured_from_fills_and_not_from_the_order_state(store, oms, db):
    """I4: a fill written behind the OMS's back still completes the leg.

    Completion is a query over persisted rows, not a tally of method calls.  If
    it lived in a counter it would drift on every dropped websocket message, and
    the drift would always be in the direction of believing we are hedged.
    """
    intent, coids = open_rv(store)
    for coid, stored in ((coids[SELL], 40), (coids[BUY], 55)):
        # 40, not 60: the SELL leg is a NO order, and `fills.price_cents` holds
        # the price of the side bought (100 - 60).
        with db.tx() as c:
            c.execute(
                """INSERT INTO fills (filled_at_us, client_order_id, venue_fill_id,
                                      price_cents, size, fee_cents, is_maker, terminal)
                   VALUES (?,?,?,?,?,0,1,1)""",
                (T0, coid, f"raw-{coid}", stored, 100),
            )
    # The orders are still `pending`; only the fills say otherwise.
    assert all(o["state"] == OrderState.PENDING.value for o in db.conn.execute(
        "SELECT state FROM orders WHERE structure_id = ?", (SID,)))
    assert store.refresh(SID, now=T0 + SEC).state is StructureState.COMPLETE


def test_a_voided_fill_does_not_count_as_filled(store, oms):
    """A non-terminal fill is a claim, not a fact: Polymarket MATCHED can FAIL.

    Completing a structure on one is how a book records a hedge it does not
    have, and it records it at exactly the moment the counterparty is unwinding.
    """
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)
    fill_order(oms, coids[BUY], price=55, size=100, terminal=False)

    statuses = store.leg_status(SID)
    buy = next(s for s in statuses if s.leg.ticker == BUY)
    assert (buy.filled_size, buy.unconfirmed_size) == (0, 100)
    assert buy.is_filled is False
    assert store.refresh(SID, now=T0 + SEC).state is StructureState.FORMING


def test_a_leg_whose_market_was_voided_does_not_count_as_filled(store, oms, db):
    """A voided market returns the stake, so the contracts are gone.

    Marking the structure complete on a voided leg would report a hedge that no
    longer exists, leaving the surviving leg naked and unwatched -- the orphan
    this module exists for, arrived by a different door.
    """
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)
    fill_order(oms, coids[BUY], price=55, size=100)
    with db.tx() as c:
        c.execute(
            """INSERT INTO settlements (venue, ticker, settled_at_us, outcome, voided)
               VALUES ('kalshi', ?, ?, 0, 1)""", (BUY, T0 + SEC))

    buy = next(s for s in store.leg_status(SID) if s.leg.ticker == BUY)
    assert buy.filled_size == 100          # the fills are still on the record
    assert buy.settlement_voided is True
    assert buy.is_filled is False          # ...but the position is not
    assert store.refresh(SID, now=T0 + 2 * SEC).state is StructureState.FORMING


# =========================================================================== #
# Orphan detection -- the core of the module
# =========================================================================== #
def test_a_half_filled_structure_past_its_deadline_is_detected_as_orphaned(store, oms):
    """One leg on, one leg not, timeout expired: a naked directional position.

    Designed to earn ~4c per contract hedged; what is actually held is 100
    contracts of short YES at 60c, $60 of collateral each and a $1 payoff
    distance.  One cent of adverse move is a quarter of the designed edge.
    """
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)

    rec = store.refresh(SID, now=intent.unwind_deadline_us + 1)
    assert rec.state is StructureState.ORPHANED
    assert rec.rationale["legs_filled"] == 1 and rec.rationale["n_legs"] == 2


def test_a_half_filled_structure_is_not_orphaned_at_or_before_its_deadline(store, oms):
    """Orphaning early pays a spread to flatten a leg about to be hedged free.

    At exactly the deadline the structure still has its full timeout, so the
    comparison is strictly greater-than.  A `>=` here fires one microsecond
    early, and in a maker book that microsecond is the difference between a free
    hedge and a paid exit.
    """
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)
    deadline = intent.unwind_deadline_us

    assert store.refresh(SID, now=deadline - 1).state is StructureState.FORMING
    assert store.refresh(SID, now=deadline).state is StructureState.FORMING
    assert store.refresh(SID, now=deadline + 1).state is StructureState.ORPHANED


def test_a_structure_with_no_leg_filled_is_not_an_orphan(store, oms):
    """Nothing filled is not naked, it is merely unfilled -- and costs nothing.

    Counting it as an orphan would inflate KPI 6's numerator with structures
    that never held a contract, which is how a real orphan problem gets buried
    under noise.
    """
    intent, _ = open_rv(store)
    rec = store.refresh(SID, now=intent.unwind_deadline_us + 1)
    assert rec.state is StructureState.CLOSED
    assert rec.realized_margin_cents == 0.0
    assert rec.rationale["reason"] == "expired_unfilled"


def test_a_structure_whose_last_leg_fills_late_is_not_left_orphaned(store, oms):
    """The hedge arrived after the bell, but it did arrive: there is no loss."""
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)
    assert store.refresh(SID, now=intent.unwind_deadline_us + 1).state \
        is StructureState.ORPHANED

    fill_order(oms, coids[BUY], price=55, size=100)
    assert store.refresh(SID, now=intent.unwind_deadline_us + 2).state \
        is StructureState.COMPLETE


def test_once_unwound_an_orphan_stays_orphaned_so_kpi_6_can_still_see_it(store, oms):
    """`orphan_loss_ratio` counts rows whose state is exactly 'orphaned'.

    Moving one to `closed` on the way out deletes it from the numerator, and the
    only statistic that measures the most expensive failure mode of an RV book
    starts reporting a confident zero.  The end of its life goes in
    `closed_at_us`; the state is a permanent fact about what happened.
    """
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)
    store.refresh(SID, now=intent.unwind_deadline_us + 1)

    books = {SELL: mk_market(SELL, bid=61, ask=62)}
    plan = store.unwind_plan(SID, books=books, bankroll_cents=BANKROLL)
    rec = store.record_unwind(plan, at_us=intent.unwind_deadline_us + 2)

    assert rec.state is StructureState.ORPHANED
    assert rec.closed_at_us == intent.unwind_deadline_us + 2
    assert rec.realized_margin_cents == pytest.approx(plan.realized_margin_cents)
    assert store.close(SID, reason="settled").state is StructureState.ORPHANED


def test_a_fill_arriving_after_an_unwind_does_not_revive_the_structure(store, oms):
    """Once the position has been flattened, a late fill is a NEW position.

    Folding it back in as a "late completion" would book the designed margin a
    second time on a structure whose legs are already closed, and would hide a
    fresh naked leg inside a row that says it is hedged.
    """
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)
    store.refresh(SID, now=intent.unwind_deadline_us + 1)
    plan = store.unwind_plan(SID, books={SELL: mk_market(SELL, bid=61, ask=62)},
                             bankroll_cents=BANKROLL)
    store.record_unwind(plan, at_us=intent.unwind_deadline_us + 2)

    fill_order(oms, coids[BUY], price=55, size=100)
    rec = store.refresh(SID, now=intent.unwind_deadline_us + 3)
    assert rec.state is StructureState.ORPHANED
    assert rec.realized_margin_cents == pytest.approx(plan.realized_margin_cents)


def test_the_sweep_reports_only_the_structures_whose_state_changed(store, oms):
    """A cycle that re-reports every structure teaches an operator to ignore it."""
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)
    changed = store.sweep(now=intent.unwind_deadline_us + 1)
    assert [r.structure_id for r in changed] == [SID]
    assert store.sweep(now=intent.unwind_deadline_us + 2) == []


# =========================================================================== #
# The complement rule -- the bug that has bitten this codebase twice
# =========================================================================== #
@pytest.mark.parametrize("price", range(1, 100))
def test_a_no_leg_is_costed_at_the_complement_of_its_yes_price(price):
    """`price_cents` is YES-referenced, so a NO leg at p locks 100 - p.

    This module's float form must agree with `risk.engine.per_contract_cost_cents`
    at every integer price, because that function is the ONE place the rule
    lives and a second, disagreeing copy is how it went wrong the first time.
    """
    assert leg_cost_cents(Side.NO, price) == per_contract_cost_cents(Side.NO, price)
    assert leg_cost_cents(Side.YES, price) == per_contract_cost_cents(Side.YES, price)
    assert leg_cost_cents(Side.NO, price) == 100 - price
    assert leg_cost_cents(Side.YES, price) == price


def test_the_entry_basis_of_a_no_leg_agrees_with_the_oms_position(store, oms):
    """`orders.price_cents` is YES-referenced; `fills.price_cents` is NOT.

    `execution.fillfeed` writes a NO fill at 100 - p and `OMS.position` reads it
    back the same way.  This module must land on the same YES price from the same
    rows -- if it did not, an orphan's entry basis would be out by (100 - 2p),
    which on the 5c longshot legs S2 rests is 90c of a 100c contract.
    """
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)      # NO leg, stored at 40

    stored = store.db.conn.execute(
        "SELECT price_cents FROM fills WHERE client_order_id = ?", (coids[SELL],)
    ).fetchone()["price_cents"]
    assert stored == 40

    leg = next(s for s in store.leg_status(SID) if s.leg.ticker == SELL)
    position = oms.position(SELL)
    assert leg.avg_price_cents == pytest.approx(60.0)
    assert leg.avg_price_cents == pytest.approx(position.avg_price_cents)
    assert leg.signed_size == position.net_contracts == -100
    assert leg.exposure_cents == pytest.approx(40.0 * 100)   # short YES at 60c


def test_a_short_basket_leg_at_five_cents_exposes_ninety_five_not_five(store, oms):
    """S2 rests NO legs at LOW yes-prices -- the exact shape of the 20x error.

    A 200-contract leg resting at a YES price of 5c collects $10 and locks $190.
    Costing it at 5c reports $10 of naked exposure where there is $190, and that
    single number feeds the orphan budget, the position cap and the cash reserve
    at once.
    """
    intent = StructureIntent(
        structure_id="S2-short-KXEV-abcd1234",
        sleeve_id="S2",
        kind=StructureKind.SHORT_BASKET,
        legs=(Leg("KXEV-A", Side.NO, 200, 5), Leg("KXEV-B", Side.NO, 200, 8)),
        target_margin_cents=300.0,
        unwind_deadline_us=T0 + TIMEOUT_US,
        rationale={"direction": "short", "event_ticker": "KXEV"},
    )
    _, coids = open_rv(store, intent)
    fill_order(oms, coids["KXEV-A"], price=5, size=200)

    leg = next(s for s in store.leg_status(intent.structure_id)
               if s.leg.ticker == "KXEV-A")
    assert leg.exposure_cents == pytest.approx(95.0 * 200)

    plan = store.unwind_plan(
        intent.structure_id,
        books={"KXEV-A": mk_market("KXEV-A", bid=4, ask=6)},
        bankroll_cents=BANKROLL,
    )
    assert plan.naked_exposure_cents == pytest.approx(19_000.0)
    # 0.5% of a $10,000 bankroll is 5,000c; $190 of exposure is nearly 4x that.
    assert plan.exceeds_orphan_budget is True
    assert plan.action is UnwindAction.UNWIND_TAKER


# =========================================================================== #
# The unwind plan -- a decision returned as data, never an order
# =========================================================================== #
def test_the_unwind_plan_closes_exactly_the_filled_legs(store, oms):
    """A leg that never filled has nothing to close; its orders are cancelled.

    Sending a closing order for an unfilled leg opens a NEW naked position in the
    other direction -- the orphan repaired by creating its mirror image.
    """
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)
    store.refresh(SID, now=intent.unwind_deadline_us + 1)

    books = {SELL: mk_market(SELL, bid=61, ask=62),
             BUY: mk_market(BUY, bid=54, ask=56)}
    plan = store.unwind_plan(SID, books=books, bankroll_cents=BANKROLL)

    assert [leg.ticker for leg in plan.legs] == [SELL]
    assert plan.legs[0].size == 100
    # The unfilled leg is still resting and must be pulled; the filled leg's
    # order is already terminal, so there is nothing left there to cancel.
    assert set(plan.cancel_order_ids) == {coids[BUY]}


BOOKS = {SELL: mk_market(SELL, bid=61, ask=62),
         BUY: mk_market(BUY, bid=54, ask=56)}


def test_the_unwind_plan_reverses_each_leg_at_a_yes_referenced_price(store, oms):
    """Closing a short YES means buying YES back: Side.YES at the YES price.

    Closing a long YES is the mirror -- Side.NO at the YES price being asked,
    which in this system is a resting YES ask.  Getting the side wrong doubles
    the position it was meant to close instead of flattening it.
    """
    short_naked = rv_intent(sid="S3:short-naked@60/55")
    _, a = open_rv(store, short_naked)
    fill_order(oms, a[SELL], price=60, size=100)          # short YES, 100 naked
    fill_order(oms, a[BUY], price=55, size=30)            # long YES, 30 matched
    store.refresh(short_naked.structure_id, now=short_naked.unwind_deadline_us + 1)
    plan = store.unwind_plan(short_naked.structure_id, books=BOOKS,
                             bankroll_cents=BANKROLL)
    assert [(leg.ticker, leg.side, leg.size) for leg in plan.legs] \
        == [(SELL, Side.YES, 70)]

    long_naked = rv_intent(sid="S3:long-naked@60/55")
    _, b = open_rv(store, long_naked)
    fill_order(oms, b[SELL], price=60, size=30)
    fill_order(oms, b[BUY], price=55, size=100)           # long YES, 70 naked
    store.refresh(long_naked.structure_id, now=long_naked.unwind_deadline_us + 1)
    plan = store.unwind_plan(long_naked.structure_id, books=BOOKS,
                             bankroll_cents=BANKROLL)
    assert [(leg.ticker, leg.side, leg.size) for leg in plan.legs] \
        == [(BUY, Side.NO, 70)]


def test_the_unwind_keeps_the_baskets_that_actually_matched(store, oms):
    """Only the residual no other leg offsets is unwound.  PLAN.md 3.2 step 4.

    Legs filled 100 and 30 are a hedged 30-basket structure plus 70 contracts of
    naked short.  Flattening all 100 would cross two spreads and pay two taker
    fees to give up 30 baskets of locked, positive margin -- paying to destroy
    the part of the trade that worked.
    """
    intent = rv_intent(size=100)
    _, coids = open_rv(store, intent)
    fill_order(oms, coids[SELL], price=60, size=100)
    fill_order(oms, coids[BUY], price=55, size=30)
    store.refresh(SID, now=intent.unwind_deadline_us + 1)

    plan = store.unwind_plan(SID, books=BOOKS, bankroll_cents=BANKROLL)
    assert plan.matched_baskets == 30
    assert plan.legs[0].size == 70
    assert plan.surviving_margin_cents == pytest.approx(
        0.30 * intent.target_margin_cents)
    # 70 contracts of short YES at 60c lock 40c each, not 100 contracts' worth.
    assert plan.naked_exposure_cents == pytest.approx(70 * 40.0)


def test_equal_partial_fills_on_both_legs_are_hedged_and_not_an_orphan(store, oms):
    """A smaller structure is still a structure, and it is still earning.

    Both legs filled 40 of 100 is exactly the trade that was designed, at 40%
    of the size.  There is no naked contract anywhere in it, so there is nothing
    to unwind -- and unwinding it anyway would pay two spreads to convert a
    locked positive margin into a certain loss.
    """
    intent = rv_intent(size=100)
    _, coids = open_rv(store, intent)
    fill_order(oms, coids[SELL], price=60, size=40,
               fee_cents=round(40 * leg_fee_cents(60, MAKER_FEE_SPEC, is_maker=True)))
    fill_order(oms, coids[BUY], price=55, size=40,
               fee_cents=round(40 * leg_fee_cents(55, MAKER_FEE_SPEC, is_maker=True)))

    rec = store.refresh(SID, now=intent.unwind_deadline_us + 1)
    assert rec.state is StructureState.CLOSED
    assert rec.rationale["reason"] == "expired_balanced"
    assert rec.rationale["matched_baskets"] == 40
    # 40% of the baskets, filled at the quoted prices and charged the modeled
    # maker fee, earns 40% of the designed margin (to the cent of fee rounding).
    assert rec.realized_margin_cents == pytest.approx(
        0.40 * intent.target_margin_cents, abs=1.0)
    # And KPI 6 sees no orphan loss, because none was incurred.
    assert orphan_loss_ratio(store.db, "S3").orphaned == 0


def test_an_orphan_inside_its_exposure_budget_posts_and_does_not_cross(store, oms):
    """Maker while it is small: crossing donates a half-spread on every contract.

    PLAN.md 3.2 step 4 -- unwind at maker prices if possible, taker only when the
    residual exposure exceeds `max_orphan_exposure_fraction`.  Slower is
    acceptable only while the position is small enough for slower to be safe.
    """
    intent = rv_intent(size=10)
    _, coids = open_rv(store, intent)
    fill_order(oms, coids[SELL], price=60, size=10)
    store.refresh(SID, now=intent.unwind_deadline_us + 1)

    books = {SELL: mk_market(SELL, bid=61, ask=62)}
    plan = store.unwind_plan(SID, books=books, bankroll_cents=BANKROLL,
                             limits=StructureLimits())
    # 10 contracts of short YES at 60c lock 400c; the budget is 0.5% of $10,000.
    assert plan.naked_exposure_cents == pytest.approx(400.0)
    assert plan.orphan_budget_cents == pytest.approx(5000.0)
    assert plan.exceeds_orphan_budget is False
    assert plan.action is UnwindAction.UNWIND_MAKER
    # Buying the short back as a maker posts on the bid, not through the ask.
    assert plan.legs[0].price_cents == 61 and plan.legs[0].post_only is True


def test_an_orphan_over_its_exposure_budget_crosses_the_spread(store, oms):
    """PLAN.md 10.3 step 2: cross the spread to flatten.  Do not wait.

    Past the budget the cost of waiting dominates the cost of the spread: the
    position is directional, unsized and already past the deadline that was
    supposed to prevent it.
    """
    intent = rv_intent(size=100)
    _, coids = open_rv(store, intent)
    fill_order(oms, coids[SELL], price=60, size=100)
    store.refresh(SID, now=intent.unwind_deadline_us + 1)

    books = {SELL: mk_market(SELL, bid=61, ask=62)}
    plan = store.unwind_plan(SID, books=books, bankroll_cents=BANKROLL)
    assert plan.naked_exposure_cents == pytest.approx(4000.0)
    assert plan.exceeds_orphan_budget is False       # 4000c < 5000c budget

    tight = store.unwind_plan(SID, books=books, bankroll_cents=500_000)
    assert tight.orphan_budget_cents == pytest.approx(2500.0)
    assert tight.exceeds_orphan_budget is True
    assert tight.action is UnwindAction.UNWIND_TAKER
    # Lifting the ask, not posting on the bid.
    assert tight.legs[0].price_cents == 62 and tight.legs[0].post_only is False


def test_an_unwind_with_no_book_is_blocked_rather_than_priced_at_a_guess(store, oms):
    """An invented exit price flows straight into KPI 6 as an invented loss.

    A leg with no side of the book to exit into is an operations incident
    (PLAN.md 6.6), and saying so is more useful than a number nobody can trust.
    """
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)
    store.refresh(SID, now=intent.unwind_deadline_us + 1)

    plan = store.unwind_plan(SID, books={SELL: mk_market(SELL)},
                             bankroll_cents=BANKROLL)
    assert plan.action is UnwindAction.BLOCKED
    assert plan.blocked_tickers == (SELL,)
    assert plan.is_priced is False
    # ...and nothing was written: an unpriceable orphan keeps a NULL margin
    # rather than a fabricated one.
    assert store.get(SID).realized_margin_cents is None


def test_the_unwind_plan_sends_nothing_and_writes_nothing(store, oms, db):
    """C4.2b: the executor is the only component allowed to talk to a venue.

    A lifecycle manager that sent its own unwinds would also bypass the risk
    engine (I3) and the kill switch (I9), which are enforced on the send path
    and nowhere else.
    """
    intent, coids = open_rv(store)
    fill_order(oms, coids[SELL], price=60, size=100)
    store.refresh(SID, now=intent.unwind_deadline_us + 1)

    def counts() -> tuple[int, int, str]:
        o = db.conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
        f = db.conn.execute("SELECT COUNT(*) n FROM fills").fetchone()["n"]
        s = db.conn.execute(
            "SELECT state FROM structures WHERE structure_id = ?", (SID,)
        ).fetchone()["state"]
        return o, f, s

    before = counts()
    books = {SELL: mk_market(SELL, bid=61, ask=62)}
    for _ in range(3):
        store.unwind_plan(SID, books=books, bankroll_cents=BANKROLL)
        store.completion_plan(SID, books=books)
    assert counts() == before


# =========================================================================== #
# The economics -- computed from real fees and real fill prices
# =========================================================================== #
def test_the_orphan_loss_is_the_designed_margin_minus_the_naked_outcome(store, oms):
    """The whole point of KPI 6, arithmetic and all.

    Designed: sell YES at 60, buy YES at 55, keep 5c per contract less maker
    fees.  Held: 100 contracts of short YES at 60c and nothing else.  Bought
    back at the 62c ask as a taker, that is 2c per contract of price plus the
    entry fee plus a taker fee four times the maker one.  None of those numbers
    is asserted here -- each is computed from `core.math.contracts.fee`.
    """
    entry_fee_cents = 42                              # actual, from the fill row
    intent = rv_intent(size=100)
    _, coids = open_rv(store, intent)
    fill_order(oms, coids[SELL], price=60, size=100, fee_cents=entry_fee_cents)
    store.refresh(SID, now=intent.unwind_deadline_us + 1)

    books = {SELL: mk_market(SELL, bid=61, ask=62)}
    plan = store.unwind_plan(SID, books=books, bankroll_cents=500_000)
    assert plan.action is UnwindAction.UNWIND_TAKER

    exit_fee = 100 * 100.0 * fee(0.62, DEFAULT_FEE_SPEC, is_maker=False)
    expected = -100.0 * (62 - 60) - entry_fee_cents - exit_fee
    assert plan.realized_margin_cents == pytest.approx(expected)
    assert plan.realized_margin_cents < 0.0

    designed = 100.0 * 5.0 - 100.0 * (
        leg_fee_cents(60, MAKER_FEE_SPEC, is_maker=True)
        + leg_fee_cents(55, MAKER_FEE_SPEC, is_maker=True))
    assert plan.target_margin_cents == pytest.approx(designed)
    assert plan.loss_cents == pytest.approx(designed - expected)
    # The hedged design was worth ~4.1c/contract; the naked outcome cost ~4.1c
    # per contract on top of it.  Orphan risk is not a rounding error.
    assert plan.loss_cents > plan.target_margin_cents


def test_an_orphan_that_moved_the_right_way_still_records_the_edge_it_lost(store, oms):
    """A profitable orphan is still a process failure, and is still measured.

    KPI 6 counts money actually lost, so a favourable mark contributes zero to
    the numerator -- but `loss_cents` keeps the forgone edge visible, because a
    book that only notices orphans when they lose is a book that will keep
    making them.
    """
    intent = rv_intent(size=100)
    _, coids = open_rv(store, intent)
    fill_order(oms, coids[SELL], price=60, size=100)
    store.refresh(SID, now=intent.unwind_deadline_us + 1)

    # The market fell after we sold: buying back at 50 is a 10c/contract gain.
    plan = store.unwind_plan(SID, books={SELL: mk_market(SELL, bid=49, ask=50)},
                             bankroll_cents=500_000)
    assert plan.realized_margin_cents > 0.0
    assert plan.loss_cents < 0.0                       # we made money by accident
    assert plan.target_margin_cents > 0.0


def test_a_completed_structure_realizes_the_margin_its_fills_actually_earned(store, oms):
    """Realized versus modeled margin (PLAN.md 12), from the fill rows.

    Settlement pays the same however the legs were filled, so the only things
    that move realized away from designed are the prices actually obtained and
    the fees actually charged.  A price improvement on the sell leg is real
    money and must show up as such.
    """
    intent = rv_intent(size=100)
    _, coids = open_rv(store, intent)
    fill_order(oms, coids[SELL], price=62, size=100, fee_cents=40)   # sold 2c better
    fill_order(oms, coids[BUY], price=55, size=100, fee_cents=43)

    rec = store.refresh(SID, now=T0 + SEC)
    assert rec.state is StructureState.COMPLETE
    expected = intent.target_margin_cents + 200.0 + intent.designed_fee_cents - 83.0
    assert rec.realized_margin_cents == pytest.approx(expected)
    assert rec.realized_margin_cents > intent.target_margin_cents


def test_a_structure_filled_exactly_as_designed_realizes_exactly_its_target(store, oms):
    """No slippage and the modeled fee charged: realized must equal target.

    This is the calibration check on the whole accounting -- if it does not hold,
    every realized-versus-modeled number in the digest carries a constant bias.
    """
    intent = rv_intent(size=100)
    _, coids = open_rv(store, intent)
    sell_fee = round(100 * leg_fee_cents(60, MAKER_FEE_SPEC, is_maker=True))
    buy_fee = round(100 * leg_fee_cents(55, MAKER_FEE_SPEC, is_maker=True))
    fill_order(oms, coids[SELL], price=60, size=100, fee_cents=sell_fee)
    fill_order(oms, coids[BUY], price=55, size=100, fee_cents=buy_fee)

    rec = store.refresh(SID, now=T0 + SEC)
    assert rec.realized_margin_cents == pytest.approx(
        intent.target_margin_cents, abs=1.0)     # within the 1c fee rounding


# =========================================================================== #
# Taker completion -- the branch that PREVENTS the orphan
# =========================================================================== #
def test_a_taker_completion_is_admissible_only_within_the_published_limits(store, oms):
    """S3 publishes `max_taker_buy_cents` so this is arithmetic, not a judgement.

    Completing keeps the designed margin; unwinding pays a spread to give it up.
    But a completion at an unchecked price is just a new directional trade at the
    worst possible moment, so the limit the sleeve certified is the gate.
    """
    intent = replace(
        rv_intent(size=100),
        rationale={"link_type": "L2",
                   "max_taker_buy_cents": 56,      # published by S3 with the quotes
                   "min_taker_sell_cents": 59,
                   "completion_taker_threshold": 0.5},
    )
    _, coids = open_rv(store, intent)
    fill_order(oms, coids[SELL], price=60, size=100)

    cheap = store.completion_plan(SID, books={BUY: mk_market(BUY, bid=54, ask=56)})
    assert cheap.action is CompletionAction.COMPLETE_AS_TAKER
    assert cheap.completion == pytest.approx(0.5)
    assert cheap.legs[0].price_cents == 56 and cheap.legs[0].limit_cents == 56

    dear = store.completion_plan(SID, books={BUY: mk_market(BUY, bid=56, ask=58)})
    assert dear.action is CompletionAction.WAIT
    assert dear.legs[0].within_limit is False


def test_a_structure_with_no_leg_filled_has_nothing_to_complete(store, oms):
    """Crossing to "complete" a structure holding nothing just opens a naked leg."""
    intent, _ = open_rv(store)
    plan = store.completion_plan(SID, books={BUY: mk_market(BUY, bid=54, ask=56)})
    assert plan.action is CompletionAction.NOT_APPLICABLE
    assert plan.completion == 0.0


# =========================================================================== #
# What the sleeves actually emit
# =========================================================================== #
def test_legs_are_bound_by_the_structure_id_field_or_by_the_rationale_key():
    """Both forms are accepted, because both are in the repo right now.

    S2 and S3 set `DesiredQuote.structure_id` AND repeat it in the rationale, and
    the rationale copy is what a replayed order row or an older shadow ledger
    still carries.  A leg that cannot find its partner is the failure this whole
    file is about, so the lookup tries the typed field first and the string key
    second rather than depending on exactly one of them being populated.
    """
    field_form = (
        DesiredQuote(ticker=SELL, side=Side.NO, price_cents=60, size=100,
                     structure_id=SID, rationale={"link_type": "L2"}),
        DesiredQuote(ticker=BUY, side=Side.YES, price_cents=55, size=100,
                     structure_id=SID, rationale={"link_type": "L2"}),
    )
    rationale_form = (
        DesiredQuote(ticker=SELL, side=Side.NO, price_cents=60, size=100,
                     rationale={"structure_id": SID, "link_type": "L2"}),
        DesiredQuote(ticker=BUY, side=Side.YES, price_cents=55, size=100,
                     rationale={"structure_id": SID, "link_type": "L2"}),
    )
    for quotes in (field_form, rationale_form):
        intent = StructureIntent.from_quotes(quotes, sleeve_id="S3", now=T0)
        assert intent.structure_id == SID
        assert intent.kind is StructureKind.LINKED_RV
        assert intent.legs[0].side is Side.NO and intent.legs[0].price_cents == 60


def test_legs_that_disagree_about_their_structure_are_refused():
    """Two half-structures quietly merged is worse than one loud failure."""
    quotes = (
        DesiredQuote(ticker=SELL, side=Side.NO, price_cents=60, size=100,
                     structure_id=SID, rationale={}),
        DesiredQuote(ticker=BUY, side=Side.YES, price_cents=55, size=100,
                     structure_id="S3:other@1/2", rationale={}),
    )
    with pytest.raises(ValueError, match="disagree"):
        StructureIntent.from_quotes(quotes, sleeve_id="S3", now=T0)


def test_the_margin_s2_publishes_in_dollars_and_s3_in_cents_both_land_in_cents():
    """A 100x unit error in KPI 6's denominator makes every ratio meaningless.

    S3 publishes `net_cents` / `fee_cents` in cents; S2 publishes `margin` /
    `fees` in dollars, because `short_basket_margin` works in dollars per
    contract.  The unit is decided by the key name, never by inspecting the
    value -- 0.04 is a plausible cent count and a plausible dollar count.
    """
    s3 = StructureIntent.from_quotes(
        (DesiredQuote(ticker=SELL, side=Side.NO, price_cents=60, size=100,
                      structure_id=SID,
                      rationale={"link_type": "L2", "net_cents": 4.15,
                                 "fee_cents": 0.85,
                                 "unwind_deadline_us": T0 + TIMEOUT_US}),),
        sleeve_id="S3", now=T0)
    assert s3.target_margin_cents == pytest.approx(415.0)
    assert s3.designed_fee_cents == pytest.approx(85.0)
    assert s3.unwind_deadline_us == T0 + TIMEOUT_US

    s2 = StructureIntent.from_quotes(
        (DesiredQuote(ticker="KXEV-A", side=Side.NO, price_cents=5, size=200,
                      structure_id="S2-short-KXEV-abcd1234",
                      rationale={"direction": "short", "margin": 0.015,
                                 "fees": 0.004, "event_ticker": "KXEV",
                                 "leg_timeout_seconds": 900}),),
        sleeve_id="S2", now=T0)
    assert s2.target_margin_cents == pytest.approx(300.0)      # 1.5c * 200
    assert s2.designed_fee_cents == pytest.approx(80.0)
    assert s2.kind is StructureKind.SHORT_BASKET
    # S2 publishes no deadline, only a timeout, so one is armed from it.
    assert s2.unwind_deadline_us == T0 + TIMEOUT_US


# =========================================================================== #
# KPI 6 -- the integration that matters
# =========================================================================== #
def test_orphan_loss_ratio_reads_the_structures_this_module_writes(store, oms):
    """`monitor.kpi.orphan_loss_ratio` was written first and is the contract.

    It sums `-realized_margin_cents` over rows whose `state = 'orphaned'` and
    `realized_margin_cents < 0`, divided by `target_margin_cents` over ALL of the
    sleeve's structures.  If this module writes the wrong state, the wrong sign,
    or the wrong unit, KPI 6 reports a confident zero and the most expensive
    failure mode of the book stays invisible.  Target: ratio < 0.20.
    """
    assert orphan_loss_ratio(store.db, "S3").available is True
    assert orphan_loss_ratio(store.db, "S3").ratio is None      # no data, not 0.0

    # One structure that completed as designed.
    good = rv_intent(sid="S3:good@60/55", size=100)
    _, good_coids = open_rv(store, good)
    fill_order(oms, good_coids[SELL], price=60, size=100, fee_cents=42)
    fill_order(oms, good_coids[BUY], price=55, size=100, fee_cents=43)
    assert store.refresh(good.structure_id, now=T0 + SEC).state \
        is StructureState.COMPLETE

    # One that orphaned: the sell leg filled, the buy leg never did.
    bad = rv_intent(sid="S3:bad@60/55", size=100)
    _, bad_coids = open_rv(store, bad)
    fill_order(oms, bad_coids[SELL], price=60, size=100, fee_cents=42)
    orphaned = store.refresh(
        bad.structure_id, now=bad.unwind_deadline_us + 1,
        books={SELL: mk_market(SELL, bid=61, ask=62)}, bankroll_cents=500_000)
    assert orphaned.state is StructureState.ORPHANED
    assert orphaned.realized_margin_cents < 0.0

    kpi = orphan_loss_ratio(store.db, "S3")
    assert kpi.available is True
    assert (kpi.structures, kpi.orphaned) == (2, 1)
    assert kpi.gross_margin_cents == pytest.approx(
        good.target_margin_cents + bad.target_margin_cents)
    assert kpi.orphan_loss_cents == pytest.approx(-orphaned.realized_margin_cents)
    assert kpi.ratio == pytest.approx(
        kpi.orphan_loss_cents / kpi.gross_margin_cents)

    # Real numbers, not a placeholder: one orphan out of two structures eats far
    # more than the 20% of gross margin PLAN.md 12 sets as the ceiling.
    assert kpi.orphan_loss_cents > 400.0
    assert kpi.ratio > 0.20 and kpi.breaches_target is True

    # A sleeve with no structures is reported as unmeasured, never as healthy.
    assert orphan_loss_ratio(store.db, "S1").ratio is None


def test_kpi_6_does_not_count_a_completed_structure_as_a_loss(store, oms):
    """Only orphans are losses.  A complete structure below its model is edge
    decay, which KPI 2 measures, and mixing the two would make both unreadable.
    """
    intent = rv_intent(size=100)
    _, coids = open_rv(store, intent)
    fill_order(oms, coids[SELL], price=57, size=100, fee_cents=400)   # bad fill
    fill_order(oms, coids[BUY], price=58, size=100, fee_cents=400)
    rec = store.refresh(SID, now=T0 + SEC)

    assert rec.state is StructureState.COMPLETE
    assert rec.realized_margin_cents < 0.0
    kpi = orphan_loss_ratio(store.db, "S3")
    assert (kpi.orphaned, kpi.orphan_loss_cents) == (0, 0.0)
    assert kpi.ratio == pytest.approx(0.0)


# =========================================================================== #
# Small surface checks
# =========================================================================== #
def test_a_leg_status_with_no_fills_prices_off_the_design():
    """An average of nothing is not zero: zero would report a free position."""
    status = LegStatus(leg=Leg(SELL, Side.NO, 100, 60))
    assert status.avg_price_cents == 60.0
    assert status.exposure_cents == 0.0
    assert status.signed_size == 0


def test_the_closing_order_of_a_leg_is_on_the_opposite_side():
    """Two-line rule, and getting it wrong doubles the position it should close."""
    assert closing_side(Side.YES) is Side.NO
    assert closing_side(Side.NO) is Side.YES


def test_a_leg_can_be_rebuilt_from_its_orders_when_the_structure_row_is_lost(store):
    """The recovery path: orders carry the structure id, so the design survives."""
    intent, _ = open_rv(store)
    with store.db.tx() as c:
        c.execute("DELETE FROM structures WHERE structure_id = ?", (SID,))
    legs = store.legs_from_orders(SID)
    assert {leg.key for leg in legs} == {(SELL, "no"), (BUY, "yes")}
    assert store.leg_status(SID) != ()
