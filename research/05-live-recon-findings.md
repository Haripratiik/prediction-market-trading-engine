# Live Kalshi Universe Reconnaissance: Measured Findings

**Harvested 2026-08-26 04:33 UTC** from the public (unauthenticated) Kalshi API,
`https://api.elections.kalshi.com/trade-api/v2`. Complete open-event universe, not a sample.

Scripts: [`harvest_kalshi.py`](recon/harvest_kalshi.py) · [`analyze_kalshi.py`](recon/analyze_kalshi.py) ·
[`dutchbook_scan.py`](recon/dutchbook_scan.py)
Raw output: [`recon_report.txt`](recon/recon_report.txt) · [`dutchbook_report.txt`](recon/dutchbook_report.txt) ·
[`mece_caveats.txt`](recon/mece_caveats.txt)

> This file replaces assumptions in PLAN.md §3 with measurements. Where a number here contradicts an
> earlier estimate, **this file wins** and PLAN.md has been updated.

---

## 0. Headline findings

| # | Finding | Consequence |
|---|---|---|
| **F1** | **`mutually_exclusive` does NOT mean exhaustive.** 33 events price at `sum(YES ask) < 0.90`, not arbitrage, but races whose listed outcomes don't cover the outcome space (no "other" leg). | A naive Dutch-book bot would treat these as the *most* profitable trades and lose ~100% on each. This is correction C4 measured. **Hard exhaustiveness gate required.** |
| **F2** | The `/markets` endpoint is ~99% multivariate parlay shards. The real universe is only reachable via `/events?with_nested_markets=true`. | Recorder must page `/events`, not `/markets`. |
| **F3** | Kalshi publishes `mutually_exclusive`, `settlement_sources`, and `collateral_return_type` **per event**. | S2's MECE candidate detection is a field read, not an inference. Rulebook engine gets structured input free. |
| **F4** | Every MECE event carries `collateral_return_type = MECNET`. | Strongly implies collateral netting across legs, would materially improve S2 capital efficiency. **Must be verified against authenticated margin endpoints before sizing.** |
| **F5** | 84.8% of markets have **zero** 24h volume; the top 200 markets are 62.7% of all volume. | Capacity concentration is worse than assumed. Liquidity filters are not optional. |
| **F6** | Fee hurdles measured: an n=2 Dutch book needs `sum(px) < 0.9650` as taker but `< 0.9913` as maker. | Correction C2 confirmed, the maker window is ~2.6 points wider, in the price region where density is highest. |
| **F7** | Genuine (n≥3) liquidity-filtered maker structures: **147**, ~$15.7k deployable, ~$282 one-shot if all complete. | S2 is real but small, confirming correction C5 (RV alone cannot fill the book). |
| **F8** | 100% of markets expose `rules_primary` (median 142 chars); 100% of events expose `settlement_sources`. | The rulebook engine is buildable immediately, at full universe coverage. |

---

## 1. Universe composition

```
events (non-parlay, with live quotes)  12,552
quoted markets                        103,449
distinct series                         3,789
```

| Category | Events | Share |
|---|---:|---:|
| Sports | 7,374 | 58.7% |
| Elections | 2,174 | 17.3% |
| Entertainment | 676 | 5.4% |
| Financials | 629 | 5.0% |
| Politics | 596 | 4.7% |
| Economics | 490 | 3.9% |
| Climate and Weather | 195 | 1.6% |
| Science and Technology | 140 | 1.1% |
| Crypto | 105 | 0.8% |
| Companies / Commodities / Mentions / other | 165 | 1.3% |

`collateral_return_type`: `MECNET` 6,088 · `DIRECNET` 2,239 · absent 4,225.

**Note on `/markets`:** a 60,000-row page-capped pull returned 59,575 `KXMVE*` multivariate parlay shards
(99.3%) and only 425 regular markets. Multivariate cross-category combos appear to dominate that endpoint's
ordering. All analysis here uses `/events?with_nested_markets=true`, which returned the complete universe
(12,553 events, un-truncated).

---

## 2. Microstructure: spreads, depth, volume

**Spreads** (`yes_ask − yes_bid`, dollars):

| p5 | p25 | p50 | p75 | p95 | mean |
|---:|---:|---:|---:|---:|---:|
| 0.010 | 0.030 | **0.070** | 0.390 | 0.990 | 0.268 |

| Band | Count | Share |
|---|---:|---:|
| ≤ 1c | 11,885 | 11.5% |
| ≤ 2c | 21,460 | 20.7% |
| ≤ 5c | 39,668 | 38.3% |
| ≥ 50c | 23,571 | 22.8% |

**Depth at touch** (yes-bid side):

| Metric | p5 | p25 | p50 | p75 | p95 |
|---|---:|---:|---:|---:|---:|
| bid size (contracts) | 0 | 0 | 20 | 200 | 3,360 |
| **bid notional ($)** | $0 | $0 | **$4** | $83 | $1,066 |

**Volume concentration:**

```
markets with ZERO 24h volume    87,753   84.8%
top   10 markets = 22.1% of 24h volume
top   50 markets = 42.1%
top  200 markets = 62.7%
top 1000 markets = 84.2%
```

**Time to close:** p50 = 1,608 hours (~67 days). 47.4% of markets close more than 3 months out; only 7.3%
within 24 hours. Capital lockup is a first-order cost, annualized return on locked capital must be computed
per structure (PLAN.md §9 `min_annualized_return_on_locked_capital`).

---

## 3. S1: structural maker basket: universe is larger than assumed

| Filter | Markets |
|---|---:|
| mid ∈ [0.70, 0.95] | 9,944 (9.6% of quoted) |
| + horizon 1h–90d, bid notional ≥ $200 | 1,497 |
| **+ nonzero 24h volume → S1 universe** | **547** |

- **Deployable at 20% of touch depth: ~$198,800.** Comfortably above the plan's working-capital scale, so
  S1 is *not* capacity-constrained at a five-figure bankroll, depth, not opportunity count, was the worry
  and it is unfounded at this size.
- Their spreads are tight: median 2c (p25 = 1c, p95 = 12c).
- Their 24h volume: median 203 contracts (p75 = 797, p95 = 9,241).
- Composition: **Sports 401, Commodities 46, Crypto 30, Financials 22, Economics 19**, Entertainment 12.
  Top series `KXNCAAFGAME` (49), `KXITFMATCH` (39), `KXNFLTOTAL` (33), `KXBTCD` (16).

**Implication for PLAN.md §3.1:** S1's universe is dominated by sports, not by the single-name/politics
categories where the documented favorite-longshot bias is largest. The `THETA_BY_HORIZON` recalibration and
the single-name adjustment must therefore be **fitted per category**, and the sleeve should report edge
separately for sports vs non-sports, they are effectively two different strategies wearing one name.

---

## 4. S2: Dutch books: real, small, and booby-trapped

### 4.1 The universe

```
events flagged mutually_exclusive        6,088   (48.5% of all events)
  ... with 2-30 live quoted legs         5,826
leg counts: n=2 2,666 | n=3 1,876 | n=4 135 | n=5 99 | n=6 222 | n≥7 ~830
by category: Sports 4,623 | Elections 913 | Entertainment 225 | Weather 94 | Economics 82
```

**Overround**: `sum(YES ask)` across MECE events:

| p5 | p25 | p50 | p75 | p95 |
|---:|---:|---:|---:|---:|
| 1.010 | 1.040 | **1.140** | 1.790 | 2.930 |

The median book is 14% overround. Buying every outcome at the ask is normally a large guaranteed loss.

### 4.2 F1: the exhaustiveness trap (the most important finding)

`mutually_exclusive = true` guarantees **at most one** outcome resolves YES. It does **not** guarantee that
**at least one** does. 33 flagged-MECE events price below 0.90, and in every inspected case the reason is a
non-exhaustive outcome list, not mispricing:

| sum(ask) | markets in event | Event | Title |
|---:|---:|---|---|
| 0.116 | 2 | `KXLAPRIMARY-02D26` | LA-02 Democratic nominee? |
| 0.125 | 2 | `KXLAPRIMARY-01R26` | LA-01 Republican nominee? |
| 0.140 | 8 | `KXSTATE51-29` | What will be the 51st state in Trump's term? |
| 0.198 | 6 | `KXNBERRECESSQ` | When will the next US recession start? |
| 0.282 | 7 | `KXNEWPOPE-70` | Who will the next Pope be? |
| 0.310 | 6 | `KXACQUANNOUNCEPINS-27JAN01` | Who will acquire Pinterest this year? |
| 0.320 | 32 | `KXNEXTTEAMNFL-26BSOR` | Brendan Sorsby's Next Team |

Quoted legs equal total markets in the event for 99.3% of flagged-MECE events, so this is **not** a data
gap in the harvest. The events genuinely list only some candidates, with no "Other/None" leg.

**A naive scanner ranks these as its best opportunities** (up to +87c "net margin"). Buying all legs of
"Who will the next Pope be?" for 28c returns $0 whenever the winner isn't one of the seven listed.

> **Gate added to PLAN.md §3.2:** `check_mece()` must verify exhaustiveness independently of the exchange
> flag. Minimum test: reject any structure whose `sum(YES bid) < 0.80` unless an explicit "Other/None" leg
> is present in the event, and require the rules text to state that one listed outcome must occur.

### 4.3 Honest profitability, after the liquidity filter

Filter: 24h volume > 0, min leg size ≥ 20 contracts, every leg spread ≤ 10c, > 1h to close.

| | Taker | Maker |
|---|---:|---:|
| candidates | 5,826 | 4,568 (legs all have a real bid) |
| positive **before** fees | 89 (1.5%) | 3,880 (84.9%) |
| positive **after** fees | 47 (0.8%) | 3,793 (83.0%) |
| **+ liquidity filter** | **11 (0.2%)** | **504 (11.0%)** |

Of the 47 fee-profitable taker structures, **33 are the F1 exhaustiveness trap**, leaving roughly a dozen
plausible ones, of which the credible tight-book examples are modest: `KXFEDDECISION-27MAR` (n=5, sum
0.940, **+2.64c**, 184 contracts, 1c spreads) and `KXDCCCCHAIR-27MAR02` (n=9, sum 0.902, **+4.20c**, 1,131
contracts, 1c spreads, 11,984 24h volume).

### 4.4 Separating arbitrage from market making

For **n = 2**, a "maker Dutch book" is just resting a bid on both sides inside the spread, that is
two-sided market making (sleeve S6), and its margin is realized only if **both** legs fill. It is not
locked arbitrage and must not be accounted as such.

```
liquidity-filtered maker-profitable structures : 504
  n == 2  (market making, both-fill risk)      : 357   70.8%
  n >= 3  (genuine multi-outcome inconsistency): 147   29.2%
```

| | n = 2 | n ≥ 3 |
|---|---|---|
| net margin p25/p50/p75 | 0.30c / 1.19c / 2.18c | 0.86c / 1.14c / 2.36c |
| p95 | 4.22c | 6.90c |

**Genuine S2 (n ≥ 3): 147 structures, ~$15,700 deployable at 20% of min-leg size, ~$282 one-shot profit if
every structure completes.** Composition: Sports 132, Elections 6, Politics 3, Financials 3, Weather 2.

This is the honest size of the Dutch-book sleeve: real, repeatable as books churn, and far too small to be
the whole book, exactly as correction C5 predicted.

### 4.5 F6: fee hurdles, measured

| n legs | taker fee hurdle | maker fee hurdle | max `sum(px)` to profit: taker | maker |
|---:|---:|---:|---:|---:|
| 2 | 3.50c | 0.87c | 0.9650 | **0.9913** |
| 3 | 4.59c | 1.15c | 0.9541 | 0.9885 |
| 5 | 5.47c | 1.37c | 0.9453 | 0.9863 |
| 8 | 5.97c | 1.49c | 0.9403 | 0.9851 |
| 12 | 6.24c | 1.56c | 0.9376 | 0.9844 |

Given that `sum(ask)` clusters just above 1.00 (p5 = 1.010, p25 = 1.040), the ~2.6-point wider maker window
is the difference between "almost never" and "regularly". **Correction C2 is confirmed on live data.**

---

## 5. S3: linked relative value: structure is abundant

```
multi-leg events NOT flagged mutually_exclusive : 5,103   <- L2/L3/L4 link candidates
events with >= 3 legs (threshold ladders)       : 8,156
```

Non-MECE multi-leg events by category: Sports 2,134 · Elections 1,162 · Financials 566 · Entertainment 328
· Economics 295 · Politics 255.

Top ladder series, these are the **L2 implication** and **L3 partition** hunting grounds:
`KXMIDTERMVOTETURN` (502), `KXMIDTERMMOV` (475), `KXNCAAF1H` (206), `KXMLBINNINGWIN` (136),
`KXNCAAFTOTAL` (78), `KXNCAAFSPREAD` (78), `KXNCAAFWINS` (73), `KXARTISTSTREAMSY` (61), `KXVOTEPRIMARY` (41).

Threshold-ladder series (`...TOTAL`, `...SPREAD`, `...MOV`, `...VOTETURN`) are structurally guaranteed to
contain monotone constraints, `P(total > 45) ≤ P(total > 40)`, which is the cleanest possible L2 link and
requires no forecasting at all. **This is the highest-value target for the first S3 implementation.**

---

## 6. Rulebook engine: fully feasible today

```
markets exposing rules_primary : 103,449   100.0%
  length p25/p50/p95           : 118 / 142 / 245 chars
events exposing settlement_sources : 12,552   100.0%
```

Rules text is short enough that LLM extraction over the entire universe is cheap (median 142 characters).

Top settlement sources by event count:

| Source | Events |
|---|---:|
| ESPN | 5,062 |
| Fox Sports | 3,057 |
| the Governing League | 1,574 |
| Kalshi using information originating from the NCAA | 1,202 |
| The Wall Street Journal | 583 |
| Flashscore | 542 |
| official election authority responsible for certifying results | 525 |
| Library of Congress | 512 |
| Reuters | 508 |
| The New York Times / AP / Washington Post / CNN | 417–427 each |

**Implication:** source-matching for S3 link equivalence is largely a small closed vocabulary. Cross-source
links (e.g. an ESPN-settled market against a Fox-Sports-settled market on the same game) are exactly the
`NEEDS_HUMAN` cases the plan routes to review.

---

## 7. Changes forced into PLAN.md

1. **§3.2 `check_mece()`**: add an explicit exhaustiveness gate; the exchange's `mutually_exclusive` flag
   is necessary but **not sufficient** (F1).
2. **§3.2**: account n=2 maker structures as S6 market making with both-fill risk, not as S2 arbitrage.
3. **§3.1**: fit `THETA_BY_HORIZON` and the single-name adjustment **per category**; report sports and
   non-sports edge separately.
4. **§6.1 recorder**: page `/events?with_nested_markets=true`, not `/markets` (F2).
5. **§5 schema**: persist `mutually_exclusive`, `collateral_return_type`, and `settlement_sources` on
   `market_snapshots` / a new `event_snapshots` table.
6. **§14**: add a task to verify MECNET collateral netting against authenticated endpoints before S2 sizing
   (F4); it changes return-on-locked-capital materially.
7. **§3.3**: prioritize threshold-ladder series (`...TOTAL`, `...SPREAD`, `...MOV`, `...VOTETURN`) as the
   first L2 implementation target.

---

## 8. Reproduction

```bash
python research/recon/harvest_kalshi.py     # ~2 min, writes kalshi_events.json.gz
python research/recon/analyze_kalshi.py     # universe characterization
python research/recon/dutchbook_scan.py     # S2 feasibility, honest maker model
```

No authentication required. Rerunning on a different date is the intended way to check whether these
opportunity counts are stable or were a snapshot artifact, **do this before committing to S2 sizing.**
