# 12 -- Latency Arbitrage Feasibility: Can a Solo Trader Buy Their Way Into This?

Written 2026-08-27. Purpose: replace the assertion "that is colocation's edge" with a costed
answer. Where is the engine, what latency is for sale, what does it cost, what is the break-even,
what would the software have to be, and is the venue reachable on those terms at all.

Every claim is tagged:

- **[M]** MEASURED -- computed by me in this session from `data/pm.db` (opened read-only via
  `file:...?mode=ro`) or from network probes run from this machine. Method described inline.
- **[C]** CITED -- someone else's published number, with link.
- **[I]** INFERRED -- my reasoning on top of [M] or [C]. Weakest tier. Treated as such.

**Verdict up front:** the latency is cheap and the money is not the problem. The binding
constraints are three, and none of them is money: (1) the only non-CDN path to the engine is gated
behind a trailing-volume threshold a $10k account cannot reach; (2) 65% of the money is taken by
participants whose two legs land within 5ms of each other, and 57% of it was one tennis match in a
two-second window; (3) lawful account access is unresolved and must not be routed around.
Sections 6 and 7 give the full verdict.

---

## 0. The caveat that governs the money numbers

**[M] The L1 snapshot recorder polls every 5 seconds.** Measured over 817,394 consecutive
per-ticker snapshot gaps on mutually-exclusive 2- and 3-leg events:

```
per-ticker snapshot gap: p10 = 5000 ms   p50 = 5001 ms   p75 = 5001 ms   p90 = 5001 ms
2,317,902 market_snapshots rows share only 5,268 distinct observed_at_us values
```

Confirmed in the repo's own code: `recorder/l1.py:307` -- `ap.add_argument("--interval",
type=float, default=5.0)`.

**[I] This means `market_snapshots` cannot resolve a millisecond dislocation, and no conclusion
about episode lifetime may rest on it.** I re-ran the Dutch-book scan against snapshots anyway as
a control: 75 episodes, $852.79 total, and **every single one had a lifetime of exactly 0** --
i.e. it appeared in one 5-second poll and was gone by the next. That is not a measurement of
speed; it is the recorder's Nyquist limit.

**[M] The `trades` table is a genuine microsecond tape and is the only sound basis here.**
2,331,279 rows, 1,346,591 distinct `traded_at_us` values, only 2,349 of which land on a whole
millisecond -- so the timestamps carry real sub-millisecond information rather than rounded
values. Span 2026-08-27 01:13 -> 16:14 UTC (15.01 h wall clock), of which **424 distinct minutes
contain trades = 7.07 hours of live tape**.

Everything below uses the trade tape.

---

## 1. Where is the matching engine, physically?

### 1.1 It is AWS us-east-2 (Ohio), and this is provable rather than inferred

**[M] DNS resolution from this machine, cross-referenced against Amazon's own published prefix
list** (`https://ip-ranges.amazonaws.com/ip-ranges.json`, syncToken `1787837825`, createDate
`2026-08-27-13-37-05`):

| Kalshi hostname | resolves to | AWS classification |
|---|---|---|
| `mm.fix.elections.kalshi.com` (FIX order entry) | 18.223.244.34, 18.119.210.162 | **us-east-2 / EC2** |
| `marketdata.fix.elections.kalshi.com` (FIX market data) | 18.220.63.230 | **us-east-2 / EC2** |
| `trading-api.kalshi.com` (legacy REST origin) | 3.143.99.222, 18.226.31.12 | **us-east-2 / EC2** |
| `api.elections.kalshi.com` (public REST + WS) | 18.244.202.{13,30,108,121} | GLOBAL / **CLOUDFRONT** |
| `fix.demo.kalshi.co` (demo only) | 44.237.38.67 | us-west-2 / EC2 |
| `kalshi.com` (marketing site) | 64.239.109.1 | not AWS |

The FIX hostnames are the decisive ones. FIX is the order-entry protocol -- it terminates at the
exchange, not at a cache. Both the order-entry and market-data FIX endpoints sit in **us-east-2**.

**[M] The origin identifies itself as an AWS load balancer.** A request to
`https://trading-api.kalshi.com/trade-api/v2/portfolio/balance` returns `401` with header
`server: awselb/2.0` -- an AWS Elastic Load Balancer, in us-east-2, in front of the engine.

**[C] Corroboration.** [latency.glassnode.com](https://latency.glassnode.com/prediction-markets/about),
an independent prediction-market latency monitor running probes on Fly.io and AWS bare metal
across 16 cities, states flatly that Kalshi's origin servers are in **AWS us-east-2 (Ohio)** and
that the public REST host sits behind CloudFront. (Same source: Polymarket is AWS eu-west-2,
London.)

**[C] Kalshi's own FIX documentation** ([docs.kalshi.com/fix/connectivity](https://docs.kalshi.com/fix/connectivity))
names the endpoints above and states that private connectivity is offered via **AWS PrivateLink**,
with "FIX traffic routed entirely within the AWS backbone." A PrivateLink service is
single-region. Kalshi offering PrivateLink and not colocation cross-connects is itself evidence
that the engine is a cloud workload, not a caged machine.

**[I] Conclusion: the matching engine is an AWS workload in us-east-2, the Columbus / New Albany
Ohio metro. There is no cage, no cross-connect, and no meet-me room to buy into.** This matters
more than it first appears -- see section 2.4.

### 1.2 The "Kalshi is in Chicago, we get 1ms" claim is measuring a CDN cache

**[C]** [quantvps.com](https://www.quantvps.com/blog/kalshi-servers-location) publishes
"Round-trip times of just 1.14ms when connecting to `api.elections.kalshi.com`" and concludes
"sub-2ms latency usually indicates that the trading server and the exchange's matching engine are
located within the same metropolitan area," therefore Chicago. Their VPS sells "from $59.99/mo."
[tradoxvps.com](https://tradoxvps.com/why-kalshi-traders-need-a-1ms-vps-in-chicago/) makes the
same claim.

**[M] That measurement is of an Amazon CloudFront edge PoP, not of Kalshi.** `api.elections.kalshi.com`
is in Amazon's `CLOUDFRONT` prefix list (table above). ICMP from this house in Atlanta returns
**4 ms average over 10 pings** to the same hostname -- I am not in Chicago either. CloudFront
terminates at whatever edge is nearest the prober; the number says nothing about the origin.

**[M] And the fast responses are stale.** A request to
`https://api.elections.kalshi.com/trade-api/v2/markets?limit=1` returns in **7.1 ms p50** with
headers:

```
x-cache: Hit from cloudfront
age: 12
```

**The 7ms market data is 12 seconds old.** For a strategy whose entire opportunity set lives
inside 30 milliseconds, a 12-second-stale cache hit is worse than useless -- it is a fast wrong
answer.

### 1.3 What the path actually costs from this house

**[M] Application-level RTT, persistent TLS connection, 20-25 samples each, from this machine
(Atlanta, US East):**

| path | endpoint | cacheable? | min | **p50** | p90 |
|---|---|---|---:|---:|---:|
| direct us-east-2 origin | `/portfolio/balance` (401) | no | 27.4 | **28.0** | 30.9 |
| CloudFront -> origin | `/portfolio/balance` (401) | no | 31.5 | **34.5** | 57.7 |
| CloudFront edge | `/markets?limit=1` (200) | **yes, age=12s** | 5.8 | 7.1 | 10.6 |
| direct us-east-2 origin | `/exchange/status` | no | 27.5 | 28.6 | 29.8 |

TCP-connect-only p50: 30.5 ms to `trading-api.kalshi.com`, 9.3 ms to the CloudFront edge.

**[M] Traceroute decomposition** (`tracert -d 3.143.99.222`): hop 11 (`163.253.1.141`, still
inside the regional research network) already costs **19 ms RTT**; the first AWS-owned hop
(`108.166.244.0`) is at **31 ms**. So roughly 19 ms of the 28 ms is burned before the packet
leaves the local/regional network, and only ~9-12 ms is Atlanta -> Ohio transit.

**[I] Cross-check against physics:** Atlanta to Columbus is ~895 km great circle; at ~2/3 c in
fibre, the round trip floor is ~8.9 ms. That matches the ~9-12 ms AWS-transit component, so the
measurement is coherent and the ~19 ms local term is genuinely local overhead that would vanish
if the bot ran inside us-east-2.

**The honest number for this house is 28 ms p50 to the engine on an uncacheable request.**
The parent brief's 21 ms figure was, I believe, measured against the CloudFront-fronted host; the
uncacheable path is slower.

### 1.4 The finding that decides the whole question

**[M] There is no non-CDN public path.** A WebSocket upgrade request to the legacy direct-origin
host returns:

```
GET https://trading-api.kalshi.com/trade-api/ws/v2
  -> 401 Unauthorized   server: awselb/2.0
  -> body: "API has been moved to https://api.elections.kalshi.com/"
```

while the same request to `api.elections.kalshi.com` returns a proper Kalshi auth error through
`via: 1.1 ... (CloudFront)`.

**[C]** The documented production WebSocket is `wss://api.elections.kalshi.com/trade-api/ws/v2`
([Kalshi WS quick start](https://docs.kalshi.com/getting_started/quick_start_websockets)).

**[C]** The non-CDN alternatives are FIX and AWS PrivateLink, and both are tier-gated:
"Existing AWS PrivateLink connectivity remains available to members on the **Premier tier or
above**"; VPC peering for production WebSocket and FIX requires **Prime tier**
([Kalshi API changelog](https://docs.kalshi.com/changelog), 2026-08-20;
[FIX connectivity](https://docs.kalshi.com/fix/connectivity)).

**[I] Therefore: a retail account is architecturally required to send every order and receive
every quote through a content delivery network. The participants who are beating us are not.**
This is not a latency you can buy with a better server. It is an access tier. Section 2.5 prices
the gate.

---

## 2. What latency is purchasable, and at what price?

### 2.1 The ladder

All prices are **us-east-2 (Ohio), Linux, on-demand**, from Amazon's own pricing feed
(`b0.p.awsstatic.com/pricing/2.0/.../US East (Ohio)/Linux/index.json`), monthly = hourly x 730.

| tier | what you buy | price | **[I] expected app-RTT to the engine** |
|---|---|---:|---|
| 0 | this house, Atlanta | $0 | **28 ms [M]** |
| 0b | a "Chicago 1ms VPS" | $59.99/mo **[C]** | ~20-25 ms to the *engine*; the 1ms is a CDN edge **[I]** |
| 1 | `t4g.small` in us-east-2 | **$0.0168/h = $12.26/mo** | **~1-5 ms** |
| 2 | `c7g.large` in us-east-2 | $0.0723/h = **$52.74/mo** | ~1-5 ms (CPU headroom, not network) |
| 2b | `c7i.2xlarge`, tuned kernel, cluster placement group | $0.3570/h = **$260.61/mo** | ~1-5 ms, better tail |
| 3 | `c7g.metal` bare metal, dedicated, kernel bypass | $2.3123/h = **$1,687.98/mo** | ~1-5 ms minus ~0.1-0.3 ms of host stack |
| 3b | `c5n.metal` (100 Gbps, ENA/EFA) | $3.8880/h = **$2,838.24/mo** | as above |
| 4 | **PrivateLink / FIX / VPC peering** | **not for sale -- volume-gated** | removes the CDN hop entirely |
| 5 | **true colocation with the engine** | **does not exist** | -- |

Price anchors, all **[C]** from the AWS Ohio feed: `t4g.2xlarge` $0.2688/h, `c7g.xlarge` $0.1445/h,
`c7g.4xlarge` $0.5781/h, `c7g.metal` $2.3123/h, `c7i.2xlarge` $0.3570/h, `c7i.48xlarge` $8.5680/h,
`c5n.metal` $3.8880/h, `c6i.metal` $5.4400/h. Within-family linear scaling gives `t4g.small` =
$0.2688/16 = $0.0168/h and `c7g.large` = $0.1445/2 = $0.0723/h; the t4g.small figure is
independently confirmed at **[C]** [$12.2640/month](https://www.economize.cloud/resources/aws/pricing/ec2/t4g.small/).

### 2.2 So what does $20, $200 and $2,000 a month actually buy?

- **$20/month** buys the entire network improvement. A `t4g.small` in us-east-2 takes you from
  28 ms to low-single-digit ms. **[I] That is roughly 90-95% of all the latency reduction that
  exists to be had**, because the remaining distance is intra-region.
- **$200/month** buys CPU headroom and a better tail, not a better median. It is worth having
  once the strategy works; it does not make a strategy work.
- **$2,000/month** buys bare metal: no hypervisor, SR-IOV, kernel bypass. **[I] Realistically
  worth 100-300 microseconds** against a well-tuned VM. In a contest decided at the 2-5 ms scale
  (section 3), that is a rounding error.

**[I] The conclusion is the opposite of the usual HFT intuition: the latency ladder here is
almost flat above $12/month.** You cannot spend your way from 3 ms to 0.5 ms, because the
distance that remains is inside Amazon's network and inside Kalshi's application, and neither is
for sale.

### 2.3 Intra-region expectations, and why I will not pretend to a precise number

**[C]** AWS's own guidance: cluster placement groups give "tens of microseconds" within a rack;
same-AZ with enhanced networking is "sub-millisecond"; cross-AZ within a region is
"single digit millisecond"
([AWS Well-Architected / real-time comms whitepaper](https://docs.aws.amazon.com/whitepapers/latest/real-time-communication-on-aws/keep-traffic-within-one-availability-zone-and-use-ec2-placement-groups.html)).

**[I] The network term is therefore 0.2-2 ms depending on whether you land in Kalshi's AZ, which
you cannot choose and cannot learn.** On top of that sits Kalshi's own ELB plus application
processing, which I cannot measure without an account.
**[C]** A vendor blog claims "the WebSocket connection typically delivers updates within ~25ms of
the event occurring on Kalshi's matching engine"
([turbinefi.com](https://www.turbinefi.com/blog/prediction-market-arbitrage-latency-speed-2026));
I record that as weakly-sourced but directionally consistent with the CDN hop being expensive.
**I am quoting 1-5 ms as a range, not a number, and it is the single largest uncertainty in this
report.** It should be measured on day one with a demo account, before anything else is built.

### 2.4 There is no colocation to buy, and that is the good news and the bad news

**[I]** AWS does not sell rack space in us-east-2. There is no Equinix cage where Kalshi's engine
lives, because Kalshi's engine is an EC2 workload. The nearest thing to "colocation" is
**an EC2 instance in the same region -- which costs $12.26 a month and which anyone can buy.**

The consequence is symmetric and unpleasant: the cheapest tier is available to every competitor
too. There is no capital moat to climb over, which also means there is no capital moat protecting
you once you climb it. What separates participants is *tier access* and *software*, not spend.

### 2.5 Pricing the gate that actually matters

**[C] Kalshi rate-limit tiers** ([docs.kalshi.com/getting_started/rate_limits](https://docs.kalshi.com/getting_started/rate_limits)):

| tier | read tok/s | write tok/s | qualification |
|---|---:|---:|---|
| Basic | 200 | 100 | complete signup |
| Advanced | 300 | 300 | call the upgrade endpoint |
| Expert | 600 | 600 | (threshold not published in the doc I fetched) |
| **Premier** | 1,000 | 1,000 | **0.125% volume share** -- unlocks PrivateLink |
| Paragon | 2,000 | 2,000 | 0.25% volume share |
| Prime | 4,000 | 4,000 | 0.50% volume share -- unlocks VPC peering |

Order create costs 10 tokens, cancel costs 2. Qualification thresholds were **halved on
2026-06-25** ([changelog](https://docs.kalshi.com/changelog)) -- i.e. these are already the
relaxed numbers.

**[C] Kalshi exchange volume, 2026:** May $16.81B, June $21.1B, July between $37.7B and $41.05B;
$148B year-to-date, ~85% of the $173B lifetime total
([Pew](https://www.pewresearch.org/short-reads/2026/05/27/trading-volume-on-prediction-markets-has-soared-in-recent-months/),
[cryptotimes](https://www.cryptotimes.io/2026/07/05/kalshi-nears-10b-monthly-volume-as-prediction-markets-grow/),
[bitrss](https://bitrss.com/kalshi-s-2026-trading-volume-tops-148b-making-up-85-of-all-time-activity-239663)).

**[I] Premier tier therefore requires roughly $21M (on May volume) to $51M (on July volume) of
trailing-30-day taker notional.** Kalshi counts volume at $1 notional per contract, so that is
21-51 million contracts in 30 days, or 700,000 to 1.7 million contracts per day.

**[M] For scale: the median winning arbitrageur in our tape traded 10 contracts** (p50 across
1,049 reconstructed episodes; p90 = 148; max = 4,721).

**[I] The gate is not reachable from this strategy.** You cannot grind 21 million contracts a
month out of a $600/hour opportunity pool with a $10,000 account. PrivateLink and FIX are
therefore permanently out of reach, and **the CDN hop is permanent.** That is the single most
important sentence in this document.

---

## 3. The break-even

### 3.1 How much money is actually there -- recomputed independently

**[M] Method.** From `trades` only. Restrict to mutually-exclusive events with 2 or 3 legs
(`event_snapshots.mutually_exclusive = 1`, leg count from distinct tickers): 4,996 events, 603,603
trades in scope, 1,678 events with any tape. Aggregate trades into taker orders by
`(traded_at_us, ticker, taker_side)` with size-weighted price. For each anchor order, find the
best same-direction counterpart on every other leg within a window W. A realized Dutch book is
all-legs-YES with `sum(price) < 100`, or all-legs-NO-side with `sum(price) > 100`. Size = min
depth across legs, capped at $10,000 of collateral. Fees exact per Kalshi's published formula.
Overlapping candidates deduped greedily by value.

**[M] Fee model verified against a real fill.** The largest episode: 4,719 contracts, legs at 31c
and 92c. Gross = 4,719 x 23c = $1,085.37. Fee leg 1 = `ceil(0.07 x 4719 x 0.31 x 0.69 x 100)/100`
= $70.66; leg 2 = $24.32. Net = **$990.39**. The formula reproduces the tape exactly.

**[M] Results:**

| leg-pairing window | episodes | total net |
|---|---:|---:|
| 5 ms | 468 | **$3,709.94** |
| 50 ms | 858 | $4,534.36 |
| 1000 ms | 1,049 | $4,246.65 |

(The 1000 ms total is below the 50 ms total because the greedy non-overlapping dedupe merges
neighbouring opportunities at wide windows. Totals are not monotonic in W; the shape is.)

**[M] This independently reproduces the parent measurement.** Parent: 148 episodes /
$3,826.65 at 5 ms pairing, 7 episodes above $100, median episode $0.54, top episode 64% of total.
Mine: 468 episodes / **$3,709.94** at 5 ms, **8** episodes above $100, median episode $0.22-0.24,
top episode 23-27% of total. The episode *counts* differ because of grouping; **the dollar totals
agree to within 3%, and the >$100 episode count agrees to within one.** Two different groupings
converging on the same money is the strongest evidence available that the money is real.

**[M] Rate: $4,246.65 over 7.07 live-hours = $600.94 per live-hour.** The parent's $1,258/h is
about 2x higher; the difference is scope (I restricted to ME 2-3 leg only) and dedupe. Either
number is the *total pool shared by every participant*, not anyone's revenue.

### 3.2 The money is one tennis match

**[M] Concentration:**

```
top  1 episode  = $  990.39 = 23.3% of $4,246.65
top  5 episodes = $2,434.67 = 57.3%
top 10 episodes = $2,743.22 = 64.6%
top 20 episodes = $3,096.39 = 72.9%
median episode  = $0.24  -- you need 416 of them to make $100
```

Five of the top six episodes are the **same ticker** (`KXWTASETWINNER-26AUG26ALETAU-1`, a WTA set
winner market) inside a **two-second window at 04:41:36-37 UTC**. That is 58% of all the
Dutch-book money in seven hours of tape.

**[M] Hourly totals ($ net):** 2563, 425, 355, 193, 160, 153, 148, 121, 92, 25, 14.

**[M] Bootstrap.** Resampling episodes (n=20,000): $/live-hour p5=337, p50=582, p95=924 -- a 2.7x
spread. Block-bootstrapping by *clock hour*, which is the honest unit because episodes cluster:
**p5 = $129, p50 = $374, p95 = $806 per hour -- a 6.3x spread.**

**[I] The hourly rate is not a plannable quantity.** Any business case built on "$600/hour" is
built on a number whose 90% interval spans a factor of six, and whose central estimate is
dominated by a single two-second event. The correct planning posture is that this is a
lottery-ticket revenue stream with a floor near zero.

### 3.3 The latency-to-money curve -- the decisive table

**[M] Where the money went, bucketed by how fast the winner actually was** (gap between the
winner's two legs, 1000 ms window, 1,049 episodes, $4,246.65):

| winner's leg-to-leg gap | episodes | $ net | **share of money** |
|---|---:|---:|---:|
| 0 - 0.5 ms | 110 | 222.91 | 5.2% |
| 0.5 - 1 ms | 47 | 36.33 | 0.9% |
| 1 - 2 ms | 59 | 751.84 | **17.7%** |
| **2 - 5 ms** | 82 | **1,762.35** | **41.5%** |
| 5 - 10 ms | 179 | 235.65 | 5.5% |
| 10 - 25 ms | 166 | 193.90 | 4.6% |
| 25 - 100 ms | 83 | 120.17 | 2.8% |
| 100 - 1000 ms | 323 | 923.51 | 21.7% |

**65.3% of the money went to someone whose two legs landed within 5 ms of each other.**

**[M] Money you would have beaten, as a function of your total detect->decide->send->ack cycle:**

| your cycle | episodes still open | $ you'd have beaten | % of pool |
|---:|---:|---:|---:|
| 0.5 ms | 939 | 4,023.74 | 94.8% |
| 1 ms | 892 | 3,987.41 | 93.9% |
| 2 ms | 833 | 3,235.57 | 76.2% |
| **5 ms** | 751 | 1,473.22 | **34.7%** |
| 8 ms | 640 | 1,305.25 | 30.7% |
| 12 ms | 529 | 1,198.12 | 28.2% |
| 20 ms | 425 | 1,060.55 | 25.0% |
| **30 ms** | 390 | 1,022.29 | **24.1%** |
| 50 ms | 363 | 1,015.09 | 23.9% |
| 200 ms | 281 | 483.37 | 11.4% |

**[I] Read the cliff: 2 ms -> 5 ms costs you 41 percentage points of the pool.** That is the
entire game, and it sits exactly in the range where an in-region VM lands.

**[I] Two honest weaknesses in this table.** First, "the winner's leg-to-leg gap" is an upper
bound on how long the window was open, not the window itself -- a trade tells you when someone
traded, not when the quote was available. Second, the 100-1000 ms bucket (21.7%) is the least
trustworthy: a participant whose legs are 300 ms apart was probably *choosing* to leg in, not
losing a race, so most of that $923 is not actually contestable. Strip it out and the picture is
worse, not better.

### 3.4 What the tape looks like when the big money moves

**[M] The $990.39 episode, reconstructed print by print** (`KXWTASETWINNER-26AUG26ALETAU-1`,
04:41:36.822257 UTC, orders >= 50 contracts, t0 = the triggering sweep):

```
  t0 +   0.000 ms   ALE   yes  17,552 @ 71.3c    <- the sweep repricing leg ALE
  t0 +   8.590 ms   TAU   no    4,719 @ 31.0c    <- arbitrageur, leg 1
  t0 +  12.246 ms   ALE   no    4,719 @ 92.0c    <- arbitrageur, leg 2, SIZE-MATCHED
  t0 +  15.107 ms   TAU   no    2,000 @ 30.0c    <- a second participant arrives
  t0 +  15.143 ms   TAU   no    1,000 @ 30.0c
  t0 +  17.492 ms   ALE   no    3,656 @ 92.0c
  t0 +  18.024 ms   TAU   no    3,004 @ 30.0c
  t0 +  30.986 ms   TAU   no      907 @ 26.3c    <- price has degraded 31.0c -> 26.3c
  t0 +  31.909 ms   ALE   no    3,004 @ 92.0c
```

**[M] Reaction time from sweep to first arbitrage leg: 8.590 ms. Leg to leg: 3.656 ms. The best
prices are gone by t0+18 ms and the episode is materially over by t0+32 ms.**

**[I] Our house cannot participate in this, at all.** Our order cannot reach the engine before
t0 + 28 ms even with zero compute time and instant data delivery -- and realistically the market
data does not reach us until ~t0+14 ms, leaving nothing. We arrive as the last prints are
degrading, into a book that has already been swept twice.

### 3.5 Break-even, stated properly

**[I] Infrastructure break-even is trivial and therefore not the question.** A `t4g.small` costs
$0.0168/hour. Against a $600/hour pool that is **0.003%**. You break even on infrastructure by
capturing roughly two median episodes per day. At $2,838/month for `c5n.metal` you still only need
$3.89/hour -- 0.6% of the pool. **Money is not the constraint at any tier on this ladder.**

**[I] The real break-even is capture share, and it is winner-take-all per episode.** Modelling it:

| tier | **[I]** cycle | pool beaten **[M]** | **[I]** plausible capture after competition | **[I]** $/live-hour |
|---|---|---:|---|---:|
| this house | 28 ms **[M]** | 24.1% | ~0% of races; the >100ms bucket is mostly deliberate legging | **~$0** |
| us-east-2 VM, good software | 2-5 ms | 34.7-76.2% | contested by an incumbent field; 1/N of the contested set | **$30-150** |
| us-east-2 VM, excellent software | ~2 ms | 76.2% | still no PrivateLink; still a CDN hop the winners lack | **$50-200** |
| bare metal, same region | ~1.7 ms | ~80% | +0.1-0.3 ms over the VM -- immaterial at this scale | **$55-210** |
| Premier + FIX + PrivateLink | <1 ms | 94.8% | not purchasable at $10k (section 2.5) | -- |

The capture-share column is **judgment, not measurement**, and I want that flagged loudly. It
rests on: (a) at least three distinct participants were visibly racing in the one episode I
reconstructed at microsecond resolution; (b) 157 episodes had sub-1ms leg gaps, which implies
batched or pre-signed atomic submission that a REST-over-CDN client structurally cannot match;
(c) the field already occupies the 2-5 ms bucket where 41.5% of the money is.

**[I] The break-even threshold, named as requested: there isn't a spend threshold, because the
curve is flat above $12/month.** Going from $12 to $2,838 a month buys you a few hundred
microseconds and no additional pool. The threshold that exists is a *volume* threshold --
~$21-51M of trailing 30-day notional to reach Premier -- and it is roughly three orders of
magnitude beyond a $10,000 account.

### 3.6 The risk the arbitrage framing hides

**[I] This is riskless at settlement, not at execution.** You are lifting stale bids on two legs
in sequence. If the first fills and the second does not, you are holding a naked directional
position in a market that has just moved violently -- which is the exact circumstance that created
the opportunity.

**[M] Sizing that risk on the largest episode:** you win TAU-no at 31c for 4,719 contracts and
miss ALE-no at 92c. You are now short 4,719 YES on TAU at 31c. From the tape above, TAU YES traded
as low as **12.7c** and as high as **50c** within 170 ms of the sweep.

| adverse mark | loss | vs the $990.39 you were chasing |
|---:|---:|---:|
| 5c | $235.95 | 0.2x |
| 10c | $471.90 | 0.5x |
| 20c | $943.80 | 1.0x |
| 31c | $1,462.89 | 1.5x |

**[I] You are risking roughly one-to-one to capture a 23c edge, on a leg you will miss precisely
when the market is fastest.** The expected value calculation in 3.5 is a *gross* number; any
honest version must subtract leg-out losses, and I have no way to estimate the miss rate without
live trading. It is not small: you are, by construction, the slowest participant in the race.

---

## 4. What would the software have to be? Is Python viable?

### 4.1 Python is not the bottleneck. The architecture is.

**[M] Hot-path microbenchmarks on this machine** (CPython, 20,000 iterations each after warmup):

| operation | p50 | p99 |
|---|---:|---:|
| `json.loads` of an `orderbook_delta` message | **1.7 us** | 2.8 us |
| `json.dumps` of an order payload | 1.3 us | 1.7 us |
| `asyncio.Queue` put + get hop | 0.3 us | -- |
| **RSA-PSS-2048 sign (Kalshi auth, per request)** | **560.9 us** | **1,118.6 us** |
| `gc.collect(0)` with 300k live objects | ~0 us | ~0 us |

**[I] Parsing and decision logic in Python cost about 3 microseconds.** Against a contest decided
at 2-5 *milliseconds*, that is 0.1% of the budget. **The parent's measured 519 ms p50 consumer
queueing delay at 100 tickers is therefore not a language problem -- it is roughly 170,000x the
per-message cost, which means the consumer is falling behind for structural reasons (unbounded
queue, blocking I/O or SQLite writes on the consumer path, GIL contention with the writer), not
because CPython is slow.** That is fixable in Python.

### 4.2 The one thing that genuinely hurts, and the trick that fixes it

**[M] The RSA-PSS signature is 561 us p50 and 1,119 us p99, and Kalshi requires one per request.**
That is 20-40% of a 3 ms budget spent on cryptography, and it is *not* a Python problem -- the
work happens in OpenSSL and a C++ client pays nearly the same.

**[C] But Kalshi's signature covers `{timestamp}{method}{path}` only -- not the request body**
([WS quick start](https://docs.kalshi.com/getting_started/quick_start_websockets): "sign the
string `{timestamp}GET/trade-api/ws/v2` (no query params, no body)").

**[I] Therefore signatures can be pre-computed.** A serious participant pre-signs a rolling buffer
of `(future_timestamp, POST, /trade-api/v2/portfolio/orders)` tuples on a background thread and
pulls one off the shelf at fire time, removing ~561 us from the critical path. A naive bot signs
inline and eats it. **This is a concrete, measurable 0.5 ms handicap that separates a careful
implementation from a casual one, and it is very likely part of why some participants pair legs in
under 1 ms.**

### 4.3 What the architecture would have to be

**[I]** For a genuine attempt:

1. **Run in us-east-2.** Nothing else matters until this is true.
2. **Single process, one thread on the hot path**, pinned, no queue between decode and decide.
   The 519 ms queueing delay says the current design hands messages across a boundary; delete the
   boundary for the tickers you actually trade.
3. **Subscribe narrowly.** Not 100 tickers -- the ME 2-3 leg sports events in the next hour.
   Everything else is a separate, slow process.
4. **Pre-signed auth buffer** (4.2).
5. **Pre-built order payloads.** For each live ME event, keep both legs' order bodies serialized
   and ready with only `count` and `yes_price` to patch. Byte-patch, do not re-serialize.
6. **Warm, pinned HTTP/2 connections** to the order endpoint, TCP_NODELAY, no connection churn.
7. **Batch both legs into one submission** where the API allows it -- the 157 sub-1ms episodes
   suggest the winners are doing exactly this. If Kalshi's batch-create is atomic across tickers,
   it also removes the leg-out risk from 3.6, which would matter more than the latency.
8. **Bounded queues that drop, never grow.** Stale market data is worse than no market data.

**[I] Is that a realistic solo build? Yes -- in about the same effort as the existing recorder.**
None of the above is exotic; it is careful engineering, not systems research. Python is adequate
for all of it. **The software is the *easy* part of this problem, which is precisely why it is not
where the edge is.**

---

## 5. Access reality

Kalshi is a CFTC-regulated DCM, and a live account carries the onboarding requirements the exchange
publishes: 18 or older, a government-issued photo ID (driver's licence or passport), name and date of birth
matching the ID, a residential address that is not a PO box and not commercial, document verification on
request, and the CFTC may additionally require proof of address, employment information or source of funds
([signing up](https://help.kalshi.com/en/articles/13823778-signing-up-as-an-individual),
[verification](https://help.kalshi.com/en/articles/13823782-what-information-is-required-to-verify-my-kalshi-account)).

**[C] Kalshi's own help centre now says** "Yes, you can trade on Kalshi from many countries," and "No, you
do not need a United States phone number to sign up for Kalshi," subject to the Member Agreement's
geographic terms
([help centre](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states)),
following the XP/Brazil deal
([Bloomberg](https://www.bloomberg.com/news/articles/2026-03-09/kalshi-teams-up-with-brazil-s-xp-for-first-international-push)).

**[M] I fetched three separate Kalshi help pages and none of them enumerates the acceptable taxpayer
identifiers.** Third-party guides fill that gap with claims the exchange does not make. The honest status is
that the requirement is unverified against current documentation, it is answerable by one enquiry to the
exchange, and it is not something to plan around either way.

None of this affects the measurement below. Every number in this file came from public unauthenticated
endpoints.

---

## 6. Honest verdict

**Marginal, and trending closed.**

Taking the six questions in order:

**1. Where is the engine?** **AWS us-east-2 (Ohio)**, proven by resolving Kalshi's own FIX
order-entry and market-data hostnames into Amazon's published `us-east-2 / EC2` prefixes, with an
`awselb/2.0` origin banner and independent third-party corroboration. The public REST and
WebSocket host is a CloudFront distribution, and every "Kalshi is 1ms from Chicago" claim in the
VPS-vendor literature is measuring a CDN edge serving 12-second-old cached data.

**2. What latency is purchasable?** Effectively one useful step, and it costs **$12.26/month**
(`t4g.small`, us-east-2), taking you from a **measured 28 ms** to an estimated 1-5 ms. Above that
the ladder is flat: $2,838/month of bare metal buys perhaps 300 microseconds. **True colocation
does not exist to buy**, because the engine is a cloud workload, not a caged server. The step that
*would* matter -- PrivateLink or FIX, removing the mandatory CDN hop -- is gated at Premier tier,
which needs roughly **$21-51 million of trailing 30-day notional**. That is not a price. It is a
wall.

**3. Break-even?** There is no spend threshold to name, because infrastructure costs 0.003% of the
pool at the bottom of the ladder and 0.6% at the top. The constraint is capture share in a
winner-take-all race where **65.3% of the money goes to participants pairing legs within 5 ms**,
where the 2 ms -> 5 ms transition costs 41 percentage points of the pool, and where the pool
itself is **$600/live-hour with a 6.3x block-bootstrap confidence interval** and **57% of it
concentrated in one tennis match's two-second window**. From this house the realistic capture is
approximately zero. From us-east-2 with excellent software it is a contested slice of a
lottery-shaped revenue stream, against a field that already has the CDN hop removed.

**4. Software?** **Python is fine.** Measured hot-path cost is ~3 microseconds per message against
a millisecond-scale contest; the 519 ms queueing delay is an architecture defect, not an
interpreter limit. The one real cost is Kalshi's mandatory per-request RSA-PSS signature at
**561 us p50 / 1,119 us p99**, and because the signature covers only `{timestamp}{method}{path}`
it can be **pre-computed off the hot path**. This is a realistic solo build. It is also the
easiest part of the problem, which is exactly why it is not the edge.

**5. Access?** The premise has shifted: Kalshi launched globally in 140+ countries, and its current
public sign-up documentation does not enumerate acceptable taxpayer identifiers at all. That is not
resolvable from public sources, only by asking the exchange. **No workaround involving misstated
residency is acceptable or on the table.** Read access, which is what every measurement in this file
used, needs no account either way.

**6. Is it reachable?** Reachable in the narrow technical sense: $12/month and a few weeks of
careful engineering put you in the race. **Not reachable in the sense that matters**, because
(a) the last 2 milliseconds separating you from 65% of the money are behind a CDN hop that only a
$21M/month volume tier can remove, (b) the revenue is a six-times-uncertain lottery whose central
case is a single two-second event you must be fastest for, and (c) you are risking roughly
one-to-one on leg-out while being, by construction, the slowest participant in the race.

**[I] The prior analysis said "colocation's edge" and stopped. That was the right answer for the
wrong reason.** Colocation is not the barrier -- colocation costs twelve dollars a month and
anyone can have it. The barrier is that Kalshi sells the last two milliseconds only to
participants who already trade tens of millions of dollars a month, and those two milliseconds are
where two-thirds of the money lives. **A well-evidenced no.**

---

## 7. If it is pursued anyway: the three things to measure first

**[I]** In order, cheapest first. Each one can kill the idea before the next is worth doing.

1. **Resolve access, honestly** (section 5). Email Kalshi support; speak to the international
   student office. Cost: two emails. **Everything else is moot until this returns a yes.**
2. **Measure the actual in-region RTT.** Spin up a `t4g.small` in us-east-2 ($12.26/month), point
   it at the *demo* environment, and measure uncacheable app-level RTT to the order endpoint over
   a warm connection. **This is the single largest uncertainty in the report** (section 2.3). If
   it comes back above ~5 ms, the CDN hop has already eaten the edge and the answer is a firm no.
3. **Fix the recorder before trusting any more numbers.** The 5-second poll (section 0) means
   `market_snapshots` cannot see this phenomenon at all. Move to `orderbook_delta` over the
   WebSocket and record with the microsecond receive timestamp. Until then, every book-based
   conclusion in this repo is sampled at 0.2 Hz against a 30-millisecond phenomenon.

---

## Sources

- [Kalshi FIX connectivity](https://docs.kalshi.com/fix/connectivity)
- [Kalshi rate limits and tiers](https://docs.kalshi.com/getting_started/rate_limits)
- [Kalshi API changelog](https://docs.kalshi.com/changelog)
- [Kalshi WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets)
- [Kalshi: signing up as an individual](https://help.kalshi.com/en/articles/13823778-signing-up-as-an-individual)
- [Kalshi: what information is required to verify my account](https://help.kalshi.com/en/articles/13823782-what-information-is-required-to-verify-my-kalshi-account)
- [Kalshi: can I trade from outside the United States](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states)
- [AWS published IP ranges](https://ip-ranges.amazonaws.com/ip-ranges.json)
- [AWS EC2 on-demand pricing](https://aws.amazon.com/ec2/pricing/on-demand/)
- [AWS: keep traffic within one AZ, use placement groups](https://docs.aws.amazon.com/whitepapers/latest/real-time-communication-on-aws/keep-traffic-within-one-availability-zone-and-use-ec2-placement-groups.html)
- [economize.cloud: t4g.small pricing](https://www.economize.cloud/resources/aws/pricing/ec2/t4g.small/)
- [Glassnode prediction-market latency monitor: methodology](https://latency.glassnode.com/prediction-markets/about)
- [QuantVPS: "Where are Kalshi's servers located?"](https://www.quantvps.com/blog/kalshi-servers-location) -- cited as the claim being refuted
- [TradoxVPS: "Why Kalshi traders need a 1ms VPS in Chicago"](https://tradoxvps.com/why-kalshi-traders-need-a-1ms-vps-in-chicago/) -- same
- [Turbine: prediction market arbitrage latency](https://www.turbinefi.com/blog/prediction-market-arbitrage-latency-speed-2026)
- [Pew Research: prediction market trading volume](https://www.pewresearch.org/short-reads/2026/05/27/trading-volume-on-prediction-markets-has-soared-in-recent-months/)
- [Crypto Times: Kalshi nears $10B monthly volume](https://www.cryptotimes.io/2026/07/05/kalshi-nears-10b-monthly-volume-as-prediction-markets-grow/)
- [BitRss: Kalshi 2026 volume tops $148B](https://bitrss.com/kalshi-s-2026-trading-volume-tops-148b-making-up-85-of-all-time-activity-239663)
- [Brave New Coin: Kalshi raises $300M, launches in 140 countries](https://bravenewcoin.com/insights/kalshi-raises-300-million-and-launches-in-140-countries)
- [Kalshi newsroom: $5B valuation amid international expansion](https://news.kalshi.com/p/kalshi-hits-5-billion-valuation-amid-international-expansion)
- [Bloomberg: Kalshi teams up with Brazil's XP](https://www.bloomberg.com/news/articles/2026-03-09/kalshi-teams-up-with-brazil-s-xp-for-first-international-push)
- [IRS: taxpayer identification numbers for foreign students and scholars](https://www.irs.gov/individuals/international-taxpayers/taxpayer-identification-numbers-tins-for-foreign-students-and-scholars)
- [Kalshi fees](https://help.kalshi.com/en/articles/13823805-fees)
