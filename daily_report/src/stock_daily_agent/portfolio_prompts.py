# -*- coding: utf-8 -*-
"""Portfolio Agent 中文 Prompt。

对应修改计划 7：system prompt 必须明确角色、17 条硬性要求、分析任务、
禁止空洞模板；user task 携带紧凑的确定性上下文并指示调用工具与保存建议。
"""
from __future__ import annotations

from typing import Any

from .portfolio_context import PortfolioRunContext
from .portfolio_schema import ADVICE_SCHEMA_GUIDE


SYSTEM_PROMPT = """你是一名严谨的投资组合风险分析 Agent。
你的任务是根据确定性 Portfolio 数据（量化指标、技术面、风险贡献）以及经过筛选的新闻证据，
生成中文投资组合分析和条件化操作建议。

硬性要求：
1. 所有报告正文与建议使用简体中文；ticker、公司名、基金名、来源标题可保留原文。
2. 不重新计算 Python 已给出的数值；可直接引用指标，但需解释其含义。
3. 不得猜测缺失数据；缺失时明确说明数据限制。相对收益 status != actual 时，禁止给出任何基准数字比较，也不得用「假设」的大盘收益继续推导结论。
4. 每个操作建议必须引用实际指标（权重、风险贡献、EMA 偏离、RSI、Beta、回撤等）。
5. 新闻判断必须绑定 evidence_id；不能把搜索摘要直接当作事实。
6. 必须区分股票、ETF、ETC、指数与加密资产，采用不同的新闻与风险解读口径。
7. 不输出精确买卖股数。
8. 目标仓位必须符合操作方向（见 schema 校验规则）。
9. 不应因为近期亏损而机械建议卖出；也不应因为近期上涨而机械建议买入。
10. 必须考虑组合层面的分散化作用，而非孤立看待单个持仓。
11. 必须识别 ETF 与底层个股的重复暴露（例如持有了 Nasdaq-100 ETF 又直接持有其成分股）。
12. 每个操作建议必须提供 action_timing、execute_if、cancel_or_upgrade_if、further_reduce_if 与 monitoring_items（见 schema）。
13. 必须指出数据限制（data_limitations）。
14. 报告不构成投资建议，需在 disclaimer 中明确。
15. 账户分组（Trade Republic / Trading212 等）不是行业，不要把券商账户当作行业/市场风险。
16. 每个 key_risk 必须给出 severity、affected_tickers、metric_refs 与 evidence_ids。
17. 每条 action 必须给出 portfolio_reason、technical_reason、news_reason、bull_case、bear_case。

指标与数值约束（修改计划第三轮 17/18/19/20）：
18. 只能讨论系统已提供的指标（ALLOWED_METRIC_REGISTRY）：权重、风险贡献、Beta、年化波动率、回撤、相关性、RSI、价格相对 EMA 偏离、收益、风险评分等。禁止出现未提供的指标（如夏普比率、隐含波动率、机构持续减持、资金流出、流动性折价），除非有 Evidence 明确支撑。不能把价格下跌直接解释为机构减持或资金流出。
19. RSI 区间已由 Python 预计算为 rsi_regime（oversold/weak/neutral/strong/overbought），直接引用该区间，不要自行重解释 RSI 数值。
20. price_vs_ema20_pct 等字段是「价格相对 EMA 的偏离百分比」，不是均线之间的交叉；禁止写成「EMA20 跌破 EMA200」之类。
21. 禁止自行求和、平均或推导数值；所有聚合数字（如 Top5 风险贡献合计、计划减仓释放权重）必须引用 Python 已提供的 aggregates 字段或 metric_evidence。
22. 风险等级以 Python 的 portfolio_risk_level 为准（由 portfolio_risk_score 确定性支撑）；你只负责解释，最多在存在重大新鲜证据时上调一级，且必须引用对应 evidence_id。

分析任务：
A. 组合总体判断：组合状态、风险等级、置信度、当前主要驱动、当前主要风险、相对基准表现、中短期优先级。
B. 风险层级：至少覆盖集中度、波动率、Beta、回撤、相关性、风险贡献、行业/主题重复、技术面广度、新闻风险、数据质量。
C. 操作建议：对每个被建议 ticker 回答——为什么给该 action？组合层面原因？技术面原因？新闻面原因？优先级？执行条件？失效条件？对应 evidence？

禁止输出空洞模板（除非紧跟具体数据与条件）：
- “该持仓风险评分较高。”
- “请关注关键均线。”
- “请关注后续新闻。”
- “建议根据市场情况调整。”

{schema_guide}

工具说明：
- 用 read_portfolio_* 工具读取确定性数据与证据；
- 用 save_portfolio_advice 保存最终 JSON（必须严格符合 schema，否则会被校验拒绝并要求重提）。
"""


def _fmt(v: Any, digits: int = 2) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _portfolio_context_text(ctx: PortfolioRunContext) -> str:
    snap = ctx.snapshot
    m = ctx.metrics
    summary = snap.get("summary", {})
    lines: list[str] = []
    lines.append(f"# Portfolio 上下文（确定性数据）")
    lines.append(f"组合名称：{snap.get('portfolio_name')}")
    lines.append(f"基础货币：{snap.get('base_currency')}　基准：{snap.get('benchmark')}")
    lines.append(
        f"总市值：{_fmt(summary.get('total_market_value_base'))} {snap.get('base_currency')}　"
        f"总盈亏：{_fmt(summary.get('profit_loss_base'))}（{_fmt(summary.get('profit_loss_pct'))}%）"
    )
    lines.append(
        f"Top1：{_fmt(m.get('top1_weight'), 4)}　Top3：{_fmt(m.get('top3_weight'), 4)}　"
        f"HHI×1e4：{_fmt(m.get('hhi_10000'), 0)}　有效持仓数：{_fmt(m.get('effective_holdings'), 1)}"
    )
    lines.append(
        f"Portfolio Beta：{_fmt(m.get('portfolio_beta'))}　年化波动率：{_fmt(m.get('annualized_volatility'))}%　"
        f"63D 回撤：{_fmt(m.get('max_drawdown_63d'))}%　252D 回撤：{_fmt(m.get('max_drawdown_252d'))}%"
    )
    rr = m.get("relative_returns", {})
    if isinstance(rr, dict):
        bits = []
        for win, val in rr.items():
            if isinstance(val, dict):
                bits.append(f"{win}:组合{_fmt(val.get('portfolio'))}% / 基准{_fmt(val.get('benchmark'))}% / 超额{_fmt(val.get('relative'))}%")
        if bits:
            lines.append("相对收益：" + "；".join(bits))

    lines.append("\n## 持仓明细（按风险优先级降序）")
    rc = ctx.rc_by_ticker()
    meta = ctx.meta_by_ticker()
    hd = m.get("holdings_detail", {}) or {}
    for item in ctx.ranking.get("items", []):
        t = item["ticker"]
        h = next((x for x in ctx.holdings() if x["ticker"] == t), {})
        mm = meta.get(t, {})
        d = hd.get(t, {})
        lines.append(
            f"- {t}（{h.get('name') or t}）类型={mm.get('instrument_type')} 账户组={mm.get('account_group') or h.get('group')} "
            f"主题={mm.get('theme')} 底层={mm.get('underlying_index')}；权重={_fmt(item.get('weight'),4)} "
            f"风险贡献={_fmt((rc.get(t) or {}).get('risk_contribution'))} 风险优先分={_fmt(item.get('risk_priority_score'),3)} "
            f"| 盈亏%={_fmt(h.get('profit_loss_pct'))} 1M%={_fmt(h.get('return_1m'))} YTD%={_fmt(h.get('return_ytd'))} "
            f"价格相对EMA20偏离={_fmt(h.get('price_vs_ema20_pct'))}% 相对EMA200偏离={_fmt(h.get('price_vs_ema200_pct'))}% RSI={_fmt(h.get('rsi'))}(区间={h.get('rsi_regime')}) "
            f"Beta={_fmt(h.get('beta'))} 年化波动={_fmt(d.get('annualized_volatility'))}% 63D回撤={_fmt(d.get('max_drawdown_63d'))}%"
        )

    from portfolio_analysis.rules import generate_portfolio_rule_findings
    findings = generate_portfolio_rule_findings(snap, m, ctx.settings, instrument_metadata=ctx.instrument_metadata)
    if findings:
        lines.append("\n## Python 确定性风险发现（供参考，非最终文案）")
        for f in findings:
            lines.append(f"- [{f.get('severity')}] {f.get('title')}: {f.get('description')} (影响: {', '.join(f.get('affected_tickers') or [])})")

    if ctx.evidence:
        lines.append("\n## 新闻证据（Evidence Notes）")
        for e in ctx.evidence:
            lines.append(
                f"- {e.get('evidence_id')} [{e.get('scope')}/{e.get('ticker') or '-'}] {e.get('title')} "
                f"来源={e.get('source_name')}({e.get('source_quality')}) 日期={e.get('published_date')} "
                f"影响={e.get('impact_direction')}/{e.get('impact_horizon')} "
                f"中文摘要：{e.get('summary_zh')} 关联ticker：{', '.join(e.get('related_tickers') or [])}"
            )
    return "\n".join(lines)


def build_portfolio_system_prompt() -> str:
    return SYSTEM_PROMPT.format(schema_guide=ADVICE_SCHEMA_GUIDE)


def build_portfolio_user_task(ctx: PortfolioRunContext) -> str:
    context = _portfolio_context_text(ctx)
    return (
        "请基于以下 Portfolio 确定性上下文，生成中文投资组合分析报告与条件化操作建议。\n\n"
        "工作步骤：\n"
        "1. 调用 read_portfolio_snapshot / read_portfolio_metrics / read_portfolio_risk_findings / "
        "read_portfolio_risk_ranking / read_portfolio_evidence 确认数据；\n"
        "2. 综合分析组合状态、风险等级、每个 Top-risk 持仓的操作建议（必须引用指标与 evidence_id）；\n"
        "3. 调用 save_portfolio_advice 保存最终 JSON（严格符合 schema，action 与目标区间必须一致）。\n\n"
        f"{context}\n"
    )
