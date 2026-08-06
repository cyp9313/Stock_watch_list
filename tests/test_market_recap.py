from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pandas as pd

from daily_report import market_recap_service as recap


def test_market_selection_and_subject_are_stable():
    assert recap.normalize_markets("cn+us") == ["cn", "us"]
    assert recap.normalize_markets(["US", "美股", "invalid"]) == ["us"]
    assert recap.market_subject_key(["cn", "us"]) == "market:cn+us"
    assert recap.market_subject_name(["us"]) == "美股大盘复盘"


def test_tnx_is_normalized_as_yield_not_price():
    data = pd.DataFrame({"^TNX": [40.0, 41.0, 42.0, 43.0, 44.0, 45.0]})
    row = recap._instrument_rows(data, {"^TNX": "十年期美债收益率"}, yield_ticker="^TNX")[0]
    assert row["value"] == 4.5
    assert row["unit"] == "%"
    assert row["1d_pct"] > 0


def test_tnx_keeps_already_percent_yahoo_representation():
    data = pd.DataFrame({"^TNX": [4.0, 4.2, 4.5]})
    row = recap._instrument_rows(data, {"^TNX": "十年期美债收益率"}, yield_ticker="^TNX")[0]
    assert row["value"] == 4.5


def test_a_share_breadth_uses_exchange_specific_limit_rules():
    quotes = pd.DataFrame({
        "代码": ["600001", "300001", "600002", "600003", "600004"],
        "名称": ["普通股", "创业板", "*ST 样本", "普通上涨", "普通下跌"],
        "最新价": [11.0, 12.0, 10.5, 10.5, 9.0],
        "昨收": [10.0, 10.0, 10.0, 10.0, 10.0],
        "成交额": [1, 2, 3, 4, 5],
    })
    breadth = recap._a_share_breadth_from_spot(quotes)
    assert breadth == {
        "advance": 4,
        "decline": 1,
        "flat": 0,
        "limit_up": 3,
        "limit_down": 1,
        "turnover": 15.0,
        "coverage": 5,
    }


def test_cn_snapshot_uses_akshare_sina_fallback_for_spot_and_industry(monkeypatch):
    spot = pd.DataFrame({
        "代码": ["600001", "600002"], "名称": ["样本A", "样本B"],
        "最新价": [11.0, 9.0], "昨收": [10.0, 10.0], "成交额": [10, 20],
    })
    industry = pd.DataFrame({"板块": ["行业A", "行业B"], "涨跌幅": [2.0, -1.0]})

    def fail():
        raise ConnectionError("Eastmoney unavailable")

    fake_efinance = SimpleNamespace(stock=SimpleNamespace(get_realtime_quotes=fail))
    fake_akshare = SimpleNamespace(
        stock_zh_a_spot_em=fail,
        stock_zh_a_spot=lambda: spot,
        stock_board_industry_name_em=fail,
        stock_sector_spot=lambda indicator: industry if indicator == "行业" else pd.DataFrame(),
    )
    monkeypatch.setitem(sys.modules, "efinance", fake_efinance)
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    monkeypatch.setenv("MARKET_RECAP_A_SHARE_PROVIDER", "auto")
    monkeypatch.setattr(recap, "_load_cached_snapshot", lambda market: None)
    monkeypatch.setattr(recap, "_save_cached_snapshot", lambda snapshot: None)
    monkeypatch.setattr(
        recap,
        "_tencent_cn_indexes",
        lambda: ([{"ticker": "000001", "name": "上证指数", "status": "ok"}], "2026-08-04", []),
    )

    snapshot = recap._cn_snapshot()

    assert snapshot["breadth"]["limit_up"] == 1
    assert snapshot["breadth"]["limit_down"] == 1
    assert snapshot["sectors"]["leaders"][0]["name"] == "行业A"
    assert "Sina Finance 全市场行情（AkShare）" in snapshot["source"]
    assert "Sina Finance 行业板块（AkShare）" in snapshot["source"]
    assert snapshot["errors"][0].startswith("东方财富适配器当前不可用；已使用")


def test_fallback_sections_include_computed_index_and_breadth_facts():
    snapshot = {
        "market": "cn", "market_name": "A股", "deterministic_regime": {"label": "均衡"},
        "indexes": [{"name": "上证指数", "value": 3800, "1d_pct": 1.0, "5d_pct": 2.0, "status": "ok"}],
        "breadth": {"advance": 3000, "decline": 2000, "flat": 100, "limit_up": 60, "limit_down": 5, "coverage": 5100},
        "macro": [], "sectors": {},
    }
    sections = recap._fallback_sections([snapshot], [])
    assert "上证指数" in sections["index_structure"]
    assert "3000/2000/100" in sections["breadth_liquidity"]


def test_market_news_filter_keeps_recent_relevant_items_only(monkeypatch):
    monkeypatch.setenv("MARKET_RECAP_NEWS_MAX_AGE_DAYS", "3")
    valid = recap._score_market_news_item(
        {
            "title": "S&P 500 and Nasdaq rise at US stock market close",
            "snippet": "Treasury yields and the VIX were in focus for Wall Street investors.",
            "link": "https://www.reuters.com/markets/us/stocks-close",
            "date": "4 hours ago",
        },
        market="us",
        as_of_date="2026-08-04",
        query="US stock market close S&P 500 Nasdaq Dow VIX",
    )
    assert valid is not None
    assert valid["published_date"] == "2026-08-04"
    assert valid["market"] == "us"

    assert recap._score_market_news_item(
        {
            "title": "Bitcoin and crypto market outlook",
            "snippet": "Wallet users discuss a memecoin.",
            "link": "https://example.test/crypto",
            "date": "1 hour ago",
        },
        market="us",
        as_of_date="2026-08-04",
        query="US stock market close S&P 500 Nasdaq Dow VIX",
    ) is None
    assert recap._score_market_news_item(
        {
            "title": "US stock market and Nasdaq review",
            "snippet": "Wall Street recap.",
            "link": "https://example.test/old",
            "date": "2026-06-23",
        },
        market="us",
        as_of_date="2026-08-04",
        query="US stock market close S&P 500 Nasdaq Dow VIX",
    ) is None
    assert recap._score_market_news_item(
        {
            "title": "General investing commentary",
            "snippet": "A portfolio thought piece.",
            "link": "https://example.test/generic",
            "date": "1 hour ago",
        },
        market="us",
        as_of_date="2026-08-04",
        query="US stock market close S&P 500 Nasdaq Dow VIX",
    ) is None


def test_market_news_search_queries_each_market_separately(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    monkeypatch.setenv("MARKET_RECAP_NEWS_MAX_ITEMS", "6")
    monkeypatch.setenv("MARKET_RECAP_NEWS_FETCH_MAX_URLS", "0")
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "news": [
                    {
                        "title": "US stock market S&P 500 Nasdaq closes higher",
                        "snippet": "Wall Street and the VIX were in focus.",
                        "link": "https://www.reuters.com/markets/us/close",
                        "date": "2 hours ago",
                    },
                    {
                        "title": "A-share market broadens as Shanghai stocks advance",
                        "snippet": "A-share market sector rotation and policy expectations.",
                        "link": "https://www.eastmoney.com/a-share/market-close",
                        "date": "1 day ago",
                    },
                    {
                        "title": "Crypto wallet market update",
                        "snippet": "Bitcoin moves with a memecoin.",
                        "link": "https://example.test/crypto",
                        "date": "1 hour ago",
                    },
                ]
            }

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"]["q"])
        return Response()

    monkeypatch.setattr(recap.requests, "post", fake_post)
    results = recap._search_market_news(
        ["us", "cn"],
        [{"market": "us", "as_of_date": "2026-08-04"}, {"market": "cn", "as_of_date": "2026-08-04"}],
    )

    assert len(calls) == 6
    assert {item["market"] for item in results} == {"us", "cn"}
    assert all("crypto" not in item["title"].casefold() for item in results)
    assert all(item["published_date"] >= "2026-08-03" for item in results)


def test_html_escapes_untrusted_news_and_keeps_safe_link_shell():
    html = recap.render_market_recap_html({
        "title": "<unsafe>", "generated_at": "now", "sections": {key: "x" for key in (
            "overview", "index_structure", "breadth_liquidity", "macro_context", "rotation", "news_catalysts", "next_session", "risk_notes"
        )},
        "snapshots": [{"market": "us", "market_name": "US", "as_of_date": "2026-08-04", "deterministic_regime": {}, "indexes": [], "macro": [], "breadth": {}, "sectors": {}, "errors": []}],
        "news": [{"market": "us", "title": "<script>alert(1)</script>", "snippet": "<b>x</b>", "source": "test", "url": "https://example.test/a"}],
    })
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "https://example.test/a" in html


def test_html_renders_market_data_and_analysis_in_tables():
    html = recap.render_market_recap_html({
        "title": "结构化复盘", "generated_at": "now",
        "sections": {
            "overview": "盘面偏暖。\n\n模型解读：上涨参与度改善。",
            "index_structure": "模型解读：指数同步上行。",
            "breadth_liquidity": "模型解读：宽度确认趋势。",
            "macro_context": "模型解读：宏观环境中性。",
            "rotation": "模型解读：科技板块领先。",
            "news_catalysts": "模型解读：新闻证据有限。",
            "next_session": "观察指数与成交额。",
            "risk_notes": "警惕数据延迟。",
        },
        "snapshots": [{
            "market": "cn", "market_name": "A股", "as_of_date": "2026-08-04", "source": "test",
            "deterministic_regime": {"label": "偏进攻", "reason": "上涨家数占优"},
            "indexes": [{"name": "上证指数", "value": 3800, "1d_pct": 1.2, "5d_pct": 2.4, "trend": "走强"}],
            "macro": [],
            "breadth": {"advance": 3000, "decline": 2000, "flat": 100, "limit_up": 60, "limit_down": 5, "coverage": 5100},
            "sectors": {"leaders": [{"name": "电子", "change_pct": 2.0, "kind": "行业"}], "laggards": []},
            "errors": [],
        }],
        "news": [],
    })
    assert "A股数据快照" in html
    assert "上涨占比（不含平盘）" in html
    assert "盘面总览" in html
    assert "<th>维度</th><th>解读</th>" in html
    assert "下一交易日框架" in html
    assert 'class="header"' in html
    assert 'class="kpi-card"' in html
    assert 'class="section"' in html
    assert "#0d1117" in html


def test_deterministic_regime_uses_breadth_and_vix():
    snapshot = {"market": "us", "breadth": {"sp500": {"ma50": 70, "ma200": 60}}, "indexes": [{"ticker": "^VIX", "value": 18}]}
    assert recap._regime(snapshot)["label"] == "偏进攻"
    snapshot["breadth"] = {"sp500": {"ma50": 30, "ma200": 38}}
    assert recap._regime(snapshot)["label"] == "防守"


def test_generate_recaps_falls_back_without_model(monkeypatch):
    snapshot = {
        "market": "us", "market_name": "美股", "as_of_date": "2026-08-03", "source": "test",
        "indexes": [], "macro": [], "breadth": {"sp500": {"ma50": 50, "ma200": 50}}, "sectors": {}, "errors": [],
    }
    monkeypatch.setattr(recap, "_us_snapshot", lambda: dict(snapshot))
    monkeypatch.setattr(recap, "_search_market_news", lambda *args, **kwargs: [])
    monkeypatch.setattr(recap, "_call_llm", lambda snapshots, news: (None, {"used": False, "error": "offline"}))
    result = recap.generate_market_recap(markets=["us"])
    assert result["success"] is True
    assert result["payload"]["llm"]["used"] is False
    assert "跨资产宏观环境" in result["html_bytes"].decode("utf-8")


def test_scheduled_delivery_marker_only_accepts_newer_market_date(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKET_RECAP_CACHE_DB", str(tmp_path / "recap.db"))
    snapshots = [{"market": "us", "as_of_date": "2026-08-01"}]
    assert recap._recap_delivery_state("schedule-1", snapshots) is True
    recap.mark_market_recap_delivered("schedule-1", snapshots)
    assert recap._recap_delivery_state("schedule-1", snapshots) is False
    assert recap._recap_delivery_state("schedule-1", [{"market": "us", "as_of_date": "2026-08-04"}]) is True


def test_market_recap_job_helpers_use_generic_queue(monkeypatch, tmp_path):
    from daily_report import jobs
    monkeypatch.setenv("REPORT_JOB_DB", str(tmp_path / "jobs.db"))
    job = jobs.enqueue_market_recap_email_job(owner_key="user-a", recipient_email="user@example.com", markets=["us"])
    assert job["report_kind"] == "market_recap"
    assert json.loads(job["payload_json"])["markets"] == ["us"]
    schedule = jobs.create_weekly_market_recap_schedule(
        owner_key="user-a", recipient_email="user@example.com", markets=["cn", "us"], weekdays=[0], local_time="20:00"
    )
    assert schedule["report_kind"] == "market_recap"


def test_us_recap_uses_futures_for_sp500_and_nasdaq100_volume():
    assert recap._US_INDEXES["ES=F"] == "标普 500 E-mini 期货"
    assert recap._US_INDEXES["NQ=F"] == "纳斯达克 100 E-mini 期货"
    assert "^GSPC" not in recap._US_INDEXES
    assert "^NDX" not in recap._US_INDEXES


def test_cached_index_session_fields_include_ohlc_and_range():
    data = pd.DataFrame({
        ("Close", "ES=F"): [100.0, 104.0],
        ("Open", "ES=F"): [99.0, 101.0],
        ("High", "ES=F"): [101.0, 106.0],
        ("Low", "ES=F"): [98.0, 100.0],
        ("Volume", "ES=F"): [10.0, 30.0],
    })
    row = recap._add_index_session_fields([{"ticker": "ES=F", "name": "S&P 500 E-mini futures"}], data)[0]
    assert row["open"] == 101.0
    assert row["high"] == 106.0
    assert row["low"] == 100.0
    assert row["amplitude_pct"] == 6.0


def test_market_news_selection_reserves_categories_and_source_diversity():
    candidates = [
        {"title": "US market close", "snippet": "S&P 500 Nasdaq Wall Street", "source": "reuters.com", "category": "market_close", "score": 90, "published_date": "2026-08-04"},
        {"title": "US market close duplicate", "snippet": "S&P 500 Nasdaq Wall Street", "source": "reuters.com", "category": "market_close", "score": 80, "published_date": "2026-08-04"},
        {"title": "Treasury yields macro impact", "snippet": "US stock market Treasury Federal Reserve", "source": "ft.com", "category": "policy_macro", "score": 85, "published_date": "2026-08-04"},
        {"title": "Technology sector rotation", "snippet": "Nasdaq technology earnings US stock market", "source": "cnbc.com", "category": "sector_rotation", "score": 83, "published_date": "2026-08-04"},
    ]
    selected = recap._select_diverse_market_news(candidates, 3)
    assert {item["category"] for item in selected} == {"market_close", "policy_macro", "sector_rotation"}
    assert len({item["source"] for item in selected}) == 3


def test_market_news_article_enrichment_reuses_safe_fetcher(monkeypatch):
    from daily_report.src.stock_daily_agent import article_fetcher

    monkeypatch.setenv("MARKET_RECAP_NEWS_FETCH_MAX_URLS", "1")
    monkeypatch.setattr(
        article_fetcher,
        "_fetch_article_text",
        lambda url, timeout, max_chars: {
            "ok": True, "meta_description": "Verified market context from the article.",
            "text": "Longer article body.", "article_text_quality_ok": True,
        },
    )
    enriched = recap._enrich_market_news_with_articles([
        {"url": "https://www.reuters.com/markets/us/example", "score": 100, "snippet": "SERP summary"},
    ])
    assert enriched[0]["article_fetch_ok"] is True
    assert enriched[0]["evidence_excerpt"] == "Verified market context from the article."


def test_board_rankings_keep_industry_and_concept_rows():
    sectors = {"leaders": [], "laggards": []}
    recap._append_board_rankings(
        sectors,
        [{"name": "Industry", "kind": "industry", "change_pct": 2.0}],
        [{"name": "Industry weak", "kind": "industry", "change_pct": -1.0}],
    )
    recap._append_board_rankings(
        sectors,
        [{"name": "Concept", "kind": "concept", "change_pct": 3.0}],
        [{"name": "Concept weak", "kind": "concept", "change_pct": -2.0}],
    )
    assert {item["kind"] for item in sectors["leaders"]} == {"industry", "concept"}
    assert {item["kind"] for item in sectors["laggards"]} == {"industry", "concept"}


def test_combined_recap_renders_market_analysis_and_sources_separately():
    fields = {key: "market-specific analysis" for key in recap._RECAP_SECTION_KEYS}
    html = recap.render_market_recap_html({
        "title": "Combined", "generated_at": "now",
        "sections": {"cn": dict(fields), "us": dict(fields)},
        "snapshots": [
            {"market": "cn", "market_name": "China", "as_of_date": "2026-08-04", "deterministic_regime": {}, "indexes": [], "macro": [], "breadth": {}, "sectors": {}, "errors": []},
            {"market": "us", "market_name": "United States", "as_of_date": "2026-08-04", "deterministic_regime": {}, "indexes": [], "macro": [], "breadth": {}, "sectors": {}, "errors": []},
        ],
        "news": [
            {"market": "cn", "title": "CN catalyst", "source": "cn.test", "url": "https://cn.test/article", "snippet": "CN evidence"},
            {"market": "us", "title": "US catalyst", "source": "us.test", "url": "https://us.test/article", "snippet": "US evidence"},
        ],
    })
    cn_start = html.index("China消息面与催化")
    us_start = html.index("United States消息面与催化")
    cn_chapter = html[cn_start:us_start]
    us_chapter = html[us_start:]
    assert "CN catalyst" in cn_chapter
    assert "US catalyst" not in cn_chapter
    assert "US catalyst" in us_chapter
