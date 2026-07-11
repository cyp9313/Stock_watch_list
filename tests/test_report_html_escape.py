"""Regression tests for untrusted text in generated daily-report HTML."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_BUILDER = REPO_ROOT / "daily_report" / "scripts" / "build_report.py"


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag.lower())
        self.attributes.extend((name.lower(), value or "") for name, value in attrs)


def _report_data() -> dict:
    return {
        "LAST_CLOSE": 101.0,
        "PREV_CLOSE": 100.0,
        "CHG": 1.0,
        "PCT": 1.0,
        "chg_sign": "up",
        "chg_arrow": "▲",
        "price_color": "#3fb950\" onclick=\"alert('price')",
        "TICKER": "SAFE</title><script>alert('ticker')</script>",
        "LONG_NAME": "<img src=x onerror=alert('name')>",
        "SHORT_NAME": "<svg/onload=alert('short')>",
        "SECTOR": "<b onmouseover=alert('sector')>sector</b>",
        "EXCHANGE": "<iframe src=javascript:alert('exchange')>",
        "CURRENCY": "USD",
        "EMPLOYEES": 1,
        "TODAY_HIGH": 102.0,
        "TODAY_LOW": 99.0,
        "TODAY_OPEN": 100.0,
        "TODAY_VOL": 1000,
        "FIFTY2W_HI": 120.0,
        "FIFTY2W_LO": 80.0,
        "MARKET_CAP": 10.0,
        "FW_PE": 20.0,
        "TTM_PE": 22.0,
        "TARGET_MEAN": 110.0,
        "TARGET_HI": 115.0,
        "TARGET_LO": 95.0,
        "ANALYST_CNT": 5,
        "BETA": 1.0,
        "DIV_YIELD": 1.2,
        "percentile_52w": 52.5,
        "ma5": 100.0,
        "ma10": 99.0,
        "ma20": 98.0,
        "ma50": 97.0,
        "ma120": 96.0,
        "ma200": 95.0,
        "ma5_pos": ["<img src=x onerror=alert('ma')>", "signal-bull\" onclick=\"alert('ma')"],
        "ma10_pos": ["above", "signal-bull"],
        "ma20_pos": ["above", "signal-bull"],
        "ma50_pos": ["above", "signal-bull"],
        "ma120_pos": ["above", "signal-bull"],
        "ma200_pos": ["above", "signal-bull"],
        "rsi": 55.0,
        "macd_line": 1.0,
        "signal_line": 0.5,
        "hist_val": 0.5,
        "k_val": 60.0,
        "d_val": 50.0,
        "j_val": 70.0,
        "bb_up": 110.0,
        "bb_mid": 100.0,
        "bb_dn": 90.0,
        "bull_ma_count": 6,
        "DESCRIPTION": "</td><script>alert('description')</script>",
        "chip_profile_primary": {
            "ok": True,
            "poc_price": 100.0,
            "poc_distance_pct": 1.0,
            "value_area_low": 95.0,
            "value_area_high": 105.0,
            "overhead_supply_ratio": 0.2,
            "support_volume_ratio": 0.3,
            "chip_score": 60.0,
            "chip_signal": "<svg/onload=alert('chip')>",
        },
        "final_rating": {
            "rating_text": "<script>alert('rating')</script>",
            "rating_class": "buy\" onclick=\"alert('rating-class')",
            "final_score": 70.0,
            "method": "<img src=x onerror=alert('method')>",
            "subscores": {"technical_score": 70.0},
            "score_status": {"technical_score": "<svg/onload=alert('status')>"},
            "effective_weights": {"technical_score": 1.0},
            "instrument_type": "EQUITY\" onclick=\"alert('instrument')",
        },
    }


def _render_report(data: dict, notes: str, report_date: str = "2026-07-11") -> str:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        data_file = temp / "data.json"
        chart_file = temp / "chart.html"
        notes_file = temp / "notes.txt"
        output_file = temp / "report.html"
        data_file.write_text(json.dumps(data), encoding="utf-8")
        chart_file.write_text('<div id="trusted-chart">chart remains interactive</div>', encoding="utf-8")
        notes_file.write_text(notes, encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(REPORT_BUILDER),
                str(data_file),
                str(chart_file),
                str(output_file),
                "--date",
                report_date,
                "--notes",
                str(notes_file),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
        return output_file.read_text(encoding="utf-8")


class ReportHtmlEscapeTests(unittest.TestCase):
    def test_untrusted_metadata_and_notes_are_rendered_as_text(self) -> None:
        rendered = _render_report(
            _report_data(),
            "[BULL] <img src=x onerror=alert('notes')>\n",
            "2026-07-11<script>alert('date')</script>",
        )

        parser = _TagCollector()
        parser.feed(rendered)
        self.assertNotIn("script", parser.tags)
        self.assertNotIn("img", parser.tags)
        self.assertNotIn("svg", parser.tags)
        self.assertFalse(any(name.startswith("on") for name, _ in parser.attributes))
        self.assertIn("&lt;script&gt;alert", rendered)
        self.assertIn("&lt;img src=x onerror=alert", rendered)
        self.assertIn("&lt;svg/onload=alert", rendered)
        self.assertIn('<div id="trusted-chart">chart remains interactive</div>', rendered)
        self.assertIn('rating-badge hold', rendered)
        self.assertIn('style="color:#3fb950"', rendered)

    def test_normal_text_and_trusted_chart_remain_compatible(self) -> None:
        data = _report_data()
        data.update({
            "price_color": "#3fb950",
            "TICKER": "600000.SS",
            "LONG_NAME": "示例 & 公司",
            "SHORT_NAME": "示例",
            "SECTOR": "金融",
            "EXCHANGE": "SSE",
            "DESCRIPTION": "正常说明 & 提示",
        })
        for key in ("ma5_pos", "ma10_pos", "ma20_pos", "ma50_pos", "ma120_pos", "ma200_pos"):
            data[key] = ["均线上方", "signal-bull"]
        data["chip_profile_primary"]["chip_signal"] = "中性"
        data["final_rating"] = {
            "rating_text": "审慎持有",
            "rating_class": "hold",
            "final_score": 70.0,
            "method": "standard_method",
            "subscores": {"technical_score": 70.0},
            "score_status": {"technical_score": "适用"},
            "effective_weights": {"technical_score": 1.0},
            "instrument_type": "EQUITY",
        }

        rendered = _render_report(data, "[MIX] 正常 notes & 数据\n")

        self.assertIn("示例 &amp; 公司", rendered)
        self.assertIn("正常说明 &amp; 提示", rendered)
        self.assertIn("正常 notes &amp; 数据", rendered)
        self.assertIn('<div id="trusted-chart">chart remains interactive</div>', rendered)


if __name__ == "__main__":
    unittest.main()
