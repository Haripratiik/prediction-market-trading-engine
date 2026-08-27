"""Kalshi WebSocket market-data feed.  T-011.

WHY THIS EXISTS
---------------
The REST recorder (`recorder/l1.py`) polls top-of-book every 5 seconds.  The
measured dislocation study found $44.08 of executable edge across 29,130
observations and **83% of those dislocations lasted a single 5-second poll**.
A 5s poller is therefore structurally blind to the only thing that exists: it
sees an event that may already be gone, and it cannot see the ones that opened
and closed between two polls at all.  This module is the only path to that data.

Everything here is built for two properties, in this order:

  1. NOT MISSING EVENTS.  A silently corrupted book is worse than no book,
     because it reads as tradeable.  Gaps are detected, counted, and resynced.
  2. LATENCY.  `recv_at_us` is stamped before any parsing, so the latency
     dataset G1 asks for is honest rather than reconstructed.

MEASURED PROTOCOL TRUTH (verified live against the DEMO socket 2026-08-27)
--------------------------------------------------------------------------
The live wire format does NOT match the shape most Kalshi examples show, and a
parser written against the documented `yes`/`no` integer-cent arrays reads
EMPTY BOOKS from it without raising anything.  Captured verbatim:

    {"type":"subscribed","id":1,"msg":{"channel":"orderbook_delta","sid":1}}
    {"type":"orderbook_snapshot","sid":1,"seq":1,"msg":{
        "market_ticker":"KXGOLDH-26AUG2711-T4611.99","market_id":"d3f4...",
        "yes_dollars_fp":[["0.0100","1360.00"]],
        "no_dollars_fp":[["0.0100","100.00"],["0.0200","2500.00"]]}}
    {"type":"orderbook_delta","sid":1,"seq":6,"msg":{
        "market_ticker":"KXGOLDH-26AUG2711-T4611.99","market_id":"d3f4...",
        "price_dollars":"0.0100","delta_fp":"10.00","side":"yes",
        "ts":"2026-08-27T13:56:23.996443Z","ts_ms":1787838983996}}

So: FIXED-POINT DOLLAR STRINGS on the wire (`yes_dollars_fp`, `no_dollars_fp`,
`price_dollars`, `delta_fp`), exactly like the V2 REST surface.  The parser
dispatches on KEY NAME, never on value type, and `snapshots_without_depth_key`
counts snapshots whose depth arrays used no key this module recognises -- that
counter is the alarm for "the protocol moved and we are now recording nothing".

Seven more things measured on the wire, each of which breaks a naive client:

  S1  **`seq` is PER-SID, not per-ticker.**  One subscription covering 40
      tickers shares one counter: snapshots came back seq 1,2,3... and deltas
      for different tickers interleaved on the same counter.  A per-ticker
      sequence tracker sees a gap on essentially every message.  It also means
      a gap invalidates EVERY ticker on that sid, because you cannot know whose
      delta went missing.

  S2  **Control ACKs consume sequence numbers.**  Measured: `{"type":"ok",
      "id":2,"sid":1,"seq":108,...}` followed by `orderbook_snapshot seq:109`.
      A gap detector that only counts book messages reports a phantom gap every
      time a subscription is modified.  `subscribed` itself carries NO seq.

  S3  **Re-subscribing does NOT resend a snapshot.**  Sending `subscribe` again
      for tickers already on the sid returns `{"type":"ok",...,"msg":
      {"market_tickers":[...]}}` and nothing else -- the seq counter marches on
      with no snapshot.  The "on a gap, just resubscribe" recipe is a SILENT
      NO-OP that leaves the corrupt book in place forever.

  S4  The resync primitives that actually work, both verified:
        per sid    `unsubscribe{sids:[S]}` then `subscribe{...}` -> a NEW sid,
                   `seq` RESET to 1, and a fresh snapshot for every ticker.
        per ticker `update_subscription{action:"delete_markets"}` then
                   `{action:"add_markets"}` -> a fresh snapshot for that ticker
                   only, on the SAME sid, with seq NOT reset.
      A seq gap must use the per-sid form (you do not know which ticker was
      hit).  Single-book corruption uses the per-ticker form.

  S5  **Only some channels carry `seq` at all.**  `orderbook_delta` does;
      `ticker` frames arrive as `{"type":"ticker","sid":4,"msg":{...}}` with no
      seq field.  Requiring seq on every frame breaks on the first ticker frame.

  S6  `ticker_v2` does not exist on this deployment: it answers
      `{"type":"error","id":3,"msg":{"code":8,"msg":"Unknown channel name"}}`.
      `orderbook_delta`, `trade`, `ticker` and `market_lifecycle_v2` all
      subscribe successfully.

  S7  **Venue timestamps are microsecond-resolution.**  `msg.ts` is RFC3339
      with 6 fractional digits and `msg.ts_ms` is epoch milliseconds; the two
      agreed to 0.6ms over 6,427 frames.  A real latency histogram is therefore
      possible.  `orderbook_snapshot` carries NEITHER, so snapshot latency is
      unmeasurable and `venue_ts_us` is None for them.

MEASURED ACCESS
---------------
    PROD  `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
          -> HTTP 401 {"code":"authentication_error","details":"NOT_FOUND"}
             with a valid demo signature.  Needs a funded prod account.
    DEMO  `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`
          -> connects in ~300ms, subscribes, streams.  WORKS TODAY, even though
             `POST /portfolio/events/orders` on the same demo account returns
             `user_not_found`.  Market data and trading are provisioned
             separately; do not infer one from the other.

A 401 raises `KalshiWSAuthError` carrying the remedy text rather than retrying
into a backoff loop forever -- an unauthorised socket never becomes authorised.

RATE LIMIT: ONE CONNECTION.  Kalshi limits concurrent WS connections, not just
messages.  One connection carries every ticker via batched `market_tickers`
lists; there is no per-ticker connect path in this module by construction.

LAYERING
--------
    Transport   raw frames in/out.  `WebsocketTransport` is the only piece that
                touches a socket; tests inject a scripted one.
    Feed        protocol, sequencing, book reconstruction, resync, stats.
                Pure with respect to the network.
    Sink        persistence.  `NullSink` by default; `SqliteBookEventSink`
                writes the PLAN.md section 5 `book_events` table to its OWN
                database file and REFUSES `data/pm.db` (a live recorder holds
                that file, and `book_events` is not in `core/db.py`'s DDL yet).
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import random
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from core.models import Market, Venue, now_us, parse_iso
from venues.kalshi.auth import KalshiSigner
from venues.kalshi.client import DEMO_WS, PROD_WS

log = logging.getLogger(__name__)

__all__ = [
    "BOOK_EVENTS_DDL",
    "Anomaly",
    "Book",
    "BookEvent",
    "BookEventSink",
    "BookTop",
    "FeedStats",
    "KalshiWSAuthError",
    "KalshiWSError",
    "KalshiWSFeed",
    "NullSink",
    "SqliteBookEventSink",
    "Subscription",
    "Transport",
    "WebsocketTransport",
    "percentiles",
]

# Channel names, as accepted by this deployment (measured -- see S6).
CH_ORDERBOOK = "orderbook_delta"
CH_TRADE = "trade"
CH_TICKER = "ticker"
CH_LIFECYCLE = "market_lifecycle_v2"
KNOWN_CHANNELS = (CH_ORDERBOOK, CH_TRADE, CH_TICKER, CH_LIFECYCLE)

# PLAN.md 6.1: "silence > 15s means dead".
SILENCE_TIMEOUT_S = 15.0
RECV_TIMEOUT_S = 1.0
BACKOFF_CAP_S = 30.0

# Kalshi limits CONCURRENT CONNECTIONS, so the ticker list is batched onto one
# socket.  A subscribe command carrying every watchlist ticker at once is the
# intended shape; this only exists so a pathological list can be split without
# ever opening a second connection.
MAX_TICKERS_PER_SUBSCRIBE = 1000

AUTH_REMEDY = (
    "Kalshi returned {status} on the WebSocket handshake. The production WS "
    "requires a funded, authenticated account -- an unauthenticated or demo key "
    "gets 401 there (measured 2026-08-27: "
    '{{"code":"authentication_error","details":"NOT_FOUND"}}). '
    "Check KALSHI_ENV / KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PATH, confirm the "
    "host clock is NTP-disciplined (skew produces the same 401), and use "
    f"{DEMO_WS} while the prod account is unfunded. Retrying will not help: an "
    "unauthorised socket never becomes authorised."
)


class KalshiWSError(RuntimeError):
    """Transport or protocol failure that the caller must see."""


class KalshiWSAuthError(KalshiWSError):
    """Handshake refused.  Carries the remedy, and is NEVER retried."""

    def __init__(self, status: int, body: str = "") -> None:
        detail = f" Body: {body[:200]}" if body else ""
        super().__init__(AUTH_REMEDY.format(status=status) + detail)
        self.status = status
        self.body = body


# --------------------------------------------------------------------------- #
# Wire-value parsing.  Dispatch on KEY NAME, never on value type.
# --------------------------------------------------------------------------- #
def _cents_from_dollars(raw: Any) -> int | None:
    """'0.0100' -> 1.  The V2 wire format for every price on this socket."""
    if raw is None:
        return None
    try:
        return int(round(float(str(raw).strip()) * 100))
    except (TypeError, ValueError):
        return None


def _cents_legacy(raw: Any) -> int | None:
    """Integer cents, the pre-V2 shape.  Kept because a rollback is cheap."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _size(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _venue_ts_us(msg: dict[str, Any]) -> int | None:
    """Venue send time in epoch micros, at the best resolution offered.

    Preference is deliberate: `ts` as an RFC3339 string carries MICROseconds
    ('2026-08-27T13:56:23.996443Z'), `ts_ms` carries milliseconds, and `ts` as
    an integer carries whole SECONDS (the `ticker` channel sends both, and its
    integer `ts` would quantise every latency sample to 1e6us).  Snapshots
    carry none of them, so this correctly returns None for those.
    """
    ts = msg.get("ts")
    if isinstance(ts, str) and ts:
        parsed = parse_iso(ts)
        if parsed is not None:
            return parsed
    ms = msg.get("ts_ms")
    if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 0:
        return int(ms * 1000)
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        return int(ts * 1_000_000)
    return None


def percentiles(values: Sequence[float],
                qs: Sequence[float] = (0.5, 0.9, 0.99)) -> dict[str, float]:
    """Nearest-rank percentiles.  No interpolation: an interpolated p99 latency
    is a number nobody observed, and latency decisions are made on observations.
    """
    if not values:
        return {}
    xs = sorted(values)
    out: dict[str, float] = {"n": float(len(xs)), "min": xs[0], "max": xs[-1]}
    for q in qs:
        idx = min(len(xs) - 1, max(0, int(round(q * len(xs))) - 1))
        out[f"p{int(q * 100)}"] = xs[idx]
    return out


# --------------------------------------------------------------------------- #
# Events and anomalies
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BookEvent:
    """One received frame, stamped on arrival.

    `recv_at_us` is taken BEFORE `json.loads` runs -- see `KalshiWSFeed.pump`.
    Stamping after parsing folds our own decode cost into the venue's latency
    and makes the histogram flatter than reality, in our favour.

    `kind` uses PLAN.md section 5's vocabulary plus `control`.  Control ACKs are
    recorded because they CONSUME SEQUENCE NUMBERS (S2): a replay that skips
    them sees holes in `seq` and reports gaps that never happened.
    """

    recv_at_us: int
    venue_ts_us: int | None
    venue: str
    ticker: str
    seq: int | None
    kind: str                       # snapshot|delta|trade|status|control
    payload: dict[str, Any]
    sid: int | None = None

    @property
    def latency_us(self) -> int | None:
        """recv - venue_send.  None when the frame carries no venue timestamp.

        This is a BOUND, not a clean one-way latency: it contains any offset
        between our clock and Kalshi's.  G1's `clock_ntp_disciplined` criterion
        is what turns it into a measurement.
        """
        if self.venue_ts_us is None:
            return None
        return self.recv_at_us - self.venue_ts_us


@dataclass(frozen=True, slots=True)
class Anomaly:
    """Something the feed refused to absorb silently.

    Every one of these is either lost data or a protocol change.  They are
    counted, logged, and handed to `on_anomaly`; none is ever swallowed.
    """

    at_us: int
    kind: str
    detail: str
    ticker: str = ""
    sid: int | None = None
    seq_expected: int | None = None
    seq_got: int | None = None
    raw: str = ""


# --------------------------------------------------------------------------- #
# Book
# --------------------------------------------------------------------------- #
@dataclass
class Book:
    """One market's L2 book, rebuilt from snapshot + deltas.

    Kalshi's book is quoted in TWO native currencies: the `yes` array is bids
    for YES, the `no` array is bids for NO.  There is no YES ask array -- a NO
    bid at price p IS a YES offer at 100 - p.  Getting that conversion wrong
    inverts the whole book (`client.get_orderbook` says the same thing).

    `stale` is the money-safe default: a book with no snapshot, or one whose
    sid took a sequence gap, is NOT readable through `top()` until it has been
    rebuilt.  Serving a stale book as if it were fresh is precisely how a
    corrupted book becomes a trade.
    """

    ticker: str
    yes: dict[int, float] = field(default_factory=dict)
    no: dict[int, float] = field(default_factory=dict)
    sid: int | None = None
    seq: int | None = None
    recv_at_us: int = 0
    venue_ts_us: int | None = None
    snapshots: int = 0
    deltas: int = 0
    stale: bool = True
    stale_reason: str = "no snapshot yet"

    # ------------------------------------------------------------- mutation
    def apply_snapshot(self, msg: dict[str, Any], *, recv_at_us: int,
                       seq: int | None = None, sid: int | None = None) -> bool:
        """Replace the book wholesale.  True if a recognised depth key was seen.

        A False return does NOT mean an error: an untraded market legitimately
        snapshots with no arrays at all (measured:
        `{"market_ticker":"...","market_id":""}`).  It means the caller must
        count it, because "no recognised depth key" at 100% of snapshots is how
        a wire-format change shows up -- as an empty book, not an exception.
        """
        yes_levels, saw_yes = _parse_levels(msg, "yes")
        no_levels, saw_no = _parse_levels(msg, "no")
        self.yes = yes_levels
        self.no = no_levels
        self.snapshots += 1
        self.recv_at_us = recv_at_us
        self.venue_ts_us = _venue_ts_us(msg)
        self.seq = seq
        self.sid = sid
        self.stale = False
        self.stale_reason = ""
        return saw_yes or saw_no

    def apply_delta(self, msg: dict[str, Any], *, recv_at_us: int,
                    seq: int | None = None) -> str | None:
        """Apply one delta.  Returns None on success, or a failure reason.

        A delta that drives a level NEGATIVE means our book and the venue's have
        diverged -- we missed something the sequence numbers did not catch.  It
        is reported as corruption rather than clamped to zero, because clamping
        produces a book that looks plausible and is wrong.
        """
        side = str(msg.get("side") or "").lower()
        if side not in ("yes", "no"):
            return f"delta with unusable side {msg.get('side')!r}"

        price = _cents_from_dollars(msg.get("price_dollars"))
        if price is None:
            price = _cents_legacy(msg.get("price"))
        if price is None:
            return "delta with no parseable price"

        delta = _size(msg.get("delta_fp"))
        if delta is None:
            delta = _size(msg.get("delta"))
        if delta is None:
            return "delta with no parseable size"

        levels = self.yes if side == "yes" else self.no
        new = levels.get(price, 0.0) + delta
        if new < -1e-9:
            return (f"delta drove {side} {price}c to {new:g} -- local book has "
                    f"diverged from the venue")
        if new <= 1e-9:
            levels.pop(price, None)
        else:
            levels[price] = new

        self.deltas += 1
        self.recv_at_us = recv_at_us
        self.venue_ts_us = _venue_ts_us(msg)
        self.seq = seq
        return None

    def mark_stale(self, reason: str) -> None:
        self.stale = True
        self.stale_reason = reason

    # -------------------------------------------------------------- reading
    @property
    def best_yes_bid(self) -> tuple[int, float] | None:
        return _best(self.yes)

    @property
    def best_no_bid(self) -> tuple[int, float] | None:
        return _best(self.no)

    def top(self) -> "BookTop":
        yb = self.best_yes_bid
        nb = self.best_no_bid
        return BookTop(
            ticker=self.ticker,
            yes_bid=yb[0] if yb else None,
            yes_bid_size=yb[1] if yb else 0.0,
            yes_ask=(100 - nb[0]) if nb else None,
            yes_ask_size=nb[1] if nb else 0.0,
            recv_at_us=self.recv_at_us,
            venue_ts_us=self.venue_ts_us,
            seq=self.seq,
        )

    def l2(self, depth: int = 10) -> dict[str, list[tuple[int, float]]]:
        """YES-referenced ladder: bids descending, asks ascending.

        Asks are the NO side reflected through 100, so descending NO prices map
        to ascending YES asks.
        """
        bids = sorted(((p, s) for p, s in self.yes.items() if s > 0), reverse=True)
        asks = [(100 - p, s) for p, s in sorted(
            ((p, s) for p, s in self.no.items() if s > 0), reverse=True)]
        return {"bids": bids[:depth], "asks": asks[:depth]}

    def levels_equal(self, other: "Book", *, tol: float = 1e-9) -> bool:
        """Do two books hold the same depth?  Zero-size levels do not count.

        This is the T-011 acceptance predicate: snapshot+deltas must equal a
        fresh snapshot, or the reconstruction is not trustworthy.
        """
        return _levels_equal(self.yes, other.yes, tol) and _levels_equal(self.no, other.no, tol)

    def clone(self) -> "Book":
        return Book(ticker=self.ticker, yes=dict(self.yes), no=dict(self.no), sid=self.sid,
                    seq=self.seq, recv_at_us=self.recv_at_us, venue_ts_us=self.venue_ts_us,
                    snapshots=self.snapshots, deltas=self.deltas, stale=self.stale,
                    stale_reason=self.stale_reason)


def _best(levels: dict[int, float]) -> tuple[int, float] | None:
    live = [(p, s) for p, s in levels.items() if s > 0]
    if not live:
        return None
    return max(live, key=lambda ps: ps[0])


def _levels_equal(a: dict[int, float], b: dict[int, float], tol: float) -> bool:
    aa = {p: s for p, s in a.items() if s > tol}
    bb = {p: s for p, s in b.items() if s > tol}
    if set(aa) != set(bb):
        return False
    return all(abs(aa[p] - bb[p]) <= tol for p in aa)


def _parse_levels(msg: dict[str, Any], side: str) -> tuple[dict[int, float], bool]:
    """Read one side's depth array.  Returns (levels, saw_a_recognised_key).

    Key dispatch, not value sniffing:
        `{side}_dollars_fp`  [["0.0100","1360.00"], ...]   <- the live V2 wire
        `{side}`             [[1, 1360], ...]              <- legacy int cents
    """
    out: dict[int, float] = {}
    raw = msg.get(f"{side}_dollars_fp")
    if isinstance(raw, list):
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            price = _cents_from_dollars(row[0])
            size = _size(row[1])
            if price is not None and size is not None and size > 0:
                out[price] = out.get(price, 0.0) + size
        return out, True

    raw = msg.get(side)
    if isinstance(raw, list):
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            price = _cents_legacy(row[0])
            size = _size(row[1])
            if price is not None and size is not None and size > 0:
                out[price] = out.get(price, 0.0) + size
        return out, True

    return out, False


@dataclass(frozen=True, slots=True)
class BookTop:
    """L1 in the same YES-referenced vocabulary as `core.models.Market`.

    `has_bid` / `has_ask` reproduce `Market`'s semantics exactly, on purpose: a
    yes_bid of 0 means NOBODY IS BIDDING and is not a restable level, and a
    yes_ask of 100 means nobody is offering.  Every RV sleeve reads those.
    """

    ticker: str
    yes_bid: int | None
    yes_bid_size: float
    yes_ask: int | None
    yes_ask_size: float
    recv_at_us: int
    venue_ts_us: int | None
    seq: int | None

    @property
    def has_bid(self) -> bool:
        return self.yes_bid is not None and self.yes_bid >= 1

    @property
    def has_ask(self) -> bool:
        return self.yes_ask is not None and 1 <= self.yes_ask <= 99

    @property
    def has_two_sided_quote(self) -> bool:
        return self.has_bid and self.has_ask and (self.yes_ask or 0) > (self.yes_bid or 0)

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

    def to_market(self, *, event_ticker: str = "", series_ticker: str = "") -> Market:
        """Hand the live book to anything that already consumes REST snapshots.

        Same type, same fields, same YES reference -- so a sleeve does not need
        a second code path to read the fast feed.
        """
        return Market(
            venue=Venue.KALSHI,
            ticker=self.ticker,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            yes_bid=self.yes_bid,
            yes_ask=self.yes_ask,
            yes_bid_size=self.yes_bid_size,
            yes_ask_size=self.yes_ask_size,
        )


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
class Transport(Protocol):
    """Raw frames in and out.  The ONLY thing that knows about sockets.

    Everything above this line is exercised in tests with a scripted transport
    and no network, which is what makes the gap/resync logic testable at all.
    """

    def connect(self) -> None: ...
    def send(self, text: str) -> None: ...
    def recv(self, timeout: float | None = None) -> str: ...
    def close(self) -> None: ...
    @property
    def is_open(self) -> bool: ...


class WebsocketTransport:
    """`websockets.sync` client with Kalshi's RSA-PSS handshake headers.

    Keepalive is the WebSocket protocol's own ping/pong, driven by the library
    (`ping_interval` / `ping_timeout`): a missed pong closes the socket, which
    surfaces here as `ConnectionClosed` and drives the reconnect path.  Measured
    RTT to the demo host: 78-79ms.  The application-level silence watchdog in
    `KalshiWSFeed` is a SECOND, independent liveness check -- a socket can stay
    ping-healthy while the venue has stopped sending data, and PLAN.md 6.1 is
    explicit that silence > 15s means dead.
    """

    def __init__(self, url: str, *, signer: KalshiSigner | None = None,
                 open_timeout: float = 12.0, ping_interval: float | None = 10.0,
                 ping_timeout: float | None = 10.0, max_queue: int = 2048) -> None:
        self.url = url
        self.signer = signer
        self.open_timeout = open_timeout
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.max_queue = max_queue
        self._ws: Any = None

    @property
    def ws_path(self) -> str:
        return urlsplit(self.url).path or "/trade-api/ws/v2"

    def connect(self) -> None:
        from websockets.exceptions import InvalidStatus
        from websockets.sync.client import connect as ws_connect

        headers: dict[str, str] = {}
        if self.signer is not None:
            headers = self.signer.ws_headers(self.ws_path)
        try:
            self._ws = ws_connect(
                self.url,
                additional_headers=headers,
                open_timeout=self.open_timeout,
                ping_interval=self.ping_interval,
                ping_timeout=self.ping_timeout,
                max_queue=self.max_queue,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            body = ""
            try:
                body = bytes(exc.response.body or b"").decode("utf-8", "replace")
            except Exception:                                # pragma: no cover
                body = ""
            if status in (401, 403):
                raise KalshiWSAuthError(status, body) from exc
            raise KalshiWSError(f"handshake failed with HTTP {status}: {body[:200]}") from exc

    def send(self, text: str) -> None:
        if self._ws is None:
            raise KalshiWSError("send on a closed transport")
        self._ws.send(text)

    def recv(self, timeout: float | None = None) -> str:
        if self._ws is None:
            raise KalshiWSError("recv on a closed transport")
        data = self._ws.recv(timeout=timeout)
        return data if isinstance(data, str) else bytes(data).decode("utf-8", "replace")

    def ping_rtt_us(self, timeout: float = 5.0) -> int | None:
        """Round trip of one WS ping.  The floor under any latency claim."""
        if self._ws is None:
            return None
        t0 = time.perf_counter_ns()
        ev = self._ws.ping()
        if not ev.wait(timeout):
            return None
        return (time.perf_counter_ns() - t0) // 1000

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:                                # pragma: no cover
                pass

    @property
    def is_open(self) -> bool:
        return self._ws is not None


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
BOOK_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS book_events (
  id           INTEGER PRIMARY KEY,
  recv_at_us   INTEGER NOT NULL,
  venue_ts_us  INTEGER,
  venue        TEXT NOT NULL,
  ticker       TEXT NOT NULL,
  seq          INTEGER,
  kind         TEXT NOT NULL,
  payload      BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_be_ticker_time ON book_events(venue, ticker, recv_at_us);
"""


class BookEventSink(Protocol):
    def write(self, events: Sequence[BookEvent]) -> int: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


class NullSink:
    """Default sink.  Counts, stores nothing -- the feed is useful without a DB."""

    def __init__(self) -> None:
        self.written = 0

    def write(self, events: Sequence[BookEvent]) -> int:
        self.written += len(events)
        return len(events)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class SqliteBookEventSink:
    """PLAN.md section 5 `book_events`, in its OWN database file.

    Two deliberate constraints:

      * It REFUSES `data/pm.db` unless forced.  `book_events` is not in
        `core/db.py`'s DDL, so this would be the only writer creating it, and
        `data/pm.db` is held open by the live REST recorder.  Creating a table
        under a running writer is how you get `database is locked` in the middle
        of a recording session.
      * `payload` is UTF-8 JSON bytes, not msgpack.  PLAN.md says msgpack;
        msgpack is not a declared dependency and is not installed.  JSON costs
        roughly 2-3x the bytes and the column is a BLOB either way, so switching
        later is a re-encode, not a migration.

    Control frames are stored too, with `ticker = ''`.  They consume sequence
    numbers (S2), so a replay that lacks them mis-reports gaps.
    """

    FORBIDDEN = ("pm.db",)

    def __init__(self, path: str | Path = "data/book_events.db", *,
                 flush_every: int = 500, allow_shared_db: bool = False) -> None:
        self.path = Path(path)
        if not allow_shared_db and self.path.name in self.FORBIDDEN:
            raise ValueError(
                f"refusing to write book_events into {self.path} -- that database "
                "belongs to the REST recorder and has no book_events table in its "
                "schema. Point this at its own file, or pass allow_shared_db=True "
                "once core/db.py owns the DDL."
            )
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(BOOK_EVENTS_DDL)
        self.conn.commit()
        self.flush_every = flush_every
        self.written = 0
        self._pending: list[tuple[Any, ...]] = []

    def write(self, events: Sequence[BookEvent]) -> int:
        for e in events:
            self._pending.append((
                e.recv_at_us, e.venue_ts_us, e.venue, e.ticker, e.seq, e.kind,
                json.dumps(e.payload, separators=(",", ":")).encode("utf-8"),
            ))
        self.written += len(events)
        if len(self._pending) >= self.flush_every:
            self.flush()
        return len(events)

    def flush(self) -> None:
        if not self._pending:
            return
        self.conn.executemany(
            """INSERT INTO book_events
               (recv_at_us, venue_ts_us, venue, ticker, seq, kind, payload)
               VALUES (?,?,?,?,?,?,?)""",
            self._pending,
        )
        self.conn.commit()
        self._pending.clear()

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM book_events").fetchone()[0])

    def close(self) -> None:
        self.flush()
        self.conn.close()


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
@dataclass
class FeedStats:
    """Everything G1 asks for and nothing in this repo currently measures."""

    frames: int = 0
    sequenced: int = 0              # frames carrying (sid, seq) -- the gap denominator
    snapshots: int = 0
    deltas: int = 0
    trades: int = 0
    controls: int = 0
    status: int = 0
    gaps: int = 0                   # gap EVENTS, not missed messages
    missed_messages: int = 0        # sum of gap widths -- the honest loss count
    resyncs: int = 0
    malformed: int = 0
    unknown_type: int = 0
    venue_errors: int = 0
    book_corruptions: int = 0
    deltas_before_snapshot: int = 0
    snapshots_without_depth_key: int = 0
    reconnects: int = 0
    connect_failures: int = 0
    duplicate_trades: int = 0
    started_us: int = field(default_factory=now_us)
    latencies_us: list[int] = field(default_factory=list)
    degraded: list[tuple[int, str]] = field(default_factory=list)
    latency_cap: int = 200_000

    def record_latency(self, us: int) -> None:
        # Reservoir-free and bounded: keep the first `latency_cap` samples and
        # then subsample, so a 24h run cannot exhaust memory while the histogram
        # stays representative of the session.
        if len(self.latencies_us) < self.latency_cap:
            self.latencies_us.append(us)
        elif random.random() < 0.05:
            self.latencies_us[random.randrange(self.latency_cap)] = us

    @property
    def gap_rate(self) -> float:
        """G1 exit criterion: `sequence_gap_rate < 0.001`.

        Denominator is SEQUENCED frames, not all frames: unsequenced channels
        (`ticker` carries no seq at all -- S5) cannot contribute a gap and must
        not dilute the rate.  Numerator is gap EVENTS; `missed_messages` reports
        how many individual messages were actually lost, which is the number
        that says how bad a gap was.
        """
        return self.gaps / self.sequenced if self.sequenced else 0.0

    def latency_percentiles(self) -> dict[str, float]:
        return percentiles(self.latencies_us, (0.5, 0.9, 0.99))

    def report(self) -> str:
        secs = max((now_us() - self.started_us) / 1_000_000, 1e-9)
        lat = self.latency_percentiles()
        lat_txt = (
            f" lat_ms p50={lat['p50']/1000:.1f} p90={lat['p90']/1000:.1f} "
            f"p99={lat['p99']/1000:.1f} n={int(lat['n'])}" if lat else " lat=n/a"
        )
        return (
            f"frames={self.frames:,} ({self.frames/secs:,.0f}/s) "
            f"snap={self.snapshots} delta={self.deltas:,} trade={self.trades} "
            f"gaps={self.gaps} missed={self.missed_messages} rate={self.gap_rate:.2e} "
            f"resync={self.resyncs} reconnect={self.reconnects} "
            f"malformed={self.malformed} corrupt={self.book_corruptions}" + lat_txt
        )


@dataclass
class Subscription:
    """One sid.  Tickers are the VENUE's view, taken from its `ok` ACK."""

    sid: int
    channel: str
    tickers: set[str] = field(default_factory=set)
    last_seq: int | None = None


# --------------------------------------------------------------------------- #
# Feed
# --------------------------------------------------------------------------- #
class KalshiWSFeed:
    """One socket, many tickers, an honest book.

    Usage::

        feed = KalshiWSFeed(url=DEMO_WS, signer=signer, tickers=watchlist)
        feed.start()
        feed.run(duration_s=60)
        top = feed.top("KXGOLDH-26AUG2711-T4611.99")   # None while stale

    `top()` returns None for a stale book by design.  A caller that wants to see
    a book it must not trade on asks for it explicitly.
    """

    def __init__(
        self,
        *,
        url: str = DEMO_WS,
        signer: KalshiSigner | None = None,
        tickers: Iterable[str] = (),
        channels: Sequence[str] = (CH_ORDERBOOK,),
        transport: Transport | None = None,
        transport_factory: Callable[[], Transport] | None = None,
        sink: BookEventSink | None = None,
        venue: str = "kalshi",
        clock: Callable[[], int] = now_us,
        on_event: Callable[[BookEvent], None] | None = None,
        on_anomaly: Callable[[Anomaly], None] | None = None,
        max_reconnects: int = 100,
        recv_timeout_s: float = RECV_TIMEOUT_S,
        silence_timeout_s: float = SILENCE_TIMEOUT_S,
        backoff_cap_s: float = BACKOFF_CAP_S,
        auto_resync: bool = True,
        strict: bool = False,
    ) -> None:
        for ch in channels:
            if ch not in KNOWN_CHANNELS:
                log.warning("channel %r is not one this module has seen accepted "
                            "(known: %s)", ch, ", ".join(KNOWN_CHANNELS))
        self.url = url
        self.signer = signer
        self.tickers: list[str] = list(dict.fromkeys(tickers))
        self.channels = tuple(channels)
        self.venue = venue
        self.sink: BookEventSink = sink or NullSink()
        self._clock = clock
        self._on_event = on_event
        self._on_anomaly = on_anomaly
        self.max_reconnects = max_reconnects
        self.recv_timeout_s = recv_timeout_s
        self.silence_timeout_s = silence_timeout_s
        self.backoff_cap_s = backoff_cap_s
        self.auto_resync = auto_resync
        self.strict = strict

        self._transport_factory = transport_factory or (lambda: WebsocketTransport(
            url, signer=signer))
        self._transport: Transport | None = transport
        self._explicit_transport = transport is not None

        self.stats = FeedStats()
        self.books: dict[str, Book] = {t: Book(ticker=t) for t in self.tickers}
        self.subscriptions: dict[int, Subscription] = {}
        self.anomalies: list[Anomaly] = []
        self.state = "disconnected"
        self.epoch = 0                       # increments on every connect

        self._pending_cmds: dict[int, dict[str, Any]] = {}
        self._next_cmd_id = 1
        self._last_frame_us = 0
        self._seen_trade_ids: dict[str, None] = {}
        self._stop = False

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Connect and subscribe.  Auth failures raise, they do not retry."""
        self._connect()
        self.subscribe()

    def _connect(self) -> None:
        self.state = "connecting"
        if self._transport is None or not self._transport.is_open:
            if self._transport is None or not self._explicit_transport:
                self._transport = self._transport_factory()
            self._transport.connect()
        self.epoch += 1
        self.state = "live"
        self._last_frame_us = self._clock()
        # A new connection means new sids and a reset seq counter (S4), so every
        # book is unreadable until its fresh snapshot lands.
        self.subscriptions.clear()
        self._pending_cmds.clear()
        for book in self.books.values():
            book.mark_stale("awaiting snapshot after connect")

    def close(self) -> None:
        self._stop = True
        if self._transport is not None:
            self._transport.close()
        self.sink.flush()
        self.state = "disconnected"

    def __enter__(self) -> "KalshiWSFeed":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --------------------------------------------------------- subscription
    def _send_cmd(self, cmd: str, params: dict[str, Any], **meta: Any) -> int:
        if self._transport is None:
            raise KalshiWSError("not connected")
        cid = self._next_cmd_id
        self._next_cmd_id += 1
        self._pending_cmds[cid] = {"cmd": cmd, **meta}
        self._transport.send(json.dumps({"id": cid, "cmd": cmd, "params": params}))
        return cid

    def subscribe(self, tickers: Sequence[str] | None = None,
                  channels: Sequence[str] | None = None) -> list[int]:
        """Subscribe every channel over the SINGLE open connection.

        Batched deliberately: Kalshi rate-limits concurrent connections, so a
        connection per ticker is not a slow design, it is a banned one.
        """
        want = list(tickers) if tickers is not None else self.tickers
        chans = list(channels) if channels is not None else list(self.channels)
        ids: list[int] = []
        for ch in chans:
            for i in range(0, max(1, len(want)), MAX_TICKERS_PER_SUBSCRIBE):
                chunk = want[i:i + MAX_TICKERS_PER_SUBSCRIBE]
                ids.append(self._send_cmd(
                    "subscribe",
                    {"channels": [ch], "market_tickers": chunk},
                    channel=ch, tickers=tuple(chunk),
                ))
        return ids

    def resync_sid(self, sid: int, reason: str) -> None:
        """The ONLY resync that fixes a sequence gap.  Verified live (S4).

        `unsubscribe` then `subscribe` yields a NEW sid with `seq` reset to 1 and
        a fresh snapshot for every ticker.  Re-subscribing WITHOUT the
        unsubscribe returns an `ok` ACK and no snapshot at all (S3) -- the book
        stays corrupt and nothing tells you.

        It is heavy on purpose: `seq` is shared across every ticker on the sid
        (S1), so a gap gives no information about WHICH book lost a delta, and
        rebuilding one of them would be a guess.
        """
        sub = self.subscriptions.get(sid)
        channel = sub.channel if sub else self.channels[0]
        tickers = sorted(sub.tickers) if sub and sub.tickers else list(self.tickers)
        for t in tickers:
            book = self.books.get(t)
            if book is not None:
                book.mark_stale(f"resync: {reason}")
        self.stats.resyncs += 1
        log.warning("resync sid=%s channel=%s tickers=%d reason=%s",
                    sid, channel, len(tickers), reason)
        try:
            self._send_cmd("unsubscribe", {"sids": [sid]}, sid=sid)
            self.subscribe(tickers, [channel])
        except KalshiWSError:
            self._degrade(f"resync of sid {sid} could not be sent: {reason}")
            raise

    def resync_ticker(self, ticker: str, reason: str) -> None:
        """Rebuild ONE book without disturbing the others.  Verified live (S4).

        `update_subscription delete_markets` then `add_markets` re-snapshots just
        that ticker on the same sid.  Correct for single-book corruption (a delta
        that drove a level negative); WRONG for a sequence gap, because a gap
        does not say whose delta went missing.
        """
        book = self.books.get(ticker)
        sid = book.sid if book and book.sid is not None else next(iter(self.subscriptions), None)
        if sid is None:
            self._degrade(f"cannot resync {ticker}: no subscription is established")
            return
        if book is not None:
            book.mark_stale(f"resync: {reason}")
        self.stats.resyncs += 1
        log.warning("resync ticker=%s sid=%s reason=%s", ticker, sid, reason)
        self._send_cmd("update_subscription",
                       {"sids": [sid], "market_tickers": [ticker],
                        "action": "delete_markets"}, sid=sid)
        self._send_cmd("update_subscription",
                       {"sids": [sid], "market_tickers": [ticker],
                        "action": "add_markets"}, sid=sid)

    # ---------------------------------------------------------------- pump
    def pump(self, timeout: float | None = None) -> list[BookEvent]:
        """Read ONE frame and process it.

        The stamp is the first statement after the socket returns and BEFORE
        `_decode`.  Stamping after parsing charges the venue for our JSON decode
        and flatters every latency percentile.
        """
        if self._transport is None:
            raise KalshiWSError("not connected")
        raw = self._transport.recv(timeout if timeout is not None else self.recv_timeout_s)
        recv_at_us = self._clock()                       # <-- BEFORE ANY PARSING
        return self.handle_frame(raw, recv_at_us)

    def _decode(self, text: str) -> Any:
        """Parse seam.  Overridden in tests to prove the stamp precedes parsing."""
        return json.loads(text)

    def handle_frame(self, raw: str, recv_at_us: int) -> list[BookEvent]:
        """Pure protocol handling.  No socket, no clock -- fully testable."""
        self.stats.frames += 1
        self._last_frame_us = recv_at_us

        try:
            frame = self._decode(raw)
        except (ValueError, TypeError) as exc:
            return self._malformed("unparseable_json", f"{type(exc).__name__}: {exc}", raw)
        if not isinstance(frame, dict):
            return self._malformed("not_an_object", f"top level is {type(frame).__name__}", raw)

        ftype = frame.get("type")
        if not isinstance(ftype, str) or not ftype:
            return self._malformed("missing_type", "frame has no 'type'", raw)

        sid = frame.get("sid") if isinstance(frame.get("sid"), int) else None
        seq = frame.get("seq") if isinstance(frame.get("seq"), int) else None
        msg = frame.get("msg")
        msg = msg if isinstance(msg, dict) else {}

        # Sequence accounting FIRST: control ACKs consume seq numbers (S2), so
        # skipping them here manufactures phantom gaps.
        gapped = self._track_seq(sid, seq, recv_at_us, raw)

        events: list[BookEvent] = []
        if ftype == "orderbook_snapshot":
            events = self._on_snapshot(msg, frame, recv_at_us, sid, seq, raw)
        elif ftype == "orderbook_delta":
            events = self._on_delta(msg, frame, recv_at_us, sid, seq, raw)
        elif ftype == "trade":
            events = self._on_trade(msg, frame, recv_at_us, sid, seq)
        elif ftype in ("ticker", "ticker_v2", "market_lifecycle_v2", "market_lifecycle",
                       "market_positions", "event_lifecycle"):
            self.stats.status += 1
            events = [self._event(recv_at_us, msg, str(msg.get("market_ticker") or ""),
                                  seq, "status", frame, sid)]
        elif ftype in ("subscribed", "unsubscribed", "ok"):
            events = self._on_control(ftype, frame, recv_at_us, sid, seq)
        elif ftype == "error":
            self.stats.venue_errors += 1
            code = msg.get("code")
            self._anomaly("venue_error", f"code={code} msg={msg.get('msg')!r} "
                          f"id={frame.get('id')}", raw=raw, sid=sid, seq=seq)
            events = [self._event(recv_at_us, msg, "", seq, "control", frame, sid)]
        else:
            self.stats.unknown_type += 1
            self._anomaly("unknown_type", f"type={ftype!r}", raw=raw, sid=sid, seq=seq)
            events = [self._event(recv_at_us, msg, str(msg.get("market_ticker") or ""),
                                  seq, "status", frame, sid)]

        if gapped and self.auto_resync and sid is not None:
            # Resync AFTER the frame is accounted for, so the gap-triggering
            # message is still recorded rather than dropped on the floor.
            try:
                self.resync_sid(sid, f"sequence gap at seq={seq}")
            except KalshiWSError:
                pass

        if events:
            self._emit(events)
        return events

    # ----------------------------------------------------------- sequencing
    def _track_seq(self, sid: int | None, seq: int | None, recv_at_us: int,
                   raw: str) -> bool:
        """True when this frame revealed a gap.

        Per-SID (S1).  A new sid starts a new counter, which is why a reconnect
        does NOT look like a gap: the fresh subscription's seq restarts at 1 on
        a sid we have never seen.  Tracking seq per CONNECTION instead makes
        every reconnect report a false gap and buries the real ones.
        """
        if sid is None or seq is None:
            return False
        self.stats.sequenced += 1
        sub = self.subscriptions.get(sid)
        if sub is None:
            sub = Subscription(sid=sid, channel=self.channels[0])
            self.subscriptions[sid] = sub
        prev = sub.last_seq
        sub.last_seq = seq
        if prev is None or seq == prev + 1:
            return False
        if seq <= prev:
            # Not a loss -- a repeat or a reordering.  Still never silent.
            self._anomaly("seq_regression", f"seq went {prev} -> {seq}", sid=sid,
                          seq_expected=prev + 1, seq_got=seq, raw=raw)
            return False
        missed = seq - prev - 1
        self.stats.gaps += 1
        self.stats.missed_messages += missed
        self._anomaly("sequence_gap", f"missed {missed} message(s) on sid {sid}",
                      sid=sid, seq_expected=prev + 1, seq_got=seq, raw=raw)
        for t in (sub.tickers or set(self.tickers)):
            book = self.books.get(t)
            if book is not None:
                book.mark_stale(f"sequence gap on sid {sid} (missed {missed})")
        return True

    # -------------------------------------------------------------- handlers
    def _on_snapshot(self, msg: dict[str, Any], frame: dict[str, Any], recv_at_us: int,
                     sid: int | None, seq: int | None, raw: str) -> list[BookEvent]:
        ticker = str(msg.get("market_ticker") or "")
        if not ticker:
            return self._malformed("missing_ticker", "snapshot without market_ticker", raw)
        book = self.books.get(ticker)
        if book is None:
            book = self.books[ticker] = Book(ticker=ticker)
        saw_depth = book.apply_snapshot(msg, recv_at_us=recv_at_us, seq=seq, sid=sid)
        self.stats.snapshots += 1
        if not saw_depth:
            self.stats.snapshots_without_depth_key += 1
        if sid is not None:
            self.subscriptions.setdefault(
                sid, Subscription(sid=sid, channel=CH_ORDERBOOK)).tickers.add(ticker)
        return [self._event(recv_at_us, msg, ticker, seq, "snapshot", frame, sid)]

    def _on_delta(self, msg: dict[str, Any], frame: dict[str, Any], recv_at_us: int,
                  sid: int | None, seq: int | None, raw: str) -> list[BookEvent]:
        ticker = str(msg.get("market_ticker") or "")
        if not ticker:
            return self._malformed("missing_ticker", "delta without market_ticker", raw)
        book = self.books.get(ticker)
        if book is None or book.snapshots == 0:
            # Applying this would invent depth that the venue never sent.
            self.stats.deltas_before_snapshot += 1
            self._anomaly("delta_before_snapshot",
                          "delta arrived before any snapshot; not applied",
                          ticker=ticker, sid=sid, seq=seq, raw=raw)
            if book is None:
                self.books[ticker] = Book(ticker=ticker)
            return [self._event(recv_at_us, msg, ticker, seq, "delta", frame, sid)]

        failure = book.apply_delta(msg, recv_at_us=recv_at_us, seq=seq)
        if failure is not None:
            self.stats.book_corruptions += 1
            book.mark_stale(failure)
            self._anomaly("book_corruption", failure, ticker=ticker, sid=sid,
                          seq=seq, raw=raw)
            if self.auto_resync:
                try:
                    self.resync_ticker(ticker, failure)
                except KalshiWSError:
                    pass
        else:
            self.stats.deltas += 1
        return [self._event(recv_at_us, msg, ticker, seq, "delta", frame, sid)]

    def _on_trade(self, msg: dict[str, Any], frame: dict[str, Any], recv_at_us: int,
                  sid: int | None, seq: int | None) -> list[BookEvent]:
        """Public tape.  Deduped on the venue's own trade id, never on a count.

        A reconnect plus a REST backfill re-delivers a window, and a double
        counted print biases every Kyle-lambda and realised-spread estimate.
        `execution/fillfeed.py` makes the same argument about fills.
        """
        ticker = str(msg.get("market_ticker") or msg.get("ticker") or "")
        tid = str(msg.get("trade_id") or "")
        if tid:
            if tid in self._seen_trade_ids:
                self.stats.duplicate_trades += 1
                return []
            # An INSERTION-ORDERED set.  Truncating a plain `set` by slicing its
            # iteration order evicts an arbitrary half, not the oldest half, so
            # a recent id can be forgotten and its print emitted a second time --
            # which is the exact double-count this dedupe exists to prevent.
            self._seen_trade_ids[tid] = None
            if len(self._seen_trade_ids) > 400_000:
                for stale in list(itertools.islice(self._seen_trade_ids, 200_000)):
                    del self._seen_trade_ids[stale]
        self.stats.trades += 1
        return [self._event(recv_at_us, msg, ticker, seq, "trade", frame, sid)]

    def _on_control(self, ftype: str, frame: dict[str, Any], recv_at_us: int,
                    sid: int | None, seq: int | None) -> list[BookEvent]:
        msg = frame.get("msg") if isinstance(frame.get("msg"), dict) else {}
        cid = frame.get("id") if isinstance(frame.get("id"), int) else None
        pending = self._pending_cmds.pop(cid, {}) if cid is not None else {}
        self.stats.controls += 1

        if ftype == "subscribed":
            new_sid = msg.get("sid")
            if isinstance(new_sid, int):
                sub = self.subscriptions.setdefault(
                    new_sid, Subscription(sid=new_sid,
                                          channel=str(msg.get("channel") or CH_ORDERBOOK)))
                sub.channel = str(msg.get("channel") or sub.channel)
                sub.tickers |= set(pending.get("tickers", ()))
                sub.last_seq = None                # S4: seq restarts at 1 here
                sid = new_sid
        elif ftype == "unsubscribed" and sid is not None:
            self.subscriptions.pop(sid, None)
        elif ftype == "ok" and sid is not None:
            # The venue's authoritative view of the subscription -- this is what
            # proves a reconnect lost nothing.
            venue_tickers = msg.get("market_tickers")
            if isinstance(venue_tickers, list):
                sub = self.subscriptions.setdefault(
                    sid, Subscription(sid=sid, channel=self.channels[0]))
                sub.tickers = {str(t) for t in venue_tickers}

        return [self._event(recv_at_us, msg, "", seq, "control", frame, sid)]

    # ------------------------------------------------------------- plumbing
    def _event(self, recv_at_us: int, msg: dict[str, Any], ticker: str,
               seq: int | None, kind: str, frame: dict[str, Any],
               sid: int | None) -> BookEvent:
        venue_ts = _venue_ts_us(msg)
        ev = BookEvent(recv_at_us=recv_at_us, venue_ts_us=venue_ts, venue=self.venue,
                       ticker=ticker, seq=seq, kind=kind, payload=frame, sid=sid)
        if venue_ts is not None:
            self.stats.record_latency(recv_at_us - venue_ts)
        return ev

    def _emit(self, events: Sequence[BookEvent]) -> None:
        self.sink.write(events)
        if self._on_event is not None:
            for e in events:
                self._on_event(e)

    def _malformed(self, kind: str, detail: str, raw: str) -> list[BookEvent]:
        self.stats.malformed += 1
        self._anomaly(kind, detail, raw=raw)
        return []

    def _anomaly(self, kind: str, detail: str, *, ticker: str = "", sid: int | None = None,
                 seq: int | None = None, seq_expected: int | None = None,
                 seq_got: int | None = None, raw: str = "") -> Anomaly:
        a = Anomaly(at_us=self._clock(), kind=kind, detail=detail, ticker=ticker, sid=sid,
                    seq_expected=seq_expected if seq_expected is not None else seq,
                    seq_got=seq_got, raw=raw[:500])
        self.anomalies.append(a)
        log.warning("anomaly %s: %s (ticker=%s sid=%s)", kind, detail, ticker or "-", sid)
        if self._on_anomaly is not None:
            self._on_anomaly(a)
        if self.strict:
            raise KalshiWSError(f"{kind}: {detail}")
        return a

    def _degrade(self, reason: str) -> None:
        """Record every drop into a degraded state.  Never silent (PLAN.md 6.1)."""
        self.state = "degraded"
        self.stats.degraded.append((self._clock(), reason))
        log.error("feed degraded: %s", reason)
        self._anomaly("degraded", reason)

    # ------------------------------------------------------------- read API
    def book(self, ticker: str) -> Book | None:
        return self.books.get(ticker)

    def top(self, ticker: str, *, allow_stale: bool = False) -> BookTop | None:
        """L1 for one ticker, or None when the book is not trustworthy.

        Stale means: no snapshot yet, a sequence gap on its sid, corruption, or
        a reconnect in flight.  Returning a quote in any of those states is how
        a corrupted book becomes an order.
        """
        book = self.books.get(ticker)
        if book is None or book.snapshots == 0:
            return None
        if book.stale and not allow_stale:
            return None
        return book.top()

    def tops(self, *, allow_stale: bool = False) -> dict[str, BookTop]:
        out: dict[str, BookTop] = {}
        for t in self.books:
            top = self.top(t, allow_stale=allow_stale)
            if top is not None:
                out[t] = top
        return out

    def stale_tickers(self) -> dict[str, str]:
        return {t: b.stale_reason for t, b in self.books.items() if b.stale}

    # ------------------------------------------------------------- run loop
    def request_stop(self, *_: object) -> None:
        self._stop = True

    def run(self, *, duration_s: float | None = None, max_frames: int | None = None,
            should_stop: Callable[[], bool] | None = None) -> int:
        """Pump until told to stop, reconnecting with backoff on failure.

        Three independent ways this loop notices trouble, because they fail
        differently:
          * `ConnectionClosed`/`OSError`  -> the socket went away.
          * silence > `silence_timeout_s` -> the socket is fine and the venue
            stopped talking (PLAN.md 6.1).  Ping/pong will NOT catch this.
          * an auth error -> raised immediately; retrying 401 forever is how a
            recorder looks alive while recording nothing.
        """
        from websockets.exceptions import ConnectionClosed

        deadline = time.monotonic() + duration_s if duration_s else None
        attempt = 0
        processed = 0

        while not self._stop:
            if deadline is not None and time.monotonic() >= deadline:
                break
            if max_frames is not None and self.stats.frames >= max_frames:
                break
            if should_stop is not None and should_stop():
                break

            if self._transport is None or not self._transport.is_open:
                if self.stats.reconnects >= self.max_reconnects:
                    self._degrade(f"giving up after {self.stats.reconnects} reconnects")
                    break
                try:
                    self._connect()
                    self.subscribe()
                    attempt = 0
                except KalshiWSAuthError:
                    raise
                except (KalshiWSError, OSError) as exc:
                    self.stats.connect_failures += 1
                    self._degrade(f"connect failed: {type(exc).__name__}: {exc}")
                    self._sleep_backoff(attempt)
                    attempt += 1
                    continue

            try:
                self.pump()
                processed += 1
            except TimeoutError:
                silence = (self._clock() - self._last_frame_us) / 1_000_000
                if silence > self.silence_timeout_s:
                    self._degrade(f"silent for {silence:.1f}s (> {self.silence_timeout_s}s)")
                    self._reconnect("silence watchdog")
                continue
            except KalshiWSAuthError:
                raise
            except (ConnectionClosed, OSError, KalshiWSError) as exc:
                self._degrade(f"read failed: {type(exc).__name__}: {exc}")
                self._reconnect(f"{type(exc).__name__}")
                self._sleep_backoff(attempt)
                attempt += 1

        self.sink.flush()
        return processed

    def _reconnect(self, reason: str) -> None:
        """Drop the socket and mark every book unreadable until it is rebuilt.

        Nothing is lost by this: the venue re-snapshots on the fresh
        subscription, and nothing is duplicated either, because a snapshot
        REPLACES the book rather than adding to it, and trades dedupe on
        `trade_id`.
        """
        self.stats.reconnects += 1
        log.warning("reconnecting (%s); reconnect #%d", reason, self.stats.reconnects)
        if self._transport is not None:
            self._transport.close()
        if not self._explicit_transport:
            self._transport = None
        for book in self.books.values():
            book.mark_stale(f"reconnecting: {reason}")
        self.subscriptions.clear()

    def _sleep_backoff(self, attempt: int) -> None:
        time.sleep(min(2.0 ** attempt, self.backoff_cap_s) * (0.5 + random.random()))


# --------------------------------------------------------------------------- #
# CLI -- the live latency probe T-016 needs.
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Kalshi WebSocket market-data feed (T-011)")
    ap.add_argument("--env", choices=("demo", "prod"), default=None,
                    help="override KALSHI_ENV")
    ap.add_argument("--tickers", default="", help="comma-separated; default: from --auto")
    ap.add_argument("--auto", type=int, default=20,
                    help="if no --tickers, take this many open markets from REST")
    ap.add_argument("--channels", default=CH_ORDERBOOK)
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--db", default="", help="write book_events here (NOT data/pm.db)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from core.config import load_settings
    from venues.kalshi.client import KalshiClient

    settings = load_settings()
    env = args.env or settings.kalshi.env
    url = DEMO_WS if env == "demo" else PROD_WS
    base = ("https://external-api.demo.kalshi.co/trade-api/v2" if env == "demo"
            else "https://api.elections.kalshi.com/trade-api/v2")

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        with KalshiClient(base_url=base) as c:
            data = c._request("GET", "/markets",
                              params={"limit": 200, "status": "open", "mve_filter": "exclude"})
            tickers = [m["ticker"] for m in data.get("markets", [])][:args.auto]
    if not tickers:
        print("[ws] no tickers to subscribe to")
        return 1

    sink: BookEventSink = SqliteBookEventSink(args.db) if args.db else NullSink()
    feed = KalshiWSFeed(url=url, signer=settings.kalshi.signer(), tickers=tickers,
                        channels=tuple(c.strip() for c in args.channels.split(",") if c.strip()),
                        sink=sink)
    print(f"[ws] {env} {url}: {len(tickers)} tickers on ONE connection", flush=True)
    try:
        feed.start()
        feed.run(duration_s=args.duration)
    except KalshiWSAuthError as exc:
        print(f"[ws] AUTH FAILED -- {exc}")
        return 2
    finally:
        feed.close()
        sink.close()

    print(f"[ws] {feed.stats.report()}", flush=True)
    ready = feed.tops()
    print(f"[ws] readable books: {len(ready)}/{len(tickers)}; "
          f"stale: {len(feed.stale_tickers())}", flush=True)
    for t, top in list(ready.items())[:5]:
        print(f"      {t:<40} {top.yes_bid}/{top.yes_ask} "
              f"({top.yes_bid_size:.0f}x{top.yes_ask_size:.0f})", flush=True)
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
