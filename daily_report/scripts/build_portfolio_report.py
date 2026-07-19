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
from datetime import datetime as _dt, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..report_components import (
    render_html_head, render_report_header, render_kpi_cards, render_section,
    render_risk_cards, render_action_summary_table, render_action_detail,
    render_news_group, render_disclaimer, render_chart_container, render_fallback_banner,
    render_table, esc,
)
from ..report_i18n import (
    action_zh, risk_level_zh, format_money, format_pct, format_number, pct_color_class,
    format_ratio_as_pct, format_pct_value, finite_float,
)
from portfolio_analysis.metric_contracts import fmt_metric
from ..report_charts import (
    svg_weight_bars, svg_weight_vs_risk, svg_allocation, svg_cumulative_returns,
)


def _load(path: Path, default: Any) -> Any:
    if not path or not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _to_berlin(iso: str | None) -> str:
    """修改计划第三轮 40：统一以 Europe/Berlin 展示时间。"""
    if not iso:
        return ""
    try:
        d = _dt.fromisoformat(str(iso))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d %H:%M Europe/Berlin")
    except Exception:
        return str(iso)


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
    research_diagnostics: dict[str, Any] | None = None,
    report_quality: dict[str, Any] | None = None,
) -> str:
    settings = settings or {}
    instrument_metadata = instrument_metadata or snapshot.get("instrument_metadata") or {}
    charts = charts or {}
    research_diagnostics = research_diagnostics or {}
    report_quality = report_quality or {}
    base = snapshot.get("base_currency", "EUR")
    summary = snapshot.get("summary", {})
    portfolio_name = snapshot.get("portfolio_name", "Portfolio")
    report_date = str(snapshot.get("report_date") or "")
    as_of = _to_berlin(snapshot.get("as_of"))
    cutoffs = snapshot.get("data_cutoffs") or {}
    price_cutoff = str(cutoffs.get("equity") or cutoffs.get("etf") or snapshot.get("as_of_prices") or report_date)
    etf_cutoff = str(cutoffs.get("etf") or price_cutoff)
    crypto_cutoff = str(cutoffs.get("crypto") or "—")
    benchmark_cutoff = str(cutoffs.get("benchmark") or "—")
    news_cutoff = str(cutoffs.get("news") or as_of or "—")

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

    # 修改计划第三轮 40：明确数据截止口径与时间。
    # 修改计划第六轮第 27 节：新闻时间字段拆分（检索时间 vs 最新事件日期）。
    news_exec_at = research_diagnostics.get("news_search_executed_at") if research_diagnostics else None
    latest_event_date = research_diagnostics.get("latest_selected_event_date") if research_diagnostics else None
    news_time_html = f'新闻检索截止：{esc(news_cutoff)}'
    if news_exec_at:
        news_time_html += f'　|　新闻检索时间：{esc(str(news_exec_at)[:19])}'
    if latest_event_date:
        news_time_html += f'　|　最新入选事件日期：{esc(str(latest_event_date))}'
    cutoff_html = (
        '<div class="data-cutoff">'
        f'报告生成时间：{esc(as_of)}　|　股票行情截止：{esc(price_cutoff)} 收盘　|　'
        f'ETF/ETC 截止：{esc(etf_cutoff)} 收盘　|　加密资产截止：{esc(crypto_cutoff)}　|　'
        f'基准截止：{esc(benchmark_cutoff)} 收盘　|　{news_time_html}'
        '</div>'
    )
    parts.append(cutoff_html)

    if is_fallback:
        parts.append(render_fallback_banner(fallback_reason))

    # ── AI 核心结论（修改计划 17.2）──
    stance_pill = f'<span class="pill info">组合态度：{esc(stance)}</span>'
    risk_pill = f'<span class="pill warn">风险等级：{esc(risk_level_zh(risk_level))}</span>'
    conf_pill = f'<span class="pill">报告最终置信度：{format_ratio_as_pct(advice.get("final_confidence") or advice.get("confidence"))}</span>'
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
    confidence_components = advice.get("confidence_components") or {}
    confidence_labels = {
        "model_confidence": "模型输出",
        "data_quality": "行情数据质量",
        "metadata_coverage": "工具元数据覆盖",
        "evidence_coverage": "Top-risk 证据覆盖",
        "evidence_freshness": "证据新鲜度",
        "evidence_verification": "证据正文验证",
    }
    finite_confidence = {
        key: finite_float(value)
        for key, value in confidence_components.items()
        if key in confidence_labels and finite_float(value) is not None
    }
    confidence_limiter = min(finite_confidence, key=finite_confidence.get) if finite_confidence else None
    confidence_sub = (
        f"限制项：{confidence_labels[confidence_limiter]} "
        f"{format_ratio_as_pct(finite_confidence[confidence_limiter])}"
        if confidence_limiter else "未提供置信度分解"
    )
    cards = [
        {"label": "总市值", "value": format_money(summary.get("total_market_value_base"), base), "sub": f"成本 {format_money(summary.get('total_cost_basis_base'), base)}"},
        {"label": "总盈亏", "value": format_money(summary.get("profit_loss_base"), base), "sub": fmt_metric("profit_loss_pct", summary.get('profit_loss_pct')), "value_cls": pct_color_class(summary.get("profit_loss_pct"))},
        {"label": "Top 1 / Top 3", "value": f"{fmt_metric('top1_weight', metrics.get('top1_weight'))} / {fmt_metric('top3_weight', metrics.get('top3_weight'))}", "sub": f"HHI×1e4 {format_number(metrics.get('hhi_10000'), 0)}"},
        {"label": "有效持仓数", "value": format_number(metrics.get("effective_holdings"), 1), "sub": f"持仓 {len(snapshot.get('holdings', []))} 只"},
        {"label": "历史组合 Beta", "value": format_number(metrics.get("portfolio_beta"), 2), "sub": f"样本 {metrics.get('portfolio_beta_observations') or 0} 日 · 本地货币近似"},
        {"label": "63D 回撤", "value": fmt_metric("max_drawdown_63d", metrics.get("max_drawdown_63d")), "sub": f"252D {fmt_metric('max_drawdown_252d', metrics.get('max_drawdown_252d'))}", "value_cls": pct_color_class(metrics.get("max_drawdown_63d"))},
        {"label": "相对基准(5D)", "value": fmt_metric("relative", rel_5d.get("relative")), "sub": f"组合 {fmt_metric('portfolio_return', rel_5d.get('portfolio'))} / 基准 {fmt_metric('benchmark_return', rel_5d.get('benchmark'))}", "value_cls": pct_color_class(rel_5d.get("relative"))},
        {"label": "Python 风险评分", "value": format_number(metrics.get("portfolio_risk_score"), 0), "sub": f"评分可信度 {format_ratio_as_pct(metrics.get('risk_score_confidence'))}"},
        {"label": "AI 置信度", "value": format_ratio_as_pct(advice.get("confidence")), "sub": confidence_sub},
    ]
    parts.append(render_section("Portfolio 概览", "📊", render_kpi_cards(cards, cols=4)))

    if finite_confidence:
        confidence_rows = []
        limiting_value = finite_confidence.get(confidence_limiter) if confidence_limiter else None
        for key in confidence_labels:
            if key not in finite_confidence:
                continue
            marker = "限制项" if limiting_value is not None and finite_confidence[key] == limiting_value else ""
            confidence_rows.append([
                confidence_labels[key], format_ratio_as_pct(finite_confidence[key]), marker,
            ])
        confidence_html = render_table(["置信度分量", "得分上限", "说明"], confidence_rows, scroll=False)
        confidence_html += (
            '<p class="kpi-sub">最终置信度取上述分量中的最小值；新闻数量多并不等于证据新鲜或已经正文验证。</p>'
        )
        parts.append(render_section("报告置信度分解", "🔎", confidence_html))

    component_labels = {
        "concentration": "集中度", "beta": "Beta", "volatility": "波动率", "drawdown": "回撤",
        "correlation": "相关性", "breadth": "技术广度", "risk_contribution": "风险贡献集中",
    }
    component_rows = []
    for key, maximum in (metrics.get("risk_score_component_max") or {}).items():
        value = (metrics.get("risk_score_components") or {}).get(key)
        component_rows.append([component_labels.get(key, key), "缺失" if value is None else f"{value}/{maximum}"])
    if component_rows:
        missing_components = "、".join(component_labels.get(x, x) for x in metrics.get("risk_score_missing_components") or []) or "无"
        score_html = render_table(["风险分项", "得分"], component_rows)
        score_html += f'<p class="kpi-sub">评分可信度：{format_ratio_as_pct(metrics.get("risk_score_confidence"))}；缺失分项：{esc(missing_components)}</p>'
        parts.append(render_section("组合风险评分分项", "🧮", score_html))

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
        chart_html += render_chart_container(
            charts["cumulative"],
            "当前持仓静态权重回溯模拟 vs 基准 · 非真实账户收益（未含买卖日期/现金流/交易成本/税务）",
        )
        methodology = metrics.get("performance_methodology") or {}
        if not methodology.get("historical_fx_aligned"):
            chart_html += '<p class="kpi-sub warn-text">历史回溯使用各标的本地货币收益近似，未进行逐日基础货币汇率转换，不适合作为精确 Alpha。</p>'
    if chart_html:
        parts.append(render_section("组合配置与风险图表", "📈", chart_html))
    else:
        parts.append(render_section("组合配置与风险图表", "📈", '<p class="kpi-sub">（暂无图表数据）</p>'))

    # ── 风险诊断（修改计划第三轮 33：确定性规则与 AI 解读分开展示，AI 不得覆盖 Python 风险）──
    key_risks = advice.get("key_risks") or []
    risk_parts = []
    if risk_findings:
        risk_parts.append(render_section("确定性风险指标（Python 规则）", "⚠️", render_risk_cards(risk_findings)))
    if key_risks:
        risk_parts.append(render_section("AI 综合风险解读", "⚠️", render_risk_cards(key_risks)))
    if not risk_parts:
        risk_parts.append(render_section("AI 风险诊断", "⚠️", render_risk_cards([])))
    parts.extend(risk_parts)

    # ── AI 操作建议总表（修改计划 17.6）──
    actions = sorted(advice.get("actions") or [], key=lambda a: (a.get("priority") or 99))
    rc_summary_map = {
        item.get("ticker"): item.get("risk_contribution")
        for item in metrics.get("risk_contributions", []) or []
    }
    if actions:
        parts.append(render_section("AI 操作建议总表", "🎯", render_action_summary_table(actions, rc_summary_map)))
        # 修改计划第三轮 29：释放资金去向说明（系统无完整现金/目标配置时不自动推荐精确替代）。
        realloc = advice.get("portfolio_reallocation") or {}
        if realloc:
            red_w = realloc.get("estimated_weight_reduction")
            reloc_html = (
                '<div class="reason-block"><p>'
                f'计划减仓预计释放组合权重：<b>{format_ratio_as_pct(red_w) if red_w is not None else "—"}</b>　|　'
                f'计算口径：目标区间中点转为现金　|　资金去向：暂留现金，具体再配置未指定　|　'
                f'{esc(realloc.get("note") or "")}'
                '</p></div>'
            )
            parts.append(render_section("Portfolio 再平衡摘要", "⚖️", reloc_html))

    # ── AI 操作建议详情（修改计划 17.7）──
    if actions:
        detail_html = ""
        rc_map = {item.get("ticker"): item.get("risk_contribution") for item in metrics.get("risk_contributions", [])}
        # 修改计划第三轮 21：为 metric_evidence 准备确定性数据查询。
        rc_by_ticker = {item.get("ticker"): item for item in metrics.get("risk_contributions", [])}
        detail_by_ticker: dict[str, dict[str, Any]] = {}
        for h in snapshot.get("holdings", []):
            t = h["ticker"]
            detail_by_ticker[t] = {
                "weight": h.get("weight"),
                "risk_contribution": (rc_by_ticker.get(t) or {}).get("risk_contribution"),
                "risk_weight_gap": (rc_by_ticker.get(t) or {}).get("risk_weight_gap"),
                "beta": h.get("beta"),
                "rsi": h.get("rsi"),
                "profit_loss_pct": h.get("profit_loss_pct"),
                "annualized_volatility": (metrics.get("holdings_detail", {}) or {}).get(t, {}).get("annualized_volatility"),
                "max_drawdown_63d": (metrics.get("holdings_detail", {}) or {}).get(t, {}).get("max_drawdown_63d"),
                "max_drawdown_252d": (metrics.get("holdings_detail", {}) or {}).get(t, {}).get("max_drawdown_252d"),
                "distance_from_52w_high": (metrics.get("holdings_detail", {}) or {}).get(t, {}).get("distance_from_52w_high"),
                "price_vs_ema20_pct": h.get("price_vs_ema20_pct"),
                "price_vs_ema50_pct": h.get("price_vs_ema50_pct"),
                "price_vs_ema200_pct": h.get("price_vs_ema200_pct"),
            }
        for a in actions:
            detail_html += render_action_detail(a, rc_map.get(a.get("ticker")), detail_by_ticker.get(a.get("ticker"), {}))
        parts.append(render_section("AI 操作建议详情", "🎯", detail_html))

    # ── Evidence Quality 概览 + Top-risk 新闻综合（修改计划第三轮 37/38）──
    if evidence:
        by_ticker: dict[str, list[dict]] = {}
        macro_items = []
        for e in evidence:
            t = e.get("ticker")
            if t:
                by_ticker.setdefault(t, []).append(e)
            else:
                macro_items.append(e)

        # 修改计划第三轮 38：Evidence 质量概览。
        top_risk_set = set(risk_ranking.get("top_risk_tickers") or [])
        covered = top_risk_set & set(by_ticker.keys())
        fresh_events = sum(1 for e in evidence if str(e.get("recency_tier")) == "fresh_event")
        tier12 = sum(1 for e in evidence if str(e.get("source_quality") or "").startswith("tier_1") or str(e.get("source_quality") or "").startswith("tier_2"))
        unknown_dates = sum(1 for e in evidence if not e.get("published_date"))
        verified_articles = sum(1 for e in evidence if e.get("article_fetch_ok"))
        unverified_snippets = len(evidence) - verified_articles
        quality_html = (
            '<div class="evidence-quality">'
            f'<span>Top-risk 覆盖：{len(covered)}/{len(top_risk_set)}</span>'
            f'<span>新鲜事件：{fresh_events}</span>'
            f'<span>Tier 1/2 来源：{tier12}</span>'
            f'<span>未知日期：{unknown_dates}</span>'
            f'<span>正文已验证：{verified_articles}</span>'
            f'<span>未验证摘要：{unverified_snippets}</span>'
            f'<span>证据总数：{len(evidence)}</span>'
            '</div>'
        )
        if len(covered) < len(top_risk_set):
            quality_html += '<p class="kpi-sub warn-text">新闻结论可信度有限：部分 Top-risk 标的缺少新闻证据支撑。</p>'
        if unverified_snippets:
            quality_html += '<p class="kpi-sub warn-text">“搜索摘要·未验证”仅作为研究线索，可能与落地页正文存在差异，不能支撑高置信度操作。</p>'

        # 修改计划第三轮 37：新闻综合每 ticker 仅保留优先级最高的 2~3 条，避免与来源附录重复堆叠。
        def _top(items: list[dict], n: int = 3) -> list[dict]:
            return sorted(items, key=lambda x: -(float(x.get("priority_score") or 0.0)))[:n]

        news_html = quality_html
        for t, items in by_ticker.items():
            news_html += render_news_group(f"{t} 相关新闻（重点 {min(3, len(items))} 条）", _top(items))
        if macro_items:
            news_html += render_news_group("宏观 / 系统性因素", _top(macro_items))
        parts.append(render_section("Top-risk 新闻综合", "📰", news_html))
    else:
        parts.append(render_section("Top-risk 新闻综合", "📰", '<p class="kpi-sub">（新闻研究未返回可发布证据；正式报告质量门槛会阻止操作建议发布。）</p>'))

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
            format_ratio_as_pct(h.get("weight")),
            format_pct_value(h.get("profit_loss_pct")),
            format_pct_value(h.get("return_1d")),
            format_pct_value(h.get("return_5d")),
            format_pct_value(h.get("return_1m")),
            format_pct_value(h.get("return_ytd")),
            format_pct_value(h.get("price_vs_ema20_pct")),
            format_pct_value(h.get("price_vs_ema50_pct")),
            format_pct_value(h.get("price_vs_ema200_pct")),
            format_number(h.get("rsi")),
            format_number(h.get("volume_ratio")),
            format_number(h.get("beta")),
            format_ratio_as_pct(item.get("risk_contribution")),
            format_number(item.get("risk_priority_score"), 3),
        ])
    headers = ["标的", "类型", "账户组", "主题", "权重", "盈亏%", "1D", "5D", "1M", "YTD",
               "EMA20偏离", "EMA50偏离", "EMA200偏离", "RSI", "量比", "Beta", "风险贡献", "风险分"]
    holdings_html = render_table(headers, rows)
    holdings_html += '<p class="kpi-sub">风险贡献采用正边际贡献归一化口径；负边际风险贡献被归零，因此 0% 不代表该资产本身没有波动或风险。</p>'
    parts.append(render_section("所有持仓技术快照（按风险优先级降序）", "🧮", holdings_html))

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
                f'<span>日期：{esc(e.get("published_date", ""))}</span><span>关联：{esc(e.get("ticker") or "—")}</span>'
                f'<span>正文：{"已验证" if e.get("article_fetch_ok") else "仅搜索摘要"}</span></div>'
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
    limitations = [str(x).rstrip("；。 ") for x in (advice.get("data_limitations") or []) if str(x).strip()]
    dq_html = ""
    if missing:
        dq_html += "<p>" + "；".join(missing) + "</p>"
    if limitations:
        dq_html += "<p>AI/数据限制：" + "；".join(esc(x) for x in limitations) + "</p>"
    status_labels = {
        "success": "成功", "not_configured": "未配置", "provider_error": "提供方请求失败",
        "no_raw_results": "未返回原始结果", "all_filtered": "全部候选被过滤",
        "no_recent_evidence": "没有近期证据", "insufficient_coverage": "Top-risk 覆盖不足",
    }
    if research_diagnostics:
        rejected = research_diagnostics.get("rejected") or {}
        reasons = "、".join(f"{esc(k)} {v}" for k, v in rejected.items() if v) or "无"
        dq_html += (
            f'<p>新闻研究状态：{esc(status_labels.get(str(research_diagnostics.get("status")), str(research_diagnostics.get("status") or "未知")))}；'
            f'原始结果：{research_diagnostics.get("raw_results_count", 0)}；过滤后：{research_diagnostics.get("filtered_results_count", 0)}；'
            f'入选证据：{research_diagnostics.get("selected_evidence_count", 0)}；主要过滤原因：{reasons}</p>'
        )
        # 第六轮：Risk-weighted Coverage（修改计划第 21 节）
        rwc = research_diagnostics.get("risk_weighted_coverage")
        if rwc is not None:
            dq_html += f'<p class="kpi-sub">风险加权覆盖率：{format_ratio_as_pct(rwc)}；Top-risk 覆盖率：{format_ratio_as_pct(research_diagnostics.get("top_risk_coverage") or 0)}</p>'
        # 第六轮：新闻时间字段拆分（修改计划第 27 节）
        news_exec_at = research_diagnostics.get("news_search_executed_at")
        latest_event = research_diagnostics.get("latest_selected_event_date")
        if news_exec_at or latest_event:
            dq_html += '<p class="kpi-sub">'
            if news_exec_at:
                dq_html += f'新闻检索时间：{esc(str(news_exec_at)[:19])}；'
            if latest_event:
                dq_html += f'最新入选事件日期：{esc(str(latest_event))}；'
            dq_html += '</p>'
        # 第六轮：Materiality 统计（修改计划第 16 节）
        mat_stats = research_diagnostics.get("materiality_stats") or {}
        if mat_stats:
            dq_html += (
                f'<p class="kpi-sub">Materiality 评分：入选 {mat_stats.get("accepted_count", 0)} 条，'
                f'拒绝 {mat_stats.get("rejected_count", 0)} 条，'
                f'事件聚类 {mat_stats.get("cluster_count", 0)} 组，'
                f'平均选择分 {mat_stats.get("avg_selection_score", 0)}；'
                f'拒绝原因：' + "、".join(f"{esc(k)} {v}" for k, v in (mat_stats.get("rejected_reasons") or {}).items() if v) + '</p>'
            )
    if report_quality:
        dq_html += f'<p>报告质量评分：{format_ratio_as_pct(report_quality.get("quality_score"))}；可操作：{"是" if report_quality.get("actionable") else "否"}</p>'
        quality_warnings = [str(item) for item in (report_quality.get("warnings") or []) if str(item).strip()]
        if quality_warnings:
            dq_html += '<p class="kpi-sub warn-text">质量提示：' + '；'.join(esc(item) for item in quality_warnings) + '</p>'
    if not dq_html:
        dq_html = '<p class="kpi-sub">数据完整。</p>'
    # 第六轮：Search Provider 与 Planner 诊断显示（修改计划第 28 节）
    planner_mode = research_diagnostics.get("planner_mode") if research_diagnostics else None
    planner_model = research_diagnostics.get("planner_model") if research_diagnostics else None
    provider_used = research_diagnostics.get("provider_used") if research_diagnostics else None
    verticals = research_diagnostics.get("verticals_used") if research_diagnostics else None
    search_lanes = research_diagnostics.get("search_lanes") if research_diagnostics else None
    gap_mode = research_diagnostics.get("gap_mode") if research_diagnostics else None
    dq_html += f'<p class="kpi-sub">请求模式：{esc(str(settings.get("search_provider") or "auto"))}；'
    if provider_used:
        dq_html += f'实际使用：{esc(str(provider_used))}；'
    if verticals:
        dq_html += f'搜索通道：{esc(" + ".join(verticals))}；'
    if search_lanes:
        lane_parts = [f"{esc(k)} {v}" for k, v in search_lanes.items() if v and k not in ("official_total", "news_total")]
        if lane_parts:
            dq_html += f'Lane 分布：{"、".join(lane_parts)}；'
    dq_html += f'AI 模型：{esc(str(settings.get("model") or "未配置"))}</p>'
    if planner_mode:
        planner_label = "AI Planner" if planner_mode == "ai" else "确定性 Fallback Planner"
        dq_html += f'<p class="kpi-sub">Planner 模式：{esc(planner_label)}'
        if planner_model:
            dq_html += f'；Planner 模型：{esc(str(planner_model))}（与 Portfolio Agent 相同）'
        fallback_reason = research_diagnostics.get("planner_fallback_reason") if research_diagnostics else None
        if fallback_reason and planner_mode != "ai":
            dq_html += f'；降级原因：{esc(str(fallback_reason))}'
        if gap_mode:
            gap_label = {"ai": "AI 缺口分析", "deterministic": "确定性补搜", "skipped": "无需补搜"}.get(gap_mode, gap_mode)
            dq_html += f'；Gap Analyzer：{esc(gap_label)}'
        dq_html += '</p>'
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
