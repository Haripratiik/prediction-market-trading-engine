# Microstructure & Market Making for Binary Expiring Contracts

Implementation reference for the quoting and execution layer. Formulas are dollars per contract with $1
settlement. Sources are primary papers, read directly, plus venue documentation.

---

## 0. The structural fact that reorganizes everything

For a contract paying $1 on an event, if `p_t` is a martingale converging to `p_T ∈ {0,1}`, then because
`p_T² = p_T`:

```
Var(p_T | F_t) = E[p_T²] − p_t² = p_t − p_t² = p_t(1 − p_t)
```

**The total remaining variance of a binary is `p(1−p)` — closed form, model-free, no volatility parameter,
and no dependence on time to expiry.** Time only decides *how* that fixed budget is spread out.

Three separate quantities scale as `p(1−p)`:

| Quantity | Formula |
|---|---|
| Remaining settlement variance | `p(1−p)` |
| Glosten–Milgrom break-even spread | `4μ·p(1−p) / (1 − μ²(2p−1)²)` |
| Kalshi / Polymarket fee | `feeRate · p(1−p)` |

All three are second moments of a Bernoulli. **Quote a spread proportional to `p(1−p)`, and normalize every
risk, fee, and signal measurement by `p(1−p)` or work in log-odds.**

### Why basis points are the wrong unit here

| p | 1c spread as bps of price | as bps of $1 notional | ÷ √(p(1−p)) | in log-odds |
|---|---:|---:|---:|---:|
| 0.02 | 5000 | 100 | 0.0714 | 0.5210 |
| 0.10 | 1000 | 100 | 0.0333 | 0.1112 |
| 0.50 | 200 | 100 | 0.0200 | 0.0400 |
| 0.95 | 105 | 100 | 0.0459 | 0.2112 |

Relative spread varies 50× across the book *mechanically*. Use **cents per contract**, **risk units
(spread ÷ √(p(1−p)))**, or **log-odds**. A 1-cent tick at p=0.02 is a 0.42 log-odds move — **10.4× the
information content** of a 1-cent tick at p=0.50.

---

## 1. Adverse selection

### 1.1 Glosten–Milgrom for a binary, and inverting it

With `V ∈ {0,1}`, prior `p`, informed fraction `μ`:

```
A = p(1+μ) / (1 + μ(2p−1))        B = p(1−μ) / (1 − μ(2p−1))
S = A − B = 4μ·p(1−p) / (1 − μ²(2p−1)²)
```

At p = ½ this reduces to `S = μ`: **at the money, the fair spread equals the informed-flow fraction.**

Inverting observed spreads to implied μ is a direct diagnostic:

| Observation | p | spread | implied μ |
|---|---:|---:|---:|
| Kalshi mid, tight | 0.50 | 1c | **1.0%** |
| Kalshi mid, typical | 0.50 | 2c | **2.0%** |
| Polymarket [0.40,0.60] median | 0.50 | 4c | 4.0% |
| Kalshi tail | 0.05 | 1c | 5.3% |
| Polymarket p<0.10 | 0.07 | 13c | **43.1%** |
| Polymarket p<0.10 | 0.07 | 18c | **54.1%** |

Kalshi's mid-book spreads imply 1–2% informed flow — plausible for a retail venue. Polymarket's tail
spreads would require 43–54% of flow to be perfectly informed, which is not credible. **Those tail spreads
are not an adverse-selection equilibrium** — they are capital lockup, weak competition, and inventory cost.
That is where room exists, and also where you must ask what else is keeping everyone out.

### 1.2 Kyle's λ and empirical estimation

`λ = ½·√Σ₀/σ_u`. Three estimators, cheapest first:

1. **Direct regression** `ΔP_k = α + λ·OF_k + ε` over signed order flow. **On Kalshi you get the taker side
   from the API — no Lee-Ready needed.**
2. Amihud proxy, using `|Δp|/volume` in absolute cents (relative return is meaningless for a binary).
3. Depth-implied: `λ ≈ 1/(2·AD)` from average top-of-book depth.

Anchor: Polymarket's 2024 Trump market saw λ fall from **0.53 (July) to 0.01 (October)** as depth built.

### 1.3 VPIN, simplified and adapted

Standard VPIN over volume buckets with Bulk Volume Classification collapses to a one-liner, since
`V^B − V^S = V(2Z−1)`:

```
VPIN = (1/n) · Σ |2·Z(ΔP_τ/σ) − 1|        ∈ [0,1]
```

**Binary-specific adaptation: standardize in log-odds, not price.** `L = ln(p/(1−p))`, use `Z(ΔL_τ/σ_L)`.
Otherwise the mechanical `p(1−p)` heteroskedasticity makes VPIN read high in the tails (where a 1-tick move
is 10× larger in log-odds) and low near 0.50.

**On Kalshi skip BVC entirely** — the API labels the taker side, so use true classification.

Known failure modes: Andersen & Bondarenko find no incremental predictive power for volatility after
controlling for volume and volatility; VPIN correlates with both **by construction**. Treat it as a
regime/kill-switch input, never as alpha. Bartlett & O'Hara did find an adapted VPIN predicts maker losses
in single-name Kalshi markets specifically, which earns it a place as a **toxicity gate** — validated
against your own realized mark-outs, not against volatility.

### 1.4 Mark-out methodology

```
Effective spread  ES = 2·D·(P − M)
Realized spread   RS(h) = 2·D·(P − M_{t+h})
Price impact      PI(h) = 2·D·(M_{t+h} − M)
Identity          ES ≡ RS(h) + PI(h)
```

**For the maker, `markout(h) = RS(h)/2` per contract, exactly.**

Recipe:
- **Horizons:** compute the whole curve — `1s, 5s, 15s, 60s, 300s, 1800s, and settlement`. The settlement
  mark-out is uniquely available in prediction markets and is the only *unbiased* one:
  `markout(∞) = D·(P − Y)` with `Y ∈ {0,1}`. Equities researchers cannot do this. Use it.
- **Reference price: the microprice (§3.1), not the mid.** In a 1-cent-tick book the mid is badly quantized
  and will manufacture spurious mean-reversion in your mark-outs.
- **Units:** cents per contract, and cents ÷ √(p(1−p)). Never bps of price.
- **Interpretation:** healthy is `markout(1s) ≈ half-spread`, decaying to a positive plateau. Negative by
  15–60s means you are picked off faster than you capture spread. **Positive at 1s but negative at
  settlement means you are capturing spread but sitting on the wrong side of the favorite–longshot bias.**

Conrad & Wahal (JFE 2020): realized-spread term structure is sharply downward sloping; most price impact
occurs within **15s for large caps, 60s for small caps**. SEC Rule 605 uses 5 minutes.

---

## 2. Optimal quoting, adapted to binaries

### 2.1 Avellaneda–Stoikov, and the one thing that does not port

```
reservation price:    r = s − q·γσ²(T−t)
optimal total spread: δ^a + δ^b = γσ²(T−t) + (2/γ)·ln(1 + γ/k)
```

Spread is **independent of inventory**; inventory enters purely as a **skew of the quote centre**.

**The binary substitution.** Since the inventory penalty is (risk aversion) × (remaining variance), and for
a binary that is exactly `p(1−p)`:

```
r = p − γ·q·p(1−p)
ψ = γ·p(1−p) + (2/γ)·ln(1 + γ/k)
```

> ### The correction that will bite you if you port AS naively
>
> In AS the inventory penalty `γσ²(T−t) → 0` as `t → T`, because you liquidate at the mid at T.
> **In a binary there is no liquidation at the mid — you settle at 0 or 1.** The penalty `γp(1−p)` does
> **not** decay with time. **A 50c contract one minute before settlement carries exactly the same
> per-contract risk as it did a month earlier.**
>
> Time to expiry does not reduce inventory risk; only price convergence toward a boundary does. This is
> what Feil & Nendel's terminal penalty encodes, and it is why their optimal spread *widens* near
> settlement at p = ½. **Never let the quoter relax inventory discipline near the close.**

### 2.2 Better: exact CARA, three lines, no Gaussian approximation

The terminal distribution is Bernoulli, so the certainty equivalent is closed-form:

```python
def CE(q, p, g):                 # certainty equivalent of holding q contracts
    return -(1/g) * log(p*exp(-g*q) + (1-p))

r_bid = CE(q+1, p, g) - CE(q,   p, g)
r_ask = CE(q,   p, g) - CE(q-1, p, g)
```

Its small-γ expansion reproduces AS exactly, and it is **strictly better in the tails because it respects
the [0,1] bound**:

| p | γ | q | exact reservation mid | AS approximation |
|---|---:|---:|---:|---:|
| 0.50 | 5e−3 | 200 | **26.89c** | 25.00c |
| 0.20 | 5e−3 | 200 | **8.42c** | 4.00c |
| 0.05 | 5e−3 | 200 | **1.90c** | 0.25c |

The approximation degenerates to nonsense (0.25c) exactly where you most need it.

### 2.3 Parameters

**`k` (fill-intensity decay).** Estimate by posting at two distances and measuring fill rates:
`k = ln(λ₂/λ₁)/(δ₁−δ₂)`. Equivalently, if moving one extra tick out multiplies fill rate by `f`:

| f | k | AS half-spread 1/k | full spread |
|---:|---:|---:|---:|
| 0.8 | 22.3 | 4.48c | 8.96c |
| 0.7 | 35.7 | 2.80c | 5.61c |
| 0.6 | 51.1 | 1.96c | 3.92c |
| 0.5 | 69.3 | 1.44c | 2.89c |
| 0.4 | 91.6 | 1.09c | 2.18c |

Feil & Nendel calibrate `k ∈ [35, 85]` → half-spreads of **1.2–2.9c**, consistent with observed Kalshi
books. Use that as a reality check on your own estimate.

**`γ` — do not estimate from utility. Back it out from your inventory cap:**

```
γ = δ_skew_max / (q_max · p(1−p))
```

At `q_max = 500`, `δ_skew_max = 0.02`, `p = 0.5` → `γ = 1.6e−4`.

**Practical simplification:** the base term `2/k` dominates the inventory term for any sane γ (at k=35,
γ=5e−3, p=0.5: 5.71c vs 0.125c). **Set width from `k`, use inventory purely as skew.** You do not need a
PDE to capture ~95% of the benefit.

### 2.4 The literature that exists for this exact problem

- **Feil & Nendel (2026), *Optimal Market Making in Prediction Markets*, [arXiv:2607.17991](https://arxiv.org/abs/2607.17991)** — the direct paper. Terminal penalty `Φ(p,q) = −γ_T q² p(1−p)`. Findings: spreads widen near settlement at p=½; inventory skew reverses sign at p=½. Monte Carlo vs myopic baseline: P&L −0.6% but **sd −63%, 5% VaR −87%, terminal inventory −69%**.
- **Xi, Moallemi, Pai & Wang (2026), [arXiv:2607.08199](https://arxiv.org/html/2607.08199)** — the volatility model to plug in: `h² = p(1−p)/τ + K·ν(V)·s²/4` with `ν(V)=√V`, **one free parameter**. Fitted on 880,719 Kalshi contract-hours; 40% better interval score than GARCH(1,1); **globally pooled parameters beat category-specific refits.**
- Guéant–Lehalle–Fernandez-Tapia closed forms; Guéant (2017) reduces the HJB to a **tridiagonal linear ODE in inventory**, solvable by matrix exponential.

---

## 3. Queue position and fill probability

### 3.1 The parameter-free imbalance formula

Cont & de Larrard (2013). For a balanced book, `P(next move is up)` given queue sizes has an exact integral
form — and a closed-form approximation **verified numerically to ±0.0005 for queues ≥ 5**:

```
P(mid up) ≈ (2/π) · arctan(q_bid / q_ask)
```

| Estimator | max error | mean error |
|---|---:|---:|
| **(2/π)·arctan(bid/ask)** | **0.0072** | **0.0001** |
| naive imbalance `bid/(bid+ask)` | 0.0452 | 0.0279 |

**Use arctan, not linear imbalance.** The naive form is biased toward 0.5 by up to **4.5 probability
points** — on a 1-cent tick against a $1 contract that is 4.5 cents of fair-value error, larger than the
entire spread. It requires **no calibration whatsoever** in the balanced case.

Two companion results: duration between price moves is **heavy-tailed with infinite mean** — never report
an "average time to fill", use quantiles. And volatility can be estimated from order flow alone via
`σ = δ√(nπλ/D(f))`.

### 3.2 Microprice

Stoikov (2018). Build an absorbing Markov chain over (spread bucket, imbalance bucket) states where the mid
has not yet moved; then `microprice = mid + G*[state]` with `G* = (I − B)^{-1}G1`. Six terms of the series
suffice.

**In a 1-cent-tick binary book the mid is badly quantized** — it lives on a half-cent grid while fair value
moves continuously. Use the microprice as the fair-value reference everywhere, especially in mark-outs.

**In thin books**, top-of-book imbalance is dominated by one participant's order. Two defenses:
(a) compute imbalance over **cumulative top-N levels** — Polymarket depth is near-uniform across levels
(median L1/top-10 = 0.137 vs 0.10 for uniform), so L1-only discards most of the information;
(b) **subtract your own resting size** before computing imbalance.

### 3.3 Estimating queue position — and Kalshi's gift

Standard practitioner model (hftbacktest): assume you join at the back; queue advances only on trades;
cancellations are probabilistically attributed ahead/behind with a pessimism dial `n ∈ [1,3]`.

> **Kalshi exposes ground truth: `GET /orders/{order_id}/queue_position` returns `queue_position_fp`, the
> number of preceding shares ahead of your order.**
>
> Use it twice: (a) live, feed real `q` into the fill model; (b) **as a labelled training set** — record
> `(q_true, L2 history)` pairs and fit `n` so the L2-only estimator reproduces `q_true`, then freeze `n` and
> use it for historical backtesting where the endpoint is unavailable.
>
> **Almost nobody has a labelled queue-position dataset. This is the single highest-leverage item in this
> document.**

### 3.4 The value of queue position, and the join-vs-improve rule

Moallemi & Yuan's accounting identity:

```
V = α(q) · (δ − AS(q))
    α = fill probability     δ = spread premium     AS = adverse-selection cost
```

Fit `α(q) = α_∞ + (α_0 − α_∞)·e^{−bq}` to your realized fills bucketed by join depth — three parameters,
and the right functional form even without the full model.

**Join-vs-improve.** With edge `E`, tick `Δ`, adverse selection `AS`:

```
improve iff   α_imp/α_join > (E − AS) / (E − Δ − AS)
```

| edge E | AS | required fill-probability ratio to justify improving |
|---:|---:|---|
| 1.5c | 0 | **3.00×** |
| 1.5c | 0.5c | **never** |
| 2.0c | 0.5c | 3.00× |
| 3.0c | 0.5c | 1.67× |
| 5.0c | 0.5c | 1.29× |

**On a 1-cent tick, unless your edge is ≥ 3c, improving must double or triple your fill probability to
break even — and it rarely does.** Moallemi & Yuan measured the *entire* front-to-back queue value at
0.21–0.26 ticks. **Join the queue; do not penny.** The exception is a queue so long that `α_join → 0`; the
exponential fit gives a critical length `Q*` above which improving wins.

### 3.5 Adverse selection increases with queue depth — the common intuition is backwards

`AS(q) = β(q)/α(q)` is **increasing in q**: orders at the back of a long queue get filled by *large* trades,
and large trades carry more impact. Empirically 0.3 ticks at the front → 0.7 ticks deep.

**Front of queue is better on both axes: higher fill probability AND lower adverse selection.** That is
precisely why queue position has value.

Independent confirmation: a one-sd increase in contracts ahead reduces average order size by 20%, versus
7.5% for a one-sd inventory increase — **adverse-selection risk drives sizing 2.6× as strongly as inventory
risk.**

**But do not chase imbalance.** Albers et al. (2025) find a *negative* correlation between fill likelihood
and post-fill returns: orders more likely to execute produce worse subsequent moves, and imbalance-following
maker strategies are unprofitable. Use `(2/π)arctan(q_b/q_a)` to **skew or withdraw**, not to chase.

### 3.6 Honest maker-fill backtesting

Lo, MacKinlay & Zhang (JFE 2002) constructed hypothetical fills two ways against actual executions:

| Stock | touch-fill (optimistic) | trade-through (pessimistic) | **actual** |
|---|---:|---:|---:|
| ABT | 15.58 min | 60.12 min | **25.39 min** |
| IBM | 16.80 | 43.26 | **23.41** |

**Touch-fill overstates fill speed ~1.6×; trade-through understates ~2.4×; the bounds are ~3.9× apart.**
Report the bracket, not a point. **If a strategy is only profitable at the optimistic bound, it does not
exist.** They also treat cancellations as **censored, not as non-fills** — a bias most backtests get wrong.

**The calibration gate that catches a too-generous fill model.** Realized adverse-fill rates on CME futures
(April 2024): ES **81.5%**, NQ **65.8%**, CL **82.9%**, ZN **88.8%**.

> **Two-thirds to nine-tenths of maker fills are immediately adverse. If your simulator produces ~50%
> adverse fills, it is handing you fills the real market would not have.**

Concrete rules: (1) fill a resting bid at `p` only when a trade prints strictly *below* `p` — a print *at*
`p` means someone ahead of you filled; (2) never let `front_q_qty` increase except via `min(front, new_qty)`;
(3) residual after a partial stays at the **front**; (4) model feed and order latency separately and
**simulate cancels as sometimes losing the race**; (5) queue resets on any reprice; (6) fees at the correct
granularity — Kalshi rounds **per order**, so a 1-lot at 50c pays ⌈1.75⌉ = 2c, a **14% rounding penalty**
versus the 100-lot rate.

**Count fill-model choices (`n`, latency, fill rule) as trials.** Calibrate `n` against held-out live fills,
freeze it, *then* evaluate the strategy. Tuning `n` until the backtest looks good is overfitting the
simulator.

---

## 4. Execution economics

### 4.1 The maker/taker decision under a `p(1−p)` fee

```
Take now:  E[π] = (V − P_ask) − r_taker·P_ask(1−P_ask)
Post bid:  E[π] = θ · [(V − P_bid) − r_maker·P_bid(1−P_bid)]
Post iff   θ > θ* = take_edge / make_edge_conditional
```

**Required mispricing just to break even on the taker fee alone:**

| ask | taker fee | as % of price |
|---:|---:|---:|
| 5c | 0.333c | **6.65%** |
| 10c | 0.630c | 6.30% |
| 50c | 1.750c | 3.50% |
| 90c | 0.630c | 0.70% |
| 95c | 0.333c | 0.35% |

**A taker round trip costs 1.26 ticks at p=0.10, 2.94 at p=0.30, 3.50 at p=0.50.** In a market whose entire
spread is 1–3c, **round-tripping as a taker is structurally unprofitable.** Almost everything must be maker.

### 4.2 Square-root impact — the capacity constraint retail bots violate most

Adapted to binaries (absolute price, remaining sd):

```
ΔP = Y · √(p(1−p)) · √(Q/V)          Y ≈ 0.5–1.0
```

Using Kalshi's actual size distribution (median $8,982 staked ≈ 18,000 contracts):

| p | market size | order Q | impact (Y=0.5) | impact (Y=1.0) |
|---:|---:|---:|---:|---:|
| 0.50 | 18,000 | 100 | 1.86c | 3.73c |
| 0.50 | 18,000 | **500** | **4.17c** | **8.33c** |
| 0.50 | 18,000 | 2,000 | 8.33c | 16.67c |
| 0.50 | 120,000 | 500 | 1.61c | 3.23c |

> **A 500-contract order — $250 at 50c — moves the median Kalshi market 4–8 cents.**
>
> At retail scale **you are already a large trader in the median prediction market.** Size relative to
> market volume, not relative to bankroll. This is the single most-violated constraint in retail bots.

Suggested gate: **order size ≤ ~0.5% of the market's expected lifetime volume.**

### 4.3 Alpha decay and latency reality

Fraction of a signal surviving execution horizon `T` with half-life `h` is `2^(−T/h)`:

| half-life | T=5s | T=30s | T=120s | T=600s |
|---:|---:|---:|---:|---:|
| 30s | 89.1% | 50.0% | 6.2% | 0.0% |
| 120s | 97.2% | 84.1% | 50.0% | 3.1% |
| 600s | 99.4% | 96.6% | 87.1% | 50.0% |

**Your execution horizon must be well under your signal half-life.** At retail latency (50–500ms, REST/WS,
no colocation) any sub-second signal is unreachable — do not build strategies that need them. Order-book
imbalance decays in tens of milliseconds in liquid venues but far slower in thin prediction-market books,
which is exactly why retail participation is viable here and not in equities.

### 4.4 Rate limits shape the quoter's architecture

Kalshi token bucket: Basic 200 reads / 100 writes per second. **Most requests cost 10 tokens; cancellations
cost 2.** Batch items bill separately (25 creates = 250 tokens). Breach returns 429 **with no `Retry-After`
or `X-RateLimit-*` headers** — implement bounded exponential backoff yourself.

> **Cancels are 5× cheaper than creates. Structure the quoter to cancel aggressively and re-create
> selectively, not to churn creates.**

Amend-vs-replace: any *price* change loses queue priority; a size *reduction* usually preserves it, a size
*increase* usually does not. **Prefer reducing over cancel-replace when trimming.**

---

## 5. Inventory in a binary book

```
terminal P&L variance = q²·p(1−p)              exact
max loss (q > 0)      = q·p
max loss (q < 0)      = |q|·(1−p)
```

No fat tails outside [0,1], no gap risk beyond settlement. **`p(1−p)` is your position-limit currency: cap
`Σ q_i² p_i(1−p_i)` across the book, not `Σ|q_i|`.**

**YES/NO netting.** 1 YES + 1 NO = $1 guaranteed. Holding `q_Y` YES and `q_N` NO gives payoff
`q_N + (q_Y − q_N)·1{event}` — so **net exposure is `δ = q_Y − q_N` and `min(q_Y,q_N)` is riskless cash.**
A YES bid at `b` *is* a NO ask at `1−b`; there is one book, mirrored, and the YES spread equals the NO
spread identically. For multi-outcome events the analogue of a Greek is the **vector of net exposures across
mutually exclusive outcomes**, subject to `Σp_i = 1`. Hedge at the event level, not the ticker level.

**The two-sided round trip.** Post a bid to buy YES at `b` and a bid to buy NO at `1−a`; if both fill:

```
paid = 1 − spread     received = $1 at settlement     profit = a − b = the quoted spread, exactly
capital ≈ $1 per contract-pair
```

At a hypothetical 1.75% maker rate, a **1-cent spread at p=0.50 nets 0.12c — essentially nothing**; you
need ≥2c of spread to quote two-sided at the money. Because the fee is `∝ p(1−p)`, it bites hardest at
p≈0.5 and is 3× cheaper in the tails. *(On Kalshi, maker fees apply to only 130 of 13,486 series — see
`06-kalshi-structure.md` §4 — so on most series this constraint vanishes.)*

**Capital efficiency:** there is no leverage; a two-sided quote locks ~$1 per contract-pair. At a 2c net
spread and one turn per day, gross ≈ 2% of bankroll per day *before* adverse selection, which is where most
of it goes. **At retail scale the rewards programs are likely a larger and more reliable revenue line than
spread capture.**

---

## 6. Prediction-market empirics that matter for quoting

### 6.1 Kalshi (Bürgi, Deng & Whelan, Jan 2026 — 313,972 contract prices)

- **Median money staked per contract: $8,982.** Mean $61,977. Top decile $526,245.
- **Mean transaction $100; median $35.** These are retail-scale books.
- **Price distribution is a barbell:** 33.8% of prices in 1–10c, 33.8% in 90–99c, **only 2.7% in 50–59c.**
- Favorite–longshot bias: average pre-fee return **−20%**; contracts ≤10c lose **>60%**; contracts >70c earn
  a small significant positive post-fee return. **Makers −9.64%, takers −31.46%; makers buying ≥50c +2.6%.**
- Mincer–Zarnowitz `ψ = 0.034 (SE 0.005)`, break-even price **≈51c**. Rejected at **every** horizon.
- By year: 2024 ψ=0.048 → 2025 ψ=**0.021** — weakening, not vanished.
- By transaction-size quintile: Q1 0.036 → **Q5 0.043**. **The largest trades show the largest bias — size
  does not arbitrage it away.**
- **Maker share of purchases rises monotonically with price: 43.5% at 1–10c → 56.5% at 90–99c.**

> **Implication for the quoter, and it is precise:** the maker edge on Kalshi is not purely spread capture —
> a material part is harvesting a directional behavioral bias. **Your inventory will drift systematically
> short YES in longshot markets.** That is a real edge with negative skew. Take it deliberately and size it,
> or hedge it out — but never let it accumulate by accident.

### 6.2 Polymarket order books (Dubach 2026 — ~30 billion events, 623.8 GB)

| Finding | Number |
|---|---|
| Longshot spread premium | median half-spread ~400 bps in [0.40,0.60]; **1,300–1,800 bps below 0.10** |
| Depth is near-uniform, not top-heavy | median L1 ÷ top-10 depth = **0.137** (uniform benchmark 0.10) |
| Maker concentration | median Herfindahl 0.031 (~32 effective makers); p90 0.119 (~8) |
| Feed latency | p50 **41.5 ms**, p90 166 ms, p99 6,108 ms |
| Depth vs time-to-close | ~6% depth reduction per tenfold decrease in time to expiry |

> **Critical warning for anyone backtesting the public feed:** feed-inferred trade direction agrees with
> on-chain ground truth on only **~59% of buckets**. Downstream **sign-flip rates are 67% for effective
> half-spread and 60% for Kyle's λ.** If you infer direction from the public book, your adverse-selection
> estimates are coin flips. Use on-chain ground truth — or trade Kalshi, where the venue labels the taker.

### 6.3 Liquidity is bimodal, not continuous

Whelan (2026) on Betfair (200,000+ matches, full book at 1-second intervals): the model produces **multiple
equilibria — thick and thin**. When matching probability is high, makers need less price improvement, so
spreads tighten, which raises matching further; when it is low, makers demand wider spreads, which reduces
matching further.

**Expect a market to be either genuinely quotable or not quotable at all, with little in between.** Do not
plan to "build liquidity" in a thin book by quoting it.

### 6.4 Near settlement: the maker edge inverts

Accuracy improves monotonically into the close, but the bias never disappears. **The "Yogi Berra effect"
replicates on three venues:** on Kalshi, closing-day maker losses on cheap contracts become as bad as taker
losses; on Betfair, "longshot bets generate large, systematic losses **even for liquidity providers**"; on
Intrade, losing-team prices in the final 15 minutes were too high.

**Settlement manipulation in ultra-short markets** (Polymarket BTC 5-/15-minute, >$4B volume): ~1,600 cycles
(~6%) classified as manipulated on final-ten-second order flow; **56% overnight, 44% weekends**; 821
manipulators captured **$8.2M**; order-flow spike ~3.9× larger in near-the-money cycles, concentrated in the
**final 50 seconds**, with price reversing within ten seconds.

> **Hard rule: do not quote passively into the last minute of near-the-money, oracle-settled ultra-short
> contracts, especially overnight and at weekends.**

### 6.5 Arbitrage is depth-constrained, not detection-constrained

- Polymarket, 259M trades: total arbitrage realized **$1.12M**; **top 10 addresses captured 75%**; ~80% of
  conversions used maker execution. Persistence: YES-side median episode **16.15s**, NO-side **7.99s**.
- NBA study, 75M snapshots: single-market arbitrage existed **0.0001% of the time**; combinatorial 0.1762%
  (median 16s, median yield 101 bps); **76.9% liquidity-constrained, average executable size 14.8 shares.**
- Deviation half-lives on Polymarket collapsed from hours (mid-2024) to **0.67–0.74 minutes** (Oct–Nov 2024).
- **Key concept: payoff-space no-arbitrage ≠ protocol-executable no-arbitrage.** A bundle violation need not
  be exploitable given actual conversion mechanics.
- PredictIt's persistent 250–290c candidate sums existed because a **10% fee on profit** creates a hard
  wedge — minimum profitable bid-sum was **$1.11**. **Violation persistence is governed by
  `fee structure × position limit × capital recycling`, not by how many people can see it.**

---

## 7. Build order and validation gates

**Tier 1 — no calibration required:**
1. `P(mid up) = (2/π)·arctan(q_bid/q_ask)` — replaces linear imbalance (wrong by up to 4.5 points).
2. Fair value = microprice over cumulative top-N depth, with your own size subtracted.
3. Quotes: `r = p_micro − γ·q·p(1−p)`; `ψ = max(tick, (2/γ)ln(1+γ/k) + γ·p(1−p))`; `γ = δ_skew_max/(q_max·p(1−p))`.
4. Kalshi `queue_position` in the live loop; explicit `self_trade_prevention_type`; `post_only` on all quotes.
5. Every backtest reported under **both** touch-fill and trade-through.

**Tier 2 — cheap calibration:**
6. `k` from your own fill-rate-vs-distance data.
7. `α(q) = α_∞ + (α_0−α_∞)e^{−bq}` fit to realized fills bucketed by join depth.
8. Join-vs-improve with critical queue length `Q*`.
9. **Calibrate the L2-only queue estimator's `n` against Kalshi's ground-truth `queue_position_fp`.**

**Tier 3:** Moallemi–Yuan Volterra solve; VPIN in log-odds as a toxicity gate; Feil–Nendel PDE or Guéant's
tridiagonal ODE with `p` frozen over short re-solve intervals.

**Validation gates (pass/fail):**

| Gate | Criterion |
|---|---|
| **A** | Simulated adverse-fill rate lands in **66–89%**. Below that, the fill model is too generous. |
| **B** | Simulated fill rate matches live fill rate at small size within ~10%, at frozen `n`. |
| **C** | L2-estimated queue position tracks Kalshi's `queue_position_fp` (report R² and bias). |
| **D** | Strategy profitable at the **trade-through** bound, not just the touch bound. |
| **E** | Markout curve positive at 1s **and** at settlement. |
| **F** | Order size ≤ ~0.5% of the market's expected lifetime volume. |

**Three things to expect to be wrong about:**
- Cancellation rate is **not** linear in queue size (increasing-concave, then flat).
- Duration between price moves has **infinite mean** in the balanced regime — never report an average
  time-to-fill.
- Pure order-book-driven simulators generate **~1/3** of realistic volatility, so a backtest will
  systematically understate adverse selection unless exogenous jumps are injected.

---


---

## 9. Execution tactics — page-verified mechanics

### 9.1 The maker/taker crossover: a price-dependent policy, not a constant

Kalshi's fee is `0.07·p(1−p)` but the tick is a flat 1 cent. Setting the taker fee equal to a half-tick:

```
0.07·p(1−p) = 0.005   ⟹   p² − p + 0.0714 = 0   ⟹   p = 0.0774  or  p = 0.9226
```

> **For 7.74% < p < 92.26%, the taker fee exceeds the entire half-spread of a 1-tick market.
> Outside that band, the tick dominates the fee.**

| p | taker fee | half-tick | taker ÷ half-tick |
|---:|---:|---:|---:|
| 0.02 | 0.137c | 0.50c | **0.27** |
| 0.05 | 0.333c | 0.50c | 0.67 |
| **0.077** | **0.500c** | 0.50c | **1.00** |
| 0.20 | 1.120c | 0.50c | 2.24 |
| **0.50** | **1.750c** | 0.50c | **3.50** |
| **0.923** | **0.500c** | 0.50c | **1.00** |
| 0.98 | 0.137c | 0.50c | 0.27 |

Break-even at p = 0.50 in a 1-tick market (bid 49 / ask 50, fair 49.5):

```
cost of crossing = half-spread + taker fee = 0.50c + 1.75c = 2.25c
cost of posting  = -half-spread + maker fee = -0.50c + 0.44c = -0.06c   (a net CREDIT)
edge required to prefer crossing = 2.31c/contract = 4.6% of price
```

**The maker/taker policy must be a function of price level.** Deep OTM contracts (p < 8% or p > 92%) invert
the economics — there the flat tick dominates and the fee is nearly free.

Note this is a *different* question from the fee-death-zone rule (`fee/price > 0.04`). That one asks whether
a trade is worth doing at all in edge terms; this one asks whether to cross or post. Both hold at once: at
p=0.05 the fee is 6.65% of stake (bad for edge) yet only 0.33c against a 0.50c half-tick (cheap to cross).

The maker/taker neutrality result of Angel-Harris-Spatt and Colliard-Foucault does **not** rescue you: a
1-cent tick on a $1 notional is a 1% relative tick, far too coarse for the quoted spread to absorb the fee.

### 9.2 The critical-fill-probability rule

With signal edge `A`, fair `p`, ask `a`, bid `b`, fees `f_t`/`f_m`, expected fill time `E[t]`, alpha time
constant `τ_α`:

```
CROSS EV = A − (a−p) − f_t
POST  EV = α · [ A·e^(−E[t]/τ_α) + (p−b) − f_m − AS ]
α*       = [A − (a−p) − f_t] / [A·e^(−E[t]/τ_α) + (p−b) − f_m − AS]
```

Cross iff estimated `α < α*`. **If `α* ≤ 0`, crossing is EV-negative at any fill probability — post
unconditionally.** At fair 49.5c with bid 49 / ask 50, crossing is EV-negative until edge exceeds ~2.25c,
and even at 5c of edge you should still post unless estimated fill probability is below ~0.70.

### 9.3 Budish–Cramton–Shim: a calibrated spread floor, and a depth taper

Equilibrium spread (their eq. 6.3):

```
λ_invest · (s*/2)  =  λ_jump · Pr(J > s*/2) · E[J − s*/2 | J > s*/2]
```

Read it as: **the spread is the level at which revenue from investors equals rents lost to snipers.**
Estimate `λ_jump`, `Pr(J > s/2)`, `E[J − s/2 | J > s/2]` from your own tape and solve for a **floor**.

Two results that should reshape intuition:

1. **`s*` does not depend on `N`.** Entry does not compete the spread down; speed investment only
   redistributes who wins the race.
2. **Depth (eq. 6.4): the sniping cost is identical at every level** (snipers take all available size at a
   stale price) while the revenue term **shrinks** with depth. **Therefore your size ladder should thin out
   fast beyond the touch — because of pick-off risk, not inventory risk.** This is a different rationale
   than the usual one and produces a steeper taper.

Empirics: ES–SPY arbitrage median duration fell **97 ms (2005) → 7 ms (2011)** while per-unit profitability
stayed **constant** at ~0.08 index points. Arbitrage frequency is explained almost entirely by "distance
travelled" — realized volatility. **So make your spread scale with short-horizon realized volatility; that
falls out of the model rather than being a heuristic.**

> At 50–500 ms you are, definitionally, always the sniped party and never the sniper. You cannot win the
> race; do not enter it. **On a firm CLOB you have no last look — your quotes are options you have written
> for free**, and that option's value to the taker rises with volatility and with your latency.

### 9.4 Alpha decay — corrected

For an execution schedule spread over `[0,T]`, the fraction *captured* is not `2^(−T/h)` but:

```
C(T/τ_α) = (τ_α/T)·(1 − e^(−T/τ_α))
```

| T/τ_α | T ÷ half-life | alpha captured | lost |
|---:|---:|---:|---:|
| 0.10 | 0.14 | 95.2% | 4.8% |
| **0.20** | **0.29** | **90.6%** | **9.4%** |
| 0.50 | 0.72 | 78.7% | 21.3% |
| **1.00** | **1.44** | **63.2%** | **36.8%** |
| 3.00 | 4.33 | 31.7% | 68.3% |

**To keep decay under 10%, `T ≤ 0.2·τ_α ≈ 0.29 × half-life`. Executing over one full decay constant throws
away 37%.**

Optimal horizon with Almgren-2005 temporary impact:
`T* = [2·τ_α·β·η·σ·(X/V)^β / A₀]^(1/(1+β))`. **When `X/V` is large relative to a fast-decaying signal there
is no interior optimum — the correct action is to shrink the order or skip the trade. Code that as an
explicit gate**, not a schedule that silently loses money.

Gârleanu & Pedersen (2013): "**aim in front of the target**, trade partially toward the current aim" —
predictors with slower mean reversion get more weight in the aim portfolio.

### 9.5 Almgren–Chriss, verified (with an erratum)

```
E(x) = ½γX² + ε·Σ|n_k| + (η̃/τ)·Σ n_k²        η̃ = η − ½γτ
x_j  = X · sinh(κ(T−t_j)) / sinh(κT)          κ ≈ √(λσ²/η̃)
half-life θ = 1/κ                              independent of the imposed horizon T
```

`½γX²` and `ε|X|` are **schedule-independent** — only the `η̃` term is controllable. With alpha, the
trajectory is the zero-drift sinh solution **plus a constant correction `x̄ = α/(2λσ²)`, independent of X**:
alpha changes your terminal target, not your decay rate. Rather than guessing risk aversion, pick the
half-life you want and invert: `λ = η/(σ²θ²)`.

> **Erratum, independently verified:** the working paper's body text on p.26 gives `x̄ ≈ 1,100 shares`.
> With the paper's own Table 1 parameters the correct value is **11,080 shares**, matching Table 1's
> "11,000". **The body text is off by 10× — calibrate off Table 1.**

Calibrated impact (Almgren, Thum, Hauptmann & Li 2005, 29,509 Citigroup orders):

```
J = I/2 + sgn(X)·η·σ·(X/(V·T))^(3/5)        γ = 0.314 ± 0.041     η = 0.142 ± 0.0062
```

**They reject β = 1/2 at 95% confidence** — the exponent is 3/5, not the folklore square root. R² < 1%: the
model explains the mean, never a single order. For prediction markets, drop the liquidity factor and refit
`η` on your own fills; 0.142 is a US large-cap number and will not transfer. Published δ range across
studies is [0.4, 0.7].

### 9.6 Adverse-selection magnitudes — the number that should worry you

Moallemi–Yuan Table 3 (model vs backtest, 30 days, **in ticks**):

| Symbol | Order value (back of queue) | Fill prob | **Adverse selection** | Touch value (front) |
|---|---:|---:|---:|---:|
| BAC | 0.14 | 0.62 | **0.57** | 0.36 |
| CSCO | 0.08 | 0.63 | **0.68** | 0.24 |
| INTC | 0.11 | 0.64 | **0.63** | 0.28 |
| PBR | **−0.03** | 0.57 | **0.85** | 0.03 |
| EFA | 0.03 | 0.57 | **0.74** | 0.06 |

**Adverse selection runs 0.57–0.85 ticks against a 1-tick spread. For PBR, back-of-queue passive liquidity
provision has negative expected value.** Fill probabilities cluster at 0.57–0.65.

Mechanism: back-of-queue orders fill only against *large* trades, and large trades are disproportionately
informed. **Front-of-queue acts as a filter on the counterparty population.** For a retail quoter at
50–500 ms who will systematically lose the queue race, **your passive fills are systematically the toxic
subset. Budget `AS` at the high end of any range you estimate.**

### 9.7 Amend vs cancel-replace — the concrete priority rules

**Any modification loses time priority except a reduction in quantity** (verified against Cboe EDGX, Nasdaq
Options, and CME rules).

1. **Never amend to increase size.** Leave the original resting and submit a **second, separate order** for
   the increment — you keep priority on the first tranche.
2. **Amend down freely** — the one free operation.
3. **Reprice only outside a hysteresis band** sized to the queue value forfeited. Reprice iff the
   improvement in `α(δ − AS)` exceeds the queue value destroyed; given queue value up to ~0.2 ticks against
   a 1-tick spread, a reprice is expensive.
4. **On a partial fill the residual keeps its queue position.** Do not cancel and re-post the residual to
   tidy up — that is a pure priority donation.
5. Prefer atomic amend-in-place over cancel-then-new: the latter has a window where you are off the book
   *and* a window where both sides can be live (the self-cross problem).
6. **Instrument `amend_count / fill_count`.** If it rises, your fair-value model is noisier than your
   spread — widen rather than chase.

### 9.8 Self-trade prevention — mode choice and the regulatory posture

| Mode | Mechanics |
|---|---|
| **Cancel Newest** | Cancel the incoming taker remainder; resting order survives |
| Cancel Oldest | Cancel the resting maker order; incoming continues |
| Cancel Both | Cancel both remainders |
| Decrement & Cancel | Cancel the smaller, decrement the larger |

**Precedence: the taker's STP instruction governs, overriding the resting order's setting.** Matching is on
account *or* shared trade-group ID — relevant if you run multiple API keys or sub-accounts.

**Pick Cancel Newest for a two-sided quoter** — your resting liquidity survives and only the erroneous new
order dies. **Cancel Oldest is dangerous:** a repricing burst can strip you off the book entirely.

Prevent self-crosses in your own logic first (maintain an internal book; never submit a bid ≥ your live
ask) and treat exchange STP as the backstop, **with alerting on every trigger**. A rising prevented-match
rate means your two sides are converging and your quoting logic has a bug.

**Regulatory:** CEA §4c(a) prohibits wash/fictitious sales — the CFTC hook, directly applicable to Kalshi as
a designated contract market. FINRA 5210 and Notice 14-28 draw the line at **intent**: unintentional
self-trades from one firm with no change in beneficial ownership are generally bona fide, but you must have
procedures to prevent *patterns* of them. **Turning STP on, logging every trigger, and being able to show
the two sides are one strategy is the defensible posture. Turning STP off to get more fills is the thing
not to do.**

### 9.9 Iceberg detection — retail-implementable, and it corrects your fill model

Needs only the public trade tape plus L2 snapshots. Maintain per-price-level state; **when
`traded_volume_at_level > displayed_volume_before_trade`, the excess is hidden size.** Build a per-market
empirical distribution of `hidden/displayed`. For size prediction use a **Kaplan–Meier survival estimator** —
correct because orders cancelled after partial execution make total peak size right-censored.

Prevalence (Frey & Sandås, Xetra): **9.3% of submitted and 15.9% of executed shares** involve icebergs;
icebergs are **12–20× larger** than ordinary limit orders; the larger the executed fraction, the *smaller*
the price impact (liquidity-motivated, not informed).

> **Consequence for §3: hidden liquidity ahead of you means your fill probability `α` is systematically
> OVER-estimated from displayed depth.** Since `V = α(δ − AS)`, that inflates your estimate of passive order
> value. **Calibrate `α` from realized fills, never from displayed queue depth.**

### 9.10 One more inventory result

Bayraktar & Ludkovski, liquidation with controlled intensity, fluid-limit closed form:

```
s*(x,T) = (λ/(a·r))^(1/a) · x^(−1/a) · (1 − e^(−r·a·T))^(1/a)
```

**Note `s* ∝ x^(−1/a)`: the more inventory left, the tighter you must quote.** This is the opposite of the
naive instinct to widen when long.

### 9.11 Revised build priorities

Amendments to the Tier list in §7:

- **Add to Tier 1:** the price-dependent maker/taker gate (9.1). Pure arithmetic, changes behavior across
  the whole book, cheapest item here.
- **Add to Tier 1:** amend-down-only / never-amend-up, plus a reprice hysteresis band (9.7). Also arithmetic.
- **Add to Tier 2:** the BCS spread floor calibrated on your own jump statistics, plus the depth taper (9.3).
- **Add to Tier 2:** live `AS` measurement as an EWMA of the signed mid-move 30s after each fill. Simpler
  than VPIN, already in the units your P&L cares about, and Moallemi–Yuan gives a calibration target.
- **Add to Tier 2:** iceberg logging via `traded > displayed` (9.9), used to deflate fill-probability
  estimates.
- **Move down:** anything depending on winning a queue-position race or on latency arbitrage. At 50–500 ms
  you are the prey in both.

## 10. Open verification items

- **Kalshi's maker-fee constant remains unverified against the official PDF** (HTTP 429 across three
  independent research passes). The taker formula `roundup(0.07·C·P·(1−P))` is confirmed in a peer-reviewed
  source. See `06-kalshi-structure.md` §4 for the per-series `fee_type` / `fee_multiplier` data read
  directly from the API, which supersedes secondary sources.
- **Nobody has published spread and depth dynamics in the *seconds* around a Kalshi CPI or FOMC print.**
  The qualitative claim is asserted but unquantified. **Your own recorder would produce a novel result
  here** — a cheap, publishable by-product of Gate 1.
