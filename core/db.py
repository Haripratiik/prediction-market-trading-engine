"""SQLite persistence.  PLAN.md section 5 data model.  T-006.

Two invariants the schema itself enforces:

  I4  Position/PnL state derives from persisted FILLS, never from a counter.
  R5a market_snapshots and event_snapshots are APPEND-ONLY. Backtests read the
      latest row with observed_at_us <= t; overwriting is how look-ahead leaks in.

The append-only rule is enforced by SQLite triggers, not by convention -- an
UPDATE on either snapshot table raises.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.models import Event, Market, Series, SettlementSource, now_us

SCHEMA_VERSION = 3

DDL = """
-- Point-in-time market metadata.  NEVER overwrite: append a new row per
-- observation.  (anti-look-ahead, PLAN.md R5a)
CREATE TABLE IF NOT EXISTS market_snapshots (
  id              INTEGER PRIMARY KEY,
  observed_at_us  INTEGER NOT NULL,
  venue           TEXT    NOT NULL,
  ticker          TEXT    NOT NULL,
  event_ticker    TEXT,
  series_ticker   TEXT,
  title           TEXT,
  status          TEXT,
  yes_bid         INTEGER,
  yes_ask         INTEGER,
  yes_bid_size    REAL,
  yes_ask_size    REAL,
  volume          REAL,
  volume_24h      REAL,
  open_interest   REAL,
  close_at_us     INTEGER,
  rules_hash      TEXT,
  UNIQUE(venue, ticker, observed_at_us)
);
CREATE INDEX IF NOT EXISTS ix_ms_ticker ON market_snapshots(venue, ticker, observed_at_us DESC);
CREATE INDEX IF NOT EXISTS ix_ms_event  ON market_snapshots(event_ticker, observed_at_us DESC);

-- Event-level metadata.  Carries the fields S2/S3 depend on.
CREATE TABLE IF NOT EXISTS event_snapshots (
  id                     INTEGER PRIMARY KEY,
  observed_at_us         INTEGER NOT NULL,
  venue                  TEXT    NOT NULL,
  event_ticker           TEXT    NOT NULL,
  series_ticker          TEXT,
  category               TEXT,
  title                  TEXT,
  mutually_exclusive     INTEGER NOT NULL DEFAULT 0,  -- exchange flag: at most one YES
  exhaustive_verified    INTEGER NOT NULL DEFAULT 0,  -- OUR verdict; NOT the same thing
  collateral_return_type TEXT,
  settlement_sources_json TEXT,
  UNIQUE(venue, event_ticker, observed_at_us)
);
CREATE INDEX IF NOT EXISTS ix_es_event ON event_snapshots(venue, event_ticker, observed_at_us DESC);

-- The fee / prohibition / settlement-source map.  Refreshed per session.
CREATE TABLE IF NOT EXISTS series_cache (
  ticker                  TEXT PRIMARY KEY,
  observed_at_us          INTEGER NOT NULL,
  title                   TEXT,
  category                TEXT,
  fee_type                TEXT NOT NULL,
  fee_multiplier          REAL NOT NULL,
  contract_terms_url      TEXT,
  settlement_sources_json TEXT,
  prohibitions_json       TEXT
);

CREATE TABLE IF NOT EXISTS rules_docs (
  rules_hash      TEXT PRIMARY KEY,
  venue           TEXT NOT NULL,
  ticker          TEXT NOT NULL,
  fetched_at_us   INTEGER NOT NULL,
  raw_text        TEXT NOT NULL,
  extraction_json TEXT,
  human_verdict   TEXT,          -- VERIFIED | REJECTED | NEEDS_HUMAN | NULL
  verdict_at_us   INTEGER,
  verdict_note    TEXT
);

CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY,     -- UUIDv4, generated BEFORE send (idempotency)
  created_at_us   INTEGER NOT NULL,
  sleeve_id       TEXT NOT NULL,
  structure_id    TEXT,
  -- Without this there is NO join from a fill back to the probability that
  -- caused it, so per-decision fee and slippage attribution is impossible and
  -- decisions can only be matched to outcomes on (venue, ticker) -- which is
  -- wrong the moment one ticker is quoted twice in a session.
  decision_id     INTEGER REFERENCES decisions(id),
  venue           TEXT NOT NULL,
  ticker          TEXT NOT NULL,
  side            TEXT NOT NULL,
  price_cents     INTEGER NOT NULL,
  size            INTEGER NOT NULL,
  post_only       INTEGER NOT NULL,
  mode            TEXT NOT NULL,        -- backtest|shadow|paper|live
  venue_order_id  TEXT,
  state           TEXT NOT NULL,
  rationale_json  TEXT NOT NULL,        -- C4.2c: mandatory
  updated_at_us   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_sleeve ON orders(sleeve_id, created_at_us DESC);
CREATE INDEX IF NOT EXISTS ix_orders_decision ON orders(decision_id);

CREATE TABLE IF NOT EXISTS fills (
  id              INTEGER PRIMARY KEY,
  filled_at_us    INTEGER NOT NULL,
  client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
  venue_fill_id   TEXT UNIQUE,          -- dedupe key from the venue
  price_cents     INTEGER NOT NULL,
  size            INTEGER NOT NULL,
  fee_cents       INTEGER NOT NULL,     -- signed: negative = rebate received
  is_maker        INTEGER NOT NULL,
  terminal        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_fills_order ON fills(client_order_id);

-- Public trade tape.  `taker_side` is LABELLED by the exchange, so trade-sign
-- inference (Lee-Ready, BVC) is never needed -- a real advantage over Polymarket,
-- where feed-inferred direction agrees with on-chain truth only ~59% of the time.
CREATE TABLE IF NOT EXISTS trades (
  trade_id        TEXT PRIMARY KEY,
  ticker          TEXT NOT NULL,
  traded_at_us    INTEGER,
  yes_price_cents INTEGER,
  size            REAL,
  taker_side      TEXT,
  is_block        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_trades_ticker ON trades(ticker, traded_at_us);

CREATE TABLE IF NOT EXISTS settlements (
  id            INTEGER PRIMARY KEY,
  venue         TEXT NOT NULL,
  ticker        TEXT NOT NULL,
  settled_at_us INTEGER NOT NULL,
  outcome       INTEGER NOT NULL,       -- 1 = YES, 0 = NO
  voided        INTEGER NOT NULL DEFAULT 0,
  UNIQUE(venue, ticker)
);

-- One row per model probability emitted, ACTED ON OR NOT.  Un-acted decisions
-- are what make calibration measurable without survivorship bias (PLAN.md 6.3).
CREATE TABLE IF NOT EXISTS decisions (
  id                 INTEGER PRIMARY KEY,
  decided_at_us      INTEGER NOT NULL,
  sleeve_id          TEXT NOT NULL,
  venue              TEXT,
  ticker             TEXT,
  -- R2.3a fits beta_c PER CATEGORY with empirical-Bayes pooling, and removes any
  -- category whose posterior beta is not credibly above 0.  Without this column
  -- the shrinkage coefficient can only be fitted sleeve-wide and that rule is
  -- not implementable at all.  `Decision.category` always carried it; the table
  -- silently dropped it on the way in.
  category           TEXT NOT NULL DEFAULT '',
  market_price       REAL NOT NULL,     -- the benchmark forecast
  p_model            REAL NOT NULL,
  raw_edge           REAL NOT NULL,
  shrunk_edge        REAL NOT NULL,
  acted              INTEGER NOT NULL,
  preregistration_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_decisions_sleeve ON decisions(sleeve_id, decided_at_us DESC);
CREATE INDEX IF NOT EXISTS ix_decisions_cat ON decisions(category, decided_at_us DESC);

-- PLAN.md section 5.  A multi-leg structure is ONE risk object: its legs must be
-- born, marked and unwound together.  Without this table `orphan_loss_ratio`
-- (KPI 6) cannot be computed, which leaves the single most expensive failure
-- mode of an RV book -- one leg filled, the other not -- entirely unmeasured.
CREATE TABLE IF NOT EXISTS structures (
  structure_id        TEXT PRIMARY KEY,
  created_at_us       INTEGER NOT NULL,
  sleeve_id           TEXT NOT NULL,
  kind                TEXT NOT NULL,   -- dutch_book|short_basket|linked_rv|hedge
  event_ticker        TEXT,
  legs_json           TEXT NOT NULL,   -- [{ticker, side, target_size, price_cents}]
  n_legs              INTEGER NOT NULL,
  state               TEXT NOT NULL,   -- forming|complete|orphaned|unwinding|closed
  target_margin_cents   REAL,   -- edge the structure was opened FOR
  realized_margin_cents REAL,   -- what it actually returned
  unwind_deadline_us  INTEGER,
  closed_at_us        INTEGER,
  rationale_json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_structures_state ON structures(state, created_at_us DESC);

-- Mark-outs, persisted rather than recomputed.  KPI 3 measures how edge decays
-- from 1s to 30m; recomputing it later from snapshots gives a DIFFERENT answer,
-- because the snapshot history is far too sparse to resolve those horizons
-- (see the staleness bound in shadow/engine.py::markout).
CREATE TABLE IF NOT EXISTS marks (
  id              INTEGER PRIMARY KEY,
  client_order_id TEXT NOT NULL,
  ticker          TEXT NOT NULL,
  venue           TEXT NOT NULL DEFAULT 'kalshi',
  filled_at_us    INTEGER NOT NULL,
  horizon_us      INTEGER NOT NULL,
  markout_cents   REAL NOT NULL,
  ref_mid         REAL NOT NULL,
  stale_us        INTEGER NOT NULL,    -- how old the reference quote actually was
  UNIQUE(client_order_id, horizon_us)
);
CREATE INDEX IF NOT EXISTS ix_marks_order ON marks(client_order_id);

-- The S3 link graph: two markets that must satisfy a logical relation.
-- `verified_by` is the promotion gate -- an 'auto' link may be watched but never
-- traded, because a mis-detected implication is an unhedged directional bet
-- wearing an arbitrage's clothes.
CREATE TABLE IF NOT EXISTS links (
  id            INTEGER PRIMARY KEY,
  created_at_us INTEGER NOT NULL,
  kind          TEXT NOT NULL,         -- implies|equivalent|disjoint|nested
  a_ticker      TEXT NOT NULL,
  b_ticker      TEXT NOT NULL,
  a_rules_hash  TEXT NOT NULL DEFAULT '',
  b_rules_hash  TEXT NOT NULL DEFAULT '',
  confidence    REAL NOT NULL,
  verified_by   TEXT NOT NULL DEFAULT 'auto',    -- auto|human
  evidence_json TEXT NOT NULL,
  UNIQUE(kind, a_ticker, b_ticker)
);
CREATE INDEX IF NOT EXISTS ix_links_a ON links(a_ticker);
CREATE INDEX IF NOT EXISTS ix_links_b ON links(b_ticker);

-- Historical 1-minute OHLC, BACKFILLED from the public candlesticks endpoint.
--
-- This is the table that unblocked the project.  Everything else here is a
-- point-in-time recording: to get 1,600 settled markets we had to WAIT for 1,600
-- markets to settle, which at ~100/day is sixteen days before any edge smaller
-- than 5pp could even be tested.  `/series/{s}/markets/{t}/candlesticks` is
-- public, unauthenticated, works on ALREADY-SETTLED markets, and returns both
-- sides of the book per minute -- so the same 1,600 settlements can be fetched
-- in an afternoon instead of recorded over a fortnight.
--
-- It does NOT replace `market_snapshots`.  A candle is an aggregate over a
-- minute (open/high/low/close) with no queue sizes, so it cannot support a
-- counterfactual fill; snapshots stay the source for anything execution-shaped.
-- Candles are for CALIBRATION and BACKTESTING, where the question is "what did
-- the market think at time t, and what happened".
CREATE TABLE IF NOT EXISTS candles (
  ticker          TEXT NOT NULL,
  series_ticker   TEXT NOT NULL DEFAULT '',
  end_period_ts   INTEGER NOT NULL,     -- unix SECONDS, candle close
  period_minutes  INTEGER NOT NULL,
  yes_bid_open    INTEGER, yes_bid_high  INTEGER,
  yes_bid_low     INTEGER, yes_bid_close INTEGER,
  yes_ask_open    INTEGER, yes_ask_high  INTEGER,
  yes_ask_low     INTEGER, yes_ask_close INTEGER,
  price_close     INTEGER,              -- last trade in the period
  volume          REAL NOT NULL DEFAULT 0,
  open_interest   REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (ticker, end_period_ts, period_minutes)
);
CREATE INDEX IF NOT EXISTS ix_candles_ticker ON candles(ticker, end_period_ts);
CREATE INDEX IF NOT EXISTS ix_candles_series ON candles(series_ticker, end_period_ts);

CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- R5a enforced by the database, not by convention.
CREATE TRIGGER IF NOT EXISTS no_update_market_snapshots
BEFORE UPDATE ON market_snapshots
BEGIN
  SELECT RAISE(ABORT, 'market_snapshots is append-only (PLAN.md R5a)');
END;

CREATE TRIGGER IF NOT EXISTS no_update_event_snapshots
BEFORE UPDATE ON event_snapshots
BEGIN
  SELECT RAISE(ABORT, 'event_snapshots is append-only (PLAN.md R5a)');
END;
"""


class Database:
    """Thin typed wrapper over SQLite.  The DB is the truth (PLAN.md I4)."""

    def __init__(self, path: str | Path = "data/pm.db") -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # check_same_thread=False hands this connection to several threads, and WAL
        # lets a writer run alongside readers -- but only one writer. Without a busy
        # timeout the second one does not wait, it raises "database is locked"
        # immediately. Five seconds is longer than any statement here takes.
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    # Columns added after v1.  `CREATE TABLE IF NOT EXISTS` is a no-op against a
    # table that already exists, so a change to an EXISTING table needs an
    # explicit ALTER or it silently never lands on the database already recorded.
    # Additive only -- no column is ever dropped or retyped, so a v1 reader keeps
    # working against a v2 file.
    _ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("decisions", "category", "TEXT NOT NULL DEFAULT ''"),
        ("orders", "decision_id", "INTEGER"),
    )

    # Columns RENAMED after their first release.  `CREATE TABLE IF NOT EXISTS`
    # cannot see a rename any more than it can see an addition, so a table that
    # already exists keeps its old column names forever and every query written
    # against the new ones fails at runtime -- which is exactly what happened to
    # `structures` between schema v2 and its first use.
    _RENAMED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("structures", "target_edge_cents", "target_margin_cents"),
        ("structures", "realised_edge_cents", "realized_margin_cents"),
    )

    def migrate(self) -> None:
        # ALTER first, THEN the DDL script.  The other order fails on an existing
        # v1 database: DDL creates an index over `orders(decision_id)`, and the
        # column does not exist yet at that point.  On a fresh database the
        # ALTERs are all no-ops, because the tables are not there to widen.
        for table, old_name, new_name in self._RENAMED_COLUMNS:
            self._rename_column(table, old_name, new_name)
        for table, column, decl in self._ADDED_COLUMNS:
            self._add_column(table, column, decl)
        self.conn.executescript(DDL)
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def _add_column(self, table: str, column: str, decl: str) -> bool:
        """Idempotent ALTER.  True if the column was actually added.

        The REFERENCES clause is deliberately omitted here: SQLite cannot add a
        column carrying a foreign key to an existing table, and the constraint is
        already declared in DDL for freshly-created databases.
        """
        cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        if not cols or column in cols:
            return False
        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        return True

    def _rename_column(self, table: str, old: str, new: str) -> bool:
        """Idempotent RENAME COLUMN.  True if it actually fired.

        Renaming preserves the rows, which `DROP`/recreate would not -- and the
        one table this currently applies to holds the audit trail for every
        multi-leg structure ever opened.
        """
        cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        if not cols or old not in cols or new in cols:
            return False
        self.conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
        return True

    def schema_version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        return int(row["value"]) if row else 0

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        # Fold the WAL back into the database before letting go. Autocheckpoint
        # only fires when a writer commits and no reader is holding an older
        # snapshot, so a long-lived reader can starve it indefinitely and the
        # -wal file grows without bound -- it has reached multiples of the
        # database size here. TRUNCATE resets it to zero on the way out.
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            # A checkpoint is housekeeping. Never let it stop a close.
            pass
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----------------------------------------------------------- series cache
    def upsert_series(self, series: Iterable[Series], *, observed_at_us: int | None = None) -> int:
        ts = observed_at_us or now_us()
        rows = [
            (
                s.ticker, ts, s.title, s.category, s.fee_type, s.fee_multiplier,
                s.contract_terms_url,
                json.dumps([sr.model_dump() for sr in s.settlement_sources]),
                json.dumps(list(s.additional_prohibitions)),
            )
            for s in series
        ]
        with self.tx() as c:
            c.executemany(
                """INSERT INTO series_cache
                   (ticker, observed_at_us, title, category, fee_type, fee_multiplier,
                    contract_terms_url, settlement_sources_json, prohibitions_json)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     observed_at_us=excluded.observed_at_us,
                     fee_type=excluded.fee_type,
                     fee_multiplier=excluded.fee_multiplier,
                     settlement_sources_json=excluded.settlement_sources_json,
                     prohibitions_json=excluded.prohibitions_json""",
                rows,
            )
        return len(rows)

    def get_series(self, ticker: str) -> Series | None:
        row = self.conn.execute(
            "SELECT * FROM series_cache WHERE ticker = ?", (ticker,)
        ).fetchone()
        if row is None:
            return None
        return Series(
            ticker=row["ticker"],
            title=row["title"] or "",
            category=row["category"] or "",
            fee_type=row["fee_type"],
            fee_multiplier=row["fee_multiplier"],
            contract_terms_url=row["contract_terms_url"],
            settlement_sources=tuple(
                SettlementSource(**s)
                for s in json.loads(row["settlement_sources_json"] or "[]")
            ),
            additional_prohibitions=tuple(json.loads(row["prohibitions_json"] or "[]")),
        )

    # -------------------------------------------------------------- snapshots
    def append_markets(self, markets: Iterable[Market],
                       *, observed_at_us: int | None = None) -> int:
        ts = observed_at_us or now_us()
        rows = [
            (
                ts, m.venue.value, m.ticker, m.event_ticker, m.series_ticker, m.title,
                m.status, m.yes_bid, m.yes_ask, m.yes_bid_size, m.yes_ask_size,
                m.volume, m.volume_24h, m.open_interest, m.close_at_us, m.rules_hash,
            )
            for m in markets
        ]
        with self.tx() as c:
            c.executemany(
                """INSERT OR IGNORE INTO market_snapshots
                   (observed_at_us, venue, ticker, event_ticker, series_ticker, title,
                    status, yes_bid, yes_ask, yes_bid_size, yes_ask_size,
                    volume, volume_24h, open_interest, close_at_us, rules_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return len(rows)

    def append_events(self, events: Iterable[Event],
                      *, observed_at_us: int | None = None) -> int:
        ts = observed_at_us or now_us()
        rows = [
            (
                ts, e.venue.value, e.event_ticker, e.series_ticker, e.category, e.title,
                int(e.mutually_exclusive), int(e.exhaustive_verified),
                e.collateral_return_type,
                json.dumps([s.model_dump() for s in e.settlement_sources]),
            )
            for e in events
        ]
        with self.tx() as c:
            c.executemany(
                """INSERT OR IGNORE INTO event_snapshots
                   (observed_at_us, venue, event_ticker, series_ticker, category, title,
                    mutually_exclusive, exhaustive_verified, collateral_return_type,
                    settlement_sources_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return len(rows)

    def latest_market(self, ticker: str, *, as_of_us: int | None = None,
                      venue: str = "kalshi") -> sqlite3.Row | None:
        """Point-in-time read.  THE anti-look-ahead accessor -- always use this."""
        ts = as_of_us if as_of_us is not None else now_us()
        return self.conn.execute(
            """SELECT * FROM market_snapshots
               WHERE venue = ? AND ticker = ? AND observed_at_us <= ?
               ORDER BY observed_at_us DESC LIMIT 1""",
            (venue, ticker, ts),
        ).fetchone()

    # ------------------------------------------------------------- rules docs
    def store_rules(self, venue: str, ticker: str, rules_hash: str, text: str) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT OR IGNORE INTO rules_docs
                   (rules_hash, venue, ticker, fetched_at_us, raw_text)
                   VALUES (?,?,?,?,?)""",
                (rules_hash, venue, ticker, now_us(), text),
            )

    # ------------------------------------------------------------------ stats
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in ("market_snapshots", "event_snapshots", "series_cache",
                      "rules_docs", "trades", "orders", "fills", "decisions",
                      "settlements"):
            out[table] = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        return out

    def universe_stats(self) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT COUNT(DISTINCT ticker) AS markets,
                      COUNT(DISTINCT event_ticker) AS events,
                      MIN(observed_at_us) AS first_us,
                      MAX(observed_at_us) AS last_us
               FROM market_snapshots"""
        ).fetchone()
        return dict(row) if row else {}
