"""T-011 acceptance: the Kalshi WebSocket feed does not lie about the book.

The money argument for every test in this file is the same one.  The measured
dislocation study found $44.08 of executable edge across 29,130 observations and
**83% of those dislocations lasted a single 5-second poll**.  The REST recorder
polls at 5s, so this socket is the only way to see them.  A socket that silently
drops a delta does not merely miss edge -- it reports a book that is wrong in an
unknown direction, and every quote priced off it is a bet nobody chose to make.
That is strictly worse than having no feed at all, because a missing feed is
visible and a corrupt one is not.

NOTHING HERE TOUCHES THE NETWORK except the single `@pytest.mark.live` test.
`FakeKalshiVenue` is a model of the protocol as MEASURED on the live demo socket
on 2026-08-27, and it deliberately reproduces the four traps that a client
written from the documented shape falls into:

    S1  `seq` is per-SID and shared by every ticker on that subscription.
    S2  control ACKs (`ok`, `unsubscribed`) CONSUME sequence numbers.
    S3  re-subscribing without unsubscribing returns `ok` and NO snapshot.
    S4  `unsubscribe` + `subscribe` is what actually resyncs; it issues a new
        sid and restarts `seq` at 1.

If a future refactor breaks one of those, these tests fail rather than the book.
"""

from __future__ import annotations

import itertools
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest

from venues.kalshi.ws import (
    CH_ORDERBOOK,
    Anomaly,
    Book,
    BookEvent,
    KalshiWSAuthError,
    KalshiWSError,
    KalshiWSFeed,
    NullSink,
    SqliteBookEventSink,
    WebsocketTransport,
    percentiles,
)

# --------------------------------------------------------------------------- #
# Frames captured VERBATIM from wss://external-api-ws.demo.kalshi.co on
# 2026-08-27.  These are the regression anchor for the wire format: if Kalshi
# changes it, the parser must fail here rather than in production, where the
# only symptom is an empty book that reads as "no liquidity".
# --------------------------------------------------------------------------- #
REAL_SUBSCRIBED = '{"type":"subscribed","id":1,"msg":{"channel":"orderbook_delta","sid":1}}'
REAL_SNAPSHOT = (
    '{"type":"orderbook_snapshot","sid":1,"seq":1,"msg":'
    '{"market_ticker":"KXGOLDH-26AUG2711-T4615.99",'
    '"market_id":"ef793c45-beed-4096-8a3a-1c70af894be7",'
    '"yes_dollars_fp":[["0.0100","1410.00"]],'
    '"no_dollars_fp":[["0.0100","100.00"],["0.0200","2500.00"]]}}'
)
REAL_DELTA = (
    '{"type":"orderbook_delta","sid":1,"seq":2,"msg":'
    '{"market_ticker":"KXGOLDH-26AUG2711-T4615.99",'
    '"market_id":"ef793c45-beed-4096-8a3a-1c70af894be7",'
    '"price_dollars":"0.0100","delta_fp":"10.00","side":"yes",'
    '"ts":"2026-08-27T13:56:23.996443Z","ts_ms":1787838983996}}'
)
# The venue's OWN L1 for the same market at the same moment.  It is the
# independent witness that yes_ask = 100 - best_no_bid: the snapshot above has a
# best NO bid of 2c for 2500 contracts, and Kalshi calls that a 98c YES ask for
# 2500.  Verified live on 1,884 such frames with zero disagreements.
REAL_TICKER = (
    '{"type":"ticker","sid":4,"msg":'
    '{"market_id":"ef793c45-beed-4096-8a3a-1c70af894be7",'
    '"market_ticker":"KXGOLDH-26AUG2711-T4615.99","price_dollars":"0.0000",'
    '"yes_bid_dollars":"0.0100","yes_ask_dollars":"0.9800","volume_fp":"0.00",'
    '"open_interest_fp":"0.00","yes_bid_size_fp":"1410.00",'
    '"yes_ask_size_fp":"2500.00","last_trade_size_fp":"0.00",'
    '"ts":1787838990,"ts_ms":1787838990880}}'
)
REAL_OK_ACK = (
    '{"type":"ok","id":2,"sid":1,"seq":108,"msg":{"market_tickers":'
    '["KXGOLDH-26AUG2711-T4615.99","KXGOLDH-26AUG2711-T4617.99"]}}'
)
REAL_ERROR = '{"type":"error","id":3,"msg":{"code":8,"msg":"Unknown channel name"}}'
REAL_EMPTY_SNAPSHOT = (
    '{"type":"orderbook_snapshot","sid":1,"seq":1,"msg":'
    '{"market_ticker":"KXBTCD-25AUG2617-T112999.99","market_id":""}}'
)

GOLD = "KXGOLDH-26AUG2711-T4615.99"


# --------------------------------------------------------------------------- #
# Fixtures: temp dirs.  pytest's `tmp_path` raises PermissionError [WinError 5]
# on this machine, so the directory is managed by hand.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def workdir():
    d = tempfile.mkdtemp(prefix="pm-ws-test-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# A model of the venue, built from measured behaviour.
# --------------------------------------------------------------------------- #
class FakeKalshiVenue:
    """Reproduces the measured Kalshi WS semantics, including its traps.

    It holds the AUTHORITATIVE book, so "the reconstruction equals a fresh
    snapshot" is a real comparison against an independent copy rather than a
    tautology over the same dict.
    """

    def __init__(self, books: dict[str, dict[str, dict[int, float]]]) -> None:
        self.truth = {t: {"yes": dict(v.get("yes", {})), "no": dict(v.get("no", {}))}
                      for t, v in books.items()}
        self.subs: dict[int, dict[str, Any]] = {}
        self.next_sid = 1
        self.outbox: list[str] = []
        self.drop_next = 0            # simulate frames lost in transit
        self.dropped: list[str] = []

    def reset_connection(self) -> None:
        """A new socket is a new subscription namespace.

        Measured: sids are per-connection and a fresh connection issues sid 1
        again with `seq` restarting at 1 -- so a reconnect re-uses the number
        the previous connection was using, which is precisely the case a
        connection-scoped sequence tracker gets wrong.
        """
        self.subs.clear()
        self.next_sid = 1
        self.outbox.clear()

    # ----------------------------------------------------------- delivery
    def _emit(self, sid: int | None, frame: dict[str, Any], *, sequenced: bool = True) -> None:
        if sid is not None and sequenced:
            self.subs[sid]["seq"] += 1
            frame["seq"] = self.subs[sid]["seq"]
        text = json.dumps(frame)
        if self.drop_next > 0 and sequenced and sid is not None:
            # The frame HAPPENED at the venue -- its seq is burned -- but the
            # client never sees it.  That is exactly what a lost delta is.
            self.drop_next -= 1
            self.dropped.append(text)
            return
        self.outbox.append(text)

    def _snapshot_frame(self, sid: int, ticker: str) -> dict[str, Any]:
        book = self.truth[ticker]
        msg: dict[str, Any] = {"market_ticker": ticker, "market_id": f"id-{ticker}"}
        for side in ("yes", "no"):
            levels = {p: s for p, s in book[side].items() if s > 0}
            if levels:
                msg[f"{side}_dollars_fp"] = [
                    [f"{p / 100:.4f}", f"{s:.2f}"] for p, s in sorted(levels.items())
                ]
        return {"type": "orderbook_snapshot", "sid": sid, "msg": msg}

    # ------------------------------------------------------------ commands
    def handle(self, text: str) -> None:
        cmd = json.loads(text)
        params = cmd.get("params") or {}
        name = cmd.get("cmd")
        cid = cmd.get("id")

        if name == "subscribe":
            channel = (params.get("channels") or [CH_ORDERBOOK])[0]
            want = [t for t in params.get("market_tickers") or [] if t in self.truth]
            existing = next((s for s, v in self.subs.items() if v["channel"] == channel), None)
            if existing is None:
                sid = self.next_sid
                self.next_sid += 1
                self.subs[sid] = {"channel": channel, "tickers": set(), "seq": 0}
                # `subscribed` carries NO seq (measured).
                self.outbox.append(json.dumps(
                    {"type": "subscribed", "id": cid, "msg": {"channel": channel, "sid": sid}}))
                for t in want:
                    self.subs[sid]["tickers"].add(t)
                    self._emit(sid, self._snapshot_frame(sid, t))
                return
            # S3: a repeat subscribe is an "add tickers" command.  Already-present
            # tickers get NO snapshot, and the `ok` ACK BURNS A SEQUENCE NUMBER.
            sid = existing
            fresh = [t for t in want if t not in self.subs[sid]["tickers"]]
            self.subs[sid]["tickers"].update(fresh)
            self._emit(sid, {"type": "ok", "id": cid, "sid": sid,
                             "msg": {"market_tickers": sorted(self.subs[sid]["tickers"])}})
            for t in fresh:
                self._emit(sid, self._snapshot_frame(sid, t))
            return

        if name == "unsubscribe":
            for sid in params.get("sids") or []:
                if sid in self.subs:
                    self._emit(sid, {"type": "unsubscribed", "id": cid, "sid": sid})
                    del self.subs[sid]
            return

        if name == "update_subscription":
            sid = (params.get("sids") or [None])[0]
            if sid not in self.subs:
                return
            action = params.get("action")
            tickers = params.get("market_tickers") or []
            if action == "delete_markets":
                self.subs[sid]["tickers"].difference_update(tickers)
            elif action == "add_markets":
                self.subs[sid]["tickers"].update(t for t in tickers if t in self.truth)
            self._emit(sid, {"type": "ok", "id": cid, "sid": sid,
                             "msg": {"market_tickers": sorted(self.subs[sid]["tickers"])}})
            if action == "add_markets":
                # S4: same sid, seq NOT reset, fresh snapshot for that ticker.
                for t in tickers:
                    if t in self.truth:
                        self._emit(sid, self._snapshot_frame(sid, t))
            return

        raise AssertionError(f"venue got an unknown command: {name}")

    # -------------------------------------------------------- market moves
    def move(self, ticker: str, side: str, price: int, delta: float,
             *, ts_ms: int = 1787838983996) -> None:
        """Change the truth and publish the delta on every subscription."""
        book = self.truth[ticker][side]
        book[price] = book.get(price, 0.0) + delta
        if book[price] <= 0:
            book.pop(price)
        for sid, sub in list(self.subs.items()):
            if ticker in sub["tickers"]:
                self._emit(sid, {
                    "type": "orderbook_delta", "sid": sid,
                    "msg": {"market_ticker": ticker, "price_dollars": f"{price / 100:.4f}",
                            "delta_fp": f"{delta:.2f}", "side": side, "ts_ms": ts_ms},
                })

    def truth_book(self, ticker: str) -> Book:
        b = Book(ticker=ticker)
        b.yes = {p: s for p, s in self.truth[ticker]["yes"].items() if s > 0}
        b.no = {p: s for p, s in self.truth[ticker]["no"].items() if s > 0}
        return b


class ScriptedTransport:
    """A `Transport` backed by `FakeKalshiVenue` (or a literal frame list).

    `sessions` lets a test simulate a drop: the first `connect()` serves session
    0, the next serves session 1, so a reconnect is a real re-handshake.
    """

    def __init__(self, venue: FakeKalshiVenue | None = None,
                 frames: list[str] | None = None,
                 *, fail_after: int | None = None) -> None:
        self.venue = venue
        self.queue: list[str] = list(frames or [])
        self.sent: list[str] = []
        self.connects = 0
        self.closes = 0
        self._open = False
        self.fail_after = fail_after
        self._served = 0

    def connect(self) -> None:
        self.connects += 1
        if self.venue is not None and self.connects > 1:
            self.venue.reset_connection()
        self._open = True

    def send(self, text: str) -> None:
        if not self._open:
            raise KalshiWSError("send on a closed transport")
        self.sent.append(text)
        if self.venue is not None:
            self.venue.handle(text)

    def recv(self, timeout: float | None = None) -> str:
        if not self._open:
            raise KalshiWSError("recv on a closed transport")
        if self.fail_after is not None and self._served >= self.fail_after:
            from websockets.exceptions import ConnectionClosedError

            self._open = False
            raise ConnectionClosedError(None, None)
        box = self.venue.outbox if self.venue is not None else self.queue
        if not box:
            raise TimeoutError("no frame")
        self._served += 1
        return box.pop(0)

    def close(self) -> None:
        self.closes += 1
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    # ------------------------------------------------------------- helpers
    def drain(self, feed: KalshiWSFeed, limit: int = 5000) -> list[BookEvent]:
        out: list[BookEvent] = []
        for _ in range(limit):
            try:
                out.extend(feed.pump(0.0))
            except TimeoutError:
                break
        return out


def build_feed(venue: FakeKalshiVenue, tickers: list[str] | None = None,
               **kwargs: Any) -> tuple[KalshiWSFeed, ScriptedTransport]:
    tr = ScriptedTransport(venue)
    feed = KalshiWSFeed(tickers=tickers or sorted(venue.truth),
                        transport=tr, signer=None, **kwargs)
    feed.start()
    return feed, tr


def two_market_venue() -> FakeKalshiVenue:
    return FakeKalshiVenue({
        "A": {"yes": {40: 100.0, 39: 250.0}, "no": {55: 80.0, 54: 300.0}},
        "B": {"yes": {12: 500.0}, "no": {80: 40.0}},
    })


# --------------------------------------------------------------------------- #
# 1. The wire format itself.
# --------------------------------------------------------------------------- #
def test_a_snapshot_in_the_live_dollar_string_format_produces_a_non_empty_book():
    """The live wire uses `yes_dollars_fp`, not the documented `yes`.

    A parser written against the documented integer-cent arrays reads this exact
    frame as an empty book and raises nothing.  Empty books read downstream as
    "no liquidity", so every sleeve simply declines to quote and the whole feed
    looks healthy while recording nothing.  This is the single most expensive
    way this module can fail, so it is the first test.
    """
    feed = KalshiWSFeed(tickers=[GOLD], transport=ScriptedTransport(frames=[]))
    feed.handle_frame(REAL_SNAPSHOT, 1_000)

    book = feed.book(GOLD)
    assert book is not None
    assert book.yes == {1: 1410.0}
    assert book.no == {1: 100.0, 2: 2500.0}
    assert feed.stats.snapshots_without_depth_key == 0


def test_a_snapshot_whose_depth_keys_are_unrecognised_is_counted_not_ignored():
    """The alarm for "Kalshi changed the wire format and we record nothing".

    An untraded market really does snapshot with no arrays, so this cannot be an
    error.  But if the key ever becomes `yes_v3`, EVERY snapshot lands in this
    counter, and a monitor watching the ratio sees the outage on the first frame
    instead of after a day of empty recordings.
    """
    feed = KalshiWSFeed(tickers=[], transport=ScriptedTransport(frames=[]))
    feed.handle_frame(REAL_EMPTY_SNAPSHOT, 1_000)
    assert feed.stats.snapshots == 1
    assert feed.stats.snapshots_without_depth_key == 1

    feed.handle_frame(REAL_SNAPSHOT, 2_000)
    assert feed.stats.snapshots == 2
    assert feed.stats.snapshots_without_depth_key == 1


def test_the_yes_ask_is_the_no_bid_reflected_through_one_hundred():
    """Get this inverted and every ask price is wrong by (100 - 2p).

    The venue's own `ticker` frame is the witness: for the same market at the
    same moment, the snapshot's best NO bid is 2c for 2,500 contracts and Kalshi
    reports `yes_ask_dollars: "0.9800"`, `yes_ask_size_fp: "2500.00"`.  Both
    frames below are verbatim captures.  Verified live over 1,884 ticker frames
    with zero disagreements.
    """
    feed = KalshiWSFeed(tickers=[GOLD], transport=ScriptedTransport(frames=[]))
    feed.handle_frame(REAL_SNAPSHOT, 1_000)
    top = feed.top(GOLD)
    assert top is not None

    venue_l1 = json.loads(REAL_TICKER)["msg"]
    assert top.yes_bid == round(float(venue_l1["yes_bid_dollars"]) * 100) == 1
    assert top.yes_ask == round(float(venue_l1["yes_ask_dollars"]) * 100) == 98
    assert top.yes_bid_size == float(venue_l1["yes_bid_size_fp"]) == 1410.0
    assert top.yes_ask_size == float(venue_l1["yes_ask_size_fp"]) == 2500.0
    assert top.spread_cents == 97

    # The ladder is YES-referenced in both directions: bids down, asks up.
    ladder = feed.book(GOLD).l2(depth=5)
    assert ladder["bids"] == [(1, 1410.0)]
    assert ladder["asks"] == [(98, 2500.0), (99, 100.0)]


def test_a_market_with_no_offers_reports_no_ask_rather_than_an_ask_of_one_hundred():
    """`Market.has_ask` treats 100 as "nobody is offering"; so must this.

    A 100c ask is outside the 1..99 tick grid and outside the fee function's
    0 < p < 1 domain.  Any sleeve that priced straight off it raises ValueError,
    which is how an empty side becomes a crashed strategy loop.
    """
    feed = KalshiWSFeed(tickers=["A"], transport=ScriptedTransport(frames=[]))
    feed.handle_frame(json.dumps({
        "type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "A", "yes_dollars_fp": [["0.4000", "100.00"]]},
    }), 1_000)
    top = feed.top("A")
    assert top is not None
    assert top.yes_ask is None
    assert not top.has_ask
    assert top.has_bid
    assert not top.has_two_sided_quote
    assert top.mid is None


# --------------------------------------------------------------------------- #
# 2. recv_at_us -- the latency dataset G1 asks for.
# --------------------------------------------------------------------------- #
def test_recv_at_us_is_stamped_before_the_frame_is_parsed():
    """Stamping after `json.loads` charges the venue for our own decode cost.

    Every latency percentile then comes out flatter than reality, in our favour,
    and latency assumptions sized off it are optimistic in exactly the regime
    that matters -- the 83% of dislocations that live for under one poll.
    """
    ticks = itertools.count(1_000)

    class StampSpy(KalshiWSFeed):
        def __init__(self, *a: Any, **kw: Any) -> None:
            self.decode_ticks: list[int] = []
            super().__init__(*a, **kw)

        def _decode(self, text: str) -> Any:
            self.decode_ticks.append(self._clock())
            return super()._decode(text)

    tr = ScriptedTransport(frames=[REAL_SNAPSHOT, REAL_DELTA])
    tr.connect()
    feed = StampSpy(tickers=[GOLD], transport=tr, clock=lambda: next(ticks))

    events = feed.pump(0.0)
    assert events
    assert feed.decode_ticks, "the parse seam never ran"
    assert events[0].recv_at_us < feed.decode_ticks[0], (
        "recv_at_us must be taken before parsing, not after"
    )


def test_a_frame_that_fails_to_parse_still_counts_as_traffic_for_the_watchdog():
    """Otherwise a venue emitting garbage looks like a silent venue.

    The silence watchdog would fire a reconnect every 15s forever, and each
    reconnect blacks out every book on the connection.  Garbage is a protocol
    problem; silence is a liveness problem; conflating them turns one outage
    into two.
    """
    feed = KalshiWSFeed(tickers=[], transport=ScriptedTransport(frames=[]))
    before = feed._last_frame_us
    feed.handle_frame("{not json at all", 9_999)
    assert feed.stats.frames == 1
    assert feed._last_frame_us == 9_999 != before
    assert feed.stats.malformed == 1


def test_latency_is_measured_from_the_venue_timestamp_at_its_best_resolution():
    """`ts` (RFC3339, microseconds) beats `ts_ms`, and both beat integer seconds.

    The `ticker` channel sends `ts` as whole SECONDS alongside a millisecond
    `ts_ms`.  Preferring the integer would quantise every latency sample to
    1,000,000us and make a sub-second histogram meaningless -- which is the only
    part of the histogram anyone will act on.
    """
    feed = KalshiWSFeed(tickers=[GOLD], transport=ScriptedTransport(frames=[]))
    feed.handle_frame(REAL_SNAPSHOT, 1_000)
    delta_recv = 1_787_838_984_100_000          # 103.557ms after the venue stamp
    events = feed.handle_frame(REAL_DELTA, delta_recv)

    assert events[0].venue_ts_us == 1_787_838_983_996_443    # from `ts`, not ts_ms
    assert events[0].latency_us == 103_557

    # A ticker frame must use ts_ms (ms), never its integer `ts` (whole seconds).
    tick_events = feed.handle_frame(REAL_TICKER, 1_787_838_990_900_000)
    assert tick_events[0].venue_ts_us == 1_787_838_990_880_000
    assert tick_events[0].latency_us == 20_000


def test_a_snapshot_reports_no_venue_timestamp_because_kalshi_sends_none():
    """Measured: `orderbook_snapshot` carries neither `ts` nor `ts_ms`.

    Inventing one -- say, from the previous delta -- would put a fabricated
    number into the latency histogram that G1 gates on.  None is the honest
    answer, and it means snapshot latency is simply unmeasurable here.
    """
    feed = KalshiWSFeed(tickers=[GOLD], transport=ScriptedTransport(frames=[]))
    events = feed.handle_frame(REAL_SNAPSHOT, 5_000)
    assert events[0].venue_ts_us is None
    assert events[0].latency_us is None
    assert feed.stats.latencies_us == []


def test_percentiles_report_observed_values_and_never_interpolate():
    """An interpolated p99 latency is a number nobody ever observed.

    Latency budgets are decided on observations; a synthetic value between two
    real samples can sit in a gap that the system never actually produces.
    """
    out = percentiles([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    assert out["p50"] == 50
    assert out["p90"] == 90
    assert out["n"] == 10
    assert out["min"] == 10 and out["max"] == 100
    assert all(v in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
               for k, v in out.items() if k.startswith("p"))
    assert percentiles([]) == {}


# --------------------------------------------------------------------------- #
# 3. Sequence gaps.
# --------------------------------------------------------------------------- #
def test_a_dropped_delta_is_detected_as_a_sequence_gap_and_never_absorbed():
    """THE test.  A missed delta corrupts the book from that moment forward.

    Nothing downstream can tell a corrupt book from a real one, so the corrupt
    book gets quoted.  Detection is the entire difference between a stale quote
    we decline to trade and a wrong quote we trade on.
    """
    venue = two_market_venue()
    feed, tr = build_feed(venue, auto_resync=False)
    tr.drain(feed)
    assert feed.stats.gaps == 0

    venue.drop_next = 1                      # one delta is lost in transit
    venue.move("A", "yes", 40, 25.0)
    venue.move("A", "yes", 39, -50.0)
    tr.drain(feed)

    assert venue.dropped, "the venue model did not actually drop anything"
    assert feed.stats.gaps == 1
    assert feed.stats.missed_messages == 1
    gap = next(a for a in feed.anomalies if a.kind == "sequence_gap")
    assert gap.seq_got == gap.seq_expected + 1
    assert feed.stats.gap_rate > 0


def test_a_sequence_gap_makes_every_book_on_that_subscription_unreadable():
    """`seq` is shared across the whole sid, so a gap names no ticker.

    Marking only the ticker on the gap-revealing frame leaves the OTHER books
    readable while one of them is the corrupt one -- and you cannot tell which.
    Quoting any of them is trading on a book you know might be wrong.
    """
    venue = two_market_venue()
    feed, tr = build_feed(venue, auto_resync=False)
    tr.drain(feed)
    assert set(feed.tops()) == {"A", "B"}

    venue.drop_next = 1
    venue.move("A", "yes", 40, 5.0)
    venue.move("B", "yes", 12, 5.0)
    tr.drain(feed)

    assert feed.top("A") is None and feed.top("B") is None
    assert set(feed.stale_tickers()) == {"A", "B"}
    assert feed.top("A", allow_stale=True) is not None, "diagnostics must still work"


def test_deltas_interleaved_across_tickers_on_one_sid_are_not_mistaken_for_gaps():
    """S1: `seq` is per-SID, not per-ticker.

    A per-ticker sequence tracker sees a gap on nearly every message when two
    markets share a subscription, so `sequence_gap_rate` pins at ~1.0 and G1 can
    never be met -- while a real gap is invisible in the noise.
    """
    venue = two_market_venue()
    feed, tr = build_feed(venue, auto_resync=False)
    tr.drain(feed)

    for i in range(20):
        venue.move("A" if i % 2 == 0 else "B", "yes", 40 if i % 2 == 0 else 12, 1.0)
    tr.drain(feed)

    assert feed.stats.gaps == 0
    assert feed.stats.gap_rate == 0.0
    assert feed.stats.sequenced >= 20


def test_a_control_acknowledgement_consumes_a_sequence_number_without_faking_a_gap():
    """S2, measured: `ok` arrived at seq 108 and the next snapshot at seq 109.

    A detector that only sequences book messages reports a phantom gap every
    time the subscription is modified -- and a resync follows each one, blacking
    out every book on the connection for no reason.  Phantom gaps also inflate
    `sequence_gap_rate` past G1's 0.001 threshold, blocking the gate on a bug.
    """
    venue = two_market_venue()
    feed, tr = build_feed(venue, ["A"], auto_resync=False)
    tr.drain(feed)

    feed.subscribe(["A", "B"])               # repeat subscribe -> `ok` + snapshot
    events = tr.drain(feed)

    kinds = [e.payload.get("type") for e in events]
    assert "ok" in kinds, "the venue model did not send the ACK that burns a seq"
    assert feed.stats.gaps == 0
    assert feed.stats.gap_rate == 0.0
    assert feed.top("B") is not None


def test_a_repeated_sequence_number_is_reported_as_a_regression_not_a_gap():
    """A replay or reorder is not lost data, but it is never silent either.

    Counting it as a gap would trigger a pointless resync; ignoring it would
    hide a genuine venue-side or proxy-side fault.
    """
    feed = KalshiWSFeed(tickers=["A"], transport=ScriptedTransport(frames=[]),
                        auto_resync=False)
    for seq in (1, 2, 2):
        feed.handle_frame(json.dumps({
            "type": "orderbook_snapshot", "sid": 1, "seq": seq,
            "msg": {"market_ticker": "A", "yes_dollars_fp": [["0.4000", "10.00"]]},
        }), 1_000 + seq)

    assert feed.stats.gaps == 0
    assert [a.kind for a in feed.anomalies] == ["seq_regression"]


def test_the_gap_rate_denominator_counts_only_frames_that_carry_a_sequence():
    """S5: the `ticker` channel sends no `seq` at all.

    Counting unsequenced frames in the denominator dilutes the rate towards
    zero, so `sequence_gap_rate < 0.001` could be met by subscribing to a chatty
    unsequenced channel.  A gate that can be passed by adding noise is not a gate.
    """
    feed = KalshiWSFeed(tickers=[GOLD], transport=ScriptedTransport(frames=[]),
                        auto_resync=False)
    feed.handle_frame(REAL_SNAPSHOT, 1_000)
    for _ in range(50):
        feed.handle_frame(REAL_TICKER, 2_000)      # unsequenced

    assert feed.stats.frames == 51
    assert feed.stats.sequenced == 1
    assert feed.stats.gap_rate == 0.0


# --------------------------------------------------------------------------- #
# 4. Resync.
# --------------------------------------------------------------------------- #
def test_a_gap_triggers_a_resync_that_unsubscribes_first_because_resubscribing_alone_does_nothing():
    """S3, measured: a bare re-subscribe returns `ok` and NO snapshot.

    "On a gap, just resubscribe" is the recipe most Kalshi examples give and it
    is a SILENT NO-OP: the corrupt book stays in place, the resync counter goes
    up, and the system reports that it healed itself.  Only
    unsubscribe-then-subscribe issues a new sid and a fresh snapshot.
    """
    venue = two_market_venue()
    feed, tr = build_feed(venue)
    tr.drain(feed)
    tr.sent.clear()

    venue.drop_next = 1
    venue.move("A", "yes", 40, 10.0)
    venue.move("A", "yes", 40, 10.0)
    tr.drain(feed)

    cmds = [json.loads(s)["cmd"] for s in tr.sent]
    assert "unsubscribe" in cmds, "resync must unsubscribe -- resubscribe alone is a no-op"
    assert cmds.index("unsubscribe") < cmds.index("subscribe")
    assert feed.stats.resyncs == 1


def test_a_resync_rebuilds_the_book_to_match_the_venues_own_state():
    """T-011 acceptance: the reconstructed book equals a fresh snapshot.

    The venue model holds the authoritative book, so this compares against an
    independent copy rather than restating our own arithmetic.  If a resync left
    the local book even one level off, every subsequent delta compounds the error
    and the divergence is permanent.
    """
    venue = two_market_venue()
    feed, tr = build_feed(venue)
    tr.drain(feed)

    venue.drop_next = 2
    for _ in range(4):
        venue.move("A", "yes", 40, 7.0)
        venue.move("B", "no", 80, -5.0)
    tr.drain(feed)
    assert feed.stats.gaps >= 1
    tr.drain(feed)                            # let the resync snapshots land

    assert feed.stats.resyncs >= 1
    for t in ("A", "B"):
        assert feed.books[t].levels_equal(venue.truth_book(t)), (
            f"{t}: rebuilt {feed.books[t].yes}/{feed.books[t].no} != "
            f"venue {venue.truth_book(t).yes}/{venue.truth_book(t).no}"
        )
        assert feed.top(t) is not None, "a rebuilt book must be readable again"


def test_a_resync_issues_a_new_subscription_id_whose_sequence_restarts_cleanly():
    """S4: the fresh sid starts at seq 1, and that must not read as a gap.

    A tracker keyed on the connection instead of the sid sees 2,563 -> 1 and
    logs a huge gap on every single resync, which both hides real gaps and makes
    the resync look like it caused the damage it repaired.
    """
    venue = two_market_venue()
    feed, tr = build_feed(venue)
    tr.drain(feed)
    old_sid = next(iter(feed.subscriptions))

    venue.drop_next = 1
    venue.move("A", "yes", 40, 1.0)
    venue.move("A", "yes", 40, 1.0)
    tr.drain(feed)
    tr.drain(feed)

    gaps_after_resync = feed.stats.gaps
    new_sids = set(feed.subscriptions) - {old_sid}
    assert new_sids, "resync did not produce a new sid"
    assert old_sid not in feed.subscriptions

    for _ in range(10):
        venue.move("A", "yes", 40, 1.0)
    tr.drain(feed)
    assert feed.stats.gaps == gaps_after_resync, "seq restarting at 1 was read as a gap"


def test_a_delta_that_drives_a_level_negative_is_reported_as_corruption_not_clamped():
    """Clamping to zero produces a book that looks plausible and is wrong.

    A negative level means our copy already diverged from the venue's in a way
    the sequence numbers did not catch.  Clamping hides that permanently; the
    honest response is to declare the book unusable and rebuild just that one.
    """
    venue = two_market_venue()
    feed, tr = build_feed(venue)
    tr.drain(feed)
    tr.sent.clear()

    corrupt = json.dumps({
        "type": "orderbook_delta", "sid": next(iter(feed.subscriptions)),
        "seq": feed.subscriptions[next(iter(feed.subscriptions))].last_seq + 1,
        "msg": {"market_ticker": "A", "price_dollars": "0.4000",
                "delta_fp": "-9999.00", "side": "yes"},
    })
    feed.handle_frame(corrupt, 1_000)

    assert feed.stats.book_corruptions == 1
    assert feed.top("A") is None
    assert any(a.kind == "book_corruption" for a in feed.anomalies)
    # Single-book damage uses the per-ticker resync, not the whole-sid one:
    # blacking out every other book because one went bad is self-inflicted loss.
    cmds = [json.loads(s) for s in tr.sent]
    actions = [c["params"].get("action") for c in cmds if c["cmd"] == "update_subscription"]
    assert actions == ["delete_markets", "add_markets"]
    assert not any(c["cmd"] == "unsubscribe" for c in cmds)


def test_a_delta_arriving_before_any_snapshot_is_refused_rather_than_applied():
    """Applying it invents depth the venue never sent.

    The phantom level then reads as real liquidity, and a sleeve sizes against
    contracts that do not exist.
    """
    feed = KalshiWSFeed(tickers=["A"], transport=ScriptedTransport(frames=[]),
                        auto_resync=False)
    feed.handle_frame(json.dumps({
        "type": "orderbook_delta", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "A", "price_dollars": "0.4000",
                "delta_fp": "100.00", "side": "yes"},
    }), 1_000)

    assert feed.books["A"].yes == {}
    assert feed.stats.deltas_before_snapshot == 1
    assert feed.stats.deltas == 0
    assert feed.top("A") is None
    assert any(a.kind == "delta_before_snapshot" for a in feed.anomalies)


# --------------------------------------------------------------------------- #
# 5. Reconstruction equals a fresh snapshot.
# --------------------------------------------------------------------------- #
def test_a_book_built_from_a_snapshot_plus_deltas_equals_a_freshly_taken_snapshot():
    """The core correctness claim of the whole module (PLAN.md 6.1 acceptance).

    Every price this system acts on comes out of this arithmetic.  If applying
    200 deltas leaves the book one contract off, the error never self-corrects
    -- it compounds until the next snapshot, and the size we think is resting at
    the touch is not the size that is there.
    """
    venue = two_market_venue()
    feed, tr = build_feed(venue)
    tr.drain(feed)

    # Every cycle keeps each level non-negative and includes a level that is
    # created and then removed, so removal is exercised 25 times over.
    moves = [("A", "yes", 40, 5.0), ("A", "yes", 41, 60.0), ("A", "yes", 41, -60.0),
             ("A", "no", 55, 8.0), ("A", "no", 53, 15.0), ("B", "yes", 12, 2.0),
             ("B", "yes", 11, 9.0), ("B", "no", 80, 20.0)]
    for _ in range(25):
        for t, side, price, delta in moves:
            venue.move(t, side, price, delta)
    tr.drain(feed)

    assert feed.stats.deltas == 200
    assert feed.stats.gaps == 0
    for t in ("A", "B"):
        assert feed.books[t].levels_equal(venue.truth_book(t))

    # And it must survive the round trip through an actual fresh snapshot.
    feed.resync_ticker("A", "equality check")
    tr.drain(feed)
    assert feed.books["A"].levels_equal(venue.truth_book("A"))


def test_a_level_driven_to_exactly_zero_is_removed_rather_than_left_as_a_ghost():
    """A 0-size level at the touch reads as a price with no size behind it.

    `best_yes_bid` must skip it, or the touch price is right and the depth is a
    lie -- which is the input to every queue-position and fill estimate.
    """
    venue = FakeKalshiVenue({"A": {"yes": {40: 100.0, 39: 50.0}, "no": {}}})
    feed, tr = build_feed(venue)
    tr.drain(feed)

    venue.move("A", "yes", 40, -100.0)
    tr.drain(feed)

    assert 40 not in feed.books["A"].yes
    top = feed.top("A")
    assert top is not None and top.yes_bid == 39
    assert feed.books["A"].levels_equal(venue.truth_book("A"))


# --------------------------------------------------------------------------- #
# 6. Reconnect.
# --------------------------------------------------------------------------- #
def test_a_reconnect_loses_no_ticker_and_duplicates_no_book_state():
    """A reconnect must be a rebuild, never a re-application.

    Losing a ticker means silently recording nothing for it; duplicating book
    state means doubled displayed depth, which makes every fill estimate
    optimistic in exactly the direction that loses money.
    """
    venue = two_market_venue()
    tr = ScriptedTransport(venue)
    feed = KalshiWSFeed(tickers=["A", "B"], transport=tr, signer=None)
    feed.start()
    tr.drain(feed)
    for _ in range(6):
        venue.move("A", "yes", 40, 10.0)
    tr.drain(feed)
    assert feed.books["A"].levels_equal(venue.truth_book("A"))

    feed._reconnect("simulated drop")
    assert feed.top("A") is None, "books must be unreadable while reconnecting"
    assert not tr.is_open

    feed._connect()
    feed.subscribe()
    tr.drain(feed)

    assert tr.connects == 2

    assert set(feed.tops()) == {"A", "B"}, "a ticker was lost across the reconnect"
    for t in ("A", "B"):
        assert feed.books[t].levels_equal(venue.truth_book(t)), (
            "book state was duplicated or lost across the reconnect"
        )
    assert feed.stats.gaps == 0, "the reconnect itself must not register as a gap"
    # The venue's own `ok`/`subscribed` view proves nothing was dropped.
    subscribed = set().union(*(s.tickers for s in feed.subscriptions.values()))
    assert subscribed == {"A", "B"}


def test_a_reconnected_trade_stream_deduplicates_on_the_venues_own_trade_id():
    """Identity, not sequence -- the same argument `execution/fillfeed.py` makes.

    A reconnect plus a REST backfill re-delivers a window of prints.  Double
    counting them biases Kyle's lambda and every realised-spread estimate, and
    those are the numbers that decide whether a sleeve is allowed real capital.
    """
    feed = KalshiWSFeed(tickers=["A"], transport=ScriptedTransport(frames=[]))
    frame = json.dumps({
        "type": "trade", "sid": 2,
        "msg": {"market_ticker": "A", "trade_id": "t-1", "yes_price_dollars": "0.4000",
                "count_fp": "10.00", "taker_side": "yes", "ts_ms": 1787838983996},
    })
    first = feed.handle_frame(frame, 1_000)
    second = feed.handle_frame(frame, 2_000)

    assert len(first) == 1 and first[0].kind == "trade"
    assert second == [], "a replayed print must not be emitted twice"
    assert feed.stats.trades == 1
    assert feed.stats.duplicate_trades == 1


def test_every_drop_into_a_degraded_state_is_recorded_with_a_reason():
    """PLAN.md 6.1: a recorder must never quietly become a worse recorder.

    A feed that degrades silently keeps reporting healthy counters while the
    data behind them thins out, and the gap only surfaces when a backtest is run
    over a hole nobody knew about.
    """
    feed = KalshiWSFeed(tickers=["A"], transport=ScriptedTransport(frames=[]))
    assert feed.stats.degraded == []

    feed._degrade("socket read failed")
    assert feed.state == "degraded"
    assert len(feed.stats.degraded) == 1
    assert "socket read failed" in feed.stats.degraded[0][1]
    assert any(a.kind == "degraded" for a in feed.anomalies)


def test_the_feed_opens_exactly_one_connection_no_matter_how_many_tickers():
    """Kalshi rate-limits CONCURRENT CONNECTIONS, not just messages.

    A connection per ticker does not merely run slowly -- it gets the account
    throttled or cut off, taking down the market-data feed for every strategy at
    once.  The ticker list is batched onto one socket by construction.
    """
    venue = FakeKalshiVenue({f"T{i}": {"yes": {40: 10.0}, "no": {}} for i in range(50)})
    tr = ScriptedTransport(venue)
    feed = KalshiWSFeed(tickers=sorted(venue.truth), transport=tr, signer=None)
    feed.start()
    tr.drain(feed)

    assert tr.connects == 1
    subscribes = [json.loads(s) for s in tr.sent if json.loads(s)["cmd"] == "subscribe"]
    assert len(subscribes) == 1
    assert len(subscribes[0]["params"]["market_tickers"]) == 50
    assert len(feed.tops()) == 50


# --------------------------------------------------------------------------- #
# 7. Malformed input.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("{not json", "unparseable_json"),
        ("[1, 2, 3]", "not_an_object"),
        ('{"sid": 1, "seq": 2}', "missing_type"),
        ('{"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {}}', "missing_ticker"),
        ('{"type": "orderbook_delta", "sid": 1, "seq": 1, "msg": {}}', "missing_ticker"),
        ('{"type": "brand_new_channel_v9", "sid": 1, "msg": {}}', "unknown_type"),
    ],
)
def test_a_malformed_frame_is_always_reported_and_never_silently_skipped(raw, kind):
    """`except Exception: continue` in a feed loop is how data disappears.

    Each of these is either lost information or a protocol change.  Absorbed
    quietly, the first symptom is a backtest over a period whose book was subtly
    wrong -- and by then the recording cannot be redone.
    """
    seen: list[Anomaly] = []
    feed = KalshiWSFeed(tickers=[], transport=ScriptedTransport(frames=[]),
                        on_anomaly=seen.append)
    feed.handle_frame(raw, 1_000)

    assert [a.kind for a in seen] == [kind]
    assert seen[0].raw, "the offending frame must be kept for diagnosis"
    assert feed.stats.frames == 1


def test_an_error_frame_from_the_venue_is_surfaced_with_its_code():
    """Measured: subscribing to `ticker_v2` answers code 8, Unknown channel name.

    Swallowed, that is a subscription that silently never delivers -- the
    recorder runs for hours convinced it is recording a channel it never got.
    """
    seen: list[Anomaly] = []
    feed = KalshiWSFeed(tickers=[], transport=ScriptedTransport(frames=[]),
                        on_anomaly=seen.append)
    feed.handle_frame(REAL_ERROR, 1_000)

    assert feed.stats.venue_errors == 1
    assert seen and seen[0].kind == "venue_error"
    assert "code=8" in seen[0].detail and "Unknown channel name" in seen[0].detail


def test_a_delta_with_an_unusable_side_or_price_is_refused_rather_than_guessed():
    """Guessing the side flips the book; guessing the price moves the wrong level.

    Both produce a book that parses cleanly and is wrong, which is the failure
    mode this whole module exists to prevent.
    """
    venue = FakeKalshiVenue({"A": {"yes": {40: 100.0}, "no": {}}})
    feed, tr = build_feed(venue, auto_resync=False)
    tr.drain(feed)
    sid = next(iter(feed.subscriptions))
    seq = feed.subscriptions[sid].last_seq

    for i, msg in enumerate([
        {"market_ticker": "A", "price_dollars": "0.4000", "delta_fp": "5.00", "side": "maybe"},
        {"market_ticker": "A", "price_dollars": "oops", "delta_fp": "5.00", "side": "yes"},
        {"market_ticker": "A", "price_dollars": "0.4000", "delta_fp": None, "side": "yes"},
    ], start=1):
        feed.handle_frame(json.dumps(
            {"type": "orderbook_delta", "sid": sid, "seq": seq + i, "msg": msg}), 1_000 + i)

    assert feed.books["A"].yes == {40: 100.0}, "a refused delta must not touch the book"
    assert feed.stats.book_corruptions == 3
    assert sum(1 for a in feed.anomalies if a.kind == "book_corruption") == 3


def test_a_strict_feed_raises_on_the_first_anomaly_for_use_in_acceptance_runs():
    """Production counts and continues; an acceptance run must stop and be looked at.

    Both behaviours are needed, and having only the tolerant one means a broken
    protocol assumption can be papered over by a rising counter nobody reads.
    """
    feed = KalshiWSFeed(tickers=[], transport=ScriptedTransport(frames=[]), strict=True)
    with pytest.raises(KalshiWSError, match="unparseable_json"):
        feed.handle_frame("{nope", 1_000)


# --------------------------------------------------------------------------- #
# 8. Auth and degradation.
# --------------------------------------------------------------------------- #
def test_an_unauthorised_handshake_raises_a_diagnostic_instead_of_retrying_forever():
    """Measured: the PRODUCTION socket answers 401 to a demo key.

    Backing off and retrying a 401 produces a process that looks alive, burns
    connection quota, and records nothing -- for as long as nobody reads the
    logs.  An unauthorised socket never becomes authorised, so this must be
    fatal and it must say what to change.
    """
    class RefusingTransport(ScriptedTransport):
        def connect(self) -> None:
            raise KalshiWSAuthError(401, '{"code":"authentication_error"}')

    feed = KalshiWSFeed(tickers=["A"], transport=RefusingTransport(frames=[]))
    with pytest.raises(KalshiWSAuthError) as exc:
        feed.start()

    text = str(exc.value)
    assert "401" in text
    assert "KALSHI_KEY_ID" in text and "clock" in text
    assert "demo.kalshi.co" in text, "the message must name the host that does work"


def test_the_run_loop_never_retries_an_auth_failure():
    """The same rule, enforced where it actually bites: inside the reconnect loop.

    A 401 surfacing mid-run means credentials were revoked or the clock drifted.
    Looping on it hides an outage behind a healthy-looking process.
    """
    class RefusingTransport(ScriptedTransport):
        def connect(self) -> None:
            self.connects += 1
            raise KalshiWSAuthError(401, "")

    tr = RefusingTransport(frames=[])
    feed = KalshiWSFeed(tickers=["A"], transport=tr, transport_factory=lambda: tr)
    with pytest.raises(KalshiWSAuthError):
        feed.run(duration_s=5.0)
    assert tr.connects == 1, "an auth failure must not be retried even once"


def test_a_stale_book_is_never_returned_as_a_tradeable_quote():
    """The single most important read-side invariant in this module.

    Every other guarantee here is worthless if a caller can still fetch the
    corrupt book.  `top()` returning None forces the caller to handle it;
    returning a quote lets a stale book quietly become an order.
    """
    venue = two_market_venue()
    feed, tr = build_feed(venue)
    tr.drain(feed)
    assert feed.top("A") is not None

    feed.books["A"].mark_stale("test")
    assert feed.top("A") is None
    assert "A" not in feed.tops()
    assert feed.top("A", allow_stale=True) is not None
    assert feed.stale_tickers()["A"] == "test"


def test_a_book_top_converts_into_the_same_market_type_the_rest_path_produces():
    """One type for both feeds, or every sleeve needs a second code path.

    A second path is a second place for the YES-reference convention to be got
    wrong, and that convention is load-bearing for every RV sleeve.
    """
    feed = KalshiWSFeed(tickers=[GOLD], transport=ScriptedTransport(frames=[]))
    feed.handle_frame(REAL_SNAPSHOT, 1_000)
    market = feed.top(GOLD).to_market(series_ticker="KXGOLDH")

    assert market.ticker == GOLD
    assert market.yes_bid == 1 and market.yes_ask == 98
    assert market.has_two_sided_quote
    assert market.spread_cents == 97
    assert market.series_ticker == "KXGOLDH"


# --------------------------------------------------------------------------- #
# 9. Persistence.
# --------------------------------------------------------------------------- #
def test_the_sqlite_sink_refuses_to_write_into_the_recorders_database(workdir):
    """`data/pm.db` is held open by the live REST recorder and has no book_events.

    Creating a table under a running writer is how a recording session dies with
    "database is locked" -- and the recording that is lost is unrepeatable,
    because the market has moved on.
    """
    with pytest.raises(ValueError, match="refusing to write"):
        SqliteBookEventSink(workdir / "pm.db")

    sink = SqliteBookEventSink(workdir / "pm.db", allow_shared_db=True)
    assert sink.count() == 0
    sink.close()


def test_persisted_book_events_keep_the_control_frames_that_consume_sequence_numbers(workdir):
    """A replay without them re-derives gaps that never happened.

    PLAN.md 6.7 replays `book_events` to backtest.  If the archive omits the
    `ok` ACK that burned seq 108, the replay sees 107 -> 109 and concludes the
    recording was lossy -- so a clean dataset gets thrown away, or a dirty one
    gets trusted, depending on which way the reader guesses.
    """
    venue = two_market_venue()
    sink = SqliteBookEventSink(workdir / "book_events.db", flush_every=1)
    tr = ScriptedTransport(venue)
    feed = KalshiWSFeed(tickers=["A"], transport=tr, signer=None, sink=sink)
    feed.start()
    tr.drain(feed)
    feed.subscribe(["A", "B"])               # produces the `ok` ACK
    tr.drain(feed)
    sink.flush()

    conn = sqlite3.connect(str(workdir / "book_events.db"))
    rows = conn.execute("SELECT kind, seq, ticker, payload FROM book_events "
                        "ORDER BY id").fetchall()
    conn.close()
    sink.close()

    kinds = [r[0] for r in rows]
    assert "control" in kinds and "snapshot" in kinds
    acks = [r for r in rows if json.loads(r[3])["type"] == "ok"]
    assert acks, "the sequence-consuming ACK was not archived"
    assert acks[0][1] is not None, "an archived ACK must keep its seq"

    # Every sequenced row replays without a hole.
    seqs = [r[1] for r in rows if r[1] is not None]
    assert seqs == list(range(min(seqs), max(seqs) + 1))


def test_a_persisted_book_event_keeps_the_arrival_stamp_and_the_venue_stamp(workdir):
    """`recv_at_us` is the latency dataset; `venue_ts_us` is the other end of it.

    Recording only one of them makes the histogram unbuildable after the fact,
    and the recording cannot be redone.
    """
    sink = SqliteBookEventSink(workdir / "be.db", flush_every=1)
    feed = KalshiWSFeed(tickers=[GOLD], transport=ScriptedTransport(frames=[]), sink=sink)
    feed.handle_frame(REAL_SNAPSHOT, 111_000)
    feed.handle_frame(REAL_DELTA, 1_787_838_984_100_000)
    sink.flush()

    conn = sqlite3.connect(str(workdir / "be.db"))
    rows = conn.execute(
        "SELECT recv_at_us, venue_ts_us, venue, ticker, kind FROM book_events ORDER BY id"
    ).fetchall()
    conn.close()
    sink.close()

    assert rows[0] == (111_000, None, "kalshi", GOLD, "snapshot")
    assert rows[1] == (1_787_838_984_100_000, 1_787_838_983_996_443, "kalshi", GOLD, "delta")


def test_the_default_sink_stores_nothing_so_the_feed_is_usable_without_a_database():
    """A feed that requires a DB cannot be used to answer a quick question.

    It also means a diagnostic run competes for the same file the recorder is
    writing, which is the one thing this task must not do.
    """
    feed = KalshiWSFeed(tickers=[GOLD], transport=ScriptedTransport(frames=[]))
    assert isinstance(feed.sink, NullSink)
    feed.handle_frame(REAL_SNAPSHOT, 1_000)
    assert feed.sink.written == 1


# --------------------------------------------------------------------------- #
# 10. Live.  Skipped by `-m "not live"`.
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_the_demo_socket_streams_a_reconstructable_book_and_survives_an_injected_gap():
    """T-011 acceptance against the real venue.

    PRODUCTION returns 401 to this key, so demo is the only socket we can prove
    anything on.  Its prices are synthetic (PLAN.md 7.2) -- this test claims
    nothing about them, only that the PROTOCOL handling is right.
    """
    import time

    from core.config import load_settings
    from venues.kalshi.client import DEMO_BASE, DEMO_WS, KalshiClient

    settings = load_settings()
    if not settings.kalshi.is_complete:
        pytest.skip(f"no credentials: {settings.kalshi.describe()}")

    with KalshiClient(base_url=DEMO_BASE) as client:
        data = client._request("GET", "/markets",
                               params={"limit": 200, "status": "open", "mve_filter": "exclude"})
        tickers = [m["ticker"] for m in data.get("markets", [])][:10]
    if not tickers:
        pytest.skip("demo exchange has no open markets right now")

    feed = KalshiWSFeed(url=DEMO_WS, signer=settings.kalshi.signer(), tickers=tickers)
    try:
        feed.start()
        # Snapshots carry no venue timestamp, so keep pumping past them: the
        # latency dataset only exists once deltas start arriving.
        deadline = time.time() + 10
        while time.time() < deadline and not (
            len(feed.tops()) == len(tickers) and feed.stats.deltas >= 20
        ):
            try:
                feed.pump(0.5)
            except TimeoutError:
                pass

        assert feed.stats.snapshots >= 1, "no snapshot arrived"
        assert len(feed.tops()) == len(tickers), f"stale: {feed.stale_tickers()}"
        assert feed.stats.malformed == 0
        assert feed.stats.snapshots_without_depth_key < len(tickers), (
            "every snapshot lacked a recognised depth key -- the wire format moved"
        )
        assert feed.stats.gap_rate < 0.001, f"G1 threshold breached: {feed.stats.report()}"
        if feed.stats.deltas:
            assert feed.stats.latencies_us, (
                "deltas arrived but carried no venue timestamp -- the latency "
                "dataset G1 gates on cannot be built from this feed"
            )

        # Inject a gap by rewinding the tracker, exactly as if a delta were lost.
        sid = next(iter(feed.subscriptions))
        feed.subscriptions[sid].last_seq -= 5
        deadline = time.time() + 12
        while time.time() < deadline and (feed.stats.gaps == 0 or feed.stale_tickers()):
            try:
                feed.pump(0.5)
            except TimeoutError:
                pass

        assert feed.stats.gaps >= 1, "an injected gap was absorbed silently"
        assert feed.stats.resyncs >= 1
        assert set(feed.subscriptions) != {sid}, "resync did not produce a new sid"
        assert not feed.stale_tickers(), (
            f"books never recovered from the resync: {feed.stale_tickers()}"
        )
    finally:
        feed.close()


@pytest.mark.live
def test_the_production_socket_still_refuses_this_account():
    """Documents the access boundary as a test, so a change is noticed.

    The day production starts accepting us, this fails and the recorder can move
    off synthetic demo prices onto the real book -- which is the only place the
    measured $44.08 of dislocation actually exists.
    """
    from core.config import load_settings
    from venues.kalshi.client import PROD_WS

    settings = load_settings()
    if not settings.kalshi.is_complete:
        pytest.skip(f"no credentials: {settings.kalshi.describe()}")

    transport = WebsocketTransport(PROD_WS, signer=settings.kalshi.signer())
    with pytest.raises(KalshiWSAuthError) as exc:
        transport.connect()
    assert exc.value.status == 401
