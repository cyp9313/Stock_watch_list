"""Multi-user Streamlit frontend with per-account editable watch lists."""
import warnings
warnings.filterwarnings("ignore", message="Timestamp.utcnow is deprecated")
import copy
import datetime
import colorsys
import html
import json
import os
import threading
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import streamlit as st
import yfinance as yf
from PIL import Image

import stock_watch_list_back_end
from daily_report.jobs import (
    ActiveJobError,
    DailyLimitError,
    QueueFullError,
    ScheduleLimitError,
    WEEKDAY_NAMES,
    check_download_generation_limits,
    create_weekly_schedule,
    delete_schedule,
    enqueue_email_job,
    finish_download_generation,
    list_owner_jobs,
    list_owner_schedules,
    set_schedule_active,
    start_download_generation,
)
from daily_report.mailer import smtp_configured
from daily_report.service import generate_report, runtime_available
from multiuser_store import (
    BREADTH_GROUPS,
    authenticate,
    broad_market_tickers,
    check_login_lock_status,
    config_to_api_groups,
    default_watchlist_config,
    get_user_config,
    normalize_config,
    save_user_config,
)
from ticker_mapping import normalize_yfinance_ticker, stockanalysis_overview_url


_PAGE_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "survival_hunter_icon.png")
_PAGE_ICON = Image.open(_PAGE_ICON_PATH) if os.path.exists(_PAGE_ICON_PATH) else "📈"

st.set_page_config(
    page_title="Stock Watchlist",
    page_icon=_PAGE_ICON,
    layout="wide",
    initial_sidebar_state="expanded",
)

API_BASE = os.environ.get("STOCK_API_BASE_URL", "http://127.0.0.1:5000")
RELATIVE_RETURN_COLUMNS = ["20D Rel%", "60D Rel%", "120D Rel%"]
RELATIVE_MOMENTUM_COLUMN = "3/6/12M Rel%"
RELATIVE_MOMENTUM_COLUMNS = RELATIVE_RETURN_COLUMNS + [RELATIVE_MOMENTUM_COLUMN]
EMA_DIFF_COLUMNS = [f"Diff_EMA{n}%" for n in [5, 10, 20, 50, 100, 200]]
FINANCIAL_COLUMNS = [
    "Next Earnings", "Trailing PE", "Forward PE", "PEG Ratio",
    "Analysts", "Price Target", "Market Cap",
]
PORTFOLIO_EXTRA_COLUMNS = [
    "Buy Price", "Shares", "Market Value",
    "P/L", "P/L 1D", "P/L 5D", "P/L 1M",
    "P/L%",
]
PORTFOLIO_CHANGE_PERIODS = [
    ("1D%", "P/L 1D"),
    ("5D%", "P/L 5D"),
    ("1M%", "P/L 1M"),
]
COLUMNS = (
    ["Ticker", "Name", "Price", "1D%", "5D%", "1M%", "YTD%"]
    + RELATIVE_RETURN_COLUMNS
    + [RELATIVE_MOMENTUM_COLUMN]
    + EMA_DIFF_COLUMNS
    + ["Diff_BB_Up%", "Diff_BB_Low%", "RSI", "Volume_Ratio"]
    + FINANCIAL_COLUMNS
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
    "20D Rel%": 76,
    "60D Rel%": 76,
    "120D Rel%": 84,
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
    "Buy Price": 96,
    "Shares": 76,
    "Market Value": 116,
    "P/L": 96,
    "P/L 1D": 96,
    "P/L 5D": 96,
    "P/L 1M": 96,
    "P/L%": 72,
}
for _ema_column in EMA_DIFF_COLUMNS:
    COLUMN_WIDTHS[_ema_column] = 76
RIGHT_ALIGNED_COLUMNS = {
    col
    for col in COLUMNS
    if col not in {"Ticker", "Name", "Next Earnings"}
}

SECTION_META = {
    "stocks_pages": {
        "tab": "Stock Watchlists",
        "title": "Stock Watchlists",
        "add_label": "Stock Watchlist page",
        "new_page": "New Stock Page",
        "help": (
            "Use these pages for individual stocks or ETFs you actively track. "
            "Each page is a separate tab, and each group becomes a gray section header in the table."
        ),
    },
    "broad_pages": {
        "tab": "Market Dashboard",
        "title": "Market Dashboard",
        "add_label": "Market Dashboard page",
        "new_page": "New Dashboard Page",
        "help": (
            "Use these pages for indices, rates, FX, commodities, crypto, sector ETFs, and other broad-market signals. "
            "They use the same table layout but are meant for macro and cross-asset monitoring."
        ),
    },
    "portfolio_pages": {
        "tab": "Portfolios",
        "title": "Portfolio Monitor",
        "add_label": "Portfolio page",
        "new_page": "New Portfolio",
        "help": (
            "Use these pages for personal holdings. Each row stores ticker, buy price, "
            "shares and buy currency, while market data is reused from the shared watchlist API."
        ),
    },
}

NON_PRICE_TICKERS = {
    "20MA_Ratio", "50MA_Ratio", "200MA_Ratio",
    "SP500_20MA_Ratio", "SP500_50MA_Ratio", "SP500_200MA_Ratio",
    "NDX100_20MA_Ratio", "NDX100_50MA_Ratio", "NDX100_200MA_Ratio",
}
TICKER_CURRENCY_SUFFIXES = {
    ".HK": "HKD",
    ".SS": "CNY",
    ".SZ": "CNY",
    ".DE": "EUR",
    ".PA": "EUR",
    ".AS": "EUR",
    ".MI": "EUR",
    ".MC": "EUR",
    ".BR": "EUR",
    ".L": "GBX",
    ".TO": "CAD",
    ".T": "JPY",
}
CURRENCY_DISPLAY_UNITS = {
    "USD": ("$", ""),
    "EUR": ("€", ""),
    "CNY": ("￥", ""),
    "CNH": ("￥", ""),
    "HKD": ("HK$", ""),
    "CAD": ("CA$", ""),
    "AUD": ("A$", ""),
    "GBP": ("£", ""),
    "GBX": ("", "p"),
    "JPY": ("¥", ""),
    "CHF": ("CHF ", ""),
    "SEK": ("SEK ", ""),
    "NOK": ("NOK ", ""),
    "DKK": ("DKK ", ""),
}

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
        "input_bg": "#ffffff",
        "input_border": "#d1d5db",
        "input_focus": "#2563eb",
        "button_bg": "#ffffff",
        "button_hover": "#f3f4f6",
        "button_border": "#d1d5db",
        "editor_bg": "#ffffff",
        "alert_bg": "#f8fafc",
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
        "input_bg": "#0f172a",
        "input_border": "#475569",
        "input_focus": "#60a5fa",
        "button_bg": "#1f2937",
        "button_hover": "#273449",
        "button_border": "#4b5563",
        "editor_bg": "#0f172a",
        "alert_bg": "#111827",
        "plot_template": "plotly_dark",
        "plot_bg": "#111827",
        "grid": "#374151",
        "link": "#93c5fd",
    },
}

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


def get_theme(dark_mode=False):
    return THEMES["dark" if dark_mode else "light"]


def inject_css(dark_mode=False):
    theme = get_theme(dark_mode)
    st.markdown(
        f"""
        <style>
        .stApp {{
            background-color: {theme["page_bg"]};
            color: {theme["text"]};
            --primary-color: {theme["input_focus"]};
            --background-color: {theme["page_bg"]};
            --secondary-background-color: {theme["panel_bg"]};
            --text-color: {theme["text"]};
            --font: "Source Sans Pro", sans-serif;
            --border-color: {theme["table_border"]};
            --input-background-color: {theme["input_bg"]};
        }}
        [data-testid="stSidebar"] {{
            background-color: {theme["panel_bg"]};
            --background-color: {theme["panel_bg"]};
            --secondary-background-color: {theme["input_bg"]};
            --text-color: {theme["text"]};
            --border-color: {theme["table_border"]};
            --input-background-color: {theme["input_bg"]};
        }}
        [data-testid="stSidebar"], [data-testid="stSidebar"] * {{
            color: {theme["text"]};
        }}
        h1, h2, h3, h4, h5, h6, p, label, span, small {{
            color: inherit;
        }}
        [data-testid="stMarkdownContainer"] p,
        [data-testid="stCaptionContainer"],
        [data-testid="stCaptionContainer"] * {{
            color: {theme["muted"]};
        }}
        [data-testid="stMetric"], [data-testid="stDataFrame"], [data-testid="stDataEditor"] {{
            background-color: {theme["panel_bg"]};
        }}
        [data-testid="stForm"] {{
            background-color: {theme["panel_bg"]};
            border: 1px solid {theme["table_border"]};
            border-radius: 8px;
            padding: 0.75rem;
        }}
        div[data-testid="stExpander"] {{
            background-color: {theme["panel_bg"]};
            border-color: {theme["table_border"]};
        }}
        div[data-testid="stExpander"] details,
        div[data-testid="stExpander"] summary {{
            color: {theme["text"]};
        }}
        div[data-testid="stTabs"] button {{
            color: {theme["text"]};
        }}
        div[data-testid="stTabs"] button[aria-selected="true"] {{
            color: {theme["input_focus"]};
        }}
        div[data-testid="stCheckbox"],
        div[data-testid="stToggle"] {{
            color: {theme["text"]};
        }}
        div[data-testid="stCheckbox"] label,
        div[data-testid="stToggle"] label {{
            color: {theme["text"]} !important;
        }}
        div[data-testid="stCheckbox"] span {{
            color: {theme["text"]} !important;
        }}
        div[data-testid="stCheckbox"] div[data-testid="stMarkdownContainer"] {{
            color: {theme["text"]} !important;
        }}
        div[data-testid="stCheckbox"] [data-baseweb="checkbox"] > div {{
            background-color: {theme["input_bg"]} !important;
            border-color: {theme["input_border"]} !important;
        }}
        div[role="switch"] {{
            background-color: {theme["input_bg"]} !important;
            border-color: {theme["input_border"]} !important;
        }}
        div[role="switch"][aria-checked="true"] {{
            background-color: {theme["input_focus"]} !important;
            border-color: {theme["input_focus"]} !important;
        }}
        div[data-testid="stTextInput"] input,
        div[data-testid="stNumberInput"] input,
        div[data-testid="stTextArea"] textarea,
        div[data-baseweb="input"] input,
        div[data-baseweb="textarea"] textarea {{
            background-color: {theme["input_bg"]} !important;
            color: {theme["text"]} !important;
            border-color: {theme["input_border"]} !important;
            caret-color: {theme["input_focus"]};
        }}
        div[data-testid="stTextInput"] input:focus,
        div[data-testid="stNumberInput"] input:focus,
        div[data-testid="stTextArea"] textarea:focus,
        div[data-baseweb="input"]:focus-within,
        div[data-baseweb="textarea"]:focus-within,
        div[data-baseweb="select"]:focus-within > div {{
            border-color: {theme["input_focus"]} !important;
            box-shadow: 0 0 0 1px {theme["input_focus"]}33 !important;
        }}
        div[data-testid="stTextInput"] input::placeholder,
        div[data-testid="stNumberInput"] input::placeholder,
        div[data-testid="stTextArea"] textarea::placeholder {{
            color: {theme["muted"]} !important;
        }}
        div[data-baseweb="input"],
        div[data-baseweb="textarea"],
        div[data-baseweb="select"] > div {{
            background-color: {theme["input_bg"]} !important;
            border-color: {theme["input_border"]} !important;
            color: {theme["text"]} !important;
        }}
        div[data-baseweb="select"] span,
        div[data-baseweb="select"] svg {{
            color: {theme["text"]} !important;
            fill: {theme["text"]} !important;
        }}
        div[data-baseweb="select"] input {{
            color: {theme["text"]} !important;
            -webkit-text-fill-color: {theme["text"]} !important;
            caret-color: {theme["input_focus"]} !important;
        }}
        div[data-baseweb="popover"] {{
            background-color: {theme["panel_bg"]} !important;
            color: {theme["text"]} !important;
            border-color: {theme["table_border"]} !important;
        }}
        div[data-baseweb="popover"] input,
        div[data-baseweb="popover"] textarea,
        div[data-baseweb="popover"] [contenteditable="true"],
        div[data-baseweb="popover"] span,
        div[data-baseweb="popover"] div[role="option"] {{
            color: {theme["text"]} !important;
            -webkit-text-fill-color: {theme["text"]} !important;
        }}
        ul[role="listbox"],
        div[role="listbox"] {{
            background-color: {theme["panel_bg"]} !important;
            border: 1px solid {theme["table_border"]} !important;
        }}
        li[role="option"],
        div[role="option"] {{
            background-color: {theme["panel_bg"]} !important;
            color: {theme["text"]} !important;
        }}
        li[role="option"]:hover,
        div[role="option"]:hover {{
            background-color: {theme["button_hover"]} !important;
        }}
        div[data-testid="stButton"] button,
        div[data-testid="stFormSubmitButton"] button {{
            background-color: {theme["button_bg"]};
            color: {theme["text"]};
            border: 1px solid {theme["button_border"]};
            border-radius: 6px;
            transition: background-color 120ms ease, border-color 120ms ease;
        }}
        div[data-testid="stButton"] button:hover,
        div[data-testid="stFormSubmitButton"] button:hover {{
            background-color: {theme["button_hover"]};
            color: {theme["text"]};
            border-color: {theme["input_focus"]};
        }}
        div[data-testid="stButton"] button:disabled,
        div[data-testid="stFormSubmitButton"] button:disabled {{
            background-color: {theme["panel_bg"]};
            color: {theme["muted"]};
            border-color: {theme["table_border"]};
            opacity: 0.65;
        }}
        div[data-testid="stDataFrame"],
        div[data-testid="stDataEditor"],
        div[data-testid="stDataFrame"] div,
        div[data-testid="stDataEditor"] div {{
            border-color: {theme["table_border"]} !important;
        }}
        div[data-testid="stDataFrame"],
        div[data-testid="stDataEditor"] {{
            --gdg-bg-cell: {theme["input_bg"]};
            --gdg-bg-cell-medium: {theme["panel_bg"]};
            --gdg-bg-header: {theme["table_header_bg"]};
            --gdg-bg-header-hovered: {theme["button_hover"]};
            --gdg-bg-header-has-focus: {theme["button_hover"]};
            --gdg-text-dark: {theme["text"]};
            --gdg-text-medium: {theme["text"]};
            --gdg-text-light: {theme["muted"]};
            --gdg-text-header: {theme["text"]};
            --gdg-text-group-header: {theme["text"]};
            --gdg-text-bubble: {theme["text"]};
            --gdg-bg-bubble: {theme["panel_bg"]};
            --gdg-bg-search-result: {theme["button_hover"]};
            --gdg-selection-color: {theme["input_focus"]};
            --gdg-drilldown-border: {theme["input_focus"]};
            --gdg-border-color: {theme["table_border"]};
            --gdg-horizontal-border-color: {theme["table_border"]};
            --gdg-accent-color: {theme["input_focus"]};
            --gdg-accent-light: {theme["input_focus"]}22;
        }}
        div[data-testid="stDataFrame"] [role="grid"],
        div[data-testid="stDataEditor"] [role="grid"],
        div[data-testid="stDataFrame"] canvas,
        div[data-testid="stDataEditor"] canvas {{
            background-color: {theme["editor_bg"]} !important;
        }}
        div[data-testid="stDataFrame"] button,
        div[data-testid="stDataEditor"] button,
        div[data-testid="stDataFrame"] svg,
        div[data-testid="stDataEditor"] svg {{
            color: {theme["text"]} !important;
            fill: {theme["text"]} !important;
        }}
        .glideDataEditor,
        .dvn-scroller,
        .gdg {{
            background-color: {theme["editor_bg"]} !important;
            color: {theme["text"]} !important;
        }}
        div[data-testid="stDataEditor"] *,
        div[data-testid="stDataEditor"] [role="grid"],
        div[data-testid="stDataEditor"] [role="row"],
        div[data-testid="stDataEditor"] [role="columnheader"],
        div[data-testid="stDataEditor"] [role="gridcell"],
        div[data-testid="stDataEditor"] [aria-colindex],
        div[data-testid="stDataEditor"] [aria-rowindex] {{
            color: {theme["text"]} !important;
        }}
        div[data-testid="stDataEditor"] input,
        div[data-testid="stDataEditor"] textarea,
        div[data-testid="stDataEditor"] div[data-baseweb="input"],
        div[data-testid="stDataEditor"] div[data-baseweb="select"] > div,
        div[data-testid="stDataEditor"] div[data-baseweb="select"] input,
        div[data-testid="stDataEditor"] div[data-baseweb="select"] span,
        div[data-testid="stDataEditor"] [contenteditable="true"] {{
            background-color: {theme["input_bg"]} !important;
            color: {theme["text"]} !important;
            -webkit-text-fill-color: {theme["text"]} !important;
            border-color: {theme["input_border"]} !important;
            caret-color: {theme["input_focus"]} !important;
        }}
        [data-testid="stAlert"] {{
            background-color: {theme["alert_bg"]};
            color: {theme["text"]};
            border-color: {theme["table_border"]};
        }}
        [data-testid="stAlert"] * {{
            color: {theme["text"]};
        }}
        hr {{
            border-color: {theme["table_border"]};
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def groups_to_editor_text(groups):
    return "\n".join(
        f"{group} | {', '.join(tickers)}"
        for group, tickers in groups.items()
    )


def editor_text_to_groups(text):
    groups = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if "|" in line:
            group_name, tickers_raw = line.split("|", 1)
        elif "\t" in line:
            group_name, tickers_raw = line.split("\t", 1)
        else:
            parts = line.split(",", 1)
            group_name = parts[0]
            tickers_raw = parts[1] if len(parts) > 1 else ""

        group_name = group_name.strip()
        tickers = []
        for token in tickers_raw.replace(";", ",").split(","):
            normalized = normalize_yfinance_ticker(token.strip())
            if normalized:
                tickers.append(normalized)
        if group_name and tickers:
            groups[group_name] = list(dict.fromkeys(tickers))
    return groups


def _format_editor_number(value):
    if value is None or pd.isna(value):
        return ""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return ""


def portfolio_holdings_to_editor_text(page):
    lines = []
    for holding in page.get("holdings", []):
        ticker = normalize_yfinance_ticker(holding.get("ticker"))
        if not ticker:
            continue
        group = str(holding.get("group") or "Portfolio").strip() or "Portfolio"
        buy_price = _format_editor_number(holding.get("buy_price"))
        shares = _format_editor_number(holding.get("shares"))
        buy_currency = normalize_currency_code(holding.get("buy_currency"))
        lines.append(f"{group} | {ticker} | {buy_price} | {shares} | {buy_currency}")
    return "\n".join(lines)


def portfolio_editor_text_to_holdings(text):
    holdings = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if "|" in line:
            parts = [part.strip() for part in line.split("|")]
        elif "\t" in line:
            parts = [part.strip() for part in line.split("\t")]
        else:
            normalized_line = line.replace("，", ",").replace(";", ",")
            parts = [part.strip() for part in normalized_line.split(",")]

        if len(parts) >= 5:
            group, ticker_raw, buy_price_raw, shares_raw, buy_currency_raw = parts[:5]
        elif len(parts) == 4:
            group = "Portfolio"
            ticker_raw, buy_price_raw, shares_raw, buy_currency_raw = parts
        else:
            continue

        ticker = normalize_yfinance_ticker(ticker_raw)
        if not ticker:
            continue
        try:
            buy_price = float(buy_price_raw)
        except (TypeError, ValueError):
            buy_price = None
        try:
            shares = float(shares_raw)
        except (TypeError, ValueError):
            shares = None
        buy_currency = normalize_currency_code(buy_currency_raw)

        holdings.append({
            "group": str(group or "Portfolio").strip() or "Portfolio",
            "ticker": ticker,
            "buy_price": buy_price,
            "shares": shares,
            "buy_currency": buy_currency,
        })
    return holdings


def page_tickers(page):
    tickers = []
    for group_tickers in page.get("groups", {}).values():
        tickers.extend(group_tickers)
    return list(dict.fromkeys(tickers))


def portfolio_page_tickers(page):
    return list(dict.fromkeys(
        normalize_yfinance_ticker(holding.get("ticker"))
        for holding in page.get("holdings", [])
        if normalize_yfinance_ticker(holding.get("ticker"))
    ))


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
    if days_until < 0:
        return None
    hue = 120.0 * min(days_until / 60.0, 1.0)
    r, g, b = colorsys.hsv_to_rgb(hue / 360.0, 1.0, 1.0)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def analyst_rating_color(rating):
    if rating is None or (isinstance(rating, float) and pd.isna(rating)) or rating == "":
        return "white"
    colors = {
        "strong buy": "#006400",
        "buy": "#90EE90",
        "hold": "#FFFFE0",
        "sell": "#FFA07A",
        "strong sell": "#8B0000",
    }
    return colors.get(str(rating).lower().strip(), "white")


def price_target_color(target_price, current_price):
    if target_price is None or current_price is None or pd.isna(target_price) or pd.isna(current_price):
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
    if beta is None or pd.isna(beta):
        return "white"
    dev = max(-1.0, min(1.0, float(beta) - 1.0))
    if dev >= 0:
        r = 255
        g = int(255 - 100 * dev)
        b = int(255 - 100 * dev)
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


def price_unit_for_ticker(ticker, display_currency="Local"):
    if display_currency == "EUR" and should_convert_price_ticker(ticker):
        return "EUR"
    if display_currency == "Local":
        return get_ticker_currency(ticker)
    return None


def format_money_value(value, ticker, display_currency="Local"):
    if pd.isna(value):
        return ""
    currency = price_unit_for_ticker(ticker, display_currency)
    try:
        formatted = f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""
    if not currency:
        return formatted
    prefix, suffix = CURRENCY_DISPLAY_UNITS.get(currency, (f"{currency} ", ""))
    return f"{prefix}{formatted}{suffix}"


def format_currency_value(value, currency):
    if value is None or pd.isna(value):
        return ""
    currency = normalize_currency_code(currency)
    try:
        formatted = f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return ""
    if not currency:
        return formatted
    prefix, suffix = CURRENCY_DISPLAY_UNITS.get(currency, (f"{currency} ", ""))
    return f"{prefix}{formatted}{suffix}"


def convert_currency_value(value, from_currency, to_currency):
    if value is None or pd.isna(value):
        return np.nan
    from_currency = normalize_currency_code(from_currency)
    to_currency = normalize_currency_code(to_currency)
    if not from_currency or not to_currency:
        return np.nan
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return np.nan
    if from_currency == to_currency:
        return amount
    from_to_eur = currency_to_eur_rate(from_currency)
    to_to_eur = currency_to_eur_rate(to_currency)
    if not from_to_eur or not to_to_eur:
        return np.nan
    return amount * float(from_to_eur) / float(to_to_eur)


def portfolio_value_change_from_return(current_value, return_pct):
    if current_value is None or return_pct is None or pd.isna(current_value) or pd.isna(return_pct):
        return np.nan
    try:
        current = float(current_value)
        pct_value = float(return_pct)
    except (TypeError, ValueError):
        return np.nan
    denominator = 1.0 + pct_value / 100.0
    if denominator == 0:
        return np.nan
    previous = current / denominator
    return current - previous


def build_grouped_df(df, groups, display_currency="Local"):
    if df.empty:
        return pd.DataFrame()

    rows = []
    for group_name, tickers in groups.items():
        header_row = {col: "" for col in COLUMNS}
        header_row["Ticker"] = group_name
        rows.append(header_row)

        df_group = df[df["Ticker"].isin(tickers)].set_index("Ticker") if not df.empty else pd.DataFrame()
        for ticker in tickers:
            if df_group is None or df_group.empty or ticker not in df_group.index:
                row_vals = {col: "" for col in COLUMNS}
                row_vals["Ticker"] = ticker
                rows.append(row_vals)
                continue

            row = df_group.loc[ticker]
            row_vals = {}
            for col in COLUMNS:
                val = row[col] if col in row else np.nan
                if ticker in NON_PRICE_TICKERS:
                    if col in ["Ticker", "Price", "1D%", "5D%", "1M%"]:
                        disp = ticker if col == "Ticker" else (f"{float(val):.2f}" if pd.notna(val) else "")
                    else:
                        disp = ""
                elif col == "Ticker":
                    disp = ticker
                elif col == "Name":
                    disp = str(val) if pd.notna(val) and val is not None else ""
                elif col == RELATIVE_MOMENTUM_COLUMN:
                    disp = f"{float(val):.2f}" if pd.notna(val) else ""
                elif col in RELATIVE_RETURN_COLUMNS:
                    disp = f"{float(val):.2f}" if pd.notna(val) else ""
                elif col == "Price":
                    disp = format_money_value(val, ticker, display_currency)
                elif col in ("RSI", "Volume_Ratio"):
                    disp = f"{float(val):.2f}" if pd.notna(val) else ""
                elif col == "Next Earnings":
                    disp = val if isinstance(val, str) else val.strftime("%Y-%m-%d") if not pd.isna(val) else ""
                elif col == "Analysts":
                    disp = str(val) if pd.notna(val) and val is not None else ""
                elif col == "Price Target":
                    disp = format_money_value(val, ticker, display_currency) if val is not None else ""
                elif col == "Market Cap":
                    disp = f"{float(val):.2e}" if pd.notna(val) else ""
                else:
                    disp = f"{float(val):.2f}" if pd.notna(val) else ""
                row_vals[col] = disp
            rows.append(row_vals)

    return pd.DataFrame(rows)


def apply_cell_colors(df_display, df_raw, groups, columns=None):
    if df_display.empty or df_raw.empty:
        return {}

    columns = list(columns or COLUMNS)
    current_date = pd.Timestamp.now(tz="America/New_York").date()
    cell_colors = {}
    row_index = 0
    for _, tickers in groups.items():
        for col_index in range(len(columns)):
            cell_colors[(row_index, col_index)] = "#cccccc"
        row_index += 1

        df_group = df_raw[df_raw["Ticker"].isin(tickers)].set_index("Ticker") if not df_raw.empty else pd.DataFrame()
        for ticker in tickers:
            if df_group is None or df_group.empty or ticker not in df_group.index:
                row_index += 1
                continue

            row = df_group.loc[ticker]
            for col_index, col in enumerate(columns):
                val = row[col] if col in row else np.nan
                if col == "Ticker":
                    cell_colors[(row_index, col_index)] = beta_color(row.get("Beta", np.nan))
                elif col == "Name":
                    continue
                elif col == "Price":
                    source = str(row.get("Price Source", "") or "").lower()
                    if source.startswith("pre-market"):
                        cell_colors[(row_index, col_index)] = "#dbeafe"
                    elif source.startswith("after-hours"):
                        cell_colors[(row_index, col_index)] = "#fef3c7"
                    elif pd.notna(val) and df_display.iloc[row_index][col] != "":
                        cell_colors[(row_index, col_index)] = "#dcfce7"
                elif pd.notna(val) and df_display.iloc[row_index][col] != "" and col != "Price":
                    if col == "Volume_Ratio":
                        cell_colors[(row_index, col_index)] = blue_color(val)
                    elif col == "RSI":
                        cell_colors[(row_index, col_index)] = rsi_color(val)
                    elif col in RELATIVE_MOMENTUM_COLUMNS:
                        cell_colors[(row_index, col_index)] = red_green(val, neg_clip=-50.0, pos_clip=50.0)
                    elif col == "Next Earnings":
                        if isinstance(val, str):
                            try:
                                earnings_date = datetime.datetime.strptime(val, "%Y-%m-%d").date()
                                color = get_earnings_color((earnings_date - current_date).days)
                                if color:
                                    cell_colors[(row_index, col_index)] = color
                            except ValueError:
                                pass
                    elif col == "Analysts":
                        cell_colors[(row_index, col_index)] = analyst_rating_color(val)
                    elif col == "Price Target":
                        cell_colors[(row_index, col_index)] = price_target_color(val, row.get("Price", np.nan))
                    elif col in ("Trailing PE", "Forward PE"):
                        cell_colors[(row_index, col_index)] = blue_color(val if val > 0 else 50, clip=50.0)
                    elif col == "PEG Ratio":
                        cell_colors[(row_index, col_index)] = blue_color(val if val > 0 else 5.0, clip=5.0)
                    elif col == "Market Cap":
                        cell_colors[(row_index, col_index)] = blue_color(val, clip=1e12)
                    else:
                        cell_colors[(row_index, col_index)] = red_green(val)
            row_index += 1

    return cell_colors


def render_grouped_table(
    df,
    groups,
    dark_mode=False,
    display_currency="Local",
    show_name_column=False,
    show_relative_momentum_columns=False,
    show_ema_columns=False,
    show_financial_columns=True,
):
    if df.empty:
        st.info("No data available")
        return

    df_display = build_grouped_df(
        df,
        groups,
        display_currency=display_currency,
    )

    if df_display.empty:
        st.info("No data available")
        return

    hidden_columns = set()

    if not show_name_column:
        hidden_columns.add("Name")

    if not show_relative_momentum_columns:
        hidden_columns.update(RELATIVE_MOMENTUM_COLUMNS)

    if not show_ema_columns:
        hidden_columns.update(EMA_DIFF_COLUMNS)

    if not show_financial_columns:
        hidden_columns.update(FINANCIAL_COLUMNS)

    visible_columns = [
        col for col in COLUMNS
        if col not in hidden_columns
    ]

    theme = get_theme(dark_mode)
    table_width = sum(COLUMN_WIDTHS.get(col, DEFAULT_COLUMN_WIDTH) for col in visible_columns)

    html_table = f"""
    <div style="width:100%; max-height:600px; overflow:auto;
                border:1px solid {theme['table_border']};">
        <table style="width:{table_width}px; min-width:100%; table-layout:fixed; border-collapse:collapse;
                      font-family:Arial; font-size:12px;
                      background-color:{theme['table_bg']};
                      color:{theme['text']};">
            <colgroup>
    """

    for col in visible_columns:
        html_table += f"<col style='width:{COLUMN_WIDTHS.get(col, DEFAULT_COLUMN_WIDTH)}px;'>"

    html_table += f"""
            </colgroup>
            <thead style="position:sticky; top:0; z-index:10;
                          background-color:{theme['table_header_bg']};">
                <tr style="background-color:{theme['table_header_bg']};">
    """

    for col in visible_columns:
        html_table += (
            f"<th style='padding:4px; text-align:left; "
            f"color:{theme['text']}; "
            f"white-space:nowrap; overflow:hidden; text-overflow:ellipsis; "
            f"border:1px solid {theme['table_border']};'>"
            f"{html.escape(col)}</th>"
        )

    html_table += "</tr></thead><tbody>"

    cell_colors = apply_cell_colors(
        df_display,
        df,
        groups,
        columns=visible_columns,
    )

    group_names = set(groups.keys())

    for row_index in range(len(df_display)):
        row = df_display.iloc[row_index]
        is_header = str(row["Ticker"]) in group_names
        html_table += "<tr>"

        for col_index, col in enumerate(visible_columns):
            val = "" if pd.isna(row[col]) else str(row[col])

            bg_color = cell_colors.get(
                (row_index, col_index),
                theme["table_group_bg"] if is_header else theme["table_bg"],
            )

            if dark_mode and bg_color.lower() in (
                "#ffffff",
                "white",
                "#cccccc",
            ):
                bg_color = (
                    theme["table_group_bg"]
                    if is_header
                    else theme["table_bg"]
                )

            text_color = (
                theme["text"]
                if bg_color == theme["table_bg"]
                else readable_text_color(bg_color)
            )

            if is_header and col_index == 0:
                html_table += (
                    # 注意这里必须使用 visible_columns
                    f"<td colspan='{len(visible_columns)}' "
                    f"style='padding:4px; color:{theme['text']}; "
                    f"background-color:{bg_color}; font-weight:bold; "
                    f"border:1px solid {theme['table_border']};'>"
                    f"{html.escape(val)}</td>"
                )
                break

            if is_header:
                continue

            align = (
                "right"
                if col in RIGHT_ALIGNED_COLUMNS
                or (val and val[0] in "+-$0123456789")
                else "left"
            )
            title_attr = (
                f" title='{html.escape(val, quote=True)}'"
                if col == "Name" and val
                else ""
            )

            html_table += (
                f"<td{title_attr} style='padding:4px; text-align:{align}; "
                f"color:{text_color}; background-color:{bg_color}; "
                f"white-space:nowrap; overflow:hidden; text-overflow:ellipsis; "
                f"border:1px solid {theme['table_border']};'>"
                f"{html.escape(val)}</td>"
            )

        html_table += "</tr>"

    html_table += """
            </tbody>
        </table>
    </div>
    """

    st.markdown(html_table, unsafe_allow_html=True)


def render_market_table(
    raw_df,
    page,
    dark_mode=False,
    display_currency="Local",
    show_name_column=False,
    show_relative_momentum_columns=False,
    show_ema_columns=False,
    show_financial_columns=True,
):
    groups = page.get("groups", {})
    tickers = page_tickers(page)

    page_df = (
        raw_df[raw_df["Ticker"].isin(tickers)].copy()
        if not raw_df.empty
        else pd.DataFrame()
    )

    render_grouped_table(
        page_df,
        groups,
        dark_mode=dark_mode,
        display_currency=display_currency,
        show_name_column=show_name_column,
        show_relative_momentum_columns=show_relative_momentum_columns,
        show_ema_columns=show_ema_columns,
        show_financial_columns=show_financial_columns,
    )


def portfolio_groups(page):
    groups = {}
    for holding in page.get("holdings", []):
        ticker = normalize_yfinance_ticker(holding.get("ticker"))
        if not ticker:
            continue
        group_name = str(holding.get("group") or "Portfolio").strip() or "Portfolio"
        groups.setdefault(group_name, []).append(ticker)
    return {group: list(dict.fromkeys(tickers)) for group, tickers in groups.items()}


def portfolio_editor_df(page):
    rows = []
    for holding in page.get("holdings", []):
        rows.append({
            "Group": holding.get("group") or "Portfolio",
            "Ticker": holding.get("ticker") or "",
            "Buy Price": holding.get("buy_price"),
            "Shares": holding.get("shares"),
            "Buy Currency": holding.get("buy_currency") or "",
        })
    return pd.DataFrame(rows, columns=["Group", "Ticker", "Buy Price", "Shares", "Buy Currency"])


def build_portfolio_enriched_df(raw_df, page):
    tickers = portfolio_page_tickers(page)
    if not tickers:
        return pd.DataFrame(), [], {"mixed_currency": False, "total_row": None}

    stock_rows = raw_df[raw_df["Ticker"].isin(tickers)].copy() if not raw_df.empty else pd.DataFrame()
    stock_by_ticker = stock_rows.set_index("Ticker") if not stock_rows.empty else pd.DataFrame()
    enriched_rows = []
    treemap_rows = []
    total_cost = 0.0
    total_market_value = 0.0
    total_beta_weighted = 0.0
    total_beta_weight = 0.0
    total_periods = {
        source_pct_col: {"ready": True, "change": 0.0, "previous": 0.0}
        for source_pct_col, _ in PORTFOLIO_CHANGE_PERIODS
    }
    total_currency = None
    mixed_currency = False
    total_ready = True

    for holding in page.get("holdings", []):
        ticker = normalize_yfinance_ticker(holding.get("ticker"))
        if not ticker:
            continue

        if stock_by_ticker is not None and not stock_by_ticker.empty and ticker in stock_by_ticker.index:
            base_row = stock_by_ticker.loc[ticker].to_dict()
        else:
            base_row = {"Ticker": ticker}
        row = {col: base_row.get(col, np.nan) for col in COLUMNS}
        row["Ticker"] = ticker
        row["Beta"] = base_row.get("Beta", np.nan)
        row["Price Source"] = base_row.get("Price Source", "")

        buy_currency = normalize_currency_code(holding.get("buy_currency"))
        ticker_currency = get_ticker_currency(ticker) or fallback_currency_from_ticker(ticker)
        try:
            buy_price = float(holding.get("buy_price"))
            shares = float(holding.get("shares"))
        except (TypeError, ValueError):
            buy_price = np.nan
            shares = np.nan
        current_price = row.get("Price", np.nan)

        market_value_local = (
            float(current_price) * shares
            if pd.notna(current_price) and pd.notna(shares)
            else np.nan
        )
        market_value = convert_currency_value(market_value_local, ticker_currency, buy_currency)
        cost_basis = buy_price * shares if pd.notna(buy_price) and pd.notna(shares) else np.nan
        pnl_abs = market_value - cost_basis if pd.notna(market_value) and pd.notna(cost_basis) else np.nan
        pnl_pct = pnl_abs / cost_basis * 100.0 if pd.notna(pnl_abs) and cost_basis not in (0, np.nan) else np.nan
        market_value_eur = convert_currency_value(market_value, buy_currency, "EUR")

        period_values = {}
        for source_pct_col, abs_col in PORTFOLIO_CHANGE_PERIODS:
            source_pct = row.get(source_pct_col, np.nan)
            abs_change = portfolio_value_change_from_return(market_value, source_pct)
            period_values[abs_col] = abs_change

            period_total = total_periods[source_pct_col]
            if pd.notna(abs_change) and pd.notna(market_value):
                previous_value = float(market_value) - float(abs_change)
                if previous_value != 0:
                    period_total["change"] += float(abs_change)
                    period_total["previous"] += previous_value
                else:
                    period_total["ready"] = False
            else:
                period_total["ready"] = False

        row.update({
            "Buy Price": buy_price,
            "Shares": shares,
            "Market Value": market_value,
            "P/L": pnl_abs,
            "P/L%": pnl_pct,
            **period_values,
            "_buy_currency": buy_currency,
            "_ticker_currency": ticker_currency,
            "_market_value_eur": market_value_eur,
        })
        enriched_rows.append(row)

        if buy_currency:
            if total_currency is None:
                total_currency = buy_currency
            elif total_currency != buy_currency:
                mixed_currency = True
        else:
            total_ready = False
        if pd.notna(cost_basis) and pd.notna(market_value):
            total_cost += float(cost_basis)
            total_market_value += float(market_value)
        else:
            total_ready = False
        if pd.notna(market_value_eur) and float(market_value_eur) > 0 and pd.notna(row.get("Beta", np.nan)):
            total_beta_weighted += float(row.get("Beta")) * float(market_value_eur)
            total_beta_weight += float(market_value_eur)

        if pd.notna(market_value) and float(market_value) > 0:
            treemap_rows.append({
                "Ticker": ticker,
                "Group": holding.get("group") or "Portfolio",
                "Name": row.get("Name") if pd.notna(row.get("Name", np.nan)) else ticker,
                "Market Value": market_value,
                "Market Value EUR": market_value_eur,
                "Buy Currency": buy_currency,
                "1D%": row.get("1D%", np.nan),
            })

    total_row = None
    if not mixed_currency and total_ready and total_currency and total_cost > 0:
        total_pnl = total_market_value - total_cost
        total_beta = total_beta_weighted / total_beta_weight if total_beta_weight > 0 else np.nan
        total_row = {
            "Ticker": "TOTAL",
            "Beta": total_beta,
            "Market Value": total_market_value,
            "P/L": total_pnl,
            "P/L%": total_pnl / total_cost * 100.0,
            "_buy_currency": total_currency,
        }
        for source_pct_col, abs_col in PORTFOLIO_CHANGE_PERIODS:
            period_total = total_periods[source_pct_col]
            if period_total["ready"] and period_total["previous"] != 0:
                total_row[abs_col] = period_total["change"]
                total_row[source_pct_col] = period_total["change"] / period_total["previous"] * 100.0
            else:
                total_row[abs_col] = np.nan
                total_row[source_pct_col] = np.nan

    return pd.DataFrame(enriched_rows), treemap_rows, {
        "mixed_currency": mixed_currency,
        "total_row": total_row,
        "total_currency": total_currency,
    }


def render_portfolio_table(
    raw_df,
    page,
    dark_mode=False,
    display_currency="Local",
    show_name_column=False,
    show_relative_momentum_columns=False,
    show_ema_columns=False,
    show_financial_columns=True,
):
    groups = portfolio_groups(page)
    enriched_df, treemap_rows, summary = build_portfolio_enriched_df(raw_df, page)
    if enriched_df.empty:
        st.info("No portfolio holdings yet. Use the editor to add ticker, buy price, shares and buy currency.")
        return treemap_rows

    df_display = build_grouped_df(enriched_df, groups, display_currency=display_currency)
    if df_display.empty:
        st.info("No portfolio data available")
        return treemap_rows

    for col in PORTFOLIO_EXTRA_COLUMNS:
        df_display[col] = ""

    extra_by_ticker = enriched_df.set_index("Ticker") if "Ticker" in enriched_df else pd.DataFrame()
    group_names = set(groups.keys())
    for row_index in range(len(df_display)):
        ticker = str(df_display.iloc[row_index]["Ticker"])
        if ticker in group_names or extra_by_ticker.empty or ticker not in extra_by_ticker.index:
            continue
        raw_row = extra_by_ticker.loc[ticker]
        buy_currency = raw_row.get("_buy_currency")
        df_display.at[row_index, "Buy Price"] = format_currency_value(raw_row.get("Buy Price"), buy_currency)
        df_display.at[row_index, "Shares"] = f"{float(raw_row.get('Shares')):,.4g}" if pd.notna(raw_row.get("Shares")) else ""
        df_display.at[row_index, "Market Value"] = format_currency_value(raw_row.get("Market Value"), buy_currency)
        df_display.at[row_index, "P/L"] = format_currency_value(raw_row.get("P/L"), buy_currency)
        df_display.at[row_index, "P/L%"] = f"{float(raw_row.get('P/L%')):+.2f}" if pd.notna(raw_row.get("P/L%")) else ""
        for _, abs_col in PORTFOLIO_CHANGE_PERIODS:
            df_display.at[row_index, abs_col] = format_currency_value(raw_row.get(abs_col), buy_currency)

    total_row = summary.get("total_row")
    if summary.get("mixed_currency"):
        st.warning("Portfolio total is hidden because buy currencies are mixed. Use one buy currency per portfolio page to show total P/L.")
    elif total_row:
        display_total = {col: "" for col in df_display.columns}
        total_beta = total_row.get("Beta", np.nan)
        display_total["Ticker"] = f"TOTAL β {float(total_beta):.2f}" if pd.notna(total_beta) else "TOTAL"
        display_total["Name"] = "Portfolio Total"
        display_total["Market Value"] = format_currency_value(total_row.get("Market Value"), total_row.get("_buy_currency"))
        display_total["P/L"] = format_currency_value(total_row.get("P/L"), total_row.get("_buy_currency"))
        display_total["P/L%"] = f"{float(total_row.get('P/L%')):+.2f}"
        for source_pct_col, abs_col in PORTFOLIO_CHANGE_PERIODS:
            display_total[abs_col] = format_currency_value(total_row.get(abs_col), total_row.get("_buy_currency"))
            display_total[source_pct_col] = f"{float(total_row.get(source_pct_col)):+.2f}" if pd.notna(total_row.get(source_pct_col)) else ""
        df_display = pd.concat([df_display, pd.DataFrame([display_total])], ignore_index=True)

    hidden_columns = set()
    if not show_name_column:
        hidden_columns.add("Name")
    if not show_relative_momentum_columns:
        hidden_columns.update(RELATIVE_MOMENTUM_COLUMNS)
    if not show_ema_columns:
        hidden_columns.update(EMA_DIFF_COLUMNS)
    if not show_financial_columns:
        hidden_columns.update(FINANCIAL_COLUMNS)

    visible_columns = [col for col in (COLUMNS + PORTFOLIO_EXTRA_COLUMNS) if col not in hidden_columns]
    theme = get_theme(dark_mode)
    table_width = sum(COLUMN_WIDTHS.get(col, DEFAULT_COLUMN_WIDTH) for col in visible_columns)
    cell_colors = apply_cell_colors(df_display, enriched_df, groups, columns=visible_columns)

    for row_index in range(len(df_display)):
        for col in PORTFOLIO_EXTRA_COLUMNS:
            if col in visible_columns:
                cell_colors.pop((row_index, visible_columns.index(col)), None)

    for row_index in range(len(df_display)):
        ticker = str(df_display.iloc[row_index]["Ticker"])
        is_total_row = str(df_display.iloc[row_index].get("Name", "")) == "Portfolio Total"
        if is_total_row:
            pnl_pct = total_row.get("P/L%") if total_row else np.nan
        elif not extra_by_ticker.empty and ticker in extra_by_ticker.index:
            pnl_pct = extra_by_ticker.loc[ticker].get("P/L%")
        else:
            pnl_pct = np.nan
        if pd.notna(pnl_pct):
            color = red_green(pnl_pct, neg_clip=-50.0, pos_clip=50.0)
            for col in ("Market Value", "P/L", "P/L%"):
                if col in visible_columns:
                    cell_colors[(row_index, visible_columns.index(col))] = color
        for source_pct_col, abs_col in PORTFOLIO_CHANGE_PERIODS:
            if is_total_row:
                period_pct = total_row.get(source_pct_col) if total_row else np.nan
            elif not extra_by_ticker.empty and ticker in extra_by_ticker.index:
                period_pct = extra_by_ticker.loc[ticker].get(source_pct_col)
            else:
                period_pct = np.nan
            if pd.notna(period_pct):
                color = red_green(period_pct)
                columns_to_color = (abs_col, source_pct_col) if is_total_row else (abs_col,)
                for col in columns_to_color:
                    if col in visible_columns:
                        cell_colors[(row_index, visible_columns.index(col))] = color
        if is_total_row and total_row and pd.notna(total_row.get("Beta", np.nan)) and "Ticker" in visible_columns:
            cell_colors[(row_index, visible_columns.index("Ticker"))] = beta_color(total_row.get("Beta"))

    html_table = f"""
    <div style="width:100%; max-height:650px; overflow:auto;
                border:1px solid {theme['table_border']};">
        <table style="width:{table_width}px; min-width:100%; table-layout:fixed; border-collapse:collapse;
                      font-family:Arial; font-size:12px;
                      background-color:{theme['table_bg']};
                      color:{theme['text']};">
            <colgroup>
    """
    for col in visible_columns:
        html_table += f"<col style='width:{COLUMN_WIDTHS.get(col, DEFAULT_COLUMN_WIDTH)}px;'>"
    html_table += f"""
            </colgroup>
            <thead style="position:sticky; top:0; z-index:10;
                          background-color:{theme['table_header_bg']};">
                <tr style="background-color:{theme['table_header_bg']};">
    """
    for col in visible_columns:
        html_table += (
            f"<th style='padding:4px; text-align:left; color:{theme['text']}; "
            f"white-space:nowrap; overflow:hidden; text-overflow:ellipsis; "
            f"border:1px solid {theme['table_border']};'>{html.escape(col)}</th>"
        )
    html_table += "</tr></thead><tbody>"

    for row_index in range(len(df_display)):
        row = df_display.iloc[row_index]
        ticker = str(row["Ticker"])
        is_header = ticker in group_names
        is_total = str(row.get("Name", "")) == "Portfolio Total"
        html_table += "<tr>"
        for col_index, col in enumerate(visible_columns):
            val = "" if pd.isna(row[col]) else str(row[col])
            bg_color = cell_colors.get(
                (row_index, col_index),
                theme["table_group_bg"] if is_header or is_total else theme["table_bg"],
            )
            if dark_mode and bg_color.lower() in ("#ffffff", "white", "#cccccc"):
                bg_color = theme["table_group_bg"] if is_header or is_total else theme["table_bg"]
            text_color = theme["text"] if bg_color == theme["table_bg"] else readable_text_color(bg_color)

            if is_header and col_index == 0:
                html_table += (
                    f"<td colspan='{len(visible_columns)}' style='padding:4px; color:{theme['text']}; "
                    f"background-color:{bg_color}; font-weight:bold; border:1px solid {theme['table_border']};'>"
                    f"{html.escape(val)}</td>"
                )
                break
            if is_header:
                continue

            align = "right" if col in RIGHT_ALIGNED_COLUMNS or col in PORTFOLIO_EXTRA_COLUMNS or (val and val[0] in "+-$€¥0123456789") else "left"
            font_weight = "font-weight:bold;" if is_total else ""
            title_attr = f" title='{html.escape(val, quote=True)}'" if col == "Name" and val else ""
            html_table += (
                f"<td{title_attr} style='padding:4px; text-align:{align}; {font_weight}"
                f"color:{text_color}; background-color:{bg_color}; white-space:nowrap; "
                f"overflow:hidden; text-overflow:ellipsis; border:1px solid {theme['table_border']};'>"
                f"{html.escape(val)}</td>"
            )
        html_table += "</tr>"

    html_table += "</tbody></table></div>"
    st.markdown(html_table, unsafe_allow_html=True)
    return treemap_rows


def build_portfolio_treemap(treemap_rows, dark_mode=False):
    rows = pd.DataFrame(treemap_rows)
    if rows.empty:
        return None
    rows["Group"] = rows["Group"].fillna("Portfolio").replace("", "Portfolio")
    rows["Area"] = pd.to_numeric(rows.get("Market Value EUR", np.nan), errors="coerce")
    rows["Market Value"] = pd.to_numeric(rows.get("Market Value", np.nan), errors="coerce")
    rows["1D%"] = pd.to_numeric(rows.get("1D%", np.nan), errors="coerce")
    rows = rows[(rows["Area"].notna()) & (rows["Area"] > 0)]
    if rows.empty:
        return None

    theme = get_theme(dark_mode)
    labels = ["Portfolio"]
    ids = ["root"]
    parents = [""]
    values = [float(rows["Area"].sum())]
    colors = [0.0]
    text = [""]
    customdata = [["", "", "", ""]]

    for group, group_df in rows.groupby("Group", sort=True):
        group_id = f"group:{group}"
        group_value = float(group_df["Area"].sum())
        valid_mask = group_df["1D%"].notna()
        group_color = float(np.average(group_df[valid_mask]["1D%"], weights=group_df[valid_mask]["Area"])) if valid_mask.any() else 0.0
        labels.append(group)
        ids.append(group_id)
        parents.append("root")
        values.append(group_value)
        colors.append(group_color)
        text.append(f"{group_color:+.2f}%" if valid_mask.any() else "N/A")
        customdata.append([group, "", "", ""])

        for _, row in group_df.sort_values("Area", ascending=False).iterrows():
            pct = float(row["1D%"]) if pd.notna(row["1D%"]) else 0.0
            pct_text = f"{pct:+.2f}%" if pd.notna(row["1D%"]) else "N/A"
            buy_currency = normalize_currency_code(row.get("Buy Currency"))
            labels.append(str(row["Ticker"]))
            ids.append(f"ticker:{row['Ticker']}")
            parents.append(group_id)
            values.append(float(row["Area"]))
            colors.append(pct)
            text.append(pct_text)
            customdata.append([
                row.get("Name") or row["Ticker"],
                group,
                format_currency_value(row.get("Market Value"), buy_currency),
                pct_text,
            ])

    fig = go.Figure(go.Treemap(
        labels=labels,
        ids=ids,
        parents=parents,
        values=values,
        branchvalues="total",
        text=text,
        textinfo="label+text",
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
            "Group: %{customdata[1]}<br>"
            "Market Value: %{customdata[2]}<br>"
            "1D: %{customdata[3]}<extra></extra>"
        ),
        maxdepth=2,
    ))
    fig.update_layout(
        title=dict(text="Portfolio Holdings Treemap (area = latest value, color = 1D%)", font=dict(color=theme["text"], size=18)),
        height=650,
        margin=dict(l=8, r=8, t=44, b=8),
        paper_bgcolor=theme["page_bg"],
        plot_bgcolor=theme["plot_bg"],
        font=dict(color=theme["text"], size=12),
    )
    return fig


def render_table_legend(dark_mode=False):
    theme = get_theme(dark_mode)
    st.markdown(
        f"""
        <div style="font-size:12px; color:{theme['muted']}; margin:4px 0 8px 0;">
            <span style="display:inline-block;width:12px;height:12px;background:#dcfce7;border:1px solid {theme['table_border']};vertical-align:middle;"></span>
            Price: regular/latest close
            &nbsp;&nbsp;
            <span style="display:inline-block;width:12px;height:12px;background:#dbeafe;border:1px solid {theme['table_border']};vertical-align:middle;"></span>
            pre-market estimate
            &nbsp;&nbsp;
            <span style="display:inline-block;width:12px;height:12px;background:#fef3c7;border:1px solid {theme['table_border']};vertical-align:middle;"></span>
            after-hours estimate
            &nbsp;&nbsp;|&nbsp;&nbsp;
            Ticker cell color reflects beta: greener below 1, redder above 1.
        </div>
        """,
        unsafe_allow_html=True,
    )


def should_convert_price_ticker(ticker):
    if not ticker:
        return False
    ticker = str(ticker).upper()
    if ticker in NON_PRICE_TICKERS:
        return False
    if ticker.startswith("^") or ticker.endswith("=X"):
        return False
    return True


def fallback_currency_from_ticker(ticker):
    ticker_upper = str(ticker).upper()
    for suffix, currency in TICKER_CURRENCY_SUFFIXES.items():
        if ticker_upper.endswith(suffix):
            return currency
    if "-" in ticker_upper and ticker_upper.endswith("-USD"):
        return "USD"
    return "USD"


def normalize_currency_code(currency):
    raw = str(currency or "").strip()
    if raw in {"GBp", "GBX", "GBX"}:
        return "GBX"
    return raw.upper()


@st.cache_data(ttl=86400, show_spinner=False)
def get_ticker_currency(ticker):
    ticker = normalize_yfinance_ticker(ticker)
    if not should_convert_price_ticker(ticker):
        return None
    try:
        fast_info = yf.Ticker(ticker).fast_info
        currency = getattr(fast_info, "currency", None)
        if not currency and hasattr(fast_info, "get"):
            currency = fast_info.get("currency")
        if currency:
            return normalize_currency_code(currency)
    except (KeyError, ValueError, AttributeError, TypeError, RuntimeError):
        pass
    try:
        currency = yf.Ticker(ticker).info.get("currency")
        if currency:
            return normalize_currency_code(currency)
    except (KeyError, ValueError, AttributeError, TypeError, RuntimeError):
        pass
    return fallback_currency_from_ticker(ticker)


def _latest_yahoo_price(ticker):
    hist = yf.Ticker(ticker).history(period="5d", interval="1d")
    if hist is None or hist.empty or "Close" not in hist:
        return None
    close = hist["Close"].dropna()
    if close.empty:
        return None
    return float(close.iloc[-1])


@st.cache_data(ttl=1800, show_spinner=False)
def currency_to_eur_rate(currency):
    currency = normalize_currency_code(currency)
    if not currency:
        return None
    if currency == "EUR":
        return 1.0
    if currency == "GBX":
        gbp_rate = currency_to_eur_rate("GBP")
        return gbp_rate / 100.0 if gbp_rate else None

    eur_base = f"EUR{currency}=X"
    try:
        quote_per_eur = _latest_yahoo_price(eur_base)
        if quote_per_eur and quote_per_eur > 0:
            return 1.0 / quote_per_eur
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        pass

    eur_quote = f"{currency}EUR=X"
    try:
        eur_per_quote = _latest_yahoo_price(eur_quote)
        if eur_per_quote and eur_per_quote > 0:
            return eur_per_quote
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        pass
    return None


def eur_multiplier_for_ticker(ticker):
    currency = get_ticker_currency(ticker)
    if not currency:
        return None, None
    return currency_to_eur_rate(currency), currency


def convert_value_to_eur(value, multiplier):
    if multiplier is None or value is None or pd.isna(value):
        return value
    try:
        return float(value) * float(multiplier)
    except (TypeError, ValueError):
        return value


def convert_stock_df_for_display(df, display_currency):
    if display_currency != "EUR" or df.empty:
        return df
    converted = df.copy()
    for idx, row in converted.iterrows():
        ticker = row.get("Ticker")
        multiplier, _ = eur_multiplier_for_ticker(ticker)
        if multiplier is None:
            continue
        for col in ["Price", "Price Target", "Market Cap"]:
            if col in converted.columns:
                converted.at[idx, col] = convert_value_to_eur(row.get(col), multiplier)
    return converted


def convert_numeric_list(values, multiplier):
    if multiplier is None:
        return values
    return [convert_value_to_eur(value, multiplier) for value in values]


def convert_kline_data_for_display(kline_data, ticker, display_currency):
    if display_currency != "EUR" or not kline_data or not kline_data.get("success"):
        return kline_data
    multiplier, original_currency = eur_multiplier_for_ticker(ticker)
    if multiplier is None:
        return kline_data

    converted = copy.deepcopy(kline_data)
    converted["display_currency"] = "EUR"
    converted["original_currency"] = original_currency
    converted["fx_multiplier_to_eur"] = multiplier

    for key in ["open", "high", "low", "close"]:
        if key in converted.get("ohlc", {}):
            converted["ohlc"][key] = convert_numeric_list(converted["ohlc"][key], multiplier)

    price_indicator_keys = [
        "ma5", "ma10", "ma20", "ma50", "ma100", "ma200",
        "bollinger_upper", "bollinger_lower",
        "macd", "signal", "hist",
        "chip_prices",
    ]
    for key in price_indicator_keys:
        if key in converted.get("indicators", {}) and converted["indicators"][key] is not None:
            converted["indicators"][key] = convert_numeric_list(converted["indicators"][key], multiplier)
    if "chip_peak_price" in converted.get("indicators", {}):
        converted["indicators"]["chip_peak_price"] = convert_value_to_eur(converted["indicators"]["chip_peak_price"], multiplier)

    for key in ["market_cap", "price_target"]:
        if key in converted.get("financials", {}):
            converted["financials"][key] = convert_value_to_eur(converted["financials"][key], multiplier)
    return converted


def update_page_from_editor(config, section, page_index, name, edited_groups_text):
    config[section][page_index]["name"] = str(name).strip() or config[section][page_index]["name"]
    config[section][page_index]["groups"] = editor_text_to_groups(edited_groups_text)
    return normalize_config(config)


def update_portfolio_page_from_editor(config, page_index, name, edited_text):
    page = config["portfolio_pages"][page_index]
    page["name"] = str(name).strip() or page["name"]
    page["holdings"] = portfolio_editor_text_to_holdings(edited_text)
    return normalize_config(config)


def add_page(config, section, name):
    if section == "portfolio_pages":
        config[section].append({"name": name.strip() or "New Portfolio", "holdings": []})
    else:
        config[section].append({"name": name.strip() or "New Page", "groups": {}})
    return normalize_config(config)


def delete_page(config, section, page_index):
    if len(config[section]) > 1:
        config[section].pop(page_index)
    return normalize_config(config)


@st.cache_data(ttl=300, show_spinner=False)
def fetch_stock_data(config_json, cache_key):
    config = normalize_config(json.loads(config_json))
    payload = {
        "groups": config_to_api_groups(config),
        "broad_market_tickers": broad_market_tickers(config),
    }
    if cache_key:
        payload["cache_key"] = cache_key
    try:
        resp = requests.post(f"{API_BASE}/api/stock_data", json=payload, timeout=180)
        if resp.status_code != 200:
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text}"}
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        return {"success": False, "error": f"Backend request failed: {e}"}


@st.cache_data(ttl=600, show_spinner=False)
def fetch_breadth_data():
    sp500_syms = stock_watch_list_back_end.get_sp500_symbols()
    nasdaq100_syms = stock_watch_list_back_end.get_nasdaq100_symbols()
    payload = {
        "sp500_symbols": json.dumps(sp500_syms),
        "nasdaq100_symbols": json.dumps(nasdaq100_syms),
    }
    try:
        resp = requests.post(f"{API_BASE}/api/breadth_data", data=payload, timeout=300)
        return resp.json() if resp.status_code == 200 else {"success": False, "error": resp.text}
    except (requests.RequestException, ValueError) as e:
        return {"success": False, "error": str(e)}


@st.cache_data(ttl=600, show_spinner=False)
def fetch_fear_greed(path):
    try:
        resp = requests.get(f"{API_BASE}{path}", timeout=10)
        return resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_vix_kline():
    params = {"ticker": "^VIX", "period": 10, "interval": "1d"}
    try:
        resp = requests.get(f"{API_BASE}/api/kline_data", params=params, timeout=120)
        return resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


@st.cache_data(ttl=60, show_spinner=False)
def fetch_kline_data(ticker, period, interval, cache_key=""):
    params = {"ticker": ticker, "period": period, "interval": interval}
    if cache_key:
        params["cache_key"] = cache_key
    try:
        resp = requests.get(f"{API_BASE}/api/kline_data", params=params, timeout=120)
        return resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


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
    chart_data = breadth_data[chart_key]
    dates = chart_data["index"]

    fig = go.Figure()
    for key, color in [("20MA_Ratio", "red"), ("50MA_Ratio", "orange"), ("200MA_Ratio", "blue")]:
        if key in chart_data:
            fig.add_trace(go.Scatter(x=dates, y=chart_data[key], name=key, line=dict(color=color, width=1.5)))
    if chart_data.get(index_key):
        gspc_color = "#f9fafb" if dark_mode else "#111827"
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=chart_data[index_key],
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


def display_fear_greed(fg_data, title, dark_mode=False):
    if not fg_data or not fg_data.get("success"):
        st.metric(title, "N/A")
        return
    try:
        value = float(fg_data.get("value", 50))
    except (TypeError, ValueError):
        st.metric(title, "N/A")
        return
    value = max(0, min(100, value))
    description = fg_data.get("description", "") or "N/A"
    st.plotly_chart(build_fear_greed_gauge(value, description, title, dark_mode=dark_mode), width="stretch")


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
    st.plotly_chart(build_vix_gauge(value, dark_mode=dark_mode), width="stretch")


def build_kline_chart(kline_data, ticker, fib_levels=None, dark_mode=False):
    if not kline_data or not kline_data.get("success"):
        st.warning("K-line data not available")
        return None

    theme = get_theme(dark_mode)
    ohlc = kline_data["ohlc"]
    indicators = kline_data["indicators"]
    financials = kline_data.get("financials", {})
    dates = pd.to_datetime(kline_data["dates"]).to_pydatetime().tolist()
    if not dates:
        st.warning("K-line data not available")
        return None

    n = len(dates)
    closes = ohlc["close"]

    def _fmt(value, fmt_str=None):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "N/A"
        if fmt_str:
            try:
                return fmt_str.format(float(value))
            except (TypeError, ValueError):
                return str(value)
        return str(value)

    sa_url = stockanalysis_overview_url(ticker)
    title_suffix = ""
    if kline_data.get("display_currency") == "EUR":
        title_suffix = f" (EUR converted from {html.escape(str(kline_data.get('original_currency') or 'local'))}, latest FX)"
    if kline_data.get("price_source"):
        source_text = html.escape(str(kline_data.get("price_source")))
        extended_time = html.escape(str(kline_data.get("extended_time") or ""))
        title_suffix += f" | {source_text}" + (f" @ {extended_time}" if extended_time else "")
    title = (
        f"<b>K-Curve {html.escape(ticker)}{title_suffix}</b> | "
        f"Market Cap: {_fmt(financials.get('market_cap'))}, "
        f"PE: {_fmt(financials.get('trailing_pe'))}/{_fmt(financials.get('forward_pe'))}, "
        f"P/S: {_fmt(financials.get('price_to_sales'))}, "
        f"P/B: {_fmt(financials.get('price_to_book'))}, "
        f"PEG: {_fmt(financials.get('peg_ratio'))}, "
        f"Next Earnings: {_fmt(financials.get('next_earnings'))}, "
        f"Analysts: {_fmt(financials.get('analyst_rating'))}, "
        f"Target: {_fmt(financials.get('price_target'))}"
    )

    td_sell = [0] * n
    td_buy = [0] * n
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

    fig = make_subplots(
        rows=6,
        cols=2,
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

    fig.add_trace(
        go.Candlestick(
            x=dates,
            open=ohlc["open"],
            high=ohlc["high"],
            low=ohlc["low"],
            close=ohlc["close"],
            name="OHLC",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        ),
        row=1,
        col=1,
    )

    ma_colors = {
        "ma5": "#60a5fa" if dark_mode else "blue",
        "ma10": "#fbbf24" if dark_mode else "orange",
        "ma20": "#c084fc" if dark_mode else "purple",
        "ma50": "#f97316" if dark_mode else "brown",
        "ma100": "#f9a8d4" if dark_mode else "pink",
        "ma200": "#d1d5db" if dark_mode else "gray",
    }
    for key, color in ma_colors.items():
        if indicators.get(key):
            fig.add_trace(
                go.Scatter(
                    x=dates,
                    y=indicators[key],
                    name=key.upper(),
                    line=dict(color=color, width=1, dash="dash"),
                    showlegend=True,
                ),
                row=1,
                col=1,
            )

    if indicators.get("bollinger_upper"):
        fig.add_trace(
            go.Scatter(x=dates, y=indicators["bollinger_upper"], name="BB Upper", line=dict(color="cyan", width=0.8)),
            row=1,
            col=1,
        )
    if indicators.get("bollinger_lower"):
        fig.add_trace(
            go.Scatter(x=dates, y=indicators["bollinger_lower"], name="BB Lower", line=dict(color="cyan", width=0.8)),
            row=1,
            col=1,
        )

    if indicators.get("chip_peak_price"):
        fig.add_hline(
            y=indicators["chip_peak_price"],
            line_dash="dash",
            line_color="gray",
            annotation_text=f"Peak of Chip: {indicators['chip_peak_price']:.2f}",
            row=1,
            col=1,
        )

    fig.add_trace(
        go.Scatter(
            x=[dates[-1]],
            y=[ohlc["close"][-1]],
            mode="markers",
            marker=dict(color="red", symbol="x", size=10),
            name=f"Latest ({dates[-1].strftime('%Y-%m-%d')}: {ohlc['close'][-1]:.2f})",
            showlegend=True,
        ),
        row=1,
        col=1,
    )

    annotations = []
    for i in range(n):
        if 0 < td_sell[i] <= 9:
            annotations.append(
                dict(
                    x=dates[i],
                    y=ohlc["high"][i] * 1.003,
                    text=str(td_sell[i]),
                    showarrow=False,
                    font=dict(color="red", size=12, family="Arial Black" if td_sell[i] == 9 else "Arial"),
                    xref="x",
                    yref="y",
                )
            )
        if 0 < td_buy[i] <= 9:
            annotations.append(
                dict(
                    x=dates[i],
                    y=ohlc["low"][i] * 0.997,
                    text=str(td_buy[i]),
                    showarrow=False,
                    font=dict(color="green", size=12, family="Arial Black" if td_buy[i] == 9 else "Arial"),
                    xref="x",
                    yref="y",
                )
            )

    if indicators.get("chip_prices") and indicators.get("chip_volumes"):
        fig.add_trace(
            go.Bar(
                x=indicators["chip_volumes"],
                y=indicators["chip_prices"],
                orientation="h",
                name="Chip",
                marker_color="skyblue",
                showlegend=False,
            ),
            row=1,
            col=2,
        )
        fig.update_xaxes(title_text="Volume", row=1, col=2, showgrid=True)
        fig.update_yaxes(matches="y", row=1, col=2, side="right", title_text="", showgrid=True)

    vol_colors = ["#26a69a" if ohlc["close"][i] >= ohlc["open"][i] else "#ef5350" for i in range(n)]
    fig.add_trace(
        go.Bar(x=dates, y=ohlc["volume"], name="Volume", marker_color=vol_colors, showlegend=False),
        row=2,
        col=1,
    )
    fig.update_yaxes(title_text="Volume", row=2, col=1, showgrid=True)
    fig.update_xaxes(row=2, col=1, showgrid=True)

    if indicators.get("macd"):
        fig.add_trace(go.Scatter(x=dates, y=indicators["macd"], name="MACD", line=dict(color="#60a5fa" if dark_mode else "blue", width=1)), row=3, col=1)
    if indicators.get("signal"):
        fig.add_trace(go.Scatter(x=dates, y=indicators["signal"], name="Signal", line=dict(color="#f87171" if dark_mode else "red", width=1)), row=3, col=1)
    if indicators.get("hist"):
        hist_colors = ["#26a69a" if value >= 0 else "#ef5350" for value in indicators["hist"]]
        fig.add_trace(go.Bar(x=dates, y=indicators["hist"], name="Hist", marker_color=hist_colors, showlegend=False), row=3, col=1)
    fig.update_yaxes(title_text="MACD", row=3, col=1, showgrid=True)
    fig.update_xaxes(row=3, col=1, showgrid=True)

    if indicators.get("kdj_k"):
        fig.add_trace(go.Scatter(x=dates, y=indicators["kdj_k"], name="K", line=dict(color="#60a5fa" if dark_mode else "blue", width=1)), row=4, col=1)
    if indicators.get("kdj_d"):
        fig.add_trace(go.Scatter(x=dates, y=indicators["kdj_d"], name="D", line=dict(color="#fbbf24" if dark_mode else "orange", width=1)), row=4, col=1)
    if indicators.get("kdj_j"):
        fig.add_trace(go.Scatter(x=dates, y=indicators["kdj_j"], name="J", line=dict(color="#34d399" if dark_mode else "green", width=1)), row=4, col=1)
    fig.add_hline(y=20, line_dash="dash", line_color="gray", row=4, col=1)
    fig.add_hline(y=80, line_dash="dash", line_color="gray", row=4, col=1)
    fig.update_yaxes(title_text="KDJ", row=4, col=1, showgrid=True)
    fig.update_xaxes(row=4, col=1, showgrid=True)

    if indicators.get("rsi"):
        fig.add_trace(go.Scatter(x=dates, y=indicators["rsi"], name="RSI", line=dict(color="#c084fc" if dark_mode else "purple", width=1)), row=5, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="gray", row=5, col=1)
    fig.add_hline(y=70, line_dash="dash", line_color="gray", row=5, col=1)
    fig.update_yaxes(title_text="RSI", row=5, col=1, showgrid=True)
    fig.update_xaxes(row=5, col=1, showgrid=True)

    fig.add_trace(go.Bar(x=dates, y=td_sell, name="TD Sell", marker_color="red", showlegend=False), row=6, col=1)
    fig.add_trace(go.Bar(x=dates, y=[-value for value in td_buy], name="TD Buy", marker_color="green", showlegend=False), row=6, col=1)
    for i in range(n):
        if 0 < td_sell[i] <= 9:
            fig.add_trace(
                go.Scatter(
                    x=[dates[i]],
                    y=[td_sell[i]],
                    text=[str(td_sell[i])],
                    mode="text",
                    textposition="top center",
                    textfont=dict(color="red", size=11, family="Arial Black" if td_sell[i] == 9 else "Arial"),
                    showlegend=False,
                ),
                row=6,
                col=1,
            )
        if 0 < td_buy[i] <= 9:
            fig.add_trace(
                go.Scatter(
                    x=[dates[i]],
                    y=[-td_buy[i]],
                    text=[str(td_buy[i])],
                    mode="text",
                    textposition="bottom center",
                    textfont=dict(color="green", size=11, family="Arial Black" if td_buy[i] == 9 else "Arial"),
                    showlegend=False,
                ),
                row=6,
                col=1,
            )
    fig.update_yaxes(title_text="TD Seq", row=6, col=1, range=[-13, 13], showgrid=True)
    fig.update_xaxes(row=6, col=1, showgrid=True)
    fig.update_xaxes(row=1, col=1, showgrid=True)
    fig.update_yaxes(row=1, col=1, showgrid=True)

    if fib_levels:
        for level, label, color in fib_levels:
            fig.add_hline(
                y=level,
                line_dash="dash",
                line_color=color,
                annotation_text=f"{label}  {level:.2f}",
                annotation_position="right",
                annotation_font=dict(size=9, color=color),
                row=1,
                col=1,
            )

    fig.update_layout(
        title=dict(text=title, x=0.01, xanchor="left", font=dict(color=theme["text"], size=15)),
        height=1100,
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.15,
            xanchor="left",
            x=0,
            font=dict(color=theme["text"], size=11),
            bgcolor="rgba(17,24,39,0.82)" if dark_mode else "rgba(255,255,255,0.82)",
            bordercolor=theme["table_border"],
            borderwidth=1,
        ),
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

    if sa_url:
        fig.add_annotation(
            text=f"<a href='{sa_url}' style='color:{theme['link']}; font-style:italic; font-size:12px;'>{sa_url}</a>",
            xref="paper",
            yref="paper",
            x=0.99,
            y=1.10,
            xanchor="right",
            yanchor="top",
            showarrow=False,
        )

    for row in range(1, 6):
        fig.update_xaxes(showticklabels=False, row=row, col=1)

    return fig


def render_auth_panel():
    with st.sidebar:
        st.header("Account")
        user = st.session_state.get("user")
        if user:
            st.success(f"Signed in as {user['display_name']}")
            st.caption(f"Cache key: {user['cache_key']}")
            if st.button("Sign out", width="stretch"):
                st.session_state.pop("user", None)
                st.session_state.pop("watchlist_config", None)
                st.rerun()
            return user

        st.caption("Guests can view the default watch list but cannot edit it.")
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in")
        if submitted:
            # Check lock status before attempting authentication.
            is_locked, remaining = check_login_lock_status(username)
            if is_locked:
                mins = remaining // 60
                secs = remaining % 60
                if mins > 0:
                    st.error(f"Too many failed attempts. Please try again in {mins}m {secs}s.")
                else:
                    st.error(f"Too many failed attempts. Please try again in {secs}s.")
            else:
                user = authenticate(username, password)
                if user:
                    st.session_state["user"] = user
                    st.session_state["watchlist_config"] = get_user_config(user["id"])
                    st.rerun()
                else:
                    # Check if this attempt triggered a lockout.
                    is_locked_now, remaining_now = check_login_lock_status(username)
                    if is_locked_now:
                        mins = remaining_now // 60
                        st.error(f"Too many failed attempts. Please try again in {mins} minutes.")
                    else:
                        st.error("Invalid username or password")
        return None


def get_active_config(user):
    if user:
        if "watchlist_config" not in st.session_state:
            st.session_state["watchlist_config"] = get_user_config(user["id"])
        return normalize_config(st.session_state["watchlist_config"])
    return default_watchlist_config()


def save_active_config(user, config):
    st.session_state["watchlist_config"] = normalize_config(config)
    save_user_config(user["id"], st.session_state["watchlist_config"])
    fetch_stock_data.clear()
    st.success("Watch list saved")


def render_readonly_groups(groups, dark_mode=False):
    theme = get_theme(dark_mode)
    html_table = f"""
    <div style="width:100%; overflow:auto; border:1px solid {theme['table_border']}; border-radius:6px;">
        <table style="width:100%; border-collapse:collapse; font-family:Arial; font-size:13px; background-color:{theme['table_bg']}; color:{theme['text']};">
            <thead>
                <tr style="background-color:{theme['table_header_bg']};">
                    <th style="padding:6px; text-align:left; border:1px solid {theme['table_border']}; color:{theme['text']};">Group</th>
                    <th style="padding:6px; text-align:left; border:1px solid {theme['table_border']}; color:{theme['text']};">Tickers</th>
                </tr>
            </thead>
            <tbody>
    """
    for group, tickers in groups.items():
        html_table += (
            "<tr>"
            f"<td style='padding:6px; border:1px solid {theme['table_border']}; color:{theme['text']};'>"
            f"{html.escape(group)}</td>"
            f"<td style='padding:6px; border:1px solid {theme['table_border']}; color:{theme['text']};'>"
            f"{html.escape(', '.join(tickers))}</td>"
            "</tr>"
        )
    html_table += """
            </tbody>
        </table>
    </div>
    """
    st.markdown(html_table, unsafe_allow_html=True)


def render_page_editor(config, section, page_index, editable, user, dark_mode=False):
    page = config[section][page_index]
    section_meta = SECTION_META[section]
    st.caption(
        "Page = one tab. Group = table section header. Tickers = comma-separated yfinance symbols. "
        f"{section_meta['help']}"
    )
    if editable:
        version_key = f"{section}_{page_index}_editor_version"
        editor_version = st.session_state.get(version_key, 0)

        with st.form(f"{section}_{page_index}_editor_form_{editor_version}"):
            new_name = st.text_input("Page name", page["name"], key=f"{section}_{page_index}_{editor_version}_name")
            edited = st.text_area(
                "Groups",
                value=groups_to_editor_text(page["groups"]),
                key=f"{section}_{page_index}_{editor_version}_groups_text",
                height=max(160, min(420, 28 * (len(page["groups"]) + 5))),
                help="One line per group: Group | AAPL, MSFT, NVDA",
            )
            submitted = st.form_submit_button("Save")

        if submitted:
            updated_config = update_page_from_editor(config, section, page_index, new_name, edited)
            save_active_config(user, updated_config)
            st.session_state[version_key] = editor_version + 1
            st.rerun()
        return config

    st.caption("Read-only guest view")
    render_readonly_groups(page["groups"], dark_mode=dark_mode)
    return config


def render_portfolio_page_editor(config, page_index, editable, user):
    page = config["portfolio_pages"][page_index]
    st.caption(
        "One holding per row: Group | Ticker | Buy Price | Shares | Buy Currency. "
        "Buy Currency is required and should be a code such as USD, EUR, CNY, HKD, GBP or JPY."
    )
    if not editable:
        if not page.get("holdings"):
            st.caption("Read-only guest view: no portfolio holdings configured.")
        else:
            st.dataframe(portfolio_editor_df(page), width="stretch", hide_index=True)
        return config

    version_key = f"portfolio_pages_{page_index}_editor_version"
    editor_version = st.session_state.get(version_key, 0)
    with st.form(f"portfolio_pages_{page_index}_editor_form_{editor_version}"):
        new_name = st.text_input(
            "Portfolio page name",
            page["name"],
            key=f"portfolio_pages_{page_index}_{editor_version}_name",
        )
        edited_text = st.text_area(
            "Holdings",
            value=portfolio_holdings_to_editor_text(page),
            key=f"portfolio_pages_{page_index}_{editor_version}_holdings_text",
            height=max(180, min(420, 28 * (len(page.get("holdings", [])) + 6))),
            help=(
                "Example: Longs | AAPL | 180.50 | 10 | USD. "
                "You can also omit Group: AAPL | 180.50 | 10 | USD."
            ),
        )
        submitted = st.form_submit_button("Save Portfolio")

    if submitted:
        updated_config = update_portfolio_page_from_editor(config, page_index, new_name, edited_text)
        save_active_config(user, updated_config)
        st.session_state[version_key] = editor_version + 1
        st.rerun()
    if page.get("holdings"):
        with st.expander("Current holdings preview", expanded=False):
            st.dataframe(portfolio_editor_df(page), width="stretch", hide_index=True)
    return config


def render_section(section_title, section_key, config, raw_df, editable, user, dark_mode=False, display_currency="Local"):
    st.subheader(section_title)
    st.caption(SECTION_META[section_key]["help"])
    render_table_legend(dark_mode=dark_mode)
    pages = config[section_key]
    tabs = st.tabs([page["name"] for page in pages])
    for i, tab in enumerate(tabs):
        with tab:
            with st.expander("Watch list editor", expanded=False):
                config = render_page_editor(config, section_key, i, editable, user, dark_mode=dark_mode)
                if editable:
                    c1, c2 = st.columns([1, 5])
                    with c1:
                        if st.button("Delete page", key=f"{section_key}_{i}_delete", disabled=len(pages) <= 1):
                            config = delete_page(config, section_key, i)
                            save_active_config(user, config)
                            st.rerun()
            # Table column display controls
            display_col1, display_col2, display_col3, display_col4 = st.columns(4)

            with display_col1:
                show_name_column = st.toggle(
                    "Show Name column next to Ticker",
                    value=False,
                    key=f"{section_key}_{i}_show_name_column",
                    help=(
                        "Turn this on to insert the cached yfinance "
                        "display-name column between Ticker and Price."
                    ),
                )

            with display_col2:
                show_relative_momentum_columns = st.toggle(
                    "Show relative momentum columns",
                    value=False,
                    key=f"{section_key}_{i}_show_relative_momentum_columns",
                    help=(
                        "Show 20D/60D/120D excess returns versus ^GSPC "
                        "and the weighted 3/6/12M relative momentum column."
                    ),
                )

            with display_col3:
                show_financial_columns = st.toggle(
                    "Show financial columns",
                    value=False,
                    key=f"{section_key}_{i}_show_financial_columns",
                    help=(
                        "Show Next Earnings, PE, PEG, Analysts, Price Target "
                        "and Market Cap columns."
                    ),
                )

            with display_col4:
                show_ema_columns = st.toggle(
                    "Show EMA deviation columns",
                    value=False,
                    key=f"{section_key}_{i}_show_ema_columns",
                    help=(
                        "Show Diff_EMA5%, Diff_EMA10%, Diff_EMA20%, "
                        "Diff_EMA50%, Diff_EMA100% and Diff_EMA200%."
                    ),
                )

            render_market_table(
                raw_df,
                config[section_key][i],
                dark_mode=dark_mode,
                display_currency=display_currency,
                show_name_column=show_name_column,
                show_relative_momentum_columns=show_relative_momentum_columns,
                show_ema_columns=show_ema_columns,
                show_financial_columns=show_financial_columns,
            )
    return config


def render_portfolio_section(config, raw_df, editable, user, dark_mode=False, display_currency="Local"):
    st.subheader(SECTION_META["portfolio_pages"]["title"])
    st.caption(SECTION_META["portfolio_pages"]["help"])
    render_table_legend(dark_mode=dark_mode)
    pages = config["portfolio_pages"]
    tabs = st.tabs([page["name"] for page in pages])
    for i, tab in enumerate(tabs):
        with tab:
            with st.expander("Portfolio editor", expanded=False):
                config = render_portfolio_page_editor(config, i, editable, user)
                if editable:
                    c1, c2 = st.columns([1, 5])
                    with c1:
                        if st.button("Delete page", key=f"portfolio_pages_{i}_delete", disabled=len(pages) <= 1):
                            config = delete_page(config, "portfolio_pages", i)
                            save_active_config(user, config)
                            st.rerun()

            display_col1, display_col2, display_col3, display_col4 = st.columns(4)
            with display_col1:
                show_name_column = st.toggle(
                    "Show Name column next to Ticker",
                    value=False,
                    key=f"portfolio_pages_{i}_show_name_column",
                    help=(
                        "Turn this on to insert the cached yfinance "
                        "display-name column between Ticker and Price."
                    ),
                )
            with display_col2:
                show_relative_momentum_columns = st.toggle(
                    "Show relative momentum columns",
                    value=False,
                    key=f"portfolio_pages_{i}_show_relative_momentum_columns",
                    help=(
                        "Show 20D/60D/120D excess returns versus ^GSPC "
                        "and the weighted 3/6/12M relative momentum column."
                    ),
                )
            with display_col3:
                show_financial_columns = st.toggle(
                    "Show financial columns",
                    value=False,
                    key=f"portfolio_pages_{i}_show_financial_columns",
                    help=(
                        "Show Next Earnings, PE, PEG, Analysts, Price Target "
                        "and Market Cap columns."
                    ),
                )
            with display_col4:
                show_ema_columns = st.toggle(
                    "Show EMA deviation columns",
                    value=False,
                    key=f"portfolio_pages_{i}_show_ema_columns",
                    help=(
                        "Show Diff_EMA5%, Diff_EMA10%, Diff_EMA20%, "
                        "Diff_EMA50%, Diff_EMA100% and Diff_EMA200%."
                    ),
                )

            treemap_rows = render_portfolio_table(
                raw_df,
                config["portfolio_pages"][i],
                dark_mode=dark_mode,
                display_currency=display_currency,
                show_name_column=show_name_column,
                show_relative_momentum_columns=show_relative_momentum_columns,
                show_ema_columns=show_ema_columns,
                show_financial_columns=show_financial_columns,
            )
            treemap_fig = build_portfolio_treemap(treemap_rows, dark_mode=dark_mode)
            if treemap_fig is not None:
                st.divider()
                st.plotly_chart(treemap_fig, width="stretch", key=f"portfolio_treemap_{i}")
    return config


def render_kline(cache_key, display_currency="Local"):
    st.subheader("K-Line Chart")
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    with c1:
        ticker = st.text_input("Ticker", "AAPL", key="kline_ticker").upper()
    with c2:
        period = int(st.number_input("Period (days)", min_value=1, max_value=3650, value=365, step=1, key="kline_period"))
    with c3:
        interval = st.selectbox("Interval", ["1d", "1wk", "1h", "4h", "15m", "5m"], index=0, key="kline_interval")
    with c4:
        st.write("")
        st.write("")
        plot = st.button("Plot", width="stretch", key="kline_plot_btn")

    request_key = f"{cache_key}_{ticker}_{period}_{interval}"
    if plot:
        if "kline_data" not in st.session_state or st.session_state.get("kline_cache_key") != request_key:
            with st.spinner(f"Loading {ticker} K-line data..."):
                st.session_state["kline_data"] = fetch_kline_data(ticker, period, interval, cache_key)
                st.session_state["kline_cache_key"] = request_key
                st.session_state["kline_ticker_cache"] = ticker
        st.session_state["current_ticker"] = ticker

    fib_scope = f"{ticker}_{display_currency}"
    if st.session_state.get("fib_ticker") != fib_scope:
        st.session_state.pop("fib_levels", None)
        st.session_state["fib_ticker"] = fib_scope

    data = st.session_state.get("kline_data")
    if not data:
        st.info("Click 'Plot' to load chart")
        return
    if not data.get("success"):
        st.error(data.get("error", "Failed to load K-line data"))
        return

    fib_levels = st.session_state.get("fib_levels")
    with st.expander("Fibonacci Retracement / Extension", expanded=bool(fib_levels)):
        st.markdown(
            """
            Enter A (swing low), B (swing high), and optionally C (pullback end) prices.
            - A + B only: retracement lines
            - A + B + C: extension lines
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
            st.session_state.pop("fib_levels", None)
            fib_levels = None

        if submit_fib and fib_a > 0 and fib_b > 0 and fib_a != fib_b:
            diff = fib_b - fib_a
            if fib_c > 0:
                ratios = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618, 2.0, 2.618]
                labels = ["0%", "23.6%", "38.2%", "50%", "61.8%", "78.6%", "100%", "127.2%", "161.8%", "200%", "261.8%"]
                fib_levels = [(fib_c + diff * ratio, label, "blue" if ratio >= 1.0 else "gray") for ratio, label in zip(ratios, labels)]
            else:
                ratios = [0, 0.236, 0.382, 0.5, 0.618, 1.0]
                labels = ["0%", "23.6%", "38.2%", "50%", "61.8%", "100%"]
                fib_levels = [(fib_b - diff * ratio, label, "gray") for ratio, label in zip(ratios, labels)]
            st.session_state["fib_levels"] = fib_levels

        if fib_levels:
            rows = []
            for level, label, color in fib_levels:
                rows.append({"Ratio": label, "Price": f"{level:.2f}", "Type": "Extension" if color == "blue" else "Retracement"})
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    dark_mode = st.session_state.get("dark_mode", False)
    chart_data = convert_kline_data_for_display(data, ticker, display_currency)
    fig = build_kline_chart(chart_data, ticker, fib_levels=fib_levels, dark_mode=dark_mode)
    if fig:
        st.plotly_chart(fig, width="stretch", key="kline_main_chart")


def render_report_form_fields(key_prefix, default_ticker="AAPL", include_email=False):
    widths = [2, 2, 1, 1] if include_email else [2, 1, 1]
    columns = st.columns(widths)
    with columns[0]:
        ticker = st.text_input("Ticker", default_ticker, key=f"{key_prefix}_ticker").upper()
    offset = 1
    recipient_email = None
    if include_email:
        with columns[1]:
            recipient_email = st.text_input(
                "Recipient email",
                key=f"{key_prefix}_email",
                placeholder="name@example.com",
            )
        offset = 2
    with columns[offset]:
        months = st.number_input(
            "Chart months",
            min_value=1,
            max_value=24,
            value=3,
            step=1,
            key=f"{key_prefix}_months",
        )
    with columns[offset + 1]:
        search_provider = st.selectbox(
            "Search provider",
            ["auto", "priority", "serper", "searxng", "both"],
            index=0,
            key=f"{key_prefix}_search_provider",
        )
    no_article_fetch = st.checkbox(
        "Skip article body fetch",
        value=False,
        help="Faster, but the news notes may rely more on search snippets.",
        key=f"{key_prefix}_no_article_fetch",
    )
    return ticker, recipient_email, int(months), search_provider, no_article_fetch


def render_report_status_table(rows, dark_mode=False):
    if not rows:
        return
    theme = get_theme(dark_mode)
    columns = list(rows[0])
    header_html = "".join(
        f"<th style='padding:8px; text-align:left; white-space:nowrap; "
        f"background:{theme['table_header_bg']}; color:{theme['text']}; "
        f"border:1px solid {theme['table_border']};'>{html.escape(str(column))}</th>"
        for column in columns
    )
    body_rows = []
    for row in rows:
        cells = "".join(
            f"<td style='padding:8px; white-space:nowrap; color:{theme['text']}; "
            f"background:{theme['table_bg']}; border:1px solid {theme['table_border']};'>"
            f"{html.escape(str(row.get(column) if row.get(column) is not None else ''))}</td>"
            for column in columns
        )
        body_rows.append(f"<tr>{cells}</tr>")
    table_html = f"""
    <div style="width:100%; max-height:320px; overflow:auto; border:1px solid {theme['table_border']}; border-radius:6px;">
        <table style="width:100%; border-collapse:collapse; font-family:Arial,sans-serif; font-size:13px; background:{theme['table_bg']}; color:{theme['text']};">
            <thead style="position:sticky; top:0; z-index:1;"><tr>{header_html}</tr></thead>
            <tbody>{''.join(body_rows)}</tbody>
        </table>
    </div>
    """
    st.markdown(table_html, unsafe_allow_html=True)


def render_email_job_status(owner_key):
    jobs = list_owner_jobs(owner_key, limit=10)
    heading_col, refresh_col = st.columns([5, 1])
    with heading_col:
        st.markdown("#### Recent Email Jobs")
    with refresh_col:
        if st.button("Refresh", key="refresh_report_jobs", width="stretch"):
            st.rerun()
    if not jobs:
        st.caption("No email report jobs yet.")
        return

    status_labels = {
        "queued": "Queued",
        "generating": "Generating",
        "sending": "Sending",
        "sent": "Sent",
        "failed": "Failed",
        "expired": "Expired",
    }
    rows = []
    for job in jobs:
        status = status_labels.get(job["status"], job["status"])
        if job["status"] == "queued" and job.get("attempts"):
            status = "Retry queued"
        if job.get("email_sent_at") and job["status"] != "sent":
            status = "Possibly sent"
        rows.append({
            "Ticker": job["ticker"],
            "Type": "Weekly" if job.get("schedule_id") else "One-time",
            "Status": status,
            "Recipient": job["recipient_masked"],
            "Attempts": f"{job['attempts']}/{job['max_attempts']}",
            "Generated (s)": round(job["generation_seconds"], 1) if job.get("generation_seconds") else None,
            "Created (UTC)": job["created_at"],
        })
    render_report_status_table(rows, dark_mode=st.session_state.get("dark_mode", False))

    latest_error = next((job for job in jobs if job.get("last_error")), None)
    if latest_error:
        with st.expander(f"Latest job message ({latest_error['ticker']})", expanded=False):
            st.code(latest_error["last_error"])


def _format_berlin_datetime(value):
    if not value:
        return "Paused"
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d %H:%M %Z")


def render_weekly_report_schedules(user, runner_ok, mail_ready):
    st.caption(
        "Create a recurring weekly report in Europe/Berlin time. Daylight-saving changes are handled automatically. "
        "Select every day for this ticker in one plan; each account can keep up to seven ticker schedules. "
        "The recipient address remains stored until the schedule is deleted."
    )
    with st.form("daily_report_schedule_form"):
        schedule_ticker, schedule_email, schedule_months, schedule_provider, schedule_no_fetch = render_report_form_fields(
            "daily_report_schedule",
            include_email=True,
        )
        day_col, time_col = st.columns([4, 1])
        with day_col:
            st.markdown("**Send on (Europe/Berlin)**")
            weekday_columns = st.columns(7)
            selected_weekdays = []
            for weekday, weekday_name in enumerate(WEEKDAY_NAMES):
                with weekday_columns[weekday]:
                    if st.checkbox(
                        weekday_name,
                        value=weekday == 0,
                        key=f"daily_report_schedule_weekday_{weekday}",
                    ):
                        selected_weekdays.append(weekday)
        with time_col:
            local_time = st.time_input(
                "Send time (Europe/Berlin)",
                value=datetime.time(hour=18, minute=0),
                step=datetime.timedelta(minutes=15),
                key="daily_report_schedule_time",
            )
        schedule_submitted = st.form_submit_button(
            "Create Weekly Plan",
            disabled=not runner_ok or not mail_ready,
        )

    if schedule_submitted:
        try:
            schedule = create_weekly_schedule(
                owner_key=user["cache_key"],
                ticker=normalize_yfinance_ticker(schedule_ticker),
                recipient_email=schedule_email,
                local_time=local_time,
                weekdays=selected_weekdays,
                months=schedule_months,
                search_provider=schedule_provider,
                no_article_fetch=schedule_no_fetch,
            )
            st.success(
                f"Weekly plan for {', '.join(WEEKDAY_NAMES[day] for day in schedule['weekdays'])} created. "
                f"Next send: {_format_berlin_datetime(schedule['next_run_at'])}."
            )
        except (ValueError, ScheduleLimitError) as exc:
            st.error(str(exc))

    schedules = list_owner_schedules(user["cache_key"])
    st.markdown("#### Weekly Schedules")
    if not schedules:
        st.caption("No weekly schedules yet.")
        return

    rows = []
    for schedule in schedules:
        rows.append({
            "Ticker": schedule["ticker"],
            "Recipient": schedule["recipient_masked"],
            "Weekly time": f"{', '.join(WEEKDAY_NAMES[day] for day in schedule['weekdays'])} {schedule['local_time']}",
            "Status": "Active" if schedule["is_active"] else "Paused",
            "Next send (Berlin)": _format_berlin_datetime(schedule["next_run_at"]),
        })
    render_report_status_table(rows, dark_mode=st.session_state.get("dark_mode", False))

    schedule_map = {schedule["id"]: schedule for schedule in schedules}
    selected_id = st.selectbox(
        "Manage schedule",
        list(schedule_map),
        format_func=lambda schedule_id: (
            f"{schedule_map[schedule_id]['ticker']} - "
            f"{', '.join(WEEKDAY_NAMES[day] for day in schedule_map[schedule_id]['weekdays'])} "
            f"{schedule_map[schedule_id]['local_time']} - "
            f"{schedule_map[schedule_id]['recipient_masked']}"
        ),
        key="manage_report_schedule",
    )
    selected = schedule_map[selected_id]
    action_col, delete_col = st.columns(2)
    with action_col:
        action_label = "Pause Schedule" if selected["is_active"] else "Resume Schedule"
        if st.button(action_label, key="toggle_report_schedule", width="stretch"):
            set_schedule_active(
                selected_id,
                owner_key=user["cache_key"],
                active=not bool(selected["is_active"]),
            )
            st.rerun()
    with delete_col:
        confirm_delete = st.checkbox("Confirm delete", key="confirm_delete_report_schedule")
        if st.button(
            "Delete Schedule",
            key="delete_report_schedule",
            width="stretch",
            disabled=not confirm_delete,
        ):
            delete_schedule(selected_id, owner_key=user["cache_key"])
            st.rerun()


def render_daily_report(user):
    st.subheader("AI Agent Stock Daily Report")
    st.caption(
        "Generate a v5.8 AI Agent HTML report for one yfinance ticker. "
        "Download it in this session, or let the background worker email it after you close the page."
    )

    runner_ok = runtime_available()
    if not runner_ok:
        st.warning("The integrated v5.8 daily report module is incomplete or unavailable.")

    download_tab, email_tab = st.tabs(["Generate & Download", "Generate & Email"])
    with download_tab:
        if not user:
            st.info(
                "Sign in with an administrator-issued account to generate AI Agent reports. "
                "Guest access is not available for this resource-intensive feature."
            )
        else:
            st.caption(
                "One active generation and five downloads per account per UTC day are allowed by default. "
                "A server-wide concurrency limit also applies."
            )
            with st.form("daily_report_download_form"):
                ticker, _, months, search_provider, no_article_fetch = render_report_form_fields("daily_report_download")
                submitted = st.form_submit_button("Generate Download", disabled=not runner_ok)

            if submitted:
                cache_key = user["cache_key"]
                session_id = None
                try:
                    check_download_generation_limits(cache_key)
                    session_id = start_download_generation(cache_key, normalize_yfinance_ticker(ticker))
                except (ActiveJobError, DailyLimitError, ValueError) as exc:
                    st.error(str(exc))
                    st.session_state["daily_report_result"] = None
                else:
                    result = None
                    try:
                        with st.spinner(f"Generating {ticker} AI Agent report... This can take a few minutes."):
                            result = generate_report(
                                normalize_yfinance_ticker(ticker),
                                user_scope=cache_key,
                                months=months,
                                search_provider=search_provider,
                                no_article_fetch=no_article_fetch,
                            )
                            st.session_state["daily_report_result"] = result
                    finally:
                        if session_id:
                            finish_download_generation(
                                session_id,
                                success=bool(result and result.get("success")),
                            )

            result = st.session_state.get("daily_report_result")
            if result:
                if result.get("success"):
                    st.success(f"Report generated in {result.get('elapsed', 0):.1f}s")
                    st.download_button(
                        "Download HTML Report",
                        data=result["html_bytes"],
                        file_name=result["file_name"],
                        mime="text/html",
                        width="stretch",
                        key=f"download_daily_report_{result['file_name']}",
                    )
                    with st.expander("Generation log", expanded=False):
                        if result.get("stdout"):
                            st.code(result["stdout"])
                        if result.get("stderr"):
                            st.code(result["stderr"])
                else:
                    st.error(result.get("error", "Daily report generation failed."))
                    with st.expander("Generation log", expanded=True):
                        if result.get("stdout"):
                            st.code(result["stdout"])
                        if result.get("stderr"):
                            st.code(result["stderr"])

    with email_tab:
        if not user:
            st.info("Sign in with an administrator-issued account to submit background email reports.")
            return

        mail_ready = smtp_configured()
        if not mail_ready:
            st.warning(
                "Email delivery is not configured. Set REPORT_SMTP_USER, "
                "REPORT_SMTP_FROM, and REPORT_SMTP_AUTH_CODE in .env."
            )
        one_time_tab, weekly_tab = st.tabs(["Send Once", "Weekly Schedule"])
        with one_time_tab:
            st.caption(
                "The job continues in the server worker after this page is closed. "
                "One active job and three manual submissions per account per UTC day are allowed by default."
            )
            with st.form("daily_report_email_form"):
                email_ticker, recipient_email, email_months, email_provider, email_no_fetch = render_report_form_fields(
                    "daily_report_email",
                    include_email=True,
                )
                email_submitted = st.form_submit_button(
                    "Queue Report Email",
                    disabled=not runner_ok or not mail_ready,
                )

            if email_submitted:
                try:
                    job = enqueue_email_job(
                        owner_key=user["cache_key"],
                        ticker=normalize_yfinance_ticker(email_ticker),
                        recipient_email=recipient_email,
                        months=email_months,
                        search_provider=email_provider,
                        no_article_fetch=email_no_fetch,
                    )
                    st.success(
                        f"Job {job['id'][:8]} queued for {job['recipient_masked']}. "
                        "You may now close this page."
                    )
                except (ValueError, ActiveJobError, DailyLimitError, QueueFullError) as exc:
                    st.error(str(exc))

        with weekly_tab:
            render_weekly_report_schedules(user, runner_ok, mail_ready)

        render_email_job_status(user["cache_key"])


_backend_ok, _backend_msg = ensure_backend()
if not _backend_ok:
    st.warning(
        f"⚠️ 后端服务不可用: {_backend_msg}。"
        "AI 日报功能不受影响，股票数据和 K 线图功能可能受限。"
    )
user = render_auth_panel()
editable = bool(user)
cache_key = user["cache_key"] if user else ""

with st.sidebar:
    dark_mode = st.toggle("Dark mode", value=False, key="dark_mode")
    display_currency = st.selectbox(
        "Display currency",
        ["Local", "EUR"],
        index=0,
        help="EUR mode uses the latest FX rate for display only. Percentage indicators are not recalculated from historical FX.",
    )
    if display_currency == "EUR":
        st.caption("EUR display converts price fields and K-line levels using latest FX. Percentage indicators stay local-currency based.")

    col_r1, col_r2 = st.columns(2)
    with col_r1:
        if st.button("Refresh Stocks", width="stretch", key="btn_refresh_stocks"):
            fetch_stock_data.clear()
            fetch_kline_data.clear()
            currency_to_eur_rate.clear()
            st.session_state.pop("kline_data", None)
            st.session_state.pop("kline_cache_key", None)
            st.rerun()
    with col_r2:
        if st.button("Refresh Breadth", width="stretch", key="btn_refresh_breadth"):
            fetch_breadth_data.clear()
            st.rerun()

    st.divider()
    if editable:
        st.header("Customize Pages")
        st.caption("Add tabs to your stock watchlists, market dashboard, or portfolio monitor.")
        page_kind = st.selectbox(
            "Add page to",
            ["stocks_pages", "broad_pages", "portfolio_pages"],
            format_func=lambda x: SECTION_META[x]["add_label"],
        )
        page_name = st.text_input("New page name", SECTION_META[page_kind]["new_page"])
        if st.button("Add page", width="stretch"):
            config = get_active_config(user)
            config = add_page(config, page_kind, page_name)
            save_active_config(user, config)
            st.rerun()
    st.caption(f"Last update: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

inject_css(dark_mode)
st.title("Stock Watchlist")
st.caption(
    "Stock Watchlists are for custom stock/ETF lists. Market Dashboard is for indices and cross-asset signals. "
    "Portfolios track personal holdings. Market Breadth is calculated from shared S&P 500 and Nasdaq 100 universes."
)

fg1, fg_vix, fg2 = st.columns(3)
with fg1:
    fg = fetch_fear_greed("/api/fear_greed")
    display_fear_greed(fg, "CNN Fear & Greed", dark_mode=dark_mode)
with fg_vix:
    vix_data = fetch_vix_kline()
    display_vix_gauge(vix_data, dark_mode=dark_mode)
with fg2:
    cfg = fetch_fear_greed("/api/fear_greed_crypto")
    display_fear_greed(cfg, "Crypto Fear & Greed", dark_mode=dark_mode)

config = get_active_config(user)
config_json = json.dumps(config, sort_keys=True)
with st.spinner("Loading watch list data..."):
    stock_payload = fetch_stock_data(config_json, cache_key)

_stock_data_ok = stock_payload.get("success", False)
if _stock_data_ok:
    raw_df = pd.DataFrame(stock_payload["data"])
    display_df = convert_stock_df_for_display(raw_df, display_currency)
else:
    display_df = pd.DataFrame()

main_tabs = st.tabs([
    SECTION_META["stocks_pages"]["tab"],
    SECTION_META["broad_pages"]["tab"],
    "Market Breadth",
    SECTION_META["portfolio_pages"]["tab"],
    "AI Agent Reports",
])
with main_tabs[0]:
    if not _stock_data_ok:
        st.error(stock_payload.get("error", "Failed to load stock data"))
    else:
        config = render_section(
            SECTION_META["stocks_pages"]["title"],
            "stocks_pages",
            config,
            display_df,
            editable,
            user,
            dark_mode=dark_mode,
            display_currency=display_currency,
        )

with main_tabs[1]:
    if not _stock_data_ok:
        st.error(stock_payload.get("error", "Failed to load stock data"))
    else:
        config = render_section(
            SECTION_META["broad_pages"]["title"],
            "broad_pages",
            config,
            display_df,
            editable,
            user,
            dark_mode=dark_mode,
            display_currency=display_currency,
        )

with main_tabs[2]:
    st.subheader("Market Breadth")
    st.caption("Automatically calculates the percentage of S&P 500 and Nasdaq 100 constituents above their 20/50/200-day moving averages. Both universes are refreshed together with one de-duplicated ticker download.")
    with st.spinner("Loading market breadth data..."):
        breadth = fetch_breadth_data()
    if breadth and breadth.get("success"):
        counts = breadth.get("breadth_universe_counts", {})
        if counts:
            st.caption(
                f"Download universe: {counts.get('combined_download', 'N/A')} unique tickers "
                f"({counts.get('sp500', 'N/A')} S&P 500, {counts.get('nasdaq100', 'N/A')} Nasdaq 100, "
                f"{counts.get('overlap', 'N/A')} overlap)."
            )
        render_grouped_table(pd.DataFrame(breadth["data"]), BREADTH_GROUPS, dark_mode=dark_mode)
        st.divider()
        fig = build_breadth_chart(breadth, dark_mode=dark_mode)
        ndx_fig = build_breadth_chart(
            breadth,
            dark_mode=dark_mode,
            chart_key="nasdaq100_breadth_chart_data",
            title="Market Breadth (Nasdaq 100)",
            index_key="NDX",
            index_label="^NDX Adj Close",
        )
        chart_col1, chart_col2 = st.columns(2)
        with chart_col1:
            if fig is not None:
                st.plotly_chart(fig, width="stretch")
        with chart_col2:
            if ndx_fig is not None:
                st.plotly_chart(ndx_fig, width="stretch")

        st.divider()
        st.caption("Treemap tile area is based on cached latest market cap; color is the latest regular 1D% move.")
        treemap_fig = build_sp500_treemap(breadth, dark_mode=dark_mode)
        ndx_treemap_fig = build_nasdaq100_treemap(breadth, dark_mode=dark_mode)
        if treemap_fig is not None:
            st.plotly_chart(treemap_fig, width="stretch")
        if ndx_treemap_fig is not None:
            st.plotly_chart(ndx_treemap_fig, width="stretch")
    else:
        st.warning(breadth.get("error", "Failed to load market breadth data") if isinstance(breadth, dict) else "Failed to load market breadth data")

with main_tabs[3]:
    if not _stock_data_ok:
        st.error(stock_payload.get("error", "Failed to load stock data"))
    else:
        config = render_portfolio_section(
            config,
            raw_df,
            editable,
            user,
            dark_mode=dark_mode,
            display_currency=display_currency,
        )

with main_tabs[4]:
    render_daily_report(user)

st.divider()
render_kline(cache_key, display_currency=display_currency)
