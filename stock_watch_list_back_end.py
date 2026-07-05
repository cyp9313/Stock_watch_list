from flask import Flask, request, jsonify
import json
import sqlite3
import yfinance as yf
import pandas as pd
import numpy as np
import datetime
import os
import requests
from io import StringIO
from dotenv import load_dotenv
import concurrent.futures
import time
# import random
import pytz
import fear_and_greed 
import warnings
warnings.filterwarnings('ignore')
import requests_cache
from stockanalysis_scraper import scrape_batch, should_query_forward_pe
from ticker_mapping import normalize_yfinance_ticker

# 加载 .env 文件中的环境变量
load_dotenv()
# DashScope API key (当前未使用，保留供未来扩展)
# DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY")

# 禁用所有缓存
requests_cache.uninstall_cache()


# ===== SQLite 缓存层 =====
DB_PATH = "stock_cache.db"
YF_DOWNLOAD_BATCH_SIZE = 100
SP500_SYMBOLS_CACHE_PATH = os.path.join(os.path.dirname(__file__), "sp500_symbols_cache.json")
SP500_SYMBOLS_CACHE_MAX_AGE_DAYS = 7


def init_db():
    """初始化 SQLite 数据库，返回连接"""
    conn = sqlite3.connect(DB_PATH)
    # 性能优化：WAL 模式提升并发读写；增量 auto_vacuum 让被删除的页面可复用
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")

    # ===== 价格缓存表（增量更新的核心）=====
    # 所有标的（watchlist + 宽基指数 + S&P500 breadth）统一存一张表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_cache (
            ticker      TEXT    NOT NULL,
            date        TEXT    NOT NULL,
            adj_close   REAL,
            volume      REAL,
            PRIMARY KEY (ticker, date)
        )
    """)
    # 为按 ticker 查询创建索引（MAX(date) 查询走索引）
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_price_cache_ticker
        ON price_cache(ticker)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_analysis_data (
            ticker          TEXT    NOT NULL,
            date            TEXT    NOT NULL,
            forward_pe      REAL,
            peg_ratio       REAL,
            trailing_pe     REAL,
            market_cap      REAL,
            earnings_date   TEXT,
            ps_ratio        REAL,
            pb_ratio        REAL,
            analyst_rating  TEXT,
            price_target    REAL,
            raw_answer      TEXT,
            created_at      TEXT    DEFAULT (datetime('now')),
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS beta_cache (
            ticker          TEXT    NOT NULL,
            date            TEXT    NOT NULL,
            beta            REAL,
            data_points     INTEGER,
            created_at      TEXT    DEFAULT (datetime('now')),
            PRIMARY KEY (ticker, date)
        )
    """)
    # 迁移：为已存在的 stock_analysis_data 表添加 peg_ratio 列（如果缺失）
    try:
        conn.execute("SELECT peg_ratio FROM stock_analysis_data LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE stock_analysis_data ADD COLUMN peg_ratio REAL")
        print("[DB] 已添加 peg_ratio 列到 stock_analysis_data 表")
    # 迁移：trailing_pe
    try:
        conn.execute("SELECT trailing_pe FROM stock_analysis_data LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE stock_analysis_data ADD COLUMN trailing_pe REAL")
        print("[DB] 已添加 trailing_pe 列到 stock_analysis_data 表")
    # 迁移：market_cap
    try:
        conn.execute("SELECT market_cap FROM stock_analysis_data LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE stock_analysis_data ADD COLUMN market_cap REAL")
        print("[DB] 已添加 market_cap 列到 stock_analysis_data 表")
    # 迁移：earnings_date
    try:
        conn.execute("SELECT earnings_date FROM stock_analysis_data LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE stock_analysis_data ADD COLUMN earnings_date TEXT")
        print("[DB] 已添加 earnings_date 列到 stock_analysis_data 表")
    # 迁移：ps_ratio
    try:
        conn.execute("SELECT ps_ratio FROM stock_analysis_data LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE stock_analysis_data ADD COLUMN ps_ratio REAL")
        print("[DB] 已添加 ps_ratio 列到 stock_analysis_data 表")
    # 迁移：pb_ratio
    try:
        conn.execute("SELECT pb_ratio FROM stock_analysis_data LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE stock_analysis_data ADD COLUMN pb_ratio REAL")
        print("[DB] 已添加 pb_ratio 列到 stock_analysis_data 表")

    # ===== 滚动窗口清理 =====
    # price_cache: 保留 750 个自然日（≈517 交易日）
    #   MA200 需 200 交易日 warmup + 1 年图表 252 交易日 = 452 交易日最低需求
    #   750 自然日 ≈ 517 交易日，留有 ~65 交易日余量
    #   注意：不能用 500 自然日，因为 500 自然日 ≈ 345 交易日 < 452 交易日
    deleted = conn.execute("DELETE FROM price_cache WHERE date < date('now', '-750 days')").rowcount
    if deleted > 0:
        print(f"[DB] price_cache 清理: 删除 {deleted} 条 750 天前的记录")
    # stock_analysis_data / beta_cache: 保留 90 天（每日一行，90 天足够）
    conn.execute("DELETE FROM stock_analysis_data WHERE date < date('now', '-90 days')")
    conn.execute("DELETE FROM beta_cache WHERE date < date('now', '-90 days')")

    conn.commit()
    return conn


def get_market_date():
    """获取美东时间当前日期 (YYYY-MM-DD)"""
    ny = pytz.timezone('America/New_York')
    return datetime.datetime.now(ny).strftime('%Y-%m-%d')


def get_cached_stock_analysis(all_tickers):
    """
    带缓存的批量 StockAnalysis 查询
    1. 先查缓存，命中直接返回
    2. 未命中的并发爬取 StockAnalysis.com
    3. 查到后写入缓存
    返回: dict {ticker: {"forward_pe": float/None, "peg_ratio": float/None, "trailing_pe": float/None, "market_cap": float/None, "earnings_date": str/None, "ps_ratio": float/None, "pb_ratio": float/None, "analyst_rating": str/None, "price_target": float/None}}
    """
    all_tickers = list(dict.fromkeys(normalize_yfinance_ticker(t) for t in all_tickers))
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
            "SELECT forward_pe, peg_ratio, trailing_pe, market_cap, earnings_date, ps_ratio, pb_ratio, analyst_rating, price_target, raw_answer FROM stock_analysis_data WHERE ticker=? AND date=?",
            (t, today),
        ).fetchone()
        if row:
            cached_data = {
                "forward_pe": row[0],
                "peg_ratio": row[1],
                "trailing_pe": row[2],
                "market_cap": row[3],
                "earnings_date": row[4],
                "ps_ratio": row[5],
                "pb_ratio": row[6],
                "analyst_rating": row[7],
                "price_target": row[8],
            }
            raw_answer = row[9] or ""
            old_empty_failure = (
                not any(cached_data.values())
                and not raw_answer.startswith("source=")
                and any(token in raw_answer for token in ("http_404", "not_found", "request_error"))
            )
            if old_empty_failure:
                missing.append(t)
            else:
                results[t] = cached_data
        else:
            missing.append(t)

    if not missing:
        conn.close()
        return results

    print(f"[StockAnalysis] 缓存命中 {len(results)}/{len(query_tickers)}，"
          f"需查询 {len(missing)} 只: {missing}")

    # 2. 并发爬取
    scraped = scrape_batch(missing)
    for t in missing:
        data = scraped.get(t, {"forward_pe": None, "peg_ratio": None, "trailing_pe": None, "market_cap": None, "earnings_date": None, "ps_ratio": None, "pb_ratio": None, "analyst_rating": None, "price_target": None, "raw": "no_result"})
        results[t] = {
            "forward_pe": data["forward_pe"],
            "peg_ratio": data["peg_ratio"],
            "trailing_pe": data["trailing_pe"],
            "market_cap": data["market_cap"],
            "earnings_date": data["earnings_date"],
            "ps_ratio": data["ps_ratio"],
            "pb_ratio": data["pb_ratio"],
            "analyst_rating": data["analyst_rating"],
            "price_target": data["price_target"],
        }
        conn.execute(
            "INSERT OR REPLACE INTO stock_analysis_data (ticker, date, forward_pe, peg_ratio, trailing_pe, market_cap, earnings_date, ps_ratio, pb_ratio, analyst_rating, price_target, raw_answer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t, today, data["forward_pe"], data["peg_ratio"], data["trailing_pe"],
             data["market_cap"], data["earnings_date"], data["ps_ratio"], data["pb_ratio"],
             data["analyst_rating"], data["price_target"], data.get("raw", "")),
        )

    conn.commit()
    conn.close()
    return results


def get_cached_beta(ticker, date_str):
    """从缓存读取 Beta，未命中返回 None"""
    conn = init_db()
    row = conn.execute(
        "SELECT beta FROM beta_cache WHERE ticker=? AND date=?",
        (ticker, date_str)
    ).fetchone()
    conn.close()
    return float(row[0]) if row is not None else None


def save_beta(ticker, date_str, beta, data_points):
    """将 Beta 计算结果写入缓存"""
    conn = init_db()
    conn.execute(
        "INSERT OR REPLACE INTO beta_cache (ticker, date, beta, data_points) VALUES (?, ?, ?, ?)",
        (ticker, date_str, beta, data_points)
    )
    conn.commit()
    conn.close()


def get_cached_betas(tickers, date_str):
    """批量从缓存读取 Beta，返回 {ticker: beta}"""
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return {}

    conn = init_db()
    placeholders = ','.join('?' * len(tickers))
    rows = conn.execute(
        f"SELECT ticker, beta FROM beta_cache WHERE date=? AND ticker IN ({placeholders})",
        [date_str] + tickers
    ).fetchall()
    conn.close()
    return {ticker: float(beta) for ticker, beta in rows if beta is not None}


def save_betas(beta_rows):
    """批量写入 Beta 缓存。beta_rows: [(ticker, date_str, beta, data_points), ...]"""
    if not beta_rows:
        return

    conn = init_db()
    conn.executemany(
        "INSERT OR REPLACE INTO beta_cache (ticker, date, beta, data_points) VALUES (?, ?, ?, ?)",
        beta_rows
    )
    conn.commit()
    conn.close()


# ===== 价格缓存层（增量更新核心）=====

def _safe_float(val):
    """将值安全转换为 float，NaN/None 返回 None"""
    if val is None:
        return None
    try:
        f = float(val)
        return None if np.isnan(f) else f
    except (ValueError, TypeError):
        return None


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _save_to_price_cache(conn, df, tickers):
    """
    将 yf.download 的输出写入 price_cache 表（INSERT OR REPLACE）。
    只保存 adj_close 和 volume，OHL/Close 是冗余数据不保存。
    支持多标的（MultiIndex 列）和单标的（扁平列）两种格式。
    """
    if df is None or df.empty:
        return

    all_rows = []

    if isinstance(df.columns, pd.MultiIndex):
        # 多标的格式：列如 ('Adj Close', 'AAPL')
        for ticker in tickers:
            rows_data = []
            if ('Adj Close', ticker) in df.columns:
                rows_data.append(('adj_close', df[('Adj Close', ticker)]))
            if ('Volume', ticker) in df.columns:
                rows_data.append(('volume', df[('Volume', ticker)]))

            if not rows_data:
                continue

            sub = pd.DataFrame({name: series for name, series in rows_data})
            sub = sub.dropna(how='all')

            for date_idx, row in sub.iterrows():
                date_str = pd.Timestamp(date_idx).strftime('%Y-%m-%d')
                all_rows.append((
                    ticker, date_str,
                    _safe_float(row.get('adj_close')),
                    _safe_float(row.get('volume')),
                ))
    elif len(tickers) == 1:
        # 单标的格式：列如 'Adj Close', 'Volume', ...
        ticker = tickers[0]
        col_map = {'Adj Close': 'adj_close', 'Volume': 'volume'}
        available = {col_map[c]: df[c] for c in df.columns if c in col_map}
        if not available:
            return
        sub = pd.DataFrame(available).dropna(how='all')

        for date_idx, row in sub.iterrows():
            date_str = pd.Timestamp(date_idx).strftime('%Y-%m-%d')
            all_rows.append((
                ticker, date_str,
                _safe_float(row.get('adj_close')),
                _safe_float(row.get('volume')),
            ))

    if all_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO price_cache "
            "(ticker, date, adj_close, volume) "
            "VALUES (?, ?, ?, ?)",
            all_rows
        )
        print(f"[PriceCache] 写入 {len(all_rows)} 条记录 ({len(tickers)} 个标的)")


def _load_from_price_cache(conn, tickers):
    """
    从 price_cache 读取数据，返回与 yf.download(group_by='column', auto_adjust=False)
    格式一致的 MultiIndex DataFrame。
    只包含 ('Adj Close', ticker) 和 ('Volume', ticker) 列。
    """
    if not tickers:
        return pd.DataFrame()

    placeholders = ','.join('?' * len(tickers))
    rows = conn.execute(
        f"SELECT ticker, date, adj_close, volume "
        f"FROM price_cache WHERE ticker IN ({placeholders}) ORDER BY ticker, date",
        tickers
    ).fetchall()

    if not rows:
        return pd.DataFrame()

    records = []
    for ticker, date_str, ac, v in rows:
        records.append({
            'date': pd.Timestamp(date_str),
            'ticker': ticker,
            'Adj Close': ac, 'Volume': v
        })

    df = pd.DataFrame(records)
    df = df.pivot(index='date', columns='ticker')
    # pivot 后列是 MultiIndex: ('Adj Close', 'AAPL'), ('Adj Close', 'MSFT'), ...
    # 这与 yf.download(group_by='column') 格式一致（仅含 Adj Close 和 Volume）
    df = df.sort_index(axis=1, level=0)
    return df


def get_prices_with_cache(tickers, period="2y", delete_stale=False):
    """
    带增量更新的价格数据获取。

    策略（3-way delta reconciliation）：
      - existing = DB ∩ 请求 → 增量下载最近几天，与缓存合并
      - new      = 请求 - DB → 全量下载 period 数据
      - stale    = DB - 请求 → 从 DB 删除（仅当 delete_stale=True）

    注意: stale 删除默认关闭。因为 /api/stock_data 和 /api/breadth_data
    请求的标的集不同，开启 stale 删除会导致两个端点互删对方数据。
    旧数据由 init_db() 的 750 天滚动窗口自动清理。

    增量下载的 period 由 gap 决定（均为自然日，用 gap+2 留 2 天缓冲）：
      - gap ≤ 7 天  → "{gap+2}d"（例: gap=1→3d, gap=6→8d，确保覆盖最新交易日）
      - 8~30 天     → "{gap+2}d"
      - > 30 天     → 全量重新下载 period

    注意: "5d" 不够用——若今天是周四(gap=6)，"5d" 只取最近 5 个自然日（周日~周四），
    上周五的数据（距今天 6 天）会被遗漏。
    """
    if not tickers:
        return pd.DataFrame()

    conn = init_db()
    today = datetime.date.today()

    # 去重保持顺序
    tickers = list(dict.fromkeys(tickers))
    req_set = set(tickers)

    # 1. 查询 DB 中已有的标的
    db_tickers = set(
        row[0] for row in conn.execute("SELECT DISTINCT ticker FROM price_cache").fetchall()
    )

    # 2. 3-way 集合划分
    existing = req_set & db_tickers    # 增量更新
    new = req_set - db_tickers         # 全量下载
    stale = db_tickers - req_set       # 删除

    # 3. 删除过期标的（默认关闭，避免 stock_data 和 breadth_data 互删）
    if delete_stale and stale:
        stale_list = sorted(stale)
        placeholders = ','.join('?' * len(stale_list))
        conn.execute(
            f"DELETE FROM price_cache WHERE ticker IN ({placeholders})",
            stale_list
        )
        conn.commit()
        print(f"[PriceCache] 删除 {len(stale_list)} 个过期标的: {stale_list}")

    # 4. 全量下载新标的
    if new:
        new_list = sorted(new)
        print(f"[PriceCache] 全量下载 {len(new_list)} 个新标的 (period={period})...")
        for batch in _chunks(new_list, YF_DOWNLOAD_BATCH_SIZE):
            print(f"[PriceCache] 全量下载批次 {len(batch)} 个标的 (period={period})...")
            df_new = yf.download(
                tickers=batch, period=period, interval="1d",
                auto_adjust=False, group_by="column", threads=True, progress=False, timeout=20
            )
            if df_new is not None and not df_new.empty:
                _save_to_price_cache(conn, df_new, batch)

    # 5. 增量更新已有标的
    if existing:
        existing_list = sorted(existing)
        # 按下载 period 分组，减少 API 调用次数
        download_batches = {}  # period_str -> [tickers]
        placeholders = ','.join('?' * len(existing_list))
        max_dates = dict(conn.execute(
            f"SELECT ticker, MAX(date) FROM price_cache WHERE ticker IN ({placeholders}) GROUP BY ticker",
            existing_list
        ).fetchall())

        for t in existing_list:
            max_date_str = max_dates.get(t)

            if max_date_str is None:
                # DB 中无数据，按全量处理
                download_batches.setdefault(period, []).append(t)
                continue

            max_date = datetime.datetime.strptime(max_date_str, '%Y-%m-%d').date()
            gap_days = (today - max_date).days

            if gap_days <= 0:
                # 同一交易日内再次刷新时，仍拉取 2d 以获取盘中最新价
                dl_period = "2d"
            elif gap_days <= 30:
                dl_period = f"{gap_days + 2}d"
            else:
                dl_period = period  # gap 过大，全量重新下载

            download_batches.setdefault(dl_period, []).append(t)

        for dl_period, batch in download_batches.items():
            print(f"[PriceCache] 增量更新 {len(batch)} 个标的 (period={dl_period})...")
            for sub_batch in _chunks(batch, YF_DOWNLOAD_BATCH_SIZE):
                print(f"[PriceCache] 增量更新批次 {len(sub_batch)} 个标的 (period={dl_period})...")
                df_inc = yf.download(
                    tickers=sub_batch, period=dl_period, interval="1d",
                    auto_adjust=False, group_by="column", threads=True, progress=False, timeout=20
                )
                if df_inc is not None and not df_inc.empty:
                    _save_to_price_cache(conn, df_inc, sub_batch)

    conn.commit()

    # 6. 从 DB 加载完整数据
    result_df = _load_from_price_cache(conn, tickers)

    conn.close()
    return result_df


app = Flask(__name__)

# 分组配置
# groups = {
#     "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA"],
#     "Chips/AI": ["AMD","INTC","AVGO","SMCI","PLTR","RGTI","DXYZ","SNPS","APP"],
#     "Fin/Crypto": ["V","JPM","BRK-B","COIN","HOOD","MSTR","CRCL","SOFI","OSCR"],
#     "Health": ["LLY","NVO","ABBV","UNH"],
#     "Energy": ["SMR","VST","OKLO","NEE","ENPH","GE","GEV"],
#     "Defense": ["LMT","BA","ACHR","AXON"],
#     "Consumer": ["LULU","NKE","CMG","COST"],
#     "China": ["BYDDY","XIACY","PDD","BABA"],
#     "Themes": ["ASTS","CRWV","NBIS","MP","RKLB"],
#     "Broad Market": [
#         "^GSPC","^NDX","^DJI","^RUT","510300.SS",
#         "RSP","QQQE","TQQQ","WNUC.DE","REMX","^TNX",
#         "EURUSD=X","GC=F",
#         "BTC-USD","^VIX"
#     ],
#     "Market Breadth": ["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"]
# }

# ===== 工具函数 =====
def get_financial_info(ticker, attr_name, default_value=None):
    try:
        value = getattr(ticker.info, attr_name, None)
        return value if value is not None else default_value
    except Exception:
        return default_value


def normalize_groups_for_yfinance(groups):
    return {
        group_name: [normalize_yfinance_ticker(ticker) for ticker in tickers]
        for group_name, tickers in groups.items()
    }

def _normalize_sp500_symbols(symbols):
    return [
        str(symbol).strip().replace(".", "-")
        for symbol in symbols
        if str(symbol).strip() and str(symbol).strip().lower() != "nan"
    ]


def _read_sp500_symbols_cache(max_age_days=SP500_SYMBOLS_CACHE_MAX_AGE_DAYS):
    try:
        if not os.path.exists(SP500_SYMBOLS_CACHE_PATH):
            return []
        with open(SP500_SYMBOLS_CACHE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        symbols = payload.get("symbols", payload if isinstance(payload, list) else [])
        symbols = _normalize_sp500_symbols(symbols)
        if not symbols:
            return []
        if max_age_days is not None:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(SP500_SYMBOLS_CACHE_PATH))
            if datetime.datetime.now() - mtime > datetime.timedelta(days=max_age_days):
                return []
        return symbols
    except Exception as e:
        print(f"S&P 500 symbols cache read failed: {e}")
        return []


def _write_sp500_symbols_cache(symbols, source):
    try:
        payload = {
            "source": source,
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "symbols": _normalize_sp500_symbols(symbols),
        }
        with open(SP500_SYMBOLS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"S&P 500 symbols cache write failed: {e}")


def get_sp500_symbols():
    """Fetch S&P 500 constituents with a local cache and non-Wikipedia fallback."""
    cached_symbols = _read_sp500_symbols_cache()
    if cached_symbols:
        return cached_symbols

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/116.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    sources = [
        (
            "Wikipedia",
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            "html",
        ),
        (
            "DataHub GitHub CSV",
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv",
            "csv",
        ),
    ]

    for source_name, url, source_type in sources:
        try:
            response = requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            if source_type == "html":
                df = pd.read_html(StringIO(response.text), attrs={"id": "constituents"})[0]
            else:
                df = pd.read_csv(StringIO(response.text))
            symbols = _normalize_sp500_symbols(df["Symbol"].tolist())
            if symbols:
                _write_sp500_symbols_cache(symbols, source_name)
                print(f"S&P 500 symbols loaded from {source_name}: {len(symbols)}")
                return symbols
        except Exception as e:
            print(f"S&P 500 symbols source failed ({source_name}): {e}")

    stale_symbols = _read_sp500_symbols_cache(max_age_days=None)
    if stale_symbols:
        print(f"S&P 500 symbols using stale local cache: {len(stale_symbols)}")
        return stale_symbols

    print("S&P 500 symbols unavailable from all sources")
    return []

# 计算股票的筹码分布(最近30个交易日)
def calculate_chip_distribution(stock_ticker,days="30d",num_bins=20):
    data = yf.Ticker(stock_ticker).history(interval="4h", period=days)
    price_min = data['Low'].min()
    price_max = data['High'].max()

    price_bins = np.linspace(price_min, price_max, num_bins)
    chip_profile = np.zeros(num_bins - 1)

    for _, row in data.iterrows():
        # 强制转成标量 float，并检查有效性
        try:
            low = float(row['Low'])
            high = float(row['High'])
            vol = float(row['Volume'])
        except Exception:
            continue

        if not (np.isfinite(low) and np.isfinite(high) and np.isfinite(vol)):
            continue
        if vol <= 0 or high <= low:
            continue

        # 找出覆盖的 bins
        idx = np.where((price_bins[:-1] >= low) & (price_bins[1:] <= high))[0]
        if idx.size > 0:
            chip_profile[idx] += vol / idx.size
        else:
            # 如果没有完全落在某个 bin（很窄的区间），分配到最近的 bin
            mid = (low + high) / 2.0
            bin_idx = np.searchsorted(price_bins, mid) - 1
            if bin_idx < 0:
                bin_idx = 0
            if bin_idx >= chip_profile.size:
                bin_idx = chip_profile.size - 1
            chip_profile[bin_idx] += vol
    dist_df = pd.DataFrame()
    dist_df['price'] = (price_bins[:-1] + price_bins[1:]) / 2.0
    dist_df['volume'] = chip_profile
    max_index = dist_df['volume'].idxmax()
    chip_peak_price = dist_df.loc[max_index, 'price']
    return dist_df,chip_peak_price
        


def calculate_market_breadth(data, symbols):
    """计算市场宽度"""
    if data is None or len(data) == 0 or not symbols:
        print("数据为空，返回空DataFrame")
        return pd.DataFrame(columns=["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"])

    # 处理不同的数据格式
    try:
        if isinstance(data.columns, pd.MultiIndex):
            # MultiIndex格式：price_cache 只存 Adj Close，直接提取
            close_prices = None
            for level in [0, 1]:
                try:
                    candidate = data.xs('Adj Close', axis=1, level=level)
                    if not candidate.empty:
                        close_prices = candidate
                        break
                except KeyError:
                    continue
            if close_prices is None:
                print("无法提取 Adj Close 价格数据")
                return pd.DataFrame(columns=["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"])
        else:
            # 普通DataFrame格式（备用方法返回的格式）
            close_prices = data
            
        print(f"Adj Close价格数据形状: {close_prices.shape}")
        
        # 删除全为NaN的列
        close_prices = close_prices.dropna(axis=1, how='all')
        print(f"删除全NaN列后形状: {close_prices.shape}")
        
        if close_prices.empty:
            print("Adj Close价格数据为空")
            return pd.DataFrame(columns=["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"])
            
        # 确保有足够的数据点
        if len(close_prices) < 20:
            print("数据点不足20个，无法计算市场宽度")
            return pd.DataFrame(columns=["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"])

        # 计算移动平均线（不使用 min_periods=1，确保 MA 值在窗口不足时为 NaN）
        # 2y 数据（500+ 交易日）足以让 MA200 在最近 1 年图表范围内全部有效
        ma20 = close_prices.rolling(window=20).mean()
        ma50 = close_prices.rolling(window=50).mean()
        ma200 = close_prices.rolling(window=200).mean()

        # 计算上涨股票数量
        adv_mask_20 = (close_prices > ma20) & (~ma20.isna())
        adv_mask_50 = (close_prices > ma50) & (~ma50.isna())
        adv_mask_200 = (close_prices > ma200) & (~ma200.isna())

        # 计算比率（上涨股票数量/总股票数量）
        valid_stocks_20 = (~ma20.isna()).sum(axis=1)
        valid_stocks_50 = (~ma50.isna()).sum(axis=1)
        valid_stocks_200 = (~ma200.isna()).sum(axis=1)
        
        ad_ratio_20 = (adv_mask_20.sum(axis=1) / valid_stocks_20 * 100).replace([np.inf, -np.inf], np.nan)
        ad_ratio_50 = (adv_mask_50.sum(axis=1) / valid_stocks_50 * 100).replace([np.inf, -np.inf], np.nan)
        ad_ratio_200 = (adv_mask_200.sum(axis=1) / valid_stocks_200 * 100).replace([np.inf, -np.inf], np.nan)

        breadth_df = pd.DataFrame({
            "20MA_Ratio": ad_ratio_20,
            "50MA_Ratio": ad_ratio_50,
            "200MA_Ratio": ad_ratio_200
        })
        
        print(f"市场宽度计算完成，有效数据点: {len(breadth_df.dropna())}")
        return breadth_df
        
    except Exception as e:
        print(f"计算市场宽度时出错: {e}")
        return pd.DataFrame(columns=["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"])

# ===== API 路由 =====
@app.route('/api/stock_data', methods=['GET'])
def get_stock_data():
    """获取股票数据"""
    try:
        # 从查询参数获取分组数据
        groups_json = request.args.get('groups', '{}')
        
        try:
            groups = json.loads(groups_json)
        except json.JSONDecodeError:
            return jsonify({
                "success": False, 
                "error": "股票代码分组数据格式错误，请提供有效的JSON格式"
            })
        groups = normalize_groups_for_yfinance(groups)
        
        # 如果客户端没有提供分组，使用默认分组（可选）
        if not groups:
            return jsonify({
                "success": False, 
                "error": "股票代码分组数据为空，请提供有效的JSON格式股票代码分组"
            })
        
        # 获取 broad_market_tickers 列表（指数/商品/加密货币等，不需要爬取 SA 财务数据）
        broad_market_json = request.args.get('broad_market_tickers', '[]')
        try:
            broad_market_set = set(normalize_yfinance_ticker(t) for t in json.loads(broad_market_json))
        except json.JSONDecodeError:
            broad_market_set = set()
        
        base_date = datetime.date(datetime.date.today().year - 1, 12, 31).strftime('%Y-%m-%d')
        # 去重：Dashboard 与 story groups 有大量重复标的，后端只需处理一次
        all_tickers = list(dict.fromkeys(
            [t for group in groups.values() for t in group
             if t not in ["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"]]
        ))
        
        ny_tz = pytz.timezone('America/New_York')
        current_date_ny = datetime.datetime.now(ny_tz).date()
        earnings_df = pd.DataFrame(columns=["Next Earnings","Trailing PE","Forward PE","PEG Ratio","Market Cap","Analysts","Price Target"])
        
        # ===== 批量获取 StockAnalysis 数据（带 SQLite 缓存）=====
        # 在循环之前一次性查询，避免循环内逐只调用
        sa_query_tickers = [t for t in all_tickers
                           if t not in broad_market_set
                           and t not in groups.get("Market Breadth", [])
                           and should_query_forward_pe(t)]
        sa_data_dict = get_cached_stock_analysis(sa_query_tickers)

        # ===== Phase 1: 确定哪些标的需要 yfinance 调用 =====
        # SA 已有 earnings_date → 不需要 yfinance calendar
        # SA 已有 trailing_pe/peg_ratio/market_cap → 不需要 yfinance .info
        tickers_need_earnings_yf = set()
        tickers_need_info_yf = set()

        for ticker_symbol in all_tickers:
            if ticker_symbol in broad_market_set or ticker_symbol in groups.get("Market Breadth", []):
                continue

            sa_data = sa_data_dict.get(ticker_symbol)

            # SA 没有 earnings_date → 需要 yfinance calendar/earnings_dates
            if sa_data is None or not sa_data.get("earnings_date"):
                tickers_need_earnings_yf.add(ticker_symbol)

            # SA 缺少 trailing_pe / peg_ratio / market_cap → 需要 yfinance .info
            if sa_data is None:
                tickers_need_info_yf.add(ticker_symbol)
            else:
                if (sa_data.get("trailing_pe") is None or
                        sa_data.get("peg_ratio") is None or
                        sa_data.get("market_cap") is None):
                    tickers_need_info_yf.add(ticker_symbol)

        # ===== Phase 2: 并行获取 yfinance 数据 =====
        need_yf = tickers_need_earnings_yf | tickers_need_info_yf

        def _fetch_yf_data(ticker_symbol):
            """并行获取单个标的的 yfinance 财报日期和 .info"""
            result = {'earnings_date': None, 'info': {}}
            try:
                ticker = yf.Ticker(ticker_symbol)

                if ticker_symbol in tickers_need_earnings_yf:
                    calendar = ticker.calendar
                    if calendar and 'Earnings Date' in calendar and calendar['Earnings Date']:
                        result['earnings_date'] = calendar['Earnings Date'][0]

                    if result['earnings_date'] is None:
                        earnings_data = ticker.get_earnings_dates()
                        if earnings_data is not None and not earnings_data.empty:
                            unreported = earnings_data[earnings_data['Reported EPS'].isna()]
                            if not unreported.empty:
                                result['earnings_date'] = unreported.index[0]

                if ticker_symbol in tickers_need_info_yf:
                    result['info'] = ticker.info  # 一次性获取整个 dict
            except Exception as e:
                print(f"获取{ticker_symbol}财报数据失败: {e}")

            return ticker_symbol, result

        yf_data = {}
        if need_yf:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(_fetch_yf_data, t) for t in need_yf]
                for future in concurrent.futures.as_completed(futures):
                    ticker_symbol, result = future.result()
                    yf_data[ticker_symbol] = result

        # ===== Phase 3: 构建 earnings_df（无 API 调用，纯内存操作）=====
        for ticker_symbol in all_tickers:
            if ticker_symbol in broad_market_set or ticker_symbol in groups.get("Market Breadth", []):
                continue

            try:
                sa_data = sa_data_dict.get(ticker_symbol)
                yf_result = yf_data.get(ticker_symbol, {'earnings_date': None, 'info': {}})
                yf_earnings = yf_result.get('earnings_date')
                yf_info = yf_result.get('info', {})

                # ===== Next Earnings：优先 StockAnalysis，缺省 fallback yfinance =====
                if sa_data is not None and sa_data.get("earnings_date"):
                    try:
                        ed_date = datetime.datetime.strptime(sa_data["earnings_date"], "%b %d, %Y").date()
                        if ed_date and ed_date >= current_date_ny:
                            earnings_df.loc[ticker_symbol, "Next Earnings"] = ed_date
                        else:
                            earnings_df.loc[ticker_symbol, "Next Earnings"] = None
                    except (ValueError, TypeError):
                        earnings_df.loc[ticker_symbol, "Next Earnings"] = None
                else:
                    # Fallback: yfinance（使用预取数据，无 API 调用）
                    if yf_earnings is not None:
                        if isinstance(yf_earnings, pd.Timestamp):
                            earnings_date = yf_earnings.date()
                        elif isinstance(yf_earnings, datetime.datetime):
                            earnings_date = yf_earnings.date()
                        elif isinstance(yf_earnings, datetime.date):
                            earnings_date = yf_earnings
                        else:
                            earnings_date = None
                        if earnings_date and earnings_date < current_date_ny:
                            earnings_df.loc[ticker_symbol, "Next Earnings"] = None
                        else:
                            earnings_df.loc[ticker_symbol, "Next Earnings"] = earnings_date
                    else:
                        earnings_df.loc[ticker_symbol, "Next Earnings"] = None

                # ===== Trailing PE：优先 StockAnalysis，缺省 fallback yfinance =====
                if sa_data is not None:
                    trail_pe = sa_data.get("trailing_pe")
                    if trail_pe is None:
                        trail_pe = yf_info.get('trailingPE', None)
                    earnings_df.loc[ticker_symbol, "Trailing PE"] = trail_pe
                else:
                    earnings_df.loc[ticker_symbol, "Trailing PE"] = yf_info.get('trailingPE', None)

                # ===== Forward PE、PEG Ratio、Analysts、Price Target =====
                if sa_data is not None:
                    earnings_df.loc[ticker_symbol, "Forward PE"] = sa_data.get("forward_pe")
                    peg_val = sa_data.get("peg_ratio")
                    if peg_val is None:
                        peg_val = yf_info.get('trailingPegRatio', None)
                    earnings_df.loc[ticker_symbol, "PEG Ratio"] = peg_val
                    earnings_df.loc[ticker_symbol, "Analysts"] = sa_data.get("analyst_rating")
                    earnings_df.loc[ticker_symbol, "Price Target"] = sa_data.get("price_target")
                else:
                    earnings_df.loc[ticker_symbol, "Forward PE"] = yf_info.get('forwardPE', None)
                    earnings_df.loc[ticker_symbol, "PEG Ratio"] = yf_info.get('trailingPegRatio', None)
                    earnings_df.loc[ticker_symbol, "Analysts"] = None
                    earnings_df.loc[ticker_symbol, "Price Target"] = None

                # ===== Market Cap：优先 StockAnalysis，缺省 fallback yfinance =====
                if sa_data is not None:
                    mcap = sa_data.get("market_cap")
                    if mcap is None:
                        mcap = yf_info.get('marketCap', None)
                    earnings_df.loc[ticker_symbol, "Market Cap"] = mcap
                else:
                    earnings_df.loc[ticker_symbol, "Market Cap"] = yf_info.get('marketCap', None)

            except Exception as e:
                print(f"构建{ticker_symbol}财报数据失败: {e}")
                earnings_df.loc[ticker_symbol] = None

        # 获取价格数据（增量更新：已有标的只下载最近几天，新标的全量下载）
        df = get_prices_with_cache(all_tickers, period="2y")

        if isinstance(df.columns, pd.MultiIndex):
            top_fields = {lvl[0] for lvl in df.columns}
            if 'Adj Close' in top_fields:
                adj_close = df['Adj Close'].copy()
            else:
                adj_close = pd.DataFrame()
            volumes = df['Volume'].copy() if 'Volume' in top_fields else pd.DataFrame()
        else:
            if 'Adj Close' in df.columns:
                price_col = 'Adj Close'
            else:
                price_col = df.columns[0]
            only_ticker = all_tickers[0]
            adj_close = df[[price_col]].rename(columns={price_col: only_ticker})
            volumes = pd.DataFrame({only_ticker: df['Volume']}) if 'Volume' in df.columns else pd.DataFrame()

        results = []

        # 从缓存数据中提取标普500价格（无需单独下载）
        sp500_ticker = "^GSPC"
        sp500_prices = None
        sp500_reference_dates = {}  # 存储参考日期：{63: date, 126: date, 252: date}

        try:
            if not adj_close.empty and sp500_ticker in adj_close.columns:
                sp500_prices = adj_close[sp500_ticker].dropna()
                print(f"{sp500_ticker} 从缓存获取成功，共 {len(sp500_prices)} 个数据点")
            elif not adj_close.empty and 'Adj Close' in df.columns:
                # 单标的情况
                sp500_prices = df['Adj Close'].dropna()
                print(f"{sp500_ticker} 从缓存获取成功，共 {len(sp500_prices)} 个数据点")
            else:
                print(f"{sp500_ticker} 不在缓存数据中")

            if sp500_prices is not None and len(sp500_prices) > 0:
                # 计算标普500的参考日期（日期对齐的关键），统一去掉时区
                def _normalize_date(ts):
                    ts = pd.Timestamp(ts).normalize()
                    return ts.tz_localize(None) if ts.tzinfo is not None else ts

                if len(sp500_prices) >= 63:
                    sp500_reference_dates[63] = _normalize_date(sp500_prices.index[-63])
                if len(sp500_prices) >= 126:
                    sp500_reference_dates[126] = _normalize_date(sp500_prices.index[-126])
                if len(sp500_prices) >= 252:
                    sp500_reference_dates[252] = _normalize_date(sp500_prices.index[-252])

                print(f"标普500参考日期: 3M={sp500_reference_dates.get(63, 'N/A')}, 6M={sp500_reference_dates.get(126, 'N/A')}, 12M={sp500_reference_dates.get(252, 'N/A')}")
        except Exception as e:
            print(f"提取 {sp500_ticker} 缓存数据失败: {e}")

        def pct(a, b):
            if pd.isna(a) or pd.isna(b) or b == 0:
                return np.nan
            return (a / b - 1.0) * 100.0

        def get_price_on_date(prices, target_date, max_days=10):
            """
            获取指定日期或最近交易日的价格
            prices: pd.Series with DatetimeIndex
            target_date: 目标日期
            max_days: 最多向前/后搜索多少天
            返回: (价格, 实际使用的日期) 或 (np.nan, None)
            """
            if prices is None or len(prices) == 0:
                return np.nan, None

            # 统一去掉时区，只保留日期部分比较，避免时区不一致问题
            try:
                prices_dates = prices.index.normalize().tz_localize(None) if prices.index.tz is not None else prices.index.normalize()
            except Exception:
                prices_dates = prices.index

            # 确保 target_date 是无时区的 pd.Timestamp（只保留日期）
            if not isinstance(target_date, pd.Timestamp):
                target_date = pd.Timestamp(target_date)
            target_date = target_date.normalize()
            if target_date.tzinfo is not None:
                target_date = target_date.tz_localize(None)

            # 在 prices_dates 中搜索（向前最多 max_days 个自然日）
            for delta in range(0, max_days + 1):
                for sign in ([0] if delta == 0 else [-1, 1]):
                    candidate = target_date + pd.Timedelta(days=delta * sign if sign != 0 else 0)
                    matches = (prices_dates == candidate)
                    if matches.any():
                        idx = matches.argmax()  # 第一个匹配
                        if delta > 0:
                            print(f"    日期 {target_date.strftime('%Y-%m-%d')} 不是交易日，使用 {candidate.strftime('%Y-%m-%d')}")
                        return prices.iloc[idx], candidate

            return np.nan, None

        today_str = get_market_date()
        beta_cache = get_cached_betas(all_tickers, today_str)
        beta_updates = []

        for ticker in all_tickers:
            if adj_close.empty or ticker not in adj_close.columns:
                continue

            price_series = adj_close[ticker].dropna()
            if len(price_series) < 2:
                continue

            if not volumes.empty and ticker in volumes.columns:
                vol_series_raw = volumes[ticker]
            else:
                vol_series_raw = pd.Series(index=price_series.index, data=np.nan)

            d = pd.DataFrame({"Adj Close": price_series}).join(vol_series_raw.rename('Volume'), how='left')

            try:
                base_price = d.loc[d.index <= base_date, "Adj Close"].iloc[-1]
            except Exception:
                base_price = np.nan

            # 计算技术指标
            for n in [5, 10, 20, 50, 100, 200]:
                d[f"EMA{n}"] = d["Adj Close"].ewm(span=n, adjust=False).mean()
            d["Volume_EMA5"] = d["Volume"].ewm(span=5, adjust=False).mean()
            d["BB_Mid"] = d["Adj Close"].rolling(20, min_periods=1).mean()
            d["BB_Std"] = d["Adj Close"].rolling(20, min_periods=1).std()
            d["BB_Up"] = d["BB_Mid"] + 2 * d["BB_Std"]
            d["BB_Low"] = d["BB_Mid"] - 2 * d["BB_Std"]

            latest = d.iloc[-1]
            prev = d.iloc[-2] if len(d) > 1 else latest
            
            next_earnings = earnings_df.get("Next Earnings",None).get(ticker,None)
            trailing_PE = earnings_df.get("Trailing PE",None).get(ticker,None)
            forward_PE = earnings_df.get("Forward PE",None).get(ticker,None)
            PEG_ratio = earnings_df.get("PEG Ratio",None).get(ticker,None)
            market_cap = earnings_df.get("Market Cap",None).get(ticker,None)
            analyst_rating = earnings_df.get("Analysts",None).get(ticker,None)
            price_target = earnings_df.get("Price Target",None).get(ticker,None)

            # 计算相对动量分数（日期对齐版本）
            relative_momentum = np.nan
            m3m = np.nan
            m6m = np.nan
            m12m = np.nan

            # 检查 sp500_prices 和参考日期是否有效
            if (sp500_prices is not None and isinstance(sp500_prices, pd.Series) and 
                len(sp500_prices) > 0 and len(price_series) > 0 and len(sp500_reference_dates) > 0):
                
                # 计算3个月（63个交易日）的收益率
                if 63 in sp500_reference_dates:
                    ref_date_3m = sp500_reference_dates[63]
                    
                    # 获取标普500在参考日期的价格
                    sp500_price_3m, actual_date_3m = get_price_on_date(sp500_prices, ref_date_3m)
                    
                    # 获取股票在相同时日期（或最近交易日）的价格
                    if actual_date_3m is not None:
                        stock_price_3m, _ = get_price_on_date(price_series, actual_date_3m)
                    else:
                        stock_price_3m, _ = get_price_on_date(price_series, ref_date_3m)
                    
                    if not pd.isna(sp500_price_3m) and not pd.isna(stock_price_3m):
                        sp500_return_3m = (sp500_prices.iloc[-1] / sp500_price_3m - 1) * 100
                        stock_return_3m = (price_series.iloc[-1] / stock_price_3m - 1) * 100
                        m3m = stock_return_3m - sp500_return_3m
                
                # 计算6个月（126个交易日）的收益率
                if 126 in sp500_reference_dates:
                    ref_date_6m = sp500_reference_dates[126]
                    
                    sp500_price_6m, actual_date_6m = get_price_on_date(sp500_prices, ref_date_6m)
                    
                    if actual_date_6m is not None:
                        stock_price_6m, _ = get_price_on_date(price_series, actual_date_6m)
                    else:
                        stock_price_6m, _ = get_price_on_date(price_series, ref_date_6m)
                    
                    if not pd.isna(sp500_price_6m) and not pd.isna(stock_price_6m):
                        sp500_return_6m = (sp500_prices.iloc[-1] / sp500_price_6m - 1) * 100
                        stock_return_6m = (price_series.iloc[-1] / stock_price_6m - 1) * 100
                        m6m = stock_return_6m - sp500_return_6m
                
                # 计算12个月（252个交易日）的收益率
                if 252 in sp500_reference_dates:
                    ref_date_12m = sp500_reference_dates[252]
                    
                    sp500_price_12m, actual_date_12m = get_price_on_date(sp500_prices, ref_date_12m)
                    
                    if actual_date_12m is not None:
                        stock_price_12m, _ = get_price_on_date(price_series, actual_date_12m)
                    else:
                        stock_price_12m, _ = get_price_on_date(price_series, ref_date_12m)
                    
                    if not pd.isna(sp500_price_12m) and not pd.isna(stock_price_12m):
                        sp500_return_12m = (sp500_prices.iloc[-1] / sp500_price_12m - 1) * 100
                        stock_return_12m = (price_series.iloc[-1] / stock_price_12m - 1) * 100
                        m12m = stock_return_12m - sp500_return_12m
                
                # 计算相对动量分数
                if not (pd.isna(m3m) or pd.isna(m6m) or pd.isna(m12m)):
                    relative_momentum = 0.2 * m3m + 0.3 * m6m + 0.5 * m12m
                elif ticker == sp500_ticker:
                    # 标普500本身的相对动量分数应为0
                    relative_momentum = 0.0
                    m3m = 0.0
                    m6m = 0.0
                    m12m = 0.0

            # ===== Beta（相对于 ^GSPC，带 SQLite 缓存，统一用过去1年数据）=====
            beta = np.nan
            beta_cached = beta_cache.get(ticker)
            if beta_cached is not None:
                beta = float(beta_cached)
            elif (sp500_prices is not None and isinstance(sp500_prices, pd.Series) 
                  and len(sp500_prices) > 1 and len(price_series) > 1):
                # 统一截断到过去1年（252个交易日）以确保所有标的的计算窗口一致
                stock_beta = price_series.iloc[-252:] if len(price_series) > 252 else price_series
                sp500_beta = sp500_prices.iloc[-252:] if len(sp500_prices) > 252 else sp500_prices
                stock_norm = stock_beta.copy()
                sp500_norm = sp500_beta.copy()
                if stock_norm.index.tz is not None:
                    stock_norm.index = stock_norm.index.tz_localize(None)
                if sp500_norm.index.tz is not None:
                    sp500_norm.index = sp500_norm.index.tz_localize(None)
                common_idx = stock_norm.index.intersection(sp500_norm.index)
                if len(common_idx) > 2:
                    stock_ret = stock_norm.loc[common_idx].pct_change().dropna()
                    sp500_ret = sp500_norm.loc[common_idx].pct_change().dropna()
                    if len(stock_ret) > 1 and len(sp500_ret) > 1:
                        cov_matrix = np.cov(stock_ret, sp500_ret)
                        var_sp500 = cov_matrix[1, 1]
                        if var_sp500 != 0:
                            beta = float(cov_matrix[0, 1] / var_sp500)
                            beta_updates.append((ticker, today_str, beta, len(common_idx)))

            row = {
                "Ticker": ticker,
                "Price": float(round(latest["Adj Close"], 2)),
                "Beta": round(beta, 2) if not pd.isna(beta) else np.nan,
                "Rel. Momentum": round(float(relative_momentum), 2) if not pd.isna(relative_momentum) else np.nan,
                "1D%": pct(latest["Adj Close"], prev["Adj Close"]),
                "5D%": pct(latest["Adj Close"], d.iloc[-6]["Adj Close"]) if len(d) > 6 else np.nan,
                "1M%": pct(latest["Adj Close"], d.iloc[-21]["Adj Close"]) if len(d) > 21 else np.nan,
                "YTD%": pct(latest["Adj Close"], base_price) if pd.notna(base_price) else np.nan,
                "Volume_Ratio": (latest["Volume"] / latest["Volume_EMA5"]) if pd.notna(latest.get("Volume_EMA5")) and latest.get("Volume_EMA5") not in (0, None) else np.nan,
                "Next Earnings": next_earnings.strftime('%Y-%m-%d') if next_earnings and not pd.isna(next_earnings) else None,
                "Trailing PE": trailing_PE,
                "Forward PE": forward_PE,
                "PEG Ratio": PEG_ratio,
                "Analysts": analyst_rating if pd.notna(analyst_rating) else None,
                "Price Target": price_target if pd.notna(price_target) else None,
                "Market Cap": market_cap 
            }

            for n in [5, 10, 20, 50, 100, 200]:
                ema = latest.get(f"EMA{n}", np.nan)
                row[f"Diff_EMA{n}%"] = pct(latest["Adj Close"], ema) if pd.notna(ema) and ema != 0 else np.nan

            row["Diff_BB_Up%"] = (latest["Adj Close"]-latest.get("BB_Up"))/(latest.get("BB_Up")-latest.get("BB_Low"))*100 if pd.notna(latest.get("BB_Up")) and latest.get("BB_Up") not in (0, None) else np.nan
            row["Diff_BB_Low%"] = (latest["Adj Close"]-latest.get("BB_Low"))/(latest.get("BB_Up")-latest.get("BB_Low"))*100 if pd.notna(latest.get("BB_Low")) and latest.get("BB_Low") not in (0, None) else np.nan

            results.append(row)

        save_betas(beta_updates)

        return jsonify({"success": True, "data": results})
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/breadth_data', methods=['POST'])
def get_breadth_data():
    """获取市场宽度数据"""
    endpoint_start = time.perf_counter()
    try:
        
                # 获取标普500股票代码列表
        sp500_symbols_json = request.form.get('sp500_symbols', '{}')
        
        try:
            sp500_symbols = json.loads(sp500_symbols_json)
        except json.JSONDecodeError:
            return jsonify({
                "success": False, 
                "error": "标普500股票代码格式错误，请提供有效的JSON格式"
            })

        # 如果无法获取标普500成分股，直接报错
        if not sp500_symbols:
            return jsonify({"success": False, "error": "无法获取标普500成分股列表"})
        
        # 获取标普500价格数据（增量更新，2y 数据确保 MA200 在 1 年图表范围内有效）
        try:
            price_start = time.perf_counter()
            sp500_data = get_prices_with_cache(sp500_symbols, period="2y")
            print(f"标普500数据获取完成，数据形状: {sp500_data.shape}, 耗时 {time.perf_counter() - price_start:.1f}s")
        except Exception as e:
            return jsonify({"success": False, "error": f"获取标普500数据失败: {str(e)}"})
        
        # 检查数据是否为空
        if sp500_data is None or sp500_data.empty:
            return jsonify({"success": False, "error": "标普500数据为空"})
        
        # 计算市场宽度
        calc_start = time.perf_counter()
        breadth_df = calculate_market_breadth(sp500_data, sp500_symbols)
        print(f"市场宽度计算完成，耗时 {time.perf_counter() - calc_start:.1f}s")
        
        # 检查市场宽度数据是否为空
        if breadth_df.empty:
            return jsonify({"success": False, "error": "市场宽度数据计算失败"})
        
        # 准备返回数据
        results = []
        for ratio in ["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"]:
            if ratio in breadth_df.columns:
                series = breadth_df[ratio].dropna()
                if not series.empty:
                    latest_val = series.iloc[-1]
                    chg_1d = latest_val - series.iloc[-2] if len(series) > 1 else np.nan
                    chg_5d = latest_val - series.iloc[-6] if len(series) > 6 else np.nan
                    chg_20d = latest_val - series.iloc[-21] if len(series) > 21 else np.nan
                    results.append({
                        "Ticker": ratio,
                        "Price": round(float(latest_val), 2),
                        "1D%": round(float(chg_1d), 2) if not np.isnan(chg_1d) else np.nan,
                        "5D%": round(float(chg_5d), 2) if not np.isnan(chg_5d) else np.nan,
                        "1M%": round(float(chg_20d), 2) if not np.isnan(chg_20d) else np.nan,
                        "YTD%": np.nan,
                        "Volume_Ratio": np.nan,
                        **{f"Diff_EMA{n}%": np.nan for n in [5, 10, 20, 50, 100, 200]},
                        "Diff_BB_Up%": np.nan,
                        "Diff_BB_Low%": np.nan
                    })

        # 准备图表数据（只取最近 252 个交易日 = 1 年）
        chart_df = breadth_df.iloc[-252:] if len(breadth_df) > 252 else breadth_df
        if hasattr(chart_df.index, 'strftime') and callable(getattr(chart_df.index, 'strftime', None)):
            index_list = chart_df.index.strftime('%Y-%m-%d').tolist()
        else:
            try:
                index_list = pd.to_datetime(chart_df.index).strftime('%Y-%m-%d').tolist()
            except:
                index_list = []

        breadth_data = {
            "index": index_list,
            "20MA_Ratio": [round(float(x), 2) if not np.isnan(x) else 0 for x in chart_df["20MA_Ratio"]] if "20MA_Ratio" in chart_df.columns else [],
            "50MA_Ratio": [round(float(x), 2) if not np.isnan(x) else 0 for x in chart_df["50MA_Ratio"]] if "50MA_Ratio" in chart_df.columns else [],
            "200MA_Ratio": [round(float(x), 2) if not np.isnan(x) else 0 for x in chart_df["200MA_Ratio"]] if "200MA_Ratio" in chart_df.columns else []
        }
        
        print(f"/api/breadth_data 完成，总耗时 {time.perf_counter() - endpoint_start:.1f}s")
        return jsonify({"success": True, "data": results, "breadth_chart_data": breadth_data})
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/kline_data', methods=['GET'])
def get_kline_data():
    """获取K线图数据"""
    try:
        ticker = normalize_yfinance_ticker(request.args.get('ticker', ''))
        time_span = max(1, int(request.args.get('period', 365)))
        interval = request.args.get('interval', '1d')
        
        if not ticker:
            return jsonify({"success": False, "error": "请输入股票代码"})
        
        # 获取股票数据
        end_date = datetime.date.today() + pd.offsets.BusinessDay(1)
        start_date = end_date - datetime.timedelta(days=time_span)
        ticker_info = yf.Ticker(ticker)
        if '1d' in interval:
            stock_data = ticker_info.history(start=start_date, end=end_date)
        elif interval in ['5m','15m','1h','4h']:
            intraday_days = min(time_span, 60)
            stock_data = ticker_info.history(period=f"{intraday_days}d", interval = interval)
        elif '1wk' in interval:
            stock_data = ticker_info.history(start=start_date, end=end_date, interval = interval)
        else:
            return jsonify({"success": False, "error": "输入时间间隔无效"})

        
        if stock_data is None or stock_data.empty:
            return jsonify({"success": False, "error": f"未找到股票代码: {ticker}"})
        
        # 计算筹码分布
        chip_data,chip_peak_price = calculate_chip_distribution(ticker)
        prices = chip_data['price'].values
        volumes = chip_data['volume'].values
        
        # 计算技术指标
        # MACD
        exp12 = stock_data['Close'].ewm(span=12, adjust=False).mean()
        exp26 = stock_data['Close'].ewm(span=26, adjust=False).mean()
        macd = exp12 - exp26
        signal = macd.ewm(span=9, adjust=False).mean()
        hist = macd - signal

        # KDJ（标准 9,3,3 参数；com=2 对应 alpha=1/3）
        low_list = stock_data['Low'].rolling(9).min()
        high_list = stock_data['High'].rolling(9).max()
        denom = (high_list - low_list).replace(0, np.nan)
        rsv = ((stock_data['Close'] - low_list) / denom * 100).fillna(50)
        kdj_k = rsv.ewm(com=2, adjust=False).mean()
        kdj_d = kdj_k.ewm(com=2, adjust=False).mean()
        kdj_j = 3 * kdj_k - 2 * kdj_d

        # RSI
        delta = stock_data['Close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

        # 移动平均线
        for win in [5, 10, 20, 50, 100, 200]:
            stock_data[f'MA{win:03d}'] = stock_data['Close'].rolling(win).mean()
        
        # 布林带
        stock_data['Bollinger_Upper'] = stock_data['MA020'] + 2 * stock_data['Close'].rolling(20).std()
        stock_data['Bollinger_Middle'] = stock_data['MA020']
        stock_data['Bollinger_Lower'] = stock_data['MA020'] - 2 * stock_data['Close'].rolling(20).std()
        
        # 获取财务信息：优先用 StockAnalysis 数据（带缓存），None 时 fallback 到 yfinance
        sa_data_dict = get_cached_stock_analysis([ticker])
        sa_data = sa_data_dict.get(ticker, {})

        trailing_pe = sa_data.get("trailing_pe") or get_financial_info(ticker_info, 'trailingPE')
        forward_pe = sa_data.get("forward_pe") or get_financial_info(ticker_info, 'forwardPE')
        peg_ratio = sa_data.get("peg_ratio") or get_financial_info(ticker_info, 'trailingPegRatio')
        price_to_sales = sa_data.get("ps_ratio") or get_financial_info(ticker_info, 'priceToSalesTrailing12Months')
        price_to_book = sa_data.get("pb_ratio") or get_financial_info(ticker_info, 'priceToBook')
        next_earnings_date = sa_data.get("earnings_date")

        market_cap = sa_data.get("market_cap")
        if market_cap is not None:
            market_cap = f"{float(market_cap):.2e}"
        else:
            try:
                market_cap = f"{float(ticker_info.info['marketCap']):.2e}"
            except Exception:
                market_cap = None

        # 准备返回数据
        kline_data = {
            "success": True,
            "ticker": ticker,
            "dates": stock_data.index.strftime('%Y-%m-%d %H:%M').tolist(), 
            "ohlc": {
                "open": stock_data['Open'].tolist(),
                "high": stock_data['High'].tolist(),
                "low": stock_data['Low'].tolist(),
                "close": stock_data['Close'].tolist(),
                "volume": stock_data['Volume'].tolist()
            },
            "indicators": {
                "macd": macd.tolist(),
                "signal": signal.tolist(),
                "hist": hist.tolist(),
                "kdj_k": kdj_k.tolist(),
                "kdj_d": kdj_d.tolist(),
                "kdj_j": kdj_j.tolist(),
                "rsi": rsi.tolist(),
                "ma5": stock_data['MA005'].tolist(),
                "ma10": stock_data['MA010'].tolist(),
                "ma20": stock_data['MA020'].tolist(),
                "ma50": stock_data['MA050'].tolist(),
                "ma100": stock_data['MA100'].tolist(),
                "ma200": stock_data['MA200'].tolist(),
                "bollinger_upper": stock_data['Bollinger_Upper'].tolist(),
                "bollinger_lower": stock_data['Bollinger_Lower'].tolist(),
                "chip_prices": prices.tolist(),
                "chip_volumes": volumes.tolist(),
                "chip_peak_price": chip_peak_price
            },
            "financials": {
                "market_cap": market_cap,
                "trailing_pe": trailing_pe,
                "forward_pe": forward_pe,
                "peg_ratio": peg_ratio,
                "price_to_sales": price_to_sales,
                "price_to_book": price_to_book,
                "next_earnings": next_earnings_date,
                "analyst_rating": sa_data.get("analyst_rating"),
                "price_target": sa_data.get("price_target"),
            }
        }
        
        return jsonify(kline_data)
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/fear_greed', methods=['GET'])
def get_fear_greed():
    """获取恐惧贪婪指数"""
    try:
        index = fear_and_greed.get()
        return jsonify({
            "success": True,
            "value": index.value,
            "description": index.description
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# @app.route('/api/fear_greed_crypto',methods=['GET'])
# def get_fear_greed_crypto():
#     """获取加密货币恐惧贪婪指数"""
#     try:
#         url="https://api.alternative.me/fng/"
#         response = requests.get(url)
#         data = response.json()
#         fear_greed_data = data["data"][0]  # 获取第一个数据点
#         value = float(fear_greed_data["value"])  # 指数值
#         classification = fear_greed_data["value_classification"]  # 分类
#         timestamp = fear_greed_data["timestamp"]  # 时间戳
#         return jsonify({
#             "success": True,
#             "value": value,
#             "description": classification
#         })
#     except Exception as e:
#         return jsonify({"success": False, "error": str(e)})

@app.route('/api/fear_greed_crypto',methods=['GET'])
def get_fear_greed_crypto():
    """获取加密货币恐惧贪婪指数"""
    try:
        url="https://api.alternative.me/fng/"
        response = requests.get(url, timeout=10)
        data = response.json()
        fear_greed_data = data["data"][0]  # 获取第一个数据点
        value = float(fear_greed_data["value"])  # 指数值
        classification = fear_greed_data["value_classification"]  # 分类
        timestamp = fear_greed_data["timestamp"]  # 时间戳
        return jsonify({
            "success": True,
            "value": value,
            "description": classification
        })
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"Error in get_fear_greed_crypto: {e}")
        print(f"Traceback: {error_details}")
        return jsonify({
            "success": False, 
            "error": str(e),
            "details": error_details
        })


@app.route('/api/sp500_symbols', methods=['POST'])
def get_breadth_data_trail():
    """获取市场宽度数据"""
    sp500_symbols_json = request.form.get('sp500_symbols', '{}')
    try:
        sp500_symbols = json.loads(sp500_symbols_json)
    except json.JSONDecodeError:
        return jsonify({
            "success": False, 
            "error": "标普500股票代码格式错误，请提供有效的JSON格式"
        })

    # 如果无法获取标普500成分股，直接报错
    if not sp500_symbols:
        return jsonify({"success": False, "error": "无法获取标普500成分股列表"})
    
    # 预热标普500价格缓存（增量更新，2y 数据确保 MA200 有效）
    try:
        sp500_data = get_prices_with_cache(sp500_symbols, period="2y")
        print(f"标普500数据预热完成，数据形状: {sp500_data.shape}")
        return jsonify({"success": True, "error": "预热标普500数据成功"})
    except Exception as e:
        return jsonify({"success": False, "error": f"预热标普500数据失败: {str(e)}"})
    
# if __name__ == "__main__":
#     app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
