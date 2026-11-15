"""Close-only chart for an open-end fund's published unit NAV."""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from kline_indicators import normalize_indicator_settings


def build_nav_chart(data, *, dark_mode=False, uirevision=None, indicator_settings=None):
    dates = pd.to_datetime(data["dates"])
    close = pd.Series(pd.to_numeric(data["ohlc"]["close"], errors="coerce"), index=dates)
    close = close.dropna()
    if close.empty:
        return None
    settings = normalize_indicator_settings(indicator_settings)
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.07,
                        row_heights=[0.6, 0.2, 0.2])
    fig.add_trace(go.Scatter(x=close.index, y=close, name="Unit NAV", mode="lines",
                             line=dict(color="#2563eb", width=2)), row=1, col=1)
    colors = ("#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#06b6d4", "#6b7280")
    for ma, color in zip(settings["moving_averages"], colors):
        window = ma["period"]
        values = (close.ewm(span=window, adjust=False).mean() if ma["type"] == "EMA"
                  else close.rolling(window, min_periods=window).mean())
        fig.add_trace(go.Scatter(x=close.index, y=values, name=f"{ma['type']}{window}",
                                 line=dict(color=color, width=1)), row=1, col=1)
    macd_settings = settings["macd"]
    ema12 = close.ewm(span=macd_settings["fast"], adjust=False).mean()
    ema26 = close.ewm(span=macd_settings["slow"], adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=macd_settings["signal"], adjust=False).mean()
    fig.add_trace(go.Scatter(x=close.index, y=macd, name="MACD"), row=2, col=1)
    fig.add_trace(go.Scatter(x=close.index, y=signal, name="Signal"), row=2, col=1)
    delta = close.diff()
    rsi_period = settings["rsi"]["period"]
    up = delta.clip(lower=0).ewm(alpha=1 / rsi_period, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / rsi_period, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / down.replace(0, float("nan")))
    fig.add_trace(go.Scatter(x=close.index, y=rsi, name=f"RSI({rsi_period})"), row=3, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="gray", row=3, col=1)
    fig.add_hline(y=70, line_dash="dash", line_color="gray", row=3, col=1)
    fig.update_layout(title=f"{data['ticker']} — published unit NAV (not OHLC)", height=780,
                      template="plotly_dark" if dark_mode else "plotly_white",
                      uirevision=uirevision, hovermode="x unified")
    fig.update_yaxes(title_text="Unit NAV", row=1, col=1)
    fig.update_yaxes(title_text="MACD", row=2, col=1)
    fig.update_yaxes(title_text="RSI", row=3, col=1)
    return fig
