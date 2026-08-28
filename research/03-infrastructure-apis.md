# Technical Infrastructure & APIs: Research Notes, August 2026

## 1. Kalshi API
**Docs:** https://docs.kalshi.com/ (rate limits, WS quickstart, demo env). Status: https://kalshistatus.com/

**Base URLs:** production `https://external-api.kalshi.com/trade-api/v2`; demo `https://external-api.demo.kalshi.co/trade-api/v2`; legacy `https://api.elections.kalshi.com/trade-api/v2` (serves ALL markets despite the name). WS: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`.

**Auth:** API key pair + per-request **RSA-PSS SHA-256** signature over `timestamp_ms + METHOD + path` (query strings excluded); headers `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE` (base64), `KALSHI-ACCESS-TIMESTAMP`. Same scheme for WS handshake. Python: `cryptography` lib, PSS padding, salt = digest length.

**Key endpoints:** `GET /markets`, `/events`, `/series/{t}`, `/markets/trades`, `/markets/{t}/orderbook` (auth), candlesticks + `GET /historical/markets/{t}/candlesticks`; `GET /exchange/status` (circuit breaker, `trading_active`), `/exchange/schedule` (maintenance Thu 3–5 AM ET); `GET /portfolio/balance`, `/positions`, `/fills`, `/orders`; **Orders V2:** `POST /portfolio/events/orders`, batch create/cancel at `/portfolio/events/orders/batched` (old endpoints deprecated ≥May 21, 2026); `GET /account/limits`, `/account/endpoint_costs`.

**Create Order V2 fields:** ticker, side (bid/ask on YES leg), count (string), price (dollar string 2–4dp), time_in_force (FOK/GTC/IOC), self_trade_prevention_type (required: taker_at_cross | maker), client_order_id, expiration_time, post_only, cancel_order_on_pause, reduce_only, subaccount, order_group_id. Response: order_id, fill_count, remaining_count, average_fill_price, average_fee_paid.

**Rate limits (token buckets since Apr 23, 2026):** most requests cost 10 tokens; batches do NOT save tokens. Tiers (read/write tokens/s): Basic 200/100 → Advanced 300/300 (free upgrade call) → Expert 600/600 … Prestige 10000/8000 (earned by 30-day volume share). 429s carry no Retry-After, exponential backoff + jitter.

**WS channels:** private `orderbook_delta` (snapshot + seq'd deltas; `get_snapshot` resync action since Apr 2026), `fill`, `market_positions`; public `ticker`, `trade`, `market_lifecycle_v2`, `multivariate`. Server pings ~10s. Book is bids-only representation: `yes_ask = 100 − best_no_bid`. **FIX 4.4** available for order entry/drop-copy/market data.

**Fees:** taker `ceil(0.07 × p(1−p) × 100)/100` (peak 1.75¢ @50¢); maker 25% of taker; cap $0.035.

**Demo:** https://demo.kalshi.co (email signup, separate keys).

**SDKs:** official `kalshi-python`; new spec-first `kalshi-python-sync`/`-async` (Jul 21, 2026, Py≥3.13); official starter https://github.com/Kalshi/kalshi-starter-code-python; community: https://github.com/TexasCoding/kalshi-python-sdk, https://github.com/arshka/pykalshi; Rust: pbeets/kalshi-trade-rs, arvchahal/kalshi-rs.

**Gotchas** (https://github.com/briancox730/unofficial-kalshi-api-docs): fills are YES-referenced (apply 1−p for NO); positions endpoint lags fills ~1s (build position from fills); close by selling held side; `last_updated_ts` is ISO string.

## 2. Polymarket APIs (global/crypto stack; US venue has its own)
**Docs:** https://docs.polymarket.com/. Note: US trading runs on Polymarket US (QCX DCM) with separate REST+gRPC API, Ed25519 keys ([polymarket.us/developer](https://polymarket.us/developer), SDK: https://github.com/Polymarket/polymarket-us-python). International CLOB below remains geoblocked for US IPs, know which venue your account is on.

**Five surfaces (global stack):**
1. **Gamma** `https://gamma-api.polymarket.com`, public metadata: `/markets`, `/events`, `/tags`, `/series` → gives `condition_id` + outcome `token_id`s.
2. **CLOB** `https://clob.polymarket.com`, `/book(s)`, `/price(s)`, `/midpoint`, `/spread`, `/tick-size`, `/prices-history`; `POST /order`, cancels, `/auth/api-key`, `/auth/derive-api-key`.
3. **Data API** `https://data-api.polymarket.com`, `/positions` (per-wallet PnL), `/trades`, `/activity`, `/holders`, `/value`, leaderboards.
4. **Relayer** `https://relayer-v2.polymarket.com`, gasless transactions.
5. **Bridge** `https://bridge.polymarket.com`, deposits/withdrawals.

**Auth:** L1 = wallet key signs EIP-712 ClobAuth (Polygon 137); orders are EIP-712 signed structs. L2 = derived creds (apiKey/secret/passphrase), HMAC-SHA256 over `timestamp+METHOD+path+body`; headers POLY_ADDRESS/SIGNATURE/TIMESTAMP/API_KEY/PASSPHRASE. **Signature types:** 0 EOA, 1 Magic proxy, 2 Gnosis Safe proxy (website accounts → pass `signature_type` + `funder`). EOAs must set USDC + CTF approvals first.

**Orders:** all signed limits; "market" = marketable FOK/FAK. GTC/GTD/FOK/FAK; post_only with GTC/GTD; tick sizes 0.1→0.0001 with decimal-precision rules.

**Rate limits:** CLOB ~9,000 req/10s; `POST /order` ~3,500/10s burst, 36,000/10min sustained; Cloudflare queues excess (latency) rather than clean 429s.

**WS:** `wss://ws-subscriptions-clob.polymarket.com/ws/market` (subscribe `assets_ids`, messages: `book` snapshot + hash, `price_change`, `last_trade_price`, `tick_size_change`) and `/ws/user` (order/trade lifecycle; trades can be MATCHED then FAILED, confirm terminal status). Send literal `"PING"` every ~10s. RTDS `wss://ws-live-data.polymarket.com` for Chainlink crypto prices (the exact resolution feed), comments; official client https://github.com/Polymarket/real-time-data-client.

**Chain:** Polygon; collateral USDC.e→**pUSD** wrap (Apr 28, 2026 v2 contract migration, v1 clients broke; subgraphs deprecated for Goldsky). Outcome tokens = ERC-1155 (Gnosis CTF). Relayer pays gas; self-sent txs cost fractions of a cent (keep a few POL).

**NegRisk:** https://github.com/Polymarket/neg-risk-ctf-adapter, convert NO-set → YES of others + collateral; enables ΣNO arb; client exposes neg-risk flag; conversion = on-chain `convertPositions`.

**Fees (2026 V2, effective Mar 30):** takers by category (crypto 0.07, sports, finance/politics 0.04, econ/culture/weather 0.05, geopolitics free); makers zero + rebates (15–25% of taker fees); liquidity rewards for resting near mid.

**Clients:** Python https://github.com/Polymarket/py-clob-client + py-clob-client-v2; TS https://github.com/Polymarket/clob-client + v2; `Polymarket/agent-skills` (terse API patterns), `Polymarket/polymarket-cli`.

## 3. Other APIs
- **Manifold** (play money, practice): https://docs.manifold.markets/api, base `https://api.manifold.markets/v0/`; `POST /v0/bet` with `Authorization: Key`; limit orders via `limitProb`; ~500 req/min; bots welcome. Python: manifoldpy, manifoldbot; official MM example: manifoldmarkets/market-maker.
- **PredictIt:** read-only `https://www.predictit.org/api/marketdata/all/` (60s-delayed, ~1 req/min courtesy). No trading API.
- **Betfair** (reference architecture): betfairlightweight + **flumine** framework (https://github.com/betcode-org/flumine, roadmap lists Polymarket/Kalshi adapters); https://github.com/betfair-down-under/AwesomeBetfair for stream-parsing/backtesting patterns.

## 4. Historical data
- **Kalshi:** official candles (+ `/historical/` for archived markets; batch 100 tickers, 10k candles/resp); full public trade tape via `/markets/trades`; **no historical L2**, record `orderbook_delta` yourself (SSRN 6583921 documents the method).
- **Polymarket:** `/prices-history` per token (fine granularity only while live; resolved markets ~12h granularity, capture live or lose it); complete tick-level trade history on-chain via **Dune** (decoded Polygon tables; free 2,500 credits/mo) and **Goldsky** official datasets/pipelines (https://docs.goldsky.com/chains/polymarket).
- **Bulk datasets:** **Jon Becker prediction-market-analysis, full Polymarket+Kalshi markets/trades, 36 GiB Parquet** (https://github.com/jon-becker/prediction-market-analysis); SII-WANGZJ/Polymarket_data (1.1B trades, HuggingFace).
- **Commercial:** Dome API (unified Kalshi+Polymarket realtime+historical, cross-platform market matching, free tier: https://domeapi.io/); Marketlens (tick-level PM books); PolymarketData.co (L2 snapshots from Aug 2025); PolyRouter.
- **Bottom line:** run your own WS recorder from day one for book-level backtests.

## 5. Backtesting
- **braedonsaunders/homerun**: most complete OSS stack: Polymarket+Kalshi, 25+ strategies, L2-replay backtests with hazard-model fills, walk-forward CV, latency injection, shadow/live modes (AGPL): https://github.com/braedonsaunders/homerun
- Quentin-Piot/prediction-market-backtester; evan-kolberg/prediction-market-backtesting (NautilusTrader-based); **NautilusTrader has a first-class Polymarket live adapter** (https://nautilustrader.io/docs/latest/integrations/polymarket/); emulo-backtest (Dome-based); Awesome list: https://github.com/aarora4/Awesome-Prediction-Market-Tools
- **Pitfalls:** survivorship (use full-universe datasets keyed by listing date); look-ahead via final-value metadata (snapshot over time); liquidity fantasy (backtest vs recorded L2 or severe haircuts; model queue position); fee modeling (pre-2026 Polymarket = zero-fee world; Kalshi 50¢ round-trip ≈3.5%); binary settlement (capital lockup, annualize per locked dollar-day); 12h-granularity history fabricates mean-reversion.

## 6. Notable bots/repos
- Official: Kalshi/kalshi-starter-code-python; **Polymarket/agents** (archived May 2026, canonical reference); **Polymarket/poly-market-maker** (Bands/AMM strategies, 30s sync loop, cancel-all on SIGTERM); Polymarket/agent-skills.
- Community: **warproxxx/poly-maker** (best-known MM: fair value + inventory skew) + poly_data; terrytrl100/polymarket-automated-mm (reward-optimized); **suislanchez/polymarket-kalshi-weather-bot** (GFS ensemble → Kelly sizing); WSOL12 BTC cross-venue arb; ryanfrigo/kalshi-ai-trading-bot; homerun (strategy catalog).
- **Caution:** niche is full of SEO-spam repos with wallet-draining "config" steps, never paste a private key into an unread repo.

## 7. External data feeds
- **Weather:** NWS `https://api.weather.gov` (free); settlement = NWS CLI climate reports per station, IEM (`mesonet.agron.iastate.edu`) is the backtest source; **Open-Meteo Ensemble API** (free 10k/day, individual GFS/ECMWF ensemble members = the money endpoint); ECMWF open data; NOMADS/HRRR. Pipeline: ensemble → station bias correction → bucket probabilities → compare to market.
- **Econ:** Cleveland Fed inflation nowcast (scrape); CME FedWatch API (~$25/mo) or reconstruct from ZQ futures; FRED/BLS APIs (8:30 ET releases, parse fast); GDPNow.
- **Sports:** The Odds API ($0–99/mo); Pinnacle direct API closed Jul 2025 → aggregators: SportsGameOdds ($99+), OddsPapi, OpticOdds. Devig sharp consensus → compare.
- **Crypto:** Binance/Coinbase WS; Polymarket crypto resolves on **Chainlink**, subscribe to RTDS `crypto_prices` (the oracle's own feed).
- **News:** X API (Polymarket is X's official PM partner); RSS/GDELT; CourtListener/PACER; Benzinga/Alpaca news WS.

## 8. Operations
- **VPS over home Windows box** (50–200ms residential vs 1–10ms us-east VPS; Windows Update reboots kill resting books). AWS/Lightsail us-east-1, Hetzner Ashburn, QuantVPS ~$20–60/mo. Windows dev fine; deploy Linux+Docker/systemd. If Windows: NSSM/Task Scheduler restart-on-failure.
- **Architecture:** separate recorder / strategy / execution-OMS / monitor. Persist orders+fills to SQLite/Postgres keyed by `client_order_id`; crash-recovery reconciles vs exchange, never trusts memory.
- **Kill switches:** Kalshi, poll `/exchange/status`, `cancel_order_on_pause: true`, batch-cancel + flatten on fatal error. Polymarket, wired cancel-all on heartbeat loss; beware UMA-dispute gap risk. Global: max-position/max-daily-loss enforced in execution layer; dead-man's switch.
- **WS hygiene:** reconnect w/ backoff+jitter, resubscribe, seq/hash-checked books, resync on gap; silence >15s = dead. Recorder on separate connection from execution.
- **Clock:** NTP mandatory (`w32tm /resync` / chrony), skew causes mysterious 401s. Log RTT per request; timestamp inbound WS locally.
- **Monitoring:** healthchecks.io + Telegram/Discord alerts on fills, drawdown, disconnects, 429s, state drift; reconcile every N minutes.
