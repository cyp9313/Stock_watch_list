"""Dedicated scheduler for bounded daily Trading 212 AI account briefs."""

from __future__ import annotations

import os
import sys
from pathlib import Path
import time

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from config_loader import load_project_env
from trading212_daily_summary import run_due_daily_summaries


def main() -> None:
    load_project_env()
    print("[Trading212DailySummary] started; checking schedules every 60s", flush=True)
    try:
        while True:
            results = run_due_daily_summaries()
            if results:
                sent = sum(item.get("status") == "sent" for item in results)
                failed = sum(item.get("status") == "error" for item in results)
                print(f"[Trading212DailySummary] due={len(results)}, sent={sent}, failed={failed}", flush=True)
            time.sleep(60)
    except KeyboardInterrupt:
        print("[Trading212DailySummary] stopped", flush=True)


if __name__ == "__main__":
    main()
