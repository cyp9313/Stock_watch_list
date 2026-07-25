"""Minimal browser audio component for short-term MACD alerts.

The component keeps its AudioContext inside one stable Streamlit iframe.  This
lets a user gesture unlock sound once while later fragment refreshes deliver
new alert events without using a server-side audio service.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

import streamlit.components.v1 as components


_COMPONENT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macd_audio_alert_component")
_macd_audio_alert = components.declare_component("macd_audio_alert", path=_COMPONENT_PATH)


def render_macd_audio_alert(
    events: Sequence[Mapping[str, Any]],
    *,
    monitoring_enabled: bool,
    batch_id: str | None,
    duration_seconds: int,
    key: str,
) -> None:
    """Render the persistent audio player and pass it only trusted event data."""
    _macd_audio_alert(
        events=[dict(event) for event in events],
        monitoring_enabled=bool(monitoring_enabled),
        batch_id=str(batch_id or ""),
        duration_seconds=int(duration_seconds),
        key=key,
        default=None,
    )
