# Risk, Bankroll, Tax & Regulatory — Research Notes, August 2026 (Georgia resident)

## 1. Bankroll / Kelly math
- Binary contract at price c, your probability p: **f* = (p − c) / (1 − c)**. Example: market 40¢, you say 55% → full Kelly = 25% of bankroll — which is why nobody runs full Kelly.
- **Estimation error:** betting 2× true Kelly → zero growth. You only trade when you disagree with the market (winner's curse), so measured edges overstate true edges. **Half Kelly ≈ 75% of growth at half the volatility.**
- **Drawdown math** (fraction c of full Kelly → P(ever hitting x of bankroll) ≈ x^(2/c − 1)): full Kelly halves 50% of the time; half Kelly 12.5%; quarter Kelly ~0.8%.
- **Correlated markets:** same-underlying contracts are one position — sum deltas, size the aggregate. Rules: >15–20% of portfolio on one underlying = concentrated; halve Kelly size when correlated with existing holdings; assume tail correlations 2–5× normal estimates.
- **Community practice:** quarter-to-half Kelly; hard cap 2–5%/position; ~30% cash reserve; circuit breakers (−20% from peak → halve sizes; −40% → stop); **be a maker** (dataset: takers −1.12%/trade vs makers +1.12%).
- Sources: [Prediction Hunt Kelly guide](https://www.predictionhunt.com/blog/prediction-market-position-sizing-kelly-criterion), [Prevayo](https://www.prevayo.com/blog/kelly-criterion-prediction-markets-complete-guide-2026).

## 2. Realistic expectations
- **Fee floor:** Kalshi taker 1.75¢ max @50¢, maker 25% of that; as a taker at mid prices you need >~2¢ true edge to break even. Cross-platform gaps typically 1.5–4.5¢ lasting 2–7 seconds.
- **Variance:** per-contract SD ≈ √(p(1−p)) ≈ ±50¢ at 50¢. Bets to validate an edge at 95% confidence: 5¢ edge → ~400 bets; 2¢ → ~2,500; 1¢ → ~10,000. A hundred trades proves nothing.
- **Base rates:** ~70–84% of Polymarket wallets lose; <0.04% capture >70% of the $3.7B realized profits; 84.1% in the red across 2.5M wallets, 2% ever made >$1k ([Yahoo/CoinDesk](https://finance.yahoo.com/news/70-polymarket-traders-lost-money-192327162.html), [KuCoin](https://www.kucoin.com/news/flash/84-of-polymarket-traders-are-losing-money-0-033-capture-majority-profits)). Winners are predominantly automated MM/arb operations. Academic: Akey et al., "Who Wins and Who Loses in Prediction Markets?" (SSRN 6443103).
- **Capacity:** headline volume is sports-concentrated; niche books have hundreds-to-thousands near the touch; strategies working at $500/position often fail at $20k.

## 3. US taxes (verified Aug 2026 — still NO IRS guidance)
Four practitioner positions ([Green Trader Tax, Apr 2026](https://greentradertax.com/prediction-market-taxes-capital-gains-gambling-or-something-else/), [Camuso CPA](https://camusocpa.com/section-1256-prediction-market-tax/)):
1. **Capital gains (§1221)** — common practitioner default for regulated platforms; Form 8949/Sch D; losses offset gains + $3k/yr; mostly short-term anyway.
2. **§1256 60/40** — aggressive; argument: Kalshi/QCX are CFTC DCMs; counter: DCM status ≠ "regulated futures contract," and §1256(b)(2)(B) excludes swaps (irony: Kalshi argues in court its contracts ARE swaps). If taken, file **Form 8275 disclosure**. $50k gain: ≈$13.4k tax under 60/40 vs $18.5k at 37% ordinary.
3. **Gambling (§165(d))** — now the WORST option: OBBBA (effective 2026) caps loss deductions at **90% of losses**; a break-even $100k/$100k year creates $10k phantom income; FAIR BET Act repeal stalled (Jan 2026). Pro-gambler Schedule C adds 15.3% SE tax.
4. **Ordinary income** — most conservative.
Pick one reasonable characterization, apply consistently, document.

**Forms:** Kalshi issues **no 1099-B for trades** (self-report from PnL statement); 1099-INT for ≥$10 interest; 1099-MISC ≥$2,000 bonuses. Polymarket global/on-chain: no forms; every trade = contract gain/loss + USDC disposal layer; answer the 1040 digital-asset question truthfully. Polymarket US (FCM channel): reporting practice still developing. **Wash sales:** likely out of scope for event contracts, unconfirmed — don't harvest aggressively. **Records:** export trade histories monthly (platforms truncate); screenshot market rules on disputes. **Estimated taxes:** quarterly 1040-ES if owing ≥$1k.

**Georgia:** flat **4.99% for 2026** (HB 463); no capital-gains preference; piggybacks federal AGI (90% gambling haircut flows through).

**Kiddie tax:** dependent full-time students under 24 — unearned income above **$2,700 taxed at parents' marginal rate**. A dependent netting $10k could pay 32–37%, not 10–12%. Also affects FAFSA.

## 4. Regulatory landscape (Aug 25, 2026)
- **Federal: strongly pro-PM.** CFTC Chair Selig; withdrew restrictive Biden-era proposals; Staff Advisory 26-08 (Mar 2026) blessing event contracts incl. sports; **NPRM Jun 10, 2026** (comments closed Jul 27): three-step test; permits sports outcomes; disfavors injuries/officiating/in-game actions/pre-collegiate; bans terrorism/assassination/war contracts. CFTC is **suing states** (IL, CT, NY, MN — won preliminary injunction vs MN's felony ban Jul 27, 2026; injunction vs AZ).
- **States: split courts.** Kalshi wins: NJ (3rd Cir.), TN. Losses: NV, MA, MD, OH, **NY (PI denied 7/7/2026; NY AG suing for ~$36B)**, CT, UT (court held CEA does NOT preempt — 10th Cir. appeal), AZ, WA, MI. Tracker: [Bransfield](https://mickbransfield.com/2025/08/11/summary-of-legal-actions-involving-kalshis-sports-event-contracts/).
- **Georgia: no regulator action; everything available.** Only case: *Georgia Gambling Recovery LLC v. Kalshi* (M.D. Ga.) — private gambling-loss-recovery suit; doesn't restrict access. (GA's recovery statute is a residual tail risk if courts ever deem this gambling.)
- **Position-voiding risk reduced but real:** Michigan TRO → Kalshi filed emergency rule to force-liquidate MI users (7/12/2026); **CFTC stayed it and invoked CEA §8a(9) ordering Kalshi to honor all trades** (7/14/2026). Precedent protects open positions, but mechanism is proven and a future CFTC could flip. NPRM could bar categories you hold. 18+ both venues.

## 5. Platform/counterparty risks
- **Kalshi custody:** segregated accounts at FDIC banks (pass-through covers bank failure, not Kalshi fraud); Kalshi Klear = CFTC-registered clearinghouse. Critique: clearing-member assessments up to 550% of deposits ([Navnoor Bawa](https://www.navnoorbawaresearch.com/p/kalshis-clearing-members-can-be-assessed)).
- **Polymarket:** US arm = FCM custody USD. Offshore = self-custody + Polygon contracts + pUSD (USDC-backed; SVB depeg precedent $0.87; Circle can freeze addresses).
- **Resolution/oracle risk — biggest under-appreciated risk:**
  - Zelenskyy suit market (Jul 2025): $160–240M volume resolved NO despite BBC calling it a suit; ~95% of UMA voting power whale-held.
  - Strategy BTC-sale dispute (2026): WSJ — >half of disputed-market votes from 10 largest wallets; ~1/5 of disputes involve financially-conflicted voters.
  - **Polymarket US resolves under CFTC exchange rules, not UMA — real advantage.**
  - **Kalshi resolution is centralized:** internal team decides; Outcome Review Committee is only appeal; $54M Khamenei market controversy → class action. **Read the full rulebook of every market; ambiguity resolves against you.**
- **Manipulation:** a few hundred dollars moves thin books 5–10¢; whales paint prices ahead of resolution votes; Kalshi opened ~200 investigations/yr.

## 6. Account risks
- **One Kalshi account per person, ever**; prohibited: wash trading, spoofing, coordinated trading, trading on confidential source info (if a market settles on data your employer/lab produces — don't trade it).
- **APIs/bots explicitly allowed on Kalshi**; latency arb not prohibited. KYC: SSN required; head-account arrangements and VPN-from-banned-state are classic freeze triggers. Withdrawal freezes after large fast profits are common — keep funding documentation.

## 7. Student-specific
- **F-1 visa: the single biggest risk if applicable.** Passive investing OK; systematic frequent trading can be construed as unauthorized employment/business activity; gambling characterization is worse. No bright-line threshold; consequences (status/OPT/green card) dwarf trading profits. NRA tax: flat 30% on gambling winnings, no loss offset, first 5 calendar years. **Consult an immigration attorney before trading systematically.** (US citizen/green card: ignore.)
- **Dependent US-citizen student:** kiddie tax above $2,700; FAFSA impact.
- Georgia Tech: no known policy, but codes of conduct + lab NDAs matter for markets tied to information you touch.

## Key takeaways
1. Quarter-to-half Kelly, 2–5% position cap, 15–20% per-theme cap; expect 12.5% chance of a 50% drawdown even at half Kelly with real edge.
2. Assume true edge ≈ half of estimated; thousands of trades to prove a 2¢ edge; be a maker.
3. Prefer Kalshi + Polymarket US (USD, CFTC venues, position-honoring precedent) over offshore Polymarket (UMA whale risk, VPN/ToS risk, dual-layer crypto taxes).
4. Taxes: no IRS guidance; capital-gains characterization is the common default; §1256 aggressive (Form 8275); gambling treatment now has the 90% haircut; GA flat 4.99%; kiddie tax if dependent; quarterly estimates.
5. Watch: CFTC NPRM finalization; NY/2nd Cir. + 6th Cir. + 10th Cir. appeals (preemption split → possible SCOTUS).
6. F-1 → immigration counsel first.
