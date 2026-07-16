"""
US Stock Watchlist — Streamlit Version
Usage: streamlit run streamlit_app.py
Fully aligned with tkinter/tksheet version's layout and coloring logic.
"""

import streamlit as st
import pandas as pd
import numpy as np
import json
import os
import requests
import html
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import datetime
import time
import threading
import colorsys
from PIL import Image
from ticker_mapping import normalize_yfinance_ticker, stockanalysis_overview_url

# ── Page config ──────────────────────────────────────────────
_PAGE_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "survival_hunter_icon.png")
_PAGE_ICON = Image.open(_PAGE_ICON_PATH) if os.path.exists(_PAGE_ICON_PATH) else "📈"

st.set_page_config(
    page_title="US Stock Watchlist",
    page_icon=_PAGE_ICON,
    layout="wide",
    initial_sidebar_state="expanded",
)

THEMES = {
    "light": {
        "page_bg": "#ffffff",
        "panel_bg": "#f8fafc",
        "text": "#263238",
        "muted": "#607d8b",
        "table_bg": "#ffffff",
        "table_header_bg": "#f0f0f0",
        "table_group_bg": "#cccccc",
        "table_border": "#cccccc",
        "plot_template": "plotly_white",
        "plot_bg": "#ffffff",
        "grid": "#e5e7eb",
        "link": "blue",
    },
    "dark": {
        "page_bg": "#0b1020",
        "panel_bg": "#111827",
        "text": "#e5e7eb",
        "muted": "#9ca3af",
        "table_bg": "#111827",
        "table_header_bg": "#1f2937",
        "table_group_bg": "#374151",
        "table_border": "#374151",
        "plot_template": "plotly_dark",
        "plot_bg": "#111827",
        "grid": "#374151",
        "link": "#93c5fd",
    },
}


def get_theme(dark_mode=False):
    return THEMES["dark" if dark_mode else "light"]


def inject_theme_css(dark_mode=False):
    theme = get_theme(dark_mode)
    st.markdown(
        f"""
        <style>
        .stApp {{
            background-color: {theme["page_bg"]};
            color: {theme["text"]};
        }}
        [data-testid="stSidebar"] {{
            background-color: {theme["panel_bg"]};
        }}
        [data-testid="stSidebar"], [data-testid="stSidebar"] * {{
            color: {theme["text"]};
        }}
        h1, h2, h3, h4, h5, h6, p, label, span {{
            color: inherit;
        }}
        [data-testid="stMetric"], [data-testid="stDataFrame"] {{
            background-color: {theme["panel_bg"]};
        }}
        div[data-testid="stExpander"] {{
            background-color: {theme["panel_bg"]};
            border-color: {theme["table_border"]};
        }}
        div[data-testid="stTabs"] button {{
            color: {theme["text"]};
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )

# ── Backend configuration & health check ─────────────────────
import stock_watch_list_back_end

API_BASE = os.environ.get("STOCK_API_BASE_URL", "http://127.0.0.1:5000")
_DEV_MODE = os.environ.get("STOCK_DEV_MODE", "1") != "0"
_backend_ready = False


def check_backend_health(timeout=3):
    """Verify the backend is our app by calling /api/health.

    Returns (ok, message).
    """
    try:
        resp = requests.get(f"{API_BASE}/api/health", timeout=timeout)
        if resp.status_code != 200:
            return False, f"后端返回 HTTP {resp.status_code}"
        data = resp.json()
        if data.get("service") != "stock-watchlist-api":
            return False, "端口上的服务不是 Stock Watchlist API"
        return True, "ok"
    except requests.ConnectionError:
        return False, "无法连接后端服务"
    except requests.Timeout:
        return False, "后端健康检查超时"
    except Exception as e:
        return False, f"健康检查失败: {e}"


def ensure_backend():
    """Ensure backend is running. In dev mode, start Flask if needed.

    In production mode (STOCK_DEV_MODE=0), only check health.
    Returns (ok, message).
    """
    global _backend_ready
    if _backend_ready:
        return True, "ok"

    ok, msg = check_backend_health()
    if ok:
        _backend_ready = True
        return True, msg

    if not _DEV_MODE:
        return False, (
            f"后端不可用 ({msg})。"
            f"请确保后端服务已启动: python stock_watch_list_back_end.py"
        )

    # Dev mode: try to start Flask in a daemon thread
    t = threading.Thread(
        target=lambda: stock_watch_list_back_end.app.run(
            host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True
        ),
        daemon=True,
    )
    t.start()

    # Retry loop instead of fixed sleep (max ~5s)
    for _ in range(10):
        time.sleep(0.5)
        ok, msg = check_backend_health(timeout=1)
        if ok:
            _backend_ready = True
            return True, "ok"

    return False, f"后端启动失败 ({msg})"

# ── Group definitions (EXACTLY matching tkinter version) ──────
STOCK_GROUPS = {
    "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA", "SPCX"],
    "Chips/AI": ["MU","ORCL", "AMD", "INTC", "AVGO", "SMCI", "PLTR", "RGTI", "DXYZ", "SNPS", "APP"],
    "Fin/Crypto": ["V", "JPM", "BRK-B", "COIN", "HOOD", "MSTR", "CRCL", "SOFI", "OSCR"],
    "Health": ["LLY", "NVO", "ABBV", "UNH"],
    "Energy": ["SMR", "VST", "OKLO", "NEE", "ENPH", "GE", "GEV"],
    "Defense": ["LMT", "BA", "ACHR", "AXON"],
    "Consumer": ["LULU", "NKE", "CMG", "COST"],
    "China": ["BYDDY", "XIACY", "PDD", "BABA", "TCEHY", "BIDU"],
    "Themes": ["ASTS", "CRWV", "NBIS", "MP", "RKLB"],
}

BROAD_MARKET_GROUPS = {
    "Dashboard": [
        "^GSPC", "^NDX", "RSP", "QQQE", "^TNX",
        "EURUSD=X", "^VIX", "GC=F", "BZ=F", "BTC-USD", "510300.SS"
    ],
    "US Mkt Dir": ["^GSPC", "^NDX", "^DJI", "^RUT"],
    "Breadth": ["RSP", "QQQE"],
    "AI/Tech Risk": ["TQQQ", "^SOX"],
    "China Beta": ["510300.SS", "510050.SS", "159915.SZ", "588000.SS", "3033.HK"],
    "Rates/FX": ["^TNX", "EURUSD=X", "EURCNY=X"],
    "Fear/Vol": ["^VIX", "^VXN"],
    "Safe Haven": ["GC=F", "SI=F"],
    "Oil/Geopol": ["BZ=F"],
    "Crypto": ["BTC-USD", "ETH-USD"],
    "Strat Resources": ["WNUC.DE", "REMX"],
}

BREADTH_GROUPS = {
    "S&P 500 Breadth": ["SP500_20MA_Ratio", "SP500_50MA_Ratio", "SP500_200MA_Ratio"],
    "Nasdaq 100 Breadth": ["NDX100_20MA_Ratio", "NDX100_50MA_Ratio", "NDX100_200MA_Ratio"],
}
NON_PRICE_TICKERS = {
    "20MA_Ratio", "50MA_Ratio", "200MA_Ratio",
    "SP500_20MA_Ratio", "SP500_50MA_Ratio", "SP500_200MA_Ratio",
    "NDX100_20MA_Ratio", "NDX100_50MA_Ratio", "NDX100_200MA_Ratio",
}


def normalize_group_tickers(group_map):
    return {
        group_name: [normalize_yfinance_ticker(ticker) for ticker in tickers]
        for group_name, tickers in group_map.items()
    }


STOCK_GROUPS = normalize_group_tickers(STOCK_GROUPS)
BROAD_MARKET_GROUPS = normalize_group_tickers(BROAD_MARKET_GROUPS)
BREADTH_GROUPS = normalize_group_tickers(BREADTH_GROUPS)

# Merge watchlist groups for the stock-data API. Market breadth is fetched separately.
ALL_GROUPS = {**STOCK_GROUPS, **BROAD_MARKET_GROUPS}

# Broad market tickers (for API optimization)
BROAD_MARKET_TICKERS = list(dict.fromkeys(
    [t for tickers in BROAD_MARKET_GROUPS.values() for t in tickers]
))

# ════════════════════════════════════════════════════════════
# COLUMNS (EXACTLY matching tkinter version)
# ════════════════════════════════════════════════════════════
COLUMNS = (
    ["Ticker", "Name", "Price", "1D%", "5D%", "1M%", "YTD%", "3/6/12M Rel%"] +
    [f"Diff_EMA{n}%" for n in [5, 10, 20, 50, 100, 200]] +
    ["Diff_BB_Up%", "Diff_BB_Low%", "RSI", "Volume_Ratio", "Next Earnings", "Trailing PE", "Forward PE", "PEG Ratio", "Analysts", "Price Target", "Market Cap"]
)
DEFAULT_COLUMN_WIDTH = 78
COLUMN_WIDTHS = {
    "Ticker": 78,
    "Name": 260,
    "Price": 86,
    "1D%": 58,
    "5D%": 58,
    "1M%": 58,
    "YTD%": 62,
    "3/6/12M Rel%": 98,
    "Diff_BB_Up%": 88,
    "Diff_BB_Low%": 92,
    "RSI": 54,
    "Volume_Ratio": 86,
    "Next Earnings": 98,
    "Trailing PE": 82,
    "Forward PE": 84,
    "PEG Ratio": 74,
    "Analysts": 88,
    "Price Target": 96,
    "Market Cap": 96,
}
for _ema_column in [f"Diff_EMA{n}%" for n in [5, 10, 20, 50, 100, 200]]:
    COLUMN_WIDTHS[_ema_column] = 76


# ── Color helpers (EXACTLY matching tkinter version) ─────────
def red_green(value, neg_clip=-10.0, pos_clip=10.0):
    """Return hex color: green for positive, red for negative."""
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
    """Return hex color: blue gradient, darker = larger value."""
    if pd.isna(value):
        return "white"
    v = min(clip, max(0, float(value)))
    t = v / clip
    r = int(200 - 50 * t)
    g = int(200 - 50 * t)
    b = int(255 - 55 * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def get_earnings_color(days_until):
    """Earnings date color: red (soon) -> green (far)."""
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
    """Analyst rating color mapping."""
    if rating is None or (isinstance(rating, float) and pd.isna(rating)) or rating == "":
        return "white"
    rating_lower = str(rating).lower().strip()
    colors = {
        "strong buy": "#006400",   # dark green
        "buy": "#90EE90",           # light green
        "hold": "#FFFFE0",          # light yellow
        "sell": "#FFA07A",          # light red/orange
        "strong sell": "#8B0000",   # dark red
    }
    return colors.get(rating_lower, "white")


def price_target_color(target_price, current_price):
    """Price Target color: based on relative upside."""
    if target_price is None or current_price is None:
        return "white"
    if pd.isna(target_price) or pd.isna(current_price):
        return "white"
    if float(current_price) == 0:
        return "white"
    upside_pct = (float(target_price) - float(current_price)) / float(current_price) * 100.0
    return red_green(upside_pct, neg_clip=-50.0, pos_clip=50.0)


def rsi_color(value):
    if value is None or pd.isna(value):
        return "white"
    return red_green(50.0 - float(value), neg_clip=-50.0, pos_clip=50.0)


def beta_color(beta):
    """Beta color: beta=1 is white, beta>1 red, beta<1 green."""
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


def readable_text_color(bg_color, default="#111827"):
    if not isinstance(bg_color, str) or not bg_color.startswith("#") or len(bg_color) != 7:
        return default
    try:
        r = int(bg_color[1:3], 16)
        g = int(bg_color[3:5], 16)
        b = int(bg_color[5:7], 16)
    except ValueError:
        return default
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#f9fafb" if luminance < 0.45 else "#111827"


def sticky_first_column_style(bg_color, z_index=5):
    return (
        "position:sticky; left:0; "
        f"z-index:{z_index}; "
        f"background-color:{bg_color}; "
        "box-shadow:2px 0 3px rgba(0,0,0,0.12); "
    )


def sticky_first_column_header_style(bg_color):
    return sticky_first_column_style(bg_color, z_index=20)


# ── API helpers with caching ─────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def fetch_stock_data():
    """Fetch stock data for all groups."""
    resp = requests.post(
        f"{API_BASE}/api/stock_data",
        json={
            "groups": ALL_GROUPS,
            "broad_market_tickers": BROAD_MARKET_TICKERS
        },
        timeout=120
    )
    if resp.status_code != 200:
        st.error(f"API error: {resp.status_code}")
        return pd.DataFrame()
    data = resp.json()
    if not data.get("success"):
        st.error(f"API returned error: {data}")
        return pd.DataFrame()
    return pd.DataFrame(data["data"])


@st.cache_data(ttl=600, show_spinner=False)
def fetch_fear_greed():
    try:
        resp = requests.get(f"{API_BASE}/api/fear_greed", timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except (requests.RequestException, ValueError):
        pass
    return None


@st.cache_data(ttl=600, show_spinner=False)
def fetch_crypto_fear_greed():
    try:
        resp = requests.get(f"{API_BASE}/api/fear_greed_crypto", timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except (requests.RequestException, ValueError):
        pass
    return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_vix_kline():
    params = {"ticker": "^VIX", "period": 10, "interval": "1d"}
    try:
        resp = requests.get(f"{API_BASE}/api/kline_data", params=params, timeout=120)
        if resp.status_code == 200:
            return resp.json()
    except (requests.RequestException, ValueError):
        pass
    return None


@st.cache_data(ttl=600, show_spinner=False)
def fetch_breadth_data(sp500_list):
    """Fetch market breadth data. Use data= for form-encoded POST."""
    if not sp500_list:
        return {"success": False, "error": "S&P 500 symbol list is empty. The server may not be able to reach Wikipedia."}
    payload = {
        "sp500_symbols": json.dumps(sp500_list),
        "nasdaq100_symbols": json.dumps(stock_watch_list_back_end.get_nasdaq100_symbols()),
    }
    try:
        resp = requests.post(f"{API_BASE}/api/breadth_data", data=payload, timeout=300)
        if resp.status_code != 200:
            return {"success": False, "error": f"Breadth API HTTP {resp.status_code}: {resp.text}"}
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        return {"success": False, "error": f"Breadth API exception: {e}"}


@st.cache_data(ttl=60, show_spinner=False)
def fetch_kline_data(ticker, period, interval):
    resp = requests.get(
        f"{API_BASE}/api/kline_data",
        params={"ticker": ticker, "period": period, "interval": interval},
        timeout=120,
    )
    if resp.status_code != 200:
        return None
    return resp.json()


@st.cache_data(ttl=3600, show_spinner=False)
def get_sp500_list():
    return stock_watch_list_back_end.get_sp500_symbols()


# ════════════════════════════════════════════════════════════
# HELPER: Build grouped dataframe with section headers
# ════════════════════════════════════════════════════════════
def build_grouped_df(df, groups):
    """
    Build a dataframe with group section headers (EXACTLY matching tkinter version).
    Each group has a header row with gray background, then data rows.
    """
    if df.empty:
        return pd.DataFrame()
    
    rows = []
    current_date = pd.Timestamp.now(tz='America/New_York').date()
    
    for grp_name, tickers in groups.items():
        # Add group header row
        header_row = {col: "" for col in COLUMNS}
        header_row["Ticker"] = grp_name
        rows.append(header_row)
        
        # Get group data
        df_grp = df[df["Ticker"].isin(tickers)].set_index("Ticker") if not df.empty else pd.DataFrame()
        
        for tk_ in tickers:
            if df_grp is None or df_grp.empty or tk_ not in df_grp.index:
                # Empty row for missing ticker
                row_vals = {col: "" for col in COLUMNS}
                row_vals["Ticker"] = tk_
                rows.append(row_vals)
                continue
            
            row = df_grp.loc[tk_]
            row_vals = {}
            
            for col in COLUMNS:
                val = row[col] if col in row else np.nan
                
                # Special handling for Market Breadth tickers
                if tk_ in NON_PRICE_TICKERS:
                    if col in ["Ticker", "Price", "1D%", "5D%", "1M%"]:
                        disp = tk_ if col == "Ticker" else (f"{float(val):.2f}" if pd.notna(val) else "")
                    else:
                        disp = ""
                else:
                    if col == "Ticker":
                        disp = tk_
                    elif col == "Name":
                        disp = str(val) if pd.notna(val) and val is not None else ""
                    elif col == "3/6/12M Rel%":
                        disp = f"{float(val):.2f}" if pd.notna(val) else ""
                    elif col in ("Price", "RSI", "Volume_Ratio"):
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
                
                row_vals[col] = disp
            
            rows.append(row_vals)
    
    return pd.DataFrame(rows)


# ════════════════════════════════════════════════════════════
# HELPER: Apply cell colors (EXACTLY matching tkinter version)
# ════════════════════════════════════════════════════════════
def apply_cell_colors(df_display, df_raw, groups, columns=None):
    """
    Apply cell background colors (EXACTLY matching tkinter/tksheet version).
    Returns a list of dicts: {row_index: {col_index: color_hex}}
    """
    if df_display.empty or df_raw.empty:
        return {}
    
    columns = list(columns or COLUMNS)
    current_date = pd.Timestamp.now(tz='America/New_York').date()
    cell_colors = {}
    r = 0
    
    for grp_name, tickers in groups.items():
        # Header row: gray background
        for c in range(len(columns)):
            cell_colors[(r, c)] = "#cccccc"
        r += 1
        
        # Get group data
        df_grp = df_raw[df_raw["Ticker"].isin(tickers)].set_index("Ticker") if not df_raw.empty else pd.DataFrame()
        
        for tk_ in tickers:
            if df_grp is None or df_grp.empty or tk_ not in df_grp.index:
                r += 1
                continue
            
            row = df_grp.loc[tk_]
            
            for j, col in enumerate(columns):
                val = row[col] if col in row else np.nan
                
                # Apply colors (EXACTLY matching tkinter logic)
                if col == "Ticker":
                    beta_val = row.get("Beta", np.nan)
                    cell_colors[(r, j)] = beta_color(beta_val)
                elif col == "Name":
                    continue
                elif pd.notna(val) and df_display.iloc[r][col] != "" and col != "Price":
                    if col == "Volume_Ratio":
                        cell_colors[(r, j)] = blue_color(val)
                    elif col == "RSI":
                        cell_colors[(r, j)] = rsi_color(val)
                    elif col == "3/6/12M Rel%":
                        cell_colors[(r, j)] = red_green(val, neg_clip=-50.0, pos_clip=50.0)
                    elif col == "Next Earnings":
                        if isinstance(val, str):
                            try:
                                earnings_date = datetime.datetime.strptime(val, '%Y-%m-%d').date()
                                days_until = (earnings_date - current_date).days
                                color = get_earnings_color(days_until)
                                if color:
                                    cell_colors[(r, j)] = color
                            except:
                                pass
                    elif col == "Analysts":
                        cell_colors[(r, j)] = analyst_rating_color(val)
                    elif col == "Price Target":
                        current_price = row.get("Price", np.nan)
                        cell_colors[(r, j)] = price_target_color(val, current_price)
                    elif col in ("Trailing PE", "Forward PE"):
                        cell_colors[(r, j)] = blue_color(val if val > 0 else 50, clip=50.0)
                    elif col == "PEG Ratio":
                        cell_colors[(r, j)] = blue_color(val if val > 0 else 5.0, clip=5.0)
                    elif col == "Market Cap":
                        cell_colors[(r, j)] = blue_color(val, clip=1e12)
                    else:
                        cell_colors[(r, j)] = red_green(val)
            
            r += 1
    
    return cell_colors


# ── Helper: render grouped table with colors ──────────────────
def render_grouped_table(df, groups, key_prefix="", dark_mode=False, show_name_column=False):
    """
    Render a table with group headers AND cell colors (matching tkinter), with fixed header.
    """
    if df.empty:
        st.info("No data available")
        return
    
    # Build display dataframe (with formatted values)
    df_display = build_grouped_df(df, groups)
    
    if df_display.empty:
        st.info("No data available")
        return
    
    visible_columns = COLUMNS if show_name_column else [col for col in COLUMNS if col != "Name"]
    theme = get_theme(dark_mode)
    table_width = sum(COLUMN_WIDTHS.get(col, DEFAULT_COLUMN_WIDTH) for col in visible_columns)
    # Display using HTML table with fixed header using CSS
    html_table = f"""
    <div style="width:100%; max-height:600px; overflow:auto; border:1px solid {theme['table_border']};">
        <table style="width:{table_width}px; min-width:100%; table-layout:fixed; border-collapse:collapse; font-family:Arial; font-size:12px; background-color:{theme['table_bg']}; color:{theme['text']};">
            <colgroup>
    """

    for col in visible_columns:
        html_table += f"<col style='width:{COLUMN_WIDTHS.get(col, DEFAULT_COLUMN_WIDTH)}px;'>"

    html_table += f"""
            </colgroup>
            <thead style="position:sticky; top:0; z-index:10; background-color:{theme['table_header_bg']};">
                <tr style="background-color:{theme['table_header_bg']};">
    """
    
    # Header
    for col_index, col in enumerate(visible_columns):
        sticky_style = sticky_first_column_header_style(theme["table_header_bg"]) if col_index == 0 else ""
        html_table += (
            f"<th style='padding:4px; text-align:left; {sticky_style}color:{theme['text']}; "
            f"white-space:nowrap; overflow:hidden; text-overflow:ellipsis; "
            f"border:1px solid {theme['table_border']};'>{html.escape(col)}</th>"
        )
    html_table += "</tr></thead><tbody>"
    
    # Apply colors
    cell_colors = apply_cell_colors(df_display, df, groups, columns=visible_columns)
    group_names = set(groups.keys())
    
    # Rows
    for r in range(len(df_display)):
        row = df_display.iloc[r]
        is_header = str(row["Ticker"]) in group_names
        
        html_table += "<tr>"
        for j, col in enumerate(visible_columns):
            val = row[col]
            bg_color = cell_colors.get((r, j), theme["table_bg"] if not is_header else theme["table_group_bg"])
            if dark_mode and bg_color.lower() in ("#ffffff", "white", "#cccccc"):
                bg_color = theme["table_group_bg"] if is_header else theme["table_bg"]
            text_color = theme["text"] if bg_color == theme["table_bg"] else readable_text_color(bg_color)
            
            # Header row styling
            if is_header and j == 0:
                html_table += f"<td colspan='{len(visible_columns)}' style='padding:4px; color:{theme['text']}; background-color:{bg_color}; font-weight:bold; border:1px solid {theme['table_border']};'>{html.escape(str(val))}</td>"
                break
            elif is_header:
                continue
            
            # Data row styling
            align = "right" if isinstance(val, (int, float)) or (isinstance(val, str) and val and val[0] in "+-$0123456789") else "left"
            title_attr = (
                f" title='{html.escape(str(val), quote=True)}'"
                if col == "Name" and val
                else ""
            )
            sticky_style = sticky_first_column_style(bg_color) if j == 0 else ""
            html_table += (
                f"<td{title_attr} style='padding:4px; text-align:{align}; {sticky_style}color:{text_color}; "
                f"background-color:{bg_color}; white-space:nowrap; overflow:hidden; "
                f"text-overflow:ellipsis; border:1px solid {theme['table_border']};'>"
                f"{html.escape(str(val))}</td>"
            )
        
        html_table += "</tr>"
    
    html_table += """
            </tbody>
        </table>
    </div>
    """
    
    st.markdown(html_table, unsafe_allow_html=True)


# ── K-line chart builder ─────────────────────────────────────
def build_kline_chart(kline_data, ticker, fib_levels=None, dark_mode=False):
    """Build a Plotly candlestick chart with all indicators.
    fib_levels: optional list of (price, label, color) tuples for Fibonacci lines.
    """
    if not kline_data or not kline_data.get("success"):
        st.warning("K-line data not available")
        return None

    theme = get_theme(dark_mode)
    ohlc = kline_data["ohlc"]
    ind = kline_data["indicators"]
    fin = kline_data.get("financials", {})
    dates_raw = kline_data["dates"]

    # Parse dates
    date_fmt = "%Y-%m-%d %H:%M" if ":" in str(dates_raw[0]) else "%Y-%m-%d"
    dates = [datetime.datetime.strptime(d[:19] if " " in d else d[:10], date_fmt) for d in dates_raw]

    n = len(dates)

    # Build title string — matching tkinter version format exactly
    # K-Curve {ticker} | Market Cap: {mc}, PE: {tpe}/{fpe}, P/S: {ps}, P/B: {pb}, PEG: {peg}, Next Earnings: {ne}, Analysts: {ar}, Target: {pt}
    def _fmt(v, fmt_str=None):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "N/A"
        if fmt_str:
            try:
                return fmt_str.format(float(v))
            except (ValueError, TypeError):
                return str(v)
        return str(v)

    sa_url = stockanalysis_overview_url(ticker)
    title = (
        f"<b>K-Curve {ticker}</b> | "
        f"Market Cap: {_fmt(fin.get('market_cap'))}, "
        f"PE: {_fmt(fin.get('trailing_pe'))}/{_fmt(fin.get('forward_pe'))}, "
        f"P/S: {_fmt(fin.get('price_to_sales'))}, "
        f"P/B: {_fmt(fin.get('price_to_book'))}, "
        f"PEG: {_fmt(fin.get('peg_ratio'))}, "
        f"Next Earnings: {_fmt(fin.get('next_earnings'))}, "
        f"Analysts: {_fmt(fin.get('analyst_rating'))}, "
        f"Target: {_fmt(fin.get('price_target'))}"
    )

    # ── Calculate TD Sequential ───────────────────────────────
    td_sell = [0] * n
    td_buy = [0] * n
    closes = ohlc["close"]
    sell_count = 0
    buy_count = 0
    for i in range(n):
        if i >= 4:
            if closes[i] > closes[i - 4]:
                sell_count += 1
                buy_count = 0
            elif closes[i] < closes[i - 4]:
                buy_count += 1
                sell_count = 0
            else:
                sell_count = 0
                buy_count = 0
        td_sell[i] = sell_count if sell_count <= 9 else 0
        td_buy[i] = buy_count if buy_count <= 9 else 0
        if sell_count >= 9 or buy_count >= 9:
            sell_count = 0
            buy_count = 0

    # ── Create subplots ───────────────────────────────────────
    fig = make_subplots(
        rows=6, cols=2,
        shared_xaxes=True,
        vertical_spacing=0.02,
        horizontal_spacing=0.03,
        row_heights=[0.4, 0.12, 0.12, 0.12, 0.12, 0.12],
        column_widths=[0.78, 0.22],
        specs=[
            [{"secondary_y": False}, {"secondary_y": False}],
            [{"secondary_y": False}, None],
            [{"secondary_y": False}, None],
            [{"secondary_y": False}, None],
            [{"secondary_y": False}, None],
            [{"secondary_y": False}, None],
        ],
    )

    # ── Row 1, Col 1: Candlestick ─────────────────────────────
    fig.add_trace(
        go.Candlestick(
            x=dates, open=ohlc["open"], high=ohlc["high"],
            low=ohlc["low"], close=ohlc["close"],
            name="OHLC",
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
        ),
        row=1, col=1,
    )

    # MAs
    ma_colors = {"ma5": "blue", "ma10": "orange", "ma20": "purple",
                 "ma50": "brown", "ma100": "pink", "ma200": "gray"}
    for key, color in ma_colors.items():
        if ind.get(key):
            fig.add_trace(
                go.Scatter(x=dates, y=ind[key], name=key.upper(), line=dict(color=color, width=1, dash="dash"),
                           showlegend=True),
                row=1, col=1,
            )

    # Bollinger Bands
    if ind.get("bollinger_upper"):
        fig.add_trace(go.Scatter(x=dates, y=ind["bollinger_upper"], name="BB Upper",
                                  line=dict(color="cyan", width=0.8)), row=1, col=1)
    if ind.get("bollinger_lower"):
        fig.add_trace(go.Scatter(x=dates, y=ind["bollinger_lower"], name="BB Lower",
                                  line=dict(color="cyan", width=0.8)), row=1, col=1)

    # Chip peak price line
    if ind.get("chip_peak_price"):
        fig.add_hline(
            y=ind["chip_peak_price"],
            line_dash="dash",
            line_color="gray",
            annotation_text=f"Peak of Chip: {ind['chip_peak_price']:.2f}",
            row=1, col=1,
        )

    # Latest price marker (matching tkinter 'rx' marker)
    fig.add_trace(
        go.Scatter(
            x=[dates[-1]], y=[ohlc["close"][-1]],
            mode="markers",
            marker=dict(color="red", symbol="x", size=10),
            name=f"Latest ({dates[-1].strftime('%Y-%m-%d')}: {ohlc['close'][-1]:.2f})",
            showlegend=True,
        ),
        row=1, col=1,
    )

    # TD Sequential annotations on main chart - larger font size
    annotations = []
    for i in range(n):
        if td_sell[i] > 0 and td_sell[i] <= 9:
            annotations.append(dict(
                x=dates[i], y=ohlc["high"][i] * 1.003, text=str(td_sell[i]),
                showarrow=False, font=dict(color="red", size=12,
                                           family="Arial Black" if td_sell[i] == 9 else "Arial"),
                xref='x', yref='y',
            ))
        if td_buy[i] > 0 and td_buy[i] <= 9:
            annotations.append(dict(
                x=dates[i], y=ohlc["low"][i] * 0.997, text=str(td_buy[i]),
                showarrow=False, font=dict(color="green", size=12,
                                           family="Arial Black" if td_buy[i] == 9 else "Arial"),
                xref='x', yref='y',
            ))

    # ── Row 1, Col 2: Chip distribution ───────────────────────
    if ind.get("chip_prices") and ind.get("chip_volumes"):
        fig.add_trace(
            go.Bar(
                x=ind["chip_volumes"], y=ind["chip_prices"],
                orientation="h", name="Chip", marker_color="skyblue",
                showlegend=False,
            ),
            row=1, col=2,
        )
        fig.update_xaxes(title_text="Volume", row=1, col=2, showgrid=True)
        fig.update_yaxes(matches="y", row=1, col=2, side="right", title_text="", showgrid=True)

    # ── Row 2: Volume ─────────────────────────────────────────
    vol_colors = ["#26a69a" if ohlc["close"][i] >= ohlc["open"][i] else "#ef5350" for i in range(n)]
    fig.add_trace(go.Bar(x=dates, y=ohlc["volume"], name="Volume", marker_color=vol_colors,
                          showlegend=False), row=2, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1, showgrid=True)
    fig.update_xaxes(row=2, col=1, showgrid=True)

    # ── Row 3: MACD ───────────────────────────────────────────
    if ind.get("macd"):
        fig.add_trace(go.Scatter(x=dates, y=ind["macd"], name="MACD", line=dict(color="blue", width=1)),
                       row=3, col=1)
    if ind.get("signal"):
        fig.add_trace(go.Scatter(x=dates, y=ind["signal"], name="Signal", line=dict(color="red", width=1)),
                       row=3, col=1)
    if ind.get("hist"):
        hist_colors = ["#26a69a" if v >= 0 else "#ef5350" for v in ind["hist"]]
        fig.add_trace(go.Bar(x=dates, y=ind["hist"], name="Hist", marker_color=hist_colors,
                              showlegend=False), row=3, col=1)
    fig.update_yaxes(title_text="MACD", row=3, col=1, showgrid=True)
    fig.update_xaxes(row=3, col=1, showgrid=True)

    # ── Row 4: KDJ ────────────────────────────────────────────
    if ind.get("kdj_k"):
        fig.add_trace(go.Scatter(x=dates, y=ind["kdj_k"], name="K", line=dict(color="blue", width=1)),
                       row=4, col=1)
    if ind.get("kdj_d"):
        fig.add_trace(go.Scatter(x=dates, y=ind["kdj_d"], name="D", line=dict(color="orange", width=1)),
                       row=4, col=1)
    if ind.get("kdj_j"):
        fig.add_trace(go.Scatter(x=dates, y=ind["kdj_j"], name="J", line=dict(color="green", width=1)),
                       row=4, col=1)
    fig.add_hline(y=20, line_dash="dash", line_color="gray", row=4, col=1)
    fig.add_hline(y=80, line_dash="dash", line_color="gray", row=4, col=1)
    fig.update_yaxes(title_text="KDJ", row=4, col=1, showgrid=True)
    fig.update_xaxes(row=4, col=1, showgrid=True)

    # ── Row 5: RSI ────────────────────────────────────────────
    if ind.get("rsi"):
        fig.add_trace(go.Scatter(x=dates, y=ind["rsi"], name="RSI", line=dict(color="purple", width=1)),
                       row=5, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="gray", row=5, col=1)
    fig.add_hline(y=70, line_dash="dash", line_color="gray", row=5, col=1)
    fig.update_yaxes(title_text="RSI", row=5, col=1, showgrid=True)
    fig.update_xaxes(row=5, col=1, showgrid=True)

    # ── Row 6: TD Sequential ──────────────────────────────────
    # Add bars with text labels attached to them (not using annotations, more tightly coupled)
    # Use individual text traces for each bar to have different font weights for 9
    
    # First add the bars
    fig.add_trace(
        go.Bar(x=dates, y=td_sell, name="TD Sell", marker_color="red", showlegend=False),
        row=6, col=1)
    fig.add_trace(
        go.Bar(x=dates, y=[-v for v in td_buy], name="TD Buy", marker_color="green", showlegend=False),
        row=6, col=1)
    
    # Then add text directly on the bars using Scatter with text mode
    for i in range(n):
        if td_sell[i] > 0 and td_sell[i] <= 9:
            fig.add_trace(
                go.Scatter(x=[dates[i]], y=[td_sell[i]], text=[str(td_sell[i])],
                          mode="text", textposition="top center",
                          textfont=dict(color="red", size=11,
                                       family="Arial Black" if td_sell[i] == 9 else "Arial"),
                          showlegend=False),
                row=6, col=1)
        if td_buy[i] > 0 and td_buy[i] <= 9:
            fig.add_trace(
                go.Scatter(x=[dates[i]], y=[-td_buy[i]], text=[str(td_buy[i])],
                          mode="text", textposition="bottom center",
                          textfont=dict(color="green", size=11,
                                       family="Arial Black" if td_buy[i] == 9 else "Arial"),
                          showlegend=False),
                row=6, col=1)
    
    fig.update_yaxes(title_text="TD Seq", row=6, col=1, range=[-13, 13], showgrid=True)
    fig.update_xaxes(row=6, col=1, showgrid=True)

    # Add grid to main chart axes
    fig.update_xaxes(row=1, col=1, showgrid=True)
    fig.update_yaxes(row=1, col=1, showgrid=True)

    # ── Fibonacci levels (on main candlestick chart) ──────────
    if fib_levels:
        for level, lbl, color in fib_levels:
            fig.add_hline(
                y=level, line_dash="dash", line_color=color,
                annotation_text=f"{lbl}  {level:.2f}",
                annotation_position="right",
                annotation_font=dict(size=9, color=color),
                row=1, col=1,
            )

    # ── Layout ────────────────────────────────────────────────
    fig.update_layout(
        title=dict(text=title, x=0.01, xanchor="left"),
        height=1100,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.15, xanchor="left", x=0),
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        template=theme["plot_template"],
        paper_bgcolor=theme["page_bg"],
        plot_bgcolor=theme["plot_bg"],
        font=dict(color=theme["text"]),
        margin=dict(l=40, r=40, t=140, b=30),
        annotations=annotations,
    )
    fig.update_xaxes(gridcolor=theme["grid"], zerolinecolor=theme["grid"])
    fig.update_yaxes(gridcolor=theme["grid"], zerolinecolor=theme["grid"])

    # StockAnalysis clickable link (top-right, matching tkinter version)
    if sa_url:
        fig.add_annotation(
            text=f"<a href='{sa_url}' style='color:{theme['link']}; font-style:italic; font-size:12px;'>{sa_url}</a>",
            xref="paper", yref="paper",
            x=0.99, y=1.10,
            xanchor="right", yanchor="top",
            showarrow=False,
        )

    # Hide x-axis labels on all but the bottom subplot
    for r in range(1, 6):
        fig.update_xaxes(showticklabels=False, row=r, col=1)

    return fig


# ── Market breadth chart ─────────────────────────────────────
def build_breadth_chart(
    breadth_data,
    dark_mode=False,
    chart_key="breadth_chart_data",
    title="Market Breadth (S&P 500)",
    index_key="GSPC",
    index_label="^GSPC Adj Close",
):
    if not breadth_data or not breadth_data.get(chart_key):
        return None
    theme = get_theme(dark_mode)
    cd = breadth_data[chart_key]
    idx = cd["index"]

    fig = go.Figure()
    for key, color in [("20MA_Ratio", "red"), ("50MA_Ratio", "orange"), ("200MA_Ratio", "blue")]:
        if key in cd:
            fig.add_trace(go.Scatter(x=idx, y=cd[key], name=key, line=dict(color=color, width=1.5)))
    if cd.get(index_key):
        gspc_color = "#f9fafb" if dark_mode else "#111827"
        fig.add_trace(
            go.Scatter(
                x=idx,
                y=cd[index_key],
                name=index_label,
                line=dict(color=gspc_color, width=2.0),
                yaxis="y2",
            )
        )

    fig.add_hline(y=15, line_dash="dash", line_color="gray")
    fig.add_hline(y=80, line_dash="dash", line_color="gray")

    fig.update_layout(
        title=dict(text=title, font=dict(color=theme["text"], size=18)),
        height=400,
        template=theme["plot_template"],
        paper_bgcolor=theme["page_bg"],
        plot_bgcolor=theme["plot_bg"],
        font=dict(color=theme["text"]),
        legend=dict(
            font=dict(color=theme["text"]),
            bgcolor="rgba(17,24,39,0.82)" if dark_mode else "rgba(255,255,255,0.82)",
            bordercolor=theme["grid"],
            borderwidth=1,
        ),
        hovermode="x unified",
        yaxis=dict(
            range=[0, 100],
            title=dict(text="% Above MA", font=dict(color=theme["text"])),
            tickfont=dict(color=theme["text"]),
            showgrid=True,
        ),
        yaxis2=dict(
            title=dict(text=index_label, font=dict(color=theme["text"])),
            tickfont=dict(color=theme["text"]),
            overlaying="y",
            side="right",
            showgrid=False,
            zeroline=False,
        ),
        xaxis=dict(tickfont=dict(color=theme["text"]), showgrid=True),
    )
    fig.update_xaxes(gridcolor=theme["grid"], zerolinecolor=theme["grid"])
    fig.update_yaxes(gridcolor=theme["grid"], zerolinecolor=theme["grid"])
    return fig


# ── Fear & greed display ─────────────────────────────────────
def build_market_treemap(
    breadth_data,
    dark_mode=False,
    data_key="breadth_treemap_data",
    root_label="S&P 500",
    title="S&P 500 Treemap by Sector (1D%)",
):
    if not breadth_data or not breadth_data.get(data_key):
        return None
    theme = get_theme(dark_mode)
    rows = pd.DataFrame(breadth_data[data_key])
    if rows.empty or "Ticker" not in rows or "Sector" not in rows or "1D%" not in rows:
        return None

    rows["Sector"] = rows["Sector"].fillna("Unknown").replace("", "Unknown")
    rows["Industry"] = rows.get("Industry", "Unknown")
    rows["Name"] = rows.get("Name", rows["Ticker"])
    rows["Size"] = pd.to_numeric(rows.get("Size", 1), errors="coerce").fillna(1).clip(lower=1)
    rows["1D%"] = pd.to_numeric(rows["1D%"], errors="coerce")
    rows["Price"] = pd.to_numeric(rows.get("Price", np.nan), errors="coerce")
    rows["Market Cap"] = pd.to_numeric(rows.get("Market Cap", np.nan), errors="coerce")

    def _format_market_cap(value):
        if pd.isna(value):
            return ""
        value = float(value)
        if value >= 1e12:
            return f"${value / 1e12:.2f}T"
        if value >= 1e9:
            return f"${value / 1e9:.2f}B"
        if value >= 1e6:
            return f"${value / 1e6:.2f}M"
        return f"${value:,.0f}"

    labels = [root_label]
    ids = ["root"]
    parents = [""]
    values = [float(rows["Size"].sum())]
    colors = [0.0]
    text = [""]
    customdata = [["", "", "", "", "", ""]]

    for sector, sector_df in rows.groupby("Sector", sort=True):
        sector_id = f"sector:{sector}"
        sector_value = float(sector_df["Size"].sum())
        valid_mask = sector_df["1D%"].notna()
        if valid_mask.any():
            valid_df = sector_df[valid_mask]
            sector_color = float(np.average(valid_df["1D%"], weights=valid_df["Size"]))
            sector_text = f"{sector_color:+.2f}%"
        else:
            sector_color = 0.0
            sector_text = "N/A"
        labels.append(sector)
        ids.append(sector_id)
        parents.append("root")
        values.append(sector_value)
        colors.append(sector_color)
        text.append(sector_text)
        customdata.append([sector, "", "", "", "", sector_text])

        for _, row in sector_df.sort_values("Size", ascending=False).iterrows():
            ticker = str(row["Ticker"])
            pct_raw = row["1D%"]
            price = row["Price"]
            stock_size = float(row["Size"])
            if pd.notna(pct_raw):
                pct = float(pct_raw)
                pct_text = f"{pct:+.2f}%"
            else:
                pct = 0.0
                pct_text = "N/A"
            labels.append(ticker)
            ids.append(f"ticker:{ticker}")
            parents.append(sector_id)
            values.append(stock_size)
            colors.append(pct)
            text.append(pct_text)
            customdata.append([
                row.get("Name", ticker),
                sector,
                row.get("Industry", "Unknown"),
                f"{float(price):.2f}" if pd.notna(price) else "",
                _format_market_cap(row.get("Market Cap")),
                pct_text,
            ])

    fig = go.Figure(
        go.Treemap(
            labels=labels,
            ids=ids,
            parents=parents,
            values=values,
            branchvalues="total",
            text=text,
            textinfo="label+text",
            textfont=dict(size=18),
            customdata=customdata,
            marker=dict(
                colors=colors,
                colorscale=[
                    [0.0, "#b91c1c"],
                    [0.35, "#fca5a5"],
                    [0.5, "#f3f4f6"],
                    [0.65, "#86efac"],
                    [1.0, "#15803d"],
                ],
                cmin=-3,
                cmid=0,
                cmax=3,
                line=dict(width=1, color=theme["page_bg"]),
            ),
            hovertemplate=(
                "<b>%{label}</b><br>"
                "Name: %{customdata[0]}<br>"
                "Sector: %{customdata[1]}<br>"
                "Industry: %{customdata[2]}<br>"
                "Price: %{customdata[3]}<br>"
                "Market Cap: %{customdata[4]}<br>"
                "1D: %{customdata[5]}"
                "<extra></extra>"
            ),
            maxdepth=2,
        )
    )
    fig.update_layout(
        title=dict(text=title, font=dict(color=theme["text"], size=18)),
        height=700,
        margin=dict(l=8, r=8, t=44, b=8),
        paper_bgcolor=theme["page_bg"],
        plot_bgcolor=theme["plot_bg"],
        font=dict(color=theme["text"], size=12),
        uniformtext=dict(minsize=13, mode="hide"),
    )
    return fig


def build_sp500_treemap(breadth_data, dark_mode=False):
    return build_market_treemap(
        breadth_data,
        dark_mode=dark_mode,
        data_key="breadth_treemap_data",
        root_label="S&P 500",
        title="S&P 500 Treemap by Sector (1D%)",
    )


def build_nasdaq100_treemap(breadth_data, dark_mode=False):
    return build_market_treemap(
        breadth_data,
        dark_mode=dark_mode,
        data_key="nasdaq100_breadth_treemap_data",
        root_label="Nasdaq 100",
        title="Nasdaq 100 Treemap by Sector (1D%)",
    )


def fear_greed_color(value):
    if value <= 25:
        return "#d32f2f"
    if value <= 45:
        return "#f57c00"
    if value <= 55:
        return "#fbc02d"
    if value <= 75:
        return "#7cb342"
    return "#2e7d32"


def format_fear_greed_description(description):
    return str(description or "N/A").strip().title()


def build_fear_greed_gauge(value, description, title, dark_mode=False):
    theme = get_theme(dark_mode)
    color = fear_greed_color(value)
    description = format_fear_greed_description(description)
    fig = go.Figure(
        go.Indicator(
            mode="gauge",
            value=value,
            title={
                "text": f"<b>{html.escape(title)}</b>",
                "font": {"size": 17},
            },
            gauge={
                "axis": {
                    "range": [0, 100],
                    "tickwidth": 1,
                    "tickcolor": theme["muted"],
                    "tickmode": "array",
                    "tickvals": [0, 25, 50, 75, 100],
                },
                "bar": {"color": color, "thickness": 0.22},
                "bgcolor": theme["plot_bg"],
                "borderwidth": 1,
                "bordercolor": theme["table_border"],
                "steps": [
                    {"range": [0, 25], "color": "#ffcdd2"},
                    {"range": [25, 45], "color": "#ffe0b2"},
                    {"range": [45, 55], "color": "#fff9c4"},
                    {"range": [55, 75], "color": "#dcedc8"},
                    {"range": [75, 100], "color": "#c8e6c9"},
                ],
                "threshold": {
                    "line": {"color": color, "width": 5},
                    "thickness": 0.85,
                    "value": value,
                },
            },
        )
    )
    fig.update_layout(
        height=250,
        margin=dict(l=16, r=16, t=42, b=24),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Arial, sans-serif", color=theme["text"]),
        annotations=[
            dict(
                text=f"<b>{html.escape(description)}</b>",
                x=0.5,
                y=0.28,
                xref="paper",
                yref="paper",
                xanchor="center",
                yanchor="middle",
                yshift=42,
                showarrow=False,
                font=dict(size=17, color=color),
            ),
            dict(
                text=f"<b>{value:.0f}</b>",
                x=0.5,
                y=0.28,
                xref="paper",
                yref="paper",
                xanchor="center",
                yanchor="middle",
                yshift=-18,
                showarrow=False,
                font=dict(size=42, color=color),
            ),
        ],
        uirevision=f"{title}-{value:.0f}-{description}",
    )
    return fig


def display_fear_greed(fg_data, title, prefix="", dark_mode=False):
    if not fg_data or not fg_data.get("success"):
        st.metric(title, "N/A")
        return
    try:
        val = float(fg_data.get("value", 50))
    except (TypeError, ValueError):
        st.metric(title, "N/A")
        return
    val = max(0, min(100, val))
    desc = fg_data.get("description", "") or "N/A"
    st.plotly_chart(build_fear_greed_gauge(val, desc, title, dark_mode=dark_mode), use_container_width=True)


def vix_status(value):
    if value <= 12:
        return "Complacent"
    if value <= 16:
        return "Calm"
    if value <= 20:
        return "Neutral"
    if value <= 30:
        return "Caution"
    if value <= 40:
        return "Fear"
    return "Panic"


def vix_color(value):
    if value <= 12:
        return "#2e7d32"
    if value <= 16:
        return "#7cb342"
    if value <= 20:
        return "#9ca3af"
    if value <= 30:
        return "#f57c00"
    if value <= 40:
        return "#d32f2f"
    return "#7f1d1d"


def latest_vix_value(kline_data):
    if not kline_data or not kline_data.get("success"):
        return None
    closes = kline_data.get("ohlc", {}).get("close") or []
    for value in reversed(closes):
        try:
            if value is not None and not pd.isna(value):
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def build_vix_gauge(value, dark_mode=False):
    theme = get_theme(dark_mode)
    gauge_value = max(0.0, min(80.0, float(value)))
    color = vix_color(float(value))
    description = vix_status(float(value))
    fig = go.Figure(
        go.Indicator(
            mode="gauge",
            value=gauge_value,
            title={
                "text": "<b>VIX Volatility</b>",
                "font": {"size": 17},
            },
            gauge={
                "axis": {
                    "range": [0, 80],
                    "tickwidth": 1,
                    "tickcolor": theme["muted"],
                    "tickmode": "array",
                    "tickvals": [0, 12, 16, 20, 30, 40, 80],
                },
                "bar": {"color": color, "thickness": 0.22},
                "bgcolor": theme["plot_bg"],
                "borderwidth": 1,
                "bordercolor": theme["table_border"],
                "steps": [
                    {"range": [0, 12], "color": "#c8e6c9"},
                    {"range": [12, 16], "color": "#dcedc8"},
                    {"range": [16, 20], "color": "#f3f4f6"},
                    {"range": [20, 30], "color": "#ffe0b2"},
                    {"range": [30, 40], "color": "#ffcdd2"},
                    {"range": [40, 80], "color": "#fecaca"},
                ],
                "threshold": {
                    "line": {"color": color, "width": 5},
                    "thickness": 0.85,
                    "value": gauge_value,
                },
            },
        )
    )
    fig.update_layout(
        height=250,
        margin=dict(l=16, r=16, t=42, b=24),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Arial, sans-serif", color=theme["text"]),
        annotations=[
            dict(
                text=f"<b>{description}</b>",
                x=0.5,
                y=0.28,
                xref="paper",
                yref="paper",
                xanchor="center",
                yanchor="middle",
                yshift=42,
                showarrow=False,
                font=dict(size=17, color=color),
            ),
            dict(
                text=f"<b>{float(value):.1f}</b>",
                x=0.5,
                y=0.28,
                xref="paper",
                yref="paper",
                xanchor="center",
                yanchor="middle",
                yshift=-18,
                showarrow=False,
                font=dict(size=42, color=color),
            ),
        ],
        uirevision=f"VIX-{float(value):.1f}-{description}",
    )
    return fig


def display_vix_gauge(kline_data, dark_mode=False):
    value = latest_vix_value(kline_data)
    if value is None:
        st.metric("VIX Volatility", "N/A")
        return
    st.plotly_chart(build_vix_gauge(value, dark_mode=dark_mode), use_container_width=True)


# ════════════════════════════════════════════════════════════
# MAIN APP
# ════════════════════════════════════════════════════════════

_backend_ok, _backend_msg = ensure_backend()
if not _backend_ok:
    st.error(f"⚠️ 后端服务不可用: {_backend_msg}")
    st.stop()

# ── Sidebar ──────────────────────────────────────────────────
with st.sidebar:
    st.header("Controls")
    dark_mode = st.toggle("Dark mode", value=False, key="dark_mode")

    col_r1, col_r2 = st.columns(2)
    with col_r1:
        if st.button("📈 Refresh Stocks", width="stretch", key="btn_refresh_stocks"):
            fetch_stock_data.clear()
            st.rerun()
    with col_r2:
        if st.button("📊 Refresh Breadth", width="stretch", key="btn_refresh_breadth"):
            fetch_breadth_data.clear()
            with st.spinner("Refreshing market breadth data..."):
                sp500_list = get_sp500_list()
                st.session_state["breadth_data"] = fetch_breadth_data(sp500_list)
                st.session_state["breadth_last_refresh"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st.rerun()

    st.divider()

    st.caption(f"Last update: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

inject_theme_css(dark_mode)
st.title("📈 US Stock Watchlist")

# ── Fear & Greed Row ─────────────────────────────────────────
fg = fetch_fear_greed()
vix = fetch_vix_kline()
cfg = fetch_crypto_fear_greed()
col_fg1, col_vix, col_fg2 = st.columns(3)
with col_fg1:
    st.caption("CNN Fear & Greed Index")
    display_fear_greed(fg, "CNN", dark_mode=dark_mode)
with col_vix:
    st.caption("^VIX")
    display_vix_gauge(vix, dark_mode=dark_mode)
with col_fg2:
    st.caption("Crypto Fear & Greed Index")
    display_fear_greed(cfg, "Crypto", dark_mode=dark_mode)

st.divider()

# ── Load Data ────────────────────────────────────────────────
with st.spinner("Loading stock data..."):
    df = fetch_stock_data()

if df.empty:
    st.error("Failed to load stock data. Please check the backend connection.")
    st.stop()

# ── Tabs: Stocks / Broad Market / Market Breadth ───────────
tab1, tab2, tab3 = st.tabs(["📊 Stocks", "🌐 Broad Market", "📈 Market Breadth"])

# Tab 1: Stocks
with tab1:
    st.subheader("Stock Watchlist")
    show_stock_names = st.toggle("Show Name column next to Ticker", value=False, key="single_stocks_show_name_column")
    if not df.empty:
        # Filter stocks (exclude broad market and breadth tickers)
        broad_and_breadth_tickers = set(BROAD_MARKET_TICKERS) | NON_PRICE_TICKERS
        stocks_df = df[~df["Ticker"].isin(broad_and_breadth_tickers)].copy()
        
        if not stocks_df.empty:
            render_grouped_table(stocks_df, STOCK_GROUPS, dark_mode=dark_mode, show_name_column=show_stock_names)
        else:
            st.info("No stock data available")
    else:
        st.info("Loading data...")

# Tab 2: Broad Market
with tab2:
    st.subheader("Broad Market Indicators")
    show_broad_names = st.toggle("Show Name column next to Ticker", value=False, key="single_broad_show_name_column")
    if not df.empty:
        broad_df = df[df["Ticker"].isin(BROAD_MARKET_TICKERS)].copy()
        
        if not broad_df.empty:
            render_grouped_table(broad_df, BROAD_MARKET_GROUPS, dark_mode=dark_mode, show_name_column=show_broad_names)
        else:
            st.info("No broad market data available")
    else:
        st.info("Loading data...")

# Tab 3: Market Breadth
with tab3:
    st.subheader("Market Breadth")
    show_breadth_names = st.toggle("Show Name column next to Ticker", value=False, key="single_breadth_show_name_column")
    st.caption("Use Refresh Breadth in the sidebar to download and recalculate this shared market dataset.")
    breadth_data = st.session_state.get("breadth_data")
    if st.session_state.get("breadth_last_refresh"):
        st.caption(f"Last refreshed: {st.session_state['breadth_last_refresh']}")

    if not breadth_data:
        st.info("Market breadth is not loaded yet. Click Refresh Breadth in the sidebar to load it.")
    elif breadth_data.get("success"):
        counts = breadth_data.get("breadth_universe_counts", {})
        if counts:
            st.caption(
                f"Download universe: {counts.get('combined_download', 'N/A')} unique tickers "
                f"({counts.get('sp500', 'N/A')} S&P 500, {counts.get('nasdaq100', 'N/A')} Nasdaq 100, "
                f"{counts.get('overlap', 'N/A')} overlap)."
            )
        breadth_df = pd.DataFrame(breadth_data["data"])
        if not breadth_df.empty:
            render_grouped_table(breadth_df, BREADTH_GROUPS, dark_mode=dark_mode, show_name_column=show_breadth_names)
            st.divider()
            fig = build_breadth_chart(breadth_data, dark_mode=dark_mode)
            ndx_fig = build_breadth_chart(
                breadth_data,
                dark_mode=dark_mode,
                chart_key="nasdaq100_breadth_chart_data",
                title="Market Breadth (Nasdaq 100)",
                index_key="NDX",
                index_label="^NDX Adj Close",
            )
            chart_col1, chart_col2 = st.columns(2)
            with chart_col1:
                if fig:
                    st.plotly_chart(fig, width="stretch", key="breadth_chart")
            with chart_col2:
                if ndx_fig:
                    st.plotly_chart(ndx_fig, width="stretch", key="nasdaq100_breadth_chart")
            st.divider()
            st.caption("Treemap tile area is based on cached latest market cap; color is the latest regular 1D% move.")
            treemap_fig = build_sp500_treemap(breadth_data, dark_mode=dark_mode)
            ndx_treemap_fig = build_nasdaq100_treemap(breadth_data, dark_mode=dark_mode)
            if treemap_fig:
                st.plotly_chart(treemap_fig, width="stretch", key="sp500_treemap")
            if ndx_treemap_fig:
                st.plotly_chart(ndx_treemap_fig, width="stretch", key="nasdaq100_treemap")
        else:
            st.warning("Market breadth data is empty")
    else:
        error_msg = breadth_data.get("error") if isinstance(breadth_data, dict) else None
        st.warning(f"Failed to load market breadth data. {error_msg or 'Try refreshing.'}")

st.divider()

# ── K-line Chart Section ─────────────────────────────────────
st.subheader("📊 K-Line Chart")

col_kl1, col_kl2, col_kl3, col_kl4 = st.columns([2, 1, 1, 1])
with col_kl1:
    ticker = st.text_input("Ticker", "AAPL", key="kline_ticker").upper()
with col_kl2:
    period = int(st.number_input("Period (days)", min_value=1, max_value=3650, value=365, step=1, key="kline_period"))
with col_kl3:
    interval = st.selectbox("Interval", ["1d", "1wk", "1h", "4h", "15m", "5m"], index=0, key="kline_interval")
with col_kl4:
    st.write("")
    st.write("")
    plot_btn = st.button("🔍 Plot", width="stretch", key="kline_plot_btn")

if plot_btn:
    cache_key = f"{ticker}_{period}_{interval}"
    if "kline_data" not in st.session_state or st.session_state.get("kline_cache_key") != cache_key:
        with st.spinner(f"Loading {ticker} K-line data..."):
            kd = fetch_kline_data(ticker, period, interval)
            st.session_state["kline_data"] = kd
            st.session_state["kline_cache_key"] = cache_key
            st.session_state["kline_ticker_cache"] = ticker

    # Store ticker in session_state for Fibonacci calculation
    st.session_state["current_ticker"] = ticker

# Clear fib levels when ticker changes
if st.session_state.get("fib_ticker") != ticker:
    st.session_state.pop("fib_levels", None)
    st.session_state["fib_ticker"] = ticker

kd = st.session_state.get("kline_data")
if kd and kd.get("success"):
    # Handle Fibonacci updates before rendering chart
    fib_levels = st.session_state.get("fib_levels")
    
    # ── Fibonacci section ──────────────────────────────────
    with st.expander("📐 Fibonacci Retracement / Extension", expanded=bool(fib_levels)):
        st.markdown(
            """
            Enter A (swing low), B (swing high), and optionally C (pullback end) prices.
            - **A + B only** → Retracement (gray lines)
            - **A + B + C** → Extension (0% at C, >100% in blue)
            """
        )
        
        with st.form(key="fib_form"):
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                fib_a = st.number_input("A (Swing Low)", value=0.0, step=0.01, format="%.2f", key="fib_a")
            with fc2:
                fib_b = st.number_input("B (Swing High)", value=0.0, step=0.01, format="%.2f", key="fib_b")
            with fc3:
                fib_c = st.number_input("C (Pullback End, optional)", value=0.0, step=0.01, format="%.2f", key="fib_c")
            
            fc_btn1, fc_btn2 = st.columns([1, 1])
            with fc_btn1:
                submit_fib = st.form_submit_button(label="Calculate Fibonacci")
            with fc_btn2:
                clear_fib = st.form_submit_button(label="Clear Fibonacci")
        
        if clear_fib:
            if "fib_levels" in st.session_state:
                st.session_state.pop("fib_levels", None)
            fib_levels = None
        
        if submit_fib and fib_a > 0 and fib_b > 0 and fib_a != fib_b:
            diff = fib_b - fib_a
            
            if fib_c > 0:
                # Extension mode
                ratios = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0,
                           1.272, 1.618, 2.0, 2.618]
                labels = ['0%', '23.6%', '38.2%', '50%', '61.8%', '78.6%', '100%',
                          '127.2%', '161.8%', '200%', '261.8%']
                fib_levels = [
                    (fib_c + diff * r, lbl, "blue" if r >= 1.0 else "gray")
                    for r, lbl in zip(ratios, labels)
                ]
            else:
                # Retracement mode
                ratios = [0, 0.236, 0.382, 0.5, 0.618, 1.0]
                labels = ['0%', '23.6%', '38.2%', '50%', '61.8%', '100%']
                fib_levels = [
                    (fib_b - diff * r, lbl, "gray")
                    for r, lbl in zip(ratios, labels)
                ]
            st.session_state["fib_levels"] = fib_levels
        
        # Display fib levels table from session_state (persists across reruns)
        if fib_levels:
            rows_data = []
            for level, lbl, color in fib_levels:
                is_ext = "🔵 Extension" if color == "blue" else "⚫ Retracement"
                rows_data.append({"Ratio": lbl, "Price": f"{level:.2f}", "Type": is_ext})
            st.dataframe(pd.DataFrame(rows_data), width="stretch", hide_index=True)
    
    # Now render the chart with potentially updated fib_levels
    fig = build_kline_chart(kd, ticker, fib_levels=fib_levels, dark_mode=dark_mode)
    if fig:
        st.plotly_chart(fig, width="stretch", key="kline_main_chart")
    else:
        if kd:
            st.error(kd.get("error", "Failed to load K-line data"))
        else:
            st.info("Click 'Plot' to load chart")
else:
    if kd:
        st.error(kd.get("error", "Failed to load K-line data"))
    else:
        st.info("Click 'Plot' to load chart")

# ── Footer ───────────────────────────────────────────────────
st.divider()
st.caption("Streamlit version of US Stock Watchlist — data from Yahoo Finance + Stock Analysis")
