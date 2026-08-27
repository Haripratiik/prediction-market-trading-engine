"""The leakage suite.  PLAN.md 6.7 checklist + R11a, T-032.

R11a is the whole point of this file:

    The leakage suite must contain at least one deliberately-cheating strategy
    that the harness is required to catch.  A backtester that cannot detect
    look-ahead when it is present is not evidence of anything.

`LookAheadSleeve` is that strategy: it reads the `settlements` table -- the
future -- and quotes only the markets it already knows resolve YES.  Three
independent detectors fire on it, and they fire for different reasons on
purpose, because a single detector is a single point of failure:

  1. PURITY      strategy/base.py C4.2a says `desired_state` is a pure function.
                 A pure function performs no I/O, so any SQL executed while it
                 runs is proof of look-ahead regardless of what it read.
  2. TRUNCATION  re-run the same decisions against a database with everything
                 after the decision cut deleted.  An honest sleeve produces a
                 byte-identical order stream; a sleeve reading the future
                 cannot.
  3. CALIBRATION realized wins minus price-implied wins, in standard deviations.
                 Catches a cheater that baked the answer in at construction time
                 and therefore never touches the database at all.

THE OTHER FIVE CHECKS are PLAN.md 6.7's checklist: point-in-time metadata only,
signal timestamps respected, voided and delisted markets included, no
resolved-price granularity artifacts, and purging plus embargo.

PURGE AND EMBARGO ARE NOT OPTIONAL (PLAN.md 6.7, research/08 section 6.4)
------------------------------------------------------------------------
In prediction markets label overlap is STRUCTURAL: a market opened at t does not
settle until t+H, so neighbouring observations share most of their outcome.
`purge_embargo_experiment` reproduces the published finding on a fixture whose
true out-of-sample AUC is exactly 0.500 by construction -- see that function's
docstring for the measured numbers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

import numpy as np

from backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from backtest.fills import RestingOrder
from core.db import Database
from core.models import Side, Venue
from strategy.base import DesiredQuote, DesiredState, MarketSnapshot, Sleeve

# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LeakageFinding:
    check: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.check}: {self.detail}"


@dataclass(frozen=True, slots=True)
class LeakageReport:
    findings: tuple[LeakageFinding, ...]

    @property
    def passed(self) -> bool:
        """G2's `leakage_checklist_all_pass`.  Any failure fails the run."""
        return all(f.passed for f in self.findings)

    @property
    def failures(self) -> tuple[LeakageFinding, ...]:
        return tuple(f for f in self.findings if not f.passed)

    def failed_checks(self) -> frozenset[str]:
        return frozenset(f.check for f in self.failures)

    def report(self) -> str:
        head = "LEAKAGE SUITE: " + ("ALL PASS" if self.passed else
                                    f"{len(self.failures)} FAILURE(S)")
        return "\n".join([head, *(str(f) for f in self.findings)])


# --------------------------------------------------------------------------- #
# The deliberately-cheating strategy.  R11a.
# --------------------------------------------------------------------------- #


@dataclass
class LookAheadSleeve:
    """DELIBERATELY CHEATS.  Never promote, never run outside this suite.

    It queries `settlements` -- data that did not exist at decision time -- and
    quotes only the markets that resolve YES.  Its backtest looks spectacular,
    which is exactly the point: the harness must fail it anyway.
    """

    db: Database
    id: str = "CHEAT"
    gate: int = 0
    size: int = 20

    def desired_state(self, snapshot: MarketSnapshot) -> DesiredState:
        quotes: list[DesiredQuote] = []
        for m in snapshot.markets:
            if not m.has_two_sided_quote or m.yes_bid is None:
                continue
            row = self.db.conn.execute(
                "SELECT outcome, voided FROM settlements WHERE ticker = ?",
                (m.ticker,),
            ).fetchone()
            if row is None or row["voided"] or row["outcome"] != 1:
                continue
            quotes.append(DesiredQuote(
                ticker=m.ticker, side=Side.YES, price_cents=m.yes_bid,
                size=self.size, post_only=True,
                rationale={"sleeve": self.id, "cheat": "read settlements.outcome"},
            ))
        return DesiredState(quotes=tuple(quotes), decisions=(),
                            rationale={"cheat": True})


def look_ahead_factory(db: Database) -> LookAheadSleeve:
    return LookAheadSleeve(db=db)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def order_digest(orders: Sequence[RestingOrder]) -> str:
    """Fingerprint of the SIGNAL, independent of how generously we filled it."""
    blob = json.dumps(
        [[o.order_id, o.ticker, o.side.value, o.price_cents, o.size, o.placed_at_us]
         for o in orders],
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _truncated_copy(db: Database, cut_us: int) -> Database:
    """An in-memory clone with everything after `cut_us` deleted.

    The clone is what the world actually looked like at `cut_us`.  Any decision
    that differs between the two databases used information that did not exist.
    """
    clone = Database(":memory:")
    db.conn.backup(clone.conn)
    with clone.tx() as c:
        c.execute("DELETE FROM market_snapshots WHERE observed_at_us > ?", (cut_us,))
        c.execute("DELETE FROM event_snapshots  WHERE observed_at_us > ?", (cut_us,))
        c.execute("DELETE FROM trades           WHERE traded_at_us  > ?", (cut_us,))
        c.execute("DELETE FROM settlements      WHERE settled_at_us > ?", (cut_us,))
    return clone


# --------------------------------------------------------------------------- #
# Check 1 -- point-in-time metadata only
# --------------------------------------------------------------------------- #


def check_point_in_time(db: Database, sleeve: Sleeve,
                        cfg: BacktestConfig) -> LeakageFinding:
    """R5a.  Every market read is `latest_market(as_of_us=t)`, never a later row.

    Implemented by spying on the accessor rather than by reading the engine's
    source, because the property that matters is what it DOES at run time.  A
    run that never calls the accessor at all also fails: an engine that
    hand-rolls its own SQL is one nobody can audit.
    """
    engine = BacktestEngine(db=db, sleeve=sleeve, cfg=cfg)
    calls: list[tuple[int | None, int | None]] = []
    original = db.latest_market

    def spy(ticker: str, *, as_of_us: int | None = None,
            venue: str = Venue.KALSHI.value) -> sqlite3.Row | None:
        row = original(ticker, as_of_us=as_of_us, venue=venue)
        calls.append((as_of_us, None if row is None else int(row["observed_at_us"])))
        return row

    db.latest_market = spy                      # type: ignore[method-assign]
    violations: list[str] = []
    total = 0
    try:
        for snap in engine.snapshots():
            for as_of, observed in calls:
                total += 1
                if as_of is None:
                    violations.append("latest_market called without as_of_us")
                elif as_of != snap.now_us:
                    violations.append(
                        f"as_of_us {as_of} != decision time {snap.now_us}")
                elif observed is not None and observed > as_of:
                    violations.append(
                        f"row observed at {observed} returned for as_of {as_of}")
            calls.clear()
    finally:
        del db.latest_market                    # type: ignore[attr-defined]

    if violations:
        return LeakageFinding("point_in_time", False,
                              f"{len(violations)} violation(s): {violations[0]}")
    reader_tickers = engine.reader.tickers()
    if reader_tickers and total == 0:
        return LeakageFinding(
            "point_in_time", False,
            f"{len(reader_tickers)} tickers recorded but latest_market was never "
            f"called -- the engine is not reading point-in-time",
        )
    return LeakageFinding("point_in_time", True,
                          f"{total} point-in-time reads, none from the future")


# --------------------------------------------------------------------------- #
# Check 2 -- signal timestamps respected
# --------------------------------------------------------------------------- #


def _holds_a_database(sleeve: Sleeve) -> bool:
    """Does this sleeve carry a Database of its own?

    Detected structurally rather than by type name so a wrapper or subclass
    cannot slip past it.
    """
    for value in vars(sleeve).values() if hasattr(sleeve, "__dict__") else ():
        if isinstance(value, Database):
            return True
    for slot in getattr(type(sleeve), "__slots__", ()) or ():
        if isinstance(getattr(sleeve, slot, None), Database):
            return True
    return False


def check_signal_timestamps(
    db: Database,
    sleeve: Sleeve,
    cfg: BacktestConfig,
    *,
    sleeve_factory: Callable[[Database], Sleeve] | None = None,
) -> LeakageFinding:
    """Truncation invariance: decisions up to t must not move when t+ is deleted.

    `sleeve_factory` matters.  A sleeve that captured a Database handle at
    construction keeps reading the ORIGINAL database unless it is rebuilt
    against the truncated one, and would pass a test it should fail.  Pass the
    factory whenever the sleeve holds external state.

    Omitting it used to SILENTLY DISARM this check: the deliberately-cheating
    `LookAheadSleeve` PASSED when `sleeve_factory` was left off, because it kept
    reading the untruncated handle.  A forgotten keyword argument was the only
    thing standing between a look-ahead strategy and a G2 promotion, and the
    result reported PASS rather than "not checked".  A safety check that can be
    turned off by forgetting an argument is not a safety check, so a sleeve
    holding its own database handle without a factory is now a FAILURE.
    """
    if sleeve_factory is None and _holds_a_database(sleeve):
        return LeakageFinding(
            "signal_timestamps", False,
            "sleeve holds its own Database handle and no sleeve_factory was "
            "given, so truncation cannot be tested -- it would read the "
            "untruncated database and pass regardless.  Pass sleeve_factory.",
        )
    timeline = BacktestEngine(db=db, sleeve=sleeve, cfg=cfg).reader.timeline(cfg)
    if len(timeline) < 2:
        return LeakageFinding("signal_timestamps", True,
                              "fewer than 2 decision points; nothing to compare")
    cut = timeline[len(timeline) // 2]
    cut_cfg = replace(cfg, end_us=cut)

    full = order_digest(BacktestEngine(db=db, sleeve=sleeve, cfg=cut_cfg).orders())
    clone = _truncated_copy(db, cut)
    try:
        truncated_sleeve = sleeve_factory(clone) if sleeve_factory else sleeve
        truncated = order_digest(
            BacktestEngine(db=clone, sleeve=truncated_sleeve, cfg=cut_cfg).orders()
        )
    finally:
        clone.close()

    if full != truncated:
        return LeakageFinding(
            "signal_timestamps", False,
            f"order stream up to {cut} changes when data after {cut} is deleted "
            f"({full[:12]} vs {truncated[:12]}) -- the sleeve is reading the future",
        )
    return LeakageFinding("signal_timestamps", True,
                          f"order stream up to {cut} is invariant to future data")


# --------------------------------------------------------------------------- #
# Check 3 -- voided and delisted markets included
# --------------------------------------------------------------------------- #


def check_voided_included(db: Database, cfg: BacktestConfig) -> LeakageFinding:
    """Survivorship is look-ahead wearing a different hat.

    A universe assembled from the markets that are still around today has quietly
    conditioned on surviving, and every statistic computed on it is conditioned
    the same way.
    """
    engine = BacktestEngine(db=db, sleeve=_NullSleeve(), cfg=cfg)
    universe = frozenset(engine.reader.tickers())
    rows = db.conn.execute(
        """SELECT DISTINCT ticker FROM settlements WHERE venue = ? AND voided = 1
           UNION
           SELECT DISTINCT ticker FROM market_snapshots
           WHERE venue = ? AND status IN ('voided', 'delisted')
           ORDER BY ticker""",
        (cfg.venue, cfg.venue),
    ).fetchall()
    expected = [r["ticker"] for r in rows]
    recorded = frozenset(
        r["ticker"] for r in db.conn.execute(
            "SELECT DISTINCT ticker FROM market_snapshots WHERE venue = ?", (cfg.venue,)
        ).fetchall()
    )
    missing = [t for t in expected if t in recorded and t not in universe]
    if not cfg.include_voided:
        return LeakageFinding(
            "voided_included", False,
            "BacktestConfig.include_voided is False: voided and delisted markets "
            "are being dropped from the universe",
        )
    if missing:
        return LeakageFinding(
            "voided_included", False,
            f"{len(missing)} voided/delisted market(s) recorded but absent from the "
            f"replay universe, e.g. {missing[0]}",
        )
    return LeakageFinding("voided_included", True,
                          f"{len(expected)} voided/delisted market(s) present in the universe")


# --------------------------------------------------------------------------- #
# Check 4 -- resolved-price granularity artifacts
# --------------------------------------------------------------------------- #


def check_resolved_price_artifacts(db: Database, cfg: BacktestConfig) -> LeakageFinding:
    """A quote pinned at the outcome is not a quote, it is the answer.

    Two ways this gets into an archive: rows recorded after settlement and left
    in the replay window, and rows whose timestamp granularity (hour or day
    buckets) places a post-resolution price BEFORE the recorded settlement
    instant.  Either one hands a strategy a 99c bid on a market that already
    resolved YES.
    """
    lo = cfg.start_us if cfg.start_us is not None else -(1 << 62)
    hi = cfg.end_us if cfg.end_us is not None else (1 << 62)
    rows = db.conn.execute(
        """SELECT ms.ticker, ms.observed_at_us, ms.yes_bid, ms.yes_ask,
                  s.settled_at_us, s.outcome
           FROM market_snapshots ms
           JOIN settlements s ON s.venue = ms.venue AND s.ticker = ms.ticker
           WHERE ms.venue = ? AND s.voided = 0
             AND ms.observed_at_us >= ? AND ms.observed_at_us <= ?
             AND ms.yes_bid IS NOT NULL AND ms.yes_ask IS NOT NULL
             AND ((s.outcome = 1 AND ms.yes_bid >= 99)
               OR (s.outcome = 0 AND ms.yes_ask <= 1))
           ORDER BY ms.ticker, ms.observed_at_us""",
        (cfg.venue, lo, hi),
    ).fetchall()
    if rows:
        after = sum(1 for r in rows if r["observed_at_us"] >= r["settled_at_us"])
        first = rows[0]
        return LeakageFinding(
            "resolved_price_artifacts", False,
            f"{len(rows)} snapshot(s) inside the replay window are pinned at the "
            f"settled outcome ({after} at or after settlement), e.g. {first['ticker']} "
            f"at {first['observed_at_us']} bid={first['yes_bid']} ask={first['yes_ask']} "
            f"outcome={first['outcome']}",
        )
    return LeakageFinding("resolved_price_artifacts", True,
                          "no quotes pinned at the settled outcome inside the window")


# --------------------------------------------------------------------------- #
# Check 5 -- the series cache is not point-in-time
# --------------------------------------------------------------------------- #


def check_series_cache_as_of(db: Database, cfg: BacktestConfig) -> LeakageFinding:
    """`series_cache` is upserted, not appended (core/db.py), so it carries
    TODAY's fee regime.  Replaying an older tape with it is a small, real
    look-ahead: fee_type and fee_multiplier do change.

    The fix when this fires is a FeeSchedule era pinning the regime that was
    actually in force (backtest/engine.py), not deleting the check.
    """
    if cfg.end_us is None:
        return LeakageFinding("series_cache_as_of", True,
                              "open-ended window; series cache is current by definition")
    row = db.conn.execute(
        "SELECT COUNT(*) AS n FROM series_cache WHERE observed_at_us > ?", (cfg.end_us,)
    ).fetchone()
    stale = int(row["n"]) if row else 0
    if stale and not cfg.fees.eras:
        return LeakageFinding(
            "series_cache_as_of", False,
            f"{stale} series row(s) were observed after the replay window ends "
            f"({cfg.end_us}) and no FeeSchedule era pins the regime in force",
        )
    return LeakageFinding("series_cache_as_of", True,
                          "fee specs are as-of the replay window or pinned by an era")


# --------------------------------------------------------------------------- #
# Check 6 -- sleeve purity (catches the cheater)
# --------------------------------------------------------------------------- #


def check_sleeve_purity(db: Database, sleeve: Sleeve,
                        cfg: BacktestConfig, *, max_steps: int = 3) -> LeakageFinding:
    """C4.2a: `desired_state` is pure.  So it must do no I/O and must repeat.

    SQLite hands us the perfect probe: `set_trace_callback` fires on every
    statement executed on a connection.  Arm it only while the sleeve runs and
    any query it issues is caught, whatever table it touched.  Then call the
    sleeve twice on the same snapshot -- a clock read or an unseeded random draw
    shows up as a differing DesiredState.
    """
    engine = BacktestEngine(db=db, sleeve=sleeve, cfg=cfg)
    statements: list[str] = []
    checked = 0
    for snap in engine.snapshots():
        if checked >= max_steps:
            break
        checked += 1
        db.conn.set_trace_callback(statements.append)
        try:
            first = sleeve.desired_state(snap)
        finally:
            db.conn.set_trace_callback(None)
        if statements:
            return LeakageFinding(
                "sleeve_purity", False,
                f"desired_state executed {len(statements)} SQL statement(s) -- it is "
                f"not a pure function (C4.2a). First: {statements[0][:120]!r}",
            )
        if sleeve.desired_state(snap) != first:
            return LeakageFinding(
                "sleeve_purity", False,
                "desired_state returned two different results for one snapshot -- it "
                "reads a clock or an unseeded source (C4.2a)",
            )
    if checked == 0:
        return LeakageFinding("sleeve_purity", True, "no decision points to probe")
    return LeakageFinding("sleeve_purity", True,
                          f"{checked} decision point(s): no I/O, repeatable")


# --------------------------------------------------------------------------- #
# Check 7 -- performance no honest strategy reaches
# --------------------------------------------------------------------------- #


def check_impossible_performance(
    result: BacktestResult, *, min_settlements: int = 10, max_z: float = 4.0
) -> LeakageFinding:
    """Realized wins minus price-implied wins, in standard deviations.

    Deliberately a backstop rather than the primary detector: it is the only
    check that still fires when the cheat was baked in at construction time and
    the strategy never queries anything.  Reads the PESSIMISTIC column (R6.7a),
    because that is the one a gate would act on.
    """
    r = result.pessimistic
    if r.settlements < min_settlements:
        return LeakageFinding(
            "impossible_performance", True,
            f"{r.settlements} settlement(s) < {min_settlements}; not yet testable",
        )
    z = r.calibration_z
    if z > max_z:
        hit = r.actual_wins / r.settlements
        implied = r.implied_wins / r.settlements
        return LeakageFinding(
            "impossible_performance", False,
            f"realized hit rate {hit:.3f} vs price-implied {implied:.3f} over "
            f"{r.settlements} settlements is {z:.1f} sd -- no honest sleeve reaches "
            f"this, and pessimistic fills make it worse, not better",
        )
    return LeakageFinding("impossible_performance", True,
                          f"calibration z = {z:.2f} over {r.settlements} settlements")


# --------------------------------------------------------------------------- #
# Check 8 -- purging and embargoing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LabelSpan:
    """When an observation's LABEL is decided, not when the observation was made.

    In prediction markets this span is the whole life of the market: you trade at
    t and learn the answer at settlement.  Overlapping spans are why naive CV
    leaks (PLAN.md 6.7).
    """

    start_us: int
    end_us: int                       # exclusive


def purge_embargo_mask(
    spans: Sequence[LabelSpan],
    test_idx: Sequence[int],
    *,
    embargo_us: int = 0,
    embargo_before_us: int = 0,
) -> np.ndarray:
    """Training mask for one test block.  PLAN.md 6.7.

    Purge: drop any training observation whose label span intersects the test
    block's label span.  Embargo: drop a further `embargo_us` of training
    observations starting after the test label span ends.

    `embargo_before_us` defaults to 0 because PLAN.md specifies the embargo as
    `~0.01*T` AFTER the test set.  It is exposed because in a K-fold layout
    training data sits on BOTH sides of the test block, and when settlement times
    are uncertain the leak is symmetric -- see `purge_embargo_experiment`, which
    measures how much the one-sided version leaves behind.
    """
    n = len(spans)
    mask = np.ones(n, dtype=bool)
    if not len(test_idx):
        return mask
    starts = np.fromiter((s.start_us for s in spans), dtype=np.int64, count=n)
    ends = np.fromiter((s.end_us for s in spans), dtype=np.int64, count=n)

    test = np.asarray(test_idx, dtype=np.int64)
    mask[test] = False
    t_lo = int(starts[test].min())
    t_hi = int(ends[test].max())

    mask &= ~((starts < t_hi) & (ends > t_lo))                      # purge
    if embargo_us > 0:
        mask &= ~((starts >= t_hi) & (starts <= t_hi + embargo_us))  # embargo after
    if embargo_before_us > 0:
        mask &= ~((ends <= t_lo) & (ends >= t_lo - embargo_before_us))
    return mask


def contiguous_blocks(n: int, block: int) -> list[np.ndarray]:
    """Contiguous test blocks.  Random K-fold on overlapping labels is the
    failure mode this whole section exists to prevent."""
    return [np.arange(lo, min(lo + block, n))
            for lo in range(0, n - block + 1, block)]


def combinatorial_purged_splits(
    n: int, *, n_groups: int = 6, n_test_groups: int = 2
) -> list[np.ndarray]:
    """CPCV test sets.  PLAN.md 6.7 prefers this to walk-forward because it
    yields a DISTRIBUTION of backtest outcomes rather than one high-variance
    path.  Group boundaries are contiguous in time; combinations of groups are
    the test sets."""
    from itertools import combinations

    edges = np.linspace(0, n, n_groups + 1).astype(int)
    groups = [np.arange(edges[i], edges[i + 1]) for i in range(n_groups)]
    return [np.concatenate([groups[i] for i in combo])
            for combo in combinations(range(n_groups), n_test_groups)]


def roc_auc(y: np.ndarray, score: np.ndarray) -> float:
    """Mann-Whitney AUC with ties at 0.5.  Ties matter here: an abstaining
    predictor scores exactly 0.5, which is the honest answer."""
    y = np.asarray(y)
    score = np.asarray(score, dtype=float)
    n1 = int(y.sum())
    n0 = int(len(y) - n1)
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = np.argsort(score, kind="mergesort")
    sorted_scores = score[order]
    ranks_sorted = np.empty(len(score), dtype=float)
    i = 0
    while i < len(sorted_scores):
        j = i
        while j + 1 < len(sorted_scores) and sorted_scores[j + 1] == sorted_scores[i]:
            j += 1
        ranks_sorted[i:j + 1] = (i + j) / 2.0 + 1.0
        i = j + 1
    ranks = np.empty(len(score), dtype=float)
    ranks[order] = ranks_sorted
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


@dataclass(frozen=True, slots=True)
class PurgeEmbargoResult:
    """Measured AUC of a predictor whose TRUE out-of-sample AUC is 0.500."""

    naive: float
    purged: float
    purged_embargoed: float             # PLAN.md's one-sided embargo, after only
    purged_embargoed_symmetric: float   # embargo on both sides of the test block
    horizon: int
    embargo: int
    reps: int
    n: int
    block: int


def purge_embargo_experiment(
    *,
    n: int = 1200,
    horizon: int = 25,
    block: int = 200,
    settlement_lag: int = 50,
    embargo: int | None = None,
    reps: int = 200,
    radius: int = 100,
    seed0: int = 0,
) -> PurgeEmbargoResult:
    """Reproduce the verified purge/embargo finding on a fixture with NO signal.

    CONSTRUCTION.  Observation i is made at time i.  Its label is
    `1{sum of iid noise over [i, i + H + lag_i) > 0}`, where `lag_i` is a
    settlement lag: a prediction market's declared close is not the instant it
    settles, and you only know the DECLARED horizon when you purge.  The
    predictor is time-only -- the label of the nearest training observation in
    time, abstaining beyond `radius`.  With iid noise and disjoint label spans
    that predictor is worthless, so its true out-of-sample AUC is 0.500 exactly.

    MEASURED (n=1200, H=25, block=200, lag<=2H, 200 seeds, this machine):

        naive contiguous CV                     0.585
        + purge on declared label spans         0.529
        + 2H embargo after the test block       0.509   <- PLAN.md's rule
        + 2H embargo on both sides              0.496

    which reproduces the published 0.573 / 0.505 / 0.500 pattern from
    research/08 section 6.4.  Two things to take from it.  First, naive CV
    manufactures an edge of ~8.5 AUC points out of pure noise, which is more
    than any real edge in this project.  Second, PLAN.md's one-sided embargo
    leaves a residual whenever training data sits on BOTH sides of the test
    block, because uncertain settlement times leak in both directions -- so the
    symmetric embargo is the one to use under K-fold, and the one-sided rule is
    the floor, not the ceiling.
    """
    emb = embargo if embargo is not None else 2 * horizon
    names = ("naive", "purged", "purged_embargoed", "purged_embargoed_symmetric")
    configs = {
        "naive": (False, 0, 0),
        "purged": (True, 0, 0),
        "purged_embargoed": (True, emb, 0),
        "purged_embargoed_symmetric": (True, emb, emb),
    }
    totals: dict[str, float] = dict.fromkeys(names, 0.0)
    counts: dict[str, int] = dict.fromkeys(names, 0)

    for rep in range(reps):
        rng = np.random.default_rng(seed0 + rep)
        noise = rng.standard_normal(n + horizon + settlement_lag + 2)
        lag = (rng.integers(0, settlement_lag + 1, size=n) if settlement_lag
               else np.zeros(n, dtype=np.int64))
        cumulative = np.concatenate([[0.0], np.cumsum(noise)])
        idx = np.arange(n)
        true_end = idx + horizon + lag
        y = ((cumulative[true_end] - cumulative[idx]) > 0).astype(int)
        # what you can purge on: the DECLARED horizon, not the realised one
        declared = [LabelSpan(int(i), int(i + horizon)) for i in idx]

        for name in names:
            purge, emb_after, emb_before = configs[name]
            preds: list[np.ndarray] = []
            truths: list[np.ndarray] = []
            for test in contiguous_blocks(n, block):
                if purge:
                    train = purge_embargo_mask(
                        declared, test, embargo_us=emb_after,
                        embargo_before_us=emb_before,
                    )
                else:
                    train = np.ones(n, dtype=bool)
                    train[test] = False
                tr = np.flatnonzero(train)
                if not len(tr):
                    continue
                k = np.searchsorted(tr, test)
                left = tr[np.clip(k - 1, 0, len(tr) - 1)]
                right = tr[np.clip(k, 0, len(tr) - 1)]
                nb = np.where(np.abs(test - left) <= np.abs(test - right), left, right)
                dist = np.abs(test - nb)
                preds.append(np.where(dist <= radius, y[nb].astype(float), 0.5))
                truths.append(y[test])
            if not preds:
                continue
            auc = roc_auc(np.concatenate(truths), np.concatenate(preds))
            if not np.isnan(auc):
                totals[name] += auc
                counts[name] += 1

    def mean(name: str) -> float:
        return totals[name] / counts[name] if counts[name] else float("nan")

    return PurgeEmbargoResult(
        naive=mean("naive"),
        purged=mean("purged"),
        purged_embargoed=mean("purged_embargoed"),
        purged_embargoed_symmetric=mean("purged_embargoed_symmetric"),
        horizon=horizon, embargo=emb, reps=reps, n=n, block=block,
    )


def check_purge_embargo(
    *, reps: int = 120, tolerance: float = 0.02, spurious_floor: float = 0.53
) -> LeakageFinding:
    """Fails if the purge/embargo machinery does not remove a signal that is not
    there.  Runs on a synthetic fixture, so it is a test of the MACHINERY -- the
    thing every model in this repo will be cross-validated with."""
    r = purge_embargo_experiment(reps=reps)
    problems: list[str] = []
    if r.naive < spurious_floor:
        problems.append(
            f"naive CV AUC {r.naive:.3f} < {spurious_floor:.2f}: the fixture no longer "
            f"produces the spurious signal the check is supposed to remove"
        )
    if abs(r.purged_embargoed_symmetric - 0.5) > tolerance:
        problems.append(
            f"purge + symmetric embargo AUC {r.purged_embargoed_symmetric:.3f} is not "
            f"0.500 +- {tolerance}: leakage survives the remedy"
        )
    if r.purged > r.naive + 1e-9:
        problems.append(f"purging made it worse: {r.purged:.3f} > {r.naive:.3f}")
    detail = (f"naive {r.naive:.3f} -> purged {r.purged:.3f} -> +{r.embargo} embargo "
              f"{r.purged_embargoed:.3f} -> symmetric {r.purged_embargoed_symmetric:.3f} "
              f"(H={r.horizon}, n={r.n}, {r.reps} seeds)")
    if problems:
        return LeakageFinding("purge_embargo", False, "; ".join(problems) + " | " + detail)
    return LeakageFinding("purge_embargo", True, detail)


# --------------------------------------------------------------------------- #
# The suite
# --------------------------------------------------------------------------- #


@dataclass
class _NullSleeve:
    """Quotes nothing.  Used by checks that only need the reader."""

    id: str = "NULL"
    gate: int = 0

    def desired_state(self, snapshot: MarketSnapshot) -> DesiredState:
        return DesiredState(rationale={"null": True})


LEAKAGE_CHECKS: tuple[str, ...] = (
    "point_in_time",
    "signal_timestamps",
    "voided_included",
    "resolved_price_artifacts",
    "series_cache_as_of",
    "sleeve_purity",
    "impossible_performance",
    "purge_embargo",
)


def run_leakage_suite(
    db: Database,
    sleeve: Sleeve,
    cfg: BacktestConfig | None = None,
    *,
    sleeve_factory: Callable[[Database], Sleeve] | None = None,
    purge_reps: int = 120,
    result: BacktestResult | None = None,
) -> LeakageReport:
    """PLAN.md 6.7 leakage checklist + R11a.  G2 requires every check to pass."""
    cfg = cfg or BacktestConfig()
    if result is None:
        result = BacktestEngine(db=db, sleeve=sleeve, cfg=cfg).run()
    findings = (
        check_point_in_time(db, sleeve, cfg),
        check_signal_timestamps(db, sleeve, cfg, sleeve_factory=sleeve_factory),
        check_voided_included(db, cfg),
        check_resolved_price_artifacts(db, cfg),
        check_series_cache_as_of(db, cfg),
        check_sleeve_purity(db, sleeve, cfg),
        check_impossible_performance(result),
        check_purge_embargo(reps=purge_reps),
    )
    return LeakageReport(findings)
