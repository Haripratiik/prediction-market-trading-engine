"""The trading loop.  Wires recorder -> strategist -> risk -> executor -> monitor.

    python -m runner --mode shadow --once
    python -m runner --mode shadow --interval 60

Four processes conceptually, one loop here for clarity.  The contract between
them (PLAN.md 4.2) is what matters:

    the DATABASE is the truth; memory is a cache          (I4)
    risk is enforced in the executor, not in a sleeve     (I3)
    LIVE orders require a sleeve at Gate 4                (I5)
    a KILL file cancels everything within 5 seconds       (I9)

Default mode is SHADOW, deliberately.  Going live is an explicit act.

Why this file owns almost no logic
----------------------------------
Every order goes through `execution.Executor.execute()` and nothing else.  An
earlier version of this loop called `risk.filter()` itself and then drove
`ShadowExecutor` directly, which looked equivalent and was not: it had no
idempotency key, so a crash mid-cycle double-sent on restart; no diff, so it
re-posted its whole book every tick and donated queue position; and no kill
check between individual sends, so a KILL file written mid-batch was not seen
until the next cycle.  The executor exists to hold those guarantees in ONE
place; the loop's job is to decide when to call it, not to reimplement it.
"""

from __future__ import annotations

import argparse
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.config import Settings, load_settings
from core.db import Database
from core.models import Market, RunMode, Side, Venue, now_us
from execution.executor import PAPERLESS_MODES, ExecutionReport, Executor
from execution.fillfeed import fill_feed_for
from execution.oms import OMS
from execution.structures import StructureIntent, StructureStore
from risk.engine import PortfolioState, RiskEngine, per_contract_cost_cents
from strategy.base import DesiredQuote, DesiredState, MarketSnapshot, Sleeve
from strategy.s1_structural import S1Structural
from strategy.s2_shortbasket import S2ShortBasket
from strategy.s3_linked_rv import S3LinkedRV
from venues.kalshi.client import KalshiError


@dataclass
class RunnerStats:
    cycles: int = 0
    considered: int = 0
    quoted: int = 0
    approved: int = 0
    denied: int = 0
    placed: int = 0
    cancelled: int = 0
    unchanged: int = 0
    replayed: int = 0
    fills: int = 0
    structures_opened: int = 0
    structure_states: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    denials: dict[str, int] = field(default_factory=dict)
    started_us: int = field(default_factory=now_us)

    def report(self) -> str:
        mins = max((now_us() - self.started_us) / 60_000_000, 1e-9)
        return (f"cycles={self.cycles} quoted={self.quoted} placed={self.placed} "
                f"cancelled={self.cancelled} unchanged={self.unchanged} "
                f"denied={self.denied} fills={self.fills} errors={self.errors} uptime={mins:.1f}m")


# A quote older than this is not a price, it is a memory.  MEASURED on the real
# database: 99% of the quotable universe was **517 minutes** stale, because the
# universe sweep has run once while only the ~400-ticker L1 watchlist is polled
# continuously.  With no staleness filter, 40 of 42 quoted legs were priced off
# 8.6-hour-old books -- so every "locked arbitrage" the engine reported was an
# overround that may have closed overnight.  A stale book does not merely add
# noise; it systematically shows arbitrage; the spread only ever LOOKS wide when
# one side has not been refreshed.
MAX_QUOTE_AGE_US = 300_000_000        # 5 minutes


def build_snapshot(db: Database, *, bankroll_cents: int,
                   limit: int = 3000,
                   max_age_us: int = MAX_QUOTE_AGE_US) -> MarketSnapshot:
    """Assemble the sleeve's view from the LATEST recorded snapshot per ticker.

    Point-in-time by construction: every row is the newest observation at or
    before now, never a later one (R5a) -- and never one so old that it cannot
    be acted on (`max_age_us`).
    """
    from core.models import Event, Series, SettlementSource
    import json

    ts = now_us()
    rows = db.conn.execute(
        """SELECT m.* FROM market_snapshots m
           JOIN (SELECT ticker, MAX(observed_at_us) AS t
                 FROM market_snapshots WHERE observed_at_us <= ?
                 GROUP BY ticker) latest
             ON m.ticker = latest.ticker AND m.observed_at_us = latest.t
           WHERE m.yes_bid IS NOT NULL AND m.yes_ask IS NOT NULL
             AND m.volume_24h > 0
             -- ONLY tradeable markets.  Without this filter the latest snapshot
             -- admitted 431 `finalized` and 36 `determined` markets -- already
             -- RESOLVED -- straight into the sleeves.  Quoting a resolved market
             -- is not a forecast, and any decision recorded against one
             -- manufactures skill from an outcome that was already known.
             AND m.status = 'active'
             AND m.observed_at_us >= ?
           ORDER BY m.volume_24h DESC LIMIT ?""",
        (ts, ts - max_age_us, limit),
    ).fetchall()

    markets = tuple(
        Market(
            ticker=r["ticker"], event_ticker=r["event_ticker"] or "",
            series_ticker=r["series_ticker"] or "", title=r["title"] or "",
            status=r["status"] or "", yes_bid=r["yes_bid"], yes_ask=r["yes_ask"],
            yes_bid_size=r["yes_bid_size"] or 0.0, yes_ask_size=r["yes_ask_size"] or 0.0,
            volume=r["volume"] or 0.0, volume_24h=r["volume_24h"] or 0.0,
            open_interest=r["open_interest"] or 0.0, close_at_us=r["close_at_us"],
            rules_hash=r["rules_hash"] or "",
        )
        for r in rows
    )

    wanted_events = {m.event_ticker for m in markets}
    events: dict[str, Event] = {}
    if wanted_events:
        q = ",".join("?" * len(wanted_events))
        for r in db.conn.execute(
            f"""SELECT e.* FROM event_snapshots e
                JOIN (SELECT event_ticker, MAX(observed_at_us) t FROM event_snapshots
                      GROUP BY event_ticker) l
                  ON e.event_ticker = l.event_ticker AND e.observed_at_us = l.t
                WHERE e.event_ticker IN ({q})""",
            tuple(wanted_events),
        ):
            events[r["event_ticker"]] = Event(
                event_ticker=r["event_ticker"], series_ticker=r["series_ticker"] or "",
                category=r["category"] or "", title=r["title"] or "",
                mutually_exclusive=bool(r["mutually_exclusive"]),
                # WITHOUT THIS the human verdict never reaches a sleeve, so
                # `safe_to_buy` is permanently False on the live path and S2's
                # long direction is unreachable dead code -- errata E9, shipped
                # a second time in a different file.  `backtest/engine.py` DOES
                # hydrate it, so omitting it here also made the backtest able to
                # take a side production cannot, which quietly invalidates any
                # gate evidence the backtest produces.
                exhaustive_verified=bool(r["exhaustive_verified"]),
                collateral_return_type=r["collateral_return_type"] or "",
                settlement_sources=tuple(
                    SettlementSource(**s)
                    for s in json.loads(r["settlement_sources_json"] or "[]")
                ),
            )

    series: dict[str, Series] = {}
    for r in db.conn.execute("SELECT * FROM series_cache"):
        series[r["ticker"]] = Series(
            ticker=r["ticker"], title=r["title"] or "", category=r["category"] or "",
            fee_type=r["fee_type"], fee_multiplier=r["fee_multiplier"],
        )

    return MarketSnapshot(
        now_us=ts, markets=markets, events=events, series=series,
        bankroll_cents=bankroll_cents,
    )


@dataclass
class Runner:
    db: Database
    settings: Settings
    sleeves: list[Sleeve]
    mode: RunMode = RunMode.SHADOW
    bankroll_cents: int = 1_000_000
    run_dir: Path | str = "."
    client: object | None = None
    stats: RunnerStats = field(default_factory=RunnerStats)
    risk: RiskEngine = field(init=False)
    oms: OMS = field(init=False)
    executor: Executor = field(init=False)
    fills: object = field(init=False)
    _stop: bool = False

    def __post_init__(self) -> None:
        self.risk = RiskEngine(self.settings.risk)
        # A risk limit that cannot be satisfied at ANY portfolio the other limits
        # permit is not conservative -- it denies everything and looks exactly
        # like a strategy that found nothing (PLAN.md errata E4).  Refuse to
        # start rather than trade a configuration nobody can clear.
        problems = self.risk.validate()
        if problems:
            raise ValueError("inconsistent risk config: " + "; ".join(problems))

        # A bankroll bigger than the account is a risk engine measuring nothing.
        # Every limit in section 9 is a FRACTION of `bankroll_cents`, so
        # declaring $10,000 against a $197 balance permits a $200 position and
        # $4,000 of gross deployment -- and the VENUE, not the risk engine,
        # becomes the binding constraint.  The run would then be measuring how
        # the exchange rejects oversized orders, which is not what any of the
        # gates are asking.
        if self.client is not None and self.mode not in PAPERLESS_MODES:
            try:
                raw = self.client.balance().get("balance")
                venue_cents = int(raw) if raw is not None else None
            except Exception:                    # noqa: BLE001
                venue_cents = None               # a probe, never a hard failure
            if venue_cents is not None and venue_cents < self.bankroll_cents:
                raise ValueError(
                    f"declared bankroll ${self.bankroll_cents/100:,.2f} exceeds the "
                    f"venue balance ${venue_cents/100:,.2f}.  Every risk limit is a "
                    f"fraction of the declared bankroll, so the venue would bind "
                    f"first and the limits would be untested.  Pass "
                    f"--bankroll {venue_cents/100:.0f} or fund the account."
                )

        self.oms = OMS(self.db, venue=Venue.KALSHI)
        # Built ONCE, not per cycle.  The executor carries the post-only
        # "the book already crossed us here" map, which has a 60s TTL -- rebuilt
        # every cycle it would be empty every cycle, and the loop would chase a
        # falling book forever, re-sending the price that just got run over.
        self.executor = Executor(
            db=self.db, risk=self.risk, mode=self.mode,
            oms=self.oms, run_dir=self.run_dir,
        )
        # Closes the loop.  Without this the engine places orders and never
        # learns whether they filled, so positions stay empty, P&L stays zero,
        # and every gate promotion criterion is unmeasurable.  One feed decides
        # per mode: shadow materialises fills from the recorded tape, live reads
        # them from the venue -- deliberately the SAME table either way, so the
        # KPIs cannot tell the two apart (PLAN.md 7.2).
        self.fills = fill_feed_for(self.mode, self.oms, client=self.client)
        # Multi-leg structures are ONE risk object with a deadline.  Without a
        # row, a leg that fills while its partner does not is an orphan nobody
        # is watching -- the most expensive failure mode an RV book has, and the
        # one KPI 6 exists to measure.
        self.structures = StructureStore(self.db, venue=Venue.KALSHI)

    def request_stop(self, *_: object) -> None:
        self._stop = True

    # ------------------------------------------------------------------ state
    def portfolio_state(self) -> PortfolioState:
        """Reconstruct exposure from the DATABASE, every cycle (I4).

        The previous loop built a blank `PortfolioState` each cycle: full
        bankroll, no exposure, nothing deployed.  Every limit in section 9 is
        computed against that state, so none of them ever accumulated -- the
        same quotes were re-approved indefinitely and gross deployment could
        grow without bound while every check reported plenty of room.

        Capital at risk is what you PAID, not the notional: a long YES of n
        contracts at average price p risks n*p, and a resting order locks
        collateral at the same rate until it fills or is cancelled.
        """
        exposure: dict[str, int] = {}

        for ticker, pos in self.oms.positions().items():
            # `avg_price_cents` is YES-referenced (execution/oms.py): a NO
            # contract bought at q is a short YES at 100-q.  So a LONG position
            # paid `avg` per contract and a SHORT one paid `100 - avg` -- using
            # `avg` for both under-counts short capital exactly the way the risk
            # engine did before `quote_cost_cents`.
            n = abs(pos.net_contracts)
            per = (pos.avg_price_cents if pos.net_contracts > 0
                   else 100.0 - pos.avg_price_cents)
            exposure[ticker] = exposure.get(ticker, 0) + int(round(n * max(0.0, per)))

        # Resting orders are committed capital too.  Omitting them lets a cycle
        # approve a new order against room its own unfilled orders already hold.
        for rec in self.oms.open_orders(venue=Venue.KALSHI):
            per = per_contract_cost_cents(rec.side, rec.price_cents)
            exposure[rec.ticker] = exposure.get(rec.ticker, 0) + per * rec.remaining

        by_theme: dict[str, int] = {}
        for ticker, cents in exposure.items():
            theme = self.risk.theme_of.get(ticker, ticker)
            by_theme[theme] = by_theme.get(theme, 0) + cents

        gross = sum(exposure.values())
        realized = self._realized_pnl_cents()
        bankroll = self.bankroll_cents + realized
        return PortfolioState(
            bankroll_cents=bankroll,
            peak_bankroll_cents=max(self.bankroll_cents, bankroll),
            cash_cents=max(0, bankroll - gross),
            day_pnl_cents=self._day_pnl_cents(),
            exposure_by_ticker=exposure,
            exposure_by_theme=by_theme,
            venue_exposure={Venue.KALSHI.value: gross},
            killed=self.executor._kill.is_engaged(),
        )

    def _settled_pnl_cents(self, *, since_us: int | None = None) -> int:
        """Realised P&L from settled markets, in cents.

        `settlements` records the OUTCOME, not the money: P&L only exists
        relative to a position, so it is derived here rather than stored.  Since
        `avg_price_cents` is YES-referenced and `net_contracts` is signed, one
        expression covers both directions --

            pnl = net * (payout - avg)

        For a long (net > 0) that is n*(100-avg) on a YES resolution.  For a
        short (net < 0) the same expression yields n*avg, which is the premium
        kept when the market resolves NO.  A voided market pays nobody.
        """
        sql = "SELECT ticker, outcome, voided FROM settlements WHERE venue = ?"
        params: list[object] = [Venue.KALSHI.value]
        if since_us is not None:
            sql += " AND settled_at_us >= ?"
            params.append(since_us)
        settled = {r["ticker"]: r for r in self.db.conn.execute(sql, tuple(params))}
        if not settled:
            return 0

        total = 0.0
        for ticker, pos in self.oms.positions().items():
            row = settled.get(ticker)
            if row is None or row["voided"]:
                continue
            payout = 100.0 if row["outcome"] else 0.0
            total += pos.net_contracts * (payout - pos.avg_price_cents)
        return int(round(total))

    def _realized_pnl_cents(self) -> int:
        """Settled P&L net of fees.  Zero until something settles, which is honest.

        Fees are signed, and a maker rebate is negative (core/models.py), so
        subtracting the total is correct in both directions.
        """
        return self._settled_pnl_cents() - self.oms.realized_fees_cents()

    def _day_pnl_cents(self) -> int:
        """Today's settled P&L, for the daily-loss stop (PLAN.md 9)."""
        return self._settled_pnl_cents(since_us=now_us() - 86_400_000_000)

    # ------------------------------------------------------------------ cycle
    def cycle(self) -> None:
        # Ingest fills FIRST.  Exposure is read from the database immediately
        # below, so a fill that landed since the last cycle must be visible
        # before any limit is evaluated against it -- otherwise the risk engine
        # sizes against a position the engine already holds but cannot see.
        ingest = self.fills.poll()
        if ingest.recorded:
            self.stats.fills += ingest.recorded
            print(f"[runner] ingested {ingest.recorded} fill(s), "
                  f"{ingest.contracts} contracts, fees {ingest.fees_cents}c",
                  flush=True)
        if getattr(ingest, "unmatched", 0):
            # A fill we cannot tie to a local order is a position we are
            # carrying and cannot see (PLAN.md 6.6 drift).
            print(f"[runner] WARNING {ingest.unmatched} unmatched fill(s) -- "
                  f"reconciling", flush=True)
            self.executor.reconcile()

        snapshot = build_snapshot(self.db, bankroll_cents=self.bankroll_cents)
        # a theme is one underlying uncertainty -- events are the natural proxy
        self.risk.theme_of = {m.ticker: (m.event_ticker or m.ticker)
                              for m in snapshot.markets}
        depth = {m.ticker: m.yes_bid_size for m in snapshot.markets}
        state = self.portfolio_state()

        for sleeve in self.sleeves:
            desired: DesiredState = sleeve.desired_state(snapshot)
            self.stats.considered += len(desired.decisions)
            self.stats.quoted += len(desired.quotes)
            decision_ids = self._record_decisions(sleeve.id, desired)

            report = self.executor.execute(
                sleeve, desired, state,
                snapshot=snapshot, depth_by_ticker=depth,
            )
            # Structure rows are written for what was ACTUALLY SENT.  Opening
            # them beforehand gave a row to every structure the risk engine then
            # denied in full -- measured live, one such row contributed 354c of
            # target margin to KPI 6's denominator for a trade that never
            # happened, and would later sweep to `orphaned` with zero fills.
            # Both directions of that error inflate the denominator and deflate
            # the ratio, in the one metric whose job is to catch orphans.
            self._open_structures(sleeve, desired, snapshot.now_us,
                                  placed=report.placed)
            self._absorb(report)
            self._link_decisions(report.placed, decision_ids)

            if report.killed:
                print("[runner] executor reports KILLED -- halting", flush=True)
                self._stop = True
                break
            if report.needs_reconcile:
                # An order whose outcome is unknown is exposure you cannot see.
                drift = self.executor.reconcile()
                print(f"[runner] reconciled after {len(report.unknown)} unknown "
                      f"send(s): {drift.as_dict()}", flush=True)

            # Exposure the batch just committed must count against the NEXT
            # sleeve in the same cycle, or two sleeves each fill the same room.
            state = self.portfolio_state()

        # Advance every live structure to the state its FILLS say it is in, and
        # surface orphans.  Done AFTER all sleeves so a structure opened this
        # cycle is swept in the same pass that could already have filled it.
        books = {m.ticker: m for m in snapshot.markets}
        changed = self.structures.sweep(
            now=snapshot.now_us, books=books, bankroll_cents=self.bankroll_cents,
        )
        for rec in changed:
            self.stats.structure_states[rec.state.value] = (
                self.stats.structure_states.get(rec.state.value, 0) + 1)
            if rec.state.value == "orphaned":
                # Not a warning to be logged and forgotten: an orphan is a naked
                # directional position where a hedge was intended.
                print(f"[runner] ORPHANED {rec.structure_id} -- "
                      f"{rec.n_legs} legs, sleeve {rec.sleeve_id}", flush=True)
        self.stats.cycles += 1

    def _open_structures(self, sleeve: Sleeve, desired: DesiredState,
                         now_us_: int, *, placed: tuple[str, ...] = ()) -> int:
        """One row per multi-leg intent that actually reached the book.

        Single-leg quotes carry no `structure_id` and are skipped -- they are
        not structures and giving them rows would drown KPI 6 in noise.
        """
        if not placed:
            return 0
        sent = {
            rec.structure_id
            for rec in (self.oms.get(c) for c in placed)
            if rec is not None and rec.structure_id
        }
        groups: dict[str, list[DesiredQuote]] = {}
        for q in desired.quotes:
            sid = q.structure_id or q.rationale.get("structure_id")
            if sid and str(sid) in sent:
                groups.setdefault(str(sid), []).append(q)
        opened = 0
        for sid, legs in groups.items():
            if len(legs) < 2:
                continue
            try:
                intent = StructureIntent.from_quotes(
                    legs, sleeve_id=sleeve.id, now=now_us_,
                )
            except ValueError as exc:
                print(f"[runner] structure {sid} rejected: {exc}", flush=True)
                continue
            if self.structures.open(intent):
                opened += 1
        self.stats.structures_opened += opened
        return opened

    def _absorb(self, report: ExecutionReport) -> None:
        self.stats.placed += len(report.placed)
        self.stats.cancelled += len(report.cancelled)
        self.stats.unchanged += len(report.unchanged)
        self.stats.replayed += len(report.replayed)
        self.stats.approved += len(report.placed)
        self.stats.denied += len(report.denied)
        for _, reason in report.denied:
            self.stats.denials[reason] = self.stats.denials.get(reason, 0) + 1
        for label, n in (("post_only_rejected", len(report.post_only_rejected)),
                         ("rejected", len(report.rejected)),
                         ("skipped_crossed", len(report.skipped_crossed))):
            if n:
                self.stats.denials[label] = self.stats.denials.get(label, 0) + n

    # ------------------------------------------------------------- persistence
    def _record_decisions(self, sleeve_id: str,
                          desired: DesiredState) -> dict[str, int]:
        """Persist EVERY model probability, acted on or not (PLAN.md 6.3).

        Returns ticker -> decision id, so the orders that follow can be joined
        back to the reasoning that produced them.
        """
        if not desired.decisions:
            return {}
        ids: dict[str, int] = {}
        ts = now_us()
        with self.db.tx() as c:
            for d in desired.decisions:
                cur = c.execute(
                    """INSERT INTO decisions
                       (decided_at_us, sleeve_id, venue, ticker, category,
                        market_price, p_model, raw_edge, shrunk_edge, acted,
                        preregistration_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (ts, sleeve_id, Venue.KALSHI.value, d.ticker, d.category,
                     d.market_price, d.p_model, d.raw_edge, d.shrunk_edge,
                     int(d.acted), None),
                )
                if cur.lastrowid is not None:
                    ids[d.ticker] = int(cur.lastrowid)
        return ids

    def _link_decisions(self, placed: tuple[str, ...],
                        decision_ids: dict[str, int]) -> None:
        """Join each new order to the decision that caused it.

        Without this, decisions match outcomes on (venue, ticker) alone, which
        is wrong the moment one ticker is quoted twice -- and per-decision fee
        and slippage attribution is impossible at any size.
        """
        if not placed or not decision_ids:
            return
        rows = []
        for coid in placed:
            rec = self.oms.get(coid)
            if rec is None:
                continue
            did = decision_ids.get(rec.ticker)
            if did is not None:
                rows.append((did, coid))
        if rows:
            with self.db.tx() as c:
                c.executemany(
                    "UPDATE orders SET decision_id = ? WHERE client_order_id = ?",
                    rows,
                )

    # ------------------------------------------------------------------- loop
    def run(self, *, interval: float | None, once: bool = False) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        print(f"[runner] mode={self.mode.value} sleeves={[s.id for s in self.sleeves]}",
              flush=True)
        while not self._stop:
            if self.executor._kill.is_engaged():
                print("[runner] KILL present -- halting", flush=True)
                break
            t0 = time.monotonic()
            try:
                self.cycle()
                print(f"[runner] cycle in {time.monotonic()-t0:.2f}s | "
                      f"{self.stats.report()}", flush=True)
                if self.stats.denials:
                    print(f"[runner] denials: {self.stats.denials}", flush=True)
            except KalshiError as exc:
                self.stats.errors += 1
                print(f"[runner] API: {exc}", flush=True)
            except Exception as exc:
                self.stats.errors += 1
                print(f"[runner] {type(exc).__name__}: {exc}", flush=True)
            if once or interval is None:
                break
            slept = 0.0
            while (slept < interval and not self._stop
                   and not self.executor._kill.is_engaged()):
                time.sleep(min(1.0, interval - slept))
                slept += 1.0
        print(f"[runner] stopped. {self.stats.report()}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="prediction market trading loop")
    ap.add_argument("--db", default="data/pm.db")
    ap.add_argument("--mode", default="shadow",
                    choices=[m.value for m in RunMode])
    ap.add_argument("--bankroll", type=float, default=10_000.0, help="dollars")
    ap.add_argument("--interval", type=float, default=None)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--run-dir", default=".", help="where the KILL file lives")
    ap.add_argument(
        "--sleeves", default="S2,S3",
        help="comma-separated sleeve ids.  DEFAULT IS THE ARBITRAGE PAIR: S2 "
             "(within-event mutually-exclusive basket) and S3 (across linked "
             "events).  S1 is a market-making sleeve, not an arbitrage one, and "
             "is opt-in.",
    )
    args = ap.parse_args(argv)

    mode = RunMode(args.mode)
    if mode is RunMode.LIVE:
        print("[runner] LIVE mode requires a sleeve at Gate 4 and an explicit "
              "compliance sign-off (PLAN.md G0/G4). Refusing to start.", flush=True)
        return 2

    settings = load_settings()
    wanted = [x.strip().upper() for x in args.sleeves.split(",") if x.strip()]
    with Database(args.db) as db:
        sleeves: list[Sleeve] = []
        for sid in wanted:
            match sid:
                case "S1":
                    s1 = S1Structural()
                    # I7: only markets whose rules have been reviewed may be
                    # traded.  In shadow we admit everything already recorded,
                    # since no capital is at risk.
                    for r in db.conn.execute(
                        "SELECT DISTINCT rules_hash FROM market_snapshots "
                        "WHERE rules_hash IS NOT NULL AND rules_hash != ''"
                    ):
                        s1.reviewed_rules.add(r["rules_hash"])
                    sleeves.append(s1)
                case "S2":
                    # Within-event: rest asks across a mutually-exclusive outcome
                    # set and collect the overround.  Selling is the safe
                    # direction -- liability is capped at $1 whether or not the
                    # listed outcomes are exhaustive, and non-exhaustiveness
                    # makes it BETTER.  Buying the basket needs a human verdict.
                    sleeves.append(S2ShortBasket())
                case "S3":
                    # Across events: trade provable inconsistencies between
                    # logically linked markets -- threshold ladders where
                    # P(X > 45) <= P(X > 40) must hold, duplicate events, and
                    # same-underlying series.
                    sleeves.append(S3LinkedRV())
                case _:
                    print(f"[runner] unknown sleeve {sid!r}", flush=True)
                    return 2
        if not sleeves:
            print("[runner] no sleeves selected", flush=True)
            return 2
        runner = Runner(db=db, settings=settings, sleeves=sleeves, mode=mode,
                        bankroll_cents=int(args.bankroll * 100),
                        run_dir=args.run_dir)
        runner.run(interval=args.interval, once=args.once)
        print(f"[runner] db: {db.counts()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
