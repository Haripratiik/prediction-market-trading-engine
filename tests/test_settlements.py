"""Settlement ingestion acceptance.  `recorder/settlements.py`.

The `settlements` table is where "we placed orders" becomes "we know whether we
were right".  Every property proven here is one whose failure shows up as money
or as a false gate promotion, not as a correctness point:

    void != NO      a voided market returns every position AT COST.  Scoring it
                    as a NO biases every Brier score in the same direction on
                    every void -- silent, systematic, and invisible in P&L.
    idempotent      a settlement is written once and NEVER flipped.  P&L is
                    booked against these rows; a row that changes after the fact
                    changes history.
    shadow works    market data settles with no credentials at all, which is the
                    only reason a shadow sleeve can ever reach G3.
    bounded polling the venue lists 106,000+ markets.  Only what we traded or
                    formed an opinion about is polled.

No test here may reach the network except the one marked `live`.  The only
client is `FakeVenue`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from core.config import KalshiCredentials
from core.db import Database
from core.models import Market, Venue, now_us
from monitor.kpi import settled_decisions
from recorder.settlements import (
    Settlement,
    SettlementRecorder,
    credentials_match,
    describe,
)
from venues.kalshi.client import DEMO_BASE, PROD_BASE, KalshiClient, KalshiError

SEC = 1_000_000
T0 = 1_700_000_000 * SEC
VENUE = Venue.KALSHI.value


# pytest's `tmp_path` cannot be used on this machine: its basetemp under
# C:\Users\...\AppData\Local\Temp\pytest-of-harie raises PermissionError
# [WinError 5].  Manage the directory ourselves.
@pytest.fixture()
def workdir():
    d = tempfile.mkdtemp(prefix="pm-settle-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def db():
    with Database(":memory:") as handle:
        yield handle


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #
class FakeVenue:
    """A venue that answers from a dict.  Never opens a socket."""

    def __init__(
        self,
        results: dict[str, tuple[str, int | None, bool]] | None = None,
        *,
        settlements: list[dict[str, Any]] | None = None,
        errors: dict[str, Exception] | None = None,
        portfolio_error: Exception | None = None,
    ) -> None:
        self.results = dict(results or {})
        self._settlements = list(settlements or [])
        self.errors = dict(errors or {})
        self.portfolio_error = portfolio_error
        self.polled: list[str] = []
        self.portfolio_calls = 0

    def market_result(self, ticker: str) -> tuple[str, int | None, bool]:
        self.polled.append(ticker)
        if ticker in self.errors:
            raise self.errors[ticker]
        # An unknown ticker is an open market, the same as the live API.
        return self.results.get(ticker, ("active", None, False))

    def iter_settlements(self, **params: Any):
        self.portfolio_calls += 1
        if self.portfolio_error is not None:
            raise self.portfolio_error
        yield from self._settlements


def no_credentials_venue(results=None, **kw: Any) -> FakeVenue:
    """What the real client does with no signer: `_request` raises before any
    socket is opened, which is exactly the shadow-mode path."""
    return FakeVenue(
        results,
        portfolio_error=KalshiError(401, "no signer configured",
                                    "/portfolio/settlements"),
        **kw,
    )


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #
def add_decision(db, ticker, *, sleeve="S1", at_us=T0, venue=VENUE,
                 p_model=0.60, market_price=0.50, acted=1):
    with db.tx() as c:
        c.execute(
            """INSERT INTO decisions
               (decided_at_us, sleeve_id, venue, ticker, category, market_price,
                p_model, raw_edge, shrunk_edge, acted, preregistration_id)
               VALUES (?,?,?,?,'',?,?,?,?,?,NULL)""",
            (at_us, sleeve, venue, ticker, market_price, p_model,
             p_model - market_price, 0.5 * (p_model - market_price), int(acted)),
        )


def add_order(db, ticker, *, coid=None, at_us=T0, sleeve="S1", venue=VENUE,
              side="yes", price=50, size=100):
    coid = coid or f"coid-{ticker}"
    with db.tx() as c:
        c.execute(
            """INSERT INTO orders
               (client_order_id, created_at_us, sleeve_id, structure_id, venue,
                ticker, side, price_cents, size, post_only, mode, venue_order_id,
                state, rationale_json, updated_at_us)
               VALUES (?,?,?,NULL,?,?,?,?,?,1,'shadow',NULL,'open','{"t":1}',?)""",
            (coid, at_us, sleeve, venue, ticker, side, price, size, at_us),
        )
    return coid


def add_fill(db, coid, *, at_us=T0, price=50, size=100, fill_id=None):
    with db.tx() as c:
        c.execute(
            """INSERT INTO fills
               (filled_at_us, client_order_id, venue_fill_id, price_cents, size,
                fee_cents, is_maker, terminal)
               VALUES (?,?,?,?,?,0,1,1)""",
            (at_us, coid, fill_id or f"fill-{coid}", price, size),
        )


def add_snapshot(db, ticker, *, close_at_us, observed_at_us=T0):
    db.append_markets([Market(ticker=ticker, close_at_us=close_at_us)],
                      observed_at_us=observed_at_us)


def rows(db):
    return {
        r["ticker"]: r
        for r in db.conn.execute("SELECT * FROM settlements").fetchall()
    }


def make_recorder(db, venue, **kw):
    kw.setdefault("sleep_between", 0.0)
    return SettlementRecorder(db, venue, **kw)


# --------------------------------------------------------------------------- #
# The basic write
# --------------------------------------------------------------------------- #
def test_a_finalized_market_that_resolved_yes_is_recorded_as_outcome_one(db):
    """MEASURED against the live API: a settled market reports status
    'finalized', not 'settled'.  Keying off the wrong word records nothing at
    all, and a sleeve with no settlements can never clear G4."""
    add_decision(db, "KXA-1")
    rec = make_recorder(db, no_credentials_venue({"KXA-1": ("finalized", 1, False)}))
    polled, written = rec.cycle()

    assert (polled, written) == (1, 1)
    row = rows(db)["KXA-1"]
    assert row["outcome"] == 1
    assert row["voided"] == 0
    assert row["venue"] == VENUE


def test_a_finalized_market_that_resolved_no_is_recorded_as_outcome_zero(db):
    add_decision(db, "KXA-1")
    rec = make_recorder(db, no_credentials_venue({"KXA-1": ("finalized", 0, False)}))
    rec.cycle()
    assert rows(db)["KXA-1"]["outcome"] == 0
    assert rows(db)["KXA-1"]["voided"] == 0


def test_a_market_that_has_not_settled_yet_is_not_recorded(db):
    """An open market has no outcome.  Writing one would be inventing the future."""
    add_decision(db, "KXA-1")
    rec = make_recorder(db, no_credentials_venue({"KXA-1": ("active", None, False)}))
    assert rec.cycle() == (1, 0)
    assert rows(db) == {}
    assert rec.stats.still_open == 1


def test_a_determined_market_is_not_recorded_because_a_dispute_can_still_amend_it(db):
    """`closed -> determined -> finalized`, and `disputed -> amended` RESTARTS
    the settlement timer (research/06 section 7).  A determined result is not
    final, and these rows are immutable -- recording early is how a wrong
    outcome becomes permanent."""
    add_decision(db, "KXA-1")
    rec = make_recorder(db, no_credentials_venue({"KXA-1": ("determined", 1, False)}))
    assert rec.cycle() == (1, 0)
    assert rows(db) == {}


# --------------------------------------------------------------------------- #
# VOID -- the expensive one
# --------------------------------------------------------------------------- #
def test_a_voided_market_is_recorded_as_voided_and_never_as_a_no(db):
    """A void returns every position AT COST.  It is not a loss and it is not a
    NO.  Scoring one as a NO understates the model's calibration by exactly the
    price paid, on every void, in the same direction -- which no amount of extra
    data ever averages out."""
    add_decision(db, "KXA-1")
    rec = make_recorder(db, no_credentials_venue({"KXA-1": ("finalized", None, True)}))
    rec.cycle()

    row = rows(db)["KXA-1"]
    assert row["voided"] == 1
    assert describe(None, True) == "VOID"


def test_a_void_status_without_a_void_result_is_still_treated_as_a_void(db):
    """`market_snapshots.status` can itself say 'voided' (backtest/engine.py
    treats it that way).  A void that arrives via the status field must not fall
    through to the not-final branch and be silently forgotten."""
    add_decision(db, "KXA-1")
    rec = make_recorder(db, no_credentials_venue({"KXA-1": ("voided", None, False)}))
    rec.cycle()
    assert rows(db)["KXA-1"]["voided"] == 1


def test_a_void_row_is_invisible_to_the_calibration_join(db):
    """End to end with the real consumer: `monitor.kpi.settled_decisions` must
    not score a decision on a voided market.  This is the test that proves the
    Brier score never sees a void."""
    add_decision(db, "KXVOID", sleeve="S1", p_model=0.90)
    add_decision(db, "KXREAL", sleeve="S1", p_model=0.90)
    rec = make_recorder(db, no_credentials_venue({
        "KXVOID": ("finalized", None, True),
        "KXREAL": ("finalized", 1, False),
    }))
    rec.cycle()

    scored = settled_decisions(db, "S1")
    assert [d.ticker for d in scored] == ["KXREAL"]


def test_a_void_row_is_distinguishable_from_a_no_row_only_by_the_voided_flag(db):
    """The hazard, asserted so it cannot be forgotten: `outcome` is NOT NULL, so
    a void has to store 0 there and is byte-identical to a NO apart from the
    flag.  Any consumer that reads `outcome` without checking `voided` first
    scores every void as a NO."""
    add_decision(db, "KXVOID")
    add_decision(db, "KXNO")
    rec = make_recorder(db, no_credentials_venue({
        "KXVOID": ("finalized", None, True),
        "KXNO": ("finalized", 0, False),
    }))
    rec.cycle()

    void_row, no_row = rows(db)["KXVOID"], rows(db)["KXNO"]
    assert void_row["outcome"] == no_row["outcome"] == 0
    assert (void_row["voided"], no_row["voided"]) == (1, 0)


# --------------------------------------------------------------------------- #
# The third outcome class the schema cannot hold
# --------------------------------------------------------------------------- #
def test_a_finalized_scalar_market_is_refused_rather_than_scored_as_a_no(db):
    """MEASURED on the live public API: of 21,000 settled markets, 199 (0.95%)
    reported `result='scalar'` -- a pro-rata payout ("resolves to the fair market
    price"), e.g. one that settled at $0.16.  That is neither YES, nor NO, nor
    void-at-cost, and `settlements` cannot represent it.

    `market_result()` reports it as (status, None, False), which is
    indistinguishable from an open market by outcome alone; only the final
    status separates the two.  Recording it as a NO would be a 1%-of-all-markets
    systematic error in both P&L and Brier."""
    add_decision(db, "KXSCALAR")
    rec = make_recorder(db, no_credentials_venue({"KXSCALAR": ("finalized", None, False)}))
    assert rec.cycle() == (1, 0)

    assert rows(db) == {}
    assert rec.stats.unresolvable == 1
    assert rec.unresolvable[0][0] == "KXSCALAR"


def test_an_unrecognised_portfolio_result_is_reported_not_assumed_to_be_no(db):
    add_decision(db, "KXA-1")
    rec = make_recorder(db, FakeVenue(
        settlements=[{"ticker": "KXA-1", "market_result": "scalar",
                      "settled_time": "2023-11-14T22:13:20Z"}],
    ))
    rec.cycle()
    assert rows(db) == {}
    assert rec.stats.unresolvable == 1


# --------------------------------------------------------------------------- #
# Idempotency -- these rows are what P&L is booked against
# --------------------------------------------------------------------------- #
def test_running_the_recorder_twice_never_duplicates_a_settlement(db):
    add_decision(db, "KXA-1")
    venue = no_credentials_venue({"KXA-1": ("finalized", 1, False)})
    rec = make_recorder(db, venue)
    rec.cycle()
    rec.cycle()

    assert db.counts()["settlements"] == 1
    assert rec.stats.written == 1


def test_a_recorded_outcome_is_never_flipped_by_a_later_contradicting_poll(db):
    """If the venue said YES yesterday and NO today, one of those is a bug --
    and P&L has already been booked against the first.  The row does not move;
    the disagreement is reported instead."""
    add_decision(db, "KXA-1")
    venue = no_credentials_venue({"KXA-1": ("finalized", 1, False)})
    rec = make_recorder(db, venue)
    rec.cycle()

    # force a re-poll of the settled ticker with the opposite answer
    venue.results["KXA-1"] = ("finalized", 0, False)
    assert rec.record(Settlement(VENUE, "KXA-1", T0, 0, False, "market")) == "conflict"

    assert rows(db)["KXA-1"]["outcome"] == 1
    assert rec.stats.conflicts == 1


def test_an_already_settled_ticker_is_never_polled_again(db):
    """The universe is 106,000+ markets and the venue is a token bucket.
    Re-polling what is already known is the difference between a recorder that
    keeps up and one that is permanently behind."""
    add_decision(db, "KXA-1")
    venue = no_credentials_venue({"KXA-1": ("finalized", 1, False)})
    rec = make_recorder(db, venue)
    rec.cycle()
    assert venue.polled == ["KXA-1"]

    rec.cycle()
    assert venue.polled == ["KXA-1"]        # not polled a second time


def test_a_contradicting_row_that_wins_the_race_is_still_reported(db):
    """Two writers, one table.  If another process inserts a CONTRADICTING row
    between our read and our write, `ON CONFLICT DO NOTHING` swallows the insert
    -- and without a re-read that passes as a harmless duplicate, which is
    exactly how a wrong outcome survives unnoticed.  The trigger below is a
    deterministic stand-in for losing that race."""
    rec = make_recorder(db, no_credentials_venue())
    db.conn.execute(
        """CREATE TEMP TRIGGER race BEFORE INSERT ON settlements
           BEGIN
             INSERT INTO settlements (venue, ticker, settled_at_us, outcome, voided)
             VALUES (NEW.venue, NEW.ticker, NEW.settled_at_us, 1, 0);
           END"""
    )
    try:
        verdict = rec.record(Settlement(VENUE, "KXRACE", T0, 0, False, "market"))
    finally:
        db.conn.execute("DROP TRIGGER race")

    assert verdict == "conflict"
    assert rows(db)["KXRACE"]["outcome"] == 1       # the winner's row is untouched
    assert rec.stats.conflicts == 1


def test_a_settlement_recorded_by_another_process_is_left_alone(db):
    """Two writers, one table.  The recorder must treat a pre-existing row as
    authoritative rather than racing it."""
    add_decision(db, "KXA-1")
    with db.tx() as c:
        c.execute(
            """INSERT INTO settlements (venue, ticker, settled_at_us, outcome, voided)
               VALUES (?,?,?,1,0)""",
            (VENUE, "KXA-1", T0),
        )
    rec = make_recorder(db, no_credentials_venue({"KXA-1": ("finalized", 1, False)}))
    rec.cycle()

    assert db.counts()["settlements"] == 1
    assert rows(db)["KXA-1"]["settled_at_us"] == T0


# --------------------------------------------------------------------------- #
# What gets polled
# --------------------------------------------------------------------------- #
def test_only_tickers_we_traded_or_decided_on_are_polled(db):
    """106,000+ markets exist.  Polling the ones we have no stake in is how the
    rate limiter turns into an outage."""
    add_decision(db, "KXMINE")
    add_snapshot(db, "KXTHEIRS", close_at_us=T0)      # in the universe, not ours
    rec = make_recorder(db, no_credentials_venue())
    assert rec.candidates() == ["KXMINE"]


def test_markets_we_hold_are_polled_before_markets_we_only_had_an_opinion_about(db):
    """A wrong answer costs money on a position and costs a data point on an
    un-acted decision, so exposure is polled first when the budget is tight."""
    add_decision(db, "KXOPINION", at_us=T0 + 99 * SEC)
    coid = add_order(db, "KXHELD", at_us=T0)
    add_fill(db, coid, at_us=T0)
    add_order(db, "KXQUOTED", at_us=T0 + 50 * SEC)

    rec = make_recorder(db, no_credentials_venue())
    assert rec.candidates() == ["KXHELD", "KXQUOTED", "KXOPINION"]


def test_the_poll_list_rotates_so_no_candidate_starves_behind_the_limit(db):
    """With more candidates than `--limit`, a fixed sort order means the tail is
    never polled and those markets never settle in our books -- at all, ever."""
    for i in range(5):
        add_decision(db, f"KX-{i}", at_us=T0 - i * SEC)
    rec = make_recorder(db, no_credentials_venue(), limit=2)

    first = rec.poll_list()
    second = rec.poll_list()
    third = rec.poll_list()
    assert len(first) == len(second) == len(third) == 2
    assert set(first) | set(second) | set(third) == {f"KX-{i}" for i in range(5)}


def test_an_un_acted_decision_still_gets_polled(db):
    """PLAN.md 6.3: scoring only the trades you took measures your execution
    filter, not your model."""
    add_decision(db, "KXNOTACTED", acted=0)
    rec = make_recorder(db, no_credentials_venue())
    assert rec.candidates() == ["KXNOTACTED"]


# --------------------------------------------------------------------------- #
# The two sources
# --------------------------------------------------------------------------- #
def test_shadow_mode_settles_from_market_data_with_no_credentials_at_all(db):
    """In shadow no order was ever sent, so the account settled nothing and the
    portfolio feed is empty BY CONSTRUCTION.  If market data could not settle a
    market, no shadow sleeve could ever produce a Brier score, and G3 would be
    unreachable."""
    add_decision(db, "KXA-1")
    venue = no_credentials_venue({"KXA-1": ("finalized", 1, False)})
    rec = make_recorder(db, venue)
    _, written = rec.cycle()

    assert written == 1
    assert rec.portfolio is False           # disabled itself, did not crash


def test_a_portfolio_settlement_is_recorded_even_when_market_data_lags(db):
    """The account was paid.  That the market-data view has not caught up does
    not make the money hypothetical."""
    add_decision(db, "KXA-1")
    rec = make_recorder(db, FakeVenue(
        {"KXA-1": ("active", None, False)},
        settlements=[{"ticker": "KXA-1", "market_result": "yes",
                      "settled_time": "2023-11-14T22:13:20Z"}],
    ))
    rec.cycle()
    assert rows(db)["KXA-1"]["outcome"] == 1


def test_agreeing_sources_keep_the_portfolios_real_settlement_timestamp(db):
    """Market data as exposed by `market_result()` carries no timestamp, so a
    market-only row is dated by inference.  When the portfolio feed also has the
    market, its `settled_time` is the real one and wins."""
    add_decision(db, "KXA-1")
    settled_iso = "2023-11-14T22:13:20Z"
    rec = make_recorder(db, FakeVenue(
        {"KXA-1": ("finalized", 1, False)},
        settlements=[{"ticker": "KXA-1", "market_result": "yes",
                      "settled_time": settled_iso}],
    ))
    rec.cycle()
    assert rows(db)["KXA-1"]["settled_at_us"] == T0     # 2023-11-14T22:13:20Z


def test_disagreeing_sources_write_nothing_and_report_loudly(db):
    """One of the two is a parsing bug.  A missing row announces itself -- the
    position stays open and unscored.  A wrong row does not, and poisons P&L,
    Brier, and every gate decision downstream of them."""
    add_decision(db, "KXA-1")
    rec = make_recorder(db, FakeVenue(
        {"KXA-1": ("finalized", 1, False)},
        settlements=[{"ticker": "KXA-1", "market_result": "no",
                      "settled_time": "2023-11-14T22:13:20Z"}],
    ))
    rec.cycle()

    assert rows(db) == {}
    assert rec.stats.conflicts == 1
    assert "CONFLICT" in str(rec.conflicts[0])
    assert "YES" in str(rec.conflicts[0]) and "NO" in str(rec.conflicts[0])


def test_a_void_against_a_no_is_a_conflict_not_a_coin_flip(db):
    """The most dangerous disagreement of all: void and NO look identical in the
    stored row, so preferring either one silently is unrecoverable."""
    add_decision(db, "KXA-1")
    rec = make_recorder(db, FakeVenue(
        {"KXA-1": ("finalized", None, True)},
        settlements=[{"ticker": "KXA-1", "market_result": "no",
                      "settled_time": "2023-11-14T22:13:20Z"}],
    ))
    rec.cycle()

    assert rows(db) == {}
    assert rec.stats.conflicts == 1
    assert "VOID" in str(rec.conflicts[0])


def test_a_portfolio_row_with_no_ticker_is_ignored(db):
    rec = make_recorder(db, FakeVenue(settlements=[{"market_result": "yes"}]))
    assert rec.portfolio_settlements() == {}


# --------------------------------------------------------------------------- #
# Dating the settlement -- anti-look-ahead (I6)
# --------------------------------------------------------------------------- #
def test_a_settlement_is_dated_to_the_market_close_not_to_the_poll(db):
    """`settled_at_us` guards the calibration join (`decided_at_us <=
    settled_at_us`).  Dating a settlement at the moment we happened to poll --
    which can be hours late if this process was down -- would let a decision made
    AFTER the market resolved be scored, manufacturing skill out of nothing."""
    close = now_us() - 3600 * SEC
    add_decision(db, "KXA-1")
    add_snapshot(db, "KXA-1", close_at_us=close, observed_at_us=close)
    rec = make_recorder(db, no_credentials_venue({"KXA-1": ("finalized", 1, False)}))
    rec.cycle()

    assert rows(db)["KXA-1"]["settled_at_us"] == close


def test_a_settlement_is_never_dated_in_the_future(db):
    """A close time still in the future (a stale snapshot of a market that
    closed early) must not produce a settlement timestamp that has not happened
    yet, or every decision on it scores against a future it cannot have seen."""
    add_decision(db, "KXA-1")
    add_snapshot(db, "KXA-1", close_at_us=now_us() + 30 * 24 * 3600 * SEC)
    rec = make_recorder(db, no_credentials_venue({"KXA-1": ("finalized", 1, False)}))
    rec.cycle()

    assert rows(db)["KXA-1"]["settled_at_us"] <= now_us()


def test_a_decision_emitted_after_the_market_closed_is_not_scored(db):
    """Why the dating rule matters, in the consumer that depends on it: a sleeve
    restarted against a stale universe can emit a 'forecast' for a market that
    already resolved."""
    close = now_us() - 3600 * SEC
    add_snapshot(db, "KXLATE", close_at_us=close, observed_at_us=close)
    add_decision(db, "KXLATE", sleeve="LATE", at_us=close + 60 * SEC)
    rec = make_recorder(db, no_credentials_venue({"KXLATE": ("finalized", 1, False)}))
    rec.cycle()

    assert db.counts()["settlements"] == 1
    assert settled_decisions(db, "LATE") == []


# --------------------------------------------------------------------------- #
# Robustness -- an unsettled book is invisible P&L
# --------------------------------------------------------------------------- #
def test_one_dead_ticker_does_not_stop_the_rest_of_the_cycle(db):
    """A delisted ticker 404s.  If that aborted the sweep, one dead market would
    hold the entire book's realised P&L hostage."""
    add_decision(db, "KXDEAD")
    add_decision(db, "KXALIVE")
    venue = no_credentials_venue(
        {"KXALIVE": ("finalized", 1, False)},
        errors={"KXDEAD": KalshiError(404, "not found", "/markets/KXDEAD")},
    )
    rec = make_recorder(db, venue)
    rec.cycle()

    assert set(rows(db)) == {"KXALIVE"}
    assert rec.stats.not_found == 1
    assert rec.stats.errors == 0


def test_an_unexpected_error_on_one_ticker_is_counted_and_survived(db):
    add_decision(db, "KXBOOM")
    add_decision(db, "KXALIVE")
    venue = no_credentials_venue(
        {"KXALIVE": ("finalized", 0, False)},
        errors={"KXBOOM": RuntimeError("kaboom")},
    )
    rec = make_recorder(db, venue)
    rec.cycle()

    assert set(rows(db)) == {"KXALIVE"}
    assert rec.stats.errors == 1


def test_an_empty_database_polls_nothing_and_writes_nothing(db):
    venue = no_credentials_venue()
    rec = make_recorder(db, venue)
    assert rec.cycle() == (0, 0)
    assert venue.polled == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_demo_credentials_are_never_used_against_a_prod_universe(workdir):
    """A demo key against a prod universe would poll DEMO tickers for markets
    recorded from PROD and cross-check prod settlements against a play-money
    account.  Garbage that looks like data is worse than no data."""
    key = workdir / "fake.pem"
    key.write_text("not a real key")
    demo = KalshiCredentials(env="demo", key_id="k", private_key_path=key)

    assert credentials_match(demo, DEMO_BASE)
    assert not credentials_match(demo, PROD_BASE)


def test_incomplete_credentials_never_enable_the_portfolio_cross_check():
    assert not credentials_match(KalshiCredentials(env="prod"), PROD_BASE)


def test_the_cli_exposes_the_same_flags_as_the_other_recorders():
    """`--db`, `--once`, `--interval`, `--limit`.  A recorder nobody can start
    the same way as the others is a recorder nobody starts."""
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "recorder.settlements", "--help"],
        cwd=root, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    for flag in ("--db", "--once", "--interval", "--limit"):
        assert flag in proc.stdout


# --------------------------------------------------------------------------- #
# Live -- skipped by `-m "not live"`
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_a_settled_market_really_does_report_finalized_with_a_binary_result():
    """The measured fact this whole module keys off, re-verified against the
    live public API.  If Kalshi ever changes the word, settlement ingestion
    silently stops and no test but this one notices."""
    with KalshiClient(base_url=PROD_BASE) as client:
        data = client._request(
            "GET", "/markets",
            params={"status": "settled", "limit": 100, "mve_filter": "exclude"},
        )
        markets = data.get("markets") or []
        assert markets, "expected some settled markets"
        assert {str(m.get("status") or "").lower() for m in markets} == {"finalized"}

        binary = [m for m in markets
                  if str(m.get("result") or "").lower() in ("yes", "no")]
        assert binary, "expected at least one yes/no result"

        status, outcome, voided = client.market_result(binary[0]["ticker"])
        assert status == "finalized"
        assert outcome in (0, 1)
        assert voided is False
