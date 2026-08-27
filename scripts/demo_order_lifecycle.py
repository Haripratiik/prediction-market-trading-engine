"""T-045: prove the full order lifecycle works on the Kalshi demo.

    python -m scripts.demo_order_lifecycle

Places tiny orders on the DEMO exchange (mock funds, no real money), exercises
place / rest / queue-position / cancel, then attempts a marketable order to see
whether the demo has a real counterparty.

Refuses to run against production.
"""

from __future__ import annotations

import sys
import uuid

from core.config import load_settings
from venues.kalshi.client import KalshiClient, KalshiError

OK, BAD, INFO = "  [ok]", "  [FAIL]", "  ->"


def pick_market(client: KalshiClient) -> dict | None:
    """A demo market with a real two-sided quote, on shard 0 where the funds are."""
    data = client._request(
        "GET", "/markets", params={"limit": 1000, "status": "open", "mve_filter": "exclude"}
    )
    def c(v):
        try:
            return int(round(float(str(v)) * 100))
        except Exception:
            return 0
    best = None
    for m in data.get("markets", []):
        bid, ask = c(m.get("yes_bid_dollars")), c(m.get("yes_ask_dollars"))
        if not (0 < bid < ask <= 99):
            continue
        if m.get("exchange_index") not in (0, None):
            continue
        spread = ask - bid
        if best is None or spread < best[0]:
            best = (spread, m, bid, ask)
    return None if best is None else {"m": best[1], "bid": best[2], "ask": best[3]}


def main() -> int:
    s = load_settings()
    if s.kalshi.env != "demo":
        print(f"{BAD} refusing to run against {s.kalshi.env}. Set KALSHI_ENV=demo.")
        return 1
    if not s.kalshi.is_complete:
        print(f"{BAD} {s.kalshi.describe()}")
        return 1

    print("=" * 72)
    print("DEMO ORDER LIFECYCLE  (mock funds -- no real money)")
    print("=" * 72)

    with KalshiClient(base_url=s.kalshi.base_url, signer=s.kalshi.signer()) as c:
        bal = c.balance()
        print(f"  balance: {bal.get('balance_dollars')}")

        pick = pick_market(c)
        if not pick:
            print(f"{BAD} no demo market with a two-sided quote on shard 0")
            return 1
        m, bid, ask = pick["m"], pick["bid"], pick["ask"]
        ticker = m["ticker"]
        print(f"  market : {ticker}")
        print(f"  book   : bid {bid}c / ask {ask}c  (spread {ask-bid}c)")

        # ---- 1. RESTING post-only order.
        # NOTE: every demo book is degenerate (the tightest spread found across
        # 1,000 markets was 98c), so "just inside the touch" is meaningless here.
        # Rest at a mid-ish price that cannot cross and is unambiguously resting.
        rest_px = max(2, min(bid + 20, ask - 20, 50))
        coid = str(uuid.uuid4())
        print(f"\n{INFO} placing post-only BID {rest_px}c x 1 (client_order_id {coid[:8]}...)")
        try:
            resp = c.create_order(ticker=ticker, side="bid", count=1,
                                  price_cents=rest_px, client_order_id=coid,
                                  post_only=True)
        except KalshiError as exc:
            print(f"{BAD} create failed: {exc}")
            return 1
        order = resp.get("order", resp)
        oid = order.get("order_id") or resp.get("order_id")
        print(f"{OK} created. order_id={oid}")
        print(f"       status={order.get('status')} fill_count={order.get('fill_count')} "
              f"remaining={order.get('remaining_count')}")

        # ---- 2. it should be RESTING
        o = c.get_order(oid)
        if o:
            print(f"{OK} readback via list: status={o.get('status')} "
                  f"remaining={o.get('remaining_count')}")
        else:
            print("  [warn] order not found among resting orders")

        # ---- 3. QUEUE POSITION -- the ground truth for fill-model calibration
        try:
            qp = c.queue_position(oid)
            print(f"{OK} queue_position: {qp}   <-- fill-model calibration input")
        except KalshiError as exc:
            print(f"  [warn] queue_position unavailable: {exc}")

        # ---- 4. CANCEL
        try:
            c.cancel_order(oid)
            print(f"{OK} cancelled")
        except KalshiError as exc:
            print(f"{BAD} cancel failed: {exc}")

        # ---- 5. Does the demo have a REAL counterparty?  Cross the spread.
        print(f"\n{INFO} testing whether the demo book is real: taking the {ask}c ask")
        coid2 = str(uuid.uuid4())
        try:
            resp2 = c.create_order(ticker=ticker, side="bid", count=1,
                                   price_cents=ask, client_order_id=coid2,
                                   post_only=False, time_in_force="immediate_or_cancel")
            o2 = resp2.get("order", resp2)
            filled = o2.get("fill_count") or 0
            print(f"       status={o2.get('status')} fill_count={filled} "
                  f"remaining={o2.get('remaining_count')}")
            if float(str(filled) or 0) > 0:
                print(f"{OK} FILLED -- the demo book has a real counterparty")
            else:
                print("  [note] no fill: the displayed quote is not a resting order "
                      "you can trade against")
        except KalshiError as exc:
            print(f"  [note] marketable order rejected: {exc}")

        # ---- 6. fills + positions
        fills = c.fills(limit=10).get("fills", [])
        print(f"\n{OK} fills endpoint: {len(fills)} rows")
        for f in fills[:3]:
            print(f"       {f.get('ticker')} {f.get('side')} "
                  f"count={f.get('count')} price={f.get('yes_price_dollars')} "
                  f"maker={f.get('is_taker') is False}")
        pos = c.positions().get("market_positions", [])
        nonzero = [p for p in pos if float(str(p.get("position_fp") or 0)) != 0]
        print(f"{OK} positions: {len(nonzero)} non-flat")
        for p in nonzero[:3]:
            print(f"       {p.get('ticker')} position={p.get('position_fp')}")

        # ---- 7. leave the account flat
        n = c.cancel_all_orders()
        print(f"{OK} cancel-all cancelled {n} resting order(s)  (I9 kill path)")
        # leave the account flat
        for p_ in c.positions().get("market_positions", []):
            if float(str(p_.get("position_fp") or 0)) != 0:
                r = c.flatten_position(p_["ticker"])
                got = (r or {}).get("order", r or {})
                print(f"{OK} flattened {p_['ticker']}: filled={got.get('fill_count')}")

    print("\n  Lifecycle complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
