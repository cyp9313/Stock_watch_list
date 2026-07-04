import tkinter as tk
import pandas as pd
import numpy as np
import tksheet
import datetime
import matplotlib
import matplotlib.gridspec as gridspec
import matplotlib.dates as mdates
matplotlib.use('TkAgg')
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import matplotlib.pyplot as plt
from matplotlib.widgets import Cursor
import mplfinance as mpf
from tkinter import messagebox
import requests
import webbrowser
from io import StringIO
import colorsys
import json
import threading
import os
import sys
import ctypes
#fafasdf
# ===== 屏蔽 C 层 stderr（消除 libpng iCCP warning）=====
# 必须在 import stock_watch_list_back_end 之前执行：
# 1) C 层 freopen 把 C FILE* stderr → NUL，压制 libpng 的 C 层 fprintf(stderr,...)
# 2) 但 freopen 会让 Python sys.stderr 底层的 fd 2 失效，导致 yfinance/Flask
#    写日志时报 I/O 错误 → 股票数据请求静默失败
# 3) 解决：freopen 前用 os.dup(2) 保存控制台 stderr fd，
#    freopen 后用 os.fdopen 重建 Python sys.stderr → 控制台
if sys.platform == 'win32':
    try:
        _saved_stderr_fd = os.dup(2)           # 保存控制台 stderr fd
        _ucrt = ctypes.CDLL('ucrtbase.dll')
        _ucrt.__acrt_iob_func.restype = ctypes.c_void_p
        _ucrt.__acrt_iob_func.argtypes = [ctypes.c_int]
        _ucrt.freopen.restype = ctypes.c_void_p
        _ucrt.freopen.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p]
        _ucrt.fflush.restype = ctypes.c_int
        _ucrt.fflush.argtypes = [ctypes.c_void_p]
        _stderr_ptr = _ucrt.__acrt_iob_func(2)
        _ucrt.fflush(_stderr_ptr)
        _ucrt.freopen(b'NUL', b'w', _stderr_ptr)  # C stderr → NUL
        # 重建 Python sys.stderr → 控制台（yfinance/Flask 日志正常工作）
        sys.stderr = os.fdopen(_saved_stderr_fd, 'w', encoding='utf-8', errors='replace', buffering=1)
    except Exception:
        pass

import stock_watch_list_back_end

def run_flask():
    stock_watch_list_back_end.app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)

t = threading.Thread(target=run_flask, daemon=True)
t.start()

# API配置
# API_BASE_URL = "http://43.157.122.165:6000"  # 修改为你的服务器地址
API_BASE_URL = "http://127.0.0.1:5000"  # 修改为你的服务器地址
# 分组配置
groups = {
    "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA","SPCX"],
    "Chips/AI": ["ORCL","AMD","INTC","AVGO","SMCI","PLTR","RGTI","DXYZ","SNPS","APP"],
    "Fin/Crypto": ["V","JPM","BRK-B","COIN","HOOD","MSTR","CRCL","SOFI","OSCR"],
    "Health": ["LLY","NVO","ABBV","UNH"],
    "Energy": ["SMR","VST","OKLO","NEE","ENPH","GE","GEV"],
    "Defense": ["LMT","BA","ACHR","AXON"],
    "Consumer": ["LULU","NKE","CMG","COST"],
    "China": ["BYDDY","XIACY","PDD","BABA","TCEHY","BIDU"],
    "Themes": ["ASTS","CRWV","NBIS","MP","RKLB"],
    "Broad Market": [
        "^GSPC","^NDX","^DJI","^RUT","510300.SS",
        "RSP","QQQE","TQQQ","WNUC.DE","REMX","^TNX",
        "EURUSD=X","GC=F","SI=F","BZ=F",
        "BTC-USD","ETH-USD","^VIX","^VXN"
    ],
    "Market Breadth": ["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"]
}

# ===== 从wikipedia上爬取最新标普500股票代码列表 =====
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

sp500_symbols = get_sp500_symbols()

# ===== API调用函数 =====
def fetch_from_api(endpoint, params=None):
    """调用API获取数据"""
    try:
        response = requests.get(f"{API_BASE_URL}{endpoint}", params=params, timeout=120)
        return response.json()
    except Exception as e:
        print(f"API调用失败: {e}")
        return {"success": False, "error": str(e)}

# ===== 颜色映射函数 =====
def red_green(value, neg_clip=-10.0, pos_clip=10.0):
    if pd.isna(value):
        return "white"
    v = max(neg_clip, min(pos_clip, float(value)))
    if v >= 0:
        t = v / pos_clip if pos_clip != 0 else 0.0
        r = int(255 - 40 * t)
        g = 255
        b = int(255 - 40 * t)
    else:
        t = abs(v) / abs(neg_clip) if neg_clip != 0 else 0.0
        r = 255
        g = int(255 - 40 * t)
        b = int(255 - 40 * t)
    return f"#{r:02x}{g:02x}{b:02x}"

def blue_color(value, clip=3.0):
    if pd.isna(value):
        return "white"
    v = min(clip, max(0, float(value)))
    t = v / clip
    r = int(200 - 50 * t)
    g = int(200 - 50 * t)
    b = int(255 - 55 * t)
    return f"#{r:02x}{g:02x}{b:02x}"

def get_earnings_color(days_until):
    """财报日期颜色渲染"""
    if days_until < 0:
        return None
    
    start_hue = 0.0
    end_hue = 120.0
    hue = start_hue + (end_hue - start_hue) * min(days_until / 60.0, 1.0)
    saturation = 1.0
    value = 1.0
    
    r, g, b = colorsys.hsv_to_rgb(hue / 360.0, saturation, value)
    r, g, b = int(r * 255), int(g * 255), int(b * 255)
    return f"#{r:02x}{g:02x}{b:02x}"

def analyst_rating_color(rating):
    """分析师评级颜色映射"""
    if rating is None or (isinstance(rating, float) and pd.isna(rating)) or rating == "":
        return "white"
    rating_lower = str(rating).lower().strip()
    colors = {
        "strong buy": "#006400",   # 深绿
        "buy": "#90EE90",           # 浅绿
        "hold": "#FFFFE0",          # 浅黄
        "sell": "#FFA07A",          # 浅红/橙
        "strong sell": "#8B0000",   # 深红
    }
    return colors.get(rating_lower, "white")

def price_target_color(target_price, current_price):
    """Price Target 颜色：基于相对当前股价的涨跌幅"""
    if target_price is None or current_price is None:
        return "white"
    if pd.isna(target_price) or pd.isna(current_price):
        return "white"
    if float(current_price) == 0:
        return "white"
    upside_pct = (float(target_price) - float(current_price)) / float(current_price) * 100.0
    # 持平（0%附近）为白色，正越多越绿，负越多越红
    return red_green(upside_pct, neg_clip=-50.0, pos_clip=50.0)

def beta_color(beta):
    """Beta 颜色映射：beta=1 为白色，beta>1 越红，beta<1 越绿"""
    if pd.isna(beta) or beta is None:
        return "white"
    beta = float(beta)
    dev = max(-1.0, min(1.0, beta - 1.0))
    if dev >= 0:
        t = dev
        r = 255
        g = int(255 - 100 * t)
        b = int(255 - 100 * t)
    else:
        t = abs(dev)
        r = int(255 - 100 * t)
        g = 255
        b = int(255 - 100 * t)
    return f"#{r:02x}{g:02x}{b:02x}"

# ===== 列定义 =====
COLUMNS = (
    ["Ticker", "Price", "1D%", "5D%", "1M%", "YTD%", "Rel. Momentum"] +
    [f"Diff_EMA{n}%" for n in [5, 10, 20, 50, 100, 200]] +
    ["Diff_BB_Up%", "Diff_BB_Low%", "Volume_Ratio","Next Earnings","Trailing PE","Forward PE","PEG Ratio","Analysts","Price Target","Market Cap"]
)

# ===== 渲染表格 =====
def render_table(df: pd.DataFrame):
    current_date = pd.Timestamp.now(tz='America/New_York').date()
    table_data = []
    cell_bg = {}
    r = 0

    for grp_name, tickers in groups.items():
        title_row = [grp_name] + [""] * (len(COLUMNS) - 1)
        table_data.append(title_row)
        for c in range(len(COLUMNS)):
            cell_bg[(r, c)] = "#cccccc"
        r += 1

        df_grp = df[df["Ticker"].isin(tickers)].set_index("Ticker") if not df.empty else pd.DataFrame()

        for tk_ in tickers:
            if df_grp is None or df_grp.empty or tk_ not in df_grp.index:
                row_vals = [tk_] + [""] * (len(COLUMNS) - 1)
                table_data.append(row_vals)
                r += 1
                continue

            row = df_grp.loc[tk_]
            row_vals = []
            for j, col in enumerate(COLUMNS):
                val = row[col] if col in row else np.nan

                if tk_ in ["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"]:
                    if col in ["Ticker", "Price", "1D%", "5D%", "1M%"]:
                        disp = tk_ if col == "Ticker" else (f"{float(val):.2f}" if pd.notna(val) else "")
                    else:
                        disp = ""
                else:
                    if col == "Ticker":
                        disp = tk_
                    elif col == "Rel. Momentum":
                        disp = f"{float(val):.2f}" if pd.notna(val) else ""
                    elif col in ("Price", "Volume_Ratio"):
                        disp = f"{float(val):.2f}" if pd.notna(val) else ""
                    elif col == "Next Earnings":
                        disp = val if isinstance(val, str) else val.strftime('%Y-%m-%d') if not pd.isna(val) else ""
                    elif col == "Analysts":
                        disp = str(val) if pd.notna(val) and val is not None else ""
                    elif col == "Price Target":
                        disp = f"${float(val):.2f}" if pd.notna(val) and val is not None else ""
                    elif col == "Market Cap":
                        disp = f"{float(val):.2e}" if pd.notna(val) else ""
                    else:
                        disp = f"{float(val):.2f}" if pd.notna(val) else ""

                row_vals.append(disp)

                if col == "Ticker":
                    beta_val = row.get("Beta", np.nan)
                    cell_bg[(r, j)] = beta_color(beta_val)
                elif pd.notna(val) and disp != "" and col != "Price":
                    if col == "Volume_Ratio":
                        cell_bg[(r, j)] = blue_color(val)
                    elif col == "Rel. Momentum":
                        cell_bg[(r, j)] = red_green(val, neg_clip=-50.0, pos_clip=50.0)
                    elif col == "Next Earnings":
                        if isinstance(val, str):
                            try:
                                earnings_date = datetime.datetime.strptime(val, '%Y-%m-%d').date()
                                days_until = (earnings_date - current_date).days
                                color = get_earnings_color(days_until)
                                if color:
                                    cell_bg[(r, j)] = color
                            except:
                                pass
                    elif col == "Analysts":
                        cell_bg[(r, j)] = analyst_rating_color(val)
                    elif col == "Price Target":
                        current_price = row.get("Price", np.nan)
                        cell_bg[(r, j)] = price_target_color(val, current_price)
                    elif col in ("Trailing PE","Forward PE"):
                        cell_bg[(r, j)] = blue_color(val if val>0 else 50, clip=50.0)
                    elif col == "PEG Ratio":
                        cell_bg[(r, j)] = blue_color(val if val>0 else 5.0, clip=5.0)
                    elif col == "Market Cap":
                        cell_bg[(r, j)] = blue_color(val, clip=1e12)
                    else:
                        cell_bg[(r, j)] = red_green(val)

            table_data.append(row_vals)
            r += 1

    sheet.set_sheet_data(table_data)
    for (ri, ci), color in cell_bg.items():
        sheet.highlight_cells(row=ri, column=ci, bg=color)
    sheet.set_column_widths([120]+[100] * (len(COLUMNS)-1))

# ===== 数据刷新函数 =====
def refresh_stock_data():
    global latest_stock_df
    try:
        result = fetch_from_api('/api/stock_data',{'groups': json.dumps(groups)})   
        if result.get('success'):
            latest_stock_df = pd.DataFrame(result['data'])
            title_var.set("US Stock Watchlist - Stocks Updated")
        else:
            raise Exception(result.get('error', 'Unknown error'))
    except Exception as e:
        latest_stock_df = pd.DataFrame(columns=COLUMNS)
        title_var.set(f"Stock Data ⚠⚠⚠️ {e}")
    
    combined = pd.concat([latest_stock_df, latest_breadth_df], ignore_index=True)
    render_table(combined)
    refresh_label.config(text=f"Last Stock Refresh: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    update_fear_greed_index()
    update_crypto_fear_greed_index()

def refresh_breadth_data():
    global latest_breadth_df, breadth_chart_data
    try:
        # result = fetch_from_api('/api/breadth_data')
        # result = fetch_from_api('/api/breadth_data',{'sp500_symbols': json.dumps(sp500_symbols)})   
        """调用API获取数据"""
        try:
            endpoint = '/api/breadth_data'
            response = requests.post(f"{API_BASE_URL}{endpoint}", data={'sp500_symbols': json.dumps(sp500_symbols)}, timeout=120)
            result = response.json()
        except Exception as e:
            print(f"API调用失败: {e}")
            result = {"success": False, "error": str(e)}
        if result.get('success'):
            latest_breadth_df = pd.DataFrame(result['data'])
            breadth_chart_data = result.get('breadth_chart_data', {})
            title_var.set("US Stock Watchlist - Breadth Updated")
        else:
            raise Exception(result.get('error', 'Unknown error'))
    except Exception as e:
        latest_breadth_df = pd.DataFrame(columns=COLUMNS)
        title_var.set(f"Breadth Data ⚠⚠⚠️ {e}")
    
    combined = pd.concat([latest_stock_df, latest_breadth_df], ignore_index=True)
    render_table(combined)
    refresh_label.config(text=f"Last Breadth Refresh: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

def update_fear_greed_index():
    try:
        result = fetch_from_api('/api/fear_greed')
        if result.get('success'):
            index_value = result['value']
            description = result['description']
            
            if index_value <= 25:
                color = "blue"
            elif index_value <= 45:
                color = "cyan"
            elif index_value <= 55:
                color = "green"
            elif index_value <= 75:
                color = "orange"
            else:
                color = "red"
                
            fear_greed_var.set(f"CNN股票恐惧贪婪指数: {index_value:.2f} ({description})")
            fear_greed_label.config(fg=color)
        else:
            fear_greed_var.set(f"CNN股票恐惧贪婪指数: 获取失败")
            fear_greed_label.config(fg="black")
    except Exception as e:
        fear_greed_var.set(f"CNN股票恐惧贪婪指数: 获取失败 ({str(e)})")
        fear_greed_label.config(fg="black")

def update_crypto_fear_greed_index():
    try:
        result = fetch_from_api('/api/fear_greed_crypto')
        if result.get('success'):
            index_value = result['value']
            description = result['description']
            
            if index_value <= 25:
                color = "blue"
            elif index_value <= 45:
                color = "cyan"
            elif index_value <= 55:
                color = "green"
            elif index_value <= 75:
                color = "orange"
            else:
                color = "red"
                
            fear_greed_crypto_var.set(f"Crypto恐惧贪婪指数: {index_value:.2f} ({description})")
            fear_greed_crypto_label.config(fg=color)
        else:
            fear_greed_crypto_var.set(f"Crypto恐惧贪婪指数: 获取失败")
            fear_greed_crypto_label.config(fg="black")
    except Exception as e:
        fear_greed_crypto_var.set(f"Crypto恐惧贪婪指数: 获取失败 ({str(e)})")
        fear_greed_crypto_label.config(fg="black")

# 计算斐波那契回撤水平线
def fibonacci_retracement_levels(max_price, min_price):
    diff = max_price - min_price
    levels = [max_price - diff * ratio for ratio in [0, 0.236, 0.382, 0.5, 0.618, 1]]
    return levels

# 计算神奇九转 (TD Sequential 简化版)
def calculate_td_sequential(closes):
    """
    神奇九转 (TD Sequential)
    买入结构: 连续收盘价 < 4根前收盘价，计数1-9，到9则潜在底部
    卖出结构: 连续收盘价 > 4根前收盘价，计数1-9，到9则潜在顶部
    Returns: list of (index, count, 'buy'/'sell')
    """
    results = []
    buy_count = 0
    sell_count = 0
    for i in range(4, len(closes)):
        if closes[i] < closes[i - 4]:
            buy_count += 1
            sell_count = 0
            if buy_count <= 9:
                results.append((i, buy_count, 'buy'))
            if buy_count >= 9:
                buy_count = 0
        elif closes[i] > closes[i - 4]:
            sell_count += 1
            buy_count = 0
            if sell_count <= 9:
                results.append((i, sell_count, 'sell'))
            if sell_count >= 9:
                sell_count = 0
        else:
            buy_count = 0
            sell_count = 0
    return results

# ===== K线图绘制函数 =====
def plot_kline():
    ticker = ticker_entry.get().strip().upper()
    try:
        time_span = int(ticker_entry2.get().strip())
    except:
        time_span = 365
    
    try:
        interval = ticker_entry3.get().strip()
    except:
        interval = "1d"

    if not ticker:
        messagebox.showwarning("输入错误", "请输入股票代码")
        return

    try:
        result = fetch_from_api('/api/kline_data', {'ticker': ticker, 'period': time_span, 'interval': interval})
        if not result.get('success') and ticker not in ['20MA_RATIO', '50MA_RATIO', '200MA_RATIO']:
            messagebox.showerror("错误", result.get('error', 'Unknown error'))
            return
        data = result
        
        # 创建新窗口
        kline_window = tk.Toplevel(root)
        kline_window.title(f"{ticker} K线图")
        kline_window.geometry("1200x800")

        if ticker in ['20MA_RATIO', '50MA_RATIO', '200MA_RATIO']:
            # 市场宽度图表
            fig = plt.figure(figsize=(12, 8))
            ax = fig.add_subplot(1, 1, 1)
            
            if breadth_chart_data:
                dates = [datetime.datetime.strptime(d, '%Y-%m-%d') for d in breadth_chart_data['index']]
                ax.plot(dates, breadth_chart_data['20MA_Ratio'], label='Market_Breadth_MAV20', color='blue')
                ax.plot(dates, breadth_chart_data['50MA_Ratio'], label='Market_Breadth_MAV50', color='red')
                ax.plot(dates, breadth_chart_data['200MA_Ratio'], label='Market_Breadth_MAV200', color='orange')
            
            ax.axhline(y=15, color='gray', label='lower boundary line',linestyle='--', linewidth=1)
            ax.axhline(y=85, color='gray', label='higher boundary line',linestyle='--', linewidth=1)
            ax.set_title('Market Breadth (Last Year) in %')
            ax.set_ylabel('Market Breadth (%)')
            ax.legend()
            ax.grid(True)
            plt.tight_layout()

        else:
            # 股票K线图
            fig = plt.figure(figsize=(12, 14))
            # 创建6行2列的网格
            gs = gridspec.GridSpec(6, 2, width_ratios=[4, 1], height_ratios=[4, 1, 1, 1, 1, 1], wspace=0.1, hspace=0.1)
            
            ax1 = fig.add_subplot(gs[0,0])  # K线图
            ax2 = fig.add_subplot(gs[1,0], sharex=ax1)  # 成交量
            ax3 = fig.add_subplot(gs[2,0], sharex=ax1)  # MACD
            ax4 = fig.add_subplot(gs[3,0], sharex=ax1)  # KDJ
            ax5 = fig.add_subplot(gs[4,0], sharex=ax1)  # RSI
            ax6 = fig.add_subplot(gs[5,0], sharex=ax1)  # 神奇九转
            # 右列：筹码峰图（只占据第0行右列，高度与K线图一致）
            ax_chip = fig.add_subplot(gs[0, 1],sharey=ax1) # 筹码峰图 - 第0行，右列

            # 转换数据格式
            if "m" in interval:
                dates = [datetime.datetime.strptime(d, '%Y-%m-%d %H:%M') for d in data['dates']]
            else:
                dates = [datetime.datetime.strptime(d[:10], '%Y-%m-%d') for d in data['dates']]
            ohlc_data = pd.DataFrame({
                'Open': data['ohlc']['open'],
                'High': data['ohlc']['high'], 
                'Low': data['ohlc']['low'],
                'Close': data['ohlc']['close'],
                'Volume': data['ohlc']['volume']
            }, index=pd.DatetimeIndex(dates))

            # 绘制K线
            mpf.plot(ohlc_data, type='candle', volume=ax2, ax=ax1, show_nontrading=True, style='charles',scale_width_adjustment=dict(candle=0.5,volume=0.5))
            
            # 添加技术指标
            ax1.plot(dates, data['indicators']['ma5'], label='MA5', linestyle='--', linewidth=1)
            ax1.plot(dates, data['indicators']['ma10'], label='MA10', linestyle='--', linewidth=1)
            ax1.plot(dates, data['indicators']['ma20'], label='MA20', linestyle='--', linewidth=1)
            ax1.plot(dates, data['indicators']['ma50'], label='MA50', linestyle='--', linewidth=1)
            ax1.plot(dates, data['indicators']['ma100'], label='MA100', linestyle='--', linewidth=1)
            ax1.plot(dates, data['indicators']['ma200'], label='MA200', linestyle='--', linewidth=1)
            ax1.plot(dates, data['indicators']['bollinger_upper'], label='BB Upper', linewidth=1)
            ax1.plot(dates, data['indicators']['bollinger_lower'], label='BB Lower', linewidth=1)
            ax1.axhline(data['indicators']['chip_peak_price'], color='gray', linestyle='--', linewidth=1,label=f"Peak of Chip:{data['indicators']['chip_peak_price']:.2f}")
            ax1.plot(ohlc_data.index[-1], ohlc_data['Close'][-1], 'rx', markersize=5, label=f"Latest ({ohlc_data.index[-1].date()}:{ohlc_data['Close'][-1]:.2f})")
            # 交互功能
            class FibonacciSelector:
                def __init__(self, ax, dates):
                    self.ax = ax
                    self.dates = dates
                    self.max_price = None   # B 点（波段高点）
                    self.min_price = None   # A 点（波段低点）
                    self.ext_price = None   # C 点（回撤结束点，Extension 的 0% 基准）
                    self.selecting_max = False
                    self.selecting_min = False
                    self.selecting_ext = False
                    self.fib_lines = []     # 保存已画的线对象
                    self.fib_texts = []     # 保存已画的文字对象
                    self.cursor = Cursor(ax, useblit=True, color='red', linewidth=1)
                    self.cid = ax.figure.canvas.mpl_connect('button_press_event', self)
                    self.kid = ax.figure.canvas.mpl_connect('key_press_event', self.on_key)

                def clear_fib_lines(self):
                    """清除所有斐波那契线和文字"""
                    for line in self.fib_lines:
                        if line in self.ax.lines:
                            line.remove()
                    for txt in self.fib_texts:
                        if txt in self.ax.texts:
                            txt.remove()
                    self.fib_lines.clear()
                    self.fib_texts.clear()

                def on_key(self, event):
                    if event.key == 'a':
                        print('Select A (swing low)')
                        self.selecting_min = True
                        self.selecting_max = False
                        self.selecting_ext = False
                    elif event.key == 'b':
                        print('Select B (swing high)')
                        self.selecting_max = True
                        self.selecting_min = False
                        self.selecting_ext = False
                    elif event.key == 'c':
                        print('Select C (pullback end, Extension 0% base)')
                        if self.min_price is not None and self.max_price is not None:
                            self.selecting_ext = True
                            self.selecting_min = False
                            self.selecting_max = False
                        else:
                            print('Please select A and B first (press a and b)')

                def __call__(self, event):
                    if event.inaxes != self.ax:
                        return
                    if self.selecting_min:
                        self.min_price = event.ydata
                        self.max_price = None
                        self.ext_price = None
                        self.clear_fib_lines()
                        self.selecting_min = False
                        print(f'A selected: {self.min_price:.2f}')
                        self.ax.figure.canvas.draw()
                    elif self.selecting_max:
                        self.max_price = event.ydata
                        self.ext_price = None
                        self.selecting_max = False
                        print(f'B selected: {self.max_price:.2f}')
                        if self.min_price is not None:
                            self.draw_fibonacci_levels()
                    elif self.selecting_ext:
                        self.ext_price = event.ydata
                        self.selecting_ext = False
                        print(f'C selected: {self.ext_price:.2f}')
                        if self.min_price is not None and self.max_price is not None:
                            self.draw_fibonacci_extension()

                def draw_fibonacci_levels(self):
                    """画斐波那契回撤线（a + b）：0% 在 max_price，100% 在 min_price"""
                    self.clear_fib_lines()
                    ylim = self.ax.get_ylim()
                    x_pos = self.dates[-1]
                    diff = self.max_price - self.min_price
                    ratios  = [0, 0.236, 0.382, 0.5, 0.618, 1.0]
                    labels  = ['0%', '23.6%', '38.2%', '50%', '61.8%', '100%']
                    for r, label in zip(ratios, labels):
                        level = self.max_price - diff * r
                        line = self.ax.axhline(y=level, color='gray', linestyle='--', linewidth=1,
                                               alpha=0.8)
                        self.fib_lines.append(line)
                        txt = self.ax.text(
                            x_pos, level,
                            f'  {level:.2f} ({label})',
                            color='gray', fontsize=8,
                            va='center', ha='left',
                            clip_on=True,
                            bbox=dict(boxstyle='round,pad=1', facecolor='white',
                                      edgecolor='none', alpha=0.7)
                        )
                        self.fib_texts.append(txt)
                    self.ax.set_ylim(ylim)
                    self.ax.figure.canvas.draw()

                def draw_fibonacci_extension(self):
                    """画斐波那契扩展线（a + b + c）
                       Level = C + (B - A) × ratio
                       0% 线在 C 点位置，1.0 以内显示典型回撤比例
                    """
                    self.clear_fib_lines()
                    ylim = self.ax.get_ylim()
                    x_pos = self.dates[-1]
                    diff = self.max_price - self.min_price
                    # 0% = C，1.0 = C + diff；0-1 之间也显示典型回撤比例
                    ext_ratios  = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0,
                                   1.272, 1.618, 2.0, 2.618]
                    ext_labels  = ['0%', '23.6%', '38.2%', '50%', '61.8%', '78.6%', '100%',
                                   '127.2%', '161.8%', '200%', '261.8%']
                    for r, label in zip(ext_ratios, ext_labels):
                        level = self.ext_price + diff * r
                        color = 'blue' if r >= 1.0 else 'gray'
                        line = self.ax.axhline(y=level, color=color, linestyle='--', linewidth=1,
                                               alpha=0.8)
                        self.fib_lines.append(line)
                        txt = self.ax.text(
                            x_pos, level,
                            f'  {level:.2f} ({label})',
                            color=color, fontsize=8,
                            va='center', ha='left',
                            clip_on=True,
                            bbox=dict(boxstyle='round,pad=1', facecolor='white',
                                      edgecolor='none', alpha=0.7)
                        )
                        self.fib_texts.append(txt)
                    self.ax.set_ylim(ylim)
                    self.ax.figure.canvas.draw()
            fib_selector = FibonacciSelector(ax1, dates)

            # 重写 ax1 的 format_coord，让工具栏显示最近 K 线的 OHLC 信息
            _date_nums = np.array([mdates.date2num(d) for d in dates])
            _opens = data['ohlc']['open']
            _highs = data['ohlc']['high']
            _lows = data['ohlc']['low']
            _closes = data['ohlc']['close']
            _vols = data['ohlc']['volume']
            _n = len(_date_nums)

            def _make_format_coord(orig_format):
                def _format_coord(x, y):
                    # 二分找最近的 K 线
                    i = np.searchsorted(_date_nums, x, side='left')
                    if i >= _n:
                        i = _n - 1
                    elif i > 0 and (x - _date_nums[i-1]) < (_date_nums[i] - x):
                        i = i - 1
                    if hasattr(dates[i], 'strftime'):
                        ds = dates[i].strftime('%Y-%m-%d')
                    else:
                        ds = str(dates[i])
                    return (f"{ds}  |  "
                            f"O:{_opens[i]:.2f}  H:{_highs[i]:.2f}  "
                            f"L:{_lows[i]:.2f}  C:{_closes[i]:.2f}  "
                            f"Vol:{_vols[i]:,.0f}")
                return _format_coord
            ax1.format_coord = _make_format_coord(ax1.format_coord)

            ax1.set_title(f"K-Curve {ticker} | Market Cap: {data['financials']['market_cap']}, PE: {data['financials']['trailing_pe']}/{data['financials']['forward_pe']}, P/S: {data['financials']['price_to_sales']}, P/B: {data['financials']['price_to_book']}, PEG: {data['financials']['peg_ratio']}, Next Earnings: {data['financials']['next_earnings']}, Analysts: {data['financials'].get('analyst_rating', 'N/A')}, Target: {data['financials'].get('price_target', 'N/A')}")

            # StockAnalysis 链接（可点击跳转浏览器）
            sa_url = f"https://stockanalysis.com/stocks/{ticker.replace('-', '.')}/"
            link_text = fig.text(0.99, 0.985, sa_url,
                                 ha='right', va='top',
                                 fontsize=7, color='blue', style='italic')

            def _on_sa_link_click(event, _url=sa_url, _txt=link_text):
                # 只处理 axes 外的点击（标题/边缘区域）
                if event.inaxes is not None:
                    return
                try:
                    renderer = fig.canvas.get_renderer()
                except Exception:
                    return
                bbox = _txt.get_window_extent(renderer=renderer)
                if bbox.contains(event.x, event.y):
                    webbrowser.open(_url)

            fig.canvas.mpl_connect('button_press_event', _on_sa_link_click)
            ax1.legend()
            ax1.grid(True)
            ax2.tick_params(axis='x', labelbottom=False) 
            ax2.grid(True)

            # MACD
            ax3.plot(dates, data['indicators']['macd'], label='MACD', color='blue')
            ax3.plot(dates, data['indicators']['signal'], label='Signal', color='red')
            ax3.set_ylabel('MACD')
            ax3.tick_params(axis='x', labelbottom=False)  
            ax3.legend()
            ax3.grid(True)

            # KDJ
            ax4.plot(dates, data['indicators']['kdj_k'], label='K', color='blue')
            ax4.plot(dates, data['indicators']['kdj_d'], label='D', color='orange')
            ax4.plot(dates, data['indicators']['kdj_j'], label='J', color='green')
            ax4.axhline(20, color='gray', linestyle='--', linewidth=1)
            ax4.axhline(80, color='gray', linestyle='--', linewidth=1)
            ax4.set_ylabel('KDJ')
            ax4.tick_params(axis='x', labelbottom=False)  
            ax4.legend()
            ax4.grid(True)

            # RSI
            ax5.plot(dates, data['indicators']['rsi'], label='RSI', color='purple')
            ax5.axhline(30, color='gray', linestyle='--', linewidth=1)
            ax5.axhline(70, color='gray', linestyle='--', linewidth=1)
            ax5.set_ylabel('RSI')
            ax5.tick_params(axis='x', labelbottom=False)
            ax5.legend()
            ax5.grid(True)

            # 神奇九转 (TD Sequential)
            closes = data['ohlc']['close']
            highs = data['ohlc']['high']
            lows = data['ohlc']['low']
            td_results = calculate_td_sequential(closes)

            # 在主图（ax1）上标注数字：卖出序列在K线上方标红，买入序列在K线下方标绿
            for idx, count, td_type in td_results:
                if td_type == 'sell':
                    ax1.text(dates[idx], highs[idx] * 1.003, str(count),
                             color='red', fontsize=7, ha='center', va='bottom',
                             fontweight='bold' if count == 9 else 'normal',
                             clip_on=True)
                else:
                    ax1.text(dates[idx], lows[idx] * 0.997, str(count),
                             color='green', fontsize=7, ha='center', va='top',
                             fontweight='bold' if count == 9 else 'normal',
                             clip_on=True)

            # 在子图（ax6）上绘制柱状图
            for idx, count, td_type in td_results:
                if td_type == 'sell':
                    ax6.bar(dates[idx], count, width=0.6, color='red', alpha=0.7)
                    ax6.text(dates[idx], count, str(count), color='red', fontsize=7,
                             ha='center', va='bottom', fontweight='bold' if count == 9 else 'normal',
                             clip_on=True)
                else:
                    ax6.bar(dates[idx], -count, width=0.6, color='green', alpha=0.7)
                    ax6.text(dates[idx], -count, str(count), color='green', fontsize=7,
                             ha='center', va='top', fontweight='bold' if count == 9 else 'normal',
                             clip_on=True)

            ax6.axhline(0, color='gray', linewidth=0.5)
            ax6.set_ylim(-10, 10)
            ax6.set_yticks([-9, -5, 0, 5, 9])
            ax6.set_ylabel('TD Seq')
            ax6.legend(['Sell (top)', 'Buy (bottom)'], loc='upper left', fontsize=7)
            ax6.grid(True)
            
            # 绘制筹码分布图（水平条形图）
            # 在右侧子图绘制水平条形图
            ax_chip.barh(data['indicators']['chip_prices'], data['indicators']['chip_volumes'], height=data['indicators']['chip_prices'][1]-data['indicators']['chip_prices'][0], 
                        color='skyblue', edgecolor='black', alpha=0.7)
            ax_chip.set_xlabel('Volume')
            ax_chip.set_ylabel('Price')
            # 将y轴刻度移动到右侧
            ax_chip.yaxis.tick_right()
            ax_chip.yaxis.set_label_position("right")
            ax_chip.grid(True)

            plt.tight_layout()

        # 嵌入到Tkinter
        canvas = FigureCanvasTkAgg(fig, master=kline_window)
        canvas.draw()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        toolbar = NavigationToolbar2Tk(canvas, kline_window)
        toolbar.update()
        
        tk.Button(kline_window, text="关闭", command=kline_window.destroy).pack(pady=10)

    except Exception as e:
        messagebox.showerror("错误", f"绘制K线图时出错: {str(e)}")

# ===== GUI初始化 =====
root = tk.Tk()
title_var = tk.StringVar(value="US Stock Watchlist")
root.title("US Stock Watchlist")
root.geometry("1800x900")

# 顶部标题
title_lbl = tk.Label(root, textvariable=title_var, anchor="w", font=("Segoe UI", 11, "bold"))
title_lbl.pack(fill="x", padx=6, pady=(6, 0))

# CNN恐惧贪婪指数
fear_greed_frame = tk.Frame(root)
fear_greed_frame.pack(fill="x", padx=6, pady=(0, 6))
fear_greed_var = tk.StringVar()
fear_greed_label = tk.Label(fear_greed_frame, textvariable=fear_greed_var, font=("Segoe UI", 9))
fear_greed_label.pack(side="left")

# Crypto恐惧贪婪指数
fear_greed_crypto_frame = tk.Frame(root)
fear_greed_crypto_frame.pack(fill="x", padx=6, pady=(0, 6))
fear_greed_crypto_var = tk.StringVar()
fear_greed_crypto_label = tk.Label(fear_greed_crypto_frame, textvariable=fear_greed_crypto_var, font=("Segoe UI", 9))
fear_greed_crypto_label.pack(side="left")

# 输入区
input_frame = tk.Frame(root)
input_frame.pack(fill="x", padx=6, pady=(10, 6))
tk.Label(input_frame, text="股票代码:").pack(side="left", padx=(0, 5))
ticker_entry = tk.Entry(input_frame, width=20)
ticker_entry.pack(side="left", padx=(0, 5))
tk.Label(input_frame, text="时间周期（天）:").pack(side="left", padx=(0, 5))
ticker_entry2 = tk.Entry(input_frame, width=5)
ticker_entry2.pack(side="left", padx=(0, 5))
ticker_entry2.insert(0, "365")
tk.Label(input_frame, text="时间间隔:").pack(side="left", padx=(0, 5))
ticker_entry3 = tk.Entry(input_frame, width=5)
ticker_entry3.pack(side="left", padx=(0, 5))
ticker_entry3.insert(0, "1d")
tk.Button(input_frame, text="绘制K线图", command=plot_kline).pack(side="left")

# 表格
sheet = tksheet.Sheet(root, data=[], headers=COLUMNS, show_row_index=False)
sheet.pack(fill="both", expand=True, padx=6, pady=6)

# 底部按钮
bottom = tk.Frame(root)
bottom.pack(fill="x", padx=6, pady=(0, 8))
tk.Button(bottom, text="Refresh Stocks", command=refresh_stock_data, width=15).pack(side="left", padx=4)
tk.Button(bottom, text="Refresh Breadth", command=refresh_breadth_data, width=15).pack(side="left", padx=4)
refresh_label = tk.Label(bottom, text="Last Refresh: Not yet", anchor="e")
refresh_label.pack(side="right")

# 初始化数据
latest_stock_df = pd.DataFrame()
latest_breadth_df = pd.DataFrame()
breadth_chart_data = {}

# 启动时加载数据
refresh_stock_data()
refresh_breadth_data()

root.mainloop()
