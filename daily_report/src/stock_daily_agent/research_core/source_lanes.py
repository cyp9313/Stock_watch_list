# -*- coding: utf-8 -*-
"""Search Source Lanes（修改计划第六轮第 14 节）。

将搜索按来源通道分类，控制每个通道的优先级和配额：

- Official Lane（第 14.1 节）：IR / SEC EDGAR / 基金发行人 / 监管机构 / 评级机构 /
  交易所公告 / 官方新闻稿。优先级最高。
- News Lane（第 14.2 节）：Serper News vertical 优先，普通 Search fallback。
- Theme Lane（第 14.3 节）：仅用于 ETF / ETC / Commodity / Index / Crypto themes。

执行顺序：news first → official web second → ordinary web fallback。
"""
from __future__ import annotations

import os
from typing import Any


# Lane 标签
LANE_OFFICIAL = "official"
LANE_OFFICIAL_AND_NEWS = "official_and_news"
LANE_NEWS = "news"
LANE_THEME = "theme"
LANE_MACRO = "macro"


# 来源优先级（修改计划第 17 节）：官方 > 监管 > 评级机构 > Reuters/Bloomberg/AP > 专业媒体 > 二次媒体 > 聚合站
SOURCE_AUTHORITY_TIERS: dict[str, int] = {
    # 官方 / 监管 / 评级机构 = tier 1（100）
    "sec.gov": 100, "investor.gov": 100,
    # 评级机构
    "spglobal.com": 95, "moodys.com": 95, "fitchratings.com": 95,
    # 主流财经媒体 = tier 2（85）
    "reuters.com": 90, "bloomberg.com": 90, "apnews.com": 85,
    "ft.com": 85, "wsj.com": 85, "nytimes.com": 80, "cnbc.com": 80,
    "marketwatch.com": 75, "barrons.com": 75, "fool.com": 65,
    # 专业行业媒体
    "seekingalpha.com": 70, "investing.com": 60, "yahoofinance.com": 55,
    "finance.yahoo.com": 55,
    # 聚合站 = tier 3（40）
    "stockanalysis.com": 45, "finviz.com": 40, "marketbeat.com": 40,
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_str_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


# Serper 搜索类型配置（第 14.2 节）
def get_serper_types() -> list[str]:
    """返回 Serper 应使用的搜索类型，按优先级排序。"""
    return _env_str_list("PORTFOLIO_SERPER_TYPES", ["news", "search"])


def news_first() -> bool:
    """是否优先使用 News vertical。"""
    return _env_bool("PORTFOLIO_SERPER_NEWS_FIRST", True)


def source_authority_score(domain: str | None) -> int:
    """根据域名返回来源权威性评分（0-100）。

    用于 materiality ranking 的 source_authority_score 分量。
    """
    if not domain:
        return 30
    d = str(domain).strip().lower().removeprefix("www.")
    # 精确匹配
    if d in SOURCE_AUTHORITY_TIERS:
        return SOURCE_AUTHORITY_TIERS[d]
    # 后缀匹配（如 subdomain.reuters.com）
    for known, score in SOURCE_AUTHORITY_TIERS.items():
        if d.endswith("." + known) or d == known:
            return score
    # 官方 IR 域名启发式（包含 investor / ir / about）
    if any(k in d for k in ("investor", "ir.", "about.", "corporate.")):
        return 75
    # 默认：未知来源
    return 30


def is_official_domain(domain: str | None, official_domains: list[str] | None) -> bool:
    """判断域名是否属于官方来源。"""
    if not domain or not official_domains:
        return False
    d = str(domain).strip().lower().removeprefix("www.")
    for od in official_domains:
        od_clean = str(od).strip().lower().removeprefix("www.")
        if d == od_clean or d.endswith("." + od_clean) or od_clean.endswith("." + d):
            return True
    return False


def classify_source(
    domain: str | None,
    *,
    official_domains: list[str] | None = None,
    regulator_domains: list[str] | None = None,
) -> dict[str, Any]:
    """分类一个来源，返回 (source_type, authority_score, is_official)。

    source_type ∈ {official, regulator, rating_agency, major_media, specialty_media,
                   aggregator, unknown}
    """
    if not domain:
        return {"source_type": "unknown", "authority_score": 30, "is_official": False}
    d = str(domain).strip().lower().removeprefix("www.")

    if is_official_domain(d, official_domains):
        return {"source_type": "official", "authority_score": 95, "is_official": True}

    if regulator_domains and d in [str(x).strip().lower() for x in regulator_domains]:
        return {"source_type": "regulator", "authority_score": 100, "is_official": True}

    # 监管机构启发式
    regulator_hints = ("sec.gov", "fca.org.uk", "esma.europa.eu", "bafin.de", "sfc.hk",
                       "mas.gov.sg", "finra.org", "cftc.gov", "eba.europa.eu")
    if any(h in d for h in regulator_hints):
        return {"source_type": "regulator", "authority_score": 100, "is_official": True}

    # 评级机构
    if any(h in d for h in ("spglobal.com", "moodys.com", "fitchratings.com")):
        return {"source_type": "rating_agency", "authority_score": 95, "is_official": False}

    # 主流财经媒体
    major_media = ("reuters.com", "bloomberg.com", "apnews.com", "ft.com", "wsj.com",
                   "nytimes.com", "cnbc.com", "marketwatch.com", "barrons.com")
    if any(h in d for h in major_media):
        return {"source_type": "major_media", "authority_score": 85, "is_official": False}

    # 专业行业媒体
    specialty = ("seekingalpha.com", "fool.com", "investing.com", "yahoofinance.com",
                 "finance.yahoo.com")
    if any(h in d for h in specialty):
        return {"source_type": "specialty_media", "authority_score": 65, "is_official": False}

    # 聚合站
    aggregator = ("stockanalysis.com", "finviz.com", "marketbeat.com", "zacks.com",
                  "simplywall.st")
    if any(h in d for h in aggregator):
        return {"source_type": "aggregator", "authority_score": 40, "is_official": False}

    score = source_authority_score(d)
    return {"source_type": "unknown", "authority_score": score, "is_official": False}


def lane_execution_order() -> list[str]:
    """返回 lane 执行优先级（news first → official → ordinary fallback）。"""
    if news_first():
        return [LANE_NEWS, LANE_OFFICIAL_AND_NEWS, LANE_OFFICIAL, LANE_THEME, LANE_MACRO]
    return [LANE_OFFICIAL, LANE_OFFICIAL_AND_NEWS, LANE_NEWS, LANE_THEME, LANE_MACRO]


def should_use_news_vertical(lane: str) -> bool:
    """判断该 lane 是否应使用 Serper News vertical。"""
    return lane in {LANE_NEWS, LANE_OFFICIAL_AND_NEWS}


def should_use_search_vertical(lane: str) -> bool:
    """判断该 lane 是否应使用普通 Search vertical（作为 fallback 或主通道）。"""
    # official lane 主要用 site: 查询，走普通 search
    # theme lane 走普通 search（news vertical 对主题覆盖差）
    # macro lane 走普通 search
    return lane in {LANE_OFFICIAL, LANE_THEME, LANE_MACRO, LANE_OFFICIAL_AND_NEWS}
