"""
main.py – Orchestrator.

Runs the full pipeline: fetch → score → trade → portfolio → web_builder.
Designed to be executed from GitHub Actions or locally.
"""

import logging
import sys
import time

from src.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)


def main() -> None:
    """Execute the full insider-tracking pipeline."""
    start = time.time()
    logger.info("=" * 60)
    logger.info("Insider Trading Tracker – pipeline starting")
    logger.info("=" * 60)

    # ── 0. Initialize database ──────────────────────────────────────────────
    logger.info("Step 0/5: Initializing database…")
    init_db()

    # ── 1. Fetch new Form 4 filings ────────────────────────────────────────
    logger.info("Step 1/5: Fetching SEC EDGAR Form 4 filings…")
    try:
        from src import fetcher
        new_txs = fetcher.run()
        logger.info("Fetcher returned %d new transactions", new_txs)
    except Exception:
        logger.exception("Fetcher failed – continuing with existing data")

    # ── 2. Score transactions ──────────────────────────────────────────────
    logger.info("Step 2/5: Scoring insider transactions…")
    try:
        from src import scorer
        scored = scorer.run()
        logger.info("Scored %d transactions", scored)
    except Exception:
        logger.exception("Scorer failed – continuing")

    # ── 3. Execute virtual trades ──────────────────────────────────────────
    logger.info("Step 3/5: Running virtual trading engine…")
    try:
        from src import trader
        result = trader.run()
        logger.info("Trader: %d buys, %d sells", result["buys"], result["sells"])
    except Exception:
        logger.exception("Trader failed – continuing")

    # ── 4. Update portfolio snapshot ───────────────────────────────────────
    logger.info("Step 4/5: Recording portfolio snapshot…")
    try:
        from src import portfolio
        metrics = portfolio.run()
        logger.info("Portfolio value: $%.2f (return: %.2f%%)",
                     metrics["current_value"], metrics["total_return_pct"])
    except Exception:
        logger.exception("Portfolio tracker failed – continuing")

    # ── 5. Generate static website ─────────────────────────────────────────
    logger.info("Step 5/5: Building static website…")
    try:
        from src import web_builder
        web_builder.run()
        logger.info("Website generated successfully")
    except Exception:
        logger.exception("Web builder failed")

    elapsed = time.time() - start
    logger.info("=" * 60)
    logger.info("Pipeline complete in %.1f seconds", elapsed)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
