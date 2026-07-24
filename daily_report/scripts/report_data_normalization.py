"""Small, dependency-free normalizers shared by daily-report scripts."""

from __future__ import annotations

from typing import Any


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def normalize_dividend_yield_pct(
    raw_yield: Any,
    annual_dividend: Any,
    last_close: Any,
    *,
    maximum_pct: float = 20.0,
) -> tuple[float, str]:
    """Return a safe annual dividend yield in percentage points.

    Providers normally expose ``dividendYield`` as a decimal ratio, but some
    endpoints return an already-percent value.  When annual cash dividend and
    close are available, that derivation is the auditable source of truth and
    prevents a second ``* 100`` (such as 0.41 becoming 41% for QQQ).
    """
    dividend = _positive_float(annual_dividend)
    close = _positive_float(last_close)
    if dividend is not None and close is not None:
        derived = dividend / close * 100.0
        if derived <= maximum_pct:
            return derived, "annual_dividend_rate/current_close"

    raw = _positive_float(raw_yield)
    if raw is None:
        return 0.0, "unavailable"

    # Apply the sanity limit after conversion; the old code omitted this.
    candidate = raw * 100.0 if raw <= 1.0 else raw
    if candidate <= maximum_pct:
        return candidate, "provider_dividend_yield"
    return 0.0, "rejected_outlier"
