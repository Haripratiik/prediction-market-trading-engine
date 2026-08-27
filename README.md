# predictionMarkets

Building a systematic prediction market trading operation (US / Georgia based).

## Status

**Phase A + venue layer + recorder + shadow engine.** 206 tests pass (offline + live public API + authenticated demo).

```bash
python -m pytest                    # everything
python -m pytest -m "not live"      # offline only
python -m recorder.main --once      # sweep the universe into data/pm.db
python -m scripts.screen_universe   # run the MECE gate over what was recorded
```

| Done | Module | What it holds |
|---|---|---|
| T-002…5 | [core/math/](core/math/) | Fee model, Kelly + shrinkage, e-process & sample sizes, tetrachoric + Dutch-book + ST Kelly |
| T-006 | [core/db.py](core/db.py) | SQLite schema; append-only snapshots enforced by **triggers**, point-in-time reads |
| T-010 | [venues/kalshi/](venues/kalshi/) | RSA-PSS signing, REST client, token-bucket limiter, universe enumerator |
| T-014 | [recorder/main.py](recorder/main.py) | 24/7 universe recorder — **106,686 markets / 13,518 series in 28s** |
| T-050b | [rulebook/exhaustiveness.py](rulebook/exhaustiveness.py) | The MECE gate — blocks **104/104** naive buy candidates on live data |
| T-007 | [core/config.py](core/config.py) + [config/risk.yaml](config/risk.yaml) | Settings; §9 risk limits live in ONE file; secrets by path only |
| T-044 | [shadow/engine.py](shadow/engine.py) | Queue-conservative counterfactual fills, mark-outs, pessimistic/optimistic bracket |

### What building it corrected

Four errors, all found by running code against reality rather than notes — recorded in PLAN.md §C:

1. **A growth-table cell that was never computed** (−31.0 bp → **−38.2 bp**).
2. **Wrong fee-ratio boundary** — claimed 13¢, actually **42.9¢** (`fee/price = θ(1−p)` is linear, not explosive).
3. **The "fee-free corner" does not exist.** research/06 named 14 series with `fee_multiplier = 0`; live there are **none**. The first-live-capital recommendation is withdrawn.
4. **A rule that rejected 3,484 events wrongly** — `settlement_sources` is an event-level *fallback* list, not per-leg assignment, so per-leg divergence isn't detectable that way.

### Data collection — running now, no credentials

```bash
python -m recorder.main --once           # full universe sweep
python -m recorder.l1 --interval 5       # L1 quotes + trade tape on a liquid watchlist
```

Kalshi's WebSocket and full L2 depth need auth, but **top of book and the trade tape are public** — and
the tape carries **`taker_side` labelled by the exchange**, so trade-sign inference is never needed. (For
contrast, feed-inferred direction on Polymarket agrees with on-chain truth only ~59% of the time.) That is
enough for trade-through detection, realized spread, mark-outs, Kyle's λ, and full shadow mode.

Measured: 300 markets polled in **0.17–0.38s**; ~3,600 quote snapshots and 36,000 trades captured in the
first 90-second run.

### Demo trading — WORKS

```bash
python -m scripts.check_auth              # verify credentials
python -m scripts.demo_order_lifecycle    # place / rest / queue-position / cancel / fill / flatten
```

Full lifecycle verified on the demo exchange with mock funds: order placed, rests, `queue_position`
returned, cancelled, marketable order **filled** (`fill_count=1.00`), position opened and flattened.

**Three endpoint corrections found only by trying it** — the spec and reality disagree:

| Documented | Reality on demo | Fix |
|---|---|---|
| `GET /portfolio/orders/{id}` | **404** (both V1 and V2 paths) | filter the list endpoint |
| `GET .../events/orders/{id}/queue_position` | **404** — it exists ONLY under `/portfolio/orders/...` | use the V1 path |
| `DELETE /portfolio/events/orders` (bulk cancel) | **404** | kill switch lists resting orders and cancels each |

That last one matters: the I9 kill path has to work on the venue we actually run against, not the one in
the spec.

**And the demo book is degenerate.** The tightest spread across 1,000 markets was **98¢** (1¢/99¢). There
is a counterparty — marketable orders fill — but at meaningless prices. Confirms demo is for integration
testing only, never strategy validation.

### Demo credentials — live and verified

`python -m scripts.check_auth` passes end to end: 2048-bit RSA loaded, signature verifies locally, clock
skew 0.4s, authenticated call returns a **$200 demo balance** (shard 0 of 4).

**Two findings that reshaped the data plan:**

1. **The demo has quotes but ZERO volume** — 202 of 1,000 sampled demo markets show a two-sided quote and
   none has ever traded. Demo is an integration sandbox, not a source of strategy data.
2. **Production WebSocket rejects unauthenticated connections (HTTP 401).** Real-time L2 depth is gated
   behind a *production* account, not just any account.

So the public REST **L1 poller + trade tape remains the production data source**, and shadow mode runs on
it. Full L2 depth is deferred — it is not on the critical path.

### Account status

| | Needs | Status |
|---|---|---|
| **Demo / paper trading** | email only — Kalshi's help centre says to use **mock** name/address/SSN and supplies sandbox card + Plaid credentials | **Open to anyone** |
| **Live trading** | US residential address, government photo ID, and a taxpayer ID — an **ITIN is accepted**, SSN is not the only route | Gate 0 |

If the F-1 question in PLAN.md §13 applies, that is settled *before* and independently of the tax-ID
mechanics.

**Next:** T-011 demo WebSocket recorder → shadow engine → Gate 1.

## Start here

**[PLAN.md](PLAN.md)** — the execution plan and single source of truth. Written for an AI coding agent:
invariants, canonical formulas, full data model, module contracts, per-sleeve strategy specs, the gate
system, risk limits, runbooks, test requirements, and an ordered task backlog with acceptance criteria.

## Supporting documents

| Document | What it is |
|---|---|
| [PLAN.md](PLAN.md) | **The plan.** Agent-executable. ~1,500 lines. |
| [The Prediction Market Playbook](https://claude.ai/code/artifact/db5e1395-1acc-45c2-b606-26a2838efac7) | Research report: venues, edges ranked, infrastructure, risk, taxes |
| [Edge Engineering](https://claude.ai/code/artifact/743ab202-4082-4145-8d8d-c39815409c64) | Visual math report: fee algebra, Kelly under estimation error, drawdown, Monte Carlo charts |
| [research/01-platforms.md](research/01-platforms.md) | Venue-by-venue: legality, fees, liquidity, APIs |
| [research/02-strategies-and-edges.md](research/02-strategies-and-edges.md) | Documented edges with academic evidence and capacity estimates |
| [research/03-infrastructure-apis.md](research/03-infrastructure-apis.md) | Kalshi/Polymarket APIs, historical data, backtesting, ops |
| [research/04-risk-tax-regulatory.md](research/04-risk-tax-regulatory.md) | Kelly math, base rates, taxes (GA), regulation, visa considerations |
| [research/05-live-recon-findings.md](research/05-live-recon-findings.md) | **MEASURED** live Kalshi universe — 12,553 events / 103,449 markets. Overrides earlier estimates. |
| [research/06-kalshi-structure.md](research/06-kalshi-structure.md) | Kalshi operational reference from the API/OpenAPI spec — MECE semantics, per-series fees, tick structures, lifecycle, limits |
| [research/07-microstructure.md](research/07-microstructure.md) | Quoting, queue position, fill modeling, impact, execution mechanics |
| [research/08-statistical-methods.md](research/08-statistical-methods.md) | Calibration, sequential/anytime-valid inference, correlated-binary portfolios, multi-market Kelly, backtest validity, fill survival models, and Python tooling verified on this machine |
| [research/recon/](research/recon/) | Harvester, analyzers, and the S2 Dutch-book / short-basket scanner prototypes |
| [research/quant/quant_research.py](research/quant/quant_research.py) | Reproducible simulations (seed 42) → [results.txt](research/quant/results.txt) |

## Core conclusions

1. **Venues:** Kalshi (primary) + Polymarket US (secondary / RV leg) + optionally ForecastEx via IBKR.
   Offshore Polymarket is forbidden.
2. **Maker, not taker.** Kalshi data: makers −9.6% average vs takers −31.5%; makers buying ≥50¢ averaged
   +2.6% post-fee. Simulated, maker discipline alone is worth ~10 points per 500 settlements.
3. **The core edge family is relative value** — intra-event Dutch books (S2) and linked-market RV (S3) —
   because it needs no forecasting skill, has guaranteed convergence at settlement, and lives in capacity
   too small for large firms. Paired with a structural maker basket (S1) for volume and an income sleeve
   (S6) that earns while validation accumulates.
4. **Maker legs decide viability.** A 5-outcome Dutch book profits below 94.5¢ as taker but below 98.6¢ as
   maker; a 5¢ implication violation nets 1.5¢ double-taker vs 4.1¢ double-maker. So: *post orders that
   only fill at prices completing a profitable structure* — patience, not latency.
5. **The moat is reading rulebooks,** not speed. Bots match titles; the durable opportunities are where
   titles and rulebooks disagree. Correlation-of-definition risk is the top loss driver, so rulebook
   equivalence is a hard gate.
6. **Size at quarter Kelly on a halved edge, 2% cap.** Shrinkage λ = σe²/(σe²+σn²) ≈ 0.5 is a theorem.
7. **Progression is gate-based, never calendar-based:** G0 compliance → G1 data → G2 pre-registered
   backtest → G3 shadow → G4 canary → G5 earned scale.
8. **Paper trading before capital, in four stages:** Manifold (bot mechanics) → Kalshi demo (integration
   only — its prices are synthetic) → historical replay → **shadow mode against live books** (the real
   validator; must be built, no product does it) → canary.
