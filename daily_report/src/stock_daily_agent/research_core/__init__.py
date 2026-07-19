# -*- coding: utf-8 -*-
"""研究核心子模块（修改计划第六轮第 4 节）。

将原 Portfolio 新闻研究管线按职责拆分为可复用组件，供 Portfolio 报告与
单标的日报共享。每个子模块保持单一职责，避免循环依赖。
"""
from .language_router import (
    determine_search_language,
    is_a_share,
    LanguageRoutingError,
)
from .source_lanes import (
    LANE_OFFICIAL,
    LANE_OFFICIAL_AND_NEWS,
    LANE_NEWS,
    LANE_THEME,
    LANE_MACRO,
    SOURCE_AUTHORITY_TIERS,
    classify_source,
    source_authority_score,
    is_official_domain,
    lane_execution_order,
    should_use_news_vertical,
    should_use_search_vertical,
    get_serper_types,
    news_first,
)
from .entity_resolution import (
    resolve_primary_entity,
    classify_etf_page,
)
from .materiality_ranker import (
    rank_evidence,
    MIN_PRIMARY_ENTITY_SCORE,
    MIN_MATERIALITY_SCORE,
)
from .event_normalizer import (
    normalize_event,
    normalize_event_type,
    extract_event_date,
)
from .event_clusterer import (
    cluster_events,
)
from .evidence_verifier import (
    verify_evidence,
    compute_corroboration_counts,
    VERIFICATION_LEVELS,
)

__all__ = [
    "determine_search_language",
    "is_a_share",
    "LanguageRoutingError",
    "LANE_OFFICIAL",
    "LANE_OFFICIAL_AND_NEWS",
    "LANE_NEWS",
    "LANE_THEME",
    "LANE_MACRO",
    "SOURCE_AUTHORITY_TIERS",
    "classify_source",
    "source_authority_score",
    "is_official_domain",
    "lane_execution_order",
    "should_use_news_vertical",
    "should_use_search_vertical",
    "get_serper_types",
    "news_first",
    "resolve_primary_entity",
    "classify_etf_page",
    "rank_evidence",
    "MIN_PRIMARY_ENTITY_SCORE",
    "MIN_MATERIALITY_SCORE",
    "normalize_event",
    "normalize_event_type",
    "extract_event_date",
    "cluster_events",
]
