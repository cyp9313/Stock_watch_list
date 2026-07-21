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
