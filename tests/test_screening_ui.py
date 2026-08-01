from pathlib import Path


def test_screening_results_use_stable_html_table_renderer():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    history_start = source.index("def render_screening_history")
    history_end = source.index("\ndef render_market_breadth_panel", history_start)
    history = source[history_start:history_end]
    assert "_render_screening_table(" in history
    assert "st.dataframe" not in history
    assert "html.escape(str(value))" in source


def test_screening_table_has_strategy_specific_metric_columns():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    mapping_start = source.index("SCREENING_METRIC_COLUMNS")
    mapping_end = source.index("\ndef _screening_display_rows", mapping_start)
    mapping = source[mapping_start:mapping_end]
    assert '"trend_quality"' in mapping
    assert '"20D Relative%"' in mapping
    assert '"volume_breakout"' in mapping
    assert '"20D Breakout%"' in mapping
    assert '"oversold_reversal"' in mapping
    assert '"BB Lower Distance%"' in mapping
    assert '"PE TTM"' in mapping
    assert '"Forward PE"' in mapping


def test_screening_tables_document_rules_and_reuse_watchlist_affordances():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    assert "SCREENING_RULE_EXPLANATIONS" in source
    assert "render_screening_rule_explanation(strategy_key)" in source
    assert "ticker_tooltip(row.get(\"_Name\", \"\"), row.get(\"_Beta\", np.nan))" in source
    assert "position:sticky;left:0" in source
    assert "beta_color(row.get(\"_Beta\", np.nan))" in source
    assert "_format_berlin_datetime(run['created_at'])" in source


def test_screening_rule_explanations_use_real_markdown_line_breaks():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    explanations = source[source.index("SCREENING_RULE_EXPLANATIONS"):source.index("\ndef render_screening_rule_explanation")]
    assert "\\\\n+**" not in explanations
    assert '"**基础分（100）：' in explanations


def test_screening_headers_have_metric_tooltips_and_market_cap_formatting():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    assert "SCREENING_COLUMN_TOOLTIPS" in source
    assert '"Forward PE":' in source
    assert '"Market Cap": "市值，来自当日基本面 SQLite 缓存；以科学计数法显示' in source
    assert 'f"{float(numeric):.2e}"' in source
    assert "SCREENING_COLUMN_TOOLTIPS.get(str(column), str(column))" in source


def test_screening_turnover_and_valuation_cells_have_explicit_formatting_rules():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    assert 'display_name in {"20D Avg Turnover", "Market Cap"}' in source
    assert 'blue_color(numeric if numeric > 0 else 50.0, clip=50.0)' in source
    assert 'return red_green(1.5 - numeric, neg_clip=-6.5, pos_clip=1.5)' in source


def test_all_watchlist_table_headers_use_shared_metric_tooltips():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    assert "WATCHLIST_COLUMN_TOOLTIPS" in source
    assert '"MACD Diff‱"' in source
    assert 'html.escape(watchlist_column_tooltip(col), quote=True)' in source
    assert 'html.escape(watchlist_column_tooltip(column), quote=True)' in source
