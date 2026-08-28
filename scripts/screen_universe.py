"""Run the MECE gate across the recorded universe and report what it catches.

    python -m scripts.screen_universe

Validates T-050b against real data rather than fixtures: how many flagged-MECE
events would a naive scanner have bought, and how many does the gate stop?
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict

from core.db import Database
from core.models import Event, Market, SettlementSource
from rulebook.exhaustiveness import Verdict, check_mece


def load_latest(db: Database) -> tuple[list[Event], dict[str, list[Market]]]:
    """Read the most recent snapshot of every event and its markets."""
    ev_rows = db.conn.execute(
        """SELECT e.* FROM event_snapshots e
           JOIN (SELECT event_ticker, MAX(observed_at_us) AS t
                 FROM event_snapshots GROUP BY event_ticker) latest
             ON e.event_ticker = latest.event_ticker AND e.observed_at_us = latest.t"""
    ).fetchall()
    events = [
        Event(
            event_ticker=r["event_ticker"],
            series_ticker=r["series_ticker"] or "",
            category=r["category"] or "",
            title=r["title"] or "",
            mutually_exclusive=bool(r["mutually_exclusive"]),
            collateral_return_type=r["collateral_return_type"] or "",
            settlement_sources=tuple(
                SettlementSource(**s)
                for s in json.loads(r["settlement_sources_json"] or "[]")
            ),
        )
        for r in ev_rows
    ]

    mk_rows = db.conn.execute(
        """SELECT m.* FROM market_snapshots m
           JOIN (SELECT ticker, MAX(observed_at_us) AS t
                 FROM market_snapshots GROUP BY ticker) latest
             ON m.ticker = latest.ticker AND m.observed_at_us = latest.t"""
    ).fetchall()
    by_event: dict[str, list[Market]] = defaultdict(list)
    for r in mk_rows:
        by_event[r["event_ticker"] or ""].append(
            Market(
                ticker=r["ticker"],
                event_ticker=r["event_ticker"] or "",
                series_ticker=r["series_ticker"] or "",
                title=r["title"] or "",
                status=r["status"] or "",
                yes_bid=r["yes_bid"],
                yes_ask=r["yes_ask"],
                yes_bid_size=r["yes_bid_size"] or 0.0,
                yes_ask_size=r["yes_ask_size"] or 0.0,
                volume_24h=r["volume_24h"] or 0.0,
                close_at_us=r["close_at_us"],
            )
        )
    return events, by_event


def main() -> int:
    with Database("data/pm.db") as db:
        events, by_event = load_latest(db)

    print("=" * 78)
    print("MECE GATE: screening the recorded universe")
    print("=" * 78)
    print(f"  events: {len(events):,}   markets: {sum(len(v) for v in by_event.values()):,}")

    mece = [e for e in events if e.mutually_exclusive]
    multi = [e for e in mece if len(by_event.get(e.event_ticker, [])) >= 2]
    print(f"  flagged mutually_exclusive : {len(mece):,}")
    print(f"  ... with >= 2 legs         : {len(multi):,}")

    checks = {e.event_ticker: check_mece(e, by_event[e.event_ticker]) for e in multi}
    verdicts = Counter(c.verdict for c in checks.values())
    print(f"\n  verdicts: {dict(verdicts)}")

    # What a NAIVE scanner would have bought: sum(ask) < 1 and nothing else.
    naive_buys = [
        t for t, c in checks.items() if c.sum_ask < 1.0
    ]
    gate_allows = [t for t in naive_buys if checks[t].safe_to_buy]
    gate_blocks = [t for t in naive_buys if not checks[t].safe_to_buy]

    print(f"\n  a NAIVE scanner would buy (sum(ask) < 1.00) : {len(naive_buys):,}")
    print(f"  the gate BLOCKS                             : {len(gate_blocks):,}")
    print(f"  the gate would allow (still NEEDS_HUMAN)    : {len(gate_allows):,}")

    traps = sorted(
        (checks[t] for t in gate_blocks),
        key=lambda c: c.sum_ask,
    )[:15]
    print("\n  the cheapest blocked books -- what a naive bot ranks as its BEST trades:")
    for t in sorted(gate_blocks, key=lambda x: checks[x].sum_ask)[:15]:
        c = checks[t]
        ev = next(e for e in multi if e.event_ticker == t)
        naive_margin = (1.0 - c.sum_ask) * 100
        print(
            f"    sum(ask)={c.sum_ask:.3f} n={c.n_legs:>2}  "
            f"'apparent' margin {naive_margin:+6.1f}c  {t[:34]:<34} {ev.title[:40]}"
        )

    # the safe direction
    sellable = [t for t, c in checks.items() if c.safe_to_sell]
    print(f"\n  events SAFE TO SELL (>=2 legs, every leg restable): {len(sellable):,}")
    print("  (selling is capped at $1 liability regardless of exhaustiveness)")

    reasons = Counter()
    for c in checks.values():
        for r in c.reasons:
            reasons[r.split(":")[0].split(",")[0][:60]] += 1
    print("\n  top rejection reasons:")
    for r, n in reasons.most_common(8):
        print(f"    {n:>6,}  {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
