"""Conservative NAV-only report for open-end mainland funds."""

from __future__ import annotations

from html import escape
import math

from akshare_fallback import fetch_daily


def _pct(current, previous):
    return (current / previous - 1) * 100 if previous and previous > 0 else None


def _fmt(value, suffix=""):
    return "N/A" if value is None or not math.isfinite(float(value)) else f"{value:+.2f}{suffix}"


def generate_fund_nav_report(ticker: str, *, include_telegram_summary=False) -> dict:
    frame = fetch_daily(ticker, period="2y")
    if frame.empty or len(frame) < 2:
        return {"success": False, "error": f"Not enough published unit NAV history for {ticker}."}
    nav = frame["Close"].astype(float)
    latest = float(nav.iloc[-1])
    previous = float(nav.iloc[-2])
    change = _pct(latest, previous)
    as_of = nav.index[-1].date().isoformat()
    windows = ((20, "20 sessions"), (60, "60 sessions"), (120, "120 sessions"), (252, "252 sessions"))
    rows = []
    for sessions, label in windows:
        base = float(nav.iloc[-sessions - 1]) if len(nav) > sessions else None
        rows.append((label, _pct(latest, base)))
    ma20 = float(nav.tail(20).mean()) if len(nav) >= 20 else None
    ma50 = float(nav.tail(50).mean()) if len(nav) >= 50 else None
    ema12 = nav.ewm(span=12, adjust=False).mean()
    ema26 = nav.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    diff = float(macd.iloc[-1] - signal.iloc[-1])
    delta = nav.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = float((100 - 100 / (1 + up / down.replace(0, float("nan")))).iloc[-1])
    code = escape(ticker)
    table_rows = "".join(f"<tr><td>{label}</td><td>{_fmt(value, '%')}</td></tr>" for label, value in rows)
    content = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>{code} 净值报告</title>
<style>body{{font:16px/1.6 system-ui;max-width:850px;margin:30px auto;padding:0 20px;color:#203040}}
h1{{color:#163d68}}table{{border-collapse:collapse;width:100%}}td,th{{padding:9px;border-bottom:1px solid #d9e2ed;text-align:left}}
.note{{background:#fff3d7;padding:12px;border-radius:6px}}</style></head><body>
<h1>{code} 开放式基金单位净值报告</h1><p>数据来源：AkShare / 东方财富；最新公布净值日期：{as_of}</p>
<table><tr><th>指标</th><th>数值</th></tr><tr><td>单位净值</td><td>{latest:.4f}</td></tr>
<tr><td>最近一次净值变化</td><td>{_fmt(change, '%')}</td></tr>{table_rows}
<tr><td>MA20 / MA50</td><td>{f'{ma20:.4f}' if ma20 is not None else 'N/A'} / {f'{ma50:.4f}' if ma50 is not None else 'N/A'}</td></tr>
<tr><td>MACD Diff</td><td>{diff:+.5f}</td></tr><tr><td>RSI(14)</td><td>{_fmt(rsi)}</td></tr></table>
<p class="note">这是基于已公布单位净值的确定性摘要，并非个股 AI 决策报告。基金净值不是盘中股价；
未提供开高低、交易量、筹码峰、估值、投资建议。单位净值涨跌未必包含分红、申赎费用或持有人实际收益。</p>
</body></html>"""
    result = {
        "success": True, "ticker": ticker, "report_date": as_of,
        "file_name": f"{ticker.replace(':', '_')}_nav_report_{as_of}.html",
        "html_bytes": content.encode("utf-8"), "elapsed": 0.0,
        "decision_dashboard": False, "fallback_used": False,
        "warnings": ["NAV-only deterministic report; equity AI scoring and OHLCV analysis are not applicable."],
    }
    if include_telegram_summary:
        result["telegram_messages"] = [
            f"{ticker} 单位净值简报（{as_of}）\n最新净值 {latest:.4f}，最近一次变化 {_fmt(change, '%')}。"
            "\n仅基于已公布净值；无个股评分或交易量分析。完整说明见 HTML 附件。"
        ]
    return result
