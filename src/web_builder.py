"""
web_builder.py – Generate static website in docs/ folder.

Reads HTML, CSS, and JS templates from src/templates/ and copies them
along with generated data.json into docs/ for GitHub Pages deployment.

Template files:
  - src/templates/index.html  (HTML structure)
  - src/templates/style.css   (CSS design system)
  - src/templates/app.js      (Chart.js + table rendering logic)

Output:
  - docs/index.html
  - docs/style.css
  - docs/app.js
  - docs/data.json
"""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from src.config import (
    DOCS_DIR,
    RECENT_TRANSACTIONS_LIMIT,
    RECENT_TRANSACTIONS_MIN_SCORE,
    STARTING_CAPITAL,
    TRADE_HISTORY_LIMIT,
)
from src.db import get_connection
from src.portfolio import compute_metrics

logger = logging.getLogger(__name__)

TEMPLATES_DIR: Path = Path(__file__).resolve().parent / "templates"


def _build_data_json(conn) -> dict:
    """Assemble all data for the frontend."""
    metrics = compute_metrics(conn)

    # ── Portfolio history for chart ────────────────────────────────────────
    history = conn.execute(
        "SELECT date, total_value, benchmark_value FROM portfolio_history ORDER BY date ASC"
    ).fetchall()

    chart_data = {
        "labels": [r["date"] for r in history],
        "portfolio": [round(float(r["total_value"]), 2) for r in history],
        "benchmark": [
            round(float(r["benchmark_value"]), 2) if r["benchmark_value"] else None
            for r in history
        ],
    }

    # ── Current positions ──────────────────────────────────────────────────
    positions_raw = conn.execute(
        "SELECT ticker, shares, avg_cost, opened_at, triggering_insider FROM positions"
    ).fetchall()

    positions = []
    for p in positions_raw:
        cost = float(p["avg_cost"])
        positions.append({
            "ticker": p["ticker"],
            "shares": float(p["shares"]),
            "avg_cost": round(cost, 2),
            "opened_at": p["opened_at"][:10] if p["opened_at"] else "",
            "triggering_insider": p["triggering_insider"] or "",
            "current_price": None,  # Filled by JS or could be enriched here
            "pnl_pct": None,
        })

    # ── Recent high-scoring insider transactions ───────────────────────────
    recent_txs = conn.execute(
        """
        SELECT it.ticker, it.insider_name, it.insider_title, it.trade_date,
               it.shares, it.price, it.value, it.tx_code,
               s.total_score, s.hit_rate, s.cluster_bonus
        FROM insider_transactions it
        JOIN insider_scores s ON s.transaction_id = it.id
        WHERE s.total_score >= ?
        ORDER BY s.total_score DESC, it.trade_date DESC
        LIMIT ?
        """,
        (RECENT_TRANSACTIONS_MIN_SCORE, RECENT_TRANSACTIONS_LIMIT),
    ).fetchall()

    transactions = [
        {
            "ticker": r["ticker"],
            "insider_name": r["insider_name"],
            "insider_title": r["insider_title"],
            "trade_date": r["trade_date"],
            "shares": float(r["shares"]),
            "price": round(float(r["price"]), 2),
            "value": round(float(r["value"]), 2),
            "tx_code": r["tx_code"],
            "score": round(float(r["total_score"]), 1),
            "hit_rate": round(float(r["hit_rate"]) * 100, 1) if r["hit_rate"] else None,
            "cluster": bool(r["cluster_bonus"] and float(r["cluster_bonus"]) > 0),
        }
        for r in recent_txs
    ]

    # ── Trade history ──────────────────────────────────────────────────────
    trades_raw = conn.execute(
        """
        SELECT timestamp, ticker, action, price, shares, total_value,
               reason, triggering_insider
        FROM virtual_trades
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (TRADE_HISTORY_LIMIT,),
    ).fetchall()

    trades = [
        {
            "timestamp": r["timestamp"][:19] if r["timestamp"] else "",
            "ticker": r["ticker"],
            "action": r["action"],
            "price": round(float(r["price"]), 2),
            "shares": float(r["shares"]),
            "total_value": round(float(r["total_value"]), 2),
            "reason": r["reason"] or "",
            "triggering_insider": r["triggering_insider"] or "",
        }
        for r in trades_raw
    ]

    return {
        "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "metrics": metrics,
        "starting_capital": STARTING_CAPITAL,
        "chart": chart_data,
        "positions": positions,
        "transactions": transactions,
        "trades": trades,
    }


def _copy_template(filename: str) -> None:
    """Copy a template file from src/templates/ to docs/."""
    src = TEMPLATES_DIR / filename
    dst = DOCS_DIR / filename
    if not src.exists():
        logger.error("Template file not found: %s", src)
        return
    shutil.copy2(src, dst)
    logger.info("Copied %s -> %s (%.1f KB)", src.name, dst, dst.stat().st_size / 1024)


def run() -> None:
    """Generate static website files in docs/ directory."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    conn = get_connection()
    try:
        data = _build_data_json(conn)
    finally:
        conn.close()

    # ── Write data.json ────────────────────────────────────────────────────
    data_path = DOCS_DIR / "data.json"
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("Written %s (%.1f KB)", data_path, data_path.stat().st_size / 1024)

    # ── Copy template files (HTML, CSS, JS) ────────────────────────────────
    _copy_template("index.html")
    _copy_template("style.css")
    _copy_template("app.js")

    logger.info(
        "Web build complete – %d positions, %d transactions, %d trades",
        len(data["positions"]),
        len(data["transactions"]),
        len(data["trades"]),
    )
