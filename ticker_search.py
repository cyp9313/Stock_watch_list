"""Safe wrappers around Yahoo Finance symbol search results."""

from __future__ import annotations

import re

from ticker_mapping import normalize_yfinance_ticker


MAX_CANDIDATES = 8
_TICKER_PATTERN = re.compile(r"^[A-Za-z0-9.^=\-]+$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _text(value, max_length=160):
    return _CONTROL_CHARS.sub(" ", str(value or "")).strip()[:max_length]


def search_candidates_from_quotes(quotes):
    """Validate and sanitize untrusted Yahoo quote-search results."""
    candidates, seen = [], set()
    for quote in quotes if isinstance(quotes, list) else []:
        if not isinstance(quote, dict):
            continue
        raw_ticker = str(quote.get("symbol") or "").strip()
        if not _TICKER_PATTERN.fullmatch(raw_ticker):
            continue
        ticker = normalize_yfinance_ticker(raw_ticker)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        candidates.append({
            "ticker": ticker,
            "name": _text(quote.get("longname") or quote.get("shortname") or ticker),
            "exchange": _text(quote.get("exchDisp") or quote.get("exchange") or "Unknown", 80),
            "quote_type": _text(quote.get("typeDisp") or quote.get("quoteType") or "Unknown", 40),
        })
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates


def search_yfinance_candidates(query):
    """Search Yahoo Finance by company name, ISIN, or ticker."""
    query = _text(query, 120)
    if not query:
        return []
    import yfinance as yf
    result = yf.Search(
        query, max_results=MAX_CANDIDATES, news_count=0, lists_count=0,
        include_cb=False, include_nav_links=False, include_research=False,
        enable_fuzzy_query=False, recommended=0, timeout=10, raise_errors=False,
    )
    return search_candidates_from_quotes(result.quotes)
