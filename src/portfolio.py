"""
portfolio.py – Portfolio valuation and performance metrics.

Records daily snapshots of portfolio value and computes key metrics:
total return, YTD return, Sharpe ratio, max drawdown, win rate.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime

import numpy as np

from src.config import BENCHMARK_TICKER, STARTING_CAPITAL
from src.db import get_connection

logger = logging.getLogger(__name__)


def _get_current_price(ticker: str) -> float | None:
    """Fetch current market price via yfinance."""
    try:
        import yfinance as yf

        stock = yf.Ticker(ticker)
        hist = stock.history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        logger.exception("Failed to get price for %s", ticker)
        return None


def _compute_portfolio_value(conn) -> tuple[float, float, float, int]:
    """
    Compute current portfolio value.
    Returns (total_value, cash, positions_value, num_positions).
    """
    cash_row = conn.execute("SELECT cash FROM portfolio_state WHERE id = 1").fetchone()
    cash = float(cash_row["cash"]) if cash_row else 0.0

    positions = conn.execute("SELECT ticker, shares, avg_cost FROM positions").fetchall()
    positions_value = 0.0
    num_positions = len(positions)

    for pos in positions:
        price = _get_current_price(pos["ticker"])
        if price is not None:
            positions_value += float(pos["shares"]) * price
        else:
            positions_value += float(pos["shares"]) * float(pos["avg_cost"])

    total = cash + positions_value
    return total, cash, positions_value, num_positions


def _get_benchmark_value(conn) -> float | None:
    """Calculate the value of the benchmark (SPY) assuming starting capital was invested."""
    try:
        from src.prices import get_price_on_date, get_current_price as get_price

        # Find first trade date
        first = conn.execute("SELECT MIN(date) as date FROM portfolio_history").fetchone()
        if not first or not first["date"]:
            return STARTING_CAPITAL

        first_date = first["date"]

        start_price = get_price_on_date(BENCHMARK_TICKER, first_date)
        if start_price is None:
            return None

        current_price = get_price(BENCHMARK_TICKER)
        if current_price is None:
            return None

        # Normalize: what would starting capital be worth invested in SPY?
        spy_return = (current_price - start_price) / start_price
        return STARTING_CAPITAL * (1 + spy_return)

    except Exception:
        logger.exception("Failed to compute benchmark value")
        return None


def record_snapshot(conn=None) -> dict | None:
    """
    Record a daily snapshot of portfolio value.
    Returns snapshot data dict.
    """
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")

        total, cash, positions_value, num_positions = _compute_portfolio_value(conn)
        benchmark = _get_benchmark_value(conn)

        # Upsert today's snapshot
        conn.execute(
            """
            INSERT INTO portfolio_history (date, total_value, cash, positions_value,
                                            benchmark_value, num_positions)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total_value = excluded.total_value,
                cash = excluded.cash,
                positions_value = excluded.positions_value,
                benchmark_value = excluded.benchmark_value,
                num_positions = excluded.num_positions
            """,
            (today, total, cash, positions_value, benchmark, num_positions),
        )
        conn.commit()

        snapshot = {
            "date": today,
            "total_value": round(total, 2),
            "cash": round(cash, 2),
            "positions_value": round(positions_value, 2),
            "benchmark_value": round(benchmark, 2) if benchmark else None,
            "num_positions": num_positions,
        }

        logger.info(
            "Portfolio snapshot: $%.2f (cash $%.2f + positions $%.2f) | %d positions | benchmark $%s",
            total, cash, positions_value, num_positions,
            f"{benchmark:.2f}" if benchmark else "N/A",
        )

        return snapshot

    finally:
        if close_conn:
            conn.close()


def compute_metrics(conn=None) -> dict:
    """
    Compute portfolio performance metrics from historical snapshots.
    Returns dict with: total_return_pct, ytd_return_pct, sharpe_ratio,
    max_drawdown_pct, total_trades, win_rate.
    """
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        snapshots = conn.execute(
            "SELECT date, total_value FROM portfolio_history ORDER BY date ASC"
        ).fetchall()

        metrics: dict = {
            "total_return_pct": 0.0,
            "ytd_return_pct": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "total_trades": 0,
            "win_rate": 0.0,
            "current_value": STARTING_CAPITAL,
        }

        if not snapshots:
            return metrics

        values = [float(s["total_value"]) for s in snapshots]
        dates = [s["date"] for s in snapshots]
        current = values[-1]
        metrics["current_value"] = round(current, 2)

        # ── Total return ───────────────────────────────────────────────────
        metrics["total_return_pct"] = round(
            ((current - STARTING_CAPITAL) / STARTING_CAPITAL) * 100, 2
        )

        # ── YTD return ─────────────────────────────────────────────────────
        current_year = datetime.utcnow().strftime("%Y")
        ytd_start_val = STARTING_CAPITAL
        for i, date in enumerate(dates):
            if date.startswith(current_year):
                ytd_start_val = values[max(0, i - 1)]
                break
        if ytd_start_val > 0:
            metrics["ytd_return_pct"] = round(
                ((current - ytd_start_val) / ytd_start_val) * 100, 2
            )

        # ── Sharpe ratio (annualized, assuming daily data) ─────────────────
        if len(values) >= 2:
            daily_returns = np.diff(values) / np.array(values[:-1])
            if len(daily_returns) > 1 and np.std(daily_returns) > 0:
                sharpe = (np.mean(daily_returns) / np.std(daily_returns)) * math.sqrt(252)
                metrics["sharpe_ratio"] = round(float(sharpe), 2)

        # ── Max drawdown ───────────────────────────────────────────────────
        peak = values[0]
        max_dd = 0.0
        for v in values:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        metrics["max_drawdown_pct"] = round(max_dd * 100, 2)

        # ── Trade stats ────────────────────────────────────────────────────
        trades = conn.execute(
            "SELECT action, price, shares, total_value, ticker FROM virtual_trades ORDER BY timestamp"
        ).fetchall()
        metrics["total_trades"] = len(trades)

        # Compute win rate from sell trades
        sells = conn.execute(
            """
            SELECT vt.ticker, vt.price as sell_price,
                   (SELECT vt2.price FROM virtual_trades vt2
                    WHERE vt2.ticker = vt.ticker AND vt2.action = 'BUY'
                    ORDER BY vt2.timestamp DESC LIMIT 1) as buy_price
            FROM virtual_trades vt
            WHERE vt.action = 'SELL'
            """
        ).fetchall()

        if sells:
            wins = sum(
                1 for s in sells
                if s["buy_price"] is not None and float(s["sell_price"]) > float(s["buy_price"])
            )
            metrics["win_rate"] = round((wins / len(sells)) * 100, 2)

        return metrics

    finally:
        if close_conn:
            conn.close()


def run() -> dict:
    """
    Main entry point: record snapshot and compute metrics.
    Returns performance metrics dict.
    """
    conn = get_connection()
    try:
        record_snapshot(conn)
        metrics = compute_metrics(conn)
    finally:
        conn.close()

    logger.info(
        "Portfolio metrics: return=%.2f%%, YTD=%.2f%%, Sharpe=%.2f, MaxDD=%.2f%%, WinRate=%.2f%%",
        metrics["total_return_pct"], metrics["ytd_return_pct"],
        metrics["sharpe_ratio"], metrics["max_drawdown_pct"], metrics["win_rate"],
    )
    return metrics
