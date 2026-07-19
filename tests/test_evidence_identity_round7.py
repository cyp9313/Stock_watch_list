# -*- coding: utf-8 -*-
"""第七轮 P0 修改确定性单测（不依赖 LLM/网络）。

覆盖：
- evidence_id.py：make_evidence_uid / finalize_evidence_ids / validate_evidence_identity /
  split_evidence_groups / is_accepted_evidence
- research_plan_validator.py：结构化错误 + 自动清洗（lookback/site/
  dedup/reason/language/trimming）
- evidence_summarizer.py：_apply_summaries_by_uid / validate_summary_identity
- report_quality.py：quality gate（accepted 证据、rejected 隔离、
  observation_only、identity_integrity）
"""
from __future__ import annotations

import pytest

# ── evidence_id.py ──────────────────────────────────────────

def test_make_evidence_uid_stable():
    """同一输入应产生同一 evidence_uid。"""
    from daily_report.src.stock_daily_agent.research_core.evidence_id import make_evidence_uid

    note = {
        "url": "https://example.com/news/1",
        "ticker": "AAPL",
        "published_date": "2026-07-15",
        "title": "Apple Reports Record Earnings",
    }
    uid1 = make_evidence_uid(note)
    uid2 = make_evidence_uid(note)
    assert uid1 == uid2
    assert uid1.startswith("ev_")
    assert len(uid1) == 23  # "ev_" + 20 hex chars


def test_make_evidence_uid_unique():
    """不同输入应产生不同 evidence_uid。"""
    from daily_report.src.stock_daily_agent.research_core.evidence_id import make_evidence_uid

    note1 = {
        "url": "https://example.com/news/1",
        "ticker": "AAPL",
        "published_date": "2026-07-15",
        "title": "Apple Reports Record Earnings",
    }
    note2 = {
        "url": "https://example.com/news/2",
        "ticker": "AAPL",
        "published_date": "2026-07-15",
        "title": "Apple Reports Record Earnings",
    }
    note3 = {
        "url": "https://example.com/news/1",
        "ticker": "MSFT",
        "published_date": "2026-07-15",
        "title": "Apple Reports Record Earnings",
    }
    uids = {make_evidence_uid(n) for n in [note1, note2, note3]}
    assert len(uids) == 3


def test_make_evidence_uid_normalization():
    """uid 应该对大小写和空白不敏感（normalization）。"""
    from daily_report.src.stock_daily_agent.research_core.evidence_id import make_evidence_uid

    note_lower = {
        "url": "https://Example.com/News/1",
        "ticker": "aapl",
        "published_date": "2026-07-15",
        "title": "  Apple Reports Record Earnings  ",
    }
    note_upper = {
        "url": "https://EXAMPLE.COM/NEWS/1",
        "ticker": "AAPL",
        "published_date": "2026-07-15",
        "title": "Apple Reports Record Earnings",
    }
    assert make_evidence_uid(note_lower) == make_evidence_uid(note_upper)


def test_is_accepted_evidence():
    """is_accepted_evidence 的各项条件标志。"""
    from daily_report.src.stock_daily_agent.research_core.evidence_id import is_accepted_evidence

    # 全满足 → accepted
    good = {
        "materiality_accepted": True,
        "accept": True,
        "entity_role": "primary",
        "is_quote_page": False,
        "is_reference_page": False,
        "recency_tier": "fresh_event",
        "article_fetch_ok": True,
        "snippet_fallback_ok": False,
    }
    assert is_accepted_evidence(good)

    # 缺 materiality_accepted
    bad1 = dict(good, materiality_accepted=False)
    assert not is_accepted_evidence(bad1)

    # 摘要器拒绝
    bad2 = dict(good, accept=False)
    assert not is_accepted_evidence(bad2)

    # entity_role 不在 {primary, theme_primary}
    bad3 = dict(good, entity_role="secondary")
    assert not is_accepted_evidence(bad3)

    # is_quote_page
    bad4 = dict(good, is_quote_page=True)
    assert not is_accepted_evidence(bad4)

    # is_reference_page
    bad5 = dict(good, is_reference_page=True)
    assert not is_accepted_evidence(bad5)

    # stale
    bad6 = dict(good, recency_tier="stale")
    assert not is_accepted_evidence(bad6)

    # 无 verify 也不够 fresh
    bad7 = dict(good, article_fetch_ok=False, snippet_fallback_ok=False)
    assert not is_accepted_evidence(bad7)

    # snippet_fallback_ok 应足够
    ok_snippet = dict(good, article_fetch_ok=False, snippet_fallback_ok=True)
    assert is_accepted_evidence(ok_snippet)

    # theme_primary 应接受
    ok_theme = dict(good, entity_role="theme_primary")
    assert is_accepted_evidence(ok_theme)


def test_finalize_evidence_ids():
    """finalize 应为 accepted 证据从 E001 起编号（按 priority_score 降序）。"""
    from daily_report.src.stock_daily_agent.research_core.evidence_id import (
        finalize_evidence_ids, is_accepted_evidence,
    )

    evidence = [
        {"evidence_uid": "ev_aaa", "materiality_accepted": True, "accept": True,
         "entity_role": "primary", "is_quote_page": False, "is_reference_page": False,
         "recency_tier": "fresh_event", "article_fetch_ok": True,
         "snippet_fallback_ok": False, "evidence_id": None, "priority_score": 0.3},
        {"evidence_uid": "ev_bbb", "materiality_accepted": True, "accept": True,
         "entity_role": "primary", "is_quote_page": False, "is_reference_page": False,
         "recency_tier": "fresh_event", "article_fetch_ok": True,
         "snippet_fallback_ok": False, "evidence_id": None, "priority_score": 0.9},
        {"evidence_uid": "ev_ccc", "materiality_accepted": False,
         "evidence_id": None, "priority_score": 0.5},
    ]

    finalize_evidence_ids(evidence)

    # 高 priority 先编号
    assert evidence[1]["evidence_id"] == "E001"  # score 0.9
    assert evidence[0]["evidence_id"] == "E002"  # score 0.3
    # rejected 无编号
    assert evidence[2]["evidence_id"] is None


def test_validate_evidence_identity():
    """应检测重复 evidence_uid 和 evidence_id。"""
    from daily_report.src.stock_daily_agent.research_core.evidence_id import validate_evidence_identity

    # 正常
    evidence = [
        {"evidence_uid": "ev_1", "evidence_id": "E001"},
        {"evidence_uid": "ev_2", "evidence_id": "E002"},
    ]
    assert validate_evidence_identity(evidence) == []

    # 重复 evidence_uid
    dup_uid = [
        {"evidence_uid": "ev_1", "evidence_id": "E001"},
        {"evidence_uid": "ev_1", "evidence_id": "E002"},
    ]
    errors = validate_evidence_identity(dup_uid)
    assert len(errors) >= 1
    assert any("duplicate_evidence_uid" in e for e in errors)

    # 重复 evidence_id
    dup_id = [
        {"evidence_uid": "ev_1", "evidence_id": "E001"},
        {"evidence_uid": "ev_2", "evidence_id": "E001"},
    ]
    errors = validate_evidence_identity(dup_id)
    assert len(errors) >= 1
    assert any("duplicate_evidence_id" in e for e in errors)


def test_split_evidence_groups():
    """应按条件分为 accepted / rejected / reference 三组。"""
    from daily_report.src.stock_daily_agent.research_core.evidence_id import split_evidence_groups

    good = {
        "evidence_uid": "ev_1", "materiality_accepted": True, "accept": True,
        "entity_role": "primary", "is_quote_page": False, "is_reference_page": False,
        "recency_tier": "fresh_event", "article_fetch_ok": True,
        "snippet_fallback_ok": False,
    }
    rejected_item = {
        "evidence_uid": "ev_2", "materiality_accepted": False,
        "is_reference_page": False, "page_classification": "news",
    }
    reference_item = {
        "evidence_uid": "ev_3", "materiality_accepted": False,
        "is_reference_page": True, "page_classification": "reference",
    }

    groups = split_evidence_groups([good, rejected_item, reference_item])
    assert len(groups["accepted"]) == 1
    assert groups["accepted"][0]["evidence_uid"] == "ev_1"
    assert len(groups["diagnostic_rejected"]) == 1
    assert groups["diagnostic_rejected"][0]["evidence_uid"] == "ev_2"
    assert len(groups["reference"]) == 1
    assert groups["reference"][0]["evidence_uid"] == "ev_3"


# ── research_plan_validator.py ──────────────────────────────

def test_validator_lookback_auto_mapping():
    """非法 lookback_days → 映射到最近档位（计划 §8.4）。"""
    from daily_report.src.stock_daily_agent.research_plan_validator import validate_research_plan

    plan = {
        "plan_version": "1.0",
        "tickers": [{
            "ticker": "AAPL",
            "research_priority": "high",
            "primary_language": "en",
            "research_questions": [{
                "question_id": "AAPL_Q1",
                "event_need": "earnings_results",
                "reason_zh": "财报季需要关注最新盈利公告对估值的影响。",
                "lane": "news",
                "lookback_days": 60,  # 不在允许档位 → 应映射到 45
                "queries": ["AAPL earnings results Q2 2026"],
                "preferred_domains": [],
                "required_entities": ["AAPL"],
                "exclude_terms": [],
                "priority": 1,
            }],
        }],
    }

    validated, errors = validate_research_plan(plan, top_risk_tickers=["AAPL"])
    assert not errors  # 无致命错误
    q = validated["tickers"][0]["research_questions"][0]
    assert q["lookback_days"] == 45  # 60→45


def test_validator_site_prefix_extraction():
    """query 中 site:domain → 移入 preferred_domains（计划 §8.4）。"""
    from daily_report.src.stock_daily_agent.research_plan_validator import validate_research_plan

    plan = {
        "plan_version": "1.0",
        "tickers": [{
            "ticker": "ORCL",
            "research_priority": "high",
            "primary_language": "en",
            "research_questions": [{
                "question_id": "ORCL_Q1",
                "event_need": "earnings_results",
                "reason_zh": "Oracle 财报对科技板块有重要信号意义。",
                "lane": "news",
                "lookback_days": 30,
                "queries": ["site:reuters.com Oracle cloud revenue growth 2026"],
                "preferred_domains": [],
                "required_entities": ["ORCL"],
                "exclude_terms": [],
                "priority": 1,
            }],
        }],
    }

    validated, errors = validate_research_plan(plan, top_risk_tickers=["ORCL"])
    assert not errors
    q = validated["tickers"][0]["research_questions"][0]
    assert "reuters.com" in q["preferred_domains"]
    # query 中不应再有 site: 前缀
    for query in q["queries"]:
        assert "site:" not in query


def test_validator_language_auto_fix():
    """语言不一致 → 自动修正为 Language Router 决策（计划 §8.4）。"""
    from daily_report.src.stock_daily_agent.research_plan_validator import validate_research_plan

    plan = {
        "plan_version": "1.0",
        "tickers": [{
            "ticker": "AAPL",  # 美股 → en
            "research_priority": "high",
            "primary_language": "zh-CN",  # 错误
            "research_questions": [{
                "question_id": "AAPL_Q1",
                "event_need": "earnings_results",
                "reason_zh": "财报需要关注最新数据。",
                "lane": "news",
                "lookback_days": 30,
                "queries": ["AAPL earnings results"],
                "preferred_domains": [],
                "required_entities": ["AAPL"],
                "exclude_terms": [],
                "priority": 1,
            }],
        }],
    }

    validated, errors = validate_research_plan(plan, top_risk_tickers=["AAPL"])
    assert not errors
    assert validated["tickers"][0]["primary_language"] == "en"


def test_validator_reason_zh_default():
    """reason_zh 过短 → 自动补默认理由（计划 §8.4）。"""
    from daily_report.src.stock_daily_agent.research_plan_validator import validate_research_plan

    plan = {
        "plan_version": "1.0",
        "tickers": [{
            "ticker": "TSLA",
            "research_priority": "high",
            "primary_language": "en",
            "research_questions": [{
                "question_id": "TSLA_Q1",
                "event_need": "product_event",
                "reason_zh": "短",  # < 8 字符
                "lane": "news",
                "lookback_days": 14,
                "queries": ["Tesla new model launch 2026"],
                "preferred_domains": [],
                "required_entities": ["TSLA"],
                "exclude_terms": [],
                "priority": 1,
            }],
        }],
    }

    validated, errors = validate_research_plan(plan, top_risk_tickers=["TSLA"])
    assert not errors
    q = validated["tickers"][0]["research_questions"][0]
    assert len(q["reason_zh"]) >= 8


def test_validator_duplicate_query_dedup():
    """重复 query → 自动去重（计划 §8.4）。"""
    from daily_report.src.stock_daily_agent.research_plan_validator import validate_research_plan

    plan = {
        "plan_version": "1.0",
        "tickers": [{
            "ticker": "MSFT",
            "research_priority": "high",
            "primary_language": "en",
            "research_questions": [{
                "question_id": "MSFT_Q1",
                "event_need": "earnings_results",
                "reason_zh": "微软财报是科技行业的核心风向标。",
                "lane": "news",
                "lookback_days": 30,
                "queries": [
                    "MSFT earnings Q2 2026",
                    "MSFT earnings Q2 2026",  # 重复
                    "Microsoft Azure growth 2026",
                ],
                "preferred_domains": [],
                "required_entities": ["MSFT"],
                "exclude_terms": [],
                "priority": 1,
            }],
        }],
    }

    validated, errors = validate_research_plan(plan, top_risk_tickers=["MSFT"])
    assert not errors
    q = validated["tickers"][0]["research_questions"][0]
    assert len(q["queries"]) == 2  # 去重后剩 2 条


def test_validator_unknown_ticker_fatal():
    """不在 top_risk_tickers 的 ticker → 致命错误。"""
    from daily_report.src.stock_daily_agent.research_plan_validator import validate_research_plan

    plan = {
        "plan_version": "1.0",
        "tickers": [{
            "ticker": "INVENTED_TICKER",
            "research_priority": "high",
            "primary_language": "en",
            "research_questions": [{
                "question_id": "INVENTED_TICKER_Q1",
                "event_need": "news_event",
                "reason_zh": "这是一个不存在的标的的测试问题。",
                "lane": "news",
                "lookback_days": 30,
                "queries": ["some query"],
                "preferred_domains": [],
                "required_entities": ["INVENTED_TICKER"],
                "exclude_terms": [],
                "priority": 1,
            }],
        }],
    }

    validated, errors = validate_research_plan(plan, top_risk_tickers=["AAPL", "MSFT"])
    assert errors  # 应有致命错误
    assert any(e["code"] == "unknown_ticker" for e in errors)
    assert not validated.get("tickers")  # 无合法 ticker


def test_validator_duplicate_ticker_warning():
    """重复 ticker → warning（非致命）。"""
    from daily_report.src.stock_daily_agent.research_plan_validator import validate_research_plan

    plan = {
        "plan_version": "1.0",
        "tickers": [
            {
                "ticker": "AAPL",
                "research_priority": "high",
                "primary_language": "en",
                "research_questions": [{
                    "question_id": "AAPL_Q1",
                    "event_need": "earnings_results",
                    "reason_zh": "苹果财报对组合影响重大。",
                    "lane": "news",
                    "lookback_days": 30,
                    "queries": ["AAPL earnings Q2 2026"],
                    "preferred_domains": [],
                    "required_entities": ["AAPL"],
                    "exclude_terms": [],
                    "priority": 1,
                }],
            },
            {
                "ticker": "AAPL",  # 重复
                "research_priority": "high",
                "primary_language": "en",
                "research_questions": [{
                    "question_id": "AAPL_Q2",
                    "event_need": "product_event",
                    "reason_zh": "苹果新产品发布需关注。",
                    "lane": "news",
                    "lookback_days": 30,
                    "queries": ["Apple new product 2026"],
                    "preferred_domains": [],
                    "required_entities": ["AAPL"],
                    "exclude_terms": [],
                    "priority": 2,
                }],
            },
        ],
    }

    validated, errors = validate_research_plan(plan, top_risk_tickers=["AAPL"])
    assert not errors  # 非致命
    assert len(validated["tickers"]) == 1  # 去重后仅 1 个 AAPL


# ── evidence_summarizer.py ──────────────────────────────────

def test_apply_summaries_by_uid():
    """_apply_summaries_by_uid 应按 evidence_uid 映射，不覆盖无关条目。"""
    from daily_report.src.stock_daily_agent.evidence_summarizer import _apply_summaries_by_uid

    evidence = [
        {"evidence_uid": "ev_aaa", "ticker": "AAPL", "title": "Orig A", "summary_zh": ""},
        {"evidence_uid": "ev_bbb", "ticker": "MSFT", "title": "Orig B", "summary_zh": ""},
    ]
    items = [
        {"evidence_uid": "ev_aaa",
         "accept": True, "ticker": "AAPL",
         "event_title_zh": "Summarized A",
         "what_happened_zh": "Something happened",
         "impact_direction": "positive"},
    ]

    errors = _apply_summaries_by_uid(evidence, items)
    assert not errors
    assert evidence[0]["summary_zh"] == "Summarized A"
    assert evidence[0]["impact_direction"] == "positive"
    assert evidence[0]["accept"] is True
    assert evidence[0]["summary_method"] == "llm_decision_summarizer"
    # ev_bbb 不受影响
    assert evidence[1]["summary_zh"] == ""


def test_apply_summaries_by_uid_duplicate():
    """LLM 返回重复 UID → by_uid dict last-wins（不抛错，行为可预测）。"""
    from daily_report.src.stock_daily_agent.evidence_summarizer import _apply_summaries_by_uid

    evidence = [
        {"evidence_uid": "ev_aaa", "ticker": "AAPL", "title": "Orig A", "summary_zh": ""},
    ]
    items = [
        {"evidence_uid": "ev_aaa",
         "accept": True, "ticker": "AAPL",
         "event_title_zh": "First"},
        {"evidence_uid": "ev_aaa",  # 重复 — last wins
         "accept": False, "ticker": "AAPL",
         "event_title_zh": "Second"},
    ]

    errors = _apply_summaries_by_uid(evidence, items)
    # LLM 返回重复 UID → dict last-wins，不会记录为错误
    assert evidence[0]["summary_zh"] == "Second"  # last wins
    assert evidence[0]["accept"] is False


def test_apply_summaries_by_uid_unknown():
    """LLM 返回未知 UID → 跳过，不崩溃（不记录错误，因循环以 evidence 为轴）。"""
    from daily_report.src.stock_daily_agent.evidence_summarizer import _apply_summaries_by_uid

    evidence = [
        {"evidence_uid": "ev_aaa", "ticker": "AAPL", "title": "Orig A", "summary_zh": ""},
    ]
    items = [
        {"evidence_uid": "ev_unknown",
         "accept": True, "ticker": "AAPL", "entity_role": "primary",
         "is_quote_page": False, "summary_zh": "Unknown summary"},
    ]

    errors = _apply_summaries_by_uid(evidence, items)
    # LLM 返回未知 UID — 因为循环以 evidence 为轴，未匹配项被跳过
    # 不抛错，但证据项也未被更新
    assert evidence[0]["summary_zh"] == ""


def test_validate_summary_identity():
    """应断言 uid 一致、ticker 未篡改（使用 AssertionError）。"""
    from daily_report.src.stock_daily_agent.evidence_summarizer import validate_summary_identity

    # 正常通过
    original = {"evidence_uid": "ev_aaa", "ticker": "AAPL"}
    summarized = {"evidence_uid": "ev_aaa", "ticker": "AAPL", "summary_zh": "Good"}
    validate_summary_identity(original, summarized)  # 不应抛异常

    # uid 不一致 → 应抛 AssertionError
    bad_uid_summarized = {"evidence_uid": "ev_bbb", "ticker": "AAPL"}
    with pytest.raises(AssertionError):
        validate_summary_identity(original, bad_uid_summarized)

    # ticker 被篡改 → 应抛 AssertionError
    bad_ticker_summarized = {"evidence_uid": "ev_aaa", "ticker": "MSFT"}
    with pytest.raises(AssertionError):
        validate_summary_identity(original, bad_ticker_summarized)


# ── report_quality.py ───────────────────────────────────────

def test_quality_gate_identity_integrity():
    """P0-5: 重复 evidence_uid → publishable=False。"""
    from portfolio_analysis.report_quality import evaluate_report_quality

    snapshot = {
        "portfolio_name": "Test", "as_of": "2026-07-18",
        "data_cutoffs": {"prices": "2026-07-18", "news": "2026-07-18"},
        "holdings": [{"ticker": "AAPL", "weight": 0.3, "currency": "USD", "shares": 100}],
    }
    metrics = {"total_value": 100000, "aggregates": {"beta_range": [1.0, 1.0]}}
    research_result = {
        "status": "success",
        "evidence": [
            {"evidence_uid": "ev_aaa", "evidence_id": "E001", "ticker": "AAPL",
             "materiality_accepted": True, "accept": True, "entity_role": "primary",
             "is_quote_page": False, "is_reference_page": False,
             "recency_tier": "fresh_event", "article_fetch_ok": True,
             "snippet_fallback_ok": False, "title": "News 1",
             "url": "https://example.com/1", "published_date": "2026-07-18",
             "priority_score": 0.8, "summary_zh": "测试新闻"},
            {"evidence_uid": "ev_aaa", "evidence_id": "E002", "ticker": "AAPL",
             "materiality_accepted": True, "accept": True, "entity_role": "primary",
             "is_quote_page": False, "is_reference_page": False,
             "recency_tier": "fresh_event", "article_fetch_ok": True,
             "snippet_fallback_ok": False, "title": "News 2",
             "url": "https://example.com/2", "published_date": "2026-07-18",
             "priority_score": 0.7, "summary_zh": "测试新闻2"},
        ],
        "diagnostics": {"status": "success", "top_risk_coverage": 1.0},
        "accepted_evidence": [
            {"evidence_uid": "ev_aaa", "evidence_id": "E001", "ticker": "AAPL"},
            {"evidence_uid": "ev_aaa", "evidence_id": "E002", "ticker": "AAPL"},
        ],
    }
    advice = {
        "actions": [{"ticker": "AAPL", "action": "hold", "confidence": 0.8, "reason": "正常"}],
        "confidence": 0.8, "final_confidence": 0.8,
    }

    quality = evaluate_report_quality(snapshot, metrics, research_result, advice, {})
    assert not quality["publishable"]  # 重复 uid


def test_quality_gate_observation_only():
    """P0-5: 所有 watch + 少证据 → observation_only=True。"""
    from portfolio_analysis.report_quality import evaluate_report_quality

    snapshot = {
        "portfolio_name": "Test", "as_of": "2026-07-18",
        "data_cutoffs": {"prices": "2026-07-18", "news": "2026-07-18"},
        "holdings": [{"ticker": "AAPL", "weight": 0.5, "currency": "USD", "shares": 100}],
    }
    metrics = {"total_value": 100000, "aggregates": {"beta_range": [1.0, 1.0]}}
    ev = {
        "evidence_uid": "ev_aaa", "evidence_id": "E001", "ticker": "AAPL",
        "materiality_accepted": True, "accept": True, "entity_role": "primary",
        "is_quote_page": False, "is_reference_page": False,
        "recency_tier": "fresh_event", "article_fetch_ok": True,
        "snippet_fallback_ok": False, "title": "News",
        "url": "https://example.com/1", "published_date": "2026-07-18",
        "priority_score": 0.8, "summary_zh": "测试新闻",
    }
    research_result = {
        "status": "success",
        "evidence": [ev],
        "diagnostics": {"status": "success", "top_risk_coverage": 1.0},
        "accepted_evidence": [ev],
    }
    advice = {
        "actions": [{"ticker": "AAPL", "action": "watch", "confidence": 0.5, "reason": "观望"}],
        "confidence": 0.5, "final_confidence": 0.5,
    }

    quality = evaluate_report_quality(snapshot, metrics, research_result, advice, {})
    # 全 watch + 少证据 → observation_only
    assert quality["observation_only"] is True or quality["directional_action_supported"] is False


def test_quality_gate_rejected_not_in_accepted():
    """P0-4: rejected 证据不应出现在 accepted_evidence 中。"""
    from daily_report.src.stock_daily_agent.research_core.evidence_id import (
        split_evidence_groups, finalize_evidence_ids,
    )

    evidence = [
        {"evidence_uid": "ev_1", "materiality_accepted": True, "accept": True,
         "entity_role": "primary", "is_quote_page": False, "is_reference_page": False,
         "recency_tier": "fresh_event", "article_fetch_ok": True,
         "snippet_fallback_ok": False, "evidence_id": None, "priority_score": 0.5},
        {"evidence_uid": "ev_2", "materiality_accepted": False,
         "evidence_id": None, "priority_score": 0.3},
    ]

    finalize_evidence_ids(evidence)
    groups = split_evidence_groups(evidence)

    assert len(groups["accepted"]) == 1
    assert len(groups["diagnostic_rejected"]) == 1
    for item in groups["diagnostic_rejected"]:
        assert item["evidence_id"] is None
    for item in groups["accepted"]:
        assert item["evidence_id"] is not None


# ── 集成回归：现有测试可正常运行 ──────────────────────────

def test_portfolio_round4_imports():
    """确保第七轮改动未破坏现有导入链。"""
    try:
        from daily_report.src.stock_daily_agent.portfolio_research import PortfolioResearchService
        from daily_report.src.stock_daily_agent.research_service import ResearchService
        from daily_report.src.stock_daily_agent.evidence_summarizer import summarize_evidence_zh
        from daily_report.src.stock_daily_agent.research_core.evidence_id import (
            make_evidence_uid, finalize_evidence_ids, validate_evidence_identity,
            split_evidence_groups, is_accepted_evidence,
        )
        from daily_report.src.stock_daily_agent.research_plan_validator import (
            validate_research_plan, ResearchPlanValidationError,
        )
        from daily_report.src.stock_daily_agent.research_query_planner import (
            build_ai_research_plan, _planner_repair_enabled,
        )
        from portfolio_analysis.report_quality import evaluate_report_quality, PortfolioReportQualityError
    except ImportError as exc:
        pytest.fail(f"Import 失败: {exc}")
