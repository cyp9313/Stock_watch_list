"""Dedicated single-writer worker for Trading 212 position monitoring."""

from __future__ import annotations

import os
import sys
from pathlib import Path
import time

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from config_loader import load_project_env
from trading212_monitor import run_all_monitors


def _poll_seconds() -> int:
    try:
        return max(60, int(os.environ.get("TRADING212_MONITOR_POLL_SECONDS", "60")))
    except ValueError:
        return 60


def main() -> None:
    load_project_env()
    poll_seconds = _poll_seconds()
    print(f"[Trading212Monitor] started; interval={poll_seconds}s", flush=True)
    try:
        while True:
            results = run_all_monitors()
            active = len(results)
            failures = sum(result.get("status") == "error" for result in results)
            sent = sum(len(result.get("events") or []) for result in results)
            technical_unavailable = sum(len(result.get("technical_unavailable") or {}) for result in results)
            if active:
                print(
                    f"[Trading212Monitor] accounts={active}, alerts={sent}, "
                    f"technical_unavailable={technical_unavailable}, failures={failures}",
                    flush=True,
                )
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print("[Trading212Monitor] stopped", flush=True)


if __name__ == "__main__":
    main()
