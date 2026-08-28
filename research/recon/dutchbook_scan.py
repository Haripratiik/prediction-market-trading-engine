"""S2 Dutch-book feasibility study on the live Kalshi universe: done honestly.

This is also the prototype for the real S2 scanner. The central discipline it
demonstrates: a resting order is only credible if there is a real bid to join or
improve. Resting at 1c on a market nobody bids is "liquidity fantasy" and it is
what makes naive arbitrage scans report absurd win rates.

Run: python research/recon/dutchbook_scan.py
"""
from __future__ import annotations
import gzip, json, os, statistics as st
from collections import Counter
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TICK = 0.01

def fee(p, maker):  return (0.0175 if maker else 0.07) * p * (1 - p)
def num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    try: return float(str(v).strip())
    except Exception: return None
def pct(x, n): return f"{100.0*x/n:5.1f}%" if n else "  n/a"
def quant(xs, qs=(0.05, 0.25, 0.5, 0.75, 0.95)):
    if not xs: return {}
    s = sorted(xs)
    return {q: s[min(len(s)-1, max(0, int(round(q*(len(s)-1)))))] for q in qs}
def qline(lbl, xs, fmt="{:.2f}"):
    q = quant(xs)
    print(f"  {lbl:<38} " + "  ".join(f"p{int(k*100)}=" + fmt.format(v) for k, v in q.items()))

# ---------------------------------------------------------------- load
with gzip.open(os.path.join(HERE, "kalshi_events.json.gz"), "rt", encoding="utf-8") as f:
    blob = json.load(f)
events, now = blob["events"], datetime.now(timezone.utc)

EV = []
for e in events:
    s = e.get("series_ticker") or ""
    if s.startswith("KXMVE") or not e.get("mutually_exclusive"):
        continue
    legs = []
    for m in (e.get("markets") or []):
        if (m.get("status") or "") not in ("active", "open"): continue
        yb, ya = num(m.get("yes_bid_dollars")), num(m.get("yes_ask_dollars"))
        if yb is None or ya is None or ya <= yb or not (0 < ya <= 1.0): continue
        try:
            hrs = (datetime.fromisoformat(str(m.get("close_time")).replace("Z","+00:00")) - now).total_seconds()/3600
        except Exception:
            hrs = None
        legs.append({"t": m.get("ticker"), "yb": yb, "ya": ya,
                     "bsz": num(m.get("yes_bid_size_fp")) or 0.0,
                     "asz": num(m.get("yes_ask_size_fp")) or 0.0,
                     "v24": num(m.get("volume_24h_fp")) or 0.0,
                     "vol": num(m.get("volume_fp")) or 0.0, "hrs": hrs})
    if 2 <= len(legs) <= 30:
        EV.append({"event": e.get("event_ticker"), "series": s,
                   "cat": e.get("category") or "?",
                   "collateral": e.get("collateral_return_type") or "?",
                   "legs": legs})

print("=" * 78)
print("S2 DUTCH-BOOK FEASIBILITY: exchange-flagged mutually_exclusive events")
print("=" * 78)
print(f"  MECE events with 2-30 live legs: {len(EV):,}")

# ---------------------------------------------------------------- models
def taker(ev):
    """Buy every YES at the ask, right now."""
    px = [l["ya"] for l in ev["legs"]]
    net = 1.0 - sum(px) - sum(fee(p, False) for p in px)
    return {"px": px, "sum": sum(px), "net": net,
            "size": min(l["asz"] for l in ev["legs"])}

def maker(ev):
    """Rest on every leg. A leg with no bid is NOT restable (fantasy guard).
    Improve the bid by one tick only when the spread leaves room; else join."""
    px = []
    for l in ev["legs"]:
        if l["yb"] < TICK:
            return None
        px.append(l["yb"] + TICK if (l["ya"] - l["yb"]) > TICK else l["yb"])
    net = 1.0 - sum(px) - sum(fee(p, True) for p in px)
    return {"px": px, "sum": sum(px), "net": net,
            "size": min(l["bsz"] for l in ev["legs"])}

rows = []
for ev in EV:
    t, m = taker(ev), maker(ev)
    legs = ev["legs"]
    rows.append({
        "ev": ev, "t": t, "m": m,
        "v24": sum(l["v24"] for l in legs),
        "vol": sum(l["vol"] for l in legs),
        "maxspr": max(l["ya"] - l["yb"] for l in legs),
        "minhrs": min([l["hrs"] for l in legs if l["hrs"] is not None] or [None]),
        "n": len(legs),
    })

# ---------------------------------------------------------------- overround
print("\n" + "-" * 78)
print("A. OVERROUND: what the books actually cost")
print("-" * 78)
qline("sum(YES ask)  [taker cost]", [r["t"]["sum"] for r in rows], "{:.3f}")
rest = [r for r in rows if r["m"]]
print(f"  events where EVERY leg has a real bid : {len(rest):,}  {pct(len(rest),len(rows))}")
if rest:
    qline("sum(maker px) [restable only]", [r["m"]["sum"] for r in rest], "{:.3f}")
    qline("implied maker->taker cost saving (c)",
          [(r["t"]["sum"] - r["m"]["sum"])*100 for r in rest], "{:.1f}")

# ---------------------------------------------------------------- filters
LIQ = dict(min_v24=1.0, min_size=20.0, max_spread=0.10, min_hrs=1.0)
def liquid(r, side):
    d = r[side]
    if not d: return False
    if r["v24"] < LIQ["min_v24"]: return False
    if d["size"] < LIQ["min_size"]: return False
    if r["maxspr"] > LIQ["max_spread"]: return False
    if r["minhrs"] is None or r["minhrs"] < LIQ["min_hrs"]: return False
    return True

print("\n" + "-" * 78)
print("B. PROFITABLE STRUCTURES: naive vs liquidity-filtered")
print(f"   filter: 24h vol>0, min leg size>={LIQ['min_size']:.0f}, every spread<={LIQ['max_spread']*100:.0f}c, >1h to close")
print("-" * 78)
for side, label in (("t", "TAKER"), ("m", "MAKER")):
    pool = [r for r in rows if r[side]]
    pre = [r for r in pool if 1.0 - r[side]["sum"] > 0]
    post = [r for r in pool if r[side]["net"] > 0]
    liq = [r for r in post if liquid(r, side)]
    print(f"\n  {label}")
    print(f"    candidates (structure computable) : {len(pool):,}")
    print(f"    positive BEFORE fees              : {len(pre):,}  {pct(len(pre),len(pool))}")
    print(f"    positive AFTER fees               : {len(post):,}  {pct(len(post),len(pool))}")
    print(f"    ... AND passes liquidity filter   : {len(liq):,}  {pct(len(liq),len(pool))}   <-- REAL")
    if liq:
        qline("      net margin (cents)", [r[side]["net"]*100 for r in liq])
        qline("      min leg size (contracts)", [r[side]["size"] for r in liq], "{:,.0f}")
        qline("      hours to close", [r["minhrs"] for r in liq], "{:,.0f}")
        cap = sum(min(r[side]["size"] * 0.20, 500) * r[side]["sum"] for r in liq)
        prof = sum(min(r[side]["size"] * 0.20, 500) * r[side]["net"] for r in liq)
        print(f"      deployable @20% of min-leg size : ${cap:,.0f}")
        print(f"      one-shot profit if ALL complete : ${prof:,.0f}")
        print(f"      by category: {dict(Counter(r['ev']['cat'] for r in liq).most_common(8))}")
        print(f"      top 12:")
        for r in sorted(liq, key=lambda x: -x[side]["net"])[:12]:
            print(f"        {r['ev']['event'][:38]:<38} n={r['n']:>2} sum={r[side]['sum']:.3f} "
                  f"net={r[side]['net']*100:+6.2f}c size={r[side]['size']:>6,.0f} "
                  f"spr<={r['maxspr']*100:>3.0f}c v24={r['v24']:>7,.0f} {r['ev']['cat'][:10]}")

# ---------------------------------------------------------------- fee impact
print("\n" + "-" * 78)
print("C. HOW MUCH DO FEES DECIDE? (C2 thesis, measured)")
print("-" * 78)
for n in (2, 3, 5, 8, 12):
    sub = [r for r in rows if r["n"] == n]
    if not sub: continue
    p = 0.97 / n
    ft, fm = n*fee(p, False), n*fee(p, True)
    print(f"  n={n:>2}: {len(sub):>5} events | fee hurdle taker {ft*100:5.2f}c  maker {fm*100:5.2f}c "
          f"| max sum(px) to profit: taker {1-ft:.4f}  maker {1-fm:.4f}")

# ---------------------------------------------------------------- collateral
print("\n" + "-" * 78)
print("D. COLLATERAL TREATMENT (capital efficiency for multi-leg structures)")
print("-" * 78)
print("  collateral_return_type across MECE events:",
      dict(Counter(e["collateral"] for e in EV).most_common()))
print("  NOTE: MECNET indicates mutually-exclusive netting. If Kalshi nets collateral")
print("        across legs of one MECE event, the capital needed for a Dutch book is")
print("        far below sum(px) x size. VERIFY against the API's margin/limits")
print("        endpoints before sizing S2 -- this materially changes ROLC.")

# ---------------------------------------------------------------- staleness
print("\n" + "-" * 78)
print("E. STALENESS: why naive scans lie")
print("-" * 78)
zero24 = sum(1 for r in rows if r["v24"] == 0)
zerovol = sum(1 for r in rows if r["vol"] == 0)
print(f"  MECE events with ZERO 24h volume      : {zero24:,}  {pct(zero24,len(rows))}")
print(f"  MECE events with ZERO lifetime volume : {zerovol:,}  {pct(zerovol,len(rows))}")
wide = sum(1 for r in rows if r["maxspr"] > 0.10)
print(f"  events with some leg spread > 10c     : {wide:,}  {pct(wide,len(rows))}")
nobid = sum(1 for r in rows if not r["m"])
print(f"  events with a leg having NO bid at all : {nobid:,}  {pct(nobid,len(rows))}")
print("\n  A scan that ignores these reports thousands of 'arbitrages' that cannot be")
print("  executed. The liquidity-filtered counts in section B are the honest number.")
print("\nDone.")
