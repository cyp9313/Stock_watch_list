# -*- coding: utf-8 -*-
"""模拟用户报错场景：watch+monitor+execute_if=["立即执行"] + reduce 无 material evidence。"""
import sys
sys.path.insert(0, '.')

from portfolio_analysis.validators import validate_portfolio_claims, _validate_action_state_consistency

# 场景1：用户报错的重试后 advice（watch+monitor+execute_if=["立即执行"]）
advice = {
    "actions": [
        {
            "ticker": "SOFI", "action": "watch", "action_timing": "monitor",
            "current_weight": 0.065, "target_weight_min": 0.065, "target_weight_max": 0.065,
            "execute_if": ["当前建议已成立，立即执行"],
            "expected_portfolio_risk_reduction": None,
            "expected_risk_change": None,
            "confidence": 0.62,
            "evidence_ids": [],
        },
    ],
    "executive_summary": ["测试"],
    "portfolio_analysis": {},
    "key_risks": [],
    "watch_items": [],
}
evidence = [
    {"evidence_id": "E001", "ticker": "SOFI", "recency_tier": "fresh_event",
     "materiality_accepted": True, "entity_role": "primary"},
]
hard, soft = validate_portfolio_claims(advice, {"holdings": [{"ticker": "SOFI", "weight": 0.065}]}, {}, evidence)
print("=== 场景1: watch+monitor+execute_if=['立即执行'] ===")
print(f"hard_errors: {hard}")
print(f"soft_warnings: {soft}")
print(f"execute_if after: {advice['actions'][0]['execute_if']}")
assert not any("立即执行" in e for e in hard), "不应有 hard error"
assert advice["actions"][0]["execute_if"] == [], "execute_if 应被自动清理"
print("PASS: 自动清理生效，无 hard error\n")

# 场景2：reduce 无 material evidence（runner 内部，_guard_actions 之前）
advice2 = {
    "actions": [
        {
            "ticker": "WNUC.DE", "action": "reduce", "action_timing": "monitor",
            "current_weight": 0.07, "target_weight_min": 0.05, "target_weight_max": 0.055,
            "execute_if": ["当前建议已成立，立即执行"],
            "confidence": 0.58,
            "evidence_ids": [],
        },
    ],
    "executive_summary": ["测试"],
    "portfolio_analysis": {},
    "key_risks": [],
    "watch_items": [],
}
evidence2 = []  # 无 evidence，无 material evidence
hard2, soft2 = validate_portfolio_claims(advice2, {"holdings": [{"ticker": "WNUC.DE", "weight": 0.07}]}, {}, evidence2)
print("=== 场景2: reduce 无 material evidence（runner 内部）===")
print(f"hard_errors: {hard2}")
print(f"soft_warnings: {soft2}")
print(f"execute_if after: {advice2['actions'][0]['execute_if']}")
# 不应有"无 material evidence"的 hard error（交给 _guard_actions）
assert not any("material evidence" in e for e in hard2), "不应有 material evidence hard error"
# 应自动清理"立即执行"
assert advice2['actions'][0]['execute_if'] == [], "execute_if 应被自动清理"
print("PASS: runner 内部不阻断 material evidence，execute_if 已清理\n")

# 场景3：_guard_actions 降级 reduce→watch
from daily_report.run_portfolio_report import _guard_actions
advice3 = {
    "actions": [
        {
            "ticker": "TSLA", "action": "trim", "action_timing": "act_now",
            "current_weight": 0.05, "target_weight_min": 0.04, "target_weight_max": 0.045,
            "execute_if": ["条件1"],
            "expected_portfolio_risk_reduction": 0.01,
            "confidence": 0.60,
            "evidence_ids": [],
        },
    ],
}
# 无 material evidence
guard_result = _guard_actions(advice3, [], {})
print("=== 场景3: _guard_actions 降级 trim→watch ===")
a = guard_result["actions"][0]
print(f"action: {a['action']}, timing: {a['action_timing']}, execute_if: {a['execute_if']}")
print(f"target: [{a['target_weight_min']}, {a['target_weight_max']}], current: {a['current_weight']}")
print(f"expected_risk_reduction: {a.get('expected_portfolio_risk_reduction')}")
print(f"quantitative_candidate_action: {a.get('quantitative_candidate_action')}")
assert a["action"] == "watch"
assert a["action_timing"] == "monitor"
assert a["execute_if"] == []
assert a["target_weight_min"] == a["current_weight"]
assert a["expected_portfolio_risk_reduction"] is None
assert a.get("quantitative_candidate_action") == "trim"
print("PASS: directional action 降级为 watch，所有字段同步清理\n")

print("ALL SCENARIOS PASSED — 用户报错场景已修复。")
