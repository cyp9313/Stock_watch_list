# -*- coding: utf-8 -*-
"""修改计划第三轮 #46：必须新增的测试。

覆盖：
- 百分比单位契约（ratio vs pct_value，禁止 ×100 放大）
- 非有限值阻断（NaN/Inf -> N/A，报告不得出现 nan）
- 累计图单位正确（入参已是百分数，不得再 ×100）
- Instrument Metadata 真实场景（无 name 时由 market row 识别 ETF / CRYPTO）
- 新闻相关性过滤（query 含 ticker 但结果无关的 SEC settlement 必须被过滤）
- Evidence Binding（action 不得引用非本 ticker 的证据，strict 必须失败）
- Evidence 时效（过期证据不支持当前短期高置信度操作）
- RSI 区间语义（45/56 neutral，30.2 weak，28 oversold）
- EMA 偏离不得被解释为均线交叉
- Action 语义分离（reduce 的利好条件进 cancel_or_upgrade_if 而非 execute_if）
- Safe HTML（Badge 真实渲染，不被转义）
- 风险贡献总表显示真实值而非 N/A
- Account Group 不得自动成为高市场风险
- Confidence Cap（数据/元数据/证据覆盖不足时置信度被压低）
"""
from __future__ import annotations

import math

import pytest

from daily_report.report_i18n import (
    format_ratio_as_pct, format_pct_value, format_number,
)
from daily_report.report_charts import svg_cumulative_returns
from daily_report.report_components import render_action_summary_table
from daily_report.scripts.build_portfolio_report import build_html
from daily_report.src.stock_daily_agent.portfolio_research import filter_candidates
from daily_report.src.stock_daily_agent.portfolio_schema import normalize_action
from daily_report.run_portfolio_report import _apply_confidence_cap
from portfolio_analysis.instrument_metadata import build_instrument_metadata
from portfolio_analysis.metrics import rsi_regime
from portfolio_analysis.rules import generate_portfolio_rule_findings
from portfolio_analysis.validators import (
    PortfolioAdviceValidationError, validate_portfolio_advice, validate_portfolio_claims,
)
from portfolio_analysis.metric_contracts import (
    data_quality_score, metadata_coverage_score, evidence_coverage_score, evidence_freshness_score,
)


# ── 百分比单位（#46 第一条）────────────────────────────────────
def test_pct_unit_contract():
    # ratio -> 百分数（×100 一次）
    assert format_ratio_as_pct(0.0646) == "6.46%"
    # pct_value 已是百分数（不再 ×100）
    assert format_pct_value(-1.3381) == "-1.34%"
    assert format_pct_value(0.5) == "0.50%"
    # 两类函数对同一个输入结果不同：确认没有重复放大
    assert format_ratio_as_pct(0.0646) != format_pct_value(0.0646)


# ── 非有限值（#46 第二条）────────────────────────────────────
def test_non_finite_renders_na_and_not_nan():
    assert format_pct_value(float("nan")) == "N/A"
    assert format_number(float("inf")) == "N/A"
    assert format_ratio_as_pct(float("nan")) == "N/A"

    # 端到端：metrics 中混入 NaN 时，HTML 不得出现字面 nan
    snap = {
        "portfolio_name": "P", "base_currency": "EUR", "benchmark": "^GSPC",
        "holdings": [{"ticker": "AAA", "group": "G", "weight": 1.0, "market_value_base": 100}],
        "summary": {"total_market_value_base": 100}, "data_quality": {},
    }
    metrics = {
        "top1_weight": 1.0,
        "risk_contributions": [{"ticker": "AAA", "risk_contribution": float("nan")}],
    }
    ranking = {"items": [{"ticker": "AAA", "risk_priority_score": 1.0}]}
    advice = {"actions": [{"ticker": "AAA", "action": "watch"}], "report_mode": "quantitative_fallback"}
    html = build_html(snap, metrics, ranking, advice, [])
    assert "nan" not in html.lower()


# ── 累计图（#46 第三条）──────────────────────────────────────
def test_cumulative_chart_does_not_double_scale():
    labels = ["2026-01-02", "2026-07-17"]
    # 入参已是百分数：7.31% 与 -17.47%
    svg = svg_cumulative_returns(labels, [0.0, 7.31], [0.0, -17.47])
    # 不得把 7.31 渲染成 731%，-17.47 渲染成 -1747%
    assert "731%" not in svg
    assert "1747%" not in svg
    # 单位正确：应在坐标轴出现真实百分数
    assert "7.3%" in svg
    assert "-17.5%" in svg


# ── Metadata 真实场景（#46 第四条）────────────────────────────
def test_metadata_real_scenario_no_holding_name():
    page = {
        "holdings": [
            {"group": "Trade Republic", "ticker": "LYMS.DE"},   # 无 name
            {"group": "Trading212", "ticker": "BTC-EUR"},        # 无 name
        ]
    }
    market_rows = [
        {"Ticker": "LYMS.DE", "Name": "LYXOR MSCI World ETF", "Currency": "EUR"},
        {"Ticker": "BTC-EUR", "Name": "Bitcoin EUR", "Currency": "EUR"},
    ]
    meta = build_instrument_metadata(page, market_rows=market_rows)
    assert meta["LYMS.DE"]["instrument_type"] == "ETF"
    assert meta["BTC-EUR"]["instrument_type"] == "CRYPTO"
    # 名称来自 market row，而非退回占位 ticker
    assert meta["LYMS.DE"]["name"] == "LYXOR MSCI World ETF"
    # 账户分组仍是独立维度，不污染主题/行业
    assert meta["LYMS.DE"]["account_group"] == "Trade Republic"
    assert meta["LYMS.DE"]["sector"] is None


# ── 新闻相关性漏洞（#46 第五条）──────────────────────────────
def test_news_relevance_filters_unrelated_settlement():
    meta = {"WNUC.DE": {"name": "Wnuc Corp Inc", "instrument_type": "EQUITY"}}
    candidates = [
        {
            "ticker": "WNUC.DE", "scope": "ticker", "event_hint": "general",
            "title": "Acme Industries SEC settlement over accounting fraud",
            "summary": "The SEC announced a settlement with Acme Industries unrelated to Wnuc.",
            "url": "https://sec.gov/litigation/acme-settlement",
        },
    ]
    filtered = filter_candidates(candidates, meta)
    # query 虽含 WNUC.DE，但结果正文与 WNUC 无关 -> 必须过滤
    assert len(filtered) == 0


# ── Evidence Binding（#46 第六条）────────────────────────────
def test_evidence_binding_strict_rejects_cross_ticker():
    snapshot = {"holdings": [
        {"ticker": "ORCL", "weight": 0.3},
        {"ticker": "COIN", "weight": 0.2},
    ]}
    evidence = [{
        "evidence_id": "E001", "ticker": "COIN", "related_tickers": ["COIN"],
        "scope": "ticker", "source_quality": "tier_1",
    }]
    # ORCL 的 action 引用了属于 COIN 的证据 E001 -> strict 必须失败
    with pytest.raises(PortfolioAdviceValidationError):
        validate_portfolio_advice(
            {"actions": [{"ticker": "ORCL", "action": "trim",
                          "target_weight_min": 0.15, "target_weight_max": 0.25,
                          "evidence_ids": ["E001"]}]},
            snapshot, evidence, mode="strict",
        )

    # 同 ticker 引用则通过
    ok = validate_portfolio_advice(
        {"actions": [{"ticker": "COIN", "action": "trim",
                      "target_weight_min": 0.1, "target_weight_max": 0.18,
                      "evidence_ids": ["E001"]}]},
        snapshot, evidence, mode="strict",
    )
    assert ok["actions"][0]["evidence_ids"] == ["E001"]


# ── Evidence 时效（#46 第七条）────────────────────────────────
def test_evidence_recency_blocks_stale_high_confidence():
    snapshot = {"holdings": [{"ticker": "AAA", "weight": 0.5, "rsi_regime": "neutral"}]}
    # 2025 年的证据，recency_tier 视为过期（非 fresh_event/recent_background）
    evidence = [{
        "evidence_id": "E001", "ticker": "AAA", "related_tickers": ["AAA"],
        "scope": "ticker", "published_date": "2025-01-15", "recency_tier": "structural",
    }]
    advice = {
        "actions": [{"ticker": "AAA", "action": "trim", "confidence": 0.9,
                     "evidence_ids": ["E001"]}],
    }
    errors, warnings = validate_portfolio_claims(advice, snapshot, {}, evidence)
    # 过期证据支撑的高置信度短期操作必须给出软性警告
    assert any("新鲜" in w or "fresh" in w.lower() for w in warnings)

    # 反例：新鲜证据不应触发该警告
    fresh = [dict(
        evidence[0], published_date="2026-07-10", recency_tier="fresh_event",
        article_fetch_ok=True,
    )]
    _, warnings2 = validate_portfolio_claims(
        {"actions": [{"ticker": "AAA", "action": "trim", "confidence": 0.9, "evidence_ids": ["E001"]}]},
        snapshot, {}, fresh,
    )
    assert not any("新鲜" in w or "fresh" in w.lower() for w in warnings2)


# ── RSI（#46 第八条）─────────────────────────────────────────
def test_rsi_regime_mapping():
    assert rsi_regime(45) == "neutral"
    assert rsi_regime(56) == "neutral"
    assert rsi_regime(30.2) == "weak"
    assert rsi_regime(28) == "oversold"
    assert rsi_regime(None) == "unknown"


# ── EMA（#46 第九条）─────────────────────────────────────────
def test_ema_deviation_not_cross_interpretation():
    snapshot = {"holdings": [{"ticker": "AAA", "weight": 0.5,
                              "rsi_regime": "neutral", "price_vs_ema20_pct": -2.9}]}
    # 把「价格相对 EMA20 偏离 -2.9%」误写成「EMA20 跌破 EMA200」-> 必须警告
    bad = {"actions": [{"ticker": "AAA", "action": "trim", "confidence": 0.6,
                        "technical_reason": "价格 EMA20 跌破 EMA200，技术面转弱。"}]}
    _, warnings = validate_portfolio_claims(bad, snapshot, {}, [])
    assert any("EMA20" in w and ("交叉" in w or "cross" in w.lower()) for w in warnings)

    # 正确的偏离描述不应触发交叉警告
    good = {"actions": [{"ticker": "AAA", "action": "trim", "confidence": 0.6,
                         "technical_reason": "价格相对 EMA20 偏离 -2.9%，位于均线下方。"}]}
    _, warnings2 = validate_portfolio_claims(good, snapshot, {}, [])
    assert not any("EMA20" in w and ("交叉" in w or "cross" in w.lower()) for w in warnings2)


# ── Action 语义（#46 第十条）─────────────────────────────────
def test_action_semantics_reduce_favorable_in_cancel():
    weights = {"SOFI": 0.0646}
    raw = {
        "ticker": "SOFI", "action": "reduce", "priority": 1,
        "current_weight": 0.0646, "target_weight_min": 0.03, "target_weight_max": 0.05,
        "confidence": 0.8,
        "execute_if": ["触发减仓条件即执行"],
        "cancel_or_upgrade_if": ["若估值修复/利好兑现则取消减仓"],
        "further_reduce_if": ["若风险贡献继续恶化则进一步减仓"],
        "monitoring_items": ["关注成交量与波动率"],
    }
    a = normalize_action(raw, weights)
    assert a["action"] == "reduce"
    # 利好条件应在取消/升级组，而不是执行组
    assert any("若估值修复" in s for s in a["cancel_or_upgrade_if"])
    assert not any("若估值修复" in s for s in a["execute_if"])
    # 进一步减仓条件独立成组
    assert any("若风险贡献继续恶化" in s for s in a["further_reduce_if"])


# ── Safe HTML（#46 第十一条）─────────────────────────────────
def test_safe_html_badge_rendered_not_escaped():
    html = render_action_summary_table(
        [{"ticker": "AAA", "action": "trim", "priority": 1,
          "current_weight": 0.1, "target_weight_min": 0.05, "target_weight_max": 0.08,
          "confidence": 0.8}],
        {"AAA": 0.13},
    )
    # 真实 Badge 标签存在
    assert '<span class="badge"' in html
    # 不得被转义成 &lt;span class="badge"
    assert "&lt;span" not in html


# ── 风险贡献总表（#46 第十二条）──────────────────────────────
def test_risk_contribution_summary_shows_real_value():
    # 风险贡献总表应显示真实百分比，而非 N/A
    html = render_action_summary_table(
        [{"ticker": "AAA", "action": "watch", "priority": 1,
          "current_weight": 1.0, "target_weight_min": 1.0, "target_weight_max": 1.0,
          "confidence": 0.4}],
        {"AAA": 0.135},
    )
    assert "13.50%" in html
    assert "N/A" not in html


# ── Account Group（#46 第十三条）─────────────────────────────
def test_account_group_not_auto_market_risk():
    snapshot = {
        "holdings": [
            {"ticker": "AAPL", "weight": 0.52, "group": "Trade Republic",
             "beta": 1.1, "instrument_type": "EQUITY"},
            {"ticker": "MSFT", "weight": 0.48, "group": "Trading212",
             "beta": 1.0, "instrument_type": "EQUITY"},
        ]
    }
    metrics = {"top1_weight": 0.52, "top3_weight": 1.0, "hhi": 0.5,
               "effective_holdings": 2, "risk_contributions": []}
    # 默认设置：账户分组不得成为市场风险发现
    findings = generate_portfolio_rule_findings(snapshot, metrics, {})
    assert not any(f["risk_id"].startswith("CONCENTRATION_ACCOUNT") for f in findings)

    # 即便显式启用 custody_risk，52/48 也未达 >80% 阈值，仍不触发
    findings2 = generate_portfolio_rule_findings(snapshot, metrics, {"custody_risk": True})
    assert not any(f["risk_id"].startswith("CONCENTRATION_ACCOUNT") for f in findings2)

    # 仅在某券商 >80% 且启用 custody_risk 时才提示托管集中
    snap80 = {
        "holdings": [
            {"ticker": "AAPL", "weight": 0.85, "group": "Trade Republic",
             "beta": 1.1, "instrument_type": "EQUITY"},
            {"ticker": "MSFT", "weight": 0.15, "group": "Trading212",
             "beta": 1.0, "instrument_type": "EQUITY"},
        ]
    }
    findings3 = generate_portfolio_rule_findings(snap80, metrics, {"custody_risk": True})
    assert any(f["risk_id"].startswith("CONCENTRATION_ACCOUNT") for f in findings3)


# ── Confidence Cap（#46 第十四条）────────────────────────────
def test_confidence_cap_when_quality_insufficient():
    snapshot = {"holdings": [
        {"ticker": "AAA", "weight": 0.5, "instrument_type": "UNKNOWN"},
        {"ticker": "BBB", "weight": 0.5, "instrument_type": "UNKNOWN"},
    ]}
    metrics = {"top1_weight": 0.5}
    # 所有 metadata 为 UNKNOWN -> 覆盖度极低
    instrument_metadata = {t: {"instrument_type": "UNKNOWN"} for t in ("AAA", "BBB")}
    ranking = {"top_risk_tickers": ["AAA"]}

    # 场景一：benchmark / metadata / evidence 均不足 -> 置信度被压低
    advice_low = {"confidence": 0.95, "report_mode": "ai"}
    capped = _apply_confidence_cap(
        advice_low, snapshot, metrics, instrument_metadata, [], ranking, [],
    )
    assert capped["final_confidence"] <= 0.55
    assert capped["confidence_components"]["metadata_coverage"] <= 0.55
    assert capped["confidence_components"]["evidence_coverage"] <= 0.55

    # 场景二：各方面充分 -> 置信度接近模型原始值
    good_meta = {t: {"instrument_type": "EQUITY"} for t in ("AAA", "BBB")}
    good_evidence = [{
        "evidence_id": "E001", "ticker": "AAA", "related_tickers": ["AAA"],
        "scope": "ticker", "source_quality": "tier_1", "published_date": "2026-07-15",
        "recency_tier": "fresh_event", "article_fetch_ok": True,
    }]
    advice_high = {"confidence": 0.9, "report_mode": "ai"}
    capped2 = _apply_confidence_cap(
        advice_high, snapshot, metrics, good_meta, good_evidence, ranking, [],
    )
    assert capped2["final_confidence"] > 0.55
    # final = min(模型, 数据质量, 元数据, 证据覆盖, 证据新鲜, 正文验证)
    assert capped2["final_confidence"] == pytest.approx(0.9, abs=1e-6)


# ── 数据质量/证据评分函数直接校验（支撑 Confidence Cap）──────
def test_confidence_factor_scores():
    assert data_quality_score(0, 0.0) == 1.0
    assert data_quality_score(3, 0.0) < 1.0
    assert metadata_coverage_score({"A": {"instrument_type": "EQUITY"}}, ["A"]) == 1.0
    assert metadata_coverage_score({"A": {"instrument_type": "UNKNOWN"}}, ["A"]) <= 0.55
    # 空证据 -> 新鲜度降权
    assert evidence_freshness_score([]) == 0.3
    # 全覆盖 tier_1 且日期新鲜 -> 高
    fresh_ev = [{"ticker": "A", "source_quality": "tier_1", "published_date": "2026-07-15"}]
    assert evidence_coverage_score(fresh_ev, ["A"]) == 1.0
    assert evidence_freshness_score(fresh_ev) > 0.5
