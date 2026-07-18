# -*- coding: utf-8 -*-
"""Portfolio 中文 HTML 报告生成器（与个股日报统一视觉）。

对应修改计划 16 / 17：复用 report_theme / report_components / report_i18n，
输出简体中文、与项目个股日报一致的暗色风格报告；包含核心结论、概览卡片、
配置与风险图表、AI 风险诊断、操作建议详情、新闻综合、行业/主题/宏观、技术快照、来源附录。

接口：
    build_html(snapshot, metrics, risk_ranking, advice, evidence, *,
               instrument_metadata=None, settings=None, charts=None,
               risk_findings=None, cumulative_labels=None, fallback_reason="")
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..report_components import (
    render_html_head, render_report_header, render_kpi_cards, render_section,
    render_risk_cards, render_action_summary_table, render_action_detail,
    render_news_group, render_disclaimer, render_chart_container, render_fallback_banner,
    render_table, esc,
)
from ..report_i18n import (
    action_zh, risk_level_zh, format_money, format_pct, format_number, pct_color_class,
)
from ..report_charts import (
    svg_weight_bars, svg_weight_vs_risk, svg_allocation, svg_cumulative_returns,
)


def _load(path: Path, default: Any) -> Any:
    if not path or not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def build_html(
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    risk_ranking: dict[str, Any],
    advice: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    instrument_metadata: dict[str, dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    charts: dict[str, str] | None = None,
    risk_findings: list[dict[str, Any]] | None = None,
    cumulative_labels: list[str] | None = None,
    fallback_reason: str = "",
) -> str:
    settings = settings or {}
    instrument_metadata = instrument_metadata or snapshot.get("instrument_metadata") or {}
    charts = charts or {}
    base = snapshot.get("base_currency", "EUR")
    summary = snapshot.get("summary", {})
    portfolio_name = snapshot.get("portfolio_name", "Portfolio")
    report_date = str(snapshot.get("report_date") or "")
    as_of = str(snapshot.get("as_of") or "")

    risk_level = advice.get("risk_level", "medium")
    stance = advice.get("portfolio_stance", "balanced")
    is_fallback = str(advice.get("report_mode") or advice.get("ai_analysis_available") == False) == "quantitative_fallback" or advice.get("report_mode") == "quantitative_fallback"

    parts: list[str] = [render_html_head(f"{portfolio_name} AI 投资组合分析报告")]

    # 头部
    parts.append(render_report_header(
        portfolio_name=portfolio_name,
        report_date=report_date,
        as_of=as_of,
        base_currency=base,
        benchmark=str(snapshot.get("benchmark") or ""),
        risk_profile=str(settings.get("risk_profile") or "balanced"),
        investment_horizon=str(settings.get("investment_horizon") or "1-3m"),
        risk_level=risk_level,
        stance=stance,
    ))

    if is_fallback:
        parts.append(render_fallback_banner(fallback_reason))

    # ── AI 核心结论（修改计划 17.2）──
    stance_pill = f'<span class="pill info">组合态度：{esc(stance)}</span>'
    risk_pill = f'<span class="pill warn">风险等级：{esc(risk_level_zh(risk_level))}</span>'
    conf_pill = f'<span class="pill">AI 置信度：{format_number(advice.get("confidence"), 2)}</span>'
    core_html = (
        f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;">{stance_pill}{risk_pill}{conf_pill}</div>'
    )
    exec_summary = advice.get("executive_summary") or []
    if exec_summary:
        core_html += '<ul class="summary-list">' + "".join(f"<li>{esc(s)}</li>" for s in exec_summary) + "</ul>"
    else:
        core_html += '<p class="kpi-sub">（AI 未生成核心结论；详见下方量化指标与风险诊断。）</p>'
    parts.append(render_section("AI 核心结论", "🧭", core_html))

    # ── Portfolio 概览卡片（修改计划 17.3）──
    pa = advice.get("portfolio_analysis", {}) or {}
    rr = metrics.get("relative_returns", {}) or {}
    rel_5d = rr.get("5D", {}) if isinstance(rr.get("5D"), dict) else {}
    cards = [
        {"label": "总市值", "value": format_money(summary.get("total_market_value_base"), base), "sub": f"成本 {format_money(summary.get('total_cost_basis_base'), base)}"},
        {"label": "总盈亏", "value": format_money(summary.get("profit_loss_base"), base), "sub": f"{format_pct(summary.get('profit_loss_pct'))}", "value_cls": pct_color_class(summary.get("profit_loss_pct"))},
        {"label": "Top 1 / Top 3", "value": f"{format_pct(metrics.get('top1_weight'))} / {format_pct(metrics.get('top3_weight'))}", "sub": f"HHI×1e4 {format_number(metrics.get('hhi_10000'), 0)}"},
        {"label": "有效持仓数", "value": format_number(metrics.get("effective_holdings"), 1), "sub": f"持仓 {len(snapshot.get('holdings', []))} 只"},
        {"label": "Portfolio Beta", "value": format_number(metrics.get("portfolio_beta"), 2), "sub": f"年化波动 {format_pct(metrics.get('annualized_volatility'))}"},
        {"label": "63D 回撤", "value": format_pct(metrics.get("max_drawdown_63d")), "sub": f"252D {format_pct(metrics.get('max_drawdown_252d'))}", "value_cls": pct_color_class(metrics.get("max_drawdown_63d"))},
        {"label": "相对基准(5D)", "value": format_pct(rel_5d.get("relative")), "sub": f"组合 {format_pct(rel_5d.get('portfolio'))} / 基准 {format_pct(rel_5d.get('benchmark'))}", "value_cls": pct_color_class(rel_5d.get("relative"))},
        {"label": "AI 置信度", "value": format_number(advice.get("confidence"), 2), "sub": f"风险等级 {risk_level_zh(risk_level)}"},
    ]
    parts.append(render_section("Portfolio 概览", "📊", render_kpi_cards(cards, cols=4)))

    # ── 组合配置与风险图表（修改计划 17.4）──
    chart_html = ""
    if charts.get("weight_bars"):
        chart_html += render_chart_container(charts["weight_bars"], "各持仓权重占比")
    if charts.get("weight_vs_risk"):
        chart_html += render_chart_container(charts["weight_vs_risk"], "权重（蓝）与风险贡献（红）对比")
    if charts.get("allocation_group"):
        chart_html += render_chart_container(charts["allocation_group"], "账户分组权重分布")
    if charts.get("allocation_theme"):
        chart_html += render_chart_container(charts["allocation_theme"], "主题/行业权重分布")
    if charts.get("cumulative"):
        chart_html += render_chart_container(charts["cumulative"], "组合 vs 基准累计收益")
    if chart_html:
        parts.append(render_section("组合配置与风险图表", "📈", chart_html))
    else:
        parts.append(render_section("组合配置与风险图表", "📈", '<p class="kpi-sub">（暂无图表数据）</p>'))

    # ── AI 风险诊断（修改计划 17.5）──
    key_risks = advice.get("key_risks") or []
    if key_risks:
        parts.append(render_section("AI 风险诊断", "⚠️", render_risk_cards(key_risks)))
    elif risk_findings:
        parts.append(render_section("风险诊断（确定性规则）", "⚠️", render_risk_cards(risk_findings)))
    else:
        parts.append(render_section("AI 风险诊断", "⚠️", render_risk_cards([])))

    # ── AI 操作建议总表（修改计划 17.6）──
    actions = sorted(advice.get("actions") or [], key=lambda a: (a.get("priority") or 99))
    if actions:
        parts.append(render_section("AI 操作建议总表", "🎯", render_action_summary_table(actions)))

    # ── AI 操作建议详情（修改计划 17.7）──
    if actions:
        detail_html = ""
        rc_map = {item.get("ticker"): item.get("risk_contribution") for item in metrics.get("risk_contributions", [])}
        for a in actions:
            detail_html += render_action_detail(a, rc_map.get(a.get("ticker")))
        parts.append(render_section("AI 操作建议详情", "🎯", detail_html))

    # ── Top-risk 新闻综合（修改计划 17.8）──
    if evidence:
        by_ticker: dict[str, list[dict]] = {}
        macro_items = []
        for e in evidence:
            t = e.get("ticker")
            if t:
                by_ticker.setdefault(t, []).append(e)
            else:
                macro_items.append(e)
        news_html = ""
        for t, items in by_ticker.items():
            news_html += render_news_group(f"{t} 相关新闻", items)
        if macro_items:
            news_html += render_news_group("宏观 / 系统性因素", macro_items)
        parts.append(render_section("Top-risk 新闻综合", "📰", news_html))
    else:
        parts.append(render_section("Top-risk 新闻综合", "📰", '<p class="kpi-sub">（未配置新闻搜索或未返回证据）</p>'))

    # ── 行业、主题与宏观分析（修改计划 17.9）──
    macro_html = ""
    for label, key in (
        ("趋势研判", "trend_view"), ("集中度视图", "concentration_view"), ("风险视图", "risk_view"),
        ("相对表现", "relative_performance_view"), ("新闻视图", "news_view"),
    ):
        val = pa.get(key)
        if val:
            macro_html += f'<div class="reason-block"><h5>{esc(label)}</h5><p>{esc(val)}</p></div>'
    watch = advice.get("watch_items") or []
    if watch:
        macro_html += '<div class="reason-block"><h5>后续观察事项</h5><ul class="trigger-list">'
        for w in watch:
            macro_html += f'<li><b>{esc(w.get("title", ""))}</b>：{esc(w.get("reason", ""))}（{esc(", ".join(w.get("affected_tickers") or []))}）</li>'
        macro_html += "</ul></div>"
    if not macro_html:
        macro_html = '<p class="kpi-sub">（AI 未生成行业/主题/宏观分析；参见上方风险诊断与新闻综合。）</p>'
    parts.append(render_section("行业、主题与宏观分析", "🌐", macro_html))

    # ── 所有持仓技术快照（修改计划 17.10，按风险优先级降序）──
    items = risk_ranking.get("items", []) or []
    meta = instrument_metadata
    rows = []
    for item in items:
        t = item["ticker"]
        h = next((x for x in snapshot.get("holdings", []) if x["ticker"] == t), {})
        m = meta.get(t, {}) or {}
        rows.append([
            t,
            m.get("instrument_type") or "—",
            m.get("account_group") or h.get("group") or "—",
            m.get("theme") or m.get("underlying_index") or "—",
            format_pct(h.get("weight")),
            format_pct(h.get("profit_loss_pct")),
            format_pct(h.get("return_1d")),
            format_pct(h.get("return_5d")),
            format_pct(h.get("return_1m")),
            format_pct(h.get("return_ytd")),
            format_pct(h.get("diff_ema20")),
            format_pct(h.get("diff_ema50")),
            format_pct(h.get("diff_ema200")),
            format_number(h.get("rsi")),
            format_number(h.get("volume_ratio")),
            format_number(h.get("beta")),
            format_pct((item.get("risk_contribution"))),
            format_number(item.get("risk_priority_score"), 3),
        ])
    headers = ["标的", "类型", "账户组", "主题", "权重", "盈亏%", "1D", "5D", "1M", "YTD",
               "EMA20", "EMA50", "EMA200", "RSI", "量比", "Beta", "风险贡献", "风险分"]
    parts.append(render_section("所有持仓技术快照（按风险优先级降序）", "🧮", render_table(headers, rows)))

    # ── 来源附录（修改计划 17.11）──
    if evidence:
        appendix = ""
        for e in evidence:
            url = str(e.get("url") or "")
            title_html = (
                f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(e.get("title", ""))}</a>'
                if url else esc(e.get("title", ""))
            )
            appendix += (
                f'<div class="source-card"><div class="sc-head"><div class="sc-title">{title_html}</div></div>'
                f'<div class="sc-meta"><span>{esc(e.get("evidence_id", ""))}</span><span>来源：{esc(e.get("source_name", ""))}</span>'
                f'<span>日期：{esc(e.get("published_date", ""))}</span><span>关联：{esc(e.get("ticker") or "—")}</span></div>'
                f'<div class="sc-summary">{esc(e.get("summary_zh") or e.get("title") or "")}</div></div>'
            )
        parts.append(render_section("来源附录（完整 Evidence）", "🔗", appendix))

    # ── 数据质量与免责声明（修改计划 17.12）──
    quality = snapshot.get("data_quality", {})
    missing = []
    for field, label in (("missing_prices", "缺失价格"), ("missing_fx", "缺失 FX 汇率"), ("missing_history", "缺失历史")):
        vals = quality.get(field) or []
        if vals:
            missing.append(f"{label}：{esc(', '.join(map(str, vals[:12])))}")
    limitations = advice.get("data_limitations") or []
    dq_html = ""
    if missing:
        dq_html += "<p>" + "；".join(missing) + "</p>"
    if limitations:
        dq_html += "<p>AI/数据限制：" + "；".join(esc(x) for x in limitations) + "</p>"
    if not dq_html:
        dq_html = '<p class="kpi-sub">数据完整。</p>'
    dq_html += f'<p class="kpi-sub">搜索提供方：{esc(str(settings.get("search_provider") or "auto"))}；AI 模型：{esc(str(settings.get("model") or "未配置"))}</p>'
    parts.append(render_section("数据质量与限制", "🛡️", dq_html))

    parts.append(render_disclaimer(advice.get("disclaimer") or "本报告仅供研究参考，不构成投资建议。"))
    parts.append("</div>\n</body>\n</html>")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--risk-ranking", required=True)
    parser.add_argument("--advice", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    html_text = build_html(
        _load(Path(args.snapshot), {}),
        _load(Path(args.metrics), {}),
        _load(Path(args.risk_ranking), {}),
        _load(Path(args.advice), {}),
        _load(Path(args.evidence), []),
    )
    Path(args.output).write_text(html_text, encoding="utf-8")


if __name__ == "__main__":
    main()
