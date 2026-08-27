# Prediction Market Trading Engine

![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=flat&logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?style=flat&logo=sqlite&logoColor=white)
![Tests](https://img.shields.io/badge/tests-1095%20passing-2ea44f?style=flat)
![Data](https://img.shields.io/badge/dataset-7.5M%20rows-blue?style=flat)
![Venue](https://img.shields.io/badge/venue-Kalshi%20(CFTC%20DCM)-0A0A0A?style=flat)

> A production grade research engine that measures whether prediction markets are efficient, and quantifies exactly where the money goes when they are not. Built on 7.5 million rows of live Kalshi data, it runs multi leg strategies in shadow, prices every fill against real venue fees and queue position, and scores its own decisions with anytime valid statistics.

The engine is a complete trading stack. It enumerates a 138,000 market universe, forms and risk checks multi leg arbitrage structures, executes through a declarative diff based order manager with idempotency guarantees and a five second kill switch, materializes counterfactual fills against the real trade tape, ingests settlements, and produces a KPI digest with confidence intervals.

It was then pointed at the hardest question in the space: **is there a retail accessible edge on Kalshi?** Seven candidate strategies were tested to destruction across 4,258 settled markets. The results below quantify precisely how efficient this venue is, and where the money that does exist actually goes.

---

## Headline results

**Kalshi is calibrated, measured about as tightly as public data allows.** 4,258 settled markets, 1.39 million one minute candles, one observation per market taken strictly before settlement. Eighteen category and lead cells spanning Sports, Crypto, Financials, Commodities, Weather, Economics, Mentions and Entertainment at 30 minute, 2 hour and 24 hour horizons. **Not one survives Bonferroni correction once observations are clustered by event.** Skill scores run +0.21 to +0.74, so these markets are genuinely informative and not merely unbiased. Backfill coverage is 99.6 percent, so the sample is the population.

**The quote process mean reverts, and the spread is priced to capture exactly all of it.** Negative autocorrelation is robust at t = -21 and survives the bid ask bounce test on a single side of the book, so it is real rather than quote flicker. The predicted reversion scales cleanly with move size across two orders of magnitude, from 0.12c after a 1c move to 4.45c after a 20c move. **The spread stays 4 to 8 times larger in every single band.** The reversion is the market maker's compensation, and seeing the two track that tightly is what efficiency looks like from the inside.

**Deterministic arbitrage is absent to the precision of the instrument.** Four independent logical constraints tested across 131,872 synchronized observations: mutually exclusive legs summing to $1, spread ladders monotone in strike, total ladders monotone, and "wins by more than k" never trading above "wins". **Zero violations**, net of real per series taker fees.

**Where the money is, and why it is unreachable.** Genuine dislocations worth about **$1,258 per hour** do exist. Median episode lifetime is a single print, p90 is 48 milliseconds, and 65 percent of the value accrues to participants pairing legs within 5 milliseconds. A home round trip to Kalshi is 21ms, longer than a competitor's entire detect to acknowledge cycle. Capital was never the binding constraint; geography is.

---

## What the measurements showed

Seven strategies, each closed on a number rather than an argument. The mechanism matters more than the verdict.

| Strategy | Result | The measurement that settled it |
|---|---|---|
| Within event basket | no edge | Net **-6.32c per structure**, CI excluding zero on the losing side. P(all legs fill) = 0.0000, P(orphan given any fill) = 1.0000 |
| Across event ladders | **$0.00** | 62,838 nested pairs, 14 gross violations, 1 surviving fees, **0 with depth on both sides** |
| Cross market hedging | no edge | 0 violations across 131,872 synchronized observations on four independent constraints |
| Favourite longshot bias | not present | P(zero losses given calibration) = 0.638. Selling at the bid is EV negative under the null |
| Cross venue vs Polymarket | no edge | Combined fee is `13p(1-p)` cents, 3.25c at 50c, against a published 2 to 4c gap |
| Weather forecasting | no edge | Market implied error sd **1.95F** beats the best forecast buildable from free public data at **2.19F**, at matched lead |
| Passive market making | no edge | Every volume band above 1k has a **1 cent** median spread. 100 lots is 0.044 percent of qualifying liquidity |

The mechanism behind the first generalizes, and is the most useful single sentence here: **the margin gate selects books whose ask is rich precisely because nobody is buying it.** 67.5 percent of legs saw zero qualifying taker flow in 43 minutes, and median queue ahead was 2,249 contracts against an order size of 58.

---

## How the results were validated

Ten candidate edges appeared during development and **ten were destroyed by adversarial self testing before they could reach a conclusion.** Every one had flattered the result, which is the direction bias always runs. This is the part I would most want a reviewer to read, and the full detail is in [PLAN.md](PLAN.md) section C.

Three failure modes recur, and they generalize well beyond this venue.

**Getting the independence unit wrong, three separate times.** A quoting loop re-evaluating one ticker produced 470 correlated rows over 69 markets, which an e process turned into **E = 124,326** against a threshold of 20. Leg scoring a mutually exclusive basket reported a **77.8 percent win rate**, when exactly one leg pays and an n leg short "wins" (n-1)/n by arithmetic; per structure it is 12.5 percent, and the confidence intervals were 1.9 times too narrow. Most recently, 1,327 sports markets turned out to span only 226 games, and clustering by game took a z of +2.47 down to +1.46.

**Measuring the instrument instead of the market.** A $44 dislocation result collapsed to **$3.31** once every leg was required to have actually refreshed. The cause: `api.elections.kalshi.com` is CloudFront and serves cached bodies with an `age` header up to 13 seconds, so the "15 second market grid" was a CDN TTL. A unique query parameter forces a cache miss and doubles the observed resolution.

**Confidence intervals that are fiction before the tail arrives.** Selling longshots showed a 95 percent CI of [+0.0063, +0.0375] excluding zero. The sample standard deviation was small only because the loss branch had not occurred yet; the correct binomial test gives P(zero losses given calibration) = 0.638. On any payoff with rare large losses, a CI from observed sd is invalid until the rare event is in the sample.

Also caught and fixed: a documented contract that nothing enforced, which would have sized a 21 leg short collecting $11.06 against **$21 of liability**; a kill switch that missed its own five second deadline at scale, where the first fix added concurrency (which does not buy rate limit tokens) and the test that "verified" it had mocked out the limiter; a leakage detector that reported PASS on a deliberately cheating strategy whenever one keyword argument was omitted; and a WebSocket parser written faithfully to Kalshi's own documentation, which reads empty books because the wire actually sends fixed point dollar strings rather than the documented integer cents.

---

## Engineering

Four processes with one contract between them: the database is the truth, risk is enforced in the executor and never in a strategy, and a file on disk cancels everything within five seconds.

| Layer | Module | What it holds |
|---|---|---|
| Math | [core/math/](core/math/) | Fee model `theta p(1-p)`, Kelly with empirical Bayes shrinkage, e processes and sample size floors, tetrachoric correlation, Dutch book hurdles |
| Storage | [core/db.py](core/db.py) | SQLite schema, append only snapshots enforced by triggers, additive migrations that upgrade a 2 GB database in place in 0.02s |
| Venue | [venues/kalshi/](venues/kalshi/) | RSA PSS request signing, REST client with token bucket, WebSocket with sequence gap detection and verified resync |
| Recording | [recorder/](recorder/) | Universe sweep at 109,000 markets in 26s, L1 quotes plus labelled trade tape, settlement ingestion, historical candle backfill |
| Strategy | [strategy/](strategy/) | Within event baskets, across event linked markets, structural maker, weather research harness |
| Risk | [risk/engine.py](risk/engine.py) | Every limit in one file, plus a validator that refuses to start on a self inconsistent configuration |
| Execution | [execution/](execution/) | Declarative diff based executor, OMS with idempotency keys minted before the network call, kill switch, structure lifecycle with orphan detection |
| Analysis | [backtest/](backtest/) [monitor/](monitor/) | Three fill models reported as a bracket, leakage suite with a known cheating strategy as its control, mark out recorder, structure level validation harness |

**Invariants that are enforced rather than documented.** Position is derived from terminal fills and never from a counter. Every order carries an idempotency key minted before the network call, so a crash mid send cannot double fill. Multi leg structures are atomic: if the risk engine denies one leg, every leg is dropped, because a partially placed hedge is a naked directional bet wearing an arbitrage's clothes.

---

## Data

Everything is public and unauthenticated. No account is required to reproduce any result here.

```
market snapshots   3,404,590        trades          2,773,285
candles            1,388,378        settlements         4,277
distinct markets     138,193        candle coverage     99.6%
```

The candle backfill is what made statistical power possible. `/series/{s}/markets/{t}/candlesticks` returns one minute OHLC of both sides of the book and **works on already settled markets**, so each backfilled market is a complete labelled example: the full price path the market believed, and the outcome that occurred. Ten minutes of fetching replaced sixteen days of waiting, and took the demonstrable edge floor from 17.7 percentage points to **4.06**.

Two traps worth knowing. Do not enumerate via `status=settled`, which returns almost entirely parlay shards; 40 pages produced 8,000 markets of which 4 were real. Resolve from your own recorded universe instead, using `/markets?tickers=` at 100 markets per request.

---

## Running it

```bash
pip install -e .
python -m pytest -m "not live"          # 1095 offline tests
python -m pytest -m live                # 16 tests against the public API

python -m scripts.operate               # the whole pipeline, unattended
python -m recorder.l1 --interval 5      # L1 quotes plus trade tape
python -m recorder.history --limit 500  # backfill candle history
python -m runner --mode shadow --once   # one shadow trading cycle
python -m monitor.main                  # the KPI digest
```

`scripts/operate.py` runs four tasks on independent cadences with isolated failures, because the tape is the only thing that cannot be backfilled. A quote missed at 14:03:22 is gone; a settlement missed now is still there in an hour.

Touch a file named `KILL` in the run directory and everything stops within one poll.
