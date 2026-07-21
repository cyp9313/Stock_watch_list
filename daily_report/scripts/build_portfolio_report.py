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
    risk_level_zh, format_money, format_number, pct_color_class,
    format_ratio_as_pct, format_pct_value, finite_float, portfolio_stance_zh,
)
from portfolio_analysis.metric_contracts import fmt_metric


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


def _first_not_none(*values: Any) -> Any:
    """Return the first non-None value without treating numeric zero as missing."""
    for value in values:
        if value is not None:
            return value
    return None


def _event_date_display(value: Any) -> str:
    """Display only validated ISO dates in the report timeline."""
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        return _dt.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, TypeError):
        try:
            return _dt.strptime(text[:10], "%Y-%m-%d").date().isoformat()
        except (ValueError, TypeError):
            return "未知"



def _market_cutoff_display(value: Any, snapshot_time: Any) -> str:
    """Avoid labelling a same-day daily bar as a completed market close."""
    cutoff = _event_date_display(value)
    if cutoff in {"—", "未知"}:
        return cutoff
    try:
        snap = _dt.fromisoformat(str(snapshot_time or "").replace("Z", "+00:00"))
        if snap.tzinfo is None:
            snap = snap.replace(tzinfo=timezone.utc)
        berlin = snap.astimezone(ZoneInfo("Europe/Berlin"))
        if cutoff == berlin.date().isoformat():
            return f"{cutoff} 最新可用（截至 {berlin:%H:%M} Europe/Berlin，可能含盘中数据）"
    except (ValueError, TypeError):
        pass
    return f"{cutoff} 收盘"


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
    source_notes: list[dict[str, Any]] | None = None,
) -> str:
    settings = settings or {}
    instrument_metadata = instrument_metadata or snapshot.get("instrument_metadata") or {}
    charts = charts or {}
    research_diagnostics = research_diagnostics or {}
    report_quality = report_quality or {}
    source_notes = list(source_notes or [])
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
    is_fallback = advice.get("report_mode") == "quantitative_fallback" or advice.get("ai_analysis_available") is False
    is_observation = bool(report_quality.get("observation_only") or advice.get("observation_only") or is_fallback)
    analyst_mode = not is_fallback
    style_title = str(advice.get("report_style_title") or settings.get("report_style") or "")
    if analyst_mode and not is_fallback:
        report_title = f"AI 投资组合{style_title}报告" if style_title else "AI 投资组合分析报告"
    else:
        report_title = "量化投资组合观察报告" if is_observation else "AI 投资组合分析报告"
    final_confidence = _first_not_none(advice.get("final_confidence"), advice.get("confidence"), 0.0)

    parts: list[str] = [render_html_head(f"{portfolio_name} {report_title}")]

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
        report_title=report_title,
    ))

    # 修改计划第三轮 40：明确数据截止口径与时间。
    # 修改计划第六轮第 27 节：新闻时间字段拆分（检索时间 vs 最新事件日期）。
    timeline = snapshot.get("run_timeline") or {}
    snapshot_time = timeline.get("snapshot_completed_at") or as_of
    news_exec_at = timeline.get("news_search_completed_at") or (
        research_diagnostics.get("news_search_executed_at") if research_diagnostics else None
    )
    rendered_at = timeline.get("report_rendered_at") or "—"
    published_dates = [
        str(item.get("published_date"))
        for item in list(evidence or []) + source_notes
        if item.get("published_date")
    ]
    latest_news_date = max(published_dates) if published_dates else None
    news_time_html = f'新闻搜索完成时间：{esc(news_exec_at or news_cutoff or "—")}'
    news_time_html += f'　|　最新 AI 消息日期：{esc(_event_date_display(latest_news_date))}'
    equity_cutoff_display = _market_cutoff_display(price_cutoff, snapshot_time)
    etf_cutoff_display = _market_cutoff_display(etf_cutoff, snapshot_time)
    benchmark_cutoff_display = _market_cutoff_display(benchmark_cutoff, snapshot_time)
    cutoff_html = (
        '<div class="data-cutoff">'
        f'数据快照时间：{esc(snapshot_time)}　|　报告完成时间：{esc(rendered_at)}　|　'
        f'股票行情截止：{esc(equity_cutoff_display)}　|　ETF/ETC 截止：{esc(etf_cutoff_display)}　|　'
        f'加密资产截止：{esc(crypto_cutoff)}　|　基准截止：{esc(benchmark_cutoff_display)}　|　{news_time_html}'
        '</div>'
    )
    parts.append(cutoff_html)

    if is_fallback:
        parts.append(render_fallback_banner(fallback_reason))

    # ── 核心结论（P0-6 + §23: observation_only 时改变标题）──
    section_title = "量化风险观察结论" if is_observation else "AI 核心结论"
    section_icon = "📊" if is_observation else "🧭"
    stance_pill = f'<span class="pill info">组合态度：{esc(portfolio_stance_zh(stance))}</span>'
    risk_pill = f'<span class="pill warn">风险等级：{esc(risk_level_zh(risk_level))}</span>'
    conf_pill = f'<span class="pill">报告最终置信度：{format_ratio_as_pct(final_confidence)}</span>'
    core_html = (
        f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;">{stance_pill}{risk_pill}{conf_pill}</div>'
    )
    if research_diagnostics:
        pipeline_text = (
            "本报告使用 Portfolio AI Analyst v3：Python 负责不可修改的价格、权重与风险指标；"
            f"{research_diagnostics.get('model') or settings.get('model') or 'deepseek-v4-pro'} "
            "执行最多 1 次联网综合分析。"
            f"报告风格为{style_title or '均衡分析'}，返回 "
            f"{int(research_diagnostics.get('news_item_count') or len(evidence or []))} 条消息分析和 "
            f"{int(research_diagnostics.get('holding_analysis_count') or len(advice.get('actions') or []))} 个重点持仓判断。"
        )
        banner_tail = (
            "量化数字以 Python 结果为准；消息面属于 AI 联网综合判断，"
            "来源匹配状态会单独标注，链接缺失不会删除其余分析。"
        )
        core_html += '<div class="banner warn">' + esc(pipeline_text + banner_tail) + '</div>'
    exec_summary = advice.get("executive_summary") or []
    if exec_summary:
        core_html += '<ul class="summary-list">' + "".join(f"<li>{esc(s)}</li>" for s in exec_summary) + "</ul>"
    else:
        core_html += '<p class="kpi-sub">（AI 未生成核心结论；详见下方量化指标与风险诊断。）</p>'
    parts.append(render_section(section_title, section_icon, core_html))

    if analyst_mode:
        focus_labels = {
            "technical": "技术面", "news": "消息面", "portfolio_risk": "组合风险",
            "macro": "宏观环境", "valuation": "估值与基本面", "actions": "操作与观察条件",
        }
        setting_rows = [
            ["报告风格", style_title or settings.get("report_style") or "—"],
            ["投资期限", settings.get("investment_horizon") or "—"],
            ["风险偏好", settings.get("risk_profile") or "—"],
            ["建议模式", settings.get("advice_mode") or "—"],
            ["分析重点", "、".join(focus_labels.get(x, x) for x in (settings.get("analysis_focus") or [])) or "—"],
            ["新闻窗口", f"{settings.get('news_lookback_days') or 30} 天"],
            ["重点持仓数", settings.get("max_focus_holdings") or "—"],
            ["模型与推理", f"{settings.get('model') or 'deepseek-v4-pro'} · thinking={settings.get('enable_thinking', True)} · {settings.get('reasoning_effort') or 'high'}"],
        ]
        parts.append(render_section("本次 AI 报告设置", "⚙️", render_table(["设置", "当前值"], setting_rows, scroll=False)))

    # ── Portfolio 概览卡片（修改计划 17.3）──
    pa = advice.get("portfolio_analysis", {}) or {}
    rr = metrics.get("relative_returns", {}) or {}
    rel_5d = rr.get("5D", {}) if isinstance(rr.get("5D"), dict) else {}
    confidence_components = advice.get("confidence_components") or {}
    confidence_labels = {
        "model_confidence": "量化模板基线" if is_observation else "模型输出",
        "data_quality": "行情数据质量",
        "metadata_coverage": "工具元数据覆盖",
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
        {"label": "历史组合 Beta", "value": format_number(metrics.get("portfolio_beta"), 2), "sub": f"2年回溯 · 观测 {metrics.get('portfolio_beta_observations') or 0} 日 · 未做货币转换 · 上一版为 1 年回测"},
        {"label": "63D 回撤", "value": fmt_metric("max_drawdown_63d", metrics.get("max_drawdown_63d")), "sub": f"252D {fmt_metric('max_drawdown_252d', metrics.get('max_drawdown_252d'))}", "value_cls": pct_color_class(metrics.get("max_drawdown_63d"))},
        {"label": "相对基准(5D)", "value": fmt_metric("relative", rel_5d.get("relative")), "sub": f"组合 {fmt_metric('portfolio_return', rel_5d.get('portfolio'))} / 基准 {fmt_metric('benchmark_return', rel_5d.get('benchmark'))}", "value_cls": pct_color_class(rel_5d.get("relative"))},
        {"label": "Python 风险评分", "value": format_number(metrics.get("portfolio_risk_score"), 0), "sub": f"评分可信度 {format_ratio_as_pct(metrics.get('risk_score_confidence'))}"},
        {"label": "报告决策置信度" if is_observation else "AI 置信度", "value": format_ratio_as_pct(final_confidence), "sub": confidence_sub},
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
            '<p class="kpi-sub">最终置信度取上述分量中的最小值；新闻数量多并不等于证据新鲜或已经正文提取。</p>'
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

    # ── 风险诊断（修改计划第三轮 33 + §12 Python Top5 渲染）──
    key_risks = advice.get("key_risks") or []
    risk_parts = []

    # §12: Python 预计算 Top5 风险贡献者，AI 只能解释不能列成员
    rc_list = sorted(
        (metrics.get("risk_contributions") or []),
        key=lambda x: -(float(x.get("risk_contribution") or 0.0)),
    )[:5]
    if rc_list:
        top5_rows = "".join(
            f"<tr><td>{esc(r.get('ticker',''))}</td>"
            f"<td>{format_ratio_as_pct(r.get('risk_contribution'))}</td></tr>"
            for r in rc_list
        )
        top5_sum = sum(float(r.get("risk_contribution") or 0.0) for r in rc_list)
        top5_html = (
            f'<p class="kpi-sub">Top 5 风险贡献者（Python 确定性计算，已按风险贡献降序排列）：</p>'
            f'<table class="simple-table"><tr><th>Ticker</th><th>风险贡献</th></tr>'
            f'{top5_rows}'
            f'<tr><td><b>合计</b></td><td><b>{format_ratio_as_pct(top5_sum)}</b></td></tr>'
            f'</table>'
        )
        risk_parts.append(render_section("Python 风险集中度", "📊", top5_html))

    if risk_findings:
        risk_parts.append(render_section("确定性风险指标（Python 规则）", "⚠️", render_risk_cards(risk_findings)))
    if key_risks:
        risk_title = "量化风险解读" if is_observation else "AI 综合风险解读"
        risk_parts.append(render_section(risk_title, "⚠️", render_risk_cards(key_risks)))
    if not risk_parts:
        risk_parts.append(render_section("量化风险诊断" if is_observation else "AI 风险诊断", "⚠️", render_risk_cards([])))
    parts.extend(risk_parts)

    # ── AI 操作建议总表（修改计划 17.6）──
    actions = sorted(advice.get("actions") or [], key=lambda a: (a.get("priority") or 99))
    rc_summary_map = {
        item.get("ticker"): item.get("risk_contribution")
        for item in metrics.get("risk_contributions", []) or []
    }
    if actions:
        action_summary_title = "重点观察清单（按综合风险优先级排序）" if is_observation else "AI 操作建议总表"
        action_summary_html = ""
        if is_observation:
            action_summary_html += (
                '<p class="kpi-sub">该清单综合考虑风险贡献、回撤、波动率和技术指标，'
                '不等同于单纯按风险贡献排序的 Top5。</p>'
            )
        action_summary_html += render_action_summary_table(
            actions, rc_summary_map, observation_only=is_observation,
        )
        parts.append(render_section(action_summary_title, "🎯", action_summary_html))
        # 修改计划第三轮 29：释放资金去向说明（系统无完整现金/目标配置时不自动推荐精确替代）。
        realloc = advice.get("portfolio_reallocation") or {}
        if realloc:
            red_w = realloc.get("estimated_weight_reduction")
            # 全部为观察项且释放权重为 0 时，不展示没有信息量的再平衡摘要。
            if not is_observation or (finite_float(red_w) is not None and abs(float(red_w)) > 1e-9):
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
            detail_html += render_action_detail(
                a,
                rc_map.get(a.get("ticker")),
                detail_by_ticker.get(a.get("ticker"), {}),
                observation_only=is_observation,
            )
        parts.append(render_section("重点观察详情" if is_observation else "AI 操作建议详情", "🎯", detail_html))

    # ── AI news analysis and transparent search-source notes ──
    if evidence or source_notes:
        news_html = ""
        if evidence:
            by_ticker: dict[str, list[dict]] = {}
            macro_items = []
            for item in evidence:
                ticker = item.get("ticker")
                if ticker:
                    by_ticker.setdefault(str(ticker), []).append(item)
                else:
                    macro_items.append(item)

            verified_sources = sum(1 for item in evidence if item.get("source_verified"))
            linked_sources = sum(1 for item in evidence if item.get("url"))
            news_html += (
                '<div class="evidence-quality">'
                f'<span>AI 消息分析：{len(evidence)}</span>'
                f'<span>带链接：{linked_sources}</span>'
                f'<span>与 DashScope 来源匹配：{verified_sources}</span>'
                f'<span>日期未知：{sum(1 for item in evidence if not item.get("published_date"))}</span>'
                '</div>'
            )

            def _top(items: list[dict], n: int = 4) -> list[dict]:
                return sorted(items, key=lambda item: -(float(item.get("confidence") or 0.0)))[:n]

            for ticker, items in by_ticker.items():
                news_html += render_news_group(f"{ticker} 相关新闻（AI 联网分析）", _top(items))
            if macro_items:
                news_html += render_news_group("宏观 / 系统性因素（AI 联网分析）", _top(macro_items))

        if source_notes:
            news_html += (
                '<div class="evidence-quality">'
                f'<span>其他搜索来源：{len(source_notes)}</span>'
                '<span>用途：透明度附录</span>'
                '<span>不单独触发交易建议</span>'
                '</div>'
            )
            news_html += render_news_group("DashScope 其他搜索来源", source_notes[:8])
        parts.append(render_section("消息面分析与联网来源", "📰", news_html))
    else:
        if not settings.get("include_news", True):
            empty_news_text = "用户在 AI report settings 中关闭了联网消息面分析。"
        else:
            empty_news_text = "本轮 AI 联网分析未返回可展示的消息条目或来源链接；技术面与组合风险分析仍然保留。"
        parts.append(render_section("消息面分析与联网来源", "📰", f'<p class="kpi-sub">{esc(empty_news_text)}</p>'))

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
        macro_html = (
            '<p class="kpi-sub">（量化观察模式未生成基本面行业/主题判断；参见上方风险指标。）</p>'
            if is_observation else
            '<p class="kpi-sub">（AI 未生成行业/主题/宏观分析；参见上方风险诊断与新闻综合。）</p>'
        )
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

    # ── Linked-source appendix ──
    appendix_items = [("AI消息分析", item) for item in evidence]
    appendix_items.extend(("搜索来源", item) for item in source_notes)
    if appendix_items:
        appendix = (
            '<table class="simple-table"><tr>'
            '<th>类型</th><th>ID</th><th>Ticker</th><th>日期</th><th>来源</th><th>标题</th><th>匹配状态</th>'
            '</tr>'
        )
        for item_type, item in appendix_items[:24]:
            url = str(item.get("url") or "")
            title_html = (
                f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(str(item.get("title", ""))[:80])}</a>'
                if url else esc(str(item.get("title", ""))[:80])
            )
            status = "与 DashScope 来源匹配" if item.get("source_verified") else ("AI 返回链接" if url else "未返回链接")
            appendix += (
                f'<tr><td>{esc(item_type)}</td>'
                f'<td>{esc(item.get("evidence_id") or item.get("reference_id") or "—")}</td>'
                f'<td>{esc(item.get("ticker") or "—")}</td>'
                f'<td>{esc(str(item.get("published_date") or "日期未提供")[:10])}</td>'
                f'<td>{esc(str(item.get("source_name") or "")[:24])}</td>'
                f'<td>{title_html}</td><td>{esc(status)}</td></tr>'
            )
        appendix += '</table>'
        appendix += '<p class="kpi-sub">来源链接用于信息透明度；Python 不把单条新闻自动转换为无条件交易指令。</p>'
        parts.append(render_section("联网来源附录", "🔗", appendix))

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
    if research_diagnostics:
        dq_html += (
            f'<p>AI Analyst 状态：{esc(str(research_diagnostics.get("status") or "未知"))}；'
            f'模型调用：{int(research_diagnostics.get("model_call_count") or 0)}/1；'
            f'联网调用：{int(research_diagnostics.get("search_call_count") or 0)}/1；'
            f'外部搜索 API：{int(research_diagnostics.get("external_search_call_count") or 0)}；'
            f'重试：{int(research_diagnostics.get("retry_count") or 0)}。</p>'
        )
        dq_html += (
            f'<p class="kpi-sub">模型：{esc(str(research_diagnostics.get("model") or settings.get("model") or "deepseek-v4-pro"))}；'
            f'深度思考：{"开启" if research_diagnostics.get("enable_thinking", settings.get("enable_thinking", True)) else "关闭"}；'
            f'推理强度：{esc(str(research_diagnostics.get("reasoning_effort") or settings.get("reasoning_effort") or "high"))}；'
            f'DashScope 来源：{int(research_diagnostics.get("source_count") or 0)}；'
            f'消息分析：{int(research_diagnostics.get("news_item_count") or len(evidence or []))}；'
            f'重点持仓分析：{int(research_diagnostics.get("holding_analysis_count") or len(advice.get("actions") or []))}；'
            f'耗时：{format_number(research_diagnostics.get("elapsed_seconds"), 2)} 秒。</p>'
        )
        dq_html += (
            f'<p class="kpi-sub">Token：输入 {int(research_diagnostics.get("input_tokens") or 0)}；'
            f'输出 {int(research_diagnostics.get("output_tokens") or 0)}；'
            f'合计 {int(research_diagnostics.get("total_tokens") or 0)}。'
            '量化数据由 Python 固定；AI 消息链接可能未经过正文级独立核验。</p>'
        )
    if report_quality:
        dq_html += (
            f'<p>报告发布状态：{"可发布" if report_quality.get("publishable") else "不可发布"}；'
            f'建议模式：{esc(str(settings.get("advice_mode") or "conditional"))}；'
            f'方向性建议：{"允许" if report_quality.get("actionable") else "关闭"}；'
            f'AI 置信度：{format_ratio_as_pct(advice.get("final_confidence") or advice.get("confidence") or 0)}。</p>'
        )
    if not dq_html:
        dq_html = '<p class="kpi-sub">数据完整。</p>'
    dq_html += (
        '<p class="kpi-sub">研究模式：Portfolio AI Analyst v3；Python 量化 + 单次 DeepSeek 联网综合分析；'
        'Portfolio 路径不调用 Serper、Query Planner、Gap Search、补搜或模型重试。</p>'
    )
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
