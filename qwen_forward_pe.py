"""
StockAnalysis 数据爬取模块
- 直接爬取 StockAnalysis.com statistics 页面提取 Forward PE、分析师评级、Price Target
- SQLite 缓存：当天已有数据直接读取，避免重复请求
- 并发请求（ThreadPoolExecutor）提高速度
"""

import sqlite3
import re
import pytz
import datetime
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===== 配置 =====
DB_PATH = "forward_pe_cache.db"
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


def init_db():
    """初始化 SQLite 数据库，返回连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forward_pe (
            ticker          TEXT    NOT NULL,
            date            TEXT    NOT NULL,
            forward_pe      REAL,
            analyst_rating  TEXT,
            price_target    REAL,
            raw_answer      TEXT,
            created_at      TEXT    DEFAULT (datetime('now')),
            PRIMARY KEY (ticker, date)
        )
    """)
    # 兼容旧表：如果表已存在但缺少新列，自动添加
    cursor = conn.execute("PRAGMA table_info(forward_pe)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if 'analyst_rating' not in existing_cols:
        conn.execute("ALTER TABLE forward_pe ADD COLUMN analyst_rating TEXT")
    if 'price_target' not in existing_cols:
        conn.execute("ALTER TABLE forward_pe ADD COLUMN price_target REAL")
    conn.commit()
    return conn


def get_market_date():
    """获取美东时间当前日期 (YYYY-MM-DD)"""
    ny = pytz.timezone('America/New_York')
    return datetime.datetime.now(ny).strftime('%Y-%m-%d')


def scrape_stock_analysis(ticker):
    """
    从 StockAnalysis.com 抓取单只股票的 Forward PE、分析师评级、Price Target
    返回: dict {
        "forward_pe": float or None,
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
        return {"forward_pe": None, "analyst_rating": None, "price_target": None,
                "raw": f"request_error: {e}"}

    if resp.status_code != 200:
        return {"forward_pe": None, "analyst_rating": None, "price_target": None,
                "raw": f"http_{resp.status_code}"}

    text = resp.text
    result = {"forward_pe": None, "analyst_rating": None, "price_target": None, "raw": ""}
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


def get_batch_forward_pe(all_tickers):
    """
    主入口：带 SQLite 缓存的批量查询
    1. 先查缓存，命中直接返回
    2. 未命中的并发爬取 StockAnalysis.com
    3. 查到后写入缓存

    参数: all_tickers - 需要查询的 ticker 列表
    返回: dict {ticker: {"forward_pe": float/None, "analyst_rating": str/None, "price_target": float/None}}
    """
    query_tickers = [t for t in all_tickers if should_query_forward_pe(t)]

    if not query_tickers:
        return {}

    conn = init_db()
    today = get_market_date()
    results = {}
    missing = []

    # 1. 查缓存
    for t in query_tickers:
        row = conn.execute(
            "SELECT forward_pe, analyst_rating, price_target FROM forward_pe WHERE ticker=? AND date=?",
            (t, today),
        ).fetchone()
        if row:
            results[t] = {
                "forward_pe": row[0],
                "analyst_rating": row[1],
                "price_target": row[2],
            }
        else:
            missing.append(t)

    if not missing:
        conn.close()
        return results

    print(f"[StockAnalysis] 缓存命中 {len(results)}/{len(query_tickers)}，"
          f"需查询 {len(missing)} 只: {missing}")

    # 2. 并发爬取 StockAnalysis
    def _scrape_one(ticker):
        data = scrape_stock_analysis(ticker)
        return ticker, data

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_scrape_one, t): t for t in missing}
        for future in as_completed(futures):
            ticker, data = future.result()
            results[ticker] = {
                "forward_pe": data["forward_pe"],
                "analyst_rating": data["analyst_rating"],
                "price_target": data["price_target"],
            }
            conn.execute(
                "INSERT OR REPLACE INTO forward_pe (ticker, date, forward_pe, analyst_rating, price_target, raw_answer) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ticker, today, data["forward_pe"], data["analyst_rating"],
                 data["price_target"], data["raw"]),
            )
            pe_str = f"{data['forward_pe']}" if data["forward_pe"] is not None else "N/A"
            rating_str = data["analyst_rating"] or "N/A"
            target_str = f"${data['price_target']}" if data["price_target"] is not None else "N/A"
            print(f"[StockAnalysis] {ticker}: PE={pe_str}, Rating={rating_str}, Target={target_str} (raw: {data['raw']})")

    conn.commit()
    conn.close()
    return results


def clear_today_cache():
    """清除当天的缓存，强制下次刷新时重新爬取"""
    conn = init_db()
    today = get_market_date()
    conn.execute("DELETE FROM forward_pe WHERE date=?", (today,))
    conn.commit()
    conn.close()
    print(f"[StockAnalysis] 已清除 {today} 的缓存")


if __name__ == "__main__":
    # 测试
    test_tickers = ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA",
                    "AMD", "INTC", "BRK-B", "BYDDY", "SPCX"]
    result = get_batch_forward_pe(test_tickers)
    print("\n=== 测试结果 ===")
    for t, data in result.items():
        pe = data["forward_pe"]
        rating = data["analyst_rating"]
        target = data["price_target"]
        print(f"  {t}: Forward PE={pe}, Analysts={rating}, Price Target={target}")
