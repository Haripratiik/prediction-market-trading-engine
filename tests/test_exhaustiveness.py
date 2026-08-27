"""T-050b acceptance: the known non-exhaustive live events are ALL rejected.

These are the events that a naive scanner ranks as its most profitable
opportunities and which return $0 when the winner is unlisted (research/05 F1).
They are committed here as a regression fixture -- if the gate ever stops
rejecting them, the RV sleeves are unsafe.
"""

from __future__ import annotations

import pytest

from core.models import Event, Market
from rulebook.exhaustiveness import (
    MIN_SUM_BID_FOR_EXHAUSTIVE,
    Verdict,
    check_mece,
    has_catch_all_leg,
)


def mk_event(ticker: str = "KXTEST", **kw) -> Event:
    kw.setdefault("mutually_exclusive", True)
    kw.setdefault("collateral_return_type", "MECNET")
    return Event(event_ticker=ticker, **kw)


def legs(*quotes: tuple[int, int], titles: list[str] | None = None) -> list[Market]:
    """Build markets from (bid, ask) cent pairs."""
    out = []
    for i, (bid, ask) in enumerate(quotes):
        out.append(
            Market(
                ticker=f"KXTEST-{i}",
                event_ticker="KXTEST",
                title=(titles[i] if titles else f"Outcome {i}"),
                yes_bid=bid,
                yes_ask=ask,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# The F1 trap, as observed live.  (event, n_legs, sum_ask)
# --------------------------------------------------------------------------- #
KNOWN_NON_EXHAUSTIVE = [
    ("KXLAPRIMARY-02D26", 2, 0.116, "LA-02 Democratic nominee?"),
    ("KXLAPRIMARY-01D26", 2, 0.119, "LA-01 Democratic nominee?"),
    ("KXLAPRIMARY-01R26", 2, 0.125, "LA-01 Republican nominee?"),
    ("KXSTATE51-29", 8, 0.140, "What will be the 51st state in Trump's term?"),
    ("KXLAPRIMARY-04D26", 2, 0.148, "LA-04 Democratic nominee?"),
    ("KXLAPRIMARY-05D26", 5, 0.155, "LA-05 Democratic nominee?"),
    ("KXNBERRECESSQ", 6, 0.198, "When will the next US recession start?"),
    ("KXNEWPOPE-70", 7, 0.282, "Who will the next Pope be?"),
    ("KXACQUANNOUNCEPINS-27JAN01", 6, 0.310, "Who will acquire Pinterest this year?"),
    ("KXNEXTTEAMNFL-26BSOR", 32, 0.320, "Brendan Sorsby's Next Team"),
]


@pytest.mark.parametrize("ticker,n_legs,sum_ask,title", KNOWN_NON_EXHAUSTIVE)
def test_known_traps_are_rejected(ticker, n_legs, sum_ask, title):
    """Every one of these must be REJECTED, with exhaustiveness cited."""
    # spread the observed sum(ask) across n legs, bids a cent below
    ask_each = max(1, round(sum_ask * 100 / n_legs))
    quotes = [(max(1, ask_each - 1), ask_each)] * n_legs
    ev = mk_event(ticker, title=title)
    check = check_mece(ev, legs(*quotes))

    assert check.verdict is Verdict.REJECTED, f"{ticker} must be rejected"
    assert any("cover the outcome space" in r for r in check.reasons), check.reasons
    assert not check.safe_to_buy


def test_an_exhaustive_book_is_not_rejected_for_exhaustiveness():
    """A bucketed temperature-style book sums close to 1 and should pass check 2."""
    check = check_mece(mk_event(), legs((19, 21), (24, 26), (29, 31), (24, 26)))
    assert check.sum_bid == pytest.approx(0.96, abs=0.01)
    assert not any("cover the outcome space" in r for r in check.reasons)


def test_a_catch_all_leg_rescues_a_cheap_book():
    """An explicit Other/None leg is what makes a candidate list exhaustive."""
    quotes = [(4, 5), (4, 5), (4, 5)]
    titles = ["Alice", "Bob", "Other candidate"]
    check = check_mece(mk_event(), legs(*quotes, titles=titles))
    assert check.has_catch_all
    assert not any("cover the outcome space" in r for r in check.reasons)


@pytest.mark.parametrize(
    "title", ["Other", "None of the above", "Any other candidate", "Neither",
              "No one", "Nobody", "All others", "Someone else", "The field"]
)
def test_catch_all_detection_covers_the_common_phrasings(title):
    assert has_catch_all_leg([Market(ticker="x", title=title)])


def test_catch_all_does_not_false_positive_on_normal_names():
    for title in ["Donald Trump", "Kamala Harris", "Above 3.75%", "80 to 81 degrees"]:
        assert not has_catch_all_leg([Market(ticker="x", title=title)])


# --------------------------------------------------------------------------- #
# The other four conditions
# --------------------------------------------------------------------------- #
def test_rejects_when_the_exchange_flag_is_absent():
    ev = Event(event_ticker="KXA", mutually_exclusive=False, collateral_return_type="DIRECNET")
    check = check_mece(ev, legs((49, 51), (49, 51)))
    assert check.verdict is Verdict.REJECTED
    assert any("not flagged mutually_exclusive" in r for r in check.reasons)


def test_many_fallback_sources_are_a_note_not_a_veto():
    """settlement_sources is an event-level FALLBACK list, not per-leg assignment.

    "Who will the next Pope be?" lists 14 news outlets -- that is one settlement
    rule, not fourteen. An earlier draft rejected 3,484 events over this.
    """
    from core.models import SettlementSource
    ev = mk_event(settlement_sources=tuple(
        SettlementSource(name=n) for n in ("NYT", "AP", "Reuters", "CNN", "BBC")
    ))
    check = check_mece(ev, legs((49, 50), (49, 50)))
    assert check.verdict is Verdict.NEEDS_HUMAN          # not REJECTED
    assert any("fallback settlement sources" in r for r in check.reasons)


def test_rejects_non_mecnet_collateral():
    ev = mk_event(collateral_return_type="DIRECNET")
    check = check_mece(ev, legs((49, 51), (49, 51)))
    assert any("MECNET" in r for r in check.reasons)


def test_rejects_a_leg_with_no_bid():
    """A bidless leg is not restable -- the liquidity-fantasy guard."""
    check = check_mece(mk_event(), legs((49, 51), (0, 51)))
    assert check.verdict is Verdict.REJECTED
    assert any("no bid at all" in r for r in check.reasons)
    assert not check.all_legs_restable
    assert not check.safe_to_sell


def test_rejects_fewer_than_two_legs():
    check = check_mece(mk_event(), legs((49, 51)))
    assert check.verdict is Verdict.REJECTED


def test_mechanically_clean_events_still_need_a_human_before_buying():
    """Void clauses require reading the rules text.  Never auto-VERIFY."""
    check = check_mece(mk_event(), legs((49, 50), (49, 50)))
    assert check.verdict is Verdict.NEEDS_HUMAN
    assert not check.safe_to_buy          # buying stays blocked until reviewed
    assert check.safe_to_sell             # selling is already safe


def test_selling_is_safe_even_when_buying_is_rejected():
    """The asymmetry that defines sleeve S2.

    A wildly non-exhaustive book is a guaranteed loss to buy and perfectly fine
    to sell -- max liability is $1 no matter how many outcomes are missing.
    """
    check = check_mece(mk_event(), legs((5, 6), (5, 6), (5, 6)))
    assert check.verdict is Verdict.REJECTED
    assert not check.safe_to_buy
    assert check.safe_to_sell


def test_threshold_is_configurable_and_documented():
    assert MIN_SUM_BID_FOR_EXHAUSTIVE == 0.80
    tight = check_mece(mk_event(), legs((40, 41), (40, 41)), min_sum_bid=0.70)
    assert not any("cover the outcome space" in r for r in tight.reasons)


# --------------------------------------------------------------------------- #
# The human verdict must be able to REACH a sleeve
# --------------------------------------------------------------------------- #
def test_a_recorded_human_verdict_promotes_to_verified():
    """Without this, safe_to_buy was permanently False and the long direction
    was unreachable dead code."""
    ev = mk_event(exhaustive_verified=True)
    check = check_mece(ev, legs((49, 50), (49, 50)))
    assert check.verdict is Verdict.VERIFIED
    assert check.safe_to_buy


def test_a_human_verdict_does_NOT_override_the_mechanical_gates():
    """Review promotes only what already passed the machine checks.  A
    non-exhaustive book stays rejected no matter who signed off on it."""
    ev = mk_event(exhaustive_verified=True)
    check = check_mece(ev, legs((5, 6), (5, 6), (5, 6)))    # sum(bid) = 0.15
    assert check.verdict is Verdict.REJECTED
    assert not check.safe_to_buy


def test_a_bidless_leg_is_not_rescued_by_human_review():
    ev = mk_event(exhaustive_verified=True)
    check = check_mece(ev, legs((49, 51), (0, 51)))
    assert check.verdict is Verdict.REJECTED


# --------------------------------------------------------------------------- #
# Mutual exclusivity is load-bearing for the SHORT side
# --------------------------------------------------------------------------- #
def test_a_non_mutually_exclusive_basket_is_not_safe_to_sell():
    """The bug that reached live quoting.

    `safe_to_sell`'s docstring said it required mutual exclusivity; the code
    checked only leg count and restability.  Without mutual exclusivity an
    n-leg short is capped at $n of liability, not $1 -- nothing stops every leg
    resolving YES at once.

    The real case: KXBTCD-26AUG2817 is flagged mutually_exclusive = 0 and lists
    NESTED THRESHOLD markets ("BTC above $66,000", "above $66,500", ...).  S2
    sized a 21-leg short collecting $11.06 against up to $21 of liability and
    reported a margin of $10.01 per contract -- on an instrument paying at most
    $1.  An arbitrage claiming ten times the maximum payout is not one.
    """
    ladder = legs((96, 97), (97, 98), (98, 99), (95, 96))
    check = check_mece(mk_event(mutually_exclusive=False), ladder)

    assert check.verdict is Verdict.REJECTED
    assert not check.safe_to_sell, "a nested ladder is not a capped-liability short"
    assert not check.safe_to_buy


def test_a_mutually_exclusive_basket_with_bids_on_every_leg_is_safe_to_sell():
    """The short side must stay reachable -- it is S2's entire book."""
    check = check_mece(mk_event(), legs((30, 32), (30, 32), (30, 32), (30, 32)))
    assert check.safe_to_sell


def test_the_flag_is_read_not_inferred_from_the_price_sum():
    """A nested ladder sums far ABOVE 1.0, which looks like a huge overround.

    Inferring exclusivity from the sum would rank exactly the dangerous events
    highest -- the sum is large *because* the legs are not alternatives.
    """
    ladder = legs((96, 97), (97, 98), (98, 99), (95, 96))
    check = check_mece(mk_event(mutually_exclusive=False), ladder)
    assert check.sum_bid > 3.0          # would look like a 300% overround
    assert not check.safe_to_sell
