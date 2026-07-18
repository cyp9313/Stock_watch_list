"""Central publication and actionability gate for portfolio reports."""
from __future__ import annotations

import os
from typing import Any


class PortfolioReportQualityError(RuntimeError):
    def __init__(self, result: dict[str, Any]):
        self.result = result
        super().__init__("Portfolio report quality gate failed: " + "; ".join(result.get("blocking_errors") or []))


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def evaluate_report_quality(
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    research_result: dict[str, Any],
    advice: dict[str, Any],
    validation_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_news = _env_bool("PORTFOLIO_REPORT_REQUIRE_TOP_RISK_NEWS", True)
    strict_coverage = _env_bool("PORTFOLIO_REPORT_STRICT_NEWS_COVERAGE", False)
    min_coverage = float(os.environ.get("PORTFOLIO_REPORT_MIN_NEWS_COVERAGE", "0.60"))
    min_fresh = int(os.environ.get("PORTFOLIO_REPORT_MIN_FRESH_EVIDENCE", "1"))
    min_actionable = float(os.environ.get("PORTFOLIO_REPORT_MIN_ACTIONABLE_CONFIDENCE", "0.50"))
    diagnostics = research_result.get("diagnostics") or {}
    evidence = research_result.get("evidence") or []
    status = str(research_result.get("status") or diagnostics.get("status") or "unknown")
    coverage = float(diagnostics.get("top_risk_coverage") or 0.0)
    fresh_count = sum(1 for item in evidence if item.get("recency_tier") in {"fresh_event", "recent_background"})
    verified_count = sum(1 for item in evidence if item.get("article_fetch_ok"))
    final_confidence = float(advice.get("final_confidence") or advice.get("confidence") or 0.0)
    blocking: list[str] = []
    warnings: list[str] = []
    if require_news:
        if status not in {"success", "insufficient_coverage"}:
            blocking.append(f"Top-risk 新闻研究状态不是成功：{status}")
        if coverage < min_coverage:
            coverage_message = f"Top-risk 新闻覆盖率 {coverage:.0%} 低于目标 {min_coverage:.0%}"
            if strict_coverage:
                blocking.append(coverage_message)
            else:
                warnings.append(coverage_message + "；报告允许降级生成，但操作置信度将受限")
        if len(evidence) == 0:
            blocking.append("Top-risk 新闻研究未返回有效证据")
        if fresh_count < min_fresh:
            blocking.append(f"新鲜新闻证据数 {fresh_count} 低于最低要求 {min_fresh}")
    if metrics.get("portfolio_beta_status") != "actual":
        warnings.append("历史组合 Beta 不可用或样本不足")
    if evidence and verified_count == 0:
        warnings.append("新闻证据均为未验证搜索摘要，报告和操作置信度已受限")
    validation_result = validation_result or {}
    blocking.extend(validation_result.get("hard_errors") or [])
    action_confidences = [float(x.get("final_confidence") or x.get("confidence") or 0.0) for x in advice.get("actions") or []]
    if action_confidences and max(action_confidences) > final_confidence + 1e-9:
        blocking.append("单项操作置信度超过报告最终置信度")
    actionable = not blocking and final_confidence >= min_actionable
    quality_parts = [coverage, min(1.0, fresh_count / max(1, min_fresh)), final_confidence]
    quality_score = sum(quality_parts) / len(quality_parts)
    return {
        "publishable": not blocking,
        "actionable": actionable,
        "quality_score": round(quality_score, 3),
        "blocking_errors": list(dict.fromkeys(blocking)),
        "warnings": list(dict.fromkeys(warnings)),
        "requirements": {
            "require_top_risk_news": require_news,
            "strict_news_coverage": strict_coverage,
            "minimum_news_coverage": min_coverage,
            "minimum_fresh_evidence": min_fresh,
            "minimum_actionable_confidence": min_actionable,
        },
    }
