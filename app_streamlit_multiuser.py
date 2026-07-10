"""Multi-user Streamlit frontend with per-account editable watch lists."""

import copy
import datetime
import colorsys
import html
import json
import socket
import threading
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import streamlit as st
import yfinance as yf

import stock_watch_list_back_end
from daily_report.service import generate_report, runtime_available
from multiuser_store import (
    BREADTH_GROUPS,
    authenticate,
    broad_market_tickers,
    config_to_api_groups,
    default_watchlist_config,
    get_user_config,
    normalize_config,
    save_user_config,
)
from ticker_mapping import normalize_yfinance_ticker, stockanalysis_overview_url


st.set_page_config(page_title="Stock Watchlist", layout="wide", initial_sidebar_state="expanded")

API_BASE = "http://127.0.0.1:5000"
COLUMNS = (
    ["Ticker", "Name", "Price", "1D%", "5D%", "1M%", "YTD%", "Rel. Momentum"]
    + [f"Diff_EMA{n}%" for n in [5, 10, 20, 50, 100, 200]]
    + ["Diff_BB_Up%", "Diff_BB_Low%", "Volume_Ratio", "Next Earnings", "Trailing PE", "Forward PE",
       "PEG Ratio", "Analysts", "Price Target", "Market Cap"]
)
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

_flask_started = False


def is_port_open(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_flask():
    global _flask_started
    if _flask_started:
        return
    if is_port_open("127.0.0.1", 5000):
        _flask_started = True
        return
    t = threading.Thread(
        target=lambda: stock_watch_list_back_end.app.run(
            host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True
        ),
        daemon=True,
    )
    t.start()
    _flask_started = True
    time.sleep(2)


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
        div[data-baseweb="popover"] {{
            background-color: {theme["panel_bg"]} !important;
            color: {theme["text"]} !important;
            border-color: {theme["table_border"]} !important;
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
        div[data-testid="stDataEditor"] {{
            --gdg-bg-cell: {theme["input_bg"]};
            --gdg-bg-cell-medium: {theme["panel_bg"]};
            --gdg-bg-header: {theme["table_header_bg"]};
            --gdg-bg-header-hovered: {theme["button_hover"]};
            --gdg-bg-header-has-focus: {theme["button_hover"]};
            --gdg-text-dark: {theme["text"]};
            --gdg-text-medium: {theme["text"]};
            --gdg-text-light: {theme["muted"]};
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
        div[data-testid="stDataEditor"] input,
        div[data-testid="stDataEditor"] textarea,
        div[data-testid="stDataEditor"] [contenteditable="true"] {{
            background-color: {theme["input_bg"]} !important;
            color: {theme["text"]} !important;
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


def page_tickers(page):
    tickers = []
    for group_tickers in page.get("groups", {}).values():
        tickers.extend(group_tickers)
    return list(dict.fromkeys(tickers))


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
                elif col == "Rel. Momentum":
                    disp = f"{float(val):.2f}" if pd.notna(val) else ""
                elif col == "Price":
                    disp = format_money_value(val, ticker, display_currency)
                elif col == "Volume_Ratio":
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
                    elif col == "Rel. Momentum":
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


def render_grouped_table(df, groups, dark_mode=False, display_currency="Local", show_name_column=False):
    if df.empty:
        st.info("No data available")
        return

    df_display = build_grouped_df(df, groups, display_currency=display_currency)
    if df_display.empty:
        st.info("No data available")
        return

    visible_columns = COLUMNS if show_name_column else [col for col in COLUMNS if col != "Name"]
    theme = get_theme(dark_mode)
    html_table = f"""
    <div style="width:100%; max-height:600px; overflow:auto; border:1px solid {theme['table_border']};">
        <table style="width:100%; border-collapse:collapse; font-family:Arial; font-size:12px; background-color:{theme['table_bg']}; color:{theme['text']};">
            <thead style="position:sticky; top:0; z-index:10; background-color:{theme['table_header_bg']};">
                <tr style="background-color:{theme['table_header_bg']};">
    """
    for col in visible_columns:
        html_table += (
            f"<th style='padding:4px; text-align:left; color:{theme['text']}; "
            f"border:1px solid {theme['table_border']};'>{html.escape(col)}</th>"
        )
    html_table += "</tr></thead><tbody>"

    cell_colors = apply_cell_colors(df_display, df, groups, columns=visible_columns)
    group_names = set(groups.keys())
    for row_index in range(len(df_display)):
        row = df_display.iloc[row_index]
        is_header = str(row["Ticker"]) in group_names
        html_table += "<tr>"
        for col_index, col in enumerate(visible_columns):
            val = "" if pd.isna(row[col]) else str(row[col])
            bg_color = cell_colors.get((row_index, col_index), theme["table_group_bg"] if is_header else theme["table_bg"])
            if dark_mode and bg_color.lower() in ("#ffffff", "white", "#cccccc"):
                bg_color = theme["table_group_bg"] if is_header else theme["table_bg"]
            text_color = theme["text"] if bg_color == theme["table_bg"] else readable_text_color(bg_color)

            if is_header and col_index == 0:
                html_table += (
                    f"<td colspan='{len(COLUMNS)}' style='padding:4px; color:{theme['text']}; "
                    f"background-color:{bg_color}; font-weight:bold; border:1px solid {theme['table_border']};'>"
                    f"{html.escape(val)}</td>"
                )
                break
            if is_header:
                continue

            align = "right" if col in RIGHT_ALIGNED_COLUMNS or (val and val[0] in "+-$0123456789") else "left"
            html_table += (
                f"<td style='padding:4px; text-align:{align}; color:{text_color}; "
                f"background-color:{bg_color}; border:1px solid {theme['table_border']};'>"
                f"{html.escape(val)}</td>"
            )
        html_table += "</tr>"

    html_table += """
            </tbody>
        </table>
    </div>
    """
    st.markdown(html_table, unsafe_allow_html=True)


def render_market_table(raw_df, page, dark_mode=False, display_currency="Local", show_name_column=False):
    groups = page.get("groups", {})
    tickers = page_tickers(page)
    page_df = raw_df[raw_df["Ticker"].isin(tickers)].copy() if not raw_df.empty else pd.DataFrame()
    render_grouped_table(
        page_df,
        groups,
        dark_mode=dark_mode,
        display_currency=display_currency,
        show_name_column=show_name_column,
    )


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
    except Exception:
        pass
    try:
        currency = yf.Ticker(ticker).info.get("currency")
        if currency:
            return normalize_currency_code(currency)
    except Exception:
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
    except Exception:
        pass

    eur_quote = f"{currency}EUR=X"
    try:
        eur_per_quote = _latest_yahoo_price(eur_quote)
        if eur_per_quote and eur_per_quote > 0:
            return eur_per_quote
    except Exception:
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


def add_page(config, section, name):
    config[section].append({"name": name.strip() or "New Page", "groups": {}})
    return normalize_config(config)


def delete_page(config, section, page_index):
    if len(config[section]) > 1:
        config[section].pop(page_index)
    return normalize_config(config)


@st.cache_data(ttl=300, show_spinner=False)
def fetch_stock_data(config_json, cache_key):
    config = normalize_config(json.loads(config_json))
    params = {
        "groups": json.dumps(config_to_api_groups(config)),
        "broad_market_tickers": json.dumps(broad_market_tickers(config)),
    }
    if cache_key:
        params["cache_key"] = cache_key
    resp = requests.get(f"{API_BASE}/api/stock_data", params=params, timeout=180)
    if resp.status_code != 200:
        return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text}"}
    return resp.json()


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
    except Exception as e:
        return {"success": False, "error": str(e)}


@st.cache_data(ttl=600, show_spinner=False)
def fetch_fear_greed(path):
    try:
        resp = requests.get(f"{API_BASE}{path}", timeout=10)
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None


@st.cache_data(ttl=60, show_spinner=False)
def fetch_kline_data(ticker, period, interval, cache_key=""):
    params = {"ticker": ticker, "period": period, "interval": interval}
    if cache_key:
        params["cache_key"] = cache_key
    resp = requests.get(f"{API_BASE}/api/kline_data", params=params, timeout=120)
    return resp.json() if resp.status_code == 200 else None


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
    rows["1D%"] = pd.to_numeric(rows["1D%"], errors="coerce").fillna(0)
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
        sector_color = float(np.average(sector_df["1D%"], weights=sector_df["Size"]))
        labels.append(sector)
        ids.append(sector_id)
        parents.append("root")
        values.append(sector_value)
        colors.append(sector_color)
        text.append(f"{sector_color:+.2f}%")
        customdata.append([sector, "", "", "", "", f"{sector_color:+.2f}%"])

        for _, row in sector_df.sort_values("Size", ascending=False).iterrows():
            ticker = str(row["Ticker"])
            pct = float(row["1D%"])
            price = row["Price"]
            stock_size = float(row["Size"])
            labels.append(ticker)
            ids.append(f"ticker:{ticker}")
            parents.append(sector_id)
            values.append(stock_size)
            colors.append(pct)
            text.append(f"{pct:+.2f}%")
            customdata.append([
                row.get("Name", ticker),
                sector,
                row.get("Industry", "Unknown"),
                f"{float(price):.2f}" if pd.notna(price) else "",
                _format_market_cap(row.get("Market Cap")),
                f"{pct:+.2f}%",
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
            user = authenticate(username, password)
            if user:
                st.session_state["user"] = user
                st.session_state["watchlist_config"] = get_user_config(user["id"])
                st.rerun()
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
            show_name_column = st.toggle(
                "Show Name column next to Ticker",
                value=False,
                key=f"{section_key}_{i}_show_name_column",
                help="Turn this on to insert the cached yfinance display-name column between Ticker and Price.",
            )
            render_market_table(
                raw_df,
                config[section_key][i],
                dark_mode=dark_mode,
                display_currency=display_currency,
                show_name_column=show_name_column,
            )
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


def render_daily_report(cache_key):
    st.subheader("Daily Stock Report")
    st.caption(
        "Generate a v5.8 HTML daily report for one yfinance ticker. "
        "Generated files are held in memory for download; server-side temporary files are removed automatically."
    )

    runner_ok = runtime_available()
    if not runner_ok:
        st.warning("The integrated v5.8 daily report module is incomplete or unavailable.")

    with st.form("daily_report_form"):
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            ticker = st.text_input("Ticker", "AAPL", key="daily_report_ticker").upper()
        with c2:
            months = st.number_input("Chart months", min_value=1, max_value=24, value=3, step=1, key="daily_report_months")
        with c3:
            search_provider = st.selectbox(
                "Search provider",
                ["auto", "priority", "serper", "searxng", "both"],
                index=0,
                key="daily_report_search_provider",
            )

        no_article_fetch = st.checkbox(
            "Skip article body fetch",
            value=False,
            help="Faster, but the news notes may rely more on search snippets.",
            key="daily_report_no_article_fetch",
        )
        submitted = st.form_submit_button("Generate Daily Report", disabled=not runner_ok)

    if submitted:
        with st.spinner(f"Generating {ticker} daily report... This can take a few minutes."):
            st.session_state["daily_report_result"] = generate_report(
                normalize_yfinance_ticker(ticker),
                user_scope=cache_key or "guest",
                months=int(months),
                search_provider=search_provider,
                no_article_fetch=no_article_fetch,
            )

    result = st.session_state.get("daily_report_result")
    if not result:
        return

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
        return

    st.error(result.get("error", "Daily report generation failed."))
    with st.expander("Generation log", expanded=True):
        if result.get("stdout"):
            st.code(result["stdout"])
        if result.get("stderr"):
            st.code(result["stderr"])
        if result.get("hint"):
            st.info(result["hint"])


ensure_flask()
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
        st.caption("Add tabs to either your stock watchlists or your broader market dashboard.")
        page_kind = st.selectbox(
            "Add page to",
            ["stocks_pages", "broad_pages"],
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
    "Market Breadth is calculated from shared S&P 500 and Nasdaq 100 universes and is not edited manually."
)

fg1, fg2 = st.columns(2)
with fg1:
    fg = fetch_fear_greed("/api/fear_greed")
    display_fear_greed(fg, "CNN Fear & Greed", dark_mode=dark_mode)
with fg2:
    cfg = fetch_fear_greed("/api/fear_greed_crypto")
    display_fear_greed(cfg, "Crypto Fear & Greed", dark_mode=dark_mode)

config = get_active_config(user)
config_json = json.dumps(config, sort_keys=True)
with st.spinner("Loading watch list data..."):
    stock_payload = fetch_stock_data(config_json, cache_key)

if not stock_payload.get("success"):
    st.error(stock_payload.get("error", "Failed to load stock data"))
    st.stop()

raw_df = pd.DataFrame(stock_payload["data"])
display_df = convert_stock_df_for_display(raw_df, display_currency)

main_tabs = st.tabs([SECTION_META["stocks_pages"]["tab"], SECTION_META["broad_pages"]["tab"], "Market Breadth", "Daily Report"])
with main_tabs[0]:
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
    render_daily_report(cache_key)

st.divider()
render_kline(cache_key, display_currency=display_currency)
