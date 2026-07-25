"""Deterministic TradingView-style Auto Fibonacci helpers for Streamlit K-lines.

The module intentionally works only with the OHLCV payload already displayed in
the chart.  It neither fetches market data nor stores state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd


ATR_PERIOD = 10
RETRACEMENT_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
EXTENSION_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618, 2.0, 2.618)


def _ratio_label(ratio: float) -> str:
    percent = ratio * 100
    return f"{percent:.1f}%" if not float(percent).is_integer() else f"{int(percent)}%"


def _prepare_frame(dates: Sequence[Any], ohlc: Mapping[str, Sequence[Any]]) -> pd.DataFrame:
    if not isinstance(ohlc, Mapping):
        raise ValueError("OHLC data must be an object")
    length = len(dates)
    if not length:
        raise ValueError("K-line data is empty")
    fields: dict[str, Any] = {"date": list(dates)}
    for key in ("high", "low", "close"):
        values = ohlc.get(key)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or len(values) != length:
            raise ValueError(f"OHLC field {key} must match dates length")
        fields[key] = pd.to_numeric(pd.Series(values, dtype="object"), errors="coerce").replace([np.inf, -np.inf], np.nan)
    frame = pd.DataFrame(fields)
    if frame[["high", "low", "close"]].notna().sum().sum() == 0:
        raise ValueError("OHLC data contains no finite prices")
    return frame


def detect_pivot_candidates(highs: Sequence[Any], lows: Sequence[Any], depth: int) -> list[dict[str, Any]]:
    """Return confirmed, center-window pivot candidates in deterministic order.

    Equal extrema use the last matching bar in the window, preventing a flat
    high/low plateau from generating several pivots.
    """
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 2:
        raise ValueError("Depth must be an integer of at least 2")
    if len(highs) != len(lows):
        raise ValueError("High and low lengths must match")
    high = pd.to_numeric(pd.Series(highs, dtype="object"), errors="coerce").replace([np.inf, -np.inf], np.nan)
    low = pd.to_numeric(pd.Series(lows, dtype="object"), errors="coerce").replace([np.inf, -np.inf], np.nan)
    radius = max(1, depth // 2)
    candidates: list[dict[str, Any]] = []
    for index in range(radius, len(high) - radius):
        window_high = high.iloc[index - radius:index + radius + 1]
        window_low = low.iloc[index - radius:index + radius + 1]
        high_value = high.iloc[index]
        low_value = low.iloc[index]
        if pd.notna(high_value) and window_high.notna().any() and high_value == window_high.max():
            equal_positions = np.flatnonzero(window_high.to_numpy() == high_value)
            if len(equal_positions) and equal_positions[-1] == radius:
                candidates.append({"index": index, "price": float(high_value), "pivot_type": "high", "confirmed": True})
        if pd.notna(low_value) and window_low.notna().any() and low_value == window_low.min():
            equal_positions = np.flatnonzero(window_low.to_numpy() == low_value)
            if len(equal_positions) and equal_positions[-1] == radius:
                candidates.append({"index": index, "price": float(low_value), "pivot_type": "low", "confirmed": True})
    return candidates


def _closest_prior_close(frame: pd.DataFrame, index: int) -> float | None:
    prior = frame.loc[:index - 1, "close"].dropna() if index else pd.Series(dtype="float64")
    return float(prior.iloc[-1]) if not prior.empty else None


def _choose_same_bar_candidate(candidates: list[dict[str, Any]], accepted: list[dict[str, Any]], frame: pd.DataFrame) -> dict[str, Any]:
    if len(candidates) == 1:
        return candidates[0]
    previous_type = accepted[-1]["pivot_type"] if accepted else None
    if previous_type == "low":
        return next(candidate for candidate in candidates if candidate["pivot_type"] == "high")
    if previous_type == "high":
        return next(candidate for candidate in candidates if candidate["pivot_type"] == "low")
    close = _closest_prior_close(frame, candidates[0]["index"])
    if close is not None:
        ordered = sorted(candidates, key=lambda candidate: (abs(candidate["price"] - close), candidate["pivot_type"] == "high"), reverse=True)
        return ordered[0]
    return next(candidate for candidate in candidates if candidate["pivot_type"] == "high")


def _append_or_replace(accepted: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    if not accepted:
        accepted.append(candidate)
        return
    previous = accepted[-1]
    if candidate["pivot_type"] != previous["pivot_type"]:
        accepted.append(candidate)
    elif (candidate["pivot_type"] == "high" and candidate["price"] >= previous["price"]) or (
        candidate["pivot_type"] == "low" and candidate["price"] <= previous["price"]
    ):
        accepted[-1] = candidate


def _with_dates(frame: pd.DataFrame, pivots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for pivot in pivots:
        item = dict(pivot)
        item["date"] = frame.iloc[item["index"]]["date"]
        result.append(item)
    return result


def _atr(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - previous_close).abs(),
        (frame["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()


def build_retracement_zigzag(frame: pd.DataFrame, deviation: float, depth: int) -> list[dict[str, Any]]:
    """Build alternating confirmed pivots using ATR-scaled reversal filtering."""
    if not 0.1 <= float(deviation) <= 20.0:
        raise ValueError("Deviation must be between 0.1 and 20.0")
    atr = _atr(frame)
    grouped: dict[int, list[dict[str, Any]]] = {}
    for candidate in detect_pivot_candidates(frame["high"].tolist(), frame["low"].tolist(), depth):
        grouped.setdefault(candidate["index"], []).append(candidate)
    accepted: list[dict[str, Any]] = []
    for index in sorted(grouped):
        candidate = _choose_same_bar_candidate(grouped[index], accepted, frame)
        candidate["threshold_pct"] = None
        if not accepted:
            _append_or_replace(accepted, candidate)
            continue
        previous = accepted[-1]
        if candidate["pivot_type"] == previous["pivot_type"]:
            _append_or_replace(accepted, candidate)
            continue
        close = frame.iloc[index]["close"]
        atr_value = atr.iloc[index]
        if pd.isna(close) or close == 0 or pd.isna(atr_value) or previous["price"] == 0:
            continue
        threshold_pct = float(atr_value / abs(close) * 100.0 * float(deviation))
        candidate["threshold_pct"] = threshold_pct
        move_pct = abs(candidate["price"] - previous["price"]) / abs(previous["price"]) * 100.0
        if move_pct >= threshold_pct:
            accepted.append(candidate)
    return _with_dates(frame, accepted)


def build_depth_pivots(frame: pd.DataFrame, depth: int, include_developing: bool = True) -> list[dict[str, Any]]:
    """Build alternating depth pivots and optionally append the live opposite leg."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for candidate in detect_pivot_candidates(frame["high"].tolist(), frame["low"].tolist(), depth):
        grouped.setdefault(candidate["index"], []).append(candidate)
    accepted: list[dict[str, Any]] = []
    for index in sorted(grouped):
        _append_or_replace(accepted, _choose_same_bar_candidate(grouped[index], accepted, frame))
    accepted = _with_dates(frame, accepted)
    if not include_developing or not accepted:
        return accepted
    last = accepted[-1]
    trailing = frame.iloc[last["index"] + 1:]
    if trailing.empty:
        return accepted
    if last["pivot_type"] == "low":
        values = trailing["high"].dropna()
        pivot_type = "high"
        chooser = values.idxmax
    else:
        values = trailing["low"].dropna()
        pivot_type = "low"
        chooser = values.idxmin
    if values.empty:
        return accepted
    index = int(chooser())
    accepted.append({
        "index": index,
        "date": frame.iloc[index]["date"],
        "price": float(values.loc[index]),
        "pivot_type": pivot_type,
        "confirmed": False,
        "threshold_pct": None,
    })
    return accepted


def _developing_endpoint(frame: pd.DataFrame, pivot: dict[str, Any]) -> dict[str, Any] | None:
    trailing = frame.iloc[pivot["index"] + 1:]
    if trailing.empty:
        return None
    field, pivot_type, chooser = ("high", "high", "idxmax") if pivot["pivot_type"] == "low" else ("low", "low", "idxmin")
    values = trailing[field].dropna()
    if values.empty:
        return None
    index = int(getattr(values, chooser)())
    return {"index": index, "date": frame.iloc[index]["date"], "price": float(values.loc[index]), "pivot_type": pivot_type, "confirmed": False, "threshold_pct": None}


def _anchor(name: str, pivot: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "index": int(pivot["index"]), "date": pivot["date"], "price": float(pivot["price"]), "pivot_type": pivot["pivot_type"], "confirmed": bool(pivot["confirmed"])}


def _levels(kind: str, ratios: Sequence[float], price_at_ratio) -> list[dict[str, Any]]:
    return [{"ratio": float(ratio), "label": _ratio_label(float(ratio)), "price": float(price_at_ratio(float(ratio))), "kind": kind} for ratio in ratios]


def calculate_auto_fibonacci(dates: Sequence[Any], ohlc: Mapping[str, Sequence[Any]], settings: Mapping[str, Any]) -> dict[str, Any]:
    """Calculate optional structured Auto Fib retracement and extension results."""
    frame = _prepare_frame(dates, ohlc)
    fibonacci = settings.get("fibonacci", settings) if isinstance(settings, Mapping) else {}
    retracement_settings = fibonacci.get("retracement", {}) if isinstance(fibonacci, Mapping) else {}
    extension_settings = fibonacci.get("extension", {}) if isinstance(fibonacci, Mapping) else {}
    result: dict[str, Any] = {
        "retracement": None,
        "extension": None,
        "diagnostics": {"retracement_reason": None, "extension_reason": None},
    }
    if bool(retracement_settings.get("enabled")):
        try:
            zigzag = build_retracement_zigzag(frame, float(retracement_settings.get("deviation", 3.0)), int(retracement_settings.get("depth", 10)))
            if zigzag:
                live = _developing_endpoint(frame, zigzag[-1])
                if live is not None:
                    zigzag.append(live)
            if len(zigzag) < 2:
                result["diagnostics"]["retracement_reason"] = "Not enough valid ZigZag pivots."
            else:
                a, b = zigzag[-2:]
                if a["price"] == b["price"]:
                    result["diagnostics"]["retracement_reason"] = "Latest ZigZag leg has zero length."
                else:
                    result["retracement"] = {
                        "direction": "bullish" if b["price"] > a["price"] else "bearish",
                        "anchors": [_anchor("A", a), _anchor("B", b)],
                        "levels": _levels("retracement", RETRACEMENT_RATIOS, lambda ratio: b["price"] - (b["price"] - a["price"]) * ratio),
                        "developing": not b["confirmed"],
                    }
        except (TypeError, ValueError):
            result["diagnostics"]["retracement_reason"] = "Invalid retracement settings or OHLC data."
    if bool(extension_settings.get("enabled")):
        try:
            pivots = build_depth_pivots(frame, int(extension_settings.get("depth", 10)), include_developing=True)
            found = None
            for start in range(len(pivots) - 3, -1, -1):
                a, b, c = pivots[start:start + 3]
                bullish = (a["pivot_type"], b["pivot_type"], c["pivot_type"]) == ("low", "high", "low")
                bearish = (a["pivot_type"], b["pivot_type"], c["pivot_type"]) == ("high", "low", "high")
                ab_length = abs(b["price"] - a["price"])
                if not (bullish or bearish) or ab_length == 0:
                    continue
                retracement_ratio = abs(c["price"] - b["price"]) / ab_length
                valid_c = (bullish and a["price"] <= c["price"] <= b["price"]) or (bearish and a["price"] >= c["price"] >= b["price"])
                if valid_c and 0.236 <= retracement_ratio <= 1.0:
                    found = (a, b, c)
                    break
            if found is None:
                result["diagnostics"]["extension_reason"] = "No recent A-B-C structure with a 23.6%-100% pullback."
            else:
                a, b, c = found
                direction = 1.0 if b["price"] > a["price"] else -1.0
                result["extension"] = {
                    "direction": "bullish" if direction > 0 else "bearish",
                    "anchors": [_anchor("A", a), _anchor("B", b), _anchor("C", c)],
                    "levels": _levels("extension", EXTENSION_RATIOS, lambda ratio: c["price"] + direction * abs(b["price"] - a["price"]) * ratio),
                    "developing": not c["confirmed"],
                }
        except (TypeError, ValueError):
            result["diagnostics"]["extension_reason"] = "Invalid extension settings or OHLC data."
    return result


def add_fibonacci_overlays(fig: Any, fibonacci: Mapping[str, Any] | None, dates: Sequence[Any], dark_mode: bool, *, row: int = 1, col: int = 1) -> None:
    """Add grouped Plotly traces for structured Fibonacci results."""
    if not fibonacci or len(dates) == 0:
        return
    import plotly.graph_objects as go

    styles = {
        "retracement": {"color": "#f59e0b" if dark_mode else "#b45309", "group": "auto_fib_retracement", "name": "Auto Fib Retracement", "prefix": "R"},
        "extension": {"color": "#a78bfa" if dark_mode else "#6d28d9", "group": "auto_fib_extension", "name": "Auto Fib Extension", "prefix": "E"},
    }
    for kind, payload in (("retracement", fibonacci.get("retracement")), ("extension", fibonacci.get("extension"))):
        if not isinstance(payload, Mapping):
            continue
        anchors = payload.get("anchors") or []
        levels = payload.get("levels") or []
        if not anchors or not levels:
            continue
        style = styles[kind]
        x0 = anchors[-1]["date"] if kind == "extension" else min(anchors, key=lambda anchor: int(anchor["index"]))["date"]
        x1 = dates[-1]
        anchor_x = [anchor["date"] for anchor in anchors]
        anchor_y = [anchor["price"] for anchor in anchors]
        confirmed = [bool(anchor.get("confirmed")) for anchor in anchors]
        anchor_labels = [f"{anchor['name']} ({str(anchor['pivot_type']).title()})" for anchor in anchors]
        fig.add_trace(go.Scatter(
            x=anchor_x, y=anchor_y, mode="lines+markers+text", text=anchor_labels, textposition="top center",
            name=style["name"], legendgroup=style["group"], showlegend=True,
            line=dict(color=style["color"], width=2, dash="dot" if not all(confirmed) else "solid"),
            marker=dict(color=style["color"], size=9, symbol=["circle" if item else "circle-open" for item in confirmed]),
            customdata=[[anchor["name"], anchor["pivot_type"], "confirmed" if anchor.get("confirmed") else "developing"] for anchor in anchors],
            hovertemplate=f"{style['name']}<br>%{{customdata[0]}}: %{{y:.4f}}<br>%{{x}}<br>%{{customdata[1]}} · %{{customdata[2]}}<extra></extra>",
        ), row=row, col=col)
        for level in levels:
            price = level.get("price")
            if not isinstance(price, (int, float)) or not np.isfinite(price):
                continue
            endpoint = ""
            if kind == "retracement" and level.get("ratio") == 0.0:
                endpoint = " (B)"
            elif kind == "retracement" and level.get("ratio") == 1.0:
                endpoint = " (A)"
            label = f"{style['prefix']} {level.get('label', '')}{endpoint}  {float(price):.2f}"
            fig.add_trace(go.Scatter(
                x=[x0, x1], y=[price, price], mode="lines+text", text=[None, label], textposition="middle right",
                name=f"{style['name']} level", legendgroup=style["group"], showlegend=False,
                line=dict(color=style["color"], width=1, dash="dash"), textfont=dict(color=style["color"], size=9),
                hovertemplate=f"{style['name']}<br>{level.get('label', '')}: %{{y:.4f}}<extra></extra>",
            ), row=row, col=col)
