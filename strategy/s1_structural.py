"""S1 -- structural maker basket.  The volume and sample engine.

Thesis (three independently measured components):

  1. Favourite-longshot bias -- contracts above 70c carry statistically
     significant positive post-fee returns; sub-10c buyers lose >60% of stake.
  2. Single-name YES bias -- in "Will [person] do X?" markets traders buy YES
     ~61% of the time while YES resolves true only ~32% of the time.
  3. Political underconfidence -- a 70c political contract a week out is ~83%.

Execution: rest post-only bids on the favourite side across many uncorrelated
events, collecting the taker flow that is systematically wrong.

THE DRIFT YOU MUST DECIDE ABOUT, NOT DISCOVER
---------------------------------------------
This edge is partly directional, not pure spread capture.  Maker share of
purchases rises monotonically with price (43.5% at 1-10c to 56.5% at 90-99c):
makers systematically buy favourites, takers buy longshots and lose.  So
inventory drifts systematically SHORT YES in longshot markets.  That is a real
edge with negative skew -- size it deliberately or hedge it, never let it
accumulate by accident.

And it INVERTS at the very end: the "Yogi Berra effect" replicates on Kalshi,
Betfair and Intrade -- on closing day, maker losses on cheap contracts become as
bad as taker losses.  Hence the no-entry-inside-the-final-hour rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from core.math.contracts import FeeSpec, edge, fee, in_fee_death_zone
from core.math.sizing import position_fraction
from core.models import Market, Side
from strategy.base import Decision, DesiredQuote, DesiredState, MarketSnapshot

# Horizon recalibration exponent theta, from the 292M-trade calibration study.
# p* = p^theta / (p^theta + (1-p)^theta).  theta > 1 means the market is
# UNDERCONFIDENT (favourites are cheap).  These are the PRIOR -- refit per
# category from your own settled data once >=100 settlements exist (R2.3a).
THETA_BY_HORIZON: dict[str, float] = {
    "under_1h": 0.99,      # the bias has collapsed by expiry
    "under_1d": 1.05,
    "under_1w": 1.15,
    "under_1m": 1.25,
    "over_1m": 1.32,
}


def horizon_bucket(hours: float) -> str:
    if hours < 1:
        return "under_1h"
    if hours < 24:
        return "under_1d"
    if hours < 24 * 7:
        return "under_1w"
    if hours < 24 * 30:
        return "under_1m"
    return "over_1m"


def recalibrate(p: float, theta: float) -> float:
    """p* = p^theta / (p^theta + (1-p)^theta).  theta=1 is the identity."""
    if not 0.0 < p < 1.0:
        return p
    a = p ** theta
    b = (1.0 - p) ** theta
    return a / (a + b)


@dataclass(frozen=True, slots=True)
class S1Config:
    # universe
    min_price: float = 0.70
    max_price: float = 0.95
    min_hours: float = 1.0            # the bias collapses inside the final hour
    max_hours: float = 2160.0         # 90d; capital lockup dominates beyond
    min_depth_usd: float = 200.0
    min_volume_24h: float = 1.0
    require_rules_reviewed: bool = True
    # signal
    single_name_yes_adj: float = 0.03  # applied AGAINST yes in single-name markets
    lam: float = 0.5                   # shrinkage; replace with fitted beta_c
    # S1 must believe the MID ITSELF is wrong -- not merely rest below it.
    # Without this, resting at the bid when fair value is the mid always shows a
    # half-spread "edge", and the sleeve silently degenerates into pure spread
    # capture (which is S6's job, and collects S6's adverse selection without
    # S6's rebates).  This threshold is the structural component only.
    min_structural_edge: float = 0.01
    # sizing
    kelly_mult: float = 0.25
    position_cap: float = 0.02
    # execution
    edge_multiple_of_fee: float = 2.0  # require shrunk edge >= 2x the maker fee
    penny_edge_cents: float = 3.0      # improve the touch only above this edge
    max_depth_fraction: float = 0.20   # capacity: <=20% of touch depth
    max_quotes: int = 40
    # Multiple strikes of one ladder are ONE theme, not many bets.  Without this
    # the sleeve ladders into a single event, the portfolio n_eff collapses, and
    # the risk engine rejects most of the basket -- which is the correct outcome
    # but wastes the quota.  Diversify at the source instead.
    max_quotes_per_event: int = 1


@dataclass
class S1Structural:
    """Rest post-only bids on structurally underpriced favourites."""

    id: str = "S1"
    gate: int = 2
    cfg: S1Config = field(default_factory=S1Config)
    theta_by_horizon: dict[str, float] = field(
        default_factory=lambda: dict(THETA_BY_HORIZON)
    )
    reviewed_rules: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------ signal
    def model_probability(self, m: Market, snapshot: MarketSnapshot) -> float | None:
        """Recalibrated probability that the FAVOURITE side resolves true."""
        mid = m.mid
        hrs = m.hours_to_close(now=snapshot.now_us)
        if mid is None or hrs is None:
            return None
        theta = self.theta_by_horizon.get(horizon_bucket(hrs), 1.0)
        p = recalibrate(mid, theta)

        ev = snapshot.event_for(m)
        # single-name markets over-attract YES; shade the YES side down
        if ev is not None and self._is_single_name(ev.title or m.title):
            p = max(0.01, p - self.cfg.single_name_yes_adj)
        return min(max(p, 0.01), 0.99)

    @staticmethod
    def _is_single_name(title: str) -> bool:
        t = (title or "").lower()
        return t.startswith("will ") and any(
            k in t for k in (" say ", " tweet ", " post ", " announce ", " resign",
                             " leave ", " visit ", " meet ", " mention")
        )

    # --------------------------------------------------------------- universe
    def in_universe(self, m: Market, snapshot: MarketSnapshot) -> tuple[bool, str]:
        if not m.has_two_sided_quote:
            return False, "no two-sided quote"
        mid = m.mid
        assert mid is not None
        if not (self.cfg.min_price <= mid <= self.cfg.max_price):
            return False, f"mid {mid:.2f} outside favourite band"
        hrs = m.hours_to_close(now=snapshot.now_us)
        if hrs is None:
            return False, "no close time"
        if hrs < self.cfg.min_hours:
            return False, "inside the final hour (bias collapses)"
        if hrs > self.cfg.max_hours:
            return False, "beyond 90d (lockup cost dominates)"
        if m.volume_24h < self.cfg.min_volume_24h:
            return False, "no recent volume"
        if m.yes_bid_size * mid < self.cfg.min_depth_usd:
            return False, "touch depth below minimum"
        spec = self._fee_spec(m, snapshot)
        if in_fee_death_zone(mid, spec, is_maker=True):
            return False, "fee ratio too high"
        if self.cfg.require_rules_reviewed and m.rules_hash not in self.reviewed_rules:
            return False, "rules not reviewed (I7)"
        return True, ""

    @staticmethod
    def _fee_spec(m: Market, snapshot: MarketSnapshot) -> FeeSpec:
        s = snapshot.series_for(m)
        return s.fee_spec if s else FeeSpec.kalshi("quadratic", 1.0)

    # -------------------------------------------------------------- execution
    def quote_price(self, m: Market, edge_cents: float) -> int:
        """JOIN the queue; do not penny.

        On a 1c tick, unless the edge is >= 3c, improving must double or triple
        your fill probability to break even -- and it rarely does, since the
        entire front-to-back queue value measures 0.21-0.26 ticks.
        """
        assert m.yes_bid is not None and m.yes_ask is not None
        if edge_cents >= self.cfg.penny_edge_cents and m.yes_ask - m.yes_bid > 1:
            return m.yes_bid + 1          # improve only when the edge pays for it
        return m.yes_bid                  # otherwise join

    def desired_state(self, snapshot: MarketSnapshot) -> DesiredState:
        quotes: list[DesiredQuote] = []
        decisions: list[Decision] = []
        skipped: dict[str, int] = {}

        for m in snapshot.markets:
            ok, why = self.in_universe(m, snapshot)
            if not ok:
                skipped[why] = skipped.get(why, 0) + 1
                continue

            p_model = self.model_probability(m, snapshot)
            mid = m.mid
            if p_model is None or mid is None:
                continue

            spec = self._fee_spec(m, snapshot)
            price = self.quote_price(m, 0.0)          # provisional, for pricing
            px = price / 100.0
            raw = edge(p_model, px, spec, is_maker=True)
            shrunk = self.cfg.lam * raw
            threshold = self.cfg.edge_multiple_of_fee * abs(fee(px, spec, is_maker=True))

            # the STRUCTURAL claim: how far the recalibrated probability departs
            # from the market's own mid.  Spread capture is deliberately excluded.
            structural = p_model - mid
            acted = (
                shrunk > 0
                and shrunk >= threshold
                and structural >= self.cfg.min_structural_edge
            )
            decisions.append(Decision(
                ticker=m.ticker, market_price=mid, p_model=p_model,
                raw_edge=raw, shrunk_edge=shrunk, acted=acted,
                category=(snapshot.event_for(m).category if snapshot.event_for(m) else ""),
            ))
            if not acted:
                continue

            # re-price now that we know the edge, then size
            price = self.quote_price(m, shrunk * 100.0)
            px = price / 100.0
            frac = position_fraction(
                p_model, px, spec, is_maker=True,
                lam=1.0,                       # already shrunk above
                kelly_mult=self.cfg.kelly_mult,
                cap=self.cfg.position_cap,
            )
            if frac <= 0:
                continue

            budget_cents = int(snapshot.bankroll_cents * frac)
            size = budget_cents // max(1, price)
            depth_cap = int(m.yes_bid_size * self.cfg.max_depth_fraction)
            size = min(size, depth_cap)
            if size < 1:
                continue

            quotes.append(DesiredQuote(
                ticker=m.ticker, side=Side.YES, price_cents=price, size=size,
                post_only=True,
                rationale={
                    "sleeve": self.id,
                    "mid": round(mid, 4),
                    "p_model": round(p_model, 4),
                    "theta": self.theta_by_horizon.get(
                        horizon_bucket(m.hours_to_close(now=snapshot.now_us) or 0.0), 1.0),
                    "structural_edge": round(p_model - mid, 5),
                    "raw_edge": round(raw, 5),
                    "shrunk_edge": round(shrunk, 5),
                    "fee": round(fee(px, spec, is_maker=True), 5),
                    "hours_to_close": round(m.hours_to_close(now=snapshot.now_us) or 0.0, 2),
                    "depth_cap": depth_cap,
                },
            ))

        quotes.sort(key=lambda q: -q.rationale.get("shrunk_edge", 0.0))

        # keep only the best few strikes per event, so the basket spreads across
        # distinct underlying uncertainties rather than one ladder
        by_event: dict[str, int] = {}
        event_of = {m.ticker: (m.event_ticker or m.ticker) for m in snapshot.markets}
        diversified: list[DesiredQuote] = []
        for q in quotes:
            ev = event_of.get(q.ticker, q.ticker)
            if by_event.get(ev, 0) >= self.cfg.max_quotes_per_event:
                continue
            by_event[ev] = by_event.get(ev, 0) + 1
            diversified.append(q)
        quotes = diversified

        return DesiredState(
            quotes=tuple(quotes[: self.cfg.max_quotes]),
            decisions=tuple(decisions),
            rationale={"considered": len(snapshot.markets),
                       "in_universe": len(decisions),
                       "quoted": min(len(quotes), self.cfg.max_quotes),
                       "skipped": skipped},
        )
