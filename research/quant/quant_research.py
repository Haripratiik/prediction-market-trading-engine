"""Quantitative research backing the prediction-market build plan.
All money in dollars per $1-payout contract unless noted. Prices c in (0,1)."""
import numpy as np
from math import log, sqrt, exp
from statistics import NormalDist

rng = np.random.default_rng(42)
ND = NormalDist()
z = ND.inv_cdf

def kalshi_taker_fee(c):   return 0.07 * c * (1 - c)          # smooth (aggregate round-up ~negligible at size)
def kalshi_maker_fee(c):   return 0.0175 * c * (1 - c)
def pmus_taker_fee(c):     return 0.06 * c * (1 - c)
def pmus_maker_rebate(c):  return -0.0125 * c * (1 - c)

print("=" * 78)
print("A. FEE ALGEBRA — break-even edge (cents) by price, hold-to-settlement (1 fee)")
print("=" * 78)
print(f"{'price':>6} | {'K taker':>8} {'K maker':>8} | {'PM taker':>8} {'PM maker':>9}")
for c in [0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95]:
    print(f"{c*100:5.0f}c | {kalshi_taker_fee(c)*100:7.3f}c {kalshi_maker_fee(c)*100:7.3f}c |"
          f" {pmus_taker_fee(c)*100:7.3f}c {pmus_maker_rebate(c)*100:+8.3f}c")
print("Round trip (enter+exit before settlement) doubles these.")
print("Fee as % of capital at risk (taker, buy YES at c): fee/c:")
for c in [0.05, 0.10, 0.50, 0.90]:
    print(f"  at {c*100:.0f}c: {kalshi_taker_fee(c)/c*100:5.2f}% of stake (Kalshi taker)")

print()
print("=" * 78)
print("B. KELLY — f* = (p-c)/(1-c); growth g(f)=p ln(1+f(1-c)/c)+(1-p) ln(1-f)")
print("=" * 78)
def growth(f, c, p):
    if f <= 0: return 0.0
    up = 1 + f * (1 - c) / c
    dn = 1 - f
    return p * log(up) + (1 - p) * log(dn)

c, p = 0.50, 0.55
fstar = (p - c) / (1 - c)
print(f"Reference bet: price 50c, true prob 55% (5c edge). f* = {fstar:.1%} of bankroll")
for m in [0.10, 0.25, 0.50, 1.00, 1.50, 2.00, 2.40]:
    g = growth(m * fstar, c, p)
    print(f"  {m:4.2f}x Kelly: stake {m*fstar:5.1%}  growth {g*1e4:+7.1f} bp/bet  "
          f"({(exp(g*500)-1)*100:+7.1f}% over 500 bets)")

# Mis-estimation: you think edge is 5c but true edge is 2.5c; you size on the estimate.
p_true = 0.525
print("\nMis-estimation: you SIZE for a 5c edge, TRUE edge is 2.5c (p=52.5%):")
for m in [0.25, 0.50, 1.00]:
    g = growth(m * fstar, c, p_true)
    print(f"  {m:4.2f}x (of estimated) Kelly: growth {g*1e4:+6.1f} bp/bet")
g_half_true = growth(0.5 * (p_true - c) / (1 - c), c, p_true)
print(f"  [comparison: half-Kelly sized on the TRUE edge: {g_half_true*1e4:+6.1f} bp/bet]")

print()
print("=" * 78)
print("C. DRAWDOWN — analytic P(ever hit x of start) = x^(2/m - 1) vs Monte Carlo")
print("   (2,000-bet horizon, price 50c, true p 55%, 20,000 paths)")
print("=" * 78)
n_paths, n_bets = 20000, 2000
for m in [1.0, 0.5, 0.25]:
    f = m * fstar
    mult_up, mult_dn = 1 + f * (1 - c) / c, 1 - f
    wins = rng.random((n_paths, n_bets)) < p
    logw = np.where(wins, log(mult_up), log(mult_dn)).cumsum(axis=1)
    W = np.exp(logw)
    run_max = np.maximum.accumulate(np.hstack([np.ones((n_paths, 1)), W]), axis=1)
    mdd = 1 - (np.hstack([np.ones((n_paths, 1)), W]) / run_max).min(axis=1)
    p_half = (W.min(axis=1) <= 0.5).mean()
    p_90 = (W.min(axis=1) <= 0.1).mean()
    an_half = 0.5 ** (2 / m - 1)
    an_90 = 0.1 ** (2 / m - 1)
    term = np.percentile(W[:, -1], [5, 50])
    print(f"  {m:4.2f}x Kelly: P(halve) MC {p_half:6.1%} (analytic {an_half:6.1%}) | "
          f"P(-90%) MC {p_90:6.2%} (an {an_90:6.2%}) | med MaxDD {np.median(mdd):5.1%} | "
          f"terminal p5 {term[0]:5.2f}x med {term[1]:5.2f}x")

print()
print("=" * 78)
print("D. SAMPLE SIZE — one-sided binomial test H0: win rate=c, alpha=5%")
print("   n = [(z_a*sqrt(c(1-c)) + z_b*sqrt(p(1-p))) / edge]^2")
print("=" * 78)
za = z(0.95)
print(f"{'price':>6} {'edge':>6} | {'n (80% power)':>14} {'n (95% power)':>14}")
for c0, e in [(0.50,0.01),(0.50,0.02),(0.50,0.03),(0.50,0.05),
              (0.85,0.01),(0.85,0.02),(0.85,0.03),(0.15,0.02),(0.15,0.03)]:
    p1 = c0 + e
    n80 = ((za*sqrt(c0*(1-c0)) + z(0.80)*sqrt(p1*(1-p1))) / e) ** 2
    n95 = ((za*sqrt(c0*(1-c0)) + z(0.95)*sqrt(p1*(1-p1))) / e) ** 2
    print(f"{c0*100:5.0f}c {e*100:5.1f}c | {n80:14,.0f} {n95:14,.0f}")

print()
print("--- D2. PEEKING: null strategy (edge=0 at 50c), naive check every 50 trades")
n_paths2, n_bets2 = 20000, 2000
wins = (rng.random((n_paths2, n_bets2)) < 0.5)
cum = wins.cumsum(axis=1)
false_pos = np.zeros(n_paths2, dtype=bool)
for k in range(50, n_bets2 + 1, 50):
    phat = cum[:, k-1] / k
    zstat = (phat - 0.5) / sqrt(0.25 / k)
    false_pos |= (zstat > za)
print(f"  P(naive sequential test declares a (nonexistent) edge within 2,000 trades): {false_pos.mean():.1%}")
print(f"  (single fixed-n test at 2,000: {( (cum[:,-1]/2000 - 0.5)/sqrt(0.25/2000) > za ).mean():.1%} — the promised 5%)")

print()
print("=" * 78)
print("E. SHRINKAGE / WINNER'S CURSE — Bayesian factor sigma_e^2/(sigma_e^2+sigma_n^2)")
print("=" * 78)
# true edges across opportunities ~ N(0, se); your estimate = true + N(0, sn); trade if est > threshold
for se, sn in [(0.03, 0.03), (0.03, 0.02), (0.02, 0.04)]:
    n_opp = 2_000_000
    true_e = rng.normal(0, se, n_opp)
    est_e = true_e + rng.normal(0, sn, n_opp)
    for thr in [0.02, 0.05]:
        sel = est_e > thr
        if sel.sum() == 0: continue
        ratio = true_e[sel].mean() / est_e[sel].mean()
        print(f"  sigma_edge={se*100:.0f}c, sigma_noise={sn*100:.0f}c, trade if est>{thr*100:.0f}c: "
              f"selected {sel.mean():5.1%} of opps | mean est {est_e[sel].mean()*100:4.2f}c -> "
              f"mean TRUE {true_e[sel].mean()*100:4.2f}c  (realized = {ratio:4.0%} of estimated)")
    print(f"    [analytic shrinkage factor: {se**2/(se**2+sn**2):.0%}]")

print()
print("=" * 78)
print("F. CORRELATED PORTFOLIO — effective independent bets N_eff = N/(1+(N-1)rho)")
print("=" * 78)
for N in [20, 50, 100]:
    row = "  N=%3d: " % N
    for rho in [0.0, 0.05, 0.10, 0.20, 0.30, 0.50]:
        row += f"rho={rho:.2f}->{N/(1+(N-1)*rho):5.1f}  "
    print(row)

print()
print("=" * 78)
print("G. FLAGSHIP SIM — maker favorite-side basket")
print("   buy at 85c, true p=88% (3c gross), maker fee 0.22c -> net edge 2.78c")
print("   10 concurrent positions (2 clusters of 5, rho=0.20 in-cluster), 2% stake each")
print("   50 rounds x 10 = 500 settlements per path, 20,000 paths")
print("=" * 78)
c_g, p_g = 0.85, 0.88
fee_g = kalshi_maker_fee(c_g)
net_edge = p_g - c_g - fee_g
full_kelly_g = net_edge / (1 - c_g)
print(f"  net edge {net_edge*100:.2f}c | full Kelly {full_kelly_g:.1%} | a 2% stake = {0.02/full_kelly_g:.2f}x Kelly")

def run_portfolio(stake_frac, fee, n_paths=20000, n_rounds=50, n_conc=10, n_clus=2, rho=0.20,
                  c_=0.85, p_=0.88):
    per_clus = n_conc // n_clus
    W = np.ones(n_paths)
    percl = [W.copy()]
    thr = ND.inv_cdf(p_)          # win if latent z < thr  (P = p_)
    mdd_track_max = W.copy()
    mdd = np.zeros(n_paths)
    for r in range(n_rounds):
        Zc = rng.normal(size=(n_paths, n_clus))
        eps = rng.normal(size=(n_paths, n_clus, per_clus))
        lat = np.sqrt(rho) * Zc[:, :, None] + np.sqrt(1 - rho) * eps
        wins = (lat < thr)                     # marginal P(win)=p_
        n_w = wins.reshape(n_paths, -1).sum(axis=1)
        stake = stake_frac * W                 # per position, set at round start
        pnl_w = (1 - c_ - fee) / c_            # per $ staked, win
        pnl_l = -1.0                            # per $ staked, loss (price -> 0)
        W = W + stake * (n_w * pnl_w + (n_conc - n_w) * pnl_l)
        W = np.maximum(W, 1e-9)
        mdd_track_max = np.maximum(mdd_track_max, W)
        mdd = np.maximum(mdd, 1 - W / mdd_track_max)
        percl.append(W.copy())
    return np.array(percl), mdd

traj, mdd = run_portfolio(0.02, fee_g)
Wf = traj[-1]
print(f"  MAKER 2%/pos: median terminal {np.median(Wf):.3f}x | mean {Wf.mean():.3f}x | "
      f"p5 {np.percentile(Wf,5):.3f}x | p95 {np.percentile(Wf,95):.3f}x")
print(f"    P(below start after 500 bets) {np.mean(Wf<1):.1%} | median MaxDD {np.median(mdd):.1%} | "
      f"p95 MaxDD {np.percentile(mdd,95):.1%}")
gpb = np.log(np.median(Wf)) / 500
print(f"    implied growth {gpb*1e4:.1f} bp/bet (median)")

traj_t, mdd_t = run_portfolio(0.02, kalshi_taker_fee(c_g))
Wt = traj_t[-1]
print(f"  TAKER same trades (fee 0.89c, net edge {((p_g-c_g-kalshi_taker_fee(c_g))*100):.2f}c): "
      f"median terminal {np.median(Wt):.3f}x | P(loss) {np.mean(Wt<1):.1%}")

traj0, mdd0 = run_portfolio(0.02, fee_g, c_=0.85, p_=0.85 - 0.0022/1)  # zero-edge sanity (p=c+? -> p= c means -fee)
W0 = traj0[-1]
print(f"  NO-EDGE control (p=true price; you only pay fees): median terminal {np.median(W0):.3f}x "
      f"| P(loss) {np.mean(W0<1):.1%}")

# rho sensitivity
for rho_ in [0.0, 0.4]:
    tr, md = run_portfolio(0.02, fee_g, rho=rho_)
    Wr = tr[-1]
    print(f"  rho={rho_:.1f}: median {np.median(Wr):.3f}x | p5 {np.percentile(Wr,5):.3f}x | "
          f"P(loss) {np.mean(Wr<1):.1%} | p95 MaxDD {np.percentile(md,95):.1%}")

print()
print("=" * 78)
print("H. CROSS-VENUE ARB — buy YES@a (Kalshi taker) + buy NO@b (PM-US taker)")
print("   locked profit per $1 = 1 - a - b - feeK(a) - feePM(1-b ... use b price)")
print("=" * 78)
print(f"{'YES a':>6} {'NO b':>6} {'gross gap':>10} {'fees':>7} {'net':>7}")
for a, b in [(0.48, 0.48), (0.49, 0.48), (0.47, 0.48), (0.83, 0.13), (0.90, 0.06), (0.60, 0.36)]:
    fees = kalshi_taker_fee(a) + pmus_taker_fee(1 - b)
    grossgap = 1 - a - b
    net = grossgap - fees
    print(f"{a*100:5.0f}c {b*100:5.0f}c {grossgap*100:9.1f}c {fees*100:6.2f}c {net*100:+6.2f}c")
print("Same trade with Kalshi MAKER leg (rest, get filled) at 48/48:")
fees_m = kalshi_maker_fee(0.48) + pmus_taker_fee(0.52)
print(f"  fees {fees_m*100:.2f}c -> net at 4c gross gap: {(0.04-fees_m)*100:+.2f}c")

print()
print("=" * 78)
print("I. ADVERSE SELECTION (Glosten-Milgrom sketch) — break-even half-spread")
print("   s/2 >= mu/(1-mu) * L   (mu = informed share of your fills, L = loss to informed)")
print("=" * 78)
for mu in [0.02, 0.05, 0.10, 0.20]:
    for L in [0.05, 0.10]:
        print(f"  mu={mu:4.0%}, L={L*100:2.0f}c -> required half-spread {mu/(1-mu)*L*100:5.2f}c", end="   ")
    print()

print()
print("=" * 78)
print("J. SVG CHART DATA")
print("=" * 78)
# Chart A: growth vs Kelly multiple. plot area x:[60,700] y:[20,280] (px). m in [0,2.4], g in [-0.6%,0.6%]
def sx(v, lo, hi, x0=60, x1=700): return x0 + (v - lo) / (hi - lo) * (x1 - x0)
def sy(v, lo, hi, y0=280, y1=20): return y0 + (v - lo) / (hi - lo) * (y1 - y0)
pts = []
for m in np.arange(0, 2.401, 0.05):
    g = growth(m * fstar, 0.50, 0.55) * 100  # % per bet
    pts.append(f"{sx(m,0,2.4):.1f},{sy(g,-0.6,0.6):.1f}")
print("CHART_A_TRUE:", " ".join(pts))
pts = []
for m in np.arange(0, 2.401, 0.05):
    g = growth(m * fstar, 0.50, 0.525) * 100   # sized on estimate, true edge half
    pts.append(f"{sx(m,0,2.4):.1f},{sy(g,-0.6,0.6):.1f}")
print("CHART_A_HALF:", " ".join(pts))
print("A_markers: f*_x=", f"{sx(1.0,0,2.4):.1f}", " zero_y=", f"{sy(0,-0.6,0.6):.1f}",
      " peak_true=", f"{sx(1.0,0,2.4):.1f},{sy(growth(fstar,0.5,0.55)*100,-0.6,0.6):.1f}",
      " peak_half_x=", f"{sx(0.5,0,2.4):.1f}")

# Chart B: break-even edge vs price. x price 2..98c -> [60,700]; y 0..2c -> [280,20]
for name, fn in [("TAKER_K", kalshi_taker_fee), ("MAKER_K", kalshi_maker_fee), ("TAKER_PM", pmus_taker_fee)]:
    pts = []
    for cc in np.arange(0.02, 0.981, 0.02):
        pts.append(f"{sx(cc,0.02,0.98):.1f},{sy(fn(cc)*100,0,2.0):.1f}")
    print(f"CHART_B_{name}:", " ".join(pts))

# Chart C: fan chart of flagship maker sim. x rounds 0..50 -> [60,700]; y multiple 0.7..2.0 -> [300,20]
qs = np.percentile(traj, [5, 25, 50, 75, 95], axis=1)  # traj shape (51, n_paths)
for i, q in enumerate([5, 25, 50, 75, 95]):
    pts = []
    for r in range(51):
        pts.append(f"{sx(r,0,50):.1f},{sy(qs[i][r],0.7,2.0,300,20):.1f}")
    print(f"CHART_C_P{q}:", " ".join(pts))
print("C_y_gridlines:", {v: f"{sy(v,0.7,2.0,300,20):.1f}" for v in [0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0]})
print("A_x_ticks:", {v: f"{sx(v,0,2.4):.1f}" for v in [0, 0.5, 1.0, 1.5, 2.0]})
print("A_y_grid:", {v: f"{sy(v,-0.6,0.6):.1f}" for v in [-0.5, -0.25, 0, 0.25, 0.5]})
print("B_x_ticks:", {v: f"{sx(v,0.02,0.98):.1f}" for v in [0.1, 0.3, 0.5, 0.7, 0.9]})
print("B_y_grid:", {v: f"{sy(v,0,2.0):.1f}" for v in [0.5, 1.0, 1.5, 2.0]})
print("C_x_ticks:", {v: f"{sx(v,0,50):.1f}" for v in [0, 10, 20, 30, 40, 50]})
