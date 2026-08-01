"""Deterministic reference levels used by the decision dashboard."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PriceLevelCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float = Field(gt=0)
    kind: Literal["support", "resistance"]
    source: str
    distance_pct: float
    strength: float = Field(ge=0)
    sources: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class LevelPlan:
    supports: list[PriceLevelCandidate]
    resistances: list[PriceLevelCandidate]
    ideal_buy: float | None
    secondary_buy: float | None
    stop_loss: float | None
    take_profit: float | None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _candidate(value: Any, kind: Literal["support", "resistance"], source: str, price: float, strength: float) -> PriceLevelCandidate | None:
    number = _number(value)
    if number is None:
        return None
    if kind == "support" and number > price:
        return None
    if kind == "resistance" and number < price:
        return None
    return PriceLevelCandidate(
        value=number,
        kind=kind,
        source=source,
        sources=[source],
        distance_pct=abs(number - price) / price * 100,
        strength=strength,
    )


def _merge_candidates(candidates: list[PriceLevelCandidate], price: float) -> list[PriceLevelCandidate]:
    """Merge levels within 0.35% of current price, preserving provenance."""
    merged: list[PriceLevelCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.value):
        if merged and abs(candidate.value - merged[-1].value) / price <= 0.0035:
            previous = merged[-1]
            sources = list(dict.fromkeys(previous.sources + candidate.sources))
            weighted_value = (previous.value * previous.strength + candidate.value * candidate.strength) / (previous.strength + candidate.strength)
            merged[-1] = PriceLevelCandidate(
                value=weighted_value,
                kind=previous.kind,
                source=" + ".join(sources),
                sources=sources,
                distance_pct=abs(weighted_value - price) / price * 100,
                strength=previous.strength + candidate.strength,
            )
        else:
            merged.append(candidate)
    return sorted(merged, key=lambda item: (item.distance_pct, -item.strength))


def calculate_level_plan(data: dict[str, Any]) -> LevelPlan:
    """Calculate technical *reference* levels without fetching any new data."""
    price = _number(data.get("LAST_CLOSE"))
    if price is None:
        return LevelPlan([], [], None, None, None, None)
    chip = data.get("chip_profile_primary") if isinstance(data.get("chip_profile_primary"), dict) else {}
    supports_raw = [
        (data.get("ma20"), "MA20", 2.0), (data.get("ma50"), "MA50", 3.0),
        (data.get("ma200"), "MA200", 4.0), (data.get("bb_dn"), "Bollinger lower", 2.5),
        (data.get("FIFTY2W_LO"), "52-week low", 1.0), (data.get("RECENT_LOW_20D"), "20-day low", 2.5),
        (data.get("RECENT_LOW_63D"), "63-day low", 3.0), (chip.get("poc_price"), "Volume profile POC", 3.5),
        (chip.get("value_area_low"), "Volume profile value-area low", 3.5),
    ]
    resistances_raw = [
        (data.get("ma20"), "MA20", 2.0), (data.get("ma50"), "MA50", 3.0),
        (data.get("ma200"), "MA200", 4.0), (data.get("bb_up"), "Bollinger upper", 2.5),
        (data.get("FIFTY2W_HI"), "52-week high", 1.0), (data.get("RECENT_HIGH_20D"), "20-day high", 2.5),
        (data.get("RECENT_HIGH_63D"), "63-day high", 3.0), (chip.get("value_area_high"), "Volume profile value-area high", 3.5),
    ]
    supports = _merge_candidates([item for raw in supports_raw if (item := _candidate(raw[0], "support", raw[1], price, raw[2]))], price)
    resistances = _merge_candidates([item for raw in resistances_raw if (item := _candidate(raw[0], "resistance", raw[1], price, raw[2]))], price)
    usable_supports = [item for item in supports if item.distance_pct <= 15]
    ideal = usable_supports[0].value if usable_supports else None
    secondary = usable_supports[1].value if len(usable_supports) > 1 else None
    atr = _number(data.get("atr14")) or price * 0.015
    if ideal is not None and secondary is None:
        derived = ideal - max(atr, price * 0.015)
        secondary = derived if derived > 0 else None
    stop = ideal - max(atr * 0.75, price * 0.01) if ideal is not None else None
    if stop is not None and stop <= 0:
        stop = None
    take_profit = None
    if ideal is not None and stop is not None:
        minimum_target = price + 1.5 * (ideal - stop)
        for candidate in resistances:
            if candidate.distance_pct <= 30 and candidate.value >= minimum_target:
                take_profit = candidate.value
                break
    return LevelPlan(supports, resistances, ideal, secondary, stop, take_profit)


def format_price(value: float | None, currency: str, atr: float | None = None) -> str | None:
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    decimals = 2 if value >= 100 else (4 if value >= 1 else 6)
    width = max((atr or value * 0.01) * 0.25, value * 0.0025)
    lower = max(0.0, value - width)
    symbol = {"USD": "$", "EUR": "€", "HKD": "HK$", "CNY": "￥", "JPY": "¥"}.get(str(currency or "USD").upper(), f"{currency} ")
    return f"{symbol}{lower:.{decimals}f}–{symbol}{(value + width):.{decimals}f}"


def level_candidates_payload(plan: LevelPlan, data: dict[str, Any]) -> dict[str, Any]:
    currency = str(data.get("CURRENCY") or "USD")
    atr = _number(data.get("atr14"))
    return {
        "supports": [item.model_dump() for item in plan.supports],
        "resistances": [item.model_dump() for item in plan.resistances],
        "ideal_buy_candidate": plan.ideal_buy,
        "secondary_buy_candidate": plan.secondary_buy,
        "stop_loss_candidate": plan.stop_loss,
        "take_profit_candidate": plan.take_profit,
        "display": {
            "ideal_buy": format_price(plan.ideal_buy, currency, atr),
            "secondary_buy": format_price(plan.secondary_buy, currency, atr),
            "stop_loss": format_price(plan.stop_loss, currency, atr),
            "take_profit": format_price(plan.take_profit, currency, atr),
        },
    }
