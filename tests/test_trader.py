"""
Unit tests for trader.py
"""

import sqlite3
from unittest.mock import patch

import pytest

from src.config import (
    MAX_HOLD_DAYS,
    MAX_POSITION_PCT,
    SCORE_THRESHOLD,
    STARTING_CAPITAL,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
)


@pytest.fixture
def test_db(tmp_path):
    """Create a test database with full schema."""
    db_path = tmp_path / "test_trader.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE insider_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            accession_no TEXT UNIQUE,
            filed_date TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            cik TEXT NOT NULL,
            insider_name TEXT NOT NULL,
            insider_title TEXT NOT NULL,
            tx_code TEXT NOT NULL,
            shares REAL NOT NULL,
            price REAL NOT NULL,
            value REAL NOT NULL,
            shares_owned_after REAL,
            is_10b5_1 INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE insider_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            hit_rate REAL,
            opportunistic REAL,
            role_weight REAL,
            size_zscore REAL,
            cluster_bonus REAL NOT NULL DEFAULT 0,
            total_score REAL NOT NULL,
            computed_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE portfolio_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash REAL NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL UNIQUE,
            shares REAL NOT NULL,
            avg_cost REAL NOT NULL,
            opened_at TEXT NOT NULL,
            triggering_insider TEXT,
            triggering_tx_id INTEGER
        );
        CREATE TABLE virtual_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            ticker TEXT NOT NULL,
            action TEXT NOT NULL,
            price REAL NOT NULL,
            shares REAL NOT NULL,
            total_value REAL NOT NULL,
            reason TEXT,
            triggering_insider TEXT,
            triggering_tx_id INTEGER
        );
    """)
    conn.execute("INSERT INTO portfolio_state (id, cash) VALUES (1, ?)", (STARTING_CAPITAL,))
    conn.commit()
    return conn


def _add_scored_transaction(conn, ticker="AAPL", score=80.0, insider="Test Insider"):
    """Insert a transaction and its score."""
    cursor = conn.execute(
        """
        INSERT INTO insider_transactions
        (accession_no, filed_date, trade_date, ticker, cik, insider_name,
         insider_title, tx_code, shares, price, value, is_10b5_1)
        VALUES (?, '2024-01-15', '2024-01-15', ?, '12345', ?, 'CEO', 'P',
                1000, 150.0, 150000.0, 0)
        """,
        (f"test-{ticker}-{score}", ticker, insider),
    )
    tx_id = cursor.lastrowid
    conn.execute(
        """
        INSERT INTO insider_scores (transaction_id, total_score)
        VALUES (?, ?)
        """,
        (tx_id, score),
    )
    conn.commit()
    return tx_id


def _add_position(conn, ticker="AAPL", shares=100, avg_cost=150.0, days_ago=10):
    """Insert an existing position."""
    from datetime import datetime, timedelta
    opened = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO positions (ticker, shares, avg_cost, opened_at, triggering_insider)
        VALUES (?, ?, ?, ?, 'Test')
        """,
        (ticker, shares, avg_cost, opened),
    )
    conn.commit()


class TestBuySignals:
    @patch("src.trader._get_current_price", return_value=150.0)
    @patch("src.trader.get_connection")
    def test_buy_on_high_score(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        _add_scored_transaction(test_db, ticker="AAPL", score=85.0)

        from src.trader import _evaluate_new_signals
        buys = _evaluate_new_signals(test_db)
        assert buys == 1

        # Check position was created
        pos = test_db.execute("SELECT * FROM positions WHERE ticker = 'AAPL'").fetchone()
        assert pos is not None
        assert float(pos["avg_cost"]) == 150.0

        # Check cash was deducted
        cash = test_db.execute("SELECT cash FROM portfolio_state WHERE id = 1").fetchone()
        assert float(cash["cash"]) < STARTING_CAPITAL

    @patch("src.trader._get_current_price", return_value=150.0)
    @patch("src.trader.get_connection")
    def test_no_buy_below_threshold(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        _add_scored_transaction(test_db, ticker="AAPL", score=50.0)

        from src.trader import _evaluate_new_signals
        buys = _evaluate_new_signals(test_db)
        assert buys == 0

    @patch("src.trader._get_current_price", return_value=150.0)
    @patch("src.trader.get_connection")
    def test_no_duplicate_position(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        _add_scored_transaction(test_db, ticker="AAPL", score=85.0)
        _add_position(test_db, ticker="AAPL")

        from src.trader import _evaluate_new_signals
        buys = _evaluate_new_signals(test_db)
        assert buys == 0  # Already holding AAPL

    @patch("src.trader._get_current_price", return_value=150.0)
    @patch("src.trader.get_connection")
    def test_position_sizing(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        _add_scored_transaction(test_db, ticker="AAPL", score=85.0)

        from src.trader import _evaluate_new_signals
        _evaluate_new_signals(test_db)

        pos = test_db.execute("SELECT * FROM positions WHERE ticker = 'AAPL'").fetchone()
        assert pos is not None
        total_cost = float(pos["shares"]) * float(pos["avg_cost"])
        # Should not exceed MAX_POSITION_PCT of portfolio
        assert total_cost <= STARTING_CAPITAL * MAX_POSITION_PCT + 1  # +1 for rounding

    @patch("src.trader._get_current_price", return_value=None)
    @patch("src.trader.get_connection")
    def test_no_buy_when_price_unavailable(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        _add_scored_transaction(test_db, ticker="AAPL", score=85.0)

        from src.trader import _evaluate_new_signals
        buys = _evaluate_new_signals(test_db)
        assert buys == 0


class TestExitConditions:
    @patch("src.trader._get_current_price")
    @patch("src.trader.get_connection")
    def test_take_profit(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        buy_price = 100.0
        _add_position(test_db, ticker="AAPL", shares=100, avg_cost=buy_price)
        # Price up 30% → triggers take profit at +25%
        mock_price.return_value = buy_price * (1 + TAKE_PROFIT_PCT + 0.05)

        from src.trader import _check_exit_conditions
        sells = _check_exit_conditions(test_db)
        assert sells == 1

        # Position should be gone
        pos = test_db.execute("SELECT * FROM positions WHERE ticker = 'AAPL'").fetchone()
        assert pos is None

    @patch("src.trader._get_current_price")
    @patch("src.trader.get_connection")
    def test_stop_loss(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        buy_price = 100.0
        _add_position(test_db, ticker="AAPL", shares=100, avg_cost=buy_price)
        # Price down 15% → triggers stop loss at -12%
        mock_price.return_value = buy_price * (1 + STOP_LOSS_PCT - 0.03)

        from src.trader import _check_exit_conditions
        sells = _check_exit_conditions(test_db)
        assert sells == 1

    @patch("src.trader._get_current_price", return_value=105.0)
    @patch("src.trader.get_connection")
    def test_max_hold_days(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        _add_position(test_db, ticker="AAPL", shares=100, avg_cost=100.0,
                       days_ago=MAX_HOLD_DAYS + 5)

        from src.trader import _check_exit_conditions
        sells = _check_exit_conditions(test_db)
        assert sells == 1

    @patch("src.trader._get_current_price", return_value=105.0)
    @patch("src.trader.get_connection")
    def test_no_exit_within_bounds(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        # Price +5%, well within TP/SL, held for 10 days
        _add_position(test_db, ticker="AAPL", shares=100, avg_cost=100.0, days_ago=10)

        from src.trader import _check_exit_conditions
        sells = _check_exit_conditions(test_db)
        assert sells == 0

    @patch("src.trader._get_current_price")
    @patch("src.trader.get_connection")
    def test_sell_records_trade(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        _add_position(test_db, ticker="TSLA", shares=50, avg_cost=200.0)
        mock_price.return_value = 260.0  # +30% → take profit

        from src.trader import _check_exit_conditions
        _check_exit_conditions(test_db)

        # Check virtual_trades has a SELL record
        trade = test_db.execute(
            "SELECT * FROM virtual_trades WHERE ticker = 'TSLA' AND action = 'SELL'"
        ).fetchone()
        assert trade is not None
        assert "Take profit" in trade["reason"]

    @patch("src.trader._get_current_price", return_value=None)
    @patch("src.trader.get_connection")
    def test_no_sell_when_price_unavailable(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        _add_position(test_db, ticker="AAPL", shares=100, avg_cost=100.0,
                       days_ago=MAX_HOLD_DAYS + 5)

        from src.trader import _check_exit_conditions
        sells = _check_exit_conditions(test_db)
        assert sells == 0  # Can't sell without knowing current price


class TestCashAccounting:
    @patch("src.trader._get_current_price", return_value=150.0)
    @patch("src.trader.get_connection")
    def test_buy_reduces_cash(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        initial_cash = test_db.execute("SELECT cash FROM portfolio_state WHERE id = 1").fetchone()
        initial = float(initial_cash["cash"])

        _add_scored_transaction(test_db, ticker="GOOG", score=85.0)

        from src.trader import _evaluate_new_signals
        _evaluate_new_signals(test_db)

        final_cash = test_db.execute("SELECT cash FROM portfolio_state WHERE id = 1").fetchone()
        final = float(final_cash["cash"])
        assert final < initial

    @patch("src.trader._get_current_price", return_value=200.0)
    @patch("src.trader.get_connection")
    def test_sell_increases_cash(self, mock_conn, mock_price, test_db):
        mock_conn.return_value = test_db
        # Reduce cash first
        test_db.execute("UPDATE portfolio_state SET cash = 80000 WHERE id = 1")
        _add_position(test_db, ticker="NVDA", shares=50, avg_cost=100.0)
        test_db.commit()

        # Price = 200 → +100% → take profit
        from src.trader import _check_exit_conditions
        _check_exit_conditions(test_db)

        cash = test_db.execute("SELECT cash FROM portfolio_state WHERE id = 1").fetchone()
        assert float(cash["cash"]) > 80000  # Cash should increase from sell
