# Platforms for a US (Georgia) Systematic Trader — Research Notes, August 2026

**Georgia headline: no state restrictions — all categories on Kalshi and Polymarket US are live in GA as of Aug 2026** ([CBS Sports state tracker](https://www.cbssports.com/prediction/news/prediction-market-legal-states/)).

## Regulatory timeline (2024 → Aug 2026)

- **2022:** Polymarket pays $1.4M CFTC settlement; agrees to geo-block US users.
- **Nov 2024:** FBI raids Polymarket CEO Shayne Coplan's apartment; DOJ/CFTC probes.
- **Dec 2024–Jan 2025:** Crypto.com then Kalshi launch CFTC-regulated sports event contracts; state cease-and-desists follow through 2025.
- **Jul 14, 2025:** CFTC Letter 25-20 — PredictIt relief amended: per-contract cap $850 → **$3,500**, 5,000-trader cap removed.
- **Jul 15, 2025:** DOJ and CFTC close Polymarket investigations, no charges ([CNBC](https://www.cnbc.com/2025/07/15/polymarket-investigations-doj-cftc-betting-market.html)).
- **Jul 2025:** Polymarket buys QCEX (CFTC DCM+DCO) for **$112M** → QCX LLC dba Polymarket US.
- **Oct 2025–2026:** ICE invests $2B at ~$8–9B valuation, later +$600M ([ICE IR](https://ir.theice.com/press/news-details/2026/Intercontinental-Exchange-Announces-New-600-Million-Investment-in-Polymarket/default.aspx)).
- **Nov 24–25, 2025:** CFTC Amended Order for QCX; Polymarket US app launches (invite-only; waitlist dropped ~May 2026).
- **Dec 2025:** FanDuel×CME launch FanDuel Predicts; DraftKings Predictions launches.
- **Apr 6, 2026:** **Third Circuit affirms Kalshi's injunction vs New Jersey** — sports event contracts likely "swaps" under CEA; CFTC jurisdiction exclusive ([Skadden](https://www.skadden.com/insights/publications/2026/04/third-circuit-affirms-kalshis-preliminary-injunction)).
- **Apr 28, 2026:** Polymarket applies to CFTC to onshore its main global exchange — pending ([Yahoo](https://finance.yahoo.com/markets/crypto/articles/polymarket-seeks-cftc-approval-bring-152113106.html)).
- **May–Jun 2026:** House Oversight insider-trading probe (Kalshi/Polymarket); new CFTC investigation into Polymarket ([CNBC](https://www.cnbc.com/2026/06/26/cftc-is-conducting-an-investigation-into-polymarket-source-says.html)); WSJ finds 1,105 influencer promo videos with $1.9M staged bets; White House reviews CFTC prediction-market rule.
- **Aug 2026:** ~12 states in active litigation (AZ, CA, CT, IL, MD, MN, NY, OH, RI, TN, TX, WI); bans effective NV, MI, UT, WA; MA sports ban. CFTC suing several states.

## 1. KALSHI — primary venue

- **Legality:** CFTC DCM. ~49 states + DC (NV blocked; sports restricted in a few states). **GA: fully available.** KYC: SSN, 18+.
- **Money:** USD; segregated customer funds at FDIC banks.
- **Categories:** Sports (~80–87% of volume), politics, econ (CPI/NFP/Fed), finance (index ranges), crypto, weather/climate, culture, mentions.
- **Fees** ([schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf), 7.7.26): taker `ceil(0.07 × C × P × (1−P))` → max **$0.0175/contract at 50¢**; maker = 25% of taker (~$0.44/100 max), only on fill. No settlement/membership fees. ACH free both ways; debit ~2%.
- **Liquidity:** 2026 YTD ~**$148B** (6.2× all of 2025); record week $8.99B (Jun 2026); ~$14.8B in Apr 2026 alone. Liquid: major sports, Fed/CPI/NFP, S&P/BTC daily ranges, marquee politics. Thin: weather, niche markets.
- **API/bots:** fully permitted, encouraged. REST + WS + **FIX**. RSA-PSS signing. Token-bucket rate tiers: Basic 200r/100w tokens/s → self-serve Advanced 300/300 → volume-earned up to Prestige 10000/8000. Subaccounts, order groups, post_only/reduce_only/self-trade prevention, batch endpoints.
- **Orders:** CLOB; limit/market/IOC/FOK/post-only; 1¢ tick; $1 settle. Position limits per series (commonly ~$25k retail).
- Valuation: $1B raise at $22B (May 2026). Pays ~3.25–4.05% APY interest on cash + open positions (balances ≥$250).

## 2. POLYMARKET — two venues, one brand

**Polymarket US (QCX) and Polymarket Global run separate, unconnected order books; identical events routinely price differently** ([crypto.news](https://crypto.news/polymarket-defi-vs-regulated-exchange/)).

### 2a. Polymarket US (QCX LLC) — legal from GA
- CFTC DCM; intermediated (FCM) model; **USD** (ACH/debit/Apple Pay, $50k/day; wire min $1k); no crypto wallet needed. KYC: photo ID + SSN + selfie.
- Available in 40+ states; ~11 restricted/contested. **GA: available.** 18+.
- **Fees** ([docs.polymarket.us/fees](https://docs.polymarket.us/fees)): fee = Θ × C × p(1−p). Since Jul 1, 2026: taker Θ=0.06 (max $1.50/100); **maker rebate Θ=−0.0125** (max −$0.31/100). Taker-volume rebates: $250k–1M/mo → 10%; $1M–10M → 25%; $10M+ → 50%. Accelerated tiering if you show 30-day volume from another prediction market (poaching Kalshi MMs). Liquidity Provider Program pays fixed weekly stipends ([CFTC filing](https://www.polymarketexchange.com/files/notices/Liquidity%20Provider%20Program%20(2026.03.03).pdf)).
- **Liquidity: thin** — ~$256M in March 2026 (vs Kalshi $14.8B).
- **API:** REST + **gRPC streaming**; **Ed25519 keys** self-served at [polymarket.us/developer](https://polymarket.us/developer); official SDK [Polymarket/polymarket-us-python](https://github.com/Polymarket/polymarket-us-python). Bots permitted.

### 2b. Polymarket Global (offshore) — NOT usable from US
- US persons geo-blocked at order placement. **Do not VPN** — that's the exact conduct behind the 2022 settlement; funds can be frozen. Onshoring application pending.
- Reference: USDC/pUSD on Polygon; hybrid CLOB; Fee V2 (Mar 30, 2026, updated Jul): taker crypto 0.07, sports 0.05, finance/politics 0.04, econ/culture/weather 0.05, geopolitics free; makers zero + rebates. Global volume ~$9–10.5B/mo peak 2026. 14 of top 20 most profitable wallets are bots.

## 3. PREDICTIT
- Operates under CFTC no-action relief (Letter 25-20); run by nonprofit PMRC. US-wide incl. GA.
- Politics only. **$3,500/contract cap.** Fees: **10% on profits + 5% withdrawal** — brutal. **No trading API** (read-only marketdata endpoint). Manual-only alpha pocket; not for automation.

## 4. FORECASTEX / INTERACTIVE BROKERS ("ForecastTrader")
- CFTC DCM+DCO (IBKR subsidiary); access via IBKR account; nationwide incl. GA.
- Econ (CPI, Fed funds, GDP, payrolls), politics/government, climate. No sports.
- **$0.01/contract embedded** (Yes+No = $1.01); zero commission. **Incentive coupons: ~3.13% APY paid on position value** — long-dated positions are carry-positive. Best venue for slow macro theses.
- **Full TWS API support for event contracts** ([IBKR docs](https://www.interactivebrokers.com/campus/ibkr-api-page/event-trading/)). Thin liquidity; patient limit orders only.

## 5. ROBINHOOD EVENT CONTRACTS
- Routes to Kalshi/ForecastEx/Rothera DCMs. ~$0.02/contract round-trip cost. 16B+ contracts YTD 2026 — but **no API, no book feed → disqualifying for systematic trading**. Trade Kalshi directly instead.

## 6. CRYPTO.COM (CDNA, ex-Nadex) + "OG" app
- CFTC-regulated; sports + crypto events; powers Underdog and (per Aug 2026 reports) FanDuel sports execution.
- Retail fees: $0.20/contract open/close; $0.10 ITM settle. **No public retail API** (institutional FIX via Direct Trading Membership only). Skip.

## 7. NEW SPORTSBOOK ENTRANTS (no APIs — monitor only)
- **DraftKings:** acquired Railbird (DCM) Oct 2025; DKeX exchange launched Jun 26, 2026; ~$11.3B annualized volume. No public API.
- **FanDuel Predicts (×CME):** Dec 2025 launch, 2% of payout fee; sports execution moving to Crypto.com's Nadex (Aug 2026). No public API.
- **Underdog:** via Crypto.com, then acquired Aristotle Exchange (Mar 2026). No public API.

## 8. MANIFOLD MARKETS — play money, best API
- Pure play-money Mana (Sweepcash real-money experiment ended Mar 2025). Fully open free API, bots celebrated, WebSocket feed. **Zero-stakes venue to develop bot infrastructure.** Not representative liquidity/calibration.

## 9. METACULUS — forecasting, not a market
- No trading; tournaments with cash prizes (e.g., Bridgewater×Metaculus $30k pool). Good API. **Use as a free probability-signal source** vs Kalshi/Polymarket prices; AI Benchmark Tournament for LLM bots ([bot template](https://github.com/Metaculus/metac-bot-template)).

## 10. INSIGHT PREDICTION — US-blocked; exclude.

## Cross-listing & arbitrage landscape
- Legal US pairs: **Kalshi ↔ Polymarket US** (sports, Fed, CPI, elections, awards); Kalshi ↔ ForecastEx (Fed/CPI/climate + coupon carry); Kalshi ↔ PredictIt (politics, manual only).
- Documented 2026 economics: 1–2¢ routine gaps Kalshi↔Polymarket on NBA/World Cup; occasionally 2–5%. All-in fees ~1.75–2.5¢/contract per pair → need >2¢ gross. Scanners commoditized ([Claw Arbs](https://clawarbs.com/blog/kalshi-vs-polymarket-arbitrage/), [PredTerminal](https://predterminal.com/blog/predterminal-arbitrage-scanner-trade-polymarket-kalshi-price-gaps-2026)).
- **Main non-fee risk: resolution-criteria mismatches** — "identical" markets can resolve differently.

## Platform risks
- **Polymarket:** repeat regulatory target (2022 settlement, 2024 raid, Jun 2026 CFTC probe, House probe, Senate letters); counterweight: ICE billions + DCM status. US funds in regulated QCX structure; Global = smart-contract custody.
- **Kalshi:** state-law war ongoing despite 3rd Cir. win; NY AG suit seeks ~$36B; insider-trading scrutiny on mention markets; CFTC leadership change = systemic tail risk (currently friendly).
- **PredictIt:** exists at CFTC staff's pleasure; 5% exit fee.
- **General:** resolution risk (exchange is final arbiter), position limits, fee schedules changed repeatedly in 2026.

## Bottom line
1. **Kalshi = primary venue** (GA-legal, deepest liquidity, full API/FIX, maker fee 25% of taker).
2. **Polymarket US = second venue + arb leg** (real API, maker rebates, LP stipends; thin books, hotter regulatory spotlight).
3. **ForecastEx/IBKR = macro sleeve** (TWS API, 1¢ embedded fee, ~3.13% coupon).
4. **PredictIt = manual political pocket. Manifold/Metaculus = sandbox + free signal. Robinhood/DK/FD/Crypto.com retail = no APIs. Insight = excluded.**
