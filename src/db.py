"""
Database initialization and connection helper.
Schema is created lazily on first connection.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from src.config import DB_PATH

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS insider_transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_no    TEXT UNIQUE,
    filed_date      TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    cik             TEXT NOT NULL,
    insider_name    TEXT NOT NULL,
    insider_title   TEXT NOT NULL,
    tx_code         TEXT NOT NULL,          -- P = purchase, S = sale, etc.
    shares          REAL NOT NULL,
    price           REAL NOT NULL,
    value           REAL NOT NULL,
    shares_owned_after REAL,
    is_10b5_1       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_it_ticker   ON insider_transactions(ticker);
CREATE INDEX IF NOT EXISTS idx_it_cik      ON insider_transactions(cik);
CREATE INDEX IF NOT EXISTS idx_it_date     ON insider_transactions(trade_date);
CREATE INDEX IF NOT EXISTS idx_it_code     ON insider_transactions(tx_code);

CREATE TABLE IF NOT EXISTS insider_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  INTEGER NOT NULL REFERENCES insider_transactions(id),
    hit_rate        REAL,
    opportunistic   REAL,
    role_weight     REAL,
    size_zscore     REAL,
    cluster_bonus   REAL NOT NULL DEFAULT 0,
    total_score     REAL NOT NULL,
    computed_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS portfolio_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    cash            REAL NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL UNIQUE,
    shares          REAL NOT NULL,
    avg_cost        REAL NOT NULL,
    opened_at       TEXT NOT NULL,
    triggering_insider TEXT,
    triggering_tx_id   INTEGER REFERENCES insider_transactions(id)
);

CREATE TABLE IF NOT EXISTS virtual_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL DEFAULT (datetime('now')),
    ticker          TEXT NOT NULL,
    action          TEXT NOT NULL,          -- BUY / SELL
    price           REAL NOT NULL,
    shares          REAL NOT NULL,
    total_value     REAL NOT NULL,
    reason          TEXT,
    triggering_insider TEXT,
    triggering_tx_id   INTEGER
);

CREATE TABLE IF NOT EXISTS portfolio_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL UNIQUE,
    total_value     REAL NOT NULL,
    cash            REAL NOT NULL,
    positions_value REAL NOT NULL,
    benchmark_value REAL,                   -- SPY normalized value
    num_positions   INTEGER NOT NULL DEFAULT 0
);
"""


def get_connection() -> sqlite3.Connection:
    """Return a connection with WAL mode and foreign keys enabled."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables if they don't exist and seed initial portfolio state."""
    conn = get_connection()
    try:
        conn.executescript(_SCHEMA)
        # Seed starting capital if missing
        row = conn.execute("SELECT cash FROM portfolio_state WHERE id = 1").fetchone()
        if row is None:
            from src.config import STARTING_CAPITAL
            conn.execute(
                "INSERT INTO portfolio_state (id, cash) VALUES (1, ?)",
                (STARTING_CAPITAL,),
            )
            conn.commit()
            logger.info("Initialized portfolio with $%.2f", STARTING_CAPITAL)
        conn.commit()
    finally:
        conn.close()
    logger.info("Database ready at %s", DB_PATH)
