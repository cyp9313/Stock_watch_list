from pathlib import Path


def test_relative_momentum_columns_place_a_sparkline_after_each_value_column():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")

    assert "RELATIVE_MOMENTUM_VALUE_COLUMNS = RELATIVE_RETURN_COLUMNS + [RELATIVE_MOMENTUM_COLUMN]" in source
    assert '"20D Rel%": "20D Rel (20)"' in source
    assert '"60D Rel%": "60D Rel (20)"' in source
    assert '"120D Rel%": "120D Rel (20)"' in source
    assert 'RELATIVE_MOMENTUM_COLUMN: "3/6/12M Rel (20)"' in source
    assert "for column in (metric_column, RELATIVE_SPARKLINE_BY_METRIC[metric_column])" in source
    assert "hidden_columns.update(RELATIVE_MOMENTUM_COLUMNS)" in source
    assert "col in RELATIVE_SPARKLINE_COLUMNS else html.escape(val)" in source


def test_single_user_relative_momentum_value_has_its_own_sparkline_column():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit.py").read_text(encoding="utf-8")

    assert '"3/6/12M Rel%", "3/6/12M Rel (20)"' in source
    assert "col == '3/6/12M Rel (20)' else html.escape(str(val))" in source
