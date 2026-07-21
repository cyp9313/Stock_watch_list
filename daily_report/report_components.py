# -*- coding: utf-8 -*-
"""共享报告渲染组件（Portfolio 与个股日报统一视觉）。

所有动态文本均经过 ``html.escape``；颜色 / class 来自 report_theme 的 allowlist。
复用 report_i18n 的中文标签与格式化助手。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import html

from .report_theme import REPORT_CSS, COLOR_TOKENS, ACTION_COLORS, IMPACT_COLORS
from .report_i18n import (
    action_zh, risk_level_zh, severity_zh, impact_zh, horizon_zh,
    portfolio_stance_zh, risk_profile_zh, investment_horizon_zh,
    format_number, format_ratio_as_pct, format_pct_value,
)


@dataclass(frozen=True)
class SafeHtml:
    """修改计划第三轮 35：仅允许内部组件生成的 Badge 等 HTML 不经转义输出。

    用户数据与 AI 文本仍必须走 ``esc``。
    """
    html: str


def esc(value: Any) -> str:
    if isinstance(value, SafeHtml):
        return value.html
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
    report_title: str | None = None,
) -> str:
    pills = [
        f'<span class="pill brand">基础货币 {esc(base_currency)}</span>',
        f'<span class="pill">基准 {esc(benchmark)}</span>',
        f'<span class="pill">风险偏好 {esc(risk_profile_zh(risk_profile))}</span>',
        f'<span class="pill">投资期限 {esc(investment_horizon_zh(investment_horizon))}</span>',
        f'<span class="pill warn">风险等级 {esc(risk_level_zh(risk_level))}</span>',
        f'<span class="pill info">组合态度 {esc(portfolio_stance_zh(stance))}</span>',
    ]
    return (
        '<div class="header">\n'
        '  <div class="logo">SWL</div>\n'
        '  <div class="header-info">\n'
        f'    <h1>{esc(report_title or "AI 投资组合分析报告")}</h1>\n'
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


def render_table(headers: list[str], rows: list[list[Any]], scroll: bool = True) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body_rows = []
    for r in rows:
        cells = "".join(f"<td>{_cell(c)}</td>" for c in r)
        body_rows.append(f"<tr>{cells}</tr>")
    wrapper = '<div class="scroll">' if scroll else ''
    close = '</div>' if scroll else ''
    return (
        f'{wrapper}<table><thead><tr>{head}</tr></thead>\n'
        f'<tbody>{"".join(body_rows)}</tbody></table>{close}\n'
    )


def _cell(value: Any) -> str:
    """单元格渲染：SafeHtml 直接输出（§35），其余统一转义。"""
    if isinstance(value, SafeHtml):
        return value.html
    return esc(value)


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


def render_action_summary_table(
    actions: list[dict[str, Any]],
    risk_contribution_by_ticker: dict[str, float] | None = None,
    *,
    observation_only: bool = False,
) -> str:
    headers = (
        ["标的", "状态", "综合风险优先级", "当前权重", "风险贡献", "置信度"]
        if observation_only
        else ["标的", "建议", "优先级", "当前权重", "目标区间", "风险贡献", "置信度"]
    )
    rc_map = risk_contribution_by_ticker or {}
    rows = []
    for a in actions:
        color = ACTION_COLORS.get(a.get("action", "watch"), COLOR_TOKENS["muted"])
        rng = f"{format_ratio_as_pct(a.get('target_weight_min'))} – {format_ratio_as_pct(a.get('target_weight_max'))}"
        rc = rc_map.get(a.get("ticker"))
        row = [
            a.get("ticker", ""),
            SafeHtml(f'<span class="badge" style="background:{color}22;color:{color};">{esc(action_zh(a.get("action")))}</span>'),
            a.get("priority", ""),
            format_ratio_as_pct(a.get("current_weight")),
        ]
        if not observation_only:
            row.append(rng)
        row.extend([
            format_ratio_as_pct(rc) if rc is not None else "—",
            format_ratio_as_pct(a.get("confidence")),
        ])
        rows.append(row)
    return render_table(headers, rows)


def render_action_detail(
    a: dict[str, Any],
    risk_contribution: Any = None,
    ticker_metrics: dict[str, Any] | None = None,
    *,
    observation_only: bool = False,
) -> str:
    action = a.get("action", "watch")
    color = ACTION_COLORS.get(action, COLOR_TOKENS["muted"])
    rng = f"{format_ratio_as_pct(a.get('target_weight_min'))} – {format_ratio_as_pct(a.get('target_weight_max'))}"
    timing = str(a.get("action_timing") or "act_now")
    timing_zh = {
        "act_now": "立即执行", "conditional": "条件触发", "monitor": "持续观察",
        "trim_on_rebound": "反弹减仓", "reduce_on_breakdown": "跌破后减仓",
    }.get(timing, "持续观察")
    if observation_only:
        grid = [
            {"k": "当前权重", "v": format_ratio_as_pct(a.get("current_weight"))},
            {"k": "风险贡献", "v": format_ratio_as_pct(risk_contribution if risk_contribution is not None else a.get("risk_contribution"))},
            {"k": "综合风险优先级", "v": str(a.get("priority") or "—")},
            {"k": "决策置信度", "v": format_ratio_as_pct(a.get("confidence"))},
        ]
    else:
        grid = [
            {"k": "当前权重", "v": format_ratio_as_pct(a.get("current_weight"))},
            {"k": "目标区间", "v": rng},
            {"k": "风险贡献", "v": format_ratio_as_pct(risk_contribution if risk_contribution is not None else a.get("risk_contribution"))},
            {"k": "置信度", "v": format_ratio_as_pct(a.get("confidence"))},
        ]
    grid_html = "".join(
        f'<div class="cell"><div class="k">{esc(c["k"])}</div><div class="v">{esc(c["v"])}</div></div>'
        for c in grid
    )
    # 修改计划第三轮 26：执行/取消/进一步减仓/观察 四组语义明确分离。
    execute = "".join(f"<li>{esc(t)}</li>" for t in (a.get("execute_if") or []))
    cancel = "".join(f"<li>{esc(t)}</li>" for t in (a.get("cancel_or_upgrade_if") or []))
    further = "".join(f"<li>{esc(t)}</li>" for t in (a.get("further_reduce_if") or []))
    monitor = "".join(f"<li>{esc(t)}</li>" for t in (a.get("monitoring_items") or []))
    # 修改计划第三轮 27：阈值必须标注来源/是否为情景假设。
    th_html = ""
    metric_labels = {
        "uranium_price": "铀现货价格", "us_10y_yield": "美国10年期国债收益率",
        "btc_price": "比特币价格", "risk_contribution": "风险贡献", "weight": "权重",
        "annualized_volatility": "年化波动率", "price_vs_ema20_pct": "价格相对EMA20偏离",
        "price_vs_ema50_pct": "价格相对EMA50偏离", "price_vs_ema200_pct": "价格相对EMA200偏离",
        "max_drawdown_63d": "63日最大回撤", "max_drawdown_252d": "252日最大回撤",
        "distance_from_52w_high": "距52周高点",
    }
    for th in (a.get("thresholds") or []):
        basis = str(th.get("basis") or "scenario_assumption")
        basis_label = {
            "evidence": "依据证据", "user_constraint": "用户约束", "scenario_assumption": "情景阈值，非市场一致预期",
        }.get(basis, "情景阈值，非市场一致预期")
        val = th.get("value")
        val_str = f"{val}" if val is not None else "—"
        th_note = str(th.get("note") or "")
        # §29: 去重 - 若 note 与 basis_label 内容相同，不重复显示
        if th_note.strip() == basis_label.strip():
            th_note = ""
        th_html += (
            f'<li><b>{esc(metric_labels.get(str(th.get("metric") or ""), str(th.get("metric") or "")))}</b> = {esc(val_str)} '
            f'<span class="chip">{esc(basis_label)}</span>'
            f'{("（" + esc(th_note) + "）") if th_note else ""}</li>'
        )
    # 修改计划第三轮 21：metric_evidence 由确定性数据渲染，避免模型复制数字出错。
    me_html = ""
    tm = ticker_metrics or {}
    for me in (a.get("metric_evidence") or []):
        metric = str(me.get("metric") or "")
        tk = str(me.get("ticker") or a.get("ticker") or "")
        val = tm.get(metric)
        if val is None:
            val_disp = "—"
        elif metric in ("weight", "risk_contribution", "risk_weight_gap"):
            val_disp = format_ratio_as_pct(val)
        elif metric in ("profit_loss_pct", "rsi"):
            val_disp = format_number(val)
        else:
            val_disp = format_pct_value(val)
        me_html += f'<li>{esc(tk)} · {esc(metric_labels.get(metric, metric))}：{esc(val_disp)}</li>'
    metric_evidence_html = ""
    if me_html:
        metric_evidence_html = (
            '<div class="reason-block"><h5>指标证据（确定性数据）</h5><ul class="trigger-list">'
            + me_html
            + "</ul></div>"
        )
    eids = "".join(f'<span class="chip">{esc(e)}</span>' for e in (a.get("evidence_ids") or []))
    epr = a.get("expected_portfolio_risk_reduction")
    epr_str = format_ratio_as_pct(epr) if epr is not None else "—"
    risk_change = a.get("expected_risk_change") or {}
    risk_change_html = ""
    if epr is not None:
        risk_change_html = (
            f'<span class="kpi-sub">Python 风险估算：方差降低 {esc(epr_str)}；'
            f'年化波动 {esc(format_pct_value(risk_change.get("current_annualized_volatility")))} → '
            f'{esc(format_pct_value(risk_change.get("new_annualized_volatility")))}</span>'
        )
    if observation_only:
        upgrade = cancel or execute
        if not upgrade:
            upgrade = "<li>获得新的 AI 综合分析并满足量化触发条件后再重新评估。</li>"
        return (
            f'<div class="action-detail" style="border-top-color:{color};">\n'
            f'  <div class="action-head">\n'
            f'    <span class="ticker">{esc(a.get("ticker", ""))}</span>\n'
            f'    <span class="action-name" style="background:{color}22;color:{color};">{esc(action_zh(action))}</span>\n'
            f'    <span class="kpi-sub">综合风险优先级 {esc(a.get("priority", ""))}</span>\n'
            f'    <span class="kpi-sub">状态：持续观察</span>\n'
            f'  </div>\n'
            f'  <div class="action-grid">{grid_html}</div>\n'
            f'  <div class="reason-block">\n'
            f'    <h5>列入观察的量化原因</h5><p>{esc(a.get("portfolio_reason") or a.get("reason") or "—")}</p>\n'
            f'    <h5>当前技术状态</h5><p>{esc(a.get("technical_reason") or "—")}</p>\n'
            f'    <h5>研究证据状态</h5><p>{esc(a.get("news_reason") or "本轮没有通过质量门的事件证据。")}</p>\n'
            f'  </div>\n'
            f'  <div class="reason-block"><h5>升级为可操作建议所需条件</h5><ul class="trigger-list">{upgrade}</ul></div>\n'
            f'  <div class="reason-block"><h5>持续监控指标</h5><ul class="trigger-list">{monitor or "<li>风险贡献、回撤与关键均线位置</li>"}</ul></div>\n'
            f'  {metric_evidence_html}\n'
            f'</div>\n'
        )
    return (
        f'<div class="action-detail" style="border-top-color:{color};">\n'
        f'  <div class="action-head">\n'
        f'    <span class="ticker">{esc(a.get("ticker", ""))}</span>\n'
        f'    <span class="action-name" style="background:{color}22;color:{color};">{esc(action_zh(action))}</span>\n'
        f'    <span class="kpi-sub">优先级 {esc(a.get("priority", ""))}</span>\n'
        f'    <span class="kpi-sub">执行时机：{esc(timing_zh)}</span>\n'
        f'    {risk_change_html}\n'
        f'  </div>\n'
        f'  <div class="action-grid">{grid_html}</div>\n'
        f'  <div class="reason-block">\n'
        f'    <h5>组合层面理由</h5><p>{esc(a.get("portfolio_reason") or "—")}</p>\n'
        f'    <h5>技术面理由</h5><p>{esc(a.get("technical_reason") or "—")}</p>\n'
        f'    <h5>消息面理由</h5><p>{esc(a.get("news_reason") or "—")}</p>\n'
        f'    <h5>多头情景</h5><p>{esc(a.get("bull_case") or "—")}</p>\n'
        f'    <h5>空头情景</h5><p>{esc(a.get("bear_case") or "—")}</p>\n'
        f'  </div>\n'
        f'  <div class="reason-block"><h5>执行条件</h5><ul class="trigger-list">{execute or "<li>等待条件确认，不立即执行</li>"}</ul></div>\n'
        f'  <div class="reason-block"><h5>取消或调整条件</h5><ul class="trigger-list">{cancel or "<li>—</li>"}</ul></div>\n'
        f'  <div class="reason-block"><h5>进一步减仓条件</h5><ul class="trigger-list">{further or "<li>—</li>"}</ul></div>\n'
        f'  <div class="reason-block"><h5>持续观察</h5><ul class="trigger-list">{monitor or "<li>—</li>"}</ul></div>\n'
        f'  {("<div class=\"reason-block\"><h5>关键阈值与依据</h5><ul class=\"trigger-list\">" + th_html + "</ul></div>") if th_html else ""}\n'
        f'  {("<div class=\"reason-block\"><h5>指标证据（确定性数据）</h5><ul class=\"trigger-list\">" + me_html + "</ul></div>") if me_html else ""}\n'
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
        if e.get("article_fetch_ok"):
            verification_color = _color("up")
        elif e.get("source_verified"):
            verification_color = COLOR_TOKENS["info"]
        else:
            verification_color = _color("warn")
        tier = str(e.get("source_quality") or "tier_3").replace("_", "-")
        # §28 修复：source_quality 显示为中文标签
        _TIER_DISPLAY = {"tier-1": "一级来源", "tier-2": "二级来源", "tier-3": "三级来源"}
        tier_display = _TIER_DISPLAY.get(tier, tier)
        url = str(e.get("url") or "")
        title_html = (
            f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(e.get("title", ""))}</a>'
            if url else esc(e.get("title", ""))
        )
        # 第六轮第 20 节：verification_level 标签（官方原文/监管文件/主流媒体正文已提取/多源交叉确认/单一来源/搜索摘要）
        verification_level_zh = e.get("verification_level_zh")
        if not verification_level_zh:
            verification_level_zh = (
                "正文已提取" if e.get("article_fetch_ok")
                else "来源 URL 已验证" if e.get("source_verified")
                else "搜索摘要·未验证"
            )
        # §18: 分离 Planned Need 与 Detected Content Type
        content_type = e.get("content_type") or ""
        event_type = e.get("event_type") or e.get("event_hint") or ""
        # 若实际内容为 opinion/forecast 但 event_hint 是 earnings_results，显示为"财报前瞻"而非"财报结果"
        _CONTENT_TYPE_LABELS = {"forecast": "前瞻", "opinion": "分析观点", "news_report": "新闻报道", "press_release": "新闻稿"}
        if content_type in ("opinion", "forecast") and "results" in str(e.get("event_hint") or "") and "preview" not in str(e.get("event_hint") or ""):
            event_type = f"{event_type}_preview"
        event_date = e.get("event_date") or e.get("published_date") or "日期未提供"
        tags = (
            f'<span class="tier-badge {esc(tier)}">{esc(tier_display)}</span>'
            f'<span class="tier-badge" style="background:{verification_color}22;color:{verification_color};">'
            f'{esc(verification_level_zh)}</span>'
            f'<span class="tier-badge" style="background:{imp_color}22;color:{imp_color};">{esc(impact_zh(imp))}</span>'
            f'<span class="tier-badge" style="background:{COLOR_TOKENS["info"]}22;color:{COLOR_TOKENS["info"]};">{esc(horizon_zh(e.get("impact_horizon")))}</span>'
            f'<span class="chip">{esc(e.get("evidence_id") or e.get("reference_id") or "")}</span>'
        )
        if e.get("source_note_only"):
            tags += (
                f'<span class="tier-badge" style="background:{COLOR_TOKENS["muted"]}22;'
                f'color:{COLOR_TOKENS["muted"]};">背景来源·非决策证据</span>'
            )
        # 第六轮第 16 节：materiality 评分标签
        selection_score = e.get("selection_score")
        if selection_score is not None:
            _muted = COLOR_TOKENS.get("muted", "#8b949e")
            tags += f'<span class="tier-badge" style="background:{_muted}22;color:{_muted};">选择分 {esc(str(selection_score))}</span>'
        # 第六轮第 32 节：新事件卡片正文（发生了什么/本轮新增变化/为什么影响标的/为什么影响当前持仓/支持什么建议/不支持什么结论）
        detail_lines: list[str] = []
        if event_type or event_date:
            detail_lines.append(f'<div class="sc-meta"><span>事件类型：{esc(event_type or "—")}</span><span>事件日期：{esc(event_date or "—")}</span></div>')
        detail_lines.append(
            f'<div class="sc-meta"><span>来源：{esc(e.get("source_name", ""))}</span><span>日期：{esc(e.get("published_date") or "日期未提供")}</span>'
            f'<span>关联：{esc(e.get("ticker") or "—")}</span></div>'
        )
        # 优先用第六轮 Decision Summarizer 字段，回退到旧 summary_zh
        what_happened = e.get("what_happened_zh") or e.get("summary_zh") or e.get("title") or ""
        if what_happened:
            detail_lines.append(f'<div class="sc-summary"><strong>发生了什么：</strong>{esc(what_happened)}</div>')
        what_changed = e.get("what_changed_zh")
        if what_changed:
            detail_lines.append(f'<div class="sc-summary"><strong>本轮新增变化：</strong>{esc(what_changed)}</div>')
        why_matters = e.get("why_it_matters_to_ticker_zh") or e.get("relevance_reason")
        if why_matters:
            detail_lines.append(f'<div class="sc-summary"><strong>为什么影响标的：</strong>{esc(why_matters)}</div>')
        portfolio_impact = e.get("portfolio_impact_zh")
        if portfolio_impact:
            detail_lines.append(f'<div class="sc-summary"><strong>为什么影响当前持仓：</strong>{esc(portfolio_impact)}</div>')
        supports_action = e.get("supports_action")
        if supports_action and supports_action != "none":
            action_label = {"reduce": "支持减仓", "trim": "支持小幅减仓", "hold": "支持持有", "watch": "支持观察", "add": "支持加仓"}.get(supports_action, supports_action)
            detail_lines.append(f'<div class="sc-summary"><strong>支持什么建议：</strong>{esc(action_label)}</div>')
        does_not_prove = e.get("does_not_prove_zh") or e.get("does_not_support")
        if does_not_prove:
            detail_lines.append(f'<div class="sc-summary"><strong>不支持什么结论：</strong>{esc(does_not_prove)}</div>')
        detail_html = "\n".join(detail_lines)
        cards.append(
            f'<div class="source-card {imp_cls}">\n'
            f'  <div class="sc-head"><div class="sc-title">{title_html}</div></div>\n'
            f'  {detail_html}\n'
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
        f'  <p>本报告仅供研究参考，不构成投资建议。由 Stock Watch List 自动生成；市场有风险，投资需谨慎。</p>\n'
        f'</div>\n'
    )


def render_chart_container(svg: str, caption: str = "") -> str:
    cap = f'<div class="chart-caption">{esc(caption)}</div>' if caption else ""
    return f'<div class="chart-container">{svg}{cap}</div>\n'


def render_fallback_banner(reason: str) -> str:
    return (
        f'<div class="banner-fallback">\n'
        f'<strong>量化降级报告 · 研究证据不足，已生成量化观察报告</strong><br>\n'
        f'本报告为量化降级报告；以下内容仅基于确定性指标，不包含未经质量门验证的模型判断。{esc(reason)}\n'
        f'</div>\n'
    )
