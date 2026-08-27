"""T-056: mark-outs are persisted as they mature, once, and never revised.

The `marks` table exists because recomputing a mark-out later gives a DIFFERENT
answer -- the archive keeps a ~400-ticker watchlist against a 106,000-market
universe, so an hour after the fact the nearest quote is minutes from the
horizon it is standing in for.  These tests pin the properties that make a
persisted mark trustworthy.
"""

from __future__ import annotations

import pytest

from core.db import Database
from core.models import Fill, OrderRequest, RunMode, Side, Venue
from execution.oms import OMS, new_client_order_id
from monitor.marks import MarkRecorder, staleness_budget_us

T0 = 1_700_000_000_000_000
SEC = 1_000_000


@pytest.fixture()
def db():
    with Database(":memory:") as d:
        yield d


def add_order_and_fill(db, *, ticker="KXM", side=Side.YES, order_px=40,
                       fill_px=None, at_us=T0):
    """`orders.price_cents` is YES-referenced; `fills.price_cents` is SIDE-referenced."""
    oms = OMS(db, venue=Venue.KALSHI)
    coid = new_client_order_id()
    oms.record_intent(OrderRequest(
        client_order_id=coid, sleeve_id="S1", venue=Venue.KALSHI, ticker=ticker,
        side=side, price_cents=order_px, size=10, post_only=True,
        mode=RunMode.SHADOW, rationale={"w": 1},
    ))
    stored = fill_px if fill_px is not None else (
        order_px if side is Side.YES else 100 - order_px)
    oms.record_fill(Fill(
        client_order_id=coid, venue_fill_id=f"vf-{coid[:8]}", filled_at_us=at_us,
        price_cents=stored, size=10, fee_cents=0, is_maker=True, terminal=True,
    ))
    return coid


def add_quote(db, ticker, *, at_us, bid, ask):
    with db.tx() as c:
        c.execute(
            """INSERT INTO market_snapshots
               (venue, ticker, event_ticker, series_ticker, title, status,
                yes_bid, yes_ask, yes_bid_size, yes_ask_size, volume,
                volume_24h, open_interest, observed_at_us, rules_hash)
               VALUES ('kalshi',?,'EV','SER','t','active',?,?,100,100,0,10,0,?,'h')""",
            (ticker, bid, ask, at_us),
        )


# --------------------------------------------------------------------------- #
# The measurement itself
# --------------------------------------------------------------------------- #
def test_a_rising_fair_value_helps_a_long_yes_and_hurts_a_long_no(db):
    """The sign is the entire signal.  Backwards, a bleeding sleeve looks healthy."""
    add_order_and_fill(db, ticker="KXY", side=Side.YES, order_px=40)
    add_order_and_fill(db, ticker="KXN", side=Side.NO, order_px=40)
    for t in ("KXY", "KXN"):
        add_quote(db, t, at_us=T0 + 1 * SEC, bid=44, ask=46)      # mid 45

    marks = {(m.ticker, m.horizon_us): m
             for m in MarkRecorder(db).mature(now=T0 + 3600 * SEC)}
    assert marks[("KXY", 1 * SEC)].markout_cents == pytest.approx(+5.0)
    assert marks[("KXN", 1 * SEC)].markout_cents == pytest.approx(-5.0)


def test_a_horizon_that_has_not_elapsed_yet_is_not_recorded(db):
    """Recording early would mark a trade against a quote from before its horizon."""
    add_order_and_fill(db, ticker="KXM")
    add_quote(db, "KXM", at_us=T0 + 1 * SEC, bid=44, ask=46)
    rec = MarkRecorder(db)
    assert rec.mature(now=T0) == []            # nothing has matured
    assert rec.stats.immature > 0


def test_a_horizon_with_no_nearby_quote_is_left_unwritten_not_zero(db):
    """A missing row must mean NOT MEASURABLE.  Writing 0.0 would drag the mean
    toward zero and make an adverse-selection problem look like a mild one."""
    add_order_and_fill(db, ticker="KXM")
    add_quote(db, "KXM", at_us=T0 + 3600 * SEC, bid=44, ask=46)   # an hour late
    rec = MarkRecorder(db)
    marks = rec.mature(now=T0 + 7200 * SEC)
    assert not any(m.horizon_us == 1 * SEC for m in marks)
    assert rec.stats.unobserved > 0


def test_the_staleness_budget_is_half_the_horizon_floored_at_one_second(db):
    """It must agree with `shadow.engine`, or persisted and on-the-fly marks
    would disagree about the same fill."""
    from shadow.engine import _staleness_budget

    for h in (1 * SEC, 5 * SEC, 60 * SEC, 300 * SEC, 1800 * SEC):
        assert staleness_budget_us(h) == _staleness_budget(h)


def test_the_reference_quote_records_how_stale_it_actually_was(db):
    """`stale_us` is what lets a later reader judge the number rather than
    trust it."""
    add_order_and_fill(db, ticker="KXM")
    add_quote(db, "KXM", at_us=T0 + 60 * SEC + 7 * SEC, bid=44, ask=46)
    m = next(m for m in MarkRecorder(db).mature(now=T0 + 3600 * SEC)
             if m.horizon_us == 60 * SEC)
    assert m.stale_us == 7 * SEC
    assert m.ref_mid == pytest.approx(45.0)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def test_a_matured_horizon_is_written_once_and_never_revised(db):
    """The whole reason the table exists.  Re-deriving later against a sparser
    archive would silently change an already-published KPI."""
    add_order_and_fill(db, ticker="KXM")
    add_quote(db, "KXM", at_us=T0 + 1 * SEC, bid=44, ask=46)
    rec = MarkRecorder(db)
    assert rec.cycle(now=T0 + 3600 * SEC) >= 1
    first = db.conn.execute("SELECT markout_cents FROM marks").fetchone()["markout_cents"]

    # the book moves, and a second pass must NOT rewrite the recorded value
    add_quote(db, "KXM", at_us=T0 + 2 * SEC, bid=10, ask=12)
    assert rec.cycle(now=T0 + 7200 * SEC) == 0
    after = db.conn.execute("SELECT markout_cents FROM marks").fetchone()["markout_cents"]
    assert after == first


def test_a_no_fill_is_converted_to_the_yes_reference_before_marking(db):
    """`fills.price_cents` is SIDE-referenced; a mark-out is YES-referenced.

    Skipping the conversion is wrong by (100 - 2p) on every NO fill -- the whole
    signal at any price away from 50c.
    """
    # NO order at YES-price 40 -> the fill is stored at 60 (the NO contract cost)
    add_order_and_fill(db, ticker="KXN", side=Side.NO, order_px=40, fill_px=60)
    add_quote(db, "KXN", at_us=T0 + 1 * SEC, bid=44, ask=46)      # mid 45
    m = next(m for m in MarkRecorder(db).mature(now=T0 + 3600 * SEC)
             if m.horizon_us == 1 * SEC)
    # yes-referenced entry is 40; fair rose to 45; a long NO is hurt by 5c
    assert m.markout_cents == pytest.approx(-5.0)


def test_an_empty_ledger_produces_no_marks_and_does_not_raise(db):
    rec = MarkRecorder(db)
    assert rec.cycle(now=T0) == 0
    assert all(n == 0 for n, _ in rec.curve().values())


def test_the_curve_reports_unmeasured_horizons_as_none_not_zero(db):
    add_order_and_fill(db, ticker="KXM")
    add_quote(db, "KXM", at_us=T0 + 1 * SEC, bid=44, ask=46)
    rec = MarkRecorder(db)
    rec.cycle(now=T0 + 3600 * SEC)
    curve = rec.curve()
    assert curve[1 * SEC][0] == 1 and curve[1 * SEC][1] == pytest.approx(5.0)
    assert curve[1800 * SEC] == (0, None)      # never measurable, never a zero
