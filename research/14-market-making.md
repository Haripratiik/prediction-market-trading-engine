# 14 -- Market Making on Kalshi: Does Retail Flow Pay for Liquidity?

Written 2026-08-27. Purpose: test the thesis that **Kalshi's counterparty mix is mostly retail
sports/event gamblers rather than professional market makers, and that providing liquidity there is
therefore structurally more attractive than in equities or options.** This is the strongest
remaining candidate after deterministic arbitrage was refuted in `research/09-edge-reality-check.md`.

Every claim is tagged:

- **[M]** MEASURED -- computed by me in this session from `data/pm.db` (opened read-only via
  `file:data/pm.db?mode=ro`) or from Kalshi's public REST API. Method described inline.
- **[C]** CITED -- someone else's published number, with link.
- **[I]** INFERRED -- my reasoning on top of [M] or [C]. Weakest tier. Treated as such.

**Scope boundary.** Whether Kalshi's taker flow is *informed* -- the adverse-selection question --
is being measured separately from the labelled trade tape. I do not duplicate it. I treat adverse
selection as a free parameter `AS` in cents per contract per fill and report every economic
conclusion at **AS = 0.0c, 0.5c, 1.0c and 2.0c**. Section 3.4 gives the breakeven `AS` for each
market class, which is the only number that matters once the measurement lands.

---

## 0. The caveats that govern every number in this file

**[M] The corpus is 20.4 hours of quotes and 15.2 hours of trades, not months.**

```
market_snapshots : 2,338,702 rows | 118,759 distinct tickers | 20.36 h
trades           : 2,420,941 rows | 338,030,125 contracts    | 15.16 h
series_cache     :    13,545 series
settlements      :       128 rows
```

Same objection as `research/09` section 0: this is one Wednesday-into-Thursday in North America. It
contains an MLB slate, a La Liga match, lower-tier tennis, CS2 esports and a weather day. It
contains no FOMC, no CPI, no NFL Sunday, no election, no exchange outage.

**[M] Two structural warnings about the data that changed my analysis:**

1. **`volume_24h` is unreliable; the trade tape is not.** Per ticker, `sum(trade size) / volume_24h`
   has median 0.30 (sane -- 15h of trades against a 24h field) but a long tail to **868x**. The cause
   is that `volume_24h` is a rolling field read at snapshot time; it decays or resets for markets
   that have finished trading, while the trade tape retains the prints. I verified the tape directly
   against the live API: for `KXMLBGAME-26AUG271305COLWSH-WSH`, summing `count_fp` over all 2,171
   trades from `/markets/trades` gives **1,424,378** against a reported `volume_fp` of **1,423,391**
   -- a 0.07% match. **All flow numbers below come from the trade tape.** The brief's "total 24h
   volume 62.1M contracts" is a `volume_24h` sum and understates true flow by close to an order of
   magnitude.

2. **Sizes are genuine contract counts, and they are fractional.** `yes_bid_size_fp`,
   `open_interest_fp` and trade `count_fp` are fixed-point strings Kalshi returns with two decimals;
   15.1% of resting sizes are non-integer. They are contracts, not dollars -- confirmed by the
   volume reconciliation above. This matters because the claim "our 100 contracts would be a large
   share of qualifying liquidity" depends entirely on it.

**[M] The snapshot corpus is two-tier.** The recorder does a broad sweep (median **2** snapshots per
ticker) plus a deep poll of ~1,199 tickers (up to **5,384** snapshots each, one per ~13.6s). Every
time-series statistic in section 4 comes from the deep-polled subset, which skews toward pre-game
and futures markets and therefore **understates** how fast quotes move in-play.

---

## 1. The Liquidity Incentive Program, read from the certified rule

I read both the [help centre article](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program)
and the [CFTC certified rule filing of 11 Feb 2026](https://www.cftc.gov/sites/default/files/filings/orgrules/26/02/rules02112639183.pdf).
They disagree in two places that matter. The filing is the legally operative text.

### 1.1 How Score is actually computed

**[C]** From Appendix A of the filing, verbatim:

```
Score(bid) = Discount Factor ^ (Reference Price - Price(bid)) x Size(bid)

Normalized Qualifying Score(bid) = Score(bid) / sum over b in bids of Score(b)

Snapshot Liquidity Provider Score(user)
    = sum of Normalized Qualifying Yes Scores for the user's yes bids
    + sum of Normalized Qualifying No Scores  for the user's no bids

Time Period Liquidity Provider Score(user)
    = sum of user's Snapshot Scores / sum of ALL users' Snapshot Scores

Payout(user) = Time Period Liquidity Provider Score(user) x Time Period Reward
```

**[I] Three consequences that the help centre summary does not make obvious.**

- **The score is normalised per side, so one snapshot is worth exactly 2.0 in total across all
  participants** (1.0 yes side, 1.0 no side). Your reward is purely your *share* of discount-weighted
  resting size. Absolute size is irrelevant except through that share. Adding capital to a market
  where you already quote has sharply diminishing returns.
- **Only bids in the "qualifying set" score at all.** The filing's walk-down starts at the best bid
  and accumulates size down the ladder until cumulative size reaches Target Size, then stops. Bids
  below that cutoff score **zero**. If one participant already rests more than Target Size at the
  best bid, the qualifying set is that single price level, and you must be **at that exact price** to
  earn anything at all.
- **Improving the price earns no bonus multiplier** -- orders at or better than the Reference Price
  all get 1.0x. But posting size at a *better* price relocates the Reference Price to you and pushes
  everyone else down the discount curve. **The scoring therefore mechanically rewards spread
  compression**, and how hard it does so is set entirely by the Discount Factor, which Kalshi sets
  per Time Period and does not publish in advance.

### 1.2 What "Reference Price" means -- the two sources contradict each other

**[C] The certified filing** says the Reference Yes Price is simply the highest yes bid:

> "If the highest yes bid price exists and is less than the highest possible price, it is assigned to
> the Reference Yes Price."

The walk-down that follows determines *which bids qualify*, and explicitly proceeds "without
reinitializing the ... Reference Yes Price."

**[C] The help centre** (updated ~3 weeks before this was written, so later than the Feb filing) says
something materially different:

> "Walking down from the best bid, it is the first price level at which cumulative resting size
> reaches one fifth of the Target Size ... it is not always the best bid or ask - a small order alone
> at the top of the book does not set it"

**[I] These are not the same rule.** Under the filing a 1-lot alone at the top sets the Reference
Price and everything below is discounted. Under the help centre it does not -- you need Target/5 of
cumulative depth, i.e. more than 20 contracts. The help centre version is an anti-gaming patch and is
probably the live implementation, but I found no certified filing containing it. **Treat the exact
Reference Price rule as unresolved and verify it on a live market page before sizing anything on it.**

### 1.3 The $0.005/contract cap belongs to a different program

**This is a correction to the brief.** Neither the certified LIP filing nor the LIP help centre
article contains any per-contract cap. The LIP caps are:

- **[C]** Time Period Reward "no less than $10 and no greater than $1,000 per calendar day" (filing).
  The help centre says "$1-$1,000 per market, per day" -- a second, minor discrepancy.
- **[C]** Minimum payout **$1.00** per Time Period, rounded down to the nearest cent.

**[C] The $0.005 per contract cap is the *Volume* Incentive Program (VIP)**, a separate scheme:
["Maximum $0.005 per contract traded"](https://help.kalshi.com/en/articles/13823850-what-is-the-kalshi-volume-incentive-program),
on contracts priced **$0.03 to $0.97**, split pro-rata by volume share, program end
**1 September 2027**. So the answer to "traded or resting?" is: **traded, and it is not the LIP.** It
is also a *cap*, not a rate -- you receive your share of a pool, never more than $0.005/contract.

**[I] The two programs stack**, and this is the one genuinely favourable structural finding in this
section: a maker who rests size *and* gets filled earns LIP share-of-pool for the resting **plus** up
to $0.005/contract for the fills. Both programs exclude Market Maker Agreement holders, so
Susquehanna is not competing for either pool.

**[M] But 41.05% of all traded contracts fall outside the 3c-97c VIP band**, because 46.0% of
contracts trade at 0-9c. The VIP therefore applies to only **58.95%** of flow.

### 1.4 What disqualifies a quote

**[C]** A snapshot is excluded entirely if the market is not open, or if there is not resting size
meeting Target Size **on both sides**. Exclusions scale the payout: reward is multiplied by
`(non-excluded snapshots / total snapshots)`. The two-sided test is on the *whole book*, not your
orders -- the market can qualify without you, and you simply score zero.

**[M] How often do markets clear a 100-contract two-sided bar?** Across the deep-polled set,
time-weighted, the fraction of snapshots with at least 100 contracts on both sides at the touch is
p25 = 0.33, **p50 = 0.79**, p75 = 1.00.

### 1.5 The sibling programs are not reachable

- **[C] Liquidity Provider Program**: "Members who have executed a Market Maker Agreement with Kalshi
  may become a Designated Liquidity Provider." Allocated by reverse auction in which you "share the
  minimum amount you're willing to receive as an Incentive Period Reward." **Not reachable.**
- **[C] Combo Incentive Program**: distributes an Incentive Amount "to liquidity providers in
  proportion to their maker-volume of Program Eligible Trades." Its help centre URL 404s; details
  survive only in the CFTC filing text. Nominally open, practically undocumented.
- **[C]** An independent analysis
  ([Bawa](https://www.navnoorbawaresearch.com/p/kalshi-publishes-one-liquidity-subsidy)) reports the
  sealed Liquidity Provider tier is capped at **$50,000 per series per week** -- 7x the public tier --
  with terms filed as "Confidential Appendix B". **[I] The open programs are the small tier; the
  large tier requires an agreement we cannot get.**

### 1.6 The certified end date is five days from now

**[C]** The certified filing says the Program continues "until the earlier of **September 1, 2026**,
or the date that Kalshi amends or terminates the Program." The help centre says "Program end:
**January 1, 2027**." I found no certified filing carrying the January date. The May 2026 Block Trade
Rebate Program filing carries the *same* September 1, 2026 sunset, so this looks like Kalshi's
standard rolling expiry rather than an oversight.

**[C] And the CFTC is actively scrutinising exactly these programs.** On **12 August 2026** the
Division of Market Oversight issued
[Staff Letter 26-23](https://www.cftc.gov/PressRoom/PressReleases/9282-26), an advisory on "an
increasing number of incentive-program rule filings ... particularly those relating to event contract
products -- that contain procedural or substantive deficiencies," asking DCMs to review and amend
previously certified incentive programs by **14 September 2026**. Reported flags include volume-based
reward structures that may incentivise wash trading and market-maker programs that guarantee profits.

**[I] The subsidy layer therefore has an unresolved legal end date inside the next week and a live
regulator review inside the next three weeks.**

### 1.7 The program is invisible from public data

**[M]** I probed the public API for LIP metadata: `/incentives`, `/liquidity_incentives`, `/rewards`
and `/markets/{ticker}/rewards` all return **404**. A full field dump of a live market
(`KXMLBGAME-26AUG292205AZSFG2-SF`) contains no incentive schedule, no Target Size, no Discount
Factor, no Time Period Reward. The help centre says these appear "on market pages."

**[I] You cannot discover which markets carry rewards, or how large the pools are, without a funded
logged-in account.** Every LIP revenue figure in section 3 is therefore a scenario, not a forecast.
This is a hard gate on the whole plan: the subsidy cannot be targeted from public data.

---

## 2. Is the counterparty claim true?

The thesis has two halves: (a) the flow is retail, and (b) professionals are not already taking the
spread. **(a) is well supported. (b) is false.**

### 2.1 The flow really is retail, and it really is bad

- **[C]** Sports is roughly **80%** of Kalshi volume since July 2024.
- **[M]** My corpus agrees on direction: of 118,759 tickers, **68,173** are Sports series markets.
- **[C]** The [Roosevelt Institute](https://rooseveltinstitute.org/blog/since-kalshis-launch-ordinary-users-have-lost-half-a-billion-dollars/),
  analysing "more than 400 million trades (with over $32 billion in total volume) ... from its launch
  in July 2021 to May 2026", estimates market takers have lost **$583.5 million**, of which
  **$371.6 million** from sports. Kalshi
  [disputes this](https://news.kalshi.com/p/no-ordinary-users-arent-losing-half-a-billion-dollars-on-kalshi),
  arguing the study counts institutional market-maker high-frequency activity as "ordinary users."
- **[C]** Roosevelt also reports **63.2%** of taker revenue flowed to just **6.3%** of matched orders
  -- those worth $200 or more.
- **[M] The flow signature is unmistakably retail longshot buying.** Across 338M contracts,
  **75.9% of taker volume is buying YES**, and **46.0% of all contracts trade at 0-9c**. That is the
  classic lottery-ticket pattern, not informed two-way institutional flow.

### 2.2 But the professionals are already there, and the academic result is unkind

This is the part that decides the thesis.

- **[C] Susquehanna International Group is Kalshi's flagship market maker**, and established a
  dedicated prediction-markets desk in 2023 ([SIG](https://sig.com/predictions/)).
- **[C] On 19 August 2026 -- eight days before this was written -- Cantor Fitzgerald and Susquehanna
  opened institutional access to Kalshi**, with Susquehanna providing pricing and liquidity and
  Cantor brokering ([CoinDesk](https://www.coindesk.com/markets/2026/08/19/cantor-opens-kalshi-prediction-markets-to-thousands-of-institutional-clients)).
  CNBC's framing the same day: ["Hedge funds are about to jump into prediction markets in a big
  way"](https://www.cnbc.com/2026/08/19/hedge-funds-are-about-to-jump-in-big-to-prediction-markets.html).
- **[C]** Wintermute confirmed in May 2026 that it quotes two-sided markets on both Kalshi and
  Polymarket; Jump Trading doubled its team to ~20 and took equity stakes. Citadel Securities, IMC and
  HRT were reported as having stayed away.

**[C] The decisive evidence is the only peer-reviewed study of Kalshi's maker/taker economics.**
Burgi, Deng and Whelan, *"Makers and Takers: The Economics of the Kalshi Prediction Market"*
(University College Dublin, January 2026;
[SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5502658),
[PDF](https://www.karlwhelan.com/Papers/Kalshi.pdf)), using 313,972 contract-price observations:

> "The average return on contracts for Makers was **-9.64%** while for Takers it was **-31.46%**."

> "Average loss rates for contracts costing 10c and under are over 60%."

> "On average, Makers who buy contracts costing 50c and over earn a **2.6%** rate of return."

**[I] Read this carefully, because it is easy to over- or under-claim.** The paper measures the
return on *contracts bought by* each side, on the purchase price. Both sides can show negative
percentage returns without violating accounting, because the denominators differ wildly: a maker
buying YES at 5c and a taker buying NO at 95c experience the same dollar transfer as -100% and +5.3%
respectively. So **-9.64% is not "market making loses money"** -- it is "the population of people
posting limit orders, including everyone posting cheap longshot bids, lost 9.64% of what they spent."

**[I] What it does establish, and this is the useful part:**

- Passively posting limit orders on Kalshi was **not** a free spread harvest even in the most
  favourable era the data covers. The paper's sample deliberately ends **April 2025**, before Kalshi
  introduced maker fees -- so this is a *zero-maker-fee* measurement, the best case.
- The profitable niche is narrow and specific: **makers buying contracts at 50c and above earned
  +2.6%**. Equivalently, *selling cheap longshots to retail*. That is a directional
  favorite-longshot harvest, not spread capture.
- The paper notes "some evidence that the bias in prices is diminishing over time" -- the edge was
  already decaying inside the sample, which ended 16 months ago and before the sports boom, before
  Susquehanna's expansion, and before institutional access.

**Verdict on the thesis.** Half (a) holds: the flow is retail and it loses. Half (b) fails: the
spread against that flow is already being collected, principally by a designated market maker with a
Market Maker Agreement, and the measured maker return is not the comfortable positive number the
thesis assumes.

---

## 3. The revenue model

Built from parameters, with the arithmetic shown.

### 3.1 The capital block

**[I]** Resting `Q` contracts on each side of a market costs `Q x p` for the yes bid plus
`Q x (1 - p - s)` for the no bid, i.e. `Q x (1 - s)` in total, where `s` is the spread in dollars.
For `Q = 100` and a 3c spread that is **$97 per market**.

```
$10,000 / $97 per market  ->  103 markets at 100 contracts per side
                          ->  10,300 contracts resting per side, 20,600 total
```

This reproduces the brief's 104-136 range.

### 3.2 The single most important measurement: where the spread is, there is no room

**[M] Spread and depth by 24h volume band** (active, two-sided markets; 75,330 of 116,011 active
markets carry a two-sided quote, i.e. **64.9%**):

| v24 band | markets | spread p25 | p50 | p75 | bid size p50 | ask size p50 | both sides >=100 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0 | 58,633 | 4 | 8 | 28 | 200 | 167 | 42.5% |
| 1-99 | 7,572 | 1 | 4 | 10 | 100 | 209 | 40.6% |
| 100-999 | 5,759 | 1 | 2 | 6 | 200 | 317 | 49.7% |
| 1k-10k | 2,816 | 1 | **1** | 3 | 625 | 990 | 57.6% |
| 10k-100k | 493 | 1 | **1** | 2 | 924 | 2,050 | 56.8% |
| 100k+ | 57 | 1 | **1** | 1 | 4,526 | 3,530 | 78.9% |

**[I] The headline "median spread 7c" is an artefact of 58,633 markets that do not trade.** Where
volume exists, the median spread is **1 cent** and the touch is thousands of contracts deep. The
7c median describes markets with no flow to capture.

**[M] A live orderbook makes the point concrete.** `KXMLBGAME-26AUG271305COLWSH-WSH`, pulled from
the API while writing this (1.39M contracts of 24h volume, spread 1c):

```
YES bids                          NO bids (= yes asks)
0.56 x 225,747                    0.43 x 207,252
0.55 x 178,124                    0.42 x 734,203
0.54 x  47,100                    0.41 x 136,975
```

**[I] Adding 100 contracts at 0.56 makes us 100/225,847 = 0.044% of the qualifying yes-side score.**
On a $1,000/day pool that is **$0.44/day -- below the $1.00 minimum payout, therefore zero.** In the
markets that actually trade, a $10,000 book is not a liquidity provider; it is a rounding error.

### 3.3 Where a small book *can* matter

**[M] Filtering for markets where 100 contracts is a meaningful share and flow genuinely exists**
(both touch sizes < 500 contracts, at least 50 contracts/hour of measured trade flow): **997 markets**,
median spread **4c**, median touch depth 34 bid / 40 ask, median rate 145 contracts/hour.

**[M] What are they?** By ticker prefix: `KXITFMATCH` and `KXITFWMATCH` (ITF lower-tier professional
tennis), `KXCS2MAP` / `KXCS2GAME` (Counter-Strike 2 esports), `KXITFDOUBLES`, `KXTTELITEMATCH`
(table tennis), `KXRAIN` (weather).

**[I] This is the adverse-selection question answering itself before it is measured.** The markets
where a $10,000 book can be the dominant liquidity provider are precisely lower-tier tennis, esports
and table tennis -- the sports with the thinnest public information, the fastest court-side data
edges, and the worst integrity reputation in the betting industry. These are not markets where an
uninformed crowd trades against you; they are markets where the only people paying attention have a
live data feed you do not have. If the parent measurement finds `AS` is low *on average*, it will
almost certainly be much higher than average here.

### 3.4 The model, and the only number that matters

**[I]** Per fill, the maker's gross edge against the mid is `spread / 2`. Adverse selection is the
amount by which fair value moves against the fill. So:

```
net edge per fill (cents) = spread/2 - AS
fills per day             = 2 x M x Q x T          T = book turns per side per day
monthly P&L               = fills/day x 30 x (spread/2 - AS) / 100
```

With `M = 103`, `Q = 100` (20,600 contracts resting):

| market class | spread | turns/day | fills/day | turnover | AS=0.0 | AS=0.5 | AS=1.0 | AS=2.0 | **breakeven AS** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| liquid, fast | 1c | 4.0 | 82,400 | 3.7x | +$12,360 | **$0** | -$12,360 | -$37,080 | **0.50c** |
| balanced median | 3c | 2.0 | 41,200 | 1.9x | +$18,540 | +$12,360 | +$6,180 | **-$6,180** | **1.50c** |
| wide, slow | 7c | 1.0 | 20,600 | 0.9x | +$21,630 | +$18,540 | +$15,450 | +$9,270 | **3.50c** |
| very wide, rare | 20c | 0.25 | 5,150 | 0.2x | +$15,450 | +$14,678 | +$13,905 | +$12,360 | **10.00c** |

(monthly dollars, $10,000 book)

**The breakeven adverse selection is exactly half the spread.** That is the whole model in one line.
Everything else is a question about which row you are actually trading in.

**[M] And the volume sits in exactly the row with the tightest breakeven.** Distributing trade-tape
contracts across active two-sided markets by that market's current spread:

| spread | contracts | share | cumulative |
|---|---:|---:|---:|
| <= 1c | 27,978,254 | **46.4%** | 46.4% |
| <= 2c | 10,152,481 | 16.8% | 63.2% |
| <= 3c | 9,426,398 | 15.6% | **78.8%** |
| <= 5c | 7,696,075 | 12.8% | 91.6% |
| <= 10c | 1,477,209 | 2.4% | 94.1% |
| > 10c | 3,583,761 | 5.9% | 100.0% |

- **46.4% of flow is in 1c-spread markets**, where you are dead at `AS >= 0.5c` and cannot get
  meaningful LIP share anyway (0.044%).
- **78.8% of flow is at 3c or tighter**, so the `AS = 1.5c` breakeven covers most of the reachable
  business.
- The wide rows look robust but **[M]** carry little flow: the 23,988 active two-sided markets with
  spreads above 10c account for just **5.9%** of contracts, and the 20c row assumes a quarter turn
  per day.

### 3.5 Why the gross numbers look absurd, and what that means

**[I] A 3c spread on a 45c contract is a 6.7% round-trip margin. In equities a 1c spread on a $50
stock is 0.02%.** Kalshi's relative spreads are two to three orders of magnitude wider. Any model
that assumes you capture them at scale produces silly returns -- my first attempt produced $28M/month
on $10,000, which is a reductio, not a forecast.

The correct reading is: **the gross spread is enormous, therefore either adverse selection is
correspondingly enormous, or a very large unexploited edge is sitting in public view on a
CFTC-regulated exchange that Susquehanna, Wintermute and Jump all quote.** The second is not
credible. The parent's `AS` measurement is not a refinement of this model -- it *is* the model.

### 3.6 The subsidy layer, sized honestly

**[M/I] VIP** at $0.005/contract on the 58.95% of flow priced 3c-97c:

| turns/day | fills/day | eligible | $/day | $/month |
|---:|---:|---:|---:|---:|
| 1 | 20,600 | 12,144 | $60.72 | $1,822 |
| 2 | 41,200 | 24,287 | $121.44 | $3,643 |
| 4 | 82,400 | 48,575 | $242.87 | $7,286 |

This is a **cap**, not a rate -- actual payment is your share of a pool, so treat these as ceilings.

**[M/I] LIP** by queue depth, at 100 contracts of our size:

| queue ahead of us | our share | $50/day pool | $1,000/day pool |
|---|---:|---:|---:|
| 225,747 (measured liquid MLB touch) | 0.044% | $0.02 -- **below $1 min, pays zero** | $0.44 -- **below $1 min, pays zero** |
| 2,500 (10k-100k band median) | 3.85% | $1.92 | $38.46 |
| 200 (thin addressable) | 33.3% | $16.67 | $333.33 |
| 34 (very thin, measured median) | 74.6% | $37.31 | $746.27 |

**[I] The subsidy inverts the market-selection problem.** LIP pays best exactly where flow is
thinnest, and pays literally nothing where flow is deepest. That is the program working as designed
-- Kalshi is buying liquidity it does not have -- but it means the subsidy cannot rescue the liquid
book, and in the thin book it is paying you to stand in front of the most informed flow on the
exchange. And **[M]** none of it is targetable from public data (section 1.7).

---

## 4. Inventory and tail risk -- the part naive analyses miss

A prediction-market maker who gets filled accumulates a directional position that settles to $0 or
$1. There is no closing bell to flatten into. This section is the strongest argument against the
whole plan, and it is measured rather than asserted.

### 4.1 Taker flow is overwhelmingly one-directional

**[M]** Per-market flow imbalance `I = |2 x buyshare - 1|`, over 38,773 markets with at least 500
contracts traded: **p10 = 0.59, p25 = 1.00, p50 = 1.00, p75 = 1.00**. **90.9% of markets are worse
than a 75/25 split.**

**[M]** Weighted by contracts, across all 118,410 tickers with trades:

| imbalance | markets | contracts | share of flow |
|---|---:|---:|---:|
| 0.0-0.2 (genuinely two-sided) | 5,342 | 96,707,926 | **28.2%** |
| 0.2-0.4 | 1,457 | 41,210,214 | 12.0% |
| 0.4-0.6 | 1,443 | 30,774,195 | 9.0% |
| 0.6-0.8 | 1,509 | 18,842,637 | 5.5% |
| 0.8-1.0 | 2,841 | 22,727,736 | 6.6% |
| 1.0 exact (completely one-way) | 105,818 | 133,093,783 | **38.8%** |

**[I] This is the finding that breaks the round-trip assumption.** A market maker's business model is
buy at the bid, sell at the ask, repeat. That requires flow to arrive from both directions. On
Kalshi, **38.8% of traded contracts arrive in markets where every single taker went the same way**,
and only **28.2%** of flow is in markets balanced within 0.2. In a one-directional market you do not
round-trip -- your capital converts once into a position and stops.

### 4.2 What the position is worth at settlement

**[M]** Of 600 markets observed active with a two-sided quote and later finalized inside the window,
the last observed mid before settlement was:

| last mid | share |
|---|---:|
| <10c | 15.2% |
| 10-25c | 18.0% |
| 25-45c | 20.7% |
| 45-55c | 14.0% |
| 55-75c | 17.7% |
| 75-90c | 9.0% |
| >90c | 5.5% |

**52.5% of markets settle from a mid between 25c and 75c** -- they go to resolution genuinely
undecided, and the position pays 0 or 100.

**[M]** Standard deviation of unhedged inventory held to settlement is `sqrt(m(100-m))/100` dollars
per contract; across this population the mean is **$0.394/contract**.

### 4.3 The ratio that decides it

**[I]** Put the two together for a fully deployed book:

```
max one-sided inventory        : 10,300 contracts (~$4,635 at 45c)
spread earned building it @3c  : 10,300 x 1.5c   = $154
1-sigma settlement swing       : 10,300 x $0.394 = $4,058   = 41% of the $10,000 account

ratio of settlement risk to spread earned : 26 : 1
```

**[M/I] And flattening actively always costs more than the spread earned.** Crossing to flat means
paying the spread plus the taker fee `7p(1-p)`, which at 45c is 1.73c:

| spread | earned as maker | cost to cross out | net |
|---:|---:|---:|---:|
| 1c | 0.5c | 2.73c | **-2.23c** |
| 3c | 1.5c | 4.73c | **-3.23c** |
| 7c | 3.5c | 8.73c | **-5.23c** |
| 20c | 10.0c | 21.73c | **-11.73c** |

**[I] This is the trap.** You earn half a spread passively; you pay a full spread plus a taker fee to
exit. So passive exit is the only economic exit, and passive exit requires the flow to reverse --
which section 4.1 measures as not happening in 38.8% of flow and only reliably happening in 28.2%.
The alternative is to hold to settlement and accept a 26:1 risk-to-reward ratio on each position.

**[I] How a real desk manages this**, and why none of it is available here: quote two-sided and lean
the quote to bleed inventory back out (works only with two-way flow); hedge into a correlated
instrument (Kalshi's mutually-exclusive events allow some netting via `MECNET` collateral, but
`research/09` found the intra-event basis is already tight to within the fee); or cap inventory hard
and stop quoting. The last is the only one that survives, and it converts the strategy into "quote
until filled once, then sit out," which collapses `T` -- and `T` is the multiplier on all of section
3.4.

### 4.4 The one strategy the evidence actually supports

**[I]** Combining **[M]** 75.9% of taker volume buys YES and 46.0% of contracts trade at 0-9c, with
**[C]** Burgi-Deng-Whelan's "makers who buy contracts costing 50c and over earn a 2.6% rate of
return" and "average loss rates for contracts costing 10c and under are over 60%": the historically
profitable maker behaviour on Kalshi was **selling cheap longshots to retail** -- i.e. resting bids on
the expensive side of a lopsided market.

That is a directional favorite-longshot harvest, **not** market making. It has the opposite risk
shape: you win a few cents most of the time and lose ~95c occasionally, with the losses correlated
across every longshot in the same game. It needs a different document, a different risk model, and it
faces the same "bias is diminishing over time" decay the paper flags. I mention it because it is
where the evidence points, not because this analysis clears it.

---

## 5. What would actually kill this, ranked

Ranked by probability x severity, most dangerous first.

1. **Adverse selection exceeds half the spread in the markets we can actually reach.** Breakeven is
   0.50c in the liquid book and 1.50c in the balanced book (3.4). The only markets where a $10,000
   book is a meaningful share are ITF tennis, CS2 and table tennis (3.3) -- the highest-information-
   asymmetry corners of the exchange. **This is the parent measurement and it is the whole decision.**
2. **Inventory, not spread, is the binding constraint.** 90.9% of markets have worse than 75/25 flow;
   38.8% of contracts trade in completely one-directional markets; settlement risk is 26x the spread
   earned; and active exit costs more than passive entry earned (4.1-4.3). This kills the strategy
   independently of adverse selection.
3. **Professional competition is arriving right now.** Susquehanna is the designated market maker;
   Cantor + Susquehanna opened institutional access **eight days ago**; hedge funds are reported to be
   entering "in a big way" (2.2). The measured 1c spreads in liquid markets are what that competition
   already looks like.
4. **The subsidy layer has a published expiry and an active regulator review.** Certified LIP end date
   **1 Sep 2026** (five days away) versus a help centre claim of 1 Jan 2027; CFTC Staff Letter 26-23
   dated 12 Aug 2026 requires DCMs to review incentive filings by **14 Sep 2026** (1.6).
5. **The LIP is not targetable from public data** -- no API surface for reward schedules, Target Size
   or Discount Factor (1.7). You cannot even build the strategy without a funded account.
6. **Account access and eligibility.** LIP and VIP both exclude non-US users, IB/FCM customers and MM
   Agreement holders; rewards above IRS thresholds require an SSN on file, and are ordinary income.
7. **The 15-second public data grid.** **[M] Least dangerous of the listed risks, on this evidence.**
   Across the deep-polled set the median market changes its touch **0.3 times per hour** (about once
   every 3 hours), p90 is 7.3/h and p99 is 31.4/h. **Zero tickers averaged faster than the 15s grid.**
   But **[I]** this set skews pre-game; `research/09` documented an in-play soccer quote repricing
   59 -> 64 -> 76 -> 79 in 40 seconds, so the grid is disqualifying for in-play quoting specifically.
8. **Maker fees.** **[M]** 8,507 of 118,759 markets (7.2%) sit on `quadratic_with_maker_fees` series
   rather than `quadratic`. Small, but it is not "makers pay zero" universally -- check per series.

**[M] One number for scale.** Mid travel while you rest: over the deep-polled set the mid's range is
p50 **3c**, p75 11.5c, p90 37.5c, against a median spread in that set of **1c**. Expressed as
multiples of the spread you are trying to earn: p50 **2.0x**, p75 8.0x, p90 29.0x. The price moves
several spreads while you wait to earn one.

---

## 6. Verdict

**No. Passive market making on Kalshi is not worth building for someone with $10,000.** I went in
wanting the thesis to survive; it does not, and it fails for a reason independent of the
adverse-selection number still being measured.

The thesis's first half is right: **[C]/[M]** the flow really is retail, really is 80% sports, really
is 75.9% one-way YES buying, and really does lose money at scale. The second half is wrong in three
separate ways, any one of which is disqualifying:

- **Where the flow is, there is no spread and no room.** **[M]** Markets with real volume have a
  **1-cent** median spread and **225,747** contracts at the touch. 100 contracts is 0.044% of
  qualifying liquidity -- below the LIP's $1.00 minimum payout, so literally zero. Breakeven adverse
  selection there is **0.50c**.
- **Where there is room, the counterparty is not the uninformed crowd.** **[M]** The 997 markets a
  $10,000 book could dominate are lower-tier tennis, CS2 esports and table tennis.
- **The inventory does not clear.** **[M]** 90.9% of markets have worse than 75/25 flow; settlement
  risk on a full book is **41% of the account** at one sigma against **$154** of spread earned; and
  crossing out costs more than resting in earned, at every spread.

And **[C]** the one academic study of Kalshi maker economics -- measured in the zero-maker-fee era,
the best case -- puts the average return on contracts bought by makers at **-9.64%**, with the only
positive niche (**+2.6%**) being makers buying contracts at 50c and over. That is longshot-selling,
not liquidity provision.

The LIP is a genuinely well-designed program and the maker-fee-free structure is real. But
**[M/C]** it pays nothing where the volume is, its certified terms expire in five days, it is under
CFTC review, and it is invisible from public data.

### 6.1 If you want to falsify this anyway, here is the smallest experiment

I would rather be proven wrong cheaply than be right in a document. The cheapest decisive test, in
order, gated not timed:

**Gate 0 -- resolve the two documentation contradictions before spending anything.** Fund the minimum
account. On a live market page, read off the actual **Target Size, Discount Factor and Time Period
Reward**, and determine empirically whether the Reference Price follows the certified rule (best bid)
or the help centre rule (cumulative Target/5). Confirm whether the program end date is 1 Sep 2026 or
1 Jan 2027. **Cost: the minimum deposit. If the LIP has lapsed or the pools are at the $1-$10/day end,
stop here.**

**Gate 1 -- one market, 100 contracts, the balanced set.** Pick a single market from the **[M]** 803
active markets with flow imbalance < 0.2 and a two-sided book -- *not* from the thin ITF/CS2 set.
Quote 100 contracts on both sides for one full market lifetime. Record every fill, the mid at fill,
the mid 1/5/30 minutes later, and the settlement. **Capital at risk: ~$100.** This directly measures
your own realised adverse selection and your own realised `T`, which are the only two free parameters
in section 3.4.

**Gate 2 -- the go/no-go arithmetic.** From Gate 1 compute realised `AS` and realised turns `T`.
Proceed only if **`spread/2 - AS > 0` with the observed spread**, *and* you closed flat without
crossing. If you ended the market holding inventory you could not passively exit, the strategy has
already failed regardless of the P&L on that one market -- section 4 says that is the modal outcome.

**Gate 3 -- ten markets, then stop and re-measure.** Only if Gates 1-2 pass. $1,000 at risk, not
$10,000. Re-run the same arithmetic before scaling further.

**[I] The honest expected outcome** is that Gate 1 shows either (a) almost no fills, because you are
behind hundreds of contracts of queue in any market worth quoting, or (b) plenty of fills in a thin
market followed by an inventory position you cannot exit. Both are cheap answers, and both are
consistent with everything measured above.

---

## Appendix: measurements run for this file

All read-only against `file:data/pm.db?mode=ro`, plus unauthenticated public API calls.

| # | measurement | result |
|---|---|---|
| 1 | tickers / active / two-sided | 118,759 / 116,011 / 75,330 (64.9%) |
| 2 | spread percentiles, all two-sided | p25 3c, p50 7c, p75 20c |
| 3 | spread by volume band | 1c median at every band with v24 >= 1,000 |
| 4 | live orderbook, COLWSH | 0.56 x 225,747 / 0.43 x 207,252, spread 1c |
| 5 | trade tape vs volume_fp reconciliation | 1,424,378 vs 1,423,391 (0.07%) |
| 6 | taker direction, exchange-wide | 75.9% buys YES |
| 7 | per-market flow imbalance | p50 = 1.00; 90.9% worse than 75/25 |
| 8 | flow in balanced (I<0.2) markets | 28.2% of contracts, 5,342 markets |
| 9 | trade price distribution | 46.0% at 0-9c; 41.05% outside VIP 3-97c band |
| 10 | volume-weighted taker fee | 0.690 c/contract |
| 11 | settlement mid distribution | 52.5% settle from 25-75c; mean sigma $0.394/contract |
| 12 | touch-change frequency, deep set | p50 0.3/h; zero tickers faster than the 15s grid |
| 13 | mid travel vs spread, deep set | p50 2.0x, p75 8.0x, p90 29.0x |
| 14 | addressable set (thin + flow) | 997 markets; ITF tennis, CS2, table tennis |
| 14b | flow share by market spread | 46.4% at 1c; 78.8% cumulative at <=3c; 5.9% above 10c |
| 15 | LIP two-sided qualification | p50 0.79 of snapshots have >=100 both sides |
| 16 | fee_type split | 109,733 quadratic / 8,507 with maker fees (7.2%) |
| 17 | LIP API surface | 404 on all incentive endpoints; not publicly observable |
