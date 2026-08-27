# Prediction Market Trading Engine

![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=flat&logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?style=flat&logo=sqlite&logoColor=white)
![Tests](https://img.shields.io/badge/tests-1095%20passing-2ea44f?style=flat)
![Venue](https://img.shields.io/badge/venue-Kalshi%20(CFTC%20DCM)-0A0A0A?style=flat)
![Mode](https://img.shields.io/badge/mode-shadow%20only-orange?style=flat)

> A systematic trading engine for Kalshi prediction markets, built to answer one question with evidence rather than opinion: does a retail accessible edge actually exist? It runs strategies in shadow against live production data, scores itself with anytime valid statistics, and refuses to trade anything it cannot demonstrate.

The engine is complete and runs end to end: it records the live universe, forms multi leg structures, enforces risk, materializes counterfactual fills, ingests settlements, and scores its own decisions. What it found is that the edge is not there. Seven strategies were tested and closed on measurements, and along the way nine separate measurement errors were caught, every one of which had flattered the result.

The headline finding is a negative. That is the point, and the [errata](#what-building-it-corrected) are the most useful part of the repository.

**Nothing here has traded real money.** `runner.py` refuses live mode, and the results below come from public market data and shadow execution.

---

## Highlights

- **Deterministic arbitrage does not exist here.** Four independent logical constraints tested across **131,872 synchronized observations**: mutually exclusive legs summing to $1, spread ladders monotone in strike, total ladders monotone, and "wins by more than k" never trading above "wins". **Zero violations**, net of real per series taker fees. See [Findings](#findings).
- **The market is calibrated, everywhere it can be measured.** **4,258 settled markets and 1.39 million one minute candles**, one observation per market, taken strictly before settlement. **17 category and lead cells** spanning Sports, Crypto, Financials, Commodities, Weather, Economics, Mentions and Entertainment, at 30 minute, 2 hour and 24 hour leads. **Not one survives Bonferroni correction.** Skill scores run from +0.21 to +0.74, so these markets are genuinely informative rather than merely unbiased. Backfill coverage is 99.6%, so the sample is the population and selection bias is eliminated by construction rather than argued away.
- **The quote process mean reverts, and the spread eats exactly all of it.** Real and robust at t = -21, surviving the bid ask bounce test on a single side of the book. The predicted reversion scales cleanly with move size over two orders of magnitude, from 0.12c after a 1c move to 4.45c after a 20c move, and **the spread stays 4 to 8 times larger in every band**. The reversion is the market maker's compensation, priced precisely.
- **Latency arbitrage is real and unreachable.** About **$1,258 per hour** of genuine dislocations exists, but median episode lifetime is a single print, p90 is 48ms, and 65% of the money goes to participants pairing legs within 5ms. Home round trip to Kalshi is 21ms, longer than a competitor's entire detect to acknowledge cycle. Capital was never the constraint.
- **A public data path that removes the calendar constraint.** Kalshi serves full order book depth and historical one minute OHLC on already settled markets, unauthenticated. Ten minutes of fetching replaced sixteen days of waiting and took the demonstrable edge floor from **17.7pp to 4.06pp**. See [Data](#data).

---

## Findings

Every hypothesis was closed on a measurement, not an argument. Each has a mechanism, which matters more than the verdict.

| Strategy | Verdict | The number that decided it |
|---|---|---|
| Within event basket (S2) | refuted | Net **-6.32c per structure**, CI excludes zero on the losing side. P(all legs fill) = 0.0000, P(orphan given any fill) = 1.0000 |
| Across event ladders (S3) | **$0.00** | 62,838 nested pairs, 14 gross violations, 1 surviving fees, **0 with depth on both sides** |
| Cross market hedging | refuted | 0 violations across 131,872 synchronized observations on four constraints |
| Favourite longshot bias | not rejected | P(zero losses given calibration) = 0.638. Selling at the bid is EV negative under the null |
| Cross venue vs Polymarket | refuted | Combined fee is `13p(1-p)` cents, 3.25c at 50c, against a published 2 to 4c gap |
| Weather forecasting | refuted | Market implied error sd **1.95F** beats the best free public forecast at **2.19F**, at matched lead |
| Passive market making | refuted | Every volume band above 1k has a **1 cent** median spread. 100 lots is 0.044% of qualifying liquidity |

The mechanism behind the first one generalizes: **the margin gate selects books whose ask is rich because nobody is buying it.** 67.5% of legs saw zero qualifying taker flow in 43 minutes, and median queue ahead was 2,249 contracts against an order size of 58.

---

## What building it corrected

Nine measurement errors, all found by running code against reality. Every one made the result look better than it was, which is the pattern worth internalizing. Full detail in [PLAN.md](PLAN.md) section C.

1. **A contract that nothing enforced.** `MeceCheck.safe_to_sell` documented a mutual exclusivity requirement and never checked it. A sleeve read the docstring and trusted it, then sized a 21 leg short on a nested threshold ladder, collecting $11.06 against **$21 of liability** and reporting a margin of $10.01 per contract on an instrument that pays at most $1.
2. **Leg scoring on a mutually exclusive basket.** Exactly one leg pays, so an n leg short "wins" (n-1)/n by arithmetic. This reported a **77.8% win rate** and an e process of **124,326** against a threshold of 20. Scored per structure it is 12.5%, and confidence intervals were 1.9 times too narrow.
3. **A zero loss sample making a short vol CI invalid.** Selling longshots showed a 95% CI of [+0.0063, +0.0375] excluding zero. The sample standard deviation was small only because the loss branch had not happened yet. The correct binomial test gives P(zero losses given calibration) = 0.638.
4. **Measuring the instrument instead of the market.** A $44 dislocation result turned out to be a 15 second polling grid. Requiring every leg to have refreshed collapsed it to **$3.31**.
5. **A CDN, not an exchange.** `api.elections.kalshi.com` is CloudFront and serves cached bodies with an `age` header up to 13 seconds. That "15 second grid" was cache TTL. A unique query parameter forces a miss and doubles the observed resolution.
6. **A kill switch that could not meet its own deadline.** Cancel all was serial through a 100 token per second bucket: 300 orders took 5.10s against a 5s promise, 1000 took 19s. The first fix added concurrency, which does not buy tokens, and the test that "verified" it had mocked out the rate limiter. The real fix is a hard cap on resting orders derived from the deadline.
7. **A safety check disabled by a missing argument.** A deliberately cheating look ahead strategy reported PASS whenever one keyword argument was omitted, because it kept reading the untruncated database. It now fails closed.
8. **Documentation that did not match the wire.** Kalshi's WebSocket sends fixed point dollar strings, not the documented integer cent arrays. A parser written faithfully to the docs reads empty books and raises nothing, which downstream looks exactly like "no liquidity".
9. **Outcome dependent selection in my own sampling.** The backfill ordered candidates by volume, and volume correlates with outcome. The sampled and unsampled sets differed at **z = -12.33**. This one was in the experimental design rather than the arithmetic, and it quietly contaminates everything downstream instead of producing one wrong number.

---

## Architecture

Four processes with one contract between them: the database is the truth, risk is enforced in the executor and never in a strategy, and a file on disk cancels everything within five seconds.

| Layer | Module | What it holds |
|---|---|---|
| Math | [core/math/](core/math/) | Fee model `theta p(1-p)`, Kelly with shrinkage, e process and sample sizes, tetrachoric correlation, Dutch book hurdles |
| Storage | [core/db.py](core/db.py) | SQLite schema, append only snapshots enforced by triggers, additive migrations that survive a 2 GB database in place |
| Venue | [venues/kalshi/](venues/kalshi/) | RSA PSS signing, REST client with token bucket, WebSocket with sequence gap detection and resync |
| Recording | [recorder/](recorder/) | Universe sweep, L1 quotes plus labelled trade tape, settlement ingestion, historical candle backfill |
| Strategy | [strategy/](strategy/) | S2 within event baskets, S3 across event links, S1 structural maker, weather research harness |
| Risk | [risk/engine.py](risk/engine.py) | Every limit in one file, plus a validator that refuses to start on a self inconsistent configuration |
| Execution | [execution/](execution/) | Declarative diff based executor, OMS with idempotency keys, kill switch, structure lifecycle with orphan detection |
| Analysis | [backtest/](backtest/) [monitor/](monitor/) | Three fill models, leakage suite, mark out recorder, structure level validation harness |

**Invariants worth naming.** Position is derived from terminal fills and never from a counter. Orders carry an idempotency key minted before the network call, so a crash cannot double send. Multi leg structures are atomic: if the risk engine denies one leg, every leg is dropped, because a partially placed hedge is a naked directional bet.

---

## Data

All of it is public and unauthenticated. No account is required to reproduce any result here.

```
market snapshots   3,404,590        trades          2,773,285
candles            1,388,378        settlements         4,277
distinct markets     138,193        candle coverage     99.6%
```

The candle backfill is what made statistical power possible. `/series/{s}/markets/{t}/candlesticks` returns one minute OHLC of both sides of the book and **works on already settled markets**, so each backfilled market is a complete labelled example: the full price path the market believed, and the outcome that occurred. Do not enumerate via `status=settled`, which returns almost entirely parlay shards. Resolve from your own recorded universe with `/markets?tickers=` at 100 markets per request.

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
