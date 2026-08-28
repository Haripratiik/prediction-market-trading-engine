"""Joint no-arbitrage across EVERY market on one game.  T-059.

The pairwise tests in this project (moneyline >= spread, ladders monotone,
mutually exclusive legs summing to $1) each check ONE logical relation. They
cannot see an inconsistency that only appears when three or more markets are
considered together, and on a soccer game Kalshi lists up to seventeen market
types over the same underlying outcome.

This is the complete test. Every market on a game is an indicator over the same
state space -- the grid of possible final scores -- so the whole board admits a
single question:

    does ANY probability distribution over final scores price every market
    inside its own bid-ask spread, simultaneously?

If no such distribution exists then, by LP duality, a portfolio exists whose
payoff is non-negative in every state and whose cost is negative. That is
riskless profit, and unlike a statistical edge it needs no forecast to collect.

WHY THIS IS STRICTLY STRONGER
-----------------------------
Exact-score markets pin the entire joint distribution. Given P(1-0), P(2-1) and
so on, the correct price of "over 2.5 goals", "Real Madrid wins", "Real Madrid
wins by more than 1.5" and "both teams score" are all determined. A pairwise
scan can only ever check the relations somebody thought to encode; the LP checks
all of them at once, including relations nobody named.

CONSERVATIVE BY CONSTRUCTION
----------------------------
Prices enter as the touch we would actually trade: we may BUY at the ask and
SELL at the bid, so a market constrains its own probability to [bid, ask]. The
grid is capped and any residual mass sits in a free `OTHER` state that no market
claims, which can only ever make the LP EASIER to satisfy. A violation reported
here is therefore a lower bound on the real inconsistency, never an artifact of
truncation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np
from scipy.optimize import linprog

#: Goals per side the grid covers.  A soccer scoreline beyond this is far rarer
#: than one tick, and anything outside falls into the free OTHER state.
MAX_GOALS = 9


@dataclass(frozen=True, slots=True)
class Leg:
    """One quoted market, as an indicator over the score grid."""

    ticker: str
    bid: float                      # probability units, 0..1
    ask: float
    covers: Callable[[int, int], bool] = field(repr=False)


@dataclass(frozen=True, slots=True)
class JointResult:
    game: str
    n_legs: int
    feasible: bool
    slack: float                    # how far outside its spread the worst leg is
    detail: str = ""

    @property
    def arbitrage(self) -> bool:
        """Infeasible means no consistent distribution exists, i.e. arbitrage."""
        return not self.feasible


def states(max_goals: int = MAX_GOALS) -> list[tuple[int, int]]:
    return [(h, a) for h in range(max_goals + 1) for a in range(max_goals + 1)]


def required_grid(suffixes: Iterable[str]) -> int:
    """Largest threshold any market on this board refers to.

    The state space has to FIT THE SPORT. A grid of 0-9 per side is soccer; a
    WNBA total of "over 160.5 points" cannot be represented on it at all, and
    the LP then reports a 76 cent violation that is entirely an artifact of the
    board not fitting the grid. Sizing from the quoted thresholds removes the
    assumption: a basketball board asks for a grid that a caller can refuse.
    """
    biggest = 0
    for suf in suffixes:
        for m in re.finditer(r"(\d+)", suf.upper()):
            biggest = max(biggest, int(m.group(1)))
    return biggest


# --------------------------------------------------------------------------- #
# Ticker parsing.  Kalshi's convention on these series is uniform: the trailing
# number k means "more than k - 0.5", i.e. ">= k".
# --------------------------------------------------------------------------- #
#: Series segments that restrict a market to PART of the game.  These must never
#: be scored against the full-time state space: `KXLALIGA1HTOTAL` ends in
#: "TOTAL" but "over 1.5 goals in the first half" is a different question from
#: "over 1.5 goals in the match".  Matching on `endswith` alone pulled first and
#: second half markets into the full-time board and manufactured violations of
#: 14 to 24 cents, which is what a mis-parsed leg always looks like: far too
#: large to be real.
PERIOD_MARKERS: tuple[str, ...] = ("1H", "2H", "F3", "F5", "F7", "1P", "2P", "3P")


def is_full_game(series: str) -> bool:
    """Does this series price the WHOLE game, rather than a period of it?"""
    s = series.upper()
    body = s
    for prefix in ("KX",):
        if body.startswith(prefix):
            body = body[len(prefix):]
    return not any(mark in body for mark in PERIOD_MARKERS)


def parse_leg(series: str, suffix: str, home: str, away: str
              ) -> Callable[[int, int], bool] | None:
    """Map one market suffix onto the set of scorelines it pays on.

    Returns None when the suffix is not understood, which is treated as "leave
    this market out" rather than guessed at -- a mis-parsed leg would invent an
    arbitrage that is not there, which is the failure mode this whole project
    keeps running into.
    """
    s = series.upper()
    up = suffix.upper()

    # A period-restricted market lives on a different state space entirely.
    if not is_full_game(s):
        return None

    if s.endswith("SCORE"):
        # RMA1RSO0 -> home scored 1, away scored 0
        m = re.fullmatch(rf"{home}(\d+){away}(\d+)", up)
        if not m:
            return None
        h, a = int(m.group(1)), int(m.group(2))
        return lambda x, y, h=h, a=a: x == h and y == a

    if s.endswith("BTTS"):
        return lambda x, y: x >= 1 and y >= 1

    if s.endswith("GAME"):
        if up == home:
            return lambda x, y: x > y
        if up == away:
            return lambda x, y: y > x
        if up in ("TIE", "DRAW"):
            return lambda x, y: x == y
        return None

    if s.endswith("TEAMTOTAL"):
        m = re.fullmatch(r"([A-Z]+)(\d+)", up)
        if not m:
            return None
        team, k = m.group(1), int(m.group(2))
        if team == home:
            return lambda x, y, k=k: x >= k
        if team == away:
            return lambda x, y, k=k: y >= k
        return None

    if s.endswith("SPREAD"):
        m = re.fullmatch(r"([A-Z]+)(\d+)", up)
        if not m:
            return None
        team, k = m.group(1), int(m.group(2))
        if team == home:
            return lambda x, y, k=k: x - y >= k
        if team == away:
            return lambda x, y, k=k: y - x >= k
        return None

    if s.endswith("TOTAL"):
        if not up.isdigit():
            return None
        k = int(up)
        return lambda x, y, k=k: x + y >= k

    return None


# --------------------------------------------------------------------------- #
# The LP
# --------------------------------------------------------------------------- #
def check(game: str, legs: Iterable[Leg], *, max_goals: int = MAX_GOALS,
          tol: float = 0.0) -> JointResult:
    """Is there a distribution pricing every leg inside its spread?

    `tol` widens every spread by that many probability units before testing, so
    a violation has to exceed it to be reported. It exists because a one-tick
    numerical wobble is not an arbitrage, and because the alternative is a
    scanner that reports thousands of half-cent "opportunities".
    """
    legs = [l for l in legs if l.covers is not None]
    if len(legs) < 3:
        return JointResult(game, len(legs), True, 0.0, "fewer than 3 parsed legs")

    grid = states(max_goals)
    n = len(grid) + 1                      # + the free OTHER state
    other = n - 1

    # Feasibility as a minimisation of the worst spread violation.  Variables
    # are the state probabilities plus a scalar slack; if the optimal slack is
    # zero a consistent distribution exists.
    c = np.zeros(n + 1)
    c[-1] = 1.0                            # minimise slack

    rows, rhs = [], []
    for leg in legs:
        ind = np.zeros(n + 1)
        for i, (h, a) in enumerate(grid):
            if leg.covers(h, a):
                ind[i] = 1.0
        # sum(p over covered) - slack <= ask
        r = ind.copy(); r[-1] = -1.0
        rows.append(r); rhs.append(leg.ask + tol)
        # -sum(p over covered) - slack <= -bid
        r = -ind; r[-1] = -1.0
        rows.append(r); rhs.append(-(leg.bid - tol))

    A_eq = np.zeros((1, n + 1)); A_eq[0, :n] = 1.0     # probabilities sum to 1
    bounds = [(0.0, 1.0)] * n + [(0.0, None)]
    bounds[other] = (0.0, 1.0)

    res = linprog(c, A_ub=np.array(rows), b_ub=np.array(rhs),
                  A_eq=A_eq, b_eq=np.array([1.0]), bounds=bounds,
                  method="highs")
    if not res.success:
        return JointResult(game, len(legs), True, 0.0, f"solver: {res.message}")

    slack = float(res.x[-1])
    feasible = slack <= 1e-9
    return JointResult(game, len(legs), feasible, slack,
                       "consistent" if feasible else
                       f"no distribution fits; worst leg is {slack*100:.2f}c outside its spread")
