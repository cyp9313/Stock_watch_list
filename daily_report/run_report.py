#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
APP_ROOT = ROOT.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# Report helper scripts reuse shared app modules such as ticker_mapping.py.
python_path = os.environ.get("PYTHONPATH", "")
os.environ["PYTHONPATH"] = os.pathsep.join(part for part in (str(APP_ROOT), python_path) if part)

from stock_daily_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
