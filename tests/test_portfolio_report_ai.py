# -*- coding: utf-8 -*-
"""Portfolio AI 报告管线测试（修改计划 5/9/11/12/13）。

重点验证修改计划第 10 点的核心诉求：测试必须证明报告真的包含 AI 分析，
而非仅仅『代码能跑』。覆盖：

- 工具类型识别（股票 / ETF / ETC / 指数 / 加密资产），且账户分组不再被误当行业；
- 动作-权重一致性：strict 模式报错、fallback 模式静默修正且不翻转动作；
- 确定性风险发现非空且带 affected_tickers / metric_refs；
- instrument-aware 新闻查询与候选过滤（绝不把券商/账户分组当搜索对象）；
- AI 路径与量化降级路径被清晰区分（report_mode + 中文免责声明 + 降级横幅）；
- 端到端：注入假 Agent / 假新闻，验证报告含真实 AI 结论、证据绑定、HTML 转义。
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest

from portfolio_analysis import calculate_portfolio_metrics, rank_portfolio_risks
from portfolio_analysis.instrument_metadata import build_instrument_metadata, classify_instrument
from portfolio_analysis.rules import generate_portfolio_rule_findings
from portfolio_analysis.snapshot import build_portfolio_snapshot
from portfolio_analysis.validators import (
    PortfolioAdviceValidationError, validate_portfolio_advice,
)
from daily_report.scripts.build_portfolio_report import build_html
from daily_report.run_portfolio_report import run_pipeline
from daily_report.src.stock_daily_agent.portfolio_schema import (
    default_fallback_advice, normalize_advice,
)
from daily_report.src.stock_daily_agent.portfolio_agent_runner import (
    PortfolioAgentUnavailable, PortfolioAgentOutputError, PortfolioRunContext,
    _llm_configured, run_portfolio_agent,
)
from daily_report.src.stock_daily_agent.portfolio_research import (
    build_instrument_aware_queries, filter_candidates,
)


# ── 工具类型识别 ───────────────────────────────────────────────
def test_classify_instrument_types():
    assert classify_instrument("AAPL", "Apple Inc.")["instrument_type"] == "EQUITY"
    etf = classify_instrument("SXRV.DE", "iShares Core MSCI World USD Acc")
    assert etf["instrument_type"] == "ETF"
    assert etf["underlying_index"] == "MSCI World"
    assert etf["theme"] == "MSCI World / 全球发达市场"
    # 商品 ETC（gold 必须在 ETF 之前被识别）
    etc = classify_instrument("XETRA-GOLD.DE", "Xetra-Gold")
    assert etc["instrument_type"] == "ETC"
    assert classify_instrument("BTC-USD")["instrument_type"] == "CRYPTO"
    assert classify_instrument("^GSPC", "S&P 500")["instrument_type"] == "INDEX"


def test_build_instrument_metadata_separates_account_group_from_industry():
    page = {
        "holdings": [
            {"group": "Trade Republic", "ticker": "AAPL", "name": "Apple Inc."},
            {"group": "Trading212", "ticker": "SXRV.DE", "name": "iShares Core MSCI World USD Acc"},
        ]
    }
    meta = build_instrument_metadata(page)
    aapl = meta["AAPL"]
    sxrv = meta["SXRV.DE"]
    # 账户分组（券商/账户）与行业/主题必须是不同维度
    assert aapl["account_group"] == "Trade Republic"
    assert aapl["instrument_type"] == "EQUITY"
    assert aapl["theme"] is None  # 个股未强行套用主题
    assert sxrv["account_group"] == "Trading212"
    assert sxrv["instrument_type"] == "ETF"
    assert "MSCI World" in (sxrv["theme"] or "")
    # 绝不允许把账户分组写进 sector/theme/industry
    assert aapl["sector"] is None and aapl["industry"] is None


# ── 动作-权重一致性校验 ────────────────────────────────────────
def _single_holding_snapshot(ticker="AAA", weight=0.10):
    return {"holdings": [{"ticker": ticker, "weight": weight}]}


def test_validators_strict_raises_on_inconsistent_action_weight():
    snap = _single_holding_snapshot("AAA", 0.10)
    # trim 的上限必须 < 当前权重
    with pytest.raises(PortfolioAdviceValidationError):
        validate_portfolio_advice(
            {"actions": [{"ticker": "AAA", "action": "trim",
                          "target_weight_min": 0.05, "target_weight_max": 0.10}]},
            snap, [], mode="strict")
    # exit 的上限必须接近 0
    with pytest.raises(PortfolioAdviceValidationError):
        validate_portfolio_advice(
            {"actions": [{"ticker": "AAA", "action": "exit",
                          "target_weight_min": 0.0, "target_weight_max": 0.10}]},
            snap, [], mode="strict")
    # add 的下限必须 >= 当前权重
    with pytest.raises(PortfolioAdviceValidationError):
        validate_portfolio_advice(
            {"actions": [{"ticker": "AAA", "action": "add",
                          "target_weight_min": 0.05, "target_weight_max": 0.12}]},
            snap, [], mode="strict")


def test_validators_fallback_sanitizes_without_flipping_action():
    snap = _single_holding_snapshot("AAA", 0.10)
    out = validate_portfolio_advice(
        {"actions": [{"ticker": "AAA", "action": "trim",
                      "target_weight_min": 0.05, "target_weight_max": 0.10}]},
        snap, [], mode="fallback")
    # 动作不得被静默翻转为相反方向
    assert out["actions"][0]["action"] == "trim"
    # 区间被收窄到一致范围（上限降到当前权重以下）
    assert out["actions"][0]["target_weight_max"] < 0.10


def test_validators_drops_unknown_ticker_and_share_keys():
    snap = _single_holding_snapshot("AAA", 0.10)
    out = validate_portfolio_advice({
        "confidence": 2,
        "actions": [
            {"ticker": "AAA", "action": "sell_now", "shares_to_sell": 3,
             "target_weight_min": -1, "target_weight_max": 2},
            {"ticker": "ZZZ", "action": "exit"},
        ],
    }, snap, [])
    assert len(out["actions"]) == 1
    assert out["actions"][0]["action"] == "watch"  # 非法动作 -> watch
    assert "shares_to_sell" not in out["actions"][0]  # 去除无约束的精确股数
    assert out["actions"][0]["current_weight"] == 0.10
    assert out["confidence"] == 1.0


# ── 确定性风险发现 ────────────────────────────────────────────
def test_rules_findings_nonempty_and_structured():
    snapshot = {
        "holdings": [
            {"ticker": "AAA", "weight": 0.66, "beta": 1.2, "group": "G"},
            {"ticker": "BBB.DE", "weight": 0.34, "beta": 1.0, "group": "G"},
        ],
        "data_quality": {},
    }
    metrics = {
        "top1_weight": 0.66, "top3_weight": 0.9, "hhi": 0.5, "effective_holdings": 2,
        "risk_contributions": [
            {"ticker": "AAA", "weight": 0.66, "risk_contribution": 0.8, "risk_weight_gap": 0.14},
            {"ticker": "BBB.DE", "weight": 0.34, "risk_contribution": 0.2, "risk_weight_gap": -0.14},
        ],
    }
    findings = generate_portfolio_rule_findings(snapshot, metrics, {"risk_profile": "balanced"})
    assert isinstance(findings, list) and len(findings) > 0
    assert all("affected_tickers" in f and "metric_refs" in f for f in findings)
    ids = {f["risk_id"] for f in findings}
    assert "CONCENTRATION_TOP1" in ids


# ── 新闻研究：查询生成与过滤 ──────────────────────────────────
def test_research_queries_are_instrument_aware_and_ignore_account_group():
    page = {
        "holdings": [
            {"group": "Trade Republic", "ticker": "AAPL", "name": "Apple Inc."},
            {"group": "Trading212", "ticker": "SXRV.DE", "name": "iShares Core MSCI World USD Acc"},
            {"group": "Trading212", "ticker": "BTC-USD", "name": "Bitcoin USD"},
        ]
    }
    meta = build_instrument_metadata(page)
    queries = build_instrument_aware_queries(["AAPL", "SXRV.DE", "BTC-USD"], meta, benchmark="^GSPC")

    # 宏观/基准查询始终存在，且不以账户分组为搜索对象
    assert any(q["scope"] == "macro" and q["ticker"] is None for q in queries)
    # 个股 -> 财报/分析师式查询
    assert any(q["ticker"] == "AAPL" and "earnings" in q["query"] for q in queries)
    # ETF -> 引用底层指数（MSCI World）
    assert any(q["ticker"] == "SXRV.DE" and "MSCI" in q["query"] for q in queries)
    # 加密资产 -> 监管/流动性式查询
    assert any(q["ticker"] == "BTC-USD" and "regulation" in q["query"] for q in queries)
    # 绝不把券商/账户分组（Trade Republic / Trading212）当作搜索词
    assert not any(
        "trade republic" in q["query"].lower() or "trading212" in q["query"].lower()
        for q in queries
    )


def test_filter_candidates_removes_account_platform_and_quote_only():
    candidates = [
        {"ticker": "AAPL", "url": "https://tr.example.com/aapl", "title": "Trade Republic account AAPL",
         "summary": "", "scope": "ticker", "event_hint": "general"},
        {"ticker": "AAPL", "url": "https://finance.yahoo.com/quote/AAPL", "title": "AAPL Stock Price",
         "summary": "real-time quote", "scope": "ticker", "event_hint": "general"},
        {"ticker": "AAPL", "url": "https://reuters.com/apple-earnings", "title": "AAPL beats earnings estimates",
         "summary": "Apple Inc reported record revenue and raised guidance", "scope": "ticker", "event_hint": "earnings"},
    ]
    filtered = filter_candidates(candidates, {"AAPL": {"name": "Apple Inc."}})
    assert len(filtered) == 1
    assert "beats earnings" in filtered[0]["title"]


# ── 降级建议与 Agent 可用性 ───────────────────────────────────
def test_default_fallback_advice_is_marked_quantitative_fallback():
    snapshot = _single_holding_snapshot("AAA", 0.10)
    ranking = {"top_risk_tickers": ["AAA"], "items": [{"ticker": "AAA"}]}
    advice = default_fallback_advice(snapshot, {}, ranking, reason="测试降级")
    assert advice["report_mode"] == "quantitative_fallback"
    assert advice.get("ai_analysis_available") is False
    assert advice["actions"][0]["action"] == "watch"
    assert "量化降级报告" in advice["disclaimer"]


def test_run_portfolio_agent_requires_llm_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _llm_configured("dashscope") is False
    ctx = PortfolioRunContext(run_dir=tmp_path, snapshot={}, metrics={}, ranking={})
    with pytest.raises(PortfolioAgentUnavailable):
        run_portfolio_agent(ctx, model="qwen-plus", provider="dashscope")


def test_normalize_advice_preserves_ai_fields():
    snapshot = {"holdings": [{"ticker": "AAA", "weight": 0.1}]}
    raw = {
        "report_mode": "ai", "portfolio_stance": "防御", "risk_level": "high",
        "executive_summary": ["市场情绪谨慎。"],
        "actions": [{"ticker": "AAA", "action": "trim", "current_weight": 0.1,
                     "target_weight_min": 0.05, "target_weight_max": 0.08}],
        "key_risks": [{"risk_id": "R1", "title": "集中度", "severity": "high"}],
    }
    out = normalize_advice(raw, snapshot=snapshot, metrics={}, ranking={})
    assert out["report_mode"] == "ai"
    assert out["risk_level"] == "high"
    assert out["actions"][0]["action"] == "trim"
    assert 0.0 <= out["confidence"] <= 1.0


# ── 端到端：注入假 Agent / 假新闻，验证真实 AI 分析存在 ────────
def _fixture():
    rng = np.random.default_rng(42)
    end = pd.Timestamp("2026-07-17")
    dates = pd.date_range(end=end, periods=130, freq="B")
    tickers = ["AAPL", "SXRV.DE", "BTC-USD"]
    benchmark = "^GSPC"
    data = {}
    for t in tickers + [benchmark]:
        base = rng.uniform(50, 200)
        vals = base * (1 + np.cumsum(rng.normal(0, 0.01, len(dates))))
        data[t] = vals
    close = pd.DataFrame(data, index=dates)
    close.index.name = "Date"

    holdings = [
        {"group": "Trade Republic", "ticker": "AAPL", "name": "Apple Inc.",
         "buy_price": 150, "shares": 10, "buy_currency": "USD"},
        {"group": "Trading212", "ticker": "SXRV.DE", "name": "iShares Core MSCI World USD Acc",
         "buy_price": 100, "shares": 5, "buy_currency": "EUR"},
        {"group": "Trading212", "ticker": "BTC-USD", "name": "Bitcoin USD",
         "buy_price": 30000, "shares": 0.1, "buy_currency": "USD"},
    ]
    market_rows = []
    for h in holdings:
        t = h["ticker"]
        price = float(close[t].iloc[-1])
        currency = "EUR" if t.endswith(".DE") else "USD"
        market_rows.append({
            "Ticker": t, "Name": h["name"], "Price": price, "Currency": currency,
            "1D%": 1.0, "5D%": 2.0, "1M%": -3.0, "YTD%": 10.0,
            "RSI": 55.0, "Beta": 1.1, "Volume_Ratio": 1.2,
            "Diff_EMA20%": -1.5, "Diff_EMA50%": 2.0, "Diff_EMA200%": 5.0,
        })
    fx = {"USDEUR": 0.9}
    payload = {
        "portfolio_page": {
            "id": "pf_test", "name": "Test Portfolio",
            "analysis_settings": {"base_currency": "EUR", "benchmark": "^GSPC"},
            "holdings": holdings,
        }
    }
    return payload, close, market_rows, fx


class _FakeResearch:
    def research(self, top_risk_tickers, instrument_metadata, benchmark="^GSPC", **kw):
        notes = []
        for i, t in enumerate(top_risk_tickers or []):
            notes.append({
                "evidence_id": "", "scope": "ticker", "ticker": t, "related_tickers": [t],
                "event_type": "earnings", "title": f"{t} 最新财报与指引",
                "source_name": "Reuters", "source_domain": "reuters.com",
                "published_date": "2026-07-15", "url": f"https://reuters.com/{t}",
                "source_quality": "tier_1", "source_quality_score": 90,
                "facts": [f"{t} 营收超预期。"],
                "summary_zh": f"{t} 最新季度营收超预期，但指引偏谨慎。",
                "impact_direction": "positive", "impact_horizon": "short_term",
                "portfolio_relevance": "直接影响个股。", "confidence": 0.9,
                "recency_tier": "fresh_event",
                "article_fetch_ok": False,
            })
        notes.append({
            "evidence_id": "", "scope": "portfolio", "ticker": None, "related_tickers": [],
            "event_type": "macro", "title": "^GSPC 利率与宏观展望",
            "source_name": "Bloomberg", "source_domain": "bloomberg.com",
            "published_date": "2026-07-16", "url": "https://bloomberg.com/macro",
            "source_quality": "tier_1", "source_quality_score": 88,
            "facts": ["美联储维持利率。"],
            "summary_zh": "宏观利率环境边际收紧，影响全部持仓的贴现率。",
            "impact_direction": "negative", "impact_horizon": "long_term",
            "portfolio_relevance": "系统性因素。", "confidence": 0.85,
            "recency_tier": "recent_background",
            "article_fetch_ok": False,
        })
        for i, n in enumerate(notes, start=1):
            n["evidence_id"] = f"E{i:03d}"
        return notes


def _fake_agent(ctx, model, provider, *, verbose=True):
    weights = ctx.weights()
    tickers = list(weights.keys())
    # 证据按 ticker 归位（修改计划第三轮 15：跨 ticker 绑定，action 只能引用属于自己
    # 的证据；宏观/组合级证据（ticker=None）不被任何单 tick action 引用）。
    ev_by_ticker: dict[str, list[str]] = {}
    for e in (ctx.evidence or []):
        et = str(e.get("ticker") or "").upper()
        for t in [et] + [str(x).upper() for x in (e.get("related_tickers") or [])]:
            if t:
                ev_by_ticker.setdefault(t, []).append(e["evidence_id"])
    actions = []
    for i, t in enumerate(tickers, start=1):
        w = float(weights[t])
        if i == 1:
            action, lo, hi = "trim", max(0.0, w * 0.6), max(0.0, w * 0.8)
        else:
            action, lo, hi = "hold", w * 0.95, w * 1.05
        eids = ev_by_ticker.get(t.upper(), [])
        if eids:
            news_reason = f"近期有财报与监管事项（见 {eids[0]}）。"
        elif i == 1:
            news_reason = "近期无新增重大事件，主要依据组合层面集中度。"
        else:
            news_reason = "近期无新增重大事件。"
        actions.append({
            "ticker": t, "action": action, "priority": i,
            "current_weight": w, "target_weight_min": lo, "target_weight_max": hi,
            "confidence": 0.8,
            "portfolio_reason": f"{t} 组合层面占比偏高，建议适度减仓以控制集中度。",
            "technical_reason": f"{t} 技术面跌破 EMA20。",
            "news_reason": news_reason,
            "evidence_ids": eids,
        })
    return {
        "report_mode": "ai", "portfolio_stance": "谨慎偏多", "risk_level": "medium",
        "confidence": 0.82,
        "executive_summary": [
            "组合整体估值偏高，头部持仓集中度需关注。",
            "AI 已综合技术面与新闻面给出操作建议。",
            "建议对 Top1 持仓适度减仓。",
        ],
        "portfolio_analysis": {
            "trend_view": "中期上行趋势放缓。",
            "concentration_view": "Top1 权重偏高。",
            "risk_view": "波动与回撤处于中高位。",
            "relative_performance_view": "近 5 日跑输基准。",
            "news_view": "财报季不确定性上升。",
        },
        "key_risks": [{
            "risk_id": "R001", "title": "集中度风险", "severity": "high",
            "description": "Top1 持仓权重过高。", "affected_tickers": tickers[:1],
            "metric_refs": ["top1_weight"], "evidence_ids": ["E001"],
        }],
        "actions": actions,
        "watch_items": [{"title": "宏观利率", "reason": "关注加息预期。", "affected_tickers": tickers}],
        "data_limitations": [],
        "disclaimer": "本报告仅供研究参考，不构成投资建议。",
    }


def test_run_pipeline_ai_mode_contains_real_analysis(tmp_path):
    payload, close, market_rows, fx = _fixture()
    out = tmp_path / "report.html"
    advice = run_pipeline(
        payload, run_dir=tmp_path, output=out,
        portfolio_name="Test", portfolio_id="pf_test", owner_scope="owner",
        model="qwen-plus", provider="dashscope", search_provider="none",
        close=close, market_rows=market_rows, fx_rates=fx,
        research_service=_FakeResearch(), agent_runner=_fake_agent, verbose=False,
    )
    # 1) 真的是 AI 报告
    assert advice["report_mode"] == "ai"
    # 2) 报告含真实 AI 结论与理由（不是模板拼接）
    html = out.read_text(encoding="utf-8")
    assert "AI 核心结论" in html
    assert "组合整体估值偏高" in html           # executive_summary 文本出现
    assert "组合层面占比偏高" in html           # 操作建议的 AI 理由出现
    # 3) 证据被绑定进报告
    assert "E001" in html
    # 4) 中文 + 中文免责声明
    assert 'lang="zh-CN"' in html
    assert "本报告仅供研究参考，不构成投资建议。" in html
    # 5) 不得伪装成降级报告
    assert "量化降级报告" not in html
    # 6) 中间产物齐全
    for name in ("portfolio_snapshot.json", "portfolio_metrics.json",
                 "portfolio_risk_ranking.json", "portfolio_evidence.json",
                 "portfolio_advice.json"):
        assert (tmp_path / name).exists()


def test_run_pipeline_fallback_mode_is_clearly_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_REPORT_ALLOW_QUANT_FALLBACK", "true")
    payload, close, market_rows, fx = _fixture()

    def _failing_agent(ctx, model, provider, *, verbose=True):
        raise PortfolioAgentUnavailable("测试：LLM 未配置")

    out = tmp_path / "report.html"
    advice = run_pipeline(
        payload, run_dir=tmp_path, output=out,
        portfolio_name="Test", portfolio_id="pf_test", owner_scope="owner",
        model="qwen-plus", provider="dashscope", search_provider="none",
        close=close, market_rows=market_rows, fx_rates=fx,
        research_service=_FakeResearch(), agent_runner=_failing_agent, verbose=False,
    )
    # 1) 明确降级，不伪装成 AI
    assert advice["report_mode"] == "quantitative_fallback"
    html = out.read_text(encoding="utf-8")
    assert "量化降级报告" in html
    assert "本报告为量化降级报告" in html
    # 2) 中文免责声明仍在
    assert "不构成投资建议" in html


def test_build_html_ai_vs_fallback_difference():
    snap = {
        "portfolio_name": "P", "base_currency": "EUR", "benchmark": "^GSPC",
        "holdings": [{"ticker": "AAA", "group": "G", "weight": 1.0, "market_value_base": 100}],
        "summary": {"total_market_value_base": 100}, "data_quality": {},
    }
    metrics = {"top1_weight": 1.0, "risk_contributions": [{"ticker": "AAA", "risk_contribution": 1.0}]}
    ranking = {"items": [{"ticker": "AAA", "risk_priority_score": 1.0}]}
    ai_html = build_html(
        snap, metrics, ranking,
        {"actions": [{"ticker": "AAA", "action": "hold"}], "report_mode": "ai",
         "executive_summary": ["AI 给出结论。"]},
        [{"evidence_id": "E001", "title": "新闻", "url": "https://x.com"}],
    )
    fb_html = build_html(
        snap, metrics, ranking,
        {"actions": [{"ticker": "AAA", "action": "watch"}], "report_mode": "quantitative_fallback"},
        [],
    )
    assert "量化降级报告" not in ai_html
    assert "量化降级报告" in fb_html



def test_zero_accepted_evidence_skips_agent_and_publishes_observation(tmp_path, monkeypatch):
    """Rejected candidates must not trigger an expensive/unsafe Agent call."""
    monkeypatch.setenv("PORTFOLIO_REPORT_ALLOW_QUANT_FALLBACK", "false")
    payload, close, market_rows, fx = _fixture()

    class RejectedOnlyResearch:
        def research(self, *args, **kwargs):
            candidate = {
                "ticker": "AAPL",
                "title": "Generic AAPL stock comparison",
                "url": "https://example.com/aapl-comparison",
                "materiality_accepted": False,
                "accept": False,
                "reject_reason": "primary_entity_score_below_0.7",
            }
            return {
                "status": "insufficient_coverage",
                "evidence": [],
                "raw_results": [candidate],
                "filtered_results": [candidate],
                "diagnostics": {
                    "status": "insufficient_coverage",
                    "raw_results_count": 1,
                    "filtered_results_count": 1,
                    "selected_evidence_count": 1,
                    "materiality_accepted_count": 0,
                    "top_risk_coverage": 0.0,
                    "risk_weighted_coverage": 0.0,
                },
            }

    called = {"agent": False}

    def should_not_run(*args, **kwargs):
        called["agent"] = True
        raise AssertionError("Agent must be skipped when Accepted Evidence is empty")

    out = tmp_path / "zero-accepted.html"
    advice = run_pipeline(
        payload, run_dir=tmp_path, output=out,
        portfolio_name="Test", portfolio_id="pf_test", owner_scope="owner",
        model="qwen-plus", provider="dashscope", search_provider="none",
        close=close, market_rows=market_rows, fx_rates=fx,
        research_service=RejectedOnlyResearch(), agent_runner=should_not_run, verbose=False,
    )

    assert called["agent"] is False
    assert advice["report_mode"] == "quantitative_fallback"
    assert advice["observation_only"] is True
    html = out.read_text(encoding="utf-8")
    assert "量化风险观察结论" in html
    assert "Accepted Evidence" in html


def test_agent_strict_validation_failure_auto_falls_back(tmp_path, monkeypatch):
    """A twice-invalid AI payload must degrade safely instead of failing the report."""
    monkeypatch.setenv("PORTFOLIO_REPORT_ALLOW_QUANT_FALLBACK", "false")
    monkeypatch.setenv("PORTFOLIO_REPORT_ALLOW_OUTPUT_VALIDATION_FALLBACK", "true")
    payload, close, market_rows, fx = _fixture()

    def invalid_agent(*args, **kwargs):
        raise PortfolioAgentOutputError(
            "Portfolio Agent validation failed: Top5 成员与 Python 注册结果不一致；"
            "仅有价格相关性而无 ETF holdings 数据"
        )

    out = tmp_path / "invalid-agent-fallback.html"
    advice = run_pipeline(
        payload, run_dir=tmp_path, output=out,
        portfolio_name="Test", portfolio_id="pf_test", owner_scope="owner",
        model="qwen-plus", provider="dashscope", search_provider="none",
        close=close, market_rows=market_rows, fx_rates=fx,
        research_service=_FakeResearch(), agent_runner=invalid_agent, verbose=False,
    )

    assert advice["report_mode"] == "quantitative_fallback"
    assert advice["observation_only"] is True
    html = out.read_text(encoding="utf-8")
    assert "量化降级报告" in html
    assert "Top5 成员与 Python 注册结果不一致" not in html
    assert "底层持仓重复" not in html
