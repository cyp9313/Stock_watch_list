# -*- coding: utf-8 -*-
"""Deterministic observation-only portfolio report view."""
from __future__ import annotations


_RISK_LEVEL_ZH = {
    "low": "低",
    "medium": "中等",
    "medium_high": "中高",
    "high": "高",
    "unknown": "未知",
}


def _build_observation_view(
    advice: dict,
    snapshot: dict,
    metrics: dict,
    ranking: dict,
    accepted_evidence: list[dict],
) -> dict:
    """不可操作时用 Python 确定性数据重建全部用户可见结论。"""
    for field in ("executive_summary", "portfolio_analysis", "key_risks", "watch_items"):
        if field in advice and f"raw_ai_{field}" not in advice:
            advice[f"raw_ai_{field}"] = advice.get(field)

    aggregates = metrics.get("aggregates") or {}
    top5 = aggregates.get("top5_risk_contributors") or []
    top5_tickers = [str(item.get("ticker")) for item in top5 if item.get("ticker")]
    top5_risk_sum = float(aggregates.get("top_risk_contribution_sum") or 0.0)
    top5_weight_sum = float(aggregates.get("top_risk_weight_sum") or 0.0)
    below_ema50_weight = float(aggregates.get("below_ema50_weight") or 0.0)
    top5_below = int(aggregates.get("top5_below_ema50_count") or 0)
    top5_count = int(aggregates.get("top5_count") or len(top5_tickers))
    score = metrics.get("portfolio_risk_score")
    level = str(metrics.get("portfolio_risk_level") or "unknown").lower()
    level_zh = _RISK_LEVEL_ZH.get(level, level)
    relative_5d = (metrics.get("relative_returns") or {}).get("5D")

    top5_text = "、".join(top5_tickers) if top5_tickers else "暂无可用成员"
    summaries = []
    if str(advice.get("report_mode") or "") == "quantitative_fallback":
        summaries.append("本轮研究证据不足，已生成量化观察报告；以下仅为确定性风险观察。")
    summaries.extend([
        f"Python 组合风险评分为 {score if score is not None else '—'}，风险等级为{level_zh}。",
        f"当前有 {below_ema50_weight:.1%} 的组合权重位于 EMA50 下方，技术广度偏弱。",
        f"Top5 风险贡献者为 {top5_text}，合计贡献 {top5_risk_sum:.1%} 风险，对应 {top5_weight_sum:.1%} 权重。",
    ])
    if top5_count:
        summaries.append(f"Top5 中有 {top5_below}/{top5_count} 位于 EMA50 下方。")
    if isinstance(relative_5d, dict) and relative_5d.get("relative") is not None:
        summaries.append(
            f"按当前静态权重回溯，组合 5 日相对基准收益为 {float(relative_5d['relative']):+.2f} 个百分点；"
            "该结果不是实际账户收益归因。"
        )
    if not accepted_evidence:
        summaries.append(
            "本轮没有通过研究质量门槛的 Accepted Evidence，因此不对基本面变化、"
            "市场心理或新闻驱动交易作结论。"
        )

    advice["executive_summary"] = summaries
    advice["portfolio_analysis"] = {
        "trend_view": f"技术面观察：{below_ema50_weight:.1%} 权重位于 EMA50 下方。",
        "concentration_view": (
            f"Top5 风险贡献者为 {top5_text}；风险贡献合计 {top5_risk_sum:.1%}，"
            f"权重合计 {top5_weight_sum:.1%}。"
        ),
        "risk_view": (
            "风险贡献采用正边际方差贡献归一化口径。价格相关性只能说明共同波动；"
            "当前没有产品成分穿透数据，不能据此判断成分是否相同。"
        ),
        "relative_performance_view": (
            "相对收益来自当前静态权重历史回溯，不包含真实买卖日期、现金流、税费或历史 FX 对齐。"
        ),
        "news_view": (
            f"Accepted Evidence 共 {len(accepted_evidence)} 条。"
            + ("没有有效事件证据，新闻研究仅保留在诊断附件中。" if not accepted_evidence else "正式新闻结论只引用 Accepted Evidence。")
        ),
    }
    advice["key_risks"] = [
        {
            "risk_id": "Q001",
            "title": "风险贡献集中",
            "severity": "high" if top5_risk_sum >= 0.50 else "medium",
            "description": (
                f"{top5_text} 的正风险贡献合计为 {top5_risk_sum:.1%}；"
                "该数值属于组合风险贡献分布，不是单个标的对风险评分的得分贡献。"
            ),
            "affected_tickers": top5_tickers,
            "metric_refs": ["top_risk_contribution_sum", "top_risk_weight_sum"],
            "evidence_ids": [],
        },
        {
            "risk_id": "Q002",
            "title": "技术广度偏弱",
            "severity": "high" if below_ema50_weight >= 0.70 else "medium",
            "description": f"{below_ema50_weight:.1%} 的组合权重位于 EMA50 下方。",
            "affected_tickers": [
                str(h.get("ticker")) for h in snapshot.get("holdings", [])
                if h.get("price_vs_ema50_pct") is not None and float(h.get("price_vs_ema50_pct")) < 0
            ],
            "metric_refs": ["below_ema50_weight"],
            "evidence_ids": [],
        },
    ]
    advice["watch_items"] = [
        {
            "title": "风险贡献成员变化",
            "reason": "观察 Top5 风险贡献成员及其正风险贡献是否继续集中。",
            "affected_tickers": top5_tickers,
        },
        {
            "title": "Accepted Evidence 覆盖",
            "reason": "等待官方来源或高质量媒体出现可验证的新事件。",
            "affected_tickers": list(ranking.get("top_risk_tickers") or []),
        },
    ]
    advice["observation_only"] = True
    advice["portfolio_stance"] = "observe"
    # Observation 模式的风险等级必须与 Python 风险评分一致，不能继续沿用
    # fallback 模板中的固定 ``medium``。
    if level in _RISK_LEVEL_ZH and level != "unknown":
        advice["risk_level"] = level
    limitations = list(advice.get("data_limitations") or [])
    for item in (
        "观察型结论由 Python 确定性指标生成，不使用未通过质量门的 AI 事件判断。",
        "没有产品成分穿透数据时，价格相关性只能解释为共同波动。",
    ):
        if item not in limitations:
            limitations.append(item)
    advice["data_limitations"] = limitations
    return advice
