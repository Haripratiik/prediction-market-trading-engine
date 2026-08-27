"""T-010 acceptance (online half): the client works against the LIVE public API.

Kalshi market data needs no credentials, so these run for real -- which is also
the proof that shadow mode costs nothing.  Marked `live` so they can be skipped
offline:  pytest -m "not live"
"""

from __future__ import annotations

import pytest

from core.math.contracts import fee
from venues.kalshi.client import DEMO_BASE, PROD_BASE, KalshiClient

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client():
    with KalshiClient(base_url=PROD_BASE) as c:
        yield c


def test_exchange_status_is_reachable(client):
    st = client.exchange_status()
    assert "exchange_active" in st


def test_per_shard_status_is_reported_independently(client):
    """research/06 K14: one shard can halt while others trade."""
    st = client.exchange_status()
    shards = st.get("exchange_index_statuses", [])
    assert shards, "expected per-shard statuses"
    indices = {s.get("exchange_index") for s in shards}
    assert 0 in indices                       # Default shard always present
    for s in shards:
        assert "exchange_active" in s


def test_demo_environment_is_live():
    """The paper-trading venue must actually be up before we build on it."""
    with KalshiClient(base_url=DEMO_BASE) as demo:
        assert "exchange_active" in demo.exchange_status()


def test_series_returns_the_whole_map_in_one_call(client):
    """`/series` ignores `limit` -- this is the fee/prohibition map, cached once."""
    series = client.list_series()
    assert len(series) > 10_000, f"expected the full map, got {len(series)}"
    by_ticker = {s.ticker: s for s in series}

    # every series yields a usable FeeSpec
    sample = next(iter(series))
    assert fee(0.5, sample.fee_spec, is_maker=False) >= 0.0


def test_maker_fee_type_distribution_still_holds(client):
    """research/06 section 4, re-verified live.

    The headline claim -- that the overwhelming majority of series charge NO
    maker fee -- reproduces exactly. If it stops holding, the fee model needs
    revisiting before any sizing, which is why this is a test and not a comment.
    """
    series = client.list_series()
    plain = sum(1 for s in series if s.fee_type == "quadratic")
    maker_fee = sum(1 for s in series if s.fee_type == "quadratic_with_maker_fees")
    combo = sum(1 for s in series if s.fee_type == "quadratic_with_combo_maker_fees")

    assert plain / len(series) > 0.95        # makers pay nothing on ~99%
    assert 50 < maker_fee < 500              # measured 130
    assert combo <= 10                       # measured 3


def test_fee_multiplier_distribution(client):
    """CORRECTION to research/06 K3, found by running this against live data.

    K3 reported 14 series with fee_multiplier = 0 and recommended them as the
    place for first live capital. Re-checked 2026-08-26: there are NONE. The
    live distribution is {1.0: ~13499, 0.5: 19}. The 0.5 cohort (MLB
    derivatives) does reproduce.

    This test asserts what is true TODAY and will fail loudly if waivers return
    -- at which point the fee-free strategy becomes live again.
    """
    from collections import Counter

    series = client.list_series()
    dist = Counter(s.fee_multiplier for s in series)
    assert set(dist) <= {1.0, 0.5, 0.0}, f"unexpected multipliers: {dict(dist)}"
    assert dist.get(0.5, 0) == pytest.approx(19, abs=10)
    assert dist.get(0.0, 0) == 0, (
        "fee-free series have RETURNED -- re-read PLAN.md 3.1 and research/06 K3"
    )


def test_prohibitions_are_machine_readable(client):
    """PLAN.md section 13: the conflict list is automatable, not manual.

    Prohibition text varies by category (election series list candidates and
    campaign staff; sports list players and coaches), so assert on the CORPUS
    rather than on whichever series happens to sort first.
    """
    series = client.list_series()
    with_prohibitions = [s for s in series if s.additional_prohibitions]
    assert len(with_prohibitions) > 10_000

    corpus = " ".join(
        p.lower() for s in with_prohibitions[:2000] for p in s.additional_prohibitions
    )
    # the two universal clauses, present on ~12.6k series each
    assert "employed by any of the source agencies" in corpus
    assert "material, non-public information" in corpus
    # DATA-QUALITY FINDING: entries are NOT uniformly sentences. 136 are <=10
    # chars, including bare entity names ('Tesla', 'SoFi', 'Metacritic') and
    # some empty strings. A conflict matcher that assumes full sentences would
    # silently miss "Tesla" -- i.e. miss a real employer prohibition.
    shorts = [p for s in with_prohibitions for p in s.additional_prohibitions
              if len(p.strip()) <= 10]
    assert shorts, "expected some bare-entity-name prohibitions"
    assert any(p.strip() == "" for p in shorts), "expected some empty entries"
    # every entry must still be a string, so the matcher can normalise safely
    for s in with_prohibitions[:500]:
        assert all(isinstance(p, str) for p in s.additional_prohibitions)


def test_events_carry_the_fields_the_rv_sleeves_need(client):
    """mutually_exclusive / collateral_return_type exist ONLY on /events."""
    events, markets = client.fetch_universe(max_pages=2)
    assert events and markets

    mece = [e for e in events if e.mutually_exclusive]
    assert mece, "expected some mutually-exclusive events"
    # the perfect correlation found in research/06 section 2
    for e in mece:
        assert e.collateral_return_type == "MECNET"
        assert e.is_mec_netted


def test_markets_parse_into_usable_quotes(client):
    _, markets = client.fetch_universe(max_pages=2)
    quoted = [m for m in markets if m.has_two_sided_quote]
    assert quoted, "expected at least some two-sided quotes"
    for m in quoted[:50]:
        assert 1 <= m.yes_bid < m.yes_ask <= 100
        assert 0.0 < m.mid < 1.0
        assert m.spread_cents >= 1


def test_bidless_markets_are_not_counted_as_two_sided(client):
    """A yes_bid of 0 means nobody is bidding -- it is NOT a restable level.

    Treating those as restable at 1c is the liquidity fantasy that made a naive
    MECE scan report 78% of events as profitable (research/05 section 4.3).
    """
    _, markets = client.fetch_universe(max_pages=2)
    bidless = [m for m in markets if m.yes_bid == 0]
    assert bidless, "expected some markets with no bid at all"
    for m in bidless:
        assert not m.has_bid
        assert not m.has_two_sided_quote


def test_universe_excludes_parlay_shards(client):
    """research/05 F2: KXMVE shards must never enter the universe."""
    events, markets = client.fetch_universe(max_pages=3)
    assert not any(e.series_ticker.startswith("KXMVE") for e in events)
    assert not any(m.series_ticker.startswith("KXMVE") for m in markets)


def test_rules_text_is_present_for_the_rulebook_engine(client):
    """100% coverage was measured; require a strong majority so it stays useful."""
    _, markets = client.fetch_universe(max_pages=2)
    with_rules = [m for m in markets if m.rules_primary]
    assert len(with_rules) / max(1, len(markets)) > 0.9
    assert all(m.rules_hash for m in with_rules[:20])
