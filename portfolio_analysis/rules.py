from __future__ import annotations

from typing import Any


def generate_portfolio_rule_findings(snapshot: dict[str, Any], metrics: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    max_position = float(settings.get("max_position_pct", 20.0) or 20.0) / 100.0
    max_group = float(settings.get("max_group_pct", 40.0) or 40.0) / 100.0
    top1 = metrics.get("top1_weight")
    top3 = metrics.get("top3_weight")
    if top1 is not None and top1 > max_position:
        findings.append({
            "risk_id": "CONCENTRATION_TOP1",
            "severity": "high",
            "title": "Single-position concentration is above limit",
            "description": f"Largest holding is {top1:.1%}, above configured {max_position:.1%}.",
        })
    if top3 is not None and top3 > max_group:
        findings.append({
            "risk_id": "CONCENTRATION_TOP3",
            "severity": "medium",
            "title": "Top-three concentration is elevated",
            "description": f"Top three holdings are {top3:.1%}, above configured group limit {max_group:.1%}.",
        })
    beta = metrics.get("portfolio_beta")
    if beta is not None and beta > 1.25:
        findings.append({
            "risk_id": "HIGH_BETA",
            "severity": "medium",
            "title": "Portfolio beta is high",
            "description": f"Estimated portfolio beta is {beta:.2f}.",
        })
    for field, label in (("missing_prices", "missing prices"), ("missing_fx", "missing FX rates"), ("missing_history", "missing history")):
        values = snapshot.get("data_quality", {}).get(field) or []
        if values:
            findings.append({
                "risk_id": field.upper(),
                "severity": "medium",
                "title": f"Data quality limitation: {label}",
                "description": ", ".join(map(str, values[:12])),
            })
    return findings
