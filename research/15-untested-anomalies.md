# 15 -- Untested Anomalies: What the Literature Documents That We Have Not Measured

Written 2026-08-27, after seven strategies were closed on numbers (README, `research/09`--`research/14`).
Purpose: inventory the documented market anomalies we have **not** tested, say which are reachable
with the data already on disk, and rank them by information per unit of effort. The target is the
cheapest test that could still overturn "this venue is efficient".

Every claim is tagged:

- **[M]** MEASURED -- computed by me in this session from `data/pm.db`, opened read-only via
  `file:data/pm.db?mode=ro`. Query described inline. Scratch scripts live in the session scratchpad,
  not in the repo.
- **[C]** CITED -- someone else's published number, with link.
- **[I]** INFERRED -- my reasoning on top of [M] or [C]. Weakest tier. Treated as such.

**Scope boundary.** I do not re-derive anything in the closed list: within-event baskets, ladder
arbitrage, cross-market hedging, cross-venue, weather, passive market making, latency, or the
calibration sweep. Where a candidate here overlaps a closed result I say so and subtract the overlap.

---

## 0. Three caveats that govern every number below

**[M] 0.1 -- The corpus, restated.**

```
market_snapshots : 3,404,590 rows | 138,193 tickers | 2026-08-26 19:49 -> 2026-08-27 21:35  (25.8 h)
trades           : 2,773,285 rows | 139,260 tickers | 2026-08-27 01:13 -> 2026-08-27 17:13  (16.0 h)
candles (1-min)  : 1,388,378 rows |   4,258 tickers | 2026-08-23 22:05 -> 2026-08-27 20:47  ( 4.9 d)
settlements      :     4,277 rows |   4,277 tickers | 1,944 YES / 2,333 NO / 0 voided
event_snapshots  :    89,818 rows |  16,148 events
```

The settled universe is **two days wide** and its composition is not representative: the largest
settled series are `KXNASDAQ100U` (520 tickers), `KXINXU` (148), `KXMLBKS` (122), `KXBTCD` (112),
`KXDJI` (104) -- i.e. hourly index and daily crypto markets. Only **388** settled tickers are
in-play sports (`%GAME%` / `%MATCH%` series), and only **1,406** settled tickers carry any trade at
all (463,012 trades, 66,323,739 contracts). Every outcome-linked result below is a two-day result.

**[M] 0.2 -- `close_at_us` is not a time-to-resolution clock, and this breaks several published test
designs.** For the 179 settled sports tickers with trades, the gap from the market's *last observed
trade* to its `close_at_us` is p10 **2,791 min**, median **4,115 min** (2.9 days), p90 **20,120 min**.
**100%** are more than six hours. Kalshi's published close time for a sports market is an
administrative deadline, not the end of the contest. Consequently:

- Conditioning the outcome tests on `close_at - traded_at` does almost nothing. **[M]** Requiring
  `>= 24 h` to nominal close retains **18,376,573 of 19,534,895** contract-purchases in the 1-10c
  band -- 94% of the sample survives a filter that was supposed to remove short-dated trades.
- Any test whose published form is "the last N minutes before the outcome is known" -- the Yogi
  Berra bias, in-play news reaction, endgame calibration -- has **no ex-ante clock in our schema.**
  Section 2.3 says what would fix it.

**[M] 0.3 -- The independence unit is the event, and it bites hard here.** 463,012 trades sit on
1,406 settled tickers, which sit on far fewer economic events (520 `KXNASDAQ100U` tickers are
hourly repeats of one underlying). The project has already been burned three times on this
(README: 1,327 sports markets spanning 226 games took z from +2.47 to +1.46). **Every number in
section 3 is an unclustered point estimate produced to decide whether a test is worth running.
None of them is a finding.** They are feasibility probes.

---

## 1. The anomaly inventory

Column "PM?" answers: has this been demonstrated in a *real-money prediction market* (not a
sportsbook, not equities)?

| # | Anomaly | PM? | Best citation | Tested by us? |
|---|---|---|---|---|
| A1 | Favourite-longshot bias, contract-level ROI by price band | **Yes, on Kalshi** | Buergi, Deng & Whelan (2026) | **No** -- see 1.1 |
| A2 | FLB split by maker vs taker | **Yes, on Kalshi** | Buergi, Deng & Whelan (2026) | No |
| A3 | FLB conditioned on time-to-expiry / long-dated discounting | Yes, InTrade | Page & Clemen (2013) via Whelan sec 2.1 | No |
| A4 | Reverse FLB (favourite bias) in-play | Yes, Betfair exchange | Angelini, De Angelis & Singleton (IJF 2022) | Partly (weather only) |
| A5 | Yogi Berra bias -- trailing side overpriced in the endgame | Yes, InTrade | Page (2012), Applied Economics 44(1) | No |
| A6 | Partition dependence / event-splitting (bias toward 1/N) | **Yes, field PMs** | Sonnemann, Camerer, Fox & Langer, PNAS 110(29) 2013 | Indirectly -- see 5.1 |
| A7 | Longshot bias as a *context* effect (set-dependent) | Lab + racetrack | Meyer & Hundtofte, Mgmt Sci (2023) | No |
| A8 | Delayed overreaction: open-to-close drift reversed by outcome | Sportsbook only | Moskowitz, J. Finance (2021) | No |
| A9 | Over/underreaction to news, graded by surprise | Yes, Betfair | Croxson & Reade (EJ 2014); Angelini et al. (2022) | Partly -- see 1.4 |
| A10 | Round-number price clustering, and reversal at round levels | Equities/FX only | Osler (2005); Baig et al. (JFR 2025) | No |
| A11 | Disposition effect (hold losers, sell winners) | Not in PMs | Odean (1998); Polymarket work is adjacent | No -- not testable |
| A12 | Attention / sentiment effects on price | **No PM evidence found** | -- | No -- not testable |
| A13 | Herding after news | Not isolated in PMs | -- | No -- not testable |
| A14 | Day-of-week / calendar effects | Rejected in betting odds | Ann. Oper. Res. (2022), EPL odds | No -- not testable |
| A15 | End-of-day / last-race longshot effect | Racetrack only | McGlothlin (1956); Ali (1977) | No -- not testable |
| A16 | Anchoring on prior prices in recurring markets | Not in PMs | -- | No -- weakly testable |
| A17 | Liquidity provision as underwriting, not spread capture | **Yes, on Kalshi NFL** | Palumbo (2026), SSRN 6325658 | Yes -- `research/14` |
| A18 | Profit concentration / order-type sorting | Yes, Polymarket | Akey, Gregoire, Harvie & Martineau (2026) | No -- not testable |

### 1.1 The one that matters: A1/A2 are not closed, they were never powered

The README records "Favourite longshot bias | not present | P(zero losses given calibration) =
0.638." Reading the source, that verdict came from a *longshot-selling backtest whose loss branch
had not yet occurred*: the binomial test correctly said that observing zero losses is consistent
with perfect calibration. That is a valid rebuttal of a spurious positive. **It is not a test of the
favourite-longshot bias.** It is a statement that one small sample had no power.

**[C]** The published Kalshi result is much sharper. Buergi, Deng & Whelan (2026), 46,282 contracts
from 12,403 events, Kalshi inception 2021 through April 2025, 313,972 contract-price observations:
contracts costing 10c or less lose **over 60%**; contracts above 50c earn small positive returns
significant above 70c; average pre-fee return **-20%**; Mincer-Zarnowitz F-test rejects unbiasedness
in **every** subsample -- all four contract types, all five volume quintiles, all five transaction-size
quintiles, all seven categories, all five calendar years. Makers average **-9.64%**, takers
**-31.46%**. Their sample deliberately stops at April 2025 because Kalshi began charging maker fees
after that.

**[I] That last sentence is the opening.** Our tape is **August 2026**, sixteen months later, under
the maker-fee regime their sample excludes, and it carries the maker/taker label on every trade.
Section 3.1 shows the test runs in minutes and reproduces their headline magnitude.

### 1.2 A3: FLB conditioned on horizon

**[C]** Page & Clemen (2013) found InTrade longshots overpriced only for contracts traded more than
ten days out, and attributed it to discounting of the deferred payout; near close the deviation
vanished. Whelan et al. find the opposite shape on Kalshi -- their largest coefficient is on the
day-of-close price (psi = 0.036, se 0.001, n = 46,282) and the bias persists at every daily horizon
out to ten days. **[I]** These two are reconcilable only if something other than discounting drives
the Kalshi version. The obvious candidate is the **1c/99c price bound**: a contract whose true
probability is 0.2% cannot trade below 1c, so it is guaranteed to show a negative return, and that
guarantee is strongest exactly when the outcome is nearly determined. Nobody appears to have
separated the two. Section 3.1 shows we can.

### 1.3 A5/A7: the two designs the literature makes obvious and our schema quietly refuses

**[C]** Page (2012) found that on InTrade, prices for the losing team in the final 15 minutes were
too high relative to how often those teams won -- the market overestimates comebacks.
**[C]** Meyer & Hundtofte (2023) argue the longshot bias is a *contrast* effect: it appears when
gambles are compared side by side on the payoff dimension and **disappears when bets are considered
in isolation**. Kalshi displays multi-leg events as a list of legs and standalone binaries alone,
which is close to a natural experiment on their mechanism.

A5 needs a game clock we do not have (0.2). A7 needs enough settled standalone binaries; **[M]** we
have **7**. Section 2 treats both.

### 1.4 A9: what our mean-reversion result already covers, and what it does not

`research/10` established **[M]** that the quote process mean-reverts (t = -21, survives the
bid-ask-bounce control) but that the spread is 4-8x the move in every size band. That is an
unconditional statement about all quote moves. **[C]** The literature's claim is conditional and
graded: Croxson & Reade (EJ 2014) find prices update "swiftly and fully" to goals scored on the cusp
of half-time -- i.e. **no drift after clean news**; Angelini, De Angelis & Singleton (IJF 2022) find
in-play mispricing that *increases with surprise*, notably when a longshot scores late. The untested
increment is the **conditional-on-jump** version, not the unconditional one.

---

## 2. Testability against our schema

### 2.1 Testable now, with the exact fields

| # | Test | Tables / fields | Blocking gap |
|---|---|---|---|
| A1 | ROI by price band, side-neutral | `trades.yes_price_cents, size, ticker` JOIN `settlements.outcome, voided` | none |
| A2 | Same, split maker vs taker | + `trades.taker_side` | none |
| A3' | ROI by band x *market lifetime* (not `close_at`) | + `candles` span per ticker | horizon proxy is weak (0.2) |
| A6/A7 | ROI by band x event structure | + `event_snapshots.mutually_exclusive`, leg count from `market_snapshots` | only 7 settled standalone binaries |
| A8 | Open-to-close drift vs settlement error | `candles.yes_bid_close/yes_ask_close, end_period_ts` + `settlements.outcome` | none -- 3,993 settled tickers have >= 30 candles |
| A9' | Drift after a large 1-minute move | `candles` only (settlement not required for the drift leg) | none |
| A10 | Price clustering; reversal at multiples of 5/10 | `trades.yes_price_cents`; `candles` for the reversal leg | none |
| -- | No-look-ahead conditional calibration | `candles` (mid, realized vol, elapsed) + `settlements` | none -- this is the honest form of A3/A4/A5 |

**[M]** Coverage for the candle-based tests: 4,258 settled tickers have candles, median **262**
one-minute bars each (p25 104, p75 462, p90 680, max 2,622); 3,993 have >= 30 bars.
`yes_bid_close` and `yes_ask_close` are non-null on all 1,388,378 rows; `price_close` (last trade in
the minute) is non-null on 1,137,513 (81.9%), so a two-sided quote series exists even in minutes
with no print.

### 2.2 Not testable, and exactly what is missing

- **A11 disposition effect** -- needs per-account positions and the sequence of a trader's own
  entries and exits. Kalshi's public tape carries no trader identifier. This is why the account-level
  literature is all Polymarket: **[C]** Akey, Gregoire, Harvie & Martineau (2026) can do it because
  Polymarket is on-chain and wallets are visible ($67bn of volume; top 1% of profitable users capture
  76.5% of profits; winners provide liquidity with limit orders, losers take it with market orders).
  **No amount of Kalshi public data substitutes.** Would need: exchange-provided anonymised account
  ids, which do not exist in any public endpoint.
- **A13 herding** -- same blocker. Herding is a statement about *who* follows *whom*. Trade-sequence
  autocorrelation is not herding; it is order splitting, and we cannot distinguish them.
- **A12 attention/sentiment** -- needs an exogenous attention series (Google Trends, social volume,
  news counts) aligned to market tickers. We have none, and I could not find a single peer-reviewed
  demonstration of an attention effect on prediction-market *prices* -- the 2026 coverage is
  journalism about volume, not price tests. **[I]** Low prior, high data cost. Skip.
- **A14 day-of-week** -- **[M]** the candle tape spans 5 calendar days and the trade tape spans one.
  There is no day-of-week test here, now or after another month of recording; it needs quarters.
  **[C]** The one betting-market test I found (Ann. Oper. Res. 2022, English Premier League, Shin
  model) reports no evidence that day-of-week is mispriced in published odds, so the prior is weak
  anyway.
- **A15 end-of-day effect** -- the racetrack construct is a *session* with a known last race.
  A 24/7 exchange has no session. A "last game of the slate" analogue exists but **[M]** our sports
  settled sample is 179 tickers over two days, which is one slate.
- **A5 Yogi Berra in published form** -- needs minutes-remaining in the contest. Not derivable:
  `close_at_us` is a median 2.9 days late (0.2), and neither `candles` nor `market_snapshots` carries
  game state. Would need an external scoreboard feed keyed to Kalshi tickers.
- **A8 in Moskowitz's published form** -- his momentum signal is the *team's* recent win record, and
  value is a team-cheapness measure. Both need season standings. Our tickers encode team codes
  (`KXMLBGAME-26AUG261945BALSTL-STL`) but not records. **[I]** The *internal* half of his design --
  does the open-to-close price move get reversed by the outcome -- needs no external data at all and
  is the version worth running (3.3).
- **A16 anchoring** -- weakly testable. Kalshi runs recurring daily/hourly series, so "does today's
  open anchor on yesterday's close after controlling for the underlying" is formulable from
  `candles`. But separating anchoring from genuine persistence in the underlying requires a model of
  the underlying (index level, crypto price) that we do not hold in the database. **[I]** Rank it low.

### 2.3 The design fix that unlocks the in-play family

**[I]** Every in-play test above (A4, A5, endgame calibration) fails for the same reason: the only
clocks available -- time to `close_at_us`, time to `settled_at_us`, time to the market's last trade
-- are either useless or **ex post**. I ran the ex-post version to see the size of the trap
(3.4); it manufactures an enormous effect that is pure look-ahead.

The fix does not require new data. Replace "time remaining" with **features knowable at time t from
the price path itself**: current mid, realized quote volatility over the trailing k minutes,
cumulative volume so far, open interest, and time *elapsed* since the market's first bar. A market
pinned at 96/97 with collapsed volatility and heavy accumulated volume *is* late-game, and a trader
at time t knows all of it. The resulting test -- "conditional on observables at t, is the mid
calibrated?" -- is tradeable, has no look-ahead, and subsumes A3, A4 and A5 in the only form that
could ever be acted on. **This, not the published form, is what should be run.**

---

## 3. Ranking by information per unit of effort

Effort is wall-clock for someone who already has the loaders. "Could it overturn efficiency?" is the
question the ranking optimises.

| Rank | Test | Effort | Could overturn? | Why |
|---|---|---|---|---|
| **1** | A1/A2 ROI by price band, maker/taker, event-clustered | hours | **Yes** | Feasibility probe already reproduces the published magnitude (3.1) |
| **2** | No-look-ahead conditional calibration on the candle tape | ~1 day | **Yes** | Only tradeable form of A3/A4/A5; produces a signal directly if it survives |
| **3** | A8' open-to-close drift reversal | hours | Yes, weakly | First over- vs under-reaction test on Kalshi that needs no external data |
| **4** | A9' post-jump drift | hours | Yes, weakly | Conditional version of a result we only have unconditionally |
| **5** | A6/A7 FLB by event structure | hours | Mostly no | Cheap; the *negative* is the valuable outcome (5.1) |
| **6** | A10 clustering + round-number reversal | hours | No | Clustering leg essentially already measured (3.5) |
| 7 | A16 anchoring in recurring series | days | No | Confounded with the underlying |
| -- | A11/A12/A13/A14/A15 | n/a | -- | Not testable (2.2) |

### 3.1 Rank 1 -- the probe, and what it shows

**[M] Method.** Join `trades` to `settlements` on ticker with `voided = 0`. Every trade creates two
contract purchases: at `yes_price_cents` on one side and at `100 - yes_price_cents` on the other.
`taker_side` assigns which is the taker's. Pool by 10c band, weight by `size`. Taker fee applied at
the band-average price as `7 * p * (1 - p)` cents. 463,012 trades, 66,323,739 contracts, 1,406
settled tickers. **No clustering, no standard errors.**

```
who     band      n_trades    contracts   avg_px  win_rate  ret_prefee  ret_post_taker_fee
taker   1-10c       51,651   10,952,707     4.30    0.0148     -65.56%          -67.72%
taker  11-20c       41,406    5,694,639    15.73    0.1024     -34.95%          -38.58%
taker  21-30c       38,669    4,832,155    25.45    0.2356      -7.45%          -12.04%
taker  71-80c       37,059    4,205,999    75.45    0.7939      +5.23%           +3.45%
taker  81-90c       39,337    4,782,830    85.18    0.9236      +8.43%           +7.32%
taker 91-100c       47,463    8,106,406    96.12    0.9939      +3.41%           +3.13%
taker    ALL       463,012   66,323,739    47.57    0.4691      -1.38%
maker   1-10c       50,901    8,582,188     4.22    0.0081     -80.79%
maker  91-100c      46,764   10,190,233    96.13    0.9865      +2.62%
maker    ALL       463,012   66,323,739    52.43    0.5309      +1.26%
```

Side-neutral pooling (both sides of every trade, by exact price): the 1-10c band holds 19,534,895
contracts at an average **4.26c** and wins **1.186%** -> **-72.2%**; the mirrored 90-99c band returns
**+3.21%**.

**[I] Read this carefully.** The shape matches Buergi/Deng/Whelan exactly at the tails
(**[C]** ">60 percent" loss on sub-10c; small significant positive above 70c), on a sample sixteen
months later and under maker fees. But two things say "do not believe it yet":

1. **The middle is noise.** Taker returns run 51-60c **-10.05%**, 61-70c **-19.40%**, 31-40c
   **+3.70%** -- non-monotone, which is what two days of correlated settlement outcomes look like.
2. **The tails may be mechanical.** **[M]** Per-cent win rates: 1c -> 0.006%, 2c -> 0.014%,
   3c -> 0.037%, 5c -> 0.129%, 7c -> 0.627%. A 3c contract winning 0.037% of the time is not a
   behavioural bias; it is the **1c price floor** meeting an hourly index market minutes from
   resolution. And **[M]** the `close_at` filter cannot remove it (0.2).

**[I] Therefore the finished test has a specific, cheap shape:** restrict to contracts whose true
resolution is genuinely uncertain at trade time, cluster by event, and report psi from the
Mincer-Zarnowitz form `Y - P = alpha + psi*P` with errors clustered at both event and contract level,
which is exactly the published specification and is directly comparable. **If psi survives with the
tick-floor region excluded, the "efficient venue" conclusion has a hole in it. If it does not, we
have shown that the published Kalshi FLB is substantially a price-bound artifact -- which is itself a
result worth writing down.** Either branch pays.

### 3.2 Rank 2 -- the no-look-ahead calibration

Described in 2.3. **[M]** Feasible on 3,993 settled tickers with >= 30 one-minute bars and
1,388,378 quote bars. Cost: one loader plus a logistic calibration with event-clustered errors.
Output is a signal, not just a p-value, which is why it outranks 3 and 4.

### 3.3 Rank 3 -- drift reversal, the internal half of Moskowitz

**[C]** Moskowitz (J. Finance 2021), 100,000+ contracts, four US leagues, three decades: price moves
from the open to the close of betting load on momentum and (weakly) value, and are **fully reversed
by the game outcome** -- overreaction, not underreaction, and not profitable after the vig.
**[I]** The internal test needs nothing external: regress `outcome - close_price` on
`close_price - open_price` across settled markets. Negative coefficient -> overreaction; positive ->
underreaction; zero -> the market is a martingale in its own price, which is the efficient null. As
far as I can find, **this has never been run on a regulated prediction market.** Fully feasible on
`candles` + `settlements`.

### 3.4 The trap I walked into, recorded so nobody repeats it

**[M]** Using "minutes before that market's last observed trade" as an in-play clock on the 179
settled sports tickers produces a spectacular Yogi Berra result:

```
window    band      contracts  win_rate  ret_prefee  ntick  top1_share
last15m   1-10c     7,063,772    0.0021      -96.2%    123        6.5%
last15m  21-30c     1,332,991    0.0553      -78.3%     60       22.0%
last15m  31-40c       716,895    0.1275      -64.1%     49       17.9%
last15m  41-50c     1,626,824    0.3780      -16.9%     43       22.5%
last15m  51-60c     1,367,434    0.6504      +17.2%     44       26.2%
last15m  61-70c       885,184    0.8923      +36.2%     55       19.6%
last15m  71-80c     1,299,733    0.9464      +25.4%     62       20.1%
15-60m   21-30c     2,918,697    0.3463      +35.8%     86        9.5%
earlier  21-30c     2,930,793    0.2665       +4.5%     71       10.3%
```

A 25c contract winning 5.5% in the final fifteen minutes, and 34.6% and 26.7% in the two earlier
windows, is not a bias -- it is **look-ahead**. Baseball and tennis have no clock, so "fifteen
minutes before the end" is only knowable after the game ends, and conditioning on it selects games
that resolved decisively. **[I]** The effect is entirely inside the ex-post window and vanishes
outside it, which is the signature. Any endgame test built on `settled_at_us`, `close_at_us`, or
last-trade time will manufacture this. Use 2.3 instead.

### 3.5 Rank 6 -- clustering is already mostly answered

**[M]** Restricting to 11-89c to avoid the boundary, mean trades per price level: multiples of 5 =
**24,539** (15 prices) versus **23,471** at non-multiples (64 prices), ratio **1.046**; multiples of
10 ratio **1.087**. So there is a ~5-9% round-number excess -- present, tiny, and an order of
magnitude below what the equity and FX literature reports. The remaining untested piece is Osler's
*reversal at round levels*, which needs the candle tape; **[I]** given a 1.05x clustering ratio I
would not expect a tradeable reversal and I rank it last of the testable set.

---

## 4. What is genuinely beyond reach without a funded account

Concretely, and without hedging:

1. **Anything about our own fills.** No `fills`, no markouts, no realised adverse selection on our
   own orders. `research/14`'s -0.51c/contract is inferred from the public tape, not from a P&L.
2. **Queue position and fill probability.** Public depth gives aggregate size at a price, never our
   place in it. `research/09`'s P(all legs fill) = 0.0000 was measured by simulation against the
   public book; it cannot be validated without resting real orders.
3. **Historical full-depth book.** We have L1 in `market_snapshots` and OHLC in `candles`. Full depth
   is available *on demand, going forward*. Any test needing historical L2 -- order-book imbalance
   predictability, Kyle's lambda, cancellation dynamics, the resting-order reconstruction Palumbo
   (2026) does on Kalshi NFL -- is unavailable for the past and expensive for the future.
4. **The production WebSocket.** The 5-15s REST grid is a CDN TTL (README). Every millisecond-scale
   question -- and `research/12` showed p90 episode life is 48ms -- is out of reach by three orders
   of magnitude.
5. **Maker-fee reality and rebate eligibility.** Which series actually charge us, what LIP/LPP pays,
   and whether the collateral-return latch traps a position are all account-scoped facts.
6. **Trader identity, at any price.** No public or funded Kalshi endpoint exposes counterparty
   identifiers. A11, A13 and A18 are permanently closed on this venue; they are Polymarket questions.

**[I]** Note what is *not* on this list: everything in section 3. Ranks 1-6 need only the settled
outcome tape and the candle tape, both already on disk. **The binding constraint on the anomaly
programme is calendar time and event count, not account status.**

---

## 5. Anomalies whose absence would itself be notable

### 5.1 Partition dependence -- the strongest candidate

**[C]** Sonnemann, Camerer, Fox & Langer (PNAS 110(29), 2013) demonstrated partition dependence in
*field* prediction markets, not just the lab: judged probabilities are biased toward 1/N, where N is
the number of bins the event space is cut into. They showed it in NBA and World Cup markets, in
economic-indicator markets run inside major financial institutions, and in nine years of horse
racing.

**[M/I]** Our ladder result bounds it. `research/09` and the closed list report **$0.00 across 62,838
nested threshold pairs** and **0 violations across 131,872 synchronized observations** on four
monotonicity constraints. If the implied distribution were being compressed toward uniform, the
compression would show up as a monotonicity or additivity violation between coarse and fine
partitions of the same underlying. It does not, to one tick. **[I] "Partition dependence on Kalshi is
smaller than one cent" is a clean negative against a PNAS field result, and we can state it today
with numbers already computed.** It needs writing up, not measuring.

### 5.2 Round-number clustering -- present but an order of magnitude too small

**[C]** Price clustering on round increments is one of the oldest and most universal findings in
market microstructure, documented since the 1930s across equities, FX and betting, and **[C]** Baig
et al. (J. Financial Research 2025) find it *causes* measurable inefficiency. **[M]** Kalshi's
11-89c range shows a clustering ratio of **1.046** at multiples of 5 and **1.087** at multiples of 10.
**[I]** A near-flat price grid on a venue whose users are overwhelmingly retail is genuinely
surprising and is consistent with the rest of what we have found: the marginal price-setter on
Kalshi behaves like an algorithm, not a person.

### 5.3 The context effect -- an early negative, and it points the wrong way

**[M]** Splitting the ROI probe by event structure, 1-10c band:

```
standalone binary        1-10c      27,862 contracts   win 0.0565    +2.8%     7 tickers
ME 2-leg                 1-10c  14,823,464 contracts   win 0.0048   -91.2%   128 tickers
ME multi (>=3 legs)      1-10c   2,694,173 contracts   win 0.0563    +2.3%   272 tickers
non-ME multi             1-10c   1,989,396 contracts   win 0.0035   -93.6%   741 tickers
```

**[C]** Meyer & Hundtofte (2023) predict the longshot bias is *created* by side-by-side comparison
and *disappears* in isolation. **[I]** Our probe points the other way: the multi-leg displays
(>= 3 legs) show no cheap-contract loss at all, while the two-leg sports moneylines and the
non-exclusive index ladders show -91% and -94%. That is a contradiction of the published mechanism,
on 272 versus 128 tickers, in two days, unclustered -- so it is a *reason to run the test*, not a
result. But it is the kind of negative that would be worth reporting if it holds.

### 5.4 What a negative is worth here

**[I]** The project's asset is that it has closed seven strategies on numbers. A well-powered
"documented anomaly X is absent on Kalshi to within one tick" is the same kind of asset, and cheaper
to produce than any new strategy. Sections 5.1 and 5.2 are already nearly finished; 5.3 is one
clustered regression away.

---

## 6. What I would actually run

**[I]** In order, and stopping the moment a branch comes back null:

1. **Finish the ROI-by-price-band test (rank 1).** Event-clustered Mincer-Zarnowitz, maker/taker
   split, with and without the 1c-3c and 97c-99c tick-floor region, restricted to markets whose
   outcome was not already determined. Hours of work. It either finds a hole in the efficiency
   conclusion or converts a published Kalshi finding into a measurement artifact. **Both outcomes are
   worth more than anything else on the list.**
2. **Write up the partition-dependence negative (5.1) from numbers already in hand.** No new
   computation.
3. **Build the no-look-ahead conditional calibration (rank 2)** only if step 1 leaves anything alive
   at the tails. It is the only in-play design that survives 3.4, and it is the only one that yields
   a tradeable signal rather than a p-value.
4. **Keep recording.** Every outcome-linked test in this file is limited by **two days of
   settlements and 179 sports tickers**, not by method and not by account status. A month of
   settlements would multiply the power of ranks 1, 3 and 5 without a line of new code.

**And the case against all of it.** **[I]** Even if rank 1 confirms the published bias, the trade it
implies is *buy favourites at 90-99c and hold to settlement*: **[M]** +3.2% pre-fee, roughly +2.9%
after the 0.27c taker fee at 96c, on capital tied up until resolution, with a payoff that loses the
full ~96c stake on **1.19%** of contracts -- about one in 84. That is a lottery ticket sold in
reverse, it has the same fat left tail
`research/09` and `research/14` already flagged, and its Sharpe cannot be estimated from two days of
settlements at all. **The honest reason to run rank 1 is that it is the cheapest remaining test that
could falsify the efficiency conclusion -- not that a strategy falls out of it.**

---

## Sources

**Prediction markets, Kalshi**
- Buergi, Deng & Whelan (Jan 2026), *Makers and Takers: The Economics of the Kalshi Prediction Market* -- [PDF](https://www.karlwhelan.com/Papers/Kalshi.pdf) | [GWU WP 2026-001](https://www2.gwu.edu/~forcpgm/2026-001.pdf) | [UCD WP2025_19](https://www.ucd.ie/economics/t4media/WP2025_19.pdf) | [CEPR column](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market)
- Palumbo (2026), *A Microstructure Perspective on Prediction Markets* -- [SSRN 6325658](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6325658) (Kalshi NFL order book; passive LPs retain outcome-dependent terminal exposure -- liquidity provision as underwriting)

**Prediction markets, other venues**
- Sonnemann, Camerer, Fox & Langer (2013), *How psychological framing affects economic market prices in the lab and field*, PNAS 110(29):11779-11784 -- [PNAS](https://pnas.org/content/110/29/11779) | [Caltech summary](https://www.caltech.edu/about/news/psychology-influences-markets-39814)
- Page (2012), *"It ain't over till it's over." Yogi Berra bias on prediction markets*, Applied Economics 44(1) -- [T&F](https://www.tandfonline.com/doi/abs/10.1080/00036846.2010.498578)
- Angelini, De Angelis & Singleton (2022), *Informational efficiency and behaviour within in-play prediction markets*, Int. J. Forecasting 38(1) -- [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0169207021000996) | [SSRN 3505287](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3505287)
- Akey, Gregoire, Harvie & Martineau (2026), *Who Wins and Who Loses in Prediction Markets? Evidence from Polymarket* -- [CEPR DP21615](https://cepr.org/publications/dp21615) | [PDF](https://www.carf.e.u-tokyo.ac.jp/wp/wp-content/uploads/2026/06/260714_polymarket.pdf)

**Betting markets**
- Moskowitz (2021), *Asset Pricing and Sports Betting*, J. Finance -- [PDF](https://spinup-000d1a-wp-offload-media.s3.amazonaws.com/faculty/wp-content/uploads/sites/3/2021/08/AssetPricingandSportsBetting_JF.pdf)
- Croxson & Reade (2014), *Information and Efficiency: Goal Arrival in Soccer Betting*, Economic Journal 124(575):62-91 -- [OUP](https://academic.oup.com/ej/article-abstract/124/575/62/5076978) | [CentAUR](https://centaur.reading.ac.uk/34884/)
- Snowberg & Wolfers (2010), *Explaining the Favorite-Long Shot Bias: Is it Risk-Love or Misperceptions?*, JPE 118(4) -- [NBER w15923](https://www.nber.org/system/files/working_papers/w15923/w15923.pdf)
- Meyer & Hundtofte (2023), *The Longshot Bias Is a Context Effect*, Management Science -- [INFORMS](https://pubsonline.informs.org/doi/10.1287/mnsc.2023.4684) | [SSRN 4130363](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4130363)
- Woodland & Woodland (1994), *Market Efficiency and the Favorite-Longshot Bias: The Baseball Betting Market*, J. Finance 49(1):269-279 -- [Wiley](https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.1994.tb04429.x) (reverse FLB; later work disputes the test statistics)
- McKenzie et al. (2016), *Are Longshots Only for Losers? A New Look at the Last Race Effect*, JBDM -- [PDF](https://pages.ucsd.edu/~mckenzie/McKenzieetal2016JBDM.pdf) | [end-of-day effect overview](https://en.wikipedia.org/wiki/End-of-the-day_betting_effect)
- Betting-market efficiency and day-of-week, Annals of Operations Research (2022) -- [Springer](https://link.springer.com/article/10.1007/s10479-022-04722-3)

**Price clustering**
- Baig et al. (2025), *Price clustering and the informational efficiency of stock prices*, J. Financial Research -- [Wiley](https://onlinelibrary.wiley.com/doi/10.1111/jfir.70020)
- Osler (2005) on round-number reversal, via [Tick Size Reduction and Price Clustering in a FX Order Book](https://arxiv.org/pdf/1307.5440)

**Our own data**: `data/pm.db`, read-only (`file:data/pm.db?mode=ro`). Quotes 2026-08-26 19:49 ->
2026-08-27 21:35 UTC; trades 2026-08-27 01:13 -> 17:13 UTC; candles 2026-08-23 22:05 ->
2026-08-27 20:47 UTC; 4,277 settlements.
