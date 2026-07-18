from __future__ import annotations

from typing import Any


ALLOWED_ACTIONS = {"add", "hold", "trim", "reduce", "exit", "watch"}
FORBIDDEN_SHARE_KEYS = {"shares_to_buy", "shares_to_sell", "exact_share_count"}


def validate_portfolio_advice(advice: dict[str, Any], snapshot: dict[str, Any], evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Validate and sanitize a portfolio advice payload.

    This intentionally overwrites current weights from deterministic Python
    data and drops unconstrained exact-share recommendations.
    """
    evidence_ids = {str(item.get("evidence_id")) for item in evidence or [] if item.get("evidence_id")}
    weights = {h["ticker"]: h.get("weight", 0.0) for h in snapshot.get("holdings", [])}
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
            lo = float(item.get("target_weight_min", item["current_weight"]))
            hi = float(item.get("target_weight_max", item["current_weight"]))
        except (TypeError, ValueError):
            lo = hi = float(item["current_weight"] or 0.0)
        lo = max(0.0, min(1.0, lo))
        hi = max(lo, min(1.0, hi))
        item["target_weight_min"] = lo
        item["target_weight_max"] = hi
        try:
            item["confidence"] = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            item["confidence"] = 0.5
        valid_eids = []
        for eid in item.get("evidence_ids") or []:
            eid = str(eid)
            if eid in evidence_ids:
                valid_eids.append(eid)
            elif evidence_ids:
                warnings.append(f"Dropped unknown evidence id {eid} for {ticker}.")
        item["evidence_ids"] = valid_eids
        actions.append(item)
    sanitized["actions"] = actions
    try:
        sanitized["confidence"] = max(0.0, min(1.0, float(sanitized.get("confidence", 0.5))))
    except (TypeError, ValueError):
        sanitized["confidence"] = 0.5
    sanitized["validation_warnings"] = warnings
    sanitized.setdefault("disclaimer", "This report is for research purposes only and is not investment advice.")
    return sanitized
