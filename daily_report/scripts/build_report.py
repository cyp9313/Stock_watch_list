#!/usr/bin/env python3
"""
build_report.py — 通用金融日报 HTML 报告生成器
将 fetch_and_calc.py 生成的 JSON 数据 + gen_chart.py 生成的 K线图，
拼装成完整的暗色主题交互式 HTML 报告。

用法:
    python build_report.py <DATA_JSON> <CHART_HTML> <OUTPUT_HTML> [--date YYYY-MM-DD] [--notes NOTES_FILE]

示例:
    python build_report.py orcl_data.json orcl_chart.html orcl-report-2026-05-21.html
    python build_report.py btc_data.json btc_chart.html btc-report-2026-05-21.html --date 2026-05-21
    python build_report.py qqq_data.json qqq_chart.html qqq-report.html --notes qqq_notes.txt

--notes 文件格式（纯文本，每行一条，用 [BULL]/[BEAR]/[MIX] 前缀标记多空）:
    [BULL] 联储暂停加息，风险资产全面反弹
    [BEAR] 通胀数据超预期，加息预期升温
    [MIX] Q3 财报营收超预期但指引下调

没有 --notes 时，消息面区块将显示占位符提示。
"""

import sys
import json
import re
from datetime import date as Date
from html import escape as html_escape

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[assignment]


_ALLOWED_SIGNAL_CLASSES = {"signal-bull", "signal-bear", "signal-neutral"}
_ALLOWED_RATING_CLASSES = {"buy", "hold", "avoid"}
_ALLOWED_PRICE_COLORS = {"#3fb950", "#f85149"}
_ALLOWED_INSTRUMENT_TYPES = {"EQUITY", "ETF", "INDEX", "CRYPTO", "OTHER"}

# ── 货币符号映射 ──────────────────────────────────────────────────
# P2-1: 根据数据中的 currency 字段动态选择符号，不再统一硬编码 $。
# 未知 currency 使用 currency code 本身作为前缀，而非错误的美元符号。
CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "GBX": "",       # 便士单位，后缀 "p"
    "HKD": "HK$",
    "CNY": "￥",
    "CNH": "￥",
    "CAD": "CA$",
    "AUD": "A$",
    "JPY": "¥",
    "CHF": "CHF ",
    "SEK": "SEK ",
    "NOK": "NOK ",
    "DKK": "DKK ",
}

# 便士等使用后缀的货币
_CURRENCY_SUFFIXES = {"GBX": "p"}


def format_price(value_str, currency):
    """Format a price string with the appropriate currency symbol.

    For known currencies, uses the mapped symbol as prefix.
    For GBX (pence), uses 'p' as suffix.
    For unknown currencies, uses the currency code as prefix (e.g. 'SGD 123.45').
    """
    currency = str(currency or "USD").upper()
    symbol = CURRENCY_SYMBOLS.get(currency)
    if symbol is not None:
        suffix = _CURRENCY_SUFFIXES.get(currency, "")
        return symbol + value_str + suffix
    # Unknown currency — use the code itself to avoid a misleading $
    return currency + " " + value_str


def get_market_date():
    """Get current date in US/Eastern timezone (NYSE/NASDAQ market date).

    Returns ISO date string (YYYY-MM-DD). Uses zoneinfo (Python 3.9+ builtin,
    no extra dependency). Falls back to date.today() if zoneinfo is unavailable.
    """
    if ZoneInfo is not None:
        from datetime import datetime
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    return Date.today().isoformat()


def escape_text(value):
    """Escape all untrusted text before it is interpolated into report HTML."""
    return html_escape(str(value if value is not None else ""), quote=True)


def allow_value(value, allowed, fallback):
    """Keep dynamic HTML/CSS tokens within a small, fixed allowlist."""
    return value if isinstance(value, str) and value in allowed else fallback


# ─────────────────────────────────────────────────
#  参数解析
# ─────────────────────────────────────────────────
if len(sys.argv) < 4:
    print("用法: python build_report.py <DATA_JSON> <CHART_HTML> <OUTPUT_HTML> [--date YYYY-MM-DD] [--months N] [--notes NOTES_FILE]")
    sys.exit(1)

DATA_FILE  = sys.argv[1]
CHART_FILE = sys.argv[2]
OUT_FILE   = sys.argv[3]
REPORT_DATE = get_market_date()
MONTHS      = 3
NOTES_FILE  = None

i = 4
while i < len(sys.argv):
    if sys.argv[i] == '--date' and i+1 < len(sys.argv):
        REPORT_DATE = sys.argv[i+1]; i += 2
    elif sys.argv[i] == '--months' and i+1 < len(sys.argv):
        MONTHS = int(sys.argv[i+1]); i += 2
    elif sys.argv[i] == '--notes' and i+1 < len(sys.argv):
        NOTES_FILE = sys.argv[i+1]; i += 2
    else:
        i += 1

REPORT_DATE = escape_text(REPORT_DATE)

# ─────────────────────────────────────────────────
#  读取数据
# ─────────────────────────────────────────────────
with open(DATA_FILE, 'r', encoding='utf-8') as f:
    d = json.load(f)

with open(CHART_FILE, 'r', encoding='utf-8') as f:
    chart_html = f.read()

# 读取消息面注释（可选）
news_items = []
if NOTES_FILE:
    try:
        with open(NOTES_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                if line.startswith('[BULL]'):
                    news_items.append(('bull', 'tag-bull', '多头 📈', line[6:].strip()))
                elif line.startswith('[BEAR]'):
                    news_items.append(('bear', 'tag-bear', '空头 📉', line[6:].strip()))
                elif line.startswith('[MIX]'):
                    news_items.append(('mixed', 'tag-mixed', '中性 ⚖', line[5:].strip()))
                else:
                    news_items.append(('mixed', 'tag-mixed', '资讯', line))
    except FileNotFoundError:
        pass

# ─────────────────────────────────────────────────
#  预计算字符串（避免 f-string 嵌套问题）
# ─────────────────────────────────────────────────
LAST      = d['LAST_CLOSE']
PREV      = d['PREV_CLOSE']
CHG       = d['CHG']
PCT       = d['PCT']
chg_sign  = d['chg_sign']
chg_arrow = d['chg_arrow']
price_col = allow_value(
    d['price_color'],
    _ALLOWED_PRICE_COLORS,
    '#3fb950' if CHG >= 0 else '#f85149',
)
TICKER    = escape_text(d['TICKER'])
LONG_NAME = escape_text(d['LONG_NAME'])
SHORT_NAME= escape_text(d['SHORT_NAME'])
SECTOR    = escape_text(d.get('SECTOR', '—'))
EXCHANGE  = escape_text(d.get('EXCHANGE', '—'))
CURRENCY  = d.get('CURRENCY', 'USD')
EMPLOYEES = d.get('EMPLOYEES', 0)
# P2-3: 市场数据截止日期 — 来自 OHLCV 最后一条数据的 timestamp，
# 由 fetch_and_calc.py 写入 data.json。区分报告生成日期和数据截止日期。
DATA_END  = escape_text(str(d.get('data_end', '') or ''))

TODAY_HIGH = d['TODAY_HIGH']
TODAY_LOW  = d['TODAY_LOW']
TODAY_OPEN = d['TODAY_OPEN']
TODAY_VOL  = d['TODAY_VOL']
FW52_HI    = d['FIFTY2W_HI']
FW52_LO    = d['FIFTY2W_LO']
MARKET_CAP = d['MARKET_CAP']
FW_PE      = d['FW_PE']
TTM_PE     = d['TTM_PE']
TARGET_MEAN= d['TARGET_MEAN']
TARGET_HI  = d['TARGET_HI']
TARGET_LO  = d['TARGET_LO']
ANALYST_CNT= d['ANALYST_CNT']
BETA       = d.get('BETA', 0)
DIV_YIELD  = d.get('DIV_YIELD', 0)
pct52      = d['percentile_52w']
ma5   = d['ma5']; ma10 = d['ma10']; ma20 = d['ma20']
ma50  = d['ma50']; ma120= d['ma120']; ma200= d['ma200']
ma5_pos  = d['ma5_pos'];  ma10_pos = d['ma10_pos']
ma20_pos = d['ma20_pos']; ma50_pos = d['ma50_pos']
ma120_pos= d['ma120_pos'];ma200_pos= d['ma200_pos']
rsi       = d['rsi']
macd_line = d['macd_line']
signal_l  = d['signal_line']
hist_val  = d['hist_val']
k_val = d['k_val']; d_val = d['d_val']; j_val = d['j_val']
bb_up = d['bb_up']; bb_mid= d['bb_mid']; bb_dn = d['bb_dn']
bull_count= d.get('bull_ma_count', 0)
vol_ratio = d.get('vol_ratio', 1.0)
atr14     = d.get('atr14', 0)
realized_vol_20d = d.get('REALIZED_VOL_20D_PCT')
max_drawdown_63d = d.get('MAX_DRAWDOWN_63D_PCT')
atr_pct = d.get('ATR_PCT')

# 格式化字符串
def fs(v, decimals=2): return f"{v:.{decimals}f}"
def fsp(v): return f"{v:+.2f}"

last_str   = fs(LAST); prev_str = fs(PREV)
chg_str    = fsp(CHG); pct_str  = fsp(PCT)
high_str   = fs(TODAY_HIGH); low_str  = fs(TODAY_LOW)
open_str   = fs(TODAY_OPEN)
vol_str    = f"{TODAY_VOL:,}"
fw52hi_str = fs(FW52_HI); fw52lo_str = fs(FW52_LO)
pct52_str  = fs(pct52, 1)
mcap_str   = f"{MARKET_CAP:.1f}B" if MARKET_CAP > 1 else f"{MARKET_CAP*1000:.0f}M"
fwpe_str   = fs(FW_PE, 1); ttmpe_str = fs(TTM_PE, 1)
tgt_mean_str = fs(TARGET_MEAN); tgt_hi_str = fs(TARGET_HI); tgt_lo_str = fs(TARGET_LO)
ana_cnt_str = str(ANALYST_CNT)
ma5_str  = fs(ma5); ma10_str = fs(ma10); ma20_str = fs(ma20)
ma50_str = fs(ma50); ma120_str= fs(ma120); ma200_str= fs(ma200)
ma5_sig  = escape_text(ma5_pos[0]); ma5_cls  = allow_value(ma5_pos[1], _ALLOWED_SIGNAL_CLASSES, 'signal-neutral')
ma10_sig = escape_text(ma10_pos[0]); ma10_cls = allow_value(ma10_pos[1], _ALLOWED_SIGNAL_CLASSES, 'signal-neutral')
ma20_sig = escape_text(ma20_pos[0]); ma20_cls = allow_value(ma20_pos[1], _ALLOWED_SIGNAL_CLASSES, 'signal-neutral')
ma50_sig = escape_text(ma50_pos[0]); ma50_cls = allow_value(ma50_pos[1], _ALLOWED_SIGNAL_CLASSES, 'signal-neutral')
ma120_sig= escape_text(ma120_pos[0]); ma120_cls= allow_value(ma120_pos[1], _ALLOWED_SIGNAL_CLASSES, 'signal-neutral')
ma200_sig= escape_text(ma200_pos[0]); ma200_cls= allow_value(ma200_pos[1], _ALLOWED_SIGNAL_CLASSES, 'signal-neutral')
rsi_str   = fs(rsi, 1); macd_str  = fs(macd_line, 3)
signal_str= fs(signal_l, 3); hist_str = fs(hist_val, 3)
k_str = fs(k_val, 1); d_str = fs(d_val, 1); j_str = fs(j_val, 1)
bb_up_str = fs(bb_up); bb_mid_str = fs(bb_mid); bb_dn_str = fs(bb_dn)
atr_str   = fs(atr14)
realized_vol_str = f"{float(realized_vol_20d):.1f}%" if realized_vol_20d is not None else 'N/A'
max_drawdown_str = f"{float(max_drawdown_63d):.1f}%" if max_drawdown_63d is not None else 'N/A'
atr_pct_str = f"{float(atr_pct):.2f}%" if atr_pct is not None else 'N/A'
vol_ratio_str = f"{float(vol_ratio):.2f}x" if vol_ratio is not None else 'N/A'
chg_cls   = 'up' if CHG >= 0 else 'down'

# 目标价涨幅
tgt_upside = (TARGET_MEAN - LAST) / LAST * 100 if TARGET_MEAN > 0 else 0
tgt_up_str = f"{tgt_upside:+.1f}"
tgt_hi_up  = f"{(TARGET_HI - LAST)/LAST*100:+.1f}" if TARGET_HI > 0 else "N/A"
tgt_lo_up  = f"{(TARGET_LO - LAST)/LAST*100:+.1f}" if TARGET_LO > 0 else "N/A"

# v5.8: 筹码峰与按标的类型切换的综合评分（如 save_news_notes 已写入 data.json）
chip = d.get('chip_profile_primary') or {}
chip_ok = isinstance(chip, dict) and chip.get('ok')
chip_poc_str = fs(float(chip.get('poc_price', 0) or 0)) if chip_ok else '—'
chip_dist_str = f"{float(chip.get('poc_distance_pct', 0) or 0):+.2f}%" if chip_ok else '—'
chip_va_str = (fs(float(chip.get('value_area_low', 0) or 0)) + ' – ' + fs(float(chip.get('value_area_high', 0) or 0))) if chip_ok else '—'
chip_overhead_str = f"{float(chip.get('overhead_supply_ratio', 0) or 0)*100:.1f}%" if chip_ok else '—'
chip_support_str = f"{float(chip.get('support_volume_ratio', 0) or 0)*100:.1f}%" if chip_ok else '—'
chip_score_str = f"{float(chip.get('chip_score', 50) or 50):.1f}/100" if chip_ok else '—'
chip_signal = escape_text(chip.get('chip_signal') or 'N/A') if chip_ok else 'N/A'
tech_score = float(d.get('technical_score', 50) or 50)
tech_subscores = d.get('technical_subscores') or {}
final_rating = d.get('final_rating') or {}

# RSI 信号
if rsi > 70:
    rsi_sig = '▼ 超买'; rsi_cls = 'signal-bear'
elif rsi < 30:
    rsi_sig = '▲ 超卖'; rsi_cls = 'signal-bull'
elif rsi > 55:
    rsi_sig = '偏多'; rsi_cls = 'signal-bull'
elif rsi < 45:
    rsi_sig = '偏空'; rsi_cls = 'signal-bear'
else:
    rsi_sig = '中性'; rsi_cls = 'signal-neutral'

# MACD 信号
if macd_line > signal_l:
    macd_sig = '▲ 金叉'; macd_cls = 'signal-bull'
else:
    macd_sig = '▼ 死叉'; macd_cls = 'signal-bear'

# KDJ 信号
if j_val < 20:
    kdj_sig = '▲ J超卖'; kdj_cls = 'signal-bull'
elif j_val > 80:
    kdj_sig = '▼ J超买'; kdj_cls = 'signal-bear'
elif k_val > d_val:
    kdj_sig = '▲ K>D 偏多'; kdj_cls = 'signal-bull'
else:
    kdj_sig = '▼ K<D 偏空'; kdj_cls = 'signal-bear'

# 均线整体信号
if bull_count >= 5:
    ma_overall = '▲ 多头排列'; ma_overall_cls = 'signal-bull'
elif bull_count <= 1:
    ma_overall = '▼ 空头排列'; ma_overall_cls = 'signal-bear'
else:
    ma_overall = '➡ 混合信号'; ma_overall_cls = 'signal-neutral'

# 技术信号与 v5.8 综合评级
signals_bull = sum([
    1 if macd_line > signal_l else 0,
    1 if rsi > 50 else 0,
    1 if k_val > d_val else 0,
    1 if bull_count >= 4 else 0,
    1 if LAST > bb_mid else 0,
])
if final_rating:
    rating_text = str(final_rating.get('rating_text') or '综合评级')
    rating_cls = str(final_rating.get('rating_class') or 'hold')
    final_score_str = f"{float(final_rating.get('final_score', 50) or 50):.1f}"
    rating_method = str(final_rating.get('method') or 'v5.8_instrument_aware_dynamic_weights')
    rating_subscores = final_rating.get('subscores') or {}
    rating_status = final_rating.get('score_status') or {}
    rating_effective_weights = final_rating.get('effective_weights') or {}
    instrument_type = str(final_rating.get('instrument_type') or d.get('INSTRUMENT_TYPE') or 'EQUITY')
else:
    if signals_bull >= 4 and tgt_upside > 15:
        rating_text = '审慎买入 BUY ★★★★☆'; rating_cls = 'buy'
    elif signals_bull >= 3:
        rating_text = '中性持有 HOLD ★★★☆☆'; rating_cls = 'hold'
    else:
        rating_text = '等待观望 WATCH ★★☆☆☆'; rating_cls = 'hold'
    final_score_str = f"{tech_score:.1f}"
    rating_method = 'legacy_technical_only_fallback'
    rating_subscores = {'technical_score': tech_score, 'news_score': 50, 'valuation_score': 50, 'analyst_score': 50, 'risk_score': 50}
    rating_status = {}
    rating_effective_weights = {}
    instrument_type = str(d.get('INSTRUMENT_TYPE') or 'EQUITY')

rating_text = escape_text(rating_text)
rating_cls = allow_value(rating_cls, _ALLOWED_RATING_CLASSES, 'hold')
rating_method = escape_text(rating_method)
instrument_type = allow_value(instrument_type.upper(), _ALLOWED_INSTRUMENT_TYPES, 'OTHER')

# 消息面区块
def build_news_html(news_items):
    if not news_items:
        return '''<div style="padding:18px; background:#1c2128; border-radius:8px; color:#8b949e; font-size:13px;">
  📭 暂无手动录入的消息面资讯。如需添加，请使用 <code>--notes notes.txt</code> 参数，
  在文本文件中用 [BULL]/[BEAR]/[MIX] 前缀逐行记录资讯。
</div>'''
    parts = []
    for sentiment, tag_cls, tag_label, text in news_items:
        parts.append(f'''<div class="news-card {sentiment}">
  <div class="news-tag {tag_cls}">{tag_label}</div>
  <div class="news-summary">{escape_text(text)}</div>
</div>''')
    return '\n'.join(parts)

news_html = build_news_html(news_items)

# 员工数格式
emp_str = f"{EMPLOYEES//10000}万人" if EMPLOYEES >= 10000 else (f"{EMPLOYEES:,}人" if EMPLOYEES > 0 else '—')
# 分红
div_str = f"{DIV_YIELD:.2f}%" if DIV_YIELD > 0 else '—'
# Beta
beta_str = fs(BETA, 2) if BETA > 0 else '—'
# 描述
desc = d.get('DESCRIPTION', '')
desc_short = (desc[:200] + '...') if len(desc) > 200 else desc
desc_short = escape_text(desc_short)

# ─────────────────────────────────────────────────
#  HTML 模板拼装（用字符串连接，避免 f-string 嵌套）
# ─────────────────────────────────────────────────
CSS = '''  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif; background: #0d1117; color: #e6edf3; line-height: 1.6; padding: 20px; }
  .container { max-width: 1100px; margin: 0 auto; }
  .header { background: linear-gradient(135deg, #1a2332 0%, #0d1117 100%); border: 1px solid #2d333b; border-radius: 12px; padding: 28px 32px; margin-bottom: 24px; display: flex; align-items: center; gap: 28px; }
  .logo { width: 64px; height: 64px; background: linear-gradient(135deg, #1f6feb 0%, #0d3fa6 100%); border-radius: 14px; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 900; color: #fff; flex-shrink: 0; text-align: center; word-break: break-all; padding: 4px; }
  .header-info h1 { font-size: 22px; font-weight: 700; color: #f0f6fc; }
  .header-info .subtitle { font-size: 13px; color: #8b949e; margin-top: 2px; }
  .header-badge { margin-left: auto; text-align: right; }
  .badge-price { font-size: 32px; font-weight: 800; }
  .badge-change { font-size: 14px; margin-top: 2px; }
  .badge-date { font-size: 11px; color: #8b949e; margin-top: 4px; }
  .kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 24px; }
  .kpi-card { background: #161b22; border: 1px solid #2d333b; border-radius: 10px; padding: 16px 18px; }
  .kpi-label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  .kpi-value { font-size: 20px; font-weight: 700; color: #f0f6fc; }
  .kpi-sub { font-size: 11px; color: #8b949e; margin-top: 4px; }
  .kpi-value.up { color: #3fb950; } .kpi-value.down { color: #f85149; } .kpi-value.warn { color: #d29922; }
  .section { background: #161b22; border: 1px solid #2d333b; border-radius: 12px; padding: 24px; margin-bottom: 20px; }
  .section-title { font-size: 16px; font-weight: 700; color: #f0f6fc; margin-bottom: 18px; display: flex; align-items: center; gap: 8px; }
  .section-title .icon { font-size: 18px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { background: #1c2128; color: #8b949e; font-weight: 600; padding: 10px 12px; text-align: left; border-bottom: 1px solid #2d333b; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
  td { padding: 10px 12px; border-bottom: 1px solid #21262d; color: #e6edf3; }
  tr:last-child td { border-bottom: none; } tr:hover td { background: #1c2128; }
  .num { text-align: right; font-family: 'SF Mono', 'Fira Code', monospace; }
  .up { color: #3fb950; } .down { color: #f85149; } .warn { color: #d29922; }
  .tech-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
  .tech-card { background: #1c2128; border: 1px solid #2d333b; border-radius: 10px; padding: 16px; }
  .tech-card h4 { font-size: 13px; color: #8b949e; margin-bottom: 10px; font-weight: 600; }
  .tech-item { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid #21262d; }
  .tech-item:last-child { border-bottom: none; }
  .tech-key { font-size: 13px; color: #8b949e; } .tech-val { font-size: 13px; font-weight: 600; font-family: 'SF Mono', monospace; }
  .signal-bull { color: #3fb950; } .signal-bear { color: #f85149; } .signal-neutral { color: #d29922; }
  .price-ruler { background: #1c2128; border: 1px solid #2d333b; border-radius: 10px; padding: 20px; margin-top: 16px; }
  .ruler-bar-bg { height: 12px; background: #21262d; border-radius: 6px; position: relative; margin: 16px 0; }
  .ruler-bar-fill { height: 100%; border-radius: 6px; background: linear-gradient(90deg, #f85149 0%, #d29922 50%, #3fb950 100%); position: relative; }
  .ruler-dot { position: absolute; top: 50%; transform: translate(-50%, -50%); width: 18px; height: 18px; border-radius: 50%; border: 3px solid #fff; box-shadow: 0 0 8px rgba(0,0,0,0.5); }
  .ruler-dot.current { background: #58a6ff; z-index: 3; } .ruler-dot.high { background: #f85149; z-index: 2; } .ruler-dot.low { background: #3fb950; z-index: 2; }
  .ruler-labels { display: flex; justify-content: space-between; font-size: 11px; color: #8b949e; margin-top: 8px; }
  .ruler-labels .val { font-family: 'SF Mono', monospace; }
  .chart-container { margin: 20px 0; background: #1c2128; border: 1px solid #2d333b; border-radius: 10px; padding: 16px; }
  .news-list { display: flex; flex-direction: column; gap: 14px; }
  .news-card { background: #1c2128; border: 1px solid #2d333b; border-radius: 10px; padding: 16px 20px; border-left: 4px solid #2d333b; }
  .news-card.bull { border-left-color: #3fb950; } .news-card.bear { border-left-color: #f85149; } .news-card.mixed { border-left-color: #d29922; }
  .news-tag { display: inline-block; font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 4px; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }
  .tag-bull { background: rgba(63,185,80,0.15); color: #3fb950; } .tag-bear { background: rgba(248,81,73,0.15); color: #f85149; } .tag-mixed { background: rgba(210,153,34,0.15); color: #d29922; }
  .news-title { font-size: 14px; font-weight: 600; color: #f0f6fc; margin-bottom: 6px; }
  .news-summary { font-size: 13px; color: #8b949e; line-height: 1.6; }
  .news-meta { font-size: 11px; color: #6e7681; margin-top: 8px; display: flex; gap: 16px; }
  .thesis-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .thesis-card { border-radius: 10px; padding: 18px; }
  .thesis-card.bull { background: rgba(63,185,80,0.06); border: 1px solid rgba(63,185,80,0.2); }
  .thesis-card.bear { background: rgba(248,81,73,0.06); border: 1px solid rgba(248,81,73,0.2); }
  .thesis-card h4 { font-size: 14px; font-weight: 700; margin-bottom: 12px; }
  .thesis-card.bull h4 { color: #3fb950; } .thesis-card.bear h4 { color: #f85149; }
  .thesis-card ul { list-style: none; padding: 0; }
  .thesis-card li { font-size: 13px; color: #e6edf3; padding: 5px 0; padding-left: 18px; position: relative; line-height: 1.5; }
  .thesis-card.bull li::before { content: "▲"; position: absolute; left: 0; color: #3fb950; font-size: 9px; }
  .thesis-card.bear li::before { content: "▼"; position: absolute; left: 0; color: #f85149; font-size: 9px; }
  .rating-hero { text-align: center; padding: 24px; background: rgba(31,111,235,0.08); border: 1px solid rgba(31,111,235,0.25); border-radius: 12px; margin-bottom: 20px; }
  .rating-hero .rating-badge { display: inline-block; font-size: 28px; font-weight: 800; padding: 8px 32px; border-radius: 10px; margin-bottom: 10px; }
  .rating-badge.buy { background: rgba(63,185,80,0.15); color: #3fb950; border: 1px solid rgba(63,185,80,0.3); }
  .rating-badge.hold { background: rgba(210,153,34,0.15); color: #d29922; border: 1px solid rgba(210,153,34,0.3); }
  .rating-badge.avoid { background: rgba(248,81,73,0.15); color: #f85149; border: 1px solid rgba(248,81,73,0.3); }
  .score-grid { display:grid; grid-template-columns: repeat(5,1fr); gap:10px; margin-top:14px; }
  .score-box { background:#1c2128; border:1px solid #2d333b; border-radius:8px; padding:10px 12px; text-align:center; }
  .score-box .label { font-size:11px; color:#8b949e; }
  .score-box .value { font-size:18px; font-weight:800; color:#f0f6fc; font-family:'SF Mono', monospace; }
  .rating-target { font-size: 14px; color: #8b949e; margin-top: 6px; }
  .rating-target span { color: #58a6ff; font-weight: 700; }
  .risk-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
  .risk-item { background: #1c2128; border: 1px solid #2d333b; border-radius: 8px; padding: 12px 14px; display: flex; gap: 10px; align-items: flex-start; }
  .risk-icon { font-size: 16px; flex-shrink: 0; margin-top: 1px; }
  .risk-text { font-size: 12px; color: #e6edf3; line-height: 1.5; }
  .risk-text strong { color: #f85149; }
  .footer { text-align: center; padding: 20px; font-size: 11px; color: #6e7681; border-top: 1px solid #21262d; margin-top: 24px; }
  .footer a { color: #58a6ff; text-decoration: none; }
  .divider { border: none; border-top: 1px solid #21262d; margin: 16px 0; }
  .highlight-cell { background: rgba(31,111,235,0.1); }
  .growth-positive { color: #3fb950; font-weight: 600; } .growth-negative { color: #f85149; font-weight: 600; }'''

# 注意：这里用 ''' + var + ''' 拼接而非 f-string，彻底避免嵌套问题
html = '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
html += '<title>' + TICKER + ' 每日投资日报 — ' + REPORT_DATE + '</title>\n'
html += '<style>\n' + CSS + '\n</style>\n</head>\n<body>\n<div class="container">\n\n'

# 头部
html += '  <!-- 头部 -->\n  <div class="header">\n'
html += '    <div class="logo">' + TICKER + '</div>\n'
html += '    <div class="header-info">\n'
html += '      <h1>' + LONG_NAME + '</h1>\n'
subtitle_parts = [EXCHANGE + ': ' + TICKER, instrument_type]
if instrument_type == 'EQUITY':
    subtitle_parts.extend([SECTOR, emp_str])
elif SECTOR and SECTOR != '—':
    subtitle_parts.append(SECTOR)
html += '      <div class="subtitle">' + ' · '.join(subtitle_parts) + '</div>\n'
html += '    </div>\n    <div class="header-badge">\n'
html += '      <div class="badge-price" style="color:' + price_col + '">' + format_price(last_str, CURRENCY) + '</div>\n'
html += '      <div class="badge-change ' + chg_cls + '">' + chg_arrow + ' ' + chg_str + ' (' + pct_str + '%)</div>\n'
html += '      <div class="badge-date">' + REPORT_DATE + ' 生成' + (' · 数据截止 ' + DATA_END if DATA_END else '') + '</div>\n'
html += '    </div>\n  </div>\n\n'

# KPI：不适用的基本面/分析师字段显示为真正的 N/A，而不是数值 0。
html += '  <!-- KPI 概览 -->\n  <div class="kpi-grid">\n'
html += '    <div class="kpi-card"><div class="kpi-label">52周区间</div><div class="kpi-value">' + format_price(fw52lo_str, CURRENCY) + ' – ' + format_price(fw52hi_str, CURRENCY) + '</div><div class="kpi-sub">当前位于 ' + pct52_str + '% 分位</div></div>\n'
if instrument_type == 'EQUITY':
    html += '    <div class="kpi-card"><div class="kpi-label">总市值</div><div class="kpi-value">' + format_price(mcap_str, CURRENCY) + '</div><div class="kpi-sub">远期 PE ' + fwpe_str + 'x · TTM PE ' + ttmpe_str + 'x</div></div>\n'
    html += '    <div class="kpi-card"><div class="kpi-label">分析师目标价</div><div class="kpi-value up">' + format_price(tgt_mean_str, CURRENCY) + '</div><div class="kpi-sub">潜在涨幅 ' + tgt_up_str + '% · ' + ana_cnt_str + '位分析师</div></div>\n'
elif instrument_type == 'ETF':
    valuation_bits = []
    if FW_PE > 0: valuation_bits.append('Forward PE ' + fwpe_str + 'x')
    if TTM_PE > 0: valuation_bits.append('TTM PE ' + ttmpe_str + 'x')
    if d.get('PB_RATIO', 0): valuation_bits.append('PB ' + fs(float(d.get('PB_RATIO')), 2) + 'x')
    html += '    <div class="kpi-card"><div class="kpi-label">组合估值</div><div class="kpi-value">' + (' · '.join(valuation_bits) if valuation_bits else 'N/A') + '</div><div class="kpi-sub">ETF 不使用个股分析师目标价评分</div></div>\n'
    html += '    <div class="kpi-card"><div class="kpi-label">风险概览</div><div class="kpi-value">波动率 ' + realized_vol_str + '</div><div class="kpi-sub">63日最大回撤 ' + max_drawdown_str + ' · ATR占比 ' + atr_pct_str + '</div></div>\n'
else:
    html += '    <div class="kpi-card"><div class="kpi-label">风险概览</div><div class="kpi-value">波动率 ' + realized_vol_str + '</div><div class="kpi-sub">63日最大回撤 ' + max_drawdown_str + ' · ATR占比 ' + atr_pct_str + '</div></div>\n'
    html += '    <div class="kpi-card"><div class="kpi-label">成交活跃度</div><div class="kpi-value">量比 ' + vol_ratio_str + '</div><div class="kpi-sub">估值与分析师评分：N/A</div></div>\n'
html += '    <div class="kpi-card"><div class="kpi-label">今日行情</div><div class="kpi-value">开 ' + format_price(open_str, CURRENCY) + ' / 高 ' + format_price(high_str, CURRENCY) + ' / 低 ' + format_price(low_str, CURRENCY) + '</div><div class="kpi-sub">成交量 ' + vol_str + '</div></div>\n'
html += '  </div>\n\n'

# 技术面
html += '  <!-- 技术面分析 -->\n  <div class="section">\n'
html += '    <div class="section-title"><span class="icon">📊</span> 技术面分析</div>\n\n'
html += '    <!-- K线图 -->\n    <div class="chart-container">\n'
html += '      <div style="font-size:13px; color:#8b949e; margin-bottom:12px;">📈 近' + str(MONTHS) + '个月K线图（含均线、布林带、成交量、MACD、RSI、KDJ）</div>\n'
# chart_html is the only trusted HTML fragment: it is generated locally by
# scripts/gen_chart.py in the isolated report run directory. All other text
# interpolated into this document is escaped or allowlisted above.
html += chart_html + '\n    </div>\n\n'

# 技术指标卡片
html += '    <div class="tech-grid" style="margin-top:20px;">\n'
html += '      <div class="tech-card">\n        <h4>📈 均线系统（精确值）</h4>\n'
html += '        <div class="tech-item"><span class="tech-key">当前价 ' + format_price(last_str, CURRENCY) + '</span><span class="tech-val">—</span></div>\n'
for label, val_s, sig, cls in [
    ('MA5', ma5_str, ma5_sig, ma5_cls),
    ('MA10', ma10_str, ma10_sig, ma10_cls),
    ('MA20', ma20_str, ma20_sig, ma20_cls),
    ('MA50', ma50_str, ma50_sig, ma50_cls),
    ('MA120', ma120_str, ma120_sig, ma120_cls),
    ('MA200', ma200_str, ma200_sig, ma200_cls),
]:
    html += '        <div class="tech-item"><span class="tech-key">' + label + ' ' + val_s + '</span><span class="tech-val ' + cls + '">' + sig + '</span></div>\n'
html += '        <div class="tech-item" style="border-top:1px solid #30363d; padding-top:10px; margin-top:4px;">'
html += '<span class="tech-key"><strong>均线信号</strong></span><span class="tech-val ' + ma_overall_cls + '">' + ma_overall + '</span></div>\n'
html += '      </div>\n'

html += '      <div class="tech-card">\n        <h4>📉 技术指标（精确值）</h4>\n'
html += '        <div class="tech-item"><span class="tech-key">RSI(14)</span><span class="tech-val ' + rsi_cls + '">' + rsi_str + ' · ' + rsi_sig + '</span></div>\n'
html += '        <div class="tech-item"><span class="tech-key">MACD线</span><span class="tech-val ' + macd_cls + '">' + macd_str + '</span></div>\n'
html += '        <div class="tech-item"><span class="tech-key">信号线</span><span class="tech-val">' + signal_str + '</span></div>\n'
html += '        <div class="tech-item"><span class="tech-key">MACD柱</span><span class="tech-val ' + macd_cls + '">' + hist_str + ' · ' + macd_sig + '</span></div>\n'
html += '        <div class="tech-item"><span class="tech-key">K值(9,3,3)</span><span class="tech-val">' + k_str + '</span></div>\n'
html += '        <div class="tech-item"><span class="tech-key">D值(9,3,3)</span><span class="tech-val">' + d_str + '</span></div>\n'
html += '        <div class="tech-item"><span class="tech-key">J值(9,3,3)</span><span class="tech-val ' + kdj_cls + '">' + j_str + ' · ' + kdj_sig + '</span></div>\n'
html += '        <div class="tech-item"><span class="tech-key">ATR(14)</span><span class="tech-val">' + format_price(atr_str, CURRENCY) + '</span></div>\n'
html += '        <div class="tech-item" style="border-top:1px solid #30363d; padding-top:10px; margin-top:4px;">'
html += '<span class="tech-key"><strong>技术综合</strong></span><span class="tech-val ' + ma_overall_cls + '">' + ma_overall + '</span></div>\n'
html += '      </div>\n    </div>\n\n'

# v5.8 筹码峰 / Volume Profile 摘要
if chip_ok:
    html += '    <div class="tech-card" style="margin-top:16px;">\n      <h4>🧩 筹码峰 / Volume Profile（126日近似）</h4>\n'
    html += '      <div class="tech-grid">\n'
    html += '        <div class="tech-item"><span class="tech-key">POC 主筹码峰</span><span class="tech-val">' + format_price(chip_poc_str, CURRENCY) + ' (' + chip_dist_str + ')</span></div>\n'
    html += '        <div class="tech-item"><span class="tech-key">70%价值区间</span><span class="tech-val">' + format_price(chip_va_str, CURRENCY) + '</span></div>\n'
    html += '        <div class="tech-item"><span class="tech-key">上方筹码占比</span><span class="tech-val signal-bear">' + chip_overhead_str + '</span></div>\n'
    html += '        <div class="tech-item"><span class="tech-key">下方支撑占比</span><span class="tech-val signal-bull">' + chip_support_str + '</span></div>\n'
    html += '      </div>\n'
    html += '      <div style="margin-top:10px; font-size:12px; color:#8b949e;">chip_score ' + chip_score_str + ' · ' + chip_signal + '。该指标基于 yfinance 日线 OHLCV 的成交量价格分布近似，不等同于券商逐笔真实筹码。</div>\n'
    html += '    </div>\n\n'

# 布林带 + 价位标尺
html += '    <table style="margin-top:18px;">\n'
html += '      <thead><tr><th>布林带（20, 2σ）</th><th class="num">上轨</th><th class="num">中轨（MA20）</th><th class="num">下轨</th><th>BB分位</th></tr></thead>\n'
html += '      <tbody><tr><td>当前值</td>'
html += '<td class="num">' + format_price(bb_up_str, CURRENCY) + '</td>'
html += '<td class="num">' + format_price(bb_mid_str, CURRENCY) + '</td>'
html += '<td class="num">' + format_price(bb_dn_str, CURRENCY) + '</td>'
bb_pct_str = f"{d.get('bb_pct', 50.0):.1f}"
html += '<td>' + bb_pct_str + '% (0%=下轨)</td>'
html += '</tr></tbody></table>\n\n'

# 价位标尺
html += '    <div class="price-ruler">\n'
html += '      <div style="font-size:13px; color:#8b949e; margin-bottom:4px;">📍 关键价位分布（52周区间）</div>\n'
html += '      <div class="ruler-bar-bg">\n'
html += '        <div class="ruler-bar-fill" style="width:' + pct52_str + '%">\n'
html += '          <div class="ruler-dot low" style="left:0%"></div>\n'
html += '          <div class="ruler-dot current" style="left:' + pct52_str + '%"></div>\n'
html += '          <div class="ruler-dot high" style="left:100%"></div>\n'
html += '        </div>\n      </div>\n'
html += '      <div class="ruler-labels">\n'
html += '        <span class="val">' + format_price(fw52lo_str, CURRENCY) + '<br><span style="color:#3fb950;">52周低点</span></span>\n'
html += '        <span class="val" style="color:#58a6ff;">' + format_price(last_str, CURRENCY) + ' 当前价</span>\n'
html += '        <span class="val" style="text-align:right;">' + format_price(fw52hi_str, CURRENCY) + '<br><span style="color:#f85149;">52周高点</span></span>\n'
html += '      </div>\n    </div>\n  </div>\n\n'

# 基本面（仅在有数据时展示，加密货币会较少）
html += '  <!-- 基本面（概要） -->\n  <div class="section">\n'
html += '    <div class="section-title"><span class="icon">📑</span> 基本面概要</div>\n'
html += '    <table>\n      <thead><tr><th>指标</th><th class="num">数值</th><th>说明</th></tr></thead>\n      <tbody>\n'
html += '        <tr><td>标的类型</td><td class="num">' + instrument_type + '</td><td>评分模型按标的类型自动切换</td></tr>\n'
if instrument_type in {'EQUITY', 'ETF'}:
    html += '        <tr><td>Beta（市场敏感度）</td><td class="num">' + beta_str + '</td><td>' + ('高波动' if BETA > 1.5 else ('中等' if BETA > 0.8 else '低波动')) + '</td></tr>\n'
    html += '        <tr><td>股息收益率</td><td class="num">' + div_str + '</td><td>' + ('正股息' if DIV_YIELD > 0 else '无/未获取') + '</td></tr>\n'
if instrument_type == 'EQUITY':
    html += '        <tr><td>目标价区间</td><td class="num">' + format_price(tgt_lo_str, CURRENCY) + ' – ' + format_price(tgt_hi_str, CURRENCY) + '</td><td>' + ana_cnt_str + '位分析师覆盖</td></tr>\n'
if desc_short:
    html += '        <tr><td colspan="3" style="color:#8b949e; font-size:12px; line-height:1.6;">' + desc_short + '</td></tr>\n'
html += '      </tbody>\n    </table>\n'
html += '    <div style="margin-top:14px; padding:12px 16px; background:rgba(31,111,235,0.06); border:1px solid rgba(31,111,235,0.2); border-radius:8px; font-size:13px; color:#8b949e;">\n'
if instrument_type in {'EQUITY', 'ETF'}:
    html += '      💡 <strong style="color:#58a6ff;">提示：</strong>此区块优先展示 StockAnalysis 估值数据（适用时），并以 yfinance 补充行情与技术数据。'
else:
    html += '      💡 <strong style="color:#58a6ff;">提示：</strong>指数和加密货币不使用个股估值/分析师评分；行情、成交量、筹码峰和技术指标来自 yfinance。'
html += '消息面由结构化检索证据生成，并在评分审计文件中记录输入与适用性。\n'
html += '    </div>\n  </div>\n\n'

# 消息面
html += '  <!-- 消息面 -->\n  <div class="section">\n'
html += '    <div class="section-title"><span class="icon">📰</span> 消息面资讯</div>\n'
html += '    <div class="news-list">\n' + news_html + '\n    </div>\n  </div>\n\n'

# 综合研判
html += '  <!-- 综合研判 -->\n  <div class="section">\n'
html += '    <div class="section-title"><span class="icon">🎯</span> 综合研判 & 操作建议（v5.8 标的自适应多因子评分）</div>\n'
html += '    <div class="rating-hero">\n'
html += '      <div class="rating-badge ' + rating_cls + '">' + rating_text + '</div>\n'
html += '      <div style="font-size:28px; font-weight:800; color:#58a6ff; margin-top:8px;">' + final_score_str + '<span style="font-size:14px;color:#8b949e;"> / 100</span></div>\n'
html += '      <div style="font-size:14px; color:#e6edf3; margin-top:10px;">'
html += '当前价 ' + format_price(last_str, CURRENCY) + ' · 标的类型 ' + instrument_type
if instrument_type == 'EQUITY' and TARGET_MEAN > 0:
    html += ' · 分析师目标价 ' + format_price(tgt_mean_str, CURRENCY) + ' · 潜在涨幅 <strong style="color:#3fb950;">' + tgt_up_str + '%</strong>'
html += '</div>\n'
html += '      <div class="rating-target">评分方法：' + rating_method + ' ｜ 技术多头 ' + str(signals_bull) + '/5 ｜ 均线多头数 ' + str(bull_count) + '/6</div>\n'
html += '      <div class="score-grid">\n'
for key, label in [('technical_score','技术'), ('news_score','消息'), ('valuation_score','估值'), ('analyst_score','分析师'), ('risk_score','风险')]:
    raw_val = rating_subscores.get(key)
    val_text = f"{float(raw_val):.0f}" if raw_val is not None else 'N/A'
    status = escape_text(rating_status.get(key, '') or '')
    eff_weight = rating_effective_weights.get(key)
    weight_text = f" · 权重 {float(eff_weight)*100:.0f}%" if eff_weight is not None else ''
    status_html = '<div style="font-size:10px;color:#8b949e;margin-top:2px;">' + status + weight_text + '</div>' if (status or weight_text) else ''
    html += '        <div class="score-box"><div class="label">' + label + '</div><div class="value">' + val_text + '</div>' + status_html + '</div>\n'
html += '      </div>\n'
html += '    </div>\n'
html += '    <div style="padding:14px 18px; background:rgba(31,111,235,0.06); border:1px solid rgba(31,111,235,0.2); border-radius:8px; font-size:13px; color:#e6edf3; line-height:1.8;">\n'
html += '      <strong style="color:#58a6ff;">📌 技术面：</strong>RSI(' + rsi_str + ') ' + rsi_sig + '；MACD ' + macd_sig + '（柱 ' + hist_str + '）；KDJ ' + kdj_sig + '；均线系统：' + ma_overall + '。\n'
if chip_ok:
    html += '      <br><strong style="color:#58a6ff;">🧩 筹码峰：</strong>126日 POC ' + format_price(chip_poc_str, CURRENCY) + '，上方筹码 ' + chip_overhead_str + '，下方支撑 ' + chip_support_str + '，chip_score ' + chip_score_str + '。\n'
html += '      <br><strong style="color:#d29922;">⚠️ 注意：</strong>v5.8 会按 EQUITY / ETF / INDEX / CRYPTO 切换适用评分项；N/A 或缺失项不会填充中性50分，而是重新归一化有效权重。\n'
html += '    </div>\n  </div>\n\n'

# 页脚
html += '  <!-- 页脚 -->\n  <div class="footer">\n'
html += '    <div>📊 ' + TICKER + ' ' + LONG_NAME + ' 每日投资日报 — ' + REPORT_DATE + (' (数据截止 ' + DATA_END + ')' if DATA_END else '') + '</div>\n'
html += '    <div style="margin-top:6px;">数据来源：yfinance · StockAnalysis.com（适用时）· Serper/DashScope/SearXNG 金融搜索</div>\n'
html += '    <div style="margin-top:8px; color:#f85149;">⚠️ 本报告仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。</div>\n'
html += '  </div>\n\n</div>\n</body>\n</html>'

with open(OUT_FILE, 'w', encoding='utf-8') as f:
    f.write(html)

print(f"[OK] 报告已生成: {OUT_FILE}")
print(f"     文件大小: {len(html):,} 字节")
