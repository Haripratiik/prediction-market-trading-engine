"""T-059: the joint no-arbitrage LP over every market on one game.

The pairwise scans in this project each check ONE logical relation. This checks
whether ANY probability distribution prices every market on a game inside its
own spread, which catches inconsistencies that only appear across three or more
markets at once.

The tests below are mostly about NOT reporting arbitrage that is not there.
Every historical false positive from this scanner came from a mis-parsed leg,
and a mis-parsed leg always looks the same: a violation far too large to be real.
"""

from __future__ import annotations

import pytest

from rulebook.jointarb import (Leg, board_grid, check, is_full_game,
                               parse_leg, required_grid, states)

HOME, AWAY = "RMA", "RSO"


def leg(series, suffix, bid, ask):
    cov = parse_leg(series, suffix, HOME, AWAY)
    assert cov is not None, f"{series}-{suffix} did not parse"
    return Leg(f"{series}-X-{suffix}", bid / 100.0, ask / 100.0, cov)


# --------------------------------------------------------------------------- #
# Parsing.  A wrong indicator invents arbitrage, so these are the load-bearing tests.
# --------------------------------------------------------------------------- #
def test_period_markets_are_refused_because_they_price_a_different_question():
    """`KXLALIGA1HTOTAL` ends in TOTAL but means the FIRST HALF.

    Matching on `endswith` alone pulled half-time markets onto the full-time
    state space and manufactured violations of 14 to 24 cents on real boards.
    """
    assert is_full_game("KXLALIGATOTAL")
    assert is_full_game("KXMLBTOTAL")
    assert not is_full_game("KXLALIGA1HTOTAL")
    assert not is_full_game("KXLALIGA2HBTTS")
    assert not is_full_game("KXMLBF5TOTAL")
    assert parse_leg("KXLALIGA1HTOTAL", "2", HOME, AWAY) is None


def test_the_trailing_number_means_at_least_that_many():
    """Kalshi writes "over 1.5" as suffix 2.  Off by one here silently shifts
    every ladder rung and breaks the whole board."""
    over_1_5 = parse_leg("KXLALIGATOTAL", "2", HOME, AWAY)
    assert over_1_5(1, 1) and over_1_5(2, 0)      # 2 goals qualifies
    assert not over_1_5(1, 0)                     # 1 goal does not


def test_each_family_maps_to_the_right_scorelines():
    home_wins = parse_leg("KXLALIGAGAME", HOME, HOME, AWAY)
    assert home_wins(2, 1) and not home_wins(1, 1) and not home_wins(0, 1)

    exact = parse_leg("KXLALIGASCORE", "RMA2RSO1", HOME, AWAY)
    assert exact(2, 1) and not exact(1, 2)

    btts = parse_leg("KXLALIGABTTS", "BTTS", HOME, AWAY)
    assert btts(1, 1) and not btts(3, 0)

    by_2 = parse_leg("KXLALIGASPREAD", "RMA2", HOME, AWAY)
    assert by_2(3, 1) and not by_2(2, 1)          # margin must be >= 2

    team = parse_leg("KXLALIGATEAMTOTAL", "RSO2", HOME, AWAY)
    assert team(0, 2) and not team(5, 1)          # away team's own goals


def test_an_unknown_suffix_is_dropped_rather_than_guessed():
    assert parse_leg("KXLALIGAGAME", "WHO", HOME, AWAY) is None
    assert parse_leg("KXLALIGASCORE", "GARBAGE", HOME, AWAY) is None


def test_the_grid_is_sized_from_the_board_not_assumed():
    """A WNBA board asks for thresholds near 160.  Scoring it on a 0-9 goal
    grid reported a 76 cent violation that was entirely the grid not fitting."""
    assert required_grid(["2", "3", "RMA2"]) == 3
    assert required_grid(["161", "155"]) == 161


# --------------------------------------------------------------------------- #
# The LP itself
# --------------------------------------------------------------------------- #
def test_a_coherent_board_is_reported_consistent():
    """Prices generated from ONE distribution must never look like arbitrage."""
    legs = [
        leg("KXLALIGAGAME", HOME, 40, 44),
        leg("KXLALIGAGAME", AWAY, 30, 34),
        leg("KXLALIGAGAME", "TIE", 24, 28),
        leg("KXLALIGATOTAL", "1", 85, 90),
        leg("KXLALIGATOTAL", "2", 60, 66),
    ]
    assert check("coherent", legs).feasible


def test_a_moneyline_that_contradicts_its_own_spread_is_caught():
    """"Wins by more than 1.5" cannot be more likely than "wins"."""
    legs = [
        leg("KXLALIGAGAME", HOME, 20, 22),        # home wins at most 22%
        leg("KXLALIGASPREAD", "RMA2", 60, 62),    # but wins BY 2+ at least 60%
        leg("KXLALIGATOTAL", "1", 80, 90),
    ]
    r = check("contradiction", legs)
    # The LP minimises the WORST violation, so it splits the gap: with the
    # moneyline capped at 0.22 and the spread floored at 0.60, the smallest
    # feasible slack is (0.60 - 0.22) / 2 = 0.19.
    assert r.arbitrage and r.slack > 0.15


def test_a_total_ladder_that_runs_the_wrong_way_is_caught():
    """Over 2.5 cannot be more likely than over 1.5."""
    legs = [
        leg("KXLALIGATOTAL", "2", 20, 22),
        leg("KXLALIGATOTAL", "3", 70, 72),
        leg("KXLALIGAGAME", HOME, 40, 45),
    ]
    assert check("bad ladder", legs).arbitrage


def test_exact_scores_that_overfill_the_probability_are_caught():
    """Disjoint scorelines whose bids sum above 1 cannot all be right."""
    legs = [leg("KXLALIGASCORE", f"RMA{h}RSO0", 40, 45) for h in range(4)]
    assert check("overfull", legs).arbitrage


def test_a_thin_board_is_not_judged():
    """Fewer than three parsed legs cannot constrain anything, and reporting
    'consistent' there would overstate what was checked."""
    r = check("thin", [leg("KXLALIGAGAME", HOME, 40, 45)])
    assert r.feasible and "fewer than 3" in r.detail


def test_the_tolerance_suppresses_a_sub_tick_violation():
    """A one cent wobble is not an arbitrage, and a scanner that says it is
    produces thousands of unactionable hits."""
    legs = [
        leg("KXLALIGAGAME", HOME, 30, 31),
        leg("KXLALIGASPREAD", "RMA2", 32, 33),    # 1c above where it may sit
        leg("KXLALIGATOTAL", "1", 80, 90),
    ]
    assert check("wobble", legs, tol=0.0).arbitrage
    assert check("wobble", legs, tol=0.02).feasible


def test_the_free_other_state_only_ever_makes_the_lp_easier():
    """Mass outside the grid is unclaimed by every market, so truncation can
    never CREATE a violation -- a reported one is a lower bound."""
    assert len(states(3)) == 16
    # Both sides winning 40% of the time REQUIRES goals, so pairing that with
    # "over 0.5 goals at 12%" is a genuine contradiction, not a truncation
    # artifact.  The board here is coherent, and must stay feasible on a grid
    # far too small to hold every scoreline.
    legs = [
        leg("KXLALIGATOTAL", "1", 88, 92),
        leg("KXLALIGAGAME", HOME, 40, 45),
        leg("KXLALIGAGAME", AWAY, 40, 45),
    ]
    assert check("sparse", legs, max_goals=3).feasible


# --------------------------------------------------------------------------- #
# Sizing the grid from the board.  These guard the SECOND parse trap: a number
# that is not a scoring threshold at all.
# --------------------------------------------------------------------------- #
def test_a_game_code_is_not_a_scoring_threshold():
    """`ticker.split("-")[-1]` yields the GAME CODE on suffix-less tickers.

    `KXMLBERA-26AUG261610PITSD` has no outcome suffix, so the naive split hands
    back the game code. Its digits run together into `261610`, which reads as a
    scoring threshold for a baseball game. Only legs that actually parse may
    size the grid.
    """
    board = [("KXMLBGAME", "PIT"), ("KXMLBGAME", "SD"),
             ("KXMLBERA", "26AUG261610PITSD"),      # game code, unparsable
             ("KXMLBTOTAL", "9")]
    assert required_grid([s for _, s in board]) == 261610    # the trap
    assert board_grid(board, "PIT", "SD") == 9               # the fix


def test_a_half_time_market_does_not_size_the_full_time_grid():
    """A period leg never enters the LP, so its digits must not size the grid."""
    board = [("KXLALIGATOTAL", "3"), ("KXLALIGA1HTOTAL", "7")]
    assert board_grid(board, HOME, AWAY) == 3


def test_the_grid_still_fits_a_high_scoring_board():
    """The point of sizing is that basketball must not be silently truncated."""
    board = [("KXWNBAGAME", "TOR"), ("KXWNBAGAME", "SEA"),
             ("KXWNBATOTAL", "160")]
    assert board_grid(board, "TOR", "SEA") == 160


def test_an_oversized_grid_cannot_manufacture_a_violation():
    """Sizing too LARGE is the safe direction, and this pins that claim.

    A bigger grid only adds free states, which can only ever make the LP easier
    to satisfy. A board that is consistent on its own grid stays consistent on
    a much larger one.
    """
    legs = [Leg("t3", 0.30, 0.34, parse_leg("KXLALIGATOTAL", "3", HOME, AWAY)),
            Leg("h", 0.50, 0.54, parse_leg("KXLALIGAGAME", HOME, HOME, AWAY)),
            Leg("a", 0.28, 0.32, parse_leg("KXLALIGAGAME", AWAY, HOME, AWAY))]
    tight = check("g", legs, max_goals=9)
    wide = check("g", legs, max_goals=40)
    assert tight.feasible and wide.feasible
    assert wide.slack <= tight.slack + 1e-9
