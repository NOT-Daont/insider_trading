"""
trader.py – Autonomous virtual trading engine.

Evaluates scored insider transactions and executes virtual buy/sell orders
based on configurable rules (score threshold, position sizing, TP/SL/time).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from src.config import (
    MAX_HOLD_DAYS,
    MAX_POSITION_PCT,
    SCORE_THRESHOLD,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
)
from src.db import get_connection

logger = logging.getLogger(__name__)


def _get_current_price(ticker: str) -> float | None:
    """Fetch current market price via yfinance. Returns None on failure."""
    try:
        import yfinance as yf

        stock = yf.Ticker(ticker)
        hist = stock.history(period="5d")
        if hist.empty:
            logger.warning("No price data for %s", ticker)
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        logger.exception("Failed to get price for %s", ticker)
        return None


def _get_portfolio_value(conn) -> float:
    """Calculate total portfolio value (cash + positions at current market)."""
    cash_row = conn.execute("SELECT cash FROM portfolio_state WHERE id = 1").fetchone()
    cash = float(cash_row["cash"]) if cash_row else 0.0

    positions = conn.execute("SELECT ticker, shares, avg_cost FROM positions").fetchall()
    positions_value = 0.0
    for pos in positions:
        price = _get_current_price(pos["ticker"])
        if price is not None:
            positions_value += float(pos["shares"]) * price
        else:
            # Fallback to cost basis
            positions_value += float(pos["shares"]) * float(pos["avg_cost"])

    return cash + positions_value


def _evaluate_new_signals(conn) -> int:
    """
    Find high-scoring transactions and open new positions.
    Returns number of new positions opened.
    """
    # Get scored transactions that meet threshold and haven't been acted on
    high_score_txs = conn.execute(
        """
        SELECT it.id, it.ticker, it.insider_name, it.trade_date,
               s.total_score
        FROM insider_transactions it
        JOIN insider_scores s ON s.transaction_id = it.id
        WHERE s.total_score >= ?
        AND it.tx_code = 'P'
        AND NOT EXISTS (
            SELECT 1 FROM virtual_trades vt
            WHERE vt.triggering_tx_id = it.id AND vt.action = 'BUY'
        )
        ORDER BY s.total_score DESC
        """,
        (SCORE_THRESHOLD,),
    ).fetchall()

    logger.info("Found %d high-scoring signals (≥%d)", len(high_score_txs), SCORE_THRESHOLD)

    opened = 0
    for tx in high_score_txs:
        ticker = tx["ticker"]

        # Check if we already hold this ticker
        existing = conn.execute(
            "SELECT id FROM positions WHERE ticker = ?", (ticker,)
        ).fetchone()
        if existing:
            logger.info("Already holding %s, skipping", ticker)
            continue

        # Get current price
        price = _get_current_price(ticker)
        if price is None:
            logger.warning("Cannot get price for %s, skipping buy signal", ticker)
            continue

        # Calculate position size
        portfolio_value = _get_portfolio_value(conn)
        max_position_value = portfolio_value * MAX_POSITION_PCT

        cash_row = conn.execute("SELECT cash FROM portfolio_state WHERE id = 1").fetchone()
        cash = float(cash_row["cash"])

        position_value = min(max_position_value, cash)
        if position_value < price:
            logger.info("Insufficient cash ($%.2f) for %s at $%.2f", cash, ticker, price)
            continue

        shares = int(position_value / price)  # Whole shares only
        if shares <= 0:
            continue

        total_cost = shares * price

        # ── Execute virtual buy ────────────────────────────────────────────
        conn.execute(
            "UPDATE portfolio_state SET cash = cash - ?, updated_at = datetime('now') WHERE id = 1",
            (total_cost,),
        )
        conn.execute(
            """
            INSERT INTO positions (ticker, shares, avg_cost, opened_at,
                                    triggering_insider, triggering_tx_id)
            VALUES (?, ?, ?, datetime('now'), ?, ?)
            """,
            (ticker, shares, price, tx["insider_name"], tx["id"]),
        )
        conn.execute(
            """
            INSERT INTO virtual_trades
            (ticker, action, price, shares, total_value, reason,
             triggering_insider, triggering_tx_id)
            VALUES (?, 'BUY', ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker, price, shares, total_cost,
                f"Score {tx['total_score']:.1f} ≥ {SCORE_THRESHOLD}",
                tx["insider_name"], tx["id"],
            ),
        )
        conn.commit()

        logger.info(
            "BUY %d shares of %s @ $%.2f ($%.2f) – triggered by %s (score %.1f)",
            shares, ticker, price, total_cost, tx["insider_name"], tx["total_score"],
        )
        opened += 1

    return opened


def _check_exit_conditions(conn) -> int:
    """
    Check existing positions for take-profit, stop-loss, or max-hold-days.
    Returns number of positions closed.
    """
    positions = conn.execute(
        "SELECT id, ticker, shares, avg_cost, opened_at, triggering_insider FROM positions"
    ).fetchall()

    closed = 0
    now = datetime.utcnow()

    for pos in positions:
        ticker = pos["ticker"]
        shares = float(pos["shares"])
        cost = float(pos["avg_cost"])

        price = _get_current_price(ticker)
        if price is None:
            continue

        pnl_pct = (price - cost) / cost
        opened_at = pos["opened_at"]

        # Parse open date
        try:
            open_dt = datetime.strptime(opened_at[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                open_dt = datetime.strptime(opened_at[:10], "%Y-%m-%d")
            except ValueError:
                continue

        days_held = (now - open_dt).days
        reason = None

        # ── Check exit conditions ──────────────────────────────────────────
        if pnl_pct >= TAKE_PROFIT_PCT:
            reason = f"Take profit ({pnl_pct:+.1%} ≥ {TAKE_PROFIT_PCT:+.1%})"
        elif pnl_pct <= STOP_LOSS_PCT:
            reason = f"Stop loss ({pnl_pct:+.1%} ≤ {STOP_LOSS_PCT:+.1%})"
        elif days_held >= MAX_HOLD_DAYS:
            reason = f"Max hold period ({days_held}d ≥ {MAX_HOLD_DAYS}d, P/L: {pnl_pct:+.1%})"

        if reason is None:
            continue

        # ── Execute virtual sell ───────────────────────────────────────────
        total_value = shares * price

        conn.execute(
            "UPDATE portfolio_state SET cash = cash + ?, updated_at = datetime('now') WHERE id = 1",
            (total_value,),
        )
        conn.execute("DELETE FROM positions WHERE id = ?", (pos["id"],))
        conn.execute(
            """
            INSERT INTO virtual_trades
            (ticker, action, price, shares, total_value, reason, triggering_insider)
            VALUES (?, 'SELL', ?, ?, ?, ?, ?)
            """,
            (ticker, price, shares, total_value, reason, pos["triggering_insider"]),
        )
        conn.commit()

        logger.info(
            "SELL %d shares of %s @ $%.2f ($%.2f) – %s",
            int(shares), ticker, price, total_value, reason,
        )
        closed += 1

    return closed


def run() -> dict[str, int]:
    """
    Main entry point: evaluate new signals and check exit conditions.
    Returns dict with counts of buys and sells.
    """
    conn = get_connection()
    try:
        sells = _check_exit_conditions(conn)
        buys = _evaluate_new_signals(conn)
    finally:
        conn.close()

    logger.info("Trader complete: %d buys, %d sells", buys, sells)
    return {"buys": buys, "sells": sells}
