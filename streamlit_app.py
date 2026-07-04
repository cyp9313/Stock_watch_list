"""
US Stock Watchlist — Streamlit Version
Usage: streamlit run streamlit_app.py
"""

import streamlit as st
import pandas as pd
import numpy as np
import json
import requests
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import datetime
import time
import threading
import colorsys

# ── Page config ──────────────────────────────────────────────
st.set_page_config(
    page_title="US Stock Watchlist",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Start Flask backend in daemon thread ─────────────────────
import stock_watch_list_back_end

_flask_started = False


def ensure_flask():
    global _flask_started
    if not _flask_started:
        t = threading.Thread(
            target=lambda: stock_watch_list_back_end.app.run(
                host="127.0.0.1", port=5000, debug=False, use_reloader=False
            ),
            daemon=True,
        )
        t.start()
        _flask_started = True
        time.sleep(2)  # wait for Flask to boot


API_BASE = "http://127.0.0.1:5000"

# ── Group definitions ────────────────────────────────────────
GROUPS = {
    "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA"],
    "Chips/AI": [
        "ORCL", "AMD", "INTC", "AVGO", "SMCI", "PLTR", "RGTI", "DXYZ", "SNPS", "APP",
    ],
    "Fin/Crypto": ["V", "JPM", "BRK-B", "COIN", "HOOD", "MSTR", "CRCL", "SOFI", "OSCR"],
    "Health": ["LLY", "NVO", "ABBV", "UNH"],
    "Energy": ["SMR", "VST", "OKLO", "NEE", "ENPH", "GE", "GEV"],
    "Defense": ["LMT", "BA", "ACHR", "AXON"],
    "Consumer": ["LULU", "NKE", "CMG", "COST"],
    "China": ["BYDDY", "XIACY", "PDD", "BABA", "TCEHY", "BIDU"],
    "Themes": ["ASTS", "CRWV", "NBIS", "MP", "RKLB"],
    "Broad Market": [
        "^GSPC", "^NDX", "^DJI", "^RUT", "510300.SS",
        "RSP", "QQQE", "TQQQ", "WNUC.DE", "REMX", "^TNX",
        "EURUSD=X", "GC=F", "SI=F", "BZ=F",
        "BTC-USD", "ETH-USD", "^VIX", "^VXN",
    ],
    "Market Breadth": ["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"],
}

# ── Color helpers ────────────────────────────────────────────
def red_green_bg(val, clip=10.0):
    """Return CSS background: green for positive, red for negative."""
    try:
        v = float(val)
    except (ValueError, TypeError):
        return ""
    if pd.isna(v):
        return ""
    ratio = min(abs(v) / clip, 1.0)
    if v >= 0:
        r, g, b = int(255 * (1 - ratio)), 255, int(255 * (1 - ratio))
    else:
        r, g, b = 255, int(255 * (1 - ratio)), int(255 * (1 - ratio))
    return f"background-color: rgb({r},{g},{b})"


def blue_bg(val, clip=3.0):
    """Return CSS background: blue gradient, darker = larger value."""
    try:
        v = float(val)
    except (ValueError, TypeError):
        return ""
    if pd.isna(v) or v == 0:
        return ""
    ratio = min(abs(v) / clip, 1.0)
    intensity = int(255 * (1 - ratio * 0.7))
    return f"background-color: rgb({intensity},{intensity},255)"


def earnings_bg(val):
    """Color based on days until earnings: red (soon) -> green (far)."""
    try:
        if isinstance(val, str) and val not in ("N/A", ""):
            d = datetime.datetime.strptime(val, "%Y-%m-%d")
            days = (d - datetime.datetime.now()).days
            hue = min(days / 60.0, 1.0) * 0.33  # 0=red, 0.33=green
            rgb = colorsys.hsv_to_rgb(hue, 0.6, 0.95)
            r, g, b = int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255)
            return f"background-color: rgb({r},{g},{b})"
    except Exception:
        pass
    return ""


# ── API helpers with caching ─────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def fetch_stock_data(selected_groups):
    """Fetch stock data for selected groups."""
    filtered = {g: GROUPS[g] for g in selected_groups if g in GROUPS}
    resp = requests.get(f"{API_BASE}/api/stock_data", params={"groups": json.dumps(filtered)}, timeout=90)
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
    except Exception:
        pass
    return None


@st.cache_data(ttl=600, show_spinner=False)
def fetch_crypto_fear_greed():
    try:
        resp = requests.get(f"{API_BASE}/api/fear_greed_crypto", timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


@st.cache_data(ttl=600, show_spinner=False)
def fetch_breadth_data(sp500_list):
    payload = {"sp500_symbols": sp500_list}
    resp = requests.post(f"{API_BASE}/api/breadth_data", json=payload, timeout=60)
    if resp.status_code != 200:
        return None
    return resp.json()


@st.cache_data(ttl=60, show_spinner=False)
def fetch_kline_data(ticker, period, interval):
    resp = requests.get(
        f"{API_BASE}/api/kline_data",
        params={"ticker": ticker, "period": period, "interval": interval},
        timeout=60,
    )
    if resp.status_code != 200:
        return None
    return resp.json()


@st.cache_data(ttl=3600, show_spinner=False)
def get_sp500_list():
    return stock_watch_list_back_end.get_sp500_symbols()


# ── Helper: build styled dataframe ───────────────────────────
def build_styled_df(df):
    """Apply per-column background gradients to the dataframe."""
    if df.empty:
        return df

    percent_cols = [
        "1D%", "5D%", "1M%", "YTD%", "Rel. Momentum",
        "Diff_EMA5%", "Diff_EMA10%", "Diff_EMA20%",
        "Diff_EMA50%", "Diff_EMA100%", "Diff_EMA200%",
        "Diff_BB_Up%", "Diff_BB_Low%",
    ]

    styled = df.style

    for col in percent_cols:
        if col in df.columns:
            clip = 50 if col == "Rel. Momentum" else 10
            styled = styled.map(lambda v: red_green_bg(v, clip), subset=[col])

    for col in ["Volume_Ratio", "Trailing PE", "Forward PE", "Market Cap"]:
        if col in df.columns:
            clip = {"Volume_Ratio": 3, "Trailing PE": 50, "Forward PE": 50, "Market Cap": 1e12}.get(col, 3)
            styled = styled.map(lambda v, c=clip: blue_bg(v, c), subset=[col])

    if "PEG Ratio" in df.columns:
        styled = styled.map(lambda v: blue_bg(v, 5), subset=["PEG Ratio"])

    if "Next Earnings" in df.columns:
        styled = styled.map(lambda v: earnings_bg(v), subset=["Next Earnings"])

    styled = styled.format(
        {
            "Price": "{:.2f}",
            "1D%": "{:+.2f}%",
            "5D%": "{:+.2f}%",
            "1M%": "{:+.2f}%",
            "YTD%": "{:+.2f}%",
            "Rel. Momentum": "{:+.2f}%",
            "Diff_EMA5%": "{:+.2f}%",
            "Diff_EMA10%": "{:+.2f}%",
            "Diff_EMA20%": "{:+.2f}%",
            "Diff_EMA50%": "{:+.2f}%",
            "Diff_EMA100%": "{:+.2f}%",
            "Diff_EMA200%": "{:+.2f}%",
            "Diff_BB_Up%": "{:+.2f}%",
            "Diff_BB_Low%": "{:+.2f}%",
            "Volume_Ratio": "{:.2f}",
            "Trailing PE": "{:.1f}",
            "Forward PE": "{:.1f}",
            "PEG Ratio": "{:.2f}",
            "Market Cap": "{:,.0f}",
        },
        na_rep="N/A",
    )
    return styled


# ── K-line chart builder ─────────────────────────────────────
def build_kline_chart(kline_data, ticker):
    """Build a Plotly candlestick chart with all indicators."""
    if not kline_data or not kline_data.get("success"):
        st.warning("K-line data not available")
        return None

    ohlc = kline_data["ohlc"]
    ind = kline_data["indicators"]
    fin = kline_data.get("financials", {})
    dates_raw = kline_data["dates"]

    # Parse dates
    date_fmt = "%Y-%m-%d %H:%M" if ":" in str(dates_raw[0]) else "%Y-%m-%d"
    dates = [datetime.datetime.strptime(d[:19] if " " in d else d[:10], date_fmt) for d in dates_raw]

    n = len(dates)

    # Build title string
    title_parts = [f"<b>{ticker}</b>"]
    if fin.get("market_cap"):
        title_parts.append(f"MCap: {fin['market_cap']}")
    if fin.get("trailing_pe"):
        title_parts.append(f"T/PE: {fin['trailing_pe']:.1f}")
    if fin.get("forward_pe"):
        title_parts.append(f"F/PE: {fin['forward_pe']:.1f}")
    if fin.get("peg_ratio"):
        title_parts.append(f"PEG: {fin['peg_ratio']:.2f}")
    title = " | ".join(title_parts)

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
        row_heights=[0.45, 0.12, 0.12, 0.12, 0.12, 0.07],
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

    # TD Sequential annotations on main chart
    for i in range(n):
        if td_sell[i] > 0 and td_sell[i] <= 9:
            fig.add_annotation(
                x=dates[i], y=ohlc["high"][i] * 1.003, text=str(td_sell[i]),
                showarrow=False, font=dict(color="red", size=8,
                                           family="Arial Black" if td_sell[i] == 9 else "Arial"),
                row=1, col=1,
            )
        if td_buy[i] > 0 and td_buy[i] <= 9:
            fig.add_annotation(
                x=dates[i], y=ohlc["low"][i] * 0.997, text=str(td_buy[i]),
                showarrow=False, font=dict(color="green", size=8,
                                           family="Arial Black" if td_buy[i] == 9 else "Arial"),
                row=1, col=1,
            )

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
        fig.update_xaxes(title_text="Volume", row=1, col=2)
        fig.update_yaxes(title_text="Price", row=1, col=2, side="right")

    # ── Row 2: Volume ─────────────────────────────────────────
    vol_colors = ["#26a69a" if ohlc["close"][i] >= ohlc["open"][i] else "#ef5350" for i in range(n)]
    fig.add_trace(go.Bar(x=dates, y=ohlc["volume"], name="Volume", marker_color=vol_colors,
                          showlegend=False), row=2, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)

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
    fig.update_yaxes(title_text="MACD", row=3, col=1)

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
    fig.update_yaxes(title_text="KDJ", row=4, col=1)

    # ── Row 5: RSI ────────────────────────────────────────────
    if ind.get("rsi"):
        fig.add_trace(go.Scatter(x=dates, y=ind["rsi"], name="RSI", line=dict(color="purple", width=1)),
                       row=5, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="gray", row=5, col=1)
    fig.add_hline(y=70, line_dash="dash", line_color="gray", row=5, col=1)
    fig.update_yaxes(title_text="RSI", row=5, col=1)

    # ── Row 6: TD Sequential ──────────────────────────────────
    fig.add_trace(
        go.Bar(x=dates, y=td_sell, name="TD Sell", marker_color="red", showlegend=False), row=6, col=1)
    fig.add_trace(
        go.Bar(x=dates, y=[-v for v in td_buy], name="TD Buy", marker_color="green", showlegend=False),
        row=6, col=1)
    fig.update_yaxes(title_text="TD Seq", row=6, col=1, range=[-10, 10])

    # ── Layout ────────────────────────────────────────────────
    fig.update_layout(
        title=title,
        height=1100,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        template="plotly_white",
        margin=dict(l=40, r=40, t=60, b=30),
    )

    # Hide x-axis labels on all but the bottom subplot
    for r in range(1, 6):
        fig.update_xaxes(showticklabels=False, row=r, col=1)

    return fig


# ── Market breadth chart ─────────────────────────────────────
def build_breadth_chart(breadth_data):
    if not breadth_data or not breadth_data.get("breadth_chart_data"):
        return None
    cd = breadth_data["breadth_chart_data"]
    idx = cd["index"]

    fig = go.Figure()
    for key, color in [("20MA_Ratio", "red"), ("50MA_Ratio", "orange"), ("200MA_Ratio", "blue")]:
        if key in cd:
            fig.add_trace(go.Scatter(x=idx, y=cd[key], name=key, line=dict(color=color, width=1.5)))

    fig.add_hline(y=15, line_dash="dash", line_color="gray")
    fig.add_hline(y=85, line_dash="dash", line_color="gray")

    fig.update_layout(
        title="Market Breadth (S&P 500)",
        height=400,
        template="plotly_white",
        hovermode="x unified",
        yaxis=dict(range=[0, 100], title="% Above MA"),
    )
    return fig


# ── Fear & Greed display ─────────────────────────────────────
def display_fear_greed(fg_data, title, prefix=""):
    if not fg_data or not fg_data.get("success"):
        st.metric(title, "N/A")
        return
    val = fg_data.get("value", 50)
    desc = fg_data.get("description", "")
    if val <= 25:
        color = "#1565C0"  # deep blue
    elif val <= 45:
        color = "#00BCD4"  # cyan
    elif val <= 55:
        color = "#4CAF50"  # green
    elif val <= 75:
        color = "#FF9800"  # orange
    else:
        color = "#F44336"  # red
    st.markdown(
        f"<span style='font-size:1.5em;font-weight:bold;color:{color}'>{prefix}{val:.0f}</span>"
        f"&nbsp;&nbsp;<span style='color:gray'>{desc}</span>",
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════
# MAIN APP
# ══════════════════════════════════════════════════════════════

ensure_flask()
st.title("📈 US Stock Watchlist")

# ── Sidebar ──────────────────────────────────────────────────
with st.sidebar:
    st.header("Groups")
    group_names = list(GROUPS.keys())
    selected_groups = []
    for g in group_names:
        if st.checkbox(g, value=(g != "Market Breadth"), key=f"grp_{g}"):
            selected_groups.append(g)

    st.divider()
    col_r1, col_r2 = st.columns(2)
    with col_r1:
        if st.button("🔄 Refresh Stocks", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
    with col_r2:
        if st.button("📊 Refresh Breadth", use_container_width=True):
            fetch_breadth_data.clear()
            st.rerun()

    if st.button("🧹 Clear All Cache", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.caption(f"Last action: {datetime.datetime.now().strftime('%H:%M:%S')}")

# ── Fear & Greed Row ─────────────────────────────────────────
fg = fetch_fear_greed()
cfg = fetch_crypto_fear_greed()
col_fg1, col_fg2 = st.columns(2)
with col_fg1:
    st.caption("CNN Fear & Greed Index")
    display_fear_greed(fg, "CNN")
with col_fg2:
    st.caption("Crypto Fear & Greed Index")
    display_fear_greed(cfg, "Crypto")

st.divider()

# ── Main Data Table ──────────────────────────────────────────
if selected_groups:
    with st.spinner("Loading stock data..."):
        df = fetch_stock_data(selected_groups)

    if not df.empty:
        # Show "Market Breadth" group rows first (if selected)
        breadth_rows = df[df["Ticker"].isin(GROUPS.get("Market Breadth", []))]
        stock_rows = df[~df["Ticker"].isin(GROUPS.get("Market Breadth", []))]

        if not stock_rows.empty:
            st.subheader("Watchlist")
            styled = build_styled_df(stock_rows)
            st.dataframe(
                styled,
                use_container_width=True,
                hide_index=True,
                height=min(35 * len(stock_rows) + 38, 800),
            )

        if not breadth_rows.empty:
            st.subheader("Market Breadth")
            st.dataframe(
                breadth_rows,
                use_container_width=True,
                hide_index=True,
            )

# ── K-line Chart Section ─────────────────────────────────────
st.divider()
st.subheader("📊 K-Line Chart")

col_kl1, col_kl2, col_kl3, col_kl4 = st.columns([2, 1, 1, 1])
with col_kl1:
    ticker = st.text_input("Ticker", "SPY", key="kline_ticker")
with col_kl2:
    period = st.selectbox("Period", [30, 60, 90, 180, 365, 730, 1095], index=3, key="kline_period")
with col_kl3:
    interval = st.selectbox("Interval", ["1d", "1wk", "1h", "4h", "15m", "5m"], index=0, key="kline_interval")
with col_kl4:
    st.write("")
    st.write("")
    plot_btn = st.button("🔍 Plot", use_container_width=True, key="kline_plot_btn")

if plot_btn or ("kline_data" in st.session_state and st.session_state.get("kline_ticker_cache") == ticker):
    cache_key = f"{ticker}_{period}_{interval}"
    if "kline_data" not in st.session_state or st.session_state.get("kline_cache_key") != cache_key:
        with st.spinner(f"Loading {ticker} K-line data..."):
            kd = fetch_kline_data(ticker, period, interval)
            st.session_state["kline_data"] = kd
            st.session_state["kline_cache_key"] = cache_key
            st.session_state["kline_ticker_cache"] = ticker

    kd = st.session_state.get("kline_data")
    if kd and kd.get("success"):
        fig = build_kline_chart(kd, ticker)
        if fig:
            st.plotly_chart(fig, use_container_width=True, key="kline_main_chart")

            # ── Fibonacci section ─────────────────────────────
            with st.expander("📐 Fibonacci Retracement / Extension"):
                st.markdown(
                    """
                    Enter A (swing low), B (swing high), and optionally C (pullback end) prices.
                    - **A + B only** → Retracement (gray lines)
                    - **A + B + C** → Extension (0% at C, >100% in blue)
                    """
                )
                fc1, fc2, fc3 = st.columns(3)
                with fc1:
                    fib_a = st.number_input("A (Swing Low)", value=0.0, step=0.01, format="%.2f")
                with fc2:
                    fib_b = st.number_input("B (Swing High)", value=0.0, step=0.01, format="%.2f")
                with fc3:
                    fib_c = st.number_input("C (Pullback End, optional)", value=0.0, step=0.01, format="%.2f")

                if fib_a > 0 and fib_b > 0 and fib_a != fib_b:
                    diff = fib_b - fib_a

                    if fib_c > 0:
                        # Extension mode
                        ratios = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618, 2.0, 2.618]
                        labels = ['0%', '23.6%', '38.2%', '50%', '61.8%', '78.6%', '100%',
                                  '127.2%', '161.8%', '200%', '261.8%']
                        rows_data = []
                        for r, lbl in zip(ratios, labels):
                            level = fib_c + diff * r
                            is_ext = "🔵 Extension" if r >= 1.0 else "⚫ Retracement"
                            rows_data.append({"Ratio": lbl, "Price": f"{level:.2f}", "Type": is_ext})
                        st.dataframe(pd.DataFrame(rows_data), use_container_width=True, hide_index=True)

                        # Add to chart
                        if fig:
                            for r, lbl in zip(ratios, labels):
                                level = fib_c + diff * r
                                color = "blue" if r >= 1.0 else "gray"
                                fig.add_hline(y=level, line_dash="dash", line_color=color,
                                              annotation_text=f"{lbl}  {level:.2f}",
                                              annotation_position="right",
                                              annotation_font=dict(size=9, color=color))
                            st.plotly_chart(fig, use_container_width=True, key="kline_fib_ext")
                    else:
                        # Retracement mode
                        ratios = [0, 0.236, 0.382, 0.5, 0.618, 1.0]
                        labels = ['0%', '23.6%', '38.2%', '50%', '61.8%', '100%']
                        rows_data = []
                        for r, lbl in zip(ratios, labels):
                            level = fib_b - diff * r
                            rows_data.append({"Ratio": lbl, "Price": f"{level:.2f}"})
                        st.dataframe(pd.DataFrame(rows_data), use_container_width=True, hide_index=True)

                        if fig:
                            for r, lbl in zip(ratios, labels):
                                level = fib_b - diff * r
                                fig.add_hline(y=level, line_dash="dash", line_color="gray",
                                              annotation_text=f"{lbl}  {level:.2f}",
                                              annotation_position="right",
                                              annotation_font=dict(size=9, color="gray"))
                            st.plotly_chart(fig, use_container_width=True, key="kline_fib_ret")
    else:
        if kd:
            st.error(kd.get("error", "Failed to load K-line data"))
        else:
            st.info("Click 'Plot' to load chart")

# ── Market Breadth Section ──────────────────────────────────
if "Market Breadth" in selected_groups:
    st.divider()
    st.subheader("📊 Market Breadth Chart")

    with st.spinner("Loading market breadth data..."):
        sp500_list = get_sp500_list()
        breadth_data = fetch_breadth_data(sp500_list)

    if breadth_data and breadth_data.get("success"):
        fig = build_breadth_chart(breadth_data)
        if fig:
            st.plotly_chart(fig, use_container_width=True, key="breadth_chart")
    else:
        st.warning("Market breadth data not available. Try refreshing.")

# ── Footer ───────────────────────────────────────────────────
st.divider()
st.caption("Streamlit version of US Stock Watchlist — data from Yahoo Finance + Alpha Vantage")

