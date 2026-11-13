"""Long-poll Telegram commands and queue account-scoped AI reports."""

from __future__ import annotations

import os
import time

from config_loader import load_project_env
from telegram_ai_reports import poll_configured_bots


def main() -> None:
    load_project_env()
    idle_seconds = max(5, int(os.environ.get("TELEGRAM_AI_REPORT_IDLE_SECONDS", "15")))
    error_backoff_seconds = max(5, int(os.environ.get("TELEGRAM_AI_REPORT_ERROR_BACKOFF_SECONDS", "20")))
    print("[TelegramAIReport] command worker started", flush=True)
    try:
        while True:
            result = poll_configured_bots()
            if result["failed"]:
                print(f"[TelegramAIReport] poll failures={result['failed']}", flush=True)
                time.sleep(error_backoff_seconds)
            elif result["bots"] == 0:
                time.sleep(idle_seconds)
    except KeyboardInterrupt:
        print("[TelegramAIReport] command worker stopped", flush=True)


if __name__ == "__main__":
    main()
