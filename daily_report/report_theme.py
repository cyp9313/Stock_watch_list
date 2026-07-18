# -*- coding: utf-8 -*-
"""共享报告视觉层（Portfolio 与个股日报统一风格）。

提取自 ``scripts/build_report.py`` 的暗色主题，避免 Portfolio 报告另起一套
CSS（修改计划 16.1 / 16.2）。个股日报与 Portfolio 日报都引用本模块。
"""
from __future__ import annotations

# 字体栈：优先 Inter，中文回落 Noto Sans SC / 微软雅黑（修改计划 16.5）。
FONT_STACK = (
    "Inter, 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', "
    "'Hiragino Sans GB', 'Segoe UI', Arial, sans-serif"
)
MONO_STACK = "'SF Mono', 'Fira Code', 'JetBrains Mono', Consolas, monospace"

# ── 颜色 token（与个股日报一致）──────────────────────────────
COLOR_TOKENS = {
    "bg": "#0d1117",
    "bg_gradient_top": "#1a2332",
    "card": "#161b22",
    "card_alt": "#1c2128",
    "border": "#2d333b",
    "border_soft": "#21262d",
    "text": "#e6edf3",
    "text_strong": "#f0f6fc",
    "muted": "#8b949e",
    "muted_soft": "#6e7681",
    "brand": "#1f6feb",
    "brand_dark": "#0d3fa6",
    # 颜色语义（项目既有风格：绿涨红跌）
    "up": "#3fb950",
    "down": "#f85149",
    "warn": "#d29922",
    "info": "#58a6ff",
    "neutral": "#8b949e",
}

# 操作动作对应颜色
ACTION_COLORS = {
    "add": "#3fb950",       # 增持 / 绿
    "hold": "#58a6ff",      # 持有 / 蓝
    "trim": "#d29922",      # 适度减仓 / 橙
    "reduce": "#f85149",    # 明显减仓 / 红
    "exit": "#da3633",      # 退出 / 深红
    "watch": "#8b949e",     # 观察 / 灰
}

# 风险等级对应颜色
RISK_COLORS = {
    "low": "#3fb950",
    "medium": "#58a6ff",
    "medium_high": "#d29922",
    "high": "#f85149",
}

# 评分颜色（按分数高低）
SCORE_COLORS = {
    "good": "#3fb950",
    "mid": "#d29922",
    "bad": "#f85149",
}

# 影响方向对应颜色
IMPACT_COLORS = {
    "positive": "#3fb950",
    "negative": "#f85149",
    "neutral": "#d29922",
}


REPORT_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: __FONT_STACK__; background: #0d1117; color: #e6edf3; line-height: 1.6; padding: 20px; }
  .container { max-width: 1180px; margin: 0 auto; }
  .header { background: linear-gradient(135deg, #1a2332 0%, #0d1117 100%); border: 1px solid #2d333b; border-radius: 12px; padding: 28px 32px; margin-bottom: 24px; display: flex; align-items: center; gap: 28px; flex-wrap: wrap; }
  .logo { width: 64px; height: 64px; background: linear-gradient(135deg, #1f6feb 0%, #0d3fa6 100%); border-radius: 14px; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 900; color: #fff; flex-shrink: 0; text-align: center; word-break: break-all; padding: 4px; }
  .header-info h1 { font-size: 22px; font-weight: 700; color: #f0f6fc; }
  .header-info .subtitle { font-size: 13px; color: #8b949e; margin-top: 2px; }
  .header-badge { margin-left: auto; text-align: right; display: flex; gap: 10px; flex-wrap: wrap; }
  .pill { display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; background: #1c2128; border: 1px solid #2d333b; color: #c9d1d9; }
  .pill.brand { background: rgba(31,111,235,0.15); border-color: rgba(31,111,235,0.4); color: #58a6ff; }
  .pill.warn { background: rgba(210,153,34,0.15); border-color: rgba(210,153,34,0.4); color: #d29922; }
  .pill.danger { background: rgba(248,81,73,0.15); border-color: rgba(248,81,73,0.4); color: #f85149; }
  .pill.good { background: rgba(63,185,80,0.15); border-color: rgba(63,185,80,0.4); color: #3fb950; }

  .kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 24px; }
  .kpi-grid.cols-3 { grid-template-columns: repeat(3, 1fr); }
  .kpi-grid.cols-5 { grid-template-columns: repeat(5, 1fr); }
  .kpi-card { background: #161b22; border: 1px solid #2d333b; border-radius: 10px; padding: 16px 18px; }
  .kpi-label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  .kpi-value { font-size: 20px; font-weight: 700; color: #f0f6fc; }
  .kpi-sub { font-size: 11px; color: #8b949e; margin-top: 4px; }
  .kpi-value.up { color: #3fb950; } .kpi-value.down { color: #f85149; } .kpi-value.warn { color: #d29922; }

  .section { background: #161b22; border: 1px solid #2d333b; border-radius: 12px; padding: 24px; margin-bottom: 20px; }
  .section-title { font-size: 16px; font-weight: 700; color: #f0f6fc; margin-bottom: 18px; display: flex; align-items: center; gap: 8px; }
  .section-title .icon { font-size: 18px; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { background: #1c2128; color: #8b949e; font-weight: 600; padding: 10px 12px; text-align: left; border-bottom: 1px solid #2d333b; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; position: sticky; top: 0; }
  td { padding: 10px 12px; border-bottom: 1px solid #21262d; color: #e6edf3; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #1c2128; }
  .num { text-align: right; font-family: __MONO_STACK__; }
  .up { color: #3fb950; } .down { color: #f85149; } .warn { color: #d29922; }

  .badge { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; }
  .badge.up { background: rgba(63,185,80,0.15); color: #3fb950; }
  .badge.down { background: rgba(248,81,73,0.15); color: #f85149; }
  .badge.warn { background: rgba(210,153,34,0.15); color: #d29922; }
  .badge.info { background: rgba(88,166,255,0.15); color: #58a6ff; }
  .badge.muted { background: rgba(139,148,158,0.15); color: #8b949e; }

  .risk-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }
  .risk-card { background: #1c2128; border: 1px solid #2d333b; border-radius: 10px; padding: 16px 18px; border-left: 4px solid #2d333b; }
  .risk-card.sev-high { border-left-color: #f85149; }
  .risk-card.sev-medium { border-left-color: #d29922; }
  .risk-card.sev-low { border-left-color: #58a6ff; }
  .risk-card h4 { font-size: 14px; font-weight: 700; color: #f0f6fc; display: flex; justify-content: space-between; gap: 8px; align-items: center; }
  .risk-card p { font-size: 13px; color: #c9d1d9; margin-top: 8px; line-height: 1.6; }
  .risk-meta { font-size: 11px; color: #8b949e; margin-top: 8px; }
  .chip { display: inline-block; padding: 2px 8px; border-radius: 4px; margin: 2px; font-size: 11px; background: #161b22; border: 1px solid #2d333b; color: #c9d1d9; font-family: __MONO_STACK__; }

  .action-detail { background: #1c2128; border: 1px solid #2d333b; border-radius: 10px; padding: 18px; margin-bottom: 14px; border-top: 3px solid #2d333b; }
  .action-detail .action-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
  .action-detail .ticker { font-size: 18px; font-weight: 800; color: #f0f6fc; font-family: __MONO_STACK__; }
  .action-detail .action-name { font-size: 15px; font-weight: 700; padding: 3px 12px; border-radius: 999px; }
  .action-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 12px; }
  .action-grid .cell { background: #161b22; border: 1px solid #2d333b; border-radius: 8px; padding: 10px; }
  .action-grid .cell .k { font-size: 11px; color: #8b949e; }
  .action-grid .cell .v { font-size: 15px; font-weight: 700; color: #f0f6fc; }
  .reason-block { margin-top: 8px; }
  .reason-block h5 { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin: 10px 0 4px; }
  .reason-block p { font-size: 13px; color: #c9d1d9; line-height: 1.6; }
  .trigger-list { margin: 4px 0 0 0; padding-left: 18px; }
  .trigger-list li { font-size: 13px; color: #c9d1d9; margin: 3px 0; }

  .news-group { margin-bottom: 18px; }
  .news-group h4 { font-size: 15px; font-weight: 700; color: #f0f6fc; margin-bottom: 10px; border-bottom: 1px solid #2d333b; padding-bottom: 6px; }
  .news-list { display: flex; flex-direction: column; gap: 12px; }
  .source-card { background: #1c2128; border: 1px solid #2d333b; border-radius: 10px; padding: 14px 18px; border-left: 4px solid #2d333b; }
  .source-card.imp-positive { border-left-color: #3fb950; }
  .source-card.imp-negative { border-left-color: #f85149; }
  .source-card.imp-neutral { border-left-color: #d29922; }
  .source-card .sc-head { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; flex-wrap: wrap; }
  .source-card .sc-title { font-size: 14px; font-weight: 600; color: #f0f6fc; }
  .source-card .sc-meta { font-size: 11px; color: #6e7681; margin-top: 4px; display: flex; gap: 14px; flex-wrap: wrap; }
  .source-card .sc-summary { font-size: 13px; color: #c9d1d9; margin-top: 8px; line-height: 1.6; }
  .source-card .sc-tags { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; }
  .tier-badge { font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
  .tier-1 { background: rgba(63,185,80,0.15); color: #3fb950; }
  .tier-2 { background: rgba(88,166,255,0.15); color: #58a6ff; }
  .tier-3 { background: rgba(139,148,158,0.15); color: #8b949e; }

  .chart-container { margin: 18px 0; background: #1c2128; border: 1px solid #2d333b; border-radius: 10px; padding: 16px; }
  .chart-container svg { width: 100%; height: auto; display: block; }
  .chart-caption { font-size: 12px; color: #8b949e; margin-top: 8px; }

  .summary-list { list-style: none; padding: 0; }
  .summary-list li { font-size: 14px; color: #c9d1d9; padding: 8px 0 8px 22px; position: relative; line-height: 1.6; border-bottom: 1px solid #21262d; }
  .summary-list li::before { content: "▸"; position: absolute; left: 0; color: #58a6ff; }

  .scroll { overflow-x: auto; }
  .data-cutoff { font-size: 11px; color: #8b949e; background: #161b22; border: 1px solid #2d333b; border-radius: 8px; padding: 10px 14px; margin-bottom: 20px; }
  .evidence-quality { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; }
  .evidence-quality span { font-size: 11px; padding: 4px 10px; border-radius: 999px; background: #1c2128; border: 1px solid #2d333b; color: #c9d1d9; }
  .warn-text { color: #d29922; }
  .footer { text-align: center; padding: 20px; font-size: 11px; color: #6e7681; border-top: 1px solid #21262d; margin-top: 24px; }
  .footer a { color: #58a6ff; text-decoration: none; }
  .divider { border: none; border-top: 1px solid #21262d; margin: 16px 0; }

  .banner-fallback { background: rgba(210,153,34,0.12); border: 1px solid rgba(210,153,34,0.4); color: #d29922; border-radius: 10px; padding: 14px 18px; margin-bottom: 20px; font-size: 13px; }
  .banner-fallback strong { color: #f0f6fc; }

  .score-bar-bg { height: 10px; background: #21262d; border-radius: 6px; position: relative; overflow: hidden; margin-top: 6px; }
  .score-bar-fill { height: 100%; border-radius: 6px; }

  @media (max-width: 860px) {
    .kpi-grid, .kpi-grid.cols-3, .kpi-grid.cols-5, .risk-grid, .action-grid { grid-template-columns: repeat(2, 1fr); }
  }
  @media print {
    body { background: #fff; color: #111; }
    .section, .kpi-card, .risk-card, .source-card, .action-detail, .chart-container { break-inside: avoid; }
    a { color: #1f6feb; }
  }
""".replace("__FONT_STACK__", FONT_STACK).replace("__MONO_STACK__", MONO_STACK)


def _reads_shared_theme(script_name: str, markers: tuple[str, ...]) -> bool:
    """修改计划第三轮 41：源码级检查——个股日报 / Portfolio 报告是否真正导入共享主题。"""
    try:
        from pathlib import Path
        src = Path(__file__).resolve().parent.parent / "scripts" / script_name
        text = src.read_text(encoding="utf-8")
        return all(m in text for m in markers)
    except Exception:
        return False


def portfolio_report_uses_shared_theme() -> bool:
    return _reads_shared_theme(
        "build_portfolio_report.py",
        ("report_theme", "render_section"),
    )


def stock_report_uses_shared_theme() -> bool:
    # 个股日报至少复用共享色彩 token（COLOR_TOKENS）。
    return _reads_shared_theme(
        "build_report.py",
        ("report_theme", "COLOR_TOKENS"),
    )
