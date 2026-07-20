"""Deterministic research-pipeline diagnostics for portfolio reports."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from daily_report.src.stock_daily_agent.research_core.evidence_id import (
    evidence_final_gate_reasons,
)


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _canonical_url(value: Any) -> str:
    """Normalize an article URL for cross-pass diagnostics deduplication."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        host = parsed.netloc.lower().removeprefix("www.")
        path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
        tracking_keys = {
            "ref", "source", "campaign", "mc_cid", "mc_eid", "fbclid", "gclid",
        }
        query_items = [
            (key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in tracking_keys
        ]
        query = urlencode(sorted(query_items))
        # Ignore http/https differences but preserve non-tracking query arguments
        # because some publisher endpoints use them as the actual article ID.
        return f"{host}{path}" + (f"?{query}" if query else "")
    except Exception:
        return raw.lower().split("?", 1)[0].rstrip("/")


def evidence_candidate_key(item: dict[str, Any]) -> str:
    """Return a cross-pass candidate key independent of Summarizer UID changes."""
    ticker = _norm_text(item.get("ticker") or "MACRO")
    url = _canonical_url(item.get("url") or item.get("raw_url"))
    if url:
        return f"url|{ticker}|{url}"
    event_key = _norm_text(item.get("event_key"))
    if event_key:
        return f"event|{ticker}|{event_key}"
    uid = _norm_text(item.get("evidence_uid"))
    if uid:
        return f"uid|{uid}"
    title = _norm_text(item.get("title") or item.get("raw_title"))
    published = _norm_text(item.get("published_date") or item.get("raw_published_date"))
    return f"fallback|{ticker}|{published}|{title}"


def _diagnostic_rank(item: dict[str, Any]) -> tuple[bool, bool, bool, float, float]:
    """Prefer the richest/latest-stage representation of a duplicate candidate."""
    return (
        bool(item.get("evidence_id")),
        item.get("accept") is True,
        bool(item.get("materiality_accepted")),
        float(item.get("selection_score") or 0.0),
        float(item.get("priority_score") or 0.0),
    )


def dedupe_evidence_for_diagnostics(
    evidence: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
    """Collapse the same article returned through multiple search/gap lanes."""
    chosen: dict[str, dict[str, Any]] = {}
    duplicate_rows: list[dict[str, object]] = []
    for index, item in enumerate(evidence or []):
        key = evidence_candidate_key(item)
        previous = chosen.get(key)
        if previous is None:
            chosen[key] = item
            continue
        duplicate_rows.append({
            "candidate_key": key,
            "ticker": item.get("ticker"),
            "title": item.get("title") or item.get("raw_title"),
            "source_domain": item.get("source_domain"),
            "lane": item.get("lane"),
            "evidence_uid": item.get("evidence_uid"),
            "index": index,
        })
        if _diagnostic_rank(item) > _diagnostic_rank(previous):
            chosen[key] = item
    return list(chosen.values()), duplicate_rows


def merge_evidence_by_identity(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge multi-pass Evidence using the canonical article identity."""
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group or []:
            key = evidence_candidate_key(item)
            previous = merged.get(key)
            if previous is None:
                merged[key] = item
                continue
            if _diagnostic_rank(item) > _diagnostic_rank(previous):
                combined = dict(previous)
                combined.update(item)
            else:
                combined = dict(item)
                combined.update(previous)
            merged[key] = combined
    return list(merged.values())


def refresh_research_stage_diagnostics(
    research_result: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, object]:
    """Synchronize displayed counts to the final canonical Evidence set."""
    diagnostics = research_result.setdefault("diagnostics", {})
    stage = evidence_stage_diagnostics(evidence)
    diagnostics.update(stage)
    totals = stage.get("evidence_stage_totals") or {}
    passed = int(totals.get("materiality_passed") or 0)
    rejected = int(totals.get("materiality_rejected") or 0)
    entered = passed + rejected
    diagnostics["selected_evidence_count"] = entered
    diagnostics["materiality_accepted_count"] = passed
    materiality_stats = dict(diagnostics.get("materiality_stats") or {})
    materiality_stats["accepted_count"] = passed
    materiality_stats["rejected_count"] = rejected
    materiality_stats["ranked_count"] = entered
    materiality_stats["expanded_ranked_count"] = entered
    materiality_stats["final_selected_count"] = entered
    materiality_stats["deduplicated_candidate_count"] = entered
    rejected_reasons: dict[str, int] = {}
    accepted_by_ticker: dict[str, int] = {}
    rejected_by_ticker: dict[str, int] = {}
    source_type_counts: dict[str, dict[str, int]] = {}
    lane_counts: dict[str, dict[str, int]] = {}
    canonical, _ = dedupe_evidence_for_diagnostics(evidence)
    for item in canonical:
        ticker = str(item.get("ticker") or "MACRO")
        source_type = str(item.get("source_type") or "unknown")
        lane = str(item.get("lane") or "unknown")
        accepted = bool(item.get("materiality_accepted"))
        outcome = "accepted" if accepted else "rejected"
        source_type_counts.setdefault(source_type, {"accepted": 0, "rejected": 0})[outcome] += 1
        lane_counts.setdefault(lane, {"accepted": 0, "rejected": 0})[outcome] += 1
        if accepted:
            accepted_by_ticker[ticker] = accepted_by_ticker.get(ticker, 0) + 1
        else:
            rejected_by_ticker[ticker] = rejected_by_ticker.get(ticker, 0) + 1
            reason = str(item.get("reject_reason") or "unspecified")
            rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
    materiality_stats["rejected_reasons"] = rejected_reasons
    materiality_stats["accepted_by_ticker"] = accepted_by_ticker
    materiality_stats["rejected_by_ticker"] = rejected_by_ticker
    materiality_stats["source_type_counts"] = source_type_counts
    materiality_stats["lane_counts"] = lane_counts
    diagnostics["materiality_stats"] = materiality_stats
    valid_dates = sorted(
        str(item.get("published_date"))[:10]
        for item in evidence or []
        if str(item.get("published_date") or "").strip()
        and len(str(item.get("published_date"))) >= 10
    )
    diagnostics["latest_selected_event_date"] = valid_dates[-1] if valid_dates else None
    return stage


def evidence_stage_diagnostics(evidence: list[dict[str, Any]]) -> dict[str, object]:
    """Build closed stage counts by ticker, source type and search lane.

    Counts are based on one canonical candidate set.  The same article can be
    returned by the initial search, a post-Materiality gap query and the final
    accepted-evidence gap query; without cross-pass URL deduplication those rows
    previously made per-dimension totals exceed ``selected_evidence_count``.
    """
    canonical_evidence, duplicate_rows = dedupe_evidence_for_diagnostics(evidence)
    dimensions: dict[str, dict[str, dict[str, int]]] = {
        "ticker": {},
        "source_type": {},
        "lane": {},
    }
    isolation_reasons: dict[str, int] = {}
    summarizer_reject_reasons: dict[str, int] = {}
    final_gate_reject_reasons: dict[str, int] = {}
    isolated_items: list[dict[str, object]] = []
    final_gate_rejected_items: list[dict[str, object]] = []
    stage_totals: dict[str, int] = {
        "materiality_passed": 0,
        "materiality_rejected": 0,
        "summary_isolated": 0,
        "summarizer_rejected": 0,
        "reference": 0,
        "final_gate_rejected": 0,
        "accepted": 0,
    }

    def add(dimension: str, key: str, stage: str) -> None:
        row = dimensions[dimension].setdefault(key or "unknown", {})
        row[stage] = row.get(stage, 0) + 1

    for item in canonical_evidence:
        ticker = str(item.get("ticker") or "MACRO")
        source_type = str(item.get("source_type") or "unknown")
        lane = str(item.get("lane") or "unknown")

        if item.get("materiality_accepted"):
            stage_totals["materiality_passed"] += 1
            for dimension, key in (("ticker", ticker), ("source_type", source_type), ("lane", lane)):
                add(dimension, key, "materiality_passed")

        isolation_reason = str(item.get("summary_isolation_reason") or "").strip()
        if not item.get("materiality_accepted"):
            stage = "materiality_rejected"
        elif isolation_reason:
            stage = "summary_isolated"
            isolation_reasons[isolation_reason] = isolation_reasons.get(isolation_reason, 0) + 1
            isolated_items.append({
                "evidence_uid": item.get("evidence_uid"),
                "ticker": item.get("ticker"),
                "title": item.get("title") or item.get("raw_title"),
                "source_domain": item.get("source_domain"),
                "source_type": source_type,
                "lane": lane,
                "reason": isolation_reason,
                "identity_error": item.get("summary_identity_error"),
            })
        elif item.get("summary_integrity_ok") is True and item.get("accept") is False:
            stage = "summarizer_rejected"
            reject_reason = str(item.get("reject_reason") or "unspecified")
            summarizer_reject_reasons[reject_reason] = summarizer_reject_reasons.get(reject_reason, 0) + 1
        elif item.get("evidence_id"):
            stage = "accepted"
        elif item.get("is_reference_page") or str(item.get("page_classification") or "") == "reference":
            stage = "reference"
        else:
            stage = "final_gate_rejected"
            reasons = [
                reason for reason in evidence_final_gate_reasons(item)
                if reason not in {"materiality_not_accepted", "summarizer_not_accepted"}
            ] or ["unspecified_final_gate_rejection"]
            for reason in reasons:
                final_gate_reject_reasons[reason] = final_gate_reject_reasons.get(reason, 0) + 1
            final_gate_rejected_items.append({
                "evidence_uid": item.get("evidence_uid"),
                "ticker": item.get("ticker") or "MACRO",
                "title": item.get("title") or item.get("raw_title") or "—",
                "source_domain": item.get("source_domain") or "unknown",
                "source_type": source_type,
                "lane": lane,
                "raw_published_date": item.get("raw_published_date"),
                "published_date": item.get("published_date"),
                "date_reference_datetime": item.get("date_reference_datetime"),
                "article_fetch_ok": bool(item.get("article_fetch_ok")),
                "snippet_fallback_ok": bool(item.get("snippet_fallback_ok")),
                "recency_tier": item.get("recency_tier") or "unknown",
                "entity_role": item.get("entity_role") or "unknown",
                "reasons": reasons,
            })

        stage_totals[stage] += 1
        for dimension, key in (("ticker", ticker), ("source_type", source_type), ("lane", lane)):
            add(dimension, key, stage)

    entered_materiality = stage_totals["materiality_passed"] + stage_totals["materiality_rejected"]
    terminal_total = sum(
        stage_totals[key]
        for key in (
            "materiality_rejected", "summary_isolated", "summarizer_rejected",
            "reference", "final_gate_rejected", "accepted",
        )
    )
    return {
        "evidence_stage_totals": stage_totals,
        "evidence_stage_by_ticker": dimensions["ticker"],
        "evidence_stage_by_source_type": dimensions["source_type"],
        "evidence_stage_by_lane": dimensions["lane"],
        "selected_evidence_count": entered_materiality,
        "stage_terminal_count": terminal_total,
        "stage_count_invariant_ok": entered_materiality == terminal_total == len(canonical_evidence),
        "diagnostic_candidate_count_before_dedupe": len(evidence or []),
        "diagnostic_candidate_count_after_dedupe": len(canonical_evidence),
        "diagnostic_duplicate_candidate_count": len(duplicate_rows),
        "diagnostic_duplicate_candidates": duplicate_rows,
        "summarizer_isolation_count": stage_totals["summary_isolated"],
        "summarizer_isolation_reasons": isolation_reasons,
        "summarizer_isolated_items": isolated_items,
        "summarizer_rejected_count": stage_totals["summarizer_rejected"],
        "summarizer_reject_reasons": summarizer_reject_reasons,
        "final_gate_rejected_count": stage_totals["final_gate_rejected"],
        "final_gate_reject_reasons": final_gate_reject_reasons,
        "final_gate_rejected_items": final_gate_rejected_items,
    }
