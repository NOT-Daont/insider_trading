prices.py - Stock price fetching module using Finnhub, Yahoo API, and Stooq.

Uses Finnhub as the primary data source (if FINNHUB_API_KEY is set).
Falls back to Yahoo Finance API (works locally, often blocked on GH Actions),
and finally Stooq.
Includes in-memory caching to save API calls during a run.
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
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

    # 1. Try Finnhub (best free tier, works on GH Actions)
    history = _fetch_finnhub(ticker)

    # 2. Try Yahoo Finance (direct request, works locally, often blocked on GH Actions)
    if not history:
        history = _fetch_yahoo(ticker)

    # 3. Try Stooq
    if not history:
        history = _fetch_stooq(ticker)

    if history:
        # Sort ascending by date (oldest first)
        history.sort(key=lambda x: x["date"])
        _history_cache[ticker] = history
    else:
        logger.warning("Cannot get price for %s (all APIs failed). Set FINNHUB_API_KEY for reliability.", ticker)

    return history


def _fetch_finnhub(ticker: str) -> list[dict[str, float | str]]:
    """Fetch history from Finnhub API (60 req/min free tier)."""
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        return []

    # Get data for the last 365 days
    to_ts = int(datetime.utcnow().timestamp())
    from_ts = to_ts - (365 * 24 * 60 * 60)
    
    url = f"https://finnhub.io/api/v1/stock/candle?symbol={ticker.upper()}&resolution=D&from={from_ts}&to={to_ts}&token={api_key}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("s") != "ok":
            logger.debug("Finnhub returned no data for %s: %s", ticker, data.get("s"))
            return []

        history = []
        timestamps = data.get("t", [])
        closes = data.get("c", [])
        
        for i in range(min(len(timestamps), len(closes))):
            dt = datetime.utcfromtimestamp(timestamps[i]).strftime("%Y-%m-%d")
            history.append({
                "date": dt,
                "close": float(closes[i])
            })
            
        logger.debug("Fetched %d days of history for %s from Finnhub", len(history), ticker)
        return history
    except Exception as e:
        logger.debug("Finnhub fetch failed for %s: %s", ticker, e)
        return []


def _fetch_yahoo(ticker: str) -> list[dict[str, float | str]]:
    """Fetch history from Yahoo Finance direct API."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=1y&interval=1d"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        result = data.get("chart", {}).get("result", [])
        if not result:
            return []
            
        timestamps = result[0].get("timestamp", [])
        closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        
        history = []
        for i in range(min(len(timestamps), len(closes))):
            if closes[i] is not None:
                dt = datetime.utcfromtimestamp(timestamps[i]).strftime("%Y-%m-%d")
                history.append({
                    "date": dt,
                    "close": float(closes[i])
                })
                
        logger.debug("Fetched %d days of history for %s from Yahoo API", len(history), ticker)
        return history
    except Exception as e:
        logger.debug("Yahoo fetch failed for %s: %s", ticker, e)
        return []


def _fetch_stooq(ticker: str) -> list[dict[str, float | str]]:
    """Fetch history from Stooq CSV endpoint."""
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=10)
        resp.raise_for_status()

        # Stooq returns HTML asking for captcha/apikey if rate limited
        if "Date,Open,High,Low,Close,Volume" not in resp.text:
            logger.debug("Stooq format unexpected or requires captcha for %s", ticker)
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


def get_current_price(ticker: str) -> float | None:
    """Get the most recent closing price."""
    # 1. Fast path: Finnhub Quote API (works on free tier)
    api_key = os.environ.get("FINNHUB_API_KEY")
    if api_key:
        try:
            url = f"https://finnhub.io/api/v1/quote?symbol={ticker.upper()}&token={api_key}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("c", 0) > 0:
                    logger.debug("Fetched current price for %s from Finnhub Quote", ticker)
                    return float(data["c"])
        except Exception as e:
            logger.debug("Finnhub quote failed for %s: %s", ticker, e)

    # 2. Fallback to historical endpoints
    hist = get_history(ticker)
    if not hist:
        return None
    return float(hist[-1]["close"])


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
