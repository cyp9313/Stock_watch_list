from __future__ import annotations

import os
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[assignment]


def _get_market_date() -> str:
    """Get current date in US/Eastern timezone (NYSE/NASDAQ market date)."""
    import datetime
    if ZoneInfo is not None:
        return datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    return datetime.date.today().isoformat()


REPORT_ROOT = Path(__file__).resolve().parent
REPORT_RUNNER = REPORT_ROOT / "run_report.py"
DEFAULT_TIMEOUT_SECONDS = 1800
_TICKER_PATTERN = re.compile(r"^[A-Z0-9.^=\-]+$")
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def runtime_available() -> bool:
    """Return whether the integrated v5.8 report runner is present."""
    return REPORT_RUNNER.is_file()


def _safe_ticker(ticker: str) -> str:
    return ticker.replace("-", "_").replace("^", "IDX_").replace(".", "_")


def _safe_scope(scope: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", scope or "guest")


def _tail(text: str | None, max_chars: int = 8000) -> str:
    text = text or ""
    return text[-max_chars:] if len(text) > max_chars else text


def _diagnostic_run_retention_enabled() -> bool:
    """Return whether complete per-run diagnostics were explicitly requested."""
    return os.environ.get("DECISION_REPORT_KEEP_RUN_DIR", "").strip().lower() in _TRUE_ENV_VALUES


def _normalize_holding_context(payload: dict | None) -> dict | None:
    if payload is None:
        return None
    allowed = {"has_position", "buy_price", "shares", "portfolio_weight", "unrealized_pnl_pct"}
    if not isinstance(payload, dict) or set(payload) - allowed or not isinstance(payload.get("has_position", False), bool):
        raise ValueError("Invalid holding context.")
    normalized = {"has_position": bool(payload.get("has_position", False))}
    for key, positive in (("buy_price", True), ("shares", True), ("portfolio_weight", False), ("unrealized_pnl_pct", False)):
        value = payload.get(key)
        if value is None:
            normalized[key] = None
            continue
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid holding context.") from exc
        if not math.isfinite(number) or (positive and number <= 0) or (key == "portfolio_weight" and not 0 <= number <= 1):
            raise ValueError("Invalid holding context.")
        normalized[key] = number
    return normalized


def _remove_run_dir(run_dir: Path) -> None:
    """Remove generated artifacts and prune empty per-user run directories."""
    shutil.rmtree(run_dir, ignore_errors=True)
    runs_root = REPORT_ROOT / "runs"
    parent = run_dir.parent
    while parent != runs_root.parent:
        try:
            parent.rmdir()
        except OSError:
            break
        if parent == runs_root:
            break
        parent = parent.parent


def generate_report(
    ticker: str,
    *,
    user_scope: str = "guest",
    months: int = 3,
    search_provider: str = "auto",
    no_article_fetch: bool = False,
    timeout_seconds: int | None = None,
    holding_context: dict | None = None,
    decision_dashboard: bool = True,
    delivery_mode: str = "html",
    include_telegram_summary: bool = False,
) -> dict:
    """Generate a v5.8 report and return its HTML in memory.

    Each invocation uses a unique temporary run directory. All report and
    intermediate files are removed before this function returns, including
    failed and timed-out runs, unless diagnostic retention is explicitly
    enabled with ``DECISION_REPORT_KEEP_RUN_DIR``.
    """
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        return {"success": False, "error": "Please enter a ticker."}
    if not _TICKER_PATTERN.fullmatch(ticker):
        return {"success": False, "error": f"Invalid ticker format: {ticker}"}
    if not runtime_available():
        return {"success": False, "error": f"Integrated daily report runner not found: {REPORT_RUNNER}"}

    delivery_mode = str(delivery_mode or "html").strip().lower()
    if delivery_mode not in {"html", "telegram"}:
        return {"success": False, "error": "Unsupported report delivery mode."}
    report_date = _get_market_date()
    file_name = f"{_safe_ticker(ticker)}_report_{report_date}.html"
    run_dir = (
        REPORT_ROOT
        / "runs"
        / "streamlit"
        / _safe_scope(user_scope)
        / f"{_safe_ticker(ticker)}_{uuid.uuid4().hex}"
    )
    output_html = run_dir / file_name if delivery_mode == "html" else None
    run_dir.mkdir(parents=True, exist_ok=False)
    keep_run_dir = _diagnostic_run_retention_enabled()
    audit_path = run_dir / f"{_safe_ticker(ticker)}_finalization_audit.json"
    try:
        normalized_holding = _normalize_holding_context(holding_context)
    except ValueError as exc:
        if not keep_run_dir:
            _remove_run_dir(run_dir)
        return {"success": False, "error": str(exc)}

    cmd = [
        sys.executable,
        str(REPORT_RUNNER),
        ticker,
        "--months",
        str(max(1, int(months))),
        "--date",
        report_date,
        "--run-dir",
        str(run_dir),
        "--quiet",
        "--delivery-mode",
        delivery_mode,
    ]
    if output_html is not None:
        cmd.extend(["--output", str(output_html)])
    if search_provider:
        cmd.extend(["--search-provider", search_provider])
    if no_article_fetch:
        cmd.append("--no-article-fetch")
    if not decision_dashboard:
        cmd.append("--disable-decision-dashboard")
    if normalized_holding is not None:
        holding_path = run_dir / f"{_safe_ticker(ticker)}_holding_context.json"
        holding_path.write_text(json.dumps(normalized_holding, ensure_ascii=False), encoding="utf-8")
        cmd.extend(["--holding-context-json", str(holding_path)])

    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    timeout = timeout_seconds or int(os.environ.get("STOCK_DAILY_REPORT_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
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
            return {
                "success": False,
                "error": f"Daily report command failed with exit code {completed.returncode}.",
                "stdout": stdout,
                "stderr": stderr,
            }
        if output_html is not None and not output_html.is_file():
            return {
                "success": False,
                "error": "Daily report command finished but did not create the expected HTML file.",
                "stdout": stdout,
                "stderr": stderr,
            }

        decision_payload = None
        decision_path = run_dir / f"{_safe_ticker(ticker)}_decision.json"
        if decision_path.is_file():
            try:
                decision_payload = json.loads(decision_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                decision_payload = None
        result = {
            "success": True,
            "ticker": ticker,
            "report_date": report_date,
            "file_name": file_name,
            "elapsed": elapsed,
            "stdout": stdout,
            "stderr": stderr,
            "decision_dashboard": bool(decision_payload),
            "fallback_used": bool(decision_payload and decision_payload.get("fallback_used")),
            "warnings": [],
            "diagnostic_run_dir": str(run_dir) if keep_run_dir else None,
            "diagnostic_audit_file": str(audit_path) if keep_run_dir and audit_path.is_file() else None,
        }
        if output_html is not None:
            result["html_bytes"] = output_html.read_bytes()
        if include_telegram_summary or output_html is None:
            data_path = run_dir / f"{_safe_ticker(ticker)}_data.json"
            notes_path = run_dir / f"{_safe_ticker(ticker)}_final_notes.json"
            if not data_path.is_file() or not notes_path.is_file():
                return {"success": False, "error": "Telegram report artifacts were not created.", "stdout": stdout, "stderr": stderr}
            try:
                from .telegram_stock_report import render_telegram_stock_report
                data_payload = json.loads(data_path.read_text(encoding="utf-8"))
                notes_payload = json.loads(notes_path.read_text(encoding="utf-8"))
                result["telegram_messages"] = render_telegram_stock_report(
                    ticker=ticker, report_date=report_date, data=data_payload,
                    decision=decision_payload, notes=notes_payload,
                )
            except (OSError, ValueError, TypeError) as exc:
                return {"success": False, "error": f"Could not render Telegram report: {exc}", "stdout": stdout, "stderr": stderr}
        return result
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "error": f"Daily report generation timed out after {timeout} seconds.",
            "stdout": _tail(exc.stdout if isinstance(exc.stdout, str) else ""),
            "stderr": _tail(exc.stderr if isinstance(exc.stderr, str) else ""),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        if not keep_run_dir:
            _remove_run_dir(run_dir)


def generate_telegram_report(ticker: str, **kwargs) -> dict:
    """Generate one complete HTML report and its compact Telegram summary."""
    return generate_report(ticker, delivery_mode="html", include_telegram_summary=True, **kwargs)
