from __future__ import annotations

import os
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
) -> dict:
    """Generate a v5.8 report and return its HTML in memory.

    Each invocation uses a unique temporary run directory. All report and
    intermediate files are removed before this function returns, including
    failed and timed-out runs.
    """
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        return {"success": False, "error": "Please enter a ticker."}
    if not _TICKER_PATTERN.fullmatch(ticker):
        return {"success": False, "error": f"Invalid ticker format: {ticker}"}
    if not runtime_available():
        return {"success": False, "error": f"Integrated daily report runner not found: {REPORT_RUNNER}"}

    report_date = _get_market_date()
    file_name = f"{_safe_ticker(ticker)}_report_{report_date}.html"
    run_dir = (
        REPORT_ROOT
        / "runs"
        / "streamlit"
        / _safe_scope(user_scope)
        / f"{_safe_ticker(ticker)}_{uuid.uuid4().hex}"
    )
    output_html = run_dir / file_name
    run_dir.mkdir(parents=True, exist_ok=False)

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
        "--output",
        str(output_html),
        "--quiet",
    ]
    if search_provider:
        cmd.extend(["--search-provider", search_provider])
    if no_article_fetch:
        cmd.append("--no-article-fetch")

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
        if not output_html.is_file():
            return {
                "success": False,
                "error": "Daily report command finished but did not create the expected HTML file.",
                "stdout": stdout,
                "stderr": stderr,
            }

        return {
            "success": True,
            "ticker": ticker,
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
            "error": f"Daily report generation timed out after {timeout} seconds.",
            "stdout": _tail(exc.stdout if isinstance(exc.stdout, str) else ""),
            "stderr": _tail(exc.stderr if isinstance(exc.stderr, str) else ""),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        _remove_run_dir(run_dir)
