from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


def _load(path: Path, default: Any) -> Any:
    if not path or not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt(value: Any, suffix: str = "", digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    return f"{number:,.{digits}f}{suffix}"


def _pct(value: Any) -> str:
    return _fmt(float(value) * 100.0 if value is not None and abs(float(value)) <= 1.5 else value, "%")


def _row(cells: list[Any], header: bool = False) -> str:
    tag = "th" if header else "td"
    return "<tr>" + "".join(f"<{tag}>{html.escape('' if c is None else str(c))}</{tag}>" for c in cells) + "</tr>"


def build_html(
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    risk_ranking: dict[str, Any],
    advice: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> str:
    title = f"{snapshot.get('portfolio_name', 'Portfolio')} AI Portfolio Report"
    base = snapshot.get("base_currency", "EUR")
    summary = snapshot.get("summary", {})
    settings = snapshot.get("analysis_settings", {})
    quality = snapshot.get("data_quality", {})

    overview_rows = [
        ["Total Market Value", f"{_fmt(summary.get('total_market_value_base'))} {base}"],
        ["Total Cost Basis", f"{_fmt(summary.get('total_cost_basis_base'))} {base}"],
        ["Total P/L", f"{_fmt(summary.get('profit_loss_base'))} {base}"],
        ["Total P/L %", _fmt(summary.get("profit_loss_pct"), "%")],
        ["Top 1 Weight", _pct(metrics.get("top1_weight"))],
        ["Top 3 Weight", _pct(metrics.get("top3_weight"))],
        ["HHI", _fmt(metrics.get("hhi_10000"), digits=0)],
        ["Effective Holdings", _fmt(metrics.get("effective_holdings"))],
        ["Portfolio Beta", _fmt(metrics.get("portfolio_beta"))],
        ["Annualized Volatility", _fmt(metrics.get("annualized_volatility"), "%")],
        ["63D Max Drawdown", _fmt(metrics.get("max_drawdown_63d"), "%")],
        ["252D Max Drawdown", _fmt(metrics.get("max_drawdown_252d"), "%")],
    ]

    holdings_rows = []
    risk_by_ticker = {item.get("ticker"): item for item in risk_ranking.get("items", [])}
    rc_by_ticker = {item.get("ticker"): item for item in metrics.get("risk_contributions", [])}
    for h in snapshot.get("holdings", []):
        rc = rc_by_ticker.get(h.get("ticker"), {})
        holdings_rows.append([
            h.get("ticker"),
            h.get("group"),
            _pct(h.get("weight")),
            f"{_fmt(h.get('market_value_base'))} {base}",
            _fmt(h.get("profit_loss_pct"), "%"),
            _fmt(h.get("return_1d"), "%"),
            _fmt(h.get("return_5d"), "%"),
            _fmt(h.get("return_1m"), "%"),
            _fmt(h.get("return_ytd"), "%"),
            _fmt(h.get("diff_ema20"), "%"),
            _fmt(h.get("diff_ema50"), "%"),
            _fmt(h.get("diff_ema200"), "%"),
            _fmt(h.get("rsi")),
            _fmt(h.get("volume_ratio")),
            _fmt(h.get("beta")),
            _pct(rc.get("risk_contribution")),
            _fmt(risk_by_ticker.get(h.get("ticker"), {}).get("risk_priority_score")),
        ])

    action_rows = []
    for item in advice.get("actions") or []:
        action_rows.append([
            item.get("ticker"),
            item.get("action"),
            item.get("priority"),
            _pct(item.get("current_weight")),
            f"{_pct(item.get('target_weight_min'))} - {_pct(item.get('target_weight_max'))}",
            _fmt(item.get("confidence")),
            " | ".join(item.get("trigger_conditions") or []),
        ])

    evidence_rows = [
        [
            e.get("evidence_id"),
            e.get("ticker") or e.get("scope"),
            e.get("title"),
            e.get("source"),
            e.get("published_date"),
            e.get("summary"),
            e.get("url"),
        ]
        for e in evidence
    ]

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body {{ margin:0; background:#0f172a; color:#e5e7eb; font-family:Arial, sans-serif; line-height:1.45; }}
main {{ max-width:1180px; margin:0 auto; padding:24px; }}
.card {{ background:#111827; border:1px solid #334155; border-radius:12px; padding:18px; margin:16px 0; box-shadow:0 8px 22px rgba(0,0,0,.22); }}
h1,h2,h3 {{ color:#f8fafc; }}
.muted {{ color:#94a3b8; }}
table {{ border-collapse:collapse; width:100%; min-width:720px; }}
.scroll {{ overflow-x:auto; }}
th,td {{ border:1px solid #334155; padding:7px 8px; text-align:left; vertical-align:top; }}
th {{ background:#1f2937; position:sticky; top:0; }}
td {{ background:#0b1220; }}
.pill {{ display:inline-block; padding:3px 8px; border-radius:999px; background:#1e40af; color:white; }}
a {{ color:#93c5fd; }}
</style>
</head>
<body><main>
<h1>{html.escape(title)}</h1>
<p class="muted">Report date: {html.escape(str(snapshot.get('report_date') or ''))} · Snapshot: {html.escape(str(snapshot.get('as_of') or ''))}</p>
<p><span class="pill">Base {html.escape(base)}</span> <span class="pill">Benchmark {html.escape(str(snapshot.get('benchmark') or ''))}</span> <span class="pill">{html.escape(str(settings.get('risk_profile') or 'balanced'))}</span></p>

<section class="card"><h2>Portfolio Overview</h2><div class="scroll"><table>{_row(['Metric','Value'], True)}{''.join(_row(r) for r in overview_rows)}</table></div></section>

<section class="card"><h2>Portfolio Stance</h2>
<p><b>{html.escape(str(advice.get('portfolio_stance', 'watch')))}</b> · Risk level: {html.escape(str(advice.get('risk_level', 'unknown')))} · Confidence: {_fmt(advice.get('confidence'))}</p>
<p>{html.escape(str(advice.get('summary') or 'Quantitative portfolio report generated. News coverage may be limited if search providers are not configured.'))}</p>
</section>

<section class="card"><h2>Risk Diagnosis</h2>
<ul>{''.join(f"<li><b>{html.escape(str(r.get('title')))}</b>: {html.escape(str(r.get('description')))}</li>" for r in advice.get('key_risks', []))}</ul>
</section>

<section class="card"><h2>Action Suggestions</h2><div class="scroll"><table>{_row(['Ticker','Action','Priority','Weight','Target Range','Confidence','Trigger Conditions'], True)}{''.join(_row(r) for r in action_rows)}</table></div></section>

<section class="card"><h2>All Holdings Technical Snapshot</h2><div class="scroll"><table>{_row(['Ticker','Group','Weight','Market Value','P/L%','1D','5D','1M','YTD','EMA20','EMA50','EMA200','RSI','Volume Ratio','Beta','Risk Contribution','Risk Score'], True)}{''.join(_row(r) for r in holdings_rows)}</table></div></section>

<section class="card"><h2>Top-risk News / Industry / Macro Evidence</h2><div class="scroll"><table>{_row(['Evidence ID','Scope','Title','Source','Date','Summary','URL'], True)}{''.join(_row(r) for r in evidence_rows)}</table></div></section>

<section class="card"><h2>Data Quality & Limitations</h2>
<p>Missing prices: {html.escape(', '.join(quality.get('missing_prices') or []) or 'None')}</p>
<p>Missing FX: {html.escape(', '.join(quality.get('missing_fx') or []) or 'None')}</p>
<p>Missing history: {html.escape(', '.join(quality.get('missing_history') or []) or 'None')}</p>
<p>{html.escape(str(advice.get('disclaimer') or 'This report is for research purposes only and is not investment advice.'))}</p>
</section>
</main></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--risk-ranking", required=True)
    parser.add_argument("--advice", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    html_text = build_html(
        _load(Path(args.snapshot), {}),
        _load(Path(args.metrics), {}),
        _load(Path(args.risk_ranking), {}),
        _load(Path(args.advice), {}),
        _load(Path(args.evidence), []),
    )
    Path(args.output).write_text(html_text, encoding="utf-8")


if __name__ == "__main__":
    main()
