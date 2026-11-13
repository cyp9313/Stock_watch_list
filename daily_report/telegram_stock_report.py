"""Compact, safe Telegram rendering for validated stock-report artifacts."""

from __future__ import annotations

import html
import math
from typing import Any


MAX_MESSAGE_LENGTH = 3800


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _text(value: Any, limit: int = 700) -> str:
    return html.escape(" ".join(str(value or "").split())[:limit], quote=False)


def _price(value: Any, currency: str) -> str:
    number = _number(value)
    return "N/A" if number is None else f"{number:,.2f} {html.escape(currency)}"


def _pct(value: Any) -> str:
    number = _number(value)
    return "N/A" if number is None else f"{number:+.2f}%"


def _item_lines(items: list[dict[str, Any]], icon: str, limit: int = 3) -> list[str]:
    lines: list[str] = []
    for item in items[:limit]:
        body = _text(item.get("text") or item.get("investment_meaning") or item.get("title"), 430)
        evidence = ", ".join(_text(item, 32) for item in item.get("evidence_ids", []) if item)
        if body:
            lines.append(f"{icon} {body}{f' <i>({evidence})</i>' if evidence else ''}")
    return lines


def _split_messages(text: str) -> list[str]:
    """Split only at line boundaries so Telegram HTML tags remain readable."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= MAX_MESSAGE_LENGTH:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # Individual fields are bounded before this point, but retain a
        # deterministic last-resort split for unexpected provider text.
        while len(line) > MAX_MESSAGE_LENGTH:
            chunks.append(line[:MAX_MESSAGE_LENGTH])
            line = line[MAX_MESSAGE_LENGTH:]
        current = line
    if current:
        chunks.append(current)
    total = len(chunks)
    return [f"<i>Part {index}/{total}</i>\n{chunk}" if total > 1 else chunk for index, chunk in enumerate(chunks, start=1)]


def render_telegram_stock_report(
    *, ticker: str, report_date: str, data: dict[str, Any], decision: dict[str, Any] | None, notes: dict[str, Any] | None,
) -> list[str]:
    """Render the web report's core facts without HTML or chart generation."""
    currency = str(data.get("CURRENCY") or "USD")
    name = _text(data.get("LONG_NAME") or data.get("SHORT_NAME") or ticker, 180)
    rating = data.get("final_rating") if isinstance(data.get("final_rating"), dict) else {}
    score = _number((decision or {}).get("final_score") if decision else rating.get("final_score"))
    action = _text((decision or {}).get("final_action") or rating.get("rating_text") or "N/A", 80)
    lines = [
        f"📘 <b>AI STOCK DAILY REPORT · {_text(ticker, 32)}</b>",
        f"<i>{name} · {html.escape(str(report_date))}</i>",
        f"💵 Price: <code>{_price(data.get('LAST_CLOSE'), currency)}</code> · 1D: <b>{_pct(data.get('PCT'))}</b>",
        f"🎯 Python score: <b>{f'{score:.1f}/100' if score is not None else 'N/A'}</b> · Action: <b>{action}</b>",
    ]
    if decision:
        one_sentence = _text(decision.get("one_sentence"), 650)
        if one_sentence:
            lines.extend(["", f"🧭 <b>Decision view</b>\n{one_sentence}"])
        advice = (decision.get("position_advice") or {}).get(
            "has_position" if decision.get("action_scope") == "has_position" else "no_position"
        )
        if advice:
            lines.append(f"• {_text(advice, 560)}")
        levels = decision.get("levels") or {}
        level_values = [
            ("Ideal buy", levels.get("ideal_buy")), ("Stop", levels.get("stop_loss")),
            ("Take profit", levels.get("take_profit")),
        ]
        visible_levels = [f"{label}: <code>{_text(value, 120)}</code>" for label, value in level_values if value]
        if visible_levels:
            lines.append("• " + " · ".join(visible_levels))
        if levels.get("invalidation_condition"):
            lines.append(f"• Invalidation: {_text(levels['invalidation_condition'], 480)}")

    lines.extend([
        "", "📊 <b>Technical snapshot</b>",
        f"• Signal: <b>{_text(data.get('technical_signal') or 'N/A', 90)}</b> · RSI(14): <code>{_number(data.get('rsi')):.1f}</code>" if _number(data.get("rsi")) is not None else f"• Signal: <b>{_text(data.get('technical_signal') or 'N/A', 90)}</b>",
        f"• MA20 / MA50 / MA200: <code>{_price(data.get('ma20'), currency)}</code> / <code>{_price(data.get('ma50'), currency)}</code> / <code>{_price(data.get('ma200'), currency)}</code>",
        f"• MACD / Signal: <code>{_number(data.get('macd_line')):.3f}</code> / <code>{_number(data.get('signal_line')):.3f}</code>" if _number(data.get("macd_line")) is not None and _number(data.get("signal_line")) is not None else "• MACD / Signal: N/A",
        f"• Volume ratio: <code>{_number(data.get('vol_ratio')):.2f}x</code> · ATR(14): <code>{_pct(data.get('ATR_PCT'))}</code>" if _number(data.get("vol_ratio")) is not None else f"• ATR(14): <code>{_pct(data.get('ATR_PCT'))}</code>",
    ])
    if decision:
        catalysts = _item_lines(list(decision.get("catalysts") or []), "🟢")
        risks = _item_lines(list(decision.get("risk_alerts") or []), "🔴")
        if catalysts:
            lines.extend(["", "✨ <b>Catalysts</b>", *catalysts])
        if risks:
            lines.extend(["", "⚠️ <b>Risk watch</b>", *risks])
        checklist = [_text(item, 360) for item in list(decision.get("action_checklist") or [])[:3] if _text(item, 360)]
        if checklist:
            lines.extend(["", "👀 <b>Next focus</b>", *(f"• {item}" for item in checklist)])

    note_items = list((notes or {}).get("items") or [])
    sources = []
    seen_urls: set[str] = set()
    for item in note_items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("evidence_url") or "").strip()
        title = _text(item.get("title") or item.get("source"), 140)
        if url.startswith(("https://", "http://")) and url not in seen_urls and title:
            seen_urls.add(url)
            sources.append(f"• <a href='{html.escape(url, quote=True)}'>{title}</a>")
        if len(sources) == 3:
            break
    if sources:
        lines.extend(["", "📰 <b>Verified sources</b>", *sources])
    lines.extend(["", "<i>Research only — not investment advice.</i>"])
    return _split_messages("\n".join(lines))
