# 09 -- Edge Reality Check: Is There Anything Left After the Arbitrage Thesis Dies?

Written 2026-08-27, after the S1/S2/S3 measurement pass. Purpose: test the "pure arbitrage is
dead" conclusion against published evidence, re-derive it independently from `data/pm.db`, and
say what -- if anything -- a solo trader with ~$10,000 should do instead.

Every claim is tagged:

- **[M]** MEASURED -- computed by me from `data/pm.db` in this session, read-only. Query described inline.
- **[C]** CITED -- someone else's published number, with link.
- **[I]** INFERRED -- my reasoning on top of [M] or [C]. Weakest tier. Treated as such.

---

## 0. The caveat that governs every number in this file

**[M] The snapshot corpus is 9.0 hours wide, not 9 months.**

```
market_snapshots : 487,302 rows | 118,759 distinct tickers
                   2026-08-26 19:49 UTC -> 2026-08-27 04:46 UTC   (9.0 h)
trades           : 1,155,650 rows | $52.2M notional
                   2026-08-27 01:13 UTC -> 2026-08-27 04:46 UTC   (3.6 h)
```

The row counts are large. The *window* is one Wednesday evening in North America. It contains one
MLB/WNBA slate, one Leagues Cup night, one weather day. It contains **no** FOMC meeting, no CPI
print, no jobs report, no election, no NFL Sunday, no exchange outage, no major news shock.

This does not overturn the conclusion, but it changes its status. "There is no taker Dutch book on
Kalshi" is not what we have shown. What we have shown is: **on a normal evening, at normal
volatility, with a normal market-maker population awake, there is no taker Dutch book.** The
interesting cases -- if they exist -- are in the tail this window does not contain. Section 1.4
returns to this.

Secondary caveats, both [M]:

- Only **25.2%** of event-observations contain every known leg of the event at the same
  `observed_at_us`. Any basket computation that groups purely on timestamp without checking leg
  completeness silently mixes stale legs and manufactures fake arbitrage. Every basket number
  below is restricted to **synchronous complete books** (`count(legs) == count(distinct tickers
  ever seen for that event)`), which costs coverage but is the only sound choice.
- `event_snapshots.exhaustive_verified` is **0 for all 13,954 events**, and `links` has **0 rows**.
  The exhaustiveness verdict and the cross-market link graph are not populated in the database.
  I therefore fall back on the exchange's own `mutually_exclusive` flag (6,689 events) and on
  `collateral_return_type` (`MECNET` 6,689 / `DIRECNET` 2,300 / empty 4,965).

---

## 1. Is the arbitrage result consistent with the literature?

Short answer: **yes, strongly, and the mechanism we found is the same one the literature names.**
But the literature also says the residual is *not exactly zero* -- it is a few dollars at a time,
in-play, on a latency clock. Our own data reproduces exactly that.

### 1.1 An independent re-derivation, and a closed form for why it fails

Kalshi's taker fee is **[C]** `ceil(0.07 * C * P * (1-P) * 100) / 100` dollars, `C` = contracts,
`P` = price in dollars -- a parabola peaking at 1.75c/contract at 50c, and the maker fee is charged
only on a handful of series
([Kalshi Fees help](https://help.kalshi.com/en/articles/13823805-fees),
[marketmath.io](https://marketmath.io/platforms/kalshi),
[pm.wiki](https://pm.wiki/learn/kalshi-fees-explained)).

**[I] This gives an exact hurdle for a complete mutually-exclusive basket.** Sell YES on every leg
of a partition. The legs' prices sum to ~1 by construction, so ignoring per-order rounding the
total taker fee in cents is

```
    fee = sum_i 7 * p_i * (1 - p_i)
        = 7 * (1 - sum_i p_i^2)
        = 7 * (1 - HHI)                 where HHI is the Herfindahl of the implied distribution
```

So **the fee on a complete taker Dutch book is 7 cents times one-minus-Herfindahl, and is bounded
above by 7 cents.** It is near zero only when the event is nearly decided (one leg at ~99c), and it
approaches the full 7c whenever probability is spread across legs -- which is precisely when an
overround is most likely to appear. The strategy is fighting its own fee bill.

**[M] The data matches the identity.** Across 39,021 usable ME event-observations covering 6,670
events:

| quantity | median | p90 | p99 | max |
|---|---:|---:|---:|---:|
| overround `sum(YES bid) - 100` (cents) | **-29.0** | -1.0 | 1.0 | **+10.0** |
| fee floor `7 * (1 - HHI)` (cents) | 5.69 | -- | -- | 7.00 |

Observations with a positive overround at all: **407**. Observations where the overround beats even
the asymptotic (large-size, no-rounding) fee floor: **20**. At realistic size-1 rounding: **3**.

Broken out by leg count, the fee floor rises and the overround falls together -- the two curves
diverge as `n` grows:

| legs | obs | median overround | max overround | median fee floor | beats floor |
|---:|---:|---:|---:|---:|---:|
| 2 | 29,108 | -28.0 | +10 | 5.18 | 19 |
| 3 | 5,197 | -30.0 | +2 | 5.69 | 0 |
| 6 | 435 | -9.0 | +5 | 4.50 | 1 |
| 10 | 256 | -64.0 | +5 | 6.88 | 0 |
| 30 | 172 | -48.5 | +4 | 6.79 | 0 |
| 184 | 40 | -100.0 | -7 | 7.00 | 0 |

**[I] This is the same result as the parent measurement, arrived at from a different direction, and
it explains the "61 of 65 are 3-leg sports moneylines" finding.** Structures with few legs and a
heavy favourite have high HHI and therefore a small fee bill. They are the only shapes that can
even in principle clear the hurdle. They are also the shapes with the smallest overround.

### 1.2 The residual is real, tiny, in-play, and gone in seconds

**[M]** Restricting to synchronous complete books, active status, and requiring at least one
contract of depth on every leg:

```
synchronous + complete ME observations examined : 29,130
  of which sum(YES bid) > 100 with depth >= 1    :    367
  of which NET POSITIVE after taker fees         :     15   (across 3 distinct events)
```

All three are **two-leg in-play sports moneylines**:

| event | legs | overround | executable depth | gross | fee | net |
|---|---:|---:|---:|---:|---:|---:|
| `KXWNBAGAME-26AUG26TORSEA` | 2 | 2c | 4,944 | $98.88 | $56.63 | **$42.25** |
| `KXLEAGUESCUPADVANCE-26AUG26AMECLB` | 2 | 10c | 25 | $2.50 | $0.72 | **$1.78** |
| `KXMLBGAME-26AUG261905HOUNYY` | 2 | 2c | 30 | $0.60 | $0.55 | **$0.05** |

Total, assuming perfect simultaneous execution of full top-of-book on both legs: **$44.08 over 9
hours.** That is the *upper bound*, not an estimate.

**[M] Why they exist.** Every one is a stale quote during rapid in-play repricing. The soccer case,
5-second polling:

```
t+00s  AME 59/66 (102)   CLB 35/38 (25)     sum(bid)= 94
t+10s  AME 64/66 ( 25)   CLB 31/35 (141)    sum(bid)= 95
t+25s  AME 76/77 (63109) CLB 34/37 ( 25)    sum(bid)=110   <-- the "arbitrage"
t+40s  AME 79/80 (102)   CLB 18/21 (275)    sum(bid)= 97
```

America's bid repriced 59 -> 64 -> 76 -> 79 in 40 seconds. Columbus's bid lagged one tick behind.
The 10c overround is the width of that lag. The executable size is set by the *lagging* leg: 25
contracts.

**[M] Lifetime.** Defining an episode as a maximal run of consecutive observations with
`sum(bid) > 100` on a synchronous complete book, there were **126 episodes across 84 events** in
9 hours:

| duration (lower bound, 5s poll) | share |
|---|---:|
| single poll (< 5s resolvable) | **83%** (104/126) |
| >= 30 s | 5 |
| >= 60 s | 4 |
| max observed | 510 s |

Median 0 s at our resolution. Max overround in any episode: 10c. By venue type: 28 episodes in
in-play sports; the remainder overwhelmingly in far-dated illiquid politics/elections
(`KXCAGOVLAMAYOR`, `KXHOUSEPOPVOTEMARGIN`, `KXDHOUSESEATS`, ...) where the median time to close was
**2,319 hours (97 days)** and the "overround" is 1-2 cents of noise on a one-contract book.

### 1.3 What the literature says

**[C] Gebele, Mutzel and Matthes, "Executable Arbitrage and Market Efficiency in Prediction
Markets", arXiv:2608.00666 (submitted 1 Aug 2026)** -- the closest published analogue, 259M
Polymarket trades through Dec 2025 plus an L2 order-book panel Apr-May 2026
([abs](https://arxiv.org/abs/2608.00666), [html](https://arxiv.org/html/2608.00666v1)). Findings
that bear directly on us:

- Their central distinction is **payoff-space no-arbitrage versus protocol-executable
  no-arbitrage**. Violations concentrate on the side the protocol *cannot* execute pre-settlement
  (2,098 positive YES-side episodes vs **36** NO-side), and the executable side is arbitraged away.
- Median duration of exactly-timed CLOB episodes: **16.15 s** (YES side, n=253).
- Total arbitrage extracted: **~1.118M USDC**, of which 97% came from the protocol converter, not
  from basket formation. Settlement-based basket arbitrage -- the direct analogue of our S1/S2 --
  was **32,283 USDC across 5,990 baskets**, i.e. ~$5 per basket.
- The ten most profitable addresses took **75%** of converter profit; one actor took 76% of
  settlement-based profit in the FPMM sample.
- Per-conversion profit decayed from **~1.00 USDC (through Jul 2024) -> ~0.20 (late 2024-2025) ->
  ~0.08 by early 2026**, with a growing negative lower tail the authors read as "intensifying
  competition."

**[C] Cheng, Yang and Zou, "Arbitrage Analysis in Polymarket NBA Markets", arXiv:2605.00864 (Apr
2026)** -- 75M+ order-book snapshots across 173 games
([abs](https://arxiv.org/abs/2605.00864)): **7** executable single-market episodes, median duration
**3.6 s**; 290 combinatorial episodes with median return 101 bps; **76.9%** of opportunities capped
at an average executable size of **14.8 shares**. Their conclusion: executable mispricing is
"structurally bounded by liquidity", confining risk-free extraction "strictly to the retail scale".

**[C] Saguillo et al., arXiv:2508.03474** -- ~$40M of arbitrage extracted on Polymarket Apr 2024 -
Apr 2025 across >7,000 markets ([abs](https://arxiv.org/abs/2508.03474)). The headline is large;
Gebele et al. is the better guide to what was *left* by 2026.

**[C] Oliven and Rietz (2004), "Suckers Are Born but Markets Are Made", Management Science 50(3)
336-351** ([INFORMS](https://pubsonline.informs.org/doi/10.1287/mnsc.1040.0191),
[preprint](https://www.biz.uiowa.edu/faculty/trietz/papers/iemarb.pdf)) -- the canonical result for
mutually-exclusive baskets: IEM price-taking traders made frequent violations of the no-arbitrage
bounds, but **price-making traders did not, and the violations were driven out of prices.** The
structural lesson generalises: irrational *order flow* does not imply arbitrageable *prices*, because
the maker population is a different and more rational population than the taker population.

**[C] Betting-exchange literature** -- inter-market arbitrage between bookmakers and exchanges is
documented and persistent, but it originates at the *bookmaker*, not the exchange
([Franck, Verbeek and Nuesch](https://www.wiwi.uni-muenster.de/uf/sites/uf/files/PublikationenNuuesch/2013franck_verbeek_nueesch_2013.pdf));
within-exchange, Betfair horse-racing returns show rapidly decaying autocorrelation and no
long-memory ([arXiv:2402.02623](https://arxiv.org/pdf/2402.02623)).

### 1.4 Verdict, including where the older literature does not transfer

**[I] The finding is consistent with the literature, and the literature is more specific than "no
arbitrage".** Three points:

1. **Our zero and their non-zero are the same measurement.** Cheng et al. find 7 single-market
   episodes in 75M snapshots at a median 3.6 s; we find 3 in 487k snapshots at a median under 5 s.
   Per snapshot the rates are the same order. Their conclusion is that this is real but retail-scale;
   ours is that at a 900 s leg timeout and a 69.8% orphan rate it is *negative*-scale. Both are
   right. The difference is execution, not detection.

2. **Kalshi has the converter that Gebele et al. say matters, and it cuts against us.** Kalshi's
   `MECNET` / `DIRECNET` collateral return is the structural analogue of Polymarket's NegRisk
   Adapter: **[C]** it returns collateral "immediately upon entering positions -- even before any
   trade actually fills"
   ([Kalshi collateral return](https://help.kalshi.com/en/articles/13823816-collateral-return)),
   and **[M]** it is enabled on 6,689 ME events and 2,300 directional ladders in our corpus. Under
   Gebele's framework, an executable-converter direction is exactly the direction that gets
   arbitraged to zero -- which is what we observe. **[C] It also carries a trap for a basket
   strategy: "Enabling this feature may make you unable to sell positions for which you've already
   had collateral returned", and the flag is latched per-event at first order and cannot be changed
   retroactively.** A short-basket sleeve that turns collateral return on to improve capital
   efficiency may lose the ability to unwind -- which is the one thing a 69.8%-orphan strategy
   cannot afford. This deserves an explicit test before any capital is committed.

3. **Where PredictIT/Intrade-era findings do NOT transfer -- and it matters here.** The classic
   inefficiency results come from venues with structural features Kalshi does not share:
   PredictIT's **5% profit fee and 5% withdrawal fee** plus an **$850 per-contract position cap**,
   Intrade's offshore status and settlement risk, IEM's **$500 cap** and academic-only
   participation. Those caps *are* the reason arbitrage persisted: they made it impossible for
   anyone to size into the mispricing. Kalshi is a CFTC-regulated DCM with no such cap, a real CLOB,
   **[C]** Susquehanna as flagship market maker and Jump Trading with ~20 dedicated staff and equity
   in both venues, and **[C]** Cantor Fitzgerald opened Kalshi block trading to ~3,000 institutional
   clients in Aug 2026
   ([CNBC](https://www.cnbc.com/2026/08/19/hedge-funds-are-about-to-jump-in-big-to-prediction-markets.html)).
   **Citing PredictIT-era arbitrage persistence as evidence that Kalshi arbitrage should exist is
   a category error.** The direction of the correction is unambiguous: modern Kalshi should be
   *tighter*, and it is.

4. **The honest limit of our own evidence.** 9 hours is not a sample of the tail. The literature's
   own longest-lived episodes cluster around events our window excludes: Gebele et al. found ~40
   FPMM episodes open beyond 50 minutes, and a non-negligible share past a day, in the *pre-adapter*
   regime -- i.e. when a structural mechanism was absent. **[I]** The Kalshi analogue would be a
   period when the netting or the maker population is impaired: an exchange incident, a listing of a
   new ME series before makers arrive, a settlement-rule surprise. We have not sampled one. Saying
   "arbitrage is dead" is over-claiming; saying **"arbitrage at steady state is dead, and we have no
   evidence about the tail"** is what the data supports.

---

## 2. The ladder result (S3), independently corroborated -- and the real obstacle

I tried to reproduce the cross-market ladder finding from a different angle, and the attempt is
more informative than the result.

**[M] Attempt 1 (naive).** Parse the numeric threshold out of the ticker suffix, treat `-T<num>`
markets in an event as a monotone ladder, test `bid(high threshold) > ask(low threshold)`. Result on
1,102,346 nested pairs: 2,022 gross violations, 604 with depth. **All false.** Two failure modes,
both verified by reading titles:

```
KXLOWTPHIL-26AUG26-T58   "Will the minimum temperature be <58 deg"
KXLOWTPHIL-26AUG26-T65   "Will the minimum temperature be >65 deg"
```

Same suffix letter, **opposite directions**, disjoint rather than nested. And:

```
KXECONSTATCPIYOY-26AUG-T2.0 ... -T4.5   26 legs, mutually_exclusive=1, MECNET
   -> sum(bid) = 77, sum(ask) = 193
```

This is a **partition of discrete CPI readings**, not a ladder at all. The "24c edge with 2,000
contracts of depth" my parser reported was an artifact of treating two disjoint outcomes as nested.

**[M] Attempt 2 (restricted to events where every leg is a numeric `-T` threshold, >= 5 legs).**
521 such events, 907 complete synchronous observations. Even here the direction is not recoverable
from the ticker: `KXARCTICICEMIN-26OCT01` has bids **rising** with threshold (3, 5, 7, 15, 16, 39,
67, 74, 93), i.e. it is `P(X <= t)`, the opposite of the assumed convention. Reading that ladder in
the correct direction by hand: **zero monotonicity violations.** Bids and asks are perfectly
ordered.

**[I] Corroboration, with the emphasis moved.** My independent test reproduces S3's `$0.00`, but the
binding constraint is not thin books -- it is that **the logical link cannot be inferred from ticker
structure, and every naive inference I made produced a 100% false-positive rate.** The `links` table
being empty (0 rows) is therefore not a gap to fill quickly; it is the entire difficulty of S3. Any
future ladder work must parse the rules text or the market title, and must be validated against
hand-checked ladders before a single dollar is risked. A ladder scanner that trusts ticker
conventions will confidently report hundreds of dollars of arbitrage that does not exist.

---

## 3. Kalshi maker economics: does a rebate exist, and can $10k reach it?

**Yes, a positive rebate programme exists, it is open to ordinary members, and it is the single most
important fact in this report.** It also has a ceiling far below the arithmetic that first suggests
itself.

### 3.1 Fees

**[M]** From `series_cache` (13,545 series):

| `fee_type` | series | share |
|---|---:|---:|
| `quadratic` (maker fee = 0) | **13,412** | **99.02%** |
| `quadratic_with_maker_fees` | 130 | 0.96% |
| `quadratic_with_combo_maker_fees` | 3 | 0.02% |

The parent's "makers pay zero on essentially every series" is confirmed. **[I] Fees are not the
obstacle to making. They are the obstacle to *taking*, and the whole arbitrage programme was a
taking programme.**

### 3.2 Two programmes, only one reachable

**[C] Liquidity Incentive Program (open tier)**
([help](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program)):

- Eligible: "most regular U.S. Kalshi members". **Excluded**: Kalshi affiliates and employees,
  holders of a Market Maker Agreement, Introducing Brokers / FCMs and their customers, non-U.S. users.
- Rewards: **$1 - $1,000 per market per calendar day**, paid as a **pool split by each participant's
  share of qualifying liquidity**. Minimum payout $1.00.
- **Target Size**: "more than 100 and fewer than 20,000 contracts" required **on both sides**;
  snapshots where two-sided liquidity misses Target Size on either side do not count.
- Score = `Order Size x Distance Multiplier`, where orders at or better than a Reference Price get
  full credit and further orders are cut by a Discount Factor in [0, 1].
- **[C]** The certified maximum is **$0.005 per contract**, against a taker fee of $0.00204 at 3c --
  i.e. the reward can exceed the entire fee the exchange collects, at prices below 7.74c and above
  92.26c; price band 3c-97c
  ([Navnoor Bawa Research](https://www.navnoorbawaresearch.com/p/kalshi-publishes-one-liquidity-subsidy)).

**[C] Liquidity Provider Program (sealed tier)**
([help](https://help.kalshi.com/en/articles/15410219-liquidity-provider-program)): requires an
executed **Market Maker Agreement**; providers are selected by reverse auction in which they bid the
minimum reward they will accept; cap **$50,000 per series per week**; terms filed as a confidential
appendix. **[I] Not reachable by a solo trader with $10,000.** Ignore it.

### 3.3 Can $10,000 qualify for the open tier? Arithmetic from our own book data

**[M]** Across 224,484 active observations priced 3c-97c:

| | |
|---|---:|
| with >= 100 contracts on **both** sides (the Target Size floor) | **62.0%** |
| with >= 1 contract on both sides | 97.2% |
| median `min(bid_size, ask_size)` | **200** contracts |
| among books already meeting the floor, median two-sided depth | **937** contracts |

**[I] Capital is not the constraint.** A 100-lot two-sided quote locks `100*p + 100*(1-p) = $100` of
collateral regardless of price. $10,000 therefore supports roughly **100 simultaneous two-sided
100-lot quotes** before any fills. The Target Size floor is genuinely retail-accessible -- this is
unusual and worth saying plainly.

**[I] Pool share is the constraint, and I cannot pin it down.** 100 lots is 10.7% of the *median
top-of-book* depth in a qualifying market -- but the LIP scores liquidity across a distance band, not
just the touch, so 10.7% is an **upper bound on your share and probably a large overstatement**. Two
further unknowns I could not resolve from public documents:

- Whether the **$0.005/contract** cap applies per contract of *resting* liquidity or per contract
  *traded*. The difference is roughly two orders of magnitude on a $10k book. The help centre says
  pool-share; the certified schedule says per-contract. I could not reconcile them. **This is the
  single highest-value unknown in this report and should be resolved by reading the certified rule
  or asking Kalshi directly before building anything.**
- **How many markets are in an incentive period at any time.** The help centre says active reward
  periods are marked on individual market pages; it does not publish a count. Our recorder does not
  capture the flag. Without it, "100 markets x $1,000/day" is a fantasy number, not a forecast.

**[I] What can be said honestly:** zero maker fees plus a positive per-contract reward on a
retail-reachable size floor makes passive liquidity provision the only strategy in this project
whose unit economics are *positive before alpha*. That is a genuinely different situation from
arbitrage, where the unit economics were negative before alpha. It does not make it profitable --
see adverse selection, below.

### 3.4 The cost the rebate does not cover

**[C]** The average Kalshi maker still loses money: **-9.64%** average return versus **-31.46%** for
takers; makers buying at >= 50c earned **+2.6%** post-fee with a **33%** standard deviation
(Buergi, Deng and Whelan, "Makers and Takers: The Economics of the Kalshi Prediction Market",
46,282 contracts / 313,972 priced sides, 2021 - Apr 2025;
[PDF](https://www.karlwhelan.com/Papers/Kalshi.pdf),
[CEPR](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market)). **[C]** Bartlett and
O'Hara document VPIN-style toxicity predicting maker losses in single-name markets
([Stanford Law](https://law.stanford.edu/2026/04/21/adverse-selection-in-prediction-markets-evidence-from-kalshi/)).

**[M] Our trade tape independently reproduces their structural finding.** Of 1,155,650 trades,
**705,001 (61.0%) were taker-YES** versus 450,649 taker-NO -- matching the ~61% YES-taking rate
Bartlett and O'Hara report. **[I] A maker who quotes symmetrically is therefore, on average, the
seller of YES to a flow that is 61% one-directional.** Whether that flow is informed or merely
biased determines whether symmetric quoting is a business or a slow bleed, and our 3.6-hour tape
with no settlements cannot distinguish the two. The `marks` table (markout accounting) has **0
rows** -- meaning the project has not yet measured its own adverse selection at all. **That is the
measurement that should come next, and it is cheap: it needs only fills and a reference mid.**

---

## 4. Documented surviving edge sources, assessed for a solo trader with $10,000

| # | edge | real? | requires | reachable at $10k, no colo? |
|---|---|---|---|---|
| a | **Maker rebate / liquidity provision** | **Yes** | LIP eligibility, quote automation, inventory + adverse-selection control | **Yes** -- best candidate |
| b | Favourite-longshot bias | Yes, decaying | patience, hundreds of settlements, maker execution | Partly |
| c | Narrow-category forecasting (weather) | Contested | see section 5 | Doubtful |
| d | Cross-venue arbitrage | Yes, thin | capital on both venues, sub-second loop, rules mapping | Marginal |
| e | Settlement / rules ambiguity | Yes, rare | legal reading, tail risk tolerance | Yes, but not systematic |
| f | Event-driven latency | Yes | colocation-class speed | **No** |

**(a) Market making with the LIP -- the only strategy still standing.** Covered in section 3.
Positive unit economics before alpha (zero maker fee + up to $0.005/contract), a retail-reachable
100-contract two-sided floor, **[M]** 62% of the 3-97c universe already meeting that floor. Risks:
adverse selection (**[C]** average maker -9.64%), and the unresolved pool-share question. **[I]
Realistic scale: low hundreds of dollars per month, not thousands, and possibly negative in year
one while the adverse-selection model is being learned.**

**(b) Favourite-longshot bias.** **[C]** The single most robust finding in the field: buyers of
sub-10c contracts lose >60%; contracts above 70c earn statistically significant positive post-fee
returns; makers buying >= 50c earned +2.6% post-fee at 33% SD (Buergi, Deng and Whelan). But it is
**decaying**: their own psi estimate falls from 0.048*** (2024) to 0.021* (2025). **[M] Our tape
shows why the naive version fails: 32.5% of all contracts traded in our window changed hands at
0-5c** -- inside the price band where the 1-cent fee rounding is a 20%+ tax and where the
Northlake postmortem's "never trade below ~$0.15" rule bites. **[I] The tradeable form is: sell
longshots *as a maker* above 15c, never as a taker.** Note this is the *same* strategy as (a),
executed with a directional tilt, which is an argument for building (a) first. **[C] Also note the
category caveat from arXiv:2602.19520: weather is *over*confident short-term (fat tails
underpriced), so blindly selling weather longshots is reverse-FLB and loses.**

**(c) Weather forecasting.** Section 5. **[I] Verdict: doubtful at this scale.**

**(d) Cross-venue Kalshi vs Polymarket.** **[C]** Typical 2026 pre-cost gaps 1.5-4.5% with windows
of seconds (15-30 s on sports); fees eat 3-5c per pair; tooling is commoditised (open-source
scanners, PredTerminal, Claw Arbs). **[I] The killer is not speed, it is
resolution-criteria divergence** -- two markets that look identical settling differently is not
arbitrage, it is an unhedged bet on wording. That risk is also the only reason residual gaps
survive: the bots that can execute fast are the ones that will not touch fuzzy pairs. **[I] For
$10k split across two venues, after funding both, the working capital per opportunity is small
enough that a single wording loss erases many months of gains.** Not recommended as a first
strategy.

**(e) Settlement-rule and ambiguity edges.** **[I]** Real, and structurally available to a careful
human because it is the one thing that does not compress with competition -- **[M]** our
`rules_docs` table and `series_cache.settlement_sources_json` already give the raw material. But it
is episodic, not systematic: you cannot plan on N opportunities per month. Treat as opportunistic
overlay, never as a sleeve.

**(f) Event-driven latency.** **[C]** Crypto hourly markets: Binance updates ~200 ms, Kalshi
reprices with 3-7 s lag; one bot extracted $271.5k in 30 days from Polymarket latency before dynamic
fees. **[M] Our own best Dutch book was a 25-contract, sub-15-second stale quote in a live soccer
match** -- that is the latency game, and we were only able to *observe* it after the fact at 5-second
polling resolution. **[I] Unreachable. Do not attempt.**

---

## 5. Weather markets specifically

### 5.1 Structure -- confirmed

**[M]** Kalshi daily temperature events are proper 6-slot partitions: four 2-degree interior
brackets plus two open tails. Verified on `KXLOWTPHIL-26AUG26`:

```
-B58.5 "58-59"   -B60.5 "60-61"   -B62.5 "62-63"   -B64.5 "64-65"
-T58   "<58"     -T65   ">65"
```

39 temperature series in the corpus; 156 complete synchronous partition observations across 78
events. Median event open interest **1,847 contracts** (p90 15,763), i.e. **a few thousand dollars
of notional per city-day**.

### 5.2 The market is not mispriced relative to the free forecast; it *is* the forecast

**[M] Implied distribution by horizon** (mid-price normalised across the 6 slots):

| hours to close | n | modal bucket probability | effective buckets `exp(H)` |
|---|---:|---:|---:|
| < 6 h | 39 | **0.966** | 1.22 |
| 6-18 h | 39 | 0.884 | 1.57 |
| 18-36 h | 69 | **0.403** | 4.00 |
| > 36 h | 9 | 0.459 | 3.37 |

**[I] Calibrating that against a Gaussian over the same 6-slot geometry:**

| forecast sd (F) | modal p | effective buckets |
|---:|---:|---:|
| 1.50 | 0.409 | 3.32 |
| 1.75 | 0.373 | 3.80 |
| 2.00 | 0.341 | 4.26 |

The measured 18-36h pair (modal 0.403, eff 4.00) brackets **sd ~1.75 F**, and because off-centre
partitions *depress* the observed modal probability, that is an **upper bound**: implied MAE
`= 0.798 * sd <= ~1.4 F`. **[C] Published NWS day-1 max-temperature skill is coarser than that** --
the NWS 24-hour high is within 3-4 F about 80% of the time
([botforkalshi](https://www.botforkalshi.com/blog/kalshi-weather-trading-strategy)), and NDFD
verification over complex western terrain showed day-1 RMSE of 3.5-4 C
([Weather and Forecasting 21(5)](https://journals.ametsoc.org/view/journals/wefo/21/5/waf946_1.xml)).

**[I] Conclusion: the market's implied distribution is at least as sharp as the free public
forecast, at day-1 horizon.** You are not buying a mispriced NWS number. You are bidding against
someone who already read it, and probably read HRRR and the NBM too. And by <6h -- the horizon where
observations and rapid-refresh models would give a human an advantage -- the modal bucket is already
at **0.966**. There is nothing left to take.

### 5.3 What an edge would actually have to be

**[M] Cost to express a view.** Per-bucket bid-ask spread for buckets priced 3-97c: median **3c**,
p75 6c, p90 9c. Taker fee plus half-spread:

| bucket price | fee | + half-spread | **edge required** |
|---:|---:|---:|---:|
| 10c | 0.63c | 1.5c | **2.13 pp** |
| 20c | 1.12c | 1.5c | **2.62 pp** |
| 30c | 1.47c | 1.5c | **2.97 pp** |
| 40c | 1.68c | 1.5c | **3.18 pp** |

**[I] Translating that into degrees.** With a 2 F forecast sd and 2 F buckets, a forecast shifted by
`delta` from the market's moves the most-affected bucket by:

| delta (F) | max bucket move |
|---:|---:|
| 0.25 | 2.45 pp |
| 0.50 | 5.07 pp |
| 0.75 | 7.80 pp |
| 1.00 | 10.58 pp |

So **you need to beat the market's implied forecast by roughly 0.3-0.5 F, consistently, on the
best-placed bucket, just to break even as a taker.** That is not a large number in absolute terms --
and that is exactly the problem. It is well inside the disagreement between two reasonable
post-processings of the same public data, which means it is **not identifiable**: you cannot tell
your 0.4 F "edge" from your 0.4 F model error without hundreds of settled days.
**[C]** From `research/08`: at delta = 2 points of disagreement you need ~10,000 settled markets to
reach `t = 2`. Kalshi lists on the order of 40 city-days per day. That is **years**.

### 5.4 Free data, and the honest state of it

**[C]** All genuinely free and API-accessible without a key: **api.weather.gov** (NWS official
forecasts and observations; also the settlement source -- Kalshi settles on the official ASOS
station high/low reported by NWS); **NOAA/NDFD** gridded forecasts; **HRRR** (hourly rapid refresh);
the **National Blend of Models** (NBM), NOAA's own calibrated multi-model blend;
**[Open-Meteo](https://open-meteo.com/)** (free for non-commercial use, no key, aggregates GFS/HRRR
and others); and the NOAA [Registry of Open Data on AWS](https://registry.opendata.aws/collab/noaa/).
**[C]** But note: Open-Meteo publishes no independent station-based benchmark at scale, so its
accuracy claims are not independently verifiable
([Jua comparison](https://jua.ai/articles/free-weather-api-comparison-2026/)).

**[I] The data being free is the argument *against* the edge, not for it.** Every competitor has the
same NWS feed on the same schedule. The NBM already is a calibrated blend -- so "blend the models
yourself" is not novel, it is reimplementing NOAA's own product. What would be novel is
**station-specific bias correction against the exact ASOS sensor Kalshi settles on**, which is a
real and under-exploited idea; but its magnitude is on the order of a few tenths of a degree, i.e.
right at the identifiability threshold computed above.

### 5.5 The documented failure

**[C]** Northlake Labs' public postmortem: **0 wins, 32 losses**, a few hundred dollars realised
loss. Causes, in the author's own diagnosis: Gaussian error assumption against fat-tailed reality
("2-sigma" events showing up 10-12% of the time rather than 5%); the 1-cent fee being a 20% tax on
cheap contracts; and a 15-60 minute polling loop against "arbitrageurs that execute within seconds",
leaving him "providing exit liquidity for the bots". He concluded the category was "a technical arms
race with dedicated weather arb infrastructure" and pivoted out
([postmortem](https://www.northlakelabs.com/max/blog/kalshi-weather-postmortem-and-pivot/)).

**[I] Every one of those three failure modes is confirmed by our own measurements** -- the fat tail
by arXiv:2602.19520's "weather is overconfident short-term"; the fee tax by our 32.5%-of-volume-at-
0-5c figure; the latency by our 0.966 modal probability inside 6 hours.

### 5.6 Weather verdict

**[I] There is no documented, replicable, retail-accessible forecast-vs-market edge in Kalshi
temperature markets, and our own data explains why: the market price already encodes day-1 NWS-or-
better skill, the required edge (0.3-0.5 F) is inside model noise, capacity is a few thousand
dollars per city-day, and the one public attempt at it went 0-for-32.** The one honest residual is
station-level ASOS bias correction, which should be treated as a research question with a
multi-season evaluation horizon, not as a strategy.

---

## 6. The honest bottom line

**What the evidence supports.** The arbitrage thesis is dead at steady state, for a structural
reason with a closed form: a complete taker Dutch book on Kalshi must clear `7 * (1 - HHI)` cents of
fee, and the exchange's overround does not reach that except in stale-quote windows lasting under
five seconds. The residual is real -- **[M] $44.08 of theoretical maximum over 9 hours, of which
$42.25 is a single WNBA observation** -- but it is a latency prize, and at a 69.8% orphan rate and
~6.1c of unwind per 1.0c of gain, attempting to collect it converts a positive gross into a
reliable negative net. That is not a tuning problem. No leg timeout fixes it.

**Ranked recommendations, by expected value net of effort and risk.**

**1. Do the two cheap measurements that are currently missing, before anything else.** [effort:
days; cost: ~$0; EV: high, because both can save months]
   - **Markout accounting.** `marks` has 0 rows. The project has never measured its own adverse
     selection. Fills plus a reference mid at fixed horizons is a small amount of code and it is the
     *only* thing that distinguishes "market making is a business" from "market making is a slow
     bleed" (**[C]** average maker: -9.64%).
   - **Resolve the LIP payment mechanics.** Whether $0.005/contract is per resting contract or per
     traded contract changes the answer by roughly 100x. Read the certified rule or ask Kalshi. Do
     not build against a guess.

**2. Rebuild the sleeve as a passive maker in LIP-eligible markets, with a favourite-longshot tilt.**
[effort: weeks; realistic EV: low hundreds of $/month, plausibly negative in the first months]
   This is the only strategy whose unit economics are positive before alpha: **[M]** zero maker fee
   on 99.0% of series, a rebate up to $0.005/contract, and **[M]** a 100-contract two-sided floor
   that 62% of the 3-97c universe already meets and that $10k can fund in ~100 markets at once.
   Constraints to respect: quote above 15c (fee rounding), tilt toward selling longshots and buying
   favourites but **not** in weather (**[C]** reverse-FLB), and **verify the collateral-return latch
   does not trap you** (**[C]** enabling it "may make you unable to sell positions"). Size small
   until markouts are positive over hundreds of fills.

**3. Keep the recorder running and widen the window.** [effort: ~0; EV: moderate, as option value]
   Nine hours cannot see the tail. Running through an FOMC, a CPI print, an NFL Sunday and a listing
   event costs nothing and is the only way to learn whether the steady-state result generalises.
   The infrastructure is already built and is the project's main durable asset.

**4. Station-level ASOS bias correction as a research project, not a strategy.** [effort: months;
EV: unknown, probably small] Only worth doing if step 1's markout work is already running and you
want a second thread. Evaluate over seasons, on paper, before risking anything.

**Explicitly not recommended:** cross-venue arbitrage at $10k (wording risk dominates the spread);
event-driven latency (unreachable); any further work on within-event or ladder arbitrage as a
*strategy* -- though the link-verification machinery from section 2 remains necessary
infrastructure for anything that trades multiple legs.

**And the case for "nothing".** It deserves stating rather than being padded around. **[C]** The
average Kalshi maker loses 9.64%; the average taker loses 31.46%; per-conversion arbitrage profit on
the better-studied venue decayed to ~$0.08 by early 2026 with a growing negative tail; the one
public retail weather attempt went 0-for-32. **[M]** The median Kalshi market traded **$11** of
notional in our window (p90 $214). **[I]** Recommendation 2's realistic ceiling is low hundreds of
dollars per month on $10,000 -- against dozens of hours per month of maintenance and a real
probability of loss. If the goal is risk-adjusted return on capital, **doing nothing beats every
option here except recommendation 1**, which is cheap and generates the information needed to make
the decision properly. If the goal is to learn microstructure with a bounded tuition payment, then
recommendation 2 at small size is a reasonable way to buy that education -- but it should be
budgeted as tuition, not as a return.

---

## Sources

**Academic**
- Gebele, Mutzel, Matthes (Aug 2026), *Executable Arbitrage and Market Efficiency in Prediction Markets* -- [arXiv:2608.00666](https://arxiv.org/abs/2608.00666) | [HTML](https://arxiv.org/html/2608.00666v1)
- Cheng, Yang, Zou (Apr 2026), *Arbitrage Analysis in Polymarket NBA Markets* -- [arXiv:2605.00864](https://arxiv.org/abs/2605.00864)
- Saguillo et al. (2025), Polymarket arbitrage extraction -- [arXiv:2508.03474](https://arxiv.org/abs/2508.03474)
- Buergi, Deng, Whelan (2026), *Makers and Takers: The Economics of the Kalshi Prediction Market* -- [PDF](https://www.karlwhelan.com/Papers/Kalshi.pdf) | [CEPR column](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market) | [GWU working paper](https://www2.gwu.edu/~forcpgm/2026-001.pdf)
- Bartlett, O'Hara (2026), *Adverse Selection in Prediction Markets: Evidence from Kalshi* -- [Stanford Law](https://law.stanford.edu/2026/04/21/adverse-selection-in-prediction-markets-evidence-from-kalshi/)
- Oliven, Rietz (2004), *Suckers Are Born but Markets Are Made*, Management Science 50(3) -- [INFORMS](https://pubsonline.informs.org/doi/10.1287/mnsc.1040.0191) | [preprint](https://www.biz.uiowa.edu/faculty/trietz/papers/iemarb.pdf)
- Franck, Verbeek, Nuesch, *Inter-market Arbitrage in Betting* -- [PDF](https://www.wiwi.uni-muenster.de/uf/sites/uf/files/PublikationenNuuesch/2013franck_verbeek_nueesch_2013.pdf)
- *Unraveling Informational Efficiency in UK Horse Racing* -- [arXiv:2402.02623](https://arxiv.org/pdf/2402.02623)
- NDFD surface-temperature verification, western US -- [Weather and Forecasting 21(5)](https://journals.ametsoc.org/view/journals/wefo/21/5/waf946_1.xml)

**Kalshi primary**
- [Fees](https://help.kalshi.com/en/articles/13823805-fees) | [Fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf) (returned HTTP 429 on every attempt this session -- formula taken from secondary sources, see below)
- [Liquidity Incentive Program (open tier)](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program)
- [Liquidity Provider Program (sealed tier)](https://help.kalshi.com/en/articles/15410219-liquidity-provider-program) | [regulatory](https://kalshi.com/regulatory/liquidity-provider-program)
- [Collateral return (MECNET / DIRECNET)](https://help.kalshi.com/en/articles/13823816-collateral-return)
- [Weather markets](https://help.kalshi.com/en/articles/13823837-weather-markets)

**Practitioner / secondary**
- [Navnoor Bawa Research -- Kalshi's two liquidity subsidies](https://www.navnoorbawaresearch.com/p/kalshi-publishes-one-liquidity-subsidy)
- [marketmath.io Kalshi fees](https://marketmath.io/platforms/kalshi) | [pm.wiki Kalshi fees](https://pm.wiki/learn/kalshi-fees-explained)
- [Northlake Labs weather postmortem (0-32)](https://www.northlakelabs.com/max/blog/kalshi-weather-postmortem-and-pivot/)
- [botforkalshi weather strategy](https://www.botforkalshi.com/blog/kalshi-weather-trading-strategy)
- [CNBC -- Cantor opens Kalshi to institutions, Aug 2026](https://www.cnbc.com/2026/08/19/hedge-funds-are-about-to-jump-in-big-to-prediction-markets.html)

**Weather data**
- [api.weather.gov](https://api.weather.gov) | [NDFD](https://vlab.noaa.gov/web/mdl/ndfd) | [Open-Meteo](https://open-meteo.com/) | [NOAA on AWS](https://registry.opendata.aws/collab/noaa/) | [free weather API comparison](https://jua.ai/articles/free-weather-api-comparison-2026/)

**Our own data**: `data/pm.db`, read-only, window 2026-08-26 19:49 UTC to 2026-08-27 04:46 UTC.
