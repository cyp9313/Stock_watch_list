"""Ticker naming helpers for yfinance and StockAnalysis.com."""

import re


PSEUDO_TICKERS = {"20MA_Ratio", "50MA_Ratio", "200MA_Ratio"}
SKIP_PREFIXES = ("^",)
SKIP_SUFFIXES = ("=F", "=X")
SKIP_EXACT = {"BTC-USD", "ETH-USD"}

YF_TO_STOCKANALYSIS_EXCHANGE = {
    ".HK": "hkg",
    ".SS": "sha",
    ".SZ": "she",
    ".DE": "etr",
    ".F": "fra",
    ".PA": "epa",
    ".AS": "ams",
    ".BR": "ebr",
    ".MI": "bit",
    ".L": "lon",
    ".SW": "swx",
    ".ST": "sto",
    ".CO": "cph",
    ".HE": "hel",
    ".OL": "osl",
    ".MC": "bme",
    ".LS": "eli",
}

STOCKANALYSIS_TO_YF_EXCHANGE = {
    "hkg": ".HK",
    "sha": ".SS",
    "she": ".SZ",
    "etr": ".DE",
    "fra": ".F",
    "epa": ".PA",
    "ams": ".AS",
    "ebr": ".BR",
    "bit": ".MI",
    "lon": ".L",
    "swx": ".SW",
    "sto": ".ST",
    "cph": ".CO",
    "hel": ".HE",
    "osl": ".OL",
    "bme": ".MC",
    "eli": ".LS",
}

US_ETF_TICKERS = {
    "ARKK", "BITO", "DIA", "EEM", "EFA", "GLD", "HYG", "IBIT", "IEF",
    "IWM", "IVV", "LQD", "QQQ", "QQQM", "SHY", "SLV", "SMH", "SOXX",
    "SPY", "TLT", "TQQQ", "UNG", "USO", "VEA", "VTI", "VOO", "VT",
    "VWO", "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU",
    "XLV", "XLY",
}


def normalize_yfinance_ticker(ticker):
    """Return the yfinance-style ticker used internally by the app."""
    if ticker is None:
        return ""

    t = str(ticker).strip()
    if not t:
        return ""
    if t in PSEUDO_TICKERS:
        return t

    if ":" in t:
        exchange, symbol = t.split(":", 1)
        exchange = exchange.strip().lower()
        symbol = symbol.strip().upper()
        suffix = STOCKANALYSIS_TO_YF_EXCHANGE.get(exchange)
        if suffix:
            return _format_yfinance_symbol(symbol, suffix)

    t = t.upper()
    for suffix in YF_TO_STOCKANALYSIS_EXCHANGE:
        if t.endswith(suffix):
            symbol = t[:-len(suffix)]
            return _format_yfinance_symbol(symbol, suffix)

    return t


def _format_yfinance_symbol(symbol, suffix):
    symbol = symbol.strip().upper()
    if suffix == ".HK" and symbol.isdigit():
        symbol = symbol.zfill(4)
    elif suffix in (".SS", ".SZ") and symbol.isdigit():
        symbol = symbol.zfill(6)
    return f"{symbol}{suffix}"


def stockanalysis_symbol(ticker):
    """Return (exchange_slug, symbol) for StockAnalysis, or (None, symbol) for US stocks."""
    yf_ticker = normalize_yfinance_ticker(ticker)
    if not yf_ticker:
        return None, ""

    for suffix, exchange_slug in YF_TO_STOCKANALYSIS_EXCHANGE.items():
        if yf_ticker.endswith(suffix):
            symbol = yf_ticker[:-len(suffix)]
            if suffix == ".HK" and symbol.isdigit():
                symbol = symbol.zfill(4)
            elif suffix in (".SS", ".SZ") and symbol.isdigit():
                symbol = symbol.zfill(6)
            return exchange_slug, symbol

    return None, yf_ticker.replace("-", ".").lower()


def stockanalysis_path(ticker, page=""):
    """Build a StockAnalysis path without host, e.g. stocks/aapl/statistics/."""
    yf_ticker = normalize_yfinance_ticker(ticker)
    if not should_query_stockanalysis(yf_ticker):
        return None

    exchange_slug, symbol = stockanalysis_symbol(yf_ticker)
    page = page.strip("/")
    suffix = f"/{page}" if page else ""

    if exchange_slug:
        return f"quote/{exchange_slug}/{symbol}{suffix}/"
    if is_known_us_etf(yf_ticker):
        if page:
            return None
        return f"etf/{symbol}/"
    return f"stocks/{symbol}{suffix}/"


def stockanalysis_url(ticker, page=""):
    path = stockanalysis_path(ticker, page=page)
    if not path:
        return None
    return f"https://stockanalysis.com/{path}"


def stockanalysis_statistics_url(ticker):
    return stockanalysis_url(ticker, page="statistics")


def stockanalysis_overview_url(ticker):
    return stockanalysis_url(ticker)


def stockanalysis_etf_url(ticker):
    yf_ticker = normalize_yfinance_ticker(ticker)
    if not should_query_stockanalysis(yf_ticker):
        return None
    exchange_slug, symbol = stockanalysis_symbol(yf_ticker)
    if exchange_slug:
        return None
    return f"https://stockanalysis.com/etf/{symbol}/"


def stockanalysis_candidate_urls(ticker):
    """Return StockAnalysis pages to try, ordered from most specific to fallback."""
    yf_ticker = normalize_yfinance_ticker(ticker)
    if not should_query_stockanalysis(yf_ticker):
        return []

    urls = []
    if is_known_us_etf(yf_ticker):
        urls.append(stockanalysis_etf_url(yf_ticker))
    else:
        urls.append(stockanalysis_statistics_url(yf_ticker))
        urls.append(stockanalysis_overview_url(yf_ticker))
        exchange_slug, _ = stockanalysis_symbol(yf_ticker)
        if not exchange_slug:
            urls.append(stockanalysis_etf_url(yf_ticker))

    return [url for url in dict.fromkeys(urls) if url]


def is_known_us_etf(ticker):
    yf_ticker = normalize_yfinance_ticker(ticker)
    return yf_ticker in US_ETF_TICKERS


def should_query_stockanalysis(ticker):
    yf_ticker = normalize_yfinance_ticker(ticker)
    if not yf_ticker or yf_ticker in PSEUDO_TICKERS:
        return False
    if yf_ticker in SKIP_EXACT:
        return False
    if yf_ticker.startswith(SKIP_PREFIXES):
        return False
    if yf_ticker.endswith(SKIP_SUFFIXES):
        return False
    if re.fullmatch(r"[A-Z]{6}=X", yf_ticker):
        return False
    return True
