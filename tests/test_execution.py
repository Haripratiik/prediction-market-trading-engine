"""T-041 / T-042 / T-043 acceptance.

The properties proven here are the ones whose failure costs money rather than
correctness points:

    I1  every order is post-only unless the sleeve spec granted taker permission
    I3  the risk engine runs BEFORE the send, and a denial actually stops it
    I4  position comes from persisted fills, never from a counter
    I5  a sleeve below Gate 4 cannot place a LIVE order
    I9  a KILL file cancels everything, from any state including mid-placement
    C4.2c every order persists its rationale
    T-041 replaying a client_order_id never double-sends

No test in this file may ever reach a venue.  The only client is `FakeVenue`,
and the shadow-mode test asserts the no-network property at the httpx layer.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.config import RiskConfig
from core.db import Database
from core.models import (
    Fill,
    Market,
    OrderRequest,
    OrderState,
    RunMode,
    Side,
    Venue,
    now_us,
)
from execution.executor import Executor, SendResult, SleeveRef, _to_venue_side
from execution.killswitch import KILL_DEADLINE_S, KillEngaged, KillSwitch
from execution.oms import OMS, DriftReport, new_client_order_id
from risk.engine import Denial, PortfolioState, RiskEngine
from shadow.engine import ShadowExecutor
from strategy.base import DesiredQuote, DesiredState, MarketSnapshot
from venues.kalshi.client import KalshiClient, KalshiError

BANKROLL = 1_000_000        # $10,000 in cents


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #
class FakeVenue:
    """A venue that records instead of trading.  Never opens a socket."""

    def __init__(
        self,
        *,
        create_error: Exception | None = None,
        create_response: dict[str, Any] | None = None,
        on_create: Any = None,
        cancel_all_error: Exception | None = None,
    ) -> None:
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.resting: dict[str, dict[str, Any]] = {}
        self.cancel_all_calls = 0
        self.create_error = create_error
        self.create_response = create_response
        self.on_create = on_create
        self.cancel_all_error = cancel_all_error
        self._n = 0

    def create_order(self, *, ticker: str, side: str, count: int, price_cents: int,
                     client_order_id: str | None = None, post_only: bool = True,
                     **kw: Any) -> dict[str, Any]:
        self.created.append({
            "ticker": ticker, "side": side, "count": count,
            "price_cents": price_cents, "client_order_id": client_order_id,
            "post_only": post_only, **kw,
        })
        if self.on_create is not None:
            self.on_create(self, self.created[-1])
        if self.create_error is not None:
            raise self.create_error
        if self.create_response is not None:
            return self.create_response
        self._n += 1
        oid = f"V-{self._n}"
        self.resting[oid] = {
            "order_id": oid, "client_order_id": client_order_id, "ticker": ticker,
            "side": side, "remaining_count": f"{count:.2f}", "status": "resting",
        }
        return {"order": {"order_id": oid, "status": "resting"}}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        self.cancelled.append(order_id)
        self.resting.pop(order_id, None)
        return {}

    def cancel_all_orders(self) -> int:
        self.cancel_all_calls += 1
        if self.cancel_all_error is not None:
            raise self.cancel_all_error
        n = len(self.resting)
        self.resting.clear()
        return n

    def resting_orders(self, **params: Any) -> list[dict[str, Any]]:
        return list(self.resting.values())


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db():
    with Database(":memory:") as d:
        yield d


@pytest.fixture()
def oms(db):
    return OMS(db)


@pytest.fixture()
def run_dir():
    """The PLAN.md 10.6 "run directory" -- where an operator touches KILL.

    Deliberately not pytest's `tmp_path`: this repo's pytest temp root is not
    readable in every environment it is run in, and a killswitch test that
    cannot create a directory proves nothing about the killswitch.
    """
    d = Path(tempfile.mkdtemp(prefix="pm-run-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def kill(run_dir):
    return KillSwitch(run_dir)


@pytest.fixture()
def risk():
    return RiskEngine(RiskConfig())


def state(**kw: Any) -> PortfolioState:
    kw.setdefault("bankroll_cents", BANKROLL)
    kw.setdefault("peak_bankroll_cents", BANKROLL)
    kw.setdefault("cash_cents", BANKROLL)
    return PortfolioState(**kw)


def quote(ticker: str = "KXA-1", side: Side = Side.YES, price: int = 40,
          size: int = 10, **kw: Any) -> DesiredQuote:
    kw.setdefault("rationale", {"p_model": 0.45, "shrunk_edge": 0.02})
    return DesiredQuote(ticker=ticker, side=side, price_cents=price, size=size, **kw)


def desired(*quotes: DesiredQuote, **kw: Any) -> DesiredState:
    kw.setdefault("rationale", {"sleeve": "s1", "filters_passed": ["depth", "horizon"]})
    return DesiredState(quotes=tuple(quotes), **kw)


def snapshot(*markets: Market) -> MarketSnapshot:
    if not markets:
        markets = (Market(ticker="KXA-1", yes_bid=40, yes_ask=42,
                          yes_bid_size=500.0, yes_ask_size=500.0),)
    return MarketSnapshot(now_us=now_us(), markets=markets, bankroll_cents=BANKROLL)


def request(coid: str | None = None, **kw: Any) -> OrderRequest:
    kw.setdefault("sleeve_id", "S1")
    kw.setdefault("venue", Venue.KALSHI)
    kw.setdefault("ticker", "KXA-1")
    kw.setdefault("side", Side.YES)
    kw.setdefault("price_cents", 40)
    kw.setdefault("size", 10)
    kw.setdefault("mode", RunMode.SHADOW)
    kw.setdefault("rationale", {"why": "test"})
    return OrderRequest(client_order_id=coid or new_client_order_id(), **kw)


def shadow_executor(db, risk, run_dir, **kw: Any) -> Executor:
    kw.setdefault("mode", RunMode.SHADOW)
    return Executor(db=db, risk=risk, run_dir=run_dir, **kw)


def paper_executor(db, risk, run_dir, venue: FakeVenue, **kw: Any) -> Executor:
    kw.setdefault("mode", RunMode.PAPER)
    return Executor(db=db, risk=risk, client=venue, run_dir=run_dir, **kw)


def place_one(ex: Executor, *, gate: int = 5, size: int = 10,
              **qkw: Any) -> tuple[str, DesiredState]:
    ds = desired(quote(size=size, **qkw))
    rep = ex.execute(SleeveRef("S1", gate), ds, state(), snapshot=snapshot())
    assert rep.placed, f"expected a placement, got {rep.as_dict()}"
    return rep.placed[0], ds


# =========================================================================== #
# T-041 -- idempotency
# =========================================================================== #
def test_client_order_id_is_a_uuid4_generated_before_the_send():
    """PLAN.md 0.3: the idempotency key is a UUIDv4 minted BEFORE the send."""
    coid = new_client_order_id()
    assert uuid.UUID(coid).version == 4


def test_replaying_a_client_order_id_is_refused_by_the_oms(oms):
    req = request()
    assert oms.record_intent(req) is True
    assert oms.record_intent(req) is False
    assert oms.replays == (req.client_order_id,)
    n = oms.db.conn.execute(
        "SELECT COUNT(*) n FROM orders WHERE client_order_id = ?",
        (req.client_order_id,),
    ).fetchone()["n"]
    assert n == 1


def test_replaying_a_client_order_id_never_double_sends(db, risk, run_dir):
    """The T-041 guarantee, proven at the layer that actually sends."""
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    req = request(mode=RunMode.PAPER)

    assert ex.submit(req) is SendResult.SENT
    assert ex.submit(req) is SendResult.REPLAY
    assert ex.submit(req) is SendResult.REPLAY

    assert len(venue.created) == 1
    assert ex.oms.get(req.client_order_id).state is OrderState.OPEN


def test_a_replay_does_not_overwrite_the_acked_state(db, risk, run_dir):
    """A replay must be inert -- it may not drag an OPEN order back to pending."""
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    req = request(mode=RunMode.PAPER)
    ex.submit(req)
    before = ex.oms.get(req.client_order_id)
    ex.submit(req)
    after = ex.oms.get(req.client_order_id)
    assert (after.state, after.venue_order_id) == (before.state, before.venue_order_id)


def test_the_intent_row_exists_before_the_network_call(db, risk, run_dir):
    """Ordering, not just presence: crash between the two and reconcile can find it."""
    seen: list[str | None] = []

    def spy(venue: FakeVenue, call: dict[str, Any]) -> None:
        rec = ex.oms.get(call["client_order_id"])
        seen.append(rec.state.value if rec else None)

    venue = FakeVenue(on_create=spy)
    ex = paper_executor(db, risk, run_dir, venue)
    ex.submit(request(mode=RunMode.PAPER))
    assert seen == ["pending"]


def test_record_ack_never_erases_a_venue_order_id(oms):
    """That id is the kill path's only handle on the order."""
    req = request()
    oms.record_intent(req)
    oms.record_ack(req.client_order_id, "V-1", OrderState.OPEN)
    oms.record_ack(req.client_order_id, None, OrderState.PARTIAL)
    assert oms.get(req.client_order_id).venue_order_id == "V-1"


# =========================================================================== #
# I4 -- position is derived from fills, never from a counter
# =========================================================================== #
def _filled(oms: OMS, *, size: int = 100, price: int = 40, side: Side = Side.YES,
            ticker: str = "KXA-1") -> str:
    req = request(ticker=ticker, side=side, price_cents=price, size=size)
    oms.record_intent(req)
    oms.record_ack(req.client_order_id, "V-1", OrderState.OPEN)
    return req.client_order_id


def test_position_is_derived_from_fills_not_counters(oms):
    coid = _filled(oms)
    for i, sz in enumerate((30, 30)):
        oms.record_fill(Fill(client_order_id=coid, venue_fill_id=f"F-{i}",
                             filled_at_us=now_us(), price_cents=40, size=sz,
                             fee_cents=0, is_maker=True, terminal=True))
    assert oms.position("KXA-1").net_contracts == 60

    # A SECOND OMS over the same database sees the same number.  If any of this
    # lived in an instance counter, this assertion would read zero.
    assert OMS(oms.db).position("KXA-1").net_contracts == 60

    # And a fill inserted behind the OMS's back is still counted, because the
    # position is a query over rows rather than a tally of method calls.
    with oms.db.tx() as c:
        c.execute(
            """INSERT INTO fills (filled_at_us, client_order_id, venue_fill_id,
                                  price_cents, size, fee_cents, is_maker, terminal)
               VALUES (?,?,?,?,?,?,1,1)""",
            (now_us(), coid, "F-raw", 40, 10, 0),
        )
    assert oms.position("KXA-1").net_contracts == 70


def test_non_terminal_fills_do_not_move_the_position(oms):
    """core/models.py: a Polymarket MATCHED fill can later FAIL."""
    coid = _filled(oms)
    oms.record_fill(Fill(client_order_id=coid, venue_fill_id="F-1",
                         filled_at_us=now_us(), price_cents=40, size=25,
                         fee_cents=0, is_maker=True, terminal=True))
    oms.record_fill(Fill(client_order_id=coid, venue_fill_id="F-2",
                         filled_at_us=now_us(), price_cents=40, size=40,
                         fee_cents=0, is_maker=True, terminal=False))
    assert oms.position("KXA-1").net_contracts == 25


def test_fills_dedupe_on_venue_fill_id(oms):
    """Websocket reconnect + REST backfill re-delivers the same fill."""
    coid = _filled(oms)
    f = Fill(client_order_id=coid, venue_fill_id="F-dup", filled_at_us=now_us(),
             price_cents=40, size=50, fee_cents=0, is_maker=True, terminal=True)
    assert oms.record_fill(f) is True
    assert oms.record_fill(f) is False
    assert oms.position("KXA-1").net_contracts == 50


def test_a_no_side_fill_is_a_short_yes_position(oms):
    """PLAN.md 0.3: everything is normalised to YES internally."""
    coid = _filled(oms, side=Side.NO, price=30, size=40)
    oms.record_fill(Fill(client_order_id=coid, venue_fill_id="F-no",
                         filled_at_us=now_us(), price_cents=30, size=40,
                         fee_cents=0, is_maker=True, terminal=True))
    pos = oms.position("KXA-1")
    assert pos.net_contracts == -40
    assert pos.side is Side.NO
    assert pos.avg_price_cents == pytest.approx(70.0)      # a NO at 30c is YES at 70c


def test_order_state_follows_the_fills(oms):
    coid = _filled(oms, size=100)
    oms.record_fill(Fill(client_order_id=coid, venue_fill_id="F-1",
                         filled_at_us=now_us(), price_cents=40, size=40,
                         fee_cents=0, is_maker=True, terminal=True))
    assert oms.get(coid).state is OrderState.PARTIAL
    assert oms.get(coid).remaining == 60
    oms.record_fill(Fill(client_order_id=coid, venue_fill_id="F-2",
                         filled_at_us=now_us(), price_cents=40, size=60,
                         fee_cents=0, is_maker=True, terminal=True))
    assert oms.get(coid).state is OrderState.FILLED
    assert oms.open_orders() == []


def test_a_fill_for_an_unknown_order_is_refused(oms):
    with pytest.raises(ValueError, match="unknown order"):
        oms.record_fill(Fill(client_order_id="ghost", venue_fill_id="F-x",
                             filled_at_us=now_us(), price_cents=40, size=1,
                             fee_cents=0, is_maker=True, terminal=True))


def test_a_fill_without_a_dedupe_key_is_refused(oms):
    coid = _filled(oms)
    with pytest.raises(ValueError, match="dedupe key"):
        oms.record_fill(Fill(client_order_id=coid, venue_fill_id="",
                             filled_at_us=now_us(), price_cents=40, size=1,
                             fee_cents=0, is_maker=True, terminal=True))


# =========================================================================== #
# T-041 -- reconciliation
# =========================================================================== #
def test_reconcile_is_clean_when_local_and_venue_agree(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    place_one(ex)
    report = ex.reconcile()
    assert report.is_clean and report.local_open == 1 == report.venue_resting


def test_reconcile_detects_an_order_the_venue_is_not_resting(db, risk, run_dir):
    """It may have filled, or the cancel may have raced us.  Either way: drift."""
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    coid, _ = place_one(ex)
    venue.resting.clear()                       # the venue disagrees with us

    report = ex.reconcile()
    assert not report.is_clean
    assert report.missing_at_venue == (coid,)
    # PLAN.md 6.6: reported, NOT silently repaired -- a human acknowledges drift.
    assert ex.oms.get(coid).state is OrderState.OPEN


def test_reconcile_detects_an_order_we_have_no_record_of(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    venue.resting["V-99"] = {"order_id": "V-99", "client_order_id": "",
                             "ticker": "KXA-9", "remaining_count": "5.00"}
    report = ex.reconcile()
    assert report.unknown_at_venue == ("V-99",) and not report.is_clean


def test_reconcile_adopts_an_order_that_crashed_mid_send(db, risk, run_dir):
    """T-041: the send landed, the ack never arrived, the process died.

    The row is `pending` with no venue_order_id, and the venue is resting it
    under our own client_order_id.  Until reconciliation reunites the two, the
    executor has nothing to cancel it BY -- so this is the recovery path.
    """
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    req = request(mode=RunMode.PAPER)
    ex.oms.record_intent(req)                   # crash happens right here
    venue.resting["V-7"] = {
        "order_id": "V-7", "client_order_id": req.client_order_id,
        "ticker": req.ticker, "remaining_count": f"{req.size:.2f}",
    }

    report = ex.reconcile()
    assert report.adopted == (req.client_order_id,)
    assert not report.is_clean
    rec = ex.oms.get(req.client_order_id)
    assert (rec.state, rec.venue_order_id) == (OrderState.OPEN, "V-7")
    assert rec.rationale["adopted_by_reconcile"] is True

    assert ex.reconcile().is_clean              # and a second pass settles


def test_reconcile_reports_size_drift(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    coid, _ = place_one(ex, size=10)
    only = next(iter(venue.resting.values()))
    only["remaining_count"] = "4.00"            # partially filled behind our back

    report = ex.reconcile()
    assert report.size_drift == ((coid, 10, 4.0),)
    assert not report.is_clean


def test_a_partially_filled_order_does_not_read_as_drift(db, risk, run_dir):
    """The residual keeps its queue position; local `remaining` must track it."""
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    coid, _ = place_one(ex, size=10)
    ex.oms.record_fill(Fill(client_order_id=coid, venue_fill_id="F-1",
                            filled_at_us=now_us(), price_cents=40, size=6,
                            fee_cents=0, is_maker=True, terminal=True))
    next(iter(venue.resting.values()))["remaining_count"] = "4.00"
    assert ex.reconcile().is_clean


# =========================================================================== #
# I5 -- the gate
# =========================================================================== #
def test_a_sleeve_below_gate_4_cannot_place_live_orders(db, risk, run_dir):
    venue = FakeVenue()
    ex = Executor(db=db, risk=risk, mode=RunMode.LIVE, client=venue, run_dir=run_dir)
    report = ex.execute(SleeveRef("S1", 3), desired(quote()), state(),
                        snapshot=snapshot())

    assert report.gate_blocked and not report.placed
    assert report.denied[0][1] == Denial.GATE.value
    assert venue.created == []
    # Nothing was even recorded as an intent -- the refusal precedes the OMS.
    assert ex.oms.counts_by_state() == {}


def test_gate_4_may_trade_live(db, risk, run_dir):
    venue = FakeVenue()
    ex = Executor(db=db, risk=risk, mode=RunMode.LIVE, client=venue, run_dir=run_dir)
    report = ex.execute(SleeveRef("S1", 4), desired(quote()), state(),
                        snapshot=snapshot())
    assert len(report.placed) == 1 and len(venue.created) == 1


def test_an_ungated_sleeve_may_still_run_in_shadow(db, risk, run_dir):
    """G3 exists to be reachable: shadow is how a sleeve earns its gate."""
    ex = shadow_executor(db, risk, run_dir)
    report = ex.execute(SleeveRef("S1", 0), desired(quote()), state(),
                        snapshot=snapshot())
    assert len(report.placed) == 1


# =========================================================================== #
# I3 -- risk runs before the send, and a denial stops it
# =========================================================================== #
def test_a_risk_denial_actually_prevents_the_send(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    # 50c x 1000 = $500 against a 2% cap on a $10,000 bankroll ($200).
    report = ex.execute(SleeveRef("S1", 5), desired(quote(price=50, size=1000)),
                        state(), snapshot=snapshot())

    assert not report.placed
    assert report.denied[0][1] == Denial.POSITION_CAP.value
    assert venue.created == []
    assert ex.oms.counts_by_state() == {}       # no intent row either


def test_risk_denies_one_quote_without_blocking_the_others(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    good = quote(ticker="KXA-1", price=40, size=10)
    bad = quote(ticker="KXA-2", price=50, size=1000)
    other = quote(ticker="KXA-3", price=40, size=10)
    report = ex.execute(SleeveRef("S1", 5), desired(good, bad, other), state(),
                        snapshot=snapshot(
                            Market(ticker="KXA-1", yes_bid=40, yes_ask=42,
                                   yes_bid_size=500.0),
                            Market(ticker="KXA-2", yes_bid=50, yes_ask=52,
                                   yes_bid_size=9e9),
                            Market(ticker="KXA-3", yes_bid=40, yes_ask=42,
                                   yes_bid_size=500.0),
                        ))
    assert len(report.placed) == 2 and len(report.denied) == 1
    assert {c["ticker"] for c in venue.created} == {"KXA-1", "KXA-3"}


def test_the_killed_flag_in_portfolio_state_denies_everything(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    report = ex.execute(SleeveRef("S1", 5), desired(quote()), state(killed=True),
                        snapshot=snapshot())
    assert not report.placed and report.denied[0][1] == Denial.KILLED.value
    assert venue.created == []


# =========================================================================== #
# I1 -- post-only by default
# =========================================================================== #
def test_orders_are_post_only_by_default(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    coid, _ = place_one(ex)
    assert venue.created[0]["post_only"] is True
    assert ex.oms.get(coid).post_only is True


def test_a_sleeve_cannot_grant_itself_taker_permission(db, risk, run_dir):
    """I1: taker permission is a sleeve-SPEC decision made at wiring time."""
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    ex.execute(SleeveRef("S1", 5), desired(quote(post_only=False)), state(),
               snapshot=snapshot())
    assert venue.created[0]["post_only"] is True


def test_taker_permission_granted_at_wiring_time_is_honoured(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue, allow_taker=True)
    ex.execute(SleeveRef("S1", 5), desired(quote(post_only=False)), state(),
               snapshot=snapshot())
    assert venue.created[0]["post_only"] is False


# =========================================================================== #
# T-042 -- diffing
# =========================================================================== #
def test_unchanged_quotes_are_not_resent(db, risk, run_dir):
    """Re-posting an unchanged quote costs 10 tokens and donates queue position."""
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    coid, ds = place_one(ex)

    again = ex.execute(SleeveRef("S1", 5), ds, state(), snapshot=snapshot())
    assert again.placed == () and again.cancelled == ()
    assert again.unchanged == (coid,)
    assert len(venue.created) == 1


def test_removed_quotes_are_cancelled(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    coid, _ = place_one(ex)

    report = ex.execute(SleeveRef("S1", 5), desired(), state(), snapshot=snapshot())
    assert report.cancelled == (coid,)
    assert venue.cancelled == ["V-1"] and venue.resting == {}
    assert ex.oms.get(coid).state is OrderState.CANCELLED


def test_a_repriced_quote_cancels_the_old_level_and_places_the_new(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    old, _ = place_one(ex, price=40)

    report = ex.execute(SleeveRef("S1", 5), desired(quote(price=41)), state(),
                        snapshot=snapshot())
    assert report.cancelled == (old,) and len(report.placed) == 1
    assert venue.created[-1]["price_cents"] == 41


def test_a_size_increase_adds_a_tranche_and_leaves_the_original_resting(
    db, risk, run_dir
):
    """PLAN.md 6.4: never amend up -- the resting tranche keeps its priority."""
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    first, _ = place_one(ex, size=10)

    report = ex.execute(SleeveRef("S1", 5), desired(quote(size=25)), state(),
                        snapshot=snapshot())
    assert report.unchanged == (first,) and len(report.placed) == 1
    assert venue.created[-1]["count"] == 15         # the increment only
    assert ex.oms.get(first).state is OrderState.OPEN


def test_a_size_decrease_sheds_the_newest_tranche_first(db, risk, run_dir):
    """The newest tranche is the one furthest back in the queue."""
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    first, _ = place_one(ex, size=10)
    rep = ex.execute(SleeveRef("S1", 5), desired(quote(size=25)), state(),
                     snapshot=snapshot())
    second = rep.placed[0]

    back = ex.execute(SleeveRef("S1", 5), desired(quote(size=10)), state(),
                      snapshot=snapshot())
    assert back.cancelled == (second,)
    assert ex.oms.get(first).state is OrderState.OPEN


def test_a_partially_filled_residual_is_topped_up_not_reposted(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    coid, ds = place_one(ex, size=10)
    ex.oms.record_fill(Fill(client_order_id=coid, venue_fill_id="F-1",
                            filled_at_us=now_us(), price_cents=40, size=4,
                            fee_cents=0, is_maker=True, terminal=True))

    report = ex.execute(SleeveRef("S1", 5), ds, state(), snapshot=snapshot())
    assert report.cancelled == ()                # the residual is never disturbed
    assert venue.created[-1]["count"] == 4       # only the shortfall is topped up


# =========================================================================== #
# C4.2c -- rationale
# =========================================================================== #
def test_every_order_persists_its_rationale(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    coid, _ = place_one(ex)

    raw = db.conn.execute(
        "SELECT rationale_json FROM orders WHERE client_order_id = ?", (coid,)
    ).fetchone()["rationale_json"]
    stored = json.loads(raw)
    assert stored["p_model"] == 0.45              # the sleeve's own reasoning
    assert stored["filters_passed"] == ["depth", "horizon"]
    assert stored["sleeve_id"] == "S1" and stored["mode"] == RunMode.PAPER.value


def test_an_order_can_never_be_written_without_a_rationale(db, risk, run_dir):
    """Even a sleeve that emits nothing gets the executor's context (C4.2c)."""
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    ds = DesiredState(quotes=(DesiredQuote(ticker="KXA-1", side=Side.YES,
                                           price_cents=40, size=10),))
    report = ex.execute(SleeveRef("S1", 5), ds, state(), snapshot=snapshot())
    assert ex.oms.get(report.placed[0]).rationale["sleeve_gate"] == 5


def test_order_request_itself_refuses_an_empty_rationale():
    with pytest.raises(ValueError, match="rationale is mandatory"):
        request(rationale={})


# =========================================================================== #
# T-042 -- post-only rejections are information
# =========================================================================== #
def _post_only_error() -> KalshiError:
    return KalshiError(400, '{"error":{"code":"post_only_would_cross"}}',
                       "/portfolio/events/orders")


def test_a_post_only_rejection_is_recorded_and_not_retried(db, risk, run_dir):
    """The book moved through us.  Re-sending at the same price chases it down."""
    venue = FakeVenue(create_error=_post_only_error())
    ex = paper_executor(db, risk, run_dir, venue)

    first = ex.execute(SleeveRef("S1", 5), desired(quote(price=40)), state(),
                       snapshot=snapshot())
    assert len(first.post_only_rejected) == 1 and not first.placed
    rec = ex.oms.get(first.post_only_rejected[0])
    assert rec.state is OrderState.REJECTED
    assert rec.rationale["reject_reason"] == "post_only_rejected"

    # Same price again on the next cycle: skipped, not re-sent.
    second = ex.execute(SleeveRef("S1", 5), desired(quote(price=40)), state(),
                        snapshot=snapshot())
    assert second.skipped_crossed and not second.placed
    assert len(venue.created) == 1


def test_a_more_aggressive_price_is_also_skipped_after_a_cross(db, risk, run_dir):
    """If the book crossed us at 40c it crosses us at 41c too."""
    venue = FakeVenue(create_error=_post_only_error())
    ex = paper_executor(db, risk, run_dir, venue)
    ex.execute(SleeveRef("S1", 5), desired(quote(price=40)), state(),
               snapshot=snapshot())
    assert ex.crossed[("KXA-1", "yes")] == 40

    report = ex.execute(SleeveRef("S1", 5), desired(quote(price=41)), state(),
                        snapshot=snapshot())
    assert report.skipped_crossed and len(venue.created) == 1


def test_repricing_away_from_the_cross_is_allowed(db, risk, run_dir):
    """A rejection is information, not a ban: a passive reprice still goes."""
    venue = FakeVenue(create_error=_post_only_error())
    ex = paper_executor(db, risk, run_dir, venue)
    ex.execute(SleeveRef("S1", 5), desired(quote(price=40)), state(),
               snapshot=snapshot())

    venue.create_error = None
    report = ex.execute(SleeveRef("S1", 5), desired(quote(price=38)), state(),
                        snapshot=snapshot())
    assert len(report.placed) == 1 and len(venue.created) == 2


def test_a_definite_4xx_rejection_is_terminal(db, risk, run_dir):
    venue = FakeVenue(create_error=KalshiError(400, "MARKET_INACTIVE", "/orders"))
    ex = paper_executor(db, risk, run_dir, venue)
    report = ex.execute(SleeveRef("S1", 5), desired(quote()), state(),
                        snapshot=snapshot())
    assert len(report.rejected) == 1
    assert ex.oms.get(report.rejected[0]).state is OrderState.REJECTED


def test_an_unknown_send_outcome_stays_pending_for_reconciliation(db, risk, run_dir):
    """A 5xx or a dead socket may still have placed the order.  Assume it did."""
    venue = FakeVenue(create_error=KalshiError(599, "connection reset", "/orders"))
    ex = paper_executor(db, risk, run_dir, venue)
    report = ex.execute(SleeveRef("S1", 5), desired(quote()), state(),
                        snapshot=snapshot())

    assert report.unknown and report.needs_reconcile
    rec = ex.oms.get(report.unknown[0])
    assert rec.state is OrderState.PENDING and rec.rationale["send_outcome"] == "unknown"


# =========================================================================== #
# Venue boundary -- YES referencing
# =========================================================================== #
def test_a_no_quote_is_sent_as_a_yes_ask_at_the_same_price(db, risk, run_dir):
    """`price_cents` is YES-referenced on BOTH sides, so only the verb changes.

    The regression this pins: the boundary used to mirror to `100 - p`, so a
    leg meant to rest as a YES ask at 30c was sent at 70c -- a different order
    on the wrong side of the book.  It would never fill where it was aimed and
    could fill where it was not.
    """
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    ex.execute(SleeveRef("S1", 5), desired(quote(side=Side.NO, price=30)),
               state(), snapshot=snapshot())

    assert venue.created[0]["side"] == "ask"
    assert venue.created[0]["price_cents"] == 30
    # ...and the OMS records the same YES-referenced price the risk engine costed.
    assert ex.oms.open_orders()[0].price_cents == 30


@pytest.mark.parametrize("price", range(1, 100))
def test_the_venue_boundary_changes_the_side_but_never_the_price(price):
    assert _to_venue_side(Side.NO, price) == ("ask", price)
    assert _to_venue_side(Side.YES, price) == ("bid", price)


# =========================================================================== #
# T-044 -- shadow mode never reaches a venue
# =========================================================================== #
def test_shadow_mode_makes_no_network_call(db, risk, run_dir, monkeypatch):
    """Network-level assertion: httpx itself is booby-trapped."""
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("shadow mode attempted a network call")

    monkeypatch.setattr(httpx.Client, "request", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)

    # A REAL client, deliberately handed to a SHADOW executor: if routing leaked,
    # this test would fail rather than quietly send.
    client = KalshiClient(base_url="https://example.invalid/trade-api/v2")
    ex = Executor(db=db, risk=risk, mode=RunMode.SHADOW, client=client,
                  shadow=ShadowExecutor(db), run_dir=run_dir)

    report = ex.execute(SleeveRef("S1", 5), desired(quote()), state(),
                        snapshot=snapshot())
    assert len(report.placed) == 1

    # The kill path must not reach the venue either -- there is nothing of ours
    # resting there to cancel.
    assert ex.panic(reason="test") == 1
    assert ex.oms.get(report.placed[0]).state is OrderState.CANCELLED
    client.close()


def test_a_shadow_order_keeps_the_oms_client_order_id(db, risk, run_dir):
    """ShadowOrder.create mints its own UUID; ours is the idempotency key."""
    ex = shadow_executor(db, risk, run_dir)
    report = ex.execute(SleeveRef("S1", 5), desired(quote()), state(),
                        snapshot=snapshot())
    coid = report.placed[0]
    assert ex.shadow.orders[0].client_order_id == coid
    assert db.conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 1


def test_backtest_orders_are_not_mislabelled_as_shadow(db, risk, run_dir):
    ex = Executor(db=db, risk=risk, mode=RunMode.BACKTEST, run_dir=run_dir)
    report = ex.execute(SleeveRef("S1", 5), desired(quote()), state(),
                        snapshot=snapshot())
    assert ex.oms.get(report.placed[0]).mode is RunMode.BACKTEST


def test_shadow_records_the_book_at_decision_time(db, risk, run_dir):
    """The queue-conservative fill model is only honest if the queue is real."""
    ex = shadow_executor(db, risk, run_dir)
    ex.execute(SleeveRef("S1", 5), desired(quote(price=40)), state(),
               snapshot=snapshot())
    order = ex.shadow.orders[0]
    assert order.queue_ahead == 500.0 and (order.book_bid, order.book_ask) == (40, 42)


def test_a_new_price_level_has_nobody_ahead_of_it(db, risk, run_dir):
    ex = shadow_executor(db, risk, run_dir)
    ex.execute(SleeveRef("S1", 5), desired(quote(price=39)), state(),
               snapshot=snapshot())
    assert ex.shadow.orders[0].queue_ahead == 0.0


def test_live_modes_require_a_venue_client(db, risk, run_dir):
    with pytest.raises(ValueError, match="requires a venue client"):
        Executor(db=db, risk=risk, mode=RunMode.LIVE, run_dir=run_dir)


# =========================================================================== #
# I9 -- the kill switch
# =========================================================================== #
def test_a_kill_file_engages_the_switch(run_dir):
    ks = KillSwitch(run_dir)
    assert not ks.is_engaged()
    (run_dir / "KILL").write_text("")          # exactly what `touch KILL` does
    assert ks.is_engaged()
    with pytest.raises(KillEngaged):
        ks.require_clear()


def test_engage_is_idempotent_and_keeps_the_first_reason(kill):
    kill.engage("first")
    kill.engage("second")
    kill.engage("third")
    assert kill.is_engaged() and kill.reason() == "first"
    assert kill.disengage() is True
    assert kill.disengage() is False             # safe to call repeatedly
    assert not kill.is_engaged()


def test_panic_cancels_everything_and_is_idempotent(kill):
    venue = FakeVenue()
    venue.resting = {f"V-{i}": {"order_id": f"V-{i}"} for i in range(25)}

    assert kill.panic(venue, reason="test") == 25
    assert venue.resting == {}
    assert kill.panic(venue) == 0                # nothing left, no exception
    assert kill.panic(venue) == 0
    assert venue.cancel_all_calls == 3
    assert kill.last_panic.ok


def test_panic_engages_before_it_touches_the_network(run_dir):
    """A kill that depends on a successful network call is not a kill."""
    ks = KillSwitch(run_dir)
    observed: list[bool] = []

    class Watcher:
        def cancel_all_orders(self) -> int:
            observed.append(ks.is_engaged())
            return 0

    ks.panic(Watcher())
    assert observed == [True]


def test_panic_does_not_raise_when_the_venue_does(kill):
    venue = FakeVenue(cancel_all_error=KalshiError(500, "boom", "/orders"))
    assert kill.panic(venue, attempts=2) == 0
    assert kill.is_engaged()                     # new orders are refused regardless
    assert not kill.last_panic.ok and len(kill.last_panic.errors) == 2


def test_panic_completes_within_the_i9_deadline(kill):
    """I9 promises 5 seconds.  Measure it, do not assume it."""
    venue = FakeVenue()
    venue.resting = {f"V-{i}": {"order_id": f"V-{i}"} for i in range(2000)}
    t0 = time.monotonic()
    cancelled = kill.panic(venue)
    elapsed = time.monotonic() - t0
    assert cancelled == 2000
    assert elapsed < KILL_DEADLINE_S
    assert kill.last_panic.within_deadline


def test_panic_all_keeps_going_after_one_venue_fails(kill):
    broken = FakeVenue(cancel_all_error=KalshiError(500, "down", "/orders"))
    healthy = FakeVenue()
    healthy.resting = {"V-1": {"order_id": "V-1"}}
    assert kill.panic_all([broken, healthy]) == 1
    assert healthy.cancel_all_calls >= 1 and not kill.last_panic.ok


def test_armed_context_engages_on_an_unhandled_exception(kill):
    venue = FakeVenue()
    venue.resting = {"V-1": {"order_id": "V-1"}}
    with pytest.raises(RuntimeError, match="strategy blew up"):
        with kill.armed_context(venue):
            raise RuntimeError("strategy blew up")
    assert kill.is_engaged()
    assert "strategy blew up" in (kill.reason() or "")
    assert venue.resting == {}


def test_armed_context_engages_on_a_keyboard_interrupt(kill):
    """Ctrl-C out of a quoting loop leaves resting orders exactly like a crash."""
    with pytest.raises(KeyboardInterrupt):
        with kill.armed_context():
            raise KeyboardInterrupt
    assert kill.is_engaged()


def test_armed_context_leaves_the_switch_clear_on_success(kill):
    with kill.armed_context():
        pass
    assert not kill.is_engaged()


def test_recovery_requires_a_clean_reconciliation(kill):
    """PLAN.md 10.6: removing the file is only HALF of recovery."""
    kill.engage("incident")
    dirty = DriftReport(checked_at_us=now_us(), local_open=2, venue_resting=1,
                        missing_at_venue=("abc",))
    with pytest.raises(KillEngaged):
        kill.recover(dirty)
    assert kill.is_engaged()

    clean = DriftReport(checked_at_us=now_us(), local_open=1, venue_resting=1)
    assert kill.recover(clean) is True
    assert not kill.is_engaged()


def test_the_executor_refuses_a_batch_while_the_switch_is_engaged(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    place_one(ex)
    (run_dir / "KILL").write_text("operator")

    report = ex.execute(SleeveRef("S1", 5), desired(quote(price=41)), state(),
                        snapshot=snapshot())
    assert report.killed and not report.placed
    assert venue.cancel_all_calls == 1 and venue.resting == {}
    assert len(venue.created) == 1               # nothing new went out


def test_the_kill_switch_fires_from_mid_placement(db, risk, run_dir):
    """I9's hardest case: the KILL file appears while the batch is in flight."""
    t0 = time.monotonic()

    def kill_after_first(venue: FakeVenue, call: dict[str, Any]) -> None:
        if len(venue.created) == 1:
            (run_dir / "KILL").write_text("operator, mid-batch")

    venue = FakeVenue(on_create=kill_after_first)
    ex = paper_executor(db, risk, run_dir, venue)
    quotes = [quote(ticker=f"KXA-{i}", price=40, size=5) for i in range(6)]
    report = ex.execute(
        SleeveRef("S1", 5), desired(*quotes), state(),
        snapshot=snapshot(*[Market(ticker=f"KXA-{i}", yes_bid=40, yes_ask=42,
                                   yes_bid_size=500.0) for i in range(6)]),
    )

    assert report.killed
    assert len(venue.created) == 1               # the batch stopped immediately
    assert venue.resting == {}                   # and everything was cancelled
    assert report.cancelled_by_kill >= 1
    assert time.monotonic() - t0 < KILL_DEADLINE_S

    # Every local order is closed out, and a second kill is a no-op.
    assert ex.oms.open_orders() == []
    assert ex.panic(reason="again") == 0


def test_a_send_is_refused_while_the_switch_is_engaged(db, risk, run_dir):
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    ex.kill.engage("test")
    assert ex.submit(request(mode=RunMode.PAPER)) is SendResult.KILLED
    assert venue.created == []
    assert ex.oms.counts_by_state() == {}        # not even an intent row


# =========================================================================== #
# Accounting and reporting surfaces the monitor reads (PLAN.md 6.6 / 12)
# =========================================================================== #
def test_a_crossed_mark_ages_out(db, risk, run_dir):
    """It is information about a book that keeps moving, so it decays."""
    venue = FakeVenue(create_error=_post_only_error())
    ex = paper_executor(db, risk, run_dir, venue, crossed_ttl_us=0)
    ex.execute(SleeveRef("S1", 5), desired(quote(price=40)), state(),
               snapshot=snapshot())

    venue.create_error = None
    report = ex.execute(SleeveRef("S1", 5), desired(quote(price=40)), state(),
                        snapshot=snapshot())
    assert len(report.placed) == 1 and ex.crossed == {}


def test_clear_crossed_is_scoped_to_one_ticker(db, risk, run_dir):
    venue = FakeVenue(create_error=_post_only_error())
    ex = paper_executor(db, risk, run_dir, venue)
    ex.execute(
        SleeveRef("S1", 5),
        desired(quote(ticker="KXA-1"), quote(ticker="KXA-2")), state(),
        snapshot=snapshot(
            Market(ticker="KXA-1", yes_bid=40, yes_ask=42, yes_bid_size=500.0),
            Market(ticker="KXA-2", yes_bid=40, yes_ask=42, yes_bid_size=500.0),
        ),
    )
    assert len(ex.crossed) == 2
    ex.clear_crossed("KXA-1")
    assert list(ex.crossed) == [("KXA-2", "yes")]
    ex.clear_crossed()
    assert ex.crossed == {}


def test_fees_are_signed_so_a_rebate_reads_negative(oms):
    """core/models.py Fill.fee_cents: negative means a rebate was RECEIVED."""
    coid = _filled(oms)
    oms.record_fill(Fill(client_order_id=coid, venue_fill_id="F-r",
                         filled_at_us=now_us(), price_cents=40, size=10,
                         fee_cents=-3, is_maker=True, terminal=True))
    assert oms.realized_fees_cents() == -3
    assert oms.realized_fees_cents(sleeve_id="S1") == -3
    assert oms.realized_fees_cents(sleeve_id="S9") == 0


def test_positions_hides_flat_tickers_unless_asked(oms):
    long_ = _filled(oms, side=Side.YES, price=40, size=10)
    short = _filled(oms, side=Side.NO, price=60, size=10)
    for coid in (long_, short):
        oms.record_fill(Fill(client_order_id=coid, venue_fill_id=f"F-{coid[:6]}",
                             filled_at_us=now_us(), price_cents=40, size=10,
                             fee_cents=0, is_maker=True, terminal=True))
    assert oms.positions() == {}                       # +10 YES and -10 YES is flat
    assert oms.positions(nonzero_only=False)["KXA-1"].net_contracts == 0


def test_the_execution_report_serialises_for_the_log(db, risk, run_dir):
    """Every order decision is logged with full context (PLAN.md 0.3)."""
    venue = FakeVenue()
    ex = paper_executor(db, risk, run_dir, venue)
    coid, _ = place_one(ex)
    payload = json.dumps(ex.execute(SleeveRef("S1", 5), desired(), state(),
                                    snapshot=snapshot()).as_dict(), default=str)
    assert coid in payload and "cancelled" in payload


def test_the_kill_switch_describes_itself_for_the_pager(kill):
    assert "clear" in kill.describe()
    kill.engage("drawdown ladder rung 4")
    described = kill.describe()
    assert "ENGAGED" in described and "drawdown ladder rung 4" in described


def test_a_bare_touched_kill_file_still_reports_a_reason(run_dir):
    (run_dir / "KILL").write_text("")
    assert KillSwitch(run_dir).reason() == "(no reason recorded)"


def test_the_book_context_survives_the_executor_path(db, risk, run_dir):
    """A shadow order's queue position must reach the ledger.

    The executor writes its intent row FIRST (no book context yet), then submits
    to the shadow ledger.  A conflict-path that preserved the existing rationale
    wholesale dropped `queue_ahead` on every order routed through the executor,
    so every counterfactual fill was computed as though NOTHING was queued ahead
    of us -- silently the most optimistic queue possible, inside the model whose
    only job is to be conservative.  Measured live, real queues on these books
    run 1,500 to 4,800 contracts against orders of 11 to 92, so treating the
    queue as empty is the difference between "fills" and "never fills".
    """
    import json

    ex = shadow_executor(db, risk, run_dir)
    snap = snapshot()
    m = snap.markets[0]
    # rest a YES bid exactly AT the touch, so there is displayed size ahead
    ex.execute(SleeveRef("S2", 5),
               desired(quote(ticker=m.ticker, side=Side.YES, price=m.yes_bid)),
               state(), snapshot=snap)

    row = db.conn.execute(
        "SELECT rationale_json FROM orders WHERE ticker = ?", (m.ticker,)
    ).fetchone()
    rationale = json.loads(row["rationale_json"])

    assert "queue_ahead" in rationale, "book context was dropped on the way in"
    assert rationale["queue_ahead"] == m.yes_bid_size
    assert rationale["book_bid"] == m.yes_bid and rationale["book_ask"] == m.yes_ask
    # ...and the executor's own rationale is not lost in the process
    assert rationale["sleeve_id"] == "S2"
