"""KPIs.  PLAN.md section 12, implemented in the order that section ranks them.

The ordering is the whole point.  P&L is not on the list at all, because P&L is
the slowest-converging statistic available: at the sizes in section 9 a real edge
takes hundreds of settlements to separate from noise in dollars, but only tens to
show up in Brier skill.  So the metrics are ranked by how fast they tell you the
truth, and the top three are all measurements of forecast quality and adverse
selection rather than money.

Every function here is PURE over a `Database` -- no clocks, no I/O beyond the
read, no mutation.  That makes the digest reproducible: running it twice on the
same database gives the same numbers, which is the only way an operator can trust
a number that says "halt this sleeve".

Two conventions inherited from the rest of the repo:

  * prices are YES-referenced.  A decision's `market_price` is P(YES); an order's
    `price_cents` is the YES price even when `side` is NO (shadow/engine.py).
  * only TERMINAL fills count (PLAN.md R5b / I4).  Polymarket's MATCHED can later
    FAIL, so a non-terminal fill is a claim, not a fact.

Sample-size guards return `None`, never a default.  A KPI that reports 0.0 on an
empty database is indistinguishable from one reporting a genuinely zero edge, and
that ambiguity is exactly how a dead sleeve stays live.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
from scipy.stats import norm  # type: ignore[import-untyped]

from core.config import CapacityLimits
from core.db import Database
from core.math.stats import (
    brier_score,
    e_to_p,
    log_e_beta_binomial,
    wilson_interval,
)
from core.math.stats import brier_skill_vs_market as _brier_skill
from core.models import Side
from shadow.engine import FillModel, ShadowOrder, fill_rate, markout

# --------------------------------------------------------------------------- #
# Thresholds that PLAN.md fixes as KPI targets rather than as risk limits.
# Anything in config/risk.yaml is read from RiskConfig instead (PLAN.md 0.3).
# --------------------------------------------------------------------------- #
LAMBDA_HALT_BELOW: Final[float] = 0.30          # PLAN.md 12 KPI 5 / R2.3a
LAMBDA_MIN_SETTLEMENTS: Final[int] = 100        # R2.3a: fit only once >= 100 exist
TARGET_ORPHAN_LOSS_RATIO: Final[float] = 0.20   # PLAN.md 12 KPI 6
DEFAULT_ALPHA: Final[float] = 0.05

# PLAN.md 12 KPI 3 names +5m/+1h; the short end is added because adverse selection
# on a 1c-tick book shows up in the first seconds (research/07).  Healthy is
# positive at 1s decaying to a positive plateau (shadow/engine.py markout).
DEFAULT_MARKOUT_HORIZONS_US: Final[tuple[int, ...]] = (
    1_000_000,        # 1s
    5_000_000,        # 5s
    60_000_000,       # 60s
    300_000_000,      # 5m
    1_800_000_000,    # 30m
)

# Reconstructing shadow fills replays the trade tape per order, so the digest
# samples the most recent orders rather than the whole history.
DEFAULT_SHADOW_SAMPLE: Final[int] = 2_000


# --------------------------------------------------------------------------- #
# Shared plumbing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SettledDecision:
    """One emitted model probability whose market has since settled."""

    decided_at_us: int
    ticker: str
    market_price: float
    p_model: float
    outcome: int
    acted: bool

    @property
    def long_yes(self) -> bool:
        """Which side the disagreement points at.  Ties resolve to YES."""
        return self.p_model >= self.market_price

    @property
    def cost(self) -> float:
        """Price-implied probability of the side we would take, in dollars."""
        return self.market_price if self.long_yes else 1.0 - self.market_price

    @property
    def won(self) -> bool:
        return (self.outcome == 1) if self.long_yes else (self.outcome == 0)


def _table_exists(db: Database, name: str) -> bool:
    row = db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def settled_decisions(db: Database, sleeve_id: str, *,
                      acted_only: bool = False,
                      one_per_market: bool = True) -> list[SettledDecision]:
    """Decisions joined to the outcome, with the look-ahead guard applied.

    `decided_at_us <= settled_at_us` is not paranoia: a sleeve restarted against a
    stale universe can emit a "forecast" for a market that already resolved, and
    scoring that would manufacture skill out of nothing (I6).

    Un-acted decisions are kept by default.  They are the whole reason the table
    records them (PLAN.md 6.3): scoring only the trades you took measures your
    execution filter, not your model.

    THE INDEPENDENCE UNIT IS THE MARKET.  A quoting loop re-evaluates the same
    ticker every cycle, so one settled market generates many decision rows that
    all resolve to the SAME outcome.  Scoring them as independent draws does not
    merely inflate `n` -- it compounds.  Measured on the real database before
    this guard: 470 decisions over 69 settled markets (6.8 rows each), which the
    e-process turned into `E = 124,326` against a threshold of 20 and a reported
    win rate of 0.775 against a price-implied 0.502.  That reads as overwhelming
    proof of edge and is an artifact of counting one coin flip seven times.

    Keeping the EARLIEST decision per market is the honest choice: it is the
    forecast made from the least information, and every later revision is
    conditioned on the earlier one rather than being a fresh draw.
    """
    dedupe = ""
    if one_per_market:
        # ROW_NUMBER needs SQLite >= 3.25; the repo already requires 3.35+ for
        # RETURNING elsewhere, so this is safe.
        dedupe = "AND d.id = (SELECT d2.id FROM decisions d2 " \
                 "WHERE d2.ticker = d.ticker AND d2.sleeve_id = d.sleeve_id " \
                 "ORDER BY d2.decided_at_us, d2.id LIMIT 1)"
    sql = f"""
        SELECT d.decided_at_us, d.ticker, d.market_price, d.p_model, d.acted,
               s.outcome
        FROM decisions d
        JOIN settlements s
          ON s.ticker = d.ticker
         AND (d.venue IS NULL OR s.venue = d.venue)
        WHERE d.sleeve_id = ?
          AND s.voided = 0
          AND d.decided_at_us <= s.settled_at_us
          {dedupe}
    """
    if acted_only:
        sql += " AND d.acted = 1"
    sql += " ORDER BY d.decided_at_us"
    return [
        SettledDecision(
            decided_at_us=int(r["decided_at_us"]),
            ticker=str(r["ticker"] or ""),
            market_price=float(r["market_price"]),
            p_model=float(r["p_model"]),
            outcome=int(r["outcome"]),
            acted=bool(r["acted"]),
        )
        for r in db.conn.execute(sql, (sleeve_id,)).fetchall()
    ]


def sleeve_ids(db: Database) -> list[str]:
    """Every sleeve that has left a trace, from decisions or from orders."""
    rows = db.conn.execute(
        """SELECT DISTINCT sleeve_id FROM decisions
           UNION
           SELECT DISTINCT sleeve_id FROM orders
           ORDER BY 1"""
    ).fetchall()
    return [str(r[0]) for r in rows if r[0]]


def realized_fee_per_contract(db: Database, sleeve_id: str) -> float:
    """Dollars of fee per filled contract, from the fill ledger.

    Modelling the fee instead would require the series fee spec, which the
    `decisions` table does not carry -- and the modelled number would be zero for
    13,353 of 13,486 series anyway (makers pay nothing).  The ledger is the only
    honest source, and I4 says the ledger is the truth.  `fee_cents` is signed, so
    a rebating venue returns a negative number and correctly *raises* net edge.
    """
    row = db.conn.execute(
        """SELECT COALESCE(SUM(f.fee_cents), 0) AS fee,
                  COALESCE(SUM(f.size), 0)      AS sz
           FROM fills f
           JOIN orders o ON o.client_order_id = f.client_order_id
           WHERE o.sleeve_id = ? AND f.terminal = 1""",
        (sleeve_id,),
    ).fetchone()
    size = float(row["sz"] or 0.0)
    if size <= 0.0:
        return 0.0
    return float(row["fee"]) / size / 100.0


# --------------------------------------------------------------------------- #
# KPI 1 -- Brier skill vs market.  THE primary metric.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BrierSkill:
    n: int
    skill: float | None
    brier_model: float | None
    brier_market: float | None

    @property
    def beats_market(self) -> bool:
        return self.skill is not None and self.skill > 0.0


def brier_skill_vs_market(db: Database, sleeve_id: str, *,
                          acted_only: bool = False) -> BrierSkill:
    """`1 - BS_model / BS_market` over settled decisions.  PLAN.md 12 KPI 1.

    Ranked first because it converges far faster than P&L and because its sign is
    a sufficient condition to stop: a sleeve whose model scores WORSE than the
    price it is trading against has negative expected edge no matter what its P&L
    happens to say, since the price is a freely available competing forecast.
    """
    rows = settled_decisions(db, sleeve_id, acted_only=acted_only)
    if not rows:
        return BrierSkill(0, None, None, None)

    model = [d.p_model for d in rows]
    market = [d.market_price for d in rows]
    outcomes = [d.outcome for d in rows]
    bs_model = brier_score(model, outcomes)
    bs_market = brier_score(market, outcomes)
    try:
        skill = _brier_skill(model, market, outcomes)
    except ValueError:
        # BS_market == 0: the price called every settled market exactly right.
        # The ratio is undefined; the briers still are not, so report those.
        skill = None
    return BrierSkill(len(rows), skill, bs_model, bs_market)


# --------------------------------------------------------------------------- #
# KPI 2 -- Net edge per settlement, with the interval that actually decides.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class NetEdge:
    n: int
    wins: int
    win_rate: float | None
    price_implied: float | None
    fee_per_contract: float
    net_edge: float | None
    ci_low: float | None
    ci_high: float | None
    alpha: float

    @property
    def excludes_zero(self) -> bool:
        """The gate condition.  PLAN.md 12 KPI 2: the CI decides, not the point."""
        return self.ci_low is not None and self.ci_low > 0.0

    @property
    def ci_width(self) -> float | None:
        if self.ci_low is None or self.ci_high is None:
            return None
        return self.ci_high - self.ci_low


def net_edge_with_ci(db: Database, sleeve_id: str, *,
                     alpha: float = DEFAULT_ALPHA) -> NetEdge:
    """Realized win rate - price-implied - fees, with a Wilson interval.

    PLAN.md 12 KPI 2.  Only ACTED decisions count: this is the realized-edge test
    statistic, and you do not realize edge on a trade you declined.

    The interval is Wilson on the win rate, shifted by the mean cost and the
    realized fee.  Treating those two as known constants understates the width
    slightly -- both are themselves estimates -- but the win-rate term dominates
    by an order of magnitude, and Wilson is the interval PLAN.md names.

    Wilson rather than normal-approximation because the win rates that matter are
    often near 0 or 1 (favourite-longshot territory), where the Wald interval
    happily runs outside [0,1) and its coverage collapses.
    """
    rows = settled_decisions(db, sleeve_id, acted_only=True)
    fee = realized_fee_per_contract(db, sleeve_id)
    if not rows:
        return NetEdge(0, 0, None, None, fee, None, None, None, alpha)

    n = len(rows)
    wins = sum(1 for d in rows if d.won)
    win_rate = wins / n
    price_implied = sum(d.cost for d in rows) / n
    offset = price_implied + fee
    lo, hi = wilson_interval(wins, n, alpha=alpha)
    return NetEdge(
        n=n,
        wins=wins,
        win_rate=win_rate,
        price_implied=price_implied,
        fee_per_contract=fee,
        net_edge=win_rate - offset,
        ci_low=lo - offset,
        ci_high=hi - offset,
        alpha=alpha,
    )


# --------------------------------------------------------------------------- #
# KPI 3 -- Mark-outs.  Direct measurement of adverse selection.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Markout:
    horizon_us: int
    n: int
    mean_cents: float | None
    n_unobserved: int

    @property
    def horizon_s(self) -> float:
        return self.horizon_us / 1_000_000.0


def markouts(db: Database, sleeve_id: str,
             horizons_us: tuple[int, ...] = DEFAULT_MARKOUT_HORIZONS_US,
             *, max_staleness_us: int | None = None) -> dict[int, Markout]:
    """Mean signed fair-price move after our fills, per horizon.  PLAN.md 12 KPI 3.

    This is `mu * L` measured instead of assumed, and it is the earliest warning a
    maker gets: fill likelihood correlates NEGATIVELY with post-fill returns
    (PLAN.md 3.4), so a quote that is filling well is the one to be suspicious of.
    Positive at 1s but negative at the long horizon means the spread capture is
    being handed back to informed flow.

    `max_staleness_us` is how far past its own horizon a reference quote may be.
    `shadow.engine.markout` now enforces a per-horizon default (half the horizon),
    so leaving this None is already safe; pass a value only to widen or tighten it
    deliberately.  The bound matters because against a sparse recorder every
    horizon below the sweep interval otherwise resolves to the SAME snapshot and
    the five columns silently become one number repeated.
    """
    # TWO CONVENTIONS MEET HERE.  `orders.price_cents` is YES-referenced;
    # `fills.price_cents` is SIDE-referenced, because `OMS.position` and the
    # runner's capital arithmetic both read it that way.  `shadow.engine.markout`
    # is YES-referenced by construction, so a NO fill MUST be converted before
    # it is handed over -- otherwise every NO fill's mark-out is wrong by
    # (100 - 2p), which is the entire signal at any price away from 50c.
    fills = db.conn.execute(
        """SELECT f.filled_at_us, o.ticker, o.side,
                  CASE WHEN o.side = 'no' THEN 100 - f.price_cents
                       ELSE f.price_cents END AS price_cents
           FROM fills f
           JOIN orders o ON o.client_order_id = f.client_order_id
           WHERE o.sleeve_id = ? AND f.terminal = 1
           ORDER BY f.filled_at_us""",
        (sleeve_id,),
    ).fetchall()

    out: dict[int, Markout] = {}
    for h in horizons_us:
        values: list[float] = []
        missing = 0
        for f in fills:
            # Pass the budget DOWN rather than pre-checking it here: a pre-check
            # can only ever reject more than `markout` does, never widen what it
            # accepts, so an explicitly-widened budget was silently ignored.
            mo = markout(
                db,
                str(f["ticker"]),
                int(f["filled_at_us"]),
                int(f["price_cents"]),
                Side(str(f["side"])),
                horizon_us=h,
                max_staleness_us=max_staleness_us,
            )
            if mo is None:
                missing += 1
            else:
                values.append(mo)
        out[h] = Markout(
            horizon_us=h,
            n=len(values),
            mean_cents=(sum(values) / len(values)) if values else None,
            n_unobserved=missing,
        )
    return out


def _has_snapshot_within(db: Database, ticker: str, at_us: int, tolerance_us: int) -> bool:
    row = db.conn.execute(
        """SELECT observed_at_us FROM market_snapshots
           WHERE ticker = ? AND observed_at_us >= ?
           ORDER BY observed_at_us LIMIT 1""",
        (ticker, at_us),
    ).fetchone()
    return row is not None and (int(row["observed_at_us"]) - at_us) <= tolerance_us


# --------------------------------------------------------------------------- #
# KPI 4 -- Fill quality.  Detects the drift that rots backtests.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FillQuality:
    live_orders: int
    live_filled: int
    live_fill_rate: float | None
    shadow_orders: int
    shadow_fill_rate_pessimistic: float | None
    shadow_fill_rate_optimistic: float | None
    ratio: float | None
    maker_share: float | None
    taker_slippage_cents: float | None

    @property
    def within_bracket(self) -> bool:
        """R6.7d: the honest claim is a bracket, never a point.

        Live landing inside [pessimistic, optimistic] means the fill model has not
        drifted; landing below the pessimistic bound means it has, badly.
        """
        if self.live_fill_rate is None or self.shadow_fill_rate_pessimistic is None:
            return False
        hi = self.shadow_fill_rate_optimistic
        return (self.live_fill_rate >= self.shadow_fill_rate_pessimistic
                and (hi is None or self.live_fill_rate <= hi))


def fill_quality(db: Database, sleeve_id: str, *,
                 sample: int = DEFAULT_SHADOW_SAMPLE) -> FillQuality:
    """Live fill rate against the shadow prediction.  PLAN.md 12 KPI 4.

    The shadow side is RECOMPUTED from the recorded tape rather than read off the
    orders table, because `ShadowExecutor` writes every order as `open` and never
    revisits it -- shadow fills only exist as a counterfactual.

    Both bounds are reported.  R6.7a: promotion reads the pessimistic column only;
    the optimistic one exists to bound the uncertainty, which is ~3.9x wide
    (R6.7d), not to flatter the sleeve.
    """
    live = db.conn.execute(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN EXISTS (
                        SELECT 1 FROM fills f
                        WHERE f.client_order_id = o.client_order_id AND f.terminal = 1
                      ) THEN 1 ELSE 0 END) AS filled
           FROM orders o
           WHERE o.sleeve_id = ? AND o.mode IN ('live', 'paper')""",
        (sleeve_id,),
    ).fetchone()
    live_n = int(live["n"] or 0)
    live_filled = int(live["filled"] or 0)
    live_rate = (live_filled / live_n) if live_n else None

    shadow_orders = _shadow_orders(db, sleeve_id, limit=sample)
    pess: float | None = None
    opt: float | None = None
    if shadow_orders:
        pess = float(fill_rate(db, shadow_orders,
                               model=FillModel.PESSIMISTIC)["fill_rate"])
        opt = float(fill_rate(db, shadow_orders,
                              model=FillModel.OPTIMISTIC)["fill_rate"])

    ratio = (live_rate / pess) if (live_rate is not None and pess) else None

    return FillQuality(
        live_orders=live_n,
        live_filled=live_filled,
        live_fill_rate=live_rate,
        shadow_orders=len(shadow_orders),
        shadow_fill_rate_pessimistic=pess,
        shadow_fill_rate_optimistic=opt,
        ratio=ratio,
        maker_share=_maker_share(db, sleeve_id),
        taker_slippage_cents=_taker_slippage(db, sleeve_id),
    )


def _shadow_orders(db: Database, sleeve_id: str, *, limit: int) -> list[ShadowOrder]:
    """Rebuild ShadowOrders from persisted rows.

    `queue_ahead` and the book state were stashed in `rationale_json` at submit
    time; without them the fill model would have to assume an empty queue, which
    is the optimistic bound wearing the pessimistic label.
    """
    rows = db.conn.execute(
        """SELECT client_order_id, created_at_us, ticker, side, price_cents, size,
                  rationale_json
           FROM orders
           WHERE sleeve_id = ? AND mode = 'shadow'
           ORDER BY created_at_us DESC LIMIT ?""",
        (sleeve_id, int(limit)),
    ).fetchall()

    out: list[ShadowOrder] = []
    for r in rows:
        try:
            rationale = json.loads(r["rationale_json"] or "{}")
        except (TypeError, ValueError):
            rationale = {}
        if not isinstance(rationale, dict):
            rationale = {}
        try:
            side = Side(str(r["side"]))
        except ValueError:
            continue
        out.append(
            ShadowOrder(
                client_order_id=str(r["client_order_id"]),
                sleeve_id=sleeve_id,
                ticker=str(r["ticker"]),
                side=side,
                price_cents=int(r["price_cents"]),
                size=int(r["size"]),
                decided_at_us=int(r["created_at_us"]),
                queue_ahead=float(rationale.get("queue_ahead") or 0.0),
                book_bid=rationale.get("book_bid"),
                book_ask=rationale.get("book_ask"),
                rationale=rationale,
            )
        )
    return out


def _maker_share(db: Database, sleeve_id: str) -> float | None:
    """Fraction of filled contracts taken as maker.  I1 says this should be ~1."""
    row = db.conn.execute(
        """SELECT COALESCE(SUM(f.size), 0) AS total,
                  COALESCE(SUM(CASE WHEN f.is_maker = 1 THEN f.size ELSE 0 END), 0) AS maker
           FROM fills f
           JOIN orders o ON o.client_order_id = f.client_order_id
           WHERE o.sleeve_id = ? AND f.terminal = 1""",
        (sleeve_id,),
    ).fetchone()
    total = float(row["total"] or 0.0)
    if total <= 0.0:
        return None
    return float(row["maker"]) / total


def _taker_slippage(db: Database, sleeve_id: str) -> float | None:
    """Mean ADVERSE slippage in cents on taker fills, versus the order's limit.

    Prices are YES-referenced throughout, so the sign flips for a NO order exactly
    as it does in `shadow.engine.markout`: buying NO at a HIGHER yes-price means
    paying LESS for the NO contract.  Positive = we paid worse than we asked.
    """
    # `fills.price_cents` is SIDE-referenced and `orders.price_cents` is
    # YES-referenced (see `markouts`), so differencing them raw is wrong by
    # (100 - 2p) on every NO fill.  Convert the fill to the order's reference
    # BEFORE subtracting.
    rows = db.conn.execute(
        """SELECT CASE WHEN o.side = 'no' THEN 100 - f.price_cents
                       ELSE f.price_cents END AS fill_px,
                  f.size AS sz, o.price_cents AS limit_px, o.side AS side
           FROM fills f
           JOIN orders o ON o.client_order_id = f.client_order_id
           WHERE o.sleeve_id = ? AND f.terminal = 1 AND f.is_maker = 0""",
        (sleeve_id,),
    ).fetchall()
    if not rows:
        return None
    weight = 0.0
    total = 0.0
    for r in rows:
        sz = float(r["sz"] or 0.0)
        if sz <= 0.0:
            continue
        diff = float(r["fill_px"]) - float(r["limit_px"])
        adverse = diff if str(r["side"]) == Side.YES.value else -diff
        total += adverse * sz
        weight += sz
    if weight <= 0.0:
        return None
    return total / weight


# --------------------------------------------------------------------------- #
# KPI 5 -- lambda_hat.  Realized shrinkage, estimated rather than guessed.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LambdaHat:
    n: int
    beta: float | None
    se: float | None
    ci_low: float | None
    ci_high: float | None
    intercept: float | None
    sufficient: bool
    halt: bool
    note: str = ""

    @property
    def credibly_positive(self) -> bool:
        """R2.3a: a category whose beta is not credibly above 0 leaves the universe."""
        return self.ci_low is not None and self.ci_low > 0.0


def lambda_hat(db: Database, sleeve_id: str, *,
               acted_only: bool = False,
               min_n: int = LAMBDA_MIN_SETTLEMENTS,
               alpha: float = DEFAULT_ALPHA) -> LambdaHat:
    """Forecast-encompassing regression in LOGIT space.  PLAN.md 2.3b / 12 KPI 5.

        logit P(y=1) = logit(m) + alpha + beta * ( logit(q) - logit(m) )

    beta is the lambda of I2, measured instead of assumed.  beta = 0 means the
    disagreement with the price is pure noise; beta = 1 means the model is right
    and the market is wrong; beta = 0.5 means literally "my edge is half what I
    think".  Trusting the model at face value (beta = 1 when the truth is ~0.45)
    destroys 95% of achievable log-growth, which is why this is a halt condition
    and not a report line.

    The regression is run in logit space with `logit(m)` as an OFFSET rather than
    as a free regressor.  That is what makes beta an encompassing coefficient --
    the question asked is "how much of your deviation from the price survives?",
    not "how well does your probability correlate with outcomes?", and a model
    that merely copies the price would score well on the latter.

    Fitted by Newton-Raphson.  Two guards, both hit in practice: a sleeve that
    never disagrees with the price gives a degenerate design matrix, and a small
    perfectly-separated sample sends beta to infinity.
    """
    rows = settled_decisions(db, sleeve_id, acted_only=acted_only)
    n = len(rows)
    if n == 0:
        return LambdaHat(0, None, None, None, None, None, False, False, "no settled decisions")

    m = np.array([d.market_price for d in rows], dtype=float)
    q = np.array([d.p_model for d in rows], dtype=float)
    y = np.array([float(d.outcome) for d in rows], dtype=float)

    fit = _fit_encompassing(m, q, y)
    sufficient = n >= min_n
    if fit is None:
        return LambdaHat(n, None, None, None, None, None, sufficient, False,
                         "degenerate design: no usable disagreement with the price")

    beta, se, intercept = fit
    # Wald interval.  Fine here: this is a 2-parameter fit and R2.3a only asks it
    # once >= 100 settlements exist, well past where the normal approximation bites.
    z = float(norm.ppf(1.0 - alpha / 2.0))
    lo = beta - z * se if math.isfinite(se) else None
    hi = beta + z * se if math.isfinite(se) else None
    # Only halt on evidence.  A beta of 0.1 from nine settlements is noise, and
    # halting on it would make the KPI a random sleeve-killer.
    halt = sufficient and beta < LAMBDA_HALT_BELOW
    return LambdaHat(n, beta, se, lo, hi, intercept, sufficient, halt,
                     "" if sufficient else f"below the {min_n}-settlement floor (R2.3a)")


def _logit(p: np.ndarray, *, clip: float = 1e-6) -> np.ndarray:
    """Clip before the logit.  A 0 or 1 forecast is otherwise an infinite row."""
    pc = np.clip(p, clip, 1.0 - clip)
    return np.log(pc / (1.0 - pc))


def _fit_encompassing(m: np.ndarray, q: np.ndarray, y: np.ndarray, *,
                      max_iter: int = 60,
                      tol: float = 1e-10) -> tuple[float, float, float] | None:
    """Newton-Raphson for the offset logistic model.  Returns (beta, se, intercept)."""
    offset = _logit(m)
    x = _logit(q) - offset
    if float(np.std(x)) < 1e-9:
        return None                       # the sleeve never disagrees with the price

    design = np.column_stack([np.ones_like(x), x])
    b = np.zeros(2, dtype=float)
    hess = np.eye(2)
    for _ in range(max_iter):
        eta = offset + design @ b
        mu = 1.0 / (1.0 + np.exp(-np.clip(eta, -700.0, 700.0)))
        w = np.clip(mu * (1.0 - mu), 1e-12, None)
        grad = design.T @ (y - mu)
        hess = design.T @ (design * w[:, None])
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            return None
        # Cap the step: under separation the unconstrained Newton step overflows
        # long before the loop notices it has not converged.
        norm_step = float(np.linalg.norm(step))
        if not math.isfinite(norm_step):
            return None
        if norm_step > 4.0:
            step = step * (4.0 / norm_step)
        b = b + step
        if float(np.max(np.abs(step))) < tol:
            break

    if not np.all(np.isfinite(b)) or abs(float(b[1])) > 50.0:
        return None
    try:
        cov = np.linalg.inv(hess)
    except np.linalg.LinAlgError:
        return None
    var = float(cov[1, 1])
    se = math.sqrt(var) if var > 0.0 else math.inf
    return float(b[1]), se, float(b[0])


# --------------------------------------------------------------------------- #
# KPI 6 -- Orphan-leg loss ratio (S2/S3).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class OrphanLoss:
    available: bool
    structures: int
    orphaned: int
    orphan_loss_cents: float
    gross_margin_cents: float
    ratio: float | None
    target: float = TARGET_ORPHAN_LOSS_RATIO

    @property
    def breaches_target(self) -> bool:
        return self.ratio is not None and self.ratio >= self.target


def orphan_loss_ratio(db: Database, sleeve_id: str) -> OrphanLoss:
    """Orphan losses over gross structure margin.  PLAN.md 12 KPI 6, target < 0.20.

    The only real risk in the relative-value sleeves: a Dutch book with one leg
    filled is not an arbitrage, it is a naked directional position that nobody
    sized.  The denominator is the gross margin the structures were SUPPOSED to
    earn, so the ratio answers "what share of the edge does leg risk eat?".

    `structures` is specified in PLAN.md section 5 but is not in the DDL that
    `core/db.py` currently creates, so absence is reported rather than raised --
    the digest must survive a database built before S2/S3 exist.
    """
    if not _table_exists(db, "structures"):
        return OrphanLoss(False, 0, 0, 0.0, 0.0, None)

    row = db.conn.execute(
        """SELECT COUNT(*) AS n,
                  COALESCE(SUM(CASE WHEN state = 'orphaned' THEN 1 ELSE 0 END), 0) AS orphaned,
                  COALESCE(SUM(CASE WHEN state = 'orphaned'
                                     AND COALESCE(realized_margin_cents, 0) < 0
                                    THEN -realized_margin_cents ELSE 0 END), 0) AS loss,
                  COALESCE(SUM(CASE WHEN target_margin_cents > 0
                                    THEN target_margin_cents ELSE 0 END), 0) AS gross
           FROM structures WHERE sleeve_id = ?""",
        (sleeve_id,),
    ).fetchone()

    gross = float(row["gross"] or 0.0)
    loss = float(row["loss"] or 0.0)
    return OrphanLoss(
        available=True,
        structures=int(row["n"] or 0),
        orphaned=int(row["orphaned"] or 0),
        orphan_loss_cents=loss,
        gross_margin_cents=gross,
        ratio=(loss / gross) if gross > 0.0 else None,
    )


# --------------------------------------------------------------------------- #
# KPI 7 -- Non-edge income.  The floor return.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class NonEdgeIncome:
    rebate_cents: float
    reward_cents: float
    interest_cents: float
    total_cents: float
    capital_cents: int
    per_unit_capital: float | None


def non_edge_income(db: Database, *, capital_cents: int = 0,
                    reward_cents: float = 0.0,
                    interest_cents: float = 0.0) -> NonEdgeIncome:
    """Rebates + rewards + interest per unit of capital.  PLAN.md 12 KPI 7.

    The consolation metric: it is the return you earn while the edge statistics
    are still accumulating, and it is the one line of the digest that is real
    money from the first day.

    Only the rebate term is measurable from this schema -- `fee_cents` is signed,
    so rebates are the negative part of the fill ledger.  Rewards (S6 liquidity
    programmes) and interest on collateral have no table in `core/db.py`, so they
    are passed in by the caller rather than silently reported as zero.
    """
    row = db.conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN fee_cents < 0 THEN -fee_cents ELSE 0 END), 0) AS rebate
           FROM fills WHERE terminal = 1"""
    ).fetchone()
    rebate = float(row["rebate"] or 0.0)
    total = rebate + reward_cents + interest_cents
    return NonEdgeIncome(
        rebate_cents=rebate,
        reward_cents=reward_cents,
        interest_cents=interest_cents,
        total_cents=total,
        capital_cents=capital_cents,
        per_unit_capital=(total / capital_cents) if capital_cents > 0 else None,
    )


# --------------------------------------------------------------------------- #
# KPI 8 -- Capacity utilization.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Capacity:
    n: int
    mean_utilization: float | None
    max_utilization: float | None
    taker_volume_share: float | None
    freeze_at: float
    freeze: bool


def capacity_utilization(db: Database, sleeve_id: str, *,
                         cfg: CapacityLimits | None = None) -> Capacity:
    """Resting size over touch depth, and taker volume over market volume.

    PLAN.md 12 KPI 8; the freeze threshold comes from `CapacityLimits`, because
    section 9 says config/risk.yaml is the only place a limit may be defined.

    Depth is read from the last snapshot AT OR BEFORE the order was created -- the
    point-in-time rule (R5a).  Using the current snapshot would score a past order
    against a book that did not exist yet.

    Side matters: a resting YES bid competes with `yes_bid_size`, while a resting
    NO bid sits on the YES ask (a taker buying YES at our price fills it), so its
    queue is `yes_ask_size`.
    """
    limits = cfg or CapacityLimits()
    rows = db.conn.execute(
        """SELECT o.ticker, o.side, o.size, o.created_at_us,
                  (SELECT CASE WHEN o.side = 'yes' THEN ms.yes_bid_size ELSE ms.yes_ask_size END
                     FROM market_snapshots ms
                    WHERE ms.ticker = o.ticker AND ms.observed_at_us <= o.created_at_us
                    ORDER BY ms.observed_at_us DESC LIMIT 1) AS depth
           FROM orders o
           WHERE o.sleeve_id = ?""",
        (sleeve_id,),
    ).fetchall()

    utils = [
        float(r["size"]) / float(r["depth"])
        for r in rows
        if r["depth"] is not None and float(r["depth"]) > 0.0
    ]
    mean_u = (sum(utils) / len(utils)) if utils else None
    max_u = max(utils) if utils else None
    return Capacity(
        n=len(utils),
        mean_utilization=mean_u,
        max_utilization=max_u,
        taker_volume_share=_taker_volume_share(db, sleeve_id),
        freeze_at=limits.freeze_at_utilization,
        freeze=(mean_u is not None and mean_u >= limits.freeze_at_utilization),
    )


def _taker_volume_share(db: Database, sleeve_id: str) -> float | None:
    """Our taken volume as a share of the market's, per PLAN.md 12 KPI 8."""
    taken = db.conn.execute(
        """SELECT COALESCE(SUM(f.size), 0) AS sz
           FROM fills f
           JOIN orders o ON o.client_order_id = f.client_order_id
           WHERE o.sleeve_id = ? AND f.terminal = 1 AND f.is_maker = 0""",
        (sleeve_id,),
    ).fetchone()
    size = float(taken["sz"] or 0.0)
    if size <= 0.0:
        return None
    # MAX rather than the latest row: volume is cumulative, so they agree, and MAX
    # survives a sweep that happened to record the field as NULL or 0.
    market = db.conn.execute(
        """SELECT COALESCE(SUM(v), 0) AS total FROM (
               SELECT (SELECT MAX(ms.volume) FROM market_snapshots ms
                        WHERE ms.ticker = o.ticker) AS v
               FROM (SELECT DISTINCT ticker FROM orders WHERE sleeve_id = ?) o
           )""",
        (sleeve_id,),
    ).fetchone()
    total = float(market["total"] or 0.0)
    if total <= 0.0:
        return None
    return size / total


# --------------------------------------------------------------------------- #
# The anytime-valid edge test.  PLAN.md R2.5b.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class EProcess:
    n: int
    successes: int
    p0: float | None
    log_e: float | None
    e_value: float | None
    threshold: float
    reject: bool
    p_value: float | None


def e_process_status(db: Database, sleeve_id: str, *,
                     alpha: float = DEFAULT_ALPHA) -> EProcess:
    """Anytime-valid test of H0: no edge over the price, net of fees.

    The null rate is the mean cost of the side taken plus the realized fee -- i.e.
    break-even.  Reject when the e-value ever exceeds 1/alpha; by Ville's
    inequality that is a level-alpha test at EVERY stopping time, so looking at
    this number daily costs nothing.

    That property is the entire reason it exists.  Naive continuous monitoring
    does not converge to any error rate: measured at 40,000 reps, P(ever falsely
    reject) is 0.363 by 100 observations and 0.739 by 100,000, climbing toward 1.0
    by the law of the iterated logarithm.  A daily digest built on a fixed-sample
    p-value would therefore promote a sleeve with no edge, eventually, always.
    """
    rows = settled_decisions(db, sleeve_id, acted_only=True)
    threshold = 1.0 / alpha
    if not rows:
        return EProcess(0, 0, None, None, None, threshold, False, None)

    n = len(rows)
    wins = sum(1 for d in rows if d.won)
    fee = realized_fee_per_contract(db, sleeve_id)
    p0 = sum(d.cost for d in rows) / n + fee
    # The e-value takes a scalar null.  The mean break-even rate is the right
    # scalar here because every row is a Bernoulli trial against its own price;
    # clipping keeps a degenerate 0c/100c book from making the log undefined.
    p0 = min(max(p0, 1e-6), 1.0 - 1e-6)

    log_e = log_e_beta_binomial(wins, n, p0)
    e_value = math.inf if log_e > 709.0 else math.exp(log_e)
    return EProcess(
        n=n,
        successes=wins,
        p0=p0,
        log_e=log_e,
        e_value=e_value,
        threshold=threshold,
        reject=e_value >= threshold,
        p_value=e_to_p(e_value) if e_value > 0.0 else 1.0,
    )


# --------------------------------------------------------------------------- #
# The whole section-12 table for one sleeve.
# --------------------------------------------------------------------------- #
def sleeve_report(db: Database, sleeve_id: str, *,
                  capital_cents: int = 0,
                  markout_horizons_us: tuple[int, ...] = DEFAULT_MARKOUT_HORIZONS_US,
                  alpha: float = DEFAULT_ALPHA) -> dict[str, Any]:
    """Every section-12 KPI for one sleeve, in section-12 order.

    Reviews are triggered by sample size (every 150 settlements) and by events,
    never by the calendar (I10) -- this function is the payload of such a review,
    so it deliberately carries the sample size of every statistic alongside it.
    """
    return {
        "sleeve_id": sleeve_id,
        "brier_skill": brier_skill_vs_market(db, sleeve_id),
        "net_edge": net_edge_with_ci(db, sleeve_id, alpha=alpha),
        "markouts": markouts(db, sleeve_id, markout_horizons_us),
        "fill_quality": fill_quality(db, sleeve_id),
        "lambda_hat": lambda_hat(db, sleeve_id, alpha=alpha),
        "orphan_loss": orphan_loss_ratio(db, sleeve_id),
        "non_edge_income": non_edge_income(db, capital_cents=capital_cents),
        "capacity": capacity_utilization(db, sleeve_id),
        "e_process": e_process_status(db, sleeve_id, alpha=alpha),
    }
