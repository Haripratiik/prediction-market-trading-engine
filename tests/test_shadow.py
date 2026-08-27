"""T-044 acceptance: shadow fills are queue-conservative, and shadow mode never
touches a venue."""

from __future__ import annotations

import pytest

from core.db import Database
from core.models import Market, Side, now_us
from shadow.engine import (
    FillModel,
    ShadowExecutor,
    ShadowOrder,
    counterfactual_fill,
    fill_rate,
    markout,
)

T0 = 1_700_000_000_000_000


@pytest.fixture()
def db():
    with Database(":memory:") as d:
        yield d


_TRADE_SEQ = [0]


def add_trades(db, ticker, rows):
    """rows = [(offset_us, yes_price_cents, size, taker_side)]"""
    with db.tx() as c:
        batch = []
        for off, px, sz, side in rows:
            _TRADE_SEQ[0] += 1
            batch.append((f"{ticker}-{_TRADE_SEQ[0]}", ticker, T0 + off, px, sz, side))
        c.executemany(
            """INSERT INTO trades
               (trade_id, ticker, traded_at_us, yes_price_cents, size, taker_side, is_block)
               VALUES (?,?,?,?,?,?,0)""",
            batch,
        )


def mk_order(**kw):
    kw.setdefault("sleeve_id", "S1")
    kw.setdefault("ticker", "KXA-1")
    kw.setdefault("side", Side.YES)
    kw.setdefault("price_cents", 40)
    kw.setdefault("size", 100)
    kw.setdefault("queue_ahead", 0.0)
    kw.setdefault("book_bid", 40)
    kw.setdefault("book_ask", 42)
    kw.setdefault("rationale", {"why": "test"})
    kw.setdefault("decided_at_us", T0)
    return ShadowOrder.create(**kw)


# --------------------------------------------------------------------------- #
# The fill rule
# --------------------------------------------------------------------------- #
def test_a_trade_through_our_price_fills_us(db):
    """Resting YES bid at 40; a taker SELLS YES at 39 -> the book traded through."""
    add_trades(db, "KXA-1", [(1_000_000, 39, 50, "no")])
    f = counterfactual_fill(db, mk_order(queue_ahead=0.0))
    assert f.filled
    assert f.filled_size == 50


def test_queue_ahead_must_be_consumed_first(db):
    """We sat behind 200 contracts; only volume BEYOND that reaches us."""
    add_trades(db, "KXA-1", [(1_000_000, 39, 150, "no")])
    assert not counterfactual_fill(db, mk_order(queue_ahead=200.0)).filled

    add_trades(db, "KXA-1", [(2_000_000, 39, 120, "no")])   # cumulative 270 > 200
    f = counterfactual_fill(db, mk_order(queue_ahead=200.0))
    assert f.filled
    assert f.filled_size == pytest.approx(70.0)


def test_a_print_at_our_price_does_not_fill_us_pessimistically(db):
    """A trade AT our price means someone at our level filled -- under FIFO that
    was whoever was ahead.  The optimistic model disagrees, which is the point."""
    add_trades(db, "KXA-1", [(1_000_000, 40, 500, "no")])
    o = mk_order(queue_ahead=0.0)
    assert not counterfactual_fill(db, o, model=FillModel.PESSIMISTIC).filled
    assert counterfactual_fill(db, o, model=FillModel.OPTIMISTIC).filled


def test_the_wrong_taker_side_never_fills_a_resting_bid(db):
    """A taker BUYING YES lifts asks; it cannot fill our YES bid."""
    add_trades(db, "KXA-1", [(1_000_000, 39, 500, "yes")])
    assert not counterfactual_fill(db, mk_order()).filled


def test_resting_no_side_is_filled_by_a_yes_taker(db):
    """Mirror case: our NO bid is lifted by someone buying YES above our price."""
    add_trades(db, "KXA-1", [(1_000_000, 45, 80, "yes")])
    f = counterfactual_fill(db, mk_order(side=Side.NO, price_cents=44))
    assert f.filled
    assert f.filled_size == 80


def test_fill_is_capped_at_order_size(db):
    add_trades(db, "KXA-1", [(1_000_000, 39, 10_000, "no")])
    f = counterfactual_fill(db, mk_order(size=100))
    assert f.filled_size == 100


def test_trades_before_the_decision_are_ignored(db):
    """You cannot be filled by a trade that happened before you decided."""
    with db.tx() as c:
        c.execute(
            """INSERT INTO trades (trade_id,ticker,traded_at_us,yes_price_cents,size,taker_side,is_block)
               VALUES ('old','KXA-1',?,39,900,'no',0)""",
            (T0 - 5_000_000,),
        )
    assert not counterfactual_fill(db, mk_order()).filled


def test_horizon_bounds_the_search(db):
    add_trades(db, "KXA-1", [(60_000_000, 39, 500, "no")])   # one minute later
    o = mk_order()
    assert not counterfactual_fill(db, o, horizon_us=10_000_000).filled
    assert counterfactual_fill(db, o, horizon_us=120_000_000).filled


def test_pessimistic_never_exceeds_optimistic(db):
    """The bracket must be ordered.  A strategy profitable only at the optimistic
    bound does not exist."""
    add_trades(db, "KXA-1", [(1_000_000, 40, 300, "no"), (2_000_000, 39, 100, "no")])
    o = mk_order(queue_ahead=50.0)
    p = counterfactual_fill(db, o, model=FillModel.PESSIMISTIC)
    opt = counterfactual_fill(db, o, model=FillModel.OPTIMISTIC)
    assert p.filled_size <= opt.filled_size


# --------------------------------------------------------------------------- #
# Executor safety
# --------------------------------------------------------------------------- #
def test_shadow_executor_never_makes_a_network_call(db, monkeypatch):
    """The hard guarantee: shadow mode cannot reach a venue."""
    import httpx

    def explode(*a, **k):
        raise AssertionError("shadow mode attempted a network call")

    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx, "get", explode)
    monkeypatch.setattr(httpx, "post", explode)

    ex = ShadowExecutor(db)
    ex.submit(mk_order())
    assert len(ex.orders) == 1


def test_submitted_orders_persist_with_mode_shadow(db):
    ex = ShadowExecutor(db)
    ex.submit(mk_order())
    row = db.conn.execute("SELECT * FROM orders").fetchone()
    assert row["mode"] == "shadow"
    assert row["post_only"] == 1
    assert row["venue_order_id"] is None


def test_rationale_is_persisted_and_includes_the_book_state(db):
    """C4.2c -- an order whose reasoning cannot be reconstructed is a bug."""
    import json

    ex = ShadowExecutor(db)
    ex.submit(mk_order(rationale={"p_model": 0.88, "shrunk_edge": 0.0139},
                       queue_ahead=250.0))
    row = db.conn.execute("SELECT rationale_json FROM orders").fetchone()
    r = json.loads(row["rationale_json"])
    assert r["p_model"] == 0.88
    assert r["queue_ahead"] == 250.0
    assert r["book_bid"] == 40


def test_rationale_is_mandatory():
    with pytest.raises(ValueError, match="rationale is mandatory"):
        ShadowOrder.create(
            sleeve_id="S1", ticker="KXA-1", side=Side.YES, price_cents=40,
            size=10, queue_ahead=0.0, book_bid=40, book_ask=42, rationale={},
        )


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #
def test_fill_rate_aggregates(db):
    add_trades(db, "KXA-1", [(1_000_000, 39, 500, "no")])
    add_trades(db, "KXB-1", [(1_000_000, 41, 500, "yes")])     # wrong side for a YES bid
    orders = [mk_order(), mk_order(ticker="KXB-1")]
    stats = fill_rate(db, orders)
    assert stats["orders"] == 2
    assert stats["filled"] == 1
    assert stats["fill_rate"] == pytest.approx(0.5)


def test_markout_is_signed_by_side(db):
    """Price moved up after we bought YES at 40 -> positive mark-out."""
    db.append_markets([Market(ticker="KXA-1", yes_bid=44, yes_ask=46)],
                      observed_at_us=T0 + 300_000_000)
    up = markout(db, "KXA-1", T0, 40, Side.YES, horizon_us=300_000_000)
    assert up == pytest.approx(5.0)
    # the same move is a LOSS for a resting NO
    down = markout(db, "KXA-1", T0, 40, Side.NO, horizon_us=300_000_000)
    assert down == pytest.approx(-5.0)


def test_markout_is_none_without_a_later_observation(db):
    assert markout(db, "KXA-1", T0, 40, Side.YES, horizon_us=300_000_000) is None


# --------------------------------------------------------------------------- #
# The THREE-model bracket.  PLAN.md 6.7.
# --------------------------------------------------------------------------- #
def _order(side=Side.YES, price=74, size=92, queue_ahead=0.0):
    return ShadowOrder.create(
        sleeve_id="S2", ticker="KXQ", side=side, price_cents=price, size=size,
        queue_ahead=queue_ahead, book_bid=73, book_ask=74,
        rationale={"why": "t"}, decided_at_us=T0,
    )


def test_the_pessimistic_model_is_structurally_zero_when_all_prints_are_at_our_price():
    """Why the middle model has to exist.

    PESSIMISTIC counts only volume that traded STRICTLY THROUGH our price.  An
    illiquid market -- which is every mutually-exclusive basket this engine
    selects -- prints at ONE price, so the strict inequality matches nothing and
    the column reads a permanent zero.  PLAN.md R6.7a says to GATE on that
    column, so a gate reading it alone would never promote anything, and would
    fail silently: it looks exactly like a strategy that found no fills.
    """
    d = Database(":memory:")
    try:
        add_trades(d, "KXQ", [(s * 1_000_000, 74, 200.0, "yes") for s in range(1, 4)])
        o = _order(side=Side.NO, price=74)
        assert counterfactual_fill(d, o, model=FillModel.PESSIMISTIC).filled_size == 0.0
        assert counterfactual_fill(d, o, model=FillModel.REALISTIC).filled_size > 0.0
    finally:
        d.close()


@pytest.mark.parametrize("queue_ahead", [0.0, 100.0, 500.0, 10_000.0])
def test_the_three_models_are_ordered_at_every_queue_depth(queue_ahead):
    """pessimistic <= realistic <= optimistic.  The bracket is only meaningful
    if it is actually a bracket."""
    d = Database(":memory:")
    try:
        add_trades(d, "KXQ", [(1_000_000, 74, 300.0, "yes"),
                              (2_000_000, 75, 150.0, "yes"),
                              (3_000_000, 74, 200.0, "yes")])
        o = _order(side=Side.NO, price=74, queue_ahead=queue_ahead)
        got = [counterfactual_fill(d, o, model=m).filled_size
               for m in (FillModel.PESSIMISTIC, FillModel.REALISTIC,
                         FillModel.OPTIMISTIC)]
        assert got[0] <= got[1] <= got[2], got
    finally:
        d.close()


def test_the_realistic_model_still_charges_the_queue_ahead():
    """It is the CENTRAL estimate, not the flattering one -- a deep queue must
    still block the fill, or resting behind 4,000 contracts looks free."""
    d = Database(":memory:")
    try:
        add_trades(d, "KXQ", [(1_000_000, 74, 300.0, "yes")])
        shallow = _order(side=Side.NO, price=74, queue_ahead=10.0)
        deep = _order(side=Side.NO, price=74, queue_ahead=5_000.0)
        assert counterfactual_fill(d, shallow, model=FillModel.REALISTIC).filled_size > 0
        assert counterfactual_fill(d, deep, model=FillModel.REALISTIC).filled_size == 0.0
    finally:
        d.close()


def test_a_yes_bid_is_filled_by_a_taker_selling_yes_at_or_below_our_price():
    """Direction check: the mirror of the NO case, so a sign error shows up."""
    d = Database(":memory:")
    try:
        add_trades(d, "KXQ", [(1_000_000, 40, 500.0, "no")])
        o = _order(side=Side.YES, price=40, size=50)
        assert counterfactual_fill(d, o, model=FillModel.REALISTIC).filled_size == 50.0
        # a taker BUYING yes does not lift our bid
        d2 = Database(":memory:")
        try:
            add_trades(d2, "KXQ", [(1_000_000, 40, 500.0, "yes")])
            assert counterfactual_fill(d2, o, model=FillModel.REALISTIC).filled_size == 0.0
        finally:
            d2.close()
    finally:
        d.close()
