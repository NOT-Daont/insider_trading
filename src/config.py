"""
Centralized configuration for the Insider Trading Tracker.
All tuneable constants live here.
"""

from __future__ import annotations

from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = PROJECT_ROOT / "data"
DOCS_DIR: Path = PROJECT_ROOT / "docs"
DB_PATH: Path = DATA_DIR / "portfolio.db"

# ── SEC EDGAR ──────────────────────────────────────────────────────────────────
SEC_USER_AGENT: str = "InsiderTracker admin@example.com"
SEC_BASE_URL: str = "https://efts.sec.gov/LATEST/search-index"
SEC_FULL_TEXT_URL: str = "https://efts.sec.gov/LATEST/search-index"
SEC_FILINGS_URL: str = "https://efts.sec.gov/LATEST/search-index"
SEC_EDGAR_FULL_TEXT: str = "https://efts.sec.gov/LATEST/search-index"
# EDGAR full-text search API (free, no key needed)
SEC_EFTS_URL: str = "https://efts.sec.gov/LATEST/search-index"
# RSS feed for recent Form 4 filings
SEC_RSS_URL: str = "https://www.sec.gov/cgi-bin/browse-edgar"
# Direct filing archive
SEC_ARCHIVES_URL: str = "https://www.sec.gov/Archives/edgar/data"
SEC_RATE_LIMIT_RPS: int = 8  # stay comfortably under the 10 req/s limit

# ── Scoring weights ───────────────────────────────────────────────────────────
WEIGHT_ROLE: float = 40.0
WEIGHT_HIT_RATE: float = 30.0
WEIGHT_OPPORTUNISTIC: float = 20.0
WEIGHT_SIZE_ZSCORE: float = 10.0
CLUSTER_BONUS: float = 15.0
CLUSTER_WINDOW_DAYS: int = 30
CLUSTER_MIN_INSIDERS: int = 2

ROLE_WEIGHTS: dict[str, float] = {
    "CEO": 1.0,
    "Chief Executive Officer": 1.0,
    "CFO": 1.0,
    "Chief Financial Officer": 1.0,
    "President": 0.8,
    "COO": 0.8,
    "Director": 0.6,
    "10% Owner": 0.4,
    "10 Percent Owner": 0.4,
}
DEFAULT_ROLE_WEIGHT: float = 0.3

# ── Trading rules ──────────────────────────────────────────────────────────────
STARTING_CAPITAL: float = 100_000.0
SCORE_THRESHOLD: int = 70
TAKE_PROFIT_PCT: float = 0.25   # +25 %
STOP_LOSS_PCT: float = -0.12    # −12 %
MAX_POSITION_PCT: float = 0.05  # 5 % of portfolio
MAX_HOLD_DAYS: int = 180

# ── Benchmark ──────────────────────────────────────────────────────────────────
BENCHMARK_TICKER: str = "SPY"
HIT_RATE_HORIZON_DAYS: int = 126  # ~6 months of trading days

# ── Web builder ────────────────────────────────────────────────────────────────
RECENT_TRANSACTIONS_LIMIT: int = 50
RECENT_TRANSACTIONS_MIN_SCORE: int = 50
TRADE_HISTORY_LIMIT: int = 100
