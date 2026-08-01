"""Failure diagnostics must explain missing local artifacts without exposing prompts."""

from __future__ import annotations

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "daily_report" / "src"
if str(AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(AGENT_SRC))

from stock_daily_agent.agent_runner import run_agent
from stock_daily_agent.config import ProjectPaths, RunContext


def test_agent_runner_reports_missing_artifacts_on_early_failure(tmp_path, monkeypatch) -> None:
    ctx = RunContext(paths=ProjectPaths.from_root(tmp_path / "daily_report"), ticker="APP", run_dir=tmp_path / "run")
    ctx.run_dir.mkdir()
    monkeypatch.setattr("stock_daily_agent.agent_runner.build_agent", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("model transport failed")))
    result = run_agent(ctx, model="test", verbose=False)
    assert not result.ok
    assert "missing data, chart, final_notes, notes" in result.warnings[0]
    assert "RuntimeError: model transport failed" in result.warnings[0]
