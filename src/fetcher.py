"""
fetcher.py – Download and parse SEC EDGAR Form 4 filings.

Uses the EDGAR full-text search API (EFTS) to find recent Form 4 filings,
then downloads and parses the XML to extract insider transaction details.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any

import requests

from src.config import (
    SEC_ARCHIVES_URL,
    SEC_RATE_LIMIT_RPS,
    SEC_USER_AGENT,
)
from src.db import get_connection

logger = logging.getLogger(__name__)

# ── Rate limiter ──────────────────────────────────────────────────────────────
_last_request_time: float = 0.0


def _rate_limit() -> None:
    """Sleep if needed to stay under SEC rate limit."""
    global _last_request_time
    min_interval = 1.0 / SEC_RATE_LIMIT_RPS
    elapsed = time.time() - _last_request_time
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_request_time = time.time()


def _get(url: str, **kwargs: Any) -> requests.Response:
    """GET with rate limiting and proper User-Agent."""
    _rate_limit()
    headers = kwargs.pop("headers", {})
    headers["User-Agent"] = SEC_USER_AGENT
    headers["Accept-Encoding"] = "gzip, deflate"
    resp = requests.get(url, headers=headers, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp


# ── EDGAR full-text search ────────────────────────────────────────────────────
EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"


def fetch_recent_form4_urls(days_back: int = 3, max_results: int = 200) -> list[dict[str, str]]:
    """
    Use the EDGAR full-text search API to find recent Form 4 filings.
    Returns list of dicts with 'accession_no' and 'filing_url' keys.
    """
    # Use the EDGAR full-text search API
    date_from = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = datetime.utcnow().strftime("%Y-%m-%d")

    url = "https://efts.sec.gov/LATEST/search-index"
    params = {
        "q": '"form 4"',
        "dateRange": "custom",
        "startdt": date_from,
        "enddt": date_to,
        "forms": "4",
        "from": 0,
        "size": max_results,
    }

    # Fallback: use the EDGAR RSS feed for recent Form 4 filings
    rss_url = "https://www.sec.gov/cgi-bin/browse-edgar"
    rss_params = {
        "action": "getcurrent",
        "type": "4",
        "dateb": "",
        "owner": "include",
        "count": str(min(max_results, 100)),
        "search_text": "",
        "start": "0",
        "output": "atom",
    }

    results: list[dict[str, str]] = []

    try:
        resp = _get(rss_url, params=rss_params)
        # Parse Atom XML feed
        root = ET.fromstring(resp.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        for entry in root.findall("atom:entry", ns):
            cat_el = entry.find("atom:category", ns)
            if cat_el is None or cat_el.get("term") not in ("4", "4/A"):
                continue

            link_el = entry.find("atom:link", ns)
            title_el = entry.find("atom:title", ns)
            if link_el is None:
                continue

            href = link_el.get("href", "")
            # Extract accession number from URL
            # URL format: https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/...
            parts = href.split("/")
            if len(parts) >= 7:
                accession_raw = parts[-2] if parts[-1].endswith(".txt") else parts[-1]
                accession_no = accession_raw.replace("-", "")
                results.append({
                    "accession_no": accession_raw,
                    "filing_url": href,
                })
        logger.info("Found %d Form 4 filings from RSS feed", len(results))

    except Exception:
        logger.exception("Failed to fetch Form 4 filings from EDGAR RSS")
        # Try alternative: EDGAR full-text search
        try:
            search_url = "https://efts.sec.gov/LATEST/search-index"
            search_params = {
                "q": "",
                "forms": "4",
                "dateRange": "custom",
                "startdt": date_from,
                "enddt": date_to,
            }
            resp = _get(search_url, params=search_params)
            data = resp.json()
            for hit in data.get("hits", {}).get("hits", []):
                source = hit.get("_source", {})
                accession = source.get("file_num", "")
                file_url = source.get("file_url", "")
                if accession and file_url:
                    results.append({
                        "accession_no": accession,
                        "filing_url": file_url,
                    })
            logger.info("Found %d Form 4 filings from EFTS", len(results))
        except Exception:
            logger.exception("EFTS search also failed")

    return results


def _find_xml_document_url(index_url: str) -> str | None:
    """Given an EDGAR filing index URL, find the primary XML document URL."""
    try:
        # Convert index URL to the filing index page
        # e.g., https://www.sec.gov/Archives/edgar/data/320193/000032019324000077/0000320193-24-000077-index.htm
        resp = _get(index_url)
        text = resp.text

        # Look for the primary XML document (the Form 4 XML)
        # It's typically named something like wf-form4_*.xml or xslForm4X01/*.xml
        import re

        # Find XML document links in the filing index
        xml_links = re.findall(r'href="([^"]*\.xml)"', text, re.IGNORECASE)
        for link in xml_links:
            # Skip XBRL and other non-form4 XML files, also skip xsl rendered wrappers
            if "xbrl" in link.lower() or "xsd" in link.lower() or "xsl" in link.lower():
                continue
            # Build full URL
            if link.startswith("http"):
                return link
            elif link.startswith("/"):
                return f"https://www.sec.gov{link}"
            else:
                # Relative URL
                base = index_url.rsplit("/", 1)[0]
                return f"{base}/{link}"

    except Exception:
        logger.exception("Failed to find XML document URL from %s", index_url)

    return None


def parse_form4_xml(xml_text: str) -> list[dict[str, Any]]:
    """
    Parse a Form 4 XML document and extract transaction details.
    Returns list of transaction dicts.

    SEC Form 4 XML schema reference:
    https://www.sec.gov/files/form4.xsd
    """
    transactions: list[dict[str, Any]] = []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Failed to parse XML")
        return transactions

    # ── Issuer info ────────────────────────────────────────────────────────
    issuer = root.find(".//issuer")
    ticker = ""
    if issuer is not None:
        ticker_el = issuer.find("issuerTradingSymbol")
        if ticker_el is not None and ticker_el.text:
            ticker = ticker_el.text.strip().upper()

    if not ticker:
        logger.debug("No ticker found in Form 4 XML, skipping")
        return transactions

    # ── Reporting owner info ───────────────────────────────────────────────
    owner = root.find(".//reportingOwner")
    if owner is None:
        return transactions

    owner_id = owner.find(".//rptOwnerCik")
    owner_name_el = owner.find(".//rptOwnerName")
    cik = owner_id.text.strip() if owner_id is not None and owner_id.text else ""
    insider_name = owner_name_el.text.strip() if owner_name_el is not None and owner_name_el.text else "Unknown"

    # Determine title/role
    relationship = owner.find(".//reportingOwnerRelationship")
    insider_title = "Other"
    if relationship is not None:
        if _text_true(relationship, "isOfficer"):
            title_el = relationship.find("officerTitle")
            if title_el is not None and title_el.text:
                insider_title = title_el.text.strip()
            else:
                insider_title = "Officer"
        elif _text_true(relationship, "isDirector"):
            insider_title = "Director"
        elif _text_true(relationship, "isTenPercentOwner"):
            insider_title = "10% Owner"

    # ── Non-derivative transactions ────────────────────────────────────────
    for tx in root.findall(".//nonDerivativeTransaction"):
        parsed = _parse_transaction_element(tx, ticker, cik, insider_name, insider_title)
        if parsed:
            transactions.append(parsed)

    # ── Derivative transactions (optional, less common for buy signals) ────
    # We skip derivatives for simplicity as they're harder to interpret

    return transactions


def _text_true(parent: ET.Element, tag: str) -> bool:
    el = parent.find(tag)
    return el is not None and el.text is not None and el.text.strip() in ("1", "true", "True")


def _parse_transaction_element(
    tx: ET.Element,
    ticker: str,
    cik: str,
    insider_name: str,
    insider_title: str,
) -> dict[str, Any] | None:
    """Parse a single nonDerivativeTransaction element."""
    # Transaction coding
    coding = tx.find(".//transactionCoding")
    if coding is None:
        return None
    code_el = coding.find("transactionCode")
    if code_el is None or not code_el.text:
        return None
    tx_code = code_el.text.strip()

    # We primarily care about P (open-market purchase) and S (sale)
    if tx_code not in ("P", "S", "A", "D", "F", "M"):
        return None

    # 10b5-1 plan flag
    form_type_el = coding.find("equitySwapInvolved")
    is_10b5_1 = False
    plan_el = coding.find("transactionFormType")
    # Check footnotes for 10b5-1 mentions (crude heuristic)
    footnotes = tx.findall(".//footnote")
    for fn in footnotes:
        if fn.text and "10b5-1" in fn.text.lower():
            is_10b5_1 = True
            break

    # Transaction amounts
    amounts = tx.find(".//transactionAmounts")
    if amounts is None:
        return None

    shares_el = amounts.find(".//transactionShares/value")
    price_el = amounts.find(".//transactionPricePerShare/value")

    shares = float(shares_el.text) if shares_el is not None and shares_el.text else 0.0
    price = float(price_el.text) if price_el is not None and price_el.text else 0.0

    if shares <= 0 or price <= 0:
        return None

    # Transaction date
    date_el = tx.find(".//transactionDate/value")
    trade_date = date_el.text.strip() if date_el is not None and date_el.text else ""
    if not trade_date:
        return None

    # Shares owned after
    post_el = tx.find(".//postTransactionAmounts/sharesOwnedFollowingTransaction/value")
    shares_after = float(post_el.text) if post_el is not None and post_el.text else None

    return {
        "trade_date": trade_date,
        "ticker": ticker,
        "cik": cik,
        "insider_name": insider_name,
        "insider_title": insider_title,
        "tx_code": tx_code,
        "shares": shares,
        "price": price,
        "value": round(shares * price, 2),
        "shares_owned_after": shares_after,
        "is_10b5_1": int(is_10b5_1),
    }


def _store_transactions(transactions: list[dict[str, Any]], accession_no: str, filed_date: str) -> int:
    """Store parsed transactions in the database. Returns number of new rows inserted."""
    conn = get_connection()
    inserted = 0
    try:
        for tx in transactions:
            # Use accession_no + ticker + cik + trade_date as uniqueness key
            unique_key = f"{accession_no}_{tx['ticker']}_{tx['cik']}_{tx['trade_date']}_{tx['tx_code']}_{tx['shares']}"
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO insider_transactions
                    (accession_no, filed_date, trade_date, ticker, cik, insider_name,
                     insider_title, tx_code, shares, price, value, shares_owned_after, is_10b5_1)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        unique_key,
                        filed_date,
                        tx["trade_date"],
                        tx["ticker"],
                        tx["cik"],
                        tx["insider_name"],
                        tx["insider_title"],
                        tx["tx_code"],
                        tx["shares"],
                        tx["price"],
                        tx["value"],
                        tx["shares_owned_after"],
                        tx["is_10b5_1"],
                    ),
                )
                if conn.total_changes > 0:
                    inserted += 1
            except Exception:
                logger.debug("Duplicate or error for %s", unique_key)
        conn.commit()
    finally:
        conn.close()
    return inserted


def run(days_back: int = 3) -> int:
    """
    Main entry point: fetch recent Form 4 filings, parse them, store transactions.
    Returns total number of new transactions stored.
    """
    logger.info("Fetching recent Form 4 filings (last %d days)...", days_back)

    filings = fetch_recent_form4_urls(days_back=days_back)
    logger.info("Found %d filing URLs to process", len(filings))

    total_new = 0
    processed = 0

    for filing in filings:
        accession_no = filing["accession_no"]
        filing_url = filing["filing_url"]

        try:
            # Try to get the XML document
            xml_url = _find_xml_document_url(filing_url)
            if xml_url is None:
                # Try directly if the URL already points to XML
                resp = _get(filing_url)
                if "<?xml" in resp.text[:200] or "<ownershipDocument" in resp.text[:500]:
                    xml_text = resp.text
                else:
                    logger.debug("No XML found for %s", accession_no)
                    continue
            else:
                resp = _get(xml_url)
                xml_text = resp.text

            transactions = parse_form4_xml(xml_text)
            if transactions:
                filed_date = datetime.utcnow().strftime("%Y-%m-%d")
                new = _store_transactions(transactions, accession_no, filed_date)
                total_new += new

            processed += 1
            if processed % 20 == 0:
                logger.info("Processed %d/%d filings, %d new transactions so far", processed, len(filings), total_new)

        except requests.RequestException as e:
            logger.warning("HTTP error fetching %s: %s", filing_url, e)
        except Exception:
            logger.exception("Error processing filing %s", accession_no)

    logger.info("Fetcher complete: processed %d filings, stored %d new transactions", processed, total_new)
    return total_new
