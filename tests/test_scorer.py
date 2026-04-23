"""
Unit tests for scorer.py
"""

import sqlite3
from unittest.mock import patch

import pytest

# We need to set up an in-memory DB for testing
from src import config
from src.scorer import (
    _check_cluster,
    _compute_opportunistic_score,
    _compute_size_zscore,
    _get_role_weight,
    score_transaction,
)


@pytest.fixture
def test_db(tmp_path):
    """Create an in-memory test database with schema."""
    db_path = tmp_path / "test.db"
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
    """)
    conn.commit()
    return conn


def _insert_tx(conn, **kwargs):
    """Helper to insert a transaction with defaults."""
    defaults = {
        "accession_no": "test-001",
        "filed_date": "2024-01-15",
        "trade_date": "2024-01-15",
        "ticker": "AAPL",
        "cik": "12345",
        "insider_name": "John Doe",
        "insider_title": "CEO",
        "tx_code": "P",
        "shares": 1000,
        "price": 150.0,
        "value": 150000.0,
        "shares_owned_after": 5000,
        "is_10b5_1": 0,
    }
    defaults.update(kwargs)
    cursor = conn.execute(
        """
        INSERT INTO insider_transactions
        (accession_no, filed_date, trade_date, ticker, cik, insider_name,
         insider_title, tx_code, shares, price, value, shares_owned_after, is_10b5_1)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(defaults.values()),
    )
    conn.commit()
    return cursor.lastrowid


class TestRoleWeight:
    def test_ceo(self):
        assert _get_role_weight("CEO") == 1.0

    def test_chief_executive(self):
        assert _get_role_weight("Chief Executive Officer") == 1.0

    def test_cfo(self):
        assert _get_role_weight("CFO") == 1.0

    def test_director(self):
        assert _get_role_weight("Director") == 0.6

    def test_ten_percent_owner(self):
        assert _get_role_weight("10% Owner") == 0.4

    def test_unknown_defaults(self):
        assert _get_role_weight("Secretary") == config.DEFAULT_ROLE_WEIGHT

    def test_case_insensitive(self):
        assert _get_role_weight("chief executive officer") == 1.0

    def test_compound_title(self):
        # "President and CEO" should match CEO
        assert _get_role_weight("President and CEO") == 1.0


class TestOpportunisticScore:
    def test_single_purchase(self, test_db):
        _insert_tx(test_db, cik="OPP1", accession_no="opp-1")
        score = _compute_opportunistic_score("OPP1", test_db)
        assert score == 0.7  # Default for single purchase

    def test_planned_10b5_1(self, test_db):
        _insert_tx(test_db, cik="OPP2", accession_no="opp-2a", trade_date="2024-01-01", is_10b5_1=1)
        _insert_tx(test_db, cik="OPP2", accession_no="opp-2b", trade_date="2024-02-01", is_10b5_1=0)
        score = _compute_opportunistic_score("OPP2", test_db)
        assert score == 0.2  # Planned → low score

    def test_irregular_trades(self, test_db):
        # Trades at very irregular intervals
        _insert_tx(test_db, cik="OPP3", accession_no="opp-3a", trade_date="2023-01-15")
        _insert_tx(test_db, cik="OPP3", accession_no="opp-3b", trade_date="2023-06-20")
        _insert_tx(test_db, cik="OPP3", accession_no="opp-3c", trade_date="2023-07-01")
        score = _compute_opportunistic_score("OPP3", test_db)
        assert 0.0 <= score <= 1.0


class TestSizeZscore:
    def test_single_purchase_default(self, test_db):
        _insert_tx(test_db, cik="SZ1", accession_no="sz-1", value=100000)
        score = _compute_size_zscore("SZ1", 100000, test_db)
        assert score == 0.5  # Not enough history

    def test_large_purchase(self, test_db):
        # Create history of smaller purchases
        for i in range(5):
            _insert_tx(
                test_db, cik="SZ2", accession_no=f"sz2-{i}",
                value=10000 + i * 100, trade_date=f"2023-0{i+1}-01"
            )
        # Now a much larger purchase
        score = _compute_size_zscore("SZ2", 500000, test_db)
        assert score > 0.5  # Above average = higher score

    def test_small_purchase(self, test_db):
        for i in range(5):
            _insert_tx(
                test_db, cik="SZ3", accession_no=f"sz3-{i}",
                value=100000 + i * 1000, trade_date=f"2023-0{i+1}-01"
            )
        score = _compute_size_zscore("SZ3", 100, test_db)
        assert score < 0.5  # Below average


class TestClusterDetection:
    def test_no_cluster(self, test_db):
        _insert_tx(test_db, ticker="MSFT", cik="CL1", accession_no="cl-1", trade_date="2024-01-15")
        assert not _check_cluster("MSFT", "CL1", "2024-01-15", test_db)

    def test_cluster_detected(self, test_db):
        # Two OTHER insiders bought same stock recently
        _insert_tx(test_db, ticker="MSFT", cik="CL2", accession_no="cl-2", trade_date="2024-01-10")
        _insert_tx(test_db, ticker="MSFT", cik="CL3", accession_no="cl-3", trade_date="2024-01-12")
        # Our insider
        _insert_tx(test_db, ticker="MSFT", cik="CL1", accession_no="cl-1", trade_date="2024-01-15")
        assert _check_cluster("MSFT", "CL1", "2024-01-15", test_db)

    def test_cluster_different_ticker(self, test_db):
        _insert_tx(test_db, ticker="GOOG", cik="CL4", accession_no="cl-4", trade_date="2024-01-10")
        _insert_tx(test_db, ticker="GOOG", cik="CL5", accession_no="cl-5", trade_date="2024-01-12")
        # Different ticker
        assert not _check_cluster("MSFT", "CL6", "2024-01-15", test_db)

    def test_cluster_outside_window(self, test_db):
        # Purchases > 30 days ago
        _insert_tx(test_db, ticker="TSLA", cik="CL7", accession_no="cl-7", trade_date="2023-11-01")
        _insert_tx(test_db, ticker="TSLA", cik="CL8", accession_no="cl-8", trade_date="2023-11-02")
        assert not _check_cluster("TSLA", "CL9", "2024-01-15", test_db)


class TestScoreTransaction:
    @patch("src.scorer.get_connection")
    @patch("src.scorer._compute_hit_rate", return_value=0.7)
    def test_ceo_purchase_high_score(self, mock_hr, mock_conn, test_db):
        mock_conn.return_value = test_db
        tx_id = _insert_tx(
            test_db, accession_no="score-1", insider_title="CEO", value=200000
        )
        score = score_transaction(tx_id, test_db)
        assert score is not None
        assert score >= 50  # CEO with good hit rate should score well

    @patch("src.scorer.get_connection")
    @patch("src.scorer._compute_hit_rate", return_value=0.7)
    def test_sale_not_scored(self, mock_hr, mock_conn, test_db):
        mock_conn.return_value = test_db
        tx_id = _insert_tx(test_db, accession_no="score-2", tx_code="S")
        score = score_transaction(tx_id, test_db)
        assert score is None  # Sales are not scored

    @patch("src.scorer.get_connection")
    @patch("src.scorer._compute_hit_rate", return_value=0.5)
    def test_score_range(self, mock_hr, mock_conn, test_db):
        mock_conn.return_value = test_db
        tx_id = _insert_tx(test_db, accession_no="score-3", insider_title="Director")
        score = score_transaction(tx_id, test_db)
        assert score is not None
        assert 0 <= score <= 100

    @patch("src.scorer.get_connection")
    @patch("src.scorer._compute_hit_rate", return_value=0.5)
    def test_idempotent(self, mock_hr, mock_conn, test_db):
        mock_conn.return_value = test_db
        tx_id = _insert_tx(test_db, accession_no="score-4")
        score1 = score_transaction(tx_id, test_db)
        score2 = score_transaction(tx_id, test_db)
        assert score1 == score2  # Should return cached score
