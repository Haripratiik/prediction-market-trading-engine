"""Daily digest.  PLAN.md 6.6 and 10.1 step 1.

    python -m monitor.main
    python -m monitor.main --db data/pm.db --sleeve S1 --capital 100000
    python -m monitor.main --json

Prints every section-12 KPI per sleeve plus the database counts.  It is read by a
human once a day and by nothing else, so it optimises for one thing: making an
absent sample size impossible to mistake for a good number.  `--` means no data;
a number means a number.

The digest is a REPORT, never an actuator.  It flags `halt`, `freeze` and
`reject` conditions but changes nothing -- the risk engine enforces limits (I3)
and a human acknowledges drift (6.6).  It also works on a completely empty
database, because the first time anyone runs it, that is what they will have.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from typing import Any

from core.config import load_settings
from core.db import Database
from monitor.alerts import WebhookAlerter
from monitor.kpi import (
    DEFAULT_MARKOUT_HORIZONS_US,
    LAMBDA_HALT_BELOW,
    TARGET_ORPHAN_LOSS_RATIO,
    sleeve_ids,
    sleeve_report,
)

WIDTH = 78


# --------------------------------------------------------------------------- #
# Formatting helpers.  `None` prints as `--`, never as 0.
# --------------------------------------------------------------------------- #
def fmt(value: float | None, *, places: int = 4, pct: bool = False,
        sign: bool = False) -> str:
    if value is None:
        return "--"
    if value != value or value in (float("inf"), float("-inf")):
        return str(value)
    if pct:
        return f"{value:{'+' if sign else ''}.{places}%}"
    return f"{value:{'+' if sign else ''}.{places}f}"


def _flag(condition: bool, text: str) -> str:
    return f"  <<< {text}" if condition else ""


def _rule(title: str) -> str:
    return f"{title} " + "-" * max(0, WIDTH - len(title) - 1)


def render_sleeve(report: dict[str, Any]) -> str:
    """The section-12 table for one sleeve, in section-12 order."""
    lines: list[str] = [_rule(f"sleeve {report['sleeve_id']} ")]

    bs = report["brier_skill"]
    lines.append(
        f"  1 brier skill vs market  {fmt(bs.skill, sign=True)}  "
        f"(model {fmt(bs.brier_model)} / market {fmt(bs.brier_market)}, n={bs.n})"
        + _flag(bs.n > 0 and not bs.beats_market, "model loses to the price")
    )

    ne = report["net_edge"]
    lines.append(
        f"  2 net edge / settlement  {fmt(ne.net_edge, sign=True)}  "
        f"CI[{fmt(ne.ci_low, sign=True)}, {fmt(ne.ci_high, sign=True)}] "
        f"n={ne.n} wins={ne.wins}"
    )
    lines.append(
        f"      win {fmt(ne.win_rate)}  price-implied {fmt(ne.price_implied)}  "
        f"fee/contract {fmt(ne.fee_per_contract)}"
        + _flag(ne.n > 0 and not ne.excludes_zero, "CI includes zero: not proven")
    )

    lines.append("  3 mark-outs (cents)")
    for mo in sorted(report["markouts"].values(), key=lambda m: m.horizon_us):
        lines.append(
            f"      +{mo.horizon_s:>7.1f}s  {fmt(mo.mean_cents, places=3, sign=True)}"
            f"  n={mo.n} unobserved={mo.n_unobserved}"
            + _flag(mo.mean_cents is not None and mo.mean_cents < 0,
                    "adverse selection")
        )

    fq = report["fill_quality"]
    lines.append(
        f"  4 fill quality           live {fmt(fq.live_fill_rate)} "
        f"({fq.live_filled}/{fq.live_orders})  shadow "
        f"[{fmt(fq.shadow_fill_rate_pessimistic)}, "
        f"{fmt(fq.shadow_fill_rate_optimistic)}] n={fq.shadow_orders}"
    )
    lines.append(
        f"      live/pessimistic {fmt(fq.ratio, places=2)}  "
        f"maker share {fmt(fq.maker_share)}  "
        f"taker slippage {fmt(fq.taker_slippage_cents, places=2, sign=True)}c"
    )

    lh = report["lambda_hat"]
    se = f" +-{fmt(lh.se)}" if lh.se is not None else ""
    lines.append(
        f"  5 lambda_hat             {fmt(lh.beta, sign=True)}{se}"
        f"  CI[{fmt(lh.ci_low, sign=True)}, {fmt(lh.ci_high, sign=True)}]"
        f"  n={lh.n}"
        + _flag(lh.halt, f"HALT: below {LAMBDA_HALT_BELOW} (R2.3a)")
    )
    if lh.note:
        lines.append(f"      {lh.note}")

    ol = report["orphan_loss"]
    if not ol.available:
        lines.append("  6 orphan loss ratio      -- (no `structures` table yet)")
    else:
        lines.append(
            f"  6 orphan loss ratio      {fmt(ol.ratio)}  "
            f"({ol.orphaned}/{ol.structures} orphaned, "
            f"loss {ol.orphan_loss_cents/100:.2f} / margin {ol.gross_margin_cents/100:.2f})"
            + _flag(ol.breaches_target, f"above target {TARGET_ORPHAN_LOSS_RATIO}")
        )

    ni = report["non_edge_income"]
    lines.append(
        f"  7 non-edge income        {fmt(ni.per_unit_capital)} per unit capital  "
        f"(rebates {ni.rebate_cents/100:.2f}, rewards {ni.reward_cents/100:.2f}, "
        f"interest {ni.interest_cents/100:.2f})"
    )

    cap = report["capacity"]
    lines.append(
        f"  8 capacity utilization   mean {fmt(cap.mean_utilization)}  "
        f"max {fmt(cap.max_utilization)}  taker share "
        f"{fmt(cap.taker_volume_share)}  n={cap.n}"
        + _flag(cap.freeze, f"FREEZE at {cap.freeze_at}")
    )

    ep = report["e_process"]
    lines.append(
        f"  * e-process (anytime)    E={fmt(ep.e_value, places=3)} vs "
        f"{ep.threshold:.0f}  p<={fmt(ep.p_value, places=4)}  "
        f"H0: win rate <= {fmt(ep.p0)}  n={ep.n}"
        + _flag(ep.reject, "EDGE PROVEN at this alpha")
    )
    return "\n".join(lines)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def build_digest(db: Database, *, sleeves: list[str] | None = None,
                 capital_cents: int = 0,
                 horizons_us: tuple[int, ...] = DEFAULT_MARKOUT_HORIZONS_US
                 ) -> dict[str, Any]:
    ids = sleeves if sleeves is not None else sleeve_ids(db)
    return {
        "counts": db.counts(),
        "universe": db.universe_stats(),
        "sleeves": [
            sleeve_report(db, sid, capital_cents=capital_cents,
                          markout_horizons_us=horizons_us)
            for sid in ids
        ],
    }


def render_digest(digest: dict[str, Any]) -> str:
    lines = ["=" * WIDTH, "DAILY DIGEST -- PLAN.md section 12 KPIs", "=" * WIDTH]

    counts = digest["counts"]
    lines.append(_rule("database "))
    lines.append("  " + "  ".join(f"{k}={v:,}" for k, v in sorted(counts.items())))
    universe = digest.get("universe") or {}
    if universe.get("markets"):
        lines.append(
            f"  universe: {universe.get('markets', 0):,} markets / "
            f"{universe.get('events', 0):,} events"
        )

    if not digest["sleeves"]:
        lines.append(_rule("sleeves "))
        lines.append("  no sleeve has recorded a decision or an order yet.")
        lines.append("  run `python -m runner --once` in shadow mode to populate this.")
    else:
        for report in digest["sleeves"]:
            lines.append(render_sleeve(report))

    lines.append(_rule("alerting "))
    lines.append("  " + WebhookAlerter().describe())
    lines.append("=" * WIDTH)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="daily KPI digest (PLAN.md section 12)")
    ap.add_argument("--db", default=None, help="sqlite path; defaults to config")
    ap.add_argument("--sleeve", action="append", default=None,
                    help="restrict to this sleeve id (repeatable)")
    ap.add_argument("--capital", type=int, default=0,
                    help="capital in CENTS, for the non-edge income denominator")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    db_path = args.db or str(load_settings().db_path)
    with Database(db_path) as db:
        digest = build_digest(db, sleeves=args.sleeve, capital_cents=args.capital)
        if args.json:
            print(json.dumps(_jsonable(digest), indent=2, sort_keys=True, default=str))
        else:
            print(render_digest(digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
