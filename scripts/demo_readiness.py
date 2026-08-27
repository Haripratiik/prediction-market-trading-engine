"""Demo deployment readiness.  One command, one go/no-go, a reason for every NO.

    python -m scripts.demo_readiness                 # READ-ONLY.  Sends no orders.
    python -m scripts.demo_readiness --json
    python -m scripts.demo_readiness --live-order-test   # places ONE tiny demo order

WHAT THIS ANSWERS, AND WHAT IT DOES NOT
---------------------------------------
It answers: "is this engine safe to point at the Kalshi DEMO exchange with mock
funds?"  Demo validates the ENGINE -- order lifecycle, fills, cancels, the kill
switch, reconciliation, settlement ingest.  It does NOT validate the EDGE, and
conflating the two is the single easiest way to fool yourself here: the demo
books are degenerate (the tightest spread found across 1,000 demo markets was
98c, scripts/demo_order_lifecycle.py), so every "arbitrage" a sleeve reports
against a demo book is an artefact of a book nobody is quoting.  A demo run that
makes money proves a bug, not a strategy.

WHY EACH CHECK EXISTS
---------------------
Every check below corresponds to a way this engine can reach the demo exchange
and quietly do nothing, or do the wrong thing:

  * an account that was RESET answers public endpoints perfectly and returns
    `{"error":{"code":"user_not_found"}}` on anything account-scoped.  The key
    still signs, the signature still verifies, `check_auth` still looks half
    healthy -- and not one order can ever be placed.  `classify_account_error`
    separates that from a bad key (401), because the remedies are opposites: one
    needs a new key, the other needs the account re-created by the operator.
  * `KALSHI_ENV=demo` and "the base URL is the demo host" are NOT the same
    statement.  `KalshiCredentials.base_url` derives one from the other today,
    but the resolved URL is what the socket connects to, so the resolved URL is
    what gets checked -- and it is checked for BOTH "is demo" and "is not prod".
  * I5 refuses gate<4 in every SENDING mode, and `RunMode.PAPER` is a sending
    mode: it uses a real venue client against whatever `KALSHI_ENV` names.  A
    check that only exercised LIVE would pass on the exact bug the executor's
    own comment records (a gate-0 sleeve reaching the venue in PAPER).
  * a stale quote is not a price, it is a memory.  `runner.build_snapshot`
    enforces a 5-minute cap; this reports how many markets currently clear it,
    because 1.8% of the recorded universe clearing it is a very different engine
    from 90% clearing it.
  * the shadow fill model reads the TRADE TAPE over the window an order rested.
    If the tape window and the quote window barely overlap, every counterfactual
    fill is computed against a tape that was not being recorded at the time --
    the overlap was once 1.4 minutes because the tape paged backwards.  The
    floor here is `runner.MAX_QUOTE_AGE_US` itself: a quote the engine is willing
    to act on must have tape underneath it.

READ-ONLY BY DEFAULT, IN BOTH DIRECTIONS
----------------------------------------
No orders are sent unless `--live-order-test` is given, and even then the script
refuses to run if the resolved base URL is production.  The database is opened
through SQLite's `mode=ro` URI (`ReadOnlyDatabase`) rather than `core.db.Database`,
because `Database.__init__` runs `migrate()`, which writes -- and a live recorder
owns `data/pm.db`.  The shadow cycle needs somewhere to put its orders, so it
gets a throwaway database in a temp directory and reads market data from the real
one.

Never prints key material.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from core.config import KalshiCredentials, Settings, load_settings
from core.db import Database
from core.models import RunMode, Side, Venue
from execution.executor import MAX_RESTING_ORDERS, Executor, SleeveRef
from execution.killswitch import KILL_DEADLINE_S, KILL_FILENAME, KillSwitch
from risk.engine import Denial, PortfolioState, RiskEngine
from runner import MAX_QUOTE_AGE_US, build_snapshot
from runner import main as runner_main
from strategy.base import DesiredQuote, DesiredState
from venues.kalshi.auth import SIG_HEADER, signing_string, verify_signature
from venues.kalshi.client import (
    DEMO_BASE,
    DEMO_WS,
    PROD_BASE,
    PROD_WS,
    KalshiClient,
    KalshiError,
    max_cancellable_within,
)

# --------------------------------------------------------------------------- #
# Thresholds.  Every one of these is DERIVED from something already in the repo,
# not chosen for feel -- a readiness check whose floors are invented tells you
# about the floors, not about the engine.
# --------------------------------------------------------------------------- #

# The engine will not act on a quote older than this (runner.MAX_QUOTE_AGE_US),
# so the trade tape must cover at least that much of the quote window or a
# freshly-quoted market has no tape underneath it to fill against.
MIN_TAPE_OVERLAP_US = MAX_QUOTE_AGE_US

# Clock skew produces 401s that look exactly like bad credentials
# (venues/kalshi/auth.py).  5s is the tolerance scripts/check_auth.py already uses.
MAX_CLOCK_SKEW_S = 5.0

# The smallest balance that can fund one order at the widest price the API
# accepts: `create_order` caps price at 99c and demands count >= 1, so a single
# contract costs at most 99c.  Below one dollar the account cannot place the
# cheapest possible order, let alone a multi-leg structure.
MIN_BALANCE_CENTS = 100

# Kalshi's error vocabulary for "this key signed correctly but the account behind
# it is gone".  Matched case-insensitively against the whole response body,
# because it arrives nested under `error.code` and the wrapper has changed shape
# before.
ACCOUNT_MISSING_MARKERS: tuple[str, ...] = (
    "user_not_found", "user not found", "account_not_found", "member_not_found",
)

# The runner's own default sleeve set.  Checking a different one would be
# checking a configuration nobody runs.
DEFAULT_SLEEVES = "S2,S3"

# The runner's own default bankroll, in dollars.  The risk engine sizes against
# THIS number, not against the venue balance, which is the whole point of the
# `sizing.bankroll_vs_venue_balance` check.
DEFAULT_BANKROLL_DOLLARS = 10_000.0

WIDTH = 78

GROUPS: tuple[str, ...] = (
    "credentials", "environment", "connectivity", "safety", "data", "loop", "sizing",
)


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


class AccountState(StrEnum):
    """The four states the brief demands be told apart, plus the two that precede them.

    They are distinct because their REMEDIES are distinct, and three of them look
    identical from a `check_auth` run that only reports "authenticated: no".
    """

    OK = "ok"                                   # (d) all good
    INSUFFICIENT_BALANCE = "insufficient_balance"   # (c) account fine, no funds
    ACCOUNT_MISSING = "account_missing"         # (b) key valid, account gone/reset
    KEY_INVALID = "key_invalid"                 # (a) key or signature rejected
    UNREACHABLE = "unreachable"                 # network / 5xx: nothing was learned
    NO_CREDENTIALS = "no_credentials"           # nothing configured to test


ACCOUNT_REMEDY: dict[AccountState, str] = {
    AccountState.OK: "nothing to do",
    AccountState.INSUFFICIENT_BALANCE:
        "top the demo account up from the Kalshi demo dashboard (mock funds)",
    AccountState.ACCOUNT_MISSING:
        "the key signs but the account behind it is gone -- re-create the demo "
        "account in the Kalshi demo dashboard, then issue a NEW api key for it",
    AccountState.KEY_INVALID:
        "issue a fresh api key on the DEMO dashboard and repoint "
        "KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PATH at it",
    AccountState.UNREACHABLE: "retry; nothing was learned about the account",
    AccountState.NO_CREDENTIALS:
        "set KALSHI_ENV / KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PATH "
        "(config/secrets.env)",
}


@dataclass(frozen=True, slots=True)
class Check:
    """One line of the checklist.  `reason` is mandatory, including on a PASS.

    A PASS with no number behind it is indistinguishable from a check that did
    not really run, which is the failure mode this whole script exists to avoid.
    """

    group: str
    name: str
    status: Status
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status is Status.FAIL

    def as_dict(self) -> dict[str, Any]:
        return {"group": self.group, "name": self.name, "status": self.status.value,
                "reason": self.reason, "detail": self.detail}


@dataclass
class Checklist:
    checks: list[Check] = field(default_factory=list)

    def add(self, group: str, name: str, status: Status, reason: str,
            **detail: Any) -> Check:
        check = Check(group, name, status, reason, detail)
        self.checks.append(check)
        return check

    def verdict(self, group: str, name: str, ok: bool, yes: str, no: str,
                **detail: Any) -> Check:
        """PASS/FAIL from a boolean, with a DIFFERENT reason for each outcome."""
        return self.add(group, name, Status.PASS if ok else Status.FAIL,
                        yes if ok else no, **detail)

    def skip(self, group: str, name: str, reason: str, **detail: Any) -> Check:
        return self.add(group, name, Status.SKIP, reason, **detail)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    @property
    def skipped(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.SKIP]

    @property
    def go(self) -> bool:
        return not self.failures

    def ranked_blockers(self) -> list[Check]:
        """Failures in the order they must be cleared.

        Group order IS dependency order: a broken key makes every account answer
        meaningless, a non-demo base URL makes every account answer dangerous,
        and a safety rail that does not hold makes the data questions moot.
        """
        order = {g: i for i, g in enumerate(GROUPS)}
        return sorted(self.failures, key=lambda c: (order.get(c.group, 99),
                                                    self.checks.index(c)))

    def has(self, name: str) -> bool:
        return any(c.name == name and c.status is Status.PASS for c in self.checks)


# --------------------------------------------------------------------------- #
# Pure helpers.  No network, no database -- these are the parts worth unit tests.
# --------------------------------------------------------------------------- #
def is_demo_base_url(url: str) -> bool:
    """True only for the demo REST host.

    Substring matching on "demo" alone is not enough: a production host under a
    path containing "demo" would pass it.  Compare the normalised URL to the
    constant the client itself ships.
    """
    return url.rstrip("/") == DEMO_BASE.rstrip("/")


def is_production_base_url(url: str) -> bool:
    """True for anything that could be the production exchange.

    Deliberately WIDER than `== PROD_BASE`: an unrecognised host is treated as
    production for the purposes of refusing to send orders, because the cost of
    a false positive is a skipped test and the cost of a false negative is a real
    order on a real account.
    """
    clean = url.rstrip("/")
    if clean == PROD_BASE.rstrip("/"):
        return True
    return not is_demo_base_url(clean)


def looks_like_missing_account(body: str) -> bool:
    """Does this response body say the account behind the key is gone?"""
    low = (body or "").lower()
    return any(marker in low for marker in ACCOUNT_MISSING_MARKERS)


def classify_account_error(status: int, body: str) -> AccountState:
    """(status, body) from an account-scoped call -> which of (a)/(b) it is.

    Body markers are checked BEFORE the status code, and that order is the whole
    point.  A reset account can answer 401 as easily as 404 -- the exchange is
    telling you the user does not exist, not that the signature was wrong -- and
    reading the status first would report "bad key" for an account that needs
    re-creating.  Rotating a key that is already valid fixes nothing and costs
    the operator the one working credential they had.
    """
    if looks_like_missing_account(body):
        return AccountState.ACCOUNT_MISSING
    if status in (401, 403):
        return AccountState.KEY_INVALID
    if status == 404:
        return AccountState.ACCOUNT_MISSING
    return AccountState.UNREACHABLE


def balance_cents(payload: dict[str, Any]) -> int | None:
    """Cents from a `/portfolio/balance` body, or None if it says nothing.

    MEASURED against the demo: `balance` is an INTEGER of cents (19707) and
    `balance_dollars` is a fixed-point string of the same amount ("197.0744").
    The integer is preferred because it needs no float round trip; the string is
    the fallback for a payload shape that changes under us.  Returning None for
    an unreadable body is deliberate -- a balance that could not be read must not
    become a zero, because a zero is itself a meaningful answer here.
    """
    raw = payload.get("balance")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    for key in ("balance_dollars", "balance"):
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return int(round(float(str(value)) * 100))
        except (TypeError, ValueError):
            continue
    return None


def classify_balance(cents: int | None, *, minimum: int = MIN_BALANCE_CENTS) -> AccountState:
    """(c) versus (d): the account answered, so is there enough in it?"""
    if cents is None:
        return AccountState.UNREACHABLE
    return AccountState.OK if cents >= minimum else AccountState.INSUFFICIENT_BALANCE


def overlap_us(a_lo: int | None, a_hi: int | None,
               b_lo: int | None, b_hi: int | None) -> int:
    """Length of the intersection of two closed intervals; 0 when disjoint or empty."""
    if None in (a_lo, a_hi, b_lo, b_hi):
        return 0
    lo = max(int(a_lo), int(b_lo))          # type: ignore[arg-type]
    hi = min(int(a_hi), int(b_hi))          # type: ignore[arg-type]
    return max(0, hi - lo)


def minutes(us: float) -> float:
    return us / 60_000_000.0


def dollars(cents: float) -> str:
    return f"${cents / 100:,.2f}"


# --------------------------------------------------------------------------- #
# Read-only database
# --------------------------------------------------------------------------- #
class ReadOnlyDatabase:
    """A `core.db.Database` look-alike that CANNOT write.

    `Database.__init__` calls `migrate()`, which INSERTs into `schema_meta`.  A
    live recorder owns `data/pm.db`, and a readiness check has no business
    writing to the file it is inspecting -- so this opens the same file through
    SQLite's `mode=ro` URI instead.  Every reader this script drives
    (`runner.build_snapshot`, `monitor.kpi.*`, `shadow.engine.counterfactual_fill`)
    touches exactly one attribute of a `Database`: `.conn`.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        uri = "file:/" + os.path.abspath(str(self.path)).replace(os.sep, "/") + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return None if row is None else row[0]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ReadOnlyDatabase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# The exact predicate `runner.build_snapshot` applies, minus the age clause, so
# the "fresh" count and the "would qualify at any age" count are the same
# question asked twice.  Copied rather than imported because build_snapshot
# returns hydrated Market objects and this needs a COUNT over 20k rows.
_QUALIFYING_SQL = """
SELECT COUNT(*) FROM market_snapshots m
JOIN (SELECT ticker, MAX(observed_at_us) AS t FROM market_snapshots
      WHERE observed_at_us <= ? GROUP BY ticker) latest
  ON m.ticker = latest.ticker AND m.observed_at_us = latest.t
WHERE m.yes_bid IS NOT NULL AND m.yes_ask IS NOT NULL
  AND m.volume_24h > 0 AND m.status = 'active'
"""


# --------------------------------------------------------------------------- #
# Fakes used by the safety-rail checks.  These never touch a network.
# --------------------------------------------------------------------------- #
@dataclass
class RecordingClient:
    """A venue client that records what it was asked to do and does nothing.

    The I5 checks assert on this being EMPTY.  A rail that returns the right
    report while still having called the venue has not held.
    """

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # The executor allowlists demo hosts for PAPER at construction, so a probe
    # has to be able to claim a host.
    base_url: str = DEMO_BASE

    def create_order(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_order", kwargs))
        return {"order": {"order_id": "should-never-exist", "status": "resting"}}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        self.calls.append(("cancel_order", {"order_id": order_id}))
        return {}

    def cancel_all_orders(self) -> int:
        self.calls.append(("cancel_all_orders", {}))
        return 0

    def resting_orders(self, **params: Any) -> list[dict[str, Any]]:
        self.calls.append(("resting_orders", params))
        return []


@contextlib.contextmanager
def scratch_dir(prefix: str = "pm-readiness-") -> Iterator[Path]:
    """A temp directory that is always removed.

    Used for the KILL-file read test and for the shadow cycle's throwaway
    database.  `tempfile.mkdtemp` rather than pytest's `tmp_path`, which raises
    PermissionError [WinError 5] on this machine.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CREDENTIALS
# --------------------------------------------------------------------------- #
def check_credentials(cl: Checklist, creds: KalshiCredentials) -> Any:
    """Key loads, signs, and verifies locally.  Returns the signer or None.

    Steps 1-3 are the same ones `scripts/check_auth.py` runs, and they are here
    for the same reason: a corrupt or truncated key file produces a 401 that
    reads as a permissions problem, and chasing that costs an afternoon.
    """
    g = "credentials"
    if not creds.is_complete:
        cl.add(g, "credentials.configured", Status.FAIL, creds.describe())
        for name in ("credentials.key_loads", "credentials.signature_valid"):
            cl.skip(g, name, "no usable credentials to test")
        return None
    cl.add(g, "credentials.configured", Status.PASS, creds.describe())

    try:
        signer = creds.signer()
    except Exception as exc:                    # noqa: BLE001 -- reported, not raised
        cl.add(g, "credentials.key_loads", Status.FAIL,
               f"{type(exc).__name__}: {exc}")
        cl.skip(g, "credentials.signature_valid", "private key did not load")
        return None

    bits = signer.private_key.key_size
    cl.verdict(g, "credentials.key_loads", bits >= 2048,
               f"{bits}-bit RSA private key loaded",
               f"{bits}-bit RSA key is below the 2048-bit minimum", key_bits=bits)

    path = "/trade-api/v2/portfolio/balance"
    stamp = 1_700_000_000_000
    headers = signer.headers("GET", path, timestamp_ms=stamp)
    ok = verify_signature(signer.private_key.public_key(),
                          signing_string(stamp, "GET", path), headers[SIG_HEADER])
    cl.verdict(g, "credentials.signature_valid", ok,
               "RSA-PSS signature verifies against its own public key",
               "signature did not verify -- the key file is corrupt or truncated")
    return signer


def check_clock_skew(cl: Checklist, base_url: str) -> None:
    """Skew is measured against the exchange we are about to sign for.

    `venues/kalshi/auth.py`: the signature covers a millisecond timestamp, so a
    skewed host produces 401s that are indistinguishable from a bad key.  This is
    the cheapest way to rule that out before blaming the credentials.
    """
    g = "credentials"
    try:
        from email.utils import parsedate_to_datetime

        resp = httpx.get(f"{base_url}/exchange/status", timeout=15)
        served = resp.headers.get("date")
        if not served:
            cl.skip(g, "credentials.clock_skew", "venue sent no Date header")
            return
        skew = abs(time.time() - parsedate_to_datetime(served).timestamp())
    except Exception as exc:                    # noqa: BLE001
        cl.skip(g, "credentials.clock_skew",
                f"could not measure: {type(exc).__name__}: {exc}")
        return
    cl.verdict(g, "credentials.clock_skew", skew < MAX_CLOCK_SKEW_S,
               f"clock skew {skew:.1f}s against the venue clock",
               f"clock skew {skew:.1f}s exceeds {MAX_CLOCK_SKEW_S}s -- run "
               f"`w32tm /resync`; skew causes 401s that look like bad credentials",
               skew_s=round(skew, 3))


# --------------------------------------------------------------------------- #
# ENVIRONMENT
# --------------------------------------------------------------------------- #
def check_environment(cl: Checklist, creds: KalshiCredentials) -> None:
    """`KALSHI_ENV` says demo AND the resolved URLs are the demo hosts.

    Two statements, not one.  `base_url` is derived from `env` today, but the
    socket connects to the URL, so the URL is what is asserted -- and it is
    asserted against production as well as for demo, because "not production" is
    the property that actually protects the account.
    """
    g = "environment"
    cl.verdict(g, "environment.kalshi_env_is_demo", creds.env == "demo",
               "KALSHI_ENV=demo",
               f"KALSHI_ENV={creds.env!r} -- demo readiness requires 'demo'",
               env=creds.env)

    url = creds.base_url
    cl.verdict(g, "environment.base_url_is_demo", is_demo_base_url(url),
               f"REST base URL is the demo host ({url})",
               f"REST base URL {url} is not the demo host ({DEMO_BASE})",
               base_url=url)
    cl.verdict(g, "environment.base_url_is_not_production", url.rstrip("/") != PROD_BASE,
               "REST base URL is not the production host",
               f"REST base URL IS PRODUCTION ({PROD_BASE}) -- refuse to proceed",
               prod_base=PROD_BASE)
    ws = creds.ws_url
    cl.verdict(g, "environment.ws_url_is_demo", ws.rstrip("/") == DEMO_WS.rstrip("/"),
               f"WebSocket URL is the demo host ({ws})",
               f"WebSocket URL {ws} is not the demo host ({DEMO_WS}); "
               f"production is {PROD_WS}", ws_url=ws)


# --------------------------------------------------------------------------- #
# CONNECTIVITY AND ACCOUNT
# --------------------------------------------------------------------------- #
@dataclass
class AccountProbe:
    """What the authenticated GETs actually said."""

    state: AccountState = AccountState.UNREACHABLE
    balance: int | None = None
    detail: str = ""
    endpoint_states: dict[str, str] = field(default_factory=dict)
    resting: int | None = None


def probe_account(client: Any, *, minimum: int = MIN_BALANCE_CENTS) -> AccountProbe:
    """Classify the account using ONLY authenticated GETs.  Sends no orders.

    Every account-scoped endpoint is probed, not just balance, because a reset is
    not always total: an account can answer `/portfolio/balance` and still be
    missing from the order book's view of the world.  The WORST state observed
    across the endpoints wins, so a single `user_not_found` anywhere is reported
    as a missing account rather than averaged away by three healthy answers.

    What this CANNOT prove is that `POST /portfolio/events/orders` works -- that
    is the endpoint the demo actually failed on while public endpoints were fine.
    `--live-order-test` is the only thing that answers it, which is why the
    default run reports that check as skipped instead of implying a pass.
    """
    probe = AccountProbe()
    severity = {
        AccountState.OK: 0,
        AccountState.INSUFFICIENT_BALANCE: 1,
        AccountState.UNREACHABLE: 2,
        AccountState.ACCOUNT_MISSING: 3,
        AccountState.KEY_INVALID: 4,
    }
    worst = AccountState.OK
    details: list[str] = []

    endpoints: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("balance", client.balance),
        ("orders", lambda: client.list_orders(status="resting", limit=1)),
        ("fills", lambda: client.fills(limit=1)),
        ("positions", lambda: client.positions(limit=1)),
    )
    for name, call in endpoints:
        try:
            payload = call()
        except KalshiError as exc:
            state = classify_account_error(int(getattr(exc, "status", 0) or 0),
                                           f"{exc} {getattr(exc, 'body', '')}")
            details.append(f"{name}: {exc}")
        except Exception as exc:                # noqa: BLE001 -- reported, not raised
            state = AccountState.UNREACHABLE
            details.append(f"{name}: {type(exc).__name__}: {exc}")
        else:
            state = AccountState.OK
            # A 200 whose BODY carries the marker is still a missing account.
            if looks_like_missing_account(json.dumps(payload, default=str)):
                state = AccountState.ACCOUNT_MISSING
                details.append(f"{name}: body reports the account does not exist")
            if name == "balance":
                probe.balance = balance_cents(payload)
            if name == "orders":
                probe.resting = len(payload.get("orders") or [])
        probe.endpoint_states[name] = state.value
        if severity[state] > severity[worst]:
            worst = state

    if worst is AccountState.OK:
        worst = classify_balance(probe.balance, minimum=minimum)
        if worst is AccountState.INSUFFICIENT_BALANCE:
            details.append(f"balance {dollars(probe.balance or 0)} "
                           f"< minimum {dollars(minimum)}")
    probe.state = worst
    probe.detail = "; ".join(details)
    return probe


def check_connectivity(cl: Checklist, client: Any, *,
                       minimum: int = MIN_BALANCE_CENTS) -> AccountProbe:
    g = "connectivity"
    try:
        status = client.exchange_status()
    except Exception as exc:                    # noqa: BLE001
        cl.add(g, "connectivity.public_reachable", Status.FAIL,
               f"{type(exc).__name__}: {exc}")
        cl.skip(g, "connectivity.trading_active", "exchange status unavailable")
        for name in ("account.auth_accepted", "account.exists",
                     "account.balance_sufficient"):
            cl.skip(g, name, "venue unreachable")
        return AccountProbe(state=AccountState.UNREACHABLE, detail=str(exc))

    cl.add(g, "connectivity.public_reachable", Status.PASS,
           "public /exchange/status answered without credentials")

    # Shard 0 is where the demo funds live (scripts/demo_order_lifecycle.py) and
    # each shard halts INDEPENDENTLY, so the aggregate flag is not sufficient.
    shard0 = next((s for s in status.get("exchange_index_statuses", [])
                   if s.get("exchange_index") == 0), {})
    active = bool(status.get("exchange_active")) and bool(shard0.get("trading_active", True))
    cl.verdict(g, "connectivity.trading_active", active,
               "exchange active and shard 0 trading",
               f"exchange_active={status.get('exchange_active')} "
               f"shard0_trading={shard0.get('trading_active')} -- orders would rest "
               f"but not trade", exchange_active=bool(status.get("exchange_active")))

    probe = probe_account(client, minimum=minimum)
    cl.verdict(g, "account.auth_accepted", probe.state is not AccountState.KEY_INVALID,
               "signature accepted on authenticated endpoints",
               f"key or signature rejected: {probe.detail[:200]} -- "
               f"{ACCOUNT_REMEDY[AccountState.KEY_INVALID]}",
               endpoint_states=probe.endpoint_states)
    cl.verdict(g, "account.exists", probe.state is not AccountState.ACCOUNT_MISSING,
               "the account behind this key exists",
               f"ACCOUNT MISSING OR RESET -- the key signs correctly and public "
               f"endpoints work, but account-scoped calls report the user does not "
               f"exist ({probe.detail[:180]}). "
               f"{ACCOUNT_REMEDY[AccountState.ACCOUNT_MISSING]}",
               endpoint_states=probe.endpoint_states)

    if probe.state in (AccountState.KEY_INVALID, AccountState.ACCOUNT_MISSING,
                       AccountState.UNREACHABLE) and probe.balance is None:
        cl.skip(g, "account.balance_sufficient",
                f"balance not readable ({probe.state.value})")
    else:
        enough = (probe.balance or 0) >= minimum
        cl.verdict(g, "account.balance_sufficient", enough,
                   f"balance {dollars(probe.balance or 0)} (mock funds)",
                   f"balance {dollars(probe.balance or 0)} is below the "
                   f"{dollars(minimum)} needed for one 1-contract order at the 99c "
                   f"price ceiling -- "
                   f"{ACCOUNT_REMEDY[AccountState.INSUFFICIENT_BALANCE]}",
                   balance_cents=probe.balance)
    return probe


# --------------------------------------------------------------------------- #
# SAFETY RAILS.  None of these touches a network; all use fakes.
# --------------------------------------------------------------------------- #
def _probe_executor(mode: RunMode, gate: int,
                    *, base_url: str = DEMO_BASE) -> tuple[Any, RecordingClient]:
    """One `Executor.execute` against an in-memory DB and a client that records.

    `base_url` matters for PAPER: the executor allowlists demo hosts at
    construction, so pointing a PAPER probe at production must RAISE.
    """
    client = RecordingClient()
    client.base_url = base_url
    db = Database(":memory:")
    try:
        with scratch_dir() as run_dir:
            executor = Executor(db=db, risk=RiskEngine(load_settings().risk), mode=mode,
                                client=client, run_dir=run_dir)
            quote = DesiredQuote(ticker="KXPROBE-1", side=Side.YES, price_cents=50,
                                 size=1, rationale={"probe": True})
            state = PortfolioState(bankroll_cents=1_000_000,
                                   peak_bankroll_cents=1_000_000, cash_cents=1_000_000)
            report = executor.execute(SleeveRef(id="PROBE", gate=gate),
                                      DesiredState(quotes=(quote,)), state)
        return report, client
    finally:
        db.close()


def check_safety(cl: Checklist, settings: Settings, run_dir: Path) -> None:
    g = "safety"

    # ---- the runner will not start LIVE at all.  `runner.main` refuses before
    # it loads settings or opens the database, so calling it here is free of
    # side effects -- which is exactly why the refusal belongs where it is.
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            rc = runner_main(["--mode", "live", "--once"])
        cl.verdict(g, "safety.runner_refuses_live", rc == 2,
                   "runner.main('--mode live') refuses to start (exit 2)",
                   f"runner.main('--mode live') returned {rc}; LIVE must be refused",
                   exit_code=rc)
    except SystemExit as exc:                   # argparse bailing out is also a refusal
        cl.verdict(g, "safety.runner_refuses_live", exc.code not in (0, None),
                   f"runner.main('--mode live') exited {exc.code}",
                   f"runner.main('--mode live') exited {exc.code} -- not a refusal")
    except Exception as exc:                    # noqa: BLE001
        cl.add(g, "safety.runner_refuses_live", Status.FAIL,
               f"runner.main raised {type(exc).__name__}: {exc}")

    # ---- I5 gates REAL CAPITAL.  LIVE must be blocked at gate<4.
    name = "safety.i5_gate_blocks_live"
    try:
        report, client = _probe_executor(RunMode.LIVE, gate=0)
        blocked = bool(report.gate_blocked) and not report.placed and not client.calls
        reasons = {reason for _, reason in report.denied}
        cl.verdict(g, name, blocked and reasons <= {Denial.GATE.value},
                   "gate-0 sleeve in live mode: gate_blocked, 0 sent, 0 venue calls",
                   f"gate-0 sleeve in live mode was NOT blocked "
                   f"(gate_blocked={report.gate_blocked}, placed={len(report.placed)}, "
                   f"venue calls={len(client.calls)})",
                   venue_calls=len(client.calls), denied=sorted(reasons))
    except Exception as exc:                    # noqa: BLE001
        cl.add(g, name, Status.FAIL, f"{type(exc).__name__}: {exc}")

    # ---- PAPER is mock funds BY CONSTRUCTION, which is what lets I5 relax for
    # it.  The guarantee is an ALLOWLIST of demo hosts checked at construction:
    # blocklisting production fails open the day Kalshi adds a host, and the
    # failure mode there is an ungated sleeve trading real money under a
    # practice label.  This check is the load-bearing half of that trade -- if
    # it ever fails, PAPER must go back into `risk.engine.REAL_CAPITAL_MODES`.
    refused: list[str] = []
    for host in ("https://api.elections.kalshi.com/trade-api/v2",
                 "https://unknown-host.kalshi.com/trade-api/v2"):
        try:
            _probe_executor(RunMode.PAPER, gate=0, base_url=host)
        except ValueError:
            refused.append(host)
        except Exception:                       # noqa: BLE001
            pass
    cl.verdict(g, "safety.paper_is_demo_only", len(refused) == 2,
               "mode=paper refuses every non-demo host (allowlist, fails closed)",
               f"mode=paper accepted a non-demo host; refused only {refused}",
               refused=len(refused))

    # ---- the risk engine refuses independently of the executor (I5, twice).
    risk = RiskEngine(settings.risk)
    quote = DesiredQuote(ticker="KXPROBE-1", side=Side.YES, price_cents=50, size=1)
    state = PortfolioState(bankroll_cents=1_000_000, peak_bankroll_cents=1_000_000,
                           cash_cents=1_000_000)
    live = risk.check(quote, state, sleeve_gate=3, mode=RunMode.LIVE)
    denied_gate = not live.allowed and live.reason is Denial.GATE
    cl.verdict(g, "safety.risk_engine_denies_gate_below_4", denied_gate,
               "RiskEngine denies gate<4 in LIVE independently of the executor",
               f"RiskEngine did NOT deny gate<4 in LIVE: {live.reason}")

    # ---- I1: post_only is forced by the executor, not requested by the sleeve.
    db = Database(":memory:")
    try:
        with scratch_dir() as tmp:
            ex = Executor(db=db, risk=risk, mode=RunMode.SHADOW, run_dir=tmp)
            forced = ex.build_request(SleeveRef("PROBE", 5),
                                      DesiredQuote(ticker="KXPROBE-1", side=Side.YES,
                                                   price_cents=50, size=1,
                                                   post_only=False))
            granted = Executor(db=db, risk=risk, mode=RunMode.SHADOW, run_dir=tmp,
                               allow_taker=True)
            opt_in = granted.build_request(SleeveRef("PROBE", 5),
                                           DesiredQuote(ticker="KXPROBE-2",
                                                        side=Side.YES, price_cents=50,
                                                        size=1, post_only=False))
    finally:
        db.close()

    client_default = inspect.signature(
        KalshiClient.create_order).parameters["post_only"].default
    ok = forced.post_only is True and opt_in.post_only is False and client_default is True
    cl.verdict(g, "safety.post_only_default", ok,
               "I1 holds: executor forces post_only=True unless allow_taker was "
               "granted at wiring time, and KalshiClient.create_order defaults True",
               f"I1 BREACHED: forced={forced.post_only} "
               f"allow_taker_optout={opt_in.post_only} "
               f"client_default={client_default}",
               client_default=client_default)

    # ---- the kill switch: right path, and the file IS the state.
    # The path is checked against what the RUNNER would build, not against a
    # constant: `Runner` passes `--run-dir` straight through to `Executor`, which
    # hands it to `KillSwitch`, so a KILL file the operator touches in one
    # directory while the engine watches another is a kill switch that does
    # nothing at all (PLAN.md 10.6 tells the operator to `touch KILL`).
    ks = KillSwitch(run_dir)
    expected = Path(run_dir) / KILL_FILENAME
    cl.verdict(g, "safety.killswitch_path", ks.path == expected,
               f"KILL path resolves to {ks.path.resolve()} -- the file "
               f"`touch {KILL_FILENAME}` creates in the run dir",
               f"KILL path is {ks.path}, expected {expected}",
               kill_path=str(ks.path.resolve()))

    with scratch_dir() as tmp:
        probe = KillSwitch(tmp)
        before = probe.is_engaged()
        probe.engage("demo readiness probe")
        during = probe.is_engaged()
        reason = probe.reason()
        probe.disengage()
        after = probe.is_engaged()
    reads = (before is False) and (during is True) and (after is False)
    cl.verdict(g, "safety.killswitch_reads_file", reads,
               f"KillSwitch.is_engaged() tracks the file on disk "
               f"(reason readback: {reason!r})",
               f"KillSwitch.is_engaged() did not track the file "
               f"(before={before} during={during} after={after})")

    engaged = ks.is_engaged()
    cl.verdict(g, "safety.killswitch_not_engaged", not engaged,
               f"no KILL file at {ks.path.resolve()}",
               f"KILL FILE PRESENT: {ks.describe()} -- the engine will refuse to "
               f"quote until it is removed AND reconciliation is clean (PLAN.md 10.6)")

    # ---- I9 is arithmetic, and the arithmetic has to close.
    capacity = max_cancellable_within(KILL_DEADLINE_S)
    cl.verdict(g, "safety.cancel_capacity_covers_resting_cap",
               capacity >= MAX_RESTING_ORDERS,
               f"max_cancellable_within({KILL_DEADLINE_S}s)={capacity} >= "
               f"MAX_RESTING_ORDERS={MAX_RESTING_ORDERS}: the kill switch can clear "
               f"a full book inside its own deadline",
               f"max_cancellable_within({KILL_DEADLINE_S}s)={capacity} < "
               f"MAX_RESTING_ORDERS={MAX_RESTING_ORDERS}: I9 cannot be honoured at a "
               f"full book", capacity=capacity, resting_cap=MAX_RESTING_ORDERS)

    problems = risk.validate()
    cl.verdict(g, "safety.risk_config_consistent", not problems,
               f"RiskEngine.validate() reports no problems "
               f"(max achievable n_eff {risk.max_achievable_n_eff():.2f} vs floor "
               f"{settings.risk.theme.min_n_eff})",
               "inconsistent risk config -- the runner refuses to start: "
               + "; ".join(problems), problems=problems)


# --------------------------------------------------------------------------- #
# DATA READINESS
# --------------------------------------------------------------------------- #
def check_data(cl: Checklist, db: ReadOnlyDatabase, *,
               min_overlap_us: int = MIN_TAPE_OVERLAP_US) -> None:
    g = "data"
    now = int(time.time() * 1_000_000)

    fresh = int(db.scalar(_QUALIFYING_SQL + " AND m.observed_at_us >= ?",
                          (now, now - MAX_QUOTE_AGE_US)) or 0)
    any_age = int(db.scalar(_QUALIFYING_SQL, (now,)) or 0)
    share = fresh / any_age if any_age else 0.0
    cl.verdict(g, "data.quotes_fresh_enough_to_act_on", fresh > 0,
               f"{fresh} market(s) inside the {minutes(MAX_QUOTE_AGE_US):.0f}-minute "
               f"freshness cap, out of {any_age} that otherwise qualify "
               f"({share:.1%}); the other {any_age - fresh} are memories, not prices",
               f"ZERO markets inside the {minutes(MAX_QUOTE_AGE_US):.0f}-minute cap "
               f"(of {any_age} that otherwise qualify) -- every sleeve would see an "
               f"empty book and quote nothing",
               fresh=fresh, qualifying_at_any_age=any_age, fresh_share=round(share, 4))

    q = db.conn.execute("SELECT MIN(observed_at_us) a, MAX(observed_at_us) b, "
                        "COUNT(*) n FROM market_snapshots").fetchone()
    t = db.conn.execute("SELECT MIN(traded_at_us) a, MAX(traded_at_us) b, "
                        "COUNT(*) n FROM trades").fetchone()
    both = overlap_us(q["a"], q["b"], t["a"], t["b"])
    quote_span = (int(q["b"]) - int(q["a"])) if q["a"] and q["b"] else 0
    tape_span = (int(t["b"]) - int(t["a"])) if t["a"] and t["b"] else 0
    cl.verdict(g, "data.tape_and_quotes_overlap", both >= min_overlap_us,
               f"tape and quote windows overlap for {minutes(both):.1f} min "
               f"(quotes span {minutes(quote_span):.1f} min over {q['n']} rows, tape "
               f"{minutes(tape_span):.1f} min over {t['n']} prints); floor is the "
               f"{minutes(min_overlap_us):.0f}-min quote freshness cap",
               f"tape and quote windows overlap for only {minutes(both):.1f} min, "
               f"below the {minutes(min_overlap_us):.0f}-min quote freshness cap -- "
               f"a quote the engine is willing to act on has no tape underneath it, "
               f"so every counterfactual fill is computed against a tape that was "
               f"not being recorded at the time",
               overlap_minutes=round(minutes(both), 2),
               quote_span_minutes=round(minutes(quote_span), 2),
               tape_span_minutes=round(minutes(tape_span), 2),
               tape_rows=t["n"], quote_rows=q["n"])

    # Tape coverage of the markets we would actually quote.  A long global
    # overlap can be one busy ticker; the shadow fill model is per ticker.
    rows = db.conn.execute(
        """SELECT m.ticker FROM market_snapshots m
           JOIN (SELECT ticker, MAX(observed_at_us) AS t FROM market_snapshots
                 WHERE observed_at_us <= ? GROUP BY ticker) latest
             ON m.ticker = latest.ticker AND m.observed_at_us = latest.t
           WHERE m.yes_bid IS NOT NULL AND m.yes_ask IS NOT NULL
             AND m.volume_24h > 0 AND m.status = 'active'
             AND m.observed_at_us >= ?""",
        (now, now - MAX_QUOTE_AGE_US)).fetchall()
    tickers = [r["ticker"] for r in rows]
    covered = 0
    if tickers and both > 0:
        lo = max(int(q["a"]), int(t["a"]))
        hi = min(int(q["b"]), int(t["b"]))
        holes = ",".join("?" * len(tickers))
        covered = int(db.scalar(
            f"SELECT COUNT(DISTINCT ticker) FROM trades WHERE ticker IN ({holes}) "
            f"AND traded_at_us BETWEEN ? AND ?", (*tickers, lo, hi)) or 0)
    cover_share = covered / len(tickers) if tickers else 0.0
    cl.verdict(g, "data.tape_covers_fresh_markets", covered > 0,
               f"{covered}/{len(tickers)} fresh markets ({cover_share:.0%}) have tape "
               f"prints inside the overlap; the rest can never produce a "
               f"counterfactual fill",
               f"none of the {len(tickers)} fresh markets has a tape print inside the "
               f"overlap -- shadow fills are structurally impossible",
               covered=covered, fresh=len(tickers), cover_share=round(cover_share, 4))

    # Series fee specs.  TWO questions: is the cache populated, and can a sleeve
    # actually JOIN to it?  `MarketSnapshot.series_for` keys on
    # `Market.series_ticker`, which the L1 recorder leaves empty.
    cached = int(db.scalar("SELECT COUNT(*) FROM series_cache") or 0)
    prefixes = sorted({t.split("-", 1)[0] for t in tickers})
    resolvable = 0
    nondefault = 0
    if prefixes:
        holes = ",".join("?" * len(prefixes))
        resolvable = int(db.scalar(
            f"SELECT COUNT(*) FROM series_cache WHERE ticker IN ({holes})",
            prefixes) or 0)
        nondefault = int(db.scalar(
            f"SELECT COUNT(*) FROM series_cache WHERE ticker IN ({holes}) "
            f"AND (fee_type != 'quadratic' OR fee_multiplier != 1.0)",
            prefixes) or 0)
    cl.verdict(g, "data.series_fee_specs_cached",
               cached > 0 and resolvable == len(prefixes),
               f"series_cache holds {cached} rows and covers all {len(prefixes)} "
               f"series behind the fresh markets ({nondefault} of them do NOT use the "
               f"default quadratic/1.0 spec)",
               f"series_cache holds {cached} rows but covers only {resolvable} of the "
               f"{len(prefixes)} series behind the fresh markets -- every uncovered "
               f"fill is charged the fallback FULL taker rate "
               f"(execution/fillfeed.py::_fallback_spec)",
               cached=cached, fresh_series=len(prefixes), resolvable=resolvable,
               non_default_fee_series=nondefault)

    # Counted over the SAME population as `tickers` -- the QUOTABLE fresh rows.
    # Counting all fresh rows instead let the numerator exceed the denominator
    # (400 of 354), which is not a near-miss, it is two different questions.
    with_key = int(db.scalar(
        """SELECT COUNT(*) FROM market_snapshots m
           JOIN (SELECT ticker, MAX(observed_at_us) AS t FROM market_snapshots
                 WHERE observed_at_us <= ? GROUP BY ticker) latest
             ON m.ticker = latest.ticker AND m.observed_at_us = latest.t
           WHERE m.yes_bid IS NOT NULL AND m.yes_ask IS NOT NULL
             AND m.volume_24h > 0 AND m.status = 'active'
             AND m.observed_at_us >= ?
             AND m.series_ticker IS NOT NULL AND m.series_ticker != ''""",
        (now, now - MAX_QUOTE_AGE_US)) or 0)
    cl.verdict(g, "data.series_join_key_present_on_fresh_rows",
               len(tickers) == 0 or with_key == len(tickers),
               f"all {with_key} fresh snapshot rows carry series_ticker, so "
               f"MarketSnapshot.series_for() resolves the real fee spec",
               f"only {with_key}/{len(tickers)} fresh snapshot rows carry "
               f"series_ticker, so MarketSnapshot.series_for() returns None and every "
               f"sleeve silently falls back to FeeSpec.kalshi('quadratic', 1.0) -- "
               f"which says MAKERS PAY ZERO.  {nondefault} of the {len(prefixes)} "
               f"live series do not use that spec, so their maker fee is understated",
               with_series_ticker=with_key, fresh=len(tickers))


# --------------------------------------------------------------------------- #
# LOOP READINESS
# --------------------------------------------------------------------------- #
def build_sleeves(ids: Sequence[str], db: ReadOnlyDatabase) -> list[Any]:
    """The runner's own wiring, minus anything that writes."""
    from strategy.s1_structural import S1Structural
    from strategy.s2_shortbasket import S2ShortBasket
    from strategy.s3_linked_rv import S3LinkedRV

    out: list[Any] = []
    for sid in ids:
        match sid.strip().upper():
            case "S1":
                s1 = S1Structural()
                for r in db.conn.execute(
                        "SELECT DISTINCT rules_hash FROM market_snapshots "
                        "WHERE rules_hash IS NOT NULL AND rules_hash != ''"):
                    s1.reviewed_rules.add(r["rules_hash"])
                out.append(s1)
            case "S2":
                out.append(S2ShortBasket())
            case "S3":
                out.append(S3LinkedRV())
            case other:
                raise ValueError(f"unknown sleeve {other!r}")
    return out


def _has_fills(db: ReadOnlyDatabase, sleeve_id: str) -> bool:
    """Did this sleeve ever fill?  A sleeve that never filled cannot mark out."""
    return bool(db.scalar(
        """SELECT COUNT(*) FROM fills f
           JOIN orders o ON o.client_order_id = f.client_order_id
           WHERE o.sleeve_id = ? AND f.terminal = 1""", (sleeve_id,)) or 0)


def check_loop(cl: Checklist, db: ReadOnlyDatabase, settings: Settings, *,
               sleeve_ids: Sequence[str], bankroll_cents: int) -> None:
    g = "loop"

    # ---- can any configured sleeve send at all?  I5 refuses gate<4 in every
    # sending mode, so a demo run whose sleeves all sit at gate 2 places nothing
    # and validates nothing.  The RAIL is correct; the CONSEQUENCE is a blocker.
    try:
        sleeves = build_sleeves(sleeve_ids, db)
    except Exception as exc:                    # noqa: BLE001
        cl.add(g, "loop.shadow_cycle_runs_clean", Status.FAIL,
               f"could not build sleeves: {type(exc).__name__}: {exc}")
        for name in ("loop.sleeve_can_send_on_demo", "loop.fills_materialise",
                     "loop.settlements_ingest", "loop.gate_kpis_computable"):
            cl.skip(g, name, "sleeves would not build")
        return

    # I5 gates REAL CAPITAL, and PAPER is mock funds by construction (the
    # executor allowlists demo hosts at construction -- see
    # `safety.paper_is_demo_only`).  So a demo run can send at any gate, and the
    # rail still refuses LIVE below Gate 4.  This check now verifies the
    # CONSEQUENCE -- that a gate-2 sleeve really can reach the venue in PAPER --
    # rather than demanding a promotion the gate system has not earned.
    gates = {s.id: int(s.gate) for s in sleeves}
    try:
        report, client = _probe_executor(RunMode.PAPER, gate=min(gates.values() or [0]))
        can_send = bool(client.calls) and not report.gate_blocked
        detail = f"{len(client.calls)} venue call(s)"
    except Exception as exc:                    # noqa: BLE001
        can_send, detail = False, f"{type(exc).__name__}: {exc}"
    cl.verdict(g, "loop.sleeve_can_send_on_demo", can_send,
               f"sleeve gates {gates}: a demo run in PAPER mode reaches the venue "
               f"({detail}), so the order lifecycle is actually exercised",
               f"sleeve gates {gates} but a PAPER probe did NOT reach the venue "
               f"({detail}) -- demo would exercise no lifecycle at all",
               gates=gates)

    # ---- a real shadow cycle: real market data (read-only) into a throwaway DB.
    started = time.monotonic()
    try:
        snapshot = build_snapshot(db, bankroll_cents=bankroll_cents)
        risk = RiskEngine(settings.risk)
        risk.theme_of = {m.ticker: (m.event_ticker or m.ticker) for m in snapshot.markets}
        depth = {m.ticker: m.yes_bid_size for m in snapshot.markets}
        state = PortfolioState(bankroll_cents=bankroll_cents,
                               peak_bankroll_cents=bankroll_cents,
                               cash_cents=bankroll_cents)
        quoted = placed = denied = 0
        denials: dict[str, int] = {}
        with scratch_dir() as tmp:
            with Database(tmp / "shadow.db") as scratch:
                executor = Executor(db=scratch, risk=risk, mode=RunMode.SHADOW,
                                    run_dir=tmp)
                for sleeve in sleeves:
                    desired = sleeve.desired_state(snapshot)
                    quoted += len(desired.quotes)
                    report = executor.execute(sleeve, desired, state,
                                              snapshot=snapshot, depth_by_ticker=depth)
                    placed += len(report.placed)
                    denied += len(report.denied)
                    for _, reason in report.denied:
                        denials[reason] = denials.get(reason, 0) + 1
                open_after = len(executor._oms.open_orders(venue=Venue.KALSHI))
                drift = executor.reconcile()
    except Exception as exc:                    # noqa: BLE001
        cl.add(g, "loop.shadow_cycle_runs_clean", Status.FAIL,
               f"shadow cycle raised {type(exc).__name__}: {exc}")
    else:
        elapsed = time.monotonic() - started
        cl.verdict(g, "loop.shadow_cycle_runs_clean", drift.is_clean,
                   f"cycle over {len(snapshot.markets)} fresh market(s) in "
                   f"{elapsed:.1f}s: {quoted} quote(s) wanted, {placed} placed, "
                   f"{denied} denied {denials or '{}'}, {open_after} resting, "
                   f"reconciliation clean",
                   f"cycle ran but reconciliation is NOT clean: {drift}",
                   markets=len(snapshot.markets), quoted=quoted, placed=placed,
                   denied=denied, denials=denials, seconds=round(elapsed, 2))

    # ---- fills.  The ledger is the evidence: shadow fills are materialised into
    # the SAME `fills` table live fills land in (PLAN.md 7.2).
    total_fills = int(db.scalar("SELECT COUNT(*) FROM fills") or 0)
    shadow_fills = int(db.scalar(
        "SELECT COUNT(*) FROM fills WHERE venue_fill_id LIKE 'shadow:%'") or 0)
    orders_n = int(db.scalar("SELECT COUNT(*) FROM orders") or 0)
    cl.verdict(g, "loop.fills_materialise", total_fills > 0,
               f"{total_fills} fill(s) in the ledger ({shadow_fills} materialised "
               f"from the tape) against {orders_n} order(s) -- the loop has closed at "
               f"least once; PLAN.md G3 wants >= 300 hypothetical fills",
               f"ZERO fills against {orders_n} order(s): the engine places and never "
               f"learns whether it filled, so position, P&L, mark-out and every gate "
               f"KPI downstream are EMPTY rather than wrong",
               fills=total_fills, shadow_fills=shadow_fills, orders=orders_n,
               g3_target=300)

    settled = int(db.scalar("SELECT COUNT(*) FROM settlements") or 0)
    voided = int(db.scalar("SELECT COUNT(*) FROM settlements WHERE voided = 1") or 0)
    newest = db.scalar("SELECT MAX(settled_at_us) FROM settlements")
    age_min = minutes(int(time.time() * 1_000_000) - int(newest)) if newest else None
    freshness = f", newest {age_min:.0f} min old" if age_min is not None else ""
    cl.verdict(g, "loop.settlements_ingest", settled > 0,
               f"{settled} settlement(s) recorded ({voided} voided){freshness}; "
               f"PLAN.md G4 wants >= 200 live settlements",
               "ZERO settlements recorded -- no realised P&L, no Brier skill, no "
               "calibration, and G4's `live_settlements >= 200` is unmeasurable "
               "(run `python -m recorder.settlements --once`)",
               settlements=settled, voided=voided,
               newest_age_minutes=None if age_min is None else round(age_min, 1),
               g4_target=200)

    # ---- every KPI a gate reads must be COMPUTABLE, not merely present.
    try:
        from monitor.kpi import sleeve_ids as kpi_sleeve_ids
        from monitor.kpi import sleeve_report

        found = kpi_sleeve_ids(db)
        if not found:
            cl.skip(g, "loop.gate_kpis_computable",
                    "no sleeve has emitted a decision yet, so there is nothing to "
                    "score")
        else:
            missing: dict[str, list[str]] = {}
            for sid in found:
                report = sleeve_report(db, sid, capital_cents=bankroll_cents)
                gaps = []
                if report["brier_skill"].n <= 0:
                    gaps.append("brier_skill")           # G4
                if report["net_edge"].net_edge is None:
                    gaps.append("net_edge")              # G3 and G4
                # A sleeve with NO FILLS has nothing to mark out, and that is
                # an honest N/A -- the same reasoning already applied to
                # `orphan_loss` below.  Only a sleeve that DID fill and still
                # cannot produce a single horizon is a real gap: that means the
                # `marks` table is unpopulated or the snapshot history is too
                # sparse to resolve any horizon (PLAN.md G3).
                n_marked = sum(m.n for m in report["markouts"].values())
                if report["fill_quality"].live_orders or n_marked or _has_fills(db, sid):
                    if all(m.mean_cents is None
                           for m in report["markouts"].values()):
                        gaps.append("markouts")          # G3 slippage haircut
                if report["fill_quality"].shadow_orders <= 0:
                    gaps.append("fill_quality")          # G3 hypothetical fills
                orphan = report["orphan_loss"]           # KPI 6
                # A sleeve that opened no multi-leg structure has no orphan ratio
                # to report, and that is an honest N/A rather than a broken KPI.
                # Only an UNAVAILABLE computation, or structures with no ratio, is
                # a gap.
                if not orphan.available or (orphan.structures > 0
                                            and orphan.ratio is None):
                    gaps.append("orphan_loss")
                if gaps:
                    missing[sid] = gaps
            cl.verdict(g, "loop.gate_kpis_computable", not missing,
                       f"every gate KPI is computable for {found}",
                       f"gate KPIs that return no number: {missing} -- a gate cannot "
                       f"be cleared on a statistic that does not exist "
                       f"(markouts need the `marks` table or a denser snapshot "
                       f"history; PLAN.md G3 slippage_haircut_recorded)",
                       sleeves=found, missing=missing)
    except Exception as exc:                    # noqa: BLE001
        cl.add(g, "loop.gate_kpis_computable", Status.FAIL,
               f"KPI computation raised {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# SIZING -- the risk engine sizes off --bankroll, NOT off the venue balance
# --------------------------------------------------------------------------- #
def check_sizing(cl: Checklist, settings: Settings, *, bankroll_cents: int,
                 balance: int | None) -> None:
    """The one number nothing in the engine reconciles for you.

    `runner.main` takes `--bankroll` as a command-line float and every limit in
    PLAN.md section 9 is a FRACTION of it.  Nothing compares it to the venue
    balance.  Point the engine at a demo account holding $197 while telling it
    the bankroll is $10,000 and the risk engine will happily approve a single
    position twenty times larger than the account can fund -- so every order is
    rejected by the venue, the engine's own limits are never the binding
    constraint, and the demo run measures rejection handling rather than the risk
    path it was supposed to exercise.
    """
    g = "sizing"
    if balance is None:
        cl.skip(g, "sizing.bankroll_vs_venue_balance",
                "venue balance unreadable, so the two cannot be compared")
        return
    cfg = settings.risk
    pos_cap = int(bankroll_cents * cfg.position.cap_fraction_default)
    gross_cap = int(bankroll_cents * cfg.deployment.max_gross_fraction)
    fits = gross_cap <= balance
    cl.verdict(g, "sizing.bankroll_vs_venue_balance", fits,
               f"declared bankroll {dollars(bankroll_cents)} with gross cap "
               f"{dollars(gross_cap)} fits inside the demo balance "
               f"{dollars(balance)}",
               f"declared bankroll {dollars(bankroll_cents)} but the demo account "
               f"holds {dollars(balance)}.  The risk engine sizes off --bankroll, so "
               f"it would permit a single position of {dollars(pos_cap)} "
               f"({cfg.position.cap_fraction_default:.0%}) and gross deployment of "
               f"{dollars(gross_cap)} ({cfg.deployment.max_gross_fraction:.0%}) "
               f"against {dollars(balance)} of mock funds -- the venue, not the risk "
               f"engine, would be the binding constraint.  Run with "
               f"`--bankroll {balance / 100:.0f}` or fund the demo account to "
               f"{dollars(int(gross_cap))}",
               declared_bankroll_cents=bankroll_cents, venue_balance_cents=balance,
               position_cap_cents=pos_cap, gross_cap_cents=gross_cap)


# --------------------------------------------------------------------------- #
# THE ONLY CHECK THAT SENDS AN ORDER
# --------------------------------------------------------------------------- #
def _pick_demo_market(client: Any) -> dict[str, Any] | None:
    """Tightest two-sided book on shard 0, where the demo funds live.

    "Tightest" is relative: the demo's tightest observed spread was 98c.  This is
    picking somewhere an order can REST, not somewhere there is edge.
    """
    data = client._request("GET", "/markets", params={"limit": 1000, "status": "open",
                                                      "mve_filter": "exclude"})

    def cents(raw: Any) -> int:
        try:
            return int(round(float(str(raw)) * 100))
        except (TypeError, ValueError):
            return 0

    best: tuple[int, dict[str, Any], int, int] | None = None
    for m in data.get("markets", []):
        bid, ask = cents(m.get("yes_bid_dollars")), cents(m.get("yes_ask_dollars"))
        if not 0 < bid < ask <= 99:
            continue
        if m.get("exchange_index") not in (0, None):
            continue
        if best is None or (ask - bid) < best[0]:
            best = (ask - bid, m, bid, ask)
    return None if best is None else {"market": best[1], "bid": best[2], "ask": best[3]}


def check_live_order(cl: Checklist, client: Any, base_url: str) -> None:
    """Place ONE post-only order that cannot cross, read it, cancel it.

    This is the only thing that can answer the question the demo actually failed
    on: `POST /portfolio/events/orders` returning `user_not_found` while every
    public endpoint was healthy.  A GET-only probe cannot see it.

    The order is post-only and priced BELOW the current best bid, so it cannot
    cross however the book moves while it is in flight.  Pricing it *inside* the
    spread looked safer and was not: the demo book churns at over 1,000 deltas
    per second, so an improving bid races the ask down, trips post-only, and the
    venue cancels it -- which then reads as "accepted but not resting", i.e. a
    broken lifecycle, when the lifecycle was fine.  Resting below the touch is
    the only price that is safe against that race.

    The cancel runs in a `finally` -- an order left resting by a readiness check
    is exactly the orphan the check exists to detect.
    """
    g = "connectivity"
    if is_production_base_url(base_url):
        cl.add(g, "account.order_send_path", Status.FAIL,
               f"REFUSING to send: {base_url} is not the demo host")
        return

    pick = _pick_demo_market(client)
    if pick is None:
        cl.skip(g, "account.order_send_path",
                "no demo market with a two-sided quote on shard 0")
        return
    ticker = pick["market"]["ticker"]
    bid, ask = pick["bid"], pick["ask"]
    price = max(1, bid - 3)          # strictly below the touch: cannot cross
    coid = str(uuid.uuid4())
    order_id: str | None = None
    try:
        resp = client.create_order(ticker=ticker, side="bid", count=1,
                                   price_cents=price, client_order_id=coid,
                                   post_only=True)
    except KalshiError as exc:
        state = classify_account_error(int(getattr(exc, "status", 0) or 0),
                                       f"{exc} {getattr(exc, 'body', '')}")
        cl.add(g, "account.order_send_path", Status.FAIL,
               f"POST /portfolio/events/orders REJECTED ({state.value}): "
               f"{str(exc)[:200]}.  {ACCOUNT_REMEDY[state]}",
               account_state=state.value, ticker=ticker, price_cents=price)
        return
    except Exception as exc:                    # noqa: BLE001
        cl.add(g, "account.order_send_path", Status.FAIL,
               f"POST /portfolio/events/orders raised {type(exc).__name__}: {exc}")
        return

    try:
        order = resp.get("order") if isinstance(resp.get("order"), dict) else resp
        order_id = order.get("order_id") or resp.get("order_id")
        # MEASURED: the venue accepts the order and returns an id BEFORE it is
        # visible on the list endpoint.  Reading back immediately reports "not
        # resting" on a perfectly healthy order -- and then the `finally` block
        # cancels one, which is how this check could FAIL while the very next
        # check reported "1 cancel, 0 left resting".  Retry briefly instead of
        # asserting on a race.
        readback = None
        deadline = time.monotonic() + 5.0
        while order_id and time.monotonic() < deadline:
            readback = client.get_order(order_id)
            if readback is not None:
                break
            time.sleep(0.5)
        try:
            queue = client.queue_position(order_id) if order_id else None
        except KalshiError as exc:
            queue = f"unavailable: {exc}"
        cl.verdict(g, "account.order_send_path", bool(order_id) and readback is not None,
                   f"placed post-only bid {price}c x1 on {ticker} (book {bid}/{ask}), "
                   f"order_id={order_id}, read back status="
                   f"{(readback or {}).get('status')}, queue_position={queue}",
                   f"order was accepted (order_id={order_id}) but does not appear "
                   f"among resting orders -- the lifecycle is broken between create "
                   f"and list", ticker=ticker, price_cents=price, order_id=order_id)
    finally:
        cancelled = 0
        started = time.monotonic()
        try:
            if order_id:
                client.cancel_order(order_id)
                cancelled = 1
            cancelled += client.cancel_all_orders()
        except Exception as exc:                # noqa: BLE001
            cl.add(g, "account.cancel_path", Status.FAIL,
                   f"cancel FAILED, an order may still be resting on the demo "
                   f"account: {type(exc).__name__}: {exc}")
        else:
            elapsed = time.monotonic() - started
            resting = len(client.resting_orders())
            cl.verdict(g, "account.cancel_path", resting == 0,
                       f"cancel path clean: {cancelled} cancel(s) in {elapsed:.2f}s "
                       f"(I9 deadline {KILL_DEADLINE_S}s), 0 orders left resting",
                       f"{resting} order(s) STILL RESTING after cancel-all -- I9 "
                       f"does not hold on this account",
                       resting_after=resting, seconds=round(elapsed, 3))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
MARK = {Status.PASS: "[PASS]", Status.FAIL: "[FAIL]", Status.SKIP: "[skip]"}


def render(cl: Checklist, *, header: dict[str, str]) -> str:
    out: list[str] = ["=" * WIDTH, "DEMO DEPLOYMENT READINESS  (Kalshi demo, mock funds)",
                      "=" * WIDTH]
    for key, value in header.items():
        out.append(f"  {key:<12}: {value}")

    for group in GROUPS:
        rows = [c for c in cl.checks if c.group == group]
        if not rows:
            continue
        out.append("")
        out.append(f"{group} " + "-" * max(0, WIDTH - len(group) - 1))
        for c in rows:
            out.append(f"  {MARK[c.status]} {c.name}")
            for line in _wrap(c.reason, WIDTH - 9):
                out.append(f"         {line}")

    n_pass = sum(1 for c in cl.checks if c.status is Status.PASS)
    out.append("")
    out.append("=" * WIDTH)
    verdict = "GO" if cl.go else "NO-GO"
    out.append(f"VERDICT: {verdict}  --  {n_pass} passed, {len(cl.failures)} failed, "
               f"{len(cl.skipped)} skipped")
    out.append("=" * WIDTH)

    if cl.failures:
        out.append("")
        out.append("BLOCKERS, in the order they must be cleared:")
        for i, c in enumerate(cl.ranked_blockers(), 1):
            out.append(f"  {i}. {c.name}")
            for line in _wrap(c.reason, WIDTH - 6):
                out.append(f"     {line}")
    if cl.skipped:
        out.append("")
        out.append("NOT CHECKED (and therefore NOT proven):")
        for c in cl.skipped:
            out.append(f"  -  {c.name}")
            for line in _wrap(c.reason, WIDTH - 6):
                out.append(f"     {line}")
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    words = str(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="go/no-go for pointing this engine at the Kalshi DEMO exchange")
    ap.add_argument("--db", default=None, help="sqlite path; defaults to config")
    ap.add_argument("--run-dir", default=".", help="where the KILL file lives")
    ap.add_argument("--sleeves", default=DEFAULT_SLEEVES,
                    help="comma-separated sleeve ids, as passed to the runner")
    ap.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL_DOLLARS,
                    help="dollars; the number the risk engine would size against")
    ap.add_argument("--min-balance", type=float, default=MIN_BALANCE_CENTS / 100,
                    help="dollars of mock funds required")
    ap.add_argument("--live-order-test", action="store_true",
                    help="place ONE tiny post-only order on the DEMO exchange and "
                         "cancel it.  OFF by default; refuses to run against prod.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    creds = settings.kalshi
    db_path = Path(args.db) if args.db else settings.db_path
    bankroll_cents = int(round(args.bankroll * 100))
    min_balance = int(round(args.min_balance * 100))
    run_dir = Path(args.run_dir)

    cl = Checklist()
    signer = check_credentials(cl, creds)
    check_environment(cl, creds)
    if signer is not None:
        check_clock_skew(cl, creds.base_url)
    else:
        cl.skip("credentials", "credentials.clock_skew", "no signer to test against")

    balance: int | None = None
    if signer is None:
        for name in ("connectivity.public_reachable", "connectivity.trading_active",
                     "account.auth_accepted", "account.exists",
                     "account.balance_sufficient"):
            cl.skip("connectivity", name, "no usable credentials")
        cl.skip("connectivity", "account.order_send_path", "no usable credentials")
    elif is_production_base_url(creds.base_url):
        for name in ("connectivity.public_reachable", "connectivity.trading_active",
                     "account.auth_accepted", "account.exists",
                     "account.balance_sufficient", "account.order_send_path"):
            cl.skip("connectivity", name,
                    f"refusing to touch {creds.base_url} -- not the demo host")
    else:
        with KalshiClient(base_url=creds.base_url, signer=signer,
                          timeout=20.0, max_retries=2) as client:
            probe = check_connectivity(cl, client, minimum=min_balance)
            balance = probe.balance
            if not args.live_order_test:
                cl.skip("connectivity", "account.order_send_path",
                        "--live-order-test is OFF (default).  GET-only probes cannot "
                        "prove POST /portfolio/events/orders works -- the demo "
                        "returned user_not_found there while public endpoints were "
                        "healthy.  Re-run with --live-order-test to send one tiny "
                        "post-only order and cancel it.")
                cl.skip("connectivity", "account.cancel_path",
                        "--live-order-test is OFF (default); nothing was placed, so "
                        "the cancel and I9 paths were not exercised against the venue")
            elif probe.state in (AccountState.KEY_INVALID,
                                 AccountState.ACCOUNT_MISSING):
                cl.skip("connectivity", "account.order_send_path",
                        f"account is {probe.state.value}; sending an order would only "
                        f"restate what the authenticated GETs already reported")
                cl.skip("connectivity", "account.cancel_path",
                        "nothing was placed")
            else:
                check_live_order(cl, client, creds.base_url)

    check_safety(cl, settings, run_dir)

    if not db_path.is_file():
        for name in ("data.quotes_fresh_enough_to_act_on", "data.tape_and_quotes_overlap",
                     "data.tape_covers_fresh_markets", "data.series_fee_specs_cached",
                     "data.series_join_key_present_on_fresh_rows"):
            cl.skip("data", name, f"no database at {db_path}")
        for name in ("loop.sleeve_can_send_on_demo", "loop.shadow_cycle_runs_clean",
                     "loop.fills_materialise", "loop.settlements_ingest",
                     "loop.gate_kpis_computable"):
            cl.skip("loop", name, f"no database at {db_path}")
    else:
        with ReadOnlyDatabase(db_path) as db:
            check_data(cl, db)
            check_loop(cl, db, settings,
                       sleeve_ids=[s for s in args.sleeves.split(",") if s.strip()],
                       bankroll_cents=bankroll_cents)

    check_sizing(cl, settings, bankroll_cents=bankroll_cents, balance=balance)

    header = {
        "environment": creds.env,
        "base url": creds.base_url,
        "database": f"{db_path} (opened read-only)",
        "run dir": str(run_dir.resolve()),
        "bankroll": f"{dollars(bankroll_cents)} (as the runner would be told)",
        "order test": "ON -- one tiny demo order will be sent"
                      if args.live_order_test else "OFF (read-only; --live-order-test)",
    }
    if args.json:
        print(json.dumps({
            "go": cl.go,
            "header": header,
            "checks": [c.as_dict() for c in cl.checks],
            "blockers": [c.name for c in cl.ranked_blockers()],
        }, indent=2, default=str))
    else:
        print(render(cl, header=header))
    return 0 if cl.go else 1


if __name__ == "__main__":
    raise SystemExit(main())
