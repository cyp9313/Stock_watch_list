import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from daily_report.src.stock_daily_agent.notes import NewsNote


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "daily_report" / "scripts" / "build_report.py"
SPEC = importlib.util.spec_from_file_location("build_report", SCRIPT)
build_report = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(build_report)


def report_data():
    return {
        "LAST_CLOSE": 100.0, "PREV_CLOSE": 99.0, "CHG": 1.0, "PCT": 1.01,
        "chg_sign": "+", "chg_arrow": "↑", "price_color": "#3fb950",
        "TICKER": "ACME<script>alert(1)</script>",
        "LONG_NAME": "普通中文公司<script>alert(1)</script>",
        "SHORT_NAME": "ACME", "SECTOR": "科技", "EXCHANGE": "NASDAQ",
        "CURRENCY": "USD", "EMPLOYEES": 1000,
        "TODAY_HIGH": 101.0, "TODAY_LOW": 98.0, "TODAY_OPEN": 99.5,
        "TODAY_VOL": 1000000, "FIFTY2W_HI": 120.0, "FIFTY2W_LO": 80.0,
        "MARKET_CAP": 2.5, "FW_PE": 20.0, "TTM_PE": 21.0,
        "TARGET_MEAN": 110.0, "TARGET_HI": 125.0, "TARGET_LO": 95.0,
        "ANALYST_CNT": 10, "BETA": 1.0, "DIV_YIELD": 0.0,
        "percentile_52w": 50.0,
        "ma5": 100.0, "ma10": 99.0, "ma20": 98.0, "ma50": 97.0,
        "ma120": 96.0, "ma200": 95.0,
        "ma5_pos": ["<img onerror=alert(1)>", "signal-bull"],
        "ma10_pos": ["正常", "signal-bull"], "ma20_pos": ["正常", "signal-bull"],
        "ma50_pos": ["正常", "signal-bull"], "ma120_pos": ["正常", "signal-bull"],
        "ma200_pos": ["正常", "signal-bull"],
        "rsi": 55.0, "macd_line": 1.0, "signal_line": 0.5, "hist_val": 0.5,
        "k_val": 60.0, "d_val": 50.0, "j_val": 80.0, "bb_up": 105.0,
        "bb_mid": 100.0, "bb_dn": 95.0, "bull_ma_count": 5,
        "DESCRIPTION": '描述含 <b>HTML</b> 与 "双引号"，金额 ¥1,000。',
        "INSTRUMENT_TYPE": "EQUITY",
        "final_rating": {
            "rating_text": '<img onerror=alert(1)>', "rating_class": 'hold" onclick="alert(1)',
            "final_score": 75.0, "method": '<img onerror=alert(1)>',
            "instrument_type": "EQUITY", "subscores": {"technical_score": 75},
            "score_status": {"technical_score": '<img onerror=alert(1)>'},
            "effective_weights": {"technical_score": 1.0},
        },
    }


class BuildReportHtmlSafetyTests(unittest.TestCase):
    def test_final_report_escapes_untrusted_content_and_preserves_plotly(self):
        note = NewsNote(
            tag="BULL",
            title='<img onerror=alert(1)>', fact='<img onerror=alert(2)>',
            logic='<img onerror=alert(3)>', investment_meaning='<img onerror=alert(4)>',
            source='普通来源 https://example.com/研究?currency=¥',
            url='javascript:alert(1)',
        )
        chart = '<div id="plotly-chart"></div><script>window.PLOTLY_TEST = true;</script>'
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            data_file = directory / "data.json"
            chart_file = directory / "chart.html"
            notes_file = directory / "notes.txt"
            output_file = directory / "report.html"
            data_file.write_text(json.dumps(report_data(), ensure_ascii=False), encoding="utf-8")
            chart_file.write_text(chart, encoding="utf-8")
            notes_file.write_text(note.render(), encoding="utf-8")

            build_report.main([str(data_file), str(chart_file), str(output_file), "--notes", str(notes_file)])
            html = output_file.read_text(encoding="utf-8")

        self.assertIn("普通中文公司&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("描述含 &lt;b&gt;HTML&lt;/b&gt; 与 &quot;双引号&quot;，金额 ¥1,000。", html)
        self.assertNotIn('<img onerror=alert(', html)
        self.assertIn('&lt;img onerror=alert(1)&gt;', html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn('onclick="alert(1)', html)
        self.assertIn("https://example.com/研究?currency=¥", html)
        self.assertIn(chart, html)
        self.assertNotIn('&lt;div id=&quot;plotly-chart&quot;', html)


if __name__ == "__main__":
    unittest.main()
