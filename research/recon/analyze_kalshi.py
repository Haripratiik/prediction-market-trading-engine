"""Characterize the live Kalshi universe from /events (nested markets) and test
the PLAN.md strategy assumptions against real quotes.

The /markets endpoint is dominated by multivariate parlay shards; /events carries
the real universe AND the fields the RV sleeves need:
    mutually_exclusive     -> exchange-declared MECE flag (S2 Dutch book gate)
    settlement_sources     -> rulebook engine input
    collateral_return_type -> capital efficiency / netting

Answers with real numbers:
  Q1 universe composition and category mix
  Q2 S1: size of the tradeable favorite band after filters
  Q3 spreads, top-of-book depth, volume concentration
  Q4 S2: mutually-exclusive events and Dutch-book margins (maker vs taker)
  Q5 S3: within-event structure available for linked RV
  Q6 rulebook engine feasibility

Run: python research/recon/analyze_kalshi.py
"""
from __future__ import annotations
import gzip, json, os, statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- canonical fee model (PLAN.md 2.1) ------------------------------------
def fee(p: float, is_maker: bool) -> float:
    return (0.0175 if is_maker else 0.07) * p * (1 - p)

def num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    s = str(v).strip()
    if not s: return None
    try: return float(s)
    except ValueError: return None

def pct(x, n): return f"{100.0*x/n:5.1f}%" if n else "  n/a"

def quantiles(xs, qs=(0.05, 0.25, 0.5, 0.75, 0.95)):
    if not xs: return {q: 0 for q in qs}
    s = sorted(xs); out = {}
    for q in qs:
        out[q] = s[min(len(s)-1, max(0, int(round(q*(len(s)-1)))))]
    return out

def qline(label, xs, fmt="{:.3f}"):
    q = quantiles(xs)
    print(f"  {label:<34} " + "  ".join(f"p{int(k*100)}=" + fmt.format(v) for k, v in q.items()))

def hist(title, xs, edges, unit=""):
    print(f"\n  {title}  (n={len(xs)})")
    for lo, hi in zip(edges[:-1], edges[1:]):
        c = sum(1 for x in xs if lo <= x < hi)
        print(f"    [{lo:>7.2f},{hi:>7.2f}){unit} {c:>7} {pct(c,len(xs))} " + "#"*int(55*c/max(1,len(xs))))

# ---- load -----------------------------------------------------------------
with gzip.open(os.path.join(HERE, "kalshi_events.json.gz"), "rt", encoding="utf-8") as f:
    blob = json.load(f)
meta, events = blob["meta"], blob["events"]
now = datetime.now(timezone.utc)

print("=" * 78); print("KALSHI LIVE UNIVERSE RECONNAISSANCE  (source: /events nested)"); print("=" * 78)
print(json.dumps(meta, indent=2))

# ---- flatten --------------------------------------------------------------
EV, Q = [], []
for e in events:
    st_ = e.get("series_ticker") or "?"
    if st_.startswith("KXMVE"):      # parlay shards: excluded from all stats
        continue
    legs = []
    for m in (e.get("markets") or []):
        if (m.get("status") or "") not in ("active", "open"):
            continue
        yb, ya = num(m.get("yes_bid_dollars")), num(m.get("yes_ask_dollars"))
        if yb is None or ya is None or ya <= yb or not (0 < ya <= 1.0):
            continue
        try:
            ct = datetime.fromisoformat(str(m.get("close_time")).replace("Z", "+00:00"))
            hrs = (ct - now).total_seconds() / 3600.0
        except Exception:
            hrs = None
        rec = {
            "ticker": m.get("ticker"), "event": e.get("event_ticker"), "series": st_,
            "category": e.get("category") or "?",
            "yb": yb, "ya": ya, "mid": (yb+ya)/2, "spread": ya-yb,
            "bsz": num(m.get("yes_bid_size_fp")) or 0.0,
            "asz": num(m.get("yes_ask_size_fp")) or 0.0,
            "vol": num(m.get("volume_fp")) or 0.0,
            "v24": num(m.get("volume_24h_fp")) or 0.0,
            "oi": num(m.get("open_interest_fp")) or 0.0,
            "hrs": hrs,
            "rules_len": len(str(m.get("rules_primary") or "")),
        }
        legs.append(rec); Q.append(rec)
    if legs:
        EV.append({
            "event": e.get("event_ticker"), "series": st_,
            "category": e.get("category") or "?",
            "mece": bool(e.get("mutually_exclusive")),
            "collateral": e.get("collateral_return_type") or "?",
            "sources": e.get("settlement_sources") or [],
            "title": e.get("title") or "", "legs": legs,
        })

print(f"\n  non-parlay events with live quotes : {len(EV):,}")
print(f"  quoted markets                     : {len(Q):,}")

# ---- Q1 -------------------------------------------------------------------
print("\n" + "="*78); print("Q1. UNIVERSE COMPOSITION"); print("="*78)
cat = Counter(e["category"] for e in EV)
print("  events by category:")
for k, v in cat.most_common(20): print(f"    {k:<32} {v:>6,}  {pct(v,len(EV))}")
ser = Counter(e["series"] for e in EV)
print(f"\n  distinct series: {len(ser):,}   top 20 by event count:")
for k, v in ser.most_common(20): print(f"    {k:<32} {v:>6,}")
print("\n  collateral_return_type:", dict(Counter(e["collateral"] for e in EV).most_common()))

# ---- Q3 -------------------------------------------------------------------
print("\n" + "="*78); print("Q3. SPREADS, DEPTH, VOLUME"); print("="*78)
sp = [q["spread"] for q in Q]
qline("spread (dollars)", sp)
print(f"  mean={st.mean(sp):.4f}  median={st.median(sp):.4f}")
for t in (0.01, 0.02, 0.05):
    c = sum(1 for s in sp if s <= t); print(f"  spread <= {t*100:.0f}c: {c:,} {pct(c,len(sp))}")
hist("spread distribution", sp, [0,.01,.02,.03,.05,.10,.20,.50,1.01])

qline("bid size at touch (contracts)", [q["bsz"] for q in Q], "{:,.0f}")
qline("bid notional at touch ($)", [q["bsz"]*q["mid"] for q in Q], "${:,.0f}")
qline("lifetime volume (contracts)", [q["vol"] for q in Q], "{:,.0f}")
qline("24h volume (contracts)", [q["v24"] for q in Q], "{:,.0f}")
qline("open interest", [q["oi"] for q in Q], "{:,.0f}")
dead = sum(1 for q in Q if q["v24"] == 0)
print(f"  markets with ZERO 24h volume       : {dead:,}  {pct(dead,len(Q))}")
tot24 = sum(q["v24"] for q in Q)
top = sorted(Q, key=lambda x: -x["v24"])
for k in (10, 50, 200, 1000):
    share = sum(q["v24"] for q in top[:k]) / max(1.0, tot24)
    print(f"  top {k:>4} markets = {share*100:5.1f}% of 24h volume")

# ---- Q2 -------------------------------------------------------------------
print("\n" + "="*78); print("Q2. S1 STRUCTURAL SLEEVE — tradeable favorite band"); print("="*78)
band = [q for q in Q if 0.70 <= q["mid"] <= 0.95]
def s1_ok(q, min_depth=200.0):
    return (q["hrs"] is not None and 1.0 <= q["hrs"] <= 2160.0
            and fee(q["mid"], True)/q["mid"] <= 0.04
            and q["bsz"]*q["mid"] >= min_depth)
s1 = [q for q in band if s1_ok(q)]
s1v = [q for q in s1 if q["v24"] > 0]
print(f"  mid in [0.70,0.95]                 : {len(band):,}  {pct(len(band),len(Q))}")
print(f"  + horizon 1h-90d, depth>=$200      : {len(s1):,}")
print(f"  + nonzero 24h volume  (S1 UNIVERSE): {len(s1v):,}")
if s1v:
    print(f"  deployable @20% of touch depth     : ${sum(min(q['bsz']*q['mid']*0.20, 5000) for q in s1v):,.0f}")
    qline("their spreads", [q["spread"] for q in s1v])
    qline("their 24h volume", [q["v24"] for q in s1v], "{:,.0f}")
    print("  top series in S1 universe:")
    for k, v in Counter(q["series"] for q in s1v).most_common(15): print(f"    {k:<32} {v:>5}")
    print("  by category:", dict(Counter(next(e["category"] for e in EV if e["event"]==q["event"]) for q in s1v).most_common(8)))

# ---- Q4 -------------------------------------------------------------------
print("\n" + "="*78); print("Q4. S2 DUTCH BOOK — exchange-declared mutually-exclusive events"); print("="*78)
mece = [e for e in EV if e["mece"]]
mece_multi = [e for e in mece if len(e["legs"]) >= 2]
print(f"  events flagged mutually_exclusive   : {len(mece):,}  {pct(len(mece),len(EV))}")
print(f"  ... with >=2 live legs              : {len(mece_multi):,}")
print("  leg-count distribution:", dict(sorted(Counter(len(e['legs']) for e in mece_multi).items())[:15]))
print("  by category:", dict(Counter(e["category"] for e in mece_multi).most_common(10)))

cands = []
for e in mece_multi:
    legs = e["legs"]
    if not (2 <= len(legs) <= 30): continue
    sum_ask = sum(l["ya"] for l in legs)
    net_t = 1.0 - sum_ask - sum(fee(l["ya"], False) for l in legs)
    px_m = [min(l["yb"] + 0.01, l["ya"]) for l in legs]
    net_m = 1.0 - sum(px_m) - sum(fee(p, True) for p in px_m)
    cands.append({**e, "n": len(legs), "sum_ask": sum_ask, "sum_m": sum(px_m),
                  "net_t": net_t, "net_m": net_m,
                  "depth": min(l["asz"] for l in legs),
                  "v24": sum(l["v24"] for l in legs)})

if cands:
    qline("sum(YES ask) over MECE events", [c["sum_ask"] for c in cands])
    hist("sum(YES ask)", [c["sum_ask"] for c in cands], [0,.90,.95,1.00,1.02,1.05,1.15,1.50,5.0])
    pre_t = sum(1 for c in cands if 1.0 - c["sum_ask"] > 0)
    pre_m = sum(1 for c in cands if 1.0 - c["sum_m"] > 0)
    prof_t = [c for c in cands if c["net_t"] > 0]
    prof_m = [c for c in cands if c["net_m"] > 0]
    print(f"\n  sum(ask)   < 1.00 before fees : {pre_t:,}   after fees -> {len(prof_t):,}")
    print(f"  sum(maker) < 1.00 before fees : {pre_m:,}   after fees -> {len(prof_m):,}")
    print(f"  TAKER Dutch book profitable   : {len(prof_t):,}  {pct(len(prof_t),len(cands))}")
    print(f"  MAKER Dutch book profitable   : {len(prof_m):,}  {pct(len(prof_m),len(cands))}   <-- C2 measured")
    for lbl, arr, key, sk in (("TAKER", prof_t, "net_t", "sum_ask"), ("MAKER", prof_m, "net_m", "sum_m")):
        if arr:
            print(f"\n  top 12 {lbl}-profitable structures:")
            for c in sorted(arr, key=lambda x: -x[key])[:12]:
                print(f"    {c['event'][:40]:<40} n={c['n']:>2} sum={c[sk]:.3f} "
                      f"net={c[key]*100:+6.2f}c depth={c['depth']:>7,.0f} v24={c['v24']:>9,.0f}  {c['category'][:12]}")
    liveish = [c for c in prof_m if c["v24"] > 0 and c["depth"] >= 20]
    print(f"\n  MAKER-profitable AND traded in 24h AND depth>=20: {len(liveish):,}   <-- realistic S2 pipeline")

# ---- Q5 -------------------------------------------------------------------
print("\n" + "="*78); print("Q5. S3 LINKED RV — structure available"); print("="*78)
non_mece_multi = [e for e in EV if not e["mece"] and len(e["legs"]) >= 2]
print(f"  multi-leg events NOT flagged MECE  : {len(non_mece_multi):,}  <- L2/L3/L4 link candidates")
print("  by category:", dict(Counter(e["category"] for e in non_mece_multi).most_common(10)))
print("  leg counts:", dict(sorted(Counter(len(e['legs']) for e in non_mece_multi).items())[:12]))
# threshold ladders: same series, ordered strikes -> L2 implication candidates
ladders = [e for e in EV if len(e["legs"]) >= 3]
print(f"  events with >=3 legs (ladders)     : {len(ladders):,}")
print("  top ladder series:", dict(Counter(e['series'] for e in ladders).most_common(12)))

# ---- Q6 -------------------------------------------------------------------
print("\n" + "="*78); print("Q6. RULEBOOK ENGINE FEASIBILITY"); print("="*78)
have = sum(1 for q in Q if q["rules_len"] > 0)
print(f"  markets with rules_primary text    : {have:,}  {pct(have,len(Q))}")
if have: qline("rules_primary length (chars)", [q["rules_len"] for q in Q if q["rules_len"] > 0], "{:,.0f}")
withsrc = sum(1 for e in EV if e["sources"])
print(f"  events with settlement_sources     : {withsrc:,}  {pct(withsrc,len(EV))}")
srcnames = Counter()
for e in EV:
    for s in e["sources"]:
        srcnames[(s.get("name") or s.get("url") or "?") if isinstance(s, dict) else str(s)] += 1
print("  top settlement sources:")
for k, v in srcnames.most_common(15): print(f"    {str(k)[:60]:<60} {v:>6,}")

# ---- horizon --------------------------------------------------------------
print("\n" + "="*78); print("TIME-TO-CLOSE (capital lockup)"); print("="*78)
hrs = [q["hrs"] for q in Q if q["hrs"] and q["hrs"] > 0]
qline("hours to close", hrs, "{:,.1f}")
for lbl, lo, hi in [("<24h",0,24),("1-7d",24,168),("1-4wk",168,720),("1-3mo",720,2160),(">3mo",2160,1e9)]:
    c = sum(1 for h in hrs if lo <= h < hi); print(f"    {lbl:<7} {c:>7,}  {pct(c,len(hrs))}")

print("\nDone.")
