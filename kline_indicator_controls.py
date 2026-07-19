"""Shared Streamlit controls for configurable K-line chart indicators."""

from __future__ import annotations

from typing import Any, Mapping

import streamlit as st

from kline_indicators import MAX_PERIOD, default_indicator_settings, normalize_indicator_settings, validate_indicator_settings


def render_indicator_settings_panel(
    settings: Mapping[str, Any],
    *,
    key_prefix: str,
    apply_label: str,
    reset_label: str,
) -> tuple[dict[str, Any], str | None]:
    """Render a compact form and return ``(settings, action)`` on submit.

    ``action`` is ``"apply"`` or ``"reset"`` only after a successful form
    submission.  Callers own session state and, for signed-in users, storage.
    """
    active = normalize_indicator_settings(settings)
    with st.container(border=True):
        st.markdown("#### Indicator settings")
        st.caption("Apply recalculates from the loaded OHLCV data; no new market-data request is made.")
        with st.form(f"{key_prefix}_form"):
            st.caption("Moving averages")
            ma_values = []
            for index, ma in enumerate(active["moving_averages"], start=1):
                left, right = st.columns(2)
                with left:
                    ma_type = st.selectbox(
                        f"MA {index} type",
                        ["SMA", "EMA"],
                        index=0 if ma["type"] == "SMA" else 1,
                        key=f"{key_prefix}_ma_{index}_type",
                    )
                with right:
                    period = int(st.number_input(
                        f"MA {index} period",
                        min_value=1,
                        max_value=MAX_PERIOD,
                        value=int(ma["period"]),
                        step=1,
                        key=f"{key_prefix}_ma_{index}_period",
                    ))
                ma_values.append({"type": ma_type, "period": period})

            st.caption("MACD")
            macd_cols = st.columns(3)
            with macd_cols[0]:
                fast = int(st.number_input("Fast", min_value=1, max_value=MAX_PERIOD, value=int(active["macd"]["fast"]), key=f"{key_prefix}_macd_fast"))
            with macd_cols[1]:
                slow = int(st.number_input("Slow", min_value=1, max_value=MAX_PERIOD, value=int(active["macd"]["slow"]), key=f"{key_prefix}_macd_slow"))
            with macd_cols[2]:
                signal = int(st.number_input("Signal", min_value=1, max_value=MAX_PERIOD, value=int(active["macd"]["signal"]), key=f"{key_prefix}_macd_signal"))

            st.caption("KDJ")
            kdj_cols = st.columns(3)
            with kdj_cols[0]:
                kdj_period = int(st.number_input("RSV", min_value=1, max_value=MAX_PERIOD, value=int(active["kdj"]["period"]), key=f"{key_prefix}_kdj_period"))
            with kdj_cols[1]:
                k_smoothing = int(st.number_input("K smooth", min_value=1, max_value=MAX_PERIOD, value=int(active["kdj"]["k_smoothing"]), key=f"{key_prefix}_kdj_k"))
            with kdj_cols[2]:
                d_smoothing = int(st.number_input("D smooth", min_value=1, max_value=MAX_PERIOD, value=int(active["kdj"]["d_smoothing"]), key=f"{key_prefix}_kdj_d"))

            rsi_period = int(st.number_input("RSI period", min_value=1, max_value=MAX_PERIOD, value=int(active["rsi"]["period"]), key=f"{key_prefix}_rsi"))
            apply_col, reset_col = st.columns(2)
            with apply_col:
                apply = st.form_submit_button(apply_label, width="stretch")
            with reset_col:
                reset = st.form_submit_button(reset_label, width="stretch")

    if reset:
        return default_indicator_settings(), "reset"
    if apply:
        candidate = {
            "moving_averages": ma_values,
            "macd": {"fast": fast, "slow": slow, "signal": signal},
            "kdj": {"period": kdj_period, "k_smoothing": k_smoothing, "d_smoothing": d_smoothing},
            "rsi": {"period": rsi_period},
        }
        try:
            return validate_indicator_settings(candidate), "apply"
        except ValueError as exc:
            st.error(str(exc))
    return active, None
