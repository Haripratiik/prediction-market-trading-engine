# Systematic Strategies & Documented Edges — Research Notes, August 2026

## Context
- Kalshi taker fee peaks 1.75¢/contract at 50¢; maker = 25% of taker. Polymarket fee V2 (2026): takers pay by category (crypto 0.07 coeff, sports, finance/politics…), makers zero + rebates.
- **Institutionalization is the defining 2026 trend:** Susquehanna (SIG) = Kalshi's flagship market maker (dedicated desk since 2023); Jump Trading doubled its PM team to ~20 and took equity in both venues; **Cantor Fitzgerald opened Kalshi block trading to ~3,000 institutional clients Aug 2026** ([CNBC](https://www.cnbc.com/2026/08/19/hedge-funds-are-about-to-jump-in-big-to-prediction-markets.html), [CoinDesk](https://www.coindesk.com/markets/2026/08/19/cantor-opens-kalshi-prediction-markets-to-thousands-of-institutional-clients)). Edges are compressing.

## 1. Favorite-longshot bias (FLB) — best-documented structural edge

**Bürgi, Deng & Whelan (UCD, Jan 2026), "Makers and Takers: The Economics of the Kalshi Prediction Market"** — 46,282 Yes contracts / 313,972 priced sides, 2021–Apr 2025 ([PDF](https://www.karlwhelan.com/Papers/Kalshi.pdf), [CEPR column](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market)):
- Buyers of contracts <10¢ lose **>60%**. Contracts >70¢ earn statistically significant positive post-fee returns (95¢ wins ~98% → +3.1% pre-fee).
- **Makers −9.64% avg vs Takers −31.46%. Makers buying ≥50¢ earned +2.6% post-fee, std dev 33%.**
- FLB largest in crypto (ψ=0.058) and single-contract markets; weakening over time (2024 ψ=0.048*** → 2025 ψ=0.021*).
- Why not competed away: tiny capacity (median market ~$8,982 total staked; top-decile avg final volume $526k; mean transaction $100), variance (33% SD vs 2.6% mean), ignorance.

**Domain calibration (arXiv:2602.19520, 292M trades both platforms)** ([HTML](https://arxiv.org/html/2602.19520v1)):
- **Politics: UNDERconfident** — a 70¢ political contract a week out is really ~83% → buy political favorites.
- **Sports: well-calibrated 0–48h**; underconfident beyond 1 month (slope 1.74) → long-dated favorites cheap.
- **Weather: OVERconfident short-term** (fat tails underpriced) — reverse-FLB. Don't blindly sell weather longshots.
- **Crypto & finance: near-perfectly calibrated** (hedgeable reference assets → no free lunch).
- **FLB worsens with time-to-expiry** (slope 0.99 <1h → 1.32 >1mo). Recalibration: p* = p^θ / (p^θ + (1−p)^θ).

**Bartlett & O'Hara 2026 (41.6M trades): structural NO-side bias** — in single-name "Will [person] do X" markets, traders buy YES ~61% of the time but YES resolves true only ~32% ([Stanford Law](https://law.stanford.edu/2026/04/21/adverse-selection-in-prediction-markets-evidence-from-kalshi/), [SSRN](https://papers.ssrn.com/sol3/Delivery.cfm/6615739.pdf?abstractid=6615739&mirid=1)).

**Exploitation:** sell longshots / buy favorites **as a maker**; prefer crypto/single-name/long-horizon-politics categories; avoid sub-15¢ "fee death zone." Low-single-digit % returns, fat left tail, capacity low-five-figures.

## 2. Cross-platform arbitrage
- 2024: Trump premium up to ~6pts Polymarket vs Kalshi. 2026: typical pre-cost gaps **1.5–4.5%, windows seconds** (15–30s on sports); occasional fat outliers persist hours–days on illiquid politics ([Polyflux](https://polyflux.io/blog/polymarket-arbitrage/)).
- **Fees eat 3–5¢ per pair**; 4¢ gross often net-negative. Capital pre-funded both venues ($10k+ serious). Risks: thin top-of-book, **resolution-criteria divergence** (the killer), UMA settlement timing (global book), capital lockup.
- Tooling commoditized (open-source scanners, PredTerminal, Claw Arbs). Residual alpha: long-tail unmapped events, "fuzzy" arbs with wording risk (= informed RV, not arb).

## 3. Intra-platform arbitrage
- YES+NO < $1 buy-both; mint-and-sell-both when > $1. **NegRisk multi-outcome:** ΣYES < $1 buy all; ΣYES > 100% buy all NO + convert (industrialized — an 8-wallet operation harvests across 10,000 markets).
- **Saguillo et al. (arXiv:2508.03474): ~$40M of arb extracted on Polymarket Apr 2024–Apr 2025** across >7,000 markets ([abs](https://arxiv.org/abs/2508.03474)).
- **UCLA NBA microstructure study (arXiv:2605.00864), Feb–Mar 2026, 75M book snapshots:** single-market arb virtually extinct (7 episodes/month, median 3.6s, ~$210 total); combinatorial (ML vs spread) 290 episodes/mo, median 101bps, **average executable size 14.8 shares, $559.59/month total capped profit across 173 games**. 81% of raw signals were phantom post-game artifacts. Residual inefficiency "structurally confined to the retail tier."
- 2024 election: deviation half-lives collapsed to **0.67–0.74 min**; Kyle's λ fell ~50× ($1M moved ~0.25pp by Oct) (arXiv:2603.03136). Note: raw Polymarket volume overstates real turnover ~2.5×.
- Durable human version: **logically-linked relative value** (implication violations, bracket ladders) where wording risk deters pure bots.

## 4. Market making / liquidity provision
- **Kalshi:** public Liquidity Incentive Program pays up to **$0.005/contract** (3–97¢ prices; per-market daily pools $10–$1,000); sealed MM LP Program (reverse auction, cap **$50k/series/week**) ([help](https://help.kalshi.com/en/articles/15410219-liquidity-provider-program)).
- **Polymarket rewards:** daily USDC to resting orders; minute-sampled; score **quadratic in proximity to mid**; two-sided required (single-sided 3× penalty); paid daily midnight UTC ([docs](https://docs.polymarket.com/programs/liquidity-rewards)). Plus maker rebates post-fee-V2.
- **The real risk is adverse selection:** quotes nearest mid fill exactly when news moves; VPIN toxicity predicts maker losses in single-name markets (Bartlett–O'Hara). Base rate: average maker still −9.64%; profitable making concentrates in ≥50¢ inventory + FLB harvesting.
- Competitive bar on flagship books: Bloomberg profiles a former poker pro doing 60 trades/min, revising quotes 30×/s. Retail edge lives in **non-flagship markets SIG/Jump ignore**. Realistic: low hundreds $/month at retail scale.

## 5. Model-based trading
### Weather (Kalshi settles on NWS climate reports per station)
- Playbook: GFS+ECMWF ensembles + climatology; 2–5 days out = widest mispricing; same-day = latency race lost to bots. Documented micro-edge: 88–95¢ NO band with ≥2°F cushion → +1.5–2.4%/trade after fees ([botforkalshi](https://www.botforkalshi.com/blog/kalshi-weather-trading-strategy)).
- **Must-read failure: Northlake Labs went 0-for-32** assuming Gaussian errors — forecast errors are fat-tailed; "weather arb bots execute within seconds of every NWS model cycle update" ([postmortem](https://www.northlakelabs.com/max/blog/kalshi-weather-postmortem-and-pivot/)).
- Scale: single snowstorm contract $6M+; weather ~$2M/day on Polymarket; "Hans323" made ~$1.1M on a London-weather structure. Capacity per market: thousands.

### Economic data
- **Kalshi Fed markets now scary-accurate:** NBER WP (Jan 2026) — perfect day-before-FOMC record 2022–Jun 2025, beats Fed funds futures ([Fortune](https://www.fortune.com/2026/01/28/kalshi-prediction-market-federal-reserve-betting-forecast-nber-working-paper)). Naive Kalshi-vs-FedWatch is dead.
- Residual: earlier-cycle lag vs rate-vol markets; **CPI bracket ladders vs Cleveland Fed nowcast** (historically beats consensus); tails of CPI ladders still overpriced (Economics ψ=0.034***); release-second latency race.

### Sports
- Kalshi vig ≈0.85% vs 4.6% at books; on liquid primetime games Kalshi is often sharper than retail books. Standard play: **devig Pinnacle → trade PM when deviation > fees**. PMs don't ban winners.
- Well-calibrated inside 48h → edge is in illiquid games (execution cost, not edge), long-dated futures (underconfident → buy favorites), and **player props** (immature liquidity, books hold most margin there).

## 6. News/latency
- **Crypto hourly/5-min markets:** Binance updates ~200ms; Kalshi reprices with 3–7s lag; one bot **extracted $271.5k in 30 days** from Polymarket latency before dynamic fees were added ([Turbine](https://www.turbinefi.com/blog/why-prediction-market-trades-get-picked-off-2026)).
- **Mention markets:** pros use TV antennas for sub-second edge; live transcription + LLM extraction pipelines wired into Kalshi API. Insider tail: Trump's teleprompter operator made $100k+ (federal probe Jul–Aug 2026); whole category under CFTC review ([NPR](https://www.npr.org/2026/08/13/nx-s1-5930689/cftc-probe-mention-markets-prediction-markets-kalshi)).
- **Oracle risk (Polymarket global):** UMA optimistic oracle — Mar 2025 Ukraine-minerals market governance-attacked 9%→100%, **$7M false payout** ([Orochi](https://orochi.network/blog/oracle-manipulation-in-polymarket-2025)); Zelensky-suit fiasco. Price markets since moved to Chainlink.
- Classic tales ([LessWrong](https://www.lesswrong.com/posts/yXHcqrCpiHC5tDuEc/tales-from-prediction-markets)): Mauna Loa CO₂ data trader (~$40k); the $156k fat-finger scooped by resting lowball bids (→ always rest deep bids); Soulja-Boy manipulation; flash-loan resolution-gaming attempt.

## 7. Time decay / "theta" (selling longshots near expiry)
- Same trade as FLB (§1). Steamroller risks: 33% SD vs 2.6% mean (one blown 95¢ position erases ~19 winners); **FLB shrinks near expiry** (slope→0.99 <1h) so last-day premium is mostly gone; short-horizon weather is fat-tailed/overconfident; sub-15¢ = fee death zone; oracle risk on global Polymarket.

## 8. Copy/flow analysis
- Mature tooling: PolyTrack, Polymarket Analytics (1M+ wallets P&L), copy bots with sub-3s mirroring. **Mostly doesn't work:** you inherit losses fully, wins at a discount; whale edge is often unobservable context (hedges, MM inventory, coordinated wallets); whales know they're watched and paint the tape.
- Realistic use: **idea/watchlist feed** — sudden smart-wallet accumulation in a sleepy market is Bayesian evidence.

## 9. Combinatorial/Dutch-booking
- Books for logically-dependent markets are isolated (no cross-margining) → implication violations persist. Full Dutch-booking is NP-hard; pairwise dies in seconds, long-tail logical inconsistencies persist. Legs resolve on **wording**, not your logic model.

## 10. Documented winners
- **Théo (French whale):** ~$85M on Trump 2024 across 11 wallets; edge = self-commissioned "neighbor polls" for shy-voter bias ([The FP](https://www.thefp.com/p/french-whale-makes-85-million-on-polymarket-trump-win)).
- **Domer (@Domahhhh):** #1 Polymarket leaderboard; 5,000+ markets, ~$300M volume, ~$1M+/3yrs, ~$3M in 2024 cycle; full-time craft: "if you don't find an edge, don't bet" ([OnChainTimes](https://www.onchaintimes.com/a-chat-with-domer-the-1-trader-on-polymarket/)).
- Institutional: SIG, Jump, Cantor clients. Anonymous: $271.5k latency bot; 8-wallet NegRisk harvester; CO₂ trader; Hans323 weather ~$1.1M; teleprompter insider.
- Bots average **89 trades/active day vs 2.2 for humans**; >30% of wallet activity automated; ~2,000 active bots.

## 11. Why retail loses; realistic returns
- **84.1% of 2.5M Polymarket wallets in the red; 2% ever made >$1,000; 0.033% made >$100k; top 1% captured 76.5% of gains** ([The Defiant](https://thedefiant.io/news/research-and-opinion/polymarket-profitability-report-april-2026)). Bloomberg: 100k wallets lost ≥$1k; <2,000 accounts took 67% of profits.
- Size gradient: <$100 stakes → −26.8% median; >$500k → +2.6%. Kalshi: takers −31.5%, makers −9.6%.
- Mechanism: taking not making; buying YES-61%/true-32%; sub-10¢ lottos; manual news trading vs 200ms bots; crossing thin spreads; exit liquidity in "easy" categories.
- **Realistic systematic returns:** FLB/maker harvesting low-single-digit %/position, 33% SD, capacity low-to-mid five figures; arb clips tens of dollars per episode; latency niches mid-5-to-low-6 figures/yr until patched (and legally gray); real forecasting alpha (Domer-style) uncapped but non-systematic craft; liquidity rewards hundreds/month retail. Above ~$10–50k working capital per strategy you must *become* the market maker or have real forecasting edge.

## Master sources
Academic: [Whelan Kalshi paper](https://www.karlwhelan.com/Papers/Kalshi.pdf) · [Bartlett & O'Hara SSRN](https://papers.ssrn.com/sol3/Delivery.cfm/6615739.pdf?abstractid=6615739&mirid=1) · [NBA arb arXiv:2605.00864](https://arxiv.org/pdf/2605.00864) · [$40M arb arXiv:2508.03474](https://arxiv.org/abs/2508.03474) · [Calibration arXiv:2602.19520](https://arxiv.org/html/2602.19520v1) · [Election microstructure arXiv:2603.03136](https://arxiv.org/html/2603.03136v2) · [NBER Fed accuracy via Fortune](https://www.fortune.com/2026/01/28/kalshi-prediction-market-federal-reserve-betting-forecast-nber-working-paper)
Practitioner: [Northlake weather postmortem](https://www.northlakelabs.com/max/blog/kalshi-weather-postmortem-and-pivot/) · [Turbine picked-off](https://www.turbinefi.com/blog/why-prediction-market-trades-get-picked-off-2026) · [Polyflux arb taxonomy](https://polyflux.io/blog/polymarket-arbitrage/) · [LW Tales](https://www.lesswrong.com/posts/yXHcqrCpiHC5tDuEc/tales-from-prediction-markets) · [Navnoor Bawa MM economics](https://www.navnoorbawaresearch.com/p/kalshi-publishes-one-liquidity-subsidy) · [Bloomberg 2026 feature](https://www.bloomberg.com/features/2026-prediction-markets-polymarket-kalshi/)
