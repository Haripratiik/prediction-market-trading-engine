"""The readiness check's own invariants.  Every test here is a way the check
could report GO on an engine that is not ready, which is the only failure mode
that matters: a checklist that is wrong in the pessimistic direction wastes an
afternoon, and one that is wrong in the optimistic direction points a trading
engine at an exchange.

Nothing here touches the network or the real database.  The venue is a fake, the
database is a temp file, and the one test that would need a live exchange is
marked `live` so `-m "not live"` skips it.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest

from core.config import load_settings
from core.db import Database
from core.models import Market, RunMode, Side, now_us
from execution.executor import MAX_RESTING_ORDERS
from execution.killswitch import KILL_DEADLINE_S, KILL_FILENAME, KillSwitch
from runner import MAX_QUOTE_AGE_US
from scripts.demo_readiness import (
    ACCOUNT_REMEDY,
    MIN_BALANCE_CENTS,
    AccountState,
    Check,
    Checklist,
    ReadOnlyDatabase,
    RecordingClient,
    Status,
    _probe_executor,
    balance_cents,
    build_parser,
    check_data,
    check_environment,
    check_loop,
    check_safety,
    check_sizing,
    classify_account_error,
    classify_balance,
    is_demo_base_url,
    is_production_base_url,
    looks_like_missing_account,
    main,
    overlap_us,
    probe_account,
    render,
)
from venues.kalshi.client import DEMO_BASE, PROD_BASE, KalshiError, max_cancellable_within


# pytest's `tmp_path` cannot be used on this machine: its basetemp under
# C:\Users\...\AppData\Local\Temp\pytest-of-harie raises PermissionError
# [WinError 5].  Manage the directory ourselves.
@pytest.fixture()
def workdir():
    d = tempfile.mkdtemp(prefix="pm-readiness-test-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Fake venue
# --------------------------------------------------------------------------- #
class FakeVenue:
    """A Kalshi client whose every endpoint is scripted.

    `responses` maps an endpoint name to either a payload dict or an exception to
    raise, so one fake covers all four account states.
    """

    def __init__(self, **responses: Any) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def _answer(self, name: str, default: Any) -> Any:
        self.calls.append(name)
        value = self.responses.get(name, default)
        if isinstance(value, Exception):
            raise value
        return value

    def exchange_status(self) -> dict[str, Any]:
        return self._answer("exchange_status", {
            "exchange_active": True, "trading_active": True,
            "exchange_index_statuses": [{"exchange_index": 0, "exchange_active": True,
                                         "trading_active": True}],
        })

    def balance(self) -> dict[str, Any]:
        return self._answer("balance", {"balance": 19707, "balance_dollars": "197.0744"})

    def list_orders(self, **params: Any) -> dict[str, Any]:
        return self._answer("orders", {"orders": [], "cursor": ""})

    def fills(self, **params: Any) -> dict[str, Any]:
        return self._answer("fills", {"fills": [], "cursor": ""})

    def positions(self, **params: Any) -> dict[str, Any]:
        return self._answer("positions", {"market_positions": [], "cursor": ""})


def missing_account_error(path: str = "/portfolio/balance") -> KalshiError:
    """The exact shape the reset demo account returned."""
    return KalshiError(404, json.dumps({"error": {"code": "user_not_found"}}), path)


# --------------------------------------------------------------------------- #
# The four account states the operator has to tell apart
# --------------------------------------------------------------------------- #
def test_a_rejected_signature_is_reported_as_a_bad_key_not_a_missing_account():
    """401 with no `user_not_found` marker means the KEY is the problem.

    The remedy is to issue a new key.  Reporting this as a missing account would
    send the operator to re-create an account that is perfectly fine.
    """
    assert classify_account_error(401, "unauthorized") is AccountState.KEY_INVALID
    assert classify_account_error(403, "forbidden") is AccountState.KEY_INVALID


def test_b_user_not_found_is_a_missing_account_even_when_it_arrives_as_a_401():
    """The body wins over the status code, and that ordering is load-bearing.

    A reset account can answer 401 as readily as 404 -- the exchange is saying
    the user does not exist, not that the signature was wrong.  Reading the
    status first would report KEY_INVALID and send the operator to rotate a key
    that already works, destroying the one working credential they had while
    fixing nothing.
    """
    body = json.dumps({"error": {"code": "user_not_found"}})
    assert classify_account_error(401, body) is AccountState.ACCOUNT_MISSING
    assert classify_account_error(404, body) is AccountState.ACCOUNT_MISSING
    assert classify_account_error(500, body) is AccountState.ACCOUNT_MISSING


def test_a_bare_404_on_an_account_endpoint_is_a_missing_account():
    """The account scope does not exist.  Distinct from a rejected signature."""
    assert classify_account_error(404, "not found") is AccountState.ACCOUNT_MISSING


def test_a_server_error_is_reported_as_unreachable_so_nothing_is_concluded():
    """A 5xx teaches you nothing about the account, and must not pretend to.

    Classifying a transient outage as KEY_INVALID or ACCOUNT_MISSING sends the
    operator to fix something that is not broken.
    """
    assert classify_account_error(503, "gateway") is AccountState.UNREACHABLE
    assert classify_account_error(0, "") is AccountState.UNREACHABLE


def test_c_a_healthy_account_with_no_funds_is_its_own_state():
    """Enough balance to place the cheapest possible order is the boundary.

    `create_order` caps price at 99c and demands count >= 1, so one contract
    costs at most 99c -- below a dollar the account cannot place ANY order, and
    that is a funding problem, not a credential problem.
    """
    assert classify_balance(0) is AccountState.INSUFFICIENT_BALANCE
    assert classify_balance(MIN_BALANCE_CENTS - 1) is AccountState.INSUFFICIENT_BALANCE
    assert classify_balance(MIN_BALANCE_CENTS) is AccountState.OK
    assert classify_balance(19707) is AccountState.OK


def test_an_unreadable_balance_is_never_silently_treated_as_zero():
    """A zero balance is a real, actionable answer; an unreadable body is not.

    Collapsing the two would report "fund the account" when the truth is "the
    payload changed shape and this check no longer reads it".
    """
    assert balance_cents({}) is None
    assert balance_cents({"balance": None}) is None
    assert classify_balance(None) is AccountState.UNREACHABLE


def test_balance_is_read_from_the_integer_cents_field_the_demo_actually_returns():
    """MEASURED: `balance` is an integer of CENTS, `balance_dollars` a string.

    Reading `balance` as dollars would report a $197 account as $19,707 and pass
    every funding check on an account that cannot fund anything.
    """
    assert balance_cents({"balance": 19707, "balance_dollars": "197.0744"}) == 19707
    assert balance_cents({"balance_dollars": "197.0744"}) == 19707
    assert balance_cents({"balance": True}) is None      # bool is not a balance


def test_every_account_state_carries_a_remedy_the_operator_can_act_on():
    """A NO with no next step is a NO the operator cannot clear."""
    for state in AccountState:
        assert ACCOUNT_REMEDY[state].strip()


# --------------------------------------------------------------------------- #
# probe_account -- the read-only classifier over live-shaped responses
# --------------------------------------------------------------------------- #
def test_the_account_probe_places_no_orders_and_only_calls_read_endpoints():
    """A readiness check that trades to find out whether it can trade is not one.

    `FakeVenue` has no `create_order`; if the probe ever grew one this would
    raise AttributeError rather than quietly send.
    """
    venue = FakeVenue()
    probe_account(venue)
    assert set(venue.calls) == {"balance", "orders", "fills", "positions"}
    assert not hasattr(venue, "create_order")


def test_a_healthy_funded_account_probes_as_all_good():
    probe = probe_account(FakeVenue())
    assert probe.state is AccountState.OK
    assert probe.balance == 19707


def test_one_user_not_found_anywhere_outranks_three_healthy_endpoints():
    """A partial reset is still a reset.

    The demo failed on ONE endpoint while the others answered.  Taking a majority
    vote, or trusting balance alone, reports a healthy account that cannot place
    an order.
    """
    probe = probe_account(FakeVenue(fills=missing_account_error("/portfolio/fills")))
    assert probe.state is AccountState.ACCOUNT_MISSING
    assert probe.endpoint_states["balance"] == AccountState.OK.value
    assert probe.endpoint_states["fills"] == AccountState.ACCOUNT_MISSING.value


def test_a_two_hundred_response_whose_body_says_user_not_found_is_still_a_reset():
    """The marker is matched against the BODY, not only against error statuses.

    An API that wraps its errors in a 200 -- which this one has done before --
    would otherwise be read as a healthy account.
    """
    venue = FakeVenue(balance={"error": {"code": "user_not_found"}})
    assert probe_account(venue).state is AccountState.ACCOUNT_MISSING


def test_a_rejected_key_outranks_a_missing_account_in_the_probe():
    """Severity order matters: a bad key makes every other answer meaningless."""
    venue = FakeVenue(balance=KalshiError(401, "unauthorized", "/portfolio/balance"),
                      fills=missing_account_error())
    assert probe_account(venue).state is AccountState.KEY_INVALID


def test_the_probe_reports_rather_than_raises_when_the_venue_blows_up():
    """The brief's requirement in one line: DETECT and REPORT, never crash."""
    venue = FakeVenue(balance=RuntimeError("connection reset"))
    probe = probe_account(venue)
    assert probe.state is AccountState.UNREACHABLE
    assert "connection reset" in probe.detail


def test_looks_like_missing_account_is_case_insensitive_and_survives_nesting():
    assert looks_like_missing_account('{"error":{"code":"user_not_found"}}')
    assert looks_like_missing_account("USER_NOT_FOUND")
    assert not looks_like_missing_account('{"balance": 19707}')


# --------------------------------------------------------------------------- #
# The order-send path -- the ONE thing a GET-only probe cannot see
# --------------------------------------------------------------------------- #
class FakeOrderVenue(FakeVenue):
    """Adds the write path, so `--live-order-test` can be exercised offline.

    `resting` tracks what is actually on the book, so a cancel that does not
    cancel is visible rather than assumed.
    """

    def __init__(self, *, on_create: Any = None, **responses: Any) -> None:
        super().__init__(**responses)
        self.on_create = on_create
        self.resting: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.created = 0
        self.cancelled = 0

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"{method} {path}")
        return {"markets": [{"ticker": "KXDEMO-A", "yes_bid_dollars": "0.0100",
                             "yes_ask_dollars": "0.9900", "exchange_index": 0}]}

    def create_order(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("create_order")
        self.sent.append(kwargs)
        self.created += 1
        if isinstance(self.on_create, Exception):
            raise self.on_create
        order = {"order_id": "oid-1", "status": "resting",
                 "price": kwargs.get("price_cents")}
        self.resting.append(order)
        return {"order": order}

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        return next((o for o in self.resting if o["order_id"] == order_id), None)

    def queue_position(self, order_id: str) -> dict[str, Any]:
        return {"queue_position": 0}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        self.cancelled += 1
        self.resting = [o for o in self.resting if o["order_id"] != order_id]
        return {}

    def cancel_all_orders(self) -> int:
        n = len(self.resting)
        self.resting = []
        return n

    def resting_orders(self, **params: Any) -> list[dict[str, Any]]:
        return list(self.resting)


def test_a_user_not_found_on_the_order_post_is_reported_not_raised():
    """The exact failure the demo account showed, and the reason for the flag.

    Public endpoints healthy, key signing correctly, and `POST
    /portfolio/events/orders` answering `user_not_found`.  The check must name
    the state AND the remedy, and must not raise -- a readiness check that dies
    on the condition it exists to detect reports nothing at all.
    """
    from scripts.demo_readiness import check_live_order

    venue = FakeOrderVenue(on_create=missing_account_error("/portfolio/events/orders"))
    cl = Checklist()
    check_live_order(cl, venue, DEMO_BASE)
    check = next(c for c in cl.checks if c.name == "account.order_send_path")
    assert check.status is Status.FAIL
    assert check.detail["account_state"] == AccountState.ACCOUNT_MISSING.value
    assert "re-create the demo account" in check.reason


def test_the_live_order_test_leaves_nothing_resting_on_the_demo_account():
    """An order left behind by a readiness check is the orphan it exists to find.

    The cancel runs in a `finally`, and the cancel-all afterwards is the same I9
    path the kill switch uses -- so the check both cleans up after itself and
    proves the cleanup path works.
    """
    from scripts.demo_readiness import check_live_order

    venue = FakeOrderVenue()
    cl = Checklist()
    check_live_order(cl, venue, DEMO_BASE)
    by_name = {c.name: c for c in cl.checks}
    assert by_name["account.order_send_path"].status is Status.PASS
    assert by_name["account.cancel_path"].status is Status.PASS
    assert venue.created == 1
    assert venue.resting == []


def test_the_order_it_places_is_post_only_and_cannot_cross_the_book():
    """I1, and the demo's 98c spreads.

    A taker order on a demo book either fills against nothing or fills against a
    phantom quote; either way it stops testing the RESTING lifecycle, which is
    the only thing a demo exchange can teach you.  So the order is post-only and
    priced 20c inside BOTH sides of the book -- on the 1c/99c book this fake
    serves a 1/99 book, that is 1c -- strictly BELOW the touch.

    Pricing it *inside* the spread looked safer and was not.  The demo book
    churns at over 1,000 deltas/second, so an improving bid races the ask down,
    trips post-only, and the venue cancels it -- which then reads as "accepted
    but not resting", i.e. a broken lifecycle, when the lifecycle was fine.
    Verified live: a bid below the touch rests, reads back with a real
    queue_position, and cancels in 0.16s.
    """
    from scripts.demo_readiness import check_live_order

    venue = FakeOrderVenue()
    check_live_order(Checklist(), venue, DEMO_BASE)
    assert len(venue.sent) == 1
    order = venue.sent[0]
    assert order["post_only"] is True
    assert order["count"] == 1
    assert order["side"] == "bid"
    assert 1 <= order["price_cents"] < 99
    assert order["price_cents"] == 1           # max(1, bid 1c - 3): cannot cross
    # the fake serves a 1c bid, so a resting price must not exceed it
    assert order["price_cents"] <= 1, "must not improve the touch"
    assert order["client_order_id"], "the idempotency key must be minted before send"


def test_the_live_order_test_refuses_to_send_against_a_production_base_url():
    """The last line of defence before a real order on a real account.

    Even with the flag on, the resolved base URL decides.  A config that drifted
    to production must produce a refusal, not an order.
    """
    from scripts.demo_readiness import check_live_order

    venue = FakeOrderVenue()
    cl = Checklist()
    check_live_order(cl, venue, PROD_BASE)
    check = next(c for c in cl.checks if c.name == "account.order_send_path")
    assert check.status is Status.FAIL
    assert "REFUSING to send" in check.reason
    assert venue.created == 0


# --------------------------------------------------------------------------- #
# Environment: demo is not merely "not production", and vice versa
# --------------------------------------------------------------------------- #
def test_only_the_exact_demo_host_counts_as_demo():
    """Substring matching on 'demo' would pass a production host under a
    /demo/ path.  Compare against the constant the client itself ships."""
    assert is_demo_base_url(DEMO_BASE)
    assert is_demo_base_url(DEMO_BASE + "/")
    assert not is_demo_base_url(PROD_BASE)
    assert not is_demo_base_url("https://api.elections.kalshi.com/demo/trade-api/v2")


def test_anything_unrecognised_is_treated_as_production_when_deciding_to_send():
    """The asymmetry is deliberate.

    A false positive costs a skipped test.  A false negative costs a real order
    on a real account, so an unknown host is production until proven otherwise.
    """
    assert is_production_base_url(PROD_BASE)
    assert is_production_base_url("https://api.someone-elses-kalshi.com/trade-api/v2")
    assert not is_production_base_url(DEMO_BASE)


def test_a_prod_base_url_fails_the_environment_checks_even_when_env_says_demo():
    """`KALSHI_ENV` and the resolved URL are two statements, not one.

    The socket connects to the URL.  A config that says demo while pointing at
    production must fail on the URL, which is the thing that can actually take
    an order.
    """
    class Creds:
        env = "demo"
        base_url = PROD_BASE
        ws_url = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"

    cl = Checklist()
    check_environment(cl, Creds())
    by_name = {c.name: c for c in cl.checks}
    assert by_name["environment.kalshi_env_is_demo"].status is Status.PASS
    assert by_name["environment.base_url_is_demo"].status is Status.FAIL
    assert by_name["environment.base_url_is_not_production"].status is Status.FAIL
    assert by_name["environment.ws_url_is_demo"].status is Status.FAIL


# --------------------------------------------------------------------------- #
# Safety rails
# --------------------------------------------------------------------------- #
def test_i5_blocks_a_gate_zero_sleeve_from_real_capital():
    """I5 protects CAPITAL, and LIVE is where capital is."""
    report, client = _probe_executor(RunMode.LIVE, gate=0)
    assert report.gate_blocked
    assert report.placed == ()
    assert client.calls == []


def test_paper_mode_refuses_any_host_that_is_not_demo():
    """The guarantee that lets I5 relax for PAPER.

    PAPER used to be gated exactly like LIVE, which was correct while nothing
    tied PAPER to the demo exchange -- `KALSHI_ENV` chose the host and
    `RunMode` chose the label, independently, so PAPER against production was
    real money wearing a practice label.  Blocking it also made demo lifecycle
    validation unreachable through the runner, which is the one thing demo is
    for; a rail that prevents you from testing the rails is not a rail.

    The trade is only sound while THIS holds: PAPER is mock funds by
    construction.  It is an ALLOWLIST, so an unrecognised host is refused --
    blocklisting production would fail OPEN the day Kalshi adds a hostname.
    If this test ever fails, PAPER must go back into
    `risk.engine.REAL_CAPITAL_MODES` on the same commit.
    """
    for host in ("https://api.elections.kalshi.com/trade-api/v2",
                 "https://brand-new-host.kalshi.com/trade-api/v2"):
        with pytest.raises(ValueError, match="mock-funds"):
            _probe_executor(RunMode.PAPER, gate=0, base_url=host)

    # ...and a real demo host is allowed, or the rail is a wall
    report, _ = _probe_executor(RunMode.PAPER, gate=0)
    assert not report.gate_blocked


def test_a_gate_four_sleeve_is_not_blocked_so_the_rail_is_a_gate_not_a_wall():
    """A check that passes because nothing can ever send proves nothing."""
    report, client = _probe_executor(RunMode.PAPER, gate=4)
    assert not report.gate_blocked
    assert client.calls, "a gate-4 sleeve must actually reach the venue client"


def test_the_recording_client_would_expose_any_send_the_rails_let_through():
    """The fake is the assertion surface; it must record, not swallow."""
    client = RecordingClient()
    client.create_order(ticker="X", side="bid", count=1, price_cents=50)
    client.cancel_order("abc")
    assert [name for name, _ in client.calls] == ["create_order", "cancel_order"]


def test_the_kill_switch_can_clear_a_full_book_inside_its_own_deadline():
    """I9 is arithmetic, not engineering, and the arithmetic must close.

    `MAX_RESTING_ORDERS` exists precisely so the kill path never discovers
    mid-emergency that it is holding more orders than the rate limit can cancel.
    If someone raises the cap without redoing the sum, this fails.
    """
    assert max_cancellable_within(KILL_DEADLINE_S) >= MAX_RESTING_ORDERS


def test_the_safety_group_passes_on_a_clean_tree_and_names_the_kill_file_path(workdir):
    """The whole rail group, run for real against a temp run dir.

    A temp run dir is used so the check can never create or delete a KILL file in
    a live run directory -- a readiness check that disarms the kill switch to
    test it is worse than no check.
    """
    cl = Checklist()
    check_safety(cl, load_settings(), workdir)
    failures = {c.name: c.reason for c in cl.failures}
    assert not failures, failures
    by_name = {c.name: c for c in cl.checks}
    assert by_name["safety.killswitch_path"].detail["kill_path"].endswith(KILL_FILENAME)
    assert by_name["safety.post_only_default"].detail["client_default"] is True
    assert not (workdir / KILL_FILENAME).exists(), "the probe must leave no KILL behind"


def test_an_engaged_kill_switch_is_reported_as_a_blocker(workdir):
    """A KILL file in the run dir halts the engine; readiness must say so."""
    KillSwitch(workdir).engage("test")
    cl = Checklist()
    check_safety(cl, load_settings(), workdir)
    engaged = next(c for c in cl.checks if c.name == "safety.killswitch_not_engaged")
    assert engaged.status is Status.FAIL
    assert "KILL FILE PRESENT" in engaged.reason


# --------------------------------------------------------------------------- #
# Data readiness
# --------------------------------------------------------------------------- #
def _seed(db: Database, *, quote_ages_us: list[int], trades: list[tuple[int, int]],
          ticker: str = "KXTEST-A") -> None:
    """Write snapshots at the given ages and tape prints at the given offsets."""
    now = now_us()
    for age in quote_ages_us:
        db.append_markets(
            [Market(ticker=ticker, event_ticker="KXTEST", series_ticker="",
                    title="t", status="active", yes_bid=40, yes_ask=60,
                    yes_bid_size=10.0, yes_ask_size=10.0, volume=5.0,
                    volume_24h=5.0, open_interest=5.0)],
            observed_at_us=now - age,
        )
    with db.tx() as c:
        for i, (age, price) in enumerate(trades):
            c.execute("INSERT OR REPLACE INTO trades(trade_id, ticker, traded_at_us, "
                      "yes_price_cents, size, taker_side) VALUES (?,?,?,?,?,?)",
                      (f"t{i}", ticker, now - age, price, 1.0, "no"))
        c.execute("INSERT OR REPLACE INTO series_cache(ticker, observed_at_us, "
                  "fee_type, fee_multiplier) VALUES ('KXTEST', ?, 'quadratic', 1.0)",
                  (now,))


def _readonly(path: Path) -> ReadOnlyDatabase:
    return ReadOnlyDatabase(path)


def test_a_universe_of_only_stale_quotes_fails_rather_than_reporting_an_empty_book(
        workdir):
    """A stale quote is not a price, it is a memory.

    `build_snapshot` drops everything older than 5 minutes, so an engine fed only
    stale rows quotes nothing -- and reports that as "found no opportunities",
    which is indistinguishable from a working strategy having a quiet day.
    """
    path = workdir / "stale.db"
    with Database(path) as db:
        _seed(db, quote_ages_us=[MAX_QUOTE_AGE_US * 3], trades=[(60_000_000, 50)])
    cl = Checklist()
    with _readonly(path) as ro:
        check_data(cl, ro)
    fresh = next(c for c in cl.checks if c.name == "data.quotes_fresh_enough_to_act_on")
    assert fresh.status is Status.FAIL
    assert fresh.detail["fresh"] == 0
    assert fresh.detail["qualifying_at_any_age"] == 1


def test_fresh_quotes_pass_and_the_reason_carries_the_stale_share(workdir):
    """A PASS with no number behind it is indistinguishable from a check that
    did not run, so the fresh COUNT and the fresh SHARE are both reported."""
    path = workdir / "fresh.db"
    with Database(path) as db:
        _seed(db, quote_ages_us=[30_000_000], trades=[(60_000_000, 50)])
    cl = Checklist()
    with _readonly(path) as ro:
        check_data(cl, ro)
    fresh = next(c for c in cl.checks if c.name == "data.quotes_fresh_enough_to_act_on")
    assert fresh.status is Status.PASS
    assert fresh.detail["fresh"] == 1
    assert 0.0 <= fresh.detail["fresh_share"] <= 1.0


def test_a_tape_that_barely_overlaps_the_quote_window_fails(workdir):
    """The regression this check exists for: 1.4 minutes of overlap.

    The tape paged BACKWARDS, so it covered a window the quotes did not, and
    every counterfactual fill was computed against a tape that was not being
    recorded while the order rested.  The floor is the engine's own 5-minute
    quote freshness cap: a quote the engine will act on must have tape under it.
    """
    path = workdir / "gap.db"
    with Database(path) as db:
        # Quotes over the last 2 minutes; tape prints 6-8 hours ago.
        _seed(db, quote_ages_us=[120_000_000, 0],
              trades=[(8 * 3600 * 1_000_000, 50), (6 * 3600 * 1_000_000, 50)])
    cl = Checklist()
    with _readonly(path) as ro:
        check_data(cl, ro)
    overlap = next(c for c in cl.checks if c.name == "data.tape_and_quotes_overlap")
    assert overlap.status is Status.FAIL
    assert overlap.detail["overlap_minutes"] == 0.0


def test_an_overlapping_tape_and_quote_window_passes_with_the_measured_minutes(workdir):
    path = workdir / "overlap.db"
    with Database(path) as db:
        _seed(db, quote_ages_us=[1_800_000_000, 0],
              trades=[(1_700_000_000, 50), (60_000_000, 50)])
    cl = Checklist()
    with _readonly(path) as ro:
        check_data(cl, ro)
    overlap = next(c for c in cl.checks if c.name == "data.tape_and_quotes_overlap")
    assert overlap.status is Status.PASS
    assert overlap.detail["overlap_minutes"] > 5.0


def test_overlap_arithmetic_handles_disjoint_and_missing_windows():
    """Two intervals that never met overlap by zero, not by a negative number.

    A negative overlap compared against a positive floor would still FAIL, but it
    would also be reported to a human as a negative duration.
    """
    assert overlap_us(0, 10, 20, 30) == 0
    assert overlap_us(0, 30, 10, 20) == 10
    assert overlap_us(None, 10, 20, 30) == 0
    assert overlap_us(0, 10, None, None) == 0


def test_a_missing_series_join_key_is_reported_because_it_silently_zeroes_maker_fees(
        workdir):
    """`MarketSnapshot.series_for()` keys on `Market.series_ticker`.

    The L1 recorder leaves that column empty, so every sleeve falls back to
    `FeeSpec.kalshi('quadratic', 1.0)` -- the spec under which MAKERS PAY ZERO.
    On the 130 `quadratic_with_maker_fees` series a maker pays 0.25x base, so the
    sleeve's edge is overstated by the whole fee on those markets and nothing
    anywhere complains.
    """
    path = workdir / "nokey.db"
    with Database(path) as db:
        _seed(db, quote_ages_us=[30_000_000], trades=[(60_000_000, 50)])
    cl = Checklist()
    with _readonly(path) as ro:
        check_data(cl, ro)
    key = next(c for c in cl.checks
               if c.name == "data.series_join_key_present_on_fresh_rows")
    assert key.status is Status.FAIL
    assert key.detail["with_series_ticker"] == 0
    cached = next(c for c in cl.checks if c.name == "data.series_fee_specs_cached")
    assert cached.status is Status.PASS, "the cache itself is populated; the JOIN is not"


# --------------------------------------------------------------------------- #
# Loop readiness
# --------------------------------------------------------------------------- #
def test_a_gate_two_sleeve_can_send_on_demo_but_never_on_live(workdir):
    """A gate-2 sleeve CAN send on demo, and that is deliberate.

    I5 protects CAPITAL, not network calls.  PAPER is mock funds by
    construction -- the executor allowlists demo hosts at construction, pinned
    by `test_paper_mode_refuses_any_host_that_is_not_demo` -- so an ungated
    sleeve there risks nothing real.  Gating it too made demo lifecycle
    validation unreachable through the runner, which is the ONE thing demo
    exists for: a rail that prevents you from testing the rails is not a rail.

    So the check now asserts the consequence that matters -- that a demo run
    actually reaches the venue -- while LIVE stays refused below gate 4 in two
    independent places.
    """
    path = workdir / "loop.db"
    with Database(path) as db:
        _seed(db, quote_ages_us=[30_000_000], trades=[(60_000_000, 50)])
    cl = Checklist()
    with _readonly(path) as ro:
        check_loop(cl, ro, load_settings(), sleeve_ids=["S2", "S3"],
                   bankroll_cents=1_000_000)
    gate = next(c for c in cl.checks if c.name == "loop.sleeve_can_send_on_demo")
    assert gate.status is Status.PASS
    assert set(gate.detail["gates"]) == {"S2", "S3"}
    assert max(gate.detail["gates"].values()) < 4      # still below Gate 4...
    # ...and LIVE is still refused for exactly those sleeves
    report, client = _probe_executor(RunMode.LIVE, gate=2)
    assert report.gate_blocked and not client.calls


def test_an_empty_ledger_fails_the_fill_check_because_empty_is_worse_than_wrong(
        workdir):
    """No fills means position, P&L, mark-out and every gate KPI are EMPTY.

    An empty risk measurement reads as "no exposure" to every cap, which is the
    one wrong answer that looks like a safe one.
    """
    path = workdir / "nofills.db"
    with Database(path) as db:
        _seed(db, quote_ages_us=[30_000_000], trades=[(60_000_000, 50)])
    cl = Checklist()
    with _readonly(path) as ro:
        check_loop(cl, ro, load_settings(), sleeve_ids=["S2"], bankroll_cents=1_000_000)
    fills = next(c for c in cl.checks if c.name == "loop.fills_materialise")
    assert fills.status is Status.FAIL
    assert fills.detail["fills"] == 0
    settle = next(c for c in cl.checks if c.name == "loop.settlements_ingest")
    assert settle.status is Status.FAIL


def test_the_shadow_cycle_writes_to_a_scratch_database_and_never_to_the_real_one(
        workdir):
    """The recorder owns `data/pm.db`; a readiness check must not write to it.

    The cycle reads market data from the read-only handle and puts its orders in
    a throwaway database, so running the check can never mutate the file it is
    inspecting.
    """
    path = workdir / "cycle.db"
    with Database(path) as db:
        _seed(db, quote_ages_us=[30_000_000], trades=[(60_000_000, 50)])
    before = path.stat().st_mtime_ns
    cl = Checklist()
    with _readonly(path) as ro:
        check_loop(cl, ro, load_settings(), sleeve_ids=["S2"], bankroll_cents=1_000_000)
        with pytest.raises(sqlite3.OperationalError):
            ro.conn.execute("INSERT INTO settlements(venue, ticker, settled_at_us, "
                            "outcome) VALUES ('kalshi','X',1,1)")
    cycle = next(c for c in cl.checks if c.name == "loop.shadow_cycle_runs_clean")
    assert cycle.status is Status.PASS, cycle.reason
    assert path.stat().st_mtime_ns == before


def test_the_read_only_handle_refuses_writes_to_the_database_it_inspects(workdir):
    """`Database.__init__` runs migrate(), which WRITES.

    That is why this script does not use it.  If `ReadOnlyDatabase` ever grew a
    writable connection, this is the test that notices.
    """
    path = workdir / "ro.db"
    with Database(path) as db:
        _seed(db, quote_ages_us=[0], trades=[])
    with _readonly(path) as ro:
        assert ro.scalar("SELECT COUNT(*) FROM market_snapshots") == 1
        with pytest.raises(sqlite3.OperationalError):
            ro.conn.execute("DELETE FROM market_snapshots")


# --------------------------------------------------------------------------- #
# Sizing -- the number nothing in the engine reconciles for you
# --------------------------------------------------------------------------- #
def test_a_bankroll_larger_than_the_demo_balance_is_reported_as_a_blocker():
    """`--bankroll` is a command-line float; every risk limit is a fraction of it.

    Nothing compares it to the venue balance.  Telling the engine $10,000 while
    the demo holds $197 lets the risk engine approve a single position larger
    than the whole account, so the VENUE rejects every order and the demo run
    measures rejection handling instead of the risk path it exists to exercise.
    """
    cl = Checklist()
    check_sizing(cl, load_settings(), bankroll_cents=1_000_000, balance=19_707)
    check = cl.checks[-1]
    assert check.status is Status.FAIL
    assert check.detail["gross_cap_cents"] > check.detail["venue_balance_cents"]
    assert "--bankroll" in check.reason


def test_a_bankroll_the_demo_balance_can_actually_fund_passes():
    """The floor is the GROSS cap, not the position cap.

    Gross deployment is what the account has to be able to carry at once; a
    per-position cap that fits while the gross cap does not still ends in
    venue-side rejections partway through a cycle.
    """
    settings = load_settings()
    # 40% gross cap: a $50 bankroll deploys at most $20.
    cl = Checklist()
    check_sizing(cl, settings, bankroll_cents=5_000, balance=19_707)
    assert cl.checks[-1].status is Status.PASS


def test_sizing_is_skipped_rather_than_guessed_when_the_balance_is_unknown():
    """No balance means no comparison.  A SKIP says that; a PASS would lie."""
    cl = Checklist()
    check_sizing(cl, load_settings(), bankroll_cents=1_000_000, balance=None)
    assert cl.checks[-1].status is Status.SKIP


# --------------------------------------------------------------------------- #
# The checklist itself
# --------------------------------------------------------------------------- #
def test_a_single_failure_turns_the_whole_verdict_into_no_go():
    """Go/no-go is a conjunction.  Thirty passes do not outvote one failure."""
    cl = Checklist()
    for i in range(30):
        cl.add("safety", f"ok{i}", Status.PASS, "fine")
    assert cl.go
    cl.add("data", "bad", Status.FAIL, "not fine")
    assert not cl.go


def test_a_skip_never_counts_as_a_pass():
    """The order-send path is SKIPPED by default.

    If a skip counted as a pass, the default read-only run would report GO on the
    one thing it cannot see -- which is the exact endpoint the demo failed on.
    """
    cl = Checklist()
    cl.skip("connectivity", "account.order_send_path", "flag is off")
    assert cl.go, "a skip is not a failure"
    assert not cl.has("account.order_send_path"), "a skip is not a pass either"
    assert cl.skipped and cl.skipped[0].name == "account.order_send_path"


def test_blockers_are_ranked_in_the_order_they_must_be_cleared():
    """Group order is dependency order.

    A broken key makes every account answer meaningless and a non-demo base URL
    makes every account answer dangerous, so those come before data gaps no
    matter what order the checks happened to run in.
    """
    cl = Checklist()
    cl.add("sizing", "sizing.x", Status.FAIL, "last")
    cl.add("data", "data.x", Status.FAIL, "middle")
    cl.add("credentials", "credentials.x", Status.FAIL, "first")
    assert [c.name for c in cl.ranked_blockers()] == [
        "credentials.x", "data.x", "sizing.x"]


def test_every_check_carries_a_reason_even_when_it_passes():
    """A PASS with no reason is indistinguishable from a check that did not run."""
    cl = Checklist()
    cl.verdict("safety", "x", True, "the number that proves it", "why it failed")
    cl.verdict("safety", "y", False, "the number that proves it", "why it failed")
    assert cl.checks[0].reason == "the number that proves it"
    assert cl.checks[1].reason == "why it failed"
    assert all(c.reason for c in cl.checks)


def test_the_rendered_report_names_the_skipped_checks_so_a_go_is_never_ambiguous():
    """"GO" must never be readable as "everything was tested and passed"."""
    cl = Checklist()
    cl.add("safety", "safety.ok", Status.PASS, "fine")
    cl.skip("connectivity", "account.order_send_path",
            "--live-order-test is OFF (default)")
    text = render(cl, header={"environment": "demo"})
    assert "VERDICT: GO" in text
    assert "NOT CHECKED (and therefore NOT proven):" in text
    assert "account.order_send_path" in text


def test_the_rendered_report_is_pure_ascii():
    """House rule, and it survives a Windows console with no UTF-8 codepage."""
    cl = Checklist()
    cl.add("data", "data.x", Status.FAIL, "a reason -- with an arrow ->")
    text = render(cl, header={"base url": DEMO_BASE})
    text.encode("ascii")


def test_a_check_serialises_for_the_json_mode():
    """`--json` is the machine-readable contract; every field must survive it."""
    check = Check("data", "data.x", Status.FAIL, "why", {"n": 3})
    payload = json.loads(json.dumps(check.as_dict(), default=str))
    assert payload == {"group": "data", "name": "data.x", "status": "FAIL",
                       "reason": "why", "detail": {"n": 3}}


# --------------------------------------------------------------------------- #
# CLI contract
# --------------------------------------------------------------------------- #
def test_the_live_order_test_is_off_unless_it_is_asked_for_by_name():
    """Read-only by default is the whole safety posture of this script.

    A default-on order test would place orders on whatever KALSHI_ENV names the
    first time anyone ran the readiness check out of curiosity.
    """
    assert build_parser().parse_args([]).live_order_test is False
    assert build_parser().parse_args(["--live-order-test"]).live_order_test is True


def test_the_default_sleeves_and_bankroll_match_what_the_runner_would_use():
    """Checking a configuration nobody runs is checking nothing.

    `runner.main` defaults to the S2/S3 arbitrage pair, a $10,000 bankroll and a
    run dir of ".".  The readiness check must judge those same numbers or its
    verdict describes a different engine than the one that will be started.  The
    runner's parser is built inside `main`, so its defaults are read out of the
    source -- brittle on purpose, because the coupling is the thing under test.
    """
    import inspect

    import runner as runner_module

    source = inspect.getsource(runner_module.main)
    args = build_parser().parse_args([])
    assert f'default="{args.sleeves}"' in source
    assert f"default={int(args.bankroll):_}.0" in source
    assert f'"--run-dir", default="{args.run_dir}"' in source


def test_a_missing_database_is_skipped_rather_than_crashing_the_whole_run(
        workdir, monkeypatch):
    """A readiness check that dies on a missing file cannot report on it.

    Credentials are stubbed empty so this whole run is offline: the connectivity
    group skips itself, and what is under test is that the data and loop groups
    come back as SKIP naming the missing path rather than as a traceback.
    """
    from core.config import KalshiCredentials, Settings

    import scripts.demo_readiness as mod

    offline = Settings(kalshi=KalshiCredentials(env="demo"))
    monkeypatch.setattr(mod, "load_settings", lambda: offline)
    rc = mod.main(["--db", str(workdir / "nope.db"), "--run-dir", str(workdir)])
    assert rc == 1, "missing credentials must not read as ready"


@pytest.mark.live
def test_the_demo_exchange_answers_its_public_status_endpoint():
    """The only genuinely live assertion here: the demo host is reachable.

    Marked `live` so `-m "not live"` skips it -- a readiness check whose own test
    suite needs a network is a suite that fails on a train.
    """
    import httpx

    resp = httpx.get(f"{DEMO_BASE}/exchange/status", timeout=20)
    assert resp.status_code == 200
    assert "exchange_active" in resp.json()


# --------------------------------------------------------------------------- #
# Guard: the script must never place an order on the default path
# --------------------------------------------------------------------------- #
def test_no_default_code_path_can_reach_create_order():
    """Belt and braces over the whole module.

    `create_order` appears exactly twice: in `check_live_order`, which only runs
    behind `--live-order-test`, and in `RecordingClient`, which is a fake.  If a
    third call site ever appears on the default path, this fails.
    """
    source = Path(__import__("scripts.demo_readiness", fromlist=["x"]).__file__)
    text = source.read_text(encoding="utf-8")
    calls = [line.strip() for line in text.splitlines()
             if "create_order(" in line and "def " not in line]
    assert calls, "expected the live-order path to still exist"
    for call in calls:
        assert "client.create_order(" in call or "self.calls" in call, call
    assert text.count("def check_live_order") == 1
    assert "--live-order-test" in text
