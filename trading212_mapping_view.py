"""Safe, lightweight HTML rendering for Trading 212 ticker mappings.

The main Streamlit data-grid can be hidden by page-level grid styling in some
browser/theme combinations.  Keep this table independent of that component so
mapping results remain visible even when the data-grid is unavailable.
"""

from __future__ import annotations

import html
import math
from typing import Any, Mapping, Sequence


MAPPING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ticker", "Trading 212 ticker"),
    ("display_name", "Name"),
    ("yfinance_ticker", "yfinance ticker"),
    ("mapping_status", "Status"),
    ("mapping_source", "Source"),
    ("last_history_date", "Latest daily bar"),
    ("last_error", "Last error"),
)


def _display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value)


def _status_background(status: str) -> str:
    normalized = status.lower()
    if normalized in {"mapped", "manual"}:
        return "#e8f5e9"
    if normalized in {"unmapped", "manual_error", "pending"}:
        return "#fff8e1"
    return "transparent"


def build_mapping_table_html(
    rows: Sequence[Mapping[str, Any]],
    *,
    border_color: str = "#d0d7de",
    header_background: str = "#f3f4f6",
    table_background: str = "#ffffff",
    text_color: str = "#172b4d",
) -> str:
    """Return an escaped, scrollable mapping table for ``st.markdown``."""
    safe_border = html.escape(border_color, quote=True)
    safe_header = html.escape(header_background, quote=True)
    safe_background = html.escape(table_background, quote=True)
    safe_text = html.escape(text_color, quote=True)
    parts = [
        f"<div style='max-height:420px;overflow:auto;border:1px solid {safe_border};border-radius:6px;'>",
        f"<table style='width:100%;border-collapse:collapse;font-size:13px;background:{safe_background};color:{safe_text};'>",
        "<thead><tr>",
    ]
    for _, heading in MAPPING_COLUMNS:
        parts.append(
            f"<th style='position:sticky;top:0;z-index:1;padding:7px;text-align:left;white-space:normal;"
            f"background:{safe_header};border:1px solid {safe_border};'>{html.escape(heading)}</th>"
        )
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        for key, _ in MAPPING_COLUMNS:
            value = _display_value(row.get(key))
            extra = ""
            if key == "mapping_status":
                extra = f"background:{_status_background(value)};"
            parts.append(
                f"<td title='{html.escape(value, quote=True)}' style='padding:7px;text-align:left;vertical-align:top;"
                f"white-space:normal;overflow-wrap:anywhere;{extra}border:1px solid {safe_border};'>"
                f"{html.escape(value)}</td>"
            )
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)
