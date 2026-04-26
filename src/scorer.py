"""
scorer.py – Compute insider transaction confidence scores.

For each new purchase transaction, compute a composite 0–100 score
based on insider role, historical hit rate, transaction regularity,
and transaction size relative to the insider's history.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

import numpy as np

from src.config import (
    BENCHMARK_TICKER,
    CLUSTER_BONUS,
    CLUSTER_MIN_INSIDERS,
    CLUSTER_WINDOW_DAYS,
    DEFAULT_ROLE_WEIGHT,
    HIT_RATE_HORIZON_DAYS,
    ROLE_WEIGHTS,
    WEIGHT_HIT_RATE,
    WEIGHT_OPPORTUNISTIC,
    WEIGHT_ROLE,
    WEIGHT_SIZE_ZSCORE,
)
from src.db import get_connection

logger = logging.getLogger(__name__)


def _get_role_weight(title: str) -> float:
    """Map insider title to a role weight in [0, 1]."""
    title_upper = title.upper()
    for key, weight in ROLE_WEIGHTS.items():
        if key.upper() in title_upper:
            return weight
    return DEFAULT_ROLE_WEIGHT


def _compute_hit_rate(cik: str, conn) -> float | None:
    """
    Compute hit rate for this insider: fraction of their past open-market
    purchases (code P) after which the stock outperformed SPY over ~6 months.

    Returns None if no historical purchases to evaluate.
    """
    # Get all past purchases by this insider with enough time elapsed
    cutoff = (datetime.utcnow() - timedelta(days=HIT_RATE_HORIZON_DAYS)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """
        SELECT ticker, trade_date, price
        FROM insider_transactions
        WHERE cik = ? AND tx_code = 'P' AND trade_date <= ?
        ORDER BY trade_date
        """,
        (cik, cutoff),
    ).fetchall()

    if not rows:
        return None

    # For a real implementation we'd fetch historical price data and compare
    # returns vs SPY. Since we can't do this retroactively without a paid API,
    # we use a simplified heuristic based on available data.
    # When running live, we can track forward performance.

    # Simplified: estimate based on average return of held stocks.
    try:
        from src.prices import get_price_on_date

        hits = 0
        total = 0

        # Only sample a subset if there are many, to save time
        for row in rows[:20]:
            ticker = row["ticker"]
            trade_date_str = row["trade_date"]
            buy_price = row["price"]

            try:
                trade_dt = datetime.strptime(trade_date_str, "%Y-%m-%d")
                end_dt = trade_dt + timedelta(days=HIT_RATE_HORIZON_DAYS)

                # Skip if end date is in the future
                if end_dt > datetime.utcnow():
                    continue

                end_price = get_price_on_date(ticker, end_dt.strftime("%Y-%m-%d"))
                if end_price is None:
                    continue
                stock_return = (end_price - buy_price) / buy_price

                # Fetch SPY return for same period
                spy_start_price = get_price_on_date(BENCHMARK_TICKER, trade_date_str)
                spy_end_price = get_price_on_date(BENCHMARK_TICKER, end_dt.strftime("%Y-%m-%d"))

                if spy_start_price is None or spy_end_price is None or spy_start_price == 0:
                    continue

                spy_return = (spy_end_price - spy_start_price) / spy_start_price

                total += 1
                if stock_return > spy_return:
                    hits += 1

            except Exception:
                logger.debug("Could not compute hit rate for %s on %s", ticker, trade_date_str)
                continue

        if total == 0:
            return None

        return hits / total

    except Exception:
        logger.exception("Error computing hit rate for CIK %s", cik)
        return None


def _compute_opportunistic_score(cik: str, conn) -> float:
    """
    Compute an 'opportunistic' score based on transaction irregularity.
    Insiders who trade on a regular schedule (e.g., 10b5-1 plans) are
    less informative than those who trade sporadically.

    Returns a value in [0, 1] where 1 = highly irregular (more informative).
    """
    rows = conn.execute(
        """
        SELECT trade_date, is_10b5_1
        FROM insider_transactions
        WHERE cik = ? AND tx_code = 'P'
        ORDER BY trade_date
        """,
        (cik,),
    ).fetchall()

    if len(rows) <= 1:
        return 0.7  # Default: single purchase is somewhat opportunistic

    # Check if any transactions are part of a 10b5-1 plan
    has_plan = any(row["is_10b5_1"] for row in rows)
    if has_plan:
        return 0.2  # Planned transactions are less informative

    # Compute coefficient of variation of intervals between trades
    dates = []
    for row in rows:
        try:
            dates.append(datetime.strptime(row["trade_date"], "%Y-%m-%d"))
        except ValueError:
            continue

    if len(dates) < 2:
        return 0.7

    dates.sort()
    intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]

    if not intervals:
        return 0.7

    mean_interval = np.mean(intervals)
    if mean_interval == 0:
        return 0.3

    std_interval = np.std(intervals)
    cv = std_interval / mean_interval  # Coefficient of variation

    # Higher CV = more irregular = more opportunistic
    # Normalize to [0, 1] using sigmoid-like transform
    score = min(1.0, cv / 2.0)
    return round(score, 4)


def _compute_size_zscore(cik: str, value: float, conn) -> float:
    """
    Compute z-score of this transaction's value relative to
    the insider's historical purchase values.

    Returns normalized value in [0, 1].
    """
    rows = conn.execute(
        """
        SELECT value FROM insider_transactions
        WHERE cik = ? AND tx_code = 'P'
        """,
        (cik,),
    ).fetchall()

    values = [float(r["value"]) for r in rows if r["value"] > 0]

    if len(values) < 2:
        return 0.5  # Default: not enough history

    mean_val = np.mean(values)
    std_val = np.std(values)

    if std_val == 0:
        return 0.5

    z = (value - mean_val) / std_val

    # Normalize z-score to [0, 1] using sigmoid
    normalized = 1.0 / (1.0 + math.exp(-z))
    return round(normalized, 4)


def _check_cluster(ticker: str, cik: str, trade_date: str, conn) -> bool:
    """
    Check if ≥ CLUSTER_MIN_INSIDERS OTHER insiders bought the same ticker
    within the last CLUSTER_WINDOW_DAYS days.
    """
    try:
        dt = datetime.strptime(trade_date, "%Y-%m-%d")
    except ValueError:
        return False

    window_start = (dt - timedelta(days=CLUSTER_WINDOW_DAYS)).strftime("%Y-%m-%d")

    row = conn.execute(
        """
        SELECT COUNT(DISTINCT cik) as cnt
        FROM insider_transactions
        WHERE ticker = ? AND cik != ? AND tx_code = 'P'
        AND trade_date BETWEEN ? AND ?
        """,
        (ticker, cik, window_start, trade_date),
    ).fetchone()

    other_count = row["cnt"] if row else 0
    return other_count >= CLUSTER_MIN_INSIDERS


def score_transaction(tx_id: int, conn=None) -> float | None:
    """
    Compute and store the composite score for a single transaction.
    Returns the total score or None if the transaction doesn't qualify.
    """
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        tx = conn.execute(
            "SELECT * FROM insider_transactions WHERE id = ?", (tx_id,)
        ).fetchone()

        if tx is None:
            logger.warning("Transaction %d not found", tx_id)
            return None

        # Only score open-market purchases
        if tx["tx_code"] != "P":
            return None

        # Check if already scored
        existing = conn.execute(
            "SELECT total_score FROM insider_scores WHERE transaction_id = ?", (tx_id,)
        ).fetchone()
        if existing:
            return float(existing["total_score"])

        # ── Compute components ─────────────────────────────────────────────
        role_weight = _get_role_weight(tx["insider_title"])
        hit_rate = _compute_hit_rate(tx["cik"], conn)
        opportunistic = _compute_opportunistic_score(tx["cik"], conn)
        size_z = _compute_size_zscore(tx["cik"], float(tx["value"]), conn)

        # Use 0.5 default for hit_rate if we can't compute it
        if hit_rate is None:
            hit_rate = 0.5

        # ── Composite score ────────────────────────────────────────────────
        raw_score = (
            WEIGHT_ROLE * role_weight
            + WEIGHT_HIT_RATE * hit_rate
            + WEIGHT_OPPORTUNISTIC * opportunistic
            + WEIGHT_SIZE_ZSCORE * size_z
        )

        # ── Cluster bonus ──────────────────────────────────────────────────
        cluster = _check_cluster(tx["ticker"], tx["cik"], tx["trade_date"], conn)
        bonus = CLUSTER_BONUS if cluster else 0.0

        total = min(100.0, round(raw_score + bonus, 2))

        # ── Store ──────────────────────────────────────────────────────────
        conn.execute(
            """
            INSERT INTO insider_scores
            (transaction_id, hit_rate, opportunistic, role_weight, size_zscore,
             cluster_bonus, total_score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (tx_id, hit_rate, opportunistic, role_weight, size_z, bonus, total),
        )
        conn.commit()

        logger.info(
            "Scored tx %d (%s by %s): role=%.2f hit=%.2f opp=%.2f size=%.2f cluster=%.1f → %.1f",
            tx_id, tx["ticker"], tx["insider_name"],
            role_weight, hit_rate, opportunistic, size_z, bonus, total,
        )

        return total

    finally:
        if close_conn:
            conn.close()


def run() -> int:
    """
    Score all unscored purchase transactions.
    Returns the number of transactions scored.
    """
    conn = get_connection()
    scored = 0
    try:
        # Find unscored purchase transactions
        unscored = conn.execute(
            """
            SELECT it.id
            FROM insider_transactions it
            LEFT JOIN insider_scores s ON s.transaction_id = it.id
            WHERE it.tx_code = 'P' AND s.id IS NULL
            ORDER BY it.trade_date DESC
            """,
        ).fetchall()

        logger.info("Found %d unscored purchase transactions", len(unscored))

        for row in unscored:
            try:
                score = score_transaction(row["id"], conn)
                if score is not None:
                    scored += 1
            except Exception:
                logger.exception("Error scoring transaction %d", row["id"])

    finally:
        conn.close()

    logger.info("Scorer complete: scored %d transactions", scored)
    return scored
