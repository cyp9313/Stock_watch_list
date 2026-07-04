"""
StockAnalysis.com 数据爬取模块（纯爬取，无缓存）
- 直接爬取 StockAnalysis.com statistics 页面提取 Forward PE、PEG Ratio、Trailing PE、Market Cap、Earnings Date、P/S Ratio、P/B Ratio、分析师评级、Price Target
- 并发请求（ThreadPoolExecutor）提高速度
- 缓存逻辑已移至 stock_watch_list_back_end.py
"""

import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===== 配置 =====
MAX_WORKERS = 5
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/116.0 Safari/537.36"
}

# 不需要查询的标的（指数、商品、加密货币、汇率等）
SKIP_PREFIXES = ('^', 'BTC', 'ETH', 'GC=', 'SI=', 'BZ=', 'EUR', 'CL=')
SKIP_TICKERS = {'20MA_Ratio', '50MA_Ratio', '200MA_Ratio'}


def should_query_forward_pe(ticker):
    """判断该 ticker 是否需要查询"""
    if ticker in SKIP_TICKERS:
        return False
    if any(ticker.startswith(p) for p in SKIP_PREFIXES):
        return False
    return True


def parse_market_cap(value_str):
    """解析 Market Cap 字符串如 '4.53T', '109.90B', '493.75M' 为浮点数"""
    if not value_str or value_str in ("n/a", "", "N/A"):
        return None
    try:
        value_str = value_str.strip()
        if value_str.endswith('T'):
            return float(value_str[:-1]) * 1e12
        elif value_str.endswith('B'):
            return float(value_str[:-1]) * 1e9
        elif value_str.endswith('M'):
            return float(value_str[:-1]) * 1e6
        else:
            return float(value_str.replace(",", ""))
    except (ValueError, TypeError):
        return None


def scrape_stock_analysis(ticker):
    """
    从 StockAnalysis.com 抓取单只股票的 Forward PE、PEG Ratio、Trailing PE、Market Cap、Earnings Date、P/S Ratio、P/B Ratio、分析师评级、Price Target
    返回: dict {
        "forward_pe": float or None,
        "peg_ratio": float or None,
        "trailing_pe": float or None,
        "market_cap": float or None,
        "earnings_date": str or None,
        "ps_ratio": float or None,
        "pb_ratio": float or None,
        "analyst_rating": str or None,
        "price_target": float or None,
        "raw": str  (用于调试的原始值摘要)
    }
    """
    sa_ticker = ticker.replace('-', '.')
    url = f"https://stockanalysis.com/stocks/{sa_ticker}/statistics/"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        return {"forward_pe": None, "peg_ratio": None, "trailing_pe": None, "market_cap": None,
                "earnings_date": None, "ps_ratio": None, "pb_ratio": None,
                "analyst_rating": None, "price_target": None,
                "raw": f"request_error: {e}"}

    if resp.status_code != 200:
        return {"forward_pe": None, "peg_ratio": None, "trailing_pe": None, "market_cap": None,
                "earnings_date": None, "ps_ratio": None, "pb_ratio": None,
                "analyst_rating": None, "price_target": None,
                "raw": f"http_{resp.status_code}"}

    text = resp.text
    result = {"forward_pe": None, "peg_ratio": None, "trailing_pe": None, "market_cap": None,
              "earnings_date": None, "ps_ratio": None, "pb_ratio": None,
              "analyst_rating": None, "price_target": None, "raw": ""}
    raw_parts = []

    # --- Forward PE ---
    # 格式: Forward PE",value:"33.88" 或 Forward PE",value:"n/a"
    pe_match = re.search(r'Forward PE",value:"([^"]+)"', text)
    if pe_match:
        raw_pe = pe_match.group(1)
        raw_parts.append(f"pe={raw_pe}")
        if raw_pe not in ("n/a", "", "N/A"):
            try:
                result["forward_pe"] = float(raw_pe)
            except ValueError:
                pass

    # --- PEG Ratio ---
    # 格式: pegRatio",title:"PEG Ratio",value:"2.95" 或 value:"n/a"
    peg_match = re.search(r'pegRatio",title:"PEG Ratio",value:"([^"]+)"', text)
    if peg_match:
        raw_peg = peg_match.group(1)
        raw_parts.append(f"peg={raw_peg}")
        if raw_peg not in ("n/a", "", "N/A"):
            try:
                result["peg_ratio"] = float(raw_peg)
            except ValueError:
                pass

    # --- Trailing PE (PE Ratio) ---
    # 格式: pe",title:"PE Ratio",value:"37.41" 或 value:"n/a"
    # 注意：只用 id:"pe" (不含 Forward)，区别于 id:"peForward"
    trail_pe_match = re.search(r'pe",title:"PE Ratio",value:"([^"]+)"', text)
    if trail_pe_match:
        raw_trail_pe = trail_pe_match.group(1)
        raw_parts.append(f"trail_pe={raw_trail_pe}")
        if raw_trail_pe not in ("n/a", "", "N/A"):
            try:
                result["trailing_pe"] = float(raw_trail_pe)
            except ValueError:
                pass

    # --- Market Cap ---
    # 格式: marketcap",title:"Market Cap",value:"4.53T" 或 value:"n/a"
    mcap_match = re.search(r'marketcap",title:"Market Cap",value:"([^"]+)"', text)
    if mcap_match:
        raw_mcap = mcap_match.group(1)
        raw_parts.append(f"mcap={raw_mcap}")
        if raw_mcap not in ("n/a", "", "N/A"):
            result["market_cap"] = parse_market_cap(raw_mcap)

    # --- Earnings Date ---
    # 格式: earningsdate",title:"Earnings Date",value:"Jul 30, 2026" 或 value:"n/a"
    ed_match = re.search(r'earningsdate",title:"Earnings Date",value:"([^"]+)"', text)
    if ed_match:
        raw_ed = ed_match.group(1)
        raw_parts.append(f"earnings={raw_ed}")
        if raw_ed not in ("n/a", "", "N/A"):
            result["earnings_date"] = raw_ed

    # --- P/S Ratio ---
    # 格式: ps",title:"PS Ratio",value:"10.04" 或 value:"n/a"
    ps_match = re.search(r'ps",title:"PS Ratio",value:"([^"]+)"', text)
    if ps_match:
        raw_ps = ps_match.group(1)
        raw_parts.append(f"ps={raw_ps}")
        if raw_ps not in ("n/a", "", "N/A"):
            try:
                result["ps_ratio"] = float(raw_ps)
            except ValueError:
                pass

    # --- P/B Ratio ---
    # 格式: pb",title:"PB Ratio",value:"42.51" 或 value:"n/a"
    pb_match = re.search(r'pb",title:"PB Ratio",value:"([^"]+)"', text)
    if pb_match:
        raw_pb = pb_match.group(1)
        raw_parts.append(f"pb={raw_pb}")
        if raw_pb not in ("n/a", "", "N/A"):
            try:
                result["pb_ratio"] = float(raw_pb)
            except ValueError:
                pass

    # --- Analyst Consensus Rating ---
    # 格式: analystRatings",title:"Analyst Consensus",value:"Strong Buy"
    rating_match = re.search(r'analystRatings",title:"Analyst Consensus",value:"([^"]+)"', text)
    if rating_match:
        rating = rating_match.group(1)
        raw_parts.append(f"rating={rating}")
        if rating not in ("n/a", "", "N/A"):
            result["analyst_rating"] = rating

    # --- Price Target ---
    # 格式: priceTarget",title:"Price Target",value:"$315.09"
    target_match = re.search(r'priceTarget",title:"Price Target",value:"([^"]+)"', text)
    if target_match:
        target_str = target_match.group(1)
        raw_parts.append(f"target={target_str}")
        if target_str not in ("n/a", "", "N/A"):
            # 去掉 $ 符号
            target_clean = target_str.replace("$", "").replace(",", "")
            try:
                result["price_target"] = float(target_clean)
            except ValueError:
                pass

    result["raw"] = ", ".join(raw_parts) if raw_parts else "not_found"
    return result


def scrape_batch(tickers):
    """
    并发爬取多只股票的 StockAnalysis 数据（无缓存，纯爬取）
    返回: dict {ticker: {"forward_pe": float/None, "peg_ratio": float/None, "trailing_pe": float/None, "market_cap": float/None, "earnings_date": str/None, "ps_ratio": float/None, "pb_ratio": float/None, "analyst_rating": str/None, "price_target": float/None, "raw": str}}
    """
    query_tickers = [t for t in tickers if should_query_forward_pe(t)]
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
            print(f"[StockAnalysis] {ticker}: PE={pe_str}, PEG={peg_str}, TrailPE={trail_str}, MCap={mcap_str}, Earnings={ed_str}, PS={ps_str}, PB={pb_str}, Rating={rating_str}, Target={target_str} (raw: {data['raw']})")

    return results


if __name__ == "__main__":
    # 测试
    test_tickers = ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA",
                    "AMD", "INTC", "BRK-B", "BYDDY", "SPCX"]
    result = scrape_batch(test_tickers)
    print("\n=== 测试结果 ===")
    for t, data in result.items():
        pe = data["forward_pe"]
        peg = data["peg_ratio"]
        trail = data["trailing_pe"]
        mcap = data["market_cap"]
        ed = data["earnings_date"]
        ps = data["ps_ratio"]
        pb = data["pb_ratio"]
        rating = data["analyst_rating"]
        target = data["price_target"]
        mcap_display = f"{mcap:,.0f}" if mcap else "N/A"
        print(f"  {t}: Forward PE={pe}, PEG Ratio={peg}, Trailing PE={trail}, MCap={mcap_display}, Earnings={ed}, PS={ps}, PB={pb}, Analysts={rating}, Price Target={target}")
