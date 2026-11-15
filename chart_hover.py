"""Shared, display-only change labels for Plotly price-chart hover cards."""

import math


def close_change_labels(closes):
    """Return each close's percentage change from the immediately prior close.

    The first point, and points without a valid nonzero previous close, have
    no comparison base. Keep them explicit rather than inventing a 0% move.
    """
    labels = []
    previous = None
    for raw in closes:
        try:
            current = float(raw)
        except (TypeError, ValueError):
            current = float("nan")
        if previous is None or not math.isfinite(current) or not math.isfinite(previous) or previous <= 0:
            labels.append("N/A")
        else:
            change = (current / previous - 1) * 100
            labels.append("0.00%" if abs(change) < 0.005 else f"{change:+.2f}%")
        previous = current
    return labels


def change_hover_label(interval):
    return "Daily change vs previous close" if interval == "1d" else "Change vs previous bar"


def candlestick_hover_kwargs(closes, interval):
    """Plotly hover properties shared by both Streamlit K-line pages."""
    return {
        "customdata": close_change_labels(closes),
        "hovertemplate": (
            "%{x|%Y-%m-%d %H:%M}<br>Open: %{open:.2f}<br>High: %{high:.2f}"
            "<br>Low: %{low:.2f}<br>Close: %{close:.2f}<br>"
            + change_hover_label(interval) + ": %{customdata}<extra></extra>"
        ),
    }
