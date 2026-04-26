"""
prices.py - Stock price fetching module using Stooq and FMP.

Uses Stooq as the primary data source (free, no API key),
and Financial Modeling Prep (FMP) as a fallback (requires FMP_API_KEY).
Includes in-memory caching to save API calls during a run.
"""
from __future__ import annotations

import csv
import logging
import os
from io import StringIO

import requests

logger = logging.getLogger(__name__)

# In-memory cache for historical prices
_history_cache: dict[str, list[dict[str, float | str]]] = {}


def get_history(ticker: str) -> list[dict[str, float | str]]:
    """
    Fetch daily historical prices for a ticker.
    Returns list of dicts: [{'date': 'YYYY-MM-DD', 'close': 123.4}, ...]
    Sorted by date ascending (oldest to newest).
    """
    ticker = ticker.upper()
    if ticker in _history_cache:
        return _history_cache[ticker]

    history = _fetch_stooq(ticker)
    if not history:
        history = _fetch_fmp(ticker)

    if history:
        # Sort ascending by date (oldest first)
        history.sort(key=lambda x: x["date"])
        _history_cache[ticker] = history

    return history


def _fetch_stooq(ticker: str) -> list[dict[str, float | str]]:
    """Fetch history from Stooq CSV endpoint."""
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=10)
        resp.raise_for_status()

        # Stooq returns text "Exceeded the daily ..." if rate limited, or HTML instead of CSV
        if "Date,Open,High,Low,Close,Volume" not in resp.text:
            logger.debug("Stooq format unexpected or rate limited for %s", ticker)
            return []

        reader = csv.DictReader(StringIO(resp.text))
        history = []
        for row in reader:
            try:
                history.append({
                    "date": row["Date"],
                    "close": float(row["Close"])
                })
            except (KeyError, ValueError):
                continue
                
        logger.debug("Fetched %d days of history for %s from Stooq", len(history), ticker)
        return history
    except requests.RequestException as e:
        logger.debug("Stooq fetch failed for %s: %s", ticker, e)
        return []


def _fetch_fmp(ticker: str) -> list[dict[str, float | str]]:
    """Fetch history from FMP API."""
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        logger.debug("FMP_API_KEY not set, skipping fallback for %s", ticker)
        return []

    url = f"https://financialmodelingprep.com/api/v3/historical-price-full/{ticker.upper()}?apikey={api_key}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if "historical" not in data:
            return []

        history = []
        for day in data["historical"]:
            history.append({
                "date": day["date"],
                "close": float(day["close"])
            })
            
        logger.debug("Fetched %d days of history for %s from FMP", len(history), ticker)
        return history
    except Exception as e:
        logger.debug("FMP fetch failed for %s: %s", ticker, e)
        return []


def get_current_price(ticker: str) -> float | None:
    """Get the most recent closing price."""
    hist = get_history(ticker)
    if not hist:
        return None
    return hist[-1]["close"]


def get_price_on_date(ticker: str, target_date: str) -> float | None:
    """
    Get the closing price on or shortly after a specific date.
    target_date format: 'YYYY-MM-DD'
    """
    hist = get_history(ticker)
    if not hist:
        return None
        
    # Find the closest date on or after target_date
    for day in hist:
        if day["date"] >= target_date:
            return float(day["close"])
            
    # If target_date is in the future relative to our data, return the latest price
    return float(hist[-1]["close"])
