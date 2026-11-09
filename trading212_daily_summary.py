"""Daily Trading 212 account brief with bounded web-aware AI commentary.

The minute-level monitor stays responsible for alerts.  This module runs at
most once per account/day, derives all monetary facts in Python, then permits
one optional DashScope web-search synthesis to explain a compact, selected set
of holdings.  It never exposes credentials to a model or persists them here.
"""

from __future__ import annotations

import datetime as dt
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

import trading212_monitor as monitor
from daily_report.src.stock_daily_agent.config import (
    dashscope_compatible_model_server,
    dashscope_native_api_server,
    uses_dashscope_responses_api,
)


SUMMARY_RETENTION_DAYS = 90
DEFAULT_HOUR = 22
DEFAULT_MINUTE = 15
MAX_FOCUS_HOLDINGS = 5
MAX_TELEGRAM_MESSAGE = 3800


def _iso(now: dt.datetime) -> str:
    return now.astimezone(monitor.UTC).replace(microsecond=0).isoformat()


def _safe_error(exc: Exception | str) -> str:
    return monitor._safe_error(exc)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_summary_settings (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            hour INTEGER NOT NULL DEFAULT 22,
            minute INTEGER NOT NULL DEFAULT 15,
            include_ai INTEGER NOT NULL DEFAULT 1,
            include_news INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            last_success_at TEXT,
            last_error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_summary_runs (
            user_id INTEGER NOT NULL,
            summary_date TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ai_json TEXT NOT NULL DEFAULT '{}',
            sent_at TEXT,
            error TEXT,
            PRIMARY KEY(user_id, summary_date)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_summary_runs_user_date "
        "ON daily_summary_runs(user_id, summary_date DESC)"
    )


def _connect(path: Path | None = None) -> sqlite3.Connection:
    conn = monitor._connect(path)
    _ensure_schema(conn)
    return conn


def _settings_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {
            "enabled": False, "hour": DEFAULT_HOUR, "minute": DEFAULT_MINUTE,
            "include_ai": True, "include_news": True, "last_success_at": None,
            "last_error": None,
        }
    return {
        "enabled": bool(row["enabled"]), "hour": int(row["hour"]), "minute": int(row["minute"]),
        "include_ai": bool(row["include_ai"]), "include_news": bool(row["include_news"]),
        "last_success_at": row["last_success_at"], "last_error": row["last_error"],
    }


def get_daily_summary_settings(user_id: int, *, path: Path | None = None) -> dict[str, Any]:
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM daily_summary_settings WHERE user_id=?", (int(user_id),)).fetchone()
        return _settings_from_row(row)
    finally:
        conn.close()


def save_daily_summary_settings(
    user_id: int,
    *,
    enabled: bool,
    hour: int,
    minute: int,
    include_ai: bool,
    include_news: bool,
    path: Path | None = None,
) -> dict[str, Any]:
    try:
        hour, minute = int(hour), int(minute)
    except (TypeError, ValueError) as exc:
        raise ValueError("Daily summary time must be a valid hour and minute.") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Daily summary time is outside the valid 24-hour range.")
    conn = _connect(path)
    try:
        account = conn.execute("SELECT * FROM monitor_accounts WHERE user_id=?", (int(user_id),)).fetchone()
        if enabled and (account is None or not account["api_key_enc"] or not account["api_secret_enc"]):
            raise ValueError("Save Trading 212 credentials before enabling the daily account summary.")
        if enabled and (account is None or not account["telegram_bot_token_enc"] or not account["telegram_chat_id_enc"]):
            raise ValueError("Save Telegram settings before enabling the daily account summary.")
        previous = conn.execute("SELECT * FROM daily_summary_settings WHERE user_id=?", (int(user_id),)).fetchone()
        conn.execute(
            """
            INSERT INTO daily_summary_settings(
                user_id, enabled, hour, minute, include_ai, include_news, updated_at, last_success_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled=excluded.enabled, hour=excluded.hour, minute=excluded.minute,
                include_ai=excluded.include_ai, include_news=excluded.include_news, updated_at=excluded.updated_at
            """,
            (
                int(user_id), int(bool(enabled)), hour, minute, int(bool(include_ai)), int(bool(include_news)),
                _iso(dt.datetime.now(tz=monitor.UTC)),
                previous["last_success_at"] if previous else None,
                previous["last_error"] if previous else None,
            ),
        )
        return _settings_from_row(conn.execute(
            "SELECT * FROM daily_summary_settings WHERE user_id=?", (int(user_id),)
        ).fetchone())
    finally:
        conn.close()


def get_daily_summary_history(user_id: int, *, limit: int = 10, path: Path | None = None) -> list[dict[str, Any]]:
    conn = _connect(path)
    try:
        rows = conn.execute(
            """SELECT summary_date, generated_at, status, sent_at, error, payload_json, ai_json
               FROM daily_summary_runs WHERE user_id=? ORDER BY summary_date DESC LIMIT ?""",
            (int(user_id), max(1, min(int(limit), 30))),
        ).fetchall()
        result = []
        for row in rows:
            payload = _json_object(row["payload_json"])
            ai = _json_object(row["ai_json"])
            result.append({
                "summary_date": row["summary_date"], "generated_at": row["generated_at"], "status": row["status"],
                "sent_at": row["sent_at"], "error": row["error"],
                "account_value": payload.get("account", {}).get("total_value"),
                "currency": payload.get("account", {}).get("currency"),
                "headline": ai.get("headline"),
            })
        return result
    finally:
        conn.close()


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def fetch_account_summary(
    api_key: str, api_secret: str, environment: str, *, request_get: Callable[..., Any] = requests.get,
) -> dict[str, Any]:
    timeout = max(3, int(os.environ.get("TRADING212_REQUEST_TIMEOUT_SECONDS", "20")))
    try:
        response = request_get(
            f"{monitor._base_url(environment)}/equity/account/summary",
            auth=(api_key, api_secret), timeout=timeout, headers={"Accept": "application/json"},
        )
    except requests.RequestException as exc:
        raise monitor.Trading212APIError(f"Trading 212 account summary failed: {type(exc).__name__}") from exc
    if response.status_code == 401:
        raise monitor.Trading212APIError("Trading 212 rejected the configured read-only credentials.")
    if response.status_code == 429:
        raise monitor.Trading212APIError("Trading 212 rate limit reached; daily summary will retry tomorrow.")
    if response.status_code >= 400:
        raise monitor.Trading212APIError(f"Trading 212 account summary failed (HTTP {response.status_code}).")
    try:
        payload = response.json()
    except ValueError as exc:
        raise monitor.Trading212APIError("Trading 212 account summary returned an invalid response.") from exc
    if not isinstance(payload, dict):
        raise monitor.Trading212APIError("Trading 212 account summary had an unexpected format.")
    return payload


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _first_number(source: Any, *keys: str) -> float | None:
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = _number(source.get(key))
        if value is not None:
            return value
    return None


def _account_fields(raw: dict[str, Any]) -> dict[str, Any]:
    cash = raw.get("cash") if isinstance(raw.get("cash"), dict) else {}
    cash_value = _first_number(cash, "availableToTrade", "free", "available", "total")
    if cash_value is None:
        cash_value = _first_number(raw, "cash")
    return {
        "currency": str(raw.get("currency") or cash.get("currency") or "").upper()[:12],
        "total_value": _first_number(raw, "totalValue", "total", "accountValue"),
        "cash": cash_value,
        "unrealized_profit_loss": _first_number(raw.get("investments"), "unrealizedProfitLoss")
        or _first_number(raw, "unrealizedProfitLoss"),
    }


def _baseline_for_position(
    conn: sqlite3.Connection, user_id: int, position_key: str, day_start: dt.datetime,
) -> sqlite3.Row | None:
    # Prefer the final snapshot before the current Berlin report day.  It is a
    # clean continuity baseline; if unavailable, use the earliest same-day
    # snapshot and explicitly label the result as an intraday estimate.
    before = conn.execute(
        """SELECT current_price, current_value, quantity, captured_at FROM position_snapshots
           WHERE user_id=? AND position_key=? AND captured_at < ?
           ORDER BY captured_at DESC LIMIT 1""",
        (int(user_id), position_key, _iso(day_start)),
    ).fetchone()
    if before is not None:
        return before
    return conn.execute(
        """SELECT current_price, current_value, quantity, captured_at FROM position_snapshots
           WHERE user_id=? AND position_key=? AND captured_at >= ?
           ORDER BY captured_at ASC LIMIT 1""",
        (int(user_id), position_key, _iso(day_start)),
    ).fetchone()


def _daily_signals(conn: sqlite3.Connection, user_id: int, day_start: dt.datetime) -> dict[str, list[str]]:
    rows = conn.execute(
        """SELECT position_key, signal_key, last_triggered_at FROM technical_signal_state
           WHERE user_id=? AND last_triggered_at IS NOT NULL AND last_triggered_at >= ?""",
        (int(user_id), _iso(day_start)),
    ).fetchall()
    found: dict[str, list[str]] = {}
    for row in rows:
        found.setdefault(str(row["position_key"]), []).append(str(row["signal_key"]))
    return found


def build_daily_account_snapshot(
    conn: sqlite3.Connection, user_id: int, positions: list[dict[str, Any]], account_raw: dict[str, Any], now: dt.datetime,
) -> dict[str, Any]:
    local = now.astimezone(monitor.BERLIN_TZ)
    day_start = dt.datetime.combine(local.date(), dt.time.min, tzinfo=monitor.BERLIN_TZ).astimezone(monitor.UTC)
    signals = _daily_signals(conn, user_id, day_start)
    holdings = []
    for position in positions:
        baseline = _baseline_for_position(conn, user_id, position["position_key"], day_start)
        current_price = float(position["current_price"])
        current_value = _number(position.get("current_value"))
        baseline_price = _number(baseline["current_price"]) if baseline is not None else None
        baseline_value = _number(baseline["current_value"]) if baseline is not None else None
        change_pct = ((current_price - baseline_price) / baseline_price * 100) if baseline_price and baseline_price > 0 else None
        value_change = current_value - baseline_value if current_value is not None and baseline_value is not None else None
        holdings.append({
            # The verified mapping is a more reliable public/search identity
            # than a stale broker instrument name (for example after a rename).
            # The raw Trading 212 key stays in the database and is used only
            # for snapshot/state joins.
            "ticker": monitor.mapped_yfinance_ticker(
                conn, position["position_key"], fallback=position["ticker"],
            ),
            "broker_ticker": position["ticker"],
            "name": position.get("display_name") or position["ticker"],
            "currency": position.get("currency") or "", "price": current_price,
            "quantity": _number(position.get("quantity")), "current_value": current_value,
            "baseline_price": baseline_price, "change_pct": change_pct, "value_change": value_change,
            "signals": signals.get(position["position_key"], []),
            "baseline_at": baseline["captured_at"] if baseline is not None else None,
        })
    known_values = [float(item["current_value"]) for item in holdings if item["current_value"] is not None]
    total_positions = sum(known_values) if known_values else None
    if total_positions and total_positions > 0:
        for item in holdings:
            item["weight_pct"] = (float(item["current_value"]) / total_positions * 100) if item["current_value"] is not None else None
    else:
        for item in holdings:
            item["weight_pct"] = None
    largest = sorted(holdings, key=lambda item: float(item.get("weight_pct") or 0), reverse=True)
    movers = sorted([item for item in holdings if item.get("change_pct") is not None], key=lambda item: abs(float(item["change_pct"])), reverse=True)
    focus: list[dict[str, Any]] = []
    for item in sorted(holdings, key=lambda row: (bool(row.get("signals")), abs(float(row.get("change_pct") or 0)), float(row.get("weight_pct") or 0)), reverse=True):
        if item not in focus:
            focus.append(item)
        if len(focus) >= MAX_FOCUS_HOLDINGS:
            break
    account = _account_fields(account_raw)
    return {
        "as_of": local.isoformat(), "summary_date": local.date().isoformat(), "timezone": "Europe/Berlin",
        "account": {**account, "tracked_position_value": total_positions, "position_count": len(holdings)},
        "holdings": holdings, "largest_holdings": largest[:5], "largest_movers": movers[:5], "focus_holdings": focus,
        "data_limitations": [
            "Daily change is calculated from the most recent stored position snapshot before this Berlin report day; it is an estimate when cash flows or intraday position changes occurred.",
            "AI commentary may only interpret the supplied account data and returned web-search sources; it does not place orders.",
        ],
    }


def _resolve_llm_settings() -> tuple[str, str, int]:
    provider = (os.environ.get("TRADING212_DAILY_SUMMARY_LLM_PROVIDER") or "inherit").strip().lower()
    if provider == "inherit":
        provider = (os.environ.get("LLM_PROVIDER") or "dashscope").strip().lower()
    model = (os.environ.get("TRADING212_DAILY_SUMMARY_LLM_MODEL") or "").strip()
    if not model:
        model = (os.environ.get("QWEN_MODEL") or "").strip()
    timeout = max(10, min(180, int(os.environ.get("TRADING212_DAILY_SUMMARY_LLM_TIMEOUT_SECONDS", "60"))))
    return provider, model, timeout


def _safe_url(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return text
    return "" if (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    ) else text


def _parse_json_object(text: str) -> dict[str, Any]:
    values = [str(text or "").strip()]
    raw = values[0]
    values.append(re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw, flags=re.IGNORECASE).strip())
    first, last = raw.find("{"), raw.rfind("}")
    if first >= 0 and last > first:
        values.append(raw[first:last + 1])
    for candidate in values:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Daily summary model did not return a JSON object.")


def _response_value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _response_text_and_sources(response: Any) -> tuple[str, list[dict[str, Any]]]:
    text = str(_response_value(response, "output_text", "") or "").strip()
    sources: list[dict[str, Any]] = []
    for item in _response_value(response, "output", []) or []:
        item_type = str(_response_value(item, "type", "") or "")
        if item_type == "web_search_call":
            action = _response_value(item, "action", {}) or {}
            for source in _response_value(action, "sources", []) or []:
                sources.append({
                    "title": str(_response_value(source, "title", "") or ""),
                    "url": _safe_url(_response_value(source, "url", "")),
                    "date": str(_response_value(source, "date", "") or ""),
                })
        elif item_type == "message" and not text:
            for block in _response_value(item, "content", []) or []:
                block_type = str(_response_value(block, "type", "") or "")
                if block_type in {"output_text", "text"}:
                    text += str(_response_value(block, "text", "") or "")
    deduped = []
    seen = set()
    for source in sources:
        if source["url"] and source["url"] not in seen:
            seen.add(source["url"])
            deduped.append(source)
    return text, deduped[:10]


def _compact_ai_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    account = dict(snapshot.get("account") or {})
    keep_account = {key: account.get(key) for key in ("currency", "total_value", "cash", "unrealized_profit_loss", "tracked_position_value", "position_count")}
    holdings = []
    for item in snapshot.get("focus_holdings") or []:
        holdings.append({key: item.get(key) for key in (
            "ticker", "price", "current_value", "weight_pct", "change_pct", "value_change", "signals"
        )})
    return {
        "as_of": snapshot.get("as_of"), "account": keep_account, "focus_holdings": holdings,
        "largest_movers": [{key: item.get(key) for key in ("ticker", "change_pct", "weight_pct")} for item in snapshot.get("largest_movers") or []],
    }


def _daily_summary_messages(snapshot: dict[str, Any], *, include_news: bool) -> tuple[str, str]:
    allowed = [str(item.get("ticker")) for item in snapshot.get("focus_holdings") or [] if item.get("ticker")]
    schema = {
        "headline": "one concise Chinese sentence",
        "summary": ["2-4 Chinese bullet statements"],
        "news": [{"ticker": "must be one allowed ticker or MARKET", "headline": "string", "why_it_matters": "string", "source_url": "https URL", "published_date": "YYYY-MM-DD"}],
        "risks": ["up to 4 Chinese risk statements"],
        "next_focus": ["up to 4 Chinese observation items"],
    }
    system = (
        "你是账户日报分析助手。必须只输出严格 JSON，不要 Markdown。所有叙述使用简体中文。"
        "Python 提供的数值和持仓是不可修改事实；不得编造金额、收益、新闻、日期、持仓或价格。"
        "ACCOUNT SNAPSHOT 中的 ticker 是已经验证的 yfinance 标的代码，也是唯一可用于联网检索和报告名称的标识；"
        "不要使用或猜测经纪商可能过时的公司名称。"
        + (
            "使用联网搜索仅查最近 48 小时与重点持仓或组合风险直接相关的新闻；没有高相关来源时 news 返回空列表。"
            if include_news else "用户关闭了联网新闻；news 必须返回空列表。"
        )
        + "不得提供保证收益或自动交易指令；建议仅为观察条件。"
        + f"允许 news.ticker: {allowed + ['MARKET']}。SCHEMA: {json.dumps(schema, ensure_ascii=False)}"
    )
    user = "ACCOUNT SNAPSHOT:\n" + json.dumps(_compact_ai_context(snapshot), ensure_ascii=False, separators=(",", ":"))
    return system, user


def _normalise_ai_payload(raw: dict[str, Any], snapshot: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    allowed = {str(item.get("ticker")) for item in snapshot.get("focus_holdings") or []}
    def texts(value: Any, max_items: int, max_len: int) -> list[str]:
        if not isinstance(value, list):
            return []
        return [re.sub(r"\s+", " ", str(item)).strip()[:max_len] for item in value if str(item).strip()][:max_items]
    news = []
    source_urls = {item["url"] for item in sources if item.get("url")}
    for item in raw.get("news") or []:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip()
        if ticker not in allowed | {"MARKET"}:
            continue
        url = _safe_url(item.get("source_url"))
        if not url:
            continue
        date = str(item.get("published_date") or "").strip()[:10]
        news.append({
            "ticker": ticker, "headline": re.sub(r"\s+", " ", str(item.get("headline") or "")).strip()[:220],
            "why_it_matters": re.sub(r"\s+", " ", str(item.get("why_it_matters") or "")).strip()[:500],
            "url": url, "published_date": date,
            "source_returned": url in source_urls,
        })
    return {
        "ok": True,
        "headline": re.sub(r"\s+", " ", str(raw.get("headline") or "")).strip()[:240],
        "summary": texts(raw.get("summary"), 4, 420), "news": news[:5],
        "risks": texts(raw.get("risks"), 4, 300), "next_focus": texts(raw.get("next_focus"), 4, 300),
        "sources": sources[:5],
    }


def _call_ai_summary(snapshot: dict[str, Any], *, include_news: bool = True) -> dict[str, Any]:
    provider, model, timeout = _resolve_llm_settings()
    if provider != "dashscope" or not model:
        return {"ok": False, "error": "AI web summary requires a configured DashScope model.", "provider": provider, "model": model}
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        return {"ok": False, "error": "DASHSCOPE_API_KEY is not configured.", "provider": provider, "model": model}
    system, user = _daily_summary_messages(snapshot, include_news=include_news)
    started = time.perf_counter()
    try:
        if uses_dashscope_responses_api(model):
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=dashscope_compatible_model_server(), timeout=timeout)
            request_args: dict[str, Any] = {
                "model": model,
                "input": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            }
            if include_news:
                request_args["tools"] = [{"type": "web_search"}]
            response = client.responses.create(
                **request_args,
            )
            content, sources = _response_text_and_sources(response)
        else:
            import dashscope
            dashscope.base_http_api_url = dashscope_native_api_server()
            response = dashscope.Generation.call(
                api_key=api_key, model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                enable_search=include_news,
                search_options={"forced_search": include_news, "search_strategy": "turbo", "enable_source": include_news},
                result_format="message",
            )
            output = _response_value(response, "output", {}) or {}
            choices = _response_value(output, "choices", []) or []
            message = _response_value(choices[0], "message", {}) if choices else {}
            content = str(_response_value(message, "content", "") or "")
            sources = []
        result = _normalise_ai_payload(_parse_json_object(content), snapshot, sources)
        result.update({"provider": provider, "model": model, "elapsed_seconds": round(time.perf_counter() - started, 2)})
        return result
    except Exception as exc:  # The deterministic report must still be delivered.
        return {"ok": False, "error": _safe_error(exc), "provider": provider, "model": model, "elapsed_seconds": round(time.perf_counter() - started, 2)}


def _format_money(value: Any, currency: str) -> str:
    number = _number(value)
    return "N/A" if number is None else f"{number:,.2f}{(' ' + currency) if currency else ''}"


def _format_pct(value: Any) -> str:
    number = _number(value)
    return "N/A" if number is None else f"{number:+.2f}%"


def _telegram_escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def render_daily_summary_telegram(snapshot: dict[str, Any], ai: dict[str, Any]) -> str:
    account = snapshot.get("account") or {}
    currency = str(account.get("currency") or "")
    lines = [
        "📘 <b>TRADING 212 DAILY ACCOUNT BRIEF</b>",
        f"🕒 <code>{_telegram_escape(snapshot.get('as_of'))}</code>",
        f"💼 Account value: <code>{_format_money(account.get('total_value'), currency)}</code> · "
        f"Cash: <code>{_format_money(account.get('cash'), currency)}</code>",
        f"📊 Positions: <b>{int(account.get('position_count') or 0)}</b> · "
        f"Tracked value: <code>{_format_money(account.get('tracked_position_value'), currency)}</code>",
    ]
    if ai.get("ok") and ai.get("headline"):
        lines.extend(["", f"🤖 <b>AI view</b>\n{_telegram_escape(ai['headline'])}"])
        if ai.get("summary"):
            lines.extend(f"• {_telegram_escape(item)}" for item in ai["summary"])
    else:
        lines.extend(["", "⚪ <b>AI/news view unavailable</b>\nDeterministic account and position data is shown below."])
    movers = snapshot.get("largest_movers") or []
    if movers:
        lines.append("\n📈 <b>Largest moves</b>")
        for item in movers[:5]:
            icon = "🟢" if float(item.get("change_pct") or 0) >= 0 else "🔴"
            lines.append(f"{icon} <b>{_telegram_escape(item.get('ticker'))}</b> {_format_pct(item.get('change_pct'))} · weight {_format_pct(item.get('weight_pct'))}")
    signals = [item for item in snapshot.get("focus_holdings") or [] if item.get("signals")]
    if signals:
        lines.append("\n⚠️ <b>Today's technical signals</b>")
        for item in signals[:5]:
            lines.append(f"• <b>{_telegram_escape(item.get('ticker'))}</b>: {_telegram_escape(', '.join(item.get('signals') or []))}")
    if ai.get("news"):
        lines.append("\n📰 <b>Relevant news</b>")
        for item in ai["news"][:3]:
            title = _telegram_escape(item.get("headline"))
            url = html.escape(str(item.get("url") or ""), quote=True)
            label = _telegram_escape(item.get("ticker"))
            lines.append(f"• <b>{label}</b>: <a href='{url}'>{title or 'Open source'}</a>")
    if ai.get("risks"):
        lines.append("\n🛡️ <b>Risk watch</b>")
        lines.extend(f"• {_telegram_escape(item)}" for item in ai["risks"][:3])
    if ai.get("next_focus"):
        lines.append("\n👀 <b>Next focus</b>")
        lines.extend(f"• {_telegram_escape(item)}" for item in ai["next_focus"][:3])
    lines.append("\n<i>Research only — not investment advice.</i>")
    return "\n".join(lines)[:MAX_TELEGRAM_MESSAGE]


def _save_run(conn: sqlite3.Connection, user_id: int, summary_date: str, *, status: str, snapshot: dict[str, Any], ai: dict[str, Any], now: dt.datetime, sent: bool, error: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO daily_summary_runs(user_id, summary_date, generated_at, status, payload_json, ai_json, sent_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, summary_date) DO UPDATE SET
            generated_at=excluded.generated_at, status=excluded.status, payload_json=excluded.payload_json,
            ai_json=excluded.ai_json, sent_at=excluded.sent_at, error=excluded.error
        """,
        (int(user_id), summary_date, _iso(now), status, json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
         json.dumps(ai, ensure_ascii=False, separators=(",", ":")), _iso(now) if sent else None, error),
    )
    cutoff = (now.astimezone(monitor.BERLIN_TZ).date() - dt.timedelta(days=SUMMARY_RETENTION_DAYS)).isoformat()
    conn.execute("DELETE FROM daily_summary_runs WHERE user_id=? AND summary_date < ?", (int(user_id), cutoff))


def generate_and_send_daily_summary(
    user_id: int,
    *,
    path: Path | None = None,
    now: dt.datetime | None = None,
    request_get: Callable[..., Any] = requests.get,
    request_post: Callable[..., Any] = requests.post,
    ai_runner: Callable[[dict[str, Any]], dict[str, Any]] = _call_ai_summary,
    force: bool = False,
) -> dict[str, Any]:
    current = (now or dt.datetime.now(tz=monitor.UTC)).astimezone(monitor.UTC).replace(microsecond=0)
    summary_date = current.astimezone(monitor.BERLIN_TZ).date().isoformat()
    conn = _connect(path)
    try:
        settings = _settings_from_row(conn.execute("SELECT * FROM daily_summary_settings WHERE user_id=?", (int(user_id),)).fetchone())
        account = conn.execute("SELECT * FROM monitor_accounts WHERE user_id=?", (int(user_id),)).fetchone()
        if account is None:
            raise monitor.MonitorConfigurationError("Save Trading 212 credentials before generating a daily summary.")
        if not force:
            prior = conn.execute("SELECT status FROM daily_summary_runs WHERE user_id=? AND summary_date=?", (int(user_id), summary_date)).fetchone()
            if prior is not None:
                return {"status": "already_processed", "summary_date": summary_date, "events": []}
        secrets = monitor._account_secrets(account)
        if not all(secrets[key] for key in ("api_key", "api_secret", "telegram_bot_token", "telegram_chat_id")):
            raise monitor.MonitorConfigurationError("Trading 212 and Telegram credentials are required for the daily summary.")
        positions = [monitor._position_from_api(item) for item in monitor.fetch_positions(
            secrets["api_key"], secrets["api_secret"], account["environment"], request_get=request_get
        )]
        positions = [item for item in positions if item is not None]
        try:
            account_raw = fetch_account_summary(
                secrets["api_key"], secrets["api_secret"], account["environment"], request_get=request_get,
            )
        except monitor.Trading212APIError as exc:
            # Some otherwise-valid read-only keys may not include account-
            # summary permission. Positions still support a useful daily brief.
            account_raw = {"_summary_error": _safe_error(exc)}
        snapshot = build_daily_account_snapshot(conn, int(user_id), positions, account_raw, current)
        if account_raw.get("_summary_error"):
            snapshot["data_limitations"].append(
                "Trading 212 account-summary data was unavailable; account value/cash may be omitted."
            )
        if settings["include_ai"]:
            ai = (
                _call_ai_summary(snapshot, include_news=settings["include_news"])
                if ai_runner is _call_ai_summary else ai_runner(snapshot)
            )
        else:
            ai = {"ok": False, "error": "AI commentary disabled by user."}
        if settings["include_ai"] and not settings["include_news"] and ai.get("ok"):
            ai = {**ai, "news": []}
        text = render_daily_summary_telegram(snapshot, ai)
        monitor.send_telegram_message(secrets["telegram_bot_token"], secrets["telegram_chat_id"], text, request_post=request_post)
        _save_run(conn, int(user_id), summary_date, status="sent", snapshot=snapshot, ai=ai, now=current, sent=True)
        conn.execute("UPDATE daily_summary_settings SET last_success_at=?, last_error=NULL WHERE user_id=?", (_iso(current), int(user_id)))
        return {"status": "sent", "summary_date": summary_date, "snapshot": snapshot, "ai": ai}
    except Exception as exc:
        error = _safe_error(exc)
        try:
            _save_run(conn, int(user_id), summary_date, status="failed", snapshot={}, ai={}, now=current, sent=False, error=error)
            conn.execute("UPDATE daily_summary_settings SET last_error=? WHERE user_id=?", (error, int(user_id)))
        except sqlite3.Error:
            pass
        return {"status": "error", "summary_date": summary_date, "error": error}
    finally:
        conn.close()


def run_due_daily_summaries(*, path: Path | None = None, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    current = (now or dt.datetime.now(tz=monitor.UTC)).astimezone(monitor.UTC).replace(microsecond=0)
    local = current.astimezone(monitor.BERLIN_TZ)
    if local.weekday() >= 5:
        return []
    conn = _connect(path)
    try:
        rows = conn.execute("SELECT user_id, hour, minute FROM daily_summary_settings WHERE enabled=1").fetchall()
        completed_user_ids = {
            int(row[0]) for row in conn.execute(
                "SELECT user_id FROM daily_summary_runs WHERE summary_date=?", (local.date().isoformat(),)
            ).fetchall()
        }
    finally:
        conn.close()
    results = []
    for row in rows:
        if int(row["user_id"]) in completed_user_ids:
            continue
        if (local.hour, local.minute) < (int(row["hour"]), int(row["minute"])):
            continue
        results.append(generate_and_send_daily_summary(int(row["user_id"]), path=path, now=current))
    return results
