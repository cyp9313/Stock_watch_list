# -*- coding: utf-8 -*-
"""共享报告渲染组件（Portfolio 与个股日报统一视觉）。

所有动态文本均经过 ``html.escape``；颜色 / class 来自 report_theme 的 allowlist。
复用 report_i18n 的中文标签与格式化助手。
"""
from __future__ import annotations

from typing import Any

import html

from .report_theme import REPORT_CSS, COLOR_TOKENS, ACTION_COLORS, RISK_COLORS, IMPACT_COLORS
from .report_i18n import (
    action_zh, risk_level_zh, severity_zh, impact_zh, horizon_zh, instrument_type_zh,
    format_money, format_pct, format_number, pct_color_class,
)


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _color(key: str, fallback: str = COLOR_TOKENS["muted"]) -> str:
    return COLOR_TOKENS.get(key, fallback)


def render_html_head(title: str) -> str:
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"<title>{esc(title)}</title>\n"
        f"<style>\n{REPORT_CSS}\n</style>\n</head>\n<body>\n<div class=\"container\">\n"
    )


def render_report_header(
    *,
    portfolio_name: str,
    report_date: str,
    as_of: str,
    base_currency: str,
    benchmark: str,
    risk_profile: str,
    investment_horizon: str,
    risk_level: str,
    stance: str,
) -> str:
    pills = [
        f'<span class="pill brand">基础货币 {esc(base_currency)}</span>',
        f'<span class="pill">基准 {esc(benchmark)}</span>',
        f'<span class="pill">风险偏好 {esc(risk_profile)}</span>',
        f'<span class="pill">投资期限 {esc(investment_horizon)}</span>',
        f'<span class="pill warn">风险等级 {esc(risk_level_zh(risk_level))}</span>',
        f'<span class="pill info">组合态度 {esc(stance)}</span>',
    ]
    return (
        '<div class="header">\n'
        '  <div class="logo">SWL</div>\n'
        '  <div class="header-info">\n'
        f'    <h1>AI 投资组合分析报告</h1>\n'
        f'    <div class="subtitle">{esc(portfolio_name)} · Stock Watch List</div>\n'
        '  </div>\n'
        '  <div class="header-badge">\n'
        f'    <div class="pill">报告日期 {esc(report_date)}</div>\n'
        f'    <div class="pill">数据快照 {esc(as_of)}</div>\n'
        '    ' + "\n    ".join(pills) + '\n'
        '  </div>\n'
        '</div>\n'
    )


def render_kpi_cards(cards: list[dict[str, Any]], cols: int = 4) -> str:
    cls = {3: "cols-3", 5: "cols-5"}.get(cols, "")
    parts = []
    for c in cards:
        val_cls = c.get("value_cls", "")
        parts.append(
            f'<div class="kpi-card">'
            f'<div class="kpi-label">{esc(c.get("label", ""))}</div>'
            f'<div class="kpi-value {val_cls}">{esc(c.get("value", ""))}</div>'
            f'<div class="kpi-sub">{esc(c.get("sub", ""))}</div>'
            f'</div>'
        )
    return f'<div class="kpi-grid {cls}">\n' + "\n".join(parts) + "\n</div>\n"


def render_section(title: str, icon: str, body_html: str) -> str:
    return (
        f'<div class="section">\n'
        f'  <div class="section-title"><span class="icon">{esc(icon)}</span>{esc(title)}</div>\n'
        f'{body_html}\n'
        f'</div>\n'
    )


def render_badge(text: str, kind: str = "muted") -> str:
    return f'<span class="badge {esc(kind)}">{esc(text)}</span>'


def render_score_bar(value: float, color: str) -> str:
    try:
        pct = max(0.0, min(1.0, float(value))) * 100.0
    except (TypeError, ValueError):
        pct = 0.0
    return (
        f'<div class="score-bar-bg"><div class="score-bar-fill" style="width:{pct:.0f}%;background:{esc(color)};"></div></div>'
    )


def render_table(headers: list[str], rows: list[list[Any]], scroll: bool = True) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body_rows = []
    for r in rows:
        cells = "".join(f"<td>{esc(c)}</td>" for c in r)
        body_rows.append(f"<tr>{cells}</tr>")
    wrapper = '<div class="scroll">' if scroll else ''
    close = '</div>' if scroll else ''
    return (
        f'{wrapper}<table><thead><tr>{head}</tr></thead>\n'
        f'<tbody>{"".join(body_rows)}</tbody></table>{close}\n'
    )


def render_risk_cards(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return (
            '<p class="kpi-sub">未发现达到高等级阈值的结构性风险；当前主要观察点为组合分散度与个别高 Beta 持仓的波动贡献。</p>'
        )
    cards = []
    for f in findings:
        sev = str(f.get("severity") or "medium").lower()
        sev_cls = {"high": "sev-high", "medium": "sev-medium", "low": "sev-low"}.get(sev, "sev-medium")
        affected = "".join(f'<span class="chip">{esc(t)}</span>' for t in (f.get("affected_tickers") or [])[:10])
        cards.append(
            f'<div class="risk-card {sev_cls}">'
            f'<h4><span>{esc(f.get("title", ""))}</span>{render_badge(severity_zh(sev), "muted")}</h4>'
            f'<p>{esc(f.get("description", ""))}</p>'
            f'<div class="risk-meta">影响标的：{affected or "—"}</div>'
            f'</div>'
        )
    return f'<div class="risk-grid">\n' + "\n".join(cards) + "\n</div>\n"


def render_action_summary_table(actions: list[dict[str, Any]]) -> str:
    headers = ["标的", "建议", "优先级", "当前权重", "目标区间", "风险贡献", "置信度"]
    rows = []
    rc_map = {a["ticker"]: a for a in actions}
    for a in actions:
        color = ACTION_COLORS.get(a.get("action", "watch"), COLOR_TOKENS["muted"])
        rng = f"{format_pct(a.get('target_weight_min'))} – {format_pct(a.get('target_weight_max'))}"
        rows.append([
            a.get("ticker", ""),
            f'<span class="badge" style="background:{color}22;color:{color};">{esc(action_zh(a.get("action")))}</span>',
            a.get("priority", ""),
            format_pct(a.get("current_weight")),
            rng,
            format_pct(a.get("risk_contribution")),
            format_number(a.get("confidence"), 2),
        ])
    return render_table(headers, rows)


def render_action_detail(a: dict[str, Any], risk_contribution: Any = None) -> str:
    action = a.get("action", "watch")
    color = ACTION_COLORS.get(action, COLOR_TOKENS["muted"])
    rng = f"{format_pct(a.get('target_weight_min'))} – {format_pct(a.get('target_weight_max'))}"
    grid = [
        {"k": "当前权重", "v": format_pct(a.get("current_weight"))},
        {"k": "目标区间", "v": rng},
        {"k": "风险贡献", "v": format_pct(risk_contribution if risk_contribution is not None else a.get("risk_contribution"))},
        {"k": "置信度", "v": format_number(a.get("confidence"), 2)},
    ]
    grid_html = "".join(
        f'<div class="cell"><div class="k">{esc(c["k"])}</div><div class="v">{esc(c["v"])}</div></div>'
        for c in grid
    )
    triggers = "".join(f"<li>{esc(t)}</li>" for t in (a.get("trigger_conditions") or []))
    inval = "".join(f"<li>{esc(t)}</li>" for t in (a.get("invalidation_conditions") or []))
    eids = "".join(f'<span class="chip">{esc(e)}</span>' for e in (a.get("evidence_ids") or []))
    return (
        f'<div class="action-detail" style="border-top-color:{color};">\n'
        f'  <div class="action-head">\n'
        f'    <span class="ticker">{esc(a.get("ticker", ""))}</span>\n'
        f'    <span class="action-name" style="background:{color}22;color:{color};">{esc(action_zh(action))}</span>\n'
        f'    <span class="kpi-sub">优先级 {esc(a.get("priority", ""))}</span>\n'
        f'  </div>\n'
        f'  <div class="action-grid">{grid_html}</div>\n'
        f'  <div class="reason-block">\n'
        f'    <h5>组合层面理由</h5><p>{esc(a.get("portfolio_reason") or "—")}</p>\n'
        f'    <h5>技术面理由</h5><p>{esc(a.get("technical_reason") or "—")}</p>\n'
        f'    <h5>消息面理由</h5><p>{esc(a.get("news_reason") or "—")}</p>\n'
        f'    <h5>多头情景</h5><p>{esc(a.get("bull_case") or "—")}</p>\n'
        f'    <h5>空头情景</h5><p>{esc(a.get("bear_case") or "—")}</p>\n'
        f'  </div>\n'
        f'  <div class="reason-block"><h5>执行触发条件</h5><ul class="trigger-list">{triggers}</ul></div>\n'
        f'  <div class="reason-block"><h5>建议失效条件</h5><ul class="trigger-list">{inval}</ul></div>\n'
        f'  <div class="risk-meta">证据：{eids or "—"}</div>\n'
        f'</div>\n'
    )


def render_news_group(title: str, items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    cards = []
    for e in items:
        imp = str(e.get("impact_direction") or "neutral").lower()
        imp_cls = {"positive": "imp-positive", "negative": "imp-negative", "neutral": "imp-neutral"}.get(imp, "imp-neutral")
        imp_color = IMPACT_COLORS.get(imp, COLOR_TOKENS["warn"])
        tier = str(e.get("source_quality") or "tier_3").replace("_", "-")
        url = str(e.get("url") or "")
        title_html = (
            f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(e.get("title", ""))}</a>'
            if url else esc(e.get("title", ""))
        )
        tags = (
            f'<span class="tier-badge {esc(tier)}">{esc(e.get("source_quality", ""))}</span>'
            f'<span class="tier-badge" style="background:{imp_color}22;color:{imp_color};">{esc(impact_zh(imp))}</span>'
            f'<span class="tier-badge" style="background:{COLOR_TOKENS["info"]}22;color:{COLOR_TOKENS["info"]};">{esc(horizon_zh(e.get("impact_horizon")))}</span>'
            f'<span class="chip">{esc(e.get("evidence_id", ""))}</span>'
        )
        cards.append(
            f'<div class="source-card {imp_cls}">\n'
            f'  <div class="sc-head"><div class="sc-title">{title_html}</div></div>\n'
            f'  <div class="sc-meta"><span>来源：{esc(e.get("source_name", ""))}</span><span>日期：{esc(e.get("published_date", ""))}</span>'
            f'<span>关联：{esc(e.get("ticker") or "—")}</span></div>\n'
            f'  <div class="sc-summary">{esc(e.get("summary_zh") or e.get("title") or "")}</div>\n'
            f'  <div class="sc-tags">{tags}</div>\n'
            f'</div>'
        )
    return (
        f'<div class="news-group"><h4>{esc(title)}</h4>\n'
        f'<div class="news-list">{"".join(cards)}</div></div>\n'
    )


def render_disclaimer(text: str) -> str:
    return (
        f'<div class="footer">\n'
        f'  <p>{esc(text)}</p>\n'
        f'  <p>本报告由 Stock Watch List 自动生成，仅供研究参考，不构成任何投资建议。市场有风险，投资需谨慎。</p>\n'
        f'</div>\n'
    )


def render_chart_container(svg: str, caption: str = "") -> str:
    cap = f'<div class="chart-caption">{esc(caption)}</div>' if caption else ""
    return f'<div class="chart-container">{svg}{cap}</div>\n'


def render_fallback_banner(reason: str) -> str:
    return (
        f'<div class="banner-fallback">\n'
        f'<strong>量化降级报告 · AI 分析未完成</strong><br>\n'
        f'以下内容仅基于确定性指标，不包含模型综合判断。{esc(reason)}\n'
        f'</div>\n'
    )
