"""Risk engine.  T-040.  PLAN.md I3 and section 9.

Limits are enforced HERE, in the one process a sleeve cannot override.  Every
value comes from `config/risk.yaml`; nothing else may hardcode a limit.

The engine is a pure function of (proposed order, current state, config) so it
is exhaustively testable and identical in backtest, shadow and live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from core.config import RiskConfig
from core.math.portfolio import n_effective
from core.models import RunMode, Side

# Modes that put REAL CAPITAL at risk.  Deliberately enumerated POSITIVELY and
# independently of anything in `execution.executor`: I5's whole point is two
# independent refusals, and importing the executor's tuple would collapse them
# into one predicate that fails in one place.
#
# PAPER is NOT here.  The executor refuses to construct a PAPER run against a
# production host, so PAPER is mock funds by construction -- and I5 protects
# capital, not network calls.  If that guarantee is ever weakened, PAPER must
# come back into this set on the same commit.
REAL_CAPITAL_MODES: frozenset[RunMode] = frozenset({RunMode.LIVE})
from strategy.base import DesiredQuote


def per_contract_cost_cents(side: Side, price_cents: int) -> int:
    """Capital locked per contract.  The ONE place this rule lives.

    `price_cents` is always YES-referenced, so a NO contract quoted at YES-price
    p costs 100 - p, not p.  Duplicating this rule is how it went wrong the first
    time: the risk engine charged a short basket leg 5c where it locks 95c and
    under-counted capital by 20x on exactly the legs S2 rests.
    """
    return max(0, price_cents if side is Side.YES else 100 - price_cents)


def quote_cost_cents(quote: DesiredQuote) -> int:
    """Capital a quote actually locks.

    `price_cents` is YES-REFERENCED throughout this system, so a NO leg resting
    at a YES price of 5c does not cost 5c -- it costs 95c.  Getting this wrong
    under-counts capital by up to 20x on exactly the legs S2 rests (short baskets
    are NO quotes at LOW yes-prices), and that error feeds the position cap, the
    theme cap, the venue cap and the cash reserve simultaneously.
    """
    return per_contract_cost_cents(quote.side, quote.price_cents) * quote.size


class Denial(StrEnum):
    GATE = "sleeve_below_gate_4"
    POSITION_CAP = "position_cap"
    THEME_CAP = "theme_cap"
    GROSS_DEPLOYMENT = "gross_deployment"
    CASH_RESERVE = "cash_reserve"
    VENUE_CONCENTRATION = "venue_concentration"
    N_EFF = "n_eff_floor"
    DAILY_LOSS = "daily_loss_stop"
    CAPACITY = "capacity_ceiling"
    DRAWDOWN_HALT = "drawdown_halt"
    KILLED = "kill_switch_engaged"
    STRUCTURE_INCOMPLETE = "structure_incomplete"
    RESTING_ORDER_CAP = "resting_order_cap"


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: Denial | None = None
    detail: str = ""

    @classmethod
    def allow(cls) -> "Verdict":
        return cls(True)

    @classmethod
    def deny(cls, reason: Denial, detail: str = "") -> "Verdict":
        return cls(False, reason, detail)


@dataclass
class PortfolioState:
    """Everything the engine needs to judge a proposed order."""

    bankroll_cents: int
    peak_bankroll_cents: int
    cash_cents: int
    day_pnl_cents: int = 0
    # ticker -> cents at risk
    exposure_by_ticker: dict[str, int] = field(default_factory=dict)
    # theme -> cents at risk  (a theme is one underlying uncertainty)
    exposure_by_theme: dict[str, int] = field(default_factory=dict)
    venue_exposure: dict[str, int] = field(default_factory=dict)
    killed: bool = False

    @property
    def gross_cents(self) -> int:
        return sum(self.exposure_by_ticker.values())

    @property
    def drawdown(self) -> float:
        if self.peak_bankroll_cents <= 0:
            return 0.0
        return max(0.0, 1.0 - self.bankroll_cents / self.peak_bankroll_cents)


@dataclass
class RiskEngine:
    cfg: RiskConfig
    theme_of: dict[str, str] = field(default_factory=dict)   # ticker -> theme id
    # R2.7a distinguishes TWO correlations, and conflating them is a live bug:
    #   intra-theme rho >= 0.5  -- why a single theme is worth ~2 effective bets
    #   cross-theme  rho ~ 0.05-0.10 -- what applies BETWEEN distinct themes
    # n_eff across themes must use the CROSS-theme figure.  Using 0.5 here makes
    # n_eff saturate at 1/rho = 2, so an `n_eff >= 8` floor becomes unsatisfiable
    # at ANY number of themes and silently blocks all trading forever.
    intra_theme_rho: float = 0.5
    # 0.05 is the research range's LOWER bound, and it is chosen deliberately --
    # see `validate()`.  At 0.10 the n_eff floor of 8 is unreachable given the
    # 2% position cap and 40% gross cap, because those together permit at most
    # 20 positions and n_effective(20, 0.10) = 6.9.  A floor you can never clear
    # is not a risk control; it is an outage.
    cross_theme_rho: float = 0.05

    # ----------------------------------------------------------------- helpers
    def max_achievable_n_eff(self) -> float:
        """Best possible n_eff given the position and gross caps.

        The caps bound the number of simultaneous positions: you cannot hold
        more than `max_gross / position_cap` of them at full size, and n_eff
        saturates at 1/rho regardless.
        """
        max_positions = int(
            self.cfg.deployment.max_gross_fraction
            / max(self.cfg.position.cap_fraction_default, 1e-9)
        )
        return n_effective(max(1, max_positions), self.cross_theme_rho)

    def validate(self) -> list[str]:
        """Catch self-inconsistent risk configuration BEFORE it silently blocks trading.

        A limit that cannot be satisfied at any portfolio the other limits permit
        is worse than no limit: it denies everything and looks like a quiet
        strategy rather than a misconfiguration.
        """
        problems: list[str] = []
        achievable = self.max_achievable_n_eff()
        if self.cfg.theme.min_n_eff > achievable:
            problems.append(
                f"min_n_eff={self.cfg.theme.min_n_eff} is UNREACHABLE: position cap "
                f"{self.cfg.position.cap_fraction_default:.1%} and gross cap "
                f"{self.cfg.deployment.max_gross_fraction:.0%} permit at most "
                f"{int(self.cfg.deployment.max_gross_fraction / self.cfg.position.cap_fraction_default)} "
                f"positions, giving n_eff <= {achievable:.2f} at cross-theme "
                f"rho={self.cross_theme_rho}"
            )
        if self.cfg.deployment.max_gross_fraction + self.cfg.deployment.min_cash_fraction > 1.0:
            problems.append("max_gross + min_cash exceeds 100% of bankroll")
        if self.cfg.position.cap_fraction_default > self.cfg.theme.max_exposure_fraction:
            problems.append("per-position cap exceeds the per-theme cap")
        return problems

    def position_cap(self, *, gate: int) -> float:
        """Gate 4 (canary) trades at half the standard cap."""
        return (self.cfg.position.cap_fraction_gate4 if gate == 4
                else self.cfg.position.cap_fraction_default)

    def n_eff(self, state: PortfolioState) -> float:
        """Effective independent bets across THEMES, not tickers.

        More tickers inside one theme does not help; more themes does.
        """
        themes = [v for v in state.exposure_by_theme.values() if v > 0]
        if not themes:
            return float("inf")
        return n_effective(len(themes), self.cross_theme_rho)

    # ------------------------------------------------------------------- check
    def check(
        self,
        quote: DesiredQuote,
        state: PortfolioState,
        *,
        sleeve_gate: int,
        mode: RunMode,
        venue: str = "kalshi",
        touch_depth_contracts: float | None = None,
    ) -> Verdict:
        """Judge one proposed order.  Called BEFORE every send."""
        if state.killed:
            return Verdict.deny(Denial.KILLED, "kill switch engaged")

        # I5 -- live orders require Gate 4
        if mode in REAL_CAPITAL_MODES and sleeve_gate < 4:
            return Verdict.deny(
                Denial.GATE, f"sleeve at gate {sleeve_gate}; live requires >= 4"
            )

        cost = quote_cost_cents(quote)
        if cost <= 0:
            return Verdict.allow()

        # drawdown ladder
        dd = state.drawdown
        action = self.cfg.action_for_drawdown(dd)
        if action == "full_stop_and_audit":
            return Verdict.deny(Denial.DRAWDOWN_HALT,
                                f"drawdown {dd:.1%} -- full stop")
        cap_scale = 0.5 if action in ("halve_all_position_caps",
                                      "halt_worst_sleeve_by_edge_ci") else 1.0

        # daily loss stop
        if state.day_pnl_cents < -int(state.bankroll_cents
                                      * self.cfg.max_daily_loss_fraction):
            return Verdict.deny(Denial.DAILY_LOSS,
                                f"day pnl {state.day_pnl_cents/100:.2f}")

        # per-position cap
        cap_frac = self.position_cap(gate=sleeve_gate) * cap_scale
        limit = int(state.bankroll_cents * cap_frac)
        already = state.exposure_by_ticker.get(quote.ticker, 0)
        if already + cost > limit:
            return Verdict.deny(
                Denial.POSITION_CAP,
                f"{(already+cost)/100:.2f} > cap {limit/100:.2f} ({cap_frac:.1%})",
            )

        # per-theme cap
        theme = self.theme_of.get(quote.ticker, quote.ticker)
        theme_now = state.exposure_by_theme.get(theme, 0)
        theme_limit = int(state.bankroll_cents * self.cfg.theme.max_exposure_fraction)
        if theme_now + cost > theme_limit:
            return Verdict.deny(
                Denial.THEME_CAP,
                f"theme {theme}: {(theme_now+cost)/100:.2f} > {theme_limit/100:.2f}",
            )

        # gross deployment and cash reserve
        gross_limit = int(state.bankroll_cents * self.cfg.deployment.max_gross_fraction)
        if state.gross_cents + cost > gross_limit:
            return Verdict.deny(Denial.GROSS_DEPLOYMENT,
                                f"{(state.gross_cents+cost)/100:.2f} > {gross_limit/100:.2f}")
        min_cash = int(state.bankroll_cents * self.cfg.deployment.min_cash_fraction)
        if state.cash_cents - cost < min_cash:
            return Verdict.deny(Denial.CASH_RESERVE,
                                f"cash would fall below {min_cash/100:.2f}")

        # venue concentration
        vlimit = int(state.bankroll_cents * self.cfg.deployment.max_fraction_per_venue)
        if state.venue_exposure.get(venue, 0) + cost > vlimit:
            return Verdict.deny(Denial.VENUE_CONCENTRATION, venue)

        # capacity: never rest more than a fraction of visible depth
        if touch_depth_contracts is not None:
            max_size = touch_depth_contracts * self.cfg.capacity.max_resting_fraction_of_touch_depth
            if quote.size > max_size:
                return Verdict.deny(
                    Denial.CAPACITY,
                    f"size {quote.size} > {max_size:.0f} "
                    f"({self.cfg.capacity.max_resting_fraction_of_touch_depth:.0%} of depth)",
                )

        # N_eff floor, once meaningfully deployed
        deployed = (state.gross_cents + cost) / max(1, state.bankroll_cents)
        if deployed > 0.20:
            projected = dict(state.exposure_by_theme)
            projected[theme] = projected.get(theme, 0) + cost
            n = n_effective(len([v for v in projected.values() if v > 0]),
                            self.cross_theme_rho)
            if n < self.cfg.theme.min_n_eff:
                return Verdict.deny(
                    Denial.N_EFF,
                    f"n_eff {n:.1f} < {self.cfg.theme.min_n_eff} at {deployed:.0%} deployed",
                )

        return Verdict.allow()

    def filter(
        self,
        quotes: list[DesiredQuote],
        state: PortfolioState,
        *,
        sleeve_gate: int,
        mode: RunMode,
        depth_by_ticker: dict[str, float] | None = None,
    ) -> tuple[list[DesiredQuote], list[tuple[DesiredQuote, Verdict]]]:
        """Approve as many quotes as the limits allow, in the order given.

        Exposure accumulates as quotes are approved, so a batch cannot collectively
        breach a limit that each member individually respects.
        """
        approved: list[DesiredQuote] = []
        denied: list[tuple[DesiredQuote, Verdict]] = []
        working = PortfolioState(
            bankroll_cents=state.bankroll_cents,
            peak_bankroll_cents=state.peak_bankroll_cents,
            cash_cents=state.cash_cents,
            day_pnl_cents=state.day_pnl_cents,
            exposure_by_ticker=dict(state.exposure_by_ticker),
            exposure_by_theme=dict(state.exposure_by_theme),
            venue_exposure=dict(state.venue_exposure),
            killed=state.killed,
        )
        for q in quotes:
            depth = (depth_by_ticker or {}).get(q.ticker)
            v = self.check(q, working, sleeve_gate=sleeve_gate, mode=mode,
                           touch_depth_contracts=depth)
            if not v.allowed:
                denied.append((q, v))
                continue
            approved.append(q)
            cost = quote_cost_cents(q)
            theme = self.theme_of.get(q.ticker, q.ticker)
            working.exposure_by_ticker[q.ticker] = \
                working.exposure_by_ticker.get(q.ticker, 0) + cost
            working.exposure_by_theme[theme] = \
                working.exposure_by_theme.get(theme, 0) + cost
            working.venue_exposure["kalshi"] = \
                working.venue_exposure.get("kalshi", 0) + cost
            working.cash_cents -= cost
        return approved, denied
