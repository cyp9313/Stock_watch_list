"""Standalone A-share and US market recap generation.

This intentionally does not use the stock-report agent loop.  It turns public
market data into a bounded snapshot, asks the configured model for one
tool-free interpretation, and always has a deterministic HTML fallback.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from .report_components import (
    esc,
    render_disclaimer,
    render_html_head,
    render_kpi_cards,
    render_news_group,
    render_section,
    render_table as render_report_table,
)
from .service import _get_market_date


APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DB = APP_ROOT / "daily_report_jobs.db"
_MARKETS = {"us", "cn"}
_US_INDEXES = {
    # Futures provide a more consistently updated Yahoo volume series than the
    # cash-index tickers. Breadth remains calculated from the corresponding
    # S&P 500 / Nasdaq 100 constituent universes below.
    "ES=F": "标普 500 E-mini 期货",
    "NQ=F": "纳斯达克 100 E-mini 期货",
    "^DJI": "道琼斯工业指数",
    "^RUT": "罗素 2000",
    "^VIX": "VIX 波动率指数",
}
_US_MACRO = {
    "^TNX": "十年期美债收益率",
    "BZ=F": "布伦特原油",
    "DX-Y.NYB": "美元指数",
}
_CN_INDEXES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000300": "沪深 300",
}


def market_recap_enabled() -> bool:
    return os.environ.get("MARKET_RECAP_ENABLED", "true").strip().lower() not in {"0", "false", "no"}


def normalize_markets(value: str | Iterable[str] | None) -> list[str]:
    if isinstance(value, str):
        raw = value.lower().replace("+", ",").replace("/", ",").split(",")
    else:
        raw = list(value or ["us"])
    values = []
    aliases = {"a股": "cn", "cn": "cn", "china": "cn", "us": "us", "美股": "us", "usa": "us"}
    for item in raw:
        normalized = aliases.get(str(item).strip().lower(), str(item).strip().lower())
        if normalized in _MARKETS and normalized not in values:
            values.append(normalized)
    return values or ["us"]


def market_subject_key(markets: str | Iterable[str] | None) -> str:
    return "market:" + "+".join(normalize_markets(markets))


def market_subject_name(markets: str | Iterable[str] | None) -> str:
    selected = normalize_markets(markets)
    label = {"us": "美股", "cn": "A股"}
    return " + ".join(label[item] for item in selected) + "大盘复盘"


def _cache_db_path() -> Path:
    configured = os.environ.get("MARKET_RECAP_CACHE_DB", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    configured = os.environ.get("REPORT_JOB_DB", "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_CACHE_DB


def _cache_connection() -> sqlite3.Connection:
    path = _cache_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS market_recap_snapshots (
        market TEXT NOT NULL, as_of_date TEXT NOT NULL, data_version TEXT NOT NULL,
        generated_at TEXT NOT NULL, payload_json TEXT NOT NULL,
        PRIMARY KEY (market, as_of_date, data_version))"""
    )
    return conn


def _cache_ttl_seconds() -> int:
    try:
        return max(0, int(os.environ.get("MARKET_RECAP_CACHE_TTL_SECONDS", "900")))
    except ValueError:
        return 900


def _load_cached_snapshot(market: str, data_version: str = "v6") -> dict[str, Any] | None:
    try:
        with _cache_connection() as conn:
            row = conn.execute(
                "SELECT generated_at, payload_json FROM market_recap_snapshots WHERE market=? AND data_version=? "
                "ORDER BY generated_at DESC LIMIT 1", (market, data_version)
            ).fetchone()
        if row is None:
            return None
        generated = datetime.fromisoformat(row["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds() > _cache_ttl_seconds():
            return None
        value = json.loads(row["payload_json"])
        return value if isinstance(value, dict) else None
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        return None


def _save_cached_snapshot(snapshot: dict[str, Any], data_version: str = "v6") -> None:
    market = str(snapshot.get("market") or "")
    as_of = str(snapshot.get("as_of_date") or "")
    if market not in _MARKETS or not as_of:
        return
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _cache_connection() as conn:
            conn.execute(
                "INSERT INTO market_recap_snapshots(market, as_of_date, data_version, generated_at, payload_json) "
                "VALUES(?,?,?,?,?) ON CONFLICT(market, as_of_date, data_version) DO UPDATE SET "
                "generated_at=excluded.generated_at,payload_json=excluded.payload_json",
                (market, as_of, data_version, now, json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))),
            )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        pass


def _recap_delivery_state(schedule_id: str, snapshots: list[dict[str, Any]]) -> bool:
    """Return whether at least one selected market has a newer completed date."""
    if not schedule_id:
        return True
    try:
        with _cache_connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS market_recap_schedule_delivery (
                schedule_id TEXT NOT NULL, market TEXT NOT NULL, as_of_date TEXT NOT NULL,
                delivered_at TEXT NOT NULL, PRIMARY KEY(schedule_id, market))"""
            )
            prior = {row["market"]: row["as_of_date"] for row in conn.execute(
                "SELECT market, as_of_date FROM market_recap_schedule_delivery WHERE schedule_id=?", (schedule_id,)
            ).fetchall()}
        return any(str(item.get("as_of_date") or "") > str(prior.get(item.get("market"), "")) for item in snapshots)
    except (OSError, sqlite3.Error):
        # Failing open preserves normal report delivery if optional de-dup state
        # is temporarily unavailable.
        return True


def mark_market_recap_delivered(schedule_id: str | None, snapshots: list[dict[str, Any]]) -> None:
    if not schedule_id:
        return
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _cache_connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS market_recap_schedule_delivery (
                schedule_id TEXT NOT NULL, market TEXT NOT NULL, as_of_date TEXT NOT NULL,
                delivered_at TEXT NOT NULL, PRIMARY KEY(schedule_id, market))"""
            )
            for snapshot in snapshots:
                market, as_of = str(snapshot.get("market") or ""), str(snapshot.get("as_of_date") or "")
                if market in _MARKETS and as_of:
                    conn.execute(
                        "INSERT INTO market_recap_schedule_delivery(schedule_id,market,as_of_date,delivered_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(schedule_id,market) DO UPDATE SET as_of_date=excluded.as_of_date, delivered_at=excluded.delivered_at",
                        (schedule_id, market, as_of, now),
                    )
    except (OSError, sqlite3.Error):
        pass


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _change(series: pd.Series, days: int) -> float | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) <= days or clean.iloc[-days - 1] == 0:
        return None
    return float((clean.iloc[-1] / clean.iloc[-days - 1] - 1.0) * 100.0)


def _instrument_rows(data: pd.DataFrame, labels: dict[str, str], *, yield_ticker: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ticker, name in labels.items():
        if ticker not in data.columns:
            rows.append({"ticker": ticker, "name": name, "status": "missing"})
            continue
        values = pd.to_numeric(data[ticker], errors="coerce").dropna()
        if values.empty:
            rows.append({"ticker": ticker, "name": name, "status": "missing"})
            continue
        latest = float(values.iloc[-1])
        if ticker == yield_ticker:
            # Yahoo has used both 4.2 and 42.0 representations for ^TNX across
            # endpoints/history.  Normalize only the legacy ten-times form.
            divisor = 10.0 if latest > 20 else 1.0
            latest /= divisor
            one_day = _change(values / divisor, 1)
            five_day = _change(values / divisor, 5)
            unit = "%"
        else:
            one_day = _change(values, 1)
            five_day = _change(values, 5)
            unit = ""
        trend = "走强" if (five_day or 0) > 0 else "走弱" if (five_day or 0) < 0 else "持平"
        rows.append({
            "ticker": ticker, "name": name, "value": round(latest, 4), "1d_pct": _round(one_day),
            "5d_pct": _round(five_day), "trend": trend, "unit": unit, "status": "ok",
        })
    return rows


def _price_field_series(data: pd.DataFrame, field: str, ticker: str) -> pd.Series:
    """Return one cached yfinance field without assuming a MultiIndex order."""
    if data is None or data.empty:
        return pd.Series(dtype="float64")
    candidates = ((field, ticker), (ticker, field))
    for key in candidates:
        if isinstance(data.columns, pd.MultiIndex) and key in data.columns:
            return pd.to_numeric(data[key], errors="coerce").dropna()
    if not isinstance(data.columns, pd.MultiIndex) and field in data.columns:
        return pd.to_numeric(data[field], errors="coerce").dropna()
    return pd.Series(dtype="float64")


def _add_index_session_fields(rows: list[dict[str, Any]], data: pd.DataFrame) -> list[dict[str, Any]]:
    """Add auditable daily OHLC/range fields already held in price_cache."""
    enriched: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        ticker = str(row.get("ticker") or "")
        close = _price_field_series(data, "Close", ticker)
        opening = _price_field_series(data, "Open", ticker)
        high = _price_field_series(data, "High", ticker)
        low = _price_field_series(data, "Low", ticker)
        volume = _price_field_series(data, "Volume", ticker)
        if not close.empty:
            previous = _number(close.iloc[-2]) if len(close) > 1 else None
            row["previous_close"] = _round(previous, 4)
        else:
            previous = None
        for output_key, series in (("open", opening), ("high", high), ("low", low)):
            row[output_key] = _round(_number(series.iloc[-1]), 4) if not series.empty else None
        high_value, low_value = _number(row.get("high")), _number(row.get("low"))
        row["amplitude_pct"] = _round((high_value - low_value) / previous * 100.0) if previous and high_value is not None and low_value is not None else None
        if not volume.empty:
            latest_volume = _number(volume.iloc[-1])
            average_volume = _number(volume.iloc[-21:-1].mean()) if len(volume) > 1 else None
            row["volume"] = _round(latest_volume, 0)
            row["volume_ratio"] = _round(latest_volume / average_volume, 2) if latest_volume is not None and average_volume and average_volume > 0 else None
        enriched.append(row)
    return enriched


def _breadth_values(frame: pd.DataFrame) -> dict[str, float | None]:
    if frame.empty:
        return {key: None for key in ("ma20", "ma20_delta_1d", "ma50", "ma50_delta_1d", "ma200", "ma200_delta_1d")}
    last = frame.iloc[-1]
    prior = frame.iloc[-2] if len(frame) > 1 else pd.Series(dtype="float64")
    return {
        "ma20": _round(last.get("20MA_Ratio")),
        "ma20_delta_1d": _round(_number(last.get("20MA_Ratio")) - _number(prior.get("20MA_Ratio"))) if _number(last.get("20MA_Ratio")) is not None and _number(prior.get("20MA_Ratio")) is not None else None,
        "ma50": _round(last.get("50MA_Ratio")),
        "ma50_delta_1d": _round(_number(last.get("50MA_Ratio")) - _number(prior.get("50MA_Ratio"))) if _number(last.get("50MA_Ratio")) is not None and _number(prior.get("50MA_Ratio")) is not None else None,
        "ma200": _round(last.get("200MA_Ratio")),
        "ma200_delta_1d": _round(_number(last.get("200MA_Ratio")) - _number(prior.get("200MA_Ratio"))) if _number(last.get("200MA_Ratio")) is not None and _number(prior.get("200MA_Ratio")) is not None else None,
    }


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(float(value)) else None


def _latest_completed_daily_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove sparse yfinance spill-over rows; complete rows stay reproducible."""
    if frame.empty:
        return frame
    valid = frame.dropna(how="all")
    if valid.empty:
        return valid
    # Daily batch downloads occasionally append an incomplete same-day row.  A
    # row with less than 60% available observations cannot describe breadth.
    threshold = max(1, math.ceil(valid.shape[1] * 0.6))
    complete = valid.loc[valid.notna().sum(axis=1) >= threshold]
    return complete if not complete.empty else valid


def _us_snapshot() -> dict[str, Any]:
    cached = _load_cached_snapshot("us")
    if cached:
        return cached
    errors: list[str] = []
    try:
        import stock_watch_list_back_end as backend
        sp500 = backend.get_sp500_symbols()
        nasdaq100 = backend.get_nasdaq100_symbols()
        tickers = list(dict.fromkeys(sp500 + nasdaq100 + list(_US_INDEXES) + list(_US_MACRO)))
        price_data = backend.get_prices_with_cache(tickers, period="2y")
        prices = backend.extract_adj_close_frame(price_data) if isinstance(price_data.columns, pd.MultiIndex) else price_data
        prices = _latest_completed_daily_frame(prices)
        if prices.empty:
            raise RuntimeError("yfinance/cache returned no daily closes")
        as_of = pd.Timestamp(prices.index[-1]).date().isoformat()
        breadth_sp = backend.calculate_market_breadth(prices, sp500)
        breadth_ndx = backend.calculate_market_breadth(prices, nasdaq100)
        treemap = backend.build_sp500_treemap_data(prices, sp500) + backend.build_nasdaq100_treemap_data(prices, nasdaq100)
        sector_values: dict[str, list[float]] = {}
        for row in treemap:
            sector = str(row.get("Sector") or "未知")
            change = _number(row.get("1D%"))
            if change is not None:
                sector_values.setdefault(sector, []).append(change)
        sectors = [{"name": name, "change_pct": _round(float(np.median(values))), "count": len(values)} for name, values in sector_values.items()]
        sectors.sort(key=lambda item: item["change_pct"] if item["change_pct"] is not None else -999, reverse=True)
        snapshot = {
            "market": "us", "market_name": "美股", "as_of_date": as_of, "source": "yfinance + existing SQLite price_cache",
            "indexes": _add_index_session_fields(_instrument_rows(prices, _US_INDEXES), price_data), "macro": _instrument_rows(prices, _US_MACRO, yield_ticker="^TNX"),
            "breadth": {"sp500": _breadth_values(breadth_sp), "nasdaq100": _breadth_values(breadth_ndx)},
            "sectors": {"leaders": sectors[:5], "laggards": list(reversed(sectors[-5:]))}, "errors": errors,
        }
    except Exception as exc:
        snapshot = {"market": "us", "market_name": "美股", "as_of_date": _get_market_date(), "source": "yfinance", "indexes": [], "macro": [], "breadth": {}, "sectors": {}, "errors": [f"美股市场数据不可用：{type(exc).__name__}"]}
    _save_cached_snapshot(snapshot)
    return snapshot


def _find_column(frame: pd.DataFrame, names: Iterable[str]) -> str | None:
    normalized = {str(col).strip().casefold(): str(col) for col in frame.columns}
    for name in names:
        if str(name).strip().casefold() in normalized:
            return normalized[str(name).strip().casefold()]
    return None


def _a_share_limit_ratio(code: Any, name: Any) -> float:
    """Return the applicable A-share daily price-limit ratio for one quote."""
    raw_code = str(code or "").strip().lower()
    digits = re.sub(r"\D", "", raw_code)
    security_name = str(name or "").upper()
    if raw_code.startswith("bj") or digits.startswith(("4", "8", "92")):
        return 0.30
    if digits.startswith(("300", "301", "688", "689")):
        return 0.20
    if "ST" in security_name:
        return 0.05
    return 0.10


def _a_share_breadth_from_spot(quotes: pd.DataFrame) -> dict[str, Any]:
    """Compute breadth from full A-share quotes using exchange-specific limit rules."""
    if quotes is None or quotes.empty:
        return {}
    code_col = _find_column(quotes, ["股票代码", "代码", "stock_code"])
    name_col = _find_column(quotes, ["股票名称", "名称", "name"])
    price_col = _find_column(quotes, ["最新价", "close", "lastPrice"])
    previous_close_col = _find_column(quotes, ["昨日收盘", "昨收", "pre_close", "lastClose"])
    amount_col = _find_column(quotes, ["成交额", "amount"])
    if not all((code_col, name_col, price_col, previous_close_col)):
        return {}

    advance = decline = flat = limit_up = limit_down = coverage = 0
    turnover = 0.0
    for _, row in quotes.iterrows():
        current = _number(row.get(price_col))
        previous = _number(row.get(previous_close_col))
        amount = _number(row.get(amount_col)) if amount_col else None
        if current is None or previous is None or current <= 0 or previous <= 0 or amount == 0:
            continue
        coverage += 1
        if amount is not None:
            turnover += amount
        if current > previous:
            advance += 1
        elif current < previous:
            decline += 1
        else:
            flat += 1
        ratio = _a_share_limit_ratio(row.get(code_col), row.get(name_col))
        upper = math.floor(previous * (1.0 + ratio) * 100.0 + 0.5) / 100.0
        lower = math.floor(previous * (1.0 - ratio) * 100.0 + 0.5) / 100.0
        if math.isclose(current, upper, abs_tol=0.005):
            limit_up += 1
        if math.isclose(current, lower, abs_tol=0.005):
            limit_down += 1
    if not coverage:
        return {}
    return {
        "advance": advance,
        "decline": decline,
        "flat": flat,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "turnover": turnover,
        "coverage": coverage,
    }


def _board_ranking_rows(board: pd.DataFrame, *, kind: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if board is None or board.empty:
        return [], []
    name_col = _find_column(board, ["板块名称", "板块", "名称", "name"])
    change_col = _find_column(board, ["涨跌幅", "pct_chg", "change_pct"])
    if not name_col or not change_col:
        return [], []
    rows = [
        {"name": str(row[name_col]), "change_pct": _round(_number(row[change_col])), "kind": kind}
        for _, row in board.iterrows()
    ]
    valid = [row for row in rows if row["change_pct"] is not None]
    valid.sort(key=lambda row: float(row["change_pct"]), reverse=True)
    return valid[:5], list(reversed(valid[-5:]))


def _append_board_rankings(sectors: dict[str, list[dict[str, Any]]], leaders: list[dict[str, Any]], laggards: list[dict[str, Any]]) -> None:
    """Keep industry and concept rankings visible rather than overwriting one with the other."""
    for key, rows in (("leaders", leaders), ("laggards", laggards)):
        existing = {(str(item.get("name")), str(item.get("kind"))) for item in sectors.get(key, [])}
        for row in rows:
            identity = (str(row.get("name")), str(row.get("kind")))
            if identity not in existing:
                sectors.setdefault(key, []).append(row)
                existing.add(identity)


def _tencent_cn_indexes() -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """Fetch A-share index daily bars from Tencent's public quote service.

    This direct endpoint is a resilient fallback when Eastmoney blocks local
    efinance/AkShare requests; it does not require an API key.
    """
    symbols = {"000001": "sh000001", "399001": "sz399001", "399006": "sz399006", "000300": "sh000300"}
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    as_of: str | None = None
    try:
        def fetch(symbol: str) -> tuple[str, list[Any]]:
            response = requests.get(
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                params={"param": f"{symbol},day,,,10,qfq"},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}, timeout=15,
            )
            response.raise_for_status()
            payload = response.json().get("data", {})
            return symbol, (payload.get(symbol) or {}).get("day") or []
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="tencent-index") as executor:
            daily_bars = dict(executor.map(fetch, symbols.values()))
        for code, symbol in symbols.items():
            bars = daily_bars.get(symbol) or []
            if not bars:
                continue
            closes = pd.Series([_number(bar[2]) for bar in bars], dtype="float64").dropna()
            latest = bars[-1]
            if closes.empty:
                continue
            as_of = str(latest[0]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(latest[0])) else as_of
            change_1d = _change(closes, 1)
            change_5d = _change(closes, 5)
            previous = _number(closes.iloc[-2]) if len(closes) > 1 else None
            open_value = _number(latest[1]) if len(latest) > 1 else None
            high_value = _number(latest[3]) if len(latest) > 3 else None
            low_value = _number(latest[4]) if len(latest) > 4 else None
            rows.append({
                "ticker": code, "name": _CN_INDEXES[code], "value": _round(float(closes.iloc[-1])),
                "1d_pct": _round(change_1d), "5d_pct": _round(change_5d),
                "trend": "走强" if (change_5d or 0) > 0 else "走弱" if (change_5d or 0) < 0 else "持平",
                "unit": "", "status": "ok", "previous_close": _round(previous, 4),
                "open": _round(open_value, 4), "high": _round(high_value, 4), "low": _round(low_value, 4),
                "amplitude_pct": _round((high_value - low_value) / previous * 100.0) if previous and high_value is not None and low_value is not None else None,
            })
    except Exception as exc:
        errors.append(f"Tencent指数：{type(exc).__name__}")
    return rows, as_of, errors


def _sina_cn_breadth() -> tuple[dict[str, Any], list[str]]:
    """Aggregate public Sina A-share quote pages as a no-key final fallback."""
    errors: list[str] = []
    try:
        page_count = min(70, max(1, int(os.environ.get("MARKET_RECAP_CN_SINA_PAGES", "60"))))
        base = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
        def fetch(page: int) -> list[dict[str, Any]]:
            response = requests.get(base, params={"page": page, "num": 100, "sort": "changepercent", "asc": 0, "node": "hs_a", "symbol": "", "_s_r_a": "page"}, headers=headers, timeout=12)
            response.raise_for_status()
            value = response.json()
            return value if isinstance(value, list) else []
        quotes: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="sina-a-share") as executor:
            for page_rows in executor.map(fetch, range(1, page_count + 1)):
                quotes.extend(page_rows)
        changes = pd.Series([_number(item.get("changepercent")) for item in quotes], dtype="float64").dropna()
        amounts = pd.Series([_number(item.get("amount")) for item in quotes], dtype="float64").dropna()
        if changes.empty:
            raise RuntimeError("Sina returned no A-share change data")
        return ({"advance": int((changes > 0).sum()), "decline": int((changes < 0).sum()), "flat": int((changes == 0).sum()), "limit_up": int((changes >= 9.8).sum()), "limit_down": int((changes <= -9.8).sum()), "turnover": _number(amounts.sum()), "coverage": int(len(changes))}, errors)
    except Exception as exc:
        errors.append(f"Sina市场宽度：{type(exc).__name__}")
        return {}, errors


def _cn_snapshot() -> dict[str, Any]:
    cached = _load_cached_snapshot("cn")
    if cached:
        return cached
    errors: list[str] = []
    direct_indexes, direct_as_of, direct_errors = _tencent_cn_indexes()
    errors.extend(direct_errors)
    indexes: list[dict[str, Any]] = list(direct_indexes)
    breadth: dict[str, Any] = {}
    sectors: dict[str, list[dict[str, Any]]] = {"leaders": [], "laggards": []}
    source_parts = ["Tencent Finance 指数"] if direct_indexes else []
    provider = os.environ.get("MARKET_RECAP_A_SHARE_PROVIDER", "auto").strip().lower()
    eastmoney_failed = False

    if provider in {"auto", "efinance"}:
        try:
            import efinance as ef  # type: ignore
            quotes = ef.stock.get_realtime_quotes()
            breadth = _a_share_breadth_from_spot(quotes)
            if breadth:
                source_parts.append("东方财富 efinance 全市场行情")
            else:
                raise RuntimeError("efinance quote response lacked complete A-share fields")
        except Exception as exc:
            eastmoney_failed = True
            errors.append(f"efinance 东方财富全市场行情：{type(exc).__name__}")

    if provider in {"auto", "akshare"}:
        try:
            import akshare as ak  # type: ignore
            if not breadth:
                try:
                    breadth = _a_share_breadth_from_spot(ak.stock_zh_a_spot_em())
                    if breadth:
                        source_parts.append("东方财富 AkShare 全市场行情")
                except Exception as exc:
                    eastmoney_failed = True
                    errors.append(f"akshare 东方财富全市场行情：{type(exc).__name__}")
            if not breadth:
                try:
                    breadth = _a_share_breadth_from_spot(ak.stock_zh_a_spot())
                    if breadth:
                        source_parts.append("Sina Finance 全市场行情（AkShare）")
                except Exception as exc:
                    errors.append(f"akshare 新浪全市场行情：{type(exc).__name__}")

            try:
                leaders, laggards = _board_ranking_rows(ak.stock_board_industry_name_em(), kind="行业（东方财富）")
                if leaders or laggards:
                    _append_board_rankings(sectors, leaders, laggards)
                    source_parts.append("东方财富行业板块（AkShare）")
            except Exception as exc:
                eastmoney_failed = True
                errors.append(f"akshare 东方财富行业板块：{type(exc).__name__}")
            if not sectors["leaders"] and not sectors["laggards"]:
                try:
                    leaders, laggards = _board_ranking_rows(ak.stock_sector_spot(indicator="行业"), kind="行业（Sina）")
                    if leaders or laggards:
                        _append_board_rankings(sectors, leaders, laggards)
                        source_parts.append("Sina Finance 行业板块（AkShare）")
                except Exception as exc:
                    errors.append(f"akshare 新浪行业板块：{type(exc).__name__}")
            try:
                leaders, laggards = _board_ranking_rows(ak.stock_board_concept_name_em(), kind="概念（东方财富）")
                if leaders or laggards:
                    _append_board_rankings(sectors, leaders, laggards)
                    source_parts.append("东方财富概念板块（AkShare）")
            except Exception as exc:
                eastmoney_failed = True
                errors.append(f"akshare 东方财富概念板块：{type(exc).__name__}")
                try:
                    leaders, laggards = _board_ranking_rows(ak.stock_sector_spot(indicator="概念"), kind="概念（Sina）")
                    if leaders or laggards:
                        _append_board_rankings(sectors, leaders, laggards)
                        source_parts.append("Sina Finance 概念板块（AkShare）")
                except Exception as fallback_exc:
                    errors.append(f"akshare 新浪概念板块：{type(fallback_exc).__name__}")
        except Exception as exc:
            errors.append(f"akshare 初始化：{type(exc).__name__}")

    if not breadth:
        sina_breadth, sina_errors = _sina_cn_breadth()
        breadth.update(sina_breadth)
        errors.extend(sina_errors)
        if sina_breadth:
            source_parts.append("Sina Finance 市场宽度（分页回退）")
    if eastmoney_failed and direct_indexes and breadth:
        errors = [
            "东方财富适配器当前不可用；已使用 " + "、".join(source_parts) + " 回退。"
        ] + [error for error in errors if not error.startswith(("efinance", "akshare"))]
    as_of = direct_as_of or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    snapshot = {"market": "cn", "market_name": "A股", "as_of_date": as_of, "source": " + ".join(source_parts) or "A股公开数据源", "indexes": indexes, "macro": [], "breadth": breadth, "sectors": sectors, "errors": errors}
    _save_cached_snapshot(snapshot)
    return snapshot


def _regime(snapshot: dict[str, Any]) -> dict[str, str]:
    if snapshot.get("market") == "us":
        sp = (snapshot.get("breadth") or {}).get("sp500") or {}
        ma50, ma200 = _number(sp.get("ma50")), _number(sp.get("ma200"))
        vix = next((item for item in snapshot.get("indexes", []) if item.get("ticker") == "^VIX"), {})
        vix_value = _number(vix.get("value"))
        if ma50 is not None and ma200 is not None and ma50 >= 60 and ma200 >= 55 and (vix_value is None or vix_value < 22):
            return {"label": "偏进攻", "reason": "中长期参与度较强，波动率未显示显著压力。"}
        if ma50 is not None and ma200 is not None and (ma50 < 40 or ma200 < 40 or (vix_value is not None and vix_value >= 28)):
            return {"label": "防守", "reason": "市场参与度或波动率显示风险偏好承压。"}
        return {"label": "均衡", "reason": "趋势与市场参与度尚未形成单边一致信号。"}
    breadth = snapshot.get("breadth") or {}
    advance, decline = _number(breadth.get("advance")), _number(breadth.get("decline"))
    if advance is not None and decline is not None and advance > decline * 1.4:
        return {"label": "偏进攻", "reason": "上涨家数明显占优，盘面参与度偏暖。"}
    if advance is not None and decline is not None and decline > advance * 1.4:
        return {"label": "防守", "reason": "下跌家数明显占优，盘面参与度偏弱。"}
    return {"label": "均衡", "reason": "涨跌家数未形成明显单边优势，宜等待确认。"}


_MARKET_NEWS_QUERIES = {
    "cn": (
        ("A股 大盘 收盘 复盘 市场热点", "market_close"),
        ("A股 今日 行业板块 涨停 盘后", "sector_rotation"),
        ("A股 政策 宏观 市场 盘后", "policy_macro"),
    ),
    "us": (
        ("US stock market close S&P 500 Nasdaq Dow VIX", "market_close"),
        ("US stock market Treasury yield dollar oil macro news", "policy_macro"),
        ("US stock market sector rotation technology earnings news", "sector_rotation"),
    ),
}
_MARKET_NEWS_KEYWORDS = {
    "cn": ("a股", "a-share", "中国股市", "中国股票", "上证", "深证", "创业板", "科创", "沪深", "中证", "股市", "证券", "板块", "涨停", "人民币"),
    "us": ("u.s. stock", "us stock", "wall street", "s&p", "nasdaq", "dow", "vix", "treasury", "federal reserve", "fed", "nyse", "american stock"),
}
_NEWS_REJECT_TERMS = (
    "bitcoin", "crypto", "cryptocurrency", "meme coin", "memecoin", "binance", "wallet", "nft",
    "best stocks", "next 10 years", "how to invest", "investing in tech stocks", "stock picks",
)
_HIGH_QUALITY_NEWS_DOMAINS = (
    "reuters.com", "bloomberg.com", "wsj.com", "ft.com", "cnbc.com", "finance.yahoo.com",
    "eastmoney.com", "cls.cn", "stcn.com", "caixin.com", "yicai.com", "thepaper.cn", "sina.com.cn",
)


def _market_recap_news_window_days() -> int:
    try:
        return max(1, min(14, int(os.environ.get("MARKET_RECAP_NEWS_MAX_AGE_DAYS", "3") or 3)))
    except ValueError:
        return 3


def _parse_market_news_date(value: Any, as_of_date: str) -> str | None:
    """Normalize absolute and relative provider dates against the report session."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        # Serper's relative values contain no clock time.  Treat the market
        # session as ending late in its as-of day so a same-session "4 hours
        # ago" result is not incorrectly labelled as the prior calendar day.
        reference = datetime.fromisoformat(as_of_date).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    except ValueError:
        reference = datetime.now(timezone.utc)
    relative = re.search(r"(\d+)\s*(minute|hour|day|week)s?\s+ago", raw, flags=re.I)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2).lower()
        seconds = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800}[unit] * amount
        return (reference - timedelta(seconds=seconds)).date().isoformat()
    parsed = pd.to_datetime(raw, errors="coerce", utc=True)
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).date().isoformat()


def _score_market_news_item(item: dict[str, Any], *, market: str, as_of_date: str, query: str, category: str = "market") -> dict[str, Any] | None:
    """Reject stale/generic search results before they can reach the recap LLM."""
    url = str(item.get("link") or item.get("url") or "").strip()
    parsed = urlparse(url)
    title = str(item.get("title") or "").strip()
    snippet = str(item.get("snippet") or "").strip()
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path in {"", "/"} or not title:
        return None
    combined = f"{title} {snippet}".casefold()
    if any(term in combined for term in _NEWS_REJECT_TERMS):
        return None
    published_date = _parse_market_news_date(item.get("date") or item.get("publishedDate") or item.get("published_date"), as_of_date)
    if published_date is None:
        return None
    try:
        age_days = (datetime.fromisoformat(as_of_date).date() - datetime.fromisoformat(published_date).date()).days
    except ValueError:
        return None
    if age_days < -1 or age_days > _market_recap_news_window_days():
        return None
    keywords = _MARKET_NEWS_KEYWORDS[market]
    market_hits = sum(keyword in combined for keyword in keywords)
    if not market_hits:
        return None
    query_hits = sum(token.casefold() in combined for token in re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", query))
    domain = parsed.netloc.casefold().removeprefix("www.")
    score = 40 + min(24, market_hits * 6) + min(12, query_hits * 3) + max(0, 12 - age_days * 4)
    trusted = any(domain == allowed or domain.endswith("." + allowed) for allowed in _HIGH_QUALITY_NEWS_DOMAINS)
    if trusted:
        score += 16
    return {
        "title": title[:240],
        "snippet": snippet[:600],
        "url": url,
        "source": domain,
        "market": market,
        "query": query,
        "category": category,
        "published_date": published_date,
        "source_quality": "tier_1" if trusted else "tier_3",
        "score": score,
    }


def _market_news_tokens(item: dict[str, Any]) -> set[str]:
    text = re.sub(r"\s+", " ", f"{item.get('title', '')} {item.get('snippet', '')}".casefold())
    latin = set(re.findall(r"[a-z0-9]{3,}", text))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    chinese_bigrams = {chinese[index:index + 2] for index in range(max(0, len(chinese) - 1))}
    return latin | chinese_bigrams


def _is_near_duplicate_market_news(candidate: dict[str, Any], selected: list[dict[str, Any]]) -> bool:
    candidate_tokens = _market_news_tokens(candidate)
    if not candidate_tokens:
        return False
    for prior in selected:
        prior_tokens = _market_news_tokens(prior)
        union = candidate_tokens | prior_tokens
        if union and len(candidate_tokens & prior_tokens) / len(union) >= 0.62:
            return True
    return False


def _select_diverse_market_news(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Reserve news slots across close, rotation, and policy instead of repeating one event."""
    selected: list[dict[str, Any]] = []
    seen_domains: set[str] = set()
    ranked = sorted(candidates, key=lambda item: (-int(item["score"]), item["published_date"], item["title"]))
    for category in ("market_close", "sector_rotation", "policy_macro"):
        for candidate in ranked:
            if candidate.get("category") != category or candidate.get("source") in seen_domains or _is_near_duplicate_market_news(candidate, selected):
                continue
            selected.append(candidate)
            seen_domains.add(str(candidate.get("source") or ""))
            break
    for candidate in ranked:
        if len(selected) >= limit:
            break
        if candidate.get("source") in seen_domains or _is_near_duplicate_market_news(candidate, selected):
            continue
        selected.append(candidate)
        seen_domains.add(str(candidate.get("source") or ""))
    return selected[:limit]


def _market_recap_article_limits() -> tuple[int, int, int]:
    try:
        max_urls = max(0, min(4, int(os.environ.get("MARKET_RECAP_NEWS_FETCH_MAX_URLS", "3") or 3)))
        timeout = max(3, min(20, int(os.environ.get("MARKET_RECAP_NEWS_FETCH_TIMEOUT_SECONDS", "8") or 8)))
        max_chars = max(400, min(4000, int(os.environ.get("MARKET_RECAP_NEWS_FETCH_MAX_CHARS", "1800") or 1800)))
        return max_urls, timeout, max_chars
    except ValueError:
        return 3, 8, 1800


def _enrich_market_news_with_articles(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reuse the stock-report SSRF-safe fetcher for a small, ranked evidence set."""
    max_urls, timeout, max_chars = _market_recap_article_limits()
    if not max_urls or not items:
        return items
    try:
        from daily_report.src.stock_daily_agent.article_fetcher import _fetch_article_text
    except Exception:
        return items

    selected = sorted(items, key=lambda item: int(item.get("score") or 0), reverse=True)[:max_urls]
    def fetch(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            return str(item["url"]), _fetch_article_text(str(item["url"]), timeout=timeout, max_chars=max_chars)
        except Exception as exc:
            return str(item["url"]), {"ok": False, "error": type(exc).__name__}
    with ThreadPoolExecutor(max_workers=min(3, len(selected)), thread_name_prefix="market-recap-news") as executor:
        records = dict(executor.map(fetch, selected))
    enriched: list[dict[str, Any]] = []
    for raw in items:
        item = dict(raw)
        article = records.get(str(item.get("url") or ""))
        if article and article.get("ok"):
            meta = re.sub(r"\s+", " ", str(article.get("meta_description") or "")).strip()
            text = re.sub(r"\s+", " ", str(article.get("text") or "")).strip()
            excerpt = (meta or text)[:1200]
            if excerpt:
                item["evidence_excerpt"] = excerpt
                item["article_fetch_ok"] = True
                item["article_text_quality_ok"] = bool(article.get("article_text_quality_ok"))
        enriched.append(item)
    return enriched


def _search_market_news(markets: list[str], snapshots: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Collect market-specific, recent news; never mix A-share and US recall pools."""
    limit = max(0, min(12, int(os.environ.get("MARKET_RECAP_NEWS_MAX_ITEMS", "6") or 6)))
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if not limit or not key:
        return []
    as_of_by_market = {str(item.get("market")): str(item.get("as_of_date") or _get_market_date()) for item in snapshots or []}
    per_market_limit = max(1, limit // max(1, len(markets)))
    accepted: dict[str, dict[str, Any]] = {}
    for market in markets:
        if market not in _MARKET_NEWS_QUERIES:
            continue
        as_of_date = as_of_by_market.get(market, _get_market_date())
        for query, category in _MARKET_NEWS_QUERIES[market]:
            try:
                response = requests.post(
                    "https://google.serper.dev/news",
                    headers={"X-API-KEY": key, "Content-Type": "application/json"},
                    json={"q": f"{query} {as_of_date}", "num": max(4, per_market_limit * 2)},
                    timeout=20,
                )
                response.raise_for_status()
                raw = response.json().get("news", [])
            except (requests.RequestException, ValueError, AttributeError):
                continue
            for item in raw if isinstance(raw, list) else []:
                candidate = _score_market_news_item(item, market=market, as_of_date=as_of_date, query=query, category=category)
                if candidate and (candidate["url"] not in accepted or candidate["score"] > accepted[candidate["url"]]["score"]):
                    accepted[candidate["url"]] = candidate
    ranked: list[dict[str, Any]] = []
    for market in markets:
        ranked.extend(_select_diverse_market_news([item for item in accepted.values() if item["market"] == market], per_market_limit))
    return _enrich_market_news_with_articles(ranked[:limit])


def _llm_settings() -> tuple[str, str, int, float]:
    provider = (os.environ.get("MARKET_RECAP_LLM_PROVIDER") or "inherit").strip().lower()
    if provider == "inherit":
        provider = (os.environ.get("LLM_PROVIDER") or "auto").strip().lower()
    if provider == "auto":
        provider = "deepseek" if os.environ.get("DEEPSEEK_API_KEY", "").strip() else "dashscope"
    model = (os.environ.get("MARKET_RECAP_LLM_MODEL") or os.environ.get("LLM_MODEL") or os.environ.get("QWEN_MODEL") or "qwen-plus").strip()
    timeout = max(5, int(os.environ.get("MARKET_RECAP_LLM_TIMEOUT_SECONDS", "60") or 60))
    temperature = max(0.0, min(1.0, float(os.environ.get("MARKET_RECAP_LLM_TEMPERATURE", "0.2") or 0.2)))
    return provider, model, timeout, temperature


def _call_llm(snapshot: list[dict[str, Any]], news: list[dict[str, Any]]) -> tuple[dict[str, dict[str, str]] | None, dict[str, Any]]:
    provider, model, timeout, temperature = _llm_settings()
    context = json.dumps({"markets": snapshot, "news": news}, ensure_ascii=False, separators=(",", ":"))
    prompt = (
        "你是受约束的市场复盘编辑。只根据 CONTEXT 的结构化数据和已筛选的来源标题/摘要/证据摘录进行中文解释；"
        "不可编造数值、新闻、因果或交易承诺，不得改变 deterministic_regime。"
        "news 中每条证据均包含 market、category、published_date、source、title、snippet，以及可选 evidence_excerpt；"
        "news_catalysts 只能引用这些证据，并将不同新闻分别归入市场走势、行业主题或宏观政策。"
        "只有标题、摘要或证据摘录明确支持时才可描述可能关联。若 news 为空，必须明确写‘未取得高相关、近期的市场新闻来源’，"
        "不能以常识或旧闻补全。数据表会单独显示数值，因此不要逐项复述表格；应解释结构、确认条件、分歧和失效条件。"
        "输出唯一 JSON 对象，结构必须为 {\"markets\": {\"cn\": {...}, \"us\": {...}}}；仅输出 CONTEXT 中存在的市场。"
        "每个市场对象必须包含 overview,index_structure,breadth_liquidity,funds_sentiment,macro_context,rotation,news_catalysts,next_session,risk_notes。"
        "每个市场对象只能使用其 market 相同的快照和新闻；A股对象不得借用美股的 VIX、利率或新闻，美股对象也不得借用 A股板块或新闻。"
        "每个字段用 2–4 句简体中文，建议 120–420 字；不得把 A股 与美股合写在同一市场对象中。若数据缺失，要明确写‘数据不可用’，不要补全。"
        "next_session 必须包含可观察的确认或失效条件，不能给出仓位百分比或确定性收益承诺。\nCONTEXT:\n" + context
    )
    try:
        from daily_report.src.stock_daily_agent.config import build_llm_cfg
        from qwen_agent.llm import get_chat_model
        cfg = build_llm_cfg(model, provider)
        cfg["generate_cfg"] = {**dict(cfg.get("generate_cfg") or {}), "temperature": temperature, "max_retries": 0, "max_input_tokens": 16000}
        def run() -> str:
            stream = get_chat_model(cfg).chat(messages=[{"role": "system", "content": "Return only strict JSON. No tools."}, {"role": "user", "content": prompt}], stream=True)
            final: Any = []
            for final in stream:
                pass
            message = final[-1] if isinstance(final, list) and final else final
            content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
            return str(content or "")
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-recap")
        future = executor.submit(run)
        try:
            raw = future.result(timeout=timeout)
        except FuturesTimeoutError as exc:
            future.cancel(); executor.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(f"market recap LLM timed out after {timeout}s") from exc
        executor.shutdown(wait=True)
        match = re.search(r"\{.*\}", raw, flags=re.S)
        parsed = json.loads(match.group(0) if match else raw)
        required = set(_RECAP_SECTION_KEYS)
        market_sections = parsed.get("markets") if isinstance(parsed, dict) else None
        requested_markets = {str(item.get("market") or "") for item in snapshot}
        if not isinstance(market_sections, dict) or not requested_markets or any(
            not isinstance(market_sections.get(market), dict) or not required.issubset(market_sections[market])
            for market in requested_markets
        ):
            raise ValueError("model response schema is invalid")
        return {
            market: {key: str(market_sections[market][key])[:2400] for key in required}
            for market in requested_markets
        }, {"used": True, "provider": provider, "model": model, "error": None}
    except Exception as exc:
        return None, {"used": False, "provider": provider, "model": model, "error": f"{type(exc).__name__}: {exc}"}


def _fallback_sections(snapshots: list[dict[str, Any]], news: list[dict[str, str]]) -> dict[str, str]:
    labels = "；".join(f"{item['market_name']}：{item['deterministic_regime']['label']}" for item in snapshots)
    index_facts = []
    breadth_facts = []
    macro_facts = []
    rotation_facts = []
    sentiment_facts = []
    for snapshot in snapshots:
        market = snapshot.get("market_name", "市场")
        rows = [f"{item.get('name')} {_format_value(item)}（1D {_pct(item.get('1d_pct'))}，5D {_pct(item.get('5d_pct'))}）" for item in snapshot.get("indexes", []) if item.get("status") == "ok"]
        if rows:
            index_facts.append(f"{market}：" + "；".join(rows))
        breadth = snapshot.get("breadth") or {}
        if snapshot.get("market") == "us":
            sp, ndx = breadth.get("sp500") or {}, breadth.get("nasdaq100") or {}
            breadth_facts.append(f"{market}：标普500 MA20/50/200 上方比例 {_human_value(sp.get('ma20'))}/{_human_value(sp.get('ma50'))}/{_human_value(sp.get('ma200'))}；纳指100为 {_human_value(ndx.get('ma20'))}/{_human_value(ndx.get('ma50'))}/{_human_value(ndx.get('ma200'))}。")
        elif breadth:
            breadth_facts.append(f"{market}：上涨/下跌/平盘 {breadth.get('advance', '数据不可用')}/{breadth.get('decline', '数据不可用')}/{breadth.get('flat', '数据不可用')}，涨停/跌停 {breadth.get('limit_up', '数据不可用')}/{breadth.get('limit_down', '数据不可用')}，覆盖 {breadth.get('coverage', '数据不可用')} 只。")
            sentiment_facts.append(f"{market}：成交额 {_human_value(breadth.get('turnover'))}；涨停-跌停差为 {(_number(breadth.get('limit_up')) or 0) - (_number(breadth.get('limit_down')) or 0):.0f}。")
        if snapshot.get("market") == "us":
            vix = next((item for item in snapshot.get("indexes", []) if item.get("ticker") == "^VIX"), {})
            if vix:
                sentiment_facts.append(f"{market}：VIX {_format_value(vix)}，1D {_pct(vix.get('1d_pct'))}。")
        macro = [f"{item.get('name')} {_format_value(item)}（1D {_pct(item.get('1d_pct'))}，5D {_pct(item.get('5d_pct'))}）" for item in snapshot.get("macro", []) if item.get("status") == "ok"]
        if macro:
            macro_facts.append("；".join(macro))
        leaders = (snapshot.get("sectors") or {}).get("leaders") or []
        if leaders:
            rotation_facts.append(f"{market}领涨：" + "、".join(f"{item.get('name')}（{_pct(item.get('change_pct'))}）" for item in leaders[:5]))
    return {
        "overview": f"本次复盘覆盖{labels}。结论来自系统计算的指数、市场宽度和可用的宏观数据，不构成投资建议。",
        "index_structure": "；".join(index_facts) or "指数数据不可用。",
        "breadth_liquidity": " ".join(breadth_facts) or "市场宽度或流动性数据不可用，不能据此判断市场参与度。",
        "funds_sentiment": " ".join(sentiment_facts) or "资金与情绪的可用代理数据有限；不能把单日涨跌替代为资金流结论。",
        "macro_context": ("；".join(macro_facts) + "。这些指标仅作为利率、通胀/能源和美元流动性背景，不构成自动交易信号。") if macro_facts else "跨资产宏观数据不可用。",
        "rotation": "；".join(rotation_facts) if rotation_facts else "行业/概念轮动数据不可用；不应以单一个股涨跌替代板块结论。",
        "news_catalysts": (
            "已验证来源："
            + "；".join(
                f"[{ {'us': '美股', 'cn': 'A股'}.get(str(item.get('market')), '市场') } "
                f"{item.get('published_date', '日期未提供')}] {item.get('title', '')}"
                for item in news[:4]
            )
        ) if news else "本次未取得高相关、近期的市场新闻来源，消息面部分保持空白。",
        "next_session": "下一交易日重点观察指数趋势是否得到市场宽度、成交/流动性和领涨板块持续性的共同确认。",
        "risk_notes": "行情与外部数据可能延迟或修订；报告仅供研究参考，不构成投资建议。",
    }


def _format_value(row: dict[str, Any]) -> str:
    value = row.get("value")
    if value is None:
        return "数据不可用"
    suffix = row.get("unit") or ""
    return f"{value:,.2f}{suffix}"


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    return render_report_table(headers, rows, scroll=True)


_BREADTH_LABELS = {
    "advance": "上涨家数",
    "decline": "下跌家数",
    "flat": "平盘家数",
    "limit_up": "涨停家数",
    "limit_down": "跌停家数",
    "turnover": "成交额",
    "coverage": "覆盖标的数",
    "sp500": "标普 500 成分股",
    "nasdaq100": "纳斯达克 100 成分股",
}


def _breadth_rows(snapshot: dict[str, Any]) -> list[list[str]]:
    """Present computed breadth as a compact, auditable table rather than prose."""
    breadth = snapshot.get("breadth") or {}
    if snapshot.get("market") == "us":
        rows = []
        for key in ("sp500", "nasdaq100"):
            values = breadth.get(key)
            if not isinstance(values, dict):
                continue
            rows.append([
                _BREADTH_LABELS[key],
                _human_value(values.get("ma20")),
                _pct(values.get("ma20_delta_1d")),
                _human_value(values.get("ma50")),
                _pct(values.get("ma50_delta_1d")),
                _human_value(values.get("ma200")),
                _pct(values.get("ma200_delta_1d")),
            ])
        return rows

    rows = []
    for key in ("advance", "decline", "flat", "limit_up", "limit_down", "coverage", "turnover"):
        if key not in breadth:
            continue
        value = _human_value(breadth.get(key))
        if key == "turnover" and value != "数据不可用":
            value += " 元"
        rows.append([_BREADTH_LABELS[key], value])
    advance, decline = _number(breadth.get("advance")), _number(breadth.get("decline"))
    if advance is not None and decline is not None and advance + decline > 0:
        rows.append(["上涨占比（不含平盘）", f"{advance / (advance + decline) * 100:.1f}%"])
    return rows


def _model_interpretation(sections: dict[str, Any], key: str) -> str:
    """Keep data tables authoritative and show only non-duplicated narrative here."""
    text = str(sections.get(key, "数据不可用")).strip()
    marker = "模型解读："
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    return text or "数据不可用"


def _safe_http_url(value: Any) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return ""
    return str(value).strip() if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else ""


def _regime_value_class(label: Any) -> str:
    return {"偏进攻": "up", "防守": "down", "均衡": "warn"}.get(str(label), "")


def _render_market_recap_header(payload: dict[str, Any]) -> str:
    """Use the same visual header contract as the AI Stock/Portfolio reports."""
    llm = payload.get("llm") or {}
    provider = str(llm.get("provider") or "deterministic")
    model = str(llm.get("model") or "—")
    model_status = "模型解读已启用" if llm.get("used") else "确定性数据报告"
    return (
        '<div class="header">'
        '<div class="logo">SWL</div>'
        '<div class="header-info">'
        f'<h1>{esc(payload.get("title", "大盘复盘"))}</h1>'
        '<div class="subtitle">Market Recap · Stock Watch List</div>'
        '</div>'
        '<div class="header-badge">'
        f'<span class="pill">生成时间 {esc(payload.get("generated_at", ""))}</span>'
        f'<span class="pill brand">{esc(model_status)}</span>'
        f'<span class="pill">LLM {esc(provider)} · {esc(model)}</span>'
        '</div></div>'
    )


_RECAP_SECTION_KEYS = (
    "overview", "index_structure", "breadth_liquidity", "funds_sentiment",
    "macro_context", "rotation", "news_catalysts", "next_session", "risk_notes",
)


def _sections_for_market(sections: dict[str, Any], market: str) -> dict[str, Any]:
    """Read new per-market sections while rendering older flat payloads safely."""
    nested = sections.get(market) if isinstance(sections, dict) else None
    return nested if isinstance(nested, dict) else sections


def _market_source_items(news: list[dict[str, Any]], market: str) -> list[dict[str, Any]]:
    market_label = {"us": "美股", "cn": "A股"}.get(market, "市场")
    source_items: list[dict[str, Any]] = []
    for item in news:
        if str(item.get("market") or "") != market:
            continue
        url = _safe_http_url(item.get("url"))
        source_items.append({
            "title": str(item.get("title") or "来源"),
            "url": url,
            "source_name": str(item.get("source") or ""),
            "summary_zh": str(item.get("evidence_excerpt") or item.get("snippet") or "")[:700],
            "published_date": str(item.get("published_date") or "日期未提供"),
            "event_type": f"{market_label} 市场新闻",
            "relevance_reason": (
                f"市场：{market_label}；类别：{str(item.get('category') or '市场')}；"
                f"检索主题：{str(item.get('query') or '市场复盘')}；"
                f"证据：{'文章正文/摘要' if item.get('article_fetch_ok') else '搜索摘要'}。"
            ),
            "source_verified": bool(url),
            "source_quality": str(item.get("source_quality") or "tier_3"),
            "impact_direction": "neutral",
            "impact_horizon": "short",
            "evidence_id": f"market-news-{market}",
        })
    return source_items


def _render_market_analysis_chapter(snapshot: dict[str, Any], all_sections: dict[str, Any], news: list[dict[str, Any]]) -> str:
    market = str(snapshot.get("market") or "")
    label = str(snapshot.get("market_name") or "市场")
    sections = _sections_for_market(all_sections, market)
    interpretation_rows = [
        ["盘面总览", _model_interpretation(sections, "overview")],
        ["指数结构", _model_interpretation(sections, "index_structure")],
        ["宽度与流动性", _model_interpretation(sections, "breadth_liquidity")],
        ["资金与情绪", _model_interpretation(sections, "funds_sentiment")],
        ["跨资产环境", _model_interpretation(sections, "macro_context")],
        ["行业 / 主题轮动", _model_interpretation(sections, "rotation")],
    ]
    interpretation = render_section(
        f"{label}市场解读",
        "🧭",
        "<p class='kpi-sub'>仅解释本市场的结构化快照；数值以数据表为准。</p>"
        + _render_table(["维度", "解读"], interpretation_rows),
    )
    source_items = _market_source_items(news, market)
    news_body = f"<p class='kpi-sub'>{esc(_model_interpretation(sections, 'news_catalysts'))}</p>"
    news_body += "<p class='kpi-sub'>仅展示本市场通过归属、日期与相关性筛选的联网来源。</p>"
    news_body += render_news_group(f"{label}可验证市场来源", source_items) or "<p class='kpi-sub'>未取得可验证的近期市场新闻来源。</p>"
    news_section = render_section(f"{label}消息面与催化", "📰", news_body)
    action = render_section(
        f"{label}下一交易日框架",
        "🎯",
        _render_table(
            ["项目", "内容"],
            [["关注重点", _model_interpretation(sections, "next_session")], ["风险提示", _model_interpretation(sections, "risk_notes")]],
        ),
    )
    return interpretation + news_section + action


def render_market_recap_html(payload: dict[str, Any]) -> str:
    snapshots = payload.get("snapshots") or []
    sections = payload.get("sections") or {}
    kpi_cards: list[dict[str, str]] = []
    detail: list[str] = []
    for snapshot in snapshots:
        regime = snapshot.get("deterministic_regime") or {}
        kpi_cards.append({
            "label": str(snapshot.get("market_name", "市场")),
            "value": str(regime.get("label", "均衡")),
            "sub": f"交易日 {snapshot.get('as_of_date', '未知')} · {regime.get('reason', '')}",
            "value_cls": _regime_value_class(regime.get("label")),
        })
        has_session_fields = any(item.get("open") is not None for item in snapshot.get("indexes", []))
        index_headers = ["名称", "最新值", "1D", "5D", "趋势"]
        if has_session_fields:
            index_headers = ["名称", "最新值", "1D", "开盘", "最高", "最低", "振幅", "5D", "趋势"]
            index_rows = [[
                item.get("name", item.get("ticker", "")), _format_value(item), _pct(item.get("1d_pct")),
                _human_value(item.get("open")), _human_value(item.get("high")), _human_value(item.get("low")),
                _pct(item.get("amplitude_pct")), _pct(item.get("5d_pct")), item.get("trend", ""),
            ] for item in snapshot.get("indexes", [])]
        else:
            index_rows = [[item.get("name", item.get("ticker", "")), _format_value(item), _pct(item.get("1d_pct")), _pct(item.get("5d_pct")), item.get("trend", "")] for item in snapshot.get("indexes", [])]
        macro_rows = [[item.get("name", item.get("ticker", "")), _format_value(item), _pct(item.get("1d_pct")), _pct(item.get("5d_pct")), item.get("trend", "")] for item in snapshot.get("macro", [])]
        breadth_rows = _breadth_rows(snapshot)
        leader_rows = [[item.get("name", ""), _pct(item.get("change_pct")), item.get("kind", "行业")] for item in (snapshot.get("sectors") or {}).get("leaders", [])]
        laggard_rows = [[item.get("name", ""), _pct(item.get("change_pct")), item.get("kind", "行业")] for item in (snapshot.get("sectors") or {}).get("laggards", [])]
        breadth_headers = ["范围", "MA20 上方", "1D变化", "MA50 上方", "1D变化", "MA200 上方", "1D变化"] if snapshot.get("market") == "us" else ["指标", "数值"]
        unavailable_html = "<p class='kpi-sub'>数据不可用</p>"
        breadth_html = _render_table(breadth_headers, breadth_rows) if breadth_rows else unavailable_html
        macro_html = _render_table(["指标", "最新值", "1D", "5D", "趋势"], macro_rows) if macro_rows else unavailable_html
        leader_html = _render_table(["名称", "涨跌幅", "类别"], leader_rows) if leader_rows else unavailable_html
        laggard_html = _render_table(["名称", "涨跌幅", "类别"], laggard_rows) if laggard_rows else unavailable_html
        index_html = _render_table(index_headers, index_rows) if index_rows else unavailable_html
        sector_html = (
            "<div class='risk-grid'>"
            f"<div><h3>领涨板块 / 主题</h3>{leader_html}</div>"
            f"<div><h3>领跌板块 / 主题</h3>{laggard_html}</div>"
            "</div>"
        )
        snapshot_body = (
            "<h3>主要指数</h3>"
            f"{index_html}"
            "<h3>市场宽度与流动性</h3>"
            f"{breadth_html}"
            f"<h3>跨资产宏观环境</h3>{macro_html}"
            f"{sector_html}"
        )
        detail.append(
            render_section(f"{snapshot.get('market_name', '市场')}数据快照", "📊", snapshot_body)
            + _render_market_analysis_chapter(snapshot, sections, list(payload.get("news") or []))
        )

    errors = [error for snapshot in snapshots for error in snapshot.get("errors", [])]
    errors_body = (
        '<ul class="trigger-list">' + "".join(f"<li>{esc(error)}</li>" for error in errors) + "</ul>"
        if errors else "<p class='kpi-sub'>本次数据源未报告额外限制。</p>"
    )
    return (
        render_html_head(str(payload.get("title") or "大盘复盘"))
        + _render_market_recap_header(payload)
        + render_kpi_cards(kpi_cards, cols=3)
        + '<div class="data-cutoff">数据快照以各市场的最近完整交易日为准；数值表优先于模型解读。</div>'
        + "".join(detail)
        + render_section("数据质量与限制", "🛡️", errors_body)
        + render_disclaimer("本报告仅供研究参考，不构成投资建议。")
        + "</div></body></html>"
    )


def _pct(value: Any) -> str:
    number = _number(value)
    return "数据不可用" if number is None else f"{number:+.2f}%"


def _human_value(value: Any) -> str:
    number = _number(value)
    return f"{number:,.2f}" if number is not None else "数据不可用"


def generate_market_recap(*, markets: str | Iterable[str] | None = None, user_scope: str = "guest") -> dict[str, Any]:
    del user_scope  # Public market data and snapshots are intentionally global.
    started = time.perf_counter()
    if not market_recap_enabled():
        return {"success": False, "report_kind": "market_recap", "error": "Market recap is disabled by MARKET_RECAP_ENABLED."}
    selected = normalize_markets(markets)
    snapshots = [_us_snapshot() if item == "us" else _cn_snapshot() for item in selected]
    for snapshot in snapshots:
        snapshot["deterministic_regime"] = _regime(snapshot)
    news = _search_market_news(selected, snapshots)
    deterministic_sections = {
        str(snapshot.get("market")): _fallback_sections(
            [snapshot], [item for item in news if str(item.get("market") or "") == str(snapshot.get("market") or "")]
        )
        for snapshot in snapshots
    }
    llm_sections, llm = _call_llm(snapshots, news)
    if llm_sections is None:
        sections = deterministic_sections
    else:
        # Preserve computed facts for each market independently; a model cannot
        # use A-share evidence to overwrite a US-market factual baseline (or vice versa).
        sections = {
            market: {
                key: deterministic_sections[market][key] + "\n\n模型解读：" + llm_sections[market][key]
                for key in deterministic_sections[market]
            }
            for market in deterministic_sections
        }
    as_of = "_".join(snapshot.get("as_of_date", "unknown") for snapshot in snapshots)
    title = market_subject_name(selected)
    payload = {"title": title, "generated_at": datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d %H:%M %Z"), "snapshots": snapshots, "news": news, "sections": sections, "llm": llm}
    safe_date = re.sub(r"[^0-9_-]+", "_", as_of)
    return {"success": True, "report_kind": "market_recap", "markets": selected, "report_date": max((snapshot.get("as_of_date", "") for snapshot in snapshots), default=_get_market_date()), "file_name": f"market_recap_{safe_date}.html", "html_bytes": render_market_recap_html(payload).encode("utf-8"), "elapsed": time.perf_counter() - started, "payload": payload, "warnings": [llm["error"]] if llm.get("error") else []}


def generate_market_recap_for_job(job: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(job.get("payload_json") or "{}")
    except (TypeError, ValueError):
        payload = {}
    result = generate_market_recap(markets=payload.get("markets"), user_scope=str(job.get("owner_key") or "guest"))
    if result.get("success") and job.get("schedule_id"):
        result["skip_delivery"] = not _recap_delivery_state(str(job.get("schedule_id")), result.get("payload", {}).get("snapshots", []))
    return result
