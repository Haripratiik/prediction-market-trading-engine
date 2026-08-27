# 11 -- Cross-Venue: Is There an Arbitrage Between Kalshi, Polymarket and the Sportsbooks?

Written 2026-08-27, after the within-Kalshi arbitrage thesis died. File 09 closed by listing
cross-venue arbitrage under "explicitly not recommended," on one line, without evidence. This file
is the test of that line. It asks whether the same real-world event, priced on two venues, leaves a
gap a US retail trader with $10,000 can actually collect.

Tags, as in 09:

- **[M]** MEASURED -- computed by me from `data/pm.db` this session, read-only. Query described inline.
- **[C]** CITED -- someone else's published number, with link.
- **[I]** INFERRED -- my reasoning on top of [M] or [C]. Weakest tier. Treated as such.

**The answer is no, and the reason is not the one file 09 guessed.** File 09 assumed wording risk
would dominate the spread. Wording risk is real and section 2.3 gives it a price tag. But the
binding constraint is simpler and arrives earlier: **the only serious measurement of cross-venue
divergence netted its execution costs against a venue that charged zero trading fees, and that
venue no longer exists for a US person.** The fee regime changed in 2026. Re-priced against the
venue a US trader can actually reach, most of the published gap is already spent.

---

## 0. What changed under this question while nobody was looking

Two structural facts, both from 2026, govern everything below. Neither is reflected in the
literature, because the literature's data ends before them.

**[C] Polymarket is now two exchanges, not one.** The crypto-native `polymarket.com` book settles on
Polygon; Polymarket US is `QCX LLC`, a CFTC-regulated DCM built on the $112M QCEX acquisition in
autumn 2025. They run **separate order books with separate clearing, and "positions, balances and
accounts never transfer between them"**
([crypto.news](https://crypto.news/polymarket-defi-vs-regulated-exchange/),
[CryptoNexa](https://www.cryptonexa.com/polymarket-us-exchange-runs-two-separate-venues-under-one-brand)).
The international book is geoblocked to US persons; the US book is the one a US person may use.

**[C] Polymarket charged no trading fee for its entire history until 2026.** Fees arrived on the
international book 30 March 2026, and the Polymarket US schedule took effect **12:00 AM ET,
1 July 2026** ([Polymarket US fee schedule](https://docs.polymarket.us/fees),
[Polymarket help](https://help.polymarket.com/en/articles/13364478-trading-fees),
[KuCoin](https://www.kucoin.com/blog/polymarket-fees-trading-guide-2026)).

**[I] Together these invalidate the transfer of the published evidence to our situation.** Every
cross-venue measurement in the literature is a measurement of `polymarket.com` against Kalshi,
during the zero-fee era. A US trader in August 2026 faces a *different, thinner, fee-charging*
venue. The published number is not wrong; it is about a trade we cannot place.

---

## 1. Is there documented evidence of persistent cross-venue mispricing?

**Yes -- one good paper, and it is thinner and older than it looks.**

### 1.1 The one serious measurement

**[C]** Gebele and Matthes (TU Munich), *Semantic Non-Fungibility and Violations of the Law of One
Price in Prediction Markets*, 5 January 2026, [arXiv:2601.01706](https://arxiv.org/abs/2601.01706).
Over **100,000 events across ten venues, 2018 to August 2025**. Headline findings:

| finding | value |
|---|---|
| events concurrently listed on more than one platform | **~6%** |
| average execution-aware price deviation on equivalent pairs | **2-4%** |
| Kalshi/Polymarket election case: execution-adjusted spread | **~$0.03 average, up to $0.07 sustained** |
| persistence | "for most of the market lifetime," directionally consistent |

Their stated reasons the gap does not close: liquidity fragmentation (funds cannot be netted across
venues), capital intensity, enforceability, and **semantic non-fungibility** -- the absence of a
shared event identity, so that two venues' "same" market are not the same claim.

Crucially, **[C] their deviations are already execution-aware**: they net "bid-ask spreads, platform
fees, gas costs, tick-size constraints, and slippage." So 2-4% is a *post-cost* number, not a gross
one. That sounds strong. Section 2.2 is why it does not survive re-pricing.

### 1.2 Nothing more recent measures cross-venue

**[C]** The follow-up -- Gebele, Mutzel, Matthes, *Executable Arbitrage and Market Efficiency in
Prediction Markets*, August 2026, [arXiv:2608.00666](https://arxiv.org/abs/2608.00666), already
cited in file 09 -- **does not measure cross-venue arbitrage at all.** It is Polymarket-internal
(NegRisk converter and settlement baskets, $1.12M total estimated profit, level-2 data 14 Apr to
19 May 2026). It cites the January paper as *the* cross-platform reference and moves on.

**[I] So the evidence base for the entire cross-venue thesis is one paper whose data ends in August
2025.** That is a thin foundation, and it predates: the Polymarket US launch, the 2026 fee
introduction on both Polymarket books, and the 2026 influx of arbitrage capital. Any claim that
"2-4% gaps exist today" is an extrapolation across all three.

### 1.3 What the non-academic sources say, and why I am discounting them

The search space for "Kalshi Polymarket arbitrage" is dominated by affiliate content selling
scanners and sportsbook signups -- `clawarbs.com`, `xclsvmedia.com`, `predterminal.com`,
`predictionmarketspicks.com`, `avo.bet`. They quote figures like "2-5% gaps, 1-3% guaranteed after
fees." **[I] I am not citing these as evidence.** They have a direct commercial interest in the
claim, publish no methodology, no sample, and no loss distribution, and the numbers they quote are
suspiciously close to the academic gross figure with the costs quietly removed. Their existence is
weak evidence *against* the thesis rather than for it: a genuinely free 1-3% per trade does not get
advertised to retail via SEO.

**[I] Their existence is also direct evidence of crowding.** A scanner sold to retail is a scanner
pointed at the same 6% of multi-listed events by everyone who bought it.

---

## 2. What it would actually cost a US retail trader

### 2.1 Access -- legal, but not to the venue in the papers

**[C] A US person can legally trade Polymarket US.** It operates as QCX LLC, a CFTC-regulated DCM
with its own clearing house, requires full KYC, dropped its waitlist in May 2026, and is available
in roughly 40+ states; about 11 states restrict it and **Minnesota banned it outright effective
1 August 2026** ([cryptonews](https://cryptonews.com/cryptocurrency/is-polymarket-legal/),
[predscope](https://predscope.com/guide/polymarket-us),
[dropstab](https://news.dropstab.com/research/is-polymarket-legal)).

So the honest answer is *not* "inaccessible." It is worse in a subtler way: **[C] the accessible
venue is the small one.** Polymarket US did **$1.3B in April 2026 against $9B on Polymarket
International** in the same month
([tradetheoutcome](https://www.tradetheoutcome.com/polymarket-vs-kalshi-liquidity-volume-deep-dive-2026/)),
while Kalshi recorded **$9.8B in February 2026** and a $6.0B 30-day rolling figure in March
([Pew Research](https://www.pewresearch.org/short-reads/2026/05/27/trading-volume-on-prediction-markets-has-soared-in-recent-months/),
[quantvps](https://www.quantvps.com/blog/prediction-markets-volume-compared)).

**[I] The trade in the literature is Kalshi vs the $9B book. The trade available to us is Kalshi vs
the $1.3B book -- roughly one seventh the size, and one fifth of Kalshi's.** No VPN, no residency
misrepresentation, and no reading of the international book changes this; those are not options and
are not considered here.

### 2.2 The cost stack, and where the published gap goes

**[C] Both venues now charge the same functional form of taker fee.** Kalshi:
`fee = 0.07 * C * P * (1-P)` dollars (file 09, [Kalshi fees](https://help.kalshi.com/en/articles/13823805-fees)).
Polymarket US: **`Fee = Theta * C * p * (1 - p)`, taker theta `0.06`, maker rebate `-0.0125`, banker's
rounding to the cent** ([docs.polymarket.us/fees](https://docs.polymarket.us/fees), fetched this
session).

**[I] A cross-venue arbitrage pays both.** Buy YES on Kalshi at its ask, buy NO on Polymarket US at
its ask; the position is riskless in payout, so the entire cost is spread plus both fees. Writing
`m` for mids, `s` for spreads, the gap you need is

```
    required gap  =  s_K/2  +  s_PM/2  +  0.07*p(1-p)*100  +  0.06*p(1-p)*100
                  =  s_K/2  +  s_PM/2  +  13 * p(1-p)   cents
```

The combined fee term alone is **3.25c at a 50c price**, 2.08c at 20c/80c, 1.17c at 10c/90c. Against
a published deviation of 2-4c, **the fee bill on its own consumes between a third and all of it,
before a single cent of spread.**

**[I] And that fee did not exist in the paper's sample.** Gebele and Matthes netted "platform fees"
over 2018-2025, when Polymarket's platform fee was zero and only gas applied. Their 2-4% is
therefore a post-cost number computed under a cost regime that ended in 2026. Adding back the
current Polymarket US taker fee -- up to 1.5c/contract -- is not double-counting; it is a cost their
sample genuinely did not contain.

One caveat I must flag. **[C]** The primary fee document states a single taker theta of 0.06. **[C]**
Secondary sources ([Phemex](https://phemex.com/news/article/polymarket-us-introduces-001-taker-fee-on-contracts-32524),
[TechFlame](https://www.techflame.com/article?id=135821&type=1)) claim category-specific thetas --
sports 0.05, crypto 0.07, tech/mentions 0.04, and **geopolitical/economic at theta 0, i.e. free.**
I could not confirm the category table in the primary document and every number below uses the
uniform 0.06. **[I] If the category table is real, the Fed/macro bucket in section 3 gets cheaper by
~1.0c and is the one place worth re-checking against the live schedule before dismissing.**

### 2.3 Settlement mismatch: the hedge that is not a hedge

This is the risk file 09 named without pricing. It is real, it is recent, and it is expensive.

**[C] The Cardi B market, Super Bowl LX halftime, February 2026.** Both venues listed whether Cardi B
would perform. She appeared on stage and danced but it was unclear whether she sang.

- **Kalshi** ruled the outcome ambiguous, invoked its rule to settle at the last traded price before
  the halt, and paid **YES $0.26 / NO $0.74**.
- **Polymarket** resolved the contract **YES at $1.00**.

Reported by [CBS News](https://www.cbsnews.com/news/cardi-b-super-bowl-prediction-market-dispute/),
[FOX Sports](https://www.foxsports.com/articles/nfl/cardi-bs-cameo-in-bad-bunnys-super-bowl-halftime-show-leads-to-dispute-on-prediction-markets),
[Fortune](https://www.fortune.com/2026/02/11/did-cardi-b-perform-at-super-bowl-prop-bet-kalshi-polymarket),
[Front Office Sports](https://frontofficesports.com/cardi-b-is-a-cautionary-tale-for-prediction-markets/)
and [Yahoo Finance](https://finance.yahoo.com/news/cardi-bs-brief-cameo-190103198.html); a trader
filed a federal Commodity Exchange Act complaint over the Kalshi settlement.

**[I] Price the hedge.** Suppose you had found a 3c gap and put $5,000 into the leg that was long
Kalshi YES and long Polymarket NO, entering at a combined 97c per riskless-looking pair -- 5,155
pairs. Settlement:

```
    Kalshi YES leg    -> $0.26   (voided to last trade)
    Polymarket NO leg -> $0.00   (resolved YES)
    ------------------------------------------------
    payout  5,155 * $0.26 = $1,340   against $5,000 deployed
    loss                  = $3,660   on a position with zero modelled risk
```

The mirror-image trader, long Polymarket YES and long Kalshi NO, collected $1.74 per pair. **[I] The
"arbitrage" was a coin flip on whose rulebook you happened to be long, at full size, with the
variance you had assumed away.** At 3c gross per pair the winning trade earns ~$155; **one Cardi B
erases roughly 24 successful arbitrages.**

**[C] The mechanism is structural, not a one-off.** Kalshi settles by exchange rule in a regulated
venue; Polymarket resolves through the UMA oracle on "consensus of credible reporting." The same
divergence shows up in wording, not just adjudication -- Gebele and Matthes document that for the
2024 presidential election **Polymarket resolved on media network calls while Kalshi resolved on
inauguration**, which is a strict subset relation, not a synonym. Further documented wording splits:
the November 2025 shutdown-end ladder resolved on the date OPM *announced* the end rather than the
date the bill was signed, and Polymarket simultaneously listed "Government shutdown by October 1?"
(requires operational suspension) alongside "Federal Appropriations Lapse on October 1?" (pays on
lapse regardless of impact), with the *stricter* question trading *higher*
([OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/kalshi-vs-polymarket-settlement-rules)).

**[I] This is the same problem file 09's section 2 hit inside one venue, one level worse.** Within
Kalshi, `links` has 0 rows and no cross-market equivalence is verified. Across venues there is no
shared identifier at all, the rulebooks are written by different institutions under different
regulators, and the paper's central claim is precisely that this non-fungibility is *why* the prices
never converge. **The gap is not a mispricing you can collect. A meaningful part of it is the market
correctly pricing two different questions.**

### 2.4 Split capital, and the drag

**[I] Cross-venue arbitrage cannot net collateral.** Kalshi's `MECNET`/`DIRECNET` collateral return
operates within Kalshi; Polymarket US clears separately and, per section 2.1, balances never move
between books. Both legs must be fully funded on their own venue. So $10,000 becomes **$5,000 of
working capital per venue**, and every dollar is locked from entry until *both* venues settle --
which they do at different times, on different rules, so the position is not flat when the first
leg resolves. That is the doubled collateral drag, and it is not recoverable by leverage: neither
venue extends any.

**[C] Transfers are slow and unidirectional in practice.** Rebalancing between venues means a
withdrawal, a bank or chain hop, and a deposit under KYC on the receiving side. **[I] For a $10k
book, rebalancing between two exchanges after every few trades is a per-trade cost of the same
order as the edge being chased.**

---

## 3. What our own data says about the overlap

All figures **[M]**, from `data/pm.db` opened read-only as `file:...?mode=ro`.

**Corpus, as recorded at the time of this file:**

```
market_snapshots : 1,951,902 rows | 118,759 distinct tickers
                   2026-08-26 19:49 UTC -> 2026-08-27 13:54 UTC   (18.1 h)
trades           : 1,887,252 rows | $78.7M notional | 85,364 tickers
                   2026-08-27 01:13 UTC -> 2026-08-27 13:54 UTC   (12.7 h)
event_snapshots  : 13,954 distinct events | series_cache: 13,545 series
```

The 09 caveat still governs: this is one Wednesday, no FOMC, no CPI, no NFL Sunday, no election.

### 3.1 The overlap set is small, and it is mostly sports

**[M] Kalshi events by category** (distinct `event_ticker` in `event_snapshots`): Sports 8,625;
Elections 2,165; Entertainment 711; Financials 652; Politics 592; Economics 508; Climate 205;
Crypto 145; Sci/Tech 140; Companies 79; Commodities 60; Mentions 48; everything else under 10.

**[M] Traded notional over the window, mapped through `series_cache`:** Sports $38.2M, Crypto
$28.5M, Exotics $7.7M, Commodities $3.8M, then a cliff -- Elections $103k, Climate $103k, Economics
$90k, Entertainment $83k, Mentions $53k, Financials $49k, Politics $33k.

**[I] Sports and crypto are 85% of the money.** Elections and macro -- the categories the literature
studied, and the ones with the cleanest Polymarket counterparts -- traded **$227k combined, 0.3% of
notional**, in this window. That is a Wednesday with no election and no Fed meeting, so it
understates the tail. But it means the cross-venue thesis, as tested by the papers, points at the
part of Kalshi that is nearly dormant on a normal day.

### 3.2 The cost hurdle, measured on our side

For each bucket of Kalshi series with a plausible Polymarket or sportsbook twin, taking the latest
snapshot per active two-sided market and weighting by `volume_24h`:

| bucket | series | mkts | vol24h (contracts) | vw Kalshi spread | required gap (both venues) |
|---|---:|---:|---:|---:|---:|
| Major-league sports moneylines | 8 | 1,008 | 5,929,814 | 1.28c | **3.94c** |
| Crypto price levels (BTC/ETH) | 11 | 3,965 | 4,142,149 | 2.18c | **3.52c** |
| Sports spreads / totals | 8 | 4,155 | 2,149,264 | 2.57c | **5.43c** |
| Sports futures (title winners) | 7 | 214 | 1,678,622 | 1.11c | **1.96c** |
| US elections / politics | 7 | 153 | 1,342,323 | 1.01c | **2.62c** |
| Fed / macro (FOMC, CPI, jobs) | 8 | 401 | 205,014 | 1.45c | **3.72c** |
| Equity index levels | 3 | 3,780 | 142,404 | 7.95c | **9.36c** |

"Required gap" is `s_K + fee_K + fee_PM` from section 2.2, i.e. **it assumes the Polymarket US book
is exactly as tight as Kalshi's.** Given section 2.1's volume figures that is optimistic; a wider
Polymarket book raises every number in the last column.

**[I] Compare to the literature's 2-4c.** The required gap on the two biggest buckets is 3.5-3.9c
under the optimistic assumption. The published deviation is 2-4c, measured on the *deep*
international book in the *zero-fee* era. The distributions overlap, they do not separate. **There
is no comfortable margin anywhere in this table.**

### 3.3 How much volume could survive a given gap

**[M]** Across all 37.7M contracts of active two-sided 24h volume, share sitting in markets whose
two-venue cost is below a hypothetical gap `G`:

| G | share of volume, PM as tight as Kalshi | share, PM twice as wide |
|---:|---:|---:|
| 1c | 0.0% | 0.0% |
| 2c | 19.9% | 12.3% |
| 3c | 34.7% | 25.5% |
| 4c | 51.6% | 42.6% |
| 5c | 78.1% | 70.5% |

**[I] At the literature's average gap of 3c, two thirds of Kalshi's volume is in markets too
expensive to attempt the trade at all** -- and that is before asking whether a Polymarket twin
exists, whether its wording matches, or whether the gap is there today.

### 3.4 Two findings that cut the *other* way, and I am reporting them as such

**[M] The liquid end of Kalshi is genuinely tight.** Among the 500 most liquid active markets, the
median bid-ask spread is **1c**, 73.4% are at 1c and 85.4% are at 2c or better. The wide medians in
the per-category table (9c on Sports) are an artifact of thousands of dormant tickers. On the names
you would actually trade, Kalshi's side of the cost is close to the tick.

**[M] Kalshi mids barely move, so this is not a latency race.** Across 409,072 sixty-second windows
in the 300 most liquid markets:

```
    |delta mid| over 60s :  median 0.00c   p75 0.00c   p90 0.00c   p95 0.50c   p99 1.50c
    share of 60s windows moving >= 1c :  3.1%
    share moving >= 3c                :  0.4%
```

**[I] This matters and it is the strongest point in favour of the thesis.** A 3c cross-venue gap
cannot be explained away as stale-quote noise the way the within-Kalshi residual could -- in file 09
the surviving Dutch books were 83% resolvable in under five seconds, driven by in-play repricing.
Here, if a 3c gap between Kalshi and Polymarket US persisted for a minute, it would be a real,
stable dislocation. **You would not need a low-latency system to capture it.** That is precisely why
the measurement in section 5 is cheap.

**[M] Depth is not the binding constraint at $10k, for the deepest names only.** Among the top 100
cross-venue-plausible markets by volume, median two-sided top-of-book depth is **1,697 contracts**
(p25 298, p75 18,824). $5,000 at 50c needs 10,000 contracts; **26 of the top 100 have that at top of
book**, 62 have at least 1,000 contracts, and 7 have under 10. **[I] So a $10k book can be deployed
in the deep names without walking the book, but the tradable universe is dozens of markets, not
thousands.**

---

## 4. Sportsbook vs Kalshi

**[I] The framing "sportsbook arbitrage" is wrong, and the wrongness is the whole answer.** A true
arb needs both legs simultaneously at a locked combined price. Against a house-banked sportsbook you
are not trading against a book you can also sell into -- you are taking a price the house chose to
offer. What people call Kalshi-vs-sportsbook arbitrage is really **+EV betting at a sportsbook using
Kalshi's price as fair value.** That distinction determines everything below.

**[C] Kalshi's sports pricing is genuinely competitive.** A Citizens Capital Markets and Advisory
report found Kalshi beat online sportsbooks on March Madness pricing by **two to three tenths of a
percentage point of vig on average**, though not uniformly -- Kalshi's vig on the First Four was
**4.9% versus a 4.76% sportsbook standard**
([Gaming America](https://gamingamerica.com/news/1056916/citizens-report-kalshi-beat-online-sportsbooks-march-madness-pricing);
the page returned HTTP 403 to direct fetch, figures via search summary and secondary coverage).
**[C]** AGA's 2025 tracker puts US sportsbook hold on NFL/NBA at **6.5-8.5% revenue-to-handle**.

**[I] Read that carefully: it says the edge runs the wrong way for arbitrage.** If Kalshi is the
*cheaper* venue, then the sportsbook is the one with the worse price, and the trade is to bet at the
sportsbook only when it is offering an outlier. That is a stale-line hunt, not an arbitrage, and it
puts every dollar of the trade on the venue that can refuse you.

**[C] And it will refuse you.** Sportsbooks prohibit arbitrage in their terms and restrict accounts
that show it. An industry insider account documents limiting **"within just hours of signing up,"**
triggered by closing-line value rather than realised profit -- so being flagged does not require
having won yet -- with post-flag limits of **"$50 on any major market, or more than $15 on any prop
market"** on the author's own account, against a Wyoming Gaming Commission claim that limits were
"$500 or $1,000"
([How Gambling Works](https://howgamblingworks.substack.com/p/the-truth-about-limits)).

**[I] Put those together for a $10,000 book.** Even granting a 2% edge per bet, a $50 major-market
limit yields **$1 per bet**. Sustaining $10k of turnover requires 200 bets. There is no bet-sizing
schedule that makes this a business, and the countermeasures -- multiple accounts, misrepresenting
identity, betting through others -- are fraud and are not on the table. **[I] Kalshi's real
advantage over sportsbooks is that it will not limit you, which is an argument for trading on
Kalshi, not an argument for an arbitrage between them.**

**Evidence quality, stated plainly: this section is the weakest in the file.** There is no
peer-reviewed measurement of Kalshi-vs-sportsbook edge. The limiting evidence is one credible
first-person industry account plus a uniform chorus of affiliate blogs with commercial interest in
the opposite conclusion. The structural argument does not depend on the numbers, but the numbers
should not be leaned on.

---

## 5. The bottom line

### 5.1 The verdict

**[I] No. Cross-venue arbitrage is not worth building for someone with $10,000, and it is a worse
proposition than the within-Kalshi arbitrage it was meant to replace.**

The case, in the order the obstacles arrive:

1. **The evidence is one paper, and its sample ends before the world changed.** [C] 2-4% deviations,
   2018 to August 2025, measured against a $9B book that charged no trading fee -- neither of which
   a US person can reach in August 2026.
2. **The fee alone eats a third to all of the published gap.** [I] `13 * p(1-p)` cents combined,
   3.25c at a 50c price, against a 2-4c deviation.
3. **Our own side is already too expensive.** [M] Required gap 3.5-3.9c on the two largest buckets
   under the optimistic assumption that Polymarket US quotes as tight as Kalshi; [M] at a 3c gap,
   65% of Kalshi volume is out of reach on cost.
4. **The accessible venue is one seventh of the one that was studied.** [C] $1.3B vs $9B, April 2026.
5. **Capital halves and the drag doubles.** [I] No cross-venue netting; $5,000 per side, locked to
   the later of two different settlements.
6. **A meaningful part of the remaining gap is not a mispricing.** [C] Cardi B, network-call vs
   inauguration, OPM-announcement vs bill-signing. [I] One settlement mismatch at $5,000 costs
   ~$3,660 and erases ~24 successful trades.
7. **The overlap is thin where it is liquid and liquid where the papers did not look.** [M] Sports
   and crypto are 85% of our notional; elections and macro together were 0.3% on a normal Wednesday.

**[I] Item 6 is the one that should end the discussion.** Items 1-5 make the edge small. Item 6 makes
the loss distribution left-tailed in exactly the place the strategy claims to have no risk at all. A
strategy whose worst case is "I was hedged" is not an arbitrage.

### 5.2 The one thing that argues for looking anyway

Honesty requires stating the counter-case. **[M] Kalshi mids move less than 1c in 96.9% of
sixty-second windows, and its liquid markets quote 1c wide.** This is not the within-Kalshi
situation, where the residual lived and died inside five seconds and was structurally
uncollectable. If a cross-venue gap exists, it would sit still long enough for a human to trade it.
**[I] That makes the *observation* cheap even though the *strategy* is bad -- and it means the
question can be settled with data rather than argument.**

### 5.3 The smallest experiment, ranked by information per dollar

**1. Observe. Cost: $0. Capital at risk: $0.** [effort: a day or two; EV: high, because it is
decisive]

**[C] Polymarket US publishes public, unauthenticated market data**: `Get Market BBO`,
`Get Market Book`, `Get Markets`, `Search`, plus a public Markets WebSocket for order book and
trades ([docs.polymarket.us](https://docs.polymarket.us/llms.txt)). No KYC, no funding, no account
needed to *read*.

So: extend the existing recorder to poll Polymarket US BBO for a hand-matched list of **30 to 50
markets** drawn from the buckets in 3.2 -- the deep sports moneylines, the BTC/ETH ladders, the Fed
decision -- alongside the Kalshi quotes already being captured. Then measure the one quantity that
decides everything:

```
    gap(t)  =  (PM_US yes_bid - Kalshi yes_ask)   and its mirror
    net(t)  =  gap(t) - s_K/2 - s_PM/2 - 13*p(1-p)/100
```

**The decision rule, set before looking:** if `net(t) > 0` does not occur on a material fraction of
observations, with executable depth, for longer than it takes to click twice -- the thesis is dead
and costs nothing further. Section 3.4 says a real gap would persist; so if nothing persists, there
is nothing there.

**[I] This is the highest information-per-dollar action available anywhere in the project right
now**, because the denominator is zero and the numerator settles a question file 09 could only
assert.

**2. Hand-verify the rulebooks on whatever survives. Cost: $0.** [effort: hours]
For any pair that shows a persistent net gap, read both contracts' settlement language side by
side before anything else. **[I] Given section 2.3, the prior should be that a persistent gap is
evidence of a wording difference, not of a mispricing.** A gap that survives step 1 and *also*
survives a rulebook read is the only thing worth funding. I expect very few, and that expectation
is itself the point: this step is cheap and it is where the thesis most likely dies.

**3. Only then, and only if 1 and 2 both pass: one funded pair at minimum size.** [cost: a few
hundred dollars, budgeted as tuition, not as return]
Not $10,000. One market, both legs, small enough that a Cardi B outcome is an anecdote rather than a
drawdown. The purpose is to measure fill quality, actual fee arithmetic, and settlement timing
mismatch -- not to make money.

**Not recommended at any size:** sportsbook-vs-Kalshi (section 4 -- the limits make it
unscalable and the countermeasures are fraud); any build-out of cross-venue execution
infrastructure before step 1 returns; and rebalancing capital between venues as a routine
operation.

### 5.4 How this changes file 09's recommendations

**[I] It does not.** File 09 ranked markout accounting and LIP mechanics first, passive making
second, and keeping the recorder running third. Nothing here displaces those. Step 1 above is a
cheap addition to recommendation 3 -- the recorder is already running, and pointing it at a second
public feed costs a day. **[I] File 09's one-line dismissal of cross-venue arbitrage was directionally
right for the wrong reason: it named wording risk, which is real but second; the fee regime change
and the venue split are what actually kill it.** The conclusion stands, better evidenced.

---

## Sources

**Academic**
- Gebele, Matthes (Jan 2026), *Semantic Non-Fungibility and Violations of the Law of One Price in Prediction Markets* -- [arXiv:2601.01706](https://arxiv.org/abs/2601.01706) | [HTML](https://arxiv.org/html/2601.01706v1). The only serious cross-venue measurement; sample ends Aug 2025.
- Gebele, Mutzel, Matthes (Aug 2026), *Executable Arbitrage and Market Efficiency in Prediction Markets* -- [arXiv:2608.00666](https://arxiv.org/abs/2608.00666). Polymarket-internal only; confirms the above is the cross-platform reference.

**Venue primary**
- [Polymarket US fee schedule](https://docs.polymarket.us/fees) -- `Fee = Theta * C * p * (1-p)`, taker theta 0.06, effective 1 Jul 2026
- [Polymarket US API index](https://docs.polymarket.us/llms.txt) -- public BBO / book / markets endpoints, Markets WebSocket
- [Polymarket trading fees help](https://help.polymarket.com/en/articles/13364478-trading-fees)
- [Kalshi fees](https://help.kalshi.com/en/articles/13823805-fees) -- `0.07 * C * P * (1-P)`

**Settlement divergence (Cardi B, Feb 2026)**
- [CBS News](https://www.cbsnews.com/news/cardi-b-super-bowl-prediction-market-dispute/) | [FOX Sports](https://www.foxsports.com/articles/nfl/cardi-bs-cameo-in-bad-bunnys-super-bowl-halftime-show-leads-to-dispute-on-prediction-markets) | [Fortune](https://www.fortune.com/2026/02/11/did-cardi-b-perform-at-super-bowl-prop-bet-kalshi-polymarket) | [Front Office Sports](https://frontofficesports.com/cardi-b-is-a-cautionary-tale-for-prediction-markets/) | [Yahoo Finance](https://finance.yahoo.com/news/cardi-bs-brief-cameo-190103198.html)
- [OddsShopper on settlement-rule splits](https://www.oddsshopper.com/articles/prediction-markets/kalshi-vs-polymarket-settlement-rules) -- shutdown ladders, appropriations-lapse wording

**Venue structure, access, volume**
- [crypto.news -- one Polymarket, two exchanges](https://crypto.news/polymarket-defi-vs-regulated-exchange/) | [CryptoNexa](https://www.cryptonexa.com/polymarket-us-exchange-runs-two-separate-venues-under-one-brand)
- [cryptonews -- Polymarket US legality Aug 2026](https://cryptonews.com/cryptocurrency/is-polymarket-legal/) | [PredScope](https://predscope.com/guide/polymarket-us) | [dropstab](https://news.dropstab.com/research/is-polymarket-legal)
- [Pew Research -- prediction market volumes, May 2026](https://www.pewresearch.org/short-reads/2026/05/27/trading-volume-on-prediction-markets-has-soared-in-recent-months/) | [quantvps volume comparison](https://www.quantvps.com/blog/prediction-markets-volume-compared) | [tradetheoutcome liquidity deep dive](https://www.tradetheoutcome.com/polymarket-vs-kalshi-liquidity-volume-deep-dive-2026/)

**Sportsbooks**
- [Gaming America -- Citizens Capital Markets on Kalshi vs sportsbook March Madness pricing](https://gamingamerica.com/news/1056916/citizens-report-kalshi-beat-online-sportsbooks-march-madness-pricing) (HTTP 403 on direct fetch)
- [How Gambling Works -- The Truth About Limits](https://howgamblingworks.substack.com/p/the-truth-about-limits)

**Discounted, listed for completeness** -- affiliate/scanner content with commercial interest in the arbitrage claim, cited nowhere above as evidence: clawarbs.com, xclsvmedia.com, predterminal.com, predictionmarketspicks.com, avo.bet, tech-insider.org.

**Our own data**: `data/pm.db`, opened read-only via `file:...?mode=ro`, window 2026-08-26 19:49 UTC to 2026-08-27 13:54 UTC.

---

## ADDENDUM -- the fee-coefficient flag, resolved by direct measurement (2026-08-27)

The report flagged one open question as worth re-checking: the primary fee doc states a uniform
taker `theta = 0.06`, while secondary sources claimed category-specific thetas including
**theta = 0 (free) for geopolitical/economic** markets. If true, the Fed/macro bucket would be
about 1.0c cheaper and would be the one place cross-venue might still work.

**It is not true.** Polymarket US publishes `feeCoefficient` directly on the market object, and the
public gateway serves it without authentication:

    GET https://gateway.polymarket.us/v1/markets?limit=500&active=true&closed=false   -> 200

Measured across 500 ACTIVE markets spanning sports (440), politics (44) and culture (16):

    feeCoefficient distribution: {'0.06': 500}

Uniform 0.06, no exceptions, no category variation. The secondary sources are wrong or stale. So
the combined two-venue fee stands at `13*p(1-p)` cents -- **3.25c at 50c** -- in every category,
and no bucket gets cheaper. **The report's conclusion is unweakened.**

Two corrections to the report's own method notes, from the same check:

1. The report gave `api.polymarket.us` as the public host. That host returns
   `401 Missing required API key headers`. The correct public base is
   **`https://gateway.polymarket.us/v1`**, whose OpenAPI spec carries `security: []`. Verified
   live: `/markets` and `/markets/{slug}/bbo` both return 200 with no credentials.
2. The zero-cost observation experiment the report proposes is therefore REAL and reachable --
   but at the corrected host. `/markets/{slug}/bbo` returns `currentPx`, `lastTradePx` and
   `settlementPx`, which is enough to compute the cross-venue gap without funding anything.

Scale note, measured the same way: the active Polymarket US universe visible through this endpoint
is dominated by sports, against Kalshi's 107,216 markets. The overlap to test is small and already
concentrated in exactly the categories where the required gap (3.94c sports) is widest.
