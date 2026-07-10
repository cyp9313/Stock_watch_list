"""StockAnalysis.com scraper used by the app-level fundamentals cache.

V5.8 expands the valuation/risk fields but deliberately does not add peer-group
comparison. StockAnalysis is treated as a best-effort source; callers must audit
missing fields and may fall back to yfinance only where explicitly allowed.
"""

import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from ticker_mapping import (
    normalize_yfinance_ticker,
    should_query_stockanalysis,
    stockanalysis_candidate_urls,
)

MAX_WORKERS = 5
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/116.0 Safari/537.36"
}

RESULT_KEYS = (
    "forward_pe", "peg_ratio", "trailing_pe", "market_cap", "earnings_date",
    "ps_ratio", "pb_ratio", "analyst_rating", "price_target",
    "ev_sales", "ev_ebitda", "ev_fcf", "p_fcf", "p_ocf", "forward_ps",
    "fcf_yield", "debt_equity", "debt_ebitda", "debt_fcf", "interest_coverage",
)


def should_query_forward_pe(ticker):
    return should_query_stockanalysis(ticker)


def empty_result(raw):
    result = {key: None for key in RESULT_KEYS}
    result["raw"] = raw
    return result


def clean_text(value):
    if value is None:
        return ""
    value = re.sub(r"<[^>]+>", "", str(value))
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def parse_float(value_str):
    value_str = clean_text(value_str)
    if not value_str or value_str.lower() in {"n/a", "na", "-", "—"}:
        return None
    match = re.search(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?|[-+]?\d+(?:\.\d+)?", value_str)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_market_cap(value_str):
    value_str = clean_text(value_str)
    if not value_str or value_str.lower() in {"n/a", "na", "-", "—"}:
        return None
    try:
        match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*([TtBbMmKk]?)", value_str.replace(",", ""))
        if not match:
            return None
        value = float(match.group(1))
        suffix = match.group(2).upper()
        return value * {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}.get(suffix, 1.0)
    except (ValueError, TypeError):
        return None


def extract_js_value(text, title):
    patterns = [
        rf'{re.escape(title)}",value:"([^"]+)"',
        rf'"title":"{re.escape(title)}"[^}}]*?"value":"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(1))
    return None


def extract_table_value(text, label):
    pattern = (
        rf'<(?:td|th)[^>]*>\s*{re.escape(label)}\s*</(?:td|th)>\s*'
        rf'<td[^>]*>(.*?)</td>'
    )
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    return clean_text(match.group(1)) if match else None


def extract_first(text, aliases):
    for label in aliases:
        value = extract_js_value(text, label) or extract_table_value(text, label)
        if value:
            return value, label
    return None, None


def has_useful_data(data):
    return any(data.get(key) is not None for key in RESULT_KEYS)


FIELD_ALIASES = {
    "forward_pe": ("Forward PE", "Forward P/E"),
    "peg_ratio": ("PEG Ratio", "PEG"),
    "trailing_pe": ("PE Ratio", "P/E Ratio", "Trailing PE"),
    "market_cap": ("Market Cap", "Assets"),
    "earnings_date": ("Earnings Date",),
    "ps_ratio": ("PS Ratio", "P/S Ratio", "Price / Sales"),
    "pb_ratio": ("PB Ratio", "P/B Ratio", "Price / Book"),
    "analyst_rating": ("Analyst Consensus", "Analyst Rating"),
    "price_target": ("Price Target", "Average Price Target"),
    "ev_sales": ("EV / Sales", "EV/Sales", "Enterprise Value / Sales"),
    "ev_ebitda": ("EV / EBITDA", "EV/EBITDA", "Enterprise Value / EBITDA"),
    "ev_fcf": ("EV / FCF", "EV/FCF", "Enterprise Value / FCF"),
    "p_fcf": ("P / FCF", "P/FCF", "Price / FCF", "Price / Free Cash Flow"),
    "p_ocf": ("P / OCF", "P/OCF", "Price / Operating Cash Flow"),
    "forward_ps": ("Forward PS", "Forward P/S", "Forward Price / Sales"),
    "fcf_yield": ("FCF Yield", "Free Cash Flow Yield"),
    "debt_equity": ("Debt / Equity", "Debt/Equity", "Debt to Equity"),
    "debt_ebitda": ("Debt / EBITDA", "Debt/EBITDA"),
    "debt_fcf": ("Debt / FCF", "Debt/FCF"),
    "interest_coverage": ("Interest Coverage", "Interest Coverage Ratio"),
}


def parse_stockanalysis_page(text, source_url):
    result = empty_result("")
    raw_parts = [f"source={source_url}"]

    for key, aliases in FIELD_ALIASES.items():
        raw_value, matched_label = extract_first(text, aliases)
        if not raw_value:
            continue
        raw_parts.append(f"{key}={raw_value}")
        if key == "market_cap":
            result[key] = parse_market_cap(raw_value)
        elif key in {"earnings_date", "analyst_rating"}:
            if raw_value.lower() not in {"n/a", "na", "-", "—"}:
                result[key] = raw_value
        else:
            result[key] = parse_float(raw_value)

    # FCF yield is a percentage. Derive it from P/FCF when a direct field is absent.
    if result.get("fcf_yield") is None and result.get("p_fcf") not in {None, 0}:
        result["fcf_yield"] = 100.0 / float(result["p_fcf"])
        raw_parts.append(f"fcf_yield_derived={result['fcf_yield']:.4f}%")

    result["raw"] = ", ".join(raw_parts) if has_useful_data(result) else f"source={source_url}, not_found"
    return result


def scrape_stock_analysis(ticker):
    urls = stockanalysis_candidate_urls(ticker)
    if not urls:
        return empty_result("unsupported_ticker")

    failures = []
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
        except Exception as exc:
            failures.append(f"{url}: request_error: {exc}")
            continue
        if resp.status_code != 200:
            failures.append(f"{url}: http_{resp.status_code}")
            continue
        result = parse_stockanalysis_page(resp.text, url)
        if has_useful_data(result):
            return result
        failures.append(result["raw"])
    return empty_result("; ".join(failures) if failures else "not_found")


def scrape_batch(tickers):
    query_tickers = [
        t for t in list(dict.fromkeys(normalize_yfinance_ticker(t) for t in tickers))
        if should_query_forward_pe(t)
    ]
    if not query_tickers:
        return {}

    results = {}

    def _scrape_one(ticker):
        return ticker, scrape_stock_analysis(ticker)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_scrape_one, ticker): ticker for ticker in query_tickers}
        for future in as_completed(futures):
            ticker, data = future.result()
            results[ticker] = data
            compact = ", ".join(f"{k}={data.get(k)}" for k in RESULT_KEYS if data.get(k) is not None)
            print(f"[StockAnalysis] {ticker}: {compact or 'N/A'} (raw: {data['raw']})")
    return results


if __name__ == "__main__":
    test_tickers = ["AAPL", "MSFT", "QQQ", "SPY", "2800.HK", "510300.SS", "BRK-B"]
    result = scrape_batch(test_tickers)
    print("\n=== Test result ===")
    for ticker, data in result.items():
        print(ticker, data)
