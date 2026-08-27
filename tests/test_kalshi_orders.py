"""T-045: order construction is correct, and destructive calls are gated.

The demo lifecycle itself is exercised by `scripts/demo_order_lifecycle.py`
(it places real mock-money orders).  These tests cover the parts that must be
right BEFORE any order is sent, plus the safety rails.
"""

from __future__ import annotations

import pytest

from venues.kalshi.client import COST_CANCEL, COST_DEFAULT, KalshiClient


class RecordingClient(KalshiClient):
    """Captures requests instead of sending them."""

    def __init__(self):
        super().__init__(base_url="https://example.invalid/trade-api/v2")
        self.calls: list[dict] = []

    def _request(self, method, path, *, params=None, json_body=None,
                 authenticated=False, cost=COST_DEFAULT):
        self.calls.append({"method": method, "path": path, "params": params,
                           "body": json_body, "auth": authenticated, "cost": cost})
        return {"order": {"order_id": "test-oid", "fill_count": "0.00",
                          "remaining_count": "1.00"}}


@pytest.fixture()
def rc():
    return RecordingClient()


# --------------------------------------------------------------------------- #
# Order construction -- fixed-point strings, not floats
# --------------------------------------------------------------------------- #
def test_price_and_count_are_fixed_point_strings(rc):
    rc.create_order(ticker="KXA-1", side="bid", count=7, price_cents=56)
    body = rc.calls[0]["body"]
    assert body["price"] == "0.5600"          # dollars, 4dp
    assert body["count"] == "7.00"            # contracts, 2dp
    assert isinstance(body["price"], str) and isinstance(body["count"], str)


def test_every_price_in_range_round_trips_exactly(rc):
    """Float formatting must not drift -- 33c must never become 0.3299."""
    for cents in range(1, 100):
        rc.calls.clear()
        rc.create_order(ticker="KXA-1", side="bid", count=1, price_cents=cents)
        assert float(rc.calls[0]["body"]["price"]) == pytest.approx(cents / 100, abs=1e-9)


def test_post_only_defaults_true(rc):
    """I1: maker by default.  A taker order must be an explicit choice."""
    rc.create_order(ticker="KXA-1", side="bid", count=1, price_cents=50)
    assert rc.calls[0]["body"]["post_only"] is True


def test_self_trade_prevention_is_always_sent(rc):
    """Required by the API, and a wash-trading compliance matter (CEA 4c(a)).

    `taker_at_cross` keeps resting liquidity and kills the incoming taker --
    what a two-sided quoter wants; cancel-oldest could strip it off the book.
    """
    rc.create_order(ticker="KXA-1", side="bid", count=1, price_cents=50)
    assert rc.calls[0]["body"]["self_trade_prevention_type"] == "taker_at_cross"


def test_cancel_on_pause_defaults_true(rc):
    """Resting orders should die if the exchange pauses -- reopening cancels
    everything anyway, and stale quotes into a resume are donations."""
    rc.create_order(ticker="KXA-1", side="bid", count=1, price_cents=50)
    assert rc.calls[0]["body"]["cancel_order_on_pause"] is True


def test_uses_the_v2_order_path(rc):
    """The legacy /portfolio/orders POST is deprecated."""
    rc.create_order(ticker="KXA-1", side="bid", count=1, price_cents=50)
    assert rc.calls[0]["path"] == "/portfolio/events/orders"
    assert rc.calls[0]["auth"] is True


def test_queue_position_uses_the_v1_path(rc):
    """MEASURED: queue_position exists ONLY under /portfolio/orders/...

    The V2 /portfolio/events/orders/{id}/queue_position path returns 404.
    """
    rc.queue_position("abc")
    assert rc.calls[0]["path"] == "/portfolio/orders/abc/queue_position"


def test_cancels_cost_fewer_tokens_than_creates(rc):
    """Cancels are 2 tokens, creates 10 -- so cancel aggressively, create selectively."""
    rc.create_order(ticker="KXA-1", side="bid", count=1, price_cents=50)
    rc.cancel_order("abc")
    assert rc.calls[0]["cost"] == COST_DEFAULT == 10
    assert rc.calls[1]["cost"] == COST_CANCEL == 2


# --------------------------------------------------------------------------- #
# Validation -- reject before the venue does
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_price", [0, 100, -1, 150])
def test_rejects_prices_outside_the_tick_grid(rc, bad_price):
    with pytest.raises(ValueError, match="price_cents"):
        rc.create_order(ticker="KXA-1", side="bid", count=1, price_cents=bad_price)


@pytest.mark.parametrize("bad_count", [0, -5])
def test_rejects_non_positive_size(rc, bad_count):
    with pytest.raises(ValueError, match="count must be positive"):
        rc.create_order(ticker="KXA-1", side="bid", count=bad_count, price_cents=50)


def test_rejects_an_invalid_side(rc):
    """Side is YES-referenced bid/ask, NOT yes/no -- an easy and costly mixup."""
    with pytest.raises(ValueError, match="side must be"):
        rc.create_order(ticker="KXA-1", side="yes", count=1, price_cents=50)


def test_unsigned_client_cannot_place_an_order():
    """No signer -> no authenticated call, ever."""
    from venues.kalshi.client import KalshiError

    c = KalshiClient(base_url="https://example.invalid/trade-api/v2")
    with pytest.raises(KalshiError) as ei:
        c.create_order(ticker="KXA-1", side="bid", count=1, price_cents=50)
    assert ei.value.status == 401


# --------------------------------------------------------------------------- #
# Kill path
# --------------------------------------------------------------------------- #
def test_cancel_all_iterates_because_the_bulk_endpoint_404s(monkeypatch, rc):
    """MEASURED: DELETE /portfolio/events/orders returns 404 on demo.

    The kill switch has to work on the venue we actually run against, so it
    lists resting orders and cancels them individually.
    """
    monkeypatch.setattr(
        rc, "resting_orders",
        lambda **kw: [{"order_id": "a"}, {"order_id": "b"}, {"order_id": "c"}],
    )
    assert rc.cancel_all_orders() == 3
    cancels = [c for c in rc.calls if c["method"] == "DELETE"]
    assert len(cancels) == 3
    assert all(c["cost"] == COST_CANCEL for c in cancels)


def test_cancel_all_keeps_going_when_one_cancel_fails(monkeypatch, rc):
    """An order that filled between listing and cancelling must not abort the kill."""
    from venues.kalshi.client import KalshiError

    monkeypatch.setattr(
        rc, "resting_orders",
        lambda **kw: [{"order_id": "a"}, {"order_id": "gone"}, {"order_id": "c"}],
    )

    def maybe_fail(order_id):
        if order_id == "gone":
            raise KalshiError(404, "not found", "/x")
        rc.calls.append({"method": "DELETE", "path": f"/cancel/{order_id}",
                         "params": None, "body": None, "auth": True,
                         "cost": COST_CANCEL})

    monkeypatch.setattr(rc, "cancel_order", maybe_fail)
    assert rc.cancel_all_orders() == 2      # a and c, despite 'gone' failing


def test_the_rate_limit_is_what_bounds_the_kill_deadline():
    """I9 is bounded by the TOKEN BUCKET, not by latency -- and my first
    attempt at this test measured the wrong thing.

    The earlier version monkeypatched `cancel_order`, which is the only place
    the write bucket is consumed, so it timed 300 thread-pooled sleeps and
    reported a pass.  Concurrency removes per-call latency stacking; it does not
    buy tokens.  Measured against the REAL bucket with zero network time:

        n= 100 ->  1.00s   n= 300 ->  5.00s   n=1000 -> 19.00s

    So the honest control is a HARD CAP on resting orders, not a faster loop.
    """
    import time

    from venues.kalshi.client import TokenBucket, max_cancellable_within
    from execution.executor import MAX_RESTING_ORDERS

    def drain(n: int) -> float:
        bucket = TokenBucket(capacity=100, refill_per_sec=100)
        t0 = time.monotonic()
        for _ in range(n):
            bucket.take(COST_CANCEL)
        return time.monotonic() - t0

    # the cap the executor enforces must fit the deadline with room to spare
    assert drain(MAX_RESTING_ORDERS) < 3.5

    # ...and the arithmetic that derives it must agree with the real bucket
    budget = max_cancellable_within(5.0)
    assert MAX_RESTING_ORDERS <= budget
    assert drain(budget) <= 5.0

    # the failure this pins: beyond the budget, the deadline is missed
    assert drain(budget + 200) > 5.0


def test_the_resting_cap_is_derived_from_the_deadline_not_guessed():
    from venues.kalshi.client import max_cancellable_within
    assert max_cancellable_within(0) < max_cancellable_within(5) < max_cancellable_within(10)
    assert max_cancellable_within(-1) >= 0          # a past deadline is not negative


def test_cancel_all_counts_only_successes(monkeypatch):
    """An order that filled between listing and cancelling must not abort the kill."""
    from venues.kalshi.client import KalshiError

    rc = RecordingClient()
    monkeypatch.setattr(rc, "resting_orders",
                        lambda **kw: [{"order_id": f"o{i}"} for i in range(10)])

    def flaky(order_id):
        if order_id in ("o3", "o7"):
            raise KalshiError(404, "not found", "/x")
        return {}

    monkeypatch.setattr(rc, "cancel_order", flaky)
    assert rc.cancel_all_orders() == 8
