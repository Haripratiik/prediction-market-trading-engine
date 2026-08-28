"""T-060: no-arbitrage ACROSS two separate events.

Kalshi prices a day's maximum temperature in one event and the same day's 4pm
reading in another. The max is at least the 4pm reading, so the two books are
bound together even though they never touch.

As in `test_jointarb`, most of these tests exist to stop the scanner reporting
arbitrage that is not there. The failure that motivated the completeness guard
produced an "80 cent riskless edge" in Miami, which is the usual tell.
"""

from __future__ import annotations

from rulebook.crossevent import (INF, Bucket, Threshold, check_high, check_low,
                                 covers_downward, covers_upward, parse_bucket,
                                 parse_threshold)

CITY = "MIA"


def _mia(*, ask: int) -> list[Bucket]:
    """A complete daily-high partition: <86, 86-87, 88-89, 90-91, >91."""
    return [Bucket("lo", -INF, 85, ask - 1, ask),
            Bucket("b86", 86, 87, ask - 1, ask),
            Bucket("b88", 88, 89, ask - 1, ask),
            Bucket("b90", 90, 91, ask - 1, ask),
            Bucket("hi", 92, INF, ask - 1, ask)]


# --------------------------------------------------------------------------- #
# Titles carry the meaning; ticker suffixes encode it three different ways.
# --------------------------------------------------------------------------- #
def test_a_bucket_title_gives_inclusive_whole_degrees():
    assert parse_bucket("Will the maximum temperature be 82-83 deg on Aug 26, 2026?") == (82, 83)


def test_the_tails_are_open_and_do_not_overlap_the_buckets():
    assert parse_bucket("Will the maximum temperature be <82 deg on Aug 26?") == (-INF, 81)
    assert parse_bucket("Will the maximum temperature be >89 deg on Aug 26?") == (90, INF)


def test_an_hourly_threshold_rounds_up_to_the_next_whole_degree():
    """Readings are whole degrees, so "above 83.99" is satisfied only by 84."""
    t = "Will the temp in Los Angeles be above 83.99 deg on Aug 26, 2026 at 4pm EDT?"
    assert parse_threshold(t) == 84


def test_a_daily_bucket_is_not_mistaken_for_an_hourly_threshold():
    assert parse_threshold("Will the maximum temperature be 82-83 deg on Aug 26?") is None
    assert parse_bucket("Will the temp be above 83.99 deg at 4pm EDT?") is None


# --------------------------------------------------------------------------- #
# The completeness guard.  This is the one that matters.
# --------------------------------------------------------------------------- #
def test_a_complete_partition_covers_upward():
    assert covers_upward(_mia(ask=20), 88) is True


def test_a_gap_in_the_basket_is_not_a_hedge():
    """Drop the 88-89 bucket and the day's max can land in an unbought state."""
    bks = [b for b in _mia(ask=20) if b.ticker != "b88"]
    assert covers_upward(bks, 86) is False


def test_a_missing_top_tail_is_not_a_hedge():
    """Without the open-ended tail the trade loses whenever it is very hot."""
    bks = [b for b in _mia(ask=20) if b.ticker != "hi"]
    assert covers_upward(bks, 88) is False


def test_the_mirror_guard_works_downward():
    full = _mia(ask=20)
    assert covers_downward(full, 89) is True
    assert covers_downward([b for b in full if b.ticker != "lo"], 89) is False


# --------------------------------------------------------------------------- #
# The scan itself
# --------------------------------------------------------------------------- #
def test_an_incomplete_basket_reports_nothing_even_when_it_looks_free():
    """The 80c Miami "edge": a basket cheap only because legs were missing.

    Three legs at 1c against an 83c hourly bid is an 80 cent riskless profit if
    you do not notice the partition has a hole in it.
    """
    holed = [b for b in _mia(ask=1) if b.ticker in ("b88", "b90", "hi")]
    hourly = [Threshold("h84", 84, 83, 85)]
    assert check_high(CITY, holed, hourly) == []


def test_a_coherent_board_reports_no_arbitrage():
    """Basket at 4 x 20c = 80c against a 60c hourly bid: correctly ordered."""
    bks = _mia(ask=20)
    assert check_high(CITY, bks, [Threshold("h86", 86, 60, 64)]) == []


def test_a_genuine_violation_is_still_detected():
    """Non-vacuity: when the basket really is complete AND underpriced, say so.

    The whole partition from 86 upward costs 4 x 2c = 8c while the hourly bids
    50c, so selling the hourly and buying the basket collects 42c that cannot
    be lost.
    """
    bks = _mia(ask=2)
    hits = check_high(CITY, bks, [Threshold("h86", 86, 50, 54)])
    assert len(hits) == 1
    assert hits[0].edge_cents == 50 - 8
    assert hits[0].kind == "high"


def test_a_straddling_bucket_must_be_bought_not_skipped():
    """X=87 falls inside 86-87, and that bucket has to be in the basket.

    Skipping it would leave `max == 87` unhedged while the hourly still pays,
    which turns a riskless trade into a losing one.
    """
    bks = _mia(ask=2)
    hits = check_high(CITY, bks, [Threshold("h87", 87, 50, 54)])
    assert len(hits) == 1
    assert "b86" in hits[0].basket


def test_the_low_side_sells_the_no_leg_not_the_yes():
    """`min <= X` is implied by `temp <= X`, which is the hourly's NO side."""
    bks = _mia(ask=2)
    hits = check_low(CITY, bks, [Threshold("h90", 90, 8, 10)])
    assert len(hits) == 1
    assert hits[0].hourly_bid_cents == 90          # 100 - ask, not the 8c bid


# --------------------------------------------------------------------------- #
# The same idea in sport: a knockout tie priced as two events.
# --------------------------------------------------------------------------- #
from rulebook.crossevent import TieLegs, check_advance   # noqa: E402


def _tie(**kw) -> TieLegs:
    base = dict(team="TOL", win_bid=40, win_ask=42, draw_bid=25, draw_ask=27,
                adv_bid=60, adv_ask=63)
    return TieLegs(**{**base, **kw})


def test_a_coherent_cup_tie_reports_nothing():
    """win 40/42, draw 25/27, advance 60/63 sits inside the sandwich."""
    assert check_advance("FIX", [_tie()]) == []


def test_advance_priced_under_the_win_is_arbitrage():
    """Winning the match means going through, so advance cannot be cheaper.

    This is the shape actually observed: the game book repriced to 87 on a
    goal while the advance book still offered at 85.
    """
    hits = check_advance("FIX", [_tie(win_bid=87, win_ask=88,
                                      adv_bid=82, adv_ask=85)])
    assert len(hits) == 1
    assert hits[0].kind == "advance-under"
    assert hits[0].edge_cents == 87 - 85


def test_advance_priced_over_win_plus_draw_is_arbitrage():
    """You cannot go through having lost in regulation."""
    hits = check_advance("FIX", [_tie(win_ask=10, draw_ask=15, adv_bid=40)])
    assert len(hits) == 1
    assert hits[0].kind == "advance-over"
    assert hits[0].edge_cents == 40 - 25


def test_the_draw_leg_is_required_for_the_upper_bound():
    """Advance legitimately exceeds the win by the whole draw probability.

    Forgetting the draw would flag every normal knockout tie as arbitrage,
    since a team that draws still advances on penalties.
    """
    assert check_advance("FIX", [_tie(win_ask=42, draw_ask=27, adv_bid=60)]) == []
    assert len(check_advance("FIX", [_tie(win_ask=42, draw_ask=0, adv_bid=60)])) == 1
