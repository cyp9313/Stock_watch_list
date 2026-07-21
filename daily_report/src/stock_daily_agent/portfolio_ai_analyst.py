# -*- coding: utf-8 -*-
"""Portfolio AI Analyst v3.

This module intentionally treats the model as an investment-report analyst rather
than as an evidence ETL pipeline. Python owns all quantitative facts; one
DashScope call may add interpretation and current public context. The returned
analysis is validated for structure, ticker scope, URLs and dates, but an
imperfect citation never suppresses the entire AI report.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import ipaddress
import os
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit


class PortfolioAnalystUnavailable(RuntimeError):
    """The single model call could not be made."""


class PortfolioAnalystOutputError(RuntimeError):
    """The model response could not be converted into a usable analyst report."""


REPORT_STYLE_OPTIONS = {
    "balanced": "均衡分析",
    "concise": "结论优先",
    "deep_dive": "深度研究",
    "risk_control": "风险控制",
    "opportunity": "机会导向",
}
DETAIL_LEVEL_OPTIONS = {
    "brief": "简洁",
    "standard": "标准",
    "detailed": "详细",
}
ADVICE_MODE_OPTIONS = {
    "observe_only": "仅观察",
    "conditional": "条件式建议",
    "actionable": "可执行建议",
}
LANGUAGE_OPTIONS = {
    "zh-CN": "简体中文",
    "en": "English",
    "de": "Deutsch",
}
FOCUS_OPTIONS = {
    "technical": "技术面",
    "news": "消息面",
    "portfolio_risk": "组合风险",
    "macro": "宏观环境",
    "valuation": "估值与基本面",
    "actions": "操作与观察条件",
}

_DEFAULT_FOCUS = ["technical", "news", "portfolio_risk", "actions"]
_ALLOWED_ACTIONS = {"watch", "hold", "add", "trim", "reduce", "exit"}
_ALLOWED_STANCES = {"bullish", "cautious_bullish", "balanced", "neutral", "cautious", "defensive", "observe"}
_ALLOWED_RISK_LEVELS = {"low", "medium", "high", "very_high"}
_ALLOWED_IMPACT = {"positive", "negative", "neutral", "mixed"}
_ALLOWED_HORIZON = {"immediate", "short_term", "medium_term", "long_term"}


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict())
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__"):
        try:
            return _jsonable(vars(value))
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(low, min(high, number))


def _safe_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clean_text(value: Any, limit: int = 2000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def normalize_analyst_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize user-facing AI report settings while preserving legacy fields."""
    raw = dict(settings or {})
    result: dict[str, Any] = {}

    result["base_currency"] = _clean_text(raw.get("base_currency") or "EUR", 8).upper() or "EUR"
    result["benchmark"] = _clean_text(raw.get("benchmark") or "^GSPC", 32) or "^GSPC"

    horizon = _clean_text(raw.get("investment_horizon") or "1-3m", 16)
    result["investment_horizon"] = horizon if horizon in {"1-4w", "1-3m", "3-6m", "6-12m", "12m+"} else "1-3m"

    risk = _clean_text(raw.get("risk_profile") or "balanced", 24).lower()
    result["risk_profile"] = risk if risk in {"conservative", "balanced", "growth", "aggressive"} else "balanced"

    style = _clean_text(raw.get("report_style") or "balanced", 24).lower()
    result["report_style"] = style if style in REPORT_STYLE_OPTIONS else "balanced"

    detail = _clean_text(raw.get("detail_level") or "standard", 24).lower()
    result["detail_level"] = detail if detail in DETAIL_LEVEL_OPTIONS else "standard"

    advice_mode = _clean_text(raw.get("advice_mode") or "conditional", 24).lower()
    result["advice_mode"] = advice_mode if advice_mode in ADVICE_MODE_OPTIONS else "conditional"

    language = _clean_text(raw.get("report_language") or "zh-CN", 16)
    result["report_language"] = language if language in LANGUAGE_OPTIONS else "zh-CN"

    focus = raw.get("analysis_focus")
    if not isinstance(focus, list):
        focus = list(_DEFAULT_FOCUS)
    focus = [str(item).strip() for item in focus if str(item).strip() in FOCUS_OPTIONS]
    result["analysis_focus"] = list(dict.fromkeys(focus)) or list(_DEFAULT_FOCUS)

    result["include_news"] = _as_bool(raw.get("include_news"), True)
    result["include_macro"] = _as_bool(raw.get("include_macro"), True)
    result["include_all_holdings"] = _as_bool(raw.get("include_all_holdings"), False)
    result["allow_add"] = _as_bool(raw.get("allow_add"), True)
    result["allow_reduce"] = _as_bool(raw.get("allow_reduce"), True)

    result["news_lookback_days"] = _safe_int(raw.get("news_lookback_days"), 3, 90, 30)
    result["max_focus_holdings"] = _safe_int(
        raw.get("max_focus_holdings", raw.get("research_max_tickers", 5)), 2, 12, 5
    )
    result["max_position_pct"] = _clamp_float(raw.get("max_position_pct"), 0.0, 100.0, 20.0)
    result["max_group_pct"] = _clamp_float(raw.get("max_group_pct"), 0.0, 100.0, 40.0)
    result["custom_instructions"] = _clean_text(raw.get("custom_instructions"), 600)

    result["model"] = _clean_text(raw.get("model") or os.getenv("PORTFOLIO_REPORT_MODEL") or "deepseek-v4-pro", 80)
    thinking_value = raw.get("enable_thinking")
    if thinking_value is None:
        thinking_value = os.getenv("PORTFOLIO_ENABLE_THINKING", "true")
    result["enable_thinking"] = _as_bool(thinking_value, True)
    reasoning = _clean_text(raw.get("reasoning_effort") or os.getenv("PORTFOLIO_REASONING_EFFORT") or "high", 16).lower()
    result["reasoning_effort"] = reasoning if reasoning in {"high", "max"} else "high"
    return result


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 3:
            raw = "\n".join(lines[1:-1]).strip()
    candidates = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _valid_http_url(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_reserved or address.is_unspecified
    ):
        return ""
    return text


def _valid_date(value: Any, report_date: dt.date) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        try:
            parsed = dt.datetime.strptime(text[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return ""
    return parsed.isoformat() if parsed <= report_date else ""


def _response_parts(response: Any) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
    output = _obj_get(response, "output", {}) or {}
    choices = _obj_get(output, "choices", []) or []
    content = ""
    reasoning_content = ""
    if choices:
        message = _obj_get(choices[0], "message", {}) or {}
        content = str(_obj_get(message, "content", "") or "")
        reasoning_content = str(_obj_get(message, "reasoning_content", "") or "")
    search_info = _obj_get(output, "search_info", {}) or {}
    raw_sources = _obj_get(search_info, "search_results", []) or []
    sources = []
    for item in raw_sources:
        obj = _jsonable(item)
        if isinstance(obj, dict):
            sources.append(obj)
    usage = _jsonable(_obj_get(response, "usage", {}) or {})
    if not usage:
        usage = _jsonable(_obj_get(output, "usage", {}) or {})
    meta = {
        "request_id": str(_obj_get(response, "request_id", "") or ""),
        "code": str(_obj_get(response, "code", "") or ""),
        "message": str(_obj_get(response, "message", "") or ""),
        "usage": usage if isinstance(usage, dict) else {},
    }
    return content, reasoning_content, sources, meta


def _style_directive(settings: dict[str, Any]) -> str:
    return {
        "concise": "结论优先，减少铺垫；每节只保留最重要的判断、风险和触发条件。",
        "deep_dive": "进行深度交叉分析，解释技术面、消息面和组合风险之间的因果关系与冲突。",
        "risk_control": "以回撤、集中度、相关性、风险贡献和下行保护为主线，机会判断放在风险约束之后。",
        "opportunity": "优先识别风险收益比改善、趋势确认和催化剂，但必须同时说明失效条件。",
        "balanced": "均衡呈现收益机会、技术状态、消息催化剂与组合风险。",
    }[settings["report_style"]]


def _horizon_directive(horizon: str) -> str:
    return {
        "1-4w": "以未来1至4周的催化剂、短期趋势、波动和止损条件为主。",
        "1-3m": "以未来1至3个月的财报、指引、趋势和仓位风险为主。",
        "3-6m": "重视未来3至6个月的基本面兑现、行业趋势和中期技术结构。",
        "6-12m": "强调未来6至12个月的盈利趋势、估值消化和战略变化。",
        "12m+": "以一年以上长期竞争力、资本配置、结构性增长和回撤承受能力为主。",
    }[horizon]


def _risk_directive(risk: str) -> str:
    return {
        "conservative": "用户偏保守：优先保护本金、降低尾部风险和高波动集中暴露。",
        "balanced": "用户风险偏好均衡：在收益机会和回撤控制之间保持平衡。",
        "growth": "用户偏成长：允许较高波动，但必须区分正常波动与基本面破坏。",
        "aggressive": "用户偏进取：可以讨论高弹性机会，但必须提供清晰的失效和减险条件。",
    }[risk]


def _detail_directive(detail: str) -> str:
    return {
        "brief": "保持简洁：核心结论3至5条，每个重点持仓用短段落，避免重复指标。",
        "standard": "采用标准篇幅：解释主要因果关系，并为重点持仓提供技术面、消息面和观察条件。",
        "detailed": "采用详细篇幅：解释信号冲突、情景差异、风险传导和建议失效条件，但不要重复罗列原始表格。",
    }[detail]


def _content_directive(settings: dict[str, Any]) -> str:
    directives = []
    if not settings["include_news"]:
        directives.append("用户关闭了联网消息面：news_analysis必须返回空列表，news_view说明该部分未启用。")
    if not settings["include_macro"]:
        directives.append("用户关闭了宏观分析：不要展开宏观叙事，只讨论直接影响持仓的公司和行业因素。")
    if settings["include_all_holdings"]:
        directives.append("需要覆盖输入中的全部持仓，但仍按重要性分配篇幅。")
    else:
        directives.append("重点分析focus_tickers，其余持仓只在影响组合结论时简要提及。")
    return " ".join(directives)


def _advice_directive(settings: dict[str, Any]) -> str:
    mode = settings["advice_mode"]
    if mode == "observe_only":
        return "所有标的的action必须为watch或hold，不得生成方向性交易指令。"
    if mode == "conditional":
        return "可以给出add、trim或reduce，但必须是条件式建议，并列出execute_if与cancel_or_upgrade_if。"
    return "可以给出可执行建议，但仍需列出触发条件、失效条件和主要不确定性。"


def _focus_directive(settings: dict[str, Any]) -> str:
    labels = [FOCUS_OPTIONS[item] for item in settings["analysis_focus"]]
    return "本报告重点：" + "、".join(labels) + "。"


def build_analyst_context(
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    settings: dict[str, Any],
    *,
    instrument_metadata: dict[str, Any] | None = None,
    risk_findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a compact immutable quantitative context for the model."""
    instrument_metadata = instrument_metadata or {}
    holdings_by_ticker = {str(h.get("ticker")): h for h in snapshot.get("holdings", []) or []}
    risk_by_ticker = {
        str(item.get("ticker")): item for item in metrics.get("risk_contributions", []) or []
    }
    ranked = [str(t) for t in (ranking.get("top_risk_tickers") or [])]
    limit = settings["max_focus_holdings"]
    focus_tickers = ranked[:limit]

    compact_holdings = []
    all_ordered = [str(item.get("ticker")) for item in ranking.get("items", []) or []]
    if not all_ordered:
        all_ordered = list(holdings_by_ticker)
    selected = all_ordered if settings["include_all_holdings"] else focus_tickers
    for ticker in selected:
        h = holdings_by_ticker.get(ticker) or {}
        risk_item = risk_by_ticker.get(ticker) or {}
        meta = instrument_metadata.get(ticker) or {}
        compact_holdings.append({
            "ticker": ticker,
            "name": h.get("name") or meta.get("name") or ticker,
            "instrument_type": meta.get("instrument_type") or "UNKNOWN",
            "theme": meta.get("theme") or meta.get("underlying_index"),
            "weight": h.get("weight"),
            "market_value": h.get("market_value_base"),
            "profit_loss_pct": h.get("profit_loss_pct"),
            "return_1d": h.get("return_1d"),
            "return_5d": h.get("return_5d"),
            "return_1m": h.get("return_1m"),
            "return_ytd": h.get("return_ytd"),
            "price_vs_ema20_pct": h.get("price_vs_ema20_pct"),
            "price_vs_ema50_pct": h.get("price_vs_ema50_pct"),
            "price_vs_ema200_pct": h.get("price_vs_ema200_pct"),
            "rsi": h.get("rsi"),
            "volume_ratio": h.get("volume_ratio"),
            "beta": h.get("beta"),
            "risk_contribution": risk_item.get("risk_contribution"),
        })

    return {
        "report_date": snapshot.get("report_date"),
        "report_time": (snapshot.get("run_timeline") or {}).get("snapshot_completed_at") or snapshot.get("as_of"),
        "portfolio_name": snapshot.get("portfolio_name"),
        "base_currency": snapshot.get("base_currency"),
        "benchmark": snapshot.get("benchmark"),
        "summary": snapshot.get("summary") or {},
        "portfolio_metrics": {
            "portfolio_risk_score": metrics.get("portfolio_risk_score"),
            "risk_score_confidence": metrics.get("risk_score_confidence"),
            "portfolio_beta": metrics.get("portfolio_beta"),
            "annualized_volatility": metrics.get("annualized_volatility"),
            "max_drawdown_63d": metrics.get("max_drawdown_63d"),
            "max_drawdown_252d": metrics.get("max_drawdown_252d"),
            "top1_weight": metrics.get("top1_weight"),
            "top3_weight": metrics.get("top3_weight"),
            "effective_holdings": metrics.get("effective_holdings"),
            "hhi_10000": metrics.get("hhi_10000"),
            "weight_below_ema20": metrics.get("weight_below_ema20"),
            "weight_below_ema50": metrics.get("weight_below_ema50"),
            "weight_below_ema200": metrics.get("weight_below_ema200"),
            "relative_returns": metrics.get("relative_returns") or {},
            "risk_score_components": metrics.get("risk_score_components") or {},
        },
        "focus_tickers": focus_tickers,
        "holdings": compact_holdings,
        "deterministic_risk_findings": risk_findings or [],
    }


def build_analyst_messages(context: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, str]]:
    language = LANGUAGE_OPTIONS[settings["report_language"]]
    schema = {
        "portfolio_view": {
            "stance": "balanced|bullish|cautious_bullish|neutral|cautious|defensive|observe",
            "risk_level": "low|medium|high|very_high",
            "confidence": "0..1",
            "summary": ["3-8 concise conclusions"],
        },
        "portfolio_analysis": {
            "trend_view": "string",
            "concentration_view": "string",
            "risk_view": "string",
            "relative_performance_view": "string",
            "news_view": "string",
        },
        "key_risks": [{
            "title": "string", "severity": "low|medium|high",
            "description": "string", "affected_tickers": ["portfolio tickers"]
        }],
        "holding_analysis": [{
            "ticker": "portfolio ticker",
            "technical_view": "string",
            "news_view": "string",
            "combined_view": "string",
            "action": "watch|hold|add|trim|reduce|exit",
            "confidence": "0..1",
            "execute_if": ["conditions"],
            "cancel_or_upgrade_if": ["conditions"],
            "monitoring_items": ["items"],
            "bull_case": "string",
            "bear_case": "string",
        }],
        "news_analysis": [{
            "ticker": "portfolio ticker or empty for macro",
            "headline": "string",
            "summary": "string",
            "why_it_matters": "string",
            "impact_direction": "positive|negative|neutral|mixed",
            "impact_horizon": "immediate|short_term|medium_term|long_term",
            "source_name": "string",
            "source_url": "http(s) URL or empty",
            "published_date": "YYYY-MM-DD or empty",
            "confidence": "0..1",
        }],
        "watch_items": [{"title": "string", "reason": "string", "affected_tickers": ["tickers"]}],
        "data_limitations": ["string"],
        "disclaimer": "string",
    }
    system = f"""
你是一名专业、审慎、结论清晰的投资组合分析师。AI分析叙事使用{language}；HTML界面标签由本地模板处理。

Python 提供的价格、权重、收益、Beta、回撤、技术指标和风险贡献是唯一可信的量化事实：
- 不得修改、重新计算或编造这些数字；
- 不得把联网搜索中的旧价格覆盖到量化数据；
- 可以解释数字，但不能声称不存在于输入中的精确数值。

使用一次内置联网搜索补充近期重要公司、行业与宏观消息。新闻分析规则：
- 只讨论与输入持仓或组合风险直接相关的内容；
- 有真实来源链接时填写 source_url；没有链接时留空，绝不构造 URL；
- 找不到重要新闻时明确说明，不要为了覆盖所有标的而凑数；
- 区分已确认事实、市场观点与推测；
- 消息链接不完美时仍可给出谨慎的综合判断，但必须说明不确定性。

用户设定：
- {_style_directive(settings)}
- {_horizon_directive(settings['investment_horizon'])}
- {_risk_directive(settings['risk_profile'])}
- {_advice_directive(settings)}
- {_detail_directive(settings['detail_level'])}
- {_focus_directive(settings)}
- {_content_directive(settings)}
- 单一持仓上限偏好：{settings['max_position_pct']:.1f}%
- 单一分组上限偏好：{settings['max_group_pct']:.1f}%
- 允许加仓建议：{'是' if settings['allow_add'] else '否'}
- 允许减仓建议：{'是' if settings['allow_reduce'] else '否'}
- 新闻回看窗口：{settings['news_lookback_days']}天
- 自定义要求：{settings['custom_instructions'] or '无'}

严格返回一个 JSON 对象，不要 Markdown，不要代码围栏，不要输出思考过程。JSON 结构：
{json.dumps(schema, ensure_ascii=False, indent=2)}
""".strip()
    user = (
        "请结合以下不可修改的 Python 量化上下文与联网搜索，生成完整投资组合报告。"
        "优先分析 focus_tickers；其余持仓仅在对组合结论有重要影响时讨论。\n\n"
        + json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _source_rows(raw_sources: list[dict[str, Any]], report_date: dt.date) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    for index, raw in enumerate(raw_sources):
        url = _valid_http_url(raw.get("url") or raw.get("link") or raw.get("source_url"))
        if not url or url in seen:
            continue
        seen.add(url)
        rows.append({
            "source_index": raw.get("index", index),
            "title": _clean_text(raw.get("title") or raw.get("name") or url, 300),
            "url": url,
            "source_name": _clean_text(raw.get("site_name") or raw.get("source") or urlsplit(url).hostname, 120),
            "published_date": _valid_date(
                raw.get("published_date") or raw.get("publish_time") or raw.get("date") or raw.get("published_time"),
                report_date,
            ),
            "snippet": _clean_text(raw.get("snippet") or raw.get("content") or raw.get("summary"), 800),
        })
    return rows


def _normalise_ticker(value: Any, allowed: set[str]) -> str:
    ticker = str(value or "").strip().upper()
    if ticker in allowed:
        return ticker
    return ""


def _normalise_strings(value: Any, *, limit_items: int = 8, limit_text: int = 800) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean_text(item, limit_text) for item in value[:limit_items] if _clean_text(item, limit_text)]


def _current_weight_map(snapshot: dict[str, Any]) -> dict[str, float]:
    result = {}
    for holding in snapshot.get("holdings", []) or []:
        ticker = str(holding.get("ticker") or "").upper()
        try:
            result[ticker] = float(holding.get("weight") or 0.0)
        except (TypeError, ValueError):
            result[ticker] = 0.0
    return result


def _action_target_range(action: str, current: float, settings: dict[str, Any]) -> tuple[float, float]:
    max_position = settings["max_position_pct"] / 100.0
    if action == "add":
        return current, max(current, min(max_position, current * 1.20))
    if action == "trim":
        return max(0.0, current * 0.82), max(0.0, current * 0.95)
    if action == "reduce":
        return max(0.0, current * 0.55), max(0.0, current * 0.85)
    if action == "exit":
        return 0.0, max(0.0, current * 0.10)
    return current, current


def _enforce_action_policy(action: str, settings: dict[str, Any]) -> str:
    action = action if action in _ALLOWED_ACTIONS else "watch"
    if settings["advice_mode"] == "observe_only":
        return "hold" if action == "hold" else "watch"
    if action == "add" and not settings["allow_add"]:
        return "watch"
    if action in {"trim", "reduce", "exit"} and not settings["allow_reduce"]:
        return "watch"
    return action


def normalise_analyst_payload(
    payload: dict[str, Any],
    *,
    snapshot: dict[str, Any],
    ranking: dict[str, Any],
    settings: dict[str, Any],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    report_date = dt.date.fromisoformat(str(snapshot.get("report_date") or dt.date.today().isoformat())[:10])
    allowed = {str(h.get("ticker") or "").upper() for h in snapshot.get("holdings", []) or []}
    weights = _current_weight_map(snapshot)
    ranked = [str(x).upper() for x in (ranking.get("top_risk_tickers") or []) if str(x).upper() in allowed]

    view = payload.get("portfolio_view") if isinstance(payload.get("portfolio_view"), dict) else {}
    stance = str(view.get("stance") or "balanced").strip().lower()
    if stance not in _ALLOWED_STANCES:
        stance = "balanced"
    risk_level = str(view.get("risk_level") or "medium").strip().lower()
    if risk_level not in _ALLOWED_RISK_LEVELS:
        risk_level = "medium"
    confidence = _clamp_float(view.get("confidence"), 0.0, 1.0, 0.55)
    summary = _normalise_strings(view.get("summary"), limit_items=8, limit_text=500)

    pa_raw = payload.get("portfolio_analysis") if isinstance(payload.get("portfolio_analysis"), dict) else {}
    portfolio_analysis = {
        key: _clean_text(pa_raw.get(key), 1400)
        for key in ("trend_view", "concentration_view", "risk_view", "relative_performance_view", "news_view")
    }

    key_risks = []
    for item in payload.get("key_risks") or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "medium").lower()
        if severity not in {"low", "medium", "high"}:
            severity = "medium"
        affected = [_normalise_ticker(x, allowed) for x in item.get("affected_tickers") or []]
        affected = [x for x in affected if x]
        title = _clean_text(item.get("title"), 180)
        description = _clean_text(item.get("description"), 1200)
        if title and description:
            key_risks.append({
                "title": title, "severity": severity, "description": description,
                "affected_tickers": list(dict.fromkeys(affected))[:10],
            })

    holding_analysis = []
    by_ticker = {}
    for item in payload.get("holding_analysis") or []:
        if not isinstance(item, dict):
            continue
        ticker = _normalise_ticker(item.get("ticker"), allowed)
        if not ticker or ticker in by_ticker:
            continue
        action = _enforce_action_policy(str(item.get("action") or "watch").lower(), settings)
        current = weights.get(ticker, 0.0)
        target_min, target_max = _action_target_range(action, current, settings)
        normalised = {
            "ticker": ticker,
            "technical_view": _clean_text(item.get("technical_view"), 1200),
            "news_view": _clean_text(item.get("news_view"), 1200),
            "combined_view": _clean_text(item.get("combined_view"), 1400),
            "action": action,
            "confidence": _clamp_float(item.get("confidence"), 0.0, 1.0, confidence),
            "execute_if": _normalise_strings(item.get("execute_if"), limit_items=6, limit_text=500),
            "cancel_or_upgrade_if": _normalise_strings(item.get("cancel_or_upgrade_if"), limit_items=6, limit_text=500),
            "monitoring_items": _normalise_strings(item.get("monitoring_items"), limit_items=8, limit_text=500),
            "bull_case": _clean_text(item.get("bull_case"), 900),
            "bear_case": _clean_text(item.get("bear_case"), 900),
            "current_weight": current,
            "target_weight_min": target_min,
            "target_weight_max": target_max,
        }
        by_ticker[ticker] = normalised
        holding_analysis.append(normalised)

    # Ensure focus names still appear even if the model omitted one. This is a
    # display safeguard, not a second model call or generated market claim.
    for ticker in ranked[: settings["max_focus_holdings"]]:
        if ticker in by_ticker:
            continue
        current = weights.get(ticker, 0.0)
        fallback_item = {
            "ticker": ticker,
            "technical_view": "请结合下方确定性技术快照持续观察。",
            "news_view": "模型未对该标的形成独立消息面结论。",
            "combined_view": "该标的是组合风险贡献靠前持仓，保留在重点观察清单。",
            "action": "watch",
            "confidence": min(confidence, 0.45),
            "execute_if": [],
            "cancel_or_upgrade_if": ["出现新的重要公司事件或关键技术位变化时重新评估。"],
            "monitoring_items": ["风险贡献、EMA20/EMA50、回撤与最新公司公告"],
            "bull_case": "技术趋势和基本面催化剂同时改善。",
            "bear_case": "风险贡献继续上升且技术趋势进一步恶化。",
            "current_weight": current,
            "target_weight_min": current,
            "target_weight_max": current,
        }
        by_ticker[ticker] = fallback_item
        holding_analysis.append(fallback_item)

    source_urls = {item["url"] for item in sources}
    news_analysis = []
    for index, item in enumerate(payload.get("news_analysis") or []):
        if not isinstance(item, dict):
            continue
        ticker = _normalise_ticker(item.get("ticker"), allowed)
        url = _valid_http_url(item.get("source_url"))
        impact = str(item.get("impact_direction") or "neutral").lower()
        if impact not in _ALLOWED_IMPACT:
            impact = "neutral"
        horizon = str(item.get("impact_horizon") or "short_term").lower()
        if horizon not in _ALLOWED_HORIZON:
            horizon = "short_term"
        headline = _clean_text(item.get("headline"), 400)
        summary_text = _clean_text(item.get("summary"), 1600)
        if not headline and not summary_text:
            continue
        news_analysis.append({
            "evidence_id": f"AI{index + 1:03d}",
            "ticker": ticker or None,
            "title": headline or "AI 联网消息分析",
            "summary_zh": summary_text,
            "what_happened_zh": summary_text,
            "why_it_matters_to_ticker_zh": _clean_text(item.get("why_it_matters"), 1200),
            "impact_direction": "neutral" if impact == "mixed" else impact,
            "impact_horizon": horizon,
            "source_name": _clean_text(item.get("source_name"), 160) or "AI 联网综合",
            "url": url,
            "published_date": _valid_date(item.get("published_date"), report_date),
            "confidence": _clamp_float(item.get("confidence"), 0.0, 1.0, 0.55),
            "source_verified": bool(url and url in source_urls),
            "article_fetch_ok": False,
            "source_quality": "tier_2" if url else "tier_3",
            "verification_level_zh": (
                "DashScope 返回来源" if url and url in source_urls
                else "AI 返回链接·未本地独立核验" if url
                else "模型联网综合·未返回链接"
            ),
            "content_type": "news_report",
            "event_type": "AI 联网分析",
            "supports_action": "watch",
            "does_not_prove_zh": "单条联网材料不足以单独证明方向性交易结论。",
        })

    if not settings["include_news"]:
        news_analysis = []
        portfolio_analysis["news_view"] = "用户设置已关闭联网消息面分析。"

    watch_items = []
    for item in payload.get("watch_items") or []:
        if not isinstance(item, dict):
            continue
        affected = [_normalise_ticker(x, allowed) for x in item.get("affected_tickers") or []]
        watch_items.append({
            "title": _clean_text(item.get("title"), 220),
            "reason": _clean_text(item.get("reason"), 900),
            "affected_tickers": [x for x in affected if x],
        })
    watch_items = [item for item in watch_items if item["title"] or item["reason"]]

    return {
        "portfolio_view": {
            "stance": stance, "risk_level": risk_level,
            "confidence": confidence, "summary": summary,
        },
        "portfolio_analysis": portfolio_analysis,
        "key_risks": key_risks,
        "holding_analysis": holding_analysis,
        "news_analysis": news_analysis,
        "watch_items": watch_items,
        "data_limitations": _normalise_strings(payload.get("data_limitations"), limit_items=10, limit_text=800),
        "disclaimer": _clean_text(payload.get("disclaimer"), 900) or "本报告仅供研究参考，不构成投资建议。",
    }


def analyst_payload_to_report(
    payload: dict[str, Any],
    *,
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    settings: dict[str, Any],
    sources: list[dict[str, Any]],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    view = payload["portfolio_view"]
    risk_contrib = {
        str(item.get("ticker") or ""): item.get("risk_contribution")
        for item in metrics.get("risk_contributions", []) or []
    }
    actions = []
    for priority, item in enumerate(payload["holding_analysis"], 1):
        ticker = item["ticker"]
        actions.append({
            "ticker": ticker,
            "action": item["action"],
            "priority": priority,
            "confidence": item["confidence"],
            "current_weight": item["current_weight"],
            "target_weight_min": item["target_weight_min"],
            "target_weight_max": item["target_weight_max"],
            "risk_contribution": risk_contrib.get(ticker),
            "action_timing": "monitor" if item["action"] in {"watch", "hold"} else "conditional",
            "portfolio_reason": item["combined_view"],
            "technical_reason": item["technical_view"],
            "news_reason": item["news_view"],
            "bull_case": item["bull_case"],
            "bear_case": item["bear_case"],
            "execute_if": item["execute_if"],
            "cancel_or_upgrade_if": item["cancel_or_upgrade_if"],
            "further_reduce_if": [],
            "monitoring_items": item["monitoring_items"],
            "evidence_ids": [],
            "metric_evidence": [],
        })

    report_style = settings["report_style"]
    style_title = REPORT_STYLE_OPTIONS[report_style]
    summary = view["summary"] or [
        "AI 已结合 Python 量化数据与一次联网搜索生成综合分析。",
        "请结合下方技术快照、风险贡献和消息来源阅读，不将单条消息视为独立交易依据。",
    ]
    return {
        "report_mode": "ai_analyst_v3",
        "ai_analysis_available": True,
        "observation_only": settings["advice_mode"] == "observe_only",
        "portfolio_stance": view["stance"],
        "risk_level": view["risk_level"],
        "confidence": view["confidence"],
        "final_confidence": view["confidence"],
        "executive_summary": summary,
        "portfolio_analysis": payload["portfolio_analysis"],
        "key_risks": payload["key_risks"],
        "actions": actions,
        "watch_items": payload["watch_items"],
        "data_limitations": payload["data_limitations"],
        "disclaimer": payload["disclaimer"],
        "report_style": report_style,
        "report_style_title": style_title,
        "model_name": settings["model"],
        "thinking_enabled": settings["enable_thinking"],
        "reasoning_effort": settings["reasoning_effort"],
        "analyst_diagnostics": diagnostics,
        "portfolio_reallocation": {
            "estimated_weight_reduction": sum(
                max(0.0, float(a["current_weight"]) - float(a["target_weight_max"]))
                for a in actions if a["action"] in {"trim", "reduce", "exit"}
            ),
            "notes": "目标区间由本地规则根据 AI action 与用户仓位上限生成，模型不能改写当前权重。",
        },
    }


def _default_call(*, api_key: str, model: str, messages: list[dict[str, str]], settings: dict[str, Any]) -> Any:
    try:
        import dashscope
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise PortfolioAnalystUnavailable("dashscope package is not installed") from exc
    return dashscope.Generation.call(
        api_key=api_key,
        model=model,
        messages=messages,
        enable_search=bool(settings["include_news"] or settings["include_macro"]),
        search_options={
            "forced_search": bool(settings["include_news"] or settings["include_macro"]),
            "search_strategy": "turbo",
            "enable_source": True,
        },
        enable_thinking=settings["enable_thinking"],
        reasoning_effort=settings["reasoning_effort"],
        result_format="message",
    )


def run_portfolio_ai_analyst(
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    settings: dict[str, Any] | None,
    *,
    instrument_metadata: dict[str, Any] | None = None,
    risk_findings: list[dict[str, Any]] | None = None,
    generation_call: Callable[..., Any] | None = None,
    reference_time: dt.datetime | None = None,
) -> dict[str, Any]:
    """Run exactly one Portfolio Analyst model call and return render-ready data."""
    settings = normalize_analyst_settings(settings)
    context = build_analyst_context(
        snapshot, metrics, ranking, settings,
        instrument_metadata=instrument_metadata, risk_findings=risk_findings,
    )
    messages = build_analyst_messages(context, settings)
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if generation_call is None and not api_key:
        raise PortfolioAnalystUnavailable("DASHSCOPE_API_KEY is not configured")

    started = time.perf_counter()
    caller = generation_call or _default_call
    try:
        response = caller(api_key=api_key, model=settings["model"], messages=messages, settings=settings)
    except PortfolioAnalystUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PortfolioAnalystUnavailable(str(exc)) from exc
    elapsed = time.perf_counter() - started

    content, reasoning_content, raw_sources, meta = _response_parts(response)
    payload = _extract_json_object(content)
    if not payload:
        raise PortfolioAnalystOutputError("model final content is not a JSON object")

    report_date = dt.date.fromisoformat(str(snapshot.get("report_date") or dt.date.today().isoformat())[:10])
    sources = _source_rows(raw_sources, report_date)
    normalised = normalise_analyst_payload(
        payload, snapshot=snapshot, ranking=ranking, settings=settings, sources=sources,
    )
    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
    diagnostics = {
        "status": "success",
        "architecture": "portfolio_ai_analyst_v3",
        "search_strategy": "turbo",
        "search_call_count": 1,
        "model_call_count": 1,
        "external_search_call_count": 0,
        "retry_count": 0,
        "gap_search_count": 0,
        "model": settings["model"],
        "enable_thinking": settings["enable_thinking"],
        "reasoning_effort": settings["reasoning_effort"],
        "report_style": settings["report_style"],
        "source_count": len(sources),
        "news_item_count": len(normalised["news_analysis"]),
        "holding_analysis_count": len(normalised["holding_analysis"]),
        "elapsed_seconds": round(elapsed, 3),
        "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
        "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "reasoning_content_present": bool(reasoning_content.strip()),
        "request_id": meta.get("request_id"),
        "generated_at": (reference_time or dt.datetime.now().astimezone()).isoformat(),
    }
    advice = analyst_payload_to_report(
        normalised, snapshot=snapshot, metrics=metrics, ranking=ranking,
        settings=settings, sources=sources, diagnostics=diagnostics,
    )
    return {
        "status": "success",
        "settings": settings,
        "context": context,
        "messages": messages,
        "raw_model_output": content,
        "reasoning_content": reasoning_content,
        "raw_model_payload": payload,
        "sources": sources,
        "news_analysis": normalised["news_analysis"],
        "advice": advice,
        "diagnostics": diagnostics,
    }
