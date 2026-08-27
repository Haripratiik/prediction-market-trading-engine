# 10 -- Dislocation Microstructure: What the Only Real Arbitrage Actually Is

Written 2026-08-27, as a follow-up to `research/09-edge-reality-check.md` section 1.2. That
section found 3 events with a net-positive Dutch book, $44.08 total, and reported that 83% of
126 episodes lasted a single 5-second poll. It called this "a latency prize, not a strategy".
This file characterises the prize properly, because it is the only arbitrage our own data says
exists.

Tags, same as 09:

- **[M]** MEASURED -- computed by me from `data/pm.db` in this session, read-only
  (`file:data/pm.db?mode=ro`). Query or arithmetic given inline.
- **[C]** CITED -- someone else's published number.
- **[I]** INFERRED -- my reasoning on top of [M] or [C]. Weakest tier.

Fee arithmetic uses the repo's own `core.math.contracts.fee(price, spec, is_maker=False)` with the
per-series `FeeSpec` from `series_cache`, rounded up to the cent per order, exactly as Kalshi
charges. Structure legality uses `rulebook.exhaustiveness.check_mece`. Nothing is reimplemented.

**All numbers are frozen at a cutoff of `observed_at_us <= 1787839200000000`
(2026-08-27 14:00:00 UTC), because the recorder is still writing.**

---

## 0. The headline, before the detail

**[M] Two things are true at once and they point in opposite directions.**

1. **Almost everything our REST quote feed calls a dislocation is not one.** Of 68 net-positive
   observations that had a later observation to check against, **67 were gone by the first poll
   after every leg had refreshed once** (median wait 10.0 s). The feed's apparent arbitrage is
   dominated by our own measurement artifact, described in section 1.
2. **Real, simultaneous, executable Dutch books do exist, they are large, and other people are
   taking them.** Measured from the millisecond trade tape with no reference to our polled quotes:
   **248 realized 2-to-6-leg Dutch books worth $7,235.60 net of fees in 5.75 hours** of tape --
   $1,258/h. At the most conservative pairing rule (legs within 5 ms) it is still **148 episodes and
   $3,826.65**.

The prize is roughly **100x larger** than research/09 estimated, and it is roughly **1,000x faster**
than research/09 could see. Both errors came from the same source: measuring a millisecond market
with a 15-second instrument.

---

## 1. The instrument problem: the public quote feed is a 15-second cache

This governs every other number in this file, so it goes first.

**[M] Kalshi's public `/markets` L1 payload refreshes on a per-ticker ~15-second grid.**

Method: for every ticker in every 5-second tape segment, collapse consecutive identical
`(yes_bid, yes_ask, yes_bid_size, yes_ask_size)` tuples and measure the gap between changes.

```
inter-change gaps on the L1 quote feed          : n = 54,179
share that are an EXACT multiple of 15.0 seconds: 0.932

top gaps: 15.0s (28,378)  30.0s (6,899)  45.0s (3,710)  60.0s (2,240)
          75.0s (1,571)   20.0s (1,372)  90.0s (1,177)
```

Per segment the share of exact 15 s multiples is 1.000 / 0.918 / 0.791 / 0.983 / 1.000. The 20.0 s
entries are our own poll clock drifting one 5 s slot relative to the grid; they are the phase-slip
signature, not a counterexample.

**[M] The cumulative `volume` counter moves on the same grid.** Run lengths of an unchanged
`volume` field are 3, 6, 9, 12, ... polls -- never 4, 5, 7, 8. A cumulative trade counter cannot
genuinely stand still for 15 s in a market printing dozens of trades a second, so this is a property
of the *payload*, not of the market.

**[M] Different tickers sit on different phases, and the phase drifts.** Within one segment, 166
tickers changed only at one phase mod 15 s, 108 at two, and the two-phase set is what you get when
the grid boundary walks past our 5 s poll clock over an hour.

**[I] The mechanism is a ~15 s TTL cache in front of the public `/markets` endpoint, keyed
per-ticker.** I cannot see the cache, but every observable consequence of one is present and no
alternative explains a frozen cumulative counter.

### 1.1 What this does to a two-leg measurement

If leg A's cache generation flips at t and leg B's flips at t+5 s, then for those 5 seconds our
"synchronous" snapshot pairs a **fresh** quote on A with a quote up to 15 s **stale** on B. During
in-play repricing that manufactures an overround out of nothing.

**[M] Tape, `KXITFMATCH-26AUG26JIASUN`, our own 5-second quote recorder:**

```
   06:15:35 | JIA  62/ 64 bs=    10 | SUN  29/ 33 bs=  316    sum(bid)= 91
   06:15:40 | JIA  62/ 64 bs=    10 | SUN  36/ 37 bs= 1182    sum(bid)= 98
   06:15:45 | JIA  62/ 64 bs=    10 | SUN  36/ 37 bs= 1182    sum(bid)= 98
   06:15:50 | JIA  80/ 81 bs=   947 | SUN  36/ 37 bs= 1182    sum(bid)=116   <-- "16c arbitrage"
   06:15:55 | JIA  80/ 81 bs=   947 | SUN  19/ 20 bs= 1423    sum(bid)= 99
```

JIA refreshes at :05/:20/:35/:50; SUN refreshes at :10/:25/:40/:55. **The two legs are on 15-second
grids offset by 5 seconds.** The "$125.63 net" at 06:15:50 is the width of that offset, not the width
of a book. Five seconds later it is 99.

**[M] The trade tape proves the quote was wrong, not the market.** At 06:16:00.458, SUN printed
seven trades at 30-31c with `taker_side='yes'` -- i.e. someone lifted a SUN ask at 31 -- while our
cached quote for SUN still read `19/20`. The quote was off by 11 cents.

### 1.2 The consequence, stated plainly

**[M] Restricting to observations where every leg's quote had already survived a full refresh
cycle removes essentially all of the money.** (Four long tape segments; each leg must have >= 3
prior polls in the segment.)

| min leg-quote age at the observation | obs | sum(bid)>100 | rate | net-positive | net $ upper bound |
|---|---:|---:|---:|---:|---:|
| < 5 s (a leg just jumped) | 7,973 | 457 | 5.73% | 28 | $440.66 |
| 5-15 s | 14,605 | 825 | 5.65% | 42 | $408.67 |
| 15-60 s (every leg confirmed by >= 1 refresh) | 20,516 | 1,040 | 5.07% | **2** | **$3.31** |
| > 60 s | 60,406 | 3,050 | 5.05% | **0** | **$0.00** |

The raw `sum(bid) > 100` rate is flat across the buckets -- that is the 1-2c noise on far-dated
illiquid books, and it is not age-dependent. **All of the dollars are in the two buckets where at
least one leg had just moved.**

**[I] This does not prove the fresh-leg cases were fake; a real dislocation also occurs exactly when
one leg has just moved. It proves the two are not separable with this instrument.** Section 3
separates them with a different instrument.

---

## 2. What the polled quote feed says, for the record

Reproducing research/09's rule on the larger archive: mutually-exclusive events only (the exchange
flag, never inferred -- research/09 section 2 and E19), every known leg present at the same
`observed_at_us`, all legs `active`, `yes_bid_size >= 1` on every leg.

**[M]**

```
synchronous complete ME observations         : 115,962
  sum(YES bid) > 100 with depth >= 1         :   5,533   (4.77%)
  NET POSITIVE after per-series taker fees   :      75   across 8 events
  upper bound, best observation per episode  : $437.92   across 26 episodes
```

(The naive sum over all 75 observations is $853.60, but persistent runs are counted three times
each because the 15 s cache is sampled by three 5 s polls. $437.92 is the non-double-counted
figure. Both are upper bounds on a quantity section 1 has already shown to be mostly artifact.)

**[M] Duration, measured two ways.**

| duration of an episode (5 s tape only, n=93) | count | share |
|---|---:|---:|
| 1 poll | 12 | 13% |
| 2 polls | 9 | 10% |
| **3 polls** | **52** | **56%** |
| 4-6 polls | 7 | 8% |
| longer | 13 | 14% |

The mode at exactly 3 polls is the 15-second cache generation, sampled 3x. Recounting in **distinct
quote generations** instead of polls:

```
1 generation : 75 of 93 episodes  (81%)
2 generations:  4
3+           : 14   (max 150 generations = a 6,211 s run of 1c noise on an illiquid election book)
```

**[M] 49 further "episodes" are single observations inside one of the two full-universe sweeps**
(2026-08-26 19:49:38 and 2026-08-27 04:27:20, ~107k markets each). They have no neighbour in time
at all and carry **zero** duration information. research/09's 126-episode count mixed these in.

**[M] The forward test.** For every net-positive observation, find the first later poll at which
*every* leg has changed at least once, and re-evaluate:

```
GONE                              67
STILL net-positive                 1
no observation after the refresh   4
median wait for a full refresh  10.0 s
median overround before -> after:  4c -> -1c
```

**[I] One survivor out of 68. A signal with a 1.5% survival rate over a 10-second decision lag is
not a trading signal; it is a description of our own sampling.**

---

## 3. The venue clock: real dislocations, measured without our quotes

The trade tape is a different instrument and a far better one.

**[M] `trades` carries venue timestamps at millisecond resolution and Kalshi's own `taker_side`
label** -- no Lee-Ready inference needed (`recorder/l1.py` docstring; 1,923,854 rows by the cutoff).

The inference that makes this work:

```
taker_side = 'no'  =>  the taker BOUGHT NO at (100 - yes_price)
                   =>  they SOLD YES at yes_price
                   =>  bid_YES >= yes_price at that instant, at the venue, in venue time.
```

So two such prints on two legs of a `mutually_exclusive` event, close together, **prove**
`bid_A + bid_B > 100` on the real book. Our polling is not involved.

**[M] The semantics check out.** Comparing every print in the 07:58-09:58 segment against the next
observed quote for that ticker:

```
taker='yes' price minus quoted ASK : median 0c,  89.2% within 1c   (n=4,746)
taker='no'  price minus quoted BID : median 0c,  84.1% within 1c   (n=1,349)
```

### 3.1 The result

**[M] Scanning all 6,689 `mutually_exclusive` events (2,928 two-leg, 2,549 three-to-six-leg) over
5.75 hours of trade tape, clustering YES-sells within 500 ms, and greedily matching best prices:**

| | clusters | net-positive | net after fees | contracts | events |
|---|---:|---:|---:|---:|---:|
| 2-leg | 347 | 196 | **$6,518.24** | 59,862 | 86 |
| 3-6 leg | 85 | 52 | **$717.36** | 5,254 | 20 |
| **total** | 432 | **248** | **$7,235.60** | 65,116 | 106 |

`$7,235.60 / 5.75 h = $1,258/h`. Zero of these clusters contain a block trade (`is_block`).

**[M] Robustness to the pairing rule.** The only real objection is that during violent repricing,
two prints 400 ms apart need not have coexisted on one book. Tightening the maximum separation
between the legs:

| max separation between legs | episodes | contracts | net |
|---|---:|---:|---:|
| 5 ms | 148 | 26,832 | **$3,826.65** |
| 10 ms | 209 | 42,526 | $4,559.47 |
| 50 ms | 230 | 59,840 | $6,737.95 |
| 100 ms | 225 | 60,917 | $6,927.34 |
| 500 ms | 248 | 65,116 | $7,235.60 |

**[I] At 5 ms the "genuine repricing between the legs" explanation is dead** -- nothing reprices a
book and reprints the other leg inside five milliseconds -- and $3,826.65 remains. The finding is
robust. Use $3,827 as the floor and $7,236 as the ceiling.

**[M] The MECE gate agrees these are legal structures.** Running
`rulebook.exhaustiveness.check_mece` on the top 8 money events, using the last quote before each
episode: all 8 are `MECNET`, all 8 return `safe_to_sell = True`. Seven are `NEEDS_HUMAN` (the void-
clause condition, which only gates *buying*); one, `KXCOPADOBRASILGAME-26AUG26VDGVIT`, is `REJECTED`
for non-exhaustiveness -- which, per the module's own docstring, makes it *better* for a seller, not
worse.

### 3.2 The tape, in full, for the largest one

**[M] `KXWTASETWINNER-26AUG26ALETAU-1`, 2026-08-27 04:41:36 UTC.** Two legs, ALE and TAU, MECNET.

```
04:41:36.822  ALE  taker=yes  69,70,71,71,76,80,81       ~17,552 ct    <-- a buy sweep lifts
                                                                          ALE's entire ask ladder
04:41:36.830  TAU  taker=no   31 x4619, 31 x100          <-- 8 ms later: sell YES on TAU at a
04:41:36.834  ALE  taker=no   92 x4719                       stale 31 bid, and sell YES on ALE
                                                             at its new 92 bid.  31 + 92 = 123.
04:41:36.837  TAU  taker=no   30 x2000, 30 x1000
04:41:36.840  TAU  taker=no   30 x3004
04:41:36.846  ALE  taker=no   92 x100
04:41:36.853  TAU  taker=no   29 x704, 19 x100, 15 x100, 13 x2, 10 x1
04:41:36.854  ALE  taker=no   92 x3004
   ...
04:41:36.955  TAU  taker=yes  11 x26, 11 x2000, 16 x1000    <-- refill of the emptied TAU bid
```

Read it once and the whole mechanism is visible:

- A large buy sweep on ALE moves ALE's price ~12c in one message batch.
- TAU's resting bids at 31/30/29 should have been pulled the instant ALE moved. They were not.
- **Eight milliseconds later** an arbitrageur sells YES on both legs.
- The `4,619 + 100 = 4,719` on TAU exactly equals the `4,719` on ALE. That is one actor firing a
  matched pair, not two coincidental takers.
- The stale TAU bid ladder is then eaten from 31 down to 10 within 23 ms.

**[M] Size of that pair alone**, priced with `core.math.contracts` and `KXWTASETWINNER`
(`fee_type='quadratic'`, `fee_multiplier=1.0`):

```
gross  4,719 x (123 - 100)c                                       = $1,085.37
fee    ceil(0.07*0.31*0.69*4719*100)/100 + ceil(0.07*0.92*0.08*4719*100)/100
     = $70.66 + $24.31                                            =    $94.97
net                                                               =   $990.40
```

The full 500 ms cluster, matched greedily across every qualifying price level, is 21,906 contracts
for **$4,597.52 net** -- 64% of the entire 5.75-hour total.

---

## 4. Q1 -- What creates a dislocation

**[M] Classification of all 248 realized episodes, from tape evidence in the 3 seconds before the
first arbitrage print.** "Travel" is the max-minus-min print price on a leg in that window.

| cause | episodes | net $ | share of $ |
|---|---:|---:|---:|
| **large taker sweep repriced one leg** (>= 500 ct swept AND >= 3c travel) | **86** | **$6,488.31** | **90%** |
| rapid repricing with no single large sweep (>= 3c travel) | 32 | $296.25 | 4% |
| large taker print, little price travel | 30 | $236.77 | 3% |
| no print at all in the prior 3 s -- quote-led reprice, invisible to the tape | 36 | $129.32 | 2% |
| first minute of trading in the event (listing / open artifact) | 6 | $33.54 | <1% |
| quiet book, 1-2c noise | 58 | $51.41 | <1% |

**[M] By count the population is bimodal**: 86 sweep-induced events carrying 90% of the money, and
58 quiet-book 1-2c episodes carrying essentially none. **[I] The second group is the same population
research/09 found in far-dated illiquid politics books -- real in the sense that the prints happened,
worthless in the sense that the median episode there is worth under a dollar.**

**Answering the specific hypotheses in the brief:**

- **One leg's bid lagging during rapid repricing** -- yes, and it is the dominant cause. But the
  lag is a *maker cancel latency* of 10-50 ms, not the seconds our quote feed suggested.
- **A large taker print sweeping one side** -- yes, this is the trigger in 116 of 248 episodes
  (86 + 30) and 93% of the money. The causal chain is: taker sweeps leg A -> leg A reprices ->
  makers on leg B have not cancelled yet -> the pair is inconsistent for tens of milliseconds.
- **New-listing / first-quote artifact** -- **[M] only 6 episodes, $33.54.** Real but negligible in
  this window. **[I]** Our window contains almost no listings of new liquid series; a Sunday NFL
  morning would contain many.
- **Settlement approaching** -- **[M] not a driver.** Of the 70 episodes whose event we actually
  observed finalize, the median was **51 minutes** before close, only 1 was inside 5 minutes, and
  6 were more than 2 hours out. Note the data caveat in section 8.
- **A stale leg in an otherwise-moving event** -- this is the same thing as the first bullet, and
  section 1.2 shows it is *also* what our own instrument fabricates. On the venue clock it is real;
  on the REST clock it is mostly not.

**[M] The signature that the takers are deliberate arbitrageurs, not coincidence:** in **35 of 196**
two-leg episodes the two legs traded the **same quantity to within 2%**.

---

## 5. Q2 -- How long do they really last

### What the 5-second poll can and cannot establish

**[M] It cannot establish anything about duration below 15 seconds, and it never could.** The
polling interval is 5 s but the *information* interval is the cache's 15 s. An episode "seen in
exactly one poll" does not mean "0 < duration <= 10 s"; it means our two legs were read from cache
generations up to 15 s apart. The correct statement about the quote feed is:

> An episode visible in exactly one quote generation lasted somewhere in (0, 30] seconds, and may
> not have existed at all.

Nothing in the polled data licenses a tighter claim, and the survival curve in research/09 should
be read as a survival curve of *our sampling*, not of the market.

### What the venue clock establishes

**[M] Lifetime of a realized 2-leg dislocation**, defined as the span from the first to the last
qualifying print, with episodes separated by a stated gap threshold:

| gap threshold | episodes | median | p75 | p90 | max |
|---|---:|---:|---:|---:|---:|
| 100 ms | 300 | 0.0 ms | 12.8 ms | 47.6 ms | 923 ms |
| 250 ms | 274 | 0.0 ms | 19.4 ms | 114.9 ms | 923 ms |
| 1,000 ms | 267 | 0.2 ms | 21.1 ms | 150.3 ms | 923 ms |
| 5,000 ms | 251 | 2.0 ms | 44.9 ms | 255.0 ms | 6,146 ms |

At a 100 ms threshold the shape is: **172 episodes are a single print (unresolvable, span 0), 46 are
under 10 ms, 71 are 10-100 ms, and 11 reach 0.1-1 s.**

**[M] Censoring, stated honestly.** This measure is censored from *below*, not above: an episode
that nobody traded twice has span 0 and we cannot tell whether the book was inconsistent for 1 ms or
400 ms. It is also censored by the tape's own coverage (5.75 h in 5 disjoint segments, section 7.1).
What it is **not** is censored from above -- the maximum observed is 923 ms at every threshold up to
1 s, and the longest thing in the 5 s bucket (6,146 ms) is a chain of separate re-openings on one
event, not one continuous book.

**[I] The honest summary: the median executable dislocation on Kalshi lives for a single print;
the p90 lives under 150 milliseconds; nothing observed survived one second.** The 5-second poll was
never within three orders of magnitude of resolving this.

---

## 6. Q3 -- How much money is actually in them

### Theoretical maximum

**[M] $7,235.60 net of correct per-series taker fees over 5.75 hours of trade tape** ($3,826.65 at
the 5 ms pairing rule). That is the total that *was actually transacted* at Dutch-book prices --
so it is not a hypothetical maximum, it is a realized one. The true maximum is higher by whatever
depth nobody reached, which this data cannot see (no L2 at venue time).

### Distribution -- the number that matters more than the total

**[M]**

```
per-episode net:  min $0.00   p25 $0.09   median $0.54   p75 $3.72   p90 $22.59   max $4,597.52
episodes worth >= $10 :  37 of 248
episodes worth >= $100:   7 of 248
concentration: top 1 episode = 64% of all net;  top 5 = 79%;  top 20 = 92%
contracts per episode: median 31   p90 404   max 21,906
sum(YES bid) reached : median 104c  p90 116c  max 151c
net cents per contract after fees: median 1.72c  p75 5.28c  p90 13.99c  max 49.12c
fee as a share of gross: median 47%
```

**[I] Read the concentration line twice.** A strategy whose entire result is one event is not a
strategy with a 5.75-hour sample behind it. Strip the single WTA set-winner episode and the rest of
the market yielded **$2,638 in 5.75 hours across 247 episodes, median $0.54 each.**

**[M] The fee takes half the gross at the median.** This is the same `7 * (1 - HHI)` identity
research/09 derived: these are 2-leg books with prices spread across the pair, so HHI is low and the
fee is near its 7c ceiling.

### What one participant could realistically get

Four haircuts, in order of severity:

1. **[M] You are not first.** The median gap from the triggering sweep to the first arbitrage print
   is **12 ms** (p10 = 1 ms, p25 = 7 ms). By the time a second actor arrives, the stale ladder has
   been eaten -- visibly, in the WTA tape, from 31c down to 10c in 23 ms.
2. **[M] You would not have seen it.** Section 7.2: our feed caught 4 of 108 observable episodes.
3. **[M] Half the gross is fees**, and the ceil-to-the-cent rounding makes small clips worse:
   at the median 31-contract size the rounding alone can be several percent of the edge.
4. **[I] Leg risk.** A partial fill on a 2-leg Dutch book is a naked directional position in a
   market that just moved 12 cents. research/09's parent measurement put the orphan rate at 69.8%
   with a 900 s leg timeout; at these speeds it would be worse, not better.

**[M] Capital is *not* the binding constraint.** Cash outlay is `(100n - sum_c)/100 * qty`:

```
without collateral netting: median $34   p90 $436   max $16,868
single-leg exposure while the other leg is unfilled: median $15  p90 $198  max $8,434
net reachable with $10,000 of unnetted capital, pro-rata: $5,363.73  (74% of the total)
net reachable with  $1,000: $2,557.98  (35%)
```

Under MECNET the guaranteed `$(n-1)` per contract is returned immediately, so the position is
self-funding and the net collateral is `(100 - sum_c)/100 * qty`, which is **negative** whenever the
book is a Dutch book at all. **[C] Kalshi's collateral-return page warns that enabling it "may make
you unable to sell positions for which you've already had collateral returned"** -- fine for a
structure held to settlement, fatal for one that needs to unwind an orphan.

**[I] $10,000 is enough capital to take 74% of everything that happened. Capital is not what
stops you.**

---

## 7. Q4 -- The infrastructure that would be required

### 7.1 What we have, measured

```
[M] quote feed : REST /markets, 400 tickers per poll in 4 sequential 100-ticker requests,
                 nominal 5 s interval, ACTUAL information cadence 15 s (section 1),
                 per-leg phase independent
[M] coverage   : 6.129 h of 5 s tape in 6 disjoint segments across an 18.2 h archive span;
                 trade tape 5.75 h in 5 further, DIFFERENT segments
[M] order path : venues/kalshi/client.py TokenBucket -- write bucket capacity 100,
                 refill 100/s, COST_DEFAULT = 10 tokens per order create
                 => 10 order-creates per second sustained, 10 in a burst
[M] network    : TCP connect to api.elections.kalshi.com (18.244.202.121, an AWS edge)
                 from this home connection: median 21.0 ms, min 2.7 ms, max 26.1 ms, n=6
                 TLS handshake on top: median 23.1 ms
[C] websocket  : venues/kalshi/ws.py, verified live 2026-08-27 -- PROD wss endpoint returns
                 HTTP 401 with a valid demo signature and needs a funded prod account;
                 DEMO connects in ~300 ms.  ONE connection limit.  Venue timestamps are
                 microsecond-resolution (msg.ts / msg.ts_ms agreed to 0.6 ms over 6,427 frames)
```

### 7.2 The measured detection failure

**[M] Of the 248 realized episodes, 108 were on events whose every leg was inside the 5 s watchlist
*and* inside a quote-tape segment -- i.e. episodes our recorder was pointed directly at.**

```
our polled quotes showed sum(bid) > 100 within +/-15 s of the episode :  4  (4%)
                                        within +/-30 s                :  5  (5%)
```

**[M] Combined with section 2's forward test, the REST quote feed has a 4% detection rate and a
98.5% false-positive rate on the same phenomenon.** Both errors have the same cause: a 15-second
cache with independent per-leg phase.

### 7.3 The budget, worked backwards from the measured clock

The race is: trigger print hits the tape -> you detect -> you decide -> you send -> the exchange
acks a fill on **both** legs.

```
competitor's total detect->decide->send->ack, measured  :  p10   1 ms
                                                           p25   7 ms
                                                           median 12 ms
                                                           p75  156 ms
                                                           p90  778 ms
dislocation lifetime, measured                          :  p75  ~13 ms
                                                           p90  ~48 ms
                                                           max  923 ms
```

Against that, from here:

| stage | best case from this machine | source |
|---|---:|---|
| detect: quote arrives | 0 ms on WS push; **up to 15,000 ms on REST** | [M] section 1 |
| decide | ~1 ms | [I] trivial arithmetic |
| send: one network round trip to the edge | **~21 ms** | [M] TCP connect above |
| edge -> matching engine -> ack | unmeasured, > 0 | [I] |
| second leg (bucket allows both; latency does not stack if sent concurrently) | +0 ms | [M] TokenBucket 100/s, 10/order |
| **total, optimistic** | **>= ~25 ms, realistically 40-80 ms** | |

**[M] The single network round trip from this home connection (21 ms) is longer than the
competition's entire cycle (median 12 ms), and is already at the p75 of the dislocation lifetime
(13 ms).** Nothing downstream of that -- faster code, a better language, a colocated *decision*
loop -- can recover it, because the photons are the floor.

**What a WebSocket feed would and would not buy:**

- **[I] It would fix detection.** Push delivery removes the 15 s cache entirely. That alone moves
  us from 4% detection to something near 100%, which is a real and worthwhile gain -- see section 10.
- **[I] It would not fix execution.** WS delivers the event; the order still goes out over REST
  over the same ~21 ms path. Detection at 25 ms and execution at 45 ms puts us at roughly the p75
  of the competitor distribution, arriving after the ladder has been eaten.
- **[M] PROD WS requires a funded production account.** We do not have one, so every WS number here
  is [I], not [M].

**[I] Reachable from a home connection with no colocation? No.** To be in the *first half* of the
observed responder distribution you need a total cycle under 12 ms. Our network round trip alone is
21 ms. That is not an engineering gap, it is a geography gap: the fix is a machine in the same AWS
region as Kalshi's matching engine, and at that point it is not a home-connection strategy any more.
The honest ceiling from home is the **p90 tail** -- the 16 of 201 episodes where the first
arbitrage print came more than a second after the trigger -- which is a small and adversely-selected
subset of an already concentrated prize.

---

## 8. Q5 -- Do they cluster?

**[M] By category: completely.**

```
Sports              : 246 episodes   $7,235.58
Climate and Weather :   2 episodes       $0.02
Politics / Economics / Crypto / Companies : ZERO
```

**[M] By series** (net-positive episodes, net $):

| series | episodes | net |
|---|---:|---:|
| KXWTASETWINNER (WTA set winner) | 7 | $4,705.81 |
| KXUSLGAME (USL soccer) | 15 | $517.39 |
| KXMLBGAME | 35 | $492.33 |
| KXMIXEDDOUBLESMATCH | 4 | $219.90 |
| KXITFWMATCH | 14 | $212.90 |
| KXITFMATCH | 31 | $185.15 |
| KXCS2MAP (esports) | 17 | $184.53 |
| KXCOPADOBRASILGAME | 4 | $157.48 |
| KXITFDOUBLES | 18 | $145.74 |
| KXLOLMAP (esports) | 8 | $79.30 |

**[I] The population is minor-league tennis, second-tier soccer, MLB and esports maps** -- in-play
markets with real volume but thin, slow maker populations. Not the headline markets.

**[M] By leg count:** 2 legs 196 episodes / $6,518.24; 3 legs 50 episodes / $717.34; 6 legs 2
episodes / $0.02. **[I] Consistent with research/09's fee identity: the fee floor `7*(1-HHI)` rises
with leg count, so only 2- and 3-leg structures can clear it.**

**[M] By time of day (UTC), normalised by prints on the tape that hour:**

| hour | prints | episodes | net $ | episodes per 100k prints |
|---:|---:|---:|---:|---:|
| 01 | 368,123 | 50 | $316.02 | 13.6 |
| 02 | 475,418 | 65 | $881.85 | 13.7 |
| 03 | 236,459 | 24 | $435.93 | 10.1 |
| 04 | 144,701 | 4 | $4,708.09 | 2.8 |
| 05 | 275,992 | 20 | $138.42 | 7.2 |
| 06 | 195,575 | 16 | $143.66 | 8.2 |
| **09** | **172,635** | **64** | **$560.45** | **37.1** |
| 13 | 54,945 | 5 | $51.18 | 9.1 |

**[M] 09:00 UTC is a 3-4x outlier in *rate*.** **[I] That is 05:00 New York / 11:00 Central Europe
/ 18:00 Tokyo -- European and Asian minor-league tennis plus the 04:00 UTC esports slate, i.e. live
sports with the thinnest US maker coverage of the day.** The dollar column tells a different story
because hour 04 contains the single WTA event; the rate column is the more stable statistic.

**[M] By liquidity:** episode events have a summed 24h volume of median 1,847 contracts (p10 = 0,
p90 = 3,979,131). All ME events with any volume have a median of 348 and p90 of 25,522; **54% of ME
events (3,589 of 6,689) have zero 24h volume at all.** **[I] Dislocations require enough volume to
generate a sweep and thin enough maker presence to leave a stale ladder -- the middle of the
liquidity distribution, not the top and not the tail.**

**[M] By proximity to settlement -- with a data caveat that matters.** `close_at_us` is a far-future
**placeholder** while an in-play sports market is live and is only rewritten to the true close after
the market finalizes. (`KXITFMATCH-26AUG26JIASUN-JIA` carried `09-10 03:00` while active and
`08-27 06:32:02` after finalizing.) Restricting to the 70 episodes whose event we actually observed
finalize:

```
minutes to close: min 1.0   p25 14.9   median 51.3   p75 89.4   max 417.4
buckets: <5min 1 | 5-30min 26 | 30-120min 37 | >2h 6
```

**[I] Mid-game, not the closing minutes.** Nothing here supports a settlement-proximity strategy.

### 8.1 What a busier window would change

The archive is Wed 19:49 UTC -> Thu 14:00 UTC: overnight US plus a Thursday morning. No FOMC, no
CPI, no NFL slate, no election, no exchange incident. **[I] Three directional predictions, none
of which this data can test:**

1. **More episodes, same shape.** The mechanism is "large sweep + slow maker cancel". Anything that
   raises sweep frequency -- an NFL Sunday, a CPI print, a live election night -- raises episode
   count roughly proportionally. The *rate per print* is what would need to change for the strategy
   to become viable, and there is no reason it would.
2. **Faster competition, not slower.** Busy windows are exactly when the fast actors are watching.
   Hour 09 -- our quietest hour by print count in the US sense -- had the *highest* episode rate,
   which is the signature of thin competitive coverage. **[I] The best window for a slow participant
   is the quiet one, and the quiet one is the one with the least money in it.**
3. **The one genuinely untested tail is an impaired maker population**: an exchange incident, a
   settlement surprise, a new liquid ME series listing before makers arrive. research/09 section 1.4
   said the same thing and it is still true. **[M] Our only measurement of the listing case is 6
   episodes worth $33.54**, from a window with essentially no liquid listings in it.

---

## 9. Q6 -- The honest bottom line

**This is a structural advantage that belongs to someone with colocation. It is not a strategy a
solo trader with $10,000 and a home internet connection can run.**

The reasoning is four measured facts, and none of them is about skill or capital:

1. **[M] Our detection instrument is 15 seconds behind and detects 4% of events.** Fixable, with a
   funded prod account and the WebSocket client that already exists in `venues/kalshi/ws.py`.
2. **[M] The opportunity's p75 lifetime is ~13 ms and its p90 is ~48 ms.** Not fixable.
3. **[M] One network round trip from this connection is 21 ms.** Not fixable without colocation.
4. **[M] The competition's measured median response is 12 ms, with a p10 of 1 ms.** Someone is
   already sitting where the packets are.

The money is real -- $3,827 to $7,236 in 5.75 hours is not noise, and it is 100x what research/09
estimated. But 64% of it was one event, the median episode is worth **$0.54**, and the queue for it
is measured in single-digit milliseconds. **[I] A home-based participant is not slightly behind in
this race; the round-trip time is longer than the median competitor's entire decision cycle. Adding
a WebSocket feed converts a 4% detection rate into a good detection rate and leaves the execution
problem exactly where it was.**

**[I] This is a valuable answer, not a failure.** It closes the last open question from research/09
and it does so in the direction the rest of that file already pointed: PLAN.md line 117 lists
"competing on latency" as an explicit non-goal, and correction C1 says do not chase cross-venue
latency arbitrage. The measurement now agrees with the doctrine, quantitatively, in milliseconds.

### 9.1 What is worth doing anyway

**[I]** Three things, in decreasing value:

1. **Build the WS feed for its *own* sake, not for arbitrage.** It is the only way to get honest
   book data, and every maker-side sleeve -- the direction research/09 section 3 says is actually
   reachable -- needs it. The arbitrage scanner is a free by-product, and it is the correct place
   to *watch* this phenomenon from, even if we never fire at it.
2. **Stop reporting REST-derived Dutch books as opportunities.** Any scanner reading
   `sum(yes_bid) > 100` off `/markets` has a 98.5% false-positive rate. If such a scanner exists in
   the sleeves, it should be gated on "every leg's quote has survived a full refresh" -- which,
   per section 1.2, reduces the whole 5 s tape to **2 observations worth $3.31**.
3. **Consider the *other* side of this trade.** The loser in every one of these 248 episodes is a
   maker who left a bid resting 10-50 ms too long. That is an argument about which markets to make
   in -- not the in-play minor-league books where the sweep risk lives.

### 9.2 What data would change the answer

**[M]** stated as concrete, falsifiable experiments:

- **A funded prod account + the existing WS client.** Records true book state with microsecond
  venue timestamps. Would replace every [I] in section 7 with an [M], and would let us measure
  our own end-to-end ack latency against the 12 ms benchmark directly. Until then the claim
  "we would arrive at ~45 ms" is inference.
- **A one-line REST experiment to confirm the cache**: poll `/markets/{ticker}` (single) and
  `/markets?tickers=` (batch) for the same ticker at 1 s for two minutes during an in-play match and
  compare the update grid. If the single-market endpoint is not on the 15 s grid, the cache is a
  batch-endpoint property and the recorder should switch. Cost: 120 requests. I did not run it --
  a live recorder holds the rate limit.
- **A busy window**: one NFL Sunday and one CPI print, recorded end-to-end. Section 8.1's three
  predictions are all testable and none of them is tested.
- **The trade tape without gaps.** Ours has 5 segments over 5.75 h and does not overlap the quote
  tape (`recorder/l1.py` documents the cursor bug that caused this). Every rate in this file is
  per-print-normalised for that reason; a continuous tape would let them be per-hour.

---

## Appendix -- reproducing every number

All queries are read-only against `file:data/pm.db?mode=ro` with
`observed_at_us <= 1787839200000000` / `traded_at_us <= 1787839200000000`.

**Coverage** (section 7.1): `SELECT DISTINCT observed_at_us FROM market_snapshots ORDER BY 1`, split
into segments wherever the gap exceeds 20 s. Two rows with ~107k markets each are full-universe
sweeps from `recorder.main`; everything else is `recorder.l1` at 400 tickers.

**The 15 s grid** (section 1): per ticker, collapse consecutive identical
`(yes_bid, yes_ask, yes_bid_size, yes_ask_size)` and histogram the gaps between changes. Repeat on
`volume` alone for the cumulative-counter check.

**Quote-feed Dutch books** (section 2): group `market_snapshots` by `(event_ticker,
observed_at_us)`; require `mutually_exclusive = 1` from the latest `event_snapshots` row, all legs
`active`, `yes_bid IS NOT NULL`, `yes_bid_size >= 1`, and
`count(rows) = count(DISTINCT ticker ever seen for that event)`. Fee per leg is
`ceil(core.math.contracts.fee(bid/100, FeeSpec.kalshi(fee_type, fee_multiplier), is_maker=False)
* C * 100) / 100`, summed. `FeeSpec` comes from `series_cache` resolved by the longest dash-prefix of
the event ticker, matching `recorder/l1.py::_series_of`.

**Leg age** (section 1.2): within a tape segment, a leg's age at poll `t` is `t` minus the time its
quote tuple last changed; require >= 3 prior polls in the segment.

**Realized Dutch books** (sections 3-6): from `trades` only. Take `taker_side = 'no'` prints
(= YES sales) on the legs of a `mutually_exclusive` event, cluster them by a maximum separation
window, require every leg present in the cluster, take the best price per leg, require the sum > 100,
and match quantities greedily from the best price down while the pairwise sum still exceeds 100.
Fees as above. The 5 ms / 10 ms / 50 ms / 100 ms / 500 ms table is that same scan with the window
varied.

**Lifetime** (section 5): a print is "qualifying" if the most recent YES-sale on another leg within
300 ms makes the sum exceed 100; episodes are maximal runs of qualifying prints separated by less
than the stated gap.

**Network** (section 7.1): six `socket.create_connection((ip, 443))` calls to the resolved address of
`api.elections.kalshi.com`, timing the TCP handshake, plus a TLS wrap. No API request was issued and
no rate limit was consumed.

**Our own data**: `data/pm.db`, read-only, quote tape 2026-08-27 01:28-13:59 UTC (6.13 h in 6
segments), trade tape 2026-08-27 01:13-13:59 UTC (5.75 h in 5 segments), inside an archive spanning
2026-08-26 19:49 UTC to 2026-08-27 14:00 UTC.
