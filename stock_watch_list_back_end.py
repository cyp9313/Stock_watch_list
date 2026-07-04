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
# import time
# import random
import pytz
import fear_and_greed 
import warnings
warnings.filterwarnings('ignore')
import requests_cache
from stockanalysis_scraper import scrape_batch, should_query_forward_pe

# 加载 .env 文件中的环境变量
load_dotenv()
# DashScope API key (当前未使用，保留供未来扩展)
# DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY")

# 禁用所有缓存
requests_cache.uninstall_cache()


# ===== SQLite 缓存层 =====
DB_PATH = "stock_cache.db"


def init_db():
    """初始化 SQLite 数据库，返回连接"""
    conn = sqlite3.connect(DB_PATH)
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
            "SELECT forward_pe, peg_ratio, trailing_pe, market_cap, earnings_date, ps_ratio, pb_ratio, analyst_rating, price_target FROM stock_analysis_data WHERE ticker=? AND date=?",
            (t, today),
        ).fetchone()
        if row:
            results[t] = {
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

def get_sp500_symbols():
    """获取标普500成分股"""
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

        html = response.text

        # ✅ 用 StringIO 包装
        df = pd.read_html(StringIO(html), attrs={"id": "constituents"})[0]

        sp500_symbols = df["Symbol"].tolist()
        return [symbol.replace(".", "-") for symbol in sp500_symbols]

    except Exception as e:
        print(f"获取标普500成分股失败: {e}")
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
            # MultiIndex格式（yf.download返回的格式）
            try:
                close_prices = data.xs('Close', axis=1, level=1)
            except Exception:
                try:
                    close_prices = data.xs('Adj Close', axis=1, level=1)
                except Exception:
                    print("无法提取Close价格数据")
                    return pd.DataFrame(columns=["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"])
        else:
            # 普通DataFrame格式（备用方法返回的格式）
            close_prices = data
            
        print(f"Close价格数据形状: {close_prices.shape}")
        
        # 删除全为NaN的列
        close_prices = close_prices.dropna(axis=1, how='all')
        print(f"删除全NaN列后形状: {close_prices.shape}")
        
        if close_prices.empty:
            print("Close价格数据为空")
            return pd.DataFrame(columns=["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"])
            
        # 确保有足够的数据点
        if len(close_prices) < 20:
            print("数据点不足20个，无法计算市场宽度")
            return pd.DataFrame(columns=["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"])

        # 计算移动平均线
        ma20 = close_prices.rolling(window=20, min_periods=1).mean()
        ma50 = close_prices.rolling(window=50, min_periods=1).mean()
        ma200 = close_prices.rolling(window=200, min_periods=1).mean()

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
        
        # 如果客户端没有提供分组，使用默认分组（可选）
        if not groups:
            return jsonify({
                "success": False, 
                "error": "股票代码分组数据为空，请提供有效的JSON格式股票代码分组"
            })
        base_date = datetime.date(datetime.date.today().year - 1, 12, 31).strftime('%Y-%m-%d')
        all_tickers = [t for group in groups.values() for t in group if t not in ["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"]]
        
        ny_tz = pytz.timezone('America/New_York')
        current_date_ny = datetime.datetime.now(ny_tz).date()
        earnings_df = pd.DataFrame(columns=["Next Earnings","Trailing PE","Forward PE","PEG Ratio","Market Cap","Analysts","Price Target"])
        
        # 获取财务数据
        tickers_obj = yf.Tickers(" ".join(all_tickers))

        # ===== 批量获取 StockAnalysis 数据（带 SQLite 缓存）=====
        # 在循环之前一次性查询，避免循环内逐只调用
        sa_query_tickers = [t for t in all_tickers
                           if t not in groups.get("Broad Market", [])
                           and t not in groups.get("Market Breadth", [])
                           and should_query_forward_pe(t)]
        print(f"[StockAnalysis] 开始批量查询 {len(sa_query_tickers)} 只股票...")
        sa_data_dict = get_cached_stock_analysis(sa_query_tickers)
        print(f"[StockAnalysis] 查询完成，获取到 {sum(1 for v in sa_data_dict.values() if v and v.get('forward_pe') is not None)}/{len(sa_query_tickers)} 个有效 Forward PE")

        for ticker_symbol in all_tickers:
            if ticker_symbol in groups.get("Broad Market", []) or ticker_symbol in groups.get("Market Breadth", []):
                continue
                
            try:
                ticker = tickers_obj.tickers[ticker_symbol]
                next_earnings_date = None
                
                # 获取财报日期
                calendar = ticker.calendar
                if calendar and 'Earnings Date' in calendar and calendar['Earnings Date']:
                    next_earnings_date = calendar['Earnings Date'][0]
                
                if next_earnings_date is None:
                    earnings_data = ticker.get_earnings_dates()
                    if earnings_data is not None and not earnings_data.empty:
                        unreported = earnings_data[earnings_data['Reported EPS'].isna()]
                        if not unreported.empty:
                            next_earnings_date = unreported.index[0]
                
                sa_data = sa_data_dict.get(ticker_symbol)

                # ===== Next Earnings：优先 StockAnalysis，缺省 fallback yfinance =====
                if sa_data is not None and sa_data.get("earnings_date"):
                    try:
                        ed_date = datetime.datetime.strptime(sa_data["earnings_date"], "%b %d, %Y").date()
                        if ed_date and ed_date >= current_date_ny:
                            earnings_df.loc[ticker_symbol,"Next Earnings"] = ed_date
                        else:
                            earnings_df.loc[ticker_symbol,"Next Earnings"] = None
                    except (ValueError, TypeError):
                        earnings_df.loc[ticker_symbol,"Next Earnings"] = None
                else:
                    # Fallback: yfinance
                    if next_earnings_date is not None:
                        if isinstance(next_earnings_date, pd.Timestamp):
                            earnings_date = next_earnings_date.date()
                        elif isinstance(next_earnings_date, datetime.datetime):
                            earnings_date = next_earnings_date.date()
                        elif isinstance(next_earnings_date, datetime.date):
                            earnings_date = next_earnings_date
                        else:
                            earnings_date = None
                        if earnings_date and earnings_date < current_date_ny:
                            earnings_df.loc[ticker_symbol,"Next Earnings"] = None
                        else:
                            earnings_df.loc[ticker_symbol,"Next Earnings"] = earnings_date
                    else:
                        earnings_df.loc[ticker_symbol,"Next Earnings"] = None

                # ===== Trailing PE：优先 StockAnalysis，缺省 fallback yfinance =====
                if sa_data is not None:
                    trail_pe = sa_data.get("trailing_pe")
                    if trail_pe is None:
                        trail_pe = ticker.info.get('trailingPE', None)
                    earnings_df.loc[ticker_symbol,"Trailing PE"] = trail_pe
                else:
                    earnings_df.loc[ticker_symbol,"Trailing PE"] = ticker.info.get('trailingPE', None)

                # ===== Forward PE、PEG Ratio、Analysts、Price Target =====
                if sa_data is not None:
                    earnings_df.loc[ticker_symbol,"Forward PE"] = sa_data.get("forward_pe")
                    # PEG Ratio：StockAnalysis 缺省时 fallback 到 yfinance
                    peg_val = sa_data.get("peg_ratio")
                    if peg_val is None:
                        peg_val = ticker.info.get('trailingPegRatio', None)
                    earnings_df.loc[ticker_symbol,"PEG Ratio"] = peg_val
                    earnings_df.loc[ticker_symbol,"Analysts"] = sa_data.get("analyst_rating")
                    earnings_df.loc[ticker_symbol,"Price Target"] = sa_data.get("price_target")
                else:
                    earnings_df.loc[ticker_symbol,"Forward PE"] = ticker.info.get('forwardPE', None)
                    earnings_df.loc[ticker_symbol,"PEG Ratio"] = ticker.info.get('trailingPegRatio', None)
                    earnings_df.loc[ticker_symbol,"Analysts"] = None
                    earnings_df.loc[ticker_symbol,"Price Target"] = None

                # ===== Market Cap：优先 StockAnalysis，缺省 fallback yfinance =====
                if sa_data is not None:
                    mcap = sa_data.get("market_cap")
                    if mcap is None:
                        mcap = ticker.info.get('marketCap', None)
                    earnings_df.loc[ticker_symbol,"Market Cap"] = mcap
                else:
                    earnings_df.loc[ticker_symbol,"Market Cap"] = ticker.info.get('marketCap', None)
                    
            except Exception as e:
                print(f"获取{ticker_symbol}财报日期失败: {e}")
                earnings_df.loc[ticker_symbol] = None

        # 获取价格数据（改为2y确保12个月计算有足够数据）
        df = yf.download(
            tickers=all_tickers,
            period="2y",
            interval="1d",
            auto_adjust=False,
            group_by="column",
            threads=True
        )

        if isinstance(df.columns, pd.MultiIndex):
            top_fields = {lvl[0] for lvl in df.columns}
            if 'Adj Close' in top_fields:
                adj_close = df['Adj Close'].copy()
            elif 'Close' in top_fields:
                adj_close = df['Close'].copy()
            else:
                adj_close = pd.DataFrame()
            volumes = df['Volume'].copy() if 'Volume' in top_fields else pd.DataFrame()
        else:
            if 'Adj Close' in df.columns:
                price_col = 'Adj Close'
            elif 'Close' in df.columns:
                price_col = 'Close'
            else:
                price_col = df.columns[0]
            only_ticker = all_tickers[0]
            adj_close = df[[price_col]].rename(columns={price_col: only_ticker})
            volumes = pd.DataFrame({only_ticker: df['Volume']}) if 'Volume' in df.columns else pd.DataFrame()

        results = []

        # 获取标普500的数据作为基准
        sp500_ticker = "^GSPC"

        # 总是尝试获取标普500的数据
        sp500_prices = None
        sp500_reference_dates = {}  # 存储参考日期：{63: date, 126: date, 252: date}
        
        try:
            print(f"正在下载 {sp500_ticker} 数据...")
            sp500_data = yf.download(sp500_ticker, period="2y", interval="1d", auto_adjust=False, progress=False)
            if not sp500_data.empty:
                if isinstance(sp500_data.columns, pd.MultiIndex):
                    if 'Adj Close' in sp500_data.columns:
                        sp500_prices = sp500_data['Adj Close'][sp500_ticker].dropna()
                    elif 'Close' in sp500_data.columns:
                        sp500_prices = sp500_data['Close'][sp500_ticker].dropna()
                else:
                    if 'Adj Close' in sp500_data.columns:
                        sp500_prices = sp500_data['Adj Close'].dropna()
                    elif 'Close' in sp500_data.columns:
                        sp500_prices = sp500_data['Close'].dropna()
                print(f"{sp500_ticker} 数据下载成功，共 {len(sp500_prices)} 个数据点")
                
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
            else:
                print(f"{sp500_ticker} 数据为空")
        except Exception as e:
            print(f"下载 {sp500_ticker} 数据失败: {e}")

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
                
                print(f"计算 {ticker} 的相对动量分数（日期对齐）...")
                
                # 计算3个月（63个交易日）的收益率
                if 63 in sp500_reference_dates:
                    ref_date_3m = sp500_reference_dates[63]
                    print(f"  参考日期 3M: {ref_date_3m.strftime('%Y-%m-%d')}")
                    
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
                        print(f"  M3M: 股票收益率={stock_return_3m:.2f}%, 标普500收益率={sp500_return_3m:.2f}%, 相对差={m3m:.2f}%")
                    else:
                        print(f"  M3M: 数据不足（sp500_price={sp500_price_3m}, stock_price={stock_price_3m})")
                
                # 计算6个月（126个交易日）的收益率
                if 126 in sp500_reference_dates:
                    ref_date_6m = sp500_reference_dates[126]
                    print(f"  参考日期 6M: {ref_date_6m.strftime('%Y-%m-%d')}")
                    
                    sp500_price_6m, actual_date_6m = get_price_on_date(sp500_prices, ref_date_6m)
                    
                    if actual_date_6m is not None:
                        stock_price_6m, _ = get_price_on_date(price_series, actual_date_6m)
                    else:
                        stock_price_6m, _ = get_price_on_date(price_series, ref_date_6m)
                    
                    if not pd.isna(sp500_price_6m) and not pd.isna(stock_price_6m):
                        sp500_return_6m = (sp500_prices.iloc[-1] / sp500_price_6m - 1) * 100
                        stock_return_6m = (price_series.iloc[-1] / stock_price_6m - 1) * 100
                        m6m = stock_return_6m - sp500_return_6m
                        print(f"  M6M: 股票收益率={stock_return_6m:.2f}%, 标普500收益率={sp500_return_6m:.2f}%, 相对差={m6m:.2f}%")
                    else:
                        print(f"  M6M: 数据不足")
                
                # 计算12个月（252个交易日）的收益率
                if 252 in sp500_reference_dates:
                    ref_date_12m = sp500_reference_dates[252]
                    print(f"  参考日期 12M: {ref_date_12m.strftime('%Y-%m-%d')}")
                    
                    sp500_price_12m, actual_date_12m = get_price_on_date(sp500_prices, ref_date_12m)
                    
                    if actual_date_12m is not None:
                        stock_price_12m, _ = get_price_on_date(price_series, actual_date_12m)
                    else:
                        stock_price_12m, _ = get_price_on_date(price_series, ref_date_12m)
                    
                    if not pd.isna(sp500_price_12m) and not pd.isna(stock_price_12m):
                        sp500_return_12m = (sp500_prices.iloc[-1] / sp500_price_12m - 1) * 100
                        stock_return_12m = (price_series.iloc[-1] / stock_price_12m - 1) * 100
                        m12m = stock_return_12m - sp500_return_12m
                        print(f"  M12M: 股票收益率={stock_return_12m:.2f}%, 标普500收益率={sp500_return_12m:.2f}%, 相对差={m12m:.2f}%")
                    else:
                        print(f"  M12M: 数据不足")
                
                # 计算相对动量分数
                if not (pd.isna(m3m) or pd.isna(m6m) or pd.isna(m12m)):
                    relative_momentum = 0.2 * m3m + 0.3 * m6m + 0.5 * m12m
                    print(f"  相对动量分数: {relative_momentum:.2f}")
                elif ticker == sp500_ticker:
                    # 标普500本身的相对动量分数应为0
                    relative_momentum = 0.0
                    m3m = 0.0
                    m6m = 0.0
                    m12m = 0.0
                    print(f"  {ticker} 是标普500，相对动量分数设为 0.0")
                else:
                    print(f"  无法计算相对动量分数: M3M={m3m}, M6M={m6m}, M12M={m12m}")
            else:
                sp500_status = '有效' if sp500_prices is not None and isinstance(sp500_prices, pd.Series) and len(sp500_prices) > 0 else '无效'
                ref_dates_status = f'{len(sp500_reference_dates)} 个参考日期' if len(sp500_reference_dates) > 0 else '无参考日期'
                print(f"跳过 {ticker} 的相对动量计算: sp500_prices={sp500_status}, 参考日期={ref_dates_status}, price_series长度={len(price_series)}")

            # ===== Beta（相对于 ^GSPC，带 SQLite 缓存，统一用过去1年数据）=====
            beta = np.nan
            today_str = get_market_date()
            beta_cached = get_cached_beta(ticker, today_str)
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
                            save_beta(ticker, today_str, beta, len(common_idx))

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

        return jsonify({"success": True, "data": results})
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/breadth_data', methods=['POST'])
def get_breadth_data():
    """获取市场宽度数据"""
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
        
        # 下载标普500数据
        try:
            sp500_data = yf.download(
                tickers=sp500_symbols,
                period="1y",
                group_by='ticker',
                auto_adjust=True,
                progress=False,
                threads=True
            )
            print(f"标普500数据下载完成，数据形状: {sp500_data.shape}")
        except Exception as e:
            return jsonify({"success": False, "error": f"下载标普500数据失败: {str(e)}"})
        
        # 检查数据是否为空
        if sp500_data is None or sp500_data.empty:
            return jsonify({"success": False, "error": "标普500数据为空"})
        
        # 计算市场宽度
        breadth_df = calculate_market_breadth(sp500_data, sp500_symbols)
        
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

        # 准备图表数据
        if hasattr(breadth_df.index, 'strftime') and callable(getattr(breadth_df.index, 'strftime', None)):
            index_list = breadth_df.index.strftime('%Y-%m-%d').tolist()
        else:
            try:
                index_list = pd.to_datetime(breadth_df.index).strftime('%Y-%m-%d').tolist()
            except:
                index_list = []
        
        breadth_data = {
            "index": index_list,
            "20MA_Ratio": [round(float(x), 2) if not np.isnan(x) else 0 for x in breadth_df["20MA_Ratio"]] if "20MA_Ratio" in breadth_df.columns else [],
            "50MA_Ratio": [round(float(x), 2) if not np.isnan(x) else 0 for x in breadth_df["50MA_Ratio"]] if "50MA_Ratio" in breadth_df.columns else [],
            "200MA_Ratio": [round(float(x), 2) if not np.isnan(x) else 0 for x in breadth_df["200MA_Ratio"]] if "200MA_Ratio" in breadth_df.columns else []
        }
        
        return jsonify({"success": True, "data": results, "breadth_chart_data": breadth_data})
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/kline_data', methods=['GET'])
def get_kline_data():
    """获取K线图数据"""
    try:
        ticker = request.args.get('ticker', '').upper()
        time_span = int(request.args.get('period', 365))
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
            stock_data = ticker_info.history(period="60d", interval = interval)
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
        response = requests.get(url)
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
    
    # 下载标普500数据
    try:
        sp500_data = yf.download(
            tickers=sp500_symbols,
            period="1y",
            group_by='ticker',
            auto_adjust=True,
            progress=False,
            threads=True
        )
        print(f"标普500数据下载完成，数据形状: {sp500_data.shape}")
        return jsonify({"success": True, "error": "下载标普500数据成功"})
    except Exception as e:
        return jsonify({"success": False, "error": f"下载标普500数据失败: {str(e)}"})
    
# if __name__ == "__main__":
#     app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
