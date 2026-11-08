"""Deployment regression checks for the isolated Trading 212 monitor worker."""

from __future__ import annotations

from pathlib import Path


SERVICE = Path(__file__).resolve().parent.parent / "deploy" / "stock-watchlist-trading212-monitor.service"


def test_monitor_service_is_hardened_and_non_root():
    text = SERVICE.read_text(encoding="utf-8")
    assert "ExecStart=/opt/Stock_watch_list/venv/bin/python -m trading212_monitor_worker" in text
    assert "User=stockwatch" in text
    assert "Group=stockwatch" in text
    assert "NoNewPrivileges=true" in text
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=true" in text
    assert "ReadWritePaths=/opt/Stock_watch_list/data" in text
    assert "UMask=0077" in text
    assert "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX" in text


def test_monitor_enabled_toggle_has_a_stable_key_and_visible_state_styles():
    source = (Path(__file__).resolve().parent.parent / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    assert 'key=f"trading212_monitor_enabled_{user[\'id\']}"' in source
    assert 'st-key-trading212_monitor_enabled_' in source
    assert 'Blue = disabled · Red = enabled.' in source
