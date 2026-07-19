# -*- coding: utf-8 -*-
"""Phase 1 端到端 smoke test：fallback planner + validator + compiler。"""
import sys
import json
sys.path.insert(0, '.')

from daily_report.src.stock_daily_agent.research_query_planner import build_ai_research_plan
from daily_report.src.stock_daily_agent.research_query_compiler import (
    compile_research_queries, expand_official_lane_queries,
)
from daily_report.src.stock_daily_agent.research_plan_validator import validate_research_plan

snapshot = {
    "portfolio_name": "Test",
    "base_currency": "EUR",
    "report_date": "2026-07-18",
    "holdings": [
        {"ticker": "ORCL", "name": "Oracle", "weight": 0.05, "beta": 1.84, "rsi": 27.5,
         "rsi_regime": "oversold", "price_vs_ema20_pct": -12.4, "price_vs_ema50_pct": -25.1,
         "price_vs_ema200_pct": -38.6, "return_1m": -18.4, "return_ytd": -40.0},
        {"ticker": "WNUC.DE", "name": "WisdomTree Uranium and Nuclear Energy UCITS ETF",
         "weight": 0.07, "beta": 1.2, "rsi": 40, "rsi_regime": "weak",
         "price_vs_ema200_pct": -20},
        {"ticker": "BTC-EUR", "name": "Bitcoin", "weight": 0.04, "beta": 1.5, "rsi": 35,
         "rsi_regime": "weak", "price_vs_ema200_pct": -25},
    ],
}
metrics = {
    "holdings_detail": {
        "ORCL": {"annualized_volatility": 48.2, "max_drawdown_63d": -49.8, "max_drawdown_252d": -55.0},
        "WNUC.DE": {"annualized_volatility": 30.0, "max_drawdown_63d": -21.0, "max_drawdown_252d": -35.0},
        "BTC-EUR": {"annualized_volatility": 60.0, "max_drawdown_63d": -28.0, "max_drawdown_252d": -50.0},
    },
    "risk_contributions": [
        {"ticker": "ORCL", "risk_contribution": 0.10},
        {"ticker": "WNUC.DE", "risk_contribution": 0.08},
        {"ticker": "BTC-EUR", "risk_contribution": 0.06},
    ],
}
ranking = {
    "top_risk_tickers": ["ORCL", "WNUC.DE", "BTC-EUR"],
    "items": [
        {"ticker": "ORCL", "weight": 0.05, "risk_priority_rank": 1,
         "risk_contribution_rank": 1, "risk_priority_score": 0.9},
        {"ticker": "WNUC.DE", "weight": 0.07, "risk_priority_rank": 2,
         "risk_contribution_rank": 2, "risk_priority_score": 0.8},
        {"ticker": "BTC-EUR", "weight": 0.04, "risk_priority_rank": 3,
         "risk_contribution_rank": 3, "risk_priority_score": 0.7},
    ],
}
instrument_metadata = {
    "ORCL": {"name": "Oracle", "instrument_type": "EQUITY", "theme": "Cloud / AI Infrastructure",
             "exchange": "NYSE", "official_domains": ["investor.oracle.com"],
             "ir_domain": "investor.oracle.com"},
    "WNUC.DE": {"name": "WisdomTree Uranium and Nuclear Energy UCITS ETF",
                "instrument_type": "ETF", "theme": "Uranium & Nuclear",
                "underlying_index": "WisdomTree Uranium and Nuclear Energy Index",
                "key_drivers": ["uranium spot price", "uranium mine supply",
                                "nuclear policy", "reactor approvals"]},
    "BTC-EUR": {"name": "Bitcoin", "instrument_type": "CRYPTO", "theme": "Crypto"},
}

plan, diag = build_ai_research_plan(
    top_risk_tickers=["ORCL", "WNUC.DE", "BTC-EUR"],
    snapshot=snapshot, metrics=metrics, ranking=ranking,
    instrument_metadata=instrument_metadata,
    model="qwen-plus", provider="dashscope",
)

print("=== Planner diagnostics ===")
print(json.dumps(diag, ensure_ascii=False, indent=2))
print()
print("=== Plan tickers ===")
for t in plan.get("tickers", []):
    print(f"  {t['ticker']} (lang={t['primary_language']}, "
          f"priority={t['research_priority']}, "
          f"questions={len(t['research_questions'])})")
    for q in t["research_questions"]:
        print(f"    - {q['question_id']}: event={q['event_need']}, "
              f"lane={q['lane']}, lookback={q['lookback_days']}d, "
              f"queries={len(q['queries'])}")
        for qq in q["queries"]:
            print(f"      query: {qq}")
print()

compiled = compile_research_queries(plan, instrument_metadata=instrument_metadata)
official = expand_official_lane_queries(compiled, instrument_metadata=instrument_metadata)
print(f"=== Compiled: {len(compiled)} main + {len(official)} official site: queries ===")
for c in compiled:
    print(f"  [{c['lane']}] {c['ticker'] or 'MACRO'} "
          f"({c['language']}, {c['lookback_days']}d): {c['query']}")
print("  --- official expansions ---")
for c in official:
    print(f"  [official] {c['ticker']} ({c['language']}): {c['query']}")
print()

validated, errors = validate_research_plan(
    plan, top_risk_tickers=["ORCL", "WNUC.DE", "BTC-EUR"],
    instrument_metadata=instrument_metadata,
)
print(f"=== Validator: errors={len(errors)} ===")
for e in errors:
    print(f"  - {e}")

# 关键断言（修改计划 35.1-35.6）
assert diag["planner_mode"] == "fallback", "planner_mode should be fallback"
assert plan["plan_version"] == "1.0", "plan_version should be 1.0"
# 35.3: 所有 lookback_days 必须在允许档位
for t in plan["tickers"]:
    for q in t["research_questions"]:
        assert q["lookback_days"] in {7, 14, 30, 45, 120, 365}, \
            f"bad lookback: {q['lookback_days']}"
# 35.4: 每 ticker 问题数 <= 4
for t in plan["tickers"]:
    assert len(t["research_questions"]) <= 4
    for q in t["research_questions"]:
        assert len(q["queries"]) <= 3
# 35.5/35.6: ORCL/WNUC.DE/COIN 英文；A 股中文
assert plan["tickers"][0]["primary_language"] == "en"  # ORCL
assert plan["tickers"][1]["primary_language"] == "en"  # WNUC.DE
assert plan["tickers"][2]["primary_language"] == "en"  # BTC-EUR
# 总 query 预算
assert plan["total_queries"] <= 24, f"total_queries {plan['total_queries']} > 24"
print()
print("ALL ASSERTIONS PASSED.")
