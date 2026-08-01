"""Small, dependency-free market session inference for report presentation."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo


MARKET_RULES: dict[str, dict[str, Any]] = {
    "US": {"suffixes": (), "timezone": "America/New_York", "open": "09:30", "close": "16:00"},
    "DE": {"suffixes": (".DE",), "timezone": "Europe/Berlin", "open": "09:00", "close": "17:30"},
    "HK": {"suffixes": (".HK",), "timezone": "Asia/Hong_Kong", "open": "09:30", "lunch_start": "12:00", "lunch_end": "13:00", "close": "16:00"},
    "CN": {"suffixes": (".SS", ".SZ"), "timezone": "Asia/Shanghai", "open": "09:30", "lunch_start": "11:30", "lunch_end": "13:00", "close": "15:00"},
    "JP": {"suffixes": (".T",), "timezone": "Asia/Tokyo", "open": "09:00", "lunch_start": "11:30", "lunch_end": "12:30", "close": "15:30"},
    "KR": {"suffixes": (".KS", ".KQ"), "timezone": "Asia/Seoul", "open": "09:00", "close": "15:30"},
    "TW": {"suffixes": (".TW", ".TWO"), "timezone": "Asia/Taipei", "open": "09:00", "close": "13:30"},
}


def _clock(value: str) -> time:
    hour, minute = (int(piece) for piece in value.split(":", 1))
    return time(hour, minute)


def infer_market(ticker: str, instrument_type: str | None = None) -> tuple[str, list[str]]:
    """Return a market code and safe display limitations.

    P0 intentionally uses weekday-only session approximation.  A ticker with
    no known exchange suffix is treated as US rather than failing a report.
    """
    normalized = str(ticker or "").strip().upper()
    kind = str(instrument_type or "").upper()
    if kind == "CRYPTO" or normalized.endswith("-USD") and kind == "CRYPTO":
        return "CRYPTO", []
    for market, rule in MARKET_RULES.items():
        if any(normalized.endswith(suffix) for suffix in rule["suffixes"]):
            return market, []
    if normalized.endswith("-USD"):
        return "CRYPTO", ["Ticker uses a crypto-like -USD suffix; instrument type was unavailable."]
    return "US", ["Market inferred as US because the ticker has no recognized exchange suffix."]


def infer_market_session(
    ticker: str,
    *,
    instrument_type: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a serializable market-session payload without external calendar calls."""
    market, limitations = infer_market(ticker, instrument_type)
    if market == "CRYPTO":
        local_now = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC"))
        return {
            "market": "CRYPTO",
            "timezone": "UTC",
            "local_datetime": local_now.isoformat(),
            "local_date": local_now.date().isoformat(),
            "phase": "continuous",
            "is_trading_day": True,
            "is_partial_bar": False,
            "calendar_accuracy": "continuous_market",
            "data_limitations": limitations,
        }

    rule = MARKET_RULES[market]
    zone = ZoneInfo(rule["timezone"])
    local_now = (now or datetime.now(zone)).astimezone(zone)
    is_trading_day = local_now.weekday() < 5
    phase = "non_trading"
    if is_trading_day:
        current = local_now.timetz().replace(tzinfo=None)
        opening = _clock(rule["open"])
        closing = _clock(rule["close"])
        lunch_start = _clock(rule["lunch_start"]) if rule.get("lunch_start") else None
        lunch_end = _clock(rule["lunch_end"]) if rule.get("lunch_end") else None
        if current < opening:
            phase = "premarket"
        elif lunch_start is not None and lunch_start <= current < lunch_end:
            phase = "lunch_break"
        elif current < closing:
            phase = "open"
        else:
            phase = "postmarket"
    return {
        "market": market,
        "timezone": rule["timezone"],
        "local_datetime": local_now.isoformat(),
        "local_date": local_now.date().isoformat(),
        "phase": phase,
        "is_trading_day": is_trading_day,
        "is_partial_bar": phase in {"open", "lunch_break"},
        "calendar_accuracy": "weekday_only",
        "data_limitations": limitations + ["Trading-day and session inference uses weekday-only approximation in P0."],
    }
