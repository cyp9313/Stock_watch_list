"""StockAnalysis.com scraper used by the app-level fundamentals cache."""

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
    "forward_pe",
    "peg_ratio",
    "trailing_pe",
    "market_cap",
    "earnings_date",
    "ps_ratio",
    "pb_ratio",
    "analyst_rating",
    "price_target",
)


def should_query_forward_pe(ticker):
    """Return True when StockAnalysis is a suitable source for this ticker."""
    return should_query_stockanalysis(ticker)


def empty_result(raw):
    return {
        "forward_pe": None,
        "peg_ratio": None,
        "trailing_pe": None,
        "market_cap": None,
        "earnings_date": None,
        "ps_ratio": None,
        "pb_ratio": None,
        "analyst_rating": None,
        "price_target": None,
        "raw": raw,
    }


def clean_text(value):
    if value is None:
        return ""
    value = re.sub(r"<[^>]+>", "", str(value))
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def parse_float(value_str):
    value_str = clean_text(value_str)
    if not value_str or value_str.lower() == "n/a":
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
    if not value_str or value_str.lower() == "n/a":
        return None
    try:
        match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*([TtBbMmKk]?)", value_str.replace(",", ""))
        if not match:
            return None
        value = float(match.group(1))
        suffix = match.group(2).upper()
        if suffix == "T":
            return value * 1e12
        if suffix == "B":
            return value * 1e9
        if suffix == "M":
            return value * 1e6
        if suffix == "K":
            return value * 1e3
        return value
    except (ValueError, TypeError):
        return None


def extract_js_value(text, title):
    match = re.search(rf'{re.escape(title)}",value:"([^"]+)"', text)
    return match.group(1) if match else None


def extract_table_value(text, label):
    pattern = (
        rf'<(?:td|th)[^>]*>\s*{re.escape(label)}\s*</(?:td|th)>\s*'
        rf'<td[^>]*>(.*?)</td>'
    )
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    return clean_text(match.group(1)) if match else None


def has_useful_data(data):
    return any(data.get(key) is not None for key in RESULT_KEYS)


def parse_stockanalysis_page(text, source_url):
    result = empty_result("")
    raw_parts = [f"source={source_url}"]

    # Stock statistics pages use embedded JS data. ETF and many non-US overview
    # pages expose the relevant data in the visible two-column overview table.
    forward_pe_raw = extract_js_value(text, "Forward PE") or extract_table_value(text, "Forward PE")
    if forward_pe_raw:
        raw_parts.append(f"pe={forward_pe_raw}")
        result["forward_pe"] = parse_float(forward_pe_raw)

    peg_raw = extract_js_value(text, "PEG Ratio") or extract_table_value(text, "PEG Ratio")
    if peg_raw:
        raw_parts.append(f"peg={peg_raw}")
        result["peg_ratio"] = parse_float(peg_raw)

    trailing_pe_raw = extract_js_value(text, "PE Ratio") or extract_table_value(text, "PE Ratio")
    if trailing_pe_raw:
        raw_parts.append(f"trail_pe={trailing_pe_raw}")
        result["trailing_pe"] = parse_float(trailing_pe_raw)

    market_cap_raw = (
        extract_js_value(text, "Market Cap")
        or extract_table_value(text, "Market Cap")
        or extract_table_value(text, "Assets")
    )
    if market_cap_raw:
        raw_parts.append(f"mcap={market_cap_raw}")
        result["market_cap"] = parse_market_cap(market_cap_raw)

    earnings_raw = extract_js_value(text, "Earnings Date") or extract_table_value(text, "Earnings Date")
    if earnings_raw:
        raw_parts.append(f"earnings={earnings_raw}")
        if earnings_raw.lower() != "n/a":
            result["earnings_date"] = earnings_raw

    ps_raw = extract_js_value(text, "PS Ratio") or extract_table_value(text, "PS Ratio")
    if ps_raw:
        raw_parts.append(f"ps={ps_raw}")
        result["ps_ratio"] = parse_float(ps_raw)

    pb_raw = extract_js_value(text, "PB Ratio") or extract_table_value(text, "PB Ratio")
    if pb_raw:
        raw_parts.append(f"pb={pb_raw}")
        result["pb_ratio"] = parse_float(pb_raw)

    rating_raw = extract_js_value(text, "Analyst Consensus") or extract_table_value(text, "Analyst Consensus")
    if rating_raw:
        raw_parts.append(f"rating={rating_raw}")
        if rating_raw.lower() != "n/a":
            result["analyst_rating"] = rating_raw

    target_raw = extract_js_value(text, "Price Target") or extract_table_value(text, "Price Target")
    if target_raw:
        raw_parts.append(f"target={target_raw}")
        result["price_target"] = parse_float(target_raw)

    result["raw"] = ", ".join(raw_parts) if has_useful_data(result) else f"source={source_url}, not_found"
    return result


def scrape_stock_analysis(ticker):
    """
    Fetch one ticker's StockAnalysis fundamentals.

    Stocks try the statistics page first. ETF and instruments without a
    statistics page fall back to overview pages, where PE Ratio is the trailing
    PE used by the chart title and watch-list table.
    """
    urls = stockanalysis_candidate_urls(ticker)
    if not urls:
        return empty_result("unsupported_ticker")

    failures = []
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
        except Exception as e:
            failures.append(f"{url}: request_error: {e}")
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
    """
    Concurrently fetch StockAnalysis data.

    Returns {ticker: data} using normalized yfinance tickers as keys.
    """
    query_tickers = [
        t for t in list(dict.fromkeys(normalize_yfinance_ticker(t) for t in tickers))
        if should_query_forward_pe(t)
    ]
    if not query_tickers:
        return {}

    results = {}

    def _scrape_one(ticker):
        data = scrape_stock_analysis(ticker)
        return ticker, data

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_scrape_one, t): t for t in query_tickers}
        for future in as_completed(futures):
            ticker, data = future.result()
            results[ticker] = data
            pe_str = f"{data['forward_pe']}" if data["forward_pe"] is not None else "N/A"
            peg_str = f"{data['peg_ratio']}" if data["peg_ratio"] is not None else "N/A"
            trail_str = f"{data['trailing_pe']}" if data["trailing_pe"] is not None else "N/A"
            mcap_str = f"{data['market_cap']}" if data["market_cap"] is not None else "N/A"
            ed_str = data["earnings_date"] or "N/A"
            ps_str = f"{data['ps_ratio']}" if data["ps_ratio"] is not None else "N/A"
            pb_str = f"{data['pb_ratio']}" if data["pb_ratio"] is not None else "N/A"
            rating_str = data["analyst_rating"] or "N/A"
            target_str = f"${data['price_target']}" if data["price_target"] is not None else "N/A"
            print(
                f"[StockAnalysis] {ticker}: PE={pe_str}, PEG={peg_str}, "
                f"TrailPE={trail_str}, MCap={mcap_str}, Earnings={ed_str}, "
                f"PS={ps_str}, PB={pb_str}, Rating={rating_str}, Target={target_str} "
                f"(raw: {data['raw']})"
            )

    return results


if __name__ == "__main__":
    test_tickers = ["AAPL", "MSFT", "QQQ", "SPY", "2800.HK", "510300.SS", "BRK-B"]
    result = scrape_batch(test_tickers)
    print("\n=== Test result ===")
    for t, data in result.items():
        mcap = data["market_cap"]
        mcap_display = f"{mcap:,.0f}" if mcap else "N/A"
        print(
            f"  {t}: Forward PE={data['forward_pe']}, PEG Ratio={data['peg_ratio']}, "
            f"Trailing PE={data['trailing_pe']}, MCap={mcap_display}, "
            f"Earnings={data['earnings_date']}, PS={data['ps_ratio']}, "
            f"PB={data['pb_ratio']}, Analysts={data['analyst_rating']}, "
            f"Price Target={data['price_target']}"
        )
