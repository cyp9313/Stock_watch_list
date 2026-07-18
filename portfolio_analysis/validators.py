# -*- coding: utf-8 -*-
"""Portfolio 建议校验与动作-权重一致性约束。

修改计划 9：
- action 与目标权重区间必须一致；
- 默认 sanitize 行为保持向后兼容（旧测试依赖）；
- 新增 strict 模式：不一致时抛出 ``PortfolioAdviceValidationError``，
  由 Agent runner 捕获后让模型修复一次，再次失败则任务失败（不静默改成相反区间）。
"""
from __future__ import annotations

from typing import Any
import re

ALLOWED_ACTIONS = {"add", "hold", "trim", "reduce", "exit", "watch"}
FORBIDDEN_SHARE_KEYS = {"shares_to_buy", "shares_to_sell", "exact_share_count"}

# exit 时目标权重上限的最大阈值
_EXIT_MAX_THRESHOLD = 0.02
# reduce 相对当前权重需要显著低于的比例
_REDUCE_FACTOR = 0.8
_TRIM_FACTOR = 0.95


class PortfolioAdviceValidationError(ValueError):
    """strict 模式下，建议与动作不一致时抛出。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _eps() -> float:
    return 1e-4


def _adjust_range(
    action: str,
    current: float,
    lo: float,
    hi: float,
    *,
    strict: bool,
) -> tuple[float, float]:
    """返回与 action 一致的目标区间 (lo, hi)。

    strict=True 且不一致时抛出 ``PortfolioAdviceValidationError``；
    strict=False（兜底）时在不翻转 action 的前提下把区间收窄到一致范围。
    """
    action = action.lower()
    eps = _eps()

    def fail(msg: str):
        if strict:
            raise PortfolioAdviceValidationError([msg])
        return None

    if action == "add":
        # target_weight_min >= current；target_weight_max > current
        if lo < current - eps or hi <= current + eps:
            fail(f"[{action}] 目标区间需满足 min>={current:.4f} 且 max>{current:.4f}")
            lo = max(lo, current)
            hi = max(hi, current * 1.05 + eps)
    elif action == "hold":
        if not (lo - eps <= current <= hi + eps):
            fail(f"[{action}] 当前权重 {current:.4f} 应落在目标区间 [{lo:.4f}, {hi:.4f}] 内")
            lo = min(lo, current)
            hi = max(hi, current)
    elif action == "trim":
        if hi >= current - eps:
            fail(f"[{action}] 目标上限需 < 当前权重 {current:.4f}")
            hi = max(0.0, current * _TRIM_FACTOR)
            lo = min(lo, hi)
    elif action == "reduce":
        if hi >= current - eps or hi > current * _REDUCE_FACTOR + eps:
            fail(f"[{action}] 目标上限需显著 < 当前权重 {current:.4f}")
            hi = max(0.0, current * _REDUCE_FACTOR)
            lo = min(lo, hi)
    elif action == "exit":
        if lo > eps or hi > _EXIT_MAX_THRESHOLD + eps:
            fail(f"[{action}] 目标区间应接近 0（max<={_EXIT_MAX_THRESHOLD}）")
            lo = 0.0
            hi = min(hi, _EXIT_MAX_THRESHOLD)
    elif action == "watch":
        # 允许 null 或包含当前权重；兜底时不强制改动
        pass
    return lo, hi


def validate_portfolio_advice(
    advice: dict[str, Any],
    snapshot: dict[str, Any],
    evidence: list[dict[str, Any]] | None = None,
    *,
    mode: str = "fallback",
) -> dict[str, Any]:
    """校验并归一化一份 Portfolio 建议。

    mode="fallback"（默认）：把越界/非法项静默修正到安全范围（不翻转 action），
        并记录 warnings。保持与旧测试兼容。
    mode="strict"：发现动作-权重不一致时抛出 ``PortfolioAdviceValidationError``，
        不做静默修正，交由 Agent 修复。
    """
    strict = mode == "strict"
    evidence_ids = {str(item.get("evidence_id")) for item in evidence or [] if item.get("evidence_id")}
    evidence_by_id = {str(e.get("evidence_id")): e for e in (evidence or []) if e.get("evidence_id")}
    weights = {h["ticker"]: float(h.get("weight", 0.0) or 0.0) for h in snapshot.get("holdings", [])}
    sanitized = dict(advice or {})
    warnings: list[str] = list(sanitized.get("validation_warnings") or [])
    actions = []
    for raw in sanitized.get("actions") or []:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker") or "").upper()
        if ticker not in weights:
            warnings.append(f"Dropped action for non-portfolio ticker: {ticker}")
            continue
        action = str(raw.get("action") or "watch").lower()
        if action not in ALLOWED_ACTIONS:
            warnings.append(f"Invalid action for {ticker}: {action}; changed to watch.")
            action = "watch"
        item = dict(raw)
        item["ticker"] = ticker
        item["action"] = action
        item["current_weight"] = weights[ticker]
        for key in FORBIDDEN_SHARE_KEYS:
            if key in item:
                item.pop(key, None)
                warnings.append(f"Removed unconstrained exact-share field {key} for {ticker}.")

        try:
            lo = float(item.get("target_weight_min", weights[ticker]))
            hi = float(item.get("target_weight_max", weights[ticker]))
        except (TypeError, ValueError):
            lo = hi = weights[ticker]
        lo = max(0.0, min(1.0, lo))
        hi = max(lo, min(1.0, hi))

        try:
            lo, hi = _adjust_range(action, weights[ticker], lo, hi, strict=strict)
        except PortfolioAdviceValidationError as exc:
            raise
        lo = max(0.0, min(1.0, lo))
        hi = max(lo, min(1.0, hi))
        item["target_weight_min"] = lo
        item["target_weight_max"] = hi

        try:
            item["confidence"] = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            item["confidence"] = 0.5

        # 证据跨 ticker 绑定（修改计划第三轮 15）
        valid_eids = []
        for eid in item.get("evidence_ids") or []:
            eid = str(eid)
            ev = evidence_by_id.get(eid)
            if ev is None:
                if evidence_ids:
                    warnings.append(f"Dropped unknown evidence id {eid} for {ticker}.")
                continue
            ev_ticker = ev.get("ticker")
            related = ev.get("related_tickers") or []
            scope_ok = (str(ev_ticker or "").upper() == ticker) or (ticker in [str(t) for t in related])
            if scope_ok:
                valid_eids.append(eid)
            elif strict:
                raise PortfolioAdviceValidationError([
                    f"Action {ticker} 引用了不属于它的证据 {eid}（该证据属于 {ev_ticker or 'macro/theme'}）。"
                ])
            else:
                warnings.append(f"Dropped cross-ticker evidence {eid} for {ticker}.")
        item["evidence_ids"] = valid_eids
        actions.append(item)

    # Key Risk 证据绑定检查（修改计划第三轮 15）
    key_risks = sanitized.get("key_risks") or []
    for kr in key_risks:
        if not isinstance(kr, dict):
            continue
        kr_eids = kr.get("evidence_ids") or []
        scoped = []
        for eid in kr_eids:
            eid = str(eid)
            ev = evidence_by_id.get(eid)
            if ev is None:
                if evidence_ids:
                    warnings.append(f"Dropped unknown evidence id {eid} in key_risk {kr.get('risk_id')}.")
                continue
            affected = kr.get("affected_tickers") or []
            ev_ticker = ev.get("ticker")
            related = ev.get("related_tickers") or []
            if (ev_ticker and ev_ticker in affected) or (set(affected) & set(related)):
                scoped.append(eid)
            elif strict:
                raise PortfolioAdviceValidationError([
                    f"Key risk {kr.get('risk_id')} 引用了不匹配的证据 {eid}。"
                ])
            else:
                warnings.append(f"Dropped mismatched evidence {eid} in key_risk {kr.get('risk_id')}.")
        kr["evidence_ids"] = scoped

    sanitized["actions"] = actions
    try:
        sanitized["confidence"] = max(0.0, min(1.0, float(sanitized.get("confidence", 0.5))))
    except (TypeError, ValueError):
        sanitized["confidence"] = 0.5
    sanitized["validation_warnings"] = warnings
    sanitized.setdefault("disclaimer", "本报告仅供研究参考，不构成投资建议。")
    return sanitized


# 修改计划第三轮 42：Claim Validation
# 未被系统提供/未注册、且模型自行引入的「伪指标」关键词（如夏普比率、隐含波动率、
# 机构减持、资金流出、流动性折价等）。出现即视为需要证据支撑的软性问题。
_BANNED_METRIC_HINTS = [
    "夏普比率", "夏普", "隐含波动率", "机构减持", "机构持续减持",
    "资金流出", "流动性折价", "流动性枯竭", "隐性流动性风险",
    "相关性套利", "套利机会", "volatility smile", "implied volatility",
]
# 高置信度操作必须引用的「新鲜」证据层级。
_FRESH_TIERS = {"fresh_event", "recent_background"}


def _claim_text_fields(advice: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for s in advice.get("executive_summary") or []:
        fields.append(str(s))
    pa = advice.get("portfolio_analysis") or {}
    for v in pa.values():
        if isinstance(v, str):
            fields.append(v)
    for kr in advice.get("key_risks") or []:
        if isinstance(kr, dict):
            fields.append(str(kr.get("description") or ""))
            fields.append(str(kr.get("title") or ""))
    for a in advice.get("actions") or []:
        if isinstance(a, dict):
            for k in ("portfolio_reason", "technical_reason", "news_reason", "bull_case", "bear_case"):
                fields.append(str(a.get(k) or ""))
    for w in advice.get("watch_items") or []:
        if isinstance(w, dict):
            fields.append(str(w.get("reason") or ""))
    return fields


def validate_portfolio_claims(
    advice: dict[str, Any],
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    evidence: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """修改计划第三轮 42：校验 AI 结论是否可追溯到确定性数据。

    返回 (hard_errors, soft_warnings)。hard_errors 表示必须让 Agent 修复的硬伤
    （NaN/Inf、缺失基准却做基准比较、阈值缺 basis）；soft_warnings 为需要证据支撑
    的软性问题（伪指标、RSI/EMA 描述不一致、高置信度缺新鲜证据）。
    """
    errors: list[str] = []
    warnings: list[str] = []
    texts = _claim_text_fields(advice)

    # 1. 正文不得出现 NaN / Inf
    for t in texts:
        low = (t or "").lower()
        if "nan" in low or "inf" in low or "非数" in (t or "") or "无穷" in (t or ""):
            errors.append("正文包含 NaN/Inf 等非有限值，必须移除。")
            break

    # 2. 缺失 benchmark 时不得给出基准数字比较
    rr = metrics.get("relative_returns", {}) or {}
    has_actual = any(isinstance(v, dict) and v.get("status") == "actual" for v in rr.values())
    if not has_actual:
        for t in texts:
            if ("基准" in (t or "") or "大盘" in (t or "")) and any(ch.isdigit() for ch in (t or "")):
                errors.append("缺少可用的基准收益（status != actual），不得给出基准数字比较或「跑赢/跑输大盘」结论。")
                break

    # 3. 未注册指标或无数据支撑的因果表述
    for t in texts:
        for hint in _BANNED_METRIC_HINTS:
            if hint.lower() in (t or "").lower():
                message = f"出现系统未提供或无证据支撑的表述「{hint}」，必须删除。"
                warnings.append(message)
                errors.append(message)
                break

    # 4. 高置信度操作必须有新鲜且正文已验证的证据支撑
    ev_by_id = {str(e.get("evidence_id")): e for e in (evidence or []) if e.get("evidence_id")}
    for a in advice.get("actions") or []:
        if not isinstance(a, dict):
            continue
        try:
            conf = float(a.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        if conf >= 0.7:
            eids = a.get("evidence_ids") or []
            fresh_verified = [
                eid for eid in eids
                if str((ev_by_id.get(str(eid)) or {}).get("recency_tier")) in _FRESH_TIERS
                and bool((ev_by_id.get(str(eid)) or {}).get("article_fetch_ok"))
            ]
            if not fresh_verified:
                message = f"{a.get('ticker')} 高置信度操作（{conf}）没有新鲜且正文已验证的证据支撑。"
                warnings.append(message)
                errors.append(message)

    # 5. RSI / EMA 描述一致性（基于确定性 rsi_regime 与 price_vs_ema*）
    holdings = {h["ticker"]: h for h in snapshot.get("holdings", [])}
    all_text = "\n".join(texts)
    for tk, h in holdings.items():
        regime = str(h.get("rsi_regime") or "")
        ticker_texts = [t for t in texts if tk and tk in t]
        for t in ticker_texts:
            bad = False
            if ("深度超卖" in t or "超卖" in t or "oversold" in t.lower()) and regime not in ("oversold",):
                bad = True
            if ("超买" in t or "overbought" in t.lower()) and regime != "overbought":
                bad = True
            if bad:
                message = f"{tk} 的 RSI 文案与确定性区间 {regime} 不一致。"
                warnings.append(message)
                errors.append(message)
            if re.search(r"strong[^。；\n]{0,20}>\s*70|>\s*70[^。；\n]{0,20}strong", t, re.I):
                message = f"{tk} 错把 RSI>70 描述为 strong；该区间应为超买。"
                warnings.append(message)
                errors.append(message)
            if "ema20" in t.lower() and "ema200" in t.lower() and ("跌破" in t or "交叉" in t or "cross" in t.lower()):
                warnings.append(f"{tk} 出现「EMA20 与 EMA200 交叉」描述，但 price_vs_ema* 仅表示价格相对 EMA 的偏离。")

    for action in advice.get("actions") or []:
        if not isinstance(action, dict):
            continue
        ticker = str(action.get("ticker") or "")
        for text in (str(action.get("technical_reason") or ""), str(action.get("news_reason") or "")):
            if "ema20" in text.lower() and "ema200" in text.lower() and ("跌破" in text or "交叉" in text or "cross" in text.lower()):
                warnings.append(f"{ticker} 出现「EMA20 与 EMA200 交叉」描述，但 price_vs_ema* 仅表示价格相对 EMA 的偏离。")

    if "相关性套利" in all_text or "套利机会" in all_text:
        errors.append("仅凭相关性不得描述为套利机会。")
    internal_tokens = [
        "portfolio_risk_score", "evidence_count", "cash_unspecified", "execute_if",
        "cancel_or_upgrade_if", "further_reduce_if", "monitoring_items",
        "uranium_price", "us_10y_yield", "btc_price",
    ]
    for token in internal_tokens:
        if token in all_text:
            errors.append(f"中文正文暴露内部字段名 {token}。")
    if re.search(r"\b(weak|neutral|oversold|overbought)\b", all_text, re.I):
        errors.append("中文正文暴露 RSI 内部英文枚举。")

    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))
