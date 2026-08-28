# Kalshi Market Structure: Operational Reference

Sources: the live public API (`https://api.elections.kalshi.com/trade-api/v2`, no auth for market data),
the authoritative OpenAPI spec at `https://docs.kalshi.com/openapi.yaml` (9,075 lines), contract-terms PDFs
under `https://assets.kalshi.com/contract_terms/`, and direct scans of **12,000 open events / 103,853 open
non-MVE markets / 13,486 series** (2026-08-26).

Companion measurements: [`05-live-recon-findings.md`](05-live-recon-findings.md).
Scanners: [`recon/`](recon/).

> Two items could not be confirmed: `kalshi.com/docs/kalshi-fee-schedule.pdf` and
> `kalshi.com/regulatory/rulebook` returned HTTP 429 on every attempt. The **0.07 taker coefficient** and the
> full text of Rules 6.3 / 7.1 must be verified from those before sizing. Everything else below is from the
> API/spec/PDFs directly.

---

## 1. Hierarchy and identifiers

`Series.ticker` (`KXHIGHNY`) → `EventData.event_ticker` (`KXHIGHNY-26AUG26`) → `Market.ticker`
(`KXHIGHNY-26AUG26-T80`). Each level carries a back-pointer field.

**Do not parse tickers to infer relationships.** Kalshi's own glossary states there are exceptions to the
pattern; use `series_ticker` / `event_ticker` fields. The market **suffix** is decodable and maps 1:1 to
`strike_type`:

| Suffix | `strike_type` | Meaning | Example |
|---|---|---|---|
| `-T<x>` | `less` / `greater` / `greater_or_equal` | one-sided threshold | `KXFED-27APR-T3.75` → "Above 3.75%" |
| `-B<mid>` | `between` | bucket, `<mid>` is the **midpoint** | `KXHIGHNY-26AUG26-B84.5` → floor 84, cap 85 |
| named code | `custom` | discrete outcome | `KXFEDDECISION-28JAN-C25` → "Cut 25bps" |
| UUID-backed | `structured` | Structured Target entity | sports teams/players |

Resolve `structured` entity UUIDs via `GET /structured-targets/{id}`, this is how you join sports markets
on the same team across different series.

---

## 2. Mutual exclusivity: the definitive rule

Two event fields, **perfectly correlated across all 12,000 open events, zero exceptions**:

| `collateral_return_type` | `mutually_exclusive` | count |
|---|---|---:|
| `MECNET` | `true` | 5,756 |
| `DIRECNET` | `false` | 2,210 |
| `""` (empty) | `false` | 4,034 |

**`mutually_exclusive == true` ⟺ `collateral_return_type == "MECNET"`.**

### 2.1 The flag means AT MOST one YES: never exactly one

There is **no exhaustiveness field anywhere in the API.** Verified by summing bids across MEC events:

```
KXNEWPOPE-70        n=7    Σbid=0.206   Σask=0.282
KXNEXTDNCCHAIR-45   n=34   Σbid=0.411   Σask=1.689
KXSBHOST-2031       n=26   Σbid=0.330   Σask=2.060
KXWC-30             n=82   Σbid=0.767   Σask=1.008
KXLLM1-26DEC31      n=8    Σbid=1.022   Σask=1.064
```

**2,531 of 6,020 MEC events have `Σbid < 0.80`**: most are simply not exhaustive.

Exhaustiveness *is* verifiable structurally for bucketed events: `KXHIGHNY-26AUG26` =
`T80` (<80), `B80.5` [80,81], `B82.5`, `B84.5`, `B86.5`, `T87` (≥88), contiguous over integer degrees.
For candidate-list events (Pope, DNC chair, World Cup, Oscars) there is usually **no "Other" leg**; where
one exists (`KXPRIMEENGCONSUMPTION-30`, `KXMODELHIGH-27-1550`) it must be detected by regex on
`yes_sub_title`. Never assume presence.

### 2.2 Consequence: the two directions are not symmetric

| Direction | Payoff | Safe? |
|---|---|---|
| **BUY** the basket (pay Σask, collect $1 if a listed leg wins) | $0 if nothing listed wins | **UNSAFE**, requires independently verified exhaustiveness |
| **SELL** the basket (collect Σbid, pay **at most** $1) | liability capped at $1 regardless | **SAFE**, non-exhaustiveness makes it *better* |

This inverts the naive scan. It is also where the density is: median `Σask = 1.15` versus median
`Σbid = 0.88`, i.e. books are overround and the overround is collected by *selling*.

### 2.3 Measured, both directions (full live MECE universe, 6,020 events)

```
events with Σbid > 1.00  (SELL candidates, structurally safe) :  47   0.8%
events with Σask < 1.00  (BUY candidates, need exhaustiveness):  90   1.5%

SELL INTO THE BID (immediate, taker fees), profitable after fees:  0     <-- ZERO
REST ASKS         (maker, fee-free on ~99% of series)           : 959 after liquidity filter
```

**Selling into the bid yields zero opportunities right now.** There is no free lunch available by crossing
the spread in either direction. That is the honest state of the venue.

The 959 "maker-profitable" resting-ask structures are **not** locked arbitrage: the margin *is* the
overround, which exists precisely because nobody is crossing it. Realizing it requires all legs to fill at
resting prices, a joint fill probability, not a certainty. Median 2 legs, so most are two-sided quoting.

### 2.4 Partial fills on a short basket: bounded, not riskless

If you rest asks on all N legs and only k < N fill, you hold a short YES position on a subset. Max liability
is still $1 (only one leg can win), and you keep the premium on the k legs you sold. Worst case is
`$1 − premium_collected`, the ordinary "sold a longshot" outcome, not a wipeout.

**This unifies two sleeves:** selling overpriced YES on longshot legs *is* the favorite–longshot trade of
S1, executed at basket granularity, with the MECE structure supplying a hard $1 liability cap on the whole
event. S1 and S2-short are the same edge at different resolution. Account them together.

---

## 3. Brackets vs. threshold ladders: two different shapes

**Not all "range" series are mutually exclusive.** Assuming CPI/Fed ranges sum to 1 is wrong:

| Event | MEC | CRT | Shape | Relation |
|---|---|---|---|---|
| `KXHIGHNY-26AUG26` | true | MECNET | 6 buckets (`between`) | `Σ P = 1` |
| `KXFEDDECISION-28JAN` | true | MECNET | 5 `custom` brackets | `Σ P = 1` |
| **`KXFED-27APR`** | **false** | DIRECNET | 18 **cumulative** thresholds T0.00…T4.25 | **monotone ladder** |
| **`KXUSCPIYEAR-*`** | **false** | DIRECNET | thresholds | monotone ladder |
| `KXINXY-27DEC31H1600` | true | MECNET | S&P year-end **ranges** | `Σ P = 1` |
| `KXINXDIRY-27DEC31H1600` | false | DIRECNET | S&P year-end **thresholds** | monotone ladder |

For `DIRECNET` ladders the tradeable structure is **monotonicity violation** (`P(>0.00) ≥ P(>0.25) ≥ …`),
i.e. adjacent-strike verticals, not sum-to-one. This is the cleanest possible S3/L2 link.

**Only 7 series mix both shapes** (`KXPRIMARYMOV`, `KXPSAVERT`, `KXGOVSENDIFF`, `KXSTARSHIPSPACE`, `KXSCFI`,
`KXMLBSS`, `KXHEISMANSPECIAL`), so read the flag **per event**, never cache per series.

### 3.1 Same underlying, two shapes: the best intra-Kalshi RV pairs

- **`KXFED-<meeting>`** (18 cumulative thresholds, DIRECNET) ⟷ **`KXFEDDECISION-<meeting>`** (5 exclusive
  brackets, MECNET). *A bracket in FEDDECISION is exactly the difference of two adjacent KXFED thresholds.*
- **`KXINXY`** (ranges, MECNET) ⟷ **`KXINXDIRY`** (thresholds, DIRECNET), same index, same timestamp.
- CPI family: `KXUSCPIYEAR` / `KXCPI` / `KXCPIYOY` / `KXLCPIMAXYOY` / `KXHIGHINFLATION`.
- Rate-path family: `KXFEDCHGCOUNT` / `KXRATECUTCOUNT` / `KXRATECUT` / `KXEMERCUTS` / `KXLARGECUT` / `KXZERORATE`.

**One genuine cross-listed duplicate** found by title collision across all 12,000 open events:
`KXOSCARVIS-27` and `KXOSCARMAH-27`, both "Oscar Winner: Best Makeup and Hairstyling", both MEC, two
independent order books. (The other 9 title collisions were false positives from shared templates across
different sports.)

### 3.2 `GET /milestones`: the built-in correlated-event index

Largely absent from community write-ups and the highest-leverage discovery endpoint for S3. Each milestone
carries `primary_event_tickers[]` and `related_event_tickers[]`, grouping events **across different series**
that resolve off one real-world occurrence:

```json
{ "title": "Strait of Hormuz", "category": "Elections",
  "primary_event_tickers": ["KXHORMUZNORM-26MAR17"],
  "related_event_tickers": ["KXHORMUZNORM-26MAR17","KXHORMUZWEEKLY-26AUG23",
                            "KXHORMUZMAX-26AUG23","KXMAXSHIPSHORMUZ-26AUG31"] }
```

**Use this as the seed for the S3 link graph** instead of title similarity.

---

## 4. Fees: fully enumerable from the API

`Series.fee_type` ∈ `{quadratic, quadratic_with_maker_fees, quadratic_with_combo_maker_fees, flat}` and
`Series.fee_multiplier` (double). Per the OpenAPI description, `quadratic` = General Trading Fees Table;
`quadratic_with_maker_fees` adds maker fees; `quadratic_with_combo_maker_fees` uses a **0.5** maker
multiplier instead of 0.25.

Across all 13,486 series:

```
fee_type:  quadratic                      13,353     <- MAKERS PAY NOTHING
           quadratic_with_maker_fees         130
           quadratic_with_combo_maker_fees     3     (MVE only)
           flat                                0     (enum exists, unused)

fee_multiplier:  1.0 -> 13,453    0.5 -> 19    0.0 -> 14
```

### 4.1 This corrects the plan's fee model

**Maker fees apply to only 130 of 13,486 series.** On the other 99%, resting orders are **free**, not
0.25× taker. The maker-fee series are the ones you most want to quote (majors), so this is not a licence to
ignore them, but the default assumption flips.

Maker-fee series worth knowing: Economics, `KXCPI`, `KXCPIYOY`, `KXFED`, `KXFEDDECISION`, `KXGDP`,
`KXPAYROLLS`, `KXU3`, `KXRATECUTCOUNT`, `KXAAAGASM`, `KXEGGS`. Financials, `KXINXY`, `KXNASDAQ100Y`,
`KXIPO`. Crypto, `KXBTCMAX125`, `KXBTCMAX150`. Sci/Tech, `KXLLM1`. Entertainment, 7 Emmy/Super Bowl
series. Sports, the remaining 107.

### 4.2 Fourteen series trade completely FEE-FREE (`fee_multiplier = 0`)

`KXBTCY`, `KXETHY`, `KXGDPYEAR`, `KXLAYOFFSYINFO`, `KXCITRINI`, `KXDOED`, `KXELECTIRAN`, `KXEXPAND`,
`KXGAMBLINGREPEAL`, `KXGREENLAND`, `KXIRANDEMOCRACY`, `KXNEXTIRANLEADER`, `KXPAHLAVIHEAD`, `KXTRUMPOUT`.

`KXBTCY`, `KXETHY`, and `KXGDPYEAR` are **also `deci_cent`** (0.1¢ ticks). Zero fees plus tenth-cent
granularity makes these the cheapest venue on the exchange to express basket and RV trades. **Prioritize
them for first live testing**, the fee floor that dominates every other sleeve simply does not apply.

Nineteen MLB derivative series carry `fee_multiplier = 0.5` (half fees).

### 4.3 Fee rounding is per-order with carry: batch, don't slice

Per `docs.kalshi.com/getting_started/fee_rounding`: each fill produces a trade fee (rounded **up** to
$0.0001), a rounding fee, and a rebate; an accumulator tracks cumulative rounding overpayment **per order,
across all its fills**, refunding $0.01 once it exceeds a cent.

**Consequence:** one large order is *not* penalized relative to many partial fills of that order, but many
separate small orders **are**, since each starts a fresh accumulator that rounds up. For basket trades use
`BatchCreateOrders` with large per-leg counts rather than many small clips.

Also check `EventData.fee_type_override` / `fee_multiplier_override` (takes precedence over the series rate)
and `Market.fee_waiver_expiration_time`.

---

## 5. Tick sizes: not always a cent

`Market.price_level_structure` + `Market.price_ranges[]`. Across 103,853 open non-MVE markets:

| Structure | Count | Share | Ranges |
|---|---:|---:|---|
| `linear_cent` | 95,171 | 91.6% | 1¢ throughout |
| **`tapered_deci_cent`** | 8,091 | 7.8% | **0.1¢ below 10¢ and above 90¢**, 1¢ in the middle |
| `deci_cent` | 591 | 0.6% | 0.1¢ throughout |

**`tapered_deci_cent` matters disproportionately**: tenth-cent resolution in both tails is exactly where
longshot-basket legs live, giving 10× finer quoting precisely where the favorite–longshot edge is largest.
Top series: `KXMIDTERMMOV` (3,939 markets), `KXHOUSERACE` (703), `KXDPWORLDTOUR` (156), `KXLPGATOUR` (144),
`KXGOLFMAJOR` (112), plus `KXPRESNOMD/R`, `KXSCOURT`, `KXNOBELPHYSICS`, `KXUKCOALITION`, 657 series.

Full `deci_cent` (53 series): `KXTEAMSINWS`, `KXWC`, `KXMLB`, `KXBTCY`, `KXETHY`, `KXGDPYEAR`,
`KXRATECUTCOUNT`, `KXNBERRECESSQ`, `KXLLM1`, `OAIAGI`, `MOON`…

> The contract-terms PDFs still say "Minimum Tick shall be $0.01", they lag the live sub-penny rollout.
> **Trust `price_ranges` from the API, not the PDF.**

Prices arrive as **fixed-point dollar strings** (`"0.6720"`). Use `Decimal`, never float.

---

## 6. Order-direction and book-side gotchas

From `docs.kalshi.com/getting_started/order_direction`:

> "`outcome_side` describes directional exposure only; it does not change the order's price. An order at
> price `p` with `outcome_side=no` is matched by an order at the same price `p` with `outcome_side=yes`."

**But the orderbook WebSocket channel is inconsistent with this by default:** no-side orders use *inverted*
pricing (no at 30¢ = yes at 70¢). **Set `use_yes_price: true`** or every cross-leg calculation is off by
`1−p`. `BookSide` is YES-referenced: `bid` = buy YES, `ask` = sell YES.

---

## 7. Lifecycle, halts, and the cancellation cliff

`Market.status` ∈ `{initialized, inactive, active, closed, determined, disputed, amended, finalized}`.
Note the `/markets?status=` **query filter** uses a different, coarser vocabulary
(`unopened, open, paused, closed, settled`), do not reuse one enum for the other.

- `initialized → active` at `open_time`; `active → closed` at `close_time`, both time-driven, **no
  WebSocket event**. Poll or schedule.
- `active ↔ inactive` on exchange pause/resume (WS event). **"Reopening cancels all existing orders."**
- `closed → determined` (starts `settlement_timer_seconds`) `→ finalized`. `disputed → amended`
  **restarts the settlement timer.**

> **After `close_time`, all order operations including cancellations are rejected with `MARKET_INACTIVE`.**
> You cannot pull resting orders after close. Size resting exposure accordingly.

`can_close_early = true` on 103,798 of 103,853 markets; the condition is in `Market.early_close_condition`.

Settlement latency (`settlement_timer_seconds`): 300s (36,506 markets), 1800s (33,690), 60s (12,728),
180s (6,462), 30s (4,641), 3600s (3,482). MVE combos settle in 5s.

**Scheduled maintenance every Thursday 03:00–05:00 ET.** In a *trading pause* you may cancel but not
place/modify; in an *exchange pause* neither. Resting orders survive unless `cancel_order_on_pause: true`.

`exchange_index` shards the venue: `0` Default (96,394 markets), `1` Combos, `2` Crypto (2,922),
`3` Tennis & Baseball (4,537). **`GET /exchange/status` reports per-shard `trading_active` independently,
one shard can halt while others trade**, which is itself a source of stale cross-shard quotes.

---

## 8. Position limits and collateral

**Position limits are NOT exposed in the API.** (`contracts_limit` on order groups is a self-throttle,
"maximum contracts matched within this group over a rolling 15-second window", not a regulatory limit.)

Discovery path: parse `Series.contract_terms_url` PDFs, which carry either a `Position Limit:` or a
`Position Accountability Level:` line, denominated in **dollars of exposure per strike per Member**:

- `KXPAYROLLS`: "Position Accountability Level ... **$25,000 per strike, per Member**"
- `KXFEDDECISION`: "Position Limit ... **$7,000,000 per Member**"

The stem is **not** the series ticker (`KXCPI` → `CPI.pdf`, `KXHIGHNY` → `GLOBALTEMPERATURE.pdf`), always
read the field, never construct the URL.

**Netting:**
- *Within a market:* automatic. `MarketPosition.position_fp` is a single signed integer, "Negative means NO
  contracts and positive means YES contracts." Buying 10 NO against 10 YES flattens you; it does not create
  a two-sided position.
- *Within a MEC event:* `MECNET`. `EventPosition.event_exposure_dollars` is a distinct field from the sum of
  `market_exposure_dollars`, implying collateral is assessed on **worst-case event outcome**, not per leg.
  This is the structural analogue of Polymarket's NegRisk.
- **No conversion primitive**: nothing equivalent to NegRisk's explicit convert call. The offset is
  implicit in collateral only.
- **No margin API for event contracts.** `/margin/*` endpoints belong to the separate Perps product. Do not
  confuse them.
- Subaccount netting toggles via `GET/PUT /portfolio/subaccounts/netting`.

**For a short basket, if MECNET behaves as described, required collateral is ~`(1 − Σbid)` per basket rather
than `Σ(1 − bid_i)`, an order-of-magnitude capital difference.** This must be verified on a funded account
before sizing (PLAN.md T-050c).

---

## 9. Compliance surface: machine-readable

`Series.additional_prohibitions[]` is queryable per series. Universal (12,597 series each):

> "Persons who are employed by any of the Source Agencies are not permitted to trade on the Contract."
> "Persons who hold any material, non-public information on the Underlying are not permitted to trade."

Sports adds league-participant bans (3,529 series). Elections adds Congressional staff (911/823),
"Holders of federal and statewide public office" (826), "Any candidate currently listed as a market within
this event" (755), pollsters, Decision Desk employees, vote-tallying personnel, FEC commissioners, Electors,
and for 170 series registered lobbyists and state election officials.

**Check this array per series before enabling automated trading on it**: it directly implements the
conflict-list requirement in PLAN.md §13.

Arbitrage itself is not restricted. Self-trade prevention is a first-class order parameter, not a ban:
`SelfTradePreventionType ∈ {taker_at_cross, maker}`. Set it explicitly if you quote both sides of a book.

---

## 10. Standard contract-terms clauses (direct quotes)

**Revision policy** (FEDDECISION, PAYROLLS, identical boilerplate):
> "Revisions to the Underlying made after Expiration will not be accounted for in determining the
> Expiration Value."

**Cancellation contingency** (FEDDECISION):
> "If the Federal Reserve cancels the target meeting, then a strike listed for 'No change' will resolve to
> Yes and all other markets will resolve to No."

**Missing-data contingency** (PAYROLLS):
> "If no data is released by the Expiration Date at the Expiration time, then the market will resolve based
> on the last available month of data."

**Review / void authority** (FEDDECISION):
> "Before Settlement, Kalshi may, at its sole discretion, initiate the Market Outcome Review Process
> pursuant to Rule 6.3(d) of the Rulebook. If an Expiration Value cannot be determined on the Expiration
> Date, Kalshi has the right to determine payouts pursuant to Rule 6.3(b)."

**Weather settlement source shifted:** `KXHIGHNY` now settles on **The Weather Company**
(`https://weather.com/kalshi`), not NWS directly, and `rules_secondary` warns:
> "Preliminary Weather Company data may be subject to rounding and conversion differences from the final
> reported value."

*(This invalidates the assumption in PLAN.md §3.6 that Kalshi temperature markets settle on NWS climate
reports. If S4 is ever built, the settlement source is The Weather Company for at least the major-city
high-temp series, model that source, not NWS CLI.)*

**Settlement sources by category** (counts across 13,486 series): Weather, The Weather Company (123), NWS
(95), NOAA (40). Economics, BLS CPI (150), BLS Employment Situation (148), NY Fed (110). Crypto, CF
Benchmarks (189), CoinGecko (38). Commodities, Pyth (15+), ICE (12), USDA (5). Elections, NYT (320),
Reuters (282), AP (280), Politico (268). Entertainment, Billboard (436), Spotify (391), Rotten Tomatoes
(241). Sports, ESPN (2,775), Fox Sports (1,581).

---

## 11. Multivariate events (MVE): the combo/parlay product

**117,477 open markets** under `KXMVECROSSCATEGORY` (+1,357 in `KXMVECROSSCATEGORY0`), category `Exotics`,
`exchange_index: 1`. Fields: `mve_collection_ticker`, `mve_selected_legs[]` (each with `event_ticker`,
`market_ticker`, `side`, `yes_settlement_value_dollars`). Endpoints under `/multivariate/*` include
**`CreateMarketInMultivariateEventCollection`**: you can mint a combo market on demand.

**Filter them with `GET /markets?mve_filter=exclude`** (or `only` to isolate). Without it they swamp
pagination, this is the cleaner fix for the `/markets` problem noted in `05-live-recon-findings.md` F2.

Since combos are priced off their legs, they are the most obvious RV surface on the venue, and they carry
combo maker fees (0.5 multiplier) on exactly 3 series.

---

## 12. A live, verified basket dislocation

`KXLLM1-26DEC31`, "Best AI at the end of 2026?", MEC/MECNET, 8 legs, `deci_cent` ticks:

```
BAID  bid 0.0010 (sz 145,537)   GOOG bid 0.0790 (sz 8,757)
OAI   bid 0.1310 (sz      98)   A    bid 0.6720 (sz   190)
XAI   bid 0.1080 (sz       6)   META bid 0.0200 (sz 15,721)
ALI   bid 0.0090 (sz   2,744)   MOON bid 0.0000 (sz     0)
                                       Σ bid = 1.0200   Σ ask = 1.0640
```

Selling YES on all legs at the bid collects $1.0200 against a max payout of $1.00 → ~2.0¢ gross. But `MOON`
has a **zero bid**, so only 7 legs are sellable, and the binding size constraint is `XAI` at ~6 contracts.
**This is the canonical shape: real, tiny, and capped by one thin leg**: which is precisely why the
residual persists at all.

---

## 13. Practical API notes

- `GET /series?limit=200` **ignores the limit and returns all 13,486 series in one response.** Cache it, it
  is the entire fee / prohibition / settlement-source map.
- Public market data needs no auth. Rate limits are real: ~0.3–0.45s between paginated calls with
  exponential backoff was stable; bursts return HTTP 429.
- Always pass `mve_filter=exclude` on `/markets` scans.
- Use `Decimal` for prices (fixed-point dollar strings). See `getting_started/fixed_point_migration`.
- Market-maker programme: reduced fees and adjusted position limits in exchange for quoting **98% of each
  1-hour increment** across 80+ products.
- Block trades and RFQs exist under `/communications/*`, a route for size that avoids sweeping thin books.

---

## 14. Corrections forced into PLAN.md

| # | Correction |
|---|---|
| K1 | **S2 primary direction is SHORT the basket**, not long. Buying requires verified exhaustiveness; selling is capped at $1 liability regardless. |
| K2 | Maker fees apply to only **130 of 13,486 series**; elsewhere makers pay **zero**, not 0.25× taker. Update the fee model to read `fee_type`/`fee_multiplier` per series. |
| K3 | **14 series are fee-free and 3 are also `deci_cent`** (`KXBTCY`, `KXETHY`, `KXGDPYEAR`), first live testing goes here. |
| K4 | Detect MECE via `mutually_exclusive` / `MECNET`, but treat **exhaustiveness as a separate, unprovided property**. |
| K5 | Threshold ladders are `DIRECNET`, not MECE, their structure is **monotonicity**, and `KXFED` ⟷ `KXFEDDECISION` (and `KXINXY` ⟷ `KXINXDIRY`) are the premier same-underlying RV pairs. |
| K6 | Seed the S3 link graph from **`GET /milestones`**, not title similarity. |
| K7 | Set **`use_yes_price: true`** on orderbook subscriptions or every cross-leg price is wrong by `1−p`. |
| K8 | **Orders cannot be cancelled after `close_time`** (`MARKET_INACTIVE`), bound resting exposure into the close. |
| K9 | Tick size is **not always 1¢**; read `price_ranges`. `tapered_deci_cent` gives 0.1¢ in both tails. |
| K10 | Fee rounding accumulates **per order**, use `BatchCreateOrders` with large per-leg counts, not many small clips. |
| K11 | Position limits live only in `contract_terms_url` PDFs, in **dollars per strike per Member**. |
| K12 | `Series.additional_prohibitions[]` implements the conflict-list check programmatically. |
| K13 | Weather settles on **The Weather Company**, not NWS, for the major high-temp series. |
| K14 | Per-shard halts (`exchange_index` 0–3) are independent, a source of stale cross-shard quotes. |
