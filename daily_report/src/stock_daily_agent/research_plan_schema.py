# -*- coding: utf-8 -*-
"""Research Plan Schema 与 Allowlist（修改计划第六轮第 7-9 节）。

定义 AI Query Planner 输出的严格 Schema 以及 Python 端的合法集合：

- ALLOWED_EVENT_NEEDS（第 8 节）
- ALLOWED_LOOKBACK_DAYS（第 9 节）
- ALLOWED_LANES（第 13/14 节）
- ALLOWED_RESEARCH_PRIORITIES
- 每个 ticker / 研究问题 / query 的预算上限（第 10/11 节）

Planner 不允许自由发明任意枚举值；任何不在 allowlist 内的字段都会被
Plan Validator 拒绝或降级到 fallback planner。
"""
from __future__ import annotations

import os
from typing import Any


# ── 第 8 节：允许的 Event Need（Python allowlist）────────────
ALLOWED_EVENT_NEEDS: frozenset[str] = frozenset({
    "latest_official_filing",
    "earnings_date",
    "earnings_results",
    "guidance",
    "credit_and_financing",
    "capital_raise",
    "analyst_revision",
    "regulatory",
    "litigation",
    "product_event",
    "major_contract",
    "management_change",
    "governance",
    "merger_acquisition",
    "index_rebalance",
    "fund_flow",
    "aum_change",
    "premium_discount",
    "theme_supply",
    "theme_policy",
    "commodity_driver",
    "crypto_regulation",
    "trading_volume",
    "security_incident",
    "macro_driver",
})


# ── 第 9 节：允许的时间窗口（仅这些档位合法）────────────────
ALLOWED_LOOKBACK_DAYS: frozenset[int] = frozenset({7, 14, 30, 45, 120, 365})


# ── 第 14 节：允许的搜索 Lane ────────────────────────────────
ALLOWED_LANES: frozenset[str] = frozenset({
    "official_and_news",   # 官方 + 主流新闻
    "news",                # 仅主流新闻 vertical
    "theme",               # 主题驱动（ETF / ETC / Index / Crypto themes）
    "macro",               # 宏观
})


# ── 第 7 节：允许的研究优先级 ────────────────────────────────
ALLOWED_RESEARCH_PRIORITIES: frozenset[str] = frozenset({
    "high", "medium", "low",
})


# ── 第 13 节：URL scheme allowlist ───────────────────────────
ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"https", "http"})


# ── 第 10/11 节：Planner 调用预算（可通过环境变量调整）─────
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


PLANNER_MAX_QUESTIONS_PER_TICKER: int = _env_int(
    "PORTFOLIO_RESEARCH_PLANNER_MAX_QUESTIONS_PER_TICKER", 4,
)
PLANNER_MAX_QUERIES_PER_QUESTION: int = _env_int(
    "PORTFOLIO_RESEARCH_PLANNER_MAX_QUERIES_PER_QUESTION", 3,
)
PLANNER_MAX_TOTAL_QUERIES: int = _env_int(
    "PORTFOLIO_RESEARCH_PLANNER_MAX_TOTAL_QUERIES", 24,
)
PLANNER_MAX_RETRIES: int = _env_int(
    "PORTFOLIO_RESEARCH_PLANNER_MAX_RETRIES", 1,
)
PLANNER_TEMPERATURE: float = float(
    os.environ.get("PORTFOLIO_RESEARCH_PLANNER_TEMPERATURE", "0.1") or "0.1"
)


# ── Schema 校验工具 ─────────────────────────────────────────
def is_valid_event_need(value: Any) -> bool:
    return isinstance(value, str) and value in ALLOWED_EVENT_NEEDS


def is_valid_lookback_days(value: Any) -> bool:
    try:
        return int(value) in ALLOWED_LOOKBACK_DAYS
    except (TypeError, ValueError):
        return False


def is_valid_lane(value: Any) -> bool:
    return isinstance(value, str) and value in ALLOWED_LANES


def is_valid_research_priority(value: Any) -> bool:
    return isinstance(value, str) and value in ALLOWED_RESEARCH_PRIORITIES


# ── Schema 描述（供 Prompt 使用）────────────────────────────
# 注意：JSON 含大量 {} 花括号，不能用 .format()，改用占位符替换。
SCHEMA_DESCRIPTION_ZH: str = """研究计划 Schema（严格 JSON，不要输出额外字段）：
{
  "plan_version": "1.0",
  "tickers": [
    {
      "ticker": "<TICKER>",
      "research_priority": "high|medium|low",
      "primary_language": "en|zh-CN",
      "research_questions": [
        {
          "question_id": "<TICKER>_Q<n>",
          "event_need": "<event_need>",
          "reason_zh": "为什么这个研究问题与当前持仓风险相关",
          "lane": "official_and_news|news|theme|macro",
          "lookback_days": 7|14|30|45|120|365,
          "queries": ["<query 1>", "<query 2>", ...],
          "preferred_domains": ["investor.example.com", "sec.gov", ...],
          "required_entities": ["<Company Name>"],
          "exclude_terms": ["<noise term>"],
          "priority": 1
        }
      ]
    }
  ],
  "macro_questions": []
}

允许的 event_need（仅这些值合法，不得发明新值）：
latest_official_filing, earnings_date, earnings_results, guidance,
credit_and_financing, capital_raise, analyst_revision, regulatory,
litigation, product_event, major_contract, management_change,
governance, merger_acquisition, index_rebalance, fund_flow,
aum_change, premium_discount, theme_supply, theme_policy,
commodity_driver, crypto_regulation, trading_volume,
security_incident, macro_driver

允许的 lookback_days（仅这些值合法）：7, 14, 30, 45, 120, 365

允许的 lane：official_and_news, news, theme, macro

每个 ticker 最多 __MAX_QUESTIONS__ 个研究问题；
每个研究问题最多 __MAX_QUERIES__ 条 query；
总 query 数不得超过 __MAX_TOTAL__。
""".replace("__MAX_QUESTIONS__", str(PLANNER_MAX_QUESTIONS_PER_TICKER)).replace(
    "__MAX_QUERIES__", str(PLANNER_MAX_QUERIES_PER_QUESTION)
).replace("__MAX_TOTAL__", str(PLANNER_MAX_TOTAL_QUERIES))
