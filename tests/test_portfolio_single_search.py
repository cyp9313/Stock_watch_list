from __future__ import annotations

import json

from daily_report.scripts.build_portfolio_report import build_html
from daily_report.src.stock_daily_agent.portfolio_single_search import (
    _canonical_url,
    run_portfolio_single_search,
)
from portfolio_analysis.report_quality import evaluate_report_quality


def _inputs():
    snapshot = {
        "portfolio_name": "Test Portfolio",
        "report_date": "2026-07-19",
        "as_of": "2026-07-19T22:00:00+02:00",
        "base_currency": "EUR",
        "benchmark": "^GSPC",
        "summary": {
            "total_market_value_base": 10000.0,
            "total_cost_basis_base": 9000.0,
            "profit_loss_base": 1000.0,
            "profit_loss_pct": 11.11,
        },
        "holdings": [
            {
                "ticker": "SOFI",
                "name": "SoFi Technologies",
                "weight": 0.60,
                "rsi": 48.0,
                "price_vs_ema50_pct": -3.0,
                "return_5d": -2.0,
                "profit_loss_pct": 10.0,
            },
            {
                "ticker": "TSLA",
                "name": "Tesla",
                "weight": 0.40,
                "rsi": 52.0,
                "price_vs_ema50_pct": 1.0,
                "return_5d": 1.0,
                "profit_loss_pct": 5.0,
            },
        ],
        "run_timeline": {"snapshot_completed_at": "2026-07-19T22:00:00+02:00"},
        "data_cutoffs": {
            "equity": "2026-07-17",
            "etf": "2026-07-17",
            "crypto": "2026-07-19",
            "benchmark": "2026-07-17",
        },
        "data_quality": {},
    }
    metrics = {
        "portfolio_risk_score": 54,
        "portfolio_risk_level": "medium_high",
        "portfolio_beta": 0.98,
        "portfolio_beta_status": "actual",
        "max_drawdown_63d": -0.09,
        "max_drawdown_252d": -0.22,
        "aggregates": {"below_ema50_weight": 0.60},
        "risk_contributions": [
            {"ticker": "SOFI", "risk_contribution": 0.65, "risk_weight_gap": 0.05},
            {"ticker": "TSLA", "risk_contribution": 0.35, "risk_weight_gap": -0.05},
        ],
        "holdings_detail": {},
        "relative_returns": {},
    }
    ranking = {
        "top_risk_tickers": ["SOFI", "TSLA"],
        "items": [
            {"ticker": "SOFI", "risk_score": 0.8, "risk_priority_score": 0.8},
            {"ticker": "TSLA", "risk_score": 0.7, "risk_priority_score": 0.7},
        ],
    }
    metadata = {
        "SOFI": {"name": "SoFi Technologies", "official_domains": ["investors.sofi.com"]},
        "TSLA": {"name": "Tesla", "official_domains": ["ir.tesla.com"]},
    }
    return snapshot, metrics, ranking, metadata


def _response(*, content, sources, usage=None):
    return {
        "request_id": "req-1",
        "output": {
            "choices": [{"message": {"content": content}}],
            "search_info": {"search_results": sources},
        },
        "usage": usage or {"input_tokens": 1000, "output_tokens": 300, "total_tokens": 1300},
    }


def test_canonical_url_removes_tracking_but_preserves_article_parameters():
    assert _canonical_url("https://Example.com/a/?id=42&utm_source=x&fbclid=y") == "https://example.com/a?id=42"


def test_single_search_makes_exactly_one_turbo_call_and_binds_source_index(monkeypatch):
    snapshot, metrics, ranking, metadata = _inputs()
    calls = []
    payload = {
        "evidence": [{
            "ticker": "SOFI",
            "source_index": 7,
            # These free-form identity fields must be ignored even if a model emits them.
            "title": "Invented model title",
            "published_date": "2026-07-01",
            "url": "https://invented.example/not-used",
            "summary": "SoFi 公布了一项与公司经营相关的明确更新，可用于组合风险观察。",
            "materiality": "high",
            "impact": "positive",
            "confidence": 0.8,
        }],
        "portfolio_assessment": {
            "portfolio_stance": "observe",
            "confidence": 0.6,
            "executive_summary": ["组合保持观察。"],
        },
        "actions": [{
            "ticker": "SOFI", "action": "watch", "reason": "等待后续确认。",
            "confidence": 0.6, "evidence_source_indices": [7],
        }],
    }

    def fake_call(**kwargs):
        calls.append(kwargs)
        return _response(
            content=json.dumps(payload, ensure_ascii=False),
            sources=[{
                "index": 7,
                "title": "SoFi announces a material update",
                "url": "https://investors.sofi.com/news/update?utm_source=dashscope",
                "published_time": "2026-07-18T08:00:00Z",
                "snippet": "official update",
            }],
        )

    result = run_portfolio_single_search(
        snapshot=snapshot,
        metrics=metrics,
        ranking=ranking,
        instrument_metadata=metadata,
        generation_call=fake_call,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == "deepseek-v4-flash"
    assert call["enable_search"] is True
    assert call["search_options"] == {
        "forced_search": True,
        "search_strategy": "turbo",
        "enable_source": True,
    }
    assert "zero items is allowed" in call["messages"][0]["content"]
    assert "Never invent, rewrite, shorten, or guess a source identity" in call["messages"][0]["content"]
    assert "Search this exact company only" in call["messages"][1]["content"]
    assert "after:2026-06-19" in call["messages"][1]["content"]
    assert "Published on or before 2026-07-19" in call["messages"][1]["content"]
    assert "SoFi Technologies" in call["messages"][1]["content"]
    assert len(result["accepted_evidence"]) == 1
    evidence = result["accepted_evidence"][0]
    assert evidence["dashscope_source_index"] == 7
    assert evidence["title"] == "SoFi announces a material update"
    assert evidence["url"] == "https://investors.sofi.com/news/update?utm_source=dashscope"
    assert evidence["published_date"] == "2026-07-18"
    assert evidence["verification_method"] == "dashscope_local_source_binding"
    assert evidence["source_verified"] is True
    assert result["advice"]["actions"][0]["evidence_ids"] == ["E001"]
    assert result["diagnostics"]["search_call_count"] == 1
    assert result["diagnostics"]["external_search_call_count"] == 0
    assert result["diagnostics"]["retry_count"] == 0
    assert result["diagnostics"]["gap_search_count"] == 0
    assert result["diagnostics"]["dashscope_sources"][0]["source_index"] == 7
    assert result["diagnostics"]["relevant_source_count"] == 1
    assert result["diagnostics"]["irrelevant_source_count"] == 0



def test_search_prompt_is_short_entity_focused_and_schema_is_isolated_from_user_message():
    snapshot, metrics, ranking, metadata = _inputs()
    calls = []
    run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **kwargs: (
            calls.append(kwargs)
            or _response(content=json.dumps({"evidence": [], "portfolio_assessment": {}, "actions": []}), sources=[])
        ),
    )
    system_prompt = calls[0]["messages"][0]["content"]
    user_prompt = calls[0]["messages"][1]["content"]
    assert "Required JSON schema" in system_prompt
    assert "portfolio_risk_score" not in user_prompt
    assert "Required JSON schema" not in user_prompt
    assert "portfolio_risk_score" not in system_prompt
    assert "SoFi Technologies" in user_prompt
    assert "Tesla" not in user_prompt
    assert "QUERY 1:" in user_prompt and "QUERY 2:" not in user_prompt
    assert "Search this exact company only" in user_prompt
    assert len(user_prompt) < 1800


def test_citation_marker_source_ref_resolves_dashscope_source():
    snapshot, metrics, ranking, metadata = _inputs()
    payload = {
        "evidence": [{
            "ticker": "SOFI",
            "source_ref": "[ref_7]",
            "summary": "SoFi 发布一项明确经营更新，可用于观察其增长与信用风险变化。",
            "materiality": "high",
            "impact": "mixed",
            "confidence": 0.75,
        }],
        "portfolio_assessment": {},
        "actions": [],
    }
    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **_: _response(
            content=json.dumps(payload, ensure_ascii=False),
            sources=[{
                "index": 7,
                "title": "SoFi announces operating update",
                "url": "https://investors.sofi.com/news/2026/07/18/operating-update",
                "published_time": "2026-07-18T08:00:00Z",
            }],
        ),
        article_fetch_call=lambda url, **_: {
            "url": url,
            "final_url": url,
            "ok": True,
            "title": "SoFi贷款发放暴增68%,这2家数字银行营收还在狂飙",
            "text": "SoFi贷款发放同比大幅增长，数字银行业务继续扩张。" * 20,
            "meta_description": "SoFi贷款发放同比大幅增长，数字银行业务继续扩张。",
            "published_date": "",
            "article_text_quality_ok": True,
        },
    )
    assert len(result["accepted_evidence"]) == 1
    assert result["accepted_evidence"][0]["dashscope_source_index"] == 7
    assert result["accepted_evidence"][0]["source_binding_method"] == "citation_or_source_index"


def test_cjk_adjacent_latin_alias_and_undated_real_article_are_published_as_reference():
    snapshot, metrics, ranking, metadata = _inputs()
    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **_: _response(
            content=json.dumps({
                "evidence": [{
                    "ticker": "SOFI",
                    "source_title_hint": "unmatched model hint",
                    "summary": "模型未能正确绑定来源，但真实文章仍应由本地程序作为背景来源发布。",
                }],
                "portfolio_assessment": {},
                "actions": [],
            }, ensure_ascii=False),
            sources=[
                {"index": 1, "title": "SOFI", "url": "https://gubaf10.eastmoney.com/SOFI"},
                {"index": 2, "title": "SoFi Technologies Inc. (SOFI)", "url": "https://xueqiu.com/S/SOFI"},
                {
                    "index": 9,
                    "title": "SoFi贷款发放暴增68%,这2家数字银行营收还在狂飙",
                    "url": "https://www.163.com/dy/article/L25MTAAO05561FZD.html",
                    "snippet": "SoFi贷款发放同比大幅增长，数字银行业务继续扩张。",
                },
                {"index": 10, "title": "软银集团", "url": "https://www.36kr.com/tags/softbank"},
            ],
        ),
        article_fetch_call=lambda url, **_: {
            "url": url,
            "final_url": url,
            "ok": True,
            "title": "SoFi贷款发放暴增68%,这2家数字银行营收还在狂飙",
            "text": "SoFi贷款发放同比大幅增长，数字银行业务继续扩张。" * 20,
            "meta_description": "SoFi贷款发放同比大幅增长，数字银行业务继续扩张。",
            "published_date": "",
            "article_text_quality_ok": True,
        },
    )
    source9 = next(x for x in result["diagnostics"]["dashscope_sources"] if x["source_index"] == 9)
    assert source9["relevance_status"] == "relevant"
    assert source9["matched_tickers"] == ["SOFI"]
    assert source9["page_type"] == "company_news_undated"
    assert source9["citable_as_reference"] is True
    assert result["accepted_evidence"] == []
    assert result["status"] == "source_notes_only"
    assert len(result["reference_evidence"]) == 1
    reference = result["reference_evidence"][0]
    assert reference["dashscope_source_index"] == 9
    assert reference["published_date"] == ""
    assert reference["publication_date_status"] == "not_provided_by_dashscope"
    assert reference["verification_level_zh"] == "来源 URL 已验证·日期未提供"

    html = build_html(
        snapshot, metrics, ranking, result["advice"], [],
        instrument_metadata=metadata,
        settings={"model": "deepseek-v4-flash"},
        research_diagnostics=result["diagnostics"],
        report_quality={"publishable": True, "observation_only": True, "quality_score": 0.0},
        reference_evidence=result["reference_evidence"],
    )
    assert "SoFi贷款发放暴增68%" in html
    assert "日期未提供" in html
    assert "背景来源·非决策证据" in html
    assert "https://www.163.com/dy/article/L25MTAAO05561FZD.html" in html


def test_nested_dashscope_publish_metadata_is_used_for_source_date():
    snapshot, metrics, ranking, metadata = _inputs()
    payload = {
        "evidence": [{
            "ticker": "SOFI",
            "source_ref": "[ref_3]",
            "summary": "SoFi 发布公司经营更新，来源日期位于 DashScope 嵌套元数据中。",
        }],
        "portfolio_assessment": {},
        "actions": [],
    }
    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **_: _response(
            content=json.dumps(payload, ensure_ascii=False),
            sources=[{
                "index": 3,
                "title": "SoFi operating update",
                "url": "https://investors.sofi.com/news/operating-update",
                "metadata": {"publishTime": "2026-07-18T09:30:00Z"},
            }],
        ),
    )
    assert result["accepted_evidence"][0]["published_date"] == "2026-07-18"

def test_model_can_bind_by_exact_source_title_without_citation_index():
    snapshot, metrics, ranking, metadata = _inputs()
    payload = {
        "evidence": [{
            "ticker": "SOFI",
            "source_title_hint": "SoFi announces second-quarter results",
            "summary": "SoFi 发布季度经营更新，相关内容可用于观察公司增长与信用风险变化。",
            "materiality": "high",
            "impact": "mixed",
            "confidence": 0.75,
        }],
        "portfolio_assessment": {},
        "actions": [],
    }
    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **_: _response(
            content=json.dumps(payload, ensure_ascii=False),
            sources=[{
                "index": 4,
                "title": "SoFi announces second-quarter results",
                "url": "https://investors.sofi.com/news/2026/07/18/results",
                "published_date": "2026-07-18",
                "snippet": "SoFi Technologies reported quarterly results.",
            }],
        ),
    )
    assert len(result["accepted_evidence"]) == 1
    evidence = result["accepted_evidence"][0]
    assert evidence["source_binding_method"] == "source_title_hint"
    assert evidence["dashscope_source_index"] == 4
    assert evidence["url"] == "https://investors.sofi.com/news/2026/07/18/results"


def test_single_relevant_source_can_bind_without_index_or_title_hint():
    snapshot, metrics, ranking, metadata = _inputs()
    ranking["top_risk_tickers"] = ["TSLA", "SOFI"]
    payload = {
        "evidence": [{
            "ticker": "TSLA",
            "summary": "Tesla 发布一项明确的公司经营更新，事件可用于观察组合中的高波动风险。",
            "materiality": "medium",
            "impact": "neutral",
            "confidence": 0.65,
        }],
        "portfolio_assessment": {},
        "actions": [],
    }
    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **_: _response(
            content=json.dumps(payload, ensure_ascii=False),
            sources=[{
                "index": 2,
                "title": "Tesla publishes operating update",
                "url": "https://ir.tesla.com/press-release/2026-07-18-update",
                "published_date": "2026-07-18",
            }],
        ),
    )
    assert len(result["accepted_evidence"]) == 1
    assert result["accepted_evidence"][0]["source_binding_method"] == "unique_entity_article_source"


def test_irrelevant_dashscope_source_is_not_bindable_to_requested_ticker():
    snapshot, metrics, ranking, metadata = _inputs()
    payload = {
        "evidence": [{
            "ticker": "SOFI",
            "summary": "模型试图把一篇无关的区块链安全文章绑定到 SoFi，但本地实体匹配必须拒绝。",
        }],
        "portfolio_assessment": {},
        "actions": [],
    }
    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **_: _response(
            content=json.dumps(payload, ensure_ascii=False),
            sources=[{
                "index": 1,
                "title": "每周区块链安全要闻",
                "url": "https://cloud.tencent.com/developer/article/2707954",
                "published_date": "2026-07-18",
            }],
        ),
    )
    assert result["accepted_evidence"] == []
    assert result["diagnostics"]["relevant_source_count"] == 0
    assert result["diagnostics"]["irrelevant_source_count"] == 1
    assert result["diagnostics"]["invalid_evidence_reasons"]["no_relevant_dashscope_source_for_ticker"] == 1


def test_deepseek_single_search_prioritizes_one_direct_equity_over_etfs():
    snapshot, metrics, ranking, metadata = _inputs()
    snapshot["holdings"] = [
        {"ticker": "WNUC.DE", "instrument_type": "ETF", "weight": 0.3},
        {"ticker": "SOFI", "instrument_type": "EQUITY", "weight": 0.25},
        {"ticker": "TSLA", "instrument_type": "EQUITY", "weight": 0.2},
        {"ticker": "ORCL", "instrument_type": "EQUITY", "weight": 0.15},
        {"ticker": "LYMS.DE", "instrument_type": "ETF", "weight": 0.1},
    ]
    ranking["top_risk_tickers"] = ["WNUC.DE", "SOFI", "TSLA", "ORCL", "LYMS.DE"]
    ranking["items"] = [{"ticker": t, "risk_score": 0.8} for t in ranking["top_risk_tickers"]]
    metadata.update({
        "WNUC.DE": {"name": "WisdomTree Uranium and Nuclear Energy UCITS ETF", "instrument_type": "ETF"},
        "ORCL": {"name": "Oracle Corporation", "official_domains": ["investor.oracle.com"]},
        "LYMS.DE": {"name": "Amundi Core Nasdaq-100 Swap UCITS ETF Acc", "instrument_type": "ETF"},
    })
    calls = []
    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **kwargs: (
            calls.append(kwargs)
            or _response(content=json.dumps({"evidence": [], "portfolio_assessment": {}, "actions": []}), sources=[])
        ),
    )
    assert result["diagnostics"]["search_target_tickers"] == ["SOFI"]
    assert result["diagnostics"]["omitted_search_tickers"] == ["WNUC.DE", "TSLA", "ORCL", "LYMS.DE"]
    assert result["diagnostics"]["search_target_strategy"] == "single_exact_entity_for_third_party_model"
    user_prompt = calls[0]["messages"][1]["content"]
    assert "SoFi Technologies" in user_prompt
    assert "Tesla" not in user_prompt
    assert "Oracle Corporation" not in user_prompt
    assert "WisdomTree Uranium" not in user_prompt


def test_bad_model_source_index_is_rejected_but_fresh_official_source_is_kept_deterministically():
    snapshot, metrics, ranking, metadata = _inputs()
    calls = 0
    payload = {
        "evidence": [{
            "ticker": "SOFI",
            "source_index": 99,
            "source_title_hint": "No matching provider title",
            "summary": "这是一条长度足够但来源索引并未出现在 DashScope 来源列表中的事件摘要。",
        }],
        "portfolio_assessment": {},
        "actions": [],
    }

    def fake_call(**kwargs):
        nonlocal calls
        calls += 1
        return _response(
            content=json.dumps(payload, ensure_ascii=False),
            sources=[
                {
                    "index": 1,
                    "title": "SoFi operating results update",
                    "url": "https://investors.sofi.com/news/real",
                    "published_date": "2026-07-18",
                },
                {
                    "index": 2,
                    "title": "SoFi financing update",
                    "url": "https://investors.sofi.com/news/financing",
                    "published_date": "2026-07-17",
                },
            ],
        )

    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata, generation_call=fake_call,
    )
    assert calls == 1
    assert len(result["accepted_evidence"]) == 1
    assert result["accepted_evidence"][0]["verification_method"] == "deterministic_dashscope_source_event"
    assert result["diagnostics"]["invalid_evidence_reasons"]["source_index_not_in_dashscope_sources"] == 1
    assert result["diagnostics"]["deterministic_evidence_count"] == 1


def test_source_date_is_authoritative_and_missing_source_date_is_rejected():
    snapshot, metrics, ranking, metadata = _inputs()
    payload = {
        "evidence": [{
            "ticker": "SOFI",
            "source_index": "[ref_1]",
            "published_date": "2026-07-18",  # ignored
            "summary": "模型声称有日期，但实际 DashScope 来源没有日期，因此必须由本地程序拒绝。",
        }],
        "portfolio_assessment": {},
        "actions": [],
    }
    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking, instrument_metadata=metadata,
        generation_call=lambda **_: _response(
            content=json.dumps(payload, ensure_ascii=False),
            sources=[{
                "index": 1,
                "title": "Source without date",
                "url": "https://investors.sofi.com/news/date-test",
            }],
        ),
    )
    assert result["accepted_evidence"] == []
    assert result["diagnostics"]["invalid_evidence_reasons"]["invalid_or_missing_source_date"] == 1


def test_source_date_can_be_inferred_from_dashscope_url_without_trusting_model_date():
    snapshot, metrics, ranking, metadata = _inputs()
    payload = {
        "evidence": [{
            "ticker": "SOFI",
            "source_title_hint": "SoFi files operating update",
            "published_date": "2099-01-01",  # ignored
            "summary": "SoFi 发布经营更新，来源 URL 自带发布日期，可由本地程序安全提取。",
        }],
        "portfolio_assessment": {},
        "actions": [],
    }
    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking, instrument_metadata=metadata,
        generation_call=lambda **_: _response(
            content=json.dumps(payload, ensure_ascii=False),
            sources=[{
                "index": 5,
                "title": "SoFi files operating update",
                "url": "https://investors.sofi.com/news/2026/07/18/operating-update",
            }],
        ),
    )
    assert result["accepted_evidence"][0]["published_date"] == "2026-07-18"


def test_empty_evidence_is_valid_and_does_not_trigger_retry():
    snapshot, metrics, ranking, metadata = _inputs()
    calls = 0

    def fake_call(**kwargs):
        nonlocal calls
        calls += 1
        return _response(
            content=json.dumps({"evidence": [], "portfolio_assessment": {}, "actions": []}),
            sources=[{
                "index": 1,
                "title": "Unrelated result",
                "url": "https://example.com/unrelated",
                "published_date": "2026-07-18",
            }],
        )

    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata, generation_call=fake_call,
    )
    assert calls == 1
    assert result["status"] == "no_valid_evidence"
    assert result["accepted_evidence"] == []
    assert result["rejected_evidence"] == []
    assert result["diagnostics"]["model_evidence_count"] == 0
    assert result["diagnostics"]["retry_count"] == 0


def test_empty_model_evidence_publishes_one_dated_relevant_source_as_background_reference():
    snapshot, metrics, ranking, metadata = _inputs()
    calls = 0

    def fake_call(**kwargs):
        nonlocal calls
        calls += 1
        return _response(
            content=json.dumps({"evidence": [], "portfolio_assessment": {}, "actions": []}),
            sources=[
                {
                    "index": 1,
                    "title": "SOFI",
                    "url": "https://stockpage.10jqka.com.cn/SOFI/",
                },
                {
                    "index": 4,
                    "title": "SoFi Technologies7月17日成交额为15.44亿美元 在当日美股中排第80名",
                    "url": "https://stock.10jqka.com.cn/usstock/20260718/c678265427.shtml",
                    "snippet": "SoFi Technologies 7月17日成交活跃，文章记录当日成交额和市场排名。",
                },
                {
                    "index": 5,
                    "title": "SoFi Technologies7月17日成交额为15.41亿美元 在当日美股中排第80名",
                    "url": "https://stock.10jqka.com.cn/20260718/c678265427.shtml",
                    "snippet": "SoFi Technologies 7月17日成交活跃，文章记录当日成交额和市场排名。",
                },
            ],
        )

    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata, generation_call=fake_call,
    )

    assert calls == 1
    assert result["status"] == "source_notes_only"
    assert result["accepted_evidence"] == []
    assert len(result["reference_evidence"]) == 1
    reference = result["reference_evidence"][0]
    assert reference["reference_id"] == "R001"
    assert reference["ticker"] == "SOFI"
    assert reference["published_date"] == "2026-07-18"
    assert reference["source_note_only"] is True
    assert reference["decision_eligible"] is False
    assert reference["source_verified"] is True
    assert reference["event_type"] == "market_activity"
    assert result["diagnostics"]["reference_evidence_count"] == 1
    assert result["diagnostics"]["local_reference_fallback_used"] is True
    assert result["diagnostics"]["search_call_count"] == 1
    assert result["diagnostics"]["retry_count"] == 0

    html = build_html(
        snapshot, metrics, ranking, result["advice"], [],
        instrument_metadata=metadata,
        settings={"model": "deepseek-v4-flash"},
        research_diagnostics=result["diagnostics"],
        report_quality={"publishable": True, "observation_only": True, "quality_score": 0.0},
        reference_evidence=result["reference_evidence"],
    )
    assert "可引用背景来源：1" in html
    assert "SOFI 已验证联网来源（仅作背景）" in html
    assert "背景来源·非决策证据" in html
    assert "来源 URL 已验证" in html
    assert "来源附录（决策证据与背景来源）" in html
    assert "仅有可引用背景来源" in html
    assert "新闻候选仅保留在诊断附件中" not in html



def test_invalid_json_does_not_trigger_a_second_call():
    snapshot, metrics, ranking, metadata = _inputs()
    calls = 0

    def fake_call(**kwargs):
        nonlocal calls
        calls += 1
        return _response(content="not-json", sources=[])

    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata, generation_call=fake_call,
    )
    assert calls == 1
    assert result["status"] == "invalid_model_output"
    assert result["diagnostics"]["response_json_parse_error"] == "invalid_json"
    assert result["diagnostics"]["retry_count"] == 0


def test_quality_gate_allows_source_notes_only_as_observation():
    snapshot, metrics, ranking, metadata = _inputs()
    research = {
        "status": "source_notes_only",
        "accepted_evidence": [],
        "evidence": [],
        "reference_evidence": [{"reference_id": "R001", "ticker": "SOFI"}],
        "diagnostics": {
            "status": "source_notes_only",
            "search_call_count": 1,
            "external_search_call_count": 0,
            "retry_count": 0,
            "gap_search_count": 0,
            "accepted_top_risk_coverage": 0.0,
            "accepted_risk_weighted_coverage": 0.0,
        },
    }
    advice = {"confidence": 0.0, "actions": []}
    quality = evaluate_report_quality(snapshot, metrics, research, advice, {})
    assert quality["publishable"] is True
    assert quality["observation_only"] is True
    assert any("可引用背景来源" in warning for warning in quality["warnings"])
    assert not any("状态异常" in warning for warning in quality["warnings"])



def test_quality_gate_blocks_call_budget_violation():
    snapshot, metrics, ranking, metadata = _inputs()
    research = {
        "status": "no_valid_evidence",
        "accepted_evidence": [],
        "evidence": [],
        "diagnostics": {
            "status": "no_valid_evidence",
            "search_call_count": 2,
            "external_search_call_count": 0,
            "retry_count": 0,
            "gap_search_count": 0,
        },
    }
    advice = {"confidence": 0.0, "actions": []}
    quality = evaluate_report_quality(snapshot, metrics, research, advice, {})
    assert quality["publishable"] is False
    assert "dashscope_search_call_budget_exceeded" in quality["blocking_errors"]


def test_html_shows_single_search_sources_cost_and_safe_same_day_cutoff():
    snapshot, metrics, ranking, metadata = _inputs()
    snapshot["data_cutoffs"].update({
        "equity": "2026-07-19",
        "etf": "2026-07-19",
        "benchmark": "2026-07-19",
    })
    advice = {
        "report_mode": "quantitative_fallback",
        "observation_only": True,
        "portfolio_stance": "observe",
        "risk_level": "medium_high",
        "confidence": 0.0,
        "final_confidence": 0.0,
        "executive_summary": ["量化观察。"],
        "portfolio_analysis": {},
        "actions": [],
        "data_limitations": [],
        "disclaimer": "仅供研究。",
        "confidence_components": {"evidence_coverage": 0.0},
    }
    diagnostics = {
        "research_mode": "dashscope_single_search",
        "status": "no_valid_evidence",
        "model": "deepseek-v4-flash",
        "search_strategy": "turbo",
        "search_call_count": 1,
        "max_search_calls": 1,
        "external_search_call_count": 0,
        "retry_count": 0,
        "gap_search_count": 0,
        "raw_source_count": 8,
        "unique_source_count": 7,
        "relevant_source_count": 1,
        "relevant_article_source_count": 1,
        "landing_or_index_source_count": 0,
        "irrelevant_source_count": 6,
        "search_target_tickers": ["SOFI", "TSLA"],
        "omitted_search_tickers": [],
        "model_evidence_count": 2,
        "valid_evidence_count": 0,
        "invalid_evidence_count": 2,
        "input_tokens": 1000,
        "output_tokens": 200,
        "total_tokens": 1200,
        "source_binding_validation": "local exact-entity matching plus source-title hint",
        "source_url_validation": "URL injected from DashScope source",
        "date_validation": "source date <= 30 days",
        "dashscope_sources": [{
            "source_index": 4,
            "published_date": "2026-07-18",
            "source_domain": "investors.sofi.com",
            "title": "SoFi official update",
            "url": "https://investors.sofi.com/news/update",
            "matched_tickers": ["SOFI"],
            "relevance_status": "relevant",
        }],
        "invalid_evidence_reasons": {"source_index_not_in_dashscope_sources": 2},
        "invalid_evidence_items": [{
            "ticker": "SOFI", "source_index": 99, "published_date": None,
            "url": None, "title": None,
            "reasons": ["source_index_not_in_dashscope_sources"],
        }],
    }
    html = build_html(
        snapshot, metrics, ranking, advice, [], instrument_metadata=metadata,
        settings={"model": "deepseek-v4-flash"}, research_diagnostics=diagnostics,
        report_quality={"publishable": True, "observation_only": True, "quality_score": 0.0},
    )
    assert "搜索调用：1/1" in html
    assert "外部搜索 API：0" in html
    assert "固定策略：turbo" in html
    assert "Token：输入 1000" in html
    assert "DashScope 实际返回来源" in html
    assert "SoFi official update" in html
    assert "直接相关 1（文章页 1，导航/索引页 0），无关或歧义 6" in html
    assert "本轮单次搜索目标：SOFI、TSLA" in html
    assert "匹配标的" in html
    assert "来源索引不在 DashScope 返回来源中" in html
    assert "2026-07-19 最新可用（截至 22:00 Europe/Berlin，可能含盘中数据）" in html
    assert "2026-07-19 收盘" not in html
    assert "Materiality" not in html
    assert "Gap Analyzer" not in html
    assert "Planner 模式" not in html


def test_run_pipeline_integration_uses_injected_single_search_only(monkeypatch, tmp_path):
    import sys
    import types
    import numpy as np
    import pandas as pd

    yfinance_stub = types.ModuleType("yfinance")
    yfinance_stub.download = lambda *args, **kwargs: pd.DataFrame()
    yfinance_stub.Ticker = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "yfinance", yfinance_stub)

    from daily_report.run_portfolio_report import run_pipeline
    from daily_report.src.stock_daily_agent.portfolio_schema import default_fallback_advice

    index = pd.bdate_range("2025-01-01", periods=390)
    rng = np.random.default_rng(11)
    close = pd.DataFrame({
        "AAA": 100 * np.cumprod(1 + rng.normal(0.0004, 0.012, len(index))),
        "BBB.DE": 80 * np.cumprod(1 + rng.normal(0.0002, 0.010, len(index))),
        "^GSPC": 5000 * np.cumprod(1 + rng.normal(0.0003, 0.008, len(index))),
    }, index=index)
    market_rows = [
        {"Ticker": "AAA", "Price": float(close["AAA"].iloc[-1]), "Currency": "EUR", "RSI": 48},
        {"Ticker": "BBB.DE", "Price": float(close["BBB.DE"].iloc[-1]), "Currency": "EUR", "RSI": 55},
    ]
    payload = {
        "portfolio_page": {
            "id": "pf-v2",
            "name": "Portfolio V2",
            "analysis_settings": {
                "base_currency": "EUR", "benchmark": "^GSPC",
                "risk_profile": "growth", "investment_horizon": "12m+",
                "research_max_tickers": 2,
            },
            "holdings": [
                {"group": "Test", "ticker": "AAA", "buy_price": 90, "shares": 30, "buy_currency": "EUR"},
                {"group": "Test", "ticker": "BBB.DE", "buy_price": 75, "shares": 20, "buy_currency": "EUR"},
            ],
        },
        "market_rows": market_rows,
        "fx_rates": {},
    }
    calls = 0

    def injected_single_search(**kwargs):
        nonlocal calls
        calls += 1
        advice = default_fallback_advice(
            kwargs["snapshot"], kwargs["metrics"], kwargs["ranking"],
            reason="Integration test without network.",
        )
        return {
            "status": "no_valid_evidence",
            "advice": advice,
            "evidence": [], "accepted_evidence": [], "rejected_evidence": [],
            "reference_evidence": [], "sources": [], "raw_results": [], "filtered_results": [],
            "raw_model_output": "", "raw_model_payload": {},
            "diagnostics": {
                "status": "no_valid_evidence",
                "research_mode": "dashscope_single_search",
                "provider_used": "dashscope_builtin_search",
                "model": "deepseek-v4-flash",
                "search_strategy": "turbo",
                "search_call_count": 1,
                "external_search_call_count": 0,
                "model_call_count": 1,
                "retry_count": 0,
                "gap_search_count": 0,
                "max_search_calls": 1,
                "raw_source_count": 0,
                "unique_source_count": 0,
                "model_evidence_count": 0,
                "valid_evidence_count": 0,
                "invalid_evidence_count": 0,
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "source_url_validation": "canonical URL match",
                "date_validation": "ISO date <= 30 days",
            },
        }

    output = tmp_path / "portfolio.html"
    run_dir = tmp_path / "run"
    advice = run_pipeline(
        payload,
        run_dir=run_dir,
        output=output,
        portfolio_name="Portfolio V2",
        portfolio_id="pf-v2",
        owner_scope="test",
        model="deepseek-v4-flash",
        provider="dashscope",
        close=close,
        market_rows=market_rows,
        fx_rates={},
        single_search_runner=injected_single_search,
        verbose=False,
    )

    assert calls == 1
    assert output.is_file()
    assert advice["report_mode"] == "quantitative_fallback"
    diagnostics = json.loads((run_dir / "portfolio_research_diagnostics.json").read_text(encoding="utf-8"))
    assert (run_dir / "portfolio_reference_evidence.json").is_file()
    assert diagnostics["search_call_count"] == 1
    assert diagnostics["external_search_call_count"] == 0
    assert diagnostics["retry_count"] == 0
    assert diagnostics["gap_search_count"] == 0


def test_local_article_metadata_fetch_promotes_undated_bound_source_to_accepted():
    snapshot, metrics, ranking, metadata = _inputs()
    payload = {
        "evidence": [{
            "ticker": "SOFI",
            "source_url_hint": "https://news.example.com/sofi-update",
            "source_title_hint": "SoFi launches a new banking product",
            "summary": "SoFi 发布新的银行产品，公司服务范围和客户触达能力出现可验证更新。",
            "materiality": "medium",
            "impact": "positive",
            "confidence": 0.72,
        }],
        "no_news_tickers": ["TSLA"],
    }
    fetch_calls = []

    def fake_fetch(url, **kwargs):
        fetch_calls.append((url, kwargs))
        return {
            "url": url,
            "final_url": url,
            "ok": True,
            "published_date": "2026-07-18T09:00:00Z",
            "title": "SoFi launches a new banking product",
            "meta_description": "SoFi announced a new banking product for its members.",
            "text": "2026-07-18 SoFi announced a new banking product for its members.",
            "article_text_quality_ok": True,
        }

    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **_: _response(
            content=json.dumps(payload, ensure_ascii=False),
            sources=[{
                "index": 5,
                "title": "SoFi launches a new banking product",
                "url": "https://news.example.com/sofi-update",
                "snippet": "SoFi announced a new banking product.",
            }],
        ),
        article_fetch_call=fake_fetch,
    )
    assert len(fetch_calls) == 1
    assert len(result["accepted_evidence"]) == 1
    assert result["accepted_evidence"][0]["published_date"] == "2026-07-18"
    assert result["accepted_evidence"][0]["source_binding_method"] == "source_url_hint"
    assert result["diagnostics"]["article_metadata_fetch_count"] == 1
    assert result["diagnostics"]["article_metadata_date_enriched_count"] == 1
    source = result["diagnostics"]["dashscope_sources"][0]
    assert source["date_provenance"] == "article_meta"
    assert source["article_fetch_ok"] is True


def test_local_article_metadata_fetch_excludes_stale_undated_background_source():
    snapshot, metrics, ranking, metadata = _inputs()
    payload = {
        "evidence": [{
            "ticker": "SOFI",
            "source_url_hint": "https://www.sohu.com/a/stale-sofi",
            "source_title_hint": "SoFi company analysis",
            "summary": "这是一条长度足够的 SoFi 公司文章摘要，但页面实际发布日期已经过期。",
        }],
    }

    result = run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **_: _response(
            content=json.dumps(payload, ensure_ascii=False),
            sources=[{
                "index": 6,
                "title": "SoFi company analysis",
                "url": "https://www.sohu.com/a/stale-sofi",
                "snippet": "SoFi company analysis and historical results.",
            }],
        ),
        article_fetch_call=lambda url, **kwargs: {
            "url": url,
            "final_url": url,
            "ok": True,
            "published_date": "2025-10-30T13:33:00+08:00",
            "title": "SoFi company analysis",
            "meta_description": "Old SoFi company analysis.",
            "text": "2025-10-30 Old SoFi company analysis.",
            "article_text_quality_ok": True,
        },
    )
    assert result["accepted_evidence"] == []
    assert result["reference_evidence"] == []
    assert result["diagnostics"]["invalid_evidence_reasons"]["outside_freshness_window"] == 1
    assert result["diagnostics"]["dashscope_sources"][0]["published_date"] == "2025-10-30"


def test_qwen_search_path_enables_provider_freshness_and_citations():
    snapshot, metrics, ranking, metadata = _inputs()
    calls = []
    run_portfolio_single_search(
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=metadata,
        model="qwen-flash",
        generation_call=lambda **kwargs: (
            calls.append(kwargs)
            or _response(content=json.dumps({"evidence": []}), sources=[])
        ),
    )
    options = calls[0]["search_options"]
    assert options["freshness"] == 30
    assert options["enable_citation"] is True
    assert options["citation_format"] == "[ref_<number>]"
    assert calls[0]["messages"][0]["content"].find("source_ref") >= 0


def test_latest_report_regression_chinese_tesla_articles_create_evidence_without_future_or_landing_noise():
    snapshot, metrics, ranking, metadata = _inputs()
    snapshot["report_date"] = "2026-07-21"
    snapshot["as_of"] = "2026-07-21T16:32:00+02:00"
    snapshot["run_timeline"]["snapshot_completed_at"] = "2026-07-21T16:32:00+02:00"
    ranking["top_risk_tickers"] = ["TSLA", "SOFI"]
    ranking["items"] = [
        {"ticker": "TSLA", "risk_score": 0.9, "risk_priority_score": 0.9},
        {"ticker": "SOFI", "risk_score": 0.8, "risk_priority_score": 0.8},
    ]
    metadata["TSLA"].update({
        "name": "Tesla, Inc.",
        "entity_aliases": ["Tesla", "Tesla Motors"],
        "localized_aliases": ["特斯拉"],
    })
    fetched_urls: list[str] = []

    sources = [
        {
            "index": 1,
            "title": "特斯拉(TSLA)-公司公告",
            "url": "http://basic.10jqka.com.cn/mobile/TSLA/pub.html",
        },
        {"index": 2, "title": "Press Releases", "url": "https://ir.tesla.com/press"},
        {
            "index": 3,
            "title": "特斯拉 2026 财年第二财季",
            "url": "https://stock.10jqka.com.cn/20260702/c677911811.shtml",
            "snippet": "特斯拉公布第二财季相关经营数据。",
        },
        {
            "index": 4,
            "title": "特斯拉 2026 年二季度交付超 48 万辆汽车,同比增长 25%",
            "url": "https://www.ithome.com/0/971/907.htm",
            "snippet": "特斯拉公布二季度汽车交付与产量数据。",
        },
        {"index": 5, "title": "SEC Filings", "url": "https://ir.tesla.com/sec-filings"},
        {
            "index": 6,
            "title": "特斯拉7月16日成交额为114.28亿美元 在当日美股中排第9名",
            "url": "https://yuanchuang.10jqka.com.cn/20260717/c678236343.shtml",
        },
        {
            "index": 7,
            "title": "特斯拉7月17日成交额为119.77亿美元 在当日美股中排第8名",
            "url": "http://news.10jqka.com.cn/20260718/c678265247.shtml",
        },
        {
            "index": 8,
            "title": "Tesla First Quarter 2026 Production, Deliveries & Deployments",
            "url": "https://ir.tesla.com/press-release/tesla-first-quarter-2026-production-deliveries-and-deployments",
        },
        {"index": 9, "title": "Press Releases", "url": "https://ir.tesla.com/press?page=1"},
    ]

    def fetch(url: str, **_):
        fetched_urls.append(url)
        if "ithome.com" in url:
            return {
                "url": url,
                "final_url": url,
                "ok": True,
                "title": "特斯拉 2026 年二季度交付超 48 万辆汽车",
                "published_date": "2026-07-02",
                "meta_description": "特斯拉公布二季度交付和产量数据。",
                "text": "发布时间：2026-07-02 特斯拉公布二季度交付数据。",
                "article_text_quality_ok": True,
            }
        if "press-release" in url:
            return {
                "url": url,
                "final_url": url,
                "ok": True,
                "title": "Tesla First Quarter 2026 Production, Deliveries & Deployments",
                "published_date": "2026-04-02",
                "meta_description": "Tesla first-quarter production and deliveries.",
                "text": "Published April 2, 2026.",
                "article_text_quality_ok": True,
            }
        if "20260702" in url:
            return {
                "url": url,
                "final_url": url,
                "ok": True,
                "title": "特斯拉 2026 财年第二财季",
                "published_date": "2026-07-02",
                "meta_description": "特斯拉公布第二财季经营数据。",
                "text": "发布时间：2026-07-02 特斯拉第二财季经营数据。",
                "article_text_quality_ok": True,
            }
        # This would reproduce the old false-future-date bug if an index page
        # were incorrectly fetched and its lead were scanned.
        return {
            "url": url,
            "final_url": url,
            "ok": True,
            "title": "公司公告",
            "published_date": "",
            "meta_description": "",
            "text": "最新公告计划日期 2026-07-22",
            "article_text_quality_ok": False,
        }

    payload = {
        "evidence": [{
            "ticker": "TSLA",
            "source_url_hint": "https://invented.example/tesla-q2",
            "source_title_hint": "Tesla Q2 deliveries update",
            "summary": "特斯拉公布了二季度交付和产量数据，可用于持续观察经营趋势。",
            "materiality": "medium",
            "impact": "neutral",
            "confidence": 0.7,
        }],
        "no_news_tickers": [],
    }
    calls: list[dict] = []
    result = run_portfolio_single_search(
        snapshot=snapshot,
        metrics=metrics,
        ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **kwargs: (
            calls.append(kwargs)
            or _response(content=json.dumps(payload, ensure_ascii=False), sources=sources)
        ),
        article_fetch_call=fetch,
    )

    assert len(calls) == 1
    assert result["diagnostics"]["search_target_tickers"] == ["TSLA"]
    assert "SOFI" not in calls[0]["messages"][1]["content"]
    by_index = {x["source_index"]: x for x in result["diagnostics"]["dashscope_sources"]}
    assert by_index[1]["page_type"] == "announcement_index"
    assert by_index[1]["citable_as_reference"] is False
    assert by_index[2]["page_type"] == "official_landing"
    assert by_index[5]["page_type"] == "official_landing"
    assert by_index[3]["matched_tickers"] == ["TSLA"]
    assert by_index[4]["matched_tickers"] == ["TSLA"]
    assert by_index[6]["matched_tickers"] == ["TSLA"]
    assert by_index[6]["page_type"] == "market_activity"
    assert "http://basic.10jqka.com.cn/mobile/TSLA/pub.html" not in fetched_urls
    assert result["accepted_evidence"]
    assert result["accepted_evidence"][0]["ticker"] == "TSLA"
    assert result["accepted_evidence"][0]["published_date"] <= "2026-07-21"
    assert result["diagnostics"]["latest_selected_event_date"] <= "2026-07-21"
    assert all(x["title"] not in {"Press Releases", "SEC Filings"} for x in result["reference_evidence"])
    assert result["diagnostics"]["landing_or_index_source_count"] >= 3


def test_bad_url_hint_falls_through_to_exact_title_binding():
    snapshot, metrics, ranking, metadata = _inputs()
    payload = {
        "evidence": [{
            "ticker": "SOFI",
            "source_url_hint": "https://redirect.example/not-in-provider-list",
            "source_title_hint": "SoFi announces second-quarter results",
            "summary": "SoFi 发布季度经营结果，可用于观察增长、利润率与信用风险变化。",
            "materiality": "high",
            "impact": "mixed",
            "confidence": 0.8,
        }],
        "no_news_tickers": [],
    }
    result = run_portfolio_single_search(
        snapshot=snapshot,
        metrics=metrics,
        ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **_: _response(
            content=json.dumps(payload, ensure_ascii=False),
            sources=[{
                "index": 4,
                "title": "SoFi announces second-quarter results",
                "url": "https://investors.sofi.com/news/2026/07/18/results",
                "published_date": "2026-07-18",
            }],
        ),
    )
    assert len(result["accepted_evidence"]) == 1
    assert result["accepted_evidence"][0]["source_binding_method"] == "source_title_hint"
    assert result["diagnostics"]["invalid_evidence_count"] == 0


def test_future_date_in_article_lead_is_not_used_as_publication_date():
    snapshot, metrics, ranking, metadata = _inputs()
    snapshot["report_date"] = "2026-07-21"
    snapshot["run_timeline"]["snapshot_completed_at"] = "2026-07-21T16:32:00+02:00"
    result = run_portfolio_single_search(
        snapshot=snapshot,
        metrics=metrics,
        ranking=ranking,
        instrument_metadata=metadata,
        generation_call=lambda **_: _response(
            content=json.dumps({"evidence": [], "no_news_tickers": ["SOFI"]}),
            sources=[{
                "index": 1,
                "title": "SoFi company operating update",
                "url": "https://investors.sofi.com/news/operating-update",
            }],
        ),
        article_fetch_call=lambda url, **_: {
            "url": url,
            "final_url": url,
            "ok": True,
            "title": "SoFi company operating update",
            "published_date": "",
            "meta_description": "SoFi company operating update.",
            "text": "The next scheduled update will take place on 2026-07-22.",
            "article_text_quality_ok": True,
        },
    )
    source = result["diagnostics"]["dashscope_sources"][0]
    assert source["published_date"] in {None, ""}
    assert result["diagnostics"]["latest_selected_event_date"] is None
    assert result["accepted_evidence"] == []
