"""Central publication and actionability gate for portfolio reports.

第七轮第 7 节扩展：在原有 ticker coverage / fresh count / article fetch / final
confidence 检查之外，新增 P0 数据完整性阻断条件：

- Evidence UID / 显示 ID 唯一性；
- 摘要串线（summary identity mismatch）；
- 被拒绝 / reference 证据泄漏进正式发布列表；
- accepted 且 material 事件数低于下限；
- risk-weighted coverage < 0.85 → 仅降级为不可操作（observation），不阻断；
- 方向性操作（add/trim/reduce/exit）缺乏有效 accepted 证据 → 阻断。

并区分三个状态：publishable / observation_only / directional_action_supported。
"""
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


_DIRECTIONAL_ACTIONS = {"add", "trim", "reduce", "exit"}


def _accepted_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """收口后 accepted = 持有最终 evidence_id 的证据（rejected/reference 的 id 为 None）。"""
    return [e for e in (evidence or []) if e.get("evidence_id")]


def _check_identity_integrity(accepted: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    uids = [e.get("evidence_uid") for e in accepted if e.get("evidence_uid")]
    if len(uids) != len(set(uids)):
        seen: set[str] = set()
        dups: set[str] = set()
        for u in uids:
            if u in seen:
                dups.add(u)
            seen.add(u)
        errors.append("duplicate_evidence_uid:" + ",".join(sorted(dups)))
    ids = [e.get("evidence_id") for e in accepted if e.get("evidence_id")]
    if len(ids) != len(set(ids)):
        seen = set()
        dups = set()
        for i in ids:
            if i in seen:
                dups.add(i)
            seen.add(i)
        errors.append("duplicate_evidence_id:" + ",".join(sorted(dups)))
    return errors


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
    min_risk_weighted_coverage = float(os.environ.get("PORTFOLIO_REPORT_MIN_RISK_WEIGHTED_COVERAGE", "0.85"))

    diagnostics = research_result.get("diagnostics") or {}
    evidence = research_result.get("evidence") or []
    accepted = _accepted_evidence(evidence)
    accepted_count = len(accepted)
    leaked = [e for e in evidence if not e.get("evidence_id")]
    # 如果没有任何 accepted 证据，leaked 不代表 rejected 泄露，只是没有通过 acceptance
    has_accepted = any(e.get("evidence_id") for e in evidence)
    if not has_accepted:
        leaked = []  # 没有 accepted 时不清算 leaked

    status = str(research_result.get("status") or diagnostics.get("status") or "unknown")
    coverage = float(diagnostics.get("top_risk_coverage") or 0.0)
    fresh_count = sum(
        1 for e in accepted if e.get("recency_tier") in {"fresh_event", "recent_background"}
    )
    risk_weighted_coverage = float(diagnostics.get("risk_weighted_coverage") or 0.0)
    materiality_accepted_count = int(diagnostics.get("materiality_accepted_count") or accepted_count)
    verified_count = sum(1 for e in accepted if e.get("article_fetch_ok"))

    final_confidence = float(advice.get("final_confidence") or advice.get("confidence") or 0.0)
    blocking: list[str] = []
    warnings: list[str] = []

    # 1) Evidence 身份完整性（第七轮第 3/7 节）
    blocking.extend(_check_identity_integrity(accepted))

    # 2) 摘要串线（第七轮第 4 节）
    summarizer_errors = diagnostics.get("summarizer_errors") or []
    if any("mismatch" in str(e) or "duplicate" in str(e) for e in summarizer_errors):
        blocking.append("summary_identity_mismatch_detected")

    # 3) rejected / reference 泄漏进发布列表（第七轮第 6 节）
    if leaked:
        blocking.append(f"rejected_or_reference_evidence_in_publishable_list:count={len(leaked)}")

    # 4) Top-risk 新闻状态与覆盖
    if require_news:
        if status not in {"success", "insufficient_coverage"}:
            blocking.append(f"Top-risk 新闻研究状态不是成功：{status}")
        if coverage < min_coverage:
            coverage_message = f"Top-risk 新闻覆盖率 {coverage:.0%} 低于目标 {min_coverage:.0%}"
            if strict_coverage:
                blocking.append(coverage_message)
            else:
                warnings.append(coverage_message + "；报告允许降级生成，但操作置信度将受限")
        if accepted_count == 0:
            # 无 accepted 证据 → observation_only，不阻断报告
            warnings.append("无 accepted（materiality + 摘要通过）证据，报告转为观察型")
        if fresh_count < min_fresh:
            if accepted_count == 0:
                warnings.append(f"新鲜 accepted 证据数 0；因无 accepted 证据，观察型报告仍可发布")
            else:
                blocking.append(f"新鲜 accepted 证据数 {fresh_count} 低于最低要求 {min_fresh}")

    # 5) Beta 可用性
    if metrics.get("portfolio_beta_status") != "actual":
        warnings.append("历史组合 Beta 不可用或样本不足")

    # 6) 全部未验证 → 置信度受限
    if accepted and verified_count == 0:
        warnings.append("新闻证据均为未验证搜索摘要，报告和操作置信度已受限")

    # 7) 风险加权覆盖（< 阈值仅降级为不可操作，不阻断）
    if risk_weighted_coverage < min_risk_weighted_coverage:
        warnings.append(
            f"风险加权覆盖 {risk_weighted_coverage:.0%} 低于 {min_risk_weighted_coverage:.0%}，"
            f"报告降级为观察型（不可操作）"
        )

    # 8) 方向性操作必须指向有效 accepted 证据（第七轮第 7 节）
    accepted_ids = {e.get("evidence_id") for e in accepted}
    directional_actions = [
        a for a in (advice.get("actions") or [])
        if str(a.get("action") or "").lower() in _DIRECTIONAL_ACTIONS
    ]
    directional_action_count = 0
    for a in directional_actions:
        ev_ids = a.get("evidence_ids") or []
        if any(eid in accepted_ids for eid in ev_ids):
            directional_action_count += 1
        else:
            blocking.append(
                f"directional_action_without_material_evidence:{a.get('ticker')}:{a.get('action')}"
            )

    # 9) Planner fallback 但未暴露错误
    if diagnostics.get("planner_mode") == "fallback" and not diagnostics.get("planner_errors"):
        warnings.append("Planner 降级为 fallback 但未暴露具体校验错误，无法诊断修复")

    # 10) 结构化校验硬错误
    validation_result = validation_result or {}
    blocking.extend(validation_result.get("hard_errors") or [])

    # 单项操作置信度不得超过报告最终置信度
    action_confidences = [
        float(x.get("final_confidence") or x.get("confidence") or 0.0)
        for x in advice.get("actions") or []
    ]
    if action_confidences and max(action_confidences) > final_confidence + 1e-9:
        blocking.append("单项操作置信度超过报告最终置信度")

    # ── 三态判定 ──
    publishable = not blocking
    research_complete = publishable and materiality_accepted_count >= min_fresh
    directional_action_supported = directional_action_count > 0 and publishable
    observation_only = publishable and not directional_action_supported
    # 可操作：有方向性证据支撑 + 置信度达标 + 风险加权覆盖达标
    actionable = (
        directional_action_supported
        and final_confidence >= min_actionable
        and risk_weighted_coverage >= min_risk_weighted_coverage
    )
    if observation_only and not blocking:
        warnings.append("当前无有效方向性操作支撑，报告作为观察型发布（不标记可操作）")

    quality_parts = [
        coverage,
        min(1.0, fresh_count / max(1, min_fresh)),
        final_confidence,
        (1.0 if not blocking else 0.0),
    ]
    quality_score = round(sum(quality_parts) / len(quality_parts), 3)

    return {
        "publishable": publishable,
        "research_complete": research_complete,
        "observation_only": observation_only,
        "directional_action_supported": directional_action_supported,
        "directional_action_count": directional_action_count,
        "actionable": actionable,
        "quality_score": quality_score,
        "blocking_errors": list(dict.fromkeys(blocking)),
        "warnings": list(dict.fromkeys(warnings)),
        "requirements": {
            "require_top_risk_news": require_news,
            "strict_news_coverage": strict_coverage,
            "minimum_news_coverage": min_coverage,
            "minimum_fresh_evidence": min_fresh,
            "minimum_actionable_confidence": min_actionable,
            "minimum_risk_weighted_coverage": min_risk_weighted_coverage,
        },
    }
