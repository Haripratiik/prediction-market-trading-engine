"""T-006 acceptance: schema round-trips, and the append-only invariant is
enforced by the DATABASE rather than by convention."""

from __future__ import annotations

import sqlite3

import pytest

from core.db import Database
from core.models import Event, Market, Series, SettlementSource, Venue, now_us


@pytest.fixture()
def db():
    with Database(":memory:") as d:
        yield d


def test_migration_creates_every_table(db):
    assert set(db.counts()) == {
        "market_snapshots", "event_snapshots", "series_cache",
        "rules_docs", "trades", "orders", "fills", "decisions", "settlements",
    }
    assert all(v == 0 for v in db.counts().values())


def test_series_round_trip_preserves_the_fee_spec(db):
    s = Series(
        ticker="KXTEST",
        title="Test series",
        category="Economics",
        fee_type="quadratic_with_maker_fees",
        fee_multiplier=0.5,
        settlement_sources=(SettlementSource(name="BLS", url="https://bls.gov"),),
        additional_prohibitions=("Persons who are employed by any of the Source Agencies...",),
    )
    db.upsert_series([s])
    got = db.get_series("KXTEST")
    assert got is not None
    assert got.fee_type == "quadratic_with_maker_fees"
    assert got.fee_multiplier == 0.5
    assert got.settlement_sources[0].name == "BLS"
    assert got.additional_prohibitions
    # and the FeeSpec it produces is the one the math layer wants
    assert got.fee_spec.fee_multiplier == 0.5
    assert got.charges_maker_fees


def test_series_upsert_is_idempotent(db):
    s = Series(ticker="KXA", fee_type="quadratic", fee_multiplier=1.0)
    db.upsert_series([s])
    db.upsert_series([s])
    assert db.counts()["series_cache"] == 1


def test_market_snapshots_are_append_only(db):
    """R5a enforced by a trigger. Overwriting is how look-ahead leaks in."""
    m = Market(ticker="KXA-1", event_ticker="KXA", yes_bid=40, yes_ask=42)
    db.append_markets([m])
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.conn.execute("UPDATE market_snapshots SET yes_bid = 99")


def test_event_snapshots_are_append_only(db):
    db.append_events([Event(event_ticker="KXA", mutually_exclusive=True)])
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.conn.execute("UPDATE event_snapshots SET mutually_exclusive = 0")


def test_repeated_observations_accumulate_rather_than_replace(db):
    m1 = Market(ticker="KXA-1", yes_bid=40, yes_ask=42)
    m2 = Market(ticker="KXA-1", yes_bid=45, yes_ask=47)
    t1, t2 = now_us(), now_us() + 1_000_000
    db.append_markets([m1], observed_at_us=t1)
    db.append_markets([m2], observed_at_us=t2)
    assert db.counts()["market_snapshots"] == 2


def test_point_in_time_read_never_sees_the_future(db):
    """The anti-look-ahead accessor: as_of returns the row visible THEN."""
    t1 = now_us()
    t2 = t1 + 10_000_000
    db.append_markets([Market(ticker="KXA-1", yes_bid=40, yes_ask=42)], observed_at_us=t1)
    db.append_markets([Market(ticker="KXA-1", yes_bid=90, yes_ask=92)], observed_at_us=t2)

    early = db.latest_market("KXA-1", as_of_us=t1 + 1)
    late = db.latest_market("KXA-1", as_of_us=t2 + 1)
    assert early["yes_bid"] == 40           # the later 90 is invisible
    assert late["yes_bid"] == 90


def test_point_in_time_read_before_any_observation_is_none(db):
    t = now_us()
    db.append_markets([Market(ticker="KXA-1", yes_bid=40, yes_ask=42)], observed_at_us=t)
    assert db.latest_market("KXA-1", as_of_us=t - 1) is None


def test_event_flags_persist_including_our_separate_verdict(db):
    """mutually_exclusive is the exchange's; exhaustive_verified is ours."""
    db.append_events([
        Event(
            event_ticker="KXNEWPOPE-70",
            mutually_exclusive=True,
            collateral_return_type="MECNET",
            exhaustive_verified=False,
        )
    ])
    row = db.conn.execute("SELECT * FROM event_snapshots").fetchone()
    assert row["mutually_exclusive"] == 1
    assert row["exhaustive_verified"] == 0      # NOT inherited from the flag
    assert row["collateral_return_type"] == "MECNET"


def test_rules_are_deduplicated_by_hash(db):
    db.store_rules("kalshi", "KXA-1", "hash123", "Some rules text")
    db.store_rules("kalshi", "KXA-1", "hash123", "Some rules text")
    assert db.counts()["rules_docs"] == 1


def test_market_model_round_trips_through_the_snapshot_table(db):
    m = Market(
        venue=Venue.KALSHI, ticker="KXA-1", event_ticker="KXA", series_ticker="KXA",
        yes_bid=40, yes_ask=42, yes_bid_size=100.0, volume_24h=250.0,
    )
    db.append_markets([m])
    row = db.latest_market("KXA-1")
    assert row["yes_bid"] == 40
    assert row["yes_ask"] == 42
    assert row["yes_bid_size"] == 100.0
    assert row["volume_24h"] == 250.0
