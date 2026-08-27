# 16 -- Portability: Where Else Could This Engine Point, and What Would It Cost to Move It?

Written 2026-08-27, after the Kalshi verdict (calibration holds across 18 category-lead cells; zero
deterministic arbitrage across 131,872 synchronized observations; adverse selection 1.94c against a
1.43c half spread). Purpose: establish what in this codebase is actually Kalshi-shaped, which other
venues we could point it at, whether any of them plausibly still contains an edge, and whether the
move is worth making at all.

Every claim is tagged:

- **[M]** MEASURED -- computed by me this session, from the repository or from a live API call I made.
  The query or the request is described inline.
- **[C]** CITED -- someone else's published number, with link.
- **[I]** INFERRED -- my reasoning on top of [M] or [C]. Weakest tier. Treated as such.

---

## 0. The constraint, stated once, and what it actually forbids

The owner is an international student in the United States who cannot obtain a US SSN and therefore
cannot fund a Kalshi account. Nothing in this file proposes misrepresenting identity, residency or
nationality, and no VPN-based access to a geoblocked venue is considered. Where a venue is closed,
this file says so and moves on.

Two distinctions matter throughout and are kept separate everywhere below:

- **READ access** -- can we pull market data, book depth, a trade tape and history? This is what the
  research engine needs. It is frequently free and frequently unauthenticated.
- **TRADE access** -- can this person legally open and fund an account? This is what a strategy
  needs. It is frequently gated behind a US tax identifier or a residency test.

The second constraint is the binding one and it does **not** move by writing code.

---

## 1. What is actually Kalshi-coupled?

I read the code before searching the web, because the interesting question is not "does another
venue exist" but "how much of this system is a Kalshi artefact".

### 1.1 The layer map, measured in lines

**[M]** Counting non-blank, non-comment lines across every `.py` in the repo, excluding
`__pycache__`, `.pytest_cache` and `data/`:

| package | LOC | LOC in files importing `venues.kalshi` | occurrences of the string `kalshi` |
|---|---:|---:|---:|
| tests | 10,755 | 3,393 | 212 |
| backtest | 3,327 | 0 | 17 |
| execution | 3,075 | 600 | 30 |
| strategy | 2,060 | 0 | 14 |
| venues | 1,868 | 1,785 | 79 |
| scripts | 1,718 | 1,612 | 60 |
| core | 1,567 | 143 | 45 |
| monitor | 1,396 | 0 | 0 |
| recorder | 1,272 | 1,272 | 47 |
| rulebook | 832 | 0 | 10 |
| research | 827 | 0 | 31 |
| runner.py | 481 | 481 | 3 |
| shadow | 256 | 0 | 5 |
| risk | 247 | 0 | 3 |
| **total** | **29,681** | **9,286** | **556** |

**[M] `backtest`, `strategy`, `monitor`, `rulebook`, `shadow` and `risk` -- 8,118 LOC, 43% of the
non-test codebase -- do not import a Kalshi module at all.** The Sleeve protocol in
`strategy/base.py` is 93 lines and names no venue: `MarketSnapshot` carries `markets`, `events`,
`series`, a clock and a bankroll, and `DesiredQuote` carries a ticker, a side, a price in cents and
a size. The architectural bet in `strategy/base.py` C4.2b -- *sleeves never call a VenueClient, only
the executor does* -- paid off. That boundary is real and it holds.

### 1.2 The three obvious suspects, checked one at a time

**Suspect 1: the fee model. NOT coupled. The functional form is literally identical.**

`core/math/contracts.py` computes `fee = theta * p * (1 - p)` with `theta` looked up per venue and,
for Kalshi, per series. **[C] Polymarket's own fee documentation gives the same formula**:
`fee = C x feeRate x p x (1 - p)`, makers never charged, with `feeRate` varying by category
([docs.polymarket.com/trading/fees](https://docs.polymarket.com/trading/fees), fetched this session):

| venue / category | theta | fee at 5c | 10c | 25c | 50c |
|---|---:|---:|---:|---:|---:|
| Kalshi taker (13,412 of 13,545 series) | 0.07 | 0.33c | 0.63c | 1.31c | 1.75c |
| Polymarket intl taker, Crypto | 0.07 | 0.33c | 0.63c | 1.31c | 1.75c |
| Polymarket intl taker, Sports/Econ/Weather/Culture | 0.05 | 0.24c | 0.45c | 0.94c | 1.25c |
| Polymarket intl taker, Politics/Finance/Tech/Mentions | 0.04 | 0.19c | 0.36c | 0.75c | 1.00c |
| Polymarket intl taker, Geopolitics | 0.00 | 0.00c | 0.00c | 0.00c | 0.00c |
| Polymarket US taker (file 11) | 0.06 | 0.29c | 0.54c | 1.12c | 1.50c |
| Kalshi maker / Polymarket intl maker | 0.00 | 0 | 0 | 0 | 0 |

**[M] This resolves the open flag in file 11 section 2.2.** That file could not confirm whether
Polymarket's category-specific thetas were real and used a uniform 0.06 throughout. The primary
fee document now states the category table explicitly, and it confirms the flagged reading:
**geopolitics is theta 0**, and politics/finance sit at 0.04 rather than 0.06.

**[M] The code change this implies is 12 lines.** `core/math/contracts.py` is 226 lines, of which
exactly 12 name a venue-specific constant (`KALSHI_BASE_TAKER`, `KALSHI_MAKER_RATIO`, `THETA_FLAT`,
`KALSHI_HISTORICALLY_FEE_FREE`). `FeeSpec` already carries a per-instrument fee parameter looked up
from a cache -- Kalshi keys it on series, Polymarket would key it on category. Same shape, same
lookup, same call sites. `edge()`, `variance()`, `fee_ratio()`, `fee_death_zone_boundary()` and
`should_post_not_cross()` need no change at all.

**Suspect 2: the YES-referencing price convention. NOT coupled, and it is 108 lines.**

`execution/executor.py` states it outright: *"Venue-boundary helpers. YES-referencing happens HERE
and nowhere else."* **[M]** The functions that constitute that boundary are `_to_venue_side` (27
lines), `_parse_ack` (25), `_send_venue` (38), `_cancel` (15) and `_is_post_only_rejection` (3):
**108 lines of 600 in that file**. Everything above it -- the diff engine, the structure-completeness
check, the risk filter, the shadow router -- speaks in `Side.YES` / `Side.NO` at a YES-referenced
price and never sees a wire format.

And the convention transfers cleanly. Kalshi returns a book as `yes_dollars` and `no_dollars` and
the client folds it (`client.py:346`: *"the book is YES-referenced: yes_ask = 100 - best_no_bid"*).
**[M]** Polymarket's binary market is two ERC-1155 token ids with one book each; the same fold
applies -- `yes_ask = 1 - best_no_bid`. I confirmed both books are separately readable at
`GET https://clob.polymarket.com/book?token_id=...` (HTTP 200, no auth). The convention is a
property of binary contracts, not of Kalshi.

**Suspect 3: the mutually-exclusive event structure. NOT coupled -- the target venue has one too,
and its version is stronger.**

`core/models.py` carries `Event.mutually_exclusive` and `collateral_return_type` (MECNET/DIRECNET).
**[C] Polymarket's equivalent is "negative risk"**: in a neg-risk event, *"a No share in any market
can be converted into 1 Yes share in every other market"* via the Neg Risk Adapter contract
([docs.polymarket.com/concepts/negative-risk](https://docs.polymarket.com/concepts/negative-risk)).
**[M]** The flag is exposed on every Gamma market object as `negRisk`, and **46.0% of the top 600
markets by 24h volume carry it** (sample described in section 3.1). Mapping `negRisk` to
`Event.mutually_exclusive` is a one-line adapter change. See section 3.3 for why that mechanism is
bad news for the S2 sleeve rather than good news.

### 1.3 The thing that IS expensive: the integer-cent grid

This is the real coupling, and it is not the fee model or the side convention.

**[M] `core/models.py` declares `Cents = Annotated[int, Field(ge=1, le=99)]` and the whole system is
built on it.** Counting identifiers that carry the integer-cent assumption (`price_cents`, `yes_bid`,
`yes_ask`, `Cents`): **937 occurrences across 52 files.** The complement mirror `100 - p` appears
**24 times across 12 files** -- `backtest/fills.py`, `execution/executor.py`, `execution/fillfeed.py`,
`execution/structures.py`, `risk/engine.py`, `shadow/engine.py`, `strategy/s2_shortbasket.py`,
`strategy/s3_linked_rv.py`, `venues/kalshi/ws.py` and three test modules.

**[M] Polymarket does not use a 1c grid.** Sampling 600 active order-book markets from the Gamma API
sorted by 24h volume, `orderPriceMinTickSize` is **0.001 on 392 of them and 0.01 on 208**. Two
thirds of the liquid book quotes in tenths of a cent. A live `/book` pull returns bid levels at
0.001, 0.002, 0.003, 0.004, 0.005.

**[I] This is the "rewrite the price convention" case, not the "swap the venue adapter" case.** The
honest options are (a) rescale to integer tenths-of-a-cent, which changes `Cents` bounds from 1..99
to 1..999, changes every `100 - p` to `1000 - p`, and invalidates the fee-rounding and tick logic in
`should_post_not_cross`; or (b) introduce a per-market tick and carry prices as a fixed-point type,
which is cleaner and touches more. Either way this is the change that reaches 52 files and 1,095
tests. Everything else in this file is small next to it.

### 1.4 The honest fraction

**[I]** Of 18,926 non-test production LOC:

- **~10,600 LOC (56%) is venue-agnostic today** and would move unchanged: `backtest`, `strategy`,
  `monitor`, `rulebook`, `shadow`, `risk`, `core/math` less 12 lines, `core/db` less the schema's
  venue column which already exists.
- **~2,000 LOC (11%) is a venue adapter** -- `venues/kalshi/` (1,868) plus the 108-line executor
  boundary. Replaced, not edited.
- **~3,500 LOC (18%) is Kalshi-specific plumbing that must be rewritten but is mechanical**:
  `recorder/` (1,272), `scripts/` (1,612), `core/config.py` credentials (143), `runner.py` wiring
  (481 LOC, of which the Kalshi part is the client construction).
- **~2,800 LOC (15%) is agnostic in structure but carries the cent grid in its types** and would
  need a mechanical but wide edit.

**The Sleeve protocol, the risk engine, the backtest harness and the statistics are portable. The
recorder, the client and the price type are not.**

---

## 2. Access: which venues can we READ, and which could this person ever TRADE?

Everything in this table with an HTTP status was tested live from the owner's machine this session.

| venue | public unauth READ | historical | fee model | TRADE access for a US-resident non-SSN person |
|---|---|---|---|---|
| **Kalshi** | **[M] yes, REST only** | **[M] yes (hourly candles, full tape)** | theta 0.07 p(1-p) taker, maker 0 | **closed** -- US tax identifier required |
| **Polymarket intl** | **[M] yes, REST + WebSocket L2** | **[M] yes (prices-history, tape with wallet ids)** | theta 0.04-0.07 by category, maker 0 | **closed** -- US geoblocked |
| **Polymarket US** | **[M] no -- HTTP 401** | n/a | theta 0.06 taker, -0.0125 maker | **closed** -- KYC gates the API itself |
| **Manifold** | **[M] yes** | **[M] yes (bets endpoint + data dumps)** | play money, no cash conversion | **open** -- but no real money exists |
| **Betfair** | **[C] no -- app key requires KYC'd account** | paid product | 2-5% commission on net winnings | **closed** -- no US exchange accounts |
| **Smarkets** | **[M] partially yes** | not exposed publicly | **[C]** 2% flat commission | **closed** -- UK/EU licensed, US not served |
| **PredictIt** | **[M] yes, L1 only** | **[M] no depth, no tape** | **[C]** 10% of profits + 5% of withdrawals | **closed** -- US residents, identity verification |
| **Metaculus** | **[M] no -- HTTP 403** | with free token | not a market | n/a -- no money either way |
| **ForecastEx / IBKR** | **[C] via TWS API, account required** | account required | **[C]** $0.01/contract in the spread | **[C] possibly open** -- see 2.4 |
| **crypto perps/options** | **[C] yes, all major venues** | yes | varies | **closed for US persons** on Hyperliquid, Deribit, Binance |

### 2.1 The finding that reframes the whole question

**[M] Kalshi's market-data REST API requires no account at all.** Tested unauthenticated from the
owner's machine, 2026-08-27:

```
GET /trade-api/v2/markets?limit=1                       -> HTTP 200
GET /trade-api/v2/events?limit=1                        -> HTTP 200
GET /trade-api/v2/series?limit=1                        -> HTTP 200  (16.3 MB, the full catalogue)
GET /trade-api/v2/markets/trades?limit=5                -> HTTP 200  (the public tape)
GET /trade-api/v2/markets/{ticker}/orderbook?depth=5    -> HTTP 200  (full L2, both sides)
GET /trade-api/v2/series/{s}/markets/{t}/candlesticks   -> HTTP 200  (hourly OHLC + OI + volume)
GET /trade-api/v2/portfolio/balance                     -> HTTP 401
```

Verified on real books, not just empty ones: `KXFEDDECISION-28JAN-H26` returned five YES levels and
five NO levels; `KXHIGHCHI-26AUG28-T87` returned a two-sided book. **This means every measurement in
files 09 through 14 -- calibration, the Dutch-book scan, the ladder tests, the 429,335-trade markout
that produced 1.94c -- can be reproduced and extended indefinitely with no account and no SSN.**

**[M] Note also that the docstring on `venues/kalshi/client.py:346` is stale.** It says the orderbook
endpoint *"Requires auth."* It does not. Not fixing it here, per the terms of this task, but it is
the sort of belief that would wrongly rule out an option.

**[M] The one thing an account does buy is the WebSocket.** Both hosts reject an unauthenticated
handshake:

```
wss://external-api-ws.kalshi.com/trade-api/ws/v2   -> HTTP 401
wss://api.elections.kalshi.com/trade-api/ws/v2     -> HTTP 401
```

**[I] So the cost of having no Kalshi account is: REST polling instead of streaming.** That degrades
latency-sensitive work (the 3.6-second arbitrage windows in file 09 section 1.2 are unreachable by
polling) and it degrades queue-position modelling. It does not touch calibration, settlement
statistics, candle-based backtests, or trade-tape markouts -- which is where every result this
project has actually produced came from.

### 2.2 Polymarket international -- the best data, permanently unreachable for trading

**[M] Read access is complete and free, and better than Kalshi's.** All tested unauthenticated:

```
GET  gamma-api.polymarket.com/markets?...            -> HTTP 200  (metadata, prices, negRisk, ticks, rewards)
GET  clob.polymarket.com/book?token_id=...           -> HTTP 200  (full L2)
GET  clob.polymarket.com/prices-history?market=...   -> HTTP 200  (per-second price series)
GET  data-api.polymarket.com/trades?limit=5          -> HTTP 200  (tape)
WSS  ws-subscriptions-clob.polymarket.com/ws/market  -> CONNECTED (book snapshot + price_change deltas)
GET  clob.polymarket.com/trades                      -> HTTP 401  (your own fills only)
```

**[M] The public WebSocket is the shape `venues/kalshi/ws.py` already consumes.** First message is a
full book snapshot with `bids`/`asks` arrays; second is a delta with `price_changes` and a
`best_bid`. `Book.apply_snapshot` / `Book.apply_delta` map onto it directly.

**[M] The trade tape carries a `proxyWallet` field -- the counterparty's address on every print.**
Kalshi's tape is anonymous. **[I] This is a materially richer research substrate: informed-trader
identification, per-wallet markouts and flow-toxicity clustering are all possible on Polymarket and
are not possible on Kalshi.**

**[M] And trading is closed.** `GET https://polymarket.com/api/geoblock` from the owner's machine
returns `{"blocked":true,"ip":"...","country":"US","region":"GA"}`. **[C]** The United States is on
the close-only list on both the frontend and the API
([docs.polymarket.com/api-reference/geoblock](https://docs.polymarket.com/api-reference/geoblock)),
and the Terms state the services are not available to persons who reside in or are located in the
United States. No amount of code changes this and no circumvention is considered.

### 2.3 Polymarket US -- the API itself is behind KYC

**[M]** `GET https://api.polymarket.us/` returns HTTP 401. **[C]** The quickstart is explicit:
*"You'll be asked to verify your identity before you can trade or access the API"*, and API keys are
issued only from the developer portal after in-app approval
([docs.polymarket.us/getting-started/quickstart](https://docs.polymarket.us/getting-started/quickstart)).
**[C]** KYC is mandatory before deposit or trade and follows US federal identity/AML standards
([docs.polymarket.us/learn/get-started/signup](https://docs.polymarket.us/learn/get-started/signup)).
Secondary sources state SSN specifically
([copytradeinsider](https://www.copytradeinsider.com/blog/polymarket-kyc-requirements/)).

**[I] Polymarket US is worse than Kalshi for this owner, not better**: Kalshi at least gives free
read access, and Polymarket US gives none. File 11 already established it is also the smaller book
($1.3B/month vs $9B international vs $9.8B Kalshi).

### 2.4 ForecastEx / IBKR -- the one venue whose access question is genuinely open

**[C]** ForecastEx is a CFTC-regulated DCM and DCO operated by Interactive Brokers, trading through
the standard TWS/Web API with security type `OPT` and exchange `FORECASTX`
([IBKR Campus](https://www.interactivebrokers.com/campus/ibkr-api-page/event-contracts/)). **[C]**
The fee is *"one cent per contract"*, inclusive, embedded in a $1.01 YES+NO pairing rather than
charged separately, and collateral earns an incentive coupon around 3.1-3.8% APY
([marketmath.io/platforms/forecastex](https://marketmath.io/platforms/forecastex),
[pm.wiki](https://pm.wiki/learn/forecastex-prediction-market)). **[C]** Event contracts are available
to eligible clients of IBKR LLC, IBKR Canada, IBKR Hong Kong, IBKR Ireland and IBKR Singapore, with
availability varying by the client's country of residence
([IBKR Prediction Markets](https://www.interactivebrokers.com/predictionmarkets/en/home.php)).

**[I] IBKR onboards non-US persons routinely and does not universally require an SSN -- a non-US
person normally onboards on a W-8BEN with a foreign tax identifier.** Whether a US-resident F-1
student specifically qualifies, and under which IBKR entity, is a question for IBKR, not for me, and
this is not financial or immigration advice. **It is the single cheapest thing on this entire list to
check, and it is the only realistic path to any funded venue that surfaced in this research.** One
support enquiry.

**[C] A parallel and equally cheap check: whether Kalshi accepts an ITIN in place of an SSN.**
Secondary sources say yes -- *"an equivalent US tax ID (ITIN) is acceptable"*
([predictionmarkets101](https://predictionmarkets101.com/how-to-sign-up-for-kalshi)). **[M] Kalshi's
own help page does not enumerate the acceptable taxpayer identifiers at all**
([help.kalshi.com signing up as an individual](https://help.kalshi.com/account/signing-up/signing-up-as-an-individual),
fetched this session), so this is unconfirmed and must not be planned around. An ITIN is the
legitimate identifier for a person who is ineligible for an SSN, so asking Kalshi support directly
involves no misrepresentation of anything. If the answer is yes, this entire file is moot.

### 2.5 The remainder, briefly

- **Betfair.** **[C]** A KYC-verified Betfair account is required before any application key is
  issued, the delayed key included; a live key costs a further GBP 499 one-off
  ([Betfair Developer Program](https://support.developer.betfair.com/hc/en-us/articles/115003864531-Are-there-any-costs-associated-with-API-access)).
  **[C]** Betfair does not serve US residents outside a horse-racing product in DE/NJ/NV. **Closed
  for read and for trade.** Move on.
- **Smarkets.** **[M]** `GET api.smarkets.com/v3/events/` and `/v3/events/{id}/contracts/` return
  HTTP 200 unauthenticated; `/v3/accounts/` returns 401. So partial read works. **[C]** Commission
  is 2% flat and API access is granted by application; Smarkets is UK/Malta licensed and does not
  serve the US. Readable, not tradeable.
- **PredictIt.** **[M]** `GET www.predictit.org/api/marketdata/all/` returns HTTP 200, 410 KB, **187
  markets and 590 contracts, best buy/sell only -- no depth, no trade tape, no timestamps.** **[C]**
  Aristotle received CFTC approval in September 2025 to operate a licensed exchange and clearing
  house, resolving the 2022 shutdown attempt, with the per-position cap raised to $3,500
  ([Yogonet](https://www.yogonet.com/international/news/2025/09/09/115247-predictit-gains-regulatory-approval-to-operate-as-a-licensed-derivatives-exchange)).
  **[C]** Fees are 10% of profits plus 5% of withdrawals. **[I] That fee form is not
  `theta*p*(1-p)`; it is a profit tax, which makes `edge()` asymmetric and path-dependent and is a
  genuine rewrite of the pricing core rather than a table change.** Combined with L1-only data, 590
  contracts and US-resident identity verification, this is the worst option on the list.
- **Metaculus.** **[M]** `GET www.metaculus.com/api/posts/?limit=1` and `/api2/questions/` both
  return HTTP 403 with a browser user agent. **[C]** The API requires a token for every endpoint; the
  token is free and needs only an email account, no financial KYC. It is a forecasting track-record
  platform, not a market -- no prices, no book, no money. Useful as a question source and as a
  calibration benchmark, useless as a venue.
- **Crypto perps and options.** **[C]** Read APIs are free and keyless nearly everywhere
  (Hyperliquid's Info API needs no key at all). **[C]** Hyperliquid blocks US IPs at the frontend;
  Deribit blocks US persons and now requires verification before any trade; Binance.US requires
  completed KYC before an API key is issued. **[I] More decisively: a perpetual future is not a
  binary contract.** `core/math/contracts.py` is built on `Var(p_T | F_t) = p(1-p)` with no time
  dependence, which is a theorem about a martingale converging to {0,1}. It is false for a perp.
  The fee model, the sizing, the MECE machinery, the settlement recorder and the rulebook layer all
  become meaningless. **This is not a port; it is a different project that reuses the database
  helpers.**

---

## 3. Is there an inefficiency left anywhere reachable?

### 3.1 Polymarket is *tighter* than Kalshi, not looser -- measured, on matched volume buckets

The thesis worth testing was "a thinner or newer venue may not have professional market makers". I
measured it rather than assuming it.

**Kalshi [M]:** latest snapshot per ticker from `data/pm.db` (opened read-only), `status='active'`,
requiring a genuine two-sided quote (`yes_bid >= 1`, `yes_ask <= 99`, `ask > bid`). 24h notional
computed as `volume_24h * mid`. n = 78,598 markets.

**Polymarket [M]:** live Gamma API pull this session, `closed=false&active=true`,
`enableOrderBook=true`, top 600 by `volume24hr` plus a separate ascending-volume sample of 1,546 for
the tail.

| bucket | Kalshi median spread | Polymarket median spread |
|---|---:|---:|
| all two-sided (Kalshi n=78,598) | **7.0c** (p75 12c, p90 63c, 16.9% at 1c) | -- |
| bottom tail, 24h volume < $100 (PM n=1,546) | -- | **2.0c** (p75 13c, p90 78c, 77% two-sided) |
| >= $10k 24h notional | **1.0c** (n=561, 73.6% at 1c) | **0.30c** (n=568, 86.6% at or under 1c) |
| >= $100k 24h notional | **1.0c** (n=90) | **0.10c** (n=61, 91.8% at or under 1c) |

**[M] In the liquid bucket Polymarket's spread is one third of Kalshi's, and in the very liquid
bucket one tenth.** Part of that is mechanical -- Kalshi's tick is 1c and cannot go lower, while 392
of the 600 Polymarket markets quote on a 0.001 tick. But the economic consequence is not mechanical.

**[I] For a market maker the tighter tick is a smaller prize, not a bigger opportunity.** The maker's
gross capture is the half spread. On Kalshi that is 0.50c in the liquid bucket; on Polymarket it is
0.15c, and 0.05c in the very liquid bucket. Our measured Kalshi adverse selection is 1.94c against a
1.43c half spread, i.e. **-0.51c per contract before fees**. For Polymarket's liquid book to be
profitable to make, adverse selection would have to be below **0.15c -- a factor of 9.5 tighter than
what we measured on Kalshi.** Both venues charge makers zero. There is no fee arbitrage to recover
the difference.

**[M] The tails are similar, and both are useless.** Polymarket's sub-$100 bucket has a median 2.0c
spread with a p90 of 78c; Kalshi's full two-sided population has a median of 7.0c with a p90 of 63c.
**[I] Wide quotes on markets nobody trades are not an edge; they are the absence of a market.** File
09 measured the same thing on Kalshi -- median market notional $11.

**[M] The one genuine economic difference is exogenous: 576 of the 600 sampled Polymarket markets
(96.0%) have a liquidity-rewards configuration attached** (`rewardsMaxSpread`, `rewardsMinSize`), and
**[C]** Polymarket pays makers daily from a rewards pool plus a 15-25% share of taker fees as a maker
rebate ([liquidity-rewards](https://docs.polymarket.com/programs/liquidity-rewards),
[fees](https://docs.polymarket.com/trading/fees)). **[I] That is real subsidy income of a kind
Kalshi's open tier does not reliably offer a small account (file 09 sections 3.2-3.3), and it is the
only argument that Polymarket market making could work where Kalshi's does not. It requires an
account. The account is geoblocked. It is therefore not available.**

### 3.2 What the literature actually measured, and it agrees

**[C] Cheng, Yang and Zou (April 2026)** reconstructed **75 million limit-order-book snapshots across
173 Polymarket NBA games**. Single-market anomalies: **7 executable in-game episodes, median duration
3.6 seconds.** Combinatorial: **290 episodes, median return 101 bps**, and *"76.9% of combinatorial
opportunities constrained to an average executable size of just 14.8 shares"*, with the theoretical
middle jackpot never empirically realised ([arXiv:2605.00864](https://arxiv.org/abs/2605.00864)).
Their conclusion: executable mispricings exist but are *"structurally bounded by liquidity, confining
risk-free extraction strictly to the retail scale."*

**[I] This is the same result as file 09 section 1.2, on a different venue, at a different frequency,
by different authors.** In-play, seconds long, single-digit-dollar depth. Our own Kalshi finding was
$44.08 of theoretical maximum over 9 hours, $42.25 of it a single WNBA observation. Two venues, one
answer. **[I] The interesting inference is that the residual inefficiency in prediction markets is
not a property of Kalshi's maturity -- it is a property of the instrument.** Binaries settle at 0 or
1, which makes deviations mechanically detectable, which makes them competed away by whoever is
fastest, which caps the residual at whatever the fastest participant leaves on the table. Moving
venue does not move that.

The $40M extraction figure in **[C]** [arXiv:2508.03474](https://arxiv.org/abs/2508.03474) is a
lifetime, all-participants, all-capital number whose fee and depth treatment the abstract does not
state. **[I] It is not evidence that a new entrant can extract anything.**

### 3.3 negRisk closes the short-basket trade by construction

**[I] This is the most important structural finding in the file and it cuts against porting S2.**

Kalshi's MECE overround is bounded below by a fee (file 09: `7 * (1 - HHI)` cents) but the overround
itself can in principle be positive because nothing mechanically forces the legs to sum to $1.

**[C]** On Polymarket, in a neg-risk event, *any* holder can convert one NO share in any leg into one
YES share in every other leg, permissionlessly, through the adapter contract. **[I] That is an
enforced no-arbitrage bound with zero execution risk and zero legging risk -- a smart contract, not a
trader, closes the gap.** Whatever residual overround exists on a Kalshi MECE event, the equivalent
Polymarket neg-risk event has a strictly stronger mechanism preventing it, available to everyone,
instantly. **The S2 short-basket sleeve is structurally worse on Polymarket than it was on Kalshi,
and it was already dead on Kalshi.**

### 3.4 Manifold, honestly

**[M] Read access is complete, free and unauthenticated.** `GET api.manifold.markets/v0/markets`,
`/v0/bets`, `/v0/search-markets` all returned HTTP 200. **[C]** Rate limit 500 req/min/IP, write
access needs a free API key, full data dumps are published.

**[M] It is not a limit-order book.** Sampled markets report `"mechanism": "cpmm-1"` with a `pool`
object -- a constant-product AMM. **[I] This matters more than the play money does.** `execution/`
(3,075 LOC), `shadow/engine.py` (256), `backtest/fills.py` (555) and the entire queue-position and
post-only apparatus model a CLOB. Against an AMM there is no queue, no maker/taker distinction of the
kind `should_post_not_cross` reasons about, and the fill model is a bonding-curve integral rather
than a priority walk. Manifold does support limit orders on top of the AMM, but the price impact
function is the AMM's.

**[C] Money.** Mana cannot be converted to cash. The Sweepcash real-money layer ran September 2024 to
28 March 2025 and was shut down; the site has been mana-only since, with no stated plan to return
([Manifold news](https://news.manifold.markets/p/cash-prizes-are-here),
[gambling911](https://www.gambling911.com/gambling/manifold-eliminates%20sweepstakes-model)).

**[C] Calibration.** Manifold's own calibration page currently reports a **Brier score of 0.17369**
on a rolling sample of ~98k trades, sampling 2% of past trades on resolved binary questions with 15+
traders ([manifold.markets/calibration](https://manifold.markets/calibration)). **[I] For context, a
constant 0.5 forecast scores 0.25, so 0.174 is informative but not sharp** -- and it is a
trade-weighted score over a play-money population, which is not comparable to our Kalshi skill scores
of +0.21 to +0.74 computed one-observation-per-market on 4,258 settled markets.

**The honest assessment, which is more mixed than the framing suggests.**

- **What Manifold IS good for:** it is the only venue on this list where this owner can place actual
  orders, with zero capital, zero KYC and zero legal risk. If the open question is *"can this person
  forecast?"* -- distinct from *"can this engine trade?"* -- then a public, timestamped, adversarial
  track record has real value, and it is free. The parts of our stack that would exercise are the
  `Decision` record, the calibration and skill-score machinery in `core/math/stats.py`, and the
  un-acted-decision logging that makes calibration measurable without survivorship bias.
- **What Manifold is NOT good for, and this is the part usually skipped:** a play-money population is
  not the population we would be trading against. **[C] The documented profitable Manifold bots
  exploited platform mechanics -- the liquidity-subsidy bot farmed Manifold's own free-liquidity
  injection until Manifold removed it -- rather than superior forecasting**
  ([Manifold discussion](https://manifold.markets/Fion/will-somebody-explain-or-link-to-an)). **[I] A
  mana track record built by beating a play-money field, on an AMM, against opponents with no
  financial stake, transfers to a CFTC exchange with professional market makers approximately as well
  as a chess rating transfers to poker.** It measures something real, but not the thing that would
  make money.
- **[I] Verdict: worth doing as a cheap, honest calibration experiment with a pre-registered
  hypothesis. Not worth building an execution stack for, and not evidence of tradeable edge if it
  succeeds.** The 3,000+ LOC of CLOB execution machinery would sit unused.

---

## 4. What porting would actually cost

Two distinct projects, and conflating them is how this decision goes wrong.

**Project A -- point the RESEARCH engine at Polymarket data. [I] Days, not weeks.**

| module | change | why |
|---|---|---|
| `venues/polymarket/client.py` | **new, ~350 LOC** | Gamma + CLOB + Data API. Simpler than Kalshi's client: no RSA-PSS signing, no signed cursors. |
| `venues/polymarket/ws.py` | **new, ~250 LOC** | The public market feed. `Book`, `apply_snapshot`, `apply_delta`, `BookTop` in `venues/kalshi/ws.py` are reusable as written -- copy the ~400-LOC book core, replace the transport and the message parsing. |
| `core/math/contracts.py` | **12 lines** | Add a category-keyed theta table. Formula unchanged. |
| `core/models.py` | **~60 lines** | A `from_polymarket` classmethod; map `negRisk` to `mutually_exclusive`; carry `tick_size` on `Market`. |
| `core/db.py` | **0 lines** | `venue` is already a column on `market_snapshots` and `event_snapshots`. |
| `recorder/` | **~400 LOC** | New L1/tape writers. `settlements.py` needs a UMA-resolution reader instead of Kalshi settlements. |
| `backtest`, `strategy`, `monitor`, `rulebook`, `risk`, `core/math/stats.py` | **0 lines** | Already venue-agnostic. |
| **the cent grid** | **avoidable here** | Research-only ingest can store price in tenths of a cent in a separate column, or round the 0.001-tick venue to 0.01 for calibration work, where a 0.1c rounding is far below the measurement noise. |

**Project B -- port the EXECUTION engine to trade somewhere. [I] Weeks, and there is nowhere to
point it.**

| module | change |
|---|---|
| `execution/executor.py` | 108-line venue boundary rewritten; `_parse_ack` against a different order lifecycle; EIP-712 order signing replaces RSA-PSS |
| `execution/oms.py`, `fillfeed.py`, `structures.py` | fill semantics differ -- Polymarket has a non-terminal MATCHED state before on-chain confirmation, which `Fill.terminal` already anticipates but has never been exercised |
| **the cent grid** | **unavoidable here** -- 937 identifier occurrences across 52 files, 24 `100 - p` mirrors across 12 files, and 1,095 tests that assert integer cents |
| `core/config.py` | wallet key management replaces PEM file management |
| `risk/engine.py` | 2 lines (the hardcoded `"kalshi"` venue-exposure key and the `venue: str = "kalshi"` default) |

**[I] Project B's cost is dominated by the price convention, and its benefit is zero, because every
venue whose execution engine is worth writing is closed to this person.** Do not start it.

---

## 5. The honest recommendation

**The finding that should drive the decision is not about another venue. It is that Kalshi's read API
never needed an account.** The premise "we cannot fund Kalshi, therefore we should look elsewhere"
conflates trading access with research access. **[M] Every result this project has produced -- the
18-cell calibration test, the 131,872-observation arbitrage scan, the 429,335-trade markout -- was
computed from endpoints that return HTTP 200 to an anonymous request.**

Ranked by expected value net of effort:

**1. Confirm whether a funded account is actually impossible. [effort: two emails; EV: highest on the
list, by a wide margin.]** Ask Kalshi support whether an ITIN satisfies their taxpayer-identification
requirement -- secondary sources say it does, Kalshi's own help page does not say, and there is
nothing to misrepresent in asking. Ask IBKR which entity a US-resident non-US-person onboards under
for ForecastEx event contracts. **[I] If either answer is yes, the entire portability question
dissolves and the correct move is to stay on a venue this engine already speaks natively.** Two
enquiries dominate weeks of porting work on expected value and should be done before anything else in
this file.

**2. Keep the Kalshi recorder running and widen the window. [effort: ~0; EV: moderate, as option
value.]** **[M]** The corpus is now 3,514,353 snapshots over 26.3 hours, 2,773,285 trades over 16.0
hours, 1,388,378 candles over 4 days, 140,669 distinct tickers. That is still one late-August stretch
with no FOMC, no CPI, no NFL Sunday. **[M] No account is needed to extend it.** This was file 09's
recommendation 3 and it is still correct.

**3. Add a Polymarket READ adapter -- as a research instrument, not a trading target. [effort: days;
EV: moderate and specific.]** Justified by three things it can do that Kalshi cannot, all measured
above: a **public unauthenticated L2 WebSocket** where Kalshi returns 401; a **trade tape carrying
counterparty wallet ids** where Kalshi's is anonymous; and a **second independent venue** on which to
replicate our own calibration and adverse-selection results. **[I] The third is the real prize.** Our
central negative result -- markets are calibrated, arbitrage is absent, adverse selection exceeds the
half spread -- currently rests on one venue and one window. Reproducing it on a venue with a
different fee table, a different tick, a different settlement oracle and a different trader population
is the strongest available test of whether we measured the world or measured Kalshi. The engine is
already 56% venue-agnostic, so this is a genuinely cheap experiment.

**4. Run a Manifold calibration experiment with a pre-registered hypothesis. [effort: 1-2 days; EV:
low but non-negative, and it is the only place orders can actually be placed.]** Zero capital, zero
KYC, no legal exposure. Log every `Decision` including un-acted ones, score against resolution, and
state in advance what result would count as evidence of forecasting skill. **[I] Be explicit in
advance that a good result does not transfer to a real venue** -- an AMM, play money and an amateur
field are three separate reasons why. Do not build execution infrastructure for it.

**Explicitly not recommended.** Porting the execution engine anywhere (Project B: weeks of work, cent
grid across 52 files, and no reachable venue at the end). Polymarket US (KYC gates the API itself, so
it is strictly worse than Kalshi for this owner). PredictIt (L1-only data, 590 contracts, a profit-tax
fee model that is a rewrite of `edge()`, and US identity verification anyway). Betfair and Smarkets
(no US access, and Betfair's API needs a KYC'd account before it will return anything). Any crypto
perp or options venue (US-blocked, and `Var = p(1-p)` is a theorem about binaries that is simply false
for a perp -- that is a different project wearing this one's clothes).

**The bottom line.** **[I] The engine is more portable than it looks -- 56% of it moves unchanged, the
fee model needs 12 lines because Polymarket independently chose the same `theta * p * (1-p)` form, and
the YES-referencing boundary is 108 lines exactly where the design said it would be. The porting cost
is not the obstacle. The obstacle is that there is no venue on the other side.** Every venue with a
comparable book is closed to this owner for trading, and the one venue that is open has no money in
it. Meanwhile the venue we are already pointed at gives away its data for free to anyone who asks.

**So: stay where you are, and spend the effort on the two enquiries in recommendation 1, on widening
the window, and on the Polymarket read adapter as a replication instrument.** The most valuable thing
this project can produce right now is not a position -- it is a second, independent confirmation that
the negative result is a fact about prediction markets rather than a fact about Kalshi. That
confirmation costs days, needs no account anywhere, and is worth more than any of the ports.

---

## Sources

**Academic**
- Cheng, Yang, Zou (Apr 2026), *Arbitrage Analysis in Polymarket NBA Markets* -- [arXiv:2605.00864](https://arxiv.org/abs/2605.00864)
- Saguillo et al. (Aug 2025), *Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets* -- [arXiv:2508.03474](https://arxiv.org/abs/2508.03474)

**Polymarket primary** (all fetched 2026-08-27)
- [Fees, with the category theta table](https://docs.polymarket.com/trading/fees)
- [Liquidity Rewards](https://docs.polymarket.com/programs/liquidity-rewards)
- [Negative Risk Markets](https://docs.polymarket.com/concepts/negative-risk)
- [Geographic Restrictions, incl. the US close-only listing](https://docs.polymarket.com/api-reference/geoblock)
- [Market Details](https://docs.polymarket.com/market-data/market-details)
- Polymarket US: [Quickstart -- API access requires completed KYC](https://docs.polymarket.us/getting-started/quickstart) | [Create an Account](https://docs.polymarket.us/learn/get-started/signup) | [Fee Schedule](https://docs.polymarket.us/fees)

**Other venue primary**
- [Manifold API docs](https://docs.manifold.markets/api) | [Platform calibration page](https://manifold.markets/calibration) | [Cash prizes announcement](https://news.manifold.markets/p/cash-prizes-are-here)
- [Kalshi -- signing up as an individual](https://help.kalshi.com/account/signing-up/signing-up-as-an-individual) (does not enumerate acceptable taxpayer identifiers)
- [Betfair Developer Program -- API costs and account requirement](https://support.developer.betfair.com/hc/en-us/articles/115003864531-Are-there-any-costs-associated-with-API-access)
- [Smarkets API access and T&Cs](https://help.smarkets.com/hc/en-gb/articles/34697834941085-Smarkets-API-Access-Integration-T-Cs)
- [IBKR -- Event Contracts in the Web API](https://www.interactivebrokers.com/campus/ibkr-api-page/event-contracts/) | [IBKR Prediction Markets](https://www.interactivebrokers.com/predictionmarkets/en/home.php) | [ForecastEx rulebook](https://forecastex-public-data.s3.amazonaws.com/regulatory/ForecastEx_LLC_Rulebook.pdf)

**Secondary**
- [Yogonet -- PredictIt/Aristotle CFTC approval, Sept 2025](https://www.yogonet.com/international/news/2025/09/09/115247-predictit-gains-regulatory-approval-to-operate-as-a-licensed-derivatives-exchange)
- [marketmath.io -- ForecastEx fees](https://marketmath.io/platforms/forecastex) | [pm.wiki -- ForecastEx](https://pm.wiki/learn/forecastex-prediction-market)
- [predictionmarkets101 -- Kalshi signup, ITIN claim (UNCONFIRMED against Kalshi's own docs)](https://predictionmarkets101.com/how-to-sign-up-for-kalshi)
- [copytradeinsider -- Polymarket US KYC/SSN](https://www.copytradeinsider.com/blog/polymarket-kyc-requirements/)
- [Datawallet -- Hyperliquid restricted countries](https://www.datawallet.com/crypto/hyperliquid-supported-and-restricted-countries) | [Deribit restricted jurisdictions](https://support.deribit.com/hc/en-us/articles/25944487427741-Restricted-Jurisdictions)
- [gambling911 -- Manifold eliminates sweepstakes](https://www.gambling911.com/gambling/manifold-eliminates%20sweepstakes-model)

**Own repository and data** (read-only; `data/pm.db` opened as `file:...?mode=ro`)
- `strategy/base.py`, `core/models.py`, `core/math/contracts.py`, `execution/executor.py`, `risk/engine.py`, `venues/kalshi/{client,ws,auth}.py`, `core/config.py`, `core/db.py`
- `README.md` for the parent measurements quoted here (1.94c adverse selection vs 1.43c half spread on 429,335 trades; 131,872 synchronized observations; 18 category-lead calibration cells)
- `research/09-edge-reality-check.md`, `research/11-cross-venue.md`
