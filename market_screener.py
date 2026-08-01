"""Market-aware deterministic stock screening for Market Breadth & Screener.

This module deliberately has no Flask, SQLite, yfinance, scraper, or LLM
dependency.  Prices are always evaluated in their native trading currency;
fundamentals are optional cached inputs supplied by the backend.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd


SCREENER_VERSION = "market-aware-factor-v2"
TOP_DISPLAY_COUNT = 30

# Thresholds are 20-day *average* traded value in each market's local currency.
# They deliberately are not the target project's current-day turnover thresholds.
MARKET_PROFILES: dict[str, dict[str, Any]] = {
    "US": {"benchmark": "^GSPC", "currency": "USD", "price": {"trend": 5.0, "breakout": 5.0, "reversal": 5.0}, "turnover": {"trend": 2_000_000.0, "breakout": 2_000_000.0, "reversal": 2_000_000.0}},
    "CN": {"benchmark": "000001.SS", "currency": "CNY", "price": {"trend": 4.0, "breakout": 3.0, "reversal": 3.0}, "turnover": {"trend": 50_000_000.0, "breakout": 30_000_000.0, "reversal": 20_000_000.0}},
    "HK": {"benchmark": "^HSI", "currency": "HKD", "price": {"trend": 5.0, "breakout": 3.0, "reversal": 3.0}, "turnover": {"trend": 5_000_000.0, "breakout": 3_000_000.0, "reversal": 2_000_000.0}},
    "JP": {"benchmark": "^N225", "currency": "JPY", "price": {"trend": 400.0, "breakout": 300.0, "reversal": 300.0}, "turnover": {"trend": 10_000_000.0, "breakout": 6_000_000.0, "reversal": 4_000_000.0}},
    "DE": {"benchmark": "^GDAXI", "currency": "EUR", "price": {"trend": 5.0, "breakout": 4.0, "reversal": 4.0}, "turnover": {"trend": 500_000.0, "breakout": 300_000.0, "reversal": 200_000.0}},
    "OTHER": {"benchmark": "^GSPC", "currency": "local", "price": {"trend": 5.0, "breakout": 5.0, "reversal": 5.0}, "turnover": {"trend": 2_000_000.0, "breakout": 2_000_000.0, "reversal": 2_000_000.0}},
}

STRATEGIES: dict[str, dict[str, str]] = {
    "trend_quality": {"label": "趋势质量", "description": "均线多头、相对动量、流动性与基本面共同确认的趋势个股。"},
    "volume_breakout": {"label": "放量突破", "description": "价格突破、量能确认且中期趋势仍向上的候选。"},
    "oversold_reversal": {"label": "超跌反转", "description": "超跌后 MACD Diff 改善的反转候选，风险相对更高。"},
}


def market_profile_for_ticker(ticker: str) -> str:
    symbol = str(ticker or "").upper()
    if symbol.endswith((".SS", ".SZ", ".BJ")):
        return "CN"
    if symbol.endswith(".HK"):
        return "HK"
    if symbol.endswith(".T"):
        return "JP"
    if symbol.endswith(".DE"):
        return "DE"
    return "US" if "." not in symbol else "OTHER"


def screening_benchmark_tickers(tickers: Any) -> list[str]:
    """Return the minimal set of local benchmarks required by a ticker collection."""
    if isinstance(tickers, dict):
        tickers = tickers.keys()
    return sorted({MARKET_PROFILES[market_profile_for_ticker(ticker)]["benchmark"] for ticker in (tickers or [])})


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _clip_score(value: Any, lower: float, upper: float, maximum: float) -> float:
    value = _finite(value)
    if value is None or upper <= lower:
        return 0.0
    return round(float(np.clip((value - lower) / (upper - lower), 0.0, 1.0) * maximum), 2)


def _price_series(data: pd.DataFrame, field: str, ticker: str) -> pd.Series:
    try:
        return pd.to_numeric(data[field][ticker], errors="coerce").dropna()
    except (KeyError, TypeError):
        return pd.Series(dtype="float64")


def _return(close: pd.Series, bars: int) -> float | None:
    if len(close) <= bars:
        return None
    start, latest = _finite(close.iloc[-bars - 1]), _finite(close.iloc[-1])
    return None if start in (None, 0) or latest is None else (latest / start - 1.0) * 100.0


def _relative_return(close: pd.Series, benchmark: pd.Series, bars: int) -> float | None:
    joined = pd.concat([close.rename("asset"), benchmark.rename("benchmark")], axis=1).dropna()
    if len(joined) <= bars:
        return None
    asset_start, benchmark_start = _finite(joined.asset.iloc[-bars - 1]), _finite(joined.benchmark.iloc[-bars - 1])
    asset_last, benchmark_last = _finite(joined.asset.iloc[-1]), _finite(joined.benchmark.iloc[-1])
    if any(value in (None, 0) for value in (asset_start, benchmark_start, asset_last, benchmark_last)):
        return None
    return ((asset_last / asset_start) - (benchmark_last / benchmark_start)) * 100.0


def _percentile(values: list[tuple[str, float]], *, ascending: bool = True) -> dict[str, float]:
    valid = [(ticker, value) for ticker, value in values if _finite(value) is not None]
    if not valid:
        return {}
    series = pd.Series({ticker: value for ticker, value in valid}, dtype="float64")
    return (series.rank(pct=True, ascending=ascending) * 100.0).round(2).to_dict()


def _features(data: pd.DataFrame, ticker: str, fundamentals: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    close = _price_series(data, "Adj Close", ticker)
    volume = _price_series(data, "Volume", ticker)
    frame = pd.concat([close.rename("close"), volume.rename("volume")], axis=1, sort=False).dropna()
    if len(frame) < 126:
        return None, "日线数据不足 126 根"
    close, volume = frame.close, frame.volume
    market = market_profile_for_ticker(ticker)
    benchmark = _price_series(data, "Adj Close", MARKET_PROFILES[market]["benchmark"])
    latest = _finite(close.iloc[-1])
    turnover = _finite((close * volume).tail(20).mean())
    if latest is None or turnover is None:
        return None, "价格或成交量无效"

    ma20, ma50, ma200 = close.rolling(20).mean(), close.rolling(50).mean(), close.rolling(200).mean()
    avg_volume20 = volume.rolling(20).mean()
    delta = close.diff()
    gains, losses = delta.clip(lower=0).rolling(14).mean(), (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100.0 - 100.0 / (1.0 + gains / losses.replace(0, np.nan))
    rsi[(losses == 0) & (gains > 0)] = 100.0
    rsi[(gains == 0) & (losses > 0)] = 0.0
    rsi[(gains == 0) & (losses == 0)] = 50.0
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_diff = macd - macd.ewm(span=9, adjust=False).mean()
    std20 = close.rolling(20).std()
    drawdown60 = close.tail(60) / close.tail(60).cummax() - 1.0
    info = fundamentals or {}
    return {
        "market": market, "currency": MARKET_PROFILES[market]["currency"], "price": latest,
        "avg_turnover": turnover, "ma20": _finite(ma20.iloc[-1]), "ma50": _finite(ma50.iloc[-1]), "ma200": _finite(ma200.iloc[-1]),
        "ma50_20d_ago": _finite(ma50.iloc[-21]) if len(ma50) > 20 else None,
        "volume_ratio": _finite(volume.iloc[-1] / avg_volume20.iloc[-1]) if _finite(avg_volume20.iloc[-1]) not in (None, 0) else None,
        "rsi14": _finite(rsi.iloc[-1]), "macd_diff": _finite(macd_diff.iloc[-1]), "macd_diff_previous": _finite(macd_diff.iloc[-2]),
        "bb_lower": _finite((ma20 - 2 * std20).iloc[-1]), "prior_20d_high": _finite(close.iloc[-21:-1].max()),
        "return_1d": _return(close, 1), "return_20d": _return(close, 20), "return_60d": _return(close, 60), "return_120d": _return(close, 120),
        "relative_20d": _relative_return(close, benchmark, 20), "relative_60d": _relative_return(close, benchmark, 60), "relative_120d": _relative_return(close, benchmark, 120),
        "annual_volatility": _finite(close.pct_change().tail(20).std() * np.sqrt(252)), "drawdown60": _finite(drawdown60.min()), "data_bars": len(close),
        "trailing_pe": _finite(info.get("trailing_pe")), "forward_pe": _finite(info.get("forward_pe")),
        # Keep the value-factor semantics transparent: realized TTM earnings
        # take priority; analyst forward estimates are only a fallback.
        "pe": _finite(info.get("trailing_pe")) or _finite(info.get("forward_pe")), "pb": _finite(info.get("pb_ratio")),
        "peg": _finite(info.get("peg_ratio")), "market_cap": _finite(info.get("market_cap")),
    }, None


def _passes_liquidity(f: dict[str, Any], strategy: str) -> tuple[bool, str | None]:
    profile = MARKET_PROFILES[f["market"]]
    if f["price"] < profile["price"][strategy]:
        return False, f"价格低于 {profile['currency']} {profile['price'][strategy]:g}"
    if f["avg_turnover"] < profile["turnover"][strategy]:
        return False, f"20 日平均成交额低于 {profile['currency']} {profile['turnover'][strategy]:,.0f}"
    return True, None


def _attach_cross_sectional_scores(records: dict[str, dict[str, Any]]) -> None:
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for ticker, feature in records.items():
        grouped[feature["market"]].append((ticker, feature))
    for group in grouped.values():
        pe_rank = _percentile([(ticker, f["pe"]) for ticker, f in group if f.get("pe") and f["pe"] > 0], ascending=False)
        pb_rank = _percentile([(ticker, f["pb"]) for ticker, f in group if f.get("pb") and f["pb"] > 0], ascending=False)
        liquidity_rank = _percentile([(ticker, np.log10(max(f["avg_turnover"], 1))) for ticker, f in group])
        size_rank = _percentile([(ticker, np.log10(max(f["market_cap"], 1))) for ticker, f in group if f.get("market_cap")])
        for ticker, f in group:
            valid_value = [value for value in (pe_rank.get(ticker), pb_rank.get(ticker)) if value is not None]
            f["value_rank"] = round((0.65 * pe_rank.get(ticker, 0) + 0.35 * pb_rank.get(ticker, 0)) if valid_value else 25.0, 2)
            f["liquidity_rank"] = liquidity_rank.get(ticker, 0.0)
            f["size_rank"] = size_rank.get(ticker, 25.0)


def _risk_deduction(f: dict[str, Any], strategy: str) -> tuple[float, list[str]]:
    deduction, tags = 0.0, []
    if (f.get("annual_volatility") or 0) >= 0.55:
        deduction += 5; tags.append("高波动")
    if (f.get("volume_ratio") or 0) >= 3.0:
        deduction += 3; tags.append("异常放量")
    if (f.get("pe") is not None and f["pe"] <= 0):
        deduction += 3; tags.append("非正 PE")
    if (f.get("pb") or 0) >= 10:
        deduction += 2; tags.append("高 PB")
    if strategy == "volume_breakout" and (f.get("return_1d") or 0) >= 9:
        deduction += 4; tags.append("当日追涨风险")
    if strategy == "trend_quality" and (f["price"] / max(f.get("ma50") or f["price"], 1) - 1) >= .15:
        deduction += 3; tags.append("偏离 MA50 过大")
    if strategy == "oversold_reversal":
        deduction += 3; tags.append("反转尚未确认")
    return deduction, tags or ["常规波动"]


def _candidate(ticker: str, source: str, f: dict[str, Any], factors: dict[str, float], reason: str, strategy: str) -> dict[str, Any]:
    base_score = round(sum(factors.values()), 2)
    deduction, tags = _risk_deduction(f, strategy)
    return {
        "Ticker": ticker, "Source": source, "Market": f["market"], "Currency": f["currency"], "Price": round(f["price"], 2),
        "1D%": _rounded(f.get("return_1d")), "Base Score": base_score, "Risk Deduction": round(deduction, 2), "Score": round(max(0.0, min(100.0, base_score - deduction)), 2),
        "Factor Scores": {name: round(value, 2) for name, value in factors.items()}, "Risk Tags": tags, "Reason": reason,
        "Metrics": {"20D Return%": _rounded(f.get("return_20d")), "60D Return%": _rounded(f.get("return_60d")), "120D Return%": _rounded(f.get("return_120d")),
                    "20D Relative%": _rounded(f.get("relative_20d")), "60D Relative%": _rounded(f.get("relative_60d")), "120D Relative%": _rounded(f.get("relative_120d")),
                    "Volume Ratio": _rounded(f.get("volume_ratio")), "20D Avg Turnover": _rounded(f.get("avg_turnover")), "RSI14": _rounded(f.get("rsi14")),
                    "MA20 Distance%": _rounded((f["price"] / f["ma20"] - 1) * 100) if f.get("ma20") else None,
                    "MA50 Distance%": _rounded((f["price"] / f["ma50"] - 1) * 100) if f.get("ma50") else None,
                    "MA50/MA200%": _rounded((f["ma50"] / f["ma200"] - 1) * 100) if f.get("ma50") and f.get("ma200") else None,
                    "20D Breakout%": _rounded((f["price"] / f["prior_20d_high"] - 1) * 100) if f.get("prior_20d_high") else None,
                    "BB Lower Distance%": _rounded((f["price"] / f["bb_lower"] - 1) * 100) if f.get("bb_lower") else None,
                    "MACD Diff": _rounded(f.get("macd_diff")), "MACD Diff Change": _rounded((f["macd_diff"] - f["macd_diff_previous"]) if f.get("macd_diff") is not None and f.get("macd_diff_previous") is not None else None),
                    "Annual Volatility": _rounded(f.get("annual_volatility")), "60D Drawdown%": _rounded((f.get("drawdown60") or 0) * 100),
                    "PE TTM": _rounded(f.get("trailing_pe")), "Forward PE": _rounded(f.get("forward_pe")), "PB": _rounded(f.get("pb")),
                    "PEG": _rounded(f.get("peg")), "Market Cap": _rounded(f.get("market_cap")),
                    "Fundamental coverage": bool(f.get("pe") or f.get("pb") or f.get("market_cap"))},
    }


def _rounded(value: Any) -> float | None:
    value = _finite(value)
    return round(value, 2) if value is not None else None


def _trend(ticker: str, source: str, f: dict[str, Any]) -> dict[str, Any] | None:
    if f["data_bars"] < 252 or not (f["price"] > f.get("ma50", np.inf) > f.get("ma200", np.inf) and f.get("ma50") > f.get("ma50_20d_ago", np.inf)):
        return None
    momentum_input = (
        (f.get("return_20d") or -20) * .45 + (f.get("return_60d") or -20) * .75 + (f.get("return_120d") or -20) * .55
        + (f.get("relative_20d") or -20) * .40 + (f.get("relative_60d") or -20) * .65 + (f.get("relative_120d") or -20) * .45
    )
    # Theme heat is intentionally omitted: this cross-market screener has no
    # verified industry/concept mapping or board-history data.  Its former
    # allocation is redistributed across observable factors so scores remain
    # on a comparable 0-100 scale without a neutral placeholder.
    momentum = _clip_score(momentum_input, 0, 95, 30)
    value = f["value_rank"] * .16
    liquidity = f["liquidity_rank"] * .19
    activity = _clip_score(f.get("volume_ratio"), .8, 2.5, 16)
    stability = _clip_score(.70 - (f.get("annual_volatility") or .70), 0, .60, 8.67) + _clip_score((f.get("drawdown60") or -.5) + .35, 0, .35, 4.33)
    reversal = _clip_score((f.get("macd_diff") or 0), 0, max(f["price"] * .01, .01), 4)
    size = f["size_rank"] * .02
    return _candidate(ticker, source, f, {"动量": momentum, "估值": value, "流动性": liquidity, "活跃度": activity, "稳定性": stability, "反转": reversal, "规模": size}, "均线多头排列，20/60/120 日动量与本地指数相对强弱共同确认。", "trend_quality")


def _breakout(ticker: str, source: str, f: dict[str, Any]) -> dict[str, Any] | None:
    high = f.get("prior_20d_high")
    if high in (None, 0) or not (f["price"] > high and (f.get("volume_ratio") or 0) >= 1.5 and f["price"] > (f.get("ma20") or np.inf)):
        return None
    breakout_strength = (f["price"] / high - 1) * 100
    momentum = _clip_score(breakout_strength + (f.get("return_20d") or 0) * .55 + (f.get("return_60d") or 0) * .25, 0, 35, 35)
    activity = _clip_score(f.get("volume_ratio"), 1.5, 4.0, 30)
    liquidity = f["liquidity_rank"] * .24
    stability = _clip_score(.70 - (f.get("annual_volatility") or .70), 0, .60, 11)
    return _candidate(ticker, source, f, {"突破动量": momentum, "活跃度": activity, "流动性": liquidity, "稳定性": stability}, "收盘价突破此前 20 日高点，并得到相对量能确认。", "volume_breakout")


def _reversal(ticker: str, source: str, f: dict[str, Any]) -> dict[str, Any] | None:
    oversold = (f.get("rsi14") or 100) <= 35 or f["price"] <= (f.get("bb_lower") or -np.inf)
    improving = f.get("macd_diff") is not None and f.get("macd_diff_previous") is not None and f["macd_diff"] > f["macd_diff_previous"]
    if not (oversold and improving):
        return None
    reversal = _clip_score(42 - (f.get("rsi14") or 42), 0, 30, 25) + _clip_score((f.get("ma20") / f["price"] - 1) * 100 if f.get("ma20") else 0, 0, 18, 15)
    stability = _clip_score(.80 - (f.get("annual_volatility") or .80), 0, .70, 20)
    liquidity = f["liquidity_rank"] * .18
    value = f["value_rank"] * .16
    activity = _clip_score(f.get("volume_ratio"), .6, 2.2, 6)
    return _candidate(ticker, source, f, {"反转": reversal, "稳定性": stability, "流动性": liquidity, "估值": value, "活跃度": activity}, "RSI 或布林带显示超跌，同时 MACD Diff 较前一日改善。", "oversold_reversal")


def run_screener(data: pd.DataFrame, candidates: dict[str, str], fundamentals: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Screen an already fetched OHLCV frame; fundamental input is cache-only optional."""
    fundamentals = fundamentals or {}
    features: dict[str, dict[str, Any]] = {}
    exclusions: dict[str, int] = {}
    for ticker in candidates:
        feature, exclusion = _features(data, ticker, fundamentals.get(ticker))
        if feature is None:
            exclusions[exclusion or "无法计算指标"] = exclusions.get(exclusion or "无法计算指标", 0) + 1
        else:
            features[ticker] = feature
    _attach_cross_sectional_scores(features)
    strategies = {key: {"key": key, "label": meta["label"], "description": meta["description"], "candidates": [], "excluded": dict(exclusions)} for key, meta in STRATEGIES.items()}
    for ticker, feature in features.items():
        source = candidates[ticker]
        for key, runner, liquidity_key in (("trend_quality", _trend, "trend"), ("volume_breakout", _breakout, "breakout"), ("oversold_reversal", _reversal, "reversal")):
            passed, reason = _passes_liquidity(feature, liquidity_key)
            if not passed:
                strategies[key]["excluded"][reason] = strategies[key]["excluded"].get(reason, 0) + 1
                continue
            row = runner(ticker, source, feature)
            if row is not None:
                strategies[key]["candidates"].append(row)
    for strategy in strategies.values():
        strategy["candidates"].sort(key=lambda row: (-float(row["Score"]), row["Ticker"]))
        strategy["matched_count"] = len(strategy["candidates"])
        strategy["display_count"] = min(TOP_DISPLAY_COUNT, strategy["matched_count"])
    coverage = sum(1 for value in features.values() if value.get("pe") or value.get("pb") or value.get("market_cap"))
    return {"version": SCREENER_VERSION, "strategies": list(strategies.values()), "fundamental_coverage": {"cached_or_enriched": coverage, "price_eligible": len(features)}}
