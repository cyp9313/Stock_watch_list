"""HTML rendering and escaping coverage for the decision dashboard."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_report_html_escape import _report_data  # noqa: E402


def test_decision_dashboard_renders_safe_evidence_anchors() -> None:
    builder = REPO_ROOT / "daily_report" / "scripts" / "build_report.py"
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        data = root / "data.json"; chart = root / "chart.html"; notes = root / "notes.txt"; decision = root / "decision.json"; output = root / "report.html"
        data.write_text(json.dumps(_report_data()), encoding="utf-8")
        chart.write_text("<div>chart</div>", encoding="utf-8")
        notes.write_text("[BULL] note", encoding="utf-8")
        decision.write_text(json.dumps({
            "one_sentence": "<script>alert(1)</script>", "final_action": "buy\" onclick=alert(2)", "final_score": 63.4,
            "position_advice": {"no_position": "<img src=x>", "has_position": "Hold"},
            "levels": {"ideal_buy": "$98.00–$99.00", "secondary_buy": None, "stop_loss": None, "take_profit": None, "invalidation_condition": "Support fails"},
            "catalysts": [{"text": "Catalyst", "evidence_ids": ["E-001", "<bad>"]}], "risk_alerts": [],
            "action_checklist": ["Observe"], "phase_decision": {"market": "US", "timezone": "America/New_York", "phase": "postmarket", "immediate_action": "Observe"},
            "adjustments": [], "fallback_used": True,
        }), encoding="utf-8")
        completed = subprocess.run([sys.executable, str(builder), str(data), str(chart), str(output), "--notes", str(notes), "--decision-json", str(decision)], capture_output=True, text=True)
        assert completed.returncode == 0, completed.stderr
        html = output.read_text(encoding="utf-8")
    assert "决策仪表盘" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "onclick=alert(2)" not in html
    assert 'href="#evidence-E-001"' in html
    assert "evidence-&lt;bad&gt;" not in html
    assert "确定性回退模板" in html
