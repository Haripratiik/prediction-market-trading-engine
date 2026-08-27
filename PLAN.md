# PLAN.md — Systematic Prediction Market Trading Operation

```yaml
version: 1.0
compiled: 2026-08-25
operator_jurisdiction: US / Georgia
venues: [kalshi, polymarket_us, forecastex]
language: python>=3.13
status: pre-Gate-0
progression_model: gate-based    # NEVER calendar-based
companion_docs:
  research_report: https://claude.ai/code/artifact/db5e1395-1acc-45c2-b606-26a2838efac7
  math_report: https://claude.ai/code/artifact/743ab202-4082-4145-8d8d-c39815409c64
  raw_notes: research/01..05-*.md
  live_recon: research/05-live-recon-findings.md   # MEASURED - overrides earlier estimates
  kalshi_structure: research/06-kalshi-structure.md # API/spec-level operational reference
  microstructure: research/07-microstructure.md     # quoting, queues, fills, impact
  statistics: research/08-statistical-methods.md    # calibration, sequential inference, Kelly, validity
  simulations: research/quant/quant_research.py
```

---

## §0 — AGENT OPERATING INSTRUCTIONS

### 0.1 How to use this file

This document is the **single source of truth** for the project. It is written for an AI coding agent
executing tasks with minimal human clarification.

- Every section has a stable ID (`§3.2`, `T-014`). Reference these in commits and PRs.
- **Task backlog is §14.** Work top-down. Each task has explicit *acceptance criteria*; a task is done
  only when every criterion is mechanically verifiable (a test passes, a file exists with a property).
- Constants and formulas in §2 are **canonical** — implement them exactly, do not re-derive or "improve"
  them. If a formula appears wrong, open an issue; do not silently change it.
- When this file conflicts with a code comment, **this file wins**. When it conflicts with a venue's live
  API documentation, **the API documentation wins** — and this file must be updated in the same PR.
- **When this file conflicts with a passing test in `tests/`, the TEST wins.** The canonical tables in §2
  are encoded as regression fixtures; if a number here disagrees with a fixture, the number here is the
  suspect. Two such errors have already been caught this way and are recorded in §C.

### 0.2 Invariants — never violate without an explicit written exception

```yaml
I1_maker_default:       All orders are post-only unless the sleeve spec grants taker permission.
I2_shrunk_edge:         Sizing NEVER uses a raw model edge. Always lambda * edge. Default lambda = 0.5.
I3_risk_in_executor:    Risk limits are enforced in the executor process, never inside a strategy module.
I4_db_is_truth:         Position/PnL state derives from persisted fills, never from in-memory counters.
I5_no_live_before_gate: No live order may be sent by a sleeve that has not passed Gate 4 (see section 8).
I6_pre_registration:    No strategy is evaluated with a test written after the data was seen.
I7_rulebook_read:       No market enters any universe until its rules text is fetched, hashed, reviewed.
I8_one_account:         One account per venue per person. No shared funding. No VPN to offshore venues.
I9_kill_always_works:   A KILL file in the run dir cancels everything, from any state, within 5 seconds.
I10_no_timeline:        Progress is gated on criteria. Never schedule or promise by date.
```

### 0.3 Conventions

| Thing | Convention |
|---|---|
| Prices | Integer cents `1..99` (`int`) in the DB. Never floats in storage. |
| Money | Integer cents. Convert at display only. |
| Probabilities | `float` in `[0,1]`, never percent. |
| Time | UTC, timezone-aware. Store as INTEGER epoch-microseconds. |
| Venue enum | `"kalshi" \| "polymarket_us" \| "forecastex" \| "manifold"` |
| Side | `"yes" \| "no"` — normalize everything to YES-referenced internally. |
| Order identity | `client_order_id` = UUIDv4, generated before send, used as idempotency key. |
| Config | Pydantic models loaded from `config/*.yaml`. No magic numbers in code. |
| Logging | `structlog` JSON to stdout + rotating file. Every order decision logged with full context. |
| Tests | `pytest`. Coverage gate 80% on `core/` and `risk/`. |

### 0.4 Glossary

| Term | Definition |
|---|---|
| **Sleeve** | Independently-accounted strategy with its own bankroll slice, gate status, kill criteria. |
| **Edge** | `p_model - price - fee`, dollars per $1-payout contract. |
| **Shrunk edge** | `lambda * raw_edge`. The only edge used for sizing (I2). |
| **Theme** | Cluster of markets sharing an underlying uncertainty (one election, one storm, one CPI print). |
| **Mark-out** | Change in fair price at t+5min / t+1h after a fill. Measures adverse selection. |
| **Shadow mode** | Full pipeline computing real orders against live books, logging instead of sending. |
| **Canary** | Smallest-size live trading, one sleeve, post-Gate-4. |
| **Dutch book** | Buying all outcomes of a mutually-exclusive set for less than the guaranteed payout. |
| **Implication violation** | `P(A)` priced below `P(B)` when event A logically contains event B. |
| **N_eff** | Effective independent bets, `N / (1 + (N-1) * rho)`. |
| **Correlation-of-definition risk** | Two markets you treat as linked whose rulebooks do not actually agree. |

---

## §1 — OBJECTIVE, CONSTRAINTS, DEFINITION OF SUCCESS

### 1.1 Objective

Build an automated system that extracts **structural and relative-value edges** from US-regulated
prediction markets, at a capital scale where those edges have not been arbitraged away, with rigorous
statistical validation before capital is risked.

### 1.2 Hard constraints

```yaml
capital:            risk capital only; total loss must be survivable without lifestyle change
venues:             Kalshi + Polymarket US (+ ForecastEx optional). Offshore Polymarket FORBIDDEN.
legal:              Gate 0 compliance clearance required before any live order (section 13)
execution_hardware: development on Windows; production on a us-east Linux VPS
edge_source:        structural and relative value FIRST; forecasting alpha LAST
capacity_ceiling:   resting <= 20% of touch depth; taking <= 5% of recent volume
```

### 1.3 Definition of success, in priority order

1. **A working, monitored, safe system** — never sends an unintended order, never exceeds a limit, always
   recoverable. This is success even at zero P&L.
2. **A validated edge** — at least one sleeve whose realized net-edge confidence interval excludes zero
   over its pre-registered sample.
3. **Positive risk-adjusted return** net of fees, taxes, and time.

Explicit non-goals: beating the market on forecasting skill; competing on latency; maximizing volume;
trading every category.

---

## §2 — QUANTITATIVE FOUNDATION (CANONICAL)

All results below were computed, not asserted. Source: `research/quant/quant_research.py`
(NumPy, seed 42, 20,000 paths per experiment). Raw output: `research/quant/results.txt`.

### 2.1 Contract algebra and fee model

```python
# core/math/contracts.py — implement EXACTLY

# Kalshi fees are PER SERIES, read from Series.fee_type + Series.fee_multiplier.
# MEASURED across all 13,486 series (research/06-kalshi-structure.md section 4):
#   fee_type = "quadratic"                       13,353 series -> MAKERS PAY ZERO
#   fee_type = "quadratic_with_maker_fees"          130 series -> maker = 0.25 x base
#   fee_type = "quadratic_with_combo_maker_fees"      3 series -> maker = 0.50 x base (MVE)
#   fee_multiplier: 1.0 on 13,453 | 0.5 on 19 (MLB) | 0.0 on 14 (FEE-FREE)
KALSHI_BASE_TAKER = 0.07          # UNVERIFIED against the fee-schedule PDF (429 on every fetch)
KALSHI_MAKER_RATIO = {"quadratic": 0.0,
                      "quadratic_with_maker_fees": 0.25,
                      "quadratic_with_combo_maker_fees": 0.50}

def kalshi_fee(price, is_maker, fee_type, fee_multiplier):
    base = KALSHI_BASE_TAKER * fee_multiplier
    theta = base * (KALSHI_MAKER_RATIO[fee_type] if is_maker else 1.0)
    return theta * price * (1.0 - price)

THETA = {                                  # non-Kalshi venues keep the flat model
    ("polymarket_us", False): 0.06,        # taker
    ("polymarket_us", True ): -0.0125,     # NEGATIVE = rebate paid to you
    ("forecastex",    False): 0.0,         # $0.01 embedded in spread, modeled separately
    ("forecastex",    True ): 0.0,
}

def fee(price: float, venue: str, is_maker: bool, **kw) -> float:
    """Fee in dollars per contract. price in (0,1). Parabolic, peaks at 0.50.
    For Kalshi, kw MUST carry fee_type and fee_multiplier from the series record;
    a flat 0.0175 maker assumption overstates cost on 99% of the venue."""
    if venue == "kalshi":
        return kalshi_fee(price, is_maker, kw["fee_type"], kw["fee_multiplier"])
    return THETA[(venue, is_maker)] * price * (1.0 - price)

def edge(p_model: float, price: float, venue: str, is_maker: bool) -> float:
    """Expected value per contract, held to settlement."""
    return p_model - price - fee(price, venue, is_maker)

def variance(p: float) -> float:
    """TOTAL remaining settlement variance of a binary. Closed form, model-free, and
    with NO dependence on time to expiry: since p_T^2 = p_T, Var(p_T|F_t) = p(1-p)."""
    return p * (1.0 - p)   # sd ~= 0.50 at p=0.5 — ten times a typical edge

# THREE separate quantities all scale as p(1-p). They are all second moments of a
# Bernoulli, so this is structural rather than coincidence:
#     remaining variance      p(1-p)
#     Glosten-Milgrom spread  4*mu*p(1-p) / (1 - mu^2 (2p-1)^2)
#     venue fee               feeRate * p(1-p)
# CONSEQUENCE: quote spreads proportional to p(1-p), and normalise every risk, fee and
# signal measurement by p(1-p) or work in log-odds. Basis points are the WRONG unit --
# relative spread varies 50x across the book purely mechanically, and a 1c tick at
# p=0.02 carries 10.4x the log-odds information of a 1c tick at p=0.50.
```

**Canonical fee table** (break-even edge in cents, hold-to-settlement, one fee):

| Price | Kalshi taker | Kalshi maker | PM-US taker | PM-US maker | Kalshi taker as % of stake |
|------:|-------------:|-------------:|------------:|------------:|---------------------------:|
| 5c | 0.33 | 0.08 | 0.29 | −0.06 | 6.65% |
| 10c | 0.63 | 0.16 | 0.54 | −0.11 | 6.30% |
| 20c | 1.12 | 0.28 | 0.96 | −0.20 | 5.60% |
| 30c | 1.47 | 0.37 | 1.26 | −0.26 | 4.90% |
| 40c | 1.68 | 0.42 | 1.44 | −0.30 | 4.20% |
| 50c | 1.75 | 0.44 | 1.50 | −0.31 | 3.50% |
| 60c | 1.68 | 0.42 | 1.44 | −0.30 | 2.80% |
| 70c | 1.47 | 0.37 | 1.26 | −0.26 | 2.10% |
| 80c | 1.12 | 0.28 | 0.96 | −0.20 | 1.40% |
| 90c | 0.63 | 0.16 | 0.54 | −0.11 | 0.70% |
| 95c | 0.33 | 0.08 | 0.29 | −0.06 | 0.35% |

Rules derived from this table, to be enforced in code:

- `R2.1a` — Round-trip (exit before settlement) doubles the fee. Early exits require sleeve permission.
- `R2.1b` — **Fee ratio screen**: reject any entry where `fee / price > 0.04`.
  Note the algebra, because an earlier draft of this plan got it wrong: `fee/price = theta*(1-p)`, which is
  **linear and decreasing** in price, running 6.65% at 5c to 3.50% at 50c. It does **not** explode on cheap
  contracts. With the default 0.04 limit and Kalshi taker fees the boundary is **42.9c** (`1 - 0.04/0.07`),
  so the rule excludes *all taker entries below mid* — consistent with the maker-first doctrine, but far
  broader than a cheap-contracts screen. For a maker on a `quadratic` series the fee is zero and the rule
  never binds. **What actually makes cheap contracts lethal is the favourite-longshot bias (sub-10c buyers
  lose >60% of stake), not the fee ratio — keep the two arguments separate.**
- `R2.1c` — Maker legs change viability by roughly 4x. Every RV or arbitrage structure is evaluated maker-first (2.6).

### 2.2 Sizing: Kelly with shrinkage

```python
# core/math/sizing.py

def kelly_fraction(p: float, price: float) -> float:
    """Full-Kelly fraction of bankroll for a binary at `price` with true probability `p`."""
    return (p - price) / (1.0 - price)

def growth(f: float, price: float, p: float) -> float:
    """Log growth per bet at stake fraction f."""
    if f <= 0:
        return 0.0
    return p * math.log(1 + f * (1 - price) / price) + (1 - p) * math.log(1 - f)

def position_fraction(p_model, price, venue, is_maker, lam, kelly_mult, cap) -> float:
    """THE sizing function. Every sleeve calls this. Never bypass it."""
    raw = edge(p_model, price, venue, is_maker)
    if raw <= 0:
        return 0.0
    p_shrunk = price + lam * raw          # I2: shrink BEFORE sizing
    f_star = kelly_fraction(p_shrunk, price)
    return max(0.0, min(kelly_mult * f_star, cap))
```

**Canonical defaults:** `lam = 0.5`, `kelly_mult = 0.25`, `cap = 0.02` (`cap = 0.01` during Gate 4).

**Growth curve** (computed; price 50c, estimated edge 5c, so estimated full Kelly = 10% stake):

| Kelly multiple (of *estimated*) | growth if true edge = 5c | growth if true edge = 2.5c |
|---:|---:|---:|
| 0.25x | +21.9 bp/bet | **+9.4 bp/bet** |
| 0.50x | +37.5 bp/bet | **+12.5 bp/bet** (max) |
| 1.00x | +50.1 bp/bet (max) | **−0.1 bp/bet** |
| 1.50x | +37.4 bp/bet | **−38.2 bp/bet** |
| 2.00x | **−1.4 bp/bet** (zero-growth point) | — |

Reading: growth hits zero at 2x Kelly. If the true edge is half your estimate, full-Kelly-on-your-estimate
earns **nothing**, half-of-estimate is the true-edge optimum, and quarter-of-estimate is true half Kelly —
75% of available growth at half the volatility. Hence the defaults above.

### 2.3a The KL identity — growth, skill, and evidence are ONE number

```
KL(q||m) = q*log(q/m) + (1-q)*log((1-q)/(1-m))
```

This single quantity is simultaneously **(A)** your Kelly growth rate, **(B)** your log-score edge over the
market, and **(C)** the growth rate of the e-process that proves you have an edge (research/08 section 0,
all three verified numerically). Two consequences used throughout this plan:

```
N ~ log(1/alpha) / KL(q||m)     settled markets before your edge is provable
N >= 4 / delta^2                settled markets to beat the market at t=2, delta = typical |q - m|
```

| your q vs m=0.50 | markets to prove edge | | typical disagreement | markets for t=2 |
|---|---:|---|---|---:|
| 0.52 | 3,744 | | 2 points | 10,000 |
| 0.55 | 598 | | 5 points | 1,600 |
| 0.60 | 149 | | 10 points | 400 |

**Capital velocity.** Prediction-market collateral is locked until resolution, so the growth rate per unit of
*time* is `KL(q||m) / T_resolution`, not `KL(q||m)`. **A 2% edge resolving in a week dominates a 6% edge
resolving in a year.** Rank opportunities by `KL/T`, and treat the budget constraint as binding across
*overlapping resolution windows*, not per market.

### 2.3 Why lambda = 0.5 — shrinkage is a theorem, not humility

```
E[true_edge | estimate] = lambda * estimate,    lambda = sigma_e^2 / (sigma_e^2 + sigma_n^2)
```

Simulated over 2,000,000 opportunities, selecting only estimates above a trading threshold:

| sigma_edge | sigma_noise | mean estimated edge (traded) | mean TRUE edge | realized / estimated |
|---:|---:|---:|---:|---:|
| 3c | 3c | 4.75c | 2.38c | **50%** |
| 3c | 2c | 4.26c | 2.95c | 69% |
| 2c | 4c | 4.93c | 0.99c | **20%** |

The third row is the trap: in a category where the market is sharp (crypto and short-horizon finance are
near-perfectly calibrated), a noisy model's "5c edges" are really 1c — below the taker fee floor.

**Correction to the usual rationale.** The common argument "parameter uncertainty implies shrinkage" is
**wrong** for log utility: `E[log W]` is linear in the outcome probability, so under a *correct posterior*
the optimal bet uses the posterior **mean**, unshrunk. The real mechanisms are (a) **selection** — you trade
where your model disagrees most with the price, which is where it is most likely wrong (this is what the
model below fixes), and (b) **induced correlation** — uncertainty in a shared edge parameter makes outcomes
positively correlated in the posterior predictive, and positive correlation reduces optimal leverage (2.7).
Implement (b) as a hierarchical scenario generator: draw the edge parameter from its posterior, *then* draw
outcomes. Sizing becomes conservative automatically, **with no ad hoc haircut anywhere**.

### 2.3b THE EDGE MODEL — estimate lambda, do not guess it

Replace the guessed shrinkage factor with a **forecast-encompassing regression in logit space**:

```
logit( P(y=1) ) = logit(m) + beta_c * ( logit(q) - logit(m) ) + alpha_c
```

- `beta_c = 0` -> your disagreement with the market is pure noise. **No edge. Stop trading that category.**
- `beta_c = 1` -> your forecast is exactly right and the market is wrong.
- `beta_c = 0.5` -> literally "my edge is half what I think".

**`beta_c` IS the lambda of I2 — but estimated from data, per category, with uncertainty.** Fit it
hierarchically (partial pooling across categories); the empirical-Bayes closed form is enough before
reaching for MCMC:

```
mu0   = sum(beta_hat_c / SE_c^2) / sum(1 / SE_c^2)
tau2  = max( mean( (beta_hat_c - mu0)^2 - SE_c^2 ), eps )
w_c   = tau2 / (tau2 + SE_c^2)
beta_EB_c = w_c * beta_hat_c + (1 - w_c) * mu0
```

**Simulated payoff (12 categories, true beta around 0.45), mean log-growth per bet:**

| sizing policy | growth/bet | % of oracle |
|---|---:|---:|
| true beta (oracle) | +0.01860 | 100% |
| **partial pooling (EB)** | **+0.01488** | **80%** |
| no pooling (raw per-category MLE) | +0.01375 | 74% |
| half-Kelly heuristic on beta=1 | +0.01350 | 73% |
| **naive beta = 1 (trust your model)** | **+0.00096** | **5%** |

**Trusting your model at face value destroys 95% of achievable growth.** The crude half-Kelly heuristic
recovers most of it only *because* the true average beta happened to be ~0.42. Estimating beta hierarchically
beats the heuristic by ~10% **and tells you which categories to stop trading**.

- `R2.3a` — Each sleeve fits `beta_c` per category once >= 100 settlements exist, hierarchically pooled.
  Sizing uses `beta_EB_c`, not the default 0.5. A category whose posterior `beta_c` is not credibly above 0
  is **removed from the universe**.
- `R2.3b` — **Do not recalibrate below ~250 settled markets.** Simulated: at n=100 both Platt and isotonic
  recalibration made out-of-sample Brier *worse* than the raw model. Platt (logit slope+shift) beats
  isotonic unless n > 1,000 and the distortion is genuinely non-monotone.

### 2.4 Drawdown and risk of ruin

```
P(bankroll ever touches x * B0) ~= x^(2/m - 1)      # m = fraction of full Kelly
```

| Sizing | P(ever −50%) analytic | Monte Carlo | P(ever −90%) an./MC | median MaxDD over 2,000 bets |
|---|---:|---:|---:|---:|
| Full Kelly | 50.0% | 47.0% | 10.0% / 9.2% | 93.9% |
| Half Kelly | 12.5% | 11.7% | 0.10% / 0.07% | 68.4% |
| Quarter Kelly | 0.8% | 0.7% | ~0 / 0 | **41.0%** |

- `R2.4b` — **Inventory risk in a binary does NOT decay as expiry approaches.** In Avellaneda-Stoikov the
  inventory penalty `gamma*sigma^2*(T-t)` goes to zero at T because you liquidate at the mid. **A binary
  settles at 0 or 1 — there is no liquidation at the mid — so the penalty is `gamma*p(1-p)`, which has no
  time term at all.** A 50c contract one minute before settlement carries exactly the same per-contract risk
  as it did a month earlier. Time to expiry does not reduce inventory risk; only price convergence toward a
  boundary does. **Never let the quoter relax inventory discipline near the close** (research/07 section 2.1).
- `R2.4a` — Expect a ~41% peak-to-trough excursion at some point even with a *real* edge at quarter Kelly.
  Drawdown alone is not evidence of a broken strategy. The section 9 ladder responds anyway, because the
  competing hypothesis (the edge decayed) is always live.

### 2.5 Validation statistics

```
n = [ (z_alpha * sqrt(c(1-c)) + z_beta * sqrt(p(1-p))) / e ]^2      # one-sided binomial
```

| Price | Edge | n @ 80% power | n @ 95% power |
|---:|---:|---:|---:|
| 50c | 1c | 15,454 | 27,050 |
| 50c | 2c | 3,862 | 6,758 |
| 50c | 3c | 1,715 | 3,001 |
| 50c | 5c | 616 | 1,077 |
| 85c | 2c | 1,894 | 3,252 |
| 85c | 3c | **823** | 1,398 |
| 15c | 3c | 921 | 1,652 |

Favorites need fewer samples because outcome variance `p(1-p)` shrinks at the extremes — a second,
independent reason S1 trades the 70-95c band.

**Peeking penalty (simulated).** A strategy with *zero* edge, tested naively at alpha = 5% every 50 trades,
is declared significant at some point within 2,000 trades **25.4%** of the time, versus the promised 5.0%
for a single fixed-n test.

Under *continuous* monitoring it is worse, and it **does not converge to any error rate — it grows with the
horizon** (verified, 40,000 reps, Bernoulli(0.5), two-sided nominal 5%):

| observations monitored | P(ever reject) |
|---:|---:|
| 100 | 0.3630 |
| 1,000 | 0.5250 |
| 10,000 | 0.6472 |
| **100,000** | **0.7389** |

By the law of the iterated logarithm this climbs toward 1.0 and never plateaus. **There is no horizon at
which a continuously-monitored fixed-n test controls error** — which is the whole reason for `R2.5b`.

- `R2.5a` — Pre-register `{hypothesis, universe, price_band, n_required, test, alpha, kill_threshold}` in
  `sleeves/<id>/PREREGISTRATION.md`, committed and dated, before the first order.
- `R2.5b` — **Monitor continuously with a beta-binomial e-process, not a fixed-n test.** By Ville's
  inequality, "reject when the e-process ever exceeds `1/alpha`" is anytime-valid: optional stopping and
  optional continuation are free. For Bernoulli outcomes it is four lines:

```python
from scipy.special import betaln
log_e = betaln(a+S, b+t-S) - betaln(a, b) - S*np.log(p0) - (t-S)*np.log1p(-p0)
```

  Verified: `E[E_t] <= 1` at every horizon, realized false-positive rate 0.041 against a 0.05 target. Naive
  continuous monitoring, by contrast, **does not converge to any error rate — it grows with the horizon**
  (40,000 reps: 0.363 by 100 observations, 0.525 by 1,000, 0.647 by 10,000, **0.739 by 100,000**, climbing
  toward 1.0 by the law of the iterated logarithm). The price of anytime validity is ~1.5-2x the fixed-n confidence
  width, or ~1.8x the observations to first detection — worth paying. Report the running **confidence
  sequence** as the live edge estimate; it is valid at every instant, including the instant you decide to
  size up or kill the sleeve.
- `R2.5c` — **Combine evidence across sleeves with e-values, not p-values.** Independent: `E = prod(E_k)`.
  **Arbitrary dependence: `E = mean(E_k)`, with no correction at all** — this has no p-value analogue and
  matters enormously here, because sleeves share market exposure in ways you cannot model. For FDR across K
  sleeves use **e-BH**: sort `e_[1] >= ... >= e_[K]`, take `k* = max{k : k*e_[k]/K >= 1/alpha}`, reject the
  `k*` largest. Also controls FDR under arbitrary dependence with no correction.
- `R2.5e` — **Backtest-selection gates.** Log **every** configuration ever tried; the deflated Sharpe ratio
  is useless if you cannot count trials honestly. Simulated under the global null at T=250, an uncorrected
  test on the best of 50 configurations is wrong **92% of the time**. Required per-bet Sharpe under the
  null: T=250 / N=100 needs **SR > 0.160**; T=1,000 / N=100 needs 0.080. Minimum backtest length is
  `< 2 ln N / E[max_N]^2` — **five years of data buys ~45 independent configurations, and after seven the
  expected in-sample max Sharpe on a two-year backtest is already 1.0. Check this before searching, not
  after.** Gate on **DSR > 0.95 and PBO < 0.2** (CSCV, S=16). DSR is markedly conservative (0.1% actual vs
  5% nominal), so treat it as a screening gate, not a p-value.
- `R2.5d` — **Brier skill versus market is the primary continuous metric.** It converges far faster than
  P&L. A sleeve whose model Brier is worse than the market price's Brier has negative expected edge
  regardless of its P&L to date.

### 2.6 Microstructure math

**Adverse selection (Glosten-Milgrom).** Minimum half-spread to break even when a fraction `mu` of your
fills are informed and cost `L` each:

```
half_spread >= mu / (1 - mu) * L - rebate
```

| mu | L = 5c | L = 10c |
|---:|---:|---:|
| 2% | 0.10c | 0.20c |
| 5% | 0.26c | 0.53c |
| 10% | 0.56c | **1.11c** |
| 20% | 1.25c | 2.50c |

Default quoting assumption until measured: `mu = 0.10`, `L = 0.10` → half-spread >= 1.11c.

**Minimum-variance hedge ratio.** `h* = rho * sigma_A / sigma_B`; residual variance fraction `1 - rho^2`:

| rho | variance removed | residual sd (% of unhedged) |
|---:|---:|---:|
| 0.3 | 9% | 95% |
| 0.5 | 25% | 87% |
| 0.7 | 49% | 71% |
| 0.8 | **64%** | 60% |
| 0.9 | 81% | 44% |

- `R2.6a` — **Do not hedge below rho = 0.8.** Below that, variance reduction does not pay for a full extra
  leg of fees and spread. Prefer diversification across uncorrelated themes (2.7) — it is cheaper.

**N-outcome Dutch book fee hurdle** (total fees to buy all N outcomes of a near-complete book):

| N outcomes | Kalshi taker | PM-US taker | Kalshi maker |
|---:|---:|---:|---:|
| 2 | 3.50c | 3.00c | **0.87c** |
| 3 | 4.59c | 3.94c | 1.15c |
| 5 | 5.47c | 4.69c | **1.37c** |
| 8 | 5.97c | 5.11c | 1.49c |
| 12 | 6.24c | 5.35c | 1.56c |

Reading: a 5-outcome Kalshi Dutch book needs `sum(YES) < 94.5c` as taker but `< 98.6c` as maker.
**The maker version has several times the opportunity frequency.** This is the single most consequential
number in the strategy discussion (3.0).

**Pair relative-value break-evens** (two legs, same venue):

| Legs | taker + taker | maker + taker | maker + maker |
|---|---:|---:|---:|
| 60c / 55c | 3.41c | 2.15c | **0.85c** |
| 85c / 80c | 2.01c | 1.34c | **0.50c** |
| 30c / 25c | 2.78c | 1.68c | **0.70c** |

A 5c implication violation nets roughly 4.1c as a double-maker structure. As a double-taker it nets +1.6c at reference prices, and −0.4c once you cross 1c of spread on each leg to actually transact — which is the number that matters, because a taker does not get reference prices.

### 2.7 Portfolio correlation

```
N_eff = N / (1 + (N-1) * rho)          lim as N -> infinity  =  1 / rho
```

| N | rho=0 | 0.05 | 0.10 | 0.20 | 0.30 | 0.50 |
|---:|---:|---:|---:|---:|---:|---:|
| 20 | 20.0 | 10.3 | 6.9 | 4.2 | 3.0 | 1.9 |
| 50 | 50.0 | 14.5 | 8.5 | 4.6 | 3.2 | 2.0 |
| 100 | 100.0 | 16.8 | 9.2 | 4.8 | 3.3 | 2.0 |

- `R2.7a` — Assume intra-theme `rho >= 0.5` (a theme is at best ~2 effective bets); cross-theme `rho = 0.05-0.10`.
- `R2.7b` — Maintain `N_eff >= 8` whenever deployment exceeds 20% of bankroll. More tickers inside one theme
  does not help; more *themes* does.
- `R2.7d` — **Never estimate outcome dependence with the phi coefficient.** Phi is bounded by the marginals
  (Prentice bound) and understates dependence badly at asymmetric prices. Verified case: at
  `p_X=0.05, p_Y=0.60` with genuine latent `rho = 0.70`, `phi = 0.1818` while `phi_max = 0.1873` —
  **phi sits at 97% of its structural ceiling while reading as "basically independent".** Any risk model
  built on a phi matrix systematically understates concentration risk. Use **tetrachoric** correlation
  (latent-normal MLE, Haldane +0.5 for zero cells, fast Gauss-Legendre `Phi2`).
- `R2.7e` — **Pairwise tetrachoric SE is ~0.14 at n=100 settled events** — you cannot distinguish rho=0.3
  from rho=0.5. And the raw sample correlation matrix needs `n >= 4p` to be usable at all (at `n <= p` it is
  numerically singular and any inverse is noise amplification). Therefore: **shrink** (Ledoit-Wolf or OAS
  cut Frobenius error 23% and the condition number ~40x), then project to the nearest correlation matrix
  (Higham with Dykstra correction). **Use clustering for structure discovery and shrinkage for anything
  requiring an inverse** — clustering recovered the true themes with ARI 1.000 even from raw phi.
- `R2.7f` — **Multi-market Kelly is NOT the sum of individual Kellys.** 10 independent markets each with
  `f* = 0.20` sum to 200% of bankroll; the joint optimizer allocates 0.0999 each. Solve
  `max E[log(1 + sum f_j r_j)] s.t. sum f_j <= 1` by sample-average approximation over copula-drawn
  scenarios (cvxpy, CLARABEL). **And correlation bites far harder than it looks:**

| latent rho | observed binary phi | per-market f | growth vs independent |
|---:|---:|---:|---:|
| 0.00 | -0.002 | 0.0999 | 1.000 |
| 0.20 | 0.123 | 0.0805 | 0.604 |
| **0.40** | **0.258** | **0.0562** | **0.407** |
| 0.60 | 0.407 | 0.0408 | 0.285 |

  A latent correlation of 0.4 — observed phi of only 0.26 — **cuts per-market size 44% and growth to 41%**.
  Combined with R2.7d, this is the single largest sizing error available in this asset class.
- `R2.7g` — **You cannot hedge longshots against each other.** For Bernoulli marginals the reachable
  negative correlation is tiny: two markets each at `p = 0.02` cannot have binary correlation below
  **-0.0204**, in any distribution. Diversification across themes is the only variance reduction available
  in the tails.

### 2.7b Market impact — the constraint retail bots violate most

Square-root impact, adapted to binaries (absolute price, remaining sd):

```
delta_P = Y * sqrt(p(1-p)) * sqrt(Q/V)        Y ~ 0.5-1.0
```

Against Kalshi's actual size distribution (median $8,982 staked ~ 18,000 contracts):

| p | market size | order Q | impact (Y=0.5) | impact (Y=1.0) |
|---:|---:|---:|---:|---:|
| 0.50 | 18,000 | 100 | 1.86c | 3.73c |
| 0.50 | 18,000 | **500** | **4.17c** | **8.33c** |
| 0.50 | 18,000 | 2,000 | 8.33c | 16.67c |

> **A 500-contract order — $250 at 50c — moves the median Kalshi market 4-8 cents.**
> At retail scale **you are already a large trader in the median prediction market.**

- `R2.7c` — Size relative to **market volume**, not to bankroll. Hard gate: order size
  <= 0.5% of the market's expected lifetime volume. Calibrated impact exponent is **3/5, not 1/2**
  (Almgren et al. 2005 reject beta=1/2 at 95%); refit `eta` on your own fills.

### 2.8 Flagship simulation — reference expectations

Setup: maker basket buying at 85c where true p = 88% (3c gross edge); Kalshi maker fee 0.22c leaves a
**2.78c net edge**; full Kelly would be 18.5%, so a 2% stake is **0.11x Kelly**; 10 concurrent positions in
2 clusters (rho = 0.20 in-cluster); 500 settlements; 20,000 paths.

| Variant | median terminal | P(below start) | median MaxDD | p5 | p95 |
|---|---:|---:|---:|---:|---:|
| **Maker, 2% stakes** | **1.366x** | **6.3%** | 11.9% | 0.977x | 1.858x |
| Identical trades as taker | 1.274x | 11.4% | — | — | — |
| **Zero-edge control** | **0.933x** | **61.4%** | — | — | — |
| rho = 0.0 | 1.372x | 3.5% | p95 MaxDD 18.9% | 1.030x | — |
| rho = 0.4 | 1.365x | 9.9% | p95 MaxDD 29.3% | 0.918x | — |

Key readings:

- Maker discipline is worth roughly 10 points per 500 settlements **from fees alone**.
- The zero-edge control **loses 6.7% at the median and finishes down 61% of the time**. Running this
  machine without a validated edge is a slow, near-certain grind toward nothing. This is the entire
  justification for the gate system in section 8.
- Correlation barely moves the median but nearly triples P(loss) and inflates tail drawdown. Correlation
  is a **tail** risk, invisible in average-case backtests.
---

## §3 — STRATEGY PORTFOLIO

### 3.0 Strategy selection rationale — why relative value is the right core

**Thesis under evaluation:** *with limited capital, the best available edge is statistical arbitrage and
hedging between events or within events.*

**Verdict: substantially correct. It is adopted as the core of this plan — with five corrections that
change how it must be implemented.**

#### Why the thesis is right

| # | Reason | Supporting evidence |
|---|---|---|
| W1 | **Capacity works in your favor.** RV and arbitrage edges are capacity-limited, which is precisely why large firms ignore them. A $500/month opportunity is invisible to Jump Trading and material to a solo operator. | UCLA NBA microstructure study: combinatorial arb totals ~$560/month across 173 games; average executable size 14.8 shares |
| W2 | **No forecasting alpha required.** You are not claiming to know politics better than the market; you are enforcing internal consistency among its own prices. A far lower bar, and objectively verifiable. | Whelan et al.: takers average −31.5%; directional retail forecasting is a losing game |
| W3 | **Lower variance per unit of edge** → fewer samples to validate (2.5), smaller drawdowns, survivable on a small bankroll. | 2.5 sample table; 2.6 hedge table |
| W4 | **Binaries have guaranteed convergence.** Unlike equity pairs trading, where a spread can diverge indefinitely and margin-call you, a logically-linked prediction pair *must* converge at settlement. No borrow cost, no unlimited downside, known terminal date. | Structural property of binary contracts |
| W5 | **The money is demonstrably there.** ~$40M of arbitrage was extracted from Polymarket in a single year; 14 of the top 20 most profitable wallets are bots. | Saguillo et al., arXiv:2508.03474 |

#### Correction C1 — do not chase cross-venue latency arbitrage

Measured reality at the liquid end: single-market arbitrage is **virtually extinct** — 7 executable
episodes across 3,042 NBA markets in a month, median duration **3.6 seconds**, ~$210 total profit.
Combinatorial arbitrage produced **$559.59/month across all 173 games**, with **76.9% of episodes
liquidity-constrained**. Deviation half-lives collapsed to roughly 40 seconds during the 2024 election.

You cannot win a 3.6-second race from a retail API tier on a VPS. **S7 (cross-venue scanner) is therefore
retained as a monitor and signal generator, not as a business.**

#### Correction C2 — maker legs decide viability (the central implementation fact)

From 2.6, computed for this plan:

- 5-outcome Dutch book: profitable below **94.5c** as taker, below **98.6c** as maker.
- A 5c implication violation nets **4.1c** double-maker; double-taker it nets **+1.6c** at reference prices and **−0.4c** after crossing 1c of spread per leg. Maker-first is not a preference here, it is the difference between the trade existing and not.

The maker version of the same structure has several times the opportunity frequency and roughly triple the
margin. The correct formulation is therefore **not** "detect an arbitrage and hit it" but:

> **Post resting orders that only ever fill at prices which complete a profitable structure.**

This converts a latency race (which you lose) into a patience-and-inventory game (which you can win). It
is the difference between arbitrage-as-HFT and arbitrage-as-market-making, and it is the organizing idea
of S2 and S3.

#### Correction C3 — hedging is not free, and usually not worth it

At `rho = 0.5` a hedge removes only **25%** of variance while costing an entire extra leg of fees and
spread. Hedge only at `rho >= 0.8` (removes >= 64%), or where the hedge leg carries independent positive
edge. **Diversification across uncorrelated themes is a cheaper source of variance reduction** (2.7) — it
costs nothing but discipline.

#### Correction C4 — the real moat is semantic, not computational

Every pure-arbitrage bot matches markets by *title similarity*. The residual, durable opportunities are
exactly the ones where:

- titles differ but the rulebooks logically imply a relation (bots miss these), or
- titles match but the rulebooks **do not** actually agree — different settlement source, deadline,
  timezone, or edge-case clause (bots trade these and eventually lose on them).

Reading settlement rules carefully has **no latency requirement**, and is the one place where a careful
solo operator with LLM assistance for scale genuinely outperforms an HFT firm. **This is the moat.**

It also means the dominant risk in this family is not market risk but **correlation-of-definition risk** —
your hedge only works if the rulebooks actually say what you assumed. Section 5 makes rulebook equivalence
a hard, auditable gate rather than an assumption.

#### Correction C5 — RV alone cannot fill the book

Because capacity is small (W1 cuts both ways), pure RV will neither absorb a whole bankroll nor generate
settlements fast enough to validate quickly (2.5 requires hundreds to thousands). It must be paired with a
**passive structural sleeve (S1)** for volume and sample count, and an **income sleeve (S6)** that earns
while the others wait.

#### Resulting portfolio and build order

| ID | Sleeve | Family | Role | Forecasting needed? | Build order |
|---|---|---|---|---|---|
| **S1** | Structural maker basket | Behavioral / structural | Volume + sample engine | No | 1 |
| **S2** | Intra-event Dutch book | Pure arbitrage (within event) | High-conviction, low-frequency | No | 2 |
| **S3** | Linked-market relative value | Statistical arbitrage (between events) | **The core thesis** | No — logic only | 3 |
| **S6** | Liquidity provision | Income | Earns while others wait | No | 4 |
| **S7** | Cross-venue scanner | Monitor | Signal; rarely executed | No | 5 |
| **S4** | Weather distribution | Model | Optional, later | Yes | 6 |
| **S5** | Economic-data ladders | Model | Optional, later | Yes | 7 |

Ordering principle: **every non-forecasting sleeve is built before any forecasting sleeve.** S4 and S5 are
optional and may never be built; they exist as the natural growth path once infrastructure is in place and
the operator has real calibration data on their own judgment.

---

### 3.1 S1 — Structural maker basket

```yaml
id: S1
family: structural
venue: kalshi
execution: post-only (taker FORBIDDEN)
role: volume and sample engine
gate_entry: G2
```

**Thesis.** Cheap contracts are systematically overpriced and expensive ones underpriced; retail flow
supplies the error and makers harvest it. Three documented components, all independently measured:

1. Favorite-longshot bias — contracts above 70c carry statistically significant positive post-fee returns;
   sub-10c buyers lose over 60% of stake.
2. Single-name YES bias — in "Will [person] do X?" markets, traders buy YES ~61% of the time while YES
   resolves true only ~32% of the time.
3. Political underconfidence — a 70c political contract a week out is empirically ~83%.

**Universe filter.**

```yaml
venue: kalshi
price_band: [0.70, 0.95]        # in the side you BUY
exclude:
  - fee_ratio > 0.04            # R2.1b fee death zone
  - hours_to_close < 1          # bias collapses near expiry; slope -> 0.99
  - hours_to_close > 2160       # 90d; capital lockup cost dominates
  - touch_depth_usd < 200       # cannot get a meaningful fill
  - rules_hash not in reviewed  # I7
prefer_categories: [single_name, crypto_novelty, politics_long_horizon]
```

**Signal.**

```python
def s1_model_probability(market, now) -> float:
    p_raw = market.mid_price
    theta = THETA_BY_HORIZON[bucket(market.hours_to_close)]   # calibration recalibration
    p = p_raw**theta / (p_raw**theta + (1 - p_raw)**theta)
    if market.is_single_name:
        p = p - SINGLE_NAME_YES_ADJ      # config; default 0.03, applied against YES
    return clamp(p, 0.01, 0.99)
```

`THETA_BY_HORIZON` is fitted from recorded data during G2 and re-fitted every 500 settlements. The
published horizon slopes (0.99 under 1h rising to ~1.32 beyond a month) are the *prior*, not the value.

**~~First live-test venue — the fee-free corner.~~ WITHDRAWN — see §C errata E3.**
research/06 K3 reported 14 series carrying `fee_multiplier = 0`. Re-checked against the live API on
2026-08-26 while building the client: **there are none.** The live distribution is `{1.0: 13,499, 0.5: 19}`;
the 0.5 (MLB) cohort reproduces exactly, the zero cohort does not. The named tickers (`KXBTCY`, `KXETHY`,
`KXGDPYEAR`, …) all read `fee_multiplier = 1.0` today.

`tests/test_kalshi_client_live.py::test_fee_multiplier_distribution` asserts this and will fail loudly if
waivers return — at which point the strategy is live again. **Until then, plan on paying fees everywhere,
and pick the first live venue on liquidity and maker-fee status instead** (the ~99% of series with
`fee_type = "quadratic"`, where makers pay nothing, remains true and verified).

**Entry rule.** Rest a post-only bid when `shrunk_edge >= 2 * maker_fee(price)` and all universe filters
pass and risk (section 9) permits.

**Join the queue; do not penny.** With edge `E`, tick `D` and adverse selection `AS`, improving is only
justified when `alpha_improved / alpha_joined > (E - AS) / (E - D - AS)`:

| edge E | AS | required fill-probability ratio to justify improving |
|---:|---:|---|
| 1.5c | 0 | **3.00x** |
| 1.5c | 0.5c | **never** |
| 3.0c | 0.5c | 1.67x |
| 5.0c | 0.5c | 1.29x |

**On a 1c tick, unless the edge is >= 3c, improving must double or triple your fill probability to break
even — and it rarely does** (the entire front-to-back queue value measures 0.21-0.26 ticks). The exception
is a queue so long that `alpha_joined -> 0`; fit `alpha(q) = a_inf + (a_0 - a_inf)e^(-bq)` to realised fills
to get the critical length `Q*` above which improving wins.

**Exit rule.** Hold to settlement (default). Early exit only on: theme-limit breach, rulebook re-review
failure, or sleeve kill. Never on P&L feeling.

**Sizing.** `position_fraction(p_model, price, "kalshi", is_maker=True, lam=0.5, kelly_mult=0.25, cap=0.02)`.

**The inventory drift you must decide about, not discover.** The maker edge on Kalshi is not purely spread
capture — a material part is harvesting a directional behavioral bias. Maker share of purchases rises
monotonically with price (43.5% at 1-10c to 56.5% at 90-99c): makers systematically buy favorites, takers
systematically buy longshots and lose. **Your inventory will therefore drift systematically short YES in
longshot markets.** That is a real edge with negative skew. **Take it deliberately and size it, or hedge it
out — but never let it accumulate by accident.**

**And it inverts at the very end.** The "Yogi Berra effect" replicates on Kalshi, Betfair and Intrade: on
closing day, maker losses on cheap contracts become as bad as taker losses, and longshots generate
systematic losses *even for liquidity providers*. This is the empirical basis for the no-entry-inside-the-
final-hour rule above.

**Pre-registered test.** H0: win rate equals price-implied rate. Target detectable edge 3c at ~85c →
**n = 823** settlements at 80% power (2.5). Kill if, at n = 823: CI includes zero, OR model Brier is worse
than market-price Brier, OR fitted `lambda_hat < 0.3`.

**Capacity — MEASURED.** Live universe scan: 9,944 markets sit in the 70-95c band; 1,497 survive the
horizon and depth filters; **547 also have nonzero 24h volume**. Deployable at 20% of touch depth:
**~$198,800** — S1 is *not* depth-constrained at a five-figure bankroll. Their spreads are tight (median 2c)
and 24h volume is healthy (median 203 contracts).

**Composition caveat (important).** That universe is **Sports 401, Commodities 46, Crypto 30, Financials
22, Economics 19, Entertainment 12, Politics 5** — dominated by sports, *not* by the single-name and
long-horizon-politics categories where the documented bias is largest. Therefore: fit `THETA_BY_HORIZON`
and the single-name adjustment **per category**, and report sports vs non-sports edge separately. They are
two different strategies wearing one name, and pooling them will hide whichever one does not work.

Utilization = average resting size / touch depth; freeze at 50%.

---

### 3.2 S2 — Intra-event Dutch book (arbitrage within an event)

```yaml
id: S2
family: pure_arbitrage
venue: [kalshi, polymarket_us]
execution: maker-first; taker permitted ONLY to complete a partially-filled structure
role: high-conviction, low-frequency
gate_entry: G2
depends_on: [rulebook_equivalence_engine, multi_outcome_map]
```

**Thesis — and the direction matters more than the thesis.** In a mutually-exclusive outcome set, prices
should satisfy `sum(YES_i) = 1`. Books for individual outcomes are separate, so the identity is violated.
But the two sides of that violation are **not symmetric**, because Kalshi's `mutually_exclusive` flag
guarantees *at most one* YES and says nothing about *at least one*:

| Direction | Payoff | Verdict |
|---|---|---|
| **BUY** the basket (pay `sum(ask)`, collect $1 if a listed leg wins) | **$0 if nothing listed wins** | **UNSAFE** — needs independently verified exhaustiveness |
| **SELL** the basket (collect `sum(bid)`, pay **at most $1**) | liability capped at $1 regardless | **SAFE** — non-exhaustiveness makes it *better* |

**S2's primary direction is therefore SHORT the basket.** This is also where the density lives: measured
across the full live MECE universe, median `sum(ask) = 1.15` versus median `sum(bid) = 0.88` — books are
overround, and overround is collected by selling.

**Partial fills are bounded, not catastrophic.** Selling k of N legs leaves a short YES position on a
subset; max liability is still $1 (only one leg can win) and you keep the k premiums. Worst case is
`$1 - premium_collected` — an ordinary sold-longshot outcome, not a wipeout. Contrast the long basket,
where an unfilled leg destroys the structure.

**This unifies S1 and S2-short:** selling overpriced YES on longshot legs *is* the favorite-longshot trade,
executed at basket granularity, with the MECE structure supplying a hard $1 liability cap per event.
Account them together.

**The MECE test — a market set qualifies only if all five hold:**

1. Exactly one outcome can resolve YES. Kalshi publishes this per event as `mutually_exclusive` (6,088
   events, 48.5% of the universe, carry the flag) — read it, do not infer it.
2. **At least one outcome must resolve YES (exhaustiveness).** The exchange flag does **NOT** promise this,
   and that gap is the biggest trap in this sleeve — measured evidence below.
3. All outcomes settle from the **same source** at the **same deadline**.
4. Void/cancellation clauses are identical across outcomes.
5. Every leg has a real bid (a leg nobody bids cannot be rested into).

Failing any of these means it is not a Dutch book; it is an unhedged directional bet with extra steps.
This test is `rulebook_equivalence.check_mece()` and is a hard gate (I7).

> **MEASURED — see `research/05-live-recon-findings.md` F1.** 33 live events flagged `mutually_exclusive`
> price at `sum(YES ask) < 0.90`, showing apparent margins up to **+87c**. None are arbitrage. They are
> races whose listed outcomes are not exhaustive: "LA-01 Republican nominee?" lists 2 candidates summing to
> 12.5c; "Who will the next Pope be?" lists 7 at 28c; one NFL next-team market lists 32 at 32c. There is no
> Other/None leg. Buying every leg returns $0 whenever the winner is unlisted. Quoted legs equal total event
> markets in 99.3% of cases, so this is not a harvest artifact.
>
> **A naive scanner ranks these as its most profitable trades.** Of 47 fee-profitable taker structures found
> live, **33 were this trap.** Concrete gate: reject any structure with `sum(YES bid) < 0.80` unless an
> explicit Other/None leg exists, AND require the rules text to state that one listed outcome must occur.

**Signal.**

```python
def s2_scan(event) -> Opportunity | None:
    legs   = event.outcomes
    if not rulebook_equivalence.check_mece(legs):  return None
    # maker-first: what would we pay if every leg were a resting fill?
    # A leg with NO bid is NOT restable. Resting at 1c on a market nobody bids is
    # liquidity fantasy, and it is what makes naive scans report 78% "profitable".
    if any(leg.best_bid < TICK for leg in legs):  return None
    px     = [leg.best_bid + TICK if (leg.best_ask - leg.best_bid) > TICK else leg.best_bid
              for leg in legs]                                 # improve only if the spread allows
    fees   = sum(fee(p, event.venue, is_maker=True) for p in px)
    margin = 1.0 - sum(px) - fees
    if margin < MIN_MARGIN:  return None                       # default 0.005 ($0.005/contract)
    size   = min(leg.depth_at(px[i]) for i, leg in enumerate(legs)) * DEPTH_FRACTION
    return Opportunity(legs, px, margin, size)
```

**Execution protocol (the part that matters).**

1. Rest all N legs simultaneously as post-only.
2. Track a `structure_id`. As legs fill, `completion = filled_legs / N`.
3. If `completion >= COMPLETION_TAKER_THRESHOLD` (default 0.6) and the remaining legs are still
   collectively cheap enough that a **taker** completion preserves positive margin → cross the spread to
   finish. Otherwise wait.
4. `LEG_TIMEOUT` (default 900s) after the first fill: if the structure is still incomplete and cannot be
   completed profitably, **unwind the filled legs at maker prices if possible**, taker if the residual
   directional exposure exceeds `MAX_ORPHAN_EXPOSURE` (default 0.5% of bankroll).
5. Orphan risk is the sleeve's only real risk and it is logged and reported as its own KPI.

**Sizing when the structure is NOT locked (partial baskets, directional legs).** Do **not** apply the binary
Kelly formula per outcome. For a mutually-exclusive set the Smoczynski-Tomkins closed form applies: sort
outcomes by descending `pi_i / p_i`, add greedily while `pi_i/p_i` exceeds the reservation rate
`(1 - sum_{k in S} pi_k) / (1 - sum_{k in S} p_k)`, then

```
x_i = w * [ pi_i/p_i  -  ( sum_{k not in S} pi_k ) / ( 1 - sum_{k in S} p_k ) ]     for i in S
```

**Two verified surprises.** (1) Optimal Kelly **buys outcomes with negative expected value** — in the worked
example, three legs with `pi/p` of 0.962, 0.952 and 0.769 all get bought, because they are *hedges* that
raise wealth in states where the main bet loses, and log utility values that more than their EV cost.
(2) The naive per-outcome approach captures **only 65.2% of the optimal growth rate**, staking 14% of
bankroll where the optimum stakes 50%. Uniqueness requires `sum p_i > 1` — **the overround is what pins the
solution down**, which is exactly the regime this venue is in (median `sum(ask) = 1.15`).

**Sizing when the structure IS locked.** Near-riskless; the binding constraint is depth and capital lockup,
not Kelly. Cap at 5% of bankroll per structure, 15% of bankroll in S2 total; require annualized return on
locked capital `>= 15%` (`margin / days_to_settle * 365 / avg_price`) — otherwise the capital is better
used by S1.

**Kill criteria.** Any structure that resolves with a loss (i.e., the MECE test was wrong) triggers an
immediate sleeve halt and a written post-mortem before restart. Target: **zero** such events; one is a
process failure, not bad luck.

**n = 2 is NOT arbitrage.** For a two-outcome event, a "maker Dutch book" is resting a bid on both sides
inside the spread — that is S6 market making, and the margin is realized only if **both** legs fill. It
carries both-fill risk and is accounted to S6, never to S2. Genuine S2 arbitrage begins at n >= 3.

**Both directions, measured on the full live MECE universe (6,020 events):**

```
events with sum(bid) > 1.00  (SELL candidates, structurally safe) :  47   0.8%
events with sum(ask) < 1.00  (BUY candidates, need exhaustiveness):  90   1.5%

SELL INTO THE BID (immediate execution, taker fees) -> profitable: 0   <-- ZERO
```

**There is currently no free lunch available by crossing the spread in either direction.** The 959
"maker-profitable" resting-ask structures are not locked arbitrage: their margin *is* the overround, which
exists precisely because nobody crosses it. Realizing it requires a joint fill across legs, and the median
such structure has only 2 legs (i.e. it is two-sided quoting, sleeve S6).

**Long-side capacity — MEASURED** (liquidity filter: 24h volume > 0, min leg size >= 20 contracts, every leg
spread <= 10c, > 1h to close):

| | taker | maker |
|---|---:|---:|
| candidates | 5,826 | 4,568 (every leg has a real bid) |
| positive before fees | 89 | 3,880 |
| positive after fees | 47 (33 of them the F1 trap) | 3,793 |
| **+ liquidity filter** | **11** | **504** |
| of which n >= 3 (true arbitrage) | — | **147** |

Genuine n>=3 structures: **~$15,700 deployable at 20% of min-leg size, ~$282 one-shot profit if every
structure completes.** Composition: Sports 132, Elections 6, Politics 3, Financials 3, Weather 2. Small and
lumpy; expect long idle periods. This is precisely why S1 runs alongside (C5).

---

### 3.3 S3 — Linked-market relative value (the core statistical-arbitrage sleeve)

```yaml
id: S3
family: statistical_arbitrage
venue: [kalshi, polymarket_us]     # same-venue pairs first; cross-venue only after G5
execution: maker-first, both legs
role: the core thesis
gate_entry: G2
depends_on: [rulebook_equivalence_engine, link_graph]
```

**Thesis.** Markets that are logically related are priced on separate books with no cross-margining, so
their prices routinely violate the logical constraint that binds them. Unlike a forecast, the constraint is
*provable* — and unlike equity pairs trading, it is *guaranteed to converge at settlement* (W4).

**Link taxonomy** — the four relations the engine models, in decreasing order of confidence:

| Type | Constraint | Example | Trade when violated |
|---|---|---|---|
| **L1 Identity** | `P(A) = P(B)` | same event listed twice (different series, or two venues) | buy cheap side, sell rich side |
| **L2 Implication** | `A ⊆ B ⟹ P(A) <= P(B)` | "Wins by 10+" vs "Wins" | buy B, sell A when `P(A) > P(B)` |
| **L3 Partition** | `sum(P(A_i)) = P(B)` | monthly buckets vs the quarter | buy/sell the cheap/rich side of the identity |
| **L4 Bounded** | `\|P(A) − P(B)\| <= k` | consecutive thresholds ("above 3.0%" vs "above 3.1%") | trade the spread beyond the bound |

L1 and L2 are hard logical constraints and carry the highest conviction. L4 requires an assumption about
`k` and is therefore the weakest — it is genuinely statistical rather than arbitrage, and is sized at half.

**Signal.**

```python
def s3_scan(link: Link) -> Opportunity | None:
    if link.equivalence_status != "VERIFIED":  return None       # I7 / C4 — hard gate
    a, b = link.market_a, link.market_b
    violation = link.violation_size(a.book, b.book)              # in dollars, per constraint type
    # maker-first pricing: assume we rest on both legs
    cost = fee(a.px, a.venue, True) + fee(b.px, b.venue, True)
    net  = violation - cost
    if net < MIN_NET[link.type]:  return None                    # L1/L2: 0.01, L3: 0.015, L4: 0.02
    size = min(a.depth, b.depth) * DEPTH_FRACTION
    if link.type == "L4": size *= 0.5
    return Opportunity(...)
```

**Execution protocol.** Same two-leg discipline as S2: rest both legs; on a single-leg fill, either complete
as taker if margin survives, or unwind within `LEG_TIMEOUT`. **Never carry a naked leg past the timeout
because "it will probably converge anyway"** — that is how a relative-value book becomes a directional book.

**Why this is where the edge is, restated concretely.** A 5c implication violation between legs priced 60c
and 55c nets **4.1c as a double-maker structure** against a 0.85c fee hurdle — a 4.8:1 ratio of edge to
cost. The equivalent double-taker trade nets 1.5c against a 3.41c hurdle **at reference prices** — i.e. `5.00 − 3.4125 = +1.59c`, thin but positive. It becomes a *loss* only once the taker actually crosses the spread to transact: at 1c of spread per leg, `3.00 − 3.418 = −0.42c`. Quoting the figure without the crossing clause makes the two statements contradict each other (errata E18). The entire
viability of the sleeve is the maker discipline (C2).

**The rulebook equivalence engine (the moat, C4).** For every candidate link, extract and compare:

```yaml
extracted_fields:
  - settlement_source          # exact publisher, series, and revision policy
  - settlement_timestamp       # including timezone and "as first published" vs "as revised"
  - resolution_criteria        # the literal predicate
  - void_conditions            # postponement, death, cancellation, no-contest
  - rounding_and_ties          # decisive for threshold markets
  - early_settlement_clause
verdict: VERIFIED | REJECTED | NEEDS_HUMAN
```

An LLM proposes the extraction and the verdict; **a human confirms every VERIFIED link before it trades**,
and the decision is stored with the rules hash. Any change in either market's `rules_hash` invalidates the
link and forces re-review. This is a hard gate, not an advisory step: correlation-of-definition risk is the
top loss driver in this whole strategy family.

**Threshold ladders are DIRECNET, not MECE — and that is the point.** `KXFED-27APR` lists 18 *cumulative*
thresholds (`mutually_exclusive: false`, `collateral_return_type: DIRECNET`); its structure is
**monotonicity** (`P(>0.00) >= P(>0.25) >= ...`), not sum-to-one. Monotonicity violation between adjacent
strikes is the cleanest L2 link on the venue and needs no forecasting at all.

**The premier same-underlying RV pairs** (same event, two different shapes, two independent books):

| Ladder (DIRECNET, monotone) | Bracket set (MECNET, sums to 1) | Relation |
|---|---|---|
| `KXFED-<meeting>` (18 thresholds) | `KXFEDDECISION-<meeting>` (5 brackets) | a bracket = difference of two adjacent thresholds |
| `KXINXDIRY-27DEC31H1600` (thresholds) | `KXINXY-27DEC31H1600` (ranges) | same index, same timestamp |

Also overlapping: the CPI family (`KXUSCPIYEAR` / `KXCPI` / `KXCPIYOY` / `KXLCPIMAXYOY` / `KXHIGHINFLATION`)
and the rate-path family (`KXFEDCHGCOUNT` / `KXRATECUTCOUNT` / `KXRATECUT` / `KXEMERCUTS` / `KXZERORATE`).
One genuine intra-Kalshi duplicate exists: `KXOSCARVIS-27` and `KXOSCARMAH-27` are the same Oscar category
on two independent books.

**Seed the link graph from `GET /milestones`, not title similarity.** That endpoint publishes
`primary_event_tickers[]` and `related_event_tickers[]` grouping events *across different series* that
resolve off one real-world occurrence — it is Kalshi's own correlated-event index and is largely unknown in
community tooling. Title matching found only 1 real duplicate in 12,000 events and 9 false positives.

**Warning: only 7 series mix both shapes** (`KXPRIMARYMOV`, `KXPSAVERT`, `KXGOVSENDIFF`, `KXSTARSHIPSPACE`,
`KXSCFI`, `KXMLBSS`, `KXHEISMANSPECIAL`), so read `mutually_exclusive` **per event, never cache per series**.

**Volume target — MEASURED.** 5,103 multi-leg events are NOT flagged mutually-exclusive and
8,156 events carry >= 3 legs. The richest L2 hunting ground is the **threshold-ladder series**
(`KXMIDTERMVOTETURN` 502 events, `KXMIDTERMMOV` 475, `KXNCAAF1H` 206, `KXMLBINNINGWIN` 136, `KXNCAAFTOTAL`
78, `KXNCAAFSPREAD` 78, `KXNCAAFWINS` 73). These are structurally guaranteed to contain monotone
constraints such as `P(total > 45) <= P(total > 40)` — the cleanest possible logical link, requiring no
forecasting whatsoever. **Build L2 on threshold ladders first.**

**Sizing.** Structures with a proven logical bound: treat like S2 (5% cap, ROLC hurdle). L4 links: treat as
directional and size via `position_fraction` at half cap.

**Pre-registered test.** Because settlements are lumpy, S3 is judged on: (a) zero MECE/equivalence failures,
(b) realized margin per completed structure versus modeled margin, (c) orphan-leg loss as a fraction of
gross margin (target < 20%).

---

### 3.4 S6 — Liquidity provision and rewards

```yaml
id: S6
family: income
venue: polymarket_us (primary — pays maker rebates + LP stipends), kalshi (pools)
execution: two-sided resting quotes
role: earn while validation accumulates; produce the toxicity dataset
gate_entry: G3
```

**Thesis.** Both venues pay for resting liquidity: Polymarket US pays a maker rebate on every fill plus
daily rewards scored quadratically by proximity to mid (two-sided quoting required), and runs a stipend-paid
LP program; Kalshi pays up to $0.005/contract with per-market daily pools of $10-$1,000. On non-flagship
books these pools are undersubscribed because institutional makers ignore them.

**Quoting rule (from 2.6).**

```python
# 1) FAIR VALUE: microprice over CUMULATIVE top-N depth, with your own size removed.
#    In a 1c-tick book the mid is badly quantised and manufactures fake mean-reversion.
#
# 2) DIRECTIONAL SIGNAL: Cont-de Larrard, parameter-free, verified to +-0.0005:
#       P(mid moves up) = (2/pi) * atan(q_bid / q_ask)
#    NOT bid/(bid+ask), which is biased toward 0.5 by up to 4.5 probability points
#    -- on a 1c tick that is 4.5c of fair-value error, larger than the whole spread.
#    Use it to SKEW OR WITHDRAW, never to chase: fill likelihood correlates NEGATIVELY
#    with post-fill returns.
#
# 3) RESERVATION PRICES: exact CARA, no Gaussian approximation. Three lines, and
#    strictly better than AS in the tails because it respects the [0,1] bound.
def CE(q, p, g):                      # certainty equivalent of holding q contracts
    return -(1/g) * math.log(p*math.exp(-g*q) + (1-p))
r_bid = CE(q+1, p, g) - CE(q,   p, g)
r_ask = CE(q,   p, g) - CE(q-1, p, g)

# 4) WIDTH: the base term dominates the inventory term for any sane gamma, so set
#    width from k and use inventory purely as skew.
half_spread = max(
    mu_hat / (1 - mu_hat) * L_hat - rebate(price, venue),   # Glosten-Milgrom floor
    bcs_floor(lambda_jump, jump_dist),                      # Budish-Cramton-Shim eq 6.3
    MIN_HALF_SPREAD,
)
# gamma is BACKED OUT of the inventory cap, never guessed from utility:
#     gamma = delta_skew_max / (q_max * p * (1-p))
```

**Spread scales with short-horizon realized volatility** — that falls out of the BCS model rather than being
a heuristic (arbitrage frequency is explained almost entirely by distance travelled).

**Depth taper.** BCS eq. 6.4: the sniping cost is *identical at every level* while revenue *shrinks* with
depth. **Size must thin out fast beyond the touch — for pick-off risk, not inventory risk.** This is a
steeper taper than the usual rationale produces.

**More inventory means quote TIGHTER, not wider** (Bayraktar-Ludkovski: `s* ∝ x^(-1/a)`) — the opposite of
the naive instinct.

`mu_hat` and `L_hat` start at 0.10 / 0.10 and are re-estimated per market from your own mark-out history.

**Hard rules.**

- Quotes are pulled or widened around every scheduled event in the calendar service (releases, game starts,
  model cycles). Stale quotes during scheduled news are donations.
- **Automatic delisting:** if trailing-200-fill average mark-out exceeds captured spread plus rebates, the
  market is removed from the quoting universe without discussion.
- Never chase proximity-to-mid for reward score past the Glosten-Milgrom floor. The reward is bounded; the
  adverse selection is not.

**Expectation.** Realistically hundreds of dollars per month at retail scale. Its strategic value is
threefold: it earns during validation; it generates the toxicity dataset every other sleeve reuses; and it
is the only sleeve whose income does not depend on an edge hypothesis being true.

---

### 3.5 S7 — Cross-venue scanner (monitor)

```yaml
id: S7
family: monitor
venue: [kalshi, polymarket_us, forecastex]
execution: alert-first; auto-execute ONLY tail-priced pairs
role: signal generation; opportunistic execution
gate_entry: G1 (as a monitor); G4 (to execute)
```

Per C1, this is not a business. It runs because the data is already there and because persistent divergence
between two *verified-equivalent* markets is a strong input to S1 and S3.

**Execution eligibility (all must hold):**

```yaml
- link.equivalence_status == VERIFIED
- net_locked_margin >= 0.005                 # after both legs' fees
- min(price) <= 0.20 or max(price) >= 0.80   # tail pricing, where the fee parabola is small
- one leg restable as maker
- both venues pre-funded (no capital transfer required at trade time)
```

The tail-pricing condition comes straight from the computed table: a 4c gross gap nets **+0.76c** at 48/48
but **+3.03c** at 90/6. Mid-priced gaps are treated as *signal* — "one of these venues is wrong" — and
routed to S1/S3 rather than legged for sub-cent margin.

---

### 3.6 S4 / S5 — Model sleeves (optional, later)

Deferred until every non-forecasting sleeve is live and validated. Full specifications retained in the
research report; summary of the essentials:

**S4 Weather.** *Correction from live data:* the major high-temperature series (e.g. `KXHIGHNY`) now settle
on **The Weather Company** (`https://weather.com/kalshi`), **not** NWS directly, and the rules warn that
"preliminary Weather Company data may be subject to rounding and conversion differences from the final
reported value". Model the actual settlement source. Series-level counts: The Weather Company 123, NWS 95,
NOAA 40 — so both exist and the source must be read per series, never assumed. Pipeline: Open-Meteo
individual ensemble members → per-station bias correction fitted on Iowa Environmental Mesonet history →
predictive distribution over settlement buckets, **fat-tailed (never Gaussian)** → trade only at 2-5 day
horizons where model-market disagreement is widest. Same-day is ceded to latency bots by rule. The
documented failure mode to avoid: a Gaussian-error assumption produced an 0-for-32 losing run.

**S5 Economic ladders.** Build the market-implied distribution across a CPI or payrolls bracket ladder;
compare against a Cleveland-Fed-nowcast-anchored distribution with dispersion fitted from historical nowcast
errors; sell overpriced tails as maker. **No positions on FOMC eve** — Kalshi's day-before record was
perfect across 2022-2025 and there is no edge there. ForecastEx legs earn the ~3.1% coupon while parked.

---
## §4 — REPOSITORY LAYOUT AND MODULE CONTRACTS

### 4.1 Tree

```
predictionMarkets/
├── PLAN.md                      # this file — source of truth
├── README.md
├── pyproject.toml               # uv/poetry; py>=3.13
├── config/
│   ├── base.yaml                # venues, endpoints, DB path, log level
│   ├── risk.yaml                # section 9 limits — the ONLY place limits live
│   ├── sleeves/{s1,s2,s3,s6,s7}.yaml
│   └── secrets.env.example      # never commit real keys
├── core/
│   ├── math/
│   │   ├── contracts.py         # fee, edge, variance                  (2.1)
│   │   ├── sizing.py            # kelly, growth, position_fraction     (2.2)
│   │   ├── stats.py             # sample size, Wilson CI, Brier, alpha spending (2.5)
│   │   └── portfolio.py         # n_eff, hedge_ratio, dutch_book_margin (2.6, 2.7)
│   ├── models.py                # Pydantic domain models (Market, Book, Order, Fill, Link...)
│   ├── db.py                    # connection, migrations, typed queries
│   └── time.py                  # UTC helpers; NEVER use naive datetimes
├── venues/
│   ├── base.py                  # VenueClient protocol — see 4.2
│   ├── kalshi/{client,auth,ws,mapping}.py
│   ├── polymarket_us/{client,auth,stream,mapping}.py
│   └── manifold/client.py       # paper venue (section 7)
├── recorder/
│   ├── main.py                  # process entrypoint
│   ├── book_recorder.py         # seq-gap detection, resync, persistence
│   └── metadata_snapshotter.py  # point-in-time market metadata (anti-look-ahead)
├── research/
│   ├── 01..04-*.md              # source notes
│   ├── quant/quant_research.py  # canonical simulations
│   └── notebooks/
├── rulebook/
│   ├── extractor.py             # LLM-assisted field extraction
│   ├── equivalence.py           # check_mece(), check_link(), verdicts
│   └── store.py                 # rules_hash -> extraction -> human verdict
├── strategy/
│   ├── base.py                  # Sleeve protocol — see 4.2
│   ├── s1_structural.py
│   ├── s2_dutchbook.py
│   ├── s3_linked_rv.py
│   ├── s6_liquidity.py
│   └── s7_scanner.py
├── execution/
│   ├── executor.py              # diff desired vs actual; place/cancel
│   ├── oms.py                   # order lifecycle, idempotency, reconciliation
│   ├── structures.py            # multi-leg structure tracking (S2/S3)
│   └── killswitch.py            # I9
├── risk/
│   ├── engine.py                # ALL limit checks (I3)
│   ├── exposure.py              # theme tagging, n_eff computation
│   └── ladder.py                # drawdown ladder state machine
├── backtest/
│   ├── engine.py                # event-driven replay
│   ├── fills.py                 # pessimistic / realistic / optimistic models (6.7)
│   └── report.py
├── shadow/
│   └── engine.py                # live data, logged orders, counterfactual fills (section 7)
├── monitor/
│   ├── main.py
│   ├── alerts.py                # Telegram/Discord
│   └── kpi.py                   # section 12 metrics
├── sleeves/<id>/PREREGISTRATION.md   # R2.5a — dated, committed before first order
└── tests/
```

### 4.2 Module contracts

```python
# venues/base.py
class VenueClient(Protocol):
    venue: str
    async def list_markets(self, **filters) -> list[Market]: ...
    async def get_book(self, ticker: str) -> Book: ...
    async def get_rules(self, ticker: str) -> RulesDoc: ...          # raw text + hash
    async def place_order(self, req: OrderRequest) -> OrderAck: ...  # MUST honor post_only
    async def cancel_order(self, client_order_id: str) -> None: ...
    async def cancel_all(self) -> int: ...                           # I9
    async def get_fills(self, since: int) -> list[Fill]: ...
    async def get_positions(self) -> list[Position]: ...
    async def exchange_ok(self) -> bool: ...                         # circuit breaker
    def stream_books(self, tickers: list[str]) -> AsyncIterator[BookEvent]: ...

# strategy/base.py
class Sleeve(Protocol):
    id: str
    gate: int                        # current gate; executor refuses live orders below 4 (I5)
    def desired_state(self, snapshot: MarketSnapshot) -> DesiredState: ...
    def on_fill(self, fill: Fill) -> None: ...
    def kill_check(self, stats: SleeveStats) -> KillVerdict: ...

# DesiredState is declarative: the executor diffs it against reality.
@dataclass(frozen=True)
class DesiredState:
    quotes: list[DesiredQuote]        # ticker, side, price_cents, size, post_only
    structures: list[DesiredStructure]  # multi-leg, with structure_id and completion policy
    rationale: dict[str, Any]         # logged verbatim: model prob, shrunk edge, filters passed
```

**Contract rules.**

- `C4.2a` — Sleeves are **pure**: `desired_state` must be a deterministic function of its inputs. No I/O,
  no clock reads (time is in the snapshot), no random without a seeded generator. This is what makes
  backtest, shadow, and live share one code path.
- `C4.2b` — Sleeves never call `VenueClient`. Only the executor does.
- `C4.2c` — Every `DesiredQuote` carries its `rationale`; the executor persists it with the order. An order
  whose rationale cannot be reconstructed later is a bug.

---

## §5 — DATA MODEL

SQLite for development (WAL mode); PostgreSQL-compatible DDL for production. Migrations in `core/db.py`.

```sql
-- Point-in-time market metadata. NEVER overwrite: append a new row per observation. (anti-look-ahead)
CREATE TABLE market_snapshots (
  id              INTEGER PRIMARY KEY,
  observed_at_us  INTEGER NOT NULL,
  venue           TEXT    NOT NULL,
  ticker          TEXT    NOT NULL,
  event_ticker    TEXT,
  series_ticker   TEXT,
  title           TEXT    NOT NULL,
  status          TEXT    NOT NULL,          -- active|closed|settled|voided
  close_at_us     INTEGER,
  settle_source   TEXT,
  rules_hash      TEXT    NOT NULL,
  theme_id        TEXT,                      -- risk.exposure tagging
  volume_cents    INTEGER,
  open_interest   INTEGER,
  UNIQUE(venue, ticker, observed_at_us)
);
CREATE INDEX ix_ms_ticker ON market_snapshots(venue, ticker, observed_at_us DESC);

-- Event-level metadata. Carries the fields S2/S3 depend on. Append-only, like markets.
CREATE TABLE event_snapshots (
  id              INTEGER PRIMARY KEY,
  observed_at_us  INTEGER NOT NULL,
  venue           TEXT    NOT NULL,
  event_ticker    TEXT    NOT NULL,
  series_ticker   TEXT,
  category        TEXT,
  title           TEXT,
  mutually_exclusive     INTEGER NOT NULL DEFAULT 0,  -- exchange flag: at most one YES (NOT exhaustive)
  exhaustive_verified    INTEGER NOT NULL DEFAULT 0,  -- OUR verdict from check_mece() + human review
  collateral_return_type TEXT,                        -- MECNET | DIRECNET | NULL (capital efficiency)
  settlement_sources_json TEXT,
  UNIQUE(venue, event_ticker, observed_at_us)
);

CREATE TABLE rules_docs (
  rules_hash   TEXT PRIMARY KEY,
  venue        TEXT NOT NULL,
  ticker       TEXT NOT NULL,
  fetched_at_us INTEGER NOT NULL,
  raw_text     TEXT NOT NULL,
  extraction_json TEXT,                      -- rulebook/extractor.py output
  human_verdict   TEXT,                      -- VERIFIED|REJECTED|NEEDS_HUMAN|NULL
  verdict_at_us   INTEGER,
  verdict_note    TEXT
);

CREATE TABLE book_events (
  id           INTEGER PRIMARY KEY,
  recv_at_us   INTEGER NOT NULL,             -- LOCAL receive time — your latency dataset
  venue_ts_us  INTEGER,
  venue        TEXT NOT NULL,
  ticker       TEXT NOT NULL,
  seq          INTEGER,
  kind         TEXT NOT NULL,                -- snapshot|delta|trade|status
  payload      BLOB NOT NULL                 -- msgpack
);
CREATE INDEX ix_be_ticker_time ON book_events(venue, ticker, recv_at_us);

CREATE TABLE orders (
  client_order_id TEXT PRIMARY KEY,          -- UUIDv4, generated BEFORE send (idempotency)
  created_at_us   INTEGER NOT NULL,
  sleeve_id       TEXT NOT NULL,
  structure_id    TEXT,                      -- multi-leg grouping (S2/S3)
  venue           TEXT NOT NULL,
  ticker          TEXT NOT NULL,
  side            TEXT NOT NULL,             -- yes|no
  price_cents     INTEGER NOT NULL,
  size            INTEGER NOT NULL,
  post_only       INTEGER NOT NULL,
  mode            TEXT NOT NULL,             -- backtest|shadow|paper|live
  venue_order_id  TEXT,
  state           TEXT NOT NULL,             -- pending|open|partial|filled|cancelled|rejected
  rationale_json  TEXT NOT NULL,             -- C4.2c
  updated_at_us   INTEGER NOT NULL
);

CREATE TABLE fills (
  id              INTEGER PRIMARY KEY,
  filled_at_us    INTEGER NOT NULL,
  client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
  venue_fill_id   TEXT UNIQUE,               -- dedupe key from the venue
  price_cents     INTEGER NOT NULL,
  size            INTEGER NOT NULL,
  fee_cents       INTEGER NOT NULL,          -- signed: negative = rebate received
  is_maker        INTEGER NOT NULL,
  terminal        INTEGER NOT NULL DEFAULT 0 -- PM: MATCHED can later FAIL; only terminal counts
);

CREATE TABLE marks (                         -- for mark-out KPI (section 12)
  id           INTEGER PRIMARY KEY,
  fill_id      INTEGER NOT NULL REFERENCES fills(id),
  horizon_s    INTEGER NOT NULL,             -- 300 | 3600
  fair_price   REAL NOT NULL,
  markout_cents REAL NOT NULL
);

CREATE TABLE structures (                    -- S2/S3 multi-leg lifecycle
  structure_id  TEXT PRIMARY KEY,
  sleeve_id     TEXT NOT NULL,
  kind          TEXT NOT NULL,               -- dutch_book|L1|L2|L3|L4
  created_at_us INTEGER NOT NULL,
  legs_json     TEXT NOT NULL,
  target_margin_cents REAL NOT NULL,
  state         TEXT NOT NULL,               -- resting|partial|complete|unwound|orphaned
  realized_margin_cents REAL,
  closed_at_us  INTEGER
);

CREATE TABLE links (                         -- S3 link graph
  link_id       TEXT PRIMARY KEY,
  type          TEXT NOT NULL,               -- L1|L2|L3|L4
  venue_a TEXT, ticker_a TEXT, venue_b TEXT, ticker_b TEXT,
  constraint_json TEXT NOT NULL,
  rules_hash_a  TEXT NOT NULL,
  rules_hash_b  TEXT NOT NULL,
  equivalence_status TEXT NOT NULL,          -- VERIFIED|REJECTED|NEEDS_HUMAN
  verified_at_us INTEGER,
  UNIQUE(venue_a, ticker_a, venue_b, ticker_b, type)
);

CREATE TABLE settlements (
  id           INTEGER PRIMARY KEY,
  venue TEXT NOT NULL, ticker TEXT NOT NULL,
  settled_at_us INTEGER NOT NULL,
  outcome      INTEGER NOT NULL,             -- 1 = YES, 0 = NO
  voided       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE decisions (                     -- one row per model probability emitted (calibration)
  id            INTEGER PRIMARY KEY,
  decided_at_us INTEGER NOT NULL,
  sleeve_id     TEXT NOT NULL,
  venue TEXT, ticker TEXT,
  market_price  REAL NOT NULL,               -- benchmark forecast
  p_model       REAL NOT NULL,
  raw_edge      REAL NOT NULL,
  shrunk_edge   REAL NOT NULL,
  acted         INTEGER NOT NULL,
  preregistration_id TEXT                    -- which test this counts toward (R2.5a)
);
```

`R5a` — **Never `UPDATE` a `market_snapshots` row.** Append. Backtests read the latest row with
`observed_at_us <= t`; overwriting is how look-ahead leaks in.
`R5b` — Position is computed from `fills` where `terminal = 1`. Never from a counter.

---

## §6 — COMPONENT SPECIFICATIONS

### 6.1 recorder

Subscribes to book/trade streams, snapshots metadata, archives external signal inputs with fetch timestamps.

- **Cache `GET /series` once per session.** It ignores `limit` and returns all 13,486 series in one
  response, carrying the complete fee (`fee_type`, `fee_multiplier`), settlement-source, and
  `additional_prohibitions` map. Every other component reads from that cache.
- **Pass `mve_filter=exclude` on any `/markets` call** to drop the 117k multivariate parlay markets.
- **Enumerate the universe via `GET /events?with_nested_markets=true&status=open`, NOT `/markets`.** A
  page-capped pull of `/markets` returned 99.3% `KXMVE*` multivariate parlay shards and only 425 real
  markets. `/events` returns the complete universe (12,553 events / 103,449 quoted markets) and is the only
  place `mutually_exclusive`, `settlement_sources`, and `collateral_return_type` are exposed.

- Own connections; never shares a socket with the executor.
- Sequence-gap detection: on a gap, discard the local book and resync (Kalshi supports a snapshot request;
  Polymarket re-sends a full `book` on reconnect, and the message hash verifies it).
- Reconnect with exponential backoff plus jitter; resubscribe everything; silence > 15s means dead.
- Writes `recv_at_us` on every message (this is the latency dataset used to size latency assumptions).
- Metadata snapshotter runs on a schedule and on every observed status change.

**Acceptance:** kill the network for 60s; the recorder resyncs, and a replay of the period reconstructs a
book identical to a fresh snapshot taken afterward.

### 6.2 rulebook service

`extractor.py` uses an LLM to pull the fields in 3.3 from raw rules text into structured JSON.
**Feasibility is measured:** 100% of the 103,449 quoted markets expose `rules_primary` (median 142 chars,
p95 245) and 100% of events expose `settlement_sources`, so full-universe extraction is cheap. Settlement
sources form a small closed vocabulary (ESPN 5,062 events, Fox Sports 3,057, the Governing League 1,574,
NCAA-derived 1,202, WSJ 583, Reuters 508, AP/NYT/WaPo/CNN ~420 each), so source matching is mostly a lookup;
cross-source links are the `NEEDS_HUMAN` cases.
`equivalence.py` implements `check_mece(legs)` and `check_link(a, b, type)`.
`store.py` persists `rules_hash -> extraction -> human verdict`.

- Any `rules_hash` change invalidates every dependent link and structure, and blocks new entries.
- The LLM never issues a final VERIFIED verdict alone — it proposes; a human confirms (C4).

### 6.3 strategist

Loads sleeves, feeds each a `MarketSnapshot`, collects `DesiredState`. Pure and deterministic (C4.2a).
Every emitted probability is written to `decisions` whether or not it is acted upon — un-acted decisions are
what make calibration measurable without survivorship bias.

### 6.4 executor / OMS

- Diffs desired versus actual; cancels what should not exist, places what should.
- Generates `client_order_id` before sending; on restart, reconciles against venue fills/orders.
- Enforces every risk limit by calling `risk.engine` **before** each send (I3). Refuses any order from a
  sleeve whose `gate < 4` in live mode (I5).
- Multi-leg structures tracked in `execution/structures.py` with the completion/timeout/unwind policy in 3.2.
- Position built from the fill stream, not from polling (Kalshi's positions endpoint lags ~1s).

**Kalshi execution gotchas — each of these silently corrupts results if missed:**

- **Set `use_yes_price: true` on every orderbook subscription.** By default the orderbook channel reports
  no-side orders with *inverted* pricing (no at 30c = yes at 70c), while the REST order API prices both
  sides identically. Without the flag every cross-leg calculation is wrong by `1-p`.
- **Orders cannot be cancelled after `close_time`** — all operations including cancels return
  `MARKET_INACTIVE`. Resting exposure into a close is irrevocable; bound it deliberately.
- **Tick size is not always 1c.** Read `Market.price_ranges`. 7.8% of markets are `tapered_deci_cent`
  (0.1c below 10c and above 90c, 1c in between) — tenth-cent resolution exactly where longshot legs live.
  Quoting those on a 1c grid forfeits nine tenths of the available price improvement.
- **Batch, do not slice.** Fee rounding accumulates *per order* across its fills, so one large order is not
  penalized while many small separate orders each round up. Use `BatchCreateOrders` with large per-leg
  counts rather than many small clips.
- **Per-shard halts are independent.** `exchange_index` 0=Default, 1=Combos, 2=Crypto, 3=Tennis/Baseball;
  `GET /exchange/status` reports `trading_active` per shard. One shard can halt while others trade — a
  direct source of stale cross-shard quotes, and a reason to check the shard of *every* leg of a structure.
- **`SelfTradePreventionType`** is `taker_at_cross` or `maker`; set it explicitly when quoting both sides.
- **Prices are fixed-point dollar strings** (`"0.6720"`). Parse with `Decimal`, never float.

**The maker/taker decision is price-dependent, not a global constant.** Solving
`0.07*p(1-p) = 0.005` (taker fee equals a half-tick) gives **p = 0.0774 and p = 0.9226**:

> For **7.74% < p < 92.26%** the taker fee exceeds the entire half-spread of a 1-tick market.
> Outside that band the flat tick dominates and the fee is nearly free.

At p = 0.50 in a 1-tick market, crossing costs 2.25c while posting is a **net credit of 0.06c** — you need
**2.31c of edge (4.6% of price)** before crossing beats a certain fill. A taker round trip costs 1.26 ticks
at p=0.10 and 3.50 ticks at p=0.50, so **round-tripping as a taker is structurally unprofitable** in a
market whose entire spread is 1-3c.

**Amend and cancel-replace rules** (any modification loses time priority *except* a quantity reduction):

- **Never amend to increase size.** Leave the original resting and submit a **second order** for the
  increment — you keep priority on the first tranche.
- **Amend down freely** — the one free operation.
- **On a partial fill the residual keeps its queue position.** Never cancel and re-post a residual to tidy
  up; that is a pure priority donation.
- **Reprice only outside a hysteresis band** sized to the queue value forfeited (up to ~0.2 ticks against a
  1-tick spread, so repricing is expensive).
- Prefer atomic amend-in-place over cancel-then-new (the latter leaves a window off the book *and* a window
  where both sides are live).
- **Cancels cost 2 rate-limit tokens; creates cost 10.** Structure the quoter to cancel aggressively and
  re-create selectively, never to churn creates.
- Instrument **`amend_count / fill_count`**. If it rises, the fair-value model is noisier than the spread —
  widen rather than chase.

**Self-trade prevention: choose `taker_at_cross` (cancel-newest semantics) for a two-sided quoter** so
resting liquidity survives and only the erroneous new order dies; cancel-oldest can strip you off the book
during a repricing burst. Prevent self-crosses in your own logic first (maintain an internal book; never
submit a bid >= your live ask), treat exchange STP as the backstop, and **alert on every trigger** — a
rising prevented-match rate means the two sides are converging and the quoting logic has a bug. CEA
§4c(a) prohibits wash sales and the line is **intent**: STP on, every trigger logged, and a demonstrable
single-strategy explanation is the defensible posture.

### 6.5 risk engine

Pure functions over current state returning `Allow | Deny(reason)`. Limits loaded from `config/risk.yaml`
(section 9) — no limit may be defined anywhere else. Computes `n_eff` per 2.7 and maintains the drawdown
ladder state machine.

### 6.6 monitor

Health, alerts, KPIs (section 12), daily digest. Periodic reconciliation of local versus venue positions;
on drift, halts the affected venue and requires human acknowledgement.

### 6.7 backtester and fill models

Event-driven replay over recorded `book_events`. Three fill models, always reported side by side:

| Model | Maker fill rule | Taker fill rule |
|---|---|---|
| **pessimistic** | book must trade **through** your price, and only the volume beyond the full resting queue ahead of you fills | walk the recorded depth with full slippage, plus one tick of penalty |

| realistic | trade-through fills proportional to modeled queue position | walk recorded depth |
| optimistic | fill at touch on any trade at your price | fill at best displayed |

- `R6.7c` — **Validation gate: simulated adverse-fill rate must land in 66-89%.** Realized adverse-fill
  rates on CME futures run ES 81.5%, NQ 65.8%, CL 82.9%, ZN 88.8%. **If the simulator produces ~50% adverse
  fills it is handing you fills the real market would not have.**
- `R6.7d` — Touch-fill overstates fill speed ~1.6x and trade-through understates ~2.4x; the bounds are ~3.9x
  apart. **Report the bracket, never a point.** Treat cancellations as **censored, not as non-fills**.
- `R6.7e` — **Calibrate fill probability from realized fills, never from displayed queue depth.** Hidden
  liquidity (icebergs: ~9.3% of submitted and 15.9% of executed shares elsewhere) means displayed depth
  systematically *over*-estimates your fill probability. Detect it cheaply: whenever
  `traded_volume_at_level > displayed_volume_before_trade`, the excess is hidden size — log it and build a
  per-market `hidden/displayed` distribution.
- `R6.7f` — Budget adverse selection at the **high** end of any estimate: measured at **0.57-0.85 ticks
  against a 1-tick spread**, and for one symbol back-of-queue passive provision had *negative* expected
  value. Adverse selection **increases** with queue depth (back-of-queue fills come from large, informed
  trades), so front of queue is better on both axes.
- `R6.7a` — **Gate promotion decisions read the pessimistic column only — and "pessimistic" means fewest FILLS, not worst P&L.** Measured on a losing fixture with identical fills: pessimistic `+15,000c`, queue-conservative `+96,429c`, optimistic `+150,000c` when everything resolves YES, and `−5,000c / −32,143c / −50,000c` when everything resolves NO. The ordering **inverts on the loss side**: on a losing sleeve the pessimistic column reports the *flattering* number, because fewer fills means less of a bad trade. It is safe to gate on because the G2 exit criterion is a per-contract edge CI, which is roughly fill-quantity invariant — not because the number itself is a lower bound on profit. Reading it as "worst case P&L" is wrong and dangerous (errata E24).
  fill-model uncertainty, not to flatter a strategy.
- Fees are modeled per venue *and per era* — pre-2026 Polymarket data is a zero-fee world and must not be
  used to justify a 2026 strategy.
- Leakage checklist runs as a test: point-in-time metadata only, signal timestamps respected, voided and
  delisted markets included, no resolved-price granularity artifacts.
- **Purge and embargo are mandatory, not optional.** In prediction markets label overlap is *structural* —
  a market opened at `t` does not settle until `t + H`. Verified: a time-only predictor whose true
  out-of-sample AUC is 0.500 scored **0.573** under naive CV, 0.505 with purging, and 0.500 only with
  purging **plus a 2H embargo**. Drop training observations whose label span overlaps any test span, then
  embargo a further `h ~ 0.01*T` after the test set.
- **Prefer Combinatorial Purged CV to walk-forward** — it yields a *distribution* of backtest outcomes
  rather than one high-variance path. Libraries: `timeseriescv`, `skfolio.model_selection`. (`mlfinlab` is
  now closed-source; use `mlfinpy`.)

### 6.7b Fill probability as a survival problem — build it as pooled logistic regression

The production fill model is **discrete-time survival = pooled logistic regression on (order, time-bucket)
rows**. It is the easiest to implement, handles time-varying covariates natively, scales, and gives
competing risks nearly free.

1. Bin time to match your decision frequency.
2. Expand each order into one row per bucket it survives into. An order filling in bucket 5 contributes
   `y = 0,0,0,0,1`; an order **cancelled** in bucket 3 contributes `y = 0,0,0` and then stops.
   **That is the entire censoring treatment — no special handling.**
3. Attach covariates as of the start of each bucket.
4. Fit one pooled logistic regression. `P(fill within J) = 1 - prod(1 - h_m)`.

- **Cluster standard errors on `order_id`** — rows from one order are not independent:
  `sm.Logit(y, X).fit(cov_type='cluster', cov_kwds={'groups': order_id})`.
- **Competing risks for free:** replace binary `y` with a multinomial `{no event, fill, cancel}` per bucket.
  This is the cleanest competing-risks implementation available in Python.
- **Censoring bias is large and points the wrong way.** Verified: treating cancels as non-fills understates
  `P(fill)` by up to **0.43** at one tick from the touch. That makes a naive classifier systematically
  pessimistic about fills, **which biases a maker strategy toward crossing the spread too often** — on a
  1-2c spread that is 1-2% of notional given away per round trip.
- **Your cancellation policy is informative censoring** (you cancel precisely when prices move away), which
  violates the independence assumption. Fix it by putting the price path in as a **time-varying covariate**,
  which the bucket format makes trivial.
- Covariates ranked: distance from mid (dominant), queue position, same-side depth, opposite depth, spread,
  book imbalance, short-window realized volatility, **time to resolution** (first-order here — the hazard is
  strongly non-stationary near settlement), size (weak), side (fit separately).
- **Expect proportional hazards to fail** on order-book data; test with scaled Schoenfeld residuals.
- Tooling: `lifelines` 0.30.3 and `scikit-survival` 0.28.0 both install cleanly on this machine.
  `CoxTimeVaryingFitter` is the only mainstream pure-Python time-varying Cox. `pysurvival` is abandoned and
  `xgbse` is archived.

### 6.8 shadow engine

See section 7 — this is the primary pre-capital validation tool.

---

## §7 — PAPER TRADING AND PRE-CAPITAL VALIDATION

**Answer to "can we paper trade before deploying real capital?": yes — through four distinct mechanisms,
each validating a different thing. No single one of them validates everything, and the important one is P3.**

### 7.1 The ladder

| Stage | Mechanism | What it validates | What it does NOT validate |
|---|---|---|---|
| **P0** | **Manifold Markets** (play money, full API, bots welcome) | Bot mechanics end-to-end against a real adversarial venue with real counterparties; order lifecycle; WebSocket handling | Nothing about Kalshi/Polymarket pricing — it is an AMM with play money and unrepresentative participants |
| **P1** | **Kalshi demo environment** (`demo.kalshi.co`, API root `https://external-api.demo.kalshi.co/trade-api/v2`, WS `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`) | Authentication (RSA-PSS signing), order placement, order types, cancellation, error handling, rate-limit behavior — the full integration surface | **Strategy edge.** Kalshi documents explicitly that demo prices and behavior "may not be reflective of those in real markets" and the order flow is not production liquidity |

> **The demo requires no KYC and no SSN.** Kalshi's own help centre instructs users to
> "use **mock information** when signing up (fake name, address, Social Security Number, etc.)" and supplies
> sandbox payment credentials (test Visa `4000 0566 5566 5556`, Plaid sandbox `user_good`/`pass_good`).
> The only real requirement is an email you can access. Demo credentials are entirely separate from
> production. **So the whole P0–P3 ladder is open to anyone, including someone who cannot open a live
> account.**
>
> **Live accounts are a different matter.** Kalshi is a CFTC-regulated DCM and must collect a taxpayer
> identification number: an **ITIN is accepted as an equivalent US tax ID**, alongside a US residential
> address (not a PO box) and government photo ID. An SSN is the common path, not the only one. Non-US
> *residents* cannot open accounts at all. This is Gate 0 work, and it is independent of — and secondary
> to — the visa question in §13, which must be settled first if it applies.
| **P2** | **Historical replay** (backtester over recorded books, section 6.7) | Whether the signal had an edge in the past, under an explicit fill model | Whether you can actually get filled today; regime change; anything not in the recording |
| **P3** | **Shadow mode** (live production data, real order decisions, logged not sent) | **The real thing**: does the strategy generate profitable orders against today's live books, and would they have filled? | Market impact of your own orders; queue priority you never actually held |
| **P4** | **Canary** (Gate 4, real money, minimum size) | Everything, including impact and true fill priority | — |

### 7.2 Why P3 is the load-bearing stage

Neither venue offers a simulator that reproduces real liquidity. Kalshi's demo is an integration sandbox
whose prices are synthetic. Polymarket US has no sandbox at all. Therefore **there is no off-the-shelf paper
trading product that validates edge — you have to build shadow mode.** It is the only mechanism that puts
the real strategy against the real book without capital.

**What is available with NO account at all.** Kalshi's WebSocket needs auth even for public channels, and
`/markets/{ticker}/orderbook` (full L2 depth) needs auth. But two things are open to anyone:

| Open without credentials | Endpoint | Why it matters |
|---|---|---|
| **Top of book** (bid/ask + sizes) | `/markets?tickers=a,b,c` | L1 time series; batches of 100 return in ~0.2s |
| **Trade tape, with `taker_side` LABELLED** | `/markets/trades` | No trade-sign inference needed — ever |

That second row is worth more than it looks. Kalshi tells you which side was the taker, so Lee-Ready and
BVC classification are simply unnecessary. Those misclassify 10–20% of trades on equities, and on
Polymarket feed-inferred direction agrees with on-chain truth only **~59%** of the time, flipping the sign
of the effective half-spread 67% of the time and Kyle's λ 60% of the time. Here the venue hands you ground
truth for free.

Combined, L1 quotes + a labelled tape are enough to detect trade-**through** events (the honest maker-fill
condition), measure realized spread and mark-outs, estimate Kyle's λ, and run shadow mode end to end.
What is *not* available is depth beyond the touch — so fill models stay queue-conservative until an
authenticated account exists. Implemented in `recorder/l1.py`.

**Shadow engine specification:**

```python
# shadow/engine.py
# Runs the IDENTICAL sleeve code path as live (C4.2a makes this possible).
# The only difference is the executor: ShadowExecutor writes orders with mode="shadow"
# and never calls the venue.

for each shadow order:
    record: (ticker, side, price, size, decision_time, full_book_state_at_decision)
    then, from the CONTINUING recorded stream, evaluate counterfactual fill:
        maker:  fill_qty = volume that traded THROUGH price after decision_time,
                           minus resting_size_ahead_of_you at that level at decision_time
                           (queue-conservative — the pessimistic model)
        taker:  fill at the depth actually available at decision_time, walked
    then, at settlement, score the hypothetical position
```

**The queue-position problem and how it is handled.** You never actually joined the queue, so you must
assume the worst: you are behind every contract resting at your level when you decided. A maker fill counts
only when cumulative trade-through volume at that price exceeds the size that was ahead of you. This
systematically *under*counts fills, which is the correct direction of error for a promotion decision
(`R6.7a`).

**Shadow acceptance metric.** The gap between shadow-predicted fill rate and live fill rate at Gate 4
becomes the permanent slippage haircut applied to all forward estimates (Gate 3 criterion, section 8).

### 7.3 Existing tools worth reading before building

- [homerun](https://github.com/braedonsaunders/homerun) — shadow mode with microstructure-simulated fills,
  and live mode behind identical APIs. The closest public thing to this plan's architecture; read its fill
  model before writing yours.
- [polybot](https://github.com/cryptuon/polybot) — every strategy runs in paper (shadow) mode by default.
- [agent-next/polymarket-paper-trader](https://github.com/agent-next/polymarket-paper-trader) — paper
  simulator built for AI agents; walks the real ask/bid book level by level with slippage tracking.
- [owenwalSe7en/Kalshi_Paper_Trading](https://github.com/owenwalSe7en/Kalshi_Paper_Trading) — minimal
  historical-close sandbox; useful as a reference for the simplest possible harness.
- [PolySimulator](https://polysimulator.com/) — free hosted paper-trading simulator for both venues.

Known limitations shared by all off-the-shelf simulators (verify before trusting any of them): they assume
your order fills first at the quoted price (no queue), they show full fills where reality gives partials,
and they do not model adverse selection — fills in reality are *selective* on direction. Any simulator
without a queue model overstates maker performance, which is exactly the strategy family this plan uses.

### 7.4 Zero-capital validation venues with real stakes

- **Metaculus AI Benchmark Tournament** — real prize pools, no capital at risk, public bot template. Useful
  as an independent, externally-scored calibration test of any forecasting model (S4/S5) before it ever
  sizes a position.
- **Manifold bot leaderboards** — adversarial play-money environment with real opponents.

---
## §8 — THE GATE SYSTEM

Progression is earned by evidence, per sleeve, in order. A sleeve sits at a gate for as long as its criteria
take (I10). Demotion is symmetric: failing a live criterion returns the sleeve to Gate 3.

### G0 — Compliance clearance (operator-level, once)

```yaml
exit_criteria:
  - visa_status_resolved: true       # if F-1/OPT: written advice from an immigration attorney ON FILE
  - tax_posture_chosen: true         # default capital-asset; 1256 only with CPA sign-off + Form 8275
  - kiddie_tax_checked: true         # dependent under 24 -> unearned income >$2,700 at parents' rate
  - record_export_pipeline_designed: true
  - one_account_per_venue_confirmed: true
  - conflict_list_written: true      # markets settling on data you/your lab touch = permanently banned
blocking: ALL live trading
```

### G1 — Data trustworthy (system-level, once)

```yaml
exit_criteria:
  - markets_recorded >= 300          # across S1/S2/S3 target categories
  - book_events_archived >= 10_000_000
  - sequence_gap_rate < 0.001
  - resync_proven: true              # deliberately kill the connection; replay reconstructs the book
  - becker_dataset_loaded_and_reconciled: true   # cross-check your recordings on overlapping markets
  - latency_histogram_published: true
  - clock_ntp_disciplined: true
blocking: G2 for all sleeves
```

### G2 — Backtest survives honest simulation (per sleeve)

```yaml
exit_criteria:
  - preregistration_committed: true               # R2.5a, dated, before any result was seen
  - simulated_settlements >= 1000
  - net_edge_ci_excludes_zero_under_PESSIMISTIC_fills: true    # R6.7a
  - leakage_checklist_all_pass: true
  - walk_forward_out_of_sample_holds: true        # refitting after seeing test data RESTARTS the gate
  - capacity_estimate_documented: true
```

### G3 — Shadow (per sleeve)

```yaml
exit_criteria:
  - shadow_hypothetical_fills >= 300
  - shadow_edge_ci_excludes_zero: true
  - kill_switches_tested_live: [heartbeat_loss, exchange_pause, KILL_file, injected_state_drift]
  - slippage_haircut_recorded: true     # becomes a permanent adjustment to forward estimates
  # S2/S3 additionally:
  - rulebook_equivalence_verified_for_every_link: true
  - orphan_leg_policy_exercised_in_shadow: true
```

### G4 — Canary (per sleeve, one at a time)

```yaml
config_overrides: {position_cap: 0.01, sleeves_live_concurrently: 1}
exit_criteria:
  - live_settlements >= 200
  - realized_net_edge >= 0.5 * shadow_estimated_edge
  - critical_ops_incidents == 0        # orphaned orders, reconciliation failures, limit breaches
  - calibration_log_complete: true
  - brier_skill_vs_market > 0
```

### G5 — Scale (per sleeve)

```yaml
review_trigger: every 150 settlements
step_up_allowed_if:
  - edge_ci_lower_bound > 0
  - capacity_utilization < 0.50
  - no_kill_criterion_fired: true
step_up_rule: position_cap 0.01 -> 0.02; then 0.02 -> 0.03 only after >=1000 settlements
demotion: any kill criterion -> halt, return to G3, written post-mortem before re-entry
portfolio_rule: new sleeves enter at G2 while validated sleeves run (grow sideways, not just up)
```

---

## §9 — RISK LIMITS

`config/risk.yaml` is the only place these values exist. The risk engine (6.5) enforces all of them.

```yaml
position:
  cap_fraction_default: 0.02          # 2.8: ~0.11x Kelly on the flagship sleeve's shrunk edge
  cap_fraction_gate4: 0.01
  cap_fraction_max: 0.05              # only after >=1000 settlements at 0.03
theme:
  max_exposure_fraction: 0.15         # 2.7: intra-theme rho>=0.5 -> a theme is ~2 effective bets
  min_n_eff: 8                        # whenever deployment > 0.20
deployment:
  max_gross_fraction: 0.40
  min_cash_fraction: 0.30             # fat-pitch reserve + dispute buffer
venue:
  max_fraction_per_venue: 0.60        # platform/regulatory tail risk
structures:                            # S2/S3
  max_per_structure_fraction: 0.05
  max_sleeve_total_fraction: 0.15
  min_annualized_return_on_locked_capital: 0.15
  leg_timeout_seconds: 900
  max_orphan_exposure_fraction: 0.005
daily:
  max_loss_fraction: 0.05             # -> no new orders until next session review
exchange_position_limits:
  # NOT exposed in the API. Parse Series.contract_terms_url PDFs at series-cache time.
  # Denominated in DOLLARS OF EXPOSURE PER STRIKE PER MEMBER, e.g.
  #   KXPAYROLLS    -> $25,000 per strike (Position Accountability Level)
  #   KXFEDDECISION -> $7,000,000 per Member (hard Position Limit)
  source: contract_terms_pdf
  enforce: true
capacity:
  max_resting_fraction_of_touch_depth: 0.20
  max_taking_fraction_of_recent_volume: 0.05
  freeze_at_utilization: 0.50
drawdown_ladder:                       # measured from bankroll peak
  - at: 0.10
    action: mandatory_written_review          # is realized edge within CI? are mark-outs deteriorating?
  - at: 0.20
    action: halve_all_position_caps; block_new_sleeves_entering_G4
  - at: 0.30
    action: halt_worst_sleeve_by_edge_CI      # by edge CI, NOT by raw P&L
  - at: 0.40
    action: full_stop; flatten_at_maker_prices_where_possible; complete_audit_before_restart
```

`R9a` — At quarter-Kelly-equivalent sizing a −40% excursion is a much-less-than-1% event under the
hypothesis that edges are real (2.4). Reaching it is evidence they are not.

---

## §10 — RUNBOOKS

### 10.1 Daily operation (automated; human reads the digest)

1. Monitor posts digest: per-sleeve P&L, settlements, edge CI, Brier skill, mark-outs, capacity, limits.
2. Reconciliation runs; any drift halts the affected venue and pages the human.
3. Calendar service loads the day's scheduled events; S6 pull-windows are scheduled.

### 10.2 Deploy

```bash
# on the VPS
git pull && uv sync
pytest -q                       # must be green
python -m core.db migrate
systemctl restart pm-recorder pm-strategist pm-executor pm-monitor
python -m monitor.smoke         # verifies streams, auth, exchange status, cancel-all path
```

### 10.3 Incident: orphaned leg (S2/S3)

1. Executor already attempted completion, then unwind, at `leg_timeout`.
2. If exposure still exceeds `max_orphan_exposure_fraction`: cross the spread to flatten. Do not wait.
3. Log to `structures` with `state='orphaned'` and realized loss; this feeds the orphan-loss KPI.

### 10.4 Incident: rules change detected

1. New `rules_hash` on a market with open exposure → freeze new entries in that market immediately.
2. Re-run extraction; if the verdict changes, treat existing positions as directional and flatten unless
   the human re-verifies.
3. Every link referencing either rules hash is invalidated pending re-review.

### 10.5 Incident: venue dispute / resolution ambiguity

1. Mark affected positions to worst case in risk accounting.
2. No new entries in that event family.
3. Record the outcome in a post-mortem regardless of which way it settles — this is training data for the
   rulebook engine.

### 10.6 Kill (I9)

```bash
touch KILL          # in the run directory
```
Executor cancels all orders on all venues within 5s, refuses new orders, and pages. Recovery requires
removing the file **and** a successful reconciliation.

---

## §11 — TEST REQUIREMENTS

| Area | Required tests |
|---|---|
| `core/math` | Property tests against the canonical tables in section 2 (fee table, Kelly growth points, sample sizes, n_eff). These are regression tests on the plan's own numbers. |
| Fee model | `fee(0.5,"kalshi",False) == 0.0175`; `fee(p,"polymarket_us",True) < 0` for all p in (0,1) |
| Sizing | `position_fraction` never exceeds cap; returns 0 for non-positive edge; monotone in edge |
| Risk engine | Every limit in section 9 has a test that trips it; a sleeve at `gate<4` cannot place a live order (I5) |
| OMS | Idempotency: replaying the same `client_order_id` never double-sends; crash mid-send recovers by reconciliation |
| Structures | Partial-fill → timeout → unwind path; orphan exposure never exceeds its limit |
| Recorder | Sequence-gap injection triggers resync; replay reconstructs a book byte-identical to a fresh snapshot |
| Backtester | Leakage suite: a strategy that peeks at settlement scores impossibly well and the suite FAILS the run |
| Killswitch | Cancel-all completes within 5s from every process state, including mid-placement |
| Rulebook | Known non-MECE sets (missing "other" outcome, mismatched deadlines) are REJECTED |

`R11a` — The leakage suite must contain at least one deliberately-cheating strategy that the harness is
required to catch. A backtester that cannot detect look-ahead when it is present is not evidence of anything.

---

## §12 — KPIs

Reviews are triggered by sample size (every 150 settlements per sleeve) and by events (limit breach, kill
trigger, venue rule change) — never by the calendar (I10).

| # | KPI | Definition | Why it ranks here |
|---|---|---|---|
| 1 | **Brier skill vs market** | `brier(market_price) - brier(p_model)` over settled decisions | Converges far faster than P&L; negative means no edge regardless of P&L |
| 2 | **Net edge per settlement + CI** | realized win rate − price-implied − fees; Wilson interval | The pre-registered test statistic. The **CI**, never the point estimate, drives gate decisions |
| 3 | **Mark-outs** | mean fair-price move at +5m / +1h after fills | Direct measurement of adverse selection (`mu * L`); earliest warning of toxic quotes |
| 4 | **Fill quality** | live fill rate ÷ shadow-predicted; realized taker slippage | Detects fill-model drift — the main way backtests rot |
| 5 | **lambda_hat** | fitted slope of outcome on model probability | Updates sizing; `< 0.3` halts the sleeve (R2.3a) |
| 6 | **Orphan-leg loss ratio** | orphan losses ÷ gross structure margin (S2/S3) | The one real risk in the RV sleeves; target `< 0.20` |
| 7 | **Non-edge income** | rewards + rebates + interest, per unit of capital | The floor return; the consolation metric while samples accumulate |
| 8 | **Capacity utilization** | resting size ÷ touch depth; taker volume ÷ market volume | Freeze trigger at 0.50 (section 9) |

---

## §13 — COMPLIANCE

- **G0 is absolute.** No live order before it clears. If on F-1/OPT, written attorney advice must be on
  file: systematic trading can be construed as unauthorized employment, and that downside dwarfs any edge.
- **Records are automated, not remembered.** The OMS exports complete trade history (timestamps, prices,
  fees, proceeds, basis) every statement cycle. Kalshi issues **no 1099-B for trades** — your export *is*
  the tax record. Store rules text and hashes for any position >= 1% of bankroll.
- **Tax posture** (from research; not advice): consistently-applied capital-asset characterization is the
  practitioner default; Section 1256 60/40 only with professional sign-off plus a Form 8275 disclosure;
  gambling characterization is now the worst case (90% loss-deduction cap). Georgia adds a flat 4.99%.
  Quarterly estimates once liability >= $1,000. A CPA reviews the first profitable year.
- **Account hygiene:** one account per venue, personally funded, forever. No VPN to offshore Polymarket
  under any rationale. Self-trade prevention set on every Kalshi order (required by the API and a
  wash-trading concern).
- **Conflict list** checked by the strategist against every new market's settlement source before it enters
  any universe. This is **automatable**: `Series.additional_prohibitions[]` is machine-readable and already
  encodes the exchange's own restrictions — universally "persons employed by any of the Source Agencies" and
  "persons who hold material non-public information", plus league-participant bans on 3,529 sports series
  and, on election series, Congressional staff, public-office holders, pollsters, Decision Desk employees,
  vote-tallying personnel, FEC commissioners, Electors, and registered lobbyists. Ingest the array, match it
  against a written personal-affiliation profile, and hard-block any series that matches.
- **Regulatory watch:** CFTC rulemaking docket, state-preemption appeals, and both venues' fee schedules.
  Any fee change reprices every sleeve's break-even (2.1) before the next order is sent.

---

## §14 — TASK BACKLOG

Ordered by dependency. Each task is done only when every acceptance criterion is mechanically verifiable.

### Phase A — Foundation (no market access needed)

| ID | Task | Acceptance criteria |
|---|---|---|
| T-001 | Repo scaffold, `pyproject.toml`, pre-commit (ruff+mypy strict), CI running pytest | `pytest -q` green on a clean clone; mypy strict passes on `core/` |
| T-002 | `core/math/contracts.py` | Reproduces the section 2.1 fee table exactly in a parametrized test |
| T-003 | `core/math/sizing.py` | Reproduces the section 2.2 growth table (±0.1 bp); cap and non-positive-edge tests pass |
| T-004 | `core/math/stats.py` | Reproduces the section 2.5 sample-size table (±1%); Wilson CI and Brier decomposition tested. **Implement the sequential-inference primitives in-house** — verified on this machine, `confseq` cannot pip-install (no Windows wheels above cp310, NumPy-2 incompatible), and statsmodels/scipy have **zero** alpha-spending / e-value functionality. The beta-binomial e-value is 4 lines with `scipy.special.betaln`. `savvi` + `matplotlib` installs cleanly for cross-checking. Note `multipletests` defaults to Holm-Sidak, not Bonferroni. |
| T-004b | Calibration + edge-model module | Spiegelhalter Z and Cox slope/intercept implemented (bin-free); **ECE is NOT reported** (a perfectly calibrated forecaster shows ECE 0.10 at n=100 — it is pure noise); CORP reliability diagram via `sklearn.isotonic`; hierarchical `beta_c` fit with empirical-Bayes shrinkage, reproducing the section 2.3b growth table |
| T-005 | `core/math/portfolio.py` | Reproduces n_eff, hedge-ratio, and Dutch-book hurdle tables from 2.6/2.7 |
| T-006 | `core/models.py` + `core/db.py` with the section 5 DDL and migrations | Migration creates every table; round-trip tests for each model; WAL enabled |
| T-007 | `config/` loader (Pydantic) incl. `risk.yaml` | Invalid config fails loudly at startup; no magic numbers remain in code (grep test) |

### Phase B — Venue access and recording (→ G1)

| ID | Task | Acceptance criteria |
|---|---|---|
| T-010 | Kalshi client: RSA-PSS auth, REST, rate-limit-aware retry with jitter | Authenticated call against **demo** succeeds; 429 handling proven by a forced-burst test |
| T-011 | Kalshi WebSocket: subscribe, ping/pong, seq-gap detection, resync | Injected gap triggers resync; reconstructed book matches a fresh snapshot |
| T-012 | Polymarket US client: Ed25519 auth, REST + stream | Authenticated read succeeds; stream reconnects after a forced drop |
| T-013 | Manifold client (paper venue P0) | Places and cancels a play-money limit order via API |
| T-014 | `recorder` process + metadata snapshotter | Runs continuously under systemd/NSSM; gap rate `< 0.001` measured over the G1 sample (>=10M events); append-only invariant enforced by a test (R5a) |
| T-015 | Load and reconcile the Becker bulk dataset | Overlapping markets agree with your recordings within tolerance; discrepancies documented |
| T-016 | Latency instrumentation + histogram report | `recv_at_us` populated on every event; published percentile report |
| **G1** | **Gate 1 review** | All G1 criteria in section 8 met and recorded in `gates/G1.md` |

### Phase C — Rulebook engine (the moat, C4)

| ID | Task | Acceptance criteria |
|---|---|---|
| T-020 | `rulebook/store.py`: fetch, hash, persist rules text | Every market in the universe has a stored `rules_hash`; change detection fires a test event |
| T-021 | `rulebook/extractor.py`: LLM field extraction into the 3.3 schema | On a 50-market labeled sample, field-level accuracy >= 95%; failures route to NEEDS_HUMAN |
| T-022 | `rulebook/equivalence.py`: `check_mece`, `check_link` | Curated negative fixtures (missing "other", mismatched deadline/source/timezone, differing void clauses) are all REJECTED |
| T-023 | Human verdict workflow + audit trail | No link reaches VERIFIED without a stored human decision; rules-hash change invalidates it (test) |

### Phase D — Backtest and first sleeve (→ G2 for S1)

| ID | Task | Acceptance criteria |
|---|---|---|
| T-030 | `backtest/engine.py` event-driven replay | Deterministic: same inputs → identical results across runs |
| T-031 | `backtest/fills.py` three fill models | Pessimistic queue-conservative model implemented per 6.7; models ordered pessimistic <= realistic <= optimistic on every fixture |
| T-032 | Leakage test suite incl. a deliberately-cheating strategy | Suite FAILS the cheating strategy (R11a); all five leakage checks implemented |
| T-033 | Fit `THETA_BY_HORIZON` and single-name adjustment from recorded data | Fitted values stored with the data range and refit procedure documented |
| T-034 | `strategy/s1_structural.py` | Pure function (C4.2a) verified by a determinism test; universe filters unit-tested incl. R2.1b |
| T-035 | `sleeves/S1/PREREGISTRATION.md` | Committed and dated **before** any backtest result is read (R2.5a) |
| **G2-S1** | **Gate 2 review for S1** | Section 8 G2 criteria met under pessimistic fills; recorded in `gates/G2-S1.md` |

### Phase E — Execution and shadow (→ G3)

| ID | Task | Acceptance criteria |
|---|---|---|
| T-040 | `risk/engine.py` with every section 9 limit | Each limit has a test that trips it; sleeve `gate<4` cannot place live orders (I5) |
| T-041 | `execution/oms.py` idempotency + reconciliation | Replay of a `client_order_id` never double-sends; crash mid-send recovers correctly |
| T-042 | `execution/executor.py` desired-vs-actual diffing, post-only enforcement | Post-only rejection handled as information, not retried blindly; every order persists its rationale (C4.2c) |
| T-043 | `execution/killswitch.py` (I9) | Cancel-all within 5s from every process state, proven by tests incl. mid-placement |
| T-044 | `shadow/engine.py` per 7.2 incl. queue-conservative counterfactual fills | Identical sleeve code path to live (asserted by a test); shadow orders never reach a venue (network-level assertion) |
| T-045 | Integration test suite against the **Kalshi demo** environment (P1) | Full order lifecycle: place, partial fill, cancel, error paths, rate limits |
| T-046 | `monitor/` alerts + daily digest | Alerts fire on fill, limit breach, disconnect, drift; digest contains every section 12 KPI |
| **G3-S1** | **Gate 3 review for S1** | 300+ shadow fills, CI excludes zero, all kill switches proven, slippage haircut recorded |

### Phase F — Relative-value sleeves (the core thesis)

| ID | Task | Acceptance criteria |
|---|---|---|
| T-050 | Multi-outcome event map (MECE candidate detection) | Reads `mutually_exclusive` from `/events`; detects every multi-outcome event; precision/recall reported against a labeled sample |
| T-050b | **Exhaustiveness gate** (F1) | The 33 known non-exhaustive live events (LAPRIMARY*, NEWPOPE, STATE51, NEXTTEAMNFL, NBERRECESSQ, ACQUANNOUNCEPINS...) are ALL rejected by `check_mece()`; committed as a regression fixture |
| T-050c | Verify MECNET collateral netting (F4) | Authenticated check of whether Kalshi nets collateral across legs of one MECE event. If `EventPosition.event_exposure_dollars` reflects worst-case rather than per-leg exposure, a short basket needs ~`(1 - sum(bid))` per basket instead of `sum(1 - bid_i)` — an order-of-magnitude capital difference. Documented; S2 ROLC recomputed before any sizing |
| T-050d | **S2-SHORT scanner** (K1) | Implements the short-basket direction with the liquidity filter; reproduces the measured counts (47 events with `sum(bid) > 1`, zero profitable sell-into-bid); partial-fill exposure bounded and reported |
| T-050e | Per-series fee ingestion (K2/K3) | `fee()` reads `fee_type` + `fee_multiplier` from the series cache; unit test asserts maker fee is ZERO on a `quadratic` series and 0.25x on a `quadratic_with_maker_fees` series; the 14 fee-free series are flagged for first live testing |
| T-044b | **Queue-position ground truth** | Kalshi exposes `GET /orders/{order_id}/queue_position` returning `queue_position_fp` (shares ahead of you). Record `(q_true, L2 history)` pairs, fit the L2-only estimator's pessimism parameter `n` so it reproduces `q_true`, then **freeze `n`** and reuse it for historical backtesting where the endpoint is unavailable. Acceptance: R² and bias reported for estimated-vs-true queue position. *Almost nobody has a labelled queue-position dataset; this is the highest-leverage calibration available.* |
| T-053b | Seed link graph from `/milestones` (K6) | Ingests `primary_event_tickers` / `related_event_tickers`; the Hormuz cluster (4 events across 4 series) is discovered without title matching |
| T-051 | `strategy/s2_dutchbook.py` with maker-first pricing | Scan reproduces the 2.6 Dutch-book hurdle table on synthetic books |
| T-052 | `execution/structures.py` lifecycle | Completion, timeout, unwind, orphan accounting all tested; orphan exposure bounded by config |
| T-053 | Link graph builder (L1-L4) over the recorded universe | Candidate links generated; every one routed through the rulebook engine before use |
| T-054 | `strategy/s3_linked_rv.py` | Reproduces the 2.6 pair break-even table; L4 sized at half; VERIFIED-only gate enforced by test |
| T-055 | Pre-registrations for S2 and S3 | Committed and dated before results |
| **G2/G3-S2,S3** | **Gate reviews** | Per section 8, including zero equivalence failures in shadow |

### Phase G — Income, monitoring, and live

| ID | Task | Acceptance criteria |
|---|---|---|
| T-060 | `strategy/s6_liquidity.py` with the Glosten-Milgrom floor and inventory skew | Never quotes inside the computed floor (test); auto-delist on mark-out breach proven |
| T-061 | Calendar service (releases, game starts, model cycles) → quote pull windows | Quotes verifiably pulled in the window around a scheduled event |
| T-062 | `strategy/s7_scanner.py` alert-first | Execution eligibility conditions of 3.5 all enforced; mid-priced gaps route to signal, not execution |
| T-063 | VPS deployment, systemd units, log shipping, NTP | Smoke test passes on the VPS; clock skew alarm tested |
| T-064 | Tax export job | Produces a complete, basis-computed trade export for an arbitrary date range |
| **G4** | **Canary for S1** | Section 8 G4 criteria; one sleeve, `position_cap = 0.01` |

---

## §A — CONSTANTS QUICK REFERENCE

```yaml
fee_theta: {kalshi_taker: 0.07, kalshi_maker: 0.0175, pmus_taker: 0.06, pmus_maker: -0.0125}
lambda_default: 0.5
kelly_multiple: 0.25
position_cap: 0.02
min_half_spread_default: 0.01        # from mu=0.10, L=0.10 -> 1.11c
hedge_rho_floor: 0.80
n_eff_floor: 8
leg_timeout_s: 900
kill_switch_sla_s: 5
fee_death_zone: fee/price > 0.04
kalshi_demo_rest: https://external-api.demo.kalshi.co/trade-api/v2
kalshi_demo_ws:   wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2
```

## §C — ERRATA

Errors found in this plan after publication, and how. Kept visible deliberately: a plan that hides its
corrections cannot be trusted about anything else.

| # | Error | Caught by | Correction |
|---|---|---|---|
| **E1** | §2.2 growth table gave **−31.0 bp** for 1.50× Kelly at a halved true edge. That cell was never computed — the simulation printed only the 0.25/0.50/1.00 rows, and the value was extrapolated by hand when the table was written up. | `tests/test_sizing.py::test_growth_table_matches_plan` | **−38.2 bp**. Verified: `0.525*ln(1.15) + 0.475*ln(0.85) = -0.0038215`. The other five cells of that table check out exactly. |
| **E2** | §2.1 `R2.1b` described the 0.04 fee-ratio limit as excluding "below roughly 13c". Wrong arithmetic: `fee/price = theta*(1-p)` is linear and decreasing, so a 0.04 limit binds at **42.9c**, not 13c. | `tests/test_contracts.py::test_fee_death_zone_boundary_is_43c_not_13c` | Rule text corrected; `fee_death_zone_boundary()` now computes the true boundary so it can never drift from the prose again. |
| **E3** | §3.1 recommended putting first live capital in a "fee-free corner" of 14 series with `fee_multiplier = 0` (research/06 K3). **No such series exist.** The live multiplier distribution is `{1.0: 13,499, 0.5: 19}` — no zeros at all. | `tests/test_kalshi_client_live.py::test_fee_multiplier_distribution`, run against the live public API while building T-010 | Recommendation withdrawn. The constant is retained as `KALSHI_HISTORICALLY_FEE_FREE` and clearly marked. The rest of research/06 §4 reproduces exactly (13,385 `quadratic` / 130 maker-fee / 3 combo, and 19 at half multiplier), so the fee *model* stands — only the waiver cohort was wrong or stale. |

| **E4** | §9's `min_n_eff: 8` is **unreachable given the other limits in §9**. A 2% position cap and 40% gross cap permit at most 20 simultaneous positions, and `n_effective(20, 0.10) = 6.9`. The floor could never be cleared at any portfolio the other limits allow. | Running the engine: `n_eff_floor` denied 28 of 40 quotes on the first live cycle | Cross-theme rho set to **0.05** (the research range's lower bound), making the floor reachable at 20 positions (`n_eff = 10.3`). `RiskEngine.validate()` now **fails loudly** on any self-inconsistent limit set. |
| **E5** | The risk engine applied **intra-theme** rho (0.5) to the **cross-theme** `n_eff` calculation. Since `n_eff` saturates at `1/rho`, that capped it at **2.0** regardless of diversification. | Same cycle — the arithmetic could not produce a passing value | The two correlations are now separate fields with the distinction documented. §2.7 always said intra-theme ≥ 0.5 and cross-theme 0.05–0.10; the code conflated them. |
| **E6** | The drawdown ladder used an exact `>=` comparison, so at *precisely* 20% drawdown it did not engage: `1 - 800000/1000000 = 0.19999999999999996`. | `tests/test_risk.py::test_drawdown_ladder_halves_caps_at_twenty_percent` | `action_for_drawdown()` takes a tolerance. The ladder now fires at the threshold it exists to catch. |
| **E7** | §3.1's S1 spec had no **structural-edge** requirement, so resting at the bid when fair value is the mid always showed a half-spread "edge". The sleeve silently degenerated into pure spread capture — collecting S6's adverse selection without S6's rebates. | `tests/test_s1.py::test_no_quote_when_the_market_is_already_fair` | S1 now requires `p_model − mid >= min_structural_edge` (default 1c): it must believe the **mid itself** is wrong, not merely rest below it. |

**Lesson applied:** every canonical table in §2 is now a parametrized test fixture (T-002 … T-005), so any
future disagreement between the prose and the arithmetic fails the suite rather than silently propagating
into sizing decisions.

| **E8** | The risk engine costed every quote at `price_cents × size`, ignoring `side`. Prices are YES-referenced, so a **NO** leg resting at a YES-price of 5c locks **95c**, not 5c — an under-count of up to **20×** on exactly the legs S2 rests (short baskets are NO quotes at LOW yes-prices). The error fed the position cap, theme cap, venue cap and cash reserve simultaneously. | Found by the S2 sleeve build; S1 emits only `Side.YES`, so it was latent until the first short sleeve existed | `per_contract_cost_cents(side, price)` is now the ONE definition of the rule, and `quote_cost_cents` and the runner's exposure reconstruction both call it. Duplicating it is what allowed it to be wrong in one place and right in another. |
| **E9** | `check_mece()` could **never return VERIFIED**. `Event.exhaustive_verified` was declared on the model and never read, so `safe_to_buy` was permanently `False` and the entire long direction of the RV sleeves was unreachable dead code. | Reading the gate while wiring S2 — no test caught it, because every test asserted the REJECT path | The recorded human verdict is now read. Mechanical checks gate *what may be reviewed*; only the human decision promotes to VERIFIED. |
| **E10** | **I9's 5-second kill guarantee did not hold.** `cancel_all_orders()` cancelled serially through the write bucket (100 tokens/s, 2 per cancel): 100 orders → 1.10s, **300 → 5.10s**, 400 → 7.10s, before any network time. The guarantee silently depended on how many orders happened to be resting. | `tests/test_kalshi_orders.py::test_cancel_all_meets_the_five_second_sla_at_scale` | Cancels now run concurrently through the (thread-safe) bucket. The rate limit still paces the writes; what is removed is per-call latency stacking. A kill switch with a load-dependent guarantee is not a kill switch. |
| **E11** | The runner built a **blank `PortfolioState` every cycle** — full bankroll, zero exposure, nothing deployed. Every limit in §9 was evaluated against an empty book regardless of what was actually resting, so the same quotes were re-approved indefinitely and gross deployment could grow **without bound** while every check reported plenty of room. | Rewiring the loop onto `execution.Executor`; `tests/test_runner.py::test_resting_orders_count_as_deployed_capital` | Exposure is reconstructed from the DATABASE each cycle (I4) — settled positions *and* resting orders, since a resting order locks collateral too. |
| **E12** | The `decisions` table had **no `category` column**. `Decision.category` existed on the model and was dropped silently on the way in. R2.3a requires `β_c` fitted **per category** with empirical-Bayes pooling and removes any category whose posterior β is not credibly above zero — that rule was **not implementable at all**, however good the estimator. | Found by the monitor/KPI build, which could only fit `λ̂` sleeve-wide | Schema v2 adds `category`, plus `orders.decision_id` so a fill can be joined back to the probability that caused it. Without that link, decisions matched outcomes on `(venue, ticker)` alone — wrong the moment one ticker is quoted twice. |
| **E13** | `markout()` took the **first snapshot at or after** the horizon, however distant. The universe sweep records ~1.05 observations per market, so all five horizons (1s … 30m) resolved to the **same row** and KPI 3's decay curve — the thing that separates real maker edge from adverse selection — silently became one number repeated five times. | Found by the monitor/KPI build against the real 107MB database | A per-horizon staleness budget (half the horizon) is now enforced at the source. An unmeasurable horizon reports itself **unmeasured** rather than returning a confident wrong number. |
| **E14** | `ShadowExecutor.submit()` used `INSERT OR REPLACE`, which DELETEs and re-INSERTs: it wiped `structure_id`, discarded any state the OMS had advanced (a filled order could be quietly reset to `open` by a replayed submit), and hardcoded `mode='shadow'` so **BACKTEST orders were mislabelled** in the one table the KPIs read. | Executor integration review | Replaced with an explicit `ON CONFLICT DO UPDATE` that advances `pending → open` only, preserves `structure_id`, and carries the real run mode. |
| **E15** | Three tables specified in §5 — `structures`, `marks`, `links` — were **never in the DDL**. `orphan_loss_ratio` (KPI 6) therefore had no data source, which left the single most expensive failure mode of an RV book (one leg filled, the other not) **unmeasured**. | Monitor/KPI build reported KPI 6 as `available=False` | All three ship in schema v2, with an additive `ALTER`-based migration so the existing 112,086-snapshot database survives in place. |
| **E16** | **Nothing populated `fills` or `settlements`.** `OMS.record_fill()` existed but had no caller outside tests. The engine placed orders and never learned whether they filled or how they resolved — so realised P&L, Brier score, calibration and every gate promotion criterion were permanently empty. The loop had no feedback path at all. | Auditing writers to each table after wiring the runner | `execution/fillfeed.py` and `recorder/settlements.py` close the loop. Shadow settlement must come from MARKET data (`result` on the market), not `/portfolio/settlements`, which is empty by construction when no orders were ever sent. |
| **E17** | `Market.has_ask` admitted `yes_ask == 100`, outside both the 1..99 tick grid and `contracts.fee`'s `0 < p < 1` domain, so any sleeve pricing directly off `yes_ask` raised `ValueError` on such a market. | Found by the S3 sleeve build | Bounded to `1 <= yes_ask <= 99`, mirroring `has_bid`: an ask of 100 means **nobody is offering**, exactly as a bid of 0 means nobody is bidding. |
| **E18** | §3.0 C2 and §3.3 disagreed on the same trade. Both said a 5c implication violation "nets 1.5c double-taker"; §3.3 additionally called it *a loss*. Both cannot hold: `5.00 − 3.4125 = +1.59c`. | Found by the S3 sleeve build | The double-taker case is only a loss **after crossing the spread**: `3.00 − 3.418 = −0.42c`. The text now states which of the three cases it means, and `maker_taker_comparison()` computes all three rather than quoting a number. |

**Lesson applied:** every canonical table in §2 is now a parametrized test fixture (T-002 … T-005), so any
future disagreement between the prose and the arithmetic fails the suite rather than silently propagating
into sizing decisions.

**Second lesson (E4–E7):** four of these seven errors were invisible to inspection and only appeared when
code ran against real data. Three were *mutually inconsistent specifications* — each rule defensible alone,
impossible together. A risk limit that can never be satisfied is not conservative; it is an outage that
looks like a quiet strategy. Hence `RiskEngine.validate()`, which refuses to start on a self-inconsistent
limit set.

| **E19** | `MeceCheck.safe_to_sell` **did not check mutual exclusivity**, though its own docstring said it did. Without it an n-leg short basket is capped at **$n** of liability, not $1. | Running S2 against the live universe: it sized a 21-leg short on `KXBTCD-26AUG2817` — flagged `mutually_exclusive = 0`, listing 50 **nested threshold** markets ("BTC above $66,000", "above $66,500", …) — collecting $11.06 against up to $21 of liability and reporting a `margin` of **$10.01 per contract on an instrument that pays at most $1** | `safe_to_sell` now requires the exchange flag. S2 needed no change: it had trusted the documented contract (`"safe_to_sell already carries mutual exclusivity"`) which was never implemented. Enforcing it cut the book from 83 legs / 11 structures to 16 / 6, and every survivor is a genuine mutually-exclusive set. |
| **E20** | The risk engine judges quotes **one at a time**, so a multi-leg structure whose last leg tripped a cap was placed as a partial — an orphan created *deliberately, at entry*, by the control that exists to prevent exactly that exposure. | Same run: a 4-leg `KXLALIGAGAME` basket had three legs denied on `position_cap` and rested the remaining **one**. A hedged 2c arbitrage silently became a single directional short at full size. | Structures are now **atomic** in the executor: if any leg is denied, every leg of that structure is dropped and reported as `structure_incomplete`. Deliberate partial entry stays available to sleeves that opt in; accidental partial entry is gone. |
| **E21** | `structure_id` was carried only inside `rationale`, so it never reached `orders.structure_id`. Every leg landed with a **NULL structure**. | Inspecting resting orders: 83 legs collapsed into ONE pseudo-structure | `DesiredQuote` gained a typed `structure_id`; S2 and S3 populate it, and the executor passes it through. Leg tracking, orphan detection and KPI 6 all key on that column, so all three were inert before this. |
| **E22** | The backtest's **calibration statistic depended on the order a sleeve emitted its quotes in**. The realised-wins accumulator keyed on ticker and stored a bool, so a sleeve holding both legs of one market had one leg's outcome overwritten by the other's. | Two sleeves with byte-identical fills and identical `gross_pnl_cents`: `actual_wins` 2.0 vs 6.0, `calibration_z` −1.36 vs +1.47, different digests | Accumulate winning **size** and divide by size, matching the size-weighted `p_implied` it is compared against. This also broke T-030 determinism, and `calibration_z` is the ONLY leakage detector that catches a cheat baked in at construction time. |
| **E23** | `check_signal_timestamps` was **silently disarmed by a missing keyword argument**. A sleeve holding its own `Database` handle kept reading the untruncated database, so the deliberately-cheating `LookAheadSleeve` reported **PASS**. One forgotten argument stood between a look-ahead strategy and a G2 promotion. | `tests/test_backtest.py`, which originally *pinned the trap* rather than fixing it | The check now detects a sleeve carrying its own database and **fails closed** when no factory is supplied. An unverifiable claim is not a verified one. |
| **E24** | §R6.7a said gate decisions read "the pessimistic column", inviting the reading that it is a worst-case P&L. It is not: the bracket orders **fill quantity**, and on a losing sleeve fewer fills means a *better* number. | Backtest bracket testing on a negative-edge fixture (see R6.7a) | Wording corrected in place. The rule still stands — the G2 criterion is a per-contract edge CI, roughly invariant to fill quantity — but for a different reason than the text implied. |

**Third lesson (E8–E18):** these eleven divide into three kinds, and the split is the useful part.

*Rules duplicated instead of shared* (E8, E11). The YES-referencing convention was implemented separately
in the risk engine and in the runner's exposure reconstruction, so it could be — and was — right in one
place and wrong in the other. The fix is never "be more careful"; it is to make the second copy impossible.

*Fields declared but never read* (E9, E12, E14). A model field, a table column, and a run mode each existed,
each looked implemented, and none was connected to anything. This class is invisible to code review because
the code reads correctly at every individual site — the defect is the **absence** of a call. The only
reliable detector is an end-to-end test that asserts the value arrives, which is why `tests/test_runner.py`
exists as a distinct suite from the component tests.

| **E25** | `market_result()` collapsed every non-yes/no resolution to `(status, None, False)`, which is what it also returns for an OPEN market. It therefore could not express a **scalar** resolution — a pro-rata payout that is neither YES ($1), NO ($0), nor void-at-cost — and silently discarded `settlement_ts`, which was already in the response it fetched. | Reported by the settlement-ingestion build; **verified directly**: `KXMLBRBI-26AUG261910MILNYM-MILJCHOURIO11-2` has `status='finalized'`, `result='scalar'`, `settlement_value_dollars='0.1600'`, `expiration_value='Cancelled'` | New `market_settlement()` returns a `MarketSettlement` with an honest `kind` (yes/no/void/scalar/unknown/open), the scalar payout in cents, and an exact `settled_at_us`. `market_result()` is kept as a wrapper so existing callers are unaffected. |

| **E26** | **`_to_venue_side()` mirrored a price the sleeves had already sent YES-referenced.** `orders.price_cents` is YES-referenced on both sides, and both sleeves say so at their emission sites (`s3_linked_rv.py`: *"rest a YES ask at this YES price"*). The venue boundary read a NO quote's price as a NO price and sent `100 - p`. | Found INDEPENDENTLY by two reviews on the same day, from opposite directions (fill ingestion and structure lifecycle) | An S2 leg meant to rest as a YES ask at 5c reached Kalshi as a YES ask at **95c** — not a mispriced order but a different one, on the wrong side of the book, which could never fill where it was aimed and could fill where it was not. The boundary now changes the verb and never the price. `_book_context` carried the same mirror, so shadow queue-ahead for every NO leg was read off the wrong book level. |
| **E27** | `monitor/kpi.py` differenced `fills.price_cents` against `orders.price_cents` in two places — across **two different price conventions**, wrong by `(100 - 2p)` on every NO fill. That is the entire signal at any price away from 50c. | Reported by the fill-ingestion build, which is what made the seam reachable | Both sites now convert the fill to the YES reference before use. |
| **E28** | The `structures` table shipped in schema v2 with column names (`target_edge_cents`, `realised_edge_cents`) that did not match the KPI code already written to read them. | The suite, immediately — `no such column: realized_margin_cents` | Renamed to the consumer's names. A table nobody can query is not a table. |

**The two price conventions — a deliberate, documented seam.** After E26 the system holds exactly one
inconsistency, and it is on purpose:

    orders.price_cents   YES-referenced   (a NO quote at p means "rest a YES ask at p")
    fills.price_cents    SIDE-referenced  (a NO fill at q means "the NO contract cost q")

`OMS.position` converts at the read (`yes = 100 - price` for a NO fill) and the runner's capital
arithmetic depends on that conversion, so the fill side is load-bearing where it is. Unifying on
YES-referencing would fix the two KPI sites *and* break the position and capital paths, which is the
strictly worse trade — so the seam stays, and every crossing of it is now commented at the crossing
point rather than assumed. **Any new code that joins `fills` to `orders` must convert.** This is the
single most dangerous piece of local knowledge in the codebase; if it ever needs to change, change it
in `OMS.position` first and let the compiler and tests find the rest.

**Scalar resolutions and the S2 premise — an open risk, measured but not closed.** S2's entire arithmetic
assumes exactly one leg of a mutually-exclusive basket pays $1, which is what makes short liability
"capped at $1" (see E19). A **scalar leg pays something in between**, so that cap does not hold as stated.

What is measured: the one confirmed scalar market sits in `KXMLBRBI-26AUG261910MILNYM` ("Milwaukee vs
New York M: RBIs"), a 39-leg player-prop event resolving 8 YES / 29 NO / **2 scalar**, and that event is
`mutually_exclusive = False`. Scalars there come from scratched players. A separate 12,000-market sweep of
`status=settled` returned **zero** scalars, so they are concentrated in specific series rather than spread
across the universe.

What is NOT established: that a scalar can never occur inside a mutually-exclusive event. One event is a
data point, not a proof, and S2 trades only mutually-exclusive events — so on current evidence the
exposure is zero and the failure mode is real. `MarketSettlement.pays_binary` exists so the RV sleeves can
assert the binary assumption instead of inheriting it, and the settlement recorder refuses to record a
scalar rather than mis-scoring it as NO. Closing this properly needs a scalar observed (or ruled out)
inside a MECE event, which is a live-data question, not one to settle by argument.

**Fourth lesson (E19–E24):** the expensive ones all had the same shape — **a stated contract that
nothing enforced**. `safe_to_sell` documented a mutual-exclusivity requirement it did not check; S2 read
that docstring and trusted it. `structure_id` was specified, carried, and never persisted. A leakage check
announced PASS for a condition it had not tested. In each case the *prose was correct* and the code
disagreed with it silently, which is strictly worse than having no rule: a documented guarantee gets
depended on.

Two practical consequences. First, a check that cannot be performed must report **FAILURE, not success** —
E23's disarmed detector is the pure case, but E19 is the same error wearing different clothes. Second, the
tell for this class is an *unread field*: `mutually_exclusive`, `exhaustive_verified` (E9), `category`
(E12), `structure_id` (E21) were all declared, all plausible on inspection, and all connected to nothing.
Grepping for writes without reads found more real bugs here than any amount of re-reading the logic.

*Guarantees that hold only at small scale* (E10, E13, E16). The kill switch worked at 100 orders and failed
at 300; the mark-out was correct against a dense tape and degenerate against the real sparse one; the fill
path was fine as long as nothing needed to know whether orders filled. Each was true when written and false
in production, and **none of them fail loudly** — they return plausible numbers. A guarantee whose validity
depends on load must state the load, and a measurement that cannot be made must report itself unmade rather
than returning a confident wrong answer.

---

## §B — SOURCE INDEX

Full annotations live in `research/01..04-*.md`. Load-bearing citations:

- Bürgi, Deng & Whelan 2026, *Makers and Takers: The Economics of the Kalshi Prediction Market* —
  maker/taker returns, favorite-longshot bias magnitude, capacity. https://www.karlwhelan.com/Papers/Kalshi.pdf
- Bartlett & O'Hara 2026, *Adverse Selection in Prediction Markets* — single-name YES bias, VPIN toxicity.
  https://law.stanford.edu/2026/04/21/adverse-selection-in-prediction-markets-evidence-from-kalshi/
- Domain calibration over 292M trades — horizon recalibration theta, political underconfidence, weather
  overconfidence. https://arxiv.org/html/2602.19520v1
- UCLA NBA microstructure 2026 — arbitrage capacity, episode durations, executable size.
  https://arxiv.org/pdf/2605.00864
- Saguillo et al. — $40M of Polymarket arbitrage extraction. https://arxiv.org/abs/2508.03474
- Kelly 1956; Thorp 2006 (fractional-Kelly drawdown laws); MacLean, Thorp & Ziemba 2011.
- Glosten & Milgrom 1985 (adverse-selection spread); Avellaneda & Stoikov 2008 (inventory skew).
- Murphy 1973 (Brier decomposition); Wilson 1927; Benjamini & Hochberg 1995; O'Brien & Fleming 1979.
- Kalshi API docs https://docs.kalshi.com · demo environment https://docs.kalshi.com/getting_started/demo_env
- Polymarket US docs https://docs.polymarket.us · developer keys https://polymarket.us/developer
- Becker bulk dataset https://github.com/jon-becker/prediction-market-analysis

---

**Disclaimer.** This plan is educational research and engineering specification, not investment, legal, tax,
or immigration advice. Its author is not a licensed advisor. Simulated results assume the stated edges exist
and persist — which is exactly what the gate system exists to test. Most prediction-market participants lose
money.
