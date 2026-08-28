# Prediction Market Trading Engine

![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=flat&logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?style=flat&logo=sqlite&logoColor=white)
![Tests](https://img.shields.io/badge/tests-1133%20passing-2ea44f?style=flat)
![Data](https://img.shields.io/badge/dataset-7.5M%20rows-blue?style=flat)
![Venue](https://img.shields.io/badge/venue-Kalshi%20(CFTC%20DCM)-0A0A0A?style=flat)

> A complete trading stack that measures whether prediction markets are efficient, and quantifies exactly where the money goes when they are not. Built on 7.5 million rows of live Kalshi data, it runs multi-leg strategies in shadow, prices every fill against real venue fees and queue position, and scores its own decisions with anytime-valid statistics.

The engine enumerates a 138,000 market universe, forms and risk checks multi-leg arbitrage structures, executes through a declarative diff-based order manager with idempotency guarantees and a five-second kill switch, materialises counterfactual fills against the real trade tape, ingests settlements, and produces a KPI digest with confidence intervals.

It was then pointed at the hardest question in the space: **is there a retail-accessible edge on Kalshi?** Seven candidate strategies were tested to destruction across 4,258 settled markets. The results below quantify precisely how efficient this venue is, and where the money that does exist actually goes.

```
23,000 lines of engine, strategy and research    1,133 offline tests, 17 live
14,000 lines of tests                            138,193 distinct markets
7.5M rows of live venue data                     99.6% candle coverage
```

---

## Headline results

**Kalshi is calibrated, measured about as tightly as public data allows.** 4,258 settled markets, 1.39 million one-minute candles, one observation per market taken strictly before settlement. Eighteen category and lead cells spanning Sports, Crypto, Financials, Commodities, Weather, Economics, Mentions and Entertainment at 30-minute, 2-hour and 24-hour horizons. **Not one survives Bonferroni correction once observations are clustered by event.** Skill scores run +0.21 to +0.74, so these markets are genuinely informative and not merely unbiased. Backfill coverage is 99.6 percent, so the sample is the population.

**The quote process mean reverts, and the spread is priced to capture exactly all of it.** Negative autocorrelation is robust at t = -21 and survives the bid-ask bounce test on a single side of the book, so it is real rather than quote flicker. The predicted reversion scales cleanly with move size across two orders of magnitude, from 0.12c after a 1c move to 4.45c after a 20c move. **The spread stays 4 to 8 times larger in every single band.** The reversion is the market maker's compensation, and seeing the two track that tightly is what efficiency looks like from the inside.

**Order flow is informed, and the spread does not cover it.** Measured on 429,335 trades across 534 markets, clustered by market: the mid moves **+1.94c in the taker's direction within one minute** of their print, t = +11.0, and the move persists at 5 and 30 minutes rather than decaying, which makes it permanent impact rather than a liquidity cost. Against a mean half-spread of 1.43c, that puts the passive market maker at **-0.51c per contract before fees**, and makers pay zero fees on 13,412 of 13,545 series, so there is nothing left to recover it. This is the mechanism behind the market-making result below, measured directly rather than cited.

Measuring it correctly is the whole trick. Marking a taker's fill price against a later mid charges them half the spread by construction and reports a number about the spread rather than about information. Measuring the change in the mid isolates the information content of the flow, and reverses the conclusion.

**The one real anomaly is a tick-size artifact, not a behavioural bias.** Longshots lose heavily. Pooled side-neutrally across 1,148 settled tickers, contracts bought between 1c and 10c return **-72.2%** on 19.5 million contracts, and the gradient runs monotonically to +3.0% at 91-99c, reproducing the published Kalshi favourite-longshot result on 2026 data. The mechanism is the **price floor**: 1c contracts win 0.006 percent against an implied 1.0 percent, a 167x gap that falls to 39x by 5c and 1.9x by 8c. A contract whose true probability is six thousandths of one percent cannot be quoted below one cent, so it is mechanically overpriced with no trader error required. Selling into it is profitable in the data and is bounded by capital rather than by edge. Size weighted across non-index markets, selling everything in the 1-7c band returns **+3.23% on locked collateral** and the 8-15c band **+4.58%**, while the 26-40c band **loses 5.00%**, so the profit is specific to the floor-bound region rather than a general longshot effect. Two things keep this from being a strategy. It needs **$14.9M of collateral** to capture $482k, and at any realistic size you are competing to be the seller in trades that already have one. And the sample has not yet paid its tail: the 1-7c band paid out $20,604 against $503,137 collected, so the loss branch is 4 percent realised, which is exactly the shape that looks excellent right up until it does not.

**Deterministic arbitrage is absent to the precision of the instrument.** Four independent logical constraints tested across 131,872 synchronised observations: mutually exclusive legs summing to $1, spread ladders monotone in strike, total ladders monotone, and "wins by more than k" never trading above "wins". **Zero violations**, net of real per-series taker fees.

**And the complete joint test agrees.** Pairwise constraints only check the relations somebody thought to encode. Kalshi lists up to twenty market types on one game -- moneyline, spread, total, team total, both-teams-score, exact score -- and all of them are indicators over the same grid of final scores, so the whole board admits one question: does **any** probability distribution price every market inside its own spread simultaneously? If none does then, by LP duality, a portfolio exists with non-negative payoff in every state and negative cost. Exact-score markets pin the entire joint distribution, which makes this strictly stronger than any pairwise scan, and it catches relations nobody named. Getting a trustworthy answer meant killing three parse bugs first, each betrayed by the same tell -- a violation far too large to be real. A first-half total scored against the full-time state space showed 24c. A soccer-sized nine-goal grid applied to a WNBA board showed 76c. And the guard written to catch the second bug read the digits of a game code, `26AUG261610PITSD`, as a scoring threshold of 261610, which quietly excluded a third of the sample. The grid is now sized from the legs that actually parse. Result across **34 games**, with no board excluded for grid size: **zero violations**. The implementation is [rulebook/jointarb.py](rulebook/jointarb.py).

**And so does the cross-event test, which is the one nobody runs.** Every constraint above lives inside a single event. Kalshi also prices the same world in two events that never touch: `KXHIGHLAX-26AUG26` partitions the day's maximum temperature into buckets, while `KXTEMPLAXH-26AUG2616` asks whether the reading at 4pm exceeds a threshold. Separate series, separate order books, and welded together by something that cannot fail -- the day's maximum is at least the reading at any hour inside it. Buying every bucket that overlaps `[X, inf)` and selling the hourly gives a payoff that is never negative, so any price gap is riskless. The first run reported 35 hits worth up to 80c, all of them false: the basket was assembled from whatever happened to be quoted that minute, and a partition with a hole in it looks cheap precisely because it is incomplete. With a tiling guard in place, **402 complete hedges priced across six cities, zero profitable**, median -43c and a best case of exactly breakeven. The implementation is [rulebook/crossevent.py](rulebook/crossevent.py).

**The one real cross-event violation found, and the tape that explains it.** The same test applied to knockout cup ties, where Kalshi prices the 90 minute result and who advances as two separate events, bounded on both sides: winning implies advancing, and advancing implies you did not lose in regulation. Across **2,683 team-minutes**, five gross violations. Priced with Kalshi's real fee, `ceil(0.07 x C x P x (1-P))` rounded up **per order**, all five die at one contract and three survive from ten contracts up, worth **+1.33c, +0.52c and +0.30c** per contract. Two of the three sit in a book quoted 6/35 with zero volume, so they are not executable at any size. The third is real, and the minute-by-minute tape shows the mechanism exactly: Toluca's game market repriced from 73 to 87 on **10,150 contracts**, the advance market stayed at 82/85 for under a minute while the game book already knew, and then repriced on **10,595 contracts**. The violation is the lag between two books during a repricing event. That is a genuine arbitrage, it was worth 0.30c per contract net, and it belonged to whoever was closest to the exchange.

**Where the money is, and why it is unreachable.** Genuine dislocations worth about **$1,258 per hour** do exist. Median episode lifetime is a single print, p90 is 48 milliseconds, and 65 percent of the value accrues to participants pairing legs within 5 milliseconds. A home round trip to Kalshi is 21ms, longer than a competitor's entire detect-to-acknowledge cycle. Capital was never the binding constraint. Geography is.

---

## What the measurements showed

Seven strategies, each closed on a number rather than an argument. The mechanism matters more than the verdict.

| Strategy | Finding | The measurement that settled it |
|---|---|---|
| Within-event basket | priced through | Net **-6.32c per structure**, CI excluding zero on the losing side. P(all legs fill) = 0.0000, P(orphan given any fill) = 1.0000 |
| Across-event ladders | **$0.00 available** | 62,838 nested pairs, 14 gross violations, 1 surviving fees, **0 with depth on both sides** |
| Cross-market hedging | no violations | 0 across 131,872 synchronised observations on four independent constraints |
| Favourite-longshot bias | **present, and it is the tick floor** | 1-10c contracts return **-72.2%** over 19.5M contracts. But 1c contracts win 0.006% against an implied 1.0%, a 167x gap that collapses by 8c, so the price floor does the work rather than a behavioural bias |
| Cross-venue vs Polymarket | fee-dominated | Combined fee is `13p(1-p)` cents, 3.25c at 50c, against a published 2 to 4c gap |
| Weather forecasting | market wins | Market-implied error sd **1.95F** beats the best forecast buildable from free public data at **2.19F**, at matched lead |
| Passive market making | negative before fees | Adverse selection **1.94c** against a **1.43c** half-spread, so **-0.51c per contract**. Every volume band above 1k has a 1 cent median spread |

The mechanism behind the first generalises, and is the most useful single sentence here: **the margin gate selects books whose ask is rich precisely because nobody is buying it.** 67.5 percent of legs saw zero qualifying taker flow in 43 minutes, and median queue ahead was 2,249 contracts against an order size of 58.

---

## The statistical protocol

Every candidate edge that appeared during development was run through the same destruction protocol before it was allowed to reach a conclusion. Bias in a backtest runs in one direction, so the protocol is built to attack the result rather than to confirm it.

**The independence unit is chosen before the test, and it is almost never the row.** A quoting loop re-evaluating one ticker generates hundreds of correlated rows across a handful of markets, and an e-process reads that as overwhelming evidence, E = 124,326 against a threshold of 20. Leg-scoring a mutually exclusive basket reports a 77.8 percent win rate when exactly one leg pays, because an n-leg short "wins" (n-1)/n by arithmetic; per structure it is 12.5 percent, and confidence intervals computed on legs are 1.9 times too narrow. 1,327 sports markets span only 226 games, and clustering by game takes a z of +2.47 down to +1.46. Everything reported above is clustered at the unit that actually varies independently, then Bonferroni corrected.

**Confidence intervals are not trusted until the tail is in the sample, and a rebuttal is not a hypothesis test.** Selling longshots showed a 95 percent CI of [+0.0063, +0.0375] excluding zero. The sample standard deviation was small only because the loss branch had not occurred yet, so on any payoff with rare large losses the correct instrument is a binomial test against the calibrated null: P(zero losses given calibration) = 0.638, and the claimed edge is refused. Refusing one strategy's edge is a different question from whether the bias exists, and separating the two is what produced the tick-floor result above. **A null from an underpowered test is not a finding**, so every claim here is stated alongside the power that produced it.

**The instrument is measured before the market is.** A dislocation result worth $44 per hour collapses to $3.31 once every leg is required to have actually refreshed since the previous observation, which is enforced rather than assumed.

**The leakage suite has a control.** A deliberately cheating strategy is checked into the suite and the detector has to catch it, because a leakage detector that only ever sees clean strategies has never been tested.

---

## What the venue actually does

Three findings about Kalshi's public infrastructure that are not in its documentation and that materially change what can be measured.

**The public API is CloudFront-fronted.** `api.elections.kalshi.com` serves cached bodies with an `age` header up to 13 seconds, so a naive "15 second market grid" is measuring a CDN TTL rather than the market. A unique query parameter forces a cache miss and doubles the observed resolution.

**The WebSocket wire format contradicts the documentation.** The published schema specifies integer cents; the wire sends fixed-point dollar strings. A parser written faithfully to the docs reads empty books and reports nothing wrong.

**Do not enumerate settled markets by status.** `status=settled` returns almost entirely parlay shards: 40 pages produced 8,000 markets of which 4 were real. Resolving from your own recorded universe via `/markets?tickers=` at 100 markets per request returns the actual population.

---

## Engineering

Four processes with one contract between them: the database is the truth, risk is enforced in the executor and never in a strategy, and a file on disk cancels everything within five seconds.

| Layer | Module | What it holds |
|---|---|---|
| Math | [core/math/](core/math/) | Fee model `theta p(1-p)`, Kelly with empirical Bayes shrinkage, e-processes and sample size floors, tetrachoric correlation, Dutch book hurdles |
| Storage | [core/db.py](core/db.py) | SQLite schema, append-only snapshots enforced by triggers, additive migrations that upgrade a 2 GB database in place in 0.02s |
| Venue | [venues/kalshi/](venues/kalshi/) | RSA-PSS request signing, REST client with token bucket, WebSocket with sequence gap detection and verified resync |
| Recording | [recorder/](recorder/) | Universe sweep at 109,000 markets in 26s, L1 quotes plus labelled trade tape, settlement ingestion, historical candle backfill |
| Rulebook | [rulebook/](rulebook/) | MECE exhaustiveness, the S3 link graph, the joint no-arbitrage LP over every market on a game, cross-event implications that emit L2 links at `NEEDS_HUMAN` rather than firing trades |
| Strategy | [strategy/](strategy/) | Within-event baskets, across-event linked markets, structural maker, weather research harness |
| Risk | [risk/engine.py](risk/engine.py) | Every limit in one file, plus a validator that refuses to start on a self-inconsistent configuration |
| Execution | [execution/](execution/) | Declarative diff-based executor, OMS with idempotency keys minted before the network call, kill switch, structure lifecycle with orphan detection |
| Analysis | [backtest/](backtest/) [monitor/](monitor/) | Three fill models reported as a bracket, leakage suite with a known cheating strategy as its control, mark-out recorder, structure-level validation harness |

**Invariants that are enforced rather than documented.** Position is derived from terminal fills and never from a counter. Every order carries an idempotency key minted before the network call, so a crash mid-send cannot double fill. Multi-leg structures are atomic: if the risk engine denies one leg, every leg is dropped, because a partially placed hedge is a naked directional bet wearing an arbitrage's clothes. Sizing contracts are checked by the engine rather than described in a comment, so a 21-leg short collecting $11.06 against $21 of liability cannot be placed.

---

## Data

Everything is public and unauthenticated. No account is required to reproduce any result here.

```
market snapshots   3,404,590        trades          2,773,285
candles            1,388,378        settlements         4,277
distinct markets     138,193        candle coverage     99.6%
```

The candle backfill is what made statistical power possible. `/series/{s}/markets/{t}/candlesticks` returns one-minute OHLC of both sides of the book and **works on already-settled markets**, so each backfilled market is a complete labelled example: the full price path the market believed, and the outcome that occurred. Ten minutes of fetching replaced sixteen days of waiting, and took the demonstrable edge floor from 17.7 percentage points to **4.06**.

---

## Running it

```bash
pip install -e .
python -m pytest -m "not live"          # 1133 offline tests
python -m pytest -m live                # 17 tests against the public API

python -m scripts.operate               # the whole pipeline, unattended
python -m recorder.l1 --interval 5      # L1 quotes plus trade tape
python -m recorder.history --limit 500  # backfill candle history
python -m runner --mode shadow --once   # one shadow trading cycle
python -m monitor.main                  # the KPI digest
```

`scripts/operate.py` runs four tasks on independent cadences with isolated failures, because the tape is the only thing that cannot be backfilled. A quote missed at 14:03:22 is gone; a settlement missed now is still there in an hour.

Touch a file named `KILL` in the run directory and everything stops within one poll.

Design document and full phase history: [PLAN.md](PLAN.md).
