"""Fill-ingestion acceptance.  PLAN.md 6.4, 7.2, I4.

`OMS.record_fill()` had no caller outside a test, so the engine placed orders and
never learned whether they filled.  Every property proven here is one whose
failure shows up as money rather than as a red build:

    I4      position comes from persisted fills, and ingestion is the only thing
            that persists them
    7.2     shadow and live write the SAME table, so a shadow P&L is comparable
            with the live one it is supposed to predict
    0.3     fills arrive YES-REFERENCED and are stored SIDE-REFERENCED; getting
            that backwards flips the sign of every NO position
    2.1     fees are per SERIES and SIGNED -- a plain-quadratic maker pays zero
            and a rebate is a credit, not a charge

No test here may reach a venue.  The only client is `FakeFillVenue`, and the
shadow test asserts the no-network property down at the httpx layer.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from core.db import Database
from core.math.contracts import FeeSpec
from core.models import (
    OrderRequest,
    OrderState,
    RunMode,
    Series,
    Side,
    Venue,
    now_us,
)
from execution.fillfeed import (
    IngestReport,
    ShadowFillFeed,
    VenueFillFeed,
    fill_feed_for,
)
from execution.oms import OMS, new_client_order_id
from shadow.engine import FillModel, ShadowExecutor, ShadowOrder

T0 = 1_700_000_000_000_000


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #
class FakeFillVenue:
    """A venue that hands back a canned fill page.  Never opens a socket."""

    def __init__(self, *pages: list[dict[str, Any]]) -> None:
        self.pages: list[list[dict[str, Any]]] = list(pages) or [[]]
        self.calls: list[dict[str, Any]] = []
        self._n = 0

    def iter_fills(self, **params: Any):
        self.calls.append(dict(params))
        page = self.pages[min(self._n, len(self.pages) - 1)]
        self._n += 1
        yield from page

    @property
    def last_call(self) -> dict[str, Any]:
        return self.calls[-1] if self.calls else {}


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
def disk_dir():
    """A real directory for the restart test.

    Deliberately NOT pytest's `tmp_path`: this machine's pytest temp root raises
    PermissionError (WinError 5), and a high-water-mark test that cannot create a
    file proves nothing about resuming after a restart.
    """
    d = Path(tempfile.mkdtemp(prefix="pm-fillfeed-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def request(**kw: Any) -> OrderRequest:
    kw.setdefault("client_order_id", new_client_order_id())
    kw.setdefault("sleeve_id", "S1")
    kw.setdefault("venue", Venue.KALSHI)
    kw.setdefault("ticker", "KXA-1")
    kw.setdefault("side", Side.YES)
    kw.setdefault("price_cents", 40)
    kw.setdefault("size", 100)
    kw.setdefault("mode", RunMode.LIVE)
    kw.setdefault("rationale", {"why": "test"})
    return OrderRequest(**kw)


def resting_live_order(oms: OMS, *, venue_order_id: str = "V-1", **kw: Any) -> str:
    """An order the venue has acknowledged -- the state a fill arrives against."""
    req = request(**kw)
    oms.record_intent(req)
    oms.record_ack(req.client_order_id, venue_order_id, OrderState.OPEN)
    return req.client_order_id


def resting_shadow_order(db: Database, oms: OMS, *, queue_ahead: float = 0.0,
                         **kw: Any) -> str:
    """The same order, recorded instead of sent -- `Executor._send_shadow`'s path.

    `record_intent` FIRST, then `ShadowExecutor.submit`, because that is the order
    the executor uses and the two write the same row.  The row's `created_at_us`
    is then pinned to T0: it is the anchor `counterfactual_fill` searches forward
    from, and a backtest order genuinely carries the historical timestamp of the
    tape it is being replayed against.
    """
    kw.setdefault("mode", RunMode.SHADOW)
    req = request(**kw)
    oms.record_intent(req)
    ShadowExecutor(db).submit(
        ShadowOrder.create(
            client_order_id=req.client_order_id,
            sleeve_id=req.sleeve_id,
            ticker=req.ticker,
            side=req.side,
            price_cents=req.price_cents,
            size=req.size,
            queue_ahead=queue_ahead,
            book_bid=38,
            book_ask=42,
            rationale=req.rationale,
            decided_at_us=T0,
            mode=req.mode,
        )
    )
    with db.tx() as c:
        c.execute(
            "UPDATE orders SET created_at_us = ? WHERE client_order_id = ?",
            (T0, req.client_order_id),
        )
    return req.client_order_id


_TRADE_SEQ = [0]


def add_trades(db: Database, ticker: str, rows: list[tuple[int, int, float, str]]) -> None:
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


def venue_fill(**kw: Any) -> dict[str, Any]:
    """A `GET /portfolio/fills` row.

    The field names are UNVERIFIED against a credentialed response -- see the
    header of `execution/fillfeed.py`.  They are exercised here in both the V1
    integer-cent and the V2 dollar-string shape precisely because we do not know
    which one the account will actually receive.
    """
    kw.setdefault("trade_id", "T-1")
    kw.setdefault("order_id", "V-1")
    kw.setdefault("yes_price", 40)
    kw.setdefault("count", 100)
    kw.setdefault("created_time", "2026-08-26T12:00:00Z")
    kw.setdefault("is_taker", False)
    return kw


# =========================================================================== #
# Idempotency -- the property that makes a reconnect survivable
# =========================================================================== #
def test_replaying_a_fill_page_does_not_double_count_the_position(oms):
    """A websocket reconnect plus a REST backfill re-delivers the whole window.

    Double-counting there does not self-correct: the position is an aggregate
    over rows, so a duplicated row is a phantom 100 contracts that every risk cap
    then measures against forever.
    """
    coid = resting_live_order(oms)
    venue = FakeFillVenue([venue_fill(order_id="V-1", count=100)])
    feed = VenueFillFeed(oms=oms, client=venue)

    first = feed.poll()
    assert (first.recorded, first.duplicates) == (1, 0)
    assert oms.position("KXA-1").net_contracts == 100

    second = feed.poll()
    assert (second.recorded, second.duplicates) == (0, 1)
    assert oms.position("KXA-1").net_contracts == 100
    assert len(oms.fills_for(coid)) == 1


def test_two_different_fills_of_one_order_are_both_kept(oms):
    """Dedupe is on the venue's fill id, NOT on (order, price, size).

    Two 50-lot fills of the same resting order at the same price are ordinary and
    are two fills; collapsing them would halve the position.
    """
    coid = resting_live_order(oms, size=100)
    venue = FakeFillVenue([
        venue_fill(trade_id="T-1", count=50),
        venue_fill(trade_id="T-2", count=50),
    ])
    feed = VenueFillFeed(oms=oms, client=venue)
    report = feed.poll()
    assert report.recorded == 2
    assert oms.position("KXA-1").net_contracts == 100
    assert len(oms.fills_for(coid)) == 2


def test_the_high_water_mark_survives_a_restart_and_resumes(disk_dir):
    """A restart must resume, not replay from the beginning of the account.

    Replaying is merely slow while dedupe holds; the reason the mark is persisted
    is that a fresh process otherwise pages the ENTIRE fill history on every
    start, and at 10 tokens a request that is a rate-limit outage during the one
    window the engine most needs to know its position.
    """
    path = disk_dir / "pm.db"
    db = Database(path)
    try:
        oms = OMS(db)
        resting_live_order(oms)
        venue = FakeFillVenue([venue_fill()])
        first = VenueFillFeed(oms=oms, client=venue).poll()
        assert first.high_water_us > 0
        assert "min_ts" not in venue.last_call        # nothing to resume from yet
    finally:
        db.close()

    reopened = Database(path)
    try:
        feed = VenueFillFeed(oms=OMS(reopened), client=FakeFillVenue([]))
        assert feed.high_water_us == first.high_water_us
        feed.poll()
        # Kalshi's window filter is epoch SECONDS, floored so the boundary second
        # is re-read rather than skipped.
        assert feed.client.last_call["min_ts"] == first.high_water_us // 1_000_000
    finally:
        reopened.close()


def test_the_high_water_mark_never_advances_past_an_unmatched_fill(oms):
    """A fill we cannot tie to an order is a position we hold and cannot see.

    If the mark stepped over it, the next poll would never offer it again and the
    contracts would be invisible until a settlement statement disagreed with us.
    """
    resting_live_order(oms, venue_order_id="V-1")
    venue = FakeFillVenue([
        venue_fill(trade_id="T-known", order_id="V-1", created_time="2026-08-26T12:00:00Z"),
        venue_fill(trade_id="T-orphan", order_id="V-UNKNOWN",
                   created_time="2026-08-26T11:00:00Z"),
    ])
    feed = VenueFillFeed(oms=oms, client=venue)
    report = feed.poll()

    assert report.unmatched == ("T-orphan",)
    assert not report.is_clean
    orphan_us = 1_787_742_000_000_000                      # 2026-08-26T11:00:00Z
    assert report.high_water_us < orphan_us
    assert oms.position("KXA-1").net_contracts == 100      # the known one still landed


def test_a_fill_we_cannot_identify_is_reported_rather_than_dropped(oms):
    """No dedupe key means no idempotency, so the fill cannot be stored at all.

    Silently dropping it would understate the position; storing it without a key
    would double it on the next poll.  Reporting is the only honest option.
    """
    resting_live_order(oms)
    venue = FakeFillVenue([{"order_id": "V-1", "yes_price": 40, "count": 10}])
    report = VenueFillFeed(oms=oms, client=venue).poll()
    assert report.recorded == 0
    assert len(report.malformed) == 1
    assert not report.is_clean


# =========================================================================== #
# YES-referencing -- the bug class that has bitten this repo twice
# =========================================================================== #
def test_a_no_fill_produces_a_negative_net_position(oms):
    """PLAN.md 0.3: a NO contract at YES-price p is a SHORT YES of the same size.

    Read it as a long and a fully hedged two-sided quoter looks like double the
    exposure it has, while a genuine short reads as a long -- the position cap,
    the theme cap and the venue cap are then all wrong at once, in the direction
    that permits more risk.
    """
    coid = resting_live_order(oms, side=Side.NO, price_cents=40)   # YES-price 40
    venue = FakeFillVenue([venue_fill(yes_price=40, count=100)])
    VenueFillFeed(oms=oms, client=venue).poll()

    pos = oms.position("KXA-1")
    assert pos.net_contracts == -100
    assert pos.side is Side.NO
    # Stored as the price PAID for the NO (60c), which is what `OMS.position`
    # re-derives the YES price from.  Store the venue's 40c verbatim and the
    # basis comes back as 60c and `runner.py` locks 40c of capital where the
    # short actually locks 60c.
    assert oms.fills_for(coid)[0].price_cents == 60
    assert pos.avg_price_cents == pytest.approx(40.0)


def test_a_yes_and_a_no_fill_of_equal_size_net_to_flat(oms):
    """The hedge test.  Two legs that cancel must read as zero, not as gross 200."""
    resting_live_order(oms, side=Side.YES, price_cents=40, venue_order_id="V-Y")
    resting_live_order(oms, side=Side.NO, price_cents=40, venue_order_id="V-N")
    venue = FakeFillVenue([
        venue_fill(trade_id="T-Y", order_id="V-Y", yes_price=40, count=100),
        venue_fill(trade_id="T-N", order_id="V-N", yes_price=40, count=100),
    ])
    VenueFillFeed(oms=oms, client=venue).poll()
    assert oms.position("KXA-1").net_contracts == 0


def test_the_payloads_own_side_field_never_decides_our_position(oms):
    """Kalshi's `side` describes its single YES book; ours names an outcome bought.

    The two vocabularies are not the same, so the sign of a position is taken
    from OUR order row and never from the payload.  Here the payload calls the
    fill a 'yes' while our order is a NO, and the position must still be short.
    """
    resting_live_order(oms, side=Side.NO, price_cents=40)
    venue = FakeFillVenue([venue_fill(side="yes", action="sell", yes_price=40)])
    VenueFillFeed(oms=oms, client=venue).poll()
    assert oms.position("KXA-1").net_contracts == -100


def test_a_dollar_string_price_and_an_integer_cent_price_are_read_alike(db):
    """Kalshi V2 quotes fixed-point DOLLAR strings ('0.4000'); V1 sent cents (40).

    Both shapes are in circulation and we cannot verify which this account gets,
    so a mis-read must be impossible rather than unlikely: read '0.4000' as 0
    cents and the position's basis collapses to nothing.
    """
    positions = []
    for payload in ({"yes_price": 40}, {"yes_price_dollars": "0.4000"}):
        with Database(":memory:") as fresh:
            oms = OMS(fresh)
            resting_live_order(oms)
            venue = FakeFillVenue([venue_fill(**payload)])
            VenueFillFeed(oms=oms, client=venue).poll()
            positions.append(oms.position("KXA-1"))
    assert positions[0].net_contracts == positions[1].net_contracts == 100
    assert positions[0].avg_price_cents == positions[1].avg_price_cents == 40.0


def test_a_fill_reported_only_by_its_no_price_is_converted(oms):
    """Some payloads carry `no_price` alongside (or instead of) `yes_price`.

    no at 30c is yes at 70c -- the same identity `per_contract_cost_cents` uses.
    """
    resting_live_order(oms, side=Side.YES, price_cents=70)
    venue = FakeFillVenue([{"trade_id": "T-1", "order_id": "V-1",
                            "no_price": 30, "count": 100,
                            "created_time": "2026-08-26T12:00:00Z"}])
    VenueFillFeed(oms=oms, client=venue).poll()
    pos = oms.position("KXA-1")
    assert (pos.net_contracts, pos.avg_price_cents) == (100, 70.0)


# =========================================================================== #
# Fees -- per series, signed, and zero for most makers
# =========================================================================== #
def test_a_maker_fill_on_a_plain_quadratic_series_pays_zero(oms):
    """13,353 of 13,518 Kalshi series charge makers NOTHING (research/06 s4).

    A constant fee here would bill the entire maker-first book for a fee it does
    not owe -- 1.68c per contract at 40c, which is larger than the whole edge the
    flagship sleeve quotes for.
    """
    resting_live_order(oms)
    venue = FakeFillVenue([venue_fill(is_taker=False, yes_price=40, count=100)])
    report = VenueFillFeed(oms=oms, client=venue).poll()
    assert report.fees_cents == 0
    assert oms.realized_fees_cents() == 0


def test_the_same_fill_taken_rather_than_made_is_charged(oms):
    """The maker/taker distinction is the entire fee, so it must survive ingest."""
    resting_live_order(oms)
    venue = FakeFillVenue([venue_fill(is_taker=True, yes_price=40, count=100)])
    report = VenueFillFeed(oms=oms, client=venue).poll()
    assert report.fees_cents == 168          # ceil(0.07 * 0.4 * 0.6 * 100 contracts)


def test_fees_are_read_from_the_series_and_not_from_a_constant(db):
    """Two series, two rates, identical fills.  research/06 section 4.

    `fee_multiplier` halves the fee on the 19 MLB derivative series and
    `fee_type` decides whether a maker is billed at all on the ~130 that bill
    them.  One global coefficient mis-prices both cohorts simultaneously.
    """
    db.upsert_series([
        Series(ticker="KXPLAIN", fee_type="quadratic", fee_multiplier=1.0),
        Series(ticker="KXHALF", fee_type="quadratic", fee_multiplier=0.5),
        Series(ticker="KXMAKER", fee_type="quadratic_with_maker_fees",
               fee_multiplier=1.0),
    ])
    oms = OMS(db)
    feed = VenueFillFeed(oms=oms)

    assert feed.fee_cents("KXPLAIN-1", 50, 100, is_maker=False) == 175
    assert feed.fee_cents("KXHALF-1", 50, 100, is_maker=False) == 88     # half, rounded up
    assert feed.fee_cents("KXPLAIN-1", 50, 100, is_maker=True) == 0
    # 0.25x the base rate -- the only cohort where a maker pays anything.
    assert feed.fee_cents("KXMAKER-1", 50, 100, is_maker=True) == 44


def test_an_unknown_series_is_charged_the_full_taker_rate(oms):
    """The fallback must be expensive, never free.

    A fill on a market we never snapshotted is common right after a restart.
    Assuming it is fee-free flatters every KPI that reads realized fees; assuming
    it is fully billed can only understate edge.
    """
    feed = VenueFillFeed(oms=oms)
    assert feed.fee_cents("NOTASERIES-1", 50, 100, is_maker=False) == 175


def test_a_rebate_is_stored_negative_and_raises_pnl(db):
    """Polymarket pays its makers.  A rebate is a NEGATIVE fee, never an abs().

    P&L is `gross - fees`, so a credit recorded as a charge is a two-sided error:
    it costs the rebate AND charges it, moving reported P&L by twice the amount.
    """
    oms = OMS(db, venue=Venue.POLYMARKET_US)
    resting_live_order(oms, venue=Venue.POLYMARKET_US, price_cents=50)
    venue = FakeFillVenue([venue_fill(is_taker=False, yes_price=50, count=100)])
    report = VenueFillFeed(oms=oms, client=venue).poll()

    assert report.fees_cents == -31          # ceil(-0.0125 * 0.25 * 100 contracts)
    assert oms.realized_fees_cents() < 0

    gross_cents = 1_000
    assert gross_cents - oms.realized_fees_cents() > gross_cents


def test_a_fee_the_venue_itself_reports_beats_our_model_of_it(oms):
    """Our fee model predicts the venue's arithmetic; the venue performs it.

    Kalshi carries a per-order rounding accumulator (research/06 s4.3) that no
    per-fill formula can reproduce, so when a payload states a fee we book that
    number and reconcile against ours rather than the other way round.
    """
    resting_live_order(oms)
    venue = FakeFillVenue([venue_fill(is_taker=True, fee_cents=999)])
    report = VenueFillFeed(oms=oms, client=venue).poll()
    assert report.fees_cents == 999


def test_a_reported_rebate_is_not_flipped_into_a_charge(oms):
    """The same path as above, with the sign that is easy to lose."""
    resting_live_order(oms)
    venue = FakeFillVenue([venue_fill(fee_dollars="-0.2500")])
    assert VenueFillFeed(oms=oms, client=venue).poll().fees_cents == -25


# =========================================================================== #
# Order state -- a partial must stay workable
# =========================================================================== #
def test_a_partial_fill_leaves_the_order_open_with_reduced_remaining(oms):
    """PLAN.md 6.4: the residual keeps its queue position, so it stays resting.

    Mark it filled and the executor stops managing an order that is still live at
    the venue -- unhedged, uncancelled, and outside every diff.  Mark it untouched
    and the next diff re-posts the size we already own.
    """
    coid = resting_live_order(oms, size=100)
    venue = FakeFillVenue([venue_fill(count=40)])
    VenueFillFeed(oms=oms, client=venue).poll()

    rec = oms.get(coid)
    assert rec.state is OrderState.PARTIAL
    assert rec.remaining == 60
    assert rec in oms.open_orders()


def test_the_final_fill_closes_the_order(oms):
    coid = resting_live_order(oms, size=100)
    venue = FakeFillVenue(
        [venue_fill(trade_id="T-1", count=40)],
        [venue_fill(trade_id="T-2", count=60)],
    )
    feed = VenueFillFeed(oms=oms, client=venue)
    feed.poll()
    feed.poll()
    assert oms.get(coid).state is OrderState.FILLED
    assert oms.open_orders() == []


# =========================================================================== #
# Shadow materialisation -- PLAN.md 7.2, the identical code path
# =========================================================================== #
def test_shadow_and_live_produce_the_same_position_for_the_same_fill_sequence(disk_dir):
    """The PLAN.md 7.2 guarantee, stated as an equality rather than a promise.

    If shadow P&L came out of a different pipe than live P&L, a shadow result
    would prove nothing about the live one, and Gate 3 would be measuring the
    fill model instead of the strategy.  Same NO order, same 60 contracts, same
    price -- the two paths must agree on sign, size and basis.
    """
    with Database(":memory:") as live_db, Database(":memory:") as shadow_db:
        # A NO quote at YES-price 40 -- i.e. a YES ask resting at 40 (PLAN.md 0.3;
        # `orders.price_cents` is YES-referenced on both sides).
        live = OMS(live_db)
        resting_live_order(live, side=Side.NO, price_cents=40, size=100)
        VenueFillFeed(
            oms=live,
            client=FakeFillVenue([venue_fill(yes_price=40, count=60)]),
        ).poll()

        shadow = OMS(shadow_db)
        resting_shadow_order(shadow_db, shadow, side=Side.NO, price_cents=40, size=100)
        # A taker BUYING YES through our resting sell-YES is what lifts it.
        add_trades(shadow_db, "KXA-1", [(1_000_000, 45, 60, "yes")])
        ShadowFillFeed(oms=shadow).poll()

        assert live.position("KXA-1") == shadow.position("KXA-1")
        assert live.position("KXA-1").net_contracts == -60
        assert live.position("KXA-1").avg_price_cents == pytest.approx(40.0)


def test_a_shadow_fill_lands_in_the_same_fills_table_the_kpis_read(db, oms):
    """Shadow positions were empty because nothing wrote the counterfactual down.

    Every KPI downstream -- P&L, mark-out, orphan-leg ratio, every risk cap --
    reads `fills`.  A shadow run that does not write there measures nothing.
    """
    coid = resting_shadow_order(db, oms)
    add_trades(db, "KXA-1", [(1_000_000, 39, 100, "no")])
    report = ShadowFillFeed(oms=oms).poll()

    assert report.recorded == 1
    assert oms.position("KXA-1").net_contracts == 100
    assert oms.fills_for(coid)[0].venue_fill_id.startswith("shadow:")


def test_replaying_the_shadow_materialisation_does_not_double_count(db, oms):
    """The same trap as a replayed venue page, reached from the other direction.

    A backtest sweep re-runs the same tape constantly.  A sequence-numbered fill
    id would double the position on every pass and the equity curve would be a
    function of how many times the sweep ran.
    """
    resting_shadow_order(db, oms)
    add_trades(db, "KXA-1", [(1_000_000, 39, 100, "no")])
    feed = ShadowFillFeed(oms=oms)

    feed.poll()
    before = oms.position("KXA-1").net_contracts
    for _ in range(3):
        feed.poll()
    assert oms.position("KXA-1").net_contracts == before == 100


def test_shadow_fills_accumulate_as_the_tape_grows(db, oms):
    """Idempotent and INCREMENTAL at once -- the harder half of the property.

    The tape keeps arriving, so the second pass must add only what is new.  An id
    that ignored the cumulative size would never record the second tranche and
    the position would freeze at its first partial.
    """
    coid = resting_shadow_order(db, oms, size=100)
    feed = ShadowFillFeed(oms=oms)

    add_trades(db, "KXA-1", [(1_000_000, 39, 60, "no")])
    feed.poll()
    assert oms.get(coid).state is OrderState.PARTIAL
    assert oms.get(coid).remaining == 40

    add_trades(db, "KXA-1", [(2_000_000, 39, 40, "no")])
    report = feed.poll()
    assert report.recorded == 1
    assert [f.size for f in oms.fills_for(coid)] == [60, 40]
    assert oms.position("KXA-1").net_contracts == 100
    assert oms.get(coid).state is OrderState.FILLED


def test_a_shadow_fill_is_priced_at_our_resting_price_not_the_takers(db, oms):
    """A maker is filled at the price it POSTED, whatever the taker was willing
    to pay.  Booking the taker's price would manufacture free basis on every
    fill and make a losing quote look profitable."""
    coid = resting_shadow_order(db, oms, side=Side.YES, price_cents=40)
    add_trades(db, "KXA-1", [(1_000_000, 30, 100, "no")])      # taker sold far below
    ShadowFillFeed(oms=oms).poll()
    assert oms.fills_for(coid)[0].price_cents == 40


def test_a_shadow_no_leg_is_filled_by_the_flow_that_would_really_have_lifted_it(db, oms):
    """`orders.price_cents` is YES-referenced on BOTH sides (PLAN.md 0.3).

    A Side.NO quote at YES-price 40 is a YES ask resting at 40, so a taker buying
    YES at 45 lifts it and a taker buying at 35 does not.  Mirror the price into
    the tape query and the error is one-sided in whichever direction the leg
    sits: a short-basket leg either never fills, or fills on prints that never
    reached it -- which books a position the tape does not support.
    """
    coid = resting_shadow_order(db, oms, side=Side.NO, price_cents=40, size=100)
    add_trades(db, "KXA-1", [(1_000_000, 35, 500, "yes")])     # below us: no fill
    assert ShadowFillFeed(oms=oms).poll().recorded == 0

    add_trades(db, "KXA-1", [(2_000_000, 45, 100, "yes")])     # through us: fills
    assert ShadowFillFeed(oms=oms).poll().recorded == 1

    pos = oms.position("KXA-1")
    assert pos.net_contracts == -100
    assert pos.avg_price_cents == pytest.approx(40.0)
    assert oms.fills_for(coid)[0].price_cents == 60            # the price paid for NO


def test_the_queue_conservative_model_materialises_no_more_than_the_optimistic_one(db):
    """R6.7a: gates read the PESSIMISTIC column, so it must be the smaller one.

    A print AT our price filled whoever was ahead of us under FIFO, so the
    conservative model records nothing while the optimistic model records a fill.
    A strategy that only exists under the optimistic bound does not exist.
    """
    counts = {}
    for model in (FillModel.PESSIMISTIC, FillModel.OPTIMISTIC):
        with Database(":memory:") as fresh:
            oms = OMS(fresh)
            resting_shadow_order(fresh, oms, price_cents=40, size=100)
            add_trades(fresh, "KXA-1", [(1_000_000, 40, 500, "no")])
            ShadowFillFeed(oms=oms, model=model).poll()
            counts[model] = oms.position("KXA-1").net_contracts
    assert counts[FillModel.PESSIMISTIC] == 0
    assert counts[FillModel.OPTIMISTIC] == 100


def test_a_counterfactual_fill_is_never_rounded_up(db, oms):
    """The tape is measured in fractional contracts; a position is not.

    Rounding 39.6 filled contracts up to 40 invents a contract the tape does not
    support, which is the one error a pessimistic fill model exists to prevent.
    """
    coid = resting_shadow_order(db, oms, size=100)
    add_trades(db, "KXA-1", [(1_000_000, 39, 39.6, "no")])
    ShadowFillFeed(oms=oms).poll()
    assert oms.fills_for(coid)[0].size == 39


def test_the_shadow_feed_never_invents_a_fill_for_a_live_order(db, oms):
    """A live order's fills come from the venue and from nowhere else.

    Materialising a counterfactual over a real resting order would book a
    position the exchange has no record of, and reconciliation would then report
    drift that does not exist.
    """
    resting_shadow_order(db, oms, mode=RunMode.LIVE)
    add_trades(db, "KXA-1", [(1_000_000, 39, 500, "no")])   # would fill it, if allowed
    report = ShadowFillFeed(oms=oms).poll()
    assert report.recorded == 0
    assert oms.position("KXA-1").net_contracts == 0


def test_the_shadow_feed_makes_no_network_call(db, oms, monkeypatch):
    """The hard guarantee shadow mode is built on (PLAN.md 7.2)."""
    import httpx

    def explode(*a: Any, **k: Any) -> None:
        raise AssertionError("shadow ingestion attempted a network call")

    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx, "get", explode)
    monkeypatch.setattr(httpx, "post", explode)

    resting_shadow_order(db, oms)
    add_trades(db, "KXA-1", [(1_000_000, 39, 100, "no")])
    assert ShadowFillFeed(oms=oms).poll().recorded == 1


# =========================================================================== #
# Wiring
# =========================================================================== #
def test_the_run_mode_alone_decides_which_path_ingests(oms):
    """One decision point, so the two paths cannot drift apart.

    A LIVE run that silently materialised counterfactuals would report a position
    the exchange never gave it; a SHADOW run that reached for a client would
    break the no-network guarantee.
    """
    assert isinstance(fill_feed_for(RunMode.SHADOW, oms), ShadowFillFeed)
    assert isinstance(fill_feed_for(RunMode.BACKTEST, oms), ShadowFillFeed)
    assert isinstance(
        fill_feed_for(RunMode.LIVE, oms, client=FakeFillVenue()), VenueFillFeed
    )
    assert isinstance(
        fill_feed_for(RunMode.PAPER, oms, client=FakeFillVenue()), VenueFillFeed
    )


def test_a_venue_mode_without_a_client_is_refused_loudly(oms):
    """Failing closed matters here: a LIVE feed that quietly ingested nothing
    would leave every position at zero while real orders filled."""
    with pytest.raises(ValueError, match="needs a venue client"):
        fill_feed_for(RunMode.LIVE, oms)


def test_the_two_feeds_do_not_share_a_high_water_mark(oms):
    """A shadow pass marking the tape it consumed must not move the live resume
    point forward -- that would skip real fills on the next live poll."""
    shadow = ShadowFillFeed(oms=oms)
    live = VenueFillFeed(oms=oms, client=FakeFillVenue())
    assert shadow.high_water_key != live.high_water_key

    shadow.set_high_water(T0 + 999)
    assert live.high_water_us == 0


def test_the_high_water_mark_never_moves_backwards(oms):
    """It is a resume point, not a clock.  Moving it back is harmless; moving it
    forward past unstored work is not, so the only rule enforced is monotonicity."""
    feed = VenueFillFeed(oms=oms, client=FakeFillVenue())
    feed.set_high_water(T0)
    feed.set_high_water(T0 - 5_000_000)
    assert feed.high_water_us == T0


def test_an_empty_poll_is_clean_and_records_nothing(oms):
    report = VenueFillFeed(oms=oms, client=FakeFillVenue([])).poll()
    assert report == IngestReport(high_water_us=0)
    assert report.is_clean


def test_a_default_fee_spec_can_be_forced_for_a_non_kalshi_venue(oms):
    """Backtests over a venue whose series map is not cached still need a rate,
    and it must be the caller's choice rather than an accident of the fallback."""
    feed = VenueFillFeed(oms=oms, default_fee_spec=FeeSpec.polymarket_us())
    assert feed.fee_cents("ANY-1", 50, 100, is_maker=True) == -31


def test_a_fill_timestamp_is_carried_through_to_the_row(oms):
    """Mark-outs are measured FROM the fill, so a wrong timestamp measures the
    passage of time instead of the trade (shadow/engine.py::markout)."""
    coid = resting_live_order(oms)
    venue = FakeFillVenue([venue_fill(created_time="2026-08-26T12:00:00Z")])
    VenueFillFeed(oms=oms, client=venue).poll()
    assert oms.fills_for(coid)[0].filled_at_us == 1_787_745_600_000_000


def test_a_fill_with_no_usable_timestamp_still_lands(oms):
    """Losing the fill because its clock field was unrecognised would be a far
    worse error than stamping it with arrival time."""
    coid = resting_live_order(oms)
    before = now_us()
    venue = FakeFillVenue([{"trade_id": "T-1", "order_id": "V-1",
                            "yes_price": 40, "count": 10}])
    VenueFillFeed(oms=oms, client=venue).poll()
    assert oms.fills_for(coid)[0].filled_at_us >= before
