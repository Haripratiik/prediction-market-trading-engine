# Statistical Methods for Binary / Prediction-Market Trading

Implementation reference. `m` = market price of a YES contract, `q` = your model's probability,
`p` = true probability, `y ∈ {0,1}` = settlement, `f` = fraction of bankroll. Claims marked **[verified]**
were confirmed by simulation.

---

## 0. Three identities that unify the whole problem

Define the KL divergence of your belief from the market price:

```
KL(q ‖ m) = q·log(q/m) + (1−q)·log((1−q)/(1−m))
```

**(A) It is your Kelly growth rate.** Allocating optimally against a complete market yields exactly
`KL(q‖m)`. **[verified exact to machine precision]**

**(B) It is your log-score edge over the market.** If `q` is true,
`E[logscore(m) − logscore(q)] = KL(q‖m)`. **[verified]**

**(C) It is the growth rate of the e-process that proves you have edge.** The Kelly betting martingale
`K_t = Π(1 + λ_i(y_i − m_i))` satisfies `log K_t / t → KL(q‖m)`. **[verified]**

> **Your growth rate, your forecasting skill, and your statistical evidence are the same number.**

**Immediate consequences:**

```
N ≈ log(1/α) / KL(q‖m)        settled markets before your edge is provable at level α
g* ≈ SR²/2                     Kelly growth in the small-edge limit  [verified]
```

| your q vs market m = 0.50 | markets needed at α = 0.05 |
|---|---:|
| 0.52 | **3,744** |
| 0.55 | 598 |
| 0.60 | 149 |

---

## 1. Calibration and evaluation

### 1.1 The sample-size law that matters most

Let `Δ_i = q_i − m_i` and suppose you are calibrated. Then the per-market Brier difference has
`E[d_i] = −Δ_i²` and `sd(d_i) ≤ |Δ_i|`, so the t-statistic for "I beat the market" is **at least `δ√N`**
where `δ² = mean(Δ_i²)`:

```
N ≥ 4 / δ²        settled markets to reach t = 2
```

**[verified: simulated t = 24.34 vs the bound 22.27 at N = 200k, δ = 0.05]**

| typical disagreement δ | markets needed for t = 2 |
|---:|---:|
| 2 points | **10,000** |
| 3 points | 4,444 |
| 5 points | **1,600** |
| 10 points | **400** |

The bound sharpens to `t ≈ 1.67·δ√N` near prices of 0.1/0.9, **you need fewer markets when trading
longshots.** The log-score version: `E[d̄] = −mean KL(q_i‖m_i)`, so **your log-score skill is literally your
Kelly growth rate**.

### 1.2 ECE is broken: do not report it

**[verified] A perfectly calibrated forecaster shows large nonzero ECE.** Forecasts ~Beta(2,2), true ECE = 0:

| n settled markets | ECE (10 uniform bins) | ECE (10 quantile bins) |
|---:|---:|---:|
| 100 | **0.1027** | 0.1107 |
| 500 | **0.0468** | 0.0495 |
| 2,000 | 0.0243 | 0.0252 |
| 10,000 | 0.0104 | 0.0109 |

Bias decays like `√(K/n)`. **"My ECE is 0.05 over 500 markets" is reporting pure noise.** Never compare ECE
across different `n` or bin counts.

### 1.3 Two bin-free calibration tests to run instead

**Spiegelhalter's Z**: no binning, no tuning:

```
Z = Σ (y_i − q_i)(1 − 2q_i) / sqrt( Σ (1 − 2q_i)² q_i(1 − q_i) )   ~ N(0,1) under calibration
```

**[verified]** correctly sized (0.051–0.052 at nominal 0.05); power against a calibration-slope-0.8
miscalibration: n=200 → 0.24, n=500 → 0.48, n=1,000 → 0.77, n=5,000 → 1.00.

**Cox / logistic recalibration:**

```
logit(P(y=1)) = a + b·logit(q)
b < 1 : overconfident      b > 1 : underconfident  ← the documented prediction-market pattern
```

**[verified] SE of the calibration slope:** n=200 → 0.183; n=500 → 0.113; n=1,000 → 0.080; n=5,000 → 0.035.
So at n = 200 you can detect a slope of 1.83 but not 1.15.

**Avoid Hosmer–Lemeshow** (bin-dependent, poor power). Use **CORP** reliability diagrams (PAV/isotonic-based,
bin-free, non-negative decomposition by construction) rather than the classical Murphy REL/RES/UNC
decomposition, whose plug-in estimator is biased upward in both terms.

**Python:** `calzone` implements Spiegelhalter Z, Cox slope/intercept, ECE/MCE/HL/ICI with bootstrap CIs.
CORP has no first-class Python package, implement with `sklearn.isotonic.IsotonicRegression` in ~20 lines.

### 1.4 Recalibrating your model: and when not to

**[verified] head-to-head, out-of-sample Brier**, true miscalibration logit-linear (Platt correct):

| calibration set n | raw | **Platt** | Isotonic | oracle |
|---:|---:|---:|---:|---:|
| 100 | 0.21899 | 0.22148 | 0.22926 | 0.21708 |
| 500 | 0.21921 | **0.21800** | 0.22051 | 0.21727 |
| 5,000 | 0.21868 | **0.21690** | 0.21743 | 0.21682 |

**Rules:**
1. **Below ~250 settled markets, do not recalibrate at all**: both methods made things *worse* than raw at
   n = 100.
2. Platt wins when the distortion is a logit-scale slope/shift (the common case, and the documented
   prediction-market pattern), and its two parameters are directly interpretable as §1.3's `a` and `b`.
3. Isotonic only pays above ~1,000 samples **and** when the distortion is genuinely non-monotone in logit.
4. Always fit the calibrator on data the model never saw (`CalibratedClassifierCV(..., cv=5)`).

---

## 2. Sequential inference: monitoring a live strategy honestly

### 2.1 The peeking penalty is worse than you think

**[verified] realized type-I error at nominal 5%, by number of pre-planned looks:**

| looks | α_actual | inflation |
|---|---:|---:|
| 1 | 0.050 | 1.0× |
| 5 | 0.142 | 2.8× |
| 50 | 0.322 | 6.4× |

**And under *continuous* monitoring it does not converge, it grows with the horizon.**
**[verified: 40,000 reps, Bernoulli(0.5), two-sided nominal 5%]**

| observations monitored | P(ever reject) |
|---:|---:|
| 10 | 0.1583 |
| 100 | 0.3630 |
| 1,000 | 0.5250 |
| 10,000 | 0.6472 |
| **100,000** | **0.7389** |

By the law of the iterated logarithm, `limsup |Z_n|/√(2 log log n) = 1`, so **any fixed threshold is crossed
with probability 1**, the table above keeps climbing toward 1.0, it does not plateau. If you re-evaluate
"does this have edge?" after every fill, there is no horizon at which your 5% test is a 5% test.

### 2.2 The tool to use: a beta-binomial e-process

**Ville's inequality:** for a nonnegative supermartingale with `M₀ = 1`, `P(∃t : M_t ≥ 1/α) ≤ α`.
**"Reject when the e-process ever exceeds 1/α" is an anytime-valid level-α test, optional stopping and
optional continuation are free.**

For Bernoulli outcomes there is a closed form (the growth-rate-optimal e-value):

```python
from scipy.special import betaln
# H0: p <= p0.  Mix over the unknown alternative with a Beta(a,b) prior.
log_e = betaln(a+S, b+t-S) - betaln(a,b) - S*np.log(p0) - (t-S)*np.log1p(-p0)
```

**[verified] `E[E_t] ≤ 1` at every horizon; P(sup ≥ 20) = 0.0408 vs the 0.05 target** (the Kelly martingale
version is exact at 0.0508; the naive repeated z-test is 0.68).

**The SPRT is the Kelly-optimal bet against a point alternative**: `K_t` equals the SPRT likelihood ratio
exactly **[verified to 7.45e−15]**, and its growth rate is `KL(p₁‖p₀)`, closing the loop with §0.

**Price of anytime validity:** ~1.5–2× the fixed-n confidence width, or **~1.8× the observations to first
detection** (per-bet SR 0.10: median anytime stop 686 vs fixed-n 384). **[verified]** That is the honest cost
of being allowed to look whenever you like, and it is worth paying, because the alternative is a 68% false
positive rate.

**Confidence sequence half-widths [verified], Bernoulli(0.5):**

| t | Hedged-betting | **Beta-binomial** | EB-stitched | fixed-n |
|---:|---:|---:|---:|---:|
| 100 | **0.1460** | 0.1550 | 0.3787 | 0.0980 |
| 1,000 | **0.0525** | 0.0555 | 0.0833 | 0.0310 |
| 20,000 | 0.0155 | **0.0138** | 0.0158 | 0.0069 |

**For binary outcomes use the beta-binomial mixture**: closed form, no tuning, tightest at large `t`.

### 2.3 Combining evidence across strategies: the e-value superpower

```
Independent e-values:   E = Π_k E_k
ARBITRARY dependence:   E = (1/K) Σ_k E_k        ← no correction needed
```

**The average being valid under arbitrary dependence has no p-value analogue.** This is the single most
practically important reason to use e-values in trading, where strategies share market exposure in ways you
cannot model.

**e-BH for FDR across K strategies:** sort `e_[1] ≥ … ≥ e_[K]`, take `k* = max{k : k·e_[k]/K ≥ 1/α}`, reject
the `k*` largest. **Controls FDR under arbitrary dependence with no correction**, unlike BH on p-values,
which needs PRDS. The motivating example in the source paper is literally K traders with
`H_k` = "trader k is not skillful."

**Python:** `confseq` (Howard & Ramdas reference implementation), `beta_binomial_mixture_bound`,
`betting_cs`, `betting_ci`. Note: PyPI `sequential` is drug-safety surveillance, not this; there is no
package named `ville`.

---

## 3. Correlation between binary outcomes

### 3.1 Phi systematically understates dependence: and it is the default everywhere

```
φ = (p₁₁ − p_X p_Y) / sqrt(p_X q_X p_Y q_Y)
φ_max = min{ sqrt(p_X q_Y/(q_X p_Y)), sqrt(p_Y q_X/(q_Y p_X)) }      (Prentice bound)
```

`|φ| = 1` is attainable **only if `p_X = p_Y`**. **[verified] attenuation:**

| latent ρ | φ (p=.5) | φ (p=.2) | φ (p=.05) |
|---:|---:|---:|---:|
| 0.30 | 0.194 | 0.163 | 0.098 |
| 0.50 | 0.333 | 0.295 | 0.204 |
| 0.90 | 0.713 | 0.687 | 0.618 |

> **[verified] The case that should alarm you:** at `p_X = 0.05, p_Y = 0.6` with genuine latent `ρ = 0.70`,
> `φ = 0.1818` while `φ_max = 0.1873`. **Phi is at 97% of its structural ceiling yet reads as "basically
> independent."** Any risk model built on the phi matrix of markets with heterogeneous prices
> **systematically understates concentration risk.**

**Use tetrachoric correlation instead** (latent bivariate normal). For a 2×2 table Olsson's estimator is the
full MLE. Solve with `brentq`; compute `Φ₂` with a 64-node Gauss–Legendre 1-D integral (**~16 µs, 60× faster
and more accurate than `scipy.stats.multivariate_normal.cdf`** **[verified]**).

**The textbook SE formula is wrong**: off by ~4.6× **[verified]**. The correct delta-method SE is in the
source notes. **The number to remember: with 100 settled events, a pairwise tetrachoric has SE ≈ 0.14.**
You cannot distinguish ρ = 0.3 from ρ = 0.5 pairwise, which is why you must shrink and cluster rather than
use raw pairwise estimates. Handle zero cells with a Haldane +0.5 correction, otherwise the MLE collapses
to ±1 **[verified: table (20,0,5,25) gives +0.9990 uncorrected]**.

### 3.2 The negative-correlation ceiling kills longshot hedging

**[verified]** For Bernoulli marginals the Gaussian-copula reachable range essentially coincides with the
Prentice band, and the negative side is brutal: **two markets each at `p = 0.02` cannot have binary
correlation below −0.0204.** You cannot build a hedged pair of longshots in *any* distribution.

### 3.3 Small-n correlation matrices

**Marchenko–Pastur:** with `q = p/n`, noise eigenvalues fill `[(1−√q)², (1+√q)²]`.

**[verified] 60 markets, 6 true themes:**

| n events | q | cond(S) | # eigenvalues > λ₊ |
|---:|---:|---:|---:|
| 40 | 1.50 | **1.4e+18** | 3 |
| 60 | 1.00 | **6.9e+16** | 4 |
| 300 | 0.20 | 2.42e+01 | **6** |

**At `n ≤ p` the matrix is numerically singular, any inverse is pure noise amplification. You need roughly
`n ≥ 4p` before the raw sample correlation matrix is usable, and you will never have that from settled
prediction-market events.**

**[verified] shrinkage head-to-head**, n=80, p=60:

| estimator | ‖C − R_true‖_F | median cond(C) |
|---|---:|---:|
| sample phi | 6.6334 | 4.25e+02 |
| **Ledoit–Wolf** | **5.0862** | 1.09e+01 |
| **OAS** | **5.0835** | 9.99e+00 |
| RMT clip | 5.8545 | 1.13e+01 |

Shrinkage cuts error 23% and the condition number ~40×. Use `sklearn.covariance.{LedoitWolf, OAS}`.
Assemble pairwise, then project to the nearest correlation matrix with **Higham's alternating projections
with Dykstra's correction** (`statsmodels.stats.correlation_tools.corr_nearest`), **[verified] 8.9% closer
in Frobenius norm than naive clip-and-rescale.**

### 3.4 Clustering is robust even when inversion is not

**[verified] Adjusted Rand index vs true themes at n=80, p=60, k=6: sample phi 1.000, Ledoit–Wolf 1.000,
RMT clip 1.000.** Silhouette on the Mantegna distance `d = √(2(1−ρ))` recovered the true theme count exactly.

> **Use clustering for structure discovery; use shrinkage for anything requiring the inverse.**

Cross-check the outcome-based partition against a text-embedding partition of market titles (ARI). Agreement
is evidence the theme is real; disagreement flags markets whose text similarity does not translate into
outcome dependence, exactly where to distrust your own thematic intuitions.

---

## 4. Kelly for multiple and correlated bets

### 4.1 The exact fractional-Kelly curve

```
g(c·f*) / g*  =  2c − c²
```

**[verified, exact vs approximation]:**

| c | 0.25 | 0.50 | 0.75 | 1.00 | 1.50 | 2.00 |
|---|---:|---:|---:|---:|---:|---:|
| `2c − c²` | 0.4375 | **0.7500** | 0.9375 | 1.0000 | 0.7500 | **0.0000** |
| exact (q=.55/m=.50) | 0.4369 | **0.7493** | 0.9372 | 1.0000 | 0.7459 | **−0.0275** |

- **Half Kelly gives 75% of growth at half the log-wealth volatility.**
- **Double Kelly gives exactly zero growth.** Beyond that, negative, ruin.
- The curve is a downward parabola: **underbetting is second-order cheap, overbetting is catastrophic.**
- **This is the precise formalization of "assume your edge is half your estimate":** if true edge is half your
  estimate, full-Kelly-on-estimate = double-Kelly-on-truth = **zero growth**.

### 4.2 Multi-market Kelly is not the sum of individual Kellys

```
maximize   E[ log(1 + Σ_j f_j r_j) ]     s.t.  Σ_j f_j ≤ 1,  f ≥ 0
```

Concave, so solve by sample-average approximation over scenarios drawn from your joint model (§3), in cvxpy
with an exponential-cone solver (CLARABEL). Use common random numbers across re-solves.

**[verified] The budget constraint binds hard.** 10 independent markets at price 0.50, true p = 0.60:
individual `f* = 0.20` each, naive sum = **200% of bankroll**. The joint optimizer allocates **0.0999 each**.

**[verified] And correlation bites much harder than it looks:**

| latent ρ | observed binary φ | per-market f | growth ÷ independent |
|---:|---:|---:|---:|
| 0.00 | −0.002 | 0.0999 | 1.000 |
| 0.20 | 0.123 | 0.0805 | **0.604** |
| 0.40 | **0.258** | 0.0562 | **0.407** |
| 0.60 | 0.407 | 0.0408 | 0.285 |

> **A latent correlation of 0.4, observed φ of only 0.26, cuts per-market size by 44% and growth to 41%
> of the independent case.** Combined with §3.1 (a φ of 0.26 can read as "basically independent"), this is
> **the single largest sizing error available to a prediction-market trader.**

### 4.3 Mutually exclusive outcomes: the closed form, and two surprises

Directly relevant to any multi-candidate market. **Do not apply the binary formula independently.**

```
max_x  Σ_i π_i · log( w + x_i − Σ_k p_k x_k ),   x_i ≥ 0
```

**Smoczynski–Tomkins closed form.** Given the optimal bet set `S`:

```
x_i = w · [ π_i/p_i − ( Σ_{k∉S} π_k ) / ( 1 − Σ_{k∈S} p_k ) ]      for i ∈ S
```

Selection: sort by descending `π_i/p_i`; add greedily while `π_i/p_i` exceeds the reservation rate
`(1 − Σ_{k∈S} π_k)/(1 − Σ_{k∈S} p_k)`; stop at the first failure. Requires `Σp_i > 1` for uniqueness, **the
vig is what pins down the solution.**

**[verified, closed form matches numerical optimization exactly]:**

```
5 outcomes, prices [0.30 0.26 0.21 0.13 0.14] (sum 1.040), my π [0.40 0.25 0.20 0.10 0.05]
edge ratios π/p:  [1.333  0.962  0.952  0.769  0.357]
optimal set S = {0,1,2,3};  x = [0.8333 0.4615 0.4524 0.2692 0]  total 0.5000, growth 0.034616
NAIVE per-outcome Kelly    x = [0.1429 0 0 0 0]                  total 0.1429, growth 0.022582
```

1. **Kelly buys three outcomes with negative expected value** (π/p = 0.962, 0.952, 0.769, all below 1).
   They are **hedges**: they raise wealth in states where the main bet loses, and log utility values that
   more than their EV cost. This is general, not an artifact.
2. **The naive single-outcome approach captures only 65.2% of the optimal growth rate**, staking 14% where
   the optimum stakes 50%.

### 4.4 Parameter uncertainty: the usual argument is wrong

**For log utility, `E[log W]` is linear in the outcome probability, so under a *correct posterior* the
optimal bet uses the posterior mean, no shrinkage.** Full Kelly on a proper posterior mean is optimal.

**The actual mechanism is induced correlation.** Uncertainty in a *shared* edge parameter makes outcomes
positively correlated in the posterior predictive distribution even when conditionally independent, and
positive correlation reduces optimal leverage (§4.2). **[verified]** with `β ~ N(1, 1)` shared across 10
markets, induced pairwise binary correlation is +0.0403 and growth falls from 0.14271 to 0.12292, with the
marginal probability held fixed at 0.60 throughout.

**So implement a hierarchical Monte Carlo scenario generator:** draw the edge parameter from its posterior,
then draw outcomes given it. The Kelly solution is automatically more conservative, **with no ad hoc haircut
anywhere.**

**The remaining, larger reasons to bet fractionally:**
1. **Your point estimate is not a posterior mean.** You select markets where your model disagrees *most*
   with the price, precisely where the model is most likely wrong. **This selection effect is real and is
   what §5 fixes.**
2. Model misspecification not captured by any posterior.
3. Drawdown aversion → use the risk-constrained formulation below.
4. **Chopra & Ziemba:** errors in means are ~11× more damaging than errors in variances. In prediction
   markets the "mean" *is* your probability estimate, so essentially all your risk is estimation risk in `q`.

### 4.5 Risk-constrained Kelly

Busseti, Ryu & Boyd: a **convex** constraint delivers a drawdown guarantee.

```
E[(rᵀb)^(−λ)] ≤ 1   ⟹   P(W_min < α) < β,     λ = log β / log α
```

**[verified]** the bound holds and is conservative, and RCK beats fractional Kelly at matched risk by ~5%
growth. **But the guarantee bounds `P(W ever falls below α × INITIAL wealth)`. Peak-to-trough drawdown of a
growing bankroll is a different event and is hit with probability 1 over a long enough horizon,
[verified] over 400 periods, full Kelly hits a 30% peak-to-trough drawdown on 100% of paths.** Never quote
RCK's β as a max-drawdown number.

### 4.6 Capital velocity

Prediction-market collateral is **fully locked until resolution**. Your growth rate per unit of *time* is
`KL(q‖m) / T_resolution`, not `KL(q‖m)`.

> **A 2% edge resolving in a week dominates a 6% edge resolving in a year.**

Rank opportunities by `KL/T`, and treat the budget constraint as binding across *overlapping resolution
windows*, not per market.

---

## 5. The hierarchical edge model: replacing the guessed shrinkage factor

### 5.1 The specification

Model your forecast's *incremental* information over the market:

```
logit( P(y_i = 1) ) = logit(m_i) + β_c · ( logit(q_i) − logit(m_i) ) + α_c
```

- `β_c = 0` → your disagreement with the market is pure noise. **No edge.**
- `β_c = 1` → your forecast is exactly right; the market is wrong.
- `β_c = 0.5` → literally "my edge is half what I think it is."
- `α_c` → systematic directional bias in category `c`.

This is a **forecast-encompassing regression** in logit space. **`β_c` is exactly the shrinkage factor the
folk rule gestures at, but estimated from data, per category, with uncertainty.**

**Hierarchical prior (partial pooling):**

```
β_c ~ Normal(μ_β, σ_β)     μ_β ~ Normal(0.3, 0.3)     σ_β ~ HalfNormal(0.3)
α_c ~ Normal(0, σ_α)       σ_α ~ HalfNormal(0.2)
```

**Empirical-Bayes closed form** (use before reaching for MCMC): fit `β̂_c`, `SE_c` per category by
1-parameter logistic regression with offset `logit(m)`, then James–Stein shrink:

```
μ₀  = Σ(β̂_c/SE_c²) / Σ(1/SE_c²)
τ²  = max( mean( (β̂_c − μ₀)² − SE_c² ), ε )
w_c = τ² / (τ² + SE_c²)
β_EB,c = w_c·β̂_c + (1 − w_c)·μ₀
```

### 5.2 It works, and it is worth real money

**[verified] 12 categories, unequal n (15–391), true `β_c` around 0.45.**

Estimation RMSE vs truth: no pooling **0.2891**, complete pooling 0.3176, **partial pooling 0.2176**.

**Mean log-growth per bet, Kelly-sized on each policy:**

| sizing policy | growth/bet | % of oracle |
|---|---:|---:|
| true β (oracle) | +0.01860 | 100% |
| **partial pooling (EB)** | **+0.01488** | **80%** |
| no pooling (raw per-category MLE) | +0.01375 | 74% |
| complete pooling | +0.01275 | 69% |
| half-Kelly heuristic on β=1 | +0.01350 | 73% |
| **naive β = 1 (trust your model)** | **+0.00096** | **5%** |

> **Trusting your model at face value destroys 95% of the achievable growth**, the `2c − c²` mechanism at
> `c ≈ 2.4`. The crude half-Kelly heuristic recovers most of it *because* the true `β̄ ≈ 0.42 ≈ 0.5`.
> **Estimating `β` hierarchically beats the heuristic by ~10%, and it tells you which categories to stop
> trading** (categories whose posterior `β_c` is indistinguishable from 0).

### 5.3 Implementation notes

Always use a **non-centered parameterization** for group effects, hierarchical models induce Neal's funnel
and centered parameterizations diverge:

```python
beta_raw = pm.Normal("beta_raw", 0, 1, dims="cat")
beta = pm.Deterministic("beta", mu_beta + sigma_beta * beta_raw)
```

Bound `β_c` to roughly `[0, 1.2]`, `β > 1` means the market is *anti*-informative, a strong claim.
Posterior predictive checks should compare **economically** meaningful statistics: realized Brier, realized
calibration slope, worst 5% drawdown, hit rate per price decile.

---

## 6. Backtest validity

### 6.1 Deflated Sharpe Ratio

Expected maximum of N iid `N(0,1)`: `E[max_N] ≈ (1−γ)Φ⁻¹(1 − 1/N) + γΦ⁻¹(1 − 1/(Ne))`, γ = 0.5772.
**[verified against 20,000-rep Monte Carlo, accurate to ~0.03.]**

**[verified] T=250, 3,000 reps, global null (no strategy has any skill):**

| N trials | **PSR(0) > .95 false-positive** | **DSR > .95 false-positive** |
|---:|---:|---:|
| 1 | 5.5% | 2.1% |
| 10 | **39.4%** | 0.0% |
| 50 | **92.0%** | 0.1% |
| 200 | **100.0%** | 0.1% |

> **After 50 backtested configurations, an uncorrected significance test on the best strategy is wrong 92%
> of the time.**

**[verified] The per-bet SR threshold you must beat under the null:**

| T settled bets | N=10 | N=100 | N=1000 |
|---:|---:|---:|---:|
| 250 | 0.100 | **0.160** | 0.206 |
| 1,000 | 0.050 | 0.080 | 0.103 |
| 5,000 | 0.022 | 0.036 | 0.046 |

**Honest caveat: DSR is markedly conservative** (0.1% actual vs 5% nominal) and will kill genuine edges.
**Treat it as a screening gate, not a p-value.**

Use **Probabilistic Sharpe Ratio** with the skew/kurtosis terms, not the normal approximation, prediction
market P&L is severely skewed (**[verified]** buying longshots at 0.10 hitting 12%: skew +2.34, kurtosis
6.47).

**Effective N when trials are correlated:** `N̂ = ρ̄ + (1 − ρ̄)·M`. With `M > T`, `ρ̄` is itself overfit,
cluster the trial return series (§3.4) and use the number of clusters. **Log every trial you ever run; DSR
is useless if you cannot count `M` honestly.**

### 6.2 Minimum backtest length

```
MinBTL < 2 ln N / E[max_N]²    years
```

**[verified] at target `E[max_N] = 1`:** N=7 → **1.92 years**; N=45 → **5.00 years**; N=100 → 6.40;
N=1,000 → 10.60.

> **With five years of data, no more than ~45 independent configurations should ever be tried. After seven
> configurations, the expected max in-sample Sharpe on a two-year backtest is already 1.0.**
>
> **Check this before you start searching, not after.**

### 6.3 PBO via CSCV

**[verified, two independent implementations agree]** T=1000, S=16:

| scenario | PBO |
|---|---:|
| all noise (global null) | **0.508** |
| weak edge (SR 0.05–0.08) | 0.377 |
| moderate (SR 0.10–0.15) | 0.090 |
| strong (SR 0.20–0.30) | **0.000** |

PBO ≈ 0.5 under the pure null exactly as designed. Use **S = 16** (12,870 combinations, sd ≈ 0.0044);
S = 4 is useless (sd 0.204). Vectorize with block sufficient statistics, looping 12,870 combinations is
too slow.

### 6.4 Purging and embargoing: the leakage is real

In prediction markets label overlap is **structural**: a market opened at `t` does not settle until `t + H`.

**[verified] Label horizon H = 25, a time-only predictor whose true OOS AUC is 0.500:**

| purge | embargo | CV AUC |
|---:|---:|---:|
| 0 | 0 | **0.573** (spurious signal) |
| H | 0 | 0.505 |
| H | **2H** | **0.500** ✓ |

Purge training observations whose label span overlaps any test span; embargo a further `h ≈ 0.01·T` after
the test set. **Prefer Combinatorial Purged CV over walk-forward**, it yields a *distribution* of backtest
outcomes rather than one high-variance path. Libraries: `timeseriescv`, `skfolio.model_selection`.
(`mlfinlab` is now closed-source; use `mlfinpy`.)

### 6.5 Benchmark comparison

Prefer **Hansen's SPA** over White's Reality Check, SPA studentizes and recenters poor models at their own
sample mean, so adding obviously-bad configurations no longer destroys power. `arch.bootstrap.SPA` (note:
`RealityCheck` is an *alias* for the SPA class; `StepM`'s property is `superior_models`, not
`better_models`). Report the lower/consistent/upper p-value triple, the bracket shows how sensitive your
conclusion is to the recentering choice.

**Harvey & Liu:** the multiple-testing haircut on Sharpe is **strongly nonlinear**, high Sharpes are lightly
penalized, marginal ones gutted. They explicitly call the "just halve it" rule a serious mistake.
Harvey, Liu & Zhu: a new factor needs **t > 3.0**, not 2.0.

---

## 7. Fill probability as a survival problem

### 7.1 Start with pooled logistic regression

Discrete-time survival = pooled logistic on (order, time-bucket) rows. Easiest to implement, handles
time-varying covariates natively, scales, and gives competing risks nearly free.

1. Bin time to match your decision frequency.
2. Expand each order into one row per bucket it survives into. An order filling in bucket 5 contributes
   `y = 0,0,0,0,1`; an order **cancelled** in bucket 3 contributes `y = 0,0,0` and then stops.
   **That is the entire censoring treatment.**
3. Attach covariates as of the start of each bucket.
4. Fit one pooled logistic regression.

```
logit(h_ij)         = α_j + βᵀx_ij      → discrete-time odds ratios, calibrated probabilities
cloglog(h_ij)       = α_j + βᵀx_ij      → EXACTLY proportional-hazards coefficients
P(fill within J)    = 1 − Π_{m≤J} (1 − ĥ_im)
```

**[verified] it recovers the truth**: 6,000 orders → 50,641 rows, 51% censored: intercept −2.082 (truth
−2.000), distance −1.463 (−1.500), log t +0.415 (+0.400).

**Cluster standard errors on `order_id`**: rows from one order are not independent:
`sm.Logit(y, X).fit(cov_type='cluster', cov_kwds={'groups': order_id})`.

**Competing risks for free:** replace binary `y` with a multinomial `{no event, fill, cancel}` per bucket.
**This is the cleanest competing-risks implementation available in Python** and sidesteps the fact that
Fine–Gray subdistribution regression exists in no maintained pure-Python package.

### 7.2 Censoring bias, and why it pushes you to cross the spread

**[verified] the bias from treating cancels as non-fills**, 6,000 orders, 51% censored:

| distance (ticks) | survival model P(fill by T=30) | naive binary classifier | understatement |
|---:|---:|---:|---:|
| 0.0 | 1.000 | 0.789 | **−0.211** |
| 1.0 | 0.914 | 0.486 | **−0.428** |
| 2.0 | 0.444 | 0.193 | −0.251 |

> **Censoring makes a naive classifier systematically pessimistic about fills, which biases a maker strategy
> toward crossing the spread too often.** On a 1–2¢ spread on a $1 contract that is 1–2% of notional given
> away per round trip.

**And the assumption that breaks:** censoring must be independent of execution conditional on covariates.
**Your cancellation policy is informative censoring**: you cancel precisely when prices move away. Fix by
putting the price path in as a **time-varying covariate** (which is why the counting-process/bucket format
matters), or by modeling competing risks properly.

### 7.3 The empirical result that should govern your quoting

Lo, MacKinlay & Zhang: median time-to-completion for one buy limit order, varying **only** the limit price:

| −2 ticks | −1 tick | at price | +1 tick | +2 ticks |
|---:|---:|---:|---:|---:|
| 100.557 min | 23.144 min | 0.128 min | 0.066 min | 0.013 min |

> **~800× change in median fill time across ±1 tick.** Order size barely matters, fill time is "very
> sensitive to the limit price, but not sensitive to limit size."

And their headline warning: *"Hypothetical limit-order executions, constructed either theoretically from
first-passage times or empirically from transactions data, are **very poor proxies for actual limit-order
executions**."* **A backtest that assumes "filled if the tape traded through my price" will materially
overstate your fill rate.**

**Covariates, ranked:** distance from mid (dominant), queue position, same-side depth, opposite depth,
spread, book imbalance, short-window realized volatility, **time to resolution** (first-order for prediction
markets, the hazard is strongly non-stationary near settlement), order size (weak), side (fit separately).

**Expect proportional hazards to fail** on order-book data, a far-from-touch order has near-zero hazard
early and large hazard once price approaches. Test with scaled Schoenfeld residuals; fix by stratifying or
making the covariate genuinely time-varying.

### 7.4 Library status (verified 2026-08-26)

| package | version | status |
|---|---|---|
| **lifelines** | 0.30.3 (2026-03) | active; only mainstream pure-Python time-varying Cox |
| **scikit-survival** | 0.28.0 (2026-07) | very active; **no time-varying covariate support** |
| statsmodels | 0.14.6 | active (`duration.hazard_regression.PHReg`) |
| xgbse | 0.3.3 | **repo ARCHIVED 2026-04** |
| pysurvival | 0.1.2 (**2019**) | **abandoned, do not use** |

Gotchas: `CoxPHFitter`'s `penalizer`/`l1_ratio` are **constructor-only**, not `fit()` args;
`check_assumptions` default `p_value_threshold` is **0.01, not 0.05**; `CoxTimeVaryingFitter` has **no**
`predict_survival_function`. scikit-survival 0.28.0 **removed** `criterion` from
`GradientBoostingSurvivalAnalysis` and renamed `event_times_` → `unique_times_`.

---


---

## 10. Python tooling: verified on THIS machine

Verified 2026-08-26 by direct install and execution on **Windows 11, Python 3.13.5, scipy 1.16.1,
NumPy 2.5.2** (R not installed). This matters because the sequential-inference ecosystem is in poor shape
and several obvious-looking packages do not work here.

### 10.1 The headline: write the sequential-inference code yourself

**`confseq` (the Howard–Ramdas reference implementation) cannot be pip-installed on this machine.**

- No Windows wheels at all, and none above cp310. Latest release 0.0.11 (2023-01-26); sdist fallback needs
  scikit-build + CMake + pybind11 + **Boost headers** + C++14.
- **NumPy-2 incompatible**: `np.float_` (removed in NumPy 2.0) appears in `src/confseq/misc.py` and
  `types.py` → `AttributeError` on import. You cannot pin around it, numpy <2 has no cp313 wheel.
- Repo is alive (last commit 2026-01-07) but **no release in 3.5 years** and no documentation site.

**Verified vendor workaround.** Only `boundaries` and `quantiles` are C++ extensions. Six files are pure
Python: `betting.py`, `predmix.py`, `betting_strategies.py`, `misc.py`, `types.py`, `other_bounded.py`.
Copy those plus an empty `__init__.py`, then `sed -i 's/np\.float_/np.float64/g' misc.py types.py`.

Confirmed working with **no compiler**, 500 iid Bernoulli(0.60):

```
predmix_empbern_cs   n=500 -> [0.5130, 0.6776]
betting_cs           n=500 -> [0.5250, 0.7050]
first n where betting_cs lower bound > 0.5: 337
```

(`confseq/__init__.py` is 0 bytes, import submodules, not the package. `betting.py` imports
`matplotlib.pyplot` at module top level.)

**The recommendation: implement §2 yourself.** Every method is 10–50 lines of numpy+scipy and all were
verified on this exact environment, the α-spending recursion (reproduces published Pocock/OBF constants
exactly), the SPRT (matches Wald's ASN), the beta-binomial e-value (`E[E] ≤ 1` verified; Ville hit rate
0.0408 vs 0.05 nominal), the hedged-capital betting CS, and the closed-form empirical-Bernstein CS. **The
beta-binomial e-value is four lines with `scipy.special.betaln`.**

### 10.2 Packages that do and do not work

| Package | Status on this machine |
|---|---|
| **`savvi` 0.3.1** | **Installs cleanly** (pure-Python wheel, py≥3.11). Undeclared dep: needs `matplotlib` installed alongside or `ModuleNotFoundError`. Good for cross-checking your own implementation. |
| **`spotify-confidence` 4.1.0** | Actively maintained; contains a **direct port of ldbounds' Lan–DeMets engine** (`sequential_bound_solver.bounds`, with incremental `ComputationState`, useful for live monitoring). Implements the Kim–DeMets power family, not OBF/Pocock verbatim. Internal module path, not public API. |
| **`lifelines` 0.30.3** | Installs cleanly with wheels. The only mainstream pure-Python time-varying Cox. |
| **`scikit-survival` 0.28.0** | Installs cleanly, Windows wheels cp311–cp314. No time-varying covariates. |
| `confseq` | **Cannot install**, see above. Vendor the pure-Python files. |
| `rpact`, `ldbounds` (Python), `ville`, `safestats` (Python), `pysprt`, `expdesign` | **404 on PyPI**, do not exist |
| `gsdesign` (PyPI) | Real, but only Jennison–Turnbull integration primitives (`gridpts`, `h1`, `hupdate`). **No spending functions.** |
| `sprt` (PyPI) | v0.0.1, **2017**, sole release. Write the ~15 lines yourself. |
| `sequential` (PyPI) | **Not** the drug-safety package, it is a 2014 function-ordering utility. The real one is R's `Sequential`. |
| `pysurvival` | Abandoned (one release, 2019). `xgbse` repo archived 2026-04. |

**Confirmed by execution: statsmodels and scipy have ZERO group-sequential, alpha-spending, always-valid, or
e-value functionality.** A regex scan of `dir(scipy.stats)` for `sprt|sequen|spend|martingale|evalue`
returned empty.

### 10.3 API corrections worth having

- `statsmodels.stats.multitest.multipletests` defaults to **`method='hs'` (Holm–Šidák), not Bonferroni.**
  Pass the method explicitly.
- `savvi` has **no** `.summary()` and **no** `.plot()`, use `savvi.utils.plot(...)`. Its `Multinomial` is
  **two-sided on the whole θ vector**, not a directional edge test; and `k` (Dirichlet concentration)
  materially changes power (at n=800 the e-value ran 0.046 → 0.51 as k went 1 → 100). For one-sided
  "do I have edge?", use the betting e-process.
- `confseq.betting.diagnostics` does not exist. Several `confseq` signatures have `v_opt` **before** `c`,
  and `bernoulli_confidence_interval` requires `t_opt`.
- R note: `ldbounds::bounds()` is **defunct**, use `ldBounds`.

**Coverage limit, stated honestly:** the "does not exist" claims are per-name lookups against the PyPI JSON
API, authoritative for those names, but not an exhaustive keyword sweep. A differently-named package cannot
be ruled out.

## 8. The five numbers to remember

1. **`N ≥ 4/δ²`**: settled markets to prove you beat the market at t = 2, where `δ` is your typical
   disagreement. A 5-point disagreement needs **1,600 markets**.
2. **`g(c·f*)/g* = 2c − c²`**, half Kelly gives 75% of growth at half the volatility; **double Kelly gives
   zero**. This is why you shrink.
3. **A latent correlation of 0.4, observed φ of only 0.26, cuts per-market Kelly by 44% and growth to
   41%.** And φ can sit at 97% of its structural ceiling while reading as 0.18.
4. **After 50 backtested configurations, an uncorrected test on the winner is wrong 92% of the time.** Five
   years of data buys ~45 independent trials, total.
5. **±1 tick moved median fill time by ~800×.** Never assume a fill because the tape traded through your
   price.

---

## 9. Build order

| Phase | Work |
|---|---|
| **1 Measurement** | Log `(q, m, y, category, horizon, timestamp)` for every settled market. Brier + log score vs market, Spiegelhalter Z, Cox slope/intercept per category, CORP reliability diagram. **No recalibration below ~250 markets; never report ECE.** |
| **2 Is the edge real** | Compute `δ²` and check `N ≥ 4/δ²`. Run the beta-binomial e-process continuously; report the confidence sequence as the live edge estimate. e-BH across strategies. |
| **3 Edge estimation** | Fit the hierarchical `β_c` model. Trade only categories whose posterior `β_c` is credibly above 0. **`β_c` replaces the guessed shrinkage factor.** |
| **4 Dependence** | Tetrachoric (fast `Φ₂`, Haldane correction) → Higham projection → Ledoit–Wolf/OAS → clustering on `√(2(1−ρ))`, cross-checked against text embeddings. |
| **5 Sizing** | Gaussian-copula scenarios with the edge parameter drawn from its posterior → SAA Kelly in cvxpy with a drawdown constraint. Smoczynski–Tomkins closed form for mutually-exclusive markets. **Rank by `KL/T`.** |
| **6 Validity** | Log every trial; estimate effective N by clustering trial returns; gate on DSR > 0.95 and PBO < 0.2 (CSCV, S=16); CombinatorialPurgedCV with purge + embargo; final comparison via `arch.bootstrap.SPA`. |
| **7 Execution** | Pooled logistic on (order, bucket) rows, cluster-robust SEs, multinomial for competing risks; validate against `CoxTimeVaryingFitter` on a subsample. |
