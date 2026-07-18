# -*- coding: utf-8 -*-
"""轻量内联 SVG 图表（无第三方依赖，确定性渲染）。

与个股日报统一暗色风格；用于 Portfolio 报告的配置与风险可视化。
"""
from __future__ import annotations

from typing import Any

from .report_theme import COLOR_TOKENS

_W = 960


def _bar_chart(title: str, items: list[tuple[str, float]], color: str, max_value: float | None = None, pct: bool = True) -> str:
    if not items:
        return ""
    max_value = max_value if max_value is not None else max((v for _, v in items), default=1.0) or 1.0
    row_h = 26
    gap = 8
    top = 36
    height = top + len(items) * (row_h + gap) + 10
    rows = [f'<text x="10" y="22" fill="{COLOR_TOKENS["text_strong"]}" font-size="14" font-weight="700">{_esc(title)}</text>']
    for i, (label, value) in enumerate(items):
        y = top + i * (row_h + gap)
        w = max(2.0, (abs(value) / max_value) * (_W - 220))
        val_text = f"{value*100:.1f}%" if pct else f"{value:.2f}"
        rows.append(f'<text x="10" y="{y+row_h-8}" fill="{COLOR_TOKENS["muted"]}" font-size="12">{_esc(label)}</text>')
        rows.append(f'<rect x="160" y="{y}" width="{w:.1f}" height="{row_h-6}" rx="4" fill="{color}" opacity="0.85"/>')
        rows.append(f'<text x="{165+w:.1f}" y="{y+row_h-8}" fill="{COLOR_TOKENS["text"]}" font-size="12">{val_text}</text>')
    return (
        f'<svg viewBox="0 0 {_W} {height}" xmlns="http://www.w3.org/2000/svg" role="img">'
        + "".join(rows) + "</svg>"
    )


def _esc(value: Any) -> str:
    from html import escape
    return escape(str(value if value is not None else ""), quote=True)


def svg_weight_bars(holdings: list[dict[str, Any]], top_n: int = 12) -> str:
    items = sorted(
        ((h.get("ticker"), float(h.get("weight") or 0.0)) for h in holdings),
        key=lambda x: -x[1],
    )[:top_n]
    return _bar_chart("持仓权重分布", items, COLOR_TOKENS["brand"], pct=True)


def svg_weight_vs_risk(
    holdings: list[dict[str, Any]],
    risk_contrib: dict[str, float],
    top_n: int = 12,
) -> str:
    items = sorted(holdings, key=lambda h: -(risk_contrib.get(h.get("ticker"), 0.0) or 0.0))[:top_n]
    if not items:
        return ""
    max_v = max(
        [float(h.get("weight") or 0.0) for h in items]
        + [risk_contrib.get(h.get("ticker"), 0.0) or 0.0 for h in items]
        + [1e-6]
    )
    row_h = 26
    gap = 10
    top = 36
    height = top + len(items) * (row_h + gap) + 10
    rows = [
        f'<text x="10" y="22" fill="{COLOR_TOKENS["text_strong"]}" font-size="14" font-weight="700">权重 vs 风险贡献</text>',
        f'<rect x="160" y="10" width="12" height="12" fill="{COLOR_TOKENS["brand"]}"/><text x="178" y="20" fill="{COLOR_TOKENS["muted"]}" font-size="11">权重</text>',
        f'<rect x="240" y="10" width="12" height="12" fill="{COLOR_TOKENS["down"]}"/><text x="258" y="20" fill="{COLOR_TOKENS["muted"]}" font-size="11">风险贡献</text>',
    ]
    for i, h in enumerate(items):
        y = top + i * (row_h + gap)
        t = h.get("ticker")
        w = float(h.get("weight") or 0.0)
        rc = risk_contrib.get(t, 0.0) or 0.0
        ww = max(2.0, (w / max_v) * (_W - 220))
        rw = max(2.0, (rc / max_v) * (_W - 220))
        rows.append(f'<text x="10" y="{y+row_h-8}" fill="{COLOR_TOKENS["muted"]}" font-size="12">{_esc(t)}</text>')
        rows.append(f'<rect x="160" y="{y}" width="{ww:.1f}" height="{row_h-8}" rx="4" fill="{COLOR_TOKENS["brand"]}" opacity="0.8"/>')
        rows.append(f'<rect x="160" y="{y+row_h-6}" width="{rw:.1f}" height="5" rx="2" fill="{COLOR_TOKENS["down"]}" opacity="0.85"/>')
        rows.append(f'<text x="{170+ww:.1f}" y="{y+row_h-8}" fill="{COLOR_TOKENS["text"]}" font-size="11">{w*100:.1f}% / {rc*100:.1f}%</text>')
    return f'<svg viewBox="0 0 {_W} {height}" xmlns="http://www.w3.org/2000/svg" role="img">' + "".join(rows) + "</svg>"


def svg_allocation(group_weights: dict[str, float], title: str, color: str = "#58a6ff") -> str:
    items = sorted(((k, v) for k, v in group_weights.items() if k), key=lambda x: -x[1])
    return _bar_chart(title, items, color, pct=True)


def svg_cumulative_returns(
    labels: list[str],
    portfolio_cumulative_pct: list[float],
    benchmark_cumulative_pct: list[float],
) -> str:
    """绘制累计收益图。

    入参已经是百分数（例如 7.31 表示 +7.31%）。不再做任何 ×100 放大，
    避免与上游 ``_cumulative_returns`` 重复放大（修改计划第三轮 6）。
    """
    if not portfolio_cumulative_pct or not benchmark_cumulative_pct:
        return ""
    n = min(len(portfolio_cumulative_pct), len(benchmark_cumulative_pct))
    if n < 2:
        return ""
    port = portfolio_cumulative_pct[:n]
    bench = benchmark_cumulative_pct[:n]
    all_v = port + bench
    lo, hi = min(all_v), max(all_v)
    span = (hi - lo) or 1.0
    height = 260
    top = 20
    bottom = height - 30
    x = lambda i: 40 + (i / (n - 1)) * (_W - 80)
    y = lambda v: bottom - ((v - lo) / span) * (bottom - top)
    grid = []
    for g in range(5):
        gy = top + g * (bottom - top) / 4
        gv = hi - g * span / 4
        grid.append(f'<line x1="40" y1="{gy:.1f}" x2="{_W-40}" y2="{gy:.1f}" stroke="{COLOR_TOKENS["border_soft"]}" stroke-width="1"/>')
        grid.append(f'<text x="{_W-36}" y="{gy+4:.1f}" fill="{COLOR_TOKENS["muted_soft"]}" font-size="10" text-anchor="end">{gv:.1f}%</text>')
    line = lambda series, color: (
        '<polyline fill="none" stroke="' + color + '" stroke-width="2" points="'
        + " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(series)) + '"/>'
    )
    n_ticks = min(6, n)
    xticks = []
    for k in range(n_ticks):
        i = int(k * (n - 1) / (n_ticks - 1)) if n_ticks > 1 else 0
        xticks.append(f'<text x="{x(i):.1f}" y="{height-8}" fill="{COLOR_TOKENS["muted_soft"]}" font-size="10" text-anchor="middle">{_esc(labels[i] if i < len(labels) else "")}</text>')
    svg = (
        f'<svg viewBox="0 0 {_W} {height}" xmlns="http://www.w3.org/2000/svg" role="img">'
        f'<text x="10" y="16" fill="{COLOR_TOKENS["text_strong"]}" font-size="14" font-weight="700">当前持仓静态权重回溯模拟 vs 基准</text>'
        + "".join(grid)
        + line(port, COLOR_TOKENS["brand"])
        + line(bench, COLOR_TOKENS["warn"])
        + "".join(xticks)
        + f'<rect x="40" y="6" width="12" height="10" fill="{COLOR_TOKENS["brand"]}"/>'
        + f'<text x="56" y="15" fill="{COLOR_TOKENS["muted"]}" font-size="10">组合</text>'
        + f'<rect x="110" y="6" width="12" height="10" fill="{COLOR_TOKENS["warn"]}"/>'
        + f'<text x="126" y="15" fill="{COLOR_TOKENS["muted"]}" font-size="10">基准</text>'
        + "</svg>"
    )
    return svg
