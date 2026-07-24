"""Regression tests for time-aware daily-report evidence scoring."""

from __future__ import annotations

import json
from pathlib import Path

from daily_report.src.stock_daily_agent.config import ProjectPaths, RunContext
from daily_report.src.stock_daily_agent.notes import NewsNote
from daily_report.src.stock_daily_agent.tools import _compute_final_rating_payload


def _note(tag: str, title: str, source_date: str) -> NewsNote:
    return NewsNote(
        tag=tag,
        title=title,
        fact="A sufficiently detailed fact used by the deterministic score.",
        logic="A sufficiently detailed explanation of the market implication.",
        investment_meaning="A sufficiently detailed investment implication.",
        source_date=source_date,
        evidence_grade="A",
        evidence_id="E-001",
    )


def test_background_evidence_is_display_only_and_aging_evidence_is_half_weighted(tmp_path: Path) -> None:
    ctx = RunContext(
        paths=ProjectPaths.from_root(Path(__file__).resolve().parents[1] / "daily_report"),
        ticker="QQQ",
        run_dir=tmp_path,
        report_date="2026-07-24",
    )
    ctx.data_file.write_text(json.dumps({
        "INSTRUMENT_TYPE": "ETF",
        "SCORING_PROFILE": "etf_four_factor",
        "technical_score": 50.0,
        "technical_subscores": {},
        "technical_effective_weights": {},
        "FUNDAMENTAL_SOURCES": {},
        "LAST_CLOSE": 100.0,
        "BETA": 0.0,
        "REALIZED_VOL_20D_PCT": 10.0,
        "MAX_DRAWDOWN_63D_PCT": 2.0,
        "ATR_PCT": 1.0,
    }), encoding="utf-8")

    payload = _compute_final_rating_payload(ctx, [
        _note("BULL", "Recent catalyst", "2026-07-23"),
        _note("BEAR", "Aging concern", "2026-07-10"),
        _note("BEAR", "Background debt concern", "2026-05-01"),
    ])

    # Recent BULL = +7, aging BEAR = -3.5, A-grade quality = +10.
    assert payload["subscores"]["news_score"] == 63.5
    assert payload["inputs"]["evidence_freshness"] == {
        "recent": 1, "aging": 1, "background": 1, "unknown": 0,
    }
    assert payload["inputs"]["weighted_note_counts"] == {
        "BULL": 1.0, "BEAR": 0.5, "MIX": 0,
    }
    # The old bear note has a risk keyword in its title but cannot reduce the
    # daily risk score because background evidence is display-only. Only the
    # aging BEAR contributes at 50% (2.5 points after the risk multiplier).
    assert payload["inputs"]["risk_penalty_parts"]["notes"] == 2.5
    assert payload["subscores"]["risk_score"] == 97.5
