from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from .service import _get_market_date, _remove_run_dir, _safe_scope, _tail


REPORT_ROOT = Path(__file__).resolve().parent
PORTFOLIO_RUNNER = REPORT_ROOT / "run_portfolio_report.py"
DEFAULT_TIMEOUT_SECONDS = 1200


def portfolio_runtime_available() -> bool:
    return PORTFOLIO_RUNNER.is_file()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "Portfolio")).strip("_") or "Portfolio"


def _extract_runner_failure(combined_output: str) -> tuple[str | None, bool]:
    """Return a concise user-facing subprocess failure and whether it is a quality failure."""
    markers = (
        (
            "Portfolio report quality gate failed:",
            "报告生成失败：单次联网研究或报告质量未达到发布要求。",
            True,
        ),
    )
    for marker, prefix, quality_failure in markers:
        if marker not in combined_output:
            continue
        detail = combined_output.split(marker, 1)[1].splitlines()[0].strip()
        return prefix + (f" {detail}" if detail else ""), quality_failure
    return None, False


def generate_portfolio_report(
    portfolio_page: dict,
    *,
    owner_key: str,
    portfolio_page_id: str | None = None,
    portfolio_name: str | None = None,
    market_rows: list[dict] | None = None,
    fx_rates: dict | None = None,
    timeout_seconds: int | None = None,
) -> dict:
    portfolio_page_id = str(portfolio_page_id or portfolio_page.get("id") or "").strip()
    portfolio_name = str(portfolio_name or portfolio_page.get("name") or "Portfolio").strip() or "Portfolio"
    if not owner_key:
        return {"success": False, "error": "A signed-in account is required for portfolio reports."}
    if not portfolio_page_id:
        return {"success": False, "error": "Portfolio page ID is missing."}
    if not portfolio_page.get("holdings"):
        return {"success": False, "error": "Portfolio has no holdings."}
    if not portfolio_runtime_available():
        return {"success": False, "error": f"Portfolio report runner not found: {PORTFOLIO_RUNNER}"}

    report_date = _get_market_date()
    file_name = f"{_safe_name(portfolio_name)}_portfolio_report_{report_date}.html"
    run_dir = REPORT_ROOT / "runs" / "portfolio" / _safe_scope(owner_key) / f"{_safe_name(portfolio_name)}_{uuid.uuid4().hex}"
    run_dir.mkdir(parents=True, exist_ok=False)
    input_file = run_dir / "portfolio_input.json"
    output_html = run_dir / file_name
    input_file.write_text(
        json.dumps(
            {
                "portfolio_page": portfolio_page,
                "market_rows": market_rows or [],
                "fx_rates": fx_rates or {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cmd = [
        sys.executable,
        str(PORTFOLIO_RUNNER),
        "--portfolio-input", str(input_file),
        "--portfolio-id", portfolio_page_id,
        "--portfolio-name", portfolio_name,
        "--owner-scope", owner_key,
        "--run-dir", str(run_dir),
        "--output", str(output_html),
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    timeout = timeout_seconds or int(os.environ.get("PORTFOLIO_REPORT_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(REPORT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        elapsed = time.perf_counter() - started
        stdout = _tail(completed.stdout, 4000)
        stderr = _tail(completed.stderr, 4000)
        if completed.returncode != 0:
            combined = f"{completed.stderr}\n{completed.stdout}"
            failure_message, quality_failure = _extract_runner_failure(combined)
            diagnostics = {}
            diagnostics_path = run_dir / "portfolio_research_diagnostics.json"
            if diagnostics_path.is_file():
                try:
                    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    diagnostics = {}
            return {
                "success": False,
                "report_kind": "portfolio",
                "error": failure_message or f"Portfolio report command failed with exit code {completed.returncode}.",
                "quality_gate_failed": quality_failure,
                "research_diagnostics": diagnostics,
                "stdout": stdout,
                "stderr": stderr,
            }
        if not output_html.is_file():
            return {
                "success": False,
                "report_kind": "portfolio",
                "error": "Portfolio report command finished but did not create the expected HTML file.",
                "stdout": stdout,
                "stderr": stderr,
            }
        return {
            "success": True,
            "report_kind": "portfolio",
            "portfolio_page_id": portfolio_page_id,
            "portfolio_name": portfolio_name,
            "report_date": report_date,
            "file_name": file_name,
            "html_bytes": output_html.read_bytes(),
            "elapsed": elapsed,
            "stdout": stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "report_kind": "portfolio",
            "error": f"Portfolio report generation timed out after {timeout} seconds.",
            "stdout": _tail(exc.stdout if isinstance(exc.stdout, str) else ""),
            "stderr": _tail(exc.stderr if isinstance(exc.stderr, str) else ""),
        }
    except Exception as exc:
        return {"success": False, "report_kind": "portfolio", "error": str(exc)}
    finally:
        _remove_run_dir(run_dir)


def generate_portfolio_report_for_job(job: dict) -> dict:
    from multiuser_store import get_portfolio_page_by_id

    payload = json.loads(job.get("payload_json") or "{}")
    portfolio_page = get_portfolio_page_by_id(job["owner_key"], job.get("subject_key") or payload.get("portfolio_page_id"))
    if portfolio_page is None:
        raise RuntimeError("Portfolio no longer exists.")
    return generate_portfolio_report(
        portfolio_page,
        owner_key=job["owner_key"],
        portfolio_page_id=portfolio_page.get("id"),
        portfolio_name=portfolio_page.get("name"),
    )
