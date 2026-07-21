from pathlib import Path


def test_retired_portfolio_research_modules_are_absent():
    root = Path(__file__).resolve().parents[1]
    retired = [
        "daily_report/src/stock_daily_agent/portfolio_single_search.py",
        "daily_report/src/stock_daily_agent/portfolio_agent_runner.py",
        "daily_report/src/stock_daily_agent/portfolio_research.py",
        "daily_report/src/stock_daily_agent/portfolio_schema.py",
        "daily_report/src/stock_daily_agent/research_query_planner.py",
        "daily_report/src/stock_daily_agent/research_gap_analyzer.py",
        "portfolio_analysis/report_quality.py",
        "portfolio_analysis/observation_view.py",
    ]
    assert all(not (root / relative).exists() for relative in retired)


def test_current_portfolio_entrypoints_exist():
    root = Path(__file__).resolve().parents[1]
    required = [
        "daily_report/src/stock_daily_agent/portfolio_ai_analyst.py",
        "daily_report/src/stock_daily_agent/portfolio_fallback.py",
        "daily_report/run_portfolio_report.py",
    ]
    assert all((root / relative).is_file() for relative in required)


def test_quantitative_fallback_is_observation_only():
    from daily_report.src.stock_daily_agent.portfolio_fallback import build_quantitative_fallback

    advice = build_quantitative_fallback(
        {"holdings": [{"ticker": "SOFI", "weight": 0.1}]},
        {"portfolio_risk_score": 45},
        {"top_risk_tickers": ["SOFI"]},
        reason="test failure",
    )
    assert advice["report_mode"] == "quantitative_fallback"
    assert advice["observation_only"] is True
    assert advice["actions"][0]["action"] == "watch"
    assert advice["actions"][0]["target_weight_min"] == 0.1
