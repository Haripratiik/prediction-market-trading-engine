# 13 -- The weather forecasting edge, tested

Evidence tags follow the house convention: **[M]** measured on our own data, **[C]** cited from a
source, **[I]** inference drawn here.

**Verdict up front. [I] There is no forecast-vs-market edge in Kalshi temperature markets at any
horizon we can measure, and the reason is now a measured number rather than an argument: at matched
lead time the market's implied forecast is 10-18% MORE accurate than the best forecast that can be
assembled from free public data, including NOAA's own National Blend of Models.** The market is not
mispricing the public forecast; it is beating it. `strategy/weather.py` therefore ships as a
research harness that emits `Decision` rows and **no quotes**.

This note also *corrects* `research/09` section 5 in two places -- its implied-sd estimate was too
sharp, and its "years to prove" estimate was too pessimistic by two orders of magnitude -- without
changing its conclusion. The correction matters because it turns "unfalsifiable" into "falsifiable
in about 114 settlements", which is the one actionable thing in this note.

---

## 1. Settlement, exactly

Getting this wrong makes every downstream number meaningless, so it is established first and
verified against realised settlements.

### 1.1 The station map

**[M]** Read verbatim from `rules_docs` in `data/pm.db` (opened read-only). 23 cities, 46 series
(a high and a low series each). The rules text names a **CLI product identifier**, which is the NWS
Daily Climate Report for one specific station:

| Kalshi city | CLI id | station | note |
|---|---|---|---|
| Chicago | `CLIMDW` | KMDW | **Midway, not O'Hare** |
| Houston | `CLIHOU` | KHOU | **Hobby, not Bush/IAH** |
| New York City | `CLINYC` | KNYC | Central Park, not JFK/LGA/EWR |
| Washington DC | `CLIDCA` | KDCA | Reagan National |
| Dallas | `CLIDFW` | KDFW | DFW, not Love Field |
| Austin | `CLIAUS` | KAUS | Bergstrom, not Camp Mabry |
| Newark | `CLIEWR` | KEWR | listed separately from NYC |
| Trenton | `CLITTN` | KTTN | |
| Atlanta / Boston / Denver / Las Vegas / Los Angeles / Miami / Minneapolis / New Orleans / Oklahoma City / Philadelphia / Phoenix / San Antonio / San Diego / San Francisco / Seattle | `CLIATL` `CLIBOS` `CLIDEN` `CLILAS` `CLILAX` `CLIMIA` `CLIMSP` `CLIMSY` `CLIOKC` `CLIPHL` `CLIPHX` `CLISAT` `CLISAN` `CLISFO` `CLISEA` | KATL KBOS KDEN KLAS KLAX KMIA KMSP KMSY KOKC KPHL KPHX KSAT KSAN KSFO KSEA | |

Four of these are the "wrong" airport relative to the obvious guess. A gridpoint forecast pulled for
"Chicago" resolves to O'Hare in most APIs and is the wrong station.

### 1.2 The source, and a discrepancy worth knowing about

**[M]** `series_cache.settlement_sources_json` and the per-market rules text both say the source is
**The Weather Company** (`https://weather.com/kalshi`). **[C]** The certified contract terms
(`https://assets.kalshi.com/contract_terms/GLOBALTEMPERATURE.pdf`, Appendix A, rulebook
`GLOBALTEMPERATURE`) instead say: "The Source Agencies are, in hierarchical order, National Weather
Service, the national weather service for <area>".

**[I]** These are not the same entity. TWC redistributes the ASOS/NWS observation, so in the normal
case they agree, but the settled number is whatever appears on the TWC page, and `research/09`'s
statement that "Kalshi settles on the official ASOS station high/low reported by NWS" is a
simplification. It is right about the underlying sensor and wrong about the publisher.

### 1.3 The window: midnight-to-midnight LOCAL STANDARD time

**[M]** `close_at_us` for every temperature event in our corpus is **01:00 local daylight time on the
following day** -- 06:00Z for Central, 07:00Z for Mountain, 08:00Z for Pacific. Phoenix, which does
not observe DST, closes at 00:00 local. That is midnight **local standard time**, which is how the
NWS climatological day is defined, and it is one hour offset from the local calendar day for the
seven months of DST.

**[M] This is not a pedantic detail for the low markets.** Recomputing the daily extreme over the
local *calendar* day instead of the LST climate day changes the value on **11.1-11.9% of station-days
for the minimum** and only 1.0-1.7% for the maximum (2,713 station-days). The maximum happens
mid-afternoon and does not care; the minimum happens near the boundary and does.

### 1.4 The other clauses that bind

**[C]** From the certified terms, verbatim:

- "Only the first official non-preliminary report published by the Source Agencies that includes the
  relevant data will be used for resolution. Revisions after the Expiration Date are not included."
- "Contract resolution is based on the full precision reported by the Source Agency. Rounding by
  media outlets, secondary reporting, or third-party summaries does not affect resolution."
- "'between' means within an inclusive range (>= lower bound and <= upper bound)".
- Expiration time 10:00 AM ET; minimum tick $0.01; position accountability $25,000 per strike.

**[I] The exhaustiveness of the 6-slot partition depends on the settlement value being a whole
number of degrees F, and the rules do not promise that.** The buckets are `<71`, `71-72`, `73-74`,
`75-76`, `77-78`, `>78`. Combine "between is inclusive" with "full precision reported by the Source
Agency" and a settlement value of 72.4 F satisfies **none** of the six: it is not <71, not in
[71,72], not in [73,74], and not >78. All six legs would resolve NO. In practice ASOS daily extremes
are published as whole degrees F and this has never happened, but it is the same class of gap that
`rulebook/exhaustiveness.py` exists to catch, and it means the short basket is safe (liability
capped, an unlisted outcome is *better* for the seller) while the long basket rests on a convention
rather than on the contract text.

### 1.5 Verification

**[M]** The settlement quantity is reproduced exactly by the **ACIS / GHCND station daily
maxt/mint** feed (`https://data.rcc-acis.org/StnData`, free, no key), which is the digitised CLI
value:

- All **7** settled weather markets in `settlements` agree: `KXHIGHPHIL-26AUG26-B85.5` YES against
  KPHL max 86 F, `KXHIGHTOKC-26AUG26-B94.5` YES against KOKC max 94 F, and the five NO legs of those
  two events all agree.
- A stricter check: the provable lower/upper bound constructed from intraday METAR observations
  (section 5) never crossed the realised settlement value in **46 settled event-days, 0 violations**.

**[I]** So `ACIS maxt/mint at the CLI station, over the midnight-LST climate day` is the settlement
quantity, and everything below is scored against it rather than against a proxy.

---

## 2. What the market's price actually implies

**[M]** 27,282 weather market snapshots -> **625 synchronous 6-leg partition observations across 81
events**, reconstructed with the R5a `latest row with observed_at_us <= t` rule. Every event in the
corpus has exactly 6 legs and exactly four 2 F interior buckets; the geometry is uniform.

For each observation the normalised mid distribution is fitted to a Gaussian over the actual cut
points by minimising `KL(market || Gaussian)`. This is the correction to `research/09`, which
instead bracketed the modal probability and the entropy against a *centred* reference partition.

**[M] TABLE B -- market-implied distribution by horizon** (identifiable partitions, i.e. interior
mass >= 0.5):

| hours to close | n | events | modal p | eff buckets | **implied sd (F)** | implied MAE (F) | KL(mkt \|\| N) | sum(bid) | spread 3-97c |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0-3 | 378 | 30 | 0.975 | 1.17 | **0.570** | 0.455 | 0.2546 | 0.990 | 3.5c |
| 6-12 | 24 | 24 | 0.840 | 1.73 | **0.972** | 0.775 | 0.1729 | 0.970 | 3.2c |
| 12-18 | 19 | 8 | 0.513 | 3.42 | **1.804** | 1.440 | 0.0935 | 0.950 | 2.0c |
| 18-24 | 81 | 2 | 0.446 | 3.29 | **1.907** | 1.521 | 0.0241 | 1.010 | 2.0c |
| 24-36 | 90 | 38 | 0.454 | 3.89 | **1.954** | 1.559 | 0.0244 | 0.990 | 2.0c |
| 36-60 | 7 | 7 | 0.459 | 3.37 | **1.981** | 1.581 | 0.0314 | 0.970 | 1.0c |

### 2.1 research/09's implied sd was too sharp

**[M]** `research/09` 5.2 concluded the 18-36h implied distribution "brackets sd ~1.75 F" and
therefore "implied MAE = 0.798 * sd <= ~1.4 F". Directly fitting the same rows gives
**sd = 1.91-1.95 F, implied MAE = 1.52-1.56 F** -- about 11% wider.

**[I]** The bias is mechanical and 09 half-anticipated it. Its heuristic compared the observed modal
probability against a Gaussian *centred* on the partition. Real partitions are not centred: the
median `|mu - partition centre|` at 24-36h is **1.3 F**, i.e. two-thirds of a bucket. 09 reasoned
that off-centring *depresses* modal p and so treated 1.75 F as an upper bound on sd; in fact the
depression it corrects for is smaller than the sharpening implied by the full six-slot shape, and
the bound went the wrong way. The direction of 09's conclusion survives -- see section 4 -- but the
number should be 1.95 F, not 1.75 F.

### 2.2 The market's implied shape is NOT Gaussian, and that is a trap

**[M]** The residual misfit is real: median `KL(market || best-fit Gaussian) = 0.024 nats` at
24-36h (p90 0.054). More concretely, **606 of 625 observations put more probability on their modal
bucket than any Gaussian with their own fitted sd can** -- the cap for a 2 F bucket is
`2*Phi(1/sd) - 1 = 0.391` at sd 1.95, and observed modal probabilities routinely exceed it.

A worked example, `KXLOWTSFO-26AUG27` at 27h to close:

```
market   [0.005 0.032 0.253 0.558 0.124 0.028]
gaussian [0.001 0.035 0.285 0.487 0.178 0.014]   mu 59.2  sd 1.50  KL 0.025
```

**[I] The market's implied density is peaked at the mode and fat in the tails relative to a
Gaussian.** Anyone pricing buckets from a point forecast plus a Gaussian error term will therefore
systematically **underprice the modal bucket and overprice the shoulders**, on every single market,
before any question of forecast skill arises. That is a pure model-specification loss, and it is
visible in the order book for free. **[C]** It is also precisely the first of the three failure modes
Northlake Labs diagnosed in their 0-32 postmortem.

---

## 3. What the free forecast is actually worth

The comparison `research/09` could not make: rather than citing published NWS skill, measure the
free forecast against **the exact settlement quantity** established in section 1.

**[M] Sample:** 23 CLI stations x 118 days = **2,713 station-days**, 2026-05-01 to 2026-08-26.
Truth from ACIS. Forecasts from Open-Meteo's previous-runs archive
(`https://previous-runs-api.open-meteo.com`, free, no key), which returns the value for a valid time
*as it was forecast by an earlier model run* -- so there is no look-ahead. Daily extremes are
recomputed by us over the midnight-LST climate day, not taken from the API's calendar-day
aggregate. Per-station bias is removed with an **expanding-window, strictly out-of-sample** mean.

**[M] TABLE A -- free-forecast error against the settlement quantity** (`max` element shown; `min` in
the harness constants):

| source | lead | raw bias | raw sd | **OOS-debiased sd** | MAE | P(\|z\|>2) | kurtosis |
|---|---|---:|---:|---:|---:|---:|---:|
| NOAA NBM | ~0-6h | -0.98 | 2.20 | **2.06** | 1.57 | 0.049 | 4.55 |
| NOAA NBM | ~24-39h | -1.03 | 2.55 | **2.45** | 1.87 | 0.050 | 4.57 |
| NOAA NBM | ~48-63h | -1.19 | 2.84 | **2.77** | 2.12 | 0.054 | 4.22 |
| NOAA NBM | ~72-87h | -1.11 | 3.16 | **3.12** | 2.38 | 0.056 | 4.07 |
| Open-Meteo best_match | ~0-6h | -0.68 | 1.48 | **1.27** | 0.97 | 0.050 | 5.45 |
| Open-Meteo best_match | ~24-39h | -0.16 | 2.89 | **2.79** | 2.06 | 0.052 | 5.96 |
| Open-Meteo best_match | ~48-63h | 1.05 | 4.16 | **3.49** | 2.65 | 0.056 | 5.04 |

**[M]** Best two-model combination at day-ahead lead, with the weight chosen **in sample** (so this
is an optimistic bound, not an achievable number): `w_NBM = 0.65`, **debiased sd 2.38 F, MAE 1.79 F**
for the max; `w_NBM = 0.70`, sd 2.48 F, MAE 1.91 F for the min.

**[M]** Per-station bias is worth having: removing it out-of-sample takes NBM day-ahead max from
sd 2.55 to 2.45 and best_match from 2.89 to 2.79. That is the "station-specific bias correction"
`research/09` 5.4 called the one honest residual, and it is worth **0.10 F**. Section 4 shows the
edge needed is 0.17-0.22 F, so the residual is real and still not enough.

### 3.1 The fat tails are real but were mis-attributed

**[M]** Error kurtosis is 4.07-5.96 against a Gaussian 3.0 -- genuinely fat-tailed. But
`P(|z| > 2)` is **4.5-5.6%** against a Gaussian 4.55%, not the 10-12% Northlake Labs reported.

**[I]** Both facts are consistent: the distribution is peaked-and-fat rather than merely wide, so
the 2-sigma rate is near-normal while the 3- and 4-sigma rates are not. **[C]** Northlake's "2-sigma
events 10-12% of the time" is therefore better read as evidence that their sigma was too small
(a Gaussian fitted to an under-dispersed forecast) than as evidence that weather has 2-sigma events
at twice the normal rate. The practical lesson is unchanged -- do not use a plain Gaussian -- but the
mechanism is different, and it points at the error *model*, not at the atmosphere.

### 3.2 Conditioning on model disagreement does not open a door

The obvious rescue is to trade only when the forecast is unusually confident. Using inter-model
disagreement `|NBM - best_match|` as the confidence proxy:

**[M]** Day-ahead error sd by disagreement quintile (2,713 station-days):

| quintile | median disagreement | error sd (F), max | error sd (F), min |
|---:|---:|---:|---:|
| 1 (most agreement) | 0.3 / 0.6 F | 2.24 | 2.46 |
| 2 | 1.0 / 1.6 F | **2.10** | 2.26 |
| 3 | 1.7 / 2.5 F | 2.34 | **2.19** |
| 4 | 2.7 / 3.5 F | 2.35 | 2.41 |
| 5 (most disagreement) | 4.4 / 5.2 F | 2.81 | 3.04 |

**[M]** `corr(|error|, disagreement)` = **0.152** (max) and 0.139 (min).

**[I] Model disagreement barely predicts model error.** The best quintile still has error sd
**2.10-2.24 F**, above the market's median implied 1.87-1.95 F, and the relationship is not even
monotone in the middle. Selecting on forecast confidence does not get the free forecast under the
market's implied sd. It also is not a free selection: restricting to a quintile cuts the sample by
80%, which multiplies the settlements needed by five.

---

## 4. The honest comparison, and the verdict

The market's implied sd is a *claim* about the accuracy of the market's own point forecast. Our
measured forecast error sd is what the free forecast actually delivers. Same units, same quantity,
so they compare directly -- provided the lead times match.

**[I] Lead matching.** Close is 01:00 LST on day D+1 and the maximum occurs around 15:00 local on
day D, so an observation `h` hours before close corresponds to a lead-to-event of roughly `h - 10`
hours. The market's 24-36h-to-close bucket is therefore **lead ~14-26h**. Our day-ahead archive is
lead ~24-39h -- *longer*, which handicaps the forecast. Interpolating linearly in lead between the
NBM 0-6h point (sd 2.06) and the optimal blend at ~31h (sd 2.38) gives the forecast its best
defensible shot:

**[M] TABLE E -- matched-lead comparison**

| | sd (F) |
|---|---:|
| **market implied, 24-36h to close (lead-to-max ~14-26h)** | **1.95** |
| best free blend, lead-interpolated to 14h | 2.19 |
| best free blend, lead-interpolated to 20h | 2.26 |
| best free blend, lead-interpolated to 26h | 2.32 |
| NBM alone, lead ~24-39h, OOS-debiased | 2.45 |
| Open-Meteo best_match, lead ~24-39h, OOS-debiased | 2.79 |

**[I] The market's point forecast is 11-16% more accurate than the best free forecast we can build,
at matched lead, and 20-30% more accurate than any single free product.** That is the whole answer.
There is no mispriced public number to buy. Both `research/09`'s conclusion and its reasoning
survive; only its arithmetic needed fixing, and fixing it made the market look *less* sharp
(sd 1.95 not 1.75) while measurement made the free forecast look *worse still* (2.19-2.38 not the
~1.4 F that would have been needed).

**[I] For an edge to exist despite this, the market would have to be overconfident by more than the
gap** -- claiming sd 1.95 while actually erring by 2.2 or more. We cannot rule that out (section 6),
but the little evidence we have points the other way: on the 41 events we can score, the realised
standardised residual has **sd 0.84-0.90**, i.e. if anything the market is mildly *under*-confident.

### 4.1 What the edge would have to be worth, net

**[M] TABLE C -- cost to express a view on one bucket.** Kalshi temperature series carry
`fee_type = "quadratic"` and `fee_multiplier = 1.0`, so **makers pay zero** (`core.math.contracts`).
Median bid-ask across tradeable 3-97c buckets at 18-36h is **2.0c** on our snapshots (research/09
reported a 3c median; ours is tighter).

| bucket price | taker fee | half-spread | **taker edge required** | **maker edge required** |
|---:|---:|---:|---:|---:|
| 10c | 0.63c | 1.00c | **1.63 pp** | 0.00 pp |
| 20c | 1.12c | 1.00c | **2.12 pp** | 0.00 pp |
| 30c | 1.47c | 1.00c | **2.47 pp** | 0.00 pp |
| 40c | 1.68c | 1.00c | **2.68 pp** | 0.00 pp |
| 50c | 1.75c | 1.00c | **2.75 pp** | 0.00 pp |

**[M] TABLE D -- translating that into degrees**, at the measured implied sd of 1.907 F over 2 F
buckets (max move over all bucket placements):

| delta (F) | max bucket move |
|---:|---:|
| 0.10 | 1.22 pp |
| 0.25 | 3.04 pp |
| 0.50 | 6.05 pp |
| 0.75 | 9.02 pp |
| 1.00 | 11.91 pp |

**[M]** Solving: a taker needs to beat the market's implied mean by **0.17 F at 20c, 0.20 F at 30c,
0.22 F at 40c**. A maker on these series pays no fee at all and needs, in principle, **zero** edge --
the entire hurdle is adverse selection.

**[I] This is a smaller hurdle than `research/09` estimated** (it said 0.3-0.5 F, using a 3c spread
and a 2.0 F sd). It does not help. The measured gap runs the wrong way by 0.24-0.43 F of forecast
sd, which is larger than the 0.17-0.22 F hurdle and of the opposite sign. You would need the free
forecast to be *better* than the market by 0.2 F; it is *worse* by more than that.

### 4.2 The one number that is better news than expected

**[M]** Building the forecast distribution from the day-ahead blend (sd 2.38 F) and comparing it to
the market on the same 38 events at 18-36h to close:

- forecast minus market-implied mean: mean **-0.14 F**, median absolute **0.90 F**, p90 **2.39 F**
- per-bucket disagreement: median `max|q - m|` = **18.80 pp**, p90 35.28 pp; median total variation
  24.13 pp

**[M]** `core.math.stats.markets_to_beat_market(0.188)` = **114 settled markets**.

**[I] `research/09` 5.3 concluded this question needs "~10,000 settled markets ... that is years",
by assuming a 2 pp typical disagreement. The measured disagreement is 18.8 pp, ten times larger,
because the free forecast disagrees with the market a great deal -- it is simply wrong when it
does.** `N >= 4/delta^2` scales as the inverse square, so the real requirement is **114
settlements**, which is about **2.5 days of all 46 listed city-days**, or two to three weeks on a
ten-city subset. The hypothesis is not unfalsifiable. It is cheap to falsify, and this note
falsifies most of it already.

**[I] Caution on that number.** `markets_to_beat_market` answers "how many settlements to detect that
one forecaster beats the other at t = 2". It is symmetric: 114 settlements is what it takes to
establish the *direction*, and section 4's evidence says the direction is that the market wins.

### 4.3 Capacity, for completeness

**[M]** Per event, one row each at the last observation (n = 81): open interest p10/p50/p90 =
642 / **3,698** / 33,277 contracts, volume 745 / 4,634 / 66,428. At a 50c average that is about
**$1,849 of notional per city-day at the median**, p90 ~$16,600. Top-of-book median bid size 704
contracts, median ask size 60. This is consistent with `research/09`'s "a few thousand dollars per
city-day". Even a real edge here would be a small business.

---

## 5. The model-free test, which is the one that would have mattered

Everything above depends on an error model. There is one claim about these markets that needs no
model at all:

> **The running maximum so far inside the climate day is a HARD LOWER BOUND on the settlement value.
> Any bucket lying entirely below it is worth exactly zero. If such a bucket still shows a bid, that
> bid is free money to sell into.**

No forecast, no distribution, no calibration -- an inequality. If it fires, it is the only genuinely
risk-free structure in the category.

**[M] It never fires. 1,591 synchronous checks across all 92 events: ZERO provably-dead buckets with
a live bid.**

### 5.1 The unit bug that manufactured $209,300 of fake edge

Worth recording because the first run of this test reported 204 hits.

**[M]** `api.weather.gov/stations/{id}/observations` returns `temperature` in **whole degrees
Celsius**, taken from the METAR. Converting naively, KLAX on 2026-08-26 read 31 C -> 87.8 F, which
"proved" that the 86-87 bucket was dead. The actual settlement value was **87 F** and that bucket was
the winner, bid at 99c. The 204 "dead buckets" included a 213,743-contract bid; totalled at observed
size the phantom premium was **$209,300**.

**[I]** METAR degC quantisation is 1.8 F wide and asymmetric under rounding, so the correct bound
from a reading of `C` whole degrees is

```
true F in [1.8C + 32 - 0.9, 1.8C + 32 + 0.9]
guaranteed integer lower bound = floor(1.8C + 32 - 0.9 + 0.5)
```

which for KLAX gives 87, not 87.8, and correctly leaves the 86-87 bucket alive. Where the METAR
remarks carry a `Txxxxxxxx` group the precise 0.1 C value is available (present on ~25 of 500
observations -- the hourly METARs only) and the bound tightens to +/- 0.18 F.

**[M]** With the bound computed correctly it was checked against reality: **0 violations in 46
settled event-days**. And with a correct bound, the number of dead-but-bid buckets is zero.

**[I] The lesson generalises past weather.** A settlement source published in different units from
the observation feed is a systematic edge generator in backtest and a systematic loss generator
live. The fake signal here was large, plausible, concentrated in the most liquid market in the
corpus, and would have been sized aggressively precisely because it looked model-free.

---

## 6. What this note does NOT establish

Stated plainly, because it is the load-bearing gap.

1. **[M] Calibration at 18-36h is untested.** Our corpus spans 2026-08-26 to 2026-08-27. Only
   Aug-26 events have settled, so the scorable set is **41 events, all at 3.5-12.2 hours to close**
   (log score 0.411, Brier 0.216, mean probability on the realised bucket 0.748). At 18-36h -- the
   only horizon where a forecast could matter -- **n = 0**. Section 4's verdict rests on comparing
   the market's *claimed* sd to our *measured* sd, not on watching the market's claim come true.
2. **[I] The lead-time match is interpolated, not measured.** Open-Meteo's free archive offers the
   latest run and then whole-day steps back; there is nothing at lead 14-26h. Table E interpolates
   linearly between lead ~3h and lead ~31h. Forecast error growth is roughly linear over day 1, so
   this is a reasonable interpolation, but it is an interpolation.
3. **[I] HRRR, and any sub-6-hour rapid-refresh product, is untested.** Inside 6 hours the market's
   implied sd is 0.57 F and the modal bucket is at 0.975; that is the latency game, and
   `research/09` 5.2 and 5.5 already argue it is unreachable. We did not attempt it.
4. **[M] Whether The Weather Company and NWS ever disagree is unmeasured.** Section 1.2 shows the
   contract names one publisher and the API another. All 7 settled markets matched the NWS-derived
   ACIS value, which is 7 observations, not a guarantee.
5. **[I] The in-sample blend weight flatters the forecast.** `w_NBM = 0.65` was chosen on the same
   2,713 days it is scored on. An honest out-of-sample blend is worse than sd 2.38 F, which widens
   the gap in section 4 rather than narrowing it -- so this cuts in favour of the verdict.
6. **[I] Sub-daily bias structure is unexplored.** We removed a per-station constant. Bias by season,
   by wind direction, by cloud regime, or by synoptic type could plausibly be worth more than the
   0.10 F a constant is worth. Whether it is worth the 0.24-0.43 F needed to close the gap is a
   multi-season research question, exactly as `research/09` 5.6 said.
7. **[M] The wide-market subset is a genuine, untested residual.** Section 4 compares medians, and
   the per-event implied sd is dispersed: p10/p25/p50/p75/p90 = **1.57 / 1.73 / 1.87 / 2.23 / 2.54 F**
   at 18-36h. On **13-21% of events** (5-8 of 38, depending on which forecast sd you use) the
   market's implied sd *exceeds* our best constant forecast sd -- the real `KXHIGHTSFO-26AUG27` book
   implies **2.70 F**, a marine-layer coin flip. **[I]** That is a *sharpness* comparison, not an
   accuracy one, and the natural explanation is that those are genuinely hard days on which our own
   constant 2.25 F sd is most optimistic -- section 3.2 shows our error sd reaches 2.81 F on the
   hardest quintile. But we could not confirm it: `corr(market implied sd, model disagreement)` =
   **+0.014 on n = 38**, where the standard error is ~0.16, so the test has no power at all. **[I]
   Whether a wide-implied-sd subset is exploitable is the single most defensible remaining question
   in this category**, and it is the one thing the harness's decision rows would settle first.

---

## 7. Recommendation

**[I] Do not build a weather sleeve.** `strategy/weather.py` ships as a `Sleeve`-conforming research
harness with `gate = 0` and a hard `EDGE_DEMONSTRATED = False`; `desired_state` returns
`quotes = ()` unconditionally and emits `Decision` rows with `acted = False` so that calibration
accumulates without capital at risk.

**[I] If anyone wants to reopen it, the experiment is cheap and now well-specified.** Record the
market-implied and blend-forecast distributions for every listed city-day, settle them against ACIS,
and after **114 settlements** the `t = 2` comparison is decided. That is days, not the years
`research/09` estimated. The harness emits exactly the rows that test needs. The prior after this
note is that the test will confirm the market wins.

**[I] Ask the one open question first.** Section 6 item 7 is the only residual with a real prior
behind it: on 13-21% of events the market's implied sd is wider than our forecast's error sd, and
we have no power to say whether that is exploitable. Stratify the harness's decisions by implied sd
and the answer arrives with the same 114 settlements. Note that this subset is 13-21% of listings,
so reaching 114 settlements *within it* takes five to eight times longer -- roughly two to three
weeks of full coverage rather than 2.5 days.

**[I] The two findings worth carrying to other categories:**

1. The market's implied distribution is peaked-and-fat, not Gaussian (section 2.2). Any sleeve that
   turns a point estimate into bucket probabilities via a Gaussian will misprice mode against
   shoulders systematically. Fit the shape, or trade only the sign of the disagreement.
2. Settlement-source unit mismatches manufacture large, confident, model-free-looking edges
   (section 5.1). Before trusting any inequality-based signal, verify the bound against realised
   settlements -- the check cost one query and killed $209,300 of imaginary profit.

---

## Sources

**Contract and settlement**
- Kalshi certified contract terms, rulebook `GLOBALTEMPERATURE` --
  https://assets.kalshi.com/contract_terms/GLOBALTEMPERATURE.pdf
- Kalshi settlement source page (per `series_cache`) -- https://weather.com/kalshi
- Kalshi weather markets help -- https://help.kalshi.com/en/articles/13823837-weather-markets

**Data**
- ACIS / RCC `StnData` (GHCND station daily maxt/mint, free, no key) -- https://data.rcc-acis.org/StnData
- Open-Meteo previous-runs archive (free, no key) -- https://previous-runs-api.open-meteo.com
- Open-Meteo forecast API, incl. `ncep_nbm_conus` -- https://open-meteo.com
- NWS API observations -- https://api.weather.gov
- NOAA National Blend of Models -- https://blend.mdl.nws.noaa.gov/nbm-dashboard

**Prior work engaged with**
- `research/09-edge-reality-check.md` section 5 -- corrected in 2.1 and 4.2, conclusion upheld
- Northlake Labs weather postmortem (0-32) --
  https://www.northlakelabs.com/max/blog/kalshi-weather-postmortem-and-pivot/
- NDFD surface-temperature verification, Weather and Forecasting 21(5) --
  https://journals.ametsoc.org/view/journals/wefo/21/5/waf946_1.xml

**Reproduction**
- `strategy/weather.py` carries every measured constant in this note as a named module-level
  constant with its provenance, and `tests/test_weather.py` asserts the load-bearing ones.
