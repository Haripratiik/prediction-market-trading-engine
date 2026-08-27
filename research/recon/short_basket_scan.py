"""S2-SHORT: the structurally safe Dutch book on Kalshi.

KEY INSIGHT (confirmed against the API's own semantics):
    `mutually_exclusive = true` guarantees AT MOST ONE leg resolves YES.
    It does NOT guarantee at least one does.

Therefore the two directions are NOT symmetric:

    BUY the basket  (pay sum(ask), collect $1 if some leg wins)
        -> UNSAFE. Requires independently verified EXHAUSTIVENESS.
           If no listed outcome occurs you collect $0. This is the trap
           that makes candidate-list events look like +87c arbitrage.

    SELL the basket (collect sum(bid), pay AT MOST $1)
        -> SAFE. Max liability is $1 regardless of exhaustiveness.
           Worst case profit = sum(bid) - 1 - fees.
           Non-exhaustiveness makes it BETTER: if nothing listed wins,
           every leg expires worthless and you keep the whole premium.

Since live books are typically OVERROUND (median sum(ask) ~ 1.14), the short
side is also where the density is. This scan measures it.

Fee note: Kalshi charges maker fees on only ~130 of 13,486 series
(`fee_type = quadratic_with_maker_fees`); on the rest makers pay ZERO.
Selling into the bid is a TAKER action, so taker fees are modelled here,
and a resting-ask (maker) variant is reported alongside.

Run: python research/recon/short_basket_scan.py
"""
from __future__ import annotations
import gzip, json, os
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
TAKER, MAKER_DEFAULT = 0.07, 0.0        # maker is free on ~99% of series

def fee(p, theta): return theta * p * (1 - p)
def num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    try: return float(str(v).strip())
    except Exception: return None
def pct(x, n): return f"{100.0*x/n:5.1f}%" if n else "  n/a"
def quant(xs, qs=(0.05,0.25,0.5,0.75,0.95)):
    if not xs: return {}
    s=sorted(xs); return {q: s[min(len(s)-1,max(0,int(round(q*(len(s)-1)))))] for q in qs}
def qline(lbl, xs, fmt="{:.3f}"):
    q=quant(xs)
    print(f"  {lbl:<40} " + "  ".join(f"p{int(k*100)}="+fmt.format(v) for k,v in q.items()))

blob = json.load(gzip.open(os.path.join(HERE,"kalshi_events.json.gz"),"rt",encoding="utf-8"))
events, now = blob["events"], datetime.now(timezone.utc)

EV=[]
for e in events:
    s = e.get("series_ticker") or ""
    if s.startswith("KXMVE") or not e.get("mutually_exclusive"): continue
    legs=[]
    for m in (e.get("markets") or []):
        if m.get("status") not in ("active","open"): continue
        yb, ya = num(m.get("yes_bid_dollars")), num(m.get("yes_ask_dollars"))
        if yb is None or ya is None or ya <= yb or not (0 < ya <= 1.0): continue
        try: hrs=(datetime.fromisoformat(str(m.get("close_time")).replace("Z","+00:00"))-now).total_seconds()/3600
        except Exception: hrs=None
        legs.append({"t":m.get("ticker"),"yb":yb,"ya":ya,
                     "bsz":num(m.get("yes_bid_size_fp")) or 0.0,
                     "asz":num(m.get("yes_ask_size_fp")) or 0.0,
                     "v24":num(m.get("volume_24h_fp")) or 0.0,
                     "hrs":hrs,"sub":m.get("yes_sub_title") or ""})
    if 2 <= len(legs) <= 90:
        EV.append({"e":e.get("event_ticker"),"series":s,"cat":e.get("category") or "?",
                   "title":(e.get("title") or "")[:60],"legs":legs,
                   "crt":e.get("collateral_return_type") or "?"})

print("="*80)
print("S2-SHORT — SELLING MUTUALLY-EXCLUSIVE BASKETS (the structurally safe direction)")
print("="*80)
print(f"  MECE events with 2-90 live legs: {len(EV):,}")
print(f"  collateral_return_type values  : {dict(Counter(x['crt'] for x in EV))}")

rows=[]
for ev in EV:
    legs=ev["legs"]
    sb = sum(l["yb"] for l in legs)          # sell into the bid  (TAKER)
    sa = sum(l["ya"] for l in legs)          # rest asks          (MAKER)
    live = [l for l in legs if l["yb"] >= 0.01]
    gross_t = sb - 1.0
    net_t   = gross_t - sum(fee(l["yb"], TAKER) for l in legs)
    gross_m = sa - 1.0
    net_m   = gross_m - sum(fee(l["ya"], MAKER_DEFAULT) for l in legs)
    rows.append({**ev,"n":len(legs),"sum_bid":sb,"sum_ask":sa,
                 "net_t":net_t,"net_m":net_m,
                 "n_bidless":len(legs)-len(live),
                 "size":min((l["bsz"] for l in legs), default=0.0),
                 "v24":sum(l["v24"] for l in legs),
                 "maxspr":max(l["ya"]-l["yb"] for l in legs),
                 "minhrs":min([l["hrs"] for l in legs if l["hrs"] is not None] or [0])})

print("\n"+"-"*80); print("A. THE OVERROUND IS ON THE SELL SIDE"); print("-"*80)
qline("sum(YES bid)  [what selling collects]", [r["sum_bid"] for r in rows])
qline("sum(YES ask)  [what buying costs]",     [r["sum_ask"] for r in rows])
over_b = sum(1 for r in rows if r["sum_bid"] > 1.0)
over_a = sum(1 for r in rows if r["sum_ask"] < 1.0)
print(f"\n  events with sum(bid) > 1.00  (SELL candidates, SAFE) : {over_b:,}  {pct(over_b,len(rows))}")
print(f"  events with sum(ask) < 1.00  (BUY candidates, UNSAFE): {over_a:,}  {pct(over_a,len(rows))}")

print("\n"+"-"*80); print("B. PROFITABLE SHORT BASKETS"); print("-"*80)
LIQ=dict(min_size=20.0, max_spread=0.20, min_hrs=1.0, min_v24=1.0)
def liquid(r):
    return (r["size"]>=LIQ["min_size"] and r["maxspr"]<=LIQ["max_spread"]
            and r["minhrs"]>=LIQ["min_hrs"] and r["v24"]>=LIQ["min_v24"])
for lbl,key,szkey in (("SELL INTO BID (taker fees)","net_t","size"),
                      ("REST ASKS (maker, fee-free on ~99% of series)","net_m","size")):
    pos=[r for r in rows if r[key]>0]
    liq=[r for r in pos if liquid(r)]
    print(f"\n  {lbl}")
    print(f"    positive after fees            : {len(pos):,}  {pct(len(pos),len(rows))}")
    print(f"    + liquidity filter             : {len(liq):,}  {pct(len(liq),len(rows))}   <-- REAL")
    if liq:
        qline("      net margin per basket (c)", [r[key]*100 for r in liq], "{:.2f}")
        qline("      min leg bid size", [r["size"] for r in liq], "{:,.0f}")
        qline("      legs", [r["n"] for r in liq], "{:.0f}")
        cap=sum(min(r["size"]*0.20,500) for r in liq)
        prof=sum(min(r["size"]*0.20,500)*r[key] for r in liq)
        print(f"      baskets @20% of min-leg size   : {cap:,.0f} contracts")
        print(f"      one-shot profit if all fill    : ${prof:,.0f}")
        print(f"      by category: {dict(Counter(r['cat'] for r in liq).most_common(8))}")
        print(f"      top 15:")
        for r in sorted(liq,key=lambda x:-x[key])[:15]:
            print(f"        {r['e'][:36]:<36} n={r['n']:>2} sumbid={r['sum_bid']:.3f} "
                  f"net={r[key]*100:+6.2f}c size={r['size']:>7,.0f} v24={r['v24']:>8,.0f} {r['cat'][:11]}")

print("\n"+"-"*80); print("C. WHY THE SHORT SIDE IS SAFE AND THE LONG SIDE IS NOT"); print("-"*80)
notexh=[r for r in rows if r["sum_bid"] < 0.80]
print(f"  events with sum(bid) < 0.80 (almost certainly NOT exhaustive): {len(notexh):,}")
print("  For these, BUYING the basket looks spectacular and returns $0 if no listed")
print("  outcome occurs. SELLING them is unaffected -- max liability is still $1.")
for r in sorted(notexh,key=lambda x:x["sum_bid"])[:8]:
    print(f"    sumbid={r['sum_bid']:.3f} sumask={r['sum_ask']:.3f} n={r['n']:>2} "
          f"{r['e'][:32]:<32} {r['title']}")

print("\n"+"-"*80); print("D. LEG-COUNT ECONOMICS (fees scale with N)"); print("-"*80)
print(f"  {'n':>3} {'events':>7} {'median sum(bid)':>16} {'taker fee cost':>15} {'maker fee cost':>15}")
for n in (2,3,4,5,6,8,10,16,25):
    sub=[r for r in rows if r["n"]==n]
    if not sub: continue
    med=quant([r["sum_bid"] for r in sub]).get(0.5,0)
    p=med/n if n else 0
    print(f"  {n:>3} {len(sub):>7,} {med:>16.3f} {n*fee(p,TAKER)*100:>14.2f}c {n*fee(p,MAKER_DEFAULT)*100:>14.2f}c")

print("\n"+"-"*80); print("E. CAPITAL: MECNET COLLATERAL"); print("-"*80)
print("  Every event here is collateral_return_type = MECNET, which per the API's own")
print("  semantics assesses collateral on the WORST-CASE EVENT OUTCOME rather than")
print("  per leg (EventPosition.event_exposure_dollars is a distinct field from the sum")
print("  of market_exposure_dollars). For a short basket the worst case is $1 per basket,")
print("  so required collateral should be ~ (1 - sum(bid)) per basket, NOT sum(1 - bid_i).")
ex=[r for r in rows if r["net_t"]>0 and liquid(r)]
if ex:
    naive=sum((r["n"]-r["sum_bid"]) for r in ex)
    netted=sum(max(0.0,1.0-r["sum_bid"]) for r in ex)
    print(f"\n  across the {len(ex)} liquid profitable short baskets, per 1 basket each:")
    print(f"    collateral if charged PER LEG      : ${naive:,.2f}")
    print(f"    collateral if MECNET (worst case)  : ${netted:,.2f}")
    if netted > 0:
        print(f"    capital efficiency multiplier      : {naive/netted:,.1f}x")
    print("  VERIFY against an authenticated account before sizing (PLAN.md T-050c).")
print("\nDone.")
