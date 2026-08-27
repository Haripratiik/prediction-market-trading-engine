"""Kalshi REST client.  T-010.

Design notes that come straight from the research:

  * **Enumerate via `/events?with_nested_markets=true`, NOT `/markets`.** A
    page-capped pull of `/markets` returned 99.3% KXMVE parlay shards and only
    425 real markets (research/05 F2).  `/events` also carries
    `mutually_exclusive`, `settlement_sources` and `collateral_return_type`,
    which nothing else exposes.
  * **`/series` ignores `limit` and returns all ~13.5k in one response** --
    cache it once; it is the whole fee / prohibition / settlement-source map.
  * **Rate limits are a token bucket.** Most requests cost 10 tokens,
    cancellations cost 2.  429 carries NO `Retry-After` or `X-RateLimit-*`
    headers, so back off on your own schedule.
  * Market data needs no auth at all, which is what makes shadow mode free.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.models import Event, Market, Series, SettlementSource
from venues.kalshi.auth import KalshiSigner

PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"
PROD_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
DEMO_WS = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"

# Token costs (research/03).  Cancels are 5x cheaper than creates, which is why
# the quoter should cancel aggressively and re-create selectively.
COST_DEFAULT = 10
COST_CANCEL = 2


def max_cancellable_within(deadline_s: float, *, capacity: float = 100.0,
                           refill_per_sec: float = 100.0,
                           cost: int = COST_CANCEL,
                           reserve_tokens: float = COST_DEFAULT) -> int:
    """How many orders can be cancelled inside `deadline_s`, from the rate limit.

    THE BINDING CONSTRAINT ON I9, and it is arithmetic, not engineering.  The
    write bucket starts full and refills continuously, so by time t it has
    released `capacity + refill*t` tokens; each cancel costs `cost`.  Concurrency
    does NOT help -- it removes per-call latency stacking, not the token spend.

    MEASURED against the real bucket with zero network time:

        n= 100 ->  1.00s   OK
        n= 250 ->  4.00s   OK
        n= 300 ->  5.00s   over the deadline
        n= 400 ->  7.00s   over
        n=1000 -> 19.00s   over

    `reserve_tokens` holds back the cost of the `resting_orders()` GET that has
    to run first.  The answer is a HARD CAP on how many orders may rest at once;
    a kill switch whose guarantee depends on how many orders you happen to be
    holding is not a guarantee, and the honest fix is to stop holding more than
    can be cancelled.
    """
    tokens = capacity + refill_per_sec * max(0.0, deadline_s) - reserve_tokens
    return max(0, int(tokens // max(1, cost)))


@dataclass(frozen=True, slots=True)
class MarketSettlement:
    """How a market actually resolved, straight off the market object.

    `kind` is the honest vocabulary: yes | no | void | scalar | unknown | open.
    A `scalar` is a pro-rata payout and is NOT reducible to yes/no/void -- see
    `KalshiClient.market_settlement`.
    """

    ticker: str
    status: str
    kind: str
    outcome: int | None                  # 1 = YES, 0 = NO, None otherwise
    settlement_value_cents: int | None   # set for scalar resolutions
    settled_at_us: int | None
    expiration_value: str = ""

    @property
    def is_settled(self) -> bool:
        return self.kind in ("yes", "no", "void", "scalar")

    @property
    def pays_binary(self) -> bool:
        """True only when the contract paid a clean $1 or $0.

        The RV sleeves may only assume a $1-capped basket payout when EVERY leg
        is binary.  A scalar leg breaks that arithmetic.
        """
        return self.kind in ("yes", "no")


def _iso_to_us(value: object) -> int | None:
    """RFC3339 -> microseconds since epoch.  None when absent or unparseable."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1_000_000)
    except ValueError:
        return None


class KalshiError(RuntimeError):
    """Non-retryable API error."""

    def __init__(self, status: int, body: str, path: str) -> None:
        super().__init__(f"Kalshi {status} on {path}: {body[:300]}")
        self.status = status
        self.body = body
        self.path = path


@dataclass
class TokenBucket:
    """Read/write token buckets.  Basic tier: 200 read / 100 write per second."""

    capacity: float
    refill_per_sec: float
    _tokens: float = field(init=False)
    _last: float = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last = time.monotonic()

    def take(self, cost: float) -> None:
        """Block until `cost` tokens are available."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.refill_per_sec
                )
                self._last = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                deficit = cost - self._tokens
                wait = deficit / self.refill_per_sec
            time.sleep(min(wait, 1.0))


class KalshiClient:
    """Synchronous REST client.

    Public market data works with no credentials; pass a `signer` to reach
    portfolio and order endpoints.
    """

    def __init__(
        self,
        *,
        base_url: str = PROD_BASE,
        signer: KalshiSigner | None = None,
        timeout: float = 30.0,
        read_tps: float = 200.0,
        write_tps: float = 100.0,
        max_retries: int = 5,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.max_retries = max_retries
        self._reads = TokenBucket(read_tps, read_tps)
        self._writes = TokenBucket(write_tps, write_tps)
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "predictionmarkets/0.1", "Accept": "application/json"},
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ core
    @property
    def path_prefix(self) -> str:
        """The path Kalshi signs, e.g. '/trade-api/v2'."""
        return "/" + self.base_url.split("//", 1)[1].split("/", 1)[1]

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        authenticated: bool = False,
        cost: int = COST_DEFAULT,
    ) -> dict[str, Any]:
        is_write = method.upper() != "GET"
        (self._writes if is_write else self._reads).take(cost)

        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {}
        if authenticated:
            if self.signer is None:
                raise KalshiError(401, "no signer configured", path)
            headers = self.signer.headers(method, f"{self.path_prefix}{path}")

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.HTTPError as exc:      # transient network
                last_error = exc
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                # 429 carries no Retry-After; use our own bounded backoff.
                last_error = KalshiError(resp.status_code, resp.text, path)
                self._sleep_backoff(attempt)
                continue
            if resp.status_code >= 400:
                raise KalshiError(resp.status_code, resp.text, path)
            if not resp.content:
                return {}
            return dict(resp.json())

        raise KalshiError(
            getattr(last_error, "status", 599), str(last_error), path
        ) from last_error

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        time.sleep(min(2.0 ** attempt, 30.0) * (0.5 + random.random()))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- exchange
    def exchange_status(self) -> dict[str, Any]:
        """Circuit breaker.  Poll before quoting.

        `exchange_index_statuses` reports per-shard `trading_active`
        INDEPENDENTLY (0=Default, 1=Combos, 2=Crypto, 3=Tennis/Baseball).  One
        shard can halt while others trade -- a direct source of stale
        cross-shard quotes (research/06 K14).
        """
        return self._request("GET", "/exchange/status")

    def is_trading_active(self, exchange_index: int | None = None) -> bool:
        st = self.exchange_status()
        if exchange_index is None:
            return bool(st.get("exchange_active") and st.get("trading_active", True))
        for shard in st.get("exchange_index_statuses", []):
            if shard.get("exchange_index") == exchange_index:
                return bool(shard.get("exchange_active"))
        return False

    # ---------------------------------------------------------------- series
    def list_series(self) -> list[Series]:
        """All series in one call.  `limit` is ignored by the API; ~13.5k rows.

        This is the fee / prohibition / settlement-source map every other
        component reads from.  Cache it once per session (PLAN.md 6.1).
        """
        data = self._request("GET", "/series", params={"limit": 200})
        return [Series.from_api(s) for s in data.get("series", [])]

    # ---------------------------------------------------------------- events
    def iter_events(
        self,
        *,
        status: str = "open",
        with_markets: bool = True,
        page_size: int = 200,
        max_pages: int = 400,
        sleep_between: float = 0.25,
    ) -> Iterator[dict[str, Any]]:
        """Page `/events`.  THE universe enumerator -- see the module docstring."""
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": page_size, "status": status}
            if with_markets:
                params["with_nested_markets"] = "true"
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/events", params=params)
            batch = data.get("events") or []
            yield from batch
            cursor = data.get("cursor")
            if not cursor or not batch:
                return
            time.sleep(sleep_between)

    def fetch_universe(self, **kwargs: Any) -> tuple[list[Event], list[Market]]:
        """Enumerate open events and their markets, skipping MVE parlay shards."""
        events: list[Event] = []
        markets: list[Market] = []
        for raw in self.iter_events(**kwargs):
            series = raw.get("series_ticker") or ""
            if series.startswith("KXMVE"):
                continue
            events.append(
                Event(
                    event_ticker=raw.get("event_ticker", ""),
                    series_ticker=series,
                    category=raw.get("category") or "",
                    title=raw.get("title") or "",
                    mutually_exclusive=bool(raw.get("mutually_exclusive")),
                    collateral_return_type=raw.get("collateral_return_type") or "",
                    settlement_sources=tuple(
                        SettlementSource(**s)
                        for s in (raw.get("settlement_sources") or [])
                        if isinstance(s, dict)
                    ),
                )
            )
            for m in raw.get("markets") or []:
                markets.append(Market.from_api(m, series_ticker=series))
        return events, markets

    # --------------------------------------------------------------- markets
    def get_market(self, ticker: str) -> Market:
        data = self._request("GET", f"/markets/{ticker}")
        return Market.from_api(data.get("market") or {})

    def list_markets(self, *, mve_filter: str = "exclude", **params: Any) -> list[Market]:
        """`mve_filter=exclude` drops the ~117k multivariate parlay markets."""
        params.setdefault("limit", 1000)
        params["mve_filter"] = mve_filter
        data = self._request("GET", "/markets", params=params)
        return [Market.from_api(m) for m in data.get("markets", [])]

    def get_orderbook(self, ticker: str, *, depth: int = 10) -> dict[str, Any]:
        """Requires auth.  Note the book is YES-referenced: yes_ask = 100 - best_no_bid."""
        return self._request(
            "GET", f"/markets/{ticker}/orderbook",
            params={"depth": depth}, authenticated=True,
        )

    # ------------------------------------------------------------- portfolio
    def balance(self) -> dict[str, Any]:
        return self._request("GET", "/portfolio/balance", authenticated=True)

    def positions(self, **params: Any) -> dict[str, Any]:
        """NOTE: this endpoint lags fills by ~1s.  Build position from FILLS (I4)."""
        return self._request("GET", "/portfolio/positions", params=params, authenticated=True)

    def fills(self, **params: Any) -> dict[str, Any]:
        """Fills are YES-referenced -- convert for NO sides (research/03)."""
        return self._request("GET", "/portfolio/fills", params=params, authenticated=True)

    def iter_fills(self, *, page_size: int = 200, max_pages: int = 200,
                   sleep_between: float = 0.1,
                   **params: Any) -> Iterator[dict[str, Any]]:
        """Page `/portfolio/fills`, oldest first when `min_ts` is supplied.

        Fills are the ONLY source of position truth (I4).  `/portfolio/positions`
        lags them by about a second, and a position counter maintained in memory
        is wrong the moment a process restarts -- so this is what gets replayed.
        """
        cursor: str | None = None
        for _ in range(max_pages):
            q: dict[str, Any] = {"limit": page_size, **params}
            if cursor:
                q["cursor"] = cursor
            data = self.fills(**q)
            batch = data.get("fills") or []
            yield from batch
            cursor = data.get("cursor")
            if not cursor or not batch:
                return
            time.sleep(sleep_between)

    def settlements(self, **params: Any) -> dict[str, Any]:
        """Settled POSITIONS of this account -- not a market-data endpoint.

        It reports what the account was paid, so it is empty in shadow mode by
        construction: no orders were ever sent, so nothing settled to us.  Shadow
        settlement has to come from MARKET data instead (`market_result`).
        """
        return self._request("GET", "/portfolio/settlements", params=params,
                             authenticated=True)

    def iter_settlements(self, *, page_size: int = 200, max_pages: int = 200,
                         sleep_between: float = 0.1,
                         **params: Any) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        for _ in range(max_pages):
            q: dict[str, Any] = {"limit": page_size, **params}
            if cursor:
                q["cursor"] = cursor
            data = self.settlements(**q)
            batch = data.get("settlements") or []
            yield from batch
            cursor = data.get("cursor")
            if not cursor or not batch:
                return
            time.sleep(sleep_between)

    def market_settlement(self, ticker: str) -> "MarketSettlement":
        """Everything the market object says about how it resolved.

        `market_result()` collapses this to (status, outcome, voided) and loses
        two things that matter:

        SCALAR RESOLUTIONS.  MEASURED on the live public API: 199 of 21,000
        settled markets (0.95%) report `result='scalar'` -- a PRO-RATA payout,
        not yes/no and not void-at-cost.  Verified example:

            KXMLBRBI-26AUG261910MILNYM-MILJCHOURIO11-2
            status='finalized'  result='scalar'
            settlement_value_dollars='0.1600'  expiration_value='Cancelled'

        It pays 16c.  `market_result()` reports `(status, None, False)` for it,
        which differs from an open market only in `status` -- so any caller
        keying off the outcome alone treats a settled scalar as still open.

        This is not a curiosity for the arbitrage sleeves.  S2's entire premise
        is that exactly one leg of a mutually-exclusive basket pays $1.  A scalar
        leg pays something in between, so the basket's payout is neither $1 nor
        $0 and the "liability capped at $1" guarantee does not hold as stated.

        SETTLEMENT TIME.  `settlement_ts` is already in the response this method
        fetches (verified: present on settled markets, None on open ones), so
        returning it costs nothing and makes `settled_at_us` exact rather than
        inferred from poll time.
        """
        data = self._request("GET", f"/markets/{ticker}")
        m = data.get("market") if isinstance(data.get("market"), dict) else data
        status = str(m.get("status") or "").lower()
        result = str(m.get("result") or "").lower()

        value_cents: int | None = None
        raw_value = m.get("settlement_value_dollars")
        if raw_value not in (None, ""):
            try:
                value_cents = int(round(float(raw_value) * 100))
            except (TypeError, ValueError):
                value_cents = None

        settled_at_us = _iso_to_us(m.get("settlement_ts"))

        if result in ("void", "voided", "canceled", "cancelled"):
            kind, outcome = "void", None
        elif result == "yes":
            kind, outcome = "yes", 1
        elif result == "no":
            kind, outcome = "no", 0
        elif result == "scalar":
            kind, outcome = "scalar", None
        elif result:
            kind, outcome = "unknown", None
        else:
            kind, outcome = "open", None

        return MarketSettlement(
            ticker=ticker, status=status, kind=kind, outcome=outcome,
            settlement_value_cents=value_cents, settled_at_us=settled_at_us,
            expiration_value=str(m.get("expiration_value") or ""),
        )

    def market_result(self, ticker: str) -> tuple[str, int | None, bool]:
        """(status, outcome, voided) read from MARKET data, no credentials needed.

        This is the settlement source that works in shadow mode.  `result` is ''
        while the market is open, then 'yes'/'no' once determined; Kalshi also
        uses 'void'/'canceled' for a market that paid nobody, which is NOT the
        same as a NO resolution and must never be scored as one -- a voided
        market returns every position at cost.
        """
        data = self._request("GET", f"/markets/{ticker}")
        m = data.get("market") if isinstance(data.get("market"), dict) else data
        status = str(m.get("status") or "").lower()
        result = str(m.get("result") or "").lower()
        if result in ("void", "voided", "canceled", "cancelled"):
            return status, None, True
        if result == "yes":
            return status, 1, False
        if result == "no":
            return status, 0, False
        return status, None, False

    def queue_position(self, order_id: str) -> dict[str, Any]:
        """Shares resting AHEAD of your order.  VERIFIED WORKING on demo.

        NOTE the path: this exists ONLY under `/portfolio/orders/...`, not the V2
        `/portfolio/events/orders/...` prefix (which 404s).

        Ground truth for calibrating the fill model, and almost nobody has a
        labelled queue-position dataset (PLAN.md T-044b).
        """
        return self._request(
            "GET", f"/portfolio/orders/{order_id}/queue_position", authenticated=True
        )

    # ---------------------------------------------------------------- orders
    # V2 shape: single-book bid/ask side + fixed-point DOLLAR STRINGS.
    # The legacy /portfolio/orders path is deprecated (no earlier than 2026-05-06).
    def create_order(
        self,
        *,
        ticker: str,
        side: str,                      # "bid" = buy YES, "ask" = sell YES
        count: int,
        price_cents: int,
        client_order_id: str | None = None,
        time_in_force: str = "good_till_canceled",
        post_only: bool = True,         # I1: maker by default
        self_trade_prevention_type: str = "taker_at_cross",
        cancel_order_on_pause: bool = True,
        reduce_only: bool = False,
        exchange_index: int | None = None,
    ) -> dict[str, Any]:
        """Place an order.  Prices and counts are FIXED-POINT DOLLAR STRINGS.

        `side` is YES-referenced: "bid" buys YES, "ask" sells YES.  Buying NO at
        price p is the same as selling YES at (1 - p), i.e. side="ask".

        `self_trade_prevention_type` is REQUIRED by the API and is also a
        compliance matter -- self-crossing reads as wash trading (CEA 4c(a)).
        `taker_at_cross` cancels the incoming taker and keeps resting liquidity,
        which is what a two-sided quoter wants.
        """
        if not 1 <= price_cents <= 99:
            raise ValueError(f"price_cents must be 1..99, got {price_cents}")
        if count <= 0:
            raise ValueError("count must be positive")
        if side not in ("bid", "ask"):
            raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")

        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "count": f"{count:.2f}",
            "price": f"{price_cents / 100:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention_type,
            "post_only": post_only,
            "cancel_order_on_pause": cancel_order_on_pause,
            "reduce_only": reduce_only,
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        if exchange_index is not None:
            body["exchange_index"] = exchange_index
        return self._request(
            "POST", "/portfolio/events/orders", json_body=body, authenticated=True
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel one resting order.  Cancels cost 2 tokens vs 10 for a create."""
        return self._request(
            "DELETE", f"/portfolio/events/orders/{order_id}",
            authenticated=True, cost=COST_CANCEL,
        )

    def cancel_all_orders(self, *, max_workers: int = 16) -> int:
        """Cancel every resting order.  I9 kill path -- must work from any state.

        MEASURED: the documented bulk `DELETE /portfolio/events/orders` returns
        404 on the demo exchange, so this must iterate.

        It cancels CONCURRENTLY because serial cancellation breaks the 5-second
        SLA that makes I9 meaningful.  Measured serially through the write bucket
        (100 tokens/s, 2 per cancel), pure rate-limit wait with zero network
        time: 100 orders -> 1.10s, **300 -> 5.10s**, 400 -> 7.10s.  Add a 50ms
        round trip per call and the serial path caps out nearer 100 orders.  A
        kill switch whose guarantee silently depends on how many orders you
        happen to be resting is not a guarantee.

        The token bucket is thread-safe, so it still paces the writes correctly;
        concurrency removes the per-call latency stacking, not the rate limit.
        Returns the number cancelled.
        """
        from concurrent.futures import ThreadPoolExecutor

        oids = [o.get("order_id") for o in self.resting_orders()]
        oids = [o for o in oids if o]
        if not oids:
            return 0

        def _cancel(oid: str) -> bool:
            try:
                self.cancel_order(oid)
                return True
            except KalshiError:
                return False      # already gone or filled -- keep cancelling the rest

        with ThreadPoolExecutor(max_workers=min(max_workers, len(oids))) as pool:
            return sum(pool.map(_cancel, oids))

    def list_orders(self, **params: Any) -> dict[str, Any]:
        return self._request(
            "GET", "/portfolio/orders", params=params, authenticated=True
        )

    def resting_orders(self, **params: Any) -> list[dict[str, Any]]:
        """All currently-resting orders."""
        params.setdefault("status", "resting")
        return list(self.list_orders(**params).get("orders", []))

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        """Look up one order.

        MEASURED: single-order GET returns 404 on the demo (both the V1 and V2
        paths), so this filters the list endpoint instead.  Returns None if the
        order is not currently resting.
        """
        for o in self.resting_orders():
            if o.get("order_id") == order_id:
                return o
        return None

    def flatten_position(self, ticker: str) -> dict[str, Any] | None:
        """Close any open position in `ticker` with a marketable order.

        Used to leave a test account clean.  Sells YES if long, buys YES if short.
        """
        for p in self.positions().get("market_positions", []):
            if p.get("ticker") != ticker:
                continue
            try:
                net = int(float(str(p.get("position_fp") or 0)))
            except (TypeError, ValueError):
                continue
            if net == 0:
                return None
            side = "ask" if net > 0 else "bid"       # sell YES if long
            price = 1 if net > 0 else 99             # cross aggressively
            return self.create_order(
                ticker=ticker, side=side, count=abs(net), price_cents=price,
                post_only=False, time_in_force="immediate_or_cancel",
            )
        return None
