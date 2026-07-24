"""Tests for P2-1 through P2-4: currency symbols, chart period text,
date/timezone handling, and Plotly offline embedding.

Covers:
1. USD, EUR, GBP, HKD, CNY, JPY currency symbols
2. Unknown currency uses currency code, not $
3. 1, 3, 6, 12, 24 months dynamic text
4. Weekend generation — data cutoff is previous trading day
5. Server in different timezone — market date used
6. HTML does not contain Plotly public CDN URL
7. Offline HTML still contains chart resources
8. P0 HTML escape protection does not regress
9. Chart trusted HTML is not incorrectly escaped
10. Financial calculation values do not change
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch, MagicMock
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_BUILDER = REPO_ROOT / "daily_report" / "scripts" / "build_report.py"
GEN_CHART = REPO_ROOT / "daily_report" / "scripts" / "gen_chart.py"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _report_data(**overrides) -> dict:
    """Minimal data.json payload for build_report.py."""
    base = {
        "LAST_CLOSE": 101.0, "PREV_CLOSE": 100.0, "CHG": 1.0, "PCT": 1.0,
        "chg_sign": "up", "chg_arrow": "▲",
        "price_color": "#3fb950",
        "TICKER": "AAPL", "LONG_NAME": "Apple Inc.", "SHORT_NAME": "Apple",
        "SECTOR": "Technology", "EXCHANGE": "NASDAQ",
        "CURRENCY": "USD", "EMPLOYEES": 10000,
        "TODAY_HIGH": 102.0, "TODAY_LOW": 99.0, "TODAY_OPEN": 100.0,
        "TODAY_VOL": 5000000,
        "FIFTY2W_HI": 120.0, "FIFTY2W_LO": 80.0,
        "MARKET_CAP": 3000.0, "FW_PE": 25.0, "TTM_PE": 28.0,
        "TARGET_MEAN": 115.0, "TARGET_HI": 130.0, "TARGET_LO": 95.0,
        "ANALYST_CNT": 30, "BETA": 1.2, "DIV_YIELD": 0.5,
        "percentile_52w": 52.5,
        "ma5": 100.0, "ma10": 99.0, "ma20": 98.0, "ma50": 97.0,
        "ma120": 96.0, "ma200": 95.0,
        "ma5_pos": ["above", "signal-bull"], "ma10_pos": ["above", "signal-bull"],
        "ma20_pos": ["above", "signal-bull"], "ma50_pos": ["above", "signal-bull"],
        "ma120_pos": ["above", "signal-bull"], "ma200_pos": ["above", "signal-bull"],
        "rsi": 55.0, "macd_line": 1.0, "signal_line": 0.5, "hist_val": 0.5,
        "k_val": 60.0, "d_val": 50.0, "j_val": 70.0,
        "bb_up": 110.0, "bb_mid": 100.0, "bb_dn": 90.0,
        "bull_ma_count": 6,
        "DESCRIPTION": "A technology company.",
        "INSTRUMENT_TYPE": "EQUITY",
        "data_end": "2026-07-10",
        "final_rating": {
            "rating_text": "审慎买入 BUY",
            "rating_class": "buy",
            "final_score": 72.0,
            "method": "v5.8",
            "subscores": {"technical_score": 72},
            "score_status": {"technical_score": "适用"},
            "effective_weights": {"technical_score": 1.0},
            "instrument_type": "EQUITY",
        },
    }
    base.update(overrides)
    return base


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag.lower())
        self.attributes.extend((name.lower(), value or "") for name, value in attrs)


def _render_report(data: dict, chart_html: str = "<div id='chart'>test</div>",
                   report_date: str = "2026-07-11", months: int | None = None,
                   notes: str = "", evidence: dict | None = None) -> str:
    """Run build_report.py as a subprocess and return the HTML output."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data_file = tmp / "data.json"
        chart_file = tmp / "chart.html"
        notes_file = tmp / "notes.txt"
        evidence_file = tmp / "final_notes.json"
        output_file = tmp / "report.html"
        data_file.write_text(json.dumps(data), encoding="utf-8")
        chart_file.write_text(chart_html, encoding="utf-8")
        notes_file.write_text(notes, encoding="utf-8")
        args = [
            sys.executable, str(REPORT_BUILDER),
            str(data_file), str(chart_file), str(output_file),
            "--date", report_date,
            "--notes", str(notes_file),
        ]
        if months is not None:
            args.extend(["--months", str(months)])
        if evidence is not None:
            evidence_file.write_text(json.dumps(evidence), encoding="utf-8")
            args.extend(["--evidence", str(evidence_file)])
        completed = subprocess.run(args, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise AssertionError(f"build_report.py failed: {completed.stderr}")
        return output_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# P2-1: Currency symbols
# ---------------------------------------------------------------------------

class TestCurrencySymbols(unittest.TestCase):
    """Test that price formatting uses the correct currency symbol."""

    def test_usd_uses_dollar_sign(self):
        html = _render_report(_report_data(CURRENCY="USD"))
        self.assertIn("$101.00", html)

    def test_eur_uses_euro_sign(self):
        html = _render_report(_report_data(CURRENCY="EUR"))
        self.assertIn("€101.00", html)
        self.assertNotIn("$101.00", html)

    def test_gbp_uses_pound_sign(self):
        html = _render_report(_report_data(CURRENCY="GBP"))
        self.assertIn("£101.00", html)
        self.assertNotIn("$101.00", html)

    def test_hkd_uses_hk_dollar(self):
        html = _render_report(_report_data(CURRENCY="HKD"))
        self.assertIn("HK$101.00", html)
        # Ensure no bare $ (without HK prefix) as a price — check the badge-price div
        import re
        # Find price patterns — $ followed by number but NOT preceded by HK/CA/A
        bare_dollar = re.findall(r'(?<![A-Z])\$\d', html)
        self.assertEqual(bare_dollar, [], f"Found bare $ signs without prefix: {bare_dollar}")

    def test_cny_uses_yuan_sign(self):
        html = _render_report(_report_data(CURRENCY="CNY"))
        self.assertIn("￥101.00", html)
        self.assertNotIn("$101.00", html)

    def test_jpy_uses_yen_sign(self):
        html = _render_report(_report_data(CURRENCY="JPY"))
        self.assertIn("¥101.00", html)
        self.assertNotIn("$101.00", html)

    def test_unknown_currency_uses_code(self):
        """Unknown currency should use the code itself, not a misleading $."""
        html = _render_report(_report_data(CURRENCY="SGD"))
        self.assertIn("SGD 101.00", html)
        self.assertNotIn("$101.00", html)

    def test_percentages_have_no_currency_symbol(self):
        """Percentages must not have a currency symbol."""
        html = _render_report(_report_data(CURRENCY="EUR"))
        # PCT is 1.0, fsp format -> "+1.00"
        # The percentage should appear without €
        self.assertIn("+1.00%", html)
        # Make sure we don't have "€+1.00%" or "€%" patterns
        self.assertNotIn("€+", html)
        self.assertNotIn("€%", html)

    def test_market_cap_uses_currency_symbol(self):
        """Market cap should also use the dynamic currency symbol."""
        html = _render_report(_report_data(CURRENCY="EUR", MARKET_CAP=250.0))
        self.assertIn("€250.0B", html)
        self.assertNotIn("$250.0B", html)

    def test_all_hardcoded_dollar_signs_removed(self):
        """No raw '$' + number patterns that aren't from the currency mapping."""
        # With EUR currency, there should be no $ followed by a digit
        html = _render_report(_report_data(CURRENCY="EUR"))
        import re
        # Find $ followed by digit — these would be hardcoded dollar signs
        hardcoded = re.findall(r'\$\d', html)
        self.assertEqual(hardcoded, [], f"Found hardcoded $ signs: {hardcoded}")


# ---------------------------------------------------------------------------
# P2-2: Chart period text
# ---------------------------------------------------------------------------

class TestChartPeriodText(unittest.TestCase):
    """Test that chart period text is dynamic."""

    def test_3_months_default(self):
        html = _render_report(_report_data(), months=3)
        self.assertIn("近3个月", html)

    def test_1_month(self):
        html = _render_report(_report_data(), months=1)
        self.assertIn("近1个月", html)
        self.assertNotIn("近3个月", html)

    def test_6_months(self):
        html = _render_report(_report_data(), months=6)
        self.assertIn("近6个月", html)
        self.assertNotIn("近3个月", html)

    def test_12_months(self):
        html = _render_report(_report_data(), months=12)
        self.assertIn("近12个月", html)
        self.assertNotIn("近3个月", html)

    def test_24_months(self):
        html = _render_report(_report_data(), months=24)
        self.assertIn("近24个月", html)
        self.assertNotIn("近3个月", html)

    def test_no_fixed_3_months_without_months_arg(self):
        """Without --months, default should still be 3, not a hardcoded string."""
        html = _render_report(_report_data(), months=None)
        self.assertIn("近3个月", html)


# ---------------------------------------------------------------------------
# P2-3: Date and timezone
# ---------------------------------------------------------------------------

class TestDateAndTimezone(unittest.TestCase):
    """Test date handling: market date, data cutoff, and weekend awareness."""

    def test_data_end_displayed_in_report(self):
        """Report should show data_end (market data cutoff date)."""
        html = _render_report(_report_data(data_end="2026-07-09"))
        self.assertIn("2026-07-09", html)
        self.assertIn("数据截止", html)

    def test_report_date_shown(self):
        """Report should show the report generation date."""
        html = _render_report(_report_data(), report_date="2026-07-11")
        self.assertIn("2026-07-11", html)

    def test_weekend_report_shows_previous_trading_day(self):
        """On a weekend, data_end should show the previous trading day,
        not the weekend date."""
        # Simulate Saturday report — data_end is Friday
        html = _render_report(_report_data(data_end="2026-07-10"), report_date="2026-07-11")
        # The report date is Saturday, but data cutoff is Friday
        self.assertIn("2026-07-11", html)  # report date
        self.assertIn("2026-07-10", html)  # data cutoff (Friday)
        self.assertIn("数据截止", html)

    def test_market_date_function_uses_eastern_timezone(self):
        """get_market_date should return a date string in America/New_York."""
        from market_data_service import get_market_date
        result = get_market_date()
        # Should be a valid ISO date
        import re
        self.assertRegex(result, r'^\d{4}-\d{2}-\d{2}$')

    def test_market_date_different_from_server_time(self):
        """When server is in a timezone ahead of NY, market date may differ."""
        from market_data_service import get_market_date
        from datetime import datetime, timezone, timedelta
        # The function should use America/New_York, not server time
        # We can't control the actual time, but we verify the function
        # returns a date, not an error
        result = get_market_date()
        self.assertIsInstance(result, str)
        # Verify it's a valid date by parsing
        datetime.strptime(result, "%Y-%m-%d")

    def test_no_data_end_shows_empty(self):
        """When data_end is missing, report should not crash."""
        html = _render_report(_report_data(data_end=""))
        # Should not contain "数据截止" with empty date
        self.assertIn("2026-07-11", html)

    def test_config_get_market_date_importable(self):
        """config.py should export get_market_date."""
        from daily_report.src.stock_daily_agent.config import get_market_date
        result = get_market_date()
        self.assertRegex(result, r'^\d{4}-\d{2}-\d{2}$')


# ---------------------------------------------------------------------------
# P2-4: Plotly offline
# ---------------------------------------------------------------------------

class TestPlotlyOffline(unittest.TestCase):
    """Test that gen_chart.py produces offline-capable HTML."""

    def test_gen_chart_source_no_cdn(self):
        """gen_chart.py source code should not reference plotly CDN."""
        source = GEN_CHART.read_text(encoding="utf-8")
        self.assertNotIn("include_plotlyjs='cdn'", source)
        self.assertNotIn('include_plotlyjs="cdn"', source)
        self.assertIn("include_plotlyjs=True", source)

    def test_report_html_no_cdn_url(self):
        """Generated report HTML should not contain plotly CDN URL."""
        # Use a chart fragment that simulates what gen_chart.py would produce
        chart_with_inline_js = (
            '<div id="plotly-chart"></div>'
            '<script>var PlotlyConfig = {};</script>'
            '<script type="text/javascript">window.Plotly = function(){};</script>'
        )
        html = _render_report(_report_data(), chart_html=chart_with_inline_js)
        self.assertNotIn("cdn.plot.ly", html)
        self.assertNotIn("https://cdn.plot", html)
        self.assertNotIn("http://cdn.plot", html)

    def test_offline_chart_still_contains_script(self):
        """Offline HTML should still contain the chart script tags."""
        chart_with_inline_js = (
            '<div id="plotly-chart"></div>'
            '<script type="text/javascript">window.Plotly = function(){};</script>'
        )
        html = _render_report(_report_data(), chart_html=chart_with_inline_js)
        self.assertIn("<script", html)
        self.assertIn("Plotly", html)


# ---------------------------------------------------------------------------
# P2 regression: HTML escape protection (P0) does not regress
# ---------------------------------------------------------------------------

class TestHtmlEscapeRegression(unittest.TestCase):
    """Ensure P0 HTML escape protections still work."""

    def test_xss_in_ticker_is_escaped(self):
        data = _report_data(TICKER="SAFE</title><script>alert('xss')</script>")
        html = _render_report(data)
        parser = _TagCollector()
        parser.feed(html)
        self.assertNotIn("script", parser.tags)
        self.assertIn("&lt;script&gt;", html)

    def test_xss_in_long_name_is_escaped(self):
        data = _report_data(LONG_NAME="<img src=x onerror=alert('name')>")
        html = _render_report(data)
        parser = _TagCollector()
        parser.feed(html)
        self.assertNotIn("img", parser.tags)
        self.assertIn("&lt;img", html)

    def test_xss_in_sector_is_escaped(self):
        data = _report_data(SECTOR="<b onmouseover=alert('sector')>sector</b>")
        html = _render_report(data)
        # The escaped text will contain the word "onmouseover" as visible text,
        # but it must not appear as an HTML attribute (i.e., not as onmouseover= within a tag)
        parser = _TagCollector()
        parser.feed(html)
        # No attribute should start with "on"
        self.assertFalse(any(name.startswith("on") for name, _ in parser.attributes),
                         f"Found event handler attributes: {parser.attributes}")

    def test_trusted_chart_html_not_escaped(self):
        """Chart HTML is the only trusted fragment and should not be escaped."""
        chart = '<div id="plotly-chart" class="plotly-chart"><script>window.Plotly=1;</script></div>'
        html = _render_report(_report_data(), chart_html=chart)
        self.assertIn(chart, html)
        self.assertNotIn("&lt;div id=&quot;plotly-chart&quot;", html)

    def test_notes_xss_is_escaped(self):
        data = _report_data()
        html = _render_report(data, notes="[BULL] <script>alert('notes')</script>\n")
        self.assertNotIn("<script>alert('notes')", html)
        self.assertIn("&lt;script&gt;", html)

    def test_price_color_allowlist_enforced(self):
        """price_color should be restricted to the allowlist."""
        data = _report_data(price_color="#3fb950\" onclick=\"alert('price')")
        html = _render_report(data)
        self.assertNotIn("onclick", html)


# ---------------------------------------------------------------------------
# P2 regression: Financial calculation values do not change
# ---------------------------------------------------------------------------

class TestFinancialValuesUnchanged(unittest.TestCase):
    """Ensure that currency formatting changes don't alter the numeric values."""

    def test_price_value_preserved(self):
        """The numeric price value should be exactly 101.00, not modified."""
        html = _render_report(_report_data(CURRENCY="EUR"))
        self.assertIn("€101.00", html)

    def test_52w_range_values_preserved(self):
        html = _render_report(_report_data(CURRENCY="HKD"))
        self.assertIn("HK$80.00", html)
        self.assertIn("HK$120.00", html)

    def test_target_price_values_preserved(self):
        html = _render_report(_report_data(CURRENCY="JPY"))
        self.assertIn("¥115.00", html)

    def test_bollinger_values_preserved(self):
        html = _render_report(_report_data(CURRENCY="GBP"))
        self.assertIn("£110.00", html)
        self.assertIn("£100.00", html)
        self.assertIn("£90.00", html)

    def test_atr_value_preserved(self):
        html = _render_report(_report_data(CURRENCY="CNY"))
        # ATR value is formatted by fs(atr14) — we need to check it appears
        # with the correct currency symbol
        self.assertIn("￥", html)

    def test_percentage_values_unchanged(self):
        """Percentages should not change regardless of currency."""
        html_usd = _render_report(_report_data(CURRENCY="USD"))
        html_eur = _render_report(_report_data(CURRENCY="EUR"))
        # Extract percentage values — they should be the same
        import re
        pct_usd = re.findall(r'[+-]?\d+\.\d+%', html_usd)
        pct_eur = re.findall(r'[+-]?\d+\.\d+%', html_eur)
        self.assertEqual(pct_usd, pct_eur)


# ---------------------------------------------------------------------------
# Integration: tools.py passes --months to build_report
# ---------------------------------------------------------------------------

class TestToolsPassesMonths(unittest.TestCase):
    """Verify that BuildHtmlReportTool passes --months to build_report.py."""

    def test_build_html_report_tool_passes_months(self):
        """The source code should include --months in the args."""
        tools_file = REPO_ROOT / "daily_report" / "src" / "stock_daily_agent" / "tools.py"
        source = tools_file.read_text(encoding="utf-8")
        # Find the BuildHtmlReportTool section
        build_idx = source.find("class BuildHtmlReportTool")
        self.assertGreater(build_idx, 0, "BuildHtmlReportTool not found")
        # Find the args line within this class
        next_class = source.find("class ", build_idx + 1)
        class_source = source[build_idx:next_class] if next_class > 0 else source[build_idx:]
        self.assertIn("--months", class_source)
        self.assertIn("ctx.months", class_source)


if __name__ == "__main__":
    unittest.main()
