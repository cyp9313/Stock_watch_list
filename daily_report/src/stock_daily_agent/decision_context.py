"""Build the compact, redacted input passed to controlled decision synthesis."""

from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
from typing import Any

from .config import RunContext
from .decision_levels import calculate_level_plan, level_candidates_payload
from .market_session import infer_market_session


_HOLDING_FIELDS = {"has_position", "buy_price", "shares", "portfolio_weight", "unrealized_pnl_pct"}
_TECHNICAL_IDS = {"TECH-001", "TECH-002", "TECH-003"}


def _finite(value: Any, *, positive: bool = False) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def load_final_notes(ctx: RunContext) -> list[dict[str, Any]]:
    """Read validated note items, with a deliberately limited legacy fallback."""
    if ctx.final_notes_json_file.is_file():
        try:
            payload = json.loads(ctx.final_notes_json_file.read_text(encoding="utf-8"))
            items = payload.get("items", []) if isinstance(payload, dict) else []
            return [item for item in items if isinstance(item, dict)]
        except (OSError, ValueError, TypeError):
            return []
    if not ctx.notes_file.is_file():
        return []
    items: list[dict[str, Any]] = []
    try:
        for index, line in enumerate(ctx.notes_file.read_text(encoding="utf-8").splitlines(), start=1):
            text = line.strip()
            if not text:
                continue
            tag = "MIX"
            for known_tag in ("BULL", "BEAR", "MIX"):
                prefix = f"[{known_tag}]"
                if text.upper().startswith(prefix):
                    tag, text = known_tag, text[len(prefix):].strip()
                    break
            items.append({"tag": tag, "title": text[:160], "fact": text[:700], "evidence_id": f"LEGACY-{index:03d}"})
    except OSError:
        return []
    return items


def collect_allowed_evidence_ids(final_notes: list[dict[str, Any]], technical_ids: set[str] | None = None) -> set[str]:
    ids = set(technical_ids or _TECHNICAL_IDS)
    for item in final_notes:
        evidence_id = str(item.get("evidence_id") or "").strip().upper()
        if evidence_id:
            ids.add(evidence_id)
    return ids


def normalize_holding_context(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Whitelist and validate the only portfolio data a report may consume."""
    if payload is None:
        return {"has_position": False, "buy_price": None, "shares": None, "portfolio_weight": None, "unrealized_pnl_pct": None}
    if not isinstance(payload, dict) or set(payload) - _HOLDING_FIELDS:
        raise ValueError("holding_context contains unsupported fields")
    has_position = payload.get("has_position", False)
    if not isinstance(has_position, bool):
        raise ValueError("holding_context.has_position must be boolean")
    result = {"has_position": has_position}
    for key in ("buy_price", "shares"):
        value = payload.get(key)
        if value is not None and _finite(value, positive=True) is None:
            raise ValueError(f"holding_context.{key} must be a positive finite number")
        result[key] = _finite(value, positive=True) if value is not None else None
    weight = payload.get("portfolio_weight")
    if weight is not None and (_finite(weight) is None or not 0 <= float(weight) <= 1):
        raise ValueError("holding_context.portfolio_weight must be between 0 and 1")
    result["portfolio_weight"] = _finite(weight) if weight is not None else None
    pnl = payload.get("unrealized_pnl_pct")
    if pnl is not None and _finite(pnl) is None:
        raise ValueError("holding_context.unrealized_pnl_pct must be finite")
    result["unrealized_pnl_pct"] = _finite(pnl) if pnl is not None else None
    return result


def _short(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _compact_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in items[:14]:
        evidence_id = _short(item.get("evidence_id"), 64)
        if not evidence_id:
            continue
        compact.append({
            "evidence_id": evidence_id,
            "tag": _short(item.get("tag"), 16).upper() or "MIX",
            "title": _short(item.get("title"), 160),
            "fact": _short(item.get("fact"), 700),
            "logic": _short(item.get("logic"), 700),
            "investment_meaning": _short(item.get("investment_meaning"), 500),
            "source": _short(item.get("source"), 160),
            "source_date": _short(item.get("source_date"), 32),
            "source_domain": _short(item.get("source_domain"), 160),
        })
    return compact


def build_decision_context(
    ctx: RunContext,
    *,
    holding_context: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create a redacted context from already-produced report artifacts only."""
    data = json.loads(Path(ctx.data_file).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("data_file must contain a JSON object")
    notes = load_final_notes(ctx)
    rating = data.get("final_rating") if isinstance(data.get("final_rating"), dict) else {}
    session = infer_market_session(ctx.ticker, instrument_type=data.get("INSTRUMENT_TYPE"), now=now)
    plan = calculate_level_plan(data)
    compact_items = _compact_evidence(notes)
    allowed_ids = collect_allowed_evidence_ids(compact_items)
    limitations = list(session.get("data_limitations") or [])
    if not compact_items:
        limitations.append("No validated final notes were available for decision synthesis.")
    if not rating:
        limitations.append("Authoritative final rating was unavailable; technical-only fallback may be used.")
    quality = "good" if rating and len(compact_items) >= 3 else ("limited" if rating else "poor")
    final_score = _finite(rating.get("final_score"))
    if final_score is None:
        final_score = _finite(data.get("technical_score")) or 50.0
    return {
        "schema_version": "1.0",
        "instrument": {
            "ticker": str(data.get("TICKER") or ctx.ticker),
            "name": _short(data.get("LONG_NAME") or data.get("SHORT_NAME"), 240),
            "instrument_type": _short(data.get("INSTRUMENT_TYPE"), 32) or "OTHER",
            "currency": _short(data.get("CURRENCY"), 12) or "USD",
            "current_price": _finite(data.get("LAST_CLOSE")),
            "data_end": _short(data.get("data_end"), 32),
        },
        "market_session": session,
        "authoritative_rating": {
            "final_score": final_score,
            "rating_text": _short(rating.get("rating_text"), 240),
            "rating_class": _short(rating.get("rating_class"), 32) or "hold",
            "method": _short(rating.get("method"), 160),
            "subscores": rating.get("subscores") if isinstance(rating.get("subscores"), dict) else {},
            "effective_weights": rating.get("effective_weights") if isinstance(rating.get("effective_weights"), dict) else {},
        },
        "technical": {
            "technical_score": _finite(data.get("technical_score")),
            "technical_signal": _short(data.get("technical_signal"), 160),
            "current_price": _finite(data.get("LAST_CLOSE")),
            "ma20": _finite(data.get("ma20")), "ma50": _finite(data.get("ma50")), "ma200": _finite(data.get("ma200")),
            "bb_upper": _finite(data.get("bb_up")), "bb_lower": _finite(data.get("bb_dn")), "atr14": _finite(data.get("atr14")),
            "rsi": _finite(data.get("rsi")), "macd_line": _finite(data.get("macd_line")), "signal_line": _finite(data.get("signal_line")),
            "volume_ratio": _finite(data.get("vol_ratio")), "realized_vol_20d_pct": _finite(data.get("REALIZED_VOL_20D_PCT")),
            "max_drawdown_63d_pct": _finite(data.get("MAX_DRAWDOWN_63D_PCT")),
            "chip_profile": data.get("chip_profile_primary") if isinstance(data.get("chip_profile_primary"), dict) else {},
        },
        "level_candidates": level_candidates_payload(plan, data),
        "holding_context": normalize_holding_context(holding_context),
        "evidence_items": compact_items,
        "allowed_evidence_ids": sorted(allowed_ids),
        "data_quality": {"level": quality, "limitations": limitations},
    }
