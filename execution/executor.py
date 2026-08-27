"""Executor.  T-042.  PLAN.md 4.2, 6.4, I1/I3/I5/I9.

The executor is the ONLY component that talks to a venue (C4.2b) and the only
place risk is enforced (I3).  A sleeve declares what it wants; this diffs that
declaration against what is actually resting and emits the minimum set of
place/cancel actions that closes the gap.

Why the diff is declarative rather than imperative
--------------------------------------------------
`DesiredState` is a statement of the world the sleeve wants, not a list of
commands.  That means an unchanged quote produces NO action -- which is not a
micro-optimisation.  Cancels cost 2 rate-limit tokens and creates cost 10
(venues/kalshi/client.py), and every re-post donates queue position.  A quoter
that re-sends its book each cycle pays both taxes on every tick and ends up
permanently behind the traders who do not.

Where the invariants live in this file
--------------------------------------
  I1  `post_only` is forced True unless the executor was constructed with
      `allow_taker=True`.  Taker permission is a SLEEVE SPEC decision made at
      wiring time, not a field a strategy can flip at runtime.
  I3  `RiskEngine.filter()` runs over the whole batch BEFORE the first send, so
      a batch cannot collectively breach a limit each member respects.
  I5  LIVE orders from a sleeve at `gate < 4` are refused HERE, before the risk
      engine is even consulted.  The risk engine also denies them; two
      independent refusals is the point, because the gate is the one limit whose
      failure mode is regulatory rather than financial.
  I9  The kill switch is checked before the batch AND before every individual
      send, so a KILL file that appears mid-placement stops the very next order
      rather than the next cycle.

Post-only rejections are INFORMATION
------------------------------------
A post-only rejection means the book moved through your price between the
snapshot the sleeve saw and the moment the order arrived -- i.e. someone with a
better view took the other side of the trade you were about to make.  Retrying
at the same price is how a quoter chases a book down.  So a rejection is
recorded, the (ticker, side) is marked crossed at that price, and any quote at
that price or MORE aggressive is skipped until the mark ages out.  The sleeve is
free to reprice; the executor will not re-send on its own.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from core.db import Database
from core.models import Market, OrderRequest, OrderState, RunMode, Side, Venue, now_us
from execution.killswitch import KillSwitch
from execution.oms import OMS, DriftReport, OrderRecord, new_client_order_id
from risk.engine import Denial, PortfolioState, RiskEngine, Verdict
from shadow.engine import ShadowExecutor, ShadowOrder
from strategy.base import DesiredQuote, DesiredState, MarketSnapshot
from venues.kalshi.client import KalshiError

# Modes that never reach a network.  BACKTEST and SHADOW share the shadow
# executor precisely so the sleeve code path is identical to live (PLAN.md 7.2).
PAPERLESS_MODES: tuple[RunMode, ...] = (RunMode.BACKTEST, RunMode.SHADOW)

# Substrings that identify a post-only rejection in a venue error body.
# UNVERIFIED against the live Kalshi API -- the demo has never rejected a
# post-only order in testing, so this is a superset matched case-insensitively.
# Widening it is safe (worst case an unrelated rejection is also not retried);
# narrowing it is not (worst case is a retry loop chasing a moving book).
POST_ONLY_MARKERS: tuple[str, ...] = (
    "post_only", "post-only", "postonly",
    "would_cross", "would cross", "crossed", "cross_market",
    "maker_only", "taker_not_allowed",
)

# How long a "the book crossed us here" observation stays actionable.  It is
# information about a book that keeps moving, so it decays rather than latching.
CROSSED_TTL_US = 60_000_000        # 60s

# I9 is a promise about the WORST case, so it has to bound the worst case.
# `max_cancellable_within(5.0)` is 295 at the shipped rate limit; 200 leaves the
# remaining ~2s of budget for the network round trips the arithmetic ignores.
# Beyond this many resting orders the kill switch cannot make its deadline, so
# the executor refuses to create the situation rather than discovering it during
# an emergency.
MAX_RESTING_ORDERS = 200

# Hosts whose funds are MOCK.  An ALLOWLIST, deliberately: blocklisting the
# production host fails OPEN the day Kalshi adds another one, and the failure
# mode there is an ungated sleeve trading real money under a practice label.
# Anything not named here is treated as real capital.
DEMO_HOST_MARKERS: tuple[str, ...] = ("demo.kalshi.co", "demo-api.kalshi.co")


class SendResult(StrEnum):
    SENT = "sent"
    REPLAY = "replay"                       # idempotency guard fired: NOT sent
    POST_ONLY_REJECTED = "post_only_rejected"
    REJECTED = "rejected"
    UNKNOWN = "unknown"                     # in flight when the link broke
    KILLED = "killed"
    SKIPPED_CROSSED = "skipped_crossed"


class VenueOrderClient(Protocol):
    """The slice of `KalshiClient` the executor uses."""

    def create_order(self, **kwargs: Any) -> dict[str, Any]: ...
    def cancel_order(self, order_id: str) -> dict[str, Any]: ...
    def cancel_all_orders(self) -> int: ...
    def resting_orders(self, **params: Any) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------- #
# Plans and reports
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SleeveRef:
    """Just the two fields the executor is allowed to care about (PLAN.md 4.2)."""

    id: str
    gate: int

    @classmethod
    def of(cls, sleeve: Any) -> "SleeveRef":
        if isinstance(sleeve, SleeveRef):
            return sleeve
        return cls(id=str(sleeve.id), gate=int(sleeve.gate))


@dataclass(frozen=True, slots=True)
class PlacePlan:
    quote: DesiredQuote
    reason: str = "new"          # new | increment | resize


@dataclass(frozen=True, slots=True)
class CancelPlan:
    record: OrderRecord
    reason: str = "not_desired"  # not_desired | size_down | kill


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    mode: RunMode
    sleeve_id: str
    placed: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    denied: tuple[tuple[str, str], ...] = ()          # (quote key, denial reason)
    post_only_rejected: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()                     # sent, outcome unknown
    replayed: tuple[str, ...] = ()                    # idempotency guard fired
    skipped_crossed: tuple[str, ...] = ()
    killed: bool = False
    cancelled_by_kill: int = 0
    gate_blocked: bool = False

    @property
    def sent(self) -> int:
        return len(self.placed)

    @property
    def needs_reconcile(self) -> bool:
        """An unknown outcome is exposure you cannot see.  Reconcile before quoting."""
        return bool(self.unknown)

    def as_dict(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v)
                for k, v in dataclasses.asdict(self).items()}


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #
@dataclass
class Executor:
    """Diffs desired state against reality and emits the difference."""

    db: Database
    risk: RiskEngine
    mode: RunMode = RunMode.SHADOW
    client: VenueOrderClient | None = None
    shadow: ShadowExecutor | None = None
    kill: KillSwitch | None = None
    oms: OMS | None = None
    venue: Venue = Venue.KALSHI
    run_dir: Path | str = "."
    # I1: taker permission is granted by the SLEEVE SPEC at wiring time.  A
    # strategy cannot award it to itself by setting post_only=False on a quote.
    allow_taker: bool = False
    max_resting_orders: int = MAX_RESTING_ORDERS
    crossed_ttl_us: int = CROSSED_TTL_US
    _crossed: dict[tuple[str, str], tuple[int, int]] = field(
        default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        if self.oms is None:
            self.oms = OMS(self.db, venue=self.venue)
        if self.kill is None:
            self.kill = KillSwitch(self.run_dir)
        if self.mode in PAPERLESS_MODES:
            if self.shadow is None:
                self.shadow = ShadowExecutor(self.db)
        elif self.client is None:
            raise ValueError(f"mode {self.mode.value} requires a venue client")

        # PAPER means MOCK FUNDS.  Nothing used to tie it to the demo exchange:
        # `KALSHI_ENV` picked the host and `RunMode` picked the label, and the
        # two were independent, so PAPER against production was real money
        # wearing a practice label.  Refusing that here makes PAPER mean what it
        # says, which is what lets I5 relax for it below.
        if self.mode is RunMode.PAPER and self.client is not None:
            # A real `KalshiClient` ALWAYS sets `base_url` (it has a default), so
            # an absent or empty one means an in-process test double, which
            # cannot reach any exchange.  Enforce only when a host is actually
            # claimed -- otherwise this check would forbid testing PAPER at all.
            host = str(getattr(self.client, "base_url", "") or "")
            if host and not any(marker in host for marker in DEMO_HOST_MARKERS):
                raise ValueError(
                    f"mode=paper is mock-funds only, but the client points at "
                    f"{host!r}, which is not a known demo host.  Use mode=live "
                    f"for real capital, or point KALSHI_ENV at demo."
                )

    # Narrowed accessors -- __post_init__ guarantees these, the type checker
    # cannot see that through Optional fields.
    @property
    def _oms(self) -> OMS:
        assert self.oms is not None
        return self.oms

    @property
    def _kill(self) -> KillSwitch:
        assert self.kill is not None
        return self.kill

    @property
    def is_paperless(self) -> bool:
        return self.mode in PAPERLESS_MODES

    # ------------------------------------------------------------------- diff
    def diff(
        self,
        desired: Sequence[DesiredQuote],
        resting: Sequence[OrderRecord],
    ) -> tuple[list[PlacePlan], list[CancelPlan], list[str]]:
        """Desired versus actual, keyed on (ticker, side, price).

        Size changes follow PLAN.md 6.4's amend rules:

          * MORE wanted -> place a SECOND order for the increment only.  Never
            amend up: an amend loses time priority, so the tranche already
            resting keeps its place in the queue and only the increment starts
            at the back.
          * LESS wanted -> cancel the NEWEST tranches first.  They are the ones
            with the worst queue position, so the priority that is thrown away is
            the least valuable priority available.
        """
        by_key: dict[tuple[str, str, int], list[OrderRecord]] = {}
        for rec in resting:
            if rec.remaining > 0:
                by_key.setdefault(rec.key(), []).append(rec)

        places: list[PlacePlan] = []
        cancels: list[CancelPlan] = []
        unchanged: list[str] = []
        seen: set[tuple[str, str, int]] = set()

        for q in desired:
            key = q.key()
            seen.add(key)
            tranches = sorted(by_key.get(key, []), key=lambda r: r.created_at_us)
            resting_size = sum(r.remaining for r in tranches)

            if resting_size == q.size:
                unchanged.extend(r.client_order_id for r in tranches)
                continue
            if resting_size < q.size:
                if tranches:
                    unchanged.extend(r.client_order_id for r in tranches)
                places.append(PlacePlan(
                    quote=replace(q, size=q.size - resting_size),
                    reason="new" if not tranches else "increment",
                ))
                continue

            # Over-sized: shed newest-first until we are at or under the target.
            over = resting_size - q.size
            for rec in reversed(tranches):
                if over <= 0:
                    unchanged.append(rec.client_order_id)
                    continue
                cancels.append(CancelPlan(rec, reason="size_down"))
                over -= rec.remaining
            if over < 0:
                # Cancelling whole tranches overshot; re-post the remainder.  The
                # REST client has no amend endpoint, so an amend-down (the one
                # free operation per 6.4) is not available and this is the
                # fallback -- a deliberate, bounded priority donation.
                places.append(PlacePlan(replace(q, size=-over), reason="resize"))

        for key, tranches in by_key.items():
            if key not in seen:
                cancels.extend(CancelPlan(r, reason="not_desired") for r in tranches)

        return places, cancels, unchanged

    # --------------------------------------------------------------- execute
    def execute(
        self,
        sleeve: Any,
        desired: DesiredState,
        portfolio: PortfolioState,
        *,
        snapshot: MarketSnapshot | None = None,
        depth_by_ticker: dict[str, float] | None = None,
    ) -> ExecutionReport:
        """One cycle: diff, cancel, risk-check, place.  The only send path."""
        ref = SleeveRef.of(sleeve)
        books: dict[str, Market] = (
            {m.ticker: m for m in snapshot.markets} if snapshot else {}
        )
        depth = depth_by_ticker
        if depth is None and snapshot is not None:
            depth = {m.ticker: m.yes_bid_size for m in snapshot.markets}

        # I9 -- before every batch.
        if self._kill.is_engaged():
            n = self.panic(reason="kill file present at batch start")
            return ExecutionReport(self.mode, ref.id, killed=True, cancelled_by_kill=n)

        quotes = list(desired.quotes)

        # I5 -- refused here, independently of the risk engine.  A misconfigured
        # RiskEngine must not be able to open the live path for an ungated sleeve.
        #
        # The test is `not self.is_paperless`, NOT `mode is LIVE`.  PAPER is not
        # in PAPERLESS_MODES: it uses a real venue client and makes real network
        # calls to whichever exchange KALSHI_ENV points at -- and nothing ties
        # PAPER to demo.  Spelling this check as `mode is LIVE`, as both this
        # gate and the risk engine's did, let a gate-0 sleeve reach the venue
        # client in PAPER mode.  The two checks were also then the SAME
        # predicate written twice, so "two independent refusals" was one
        # refusal, and they failed together.
        # LIVE only.  PAPER is now provably the demo exchange (see
        # `__post_init__`), so an ungated sleeve there risks mock funds and
        # nothing else -- and blocking it made demo lifecycle validation
        # unreachable through the runner, which is the ONE thing demo is for.
        # A rail that prevents you from testing the rails is not a rail.
        if self.mode is RunMode.LIVE and ref.gate < 4:
            return ExecutionReport(
                self.mode, ref.id, gate_blocked=True,
                denied=tuple((str(q.key()), Denial.GATE.value) for q in quotes),
            )

        over_denied: list[tuple[DesiredQuote, Verdict]] = []
        resting = self._oms.open_orders(sleeve_id=ref.id, venue=self.venue)
        places, cancels, unchanged = self.diff(quotes, resting)

        # I9 -- never rest more than the kill switch can cancel in time.
        all_open = len(self._oms.open_orders(venue=self.venue))
        room = max(0, self.max_resting_orders - all_open + len(cancels))
        if len(places) > room:
            over = places[room:]
            places = places[:room]
            over_denied.extend(
                (p.quote, Verdict(False, Denial.RESTING_ORDER_CAP)) for p in over
            )

        # Cancel first: it frees exposure the risk engine will otherwise count
        # against the replacements, and it is the cheap operation (2 tokens).
        cancelled: list[str] = []
        for plan in cancels:
            if self._cancel(plan):
                cancelled.append(plan.record.client_order_id)

        # A price the book has already crossed is not worth re-sending.
        live_plans: list[PlacePlan] = []
        skipped: list[str] = []
        for p in places:
            if self._is_crossed(p.quote):
                skipped.append(str(p.quote.key()))
            else:
                live_plans.append(p)

        # I3 -- risk over the WHOLE batch, before the first send.
        plan_quotes = [p.quote for p in live_plans]
        approved, denied = self.risk.filter(
            plan_quotes, portfolio,
            sleeve_gate=ref.gate, mode=self.mode, depth_by_ticker=depth,
        )
        # A multi-leg structure is ATOMIC.  The risk engine judges quotes one at
        # a time, so a 4-leg hedge whose 4th leg trips the position cap would
        # otherwise be placed as a 3-leg naked short -- an orphan created
        # deliberately, at the moment of entry, by the control that exists to
        # prevent exactly that exposure.
        #
        # Observed live before this guard: a 4-leg KXLALIGAGAME basket had three
        # legs denied on `position_cap` and rested the remaining ONE.  A hedged
        # 2c arbitrage silently became a single directional short at full size.
        #
        # Partial entry is legitimate only when the sleeve says so (S2 sizes its
        # legs to be independently safe when `allow_partial` is set); it is never
        # legitimate as an ACCIDENT of per-quote filtering.
        approved, structure_denied = _drop_incomplete_structures(
            plan_quotes, approved
        )
        denied = list(denied) + structure_denied + over_denied

        placed: list[str] = []
        post_only_rejected: list[str] = []
        rejected: list[str] = []
        unknown: list[str] = []
        replayed: list[str] = []
        killed = False
        cancelled_by_kill = 0

        # `filter` returns the SAME objects in the SAME order, so a pointer walk
        # matches plans to verdicts exactly even when two quotes are identical.
        cursor = 0
        for plan, q in zip(live_plans, plan_quotes, strict=True):
            if cursor >= len(approved) or approved[cursor] is not q:
                continue                     # denied; the verdict is reported below
            cursor += 1

            # I9 -- and again before EVERY send, not once per batch.  A KILL file
            # written while this loop runs stops the next order, not the next cycle.
            if self._kill.is_engaged():
                killed = True
                cancelled_by_kill = self.panic(reason="kill file appeared mid-placement")
                break

            req = self.build_request(ref, plan.quote, desired, reason=plan.reason,
                                     structure_id=plan.quote.structure_id)
            outcome = self.submit(req, market=books.get(req.ticker))
            match outcome:
                case SendResult.SENT:
                    placed.append(req.client_order_id)
                case SendResult.POST_ONLY_REJECTED:
                    post_only_rejected.append(req.client_order_id)
                case SendResult.REJECTED:
                    rejected.append(req.client_order_id)
                case SendResult.UNKNOWN:
                    unknown.append(req.client_order_id)
                case SendResult.REPLAY:
                    replayed.append(req.client_order_id)
                case SendResult.SKIPPED_CROSSED:
                    skipped.append(str(plan.quote.key()))
                case SendResult.KILLED:
                    killed = True
                    break

        return ExecutionReport(
            mode=self.mode,
            sleeve_id=ref.id,
            placed=tuple(placed),
            cancelled=tuple(cancelled),
            unchanged=tuple(unchanged),
            denied=tuple(
                (str(q.key()), v.reason.value if v.reason else "unknown")
                for q, v in denied
            ),
            post_only_rejected=tuple(post_only_rejected),
            rejected=tuple(rejected),
            unknown=tuple(unknown),
            replayed=tuple(replayed),
            skipped_crossed=tuple(skipped),
            killed=killed,
            cancelled_by_kill=cancelled_by_kill,
        )

    # --------------------------------------------------------------- requests
    def build_request(
        self,
        sleeve: SleeveRef,
        quote: DesiredQuote,
        desired: DesiredState | None = None,
        *,
        reason: str = "new",
        structure_id: str | None = None,
    ) -> OrderRequest:
        """Mint the idempotency key and attach the rationale.

        C4.2c: the rationale is assembled here and persisted with the order in the
        same transaction that records the intent, so there is no moment at which
        an order exists whose reasoning cannot be reconstructed.  The sleeve's own
        rationale wins on key collisions -- the executor's fields are context.
        """
        rationale: dict[str, Any] = {
            "sleeve_id": sleeve.id,
            "sleeve_gate": sleeve.gate,
            "mode": self.mode.value,
            "diff_reason": reason,
            "decided_at_us": now_us(),
            **(dict(desired.rationale) if desired is not None else {}),
            **dict(quote.rationale),
        }
        return OrderRequest(
            client_order_id=new_client_order_id(),
            sleeve_id=sleeve.id,
            venue=self.venue,
            ticker=quote.ticker,
            side=quote.side,
            price_cents=quote.price_cents,
            size=quote.size,
            # I1: maker by default.  `allow_taker` is the sleeve spec's grant.
            post_only=quote.post_only if self.allow_taker else True,
            mode=self.mode,
            structure_id=structure_id,
            rationale=rationale,
        )

    # ------------------------------------------------------------------ send
    def submit(self, req: OrderRequest, *, market: Market | None = None) -> SendResult:
        """The ONE send path.  Idempotent by construction.

        Order of operations is the whole point:
          1. kill check   (I9)
          2. crossed check (do not chase a book that already went through us)
          3. record intent -- state='pending', BEFORE the network call
          4. send only if the intent was NEW
        Step 3 returning False means this `client_order_id` has been here before,
        and the send is skipped.  That is the T-041 guarantee, and it holds no
        matter how the caller got here: retry loop, crash recovery, or a replayed
        message from a reconnected feed.
        """
        if self._kill.is_engaged():
            return SendResult.KILLED
        if self._is_crossed_price(req.ticker, req.side, req.price_cents):
            return SendResult.SKIPPED_CROSSED

        if not self._oms.record_intent(req):
            return SendResult.REPLAY

        if self.is_paperless:
            return self._send_shadow(req, market)
        return self._send_venue(req)

    def _send_shadow(self, req: OrderRequest, market: Market | None) -> SendResult:
        assert self.shadow is not None
        queue_ahead, bid, ask = _book_context(market, req.side, req.price_cents)
        order = ShadowOrder.create(
            # Our id is the idempotency key AND the primary key of the row
            # `record_intent` already wrote, so it must be the one that reaches
            # the shadow ledger -- otherwise every shadow send writes a second,
            # untracked orders row.
            client_order_id=req.client_order_id,
            sleeve_id=req.sleeve_id,
            ticker=req.ticker,
            side=req.side,
            price_cents=req.price_cents,
            size=req.size,
            queue_ahead=queue_ahead,
            book_bid=bid,
            book_ask=ask,
            rationale=req.rationale,
            structure_id=req.structure_id,
            mode=req.mode,
        )
        self.shadow.submit(order)
        self._oms.record_ack(req.client_order_id, None, OrderState.OPEN, mode=req.mode)
        return SendResult.SENT

    def _send_venue(self, req: OrderRequest) -> SendResult:
        assert self.client is not None
        side, price = _to_venue_side(req.side, req.price_cents)
        try:
            resp = self.client.create_order(
                ticker=req.ticker,
                side=side,
                count=req.size,
                price_cents=price,
                client_order_id=req.client_order_id,
                post_only=req.post_only,
            )
        except KalshiError as exc:
            body = f"{exc} {getattr(exc, 'body', '')}"
            if _is_post_only_rejection(body):
                return self._note_post_only(req, body)
            status = int(getattr(exc, "status", 0) or 0)
            if 400 <= status < 500 and status != 429:
                # A definite refusal: the order does not exist at the venue.
                self._oms.record_reject(req.client_order_id, "venue_rejected",
                                        status=status, detail=str(exc)[:300])
                return SendResult.REJECTED
            # 429 / 5xx / exhausted network retries: the order MAY be resting.
            # Leave the row `pending` -- that is exactly what `reconcile()` adopts.
            self._oms.record_ack(
                req.client_order_id, None, OrderState.PENDING,
                rationale_extra={"send_outcome": "unknown", "detail": str(exc)[:300]},
            )
            return SendResult.UNKNOWN

        venue_order_id, state, note = _parse_ack(resp)
        if state is OrderState.REJECTED and _is_post_only_rejection(note):
            return self._note_post_only(req, note)
        self._oms.record_ack(req.client_order_id, venue_order_id, state)
        if state is OrderState.REJECTED:
            self._oms.record_reject(req.client_order_id, "venue_rejected", detail=note)
            return SendResult.REJECTED
        return SendResult.SENT

    # ---------------------------------------------------------------- cancel
    def _cancel(self, plan: CancelPlan) -> bool:
        rec = plan.record
        if not self.is_paperless and rec.venue_order_id and self.client is not None:
            try:
                self.client.cancel_order(rec.venue_order_id)
            except KalshiError as exc:
                status = int(getattr(exc, "status", 0) or 0)
                if not (400 <= status < 500):
                    # The venue may still be resting it; do NOT mark it cancelled
                    # locally, or the diff will happily place a duplicate.
                    return False
                # 4xx on a cancel means it is already gone (filled, or cancelled
                # by the exchange at close).  Recording it as cancelled is right.
        self._oms.mark_cancelled(rec.client_order_id, reason=plan.reason)
        return True

    def panic(self, *, reason: str = "panic") -> int:
        """I9 cancel-all.  Returns the number of venue orders cancelled.

        In BACKTEST/SHADOW there is nothing of ours at the venue, so the kill path
        must NOT open a connection -- shadow mode makes no network call, ever
        (PLAN.md 7.2, asserted by a test).  Local shadow orders are closed out
        instead, which is what the counterfactual fill model reads.
        """
        self._kill.engage(reason)
        if self.is_paperless or self.client is None:
            open_local = self._oms.open_orders(venue=self.venue)
            for rec in open_local:
                self._oms.mark_cancelled(rec.client_order_id, reason="kill")
            return len(open_local)
        cancelled = self._kill.panic(self.client, reason=reason)
        for rec in self._oms.open_orders(venue=self.venue):
            self._oms.mark_cancelled(rec.client_order_id, reason="kill")
        return cancelled

    def reconcile(self, *, adopt: bool = True) -> DriftReport:
        """Local versus venue.  Shadow has no venue, so it is clean by definition."""
        if self.is_paperless or self.client is None:
            n = len(self._oms.open_orders(venue=self.venue))
            return DriftReport(checked_at_us=now_us(), local_open=n, venue_resting=n)
        return self._oms.reconcile(self.client, venue=self.venue, adopt=adopt)

    # --------------------------------------------------- post-only bookkeeping
    def _note_post_only(self, req: OrderRequest, detail: str) -> SendResult:
        """Record the rejection as the observation it is, and do not retry it."""
        self._oms.record_reject(
            req.client_order_id, "post_only_rejected",
            detail=detail[:300], price_cents=req.price_cents,
            book_moved_through=True,
        )
        self._crossed[(req.ticker, req.side.value)] = (req.price_cents, now_us())
        return SendResult.POST_ONLY_REJECTED

    def _is_crossed(self, quote: DesiredQuote) -> bool:
        return self._is_crossed_price(quote.ticker, quote.side, quote.price_cents)

    def _is_crossed_price(self, ticker: str, side: Side, price_cents: int) -> bool:
        """Both sides are BUYS in their own reference (buy YES at p, buy NO at q),
        so a higher price is a MORE aggressive price.  If the book crossed us at
        p, it crosses us at anything >= p too."""
        hit = self._crossed.get((ticker, side.value))
        if hit is None:
            return False
        price, at_us = hit
        if now_us() - at_us > self.crossed_ttl_us:
            del self._crossed[(ticker, side.value)]
            return False
        return price_cents >= price

    @property
    def crossed(self) -> dict[tuple[str, str], int]:
        """(ticker, side) -> the price the book last moved through."""
        return {k: v[0] for k, v in self._crossed.items()}

    def clear_crossed(self, ticker: str | None = None) -> None:
        if ticker is None:
            self._crossed.clear()
            return
        for key in [k for k in self._crossed if k[0] == ticker]:
            del self._crossed[key]


# --------------------------------------------------------------------------- #
# Structure atomicity
# --------------------------------------------------------------------------- #
def _drop_incomplete_structures(
    planned: Sequence[DesiredQuote],
    approved: Sequence[DesiredQuote],
) -> tuple[list[DesiredQuote], list[tuple[DesiredQuote, Verdict]]]:
    """All legs of a structure, or none of them.

    Returns the surviving approvals plus the extra denials, so the caller
    reports a dropped leg as `structure_incomplete` rather than silently
    losing it.  Quotes with no `structure_id` are single-leg intents and pass
    through untouched.
    """
    wanted: dict[str, int] = {}
    for q in planned:
        if q.structure_id:
            wanted[q.structure_id] = wanted.get(q.structure_id, 0) + 1
    if not wanted:
        return list(approved), []

    got: dict[str, int] = {}
    for q in approved:
        if q.structure_id:
            got[q.structure_id] = got.get(q.structure_id, 0) + 1

    broken = {sid for sid, n in wanted.items() if got.get(sid, 0) < n}
    if not broken:
        return list(approved), []

    kept: list[DesiredQuote] = []
    dropped: list[tuple[DesiredQuote, Verdict]] = []
    for q in approved:
        if q.structure_id in broken:
            dropped.append((q, Verdict(False, Denial.STRUCTURE_INCOMPLETE)))
        else:
            kept.append(q)
    return kept, dropped


# --------------------------------------------------------------------------- #
# Venue-boundary helpers.  YES-referencing happens HERE and nowhere else.
# --------------------------------------------------------------------------- #
def _to_venue_side(side: Side, price_cents: int) -> tuple[str, int]:
    """Internal quote -> Kalshi wire (bid/ask).  ONLY the side changes.

    `price_cents` is YES-REFERENCED everywhere in this system (PLAN.md 0.3), on
    both sides.  A `Side.NO` quote at `price_cents = p` means "rest a YES ask at
    p", which both sleeves state explicitly at their emission sites:

        s3_linked_rv.py:  side=Side.NO,  # rest a YES ask at this YES price
        s2_shortbasket.py: rationale carries rest_yes_price_cents = price

    Kalshi's wire is the same convention: "bid" buys YES at the given price,
    "ask" SELLS YES at the given price (see `KalshiClient.create_order`).  So the
    price passes through untouched and only the verb changes.

    THE BUG THIS REPLACES.  The old version mirrored to `100 - p`, on the
    assumption that a quote is a side-referenced BUY of the named side.  That
    assumption used to hold and stopped holding when the risk engine was
    corrected to YES-referencing (errata E8) -- the venue boundary was left
    behind.  Consequence: an S2 short-basket leg meant to rest as a YES ask at
    5c reached Kalshi as a YES ask at 95c.  Not a mispriced order -- a
    completely different one, on the wrong side of the book, which would never
    fill where it was aimed and could fill where it was not.  Two independent
    reviews found this the same day.
    """
    if side is Side.YES:
        return "bid", price_cents
    return "ask", price_cents


def _book_context(
    market: Market | None, side: Side, price_cents: int
) -> tuple[float, int | None, int | None]:
    """Queue ahead of us at our price, plus the touch, at DECISION time.

    Shadow's fill model assumes we joined behind everything already displayed at
    our price (shadow/engine.py), so this reports the displayed size only when we
    are joining an existing level -- a new level has nobody ahead of it.
    """
    if market is None:
        return 0.0, None, None
    if side is Side.YES:
        ahead = market.yes_bid_size if market.yes_bid == price_cents else 0.0
    else:
        # A Side.NO quote rests on the YES ASK at its own (YES-referenced)
        # price.  The old `100 - price_cents` here was the same side-referenced
        # mistake as `_to_venue_side`, so queue-ahead for every NO leg was read
        # off the wrong level of the book and persisted into the rationale --
        # which is exactly the number the shadow fill model reads back.
        ahead = market.yes_ask_size if market.yes_ask == price_cents else 0.0
    return float(ahead or 0.0), market.yes_bid, market.yes_ask


def _is_post_only_rejection(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in POST_ONLY_MARKERS)


def _parse_ack(resp: dict[str, Any]) -> tuple[str | None, OrderState, str]:
    """Kalshi's create-order reply -> (venue_order_id, state, note)."""
    order = resp.get("order") if isinstance(resp.get("order"), dict) else resp
    venue_order_id = order.get("order_id") or resp.get("order_id")
    status = str(order.get("status") or "").lower()
    note = str(resp)

    match status:
        case "resting" | "open" | "":
            state = OrderState.OPEN
        case "executed" | "filled":
            # The order is no longer resting.  The POSITION still comes from the
            # fill stream and only from there (I4) -- this only takes it out of
            # the resting diff.
            state = OrderState.FILLED
        case "canceled" | "cancelled":
            state = OrderState.REJECTED if _is_post_only_rejection(note) \
                else OrderState.CANCELLED
        case "rejected":
            state = OrderState.REJECTED
        case "pending":
            state = OrderState.PENDING
        case _:
            state = OrderState.OPEN
    return (str(venue_order_id) if venue_order_id else None), state, note
