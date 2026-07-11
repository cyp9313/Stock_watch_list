#!/usr/bin/env python3
"""
fetch_and_calc.py — 通用金融日报数据获取与技术指标计算脚本
支持：股票（A股需用 yfinance 格式如 600519.SS）、美股、港股、加密货币（BTC-USD）、ETF

用法:
    python fetch_and_calc.py <TICKER> [OUTPUT_JSON]

示例:
    python fetch_and_calc.py ORCL orcl_report_data.json
    python fetch_and_calc.py BTC-USD btc_report_data.json
    python fetch_and_calc.py QQQ qqq_report_data.json
    python fetch_and_calc.py 0700.HK hk_report_data.json

输出: JSON 文件，包含所有计算结果，供后续 HTML 报告生成脚本使用
"""

import sys
import os
import json
import warnings
import yfinance as yf
import pandas as pd
import numpy as np

# Shared market data service — provides OHLCV snapshot sharing and unified provider layer.
from market_data_service import MarketDataService

# Optional StockAnalysis.com fundamentals scraper.
# It is used for valuation / analyst fields because yfinance info can be stale or inaccurate.
try:
    from stockanalysis_scraper import scrape_stock_analysis, should_query_forward_pe
    from ticker_mapping import is_known_us_etf
except Exception:
    scrape_stock_analysis = None
    should_query_forward_pe = None
    is_known_us_etf = None


warnings.filterwarnings('ignore')

# ── 参数处理 ──────────────────────────────────────────────────────
if len(sys.argv) < 2:
    print("用法: python fetch_and_calc.py <TICKER> [OUTPUT_JSON]")
    sys.exit(1)

TICKER = sys.argv[1].upper()
OUT_FILE = sys.argv[2] if len(sys.argv) >= 3 else f"{TICKER.lower().replace('-','_')}_report_data.json"

print(f"[INFO] 获取 {TICKER} 数据...")

# ── 数据获取 ───────────────────────────────────────────────────────
# Use MarketDataService for unified data fetching. Save a snapshot so
# gen_chart.py can reuse the same OHLCV data instead of re-downloading.
raw = MarketDataService.fetch_ohlcv(TICKER, period='1y', interval='1d', auto_adjust=False)
MarketDataService.save_ohlcv_snapshot(raw, TICKER, run_dir=os.getcwd())
if isinstance(raw.columns, pd.MultiIndex):
    raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
raw = raw.dropna(subset=['Close'])

if len(raw) < 30:
    print(f"[ERROR] 数据不足（仅 {len(raw)} 条），无法计算技术指标")
    sys.exit(1)

df = raw.copy()

# ── 基础信息 ───────────────────────────────────────────────────────
info = MarketDataService.fetch_ticker_info(TICKER)

LAST_CLOSE  = float(df['Close'].iloc[-1])
PREV_CLOSE  = float(df['Close'].iloc[-2])
CHG         = LAST_CLOSE - PREV_CLOSE
PCT         = CHG / PREV_CLOSE * 100
TODAY_HIGH  = float(df['High'].iloc[-1])
TODAY_LOW   = float(df['Low'].iloc[-1])
TODAY_OPEN  = float(df['Open'].iloc[-1])
TODAY_VOL   = int(df['Volume'].iloc[-1])

chg_sign  = 'up' if CHG >= 0 else 'down'
chg_arrow = '▲' if CHG >= 0 else '▼'
price_color = '#3fb950' if CHG >= 0 else '#f85149'  # 绿涨红跌（国际惯例）

# ── 52周区间 ───────────────────────────────────────────────────────
hi52 = float(df['High'].max())
lo52 = float(df['Low'].min())
pct52 = (LAST_CLOSE - lo52) / (hi52 - lo52) * 100 if (hi52 - lo52) > 0 else 50.0

# ── 基本信息（从 yfinance info 获取，缺失时用 N/A）────────────────────
market_cap   = info.get('marketCap', 0) / 1e9 if info.get('marketCap') else 0  # 单位：十亿美元
fw_pe        = info.get('forwardPE', 0) or 0
ttm_pe       = info.get('trailingPE', 0) or 0
target_mean  = info.get('targetMeanPrice', 0) or 0
target_hi    = info.get('targetHighPrice', 0) or 0
target_lo    = info.get('targetLowPrice', 0) or 0
analyst_cnt  = info.get('numberOfAnalystOpinions', 0) or 0
short_name   = info.get('shortName', TICKER)
long_name    = info.get('longName', TICKER)
sector       = info.get('sector', '—')
industry     = info.get('industry', '—')
exchange     = info.get('exchange', '—')
currency     = info.get('currency', 'USD')
employees    = info.get('fullTimeEmployees', 0) or 0
beta         = info.get('beta', 0) or 0
# yfinance dividendYield is usually a decimal ratio (e.g. 0.0141 for 1.41%),
# but some endpoints/markets may already return a percent-like value. Guard
# against double-multiplication and obviously broken values.
raw_div_yield = info.get('dividendYield', 0) or 0
try:
    raw_div_yield = float(raw_div_yield)
except Exception:
    raw_div_yield = 0
if 0 < raw_div_yield <= 1:
    div_yield = raw_div_yield * 100
elif 1 < raw_div_yield <= 20:
    div_yield = raw_div_yield
else:
    div_yield = 0
description  = info.get('longBusinessSummary', '')

# ── 标的类型识别（v5.8）──────────────────────────────────────────────
def _detect_instrument_type(ticker: str, yf_info: dict) -> tuple[str, str, str]:
    quote_type = str(yf_info.get('quoteType') or '').upper().strip()
    t = ticker.upper().strip()
    if quote_type in {'CRYPTOCURRENCY', 'CRYPTO'} or t.endswith('-USD'):
        return 'CRYPTO', quote_type or 'CRYPTOCURRENCY', 'crypto_three_factor'
    if quote_type == 'INDEX' or t.startswith('^'):
        return 'INDEX', quote_type or 'INDEX', 'index_three_factor'
    if quote_type in {'ETF', 'MUTUALFUND'} or (is_known_us_etf is not None and is_known_us_etf(t)):
        return 'ETF', quote_type or 'ETF', 'etf_four_factor'
    if quote_type in {'EQUITY', 'STOCK'} or not quote_type:
        return 'EQUITY', quote_type or 'EQUITY', 'equity_five_factor'
    return 'OTHER', quote_type or 'OTHER', 'market_three_factor'

instrument_type, quote_type_raw, scoring_profile = _detect_instrument_type(TICKER, info)

# ── StockAnalysis.com 基本面/估值/分析师数据增强 ─────────────────────
# yfinance 的 forwardPE/targetMeanPrice 等字段经常缺失或不准确。
# 因此股票/ETF/部分海外市场默认优先尝试 StockAnalysis，并把返回结果和字段来源写入 data.json。
stockanalysis_enabled = os.environ.get('STOCKANALYSIS_ENABLED', 'true').lower() not in {'0', 'false', 'no'}
stockanalysis_data = None
stockanalysis_error = ''
fundamental_sources = {
    'market_cap': 'yfinance',
    'forward_pe': 'yfinance',
    'trailing_pe': 'yfinance',
    'peg_ratio': 'missing',
    'ps_ratio': 'missing',
    'pb_ratio': 'missing',
    'ev_sales': 'missing',
    'ev_ebitda': 'missing',
    'ev_fcf': 'missing',
    'p_fcf': 'missing',
    'p_ocf': 'missing',
    'forward_ps': 'missing',
    'fcf_yield': 'missing',
    'debt_equity': 'missing',
    'debt_ebitda': 'missing',
    'debt_fcf': 'missing',
    'interest_coverage': 'missing',
    'analyst_rating': 'missing',
    'price_target': 'yfinance' if target_mean else 'missing',
}
peg_ratio = 0
ps_ratio = 0
pb_ratio = 0
analyst_rating = ''
price_target_sa = 0
# Non-equity instruments must not appear to have missing equity valuation fields.
if instrument_type not in {'EQUITY', 'ETF'}:
    for field_name in [
        'forward_pe', 'trailing_pe', 'peg_ratio', 'ps_ratio', 'pb_ratio',
        'ev_sales', 'ev_ebitda', 'ev_fcf', 'p_fcf', 'p_ocf', 'forward_ps',
        'fcf_yield', 'debt_equity', 'debt_ebitda', 'debt_fcf',
        'interest_coverage', 'analyst_rating', 'price_target',
    ]:
        fundamental_sources[field_name] = 'not_applicable'
ev_sales = ev_ebitda = ev_fcf = p_fcf = p_ocf = forward_ps = None
fcf_yield = debt_equity = debt_ebitda = debt_fcf = interest_coverage = None

if stockanalysis_enabled and scrape_stock_analysis is not None:
    try:
        if should_query_forward_pe is None or should_query_forward_pe(TICKER):
            stockanalysis_data = scrape_stock_analysis(TICKER)
            if stockanalysis_data:
                sa_mcap = stockanalysis_data.get('market_cap')
                if sa_mcap:
                    market_cap = float(sa_mcap) / 1e9
                    fundamental_sources['market_cap'] = 'stockanalysis'
                if stockanalysis_data.get('forward_pe') is not None:
                    fw_pe = float(stockanalysis_data.get('forward_pe') or 0)
                    fundamental_sources['forward_pe'] = 'stockanalysis'
                if stockanalysis_data.get('trailing_pe') is not None:
                    ttm_pe = float(stockanalysis_data.get('trailing_pe') or 0)
                    fundamental_sources['trailing_pe'] = 'stockanalysis'
                if stockanalysis_data.get('peg_ratio') is not None:
                    peg_ratio = float(stockanalysis_data.get('peg_ratio') or 0)
                    fundamental_sources['peg_ratio'] = 'stockanalysis'
                if stockanalysis_data.get('ps_ratio') is not None:
                    ps_ratio = float(stockanalysis_data.get('ps_ratio') or 0)
                    fundamental_sources['ps_ratio'] = 'stockanalysis'
                if stockanalysis_data.get('pb_ratio') is not None:
                    pb_ratio = float(stockanalysis_data.get('pb_ratio') or 0)
                    fundamental_sources['pb_ratio'] = 'stockanalysis'
                for field_name in [
                    'ev_sales', 'ev_ebitda', 'ev_fcf', 'p_fcf', 'p_ocf',
                    'forward_ps', 'fcf_yield', 'debt_equity', 'debt_ebitda',
                    'debt_fcf', 'interest_coverage',
                ]:
                    if stockanalysis_data.get(field_name) is not None:
                        fundamental_sources[field_name] = 'stockanalysis'
                ev_sales = float(stockanalysis_data['ev_sales']) if stockanalysis_data.get('ev_sales') is not None else ev_sales
                ev_ebitda = float(stockanalysis_data['ev_ebitda']) if stockanalysis_data.get('ev_ebitda') is not None else ev_ebitda
                ev_fcf = float(stockanalysis_data['ev_fcf']) if stockanalysis_data.get('ev_fcf') is not None else ev_fcf
                p_fcf = float(stockanalysis_data['p_fcf']) if stockanalysis_data.get('p_fcf') is not None else p_fcf
                p_ocf = float(stockanalysis_data['p_ocf']) if stockanalysis_data.get('p_ocf') is not None else p_ocf
                forward_ps = float(stockanalysis_data['forward_ps']) if stockanalysis_data.get('forward_ps') is not None else forward_ps
                fcf_yield = float(stockanalysis_data['fcf_yield']) if stockanalysis_data.get('fcf_yield') is not None else fcf_yield
                debt_equity = float(stockanalysis_data['debt_equity']) if stockanalysis_data.get('debt_equity') is not None else debt_equity
                debt_ebitda = float(stockanalysis_data['debt_ebitda']) if stockanalysis_data.get('debt_ebitda') is not None else debt_ebitda
                debt_fcf = float(stockanalysis_data['debt_fcf']) if stockanalysis_data.get('debt_fcf') is not None else debt_fcf
                interest_coverage = float(stockanalysis_data['interest_coverage']) if stockanalysis_data.get('interest_coverage') is not None else interest_coverage
                if stockanalysis_data.get('analyst_rating'):
                    analyst_rating = str(stockanalysis_data.get('analyst_rating') or '')
                    fundamental_sources['analyst_rating'] = 'stockanalysis'
                if stockanalysis_data.get('price_target') is not None:
                    price_target_sa = float(stockanalysis_data.get('price_target') or 0)
                    if price_target_sa > 0:
                        target_mean = price_target_sa
                        fundamental_sources['price_target'] = 'stockanalysis'
    except Exception as e:
        stockanalysis_error = str(e)

# ── 均线 ───────────────────────────────────────────────────────────
def calc_ma_pos(close_val, ma_val, period_name):
    if pd.isna(ma_val):
        return ['N/A', 'sig-neutral']
    diff_pct = (close_val - ma_val) / ma_val * 100
    if diff_pct > 0:
        return [f'▲ +{diff_pct:.2f}% 站上', 'sig-bull']
    elif diff_pct < -2:
        return [f'▼ {diff_pct:.2f}% 下方', 'sig-bear']
    else:
        return [f'≈ {diff_pct:.2f}%', 'sig-neutral']

periods = [5, 10, 20, 50, 120, 200]
ma_vals = {}
ma_pos  = {}
for p in periods:
    val = float(df['Close'].rolling(p).mean().iloc[-1])
    ma_vals[f'ma{p}'] = val
    ma_pos[f'ma{p}_pos'] = calc_ma_pos(LAST_CLOSE, val, f'MA{p}')

# 均线排列
bull_count = sum(1 for p in periods if not pd.isna(ma_vals[f'ma{p}']) and LAST_CLOSE > ma_vals[f'ma{p}'])

# ── 布林带 (20, 2σ) ──────────────────────────────────────────────
bb_mid_s = df['Close'].rolling(20).mean()
bb_std_s = df['Close'].rolling(20).std()
bb_mid = float(bb_mid_s.iloc[-1])
bb_up  = float((bb_mid_s + 2 * bb_std_s).iloc[-1])
bb_dn  = float((bb_mid_s - 2 * bb_std_s).iloc[-1])
bb_pct = (LAST_CLOSE - bb_dn) / (bb_up - bb_dn) * 100 if (bb_up - bb_dn) > 0 else 50.0

# ── MACD (12, 26, 9) ─────────────────────────────────────────────
ema12 = df['Close'].ewm(span=12, adjust=False).mean()
ema26 = df['Close'].ewm(span=26, adjust=False).mean()
macd_s  = ema12 - ema26
signal_s = macd_s.ewm(span=9, adjust=False).mean()
hist_s  = macd_s - signal_s
macd_line  = float(macd_s.iloc[-1])
signal_line = float(signal_s.iloc[-1])
hist_val   = float(hist_s.iloc[-1])

# ── RSI (14) ─────────────────────────────────────────────────────
delta  = df['Close'].diff()
gain   = delta.clip(lower=0)
loss   = -delta.clip(upper=0)
avg_g  = gain.ewm(com=13, adjust=False).mean()
avg_l  = loss.ewm(com=13, adjust=False).mean()
rs     = avg_g / avg_l
rsi_val = float((100 - 100 / (1 + rs)).iloc[-1])

# ── KDJ (9, 3, 3) ────────────────────────────────────────────────
low9  = df['Low'].rolling(9).min()
high9 = df['High'].rolling(9).max()
rsv   = (df['Close'] - low9) / (high9 - low9) * 100
k_s   = rsv.ewm(com=2, adjust=False).mean()
d_s   = k_s.ewm(com=2, adjust=False).mean()
j_s   = 3 * k_s - 2 * d_s
k_val = float(k_s.iloc[-1])
d_val = float(d_s.iloc[-1])
j_val = float(j_s.iloc[-1])

# ── ATR (14) ─────────────────────────────────────────────────────
tr_s = pd.concat([
    df['High'] - df['Low'],
    (df['High'] - df['Close'].shift()).abs(),
    (df['Low']  - df['Close'].shift()).abs()
], axis=1).max(axis=1)
atr14 = float(tr_s.rolling(14).mean().iloc[-1])

# ── 成交量均线 ────────────────────────────────────────────────────
vol_ma5  = float(df['Volume'].rolling(5).mean().iloc[-1])
vol_ma20 = float(df['Volume'].rolling(20).mean().iloc[-1])
positive_volume_days_20 = int((df['Volume'].tail(20).fillna(0) > 0).sum())
volume_data_valid = bool(vol_ma20 > 0 and positive_volume_days_20 >= 10)
vol_ratio = TODAY_VOL / vol_ma20 if volume_data_valid else None

# Deterministic market-risk inputs used by risk_score for all instrument types.
returns = df['Close'].pct_change().dropna()
annualization_days = 365 if instrument_type == 'CRYPTO' else 252
realized_vol_20d_pct = float(returns.tail(20).std() * np.sqrt(annualization_days) * 100) if len(returns.tail(20)) >= 10 else None
close63 = df['Close'].tail(63)
rolling_peak63 = close63.cummax()
max_drawdown_63d_pct = float(abs(((close63 / rolling_peak63) - 1.0).min()) * 100) if len(close63) >= 20 else None
atr_pct = float(atr14 / LAST_CLOSE * 100) if LAST_CLOSE > 0 else None
distance_from_52w_high_pct = float((hi52 - LAST_CLOSE) / hi52 * 100) if hi52 > 0 else None



# ── 筹码峰 / Volume Profile（基于日线 OHLCV 的近似成交量价格分布） ─────────────
def _clamp(v, lo=0.0, hi=100.0):
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return 50.0


def _calc_volume_profile(frame: pd.DataFrame, window: int, bins: int = 48, value_area_pct: float = 0.70) -> dict:
    """Approximate volume-by-price profile from daily OHLCV.

    This is not broker-level chip distribution. With yfinance daily data, we
    distribute each day's volume across the intraday Low-High range by overlap
    with price bins. It gives a useful support/resistance proxy for daily reports.
    """
    sub = frame.tail(window).dropna(subset=['High', 'Low', 'Close', 'Volume']).copy()
    if len(sub) < max(20, min(window, 30)):
        return {"window": window, "ok": False, "reason": "insufficient_data"}

    lo = float(sub['Low'].min())
    hi = float(sub['High'].max())
    close = float(sub['Close'].iloc[-1])
    total_vol = float(sub['Volume'].sum())
    if hi <= lo or total_vol <= 0:
        return {"window": window, "ok": False, "reason": "invalid_range_or_volume"}

    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    prof = np.zeros(bins, dtype=float)

    for _, row in sub.iterrows():
        day_lo = float(row['Low'])
        day_hi = float(row['High'])
        vol = float(row['Volume'])
        if vol <= 0:
            continue
        if day_hi <= day_lo:
            idx = int(np.argmin(np.abs(centers - float(row['Close']))))
            prof[idx] += vol
            continue
        overlaps = np.maximum(0, np.minimum(edges[1:], day_hi) - np.maximum(edges[:-1], day_lo))
        overlap_sum = float(overlaps.sum())
        if overlap_sum > 0:
            prof += vol * overlaps / overlap_sum
        else:
            idx = int(np.argmin(np.abs(centers - float(row['Close']))))
            prof[idx] += vol

    # Light smoothing to reduce one-bin noise.
    if bins >= 5:
        kernel = np.array([0.25, 0.5, 0.25])
        prof = np.convolve(prof, kernel, mode='same')

    if float(prof.sum()) <= 0:
        return {"window": window, "ok": False, "reason": "empty_profile"}

    poc_idx = int(np.argmax(prof))
    poc_price = float(centers[poc_idx])
    poc_distance_pct = (close - poc_price) / poc_price * 100 if poc_price > 0 else 0.0

    # Value area: choose highest-volume bins until reaching 70% of total profile volume.
    order = np.argsort(prof)[::-1]
    selected = []
    acc = 0.0
    target = float(prof.sum()) * value_area_pct
    for idx in order:
        selected.append(int(idx))
        acc += float(prof[idx])
        if acc >= target:
            break
    va_low = float(edges[min(selected)]) if selected else lo
    va_high = float(edges[max(selected) + 1]) if selected else hi
    if close < va_low:
        va_pos = "below_value_area"
    elif close > va_high:
        va_pos = "above_value_area"
    elif close < poc_price:
        va_pos = "inside_lower_value_area"
    elif close > poc_price:
        va_pos = "inside_upper_value_area"
    else:
        va_pos = "near_poc"

    above = float(prof[centers > close].sum()) / float(prof.sum())
    below = float(prof[centers < close].sum()) / float(prof.sum())

    # Extract top local peaks, deduplicated by distance.
    candidate_idx = list(np.argsort(prof)[::-1])
    peaks = []
    min_sep = max((hi - lo) / bins * 2.0, close * 0.005)
    for idx in candidate_idx:
        price = float(centers[int(idx)])
        if any(abs(price - p['price']) < min_sep for p in peaks):
            continue
        dist = (price - close) / close * 100 if close > 0 else 0.0
        if abs(dist) <= 1.2:
            role = "balance_area_near_price"
        elif price > close:
            role = "resistance_or_overhead_supply"
        else:
            role = "support_or_cost_base"
        peaks.append({
            "price": round(price, 4),
            "volume_share_pct": round(float(prof[int(idx)]) / float(prof.sum()) * 100, 2),
            "distance_pct": round(dist, 2),
            "role": role,
        })
        if len(peaks) >= 5:
            break

    top3_share = float(np.sort(prof)[-3:].sum()) / float(prof.sum()) if len(prof) >= 3 else float(prof.max()) / float(prof.sum())
    hhi = float(((prof / float(prof.sum())) ** 2).sum())
    concentration = _clamp((top3_share * 70 + hhi * bins * 30), 0, 100)

    score = 50.0
    score += (below - above) * 28.0
    score += 8.0 if close >= poc_price else -8.0
    if close > va_high:
        score += 8.0
    elif close < va_low:
        score -= 8.0
    # Being very close to POC is more neutral/balanced than directional.
    if abs(poc_distance_pct) <= 1.0:
        score += 2.0
    score = _clamp(score)

    if score >= 65:
        chip_signal = "BULL_SUPPORTIVE"
    elif score <= 40:
        chip_signal = "BEAR_OVERHEAD_SUPPLY"
    else:
        chip_signal = "MIX_BALANCE_AREA"

    return {
        "window": window,
        "ok": True,
        "method": "daily_range_distributed_volume_profile",
        "bins": bins,
        "close": round(close, 4),
        "poc_price": round(poc_price, 4),
        "poc_distance_pct": round(poc_distance_pct, 2),
        "value_area_low": round(va_low, 4),
        "value_area_high": round(va_high, 4),
        "value_area_position": va_pos,
        "overhead_supply_ratio": round(above, 3),
        "support_volume_ratio": round(below, 3),
        "profile_concentration": round(concentration, 1),
        "top_peaks": peaks,
        "chip_score": round(score, 1),
        "chip_signal": chip_signal,
    }


chip_profiles = {
    "63d": _calc_volume_profile(df, 63),
    "126d": _calc_volume_profile(df, 126),
    "252d": _calc_volume_profile(df, 252),
}
chip_primary = chip_profiles.get("126d") or {}
chip_score = float(chip_primary.get("chip_score")) if chip_primary.get("ok") and chip_primary.get("chip_score") is not None else None

# 技术面分项评分（0-100）。无效成交量/筹码数据标记为 N/A，并重新归一化有效权重。
trend_score = _clamp(bull_count / 6 * 100)
rsi_component = 100 - min(abs(rsi_val - 55) * 2.2, 55) if rsi_val >= 50 else max(0, 50 - (50 - rsi_val) * 1.6)
macd_component = 65 if macd_line > signal_line else 35
kdj_component = 60 if k_val > d_val else 40
momentum_score = _clamp(rsi_component * 0.45 + macd_component * 0.35 + kdj_component * 0.20)
volume_score = _clamp(50 + min(max(float(vol_ratio) - 1.0, -1.0), 2.0) * 15) if volume_data_valid and vol_ratio is not None else None
volatility_score = _clamp(100 - abs(bb_pct - 50) * 0.8)

technical_nominal_weights = {
    "trend_score": 0.35,
    "momentum_score": 0.25,
    "chip_profile_score": 0.20,
    "volume_score": 0.10,
    "volatility_score": 0.10,
}
technical_components = {
    "trend_score": trend_score,
    "momentum_score": momentum_score,
    "chip_profile_score": chip_score,
    "volume_score": volume_score,
    "volatility_score": volatility_score,
}
available_technical_weight = sum(technical_nominal_weights[k] for k, v in technical_components.items() if v is not None)
technical_effective_weights = {
    k: round(technical_nominal_weights[k] / available_technical_weight, 4)
    for k, v in technical_components.items() if v is not None and available_technical_weight > 0
}
technical_unavailable_components = [k for k, v in technical_components.items() if v is None]
technical_score = _clamp(sum(float(technical_components[k]) * w for k, w in technical_effective_weights.items()))
if technical_score >= 65:
    technical_signal = "BULLISH_TECHNICAL"
elif technical_score <= 40:
    technical_signal = "BEARISH_TECHNICAL"
else:
    technical_signal = "MIXED_TECHNICAL"

# ── 汇总输出 ──────────────────────────────────────────────────────
result = {
    # 基础行情
    "TICKER":       TICKER,
    "INSTRUMENT_TYPE": instrument_type,
    "QUOTE_TYPE_RAW": quote_type_raw,
    "SCORING_PROFILE": scoring_profile,
    "SHORT_NAME":   short_name,
    "LONG_NAME":    long_name,
    "SECTOR":       sector,
    "INDUSTRY":     industry,
    "EXCHANGE":     exchange,
    "CURRENCY":     currency,
    "EMPLOYEES":    employees,
    "DESCRIPTION":  description[:500] if description else '',

    # 价格数据
    "LAST_CLOSE":   LAST_CLOSE,
    "PREV_CLOSE":   PREV_CLOSE,
    "CHG":          CHG,
    "PCT":          PCT,
    "chg_sign":     chg_sign,
    "chg_arrow":    chg_arrow,
    "price_color":  price_color,
    "TODAY_HIGH":   TODAY_HIGH,
    "TODAY_LOW":    TODAY_LOW,
    "TODAY_OPEN":   TODAY_OPEN,
    "TODAY_VOL":    TODAY_VOL,

    # 52周
    "FIFTY2W_HI":   hi52,
    "FIFTY2W_LO":   lo52,
    "percentile_52w": pct52,

    # 基本面（部分数据加密货币/ETF 会为0）
    "MARKET_CAP":   market_cap,
    "FW_PE":        fw_pe,
    "TTM_PE":       ttm_pe,
    "TARGET_MEAN":  target_mean,
    "TARGET_HI":    target_hi,
    "TARGET_LO":    target_lo,
    "ANALYST_CNT":  analyst_cnt,
    "BETA":         beta,
    "DIV_YIELD":    div_yield,
    "PEG_RATIO":    peg_ratio,
    "PS_RATIO":     ps_ratio,
    "PB_RATIO":     pb_ratio,
    "ANALYST_RATING": analyst_rating,
    "PRICE_TARGET_STOCKANALYSIS": price_target_sa,
    "EV_SALES": ev_sales,
    "EV_EBITDA": ev_ebitda,
    "EV_FCF": ev_fcf,
    "P_FCF": p_fcf,
    "P_OCF": p_ocf,
    "FORWARD_PS": forward_ps,
    "FCF_YIELD": fcf_yield,
    "DEBT_EQUITY": debt_equity,
    "DEBT_EBITDA": debt_ebitda,
    "DEBT_FCF": debt_fcf,
    "INTEREST_COVERAGE": interest_coverage,
    "FUNDAMENTAL_SOURCES": fundamental_sources,
    "STOCKANALYSIS_ENABLED": stockanalysis_enabled,
    "STOCKANALYSIS_DATA": stockanalysis_data or {},
    "STOCKANALYSIS_ERROR": stockanalysis_error,

    # 均线
    **{k: (0.0 if pd.isna(v) else v) for k, v in ma_vals.items()},
    **{k: v for k, v in ma_pos.items()},
    "bull_ma_count": bull_count,

    # 布林带
    "bb_up":   bb_up,
    "bb_mid":  bb_mid,
    "bb_dn":   bb_dn,
    "bb_pct":  bb_pct,

    # MACD
    "macd_line":   macd_line,
    "signal_line": signal_line,
    "hist_val":    hist_val,

    # RSI
    "rsi": rsi_val,

    # KDJ
    "k_val": k_val,
    "d_val": d_val,
    "j_val": j_val,

    # 成交量
    "vol_ma5":   vol_ma5,
    "vol_ma20":  vol_ma20,
    "vol_ratio": vol_ratio,
    "volume_data_valid": volume_data_valid,
    "positive_volume_days_20": positive_volume_days_20,

    # ATR
    "atr14": atr14,
    "ATR_PCT": atr_pct,
    "REALIZED_VOL_20D_PCT": realized_vol_20d_pct,
    "MAX_DRAWDOWN_63D_PCT": max_drawdown_63d_pct,
    "DISTANCE_FROM_52W_HIGH_PCT": distance_from_52w_high_pct,

    # 筹码峰 / Volume Profile（基于 yfinance 日线 OHLCV 近似）
    "chip_profiles": chip_profiles,
    "chip_profile_primary_window": "126d",
    "chip_profile_primary": chip_primary,
    "chip_score": chip_score,

    # 技术面分项评分
    "technical_score": technical_score,
    "technical_signal": technical_signal,
    "technical_subscores": {
        "trend_score": trend_score,
        "momentum_score": momentum_score,
        "volume_score": volume_score,
        "chip_profile_score": chip_score,
        "volatility_score": volatility_score,
    },
    "technical_nominal_weights": technical_nominal_weights,
    "technical_effective_weights": technical_effective_weights,
    "technical_unavailable_components": technical_unavailable_components,

    # 历史数据条数
    "data_days": len(df),
    "data_start": str(df.index[0].date()),
    "data_end":   str(df.index[-1].date()),
}

with open(OUT_FILE, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"[OK] 数据已写入 {OUT_FILE}")
print(f"     最新收盘价: {LAST_CLOSE:.4f} {currency}")
print(f"     涨跌: {chg_arrow} {CHG:+.4f} ({PCT:+.2f}%)")
print(f"     RSI(14): {rsi_val:.1f}")
print(f"     MACD: {macd_line:.4f} / 信号线: {signal_line:.4f}")
print(f"     多头均线数: {bull_count}/6")
print(f"     基本面来源: {fundamental_sources}; StockAnalysis error={stockanalysis_error or 'None'}")
print(f"     标的类型: {instrument_type} / scoring_profile={scoring_profile}")
print(f"     筹码峰(126d): POC={chip_primary.get('poc_price', 0)}，score={chip_score if chip_score is not None else 'N/A'}，signal={chip_primary.get('chip_signal', 'N/A')}")
print(f"     技术面综合分: {technical_score:.1f}，signal={technical_signal}")
