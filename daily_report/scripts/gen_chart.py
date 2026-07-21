#!/usr/bin/env python3
"""
gen_chart.py — 通用 Plotly K线图生成脚本
生成包含 K线+均线+布林带+成交量+MACD+RSI+KDJ 的多子图交互式图表

用法:
    python gen_chart.py <TICKER> [OUTPUT_HTML] [--months 3]

示例:
    python gen_chart.py ORCL orcl_chart_full.html
    python gen_chart.py BTC-USD btc_chart_full.html --months 6
    python gen_chart.py QQQ qqq_chart_full.html

输出: 可嵌入 HTML 的图表片段（div + script，无 html/head/body 标签）
"""

import sys
import os
import re
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Shared market data service — provides OHLCV snapshot sharing and unified provider layer.
from market_data_service import MarketDataService

# ── 参数 ──────────────────────────────────────────────────────────
TICKER = sys.argv[1].upper() if len(sys.argv) > 1 else 'ORCL'
OUT_FILE = sys.argv[2] if len(sys.argv) > 2 else f"{TICKER.lower().replace('-','_')}_chart_full.html"
MONTHS = 3
for i, arg in enumerate(sys.argv):
    if arg == '--months' and i+1 < len(sys.argv):
        MONTHS = int(sys.argv[i+1])

# ── 数据获取 ───────────────────────────────────────────────────────
# Try to reuse the OHLCV snapshot saved by fetch_and_calc.py (same run_dir).
# This ensures the chart uses the same data as the technical indicators,
# and avoids a redundant yf.download call.
snapshot = MarketDataService.load_ohlcv_snapshot(TICKER, run_dir=os.getcwd())
if snapshot is not None:
    print(f"[INFO] 使用 fetch_and_calc.py 保存的 {TICKER} 数据快照...")
    data = snapshot
else:
    print(f"[INFO] 获取 {TICKER} 近1年数据...")
    data = MarketDataService.fetch_ohlcv(TICKER, period='1y', interval='1d', auto_adjust=False)
if isinstance(data.columns, pd.MultiIndex):
    data.columns = [c[0] if isinstance(c, tuple) else c for c in data.columns]
data = data.dropna(subset=['Close'])

# ── 技术指标 ──────────────────────────────────────────────────────
for p in [5, 10, 20, 50, 120, 200]:
    data[f'MA{p}'] = data['Close'].rolling(p).mean()

bb_mid = data['Close'].rolling(20).mean()
bb_std = data['Close'].rolling(20).std()
data['BB_UP']  = bb_mid + 2 * bb_std
data['BB_DN']  = bb_mid - 2 * bb_std
data['BB_MID'] = bb_mid

ema12 = data['Close'].ewm(span=12, adjust=False).mean()
ema26 = data['Close'].ewm(span=26, adjust=False).mean()
data['MACD']   = ema12 - ema26
data['SIGNAL'] = data['MACD'].ewm(span=9, adjust=False).mean()
data['HIST']   = data['MACD'] - data['SIGNAL']

delta = data['Close'].diff()
gain  = delta.clip(lower=0)
loss  = -delta.clip(upper=0)
avg_g = gain.ewm(com=13, adjust=False).mean()
avg_l = loss.ewm(com=13, adjust=False).mean()
data['RSI'] = 100 - 100 / (1 + avg_g / avg_l)

low9  = data['Low'].rolling(9).min()
high9 = data['High'].rolling(9).max()
rsv   = (data['Close'] - low9) / (high9 - low9) * 100
data['K'] = rsv.ewm(com=2, adjust=False).mean()
data['D'] = data['K'].ewm(com=2, adjust=False).mean()
data['J'] = 3 * data['K'] - 2 * data['D']

# ── 截取指定月份数据 ──────────────────────────────────────────────
cutoff = data.index[-1] - pd.DateOffset(months=MONTHS)
dfp    = data[data.index >= cutoff].copy()
last_close = float(dfp['Close'].iloc[-1])

print(f"[INFO] 使用 {len(dfp)} 个交易日数据（近 {MONTHS} 个月）绘图")

# ── 绘图 ──────────────────────────────────────────────────────────
fig = make_subplots(
    rows=5, cols=1,
    shared_xaxes=True,
    vertical_spacing=0.025,
    row_heights=[0.38, 0.11, 0.17, 0.17, 0.17],
    subplot_titles=(
        f'K线 & 均线 & 布林带（近{MONTHS}个月）',
        '成交量',
        'MACD (12,26,9)',
        'RSI(14)',
        'KDJ(9,3,3)'
    )
)

# K线（绿涨红跌：国际惯例）
fig.add_trace(go.Candlestick(
    x=dfp.index, open=dfp['Open'], high=dfp['High'],
    low=dfp['Low'], close=dfp['Close'],
    name='K线',
    increasing_line_color='green', decreasing_line_color='red',
    increasing_fillcolor='green',  decreasing_fillcolor='red'
), row=1, col=1)

# 均线
ma_colors = {5:'#F59E0B', 10:'#3B82F6', 20:'#10B981', 50:'#A78BFA', 120:'#EC4899', 200:'#6B7280'}
for p, c in ma_colors.items():
    fig.add_trace(go.Scatter(
        x=dfp.index, y=dfp[f'MA{p}'],
        name=f'MA{p}', line=dict(color=c, width=1.2)
    ), row=1, col=1)

# 布林带
fig.add_trace(go.Scatter(x=dfp.index, y=dfp['BB_UP'], name='BB上轨',
    line=dict(color='rgba(255,255,255,0.3)', width=1)), row=1, col=1)
fig.add_trace(go.Scatter(x=dfp.index, y=dfp['BB_DN'], name='BB下轨',
    line=dict(color='rgba(255,255,255,0.3)', width=1),
    fill='tonexty', fillcolor='rgba(255,255,255,0.04)'), row=1, col=1)
fig.add_trace(go.Scatter(x=dfp.index, y=dfp['BB_MID'], name='BB中轨',
    line=dict(color='rgba(255,255,255,0.5)', width=1, dash='dash')), row=1, col=1)

# 成交量
vol_colors = ['green' if c >= o else 'red' for c, o in zip(dfp['Close'], dfp['Open'])]
fig.add_trace(go.Bar(x=dfp.index, y=dfp['Volume'], name='成交量',
    marker_color=vol_colors, opacity=0.7), row=2, col=1)
vol_ma5 = dfp['Volume'].rolling(5).mean()
fig.add_trace(go.Scatter(x=dfp.index, y=vol_ma5, name='Vol MA5',
    line=dict(color='#F59E0B', width=1.2)), row=2, col=1)

# MACD
hist_colors = ['green' if v >= 0 else 'red' for v in dfp['HIST']]
fig.add_trace(go.Bar(x=dfp.index, y=dfp['HIST'], name='MACD柱',
    marker_color=hist_colors, opacity=0.7), row=3, col=1)
fig.add_trace(go.Scatter(x=dfp.index, y=dfp['MACD'], name='MACD线',
    line=dict(color='#E2E8F0', width=1.5)), row=3, col=1)
fig.add_trace(go.Scatter(x=dfp.index, y=dfp['SIGNAL'], name='信号线',
    line=dict(color='#F59E0B', width=1.5)), row=3, col=1)

# RSI
fig.add_trace(go.Scatter(x=dfp.index, y=dfp['RSI'], name='RSI(14)',
    line=dict(color='#E2E8F0', width=1.5)), row=4, col=1)
fig.add_hline(y=70, line_dash='dash', line_color='#f85149', row=4, col=1)
fig.add_hline(y=30, line_dash='dash', line_color='#3fb950', row=4, col=1)
fig.add_hline(y=50, line_dash='dot', line_color='rgba(255,255,255,0.2)', row=4, col=1)

# KDJ
fig.add_trace(go.Scatter(x=dfp.index, y=dfp['K'], name='K值',
    line=dict(color='#E2E8F0', width=1.2)), row=5, col=1)
fig.add_trace(go.Scatter(x=dfp.index, y=dfp['D'], name='D值',
    line=dict(color='#F59E0B', width=1.2)), row=5, col=1)
fig.add_trace(go.Scatter(x=dfp.index, y=dfp['J'], name='J值',
    line=dict(color='#EC4899', width=1.2)), row=5, col=1)

# 布局
fig.update_layout(
    title=dict(
        text=f'{TICKER} 近{MONTHS}个月K线技术分析（最新收盘: {last_close:.4f}）',
        x=0.5,
        font=dict(size=16, family='Microsoft YaHei, PingFang SC, sans-serif', color='#E2E8F0')
    ),
    paper_bgcolor='#0d1117',
    plot_bgcolor='#0d1117',
    font=dict(color='#E2E8F0', family='Microsoft YaHei, PingFang SC, sans-serif'),
    legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='center', x=0.5, font_size=11),
    margin=dict(t=60, b=30, l=10, r=10),
    height=1350,
    xaxis_rangeslider_visible=False,
    hovermode='x unified'
)
fig.update_xaxes(gridcolor='#21262d', zeroline=False, showgrid=True)
fig.update_yaxes(gridcolor='#21262d', zeroline=False, showgrid=True)

# ── 输出为嵌入片段（只取 body 内容）────────────────────────────────
# P2-4: include_plotlyjs=True 内联完整 plotly.js (~3.4MB)，使报告完全离线可用。
# 不再依赖公共 CDN (https://cdn.plot.ly/plotly-*.min.js)。
# 图表交互功能不受影响。报告体积会增大约 3.4MB。
html_str = fig.to_html(
    include_plotlyjs=True,
    config={'displayModeBar': True, 'responsive': True}
)
body_match = re.search(r'<body>(.*?)</body>', html_str, re.DOTALL)
chart_html = body_match.group(1) if body_match else html_str

with open(OUT_FILE, 'w', encoding='utf-8') as f:
    f.write(chart_html)

print(f"[OK] K线图已写入 {OUT_FILE}（{len(chart_html)} 字节）")
