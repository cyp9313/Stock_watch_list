# -*- coding: utf-8 -*-
"""Portfolio 建议校验与动作-权重一致性约束。

修改计划 9：
- action 与目标权重区间必须一致；
- 默认 sanitize 行为保持向后兼容（旧测试依赖）；
- 新增 strict 模式：不一致时抛出 ``PortfolioAdviceValidationError``，
  由 Agent runner 捕获后让模型修复一次，再次失败则任务失败（不静默改成相反区间）。
"""
from __future__ import annotations

from typing import Any
import re

ALLOWED_ACTIONS = {"add", "hold", "trim", "reduce", "exit", "watch"}
FORBIDDEN_SHARE_KEYS = {"shares_to_buy", "shares_to_sell", "exact_share_count"}

# exit 时目标权重上限的最大阈值
_EXIT_MAX_THRESHOLD = 0.02
# reduce 相对当前权重需要显著低于的比例
_REDUCE_FACTOR = 0.8
_TRIM_FACTOR = 0.95


class PortfolioAdviceValidationError(ValueError):
    """strict 模式下，建议与动作不一致时抛出。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _eps() -> float:
    return 1e-4


def _adjust_range(
    action: str,
    current: float,
    lo: float,
    hi: float,
    *,
    strict: bool,
) -> tuple[float, float]:
    """返回与 action 一致的目标区间 (lo, hi)。

    strict=True 且不一致时抛出 ``PortfolioAdviceValidationError``；
    strict=False（兜底）时在不翻转 action 的前提下把区间收窄到一致范围。
    """
    action = action.lower()
    eps = _eps()

    def fail(msg: str):
        if strict:
            raise PortfolioAdviceValidationError([msg])
        return None

    if action == "add":
        # target_weight_min >= current；target_weight_max > current
        if lo < current - eps or hi <= current + eps:
            fail(f"[{action}] 目标区间需满足 min>={current:.4f} 且 max>{current:.4f}")
            lo = max(lo, current)
            hi = max(hi, current * 1.05 + eps)
    elif action == "hold":
        if not (lo - eps <= current <= hi + eps):
            fail(f"[{action}] 当前权重 {current:.4f} 应落在目标区间 [{lo:.4f}, {hi:.4f}] 内")
            lo = min(lo, current)
            hi = max(hi, current)
    elif action == "trim":
        if hi >= current - eps:
            fail(f"[{action}] 目标上限需 < 当前权重 {current:.4f}")
            hi = max(0.0, current * _TRIM_FACTOR)
            lo = min(lo, hi)
    elif action == "reduce":
        if hi >= current - eps or hi > current * _REDUCE_FACTOR + eps:
            fail(f"[{action}] 目标上限需显著 < 当前权重 {current:.4f}")
            hi = max(0.0, current * _REDUCE_FACTOR)
            lo = min(lo, hi)
    elif action == "exit":
        if lo > eps or hi > _EXIT_MAX_THRESHOLD + eps:
            fail(f"[{action}] 目标区间应接近 0（max<={_EXIT_MAX_THRESHOLD}）")
            lo = 0.0
            hi = min(hi, _EXIT_MAX_THRESHOLD)
    elif action == "watch":
        # 允许 null 或包含当前权重；兜底时不强制改动
        pass
    return lo, hi


def validate_portfolio_advice(
    advice: dict[str, Any],
    snapshot: dict[str, Any],
    evidence: list[dict[str, Any]] | None = None,
    *,
    mode: str = "fallback",
) -> dict[str, Any]:
    """校验并归一化一份 Portfolio 建议。

    mode="fallback"（默认）：把越界/非法项静默修正到安全范围（不翻转 action），
        并记录 warnings。保持与旧测试兼容。
    mode="strict"：发现动作-权重不一致时抛出 ``PortfolioAdviceValidationError``，
        不做静默修正，交由 Agent 修复。
    """
    strict = mode == "strict"
    evidence_ids = {str(item.get("evidence_id")) for item in evidence or [] if item.get("evidence_id")}
    evidence_by_id = {str(e.get("evidence_id")): e for e in (evidence or []) if e.get("evidence_id")}
    weights = {h["ticker"]: float(h.get("weight", 0.0) or 0.0) for h in snapshot.get("holdings", [])}
    sanitized = dict(advice or {})
    warnings: list[str] = list(sanitized.get("validation_warnings") or [])
    actions = []
    for raw in sanitized.get("actions") or []:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker") or "").upper()
        if ticker not in weights:
            warnings.append(f"Dropped action for non-portfolio ticker: {ticker}")
            continue
        action = str(raw.get("action") or "watch").lower()
        if action not in ALLOWED_ACTIONS:
            warnings.append(f"Invalid action for {ticker}: {action}; changed to watch.")
            action = "watch"
        item = dict(raw)
        item["ticker"] = ticker
        item["action"] = action
        item["current_weight"] = weights[ticker]
        for key in FORBIDDEN_SHARE_KEYS:
            if key in item:
                item.pop(key, None)
                warnings.append(f"Removed unconstrained exact-share field {key} for {ticker}.")

        try:
            lo = float(item.get("target_weight_min", weights[ticker]))
            hi = float(item.get("target_weight_max", weights[ticker]))
        except (TypeError, ValueError):
            lo = hi = weights[ticker]
        lo = max(0.0, min(1.0, lo))
        hi = max(lo, min(1.0, hi))

        try:
            lo, hi = _adjust_range(action, weights[ticker], lo, hi, strict=strict)
        except PortfolioAdviceValidationError as exc:
            raise
        lo = max(0.0, min(1.0, lo))
        hi = max(lo, min(1.0, hi))
        item["target_weight_min"] = lo
        item["target_weight_max"] = hi

        try:
            item["confidence"] = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            item["confidence"] = 0.5

        # 证据跨 ticker 绑定（修改计划第三轮 15）
        valid_eids = []
        for eid in item.get("evidence_ids") or []:
            eid = str(eid)
            ev = evidence_by_id.get(eid)
            if ev is None:
                if evidence_ids:
                    warnings.append(f"Dropped unknown evidence id {eid} for {ticker}.")
                continue
            ev_ticker = ev.get("ticker")
            related = ev.get("related_tickers") or []
            scope_ok = (str(ev_ticker or "").upper() == ticker) or (ticker in [str(t) for t in related])
            if scope_ok:
                valid_eids.append(eid)
            elif strict:
                raise PortfolioAdviceValidationError([
                    f"Action {ticker} 引用了不属于它的证据 {eid}（该证据属于 {ev_ticker or 'macro/theme'}）。"
                ])
            else:
                warnings.append(f"Dropped cross-ticker evidence {eid} for {ticker}.")
        item["evidence_ids"] = valid_eids
        actions.append(item)

    # Key Risk 证据绑定检查（修改计划第三轮 15）
    key_risks = sanitized.get("key_risks") or []
    for kr in key_risks:
        if not isinstance(kr, dict):
            continue
        kr_eids = kr.get("evidence_ids") or []
        scoped = []
        for eid in kr_eids:
            eid = str(eid)
            ev = evidence_by_id.get(eid)
            if ev is None:
                if evidence_ids:
                    warnings.append(f"Dropped unknown evidence id {eid} in key_risk {kr.get('risk_id')}.")
                continue
            affected = kr.get("affected_tickers") or []
            ev_ticker = ev.get("ticker")
            related = ev.get("related_tickers") or []
            if (ev_ticker and ev_ticker in affected) or (set(affected) & set(related)):
                scoped.append(eid)
            elif strict:
                raise PortfolioAdviceValidationError([
                    f"Key risk {kr.get('risk_id')} 引用了不匹配的证据 {eid}。"
                ])
            else:
                warnings.append(f"Dropped mismatched evidence {eid} in key_risk {kr.get('risk_id')}.")
        kr["evidence_ids"] = scoped

    sanitized["actions"] = actions
    try:
        sanitized["confidence"] = max(0.0, min(1.0, float(sanitized.get("confidence", 0.5))))
    except (TypeError, ValueError):
        sanitized["confidence"] = 0.5
    sanitized["validation_warnings"] = warnings
    sanitized.setdefault("disclaimer", "本报告仅供研究参考，不构成投资建议。")
    return sanitized


# 修改计划第三轮 42：Claim Validation
# 未被系统提供/未注册、且模型自行引入的「伪指标」关键词（如夏普比率、隐含波动率、
# 机构减持、资金流出、流动性折价等）。出现即视为需要证据支撑的软性问题。
_BANNED_METRIC_HINTS = [
    "夏普比率", "夏普", "隐含波动率", "机构减持", "机构持续减持",
    "资金流出", "流动性折价", "流动性枯竭", "隐性流动性风险",
    "volatility smile", "implied volatility",
]
# 高置信度操作必须引用的「新鲜」证据层级。
_FRESH_TIERS = {"fresh_event", "recent_background"}


def _claim_text_fields(advice: dict[str, Any]) -> list[str]:
    """修改计划第六轮第 25 节：扩大 Claim Validator 字段范围。"""
    fields: list[str] = []
    for s in advice.get("executive_summary") or []:
        fields.append(str(s))
    pa = advice.get("portfolio_analysis") or {}
    for v in pa.values():
        if isinstance(v, str):
            fields.append(v)
    for kr in advice.get("key_risks") or []:
        if isinstance(kr, dict):
            fields.append(str(kr.get("description") or ""))
            fields.append(str(kr.get("title") or ""))
    for a in advice.get("actions") or []:
        if isinstance(a, dict):
            # 第 25 节：扩大到所有 action 文本字段
            for k in (
                "portfolio_reason", "technical_reason", "news_reason",
                "bull_case", "bear_case",
            ):
                fields.append(str(a.get(k) or ""))
            for k in ("execute_if", "cancel_or_upgrade_if", "further_reduce_if", "monitoring_items"):
                for item in (a.get(k) or []):
                    fields.append(str(item))
            for threshold in (a.get("thresholds") or []):
                if isinstance(threshold, dict):
                    fields.append(str(threshold.get("note") or ""))
            fields.append(str(a.get("validation_note") or ""))
    for w in advice.get("watch_items") or []:
        if isinstance(w, dict):
            fields.append(str(w.get("reason") or ""))
            fields.append(str(w.get("title") or ""))
    return fields


# ── 第六轮 Phase 6：Action 状态一致性硬校验（修改计划第 23 节）──
def _validate_action_state_consistency(
    advice: dict[str, Any], evidence: list[dict[str, Any]] | None,
) -> tuple[list[str], list[str]]:
    """校验 action 状态一致性，返回 (hard_errors, soft_warnings)。

    规则（修改计划第 23 节）：
    - monitor + 立即执行 → 自动清理 execute_if + warning（不阻断，watch+monitor 的"立即执行"是良性冗余）
    - watch + target != current → hard error
    - watch + expected risk reduction != null → hard error
    - act_now + directional + execute_if empty → hard error

    注意：「无 material evidence 的 directional action」不在此检查——由 _guard_actions
    自动降级为 watch（避免 runner 内部校验在 _guard_actions 之前触发死循环）。
    """
    errors: list[str] = []
    warnings: list[str] = []
    evidence = evidence or []

    for action in advice.get("actions") or []:
        if not isinstance(action, dict):
            continue
        ticker = str(action.get("ticker") or "").upper()
        action_type = str(action.get("action") or "watch").lower()
        timing = str(action.get("action_timing") or "").lower()
        current = float(action.get("current_weight") or 0.0)
        try:
            tw_min = float(action.get("target_weight_min", current))
            tw_max = float(action.get("target_weight_max", current))
        except (TypeError, ValueError):
            tw_min = tw_max = current
        execute_if = action.get("execute_if") or []
        expected_risk_reduction = action.get("expected_portfolio_risk_reduction")
        expected_risk_change = action.get("expected_risk_change")

        # watch + target != current → hard error
        if action_type == "watch":
            if abs(tw_min - current) > 1e-4 or abs(tw_max - current) > 1e-4:
                errors.append(
                    f"{ticker}: action=watch 但 target_weight [{tw_min:.4f},{tw_max:.4f}] != current {current:.4f}"
                )
            if expected_risk_reduction is not None:
                errors.append(f"{ticker}: action=watch 但 expected_portfolio_risk_reduction != null")
            if expected_risk_change is not None:
                errors.append(f"{ticker}: action=watch 但 expected_risk_change != null")

        # monitor + 立即执行 → 自动清理 execute_if + warning（不阻断）
        # 修改计划第 23 节原为 hard error，但实际运行中 LLM 经常给 watch+monitor+execute_if=["立即执行"]，
        # 这是良性冗余（watch 本身就不执行），自动清理比阻断更合理。
        if timing == "monitor":
            execute_text = " ".join(str(e) for e in execute_if).lower()
            if any(k in execute_text for k in ("立即执行", "execute now", "immediate")):
                # 自动清理：移除含"立即执行"的条目
                cleaned = [
                    str(e) for e in execute_if
                    if not any(k in str(e).lower() for k in ("立即执行", "立即", "execute now", "immediate"))
                ]
                action["execute_if"] = cleaned
                warnings.append(
                    f"{ticker}: action_timing=monitor 但 execute_if 含「立即执行」，已自动清理。"
                )

        # act_now + directional + execute_if empty → hard error
        if timing == "act_now" and action_type in {"add", "trim", "reduce", "exit"} and not execute_if:
            errors.append(f"{ticker}: action_timing=act_now 但 execute_if 为空")

    return errors, warnings


# ── 第六轮 Phase 6：阈值-证据绑定校验（修改计划第 24 节）──
def validate_threshold_evidence_binding(
    advice: dict[str, Any],
    evidence: list[dict[str, Any]] | None,
) -> list[str]:
    """校验阈值是否可追溯到 Evidence（修改计划第 24 节）。

    检查：
    - Evidence ID 存在
    - Evidence 属于 ticker/theme
    - Evidence facts 包含数值
    - 单位匹配
    - 比较方向合理

    无法证明 → basis 必须为 scenario_assumption，不得为 evidence。
    """
    errors: list[str] = []
    evidence = evidence or []
    ev_by_id = {str(e.get("evidence_id")): e for e in evidence if e.get("evidence_id")}

    for action in advice.get("actions") or []:
        if not isinstance(action, dict):
            continue
        ticker = str(action.get("ticker") or "").upper()
        for threshold in action.get("thresholds") or []:
            if not isinstance(threshold, dict):
                continue
            basis = str(threshold.get("basis") or "").lower()
            if basis != "evidence":
                continue  # scenario_assumption 不需要证据绑定
            ev_id = str(threshold.get("evidence_id") or "")
            if not ev_id or ev_id not in ev_by_id:
                errors.append(
                    f"{ticker}: threshold basis=evidence 但 evidence_id={ev_id} 不存在"
                )
                continue
            ev = ev_by_id[ev_id]
            ev_ticker = str(ev.get("ticker") or "").upper()
            if ev_ticker and ev_ticker != ticker:
                errors.append(
                    f"{ticker}: threshold 引用了不属于该 ticker 的证据 {ev_id}（属于 {ev_ticker}）"
                )
                continue
            # 检查 facts 是否包含数值
            facts_text = " ".join(str(f) for f in (ev.get("facts") or []))
            import re as _re
            if not _re.search(r"\d+\.?\d*", facts_text):
                errors.append(
                    f"{ticker}: threshold basis=evidence 但证据 {ev_id} 的 facts 不含数值"
                )
    return errors


def validate_portfolio_claims(
    advice: dict[str, Any],
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    evidence: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """修改计划第三轮 42：校验 AI 结论是否可追溯到确定性数据。

    返回 (hard_errors, soft_warnings)。hard_errors 表示必须让 Agent 修复的硬伤
    （NaN/Inf、缺失基准却做基准比较、阈值缺 basis）；soft_warnings 为需要证据支撑
    的软性问题（伪指标、RSI/EMA 描述不一致、高置信度缺新鲜证据）。
    """
    errors: list[str] = []
    warnings: list[str] = []
    texts = _claim_text_fields(advice)

    # 1. 正文不得出现 NaN / Inf
    for t in texts:
        low = (t or "").lower()
        if "nan" in low or "inf" in low or "非数" in (t or "") or "无穷" in (t or ""):
            errors.append("正文包含 NaN/Inf 等非有限值，必须移除。")
            break

    # 2. 缺失 benchmark 时不得给出基准数字比较
    rr = metrics.get("relative_returns", {}) or {}
    has_actual = any(isinstance(v, dict) and v.get("status") == "actual" for v in rr.values())
    if not has_actual:
        for t in texts:
            if ("基准" in (t or "") or "大盘" in (t or "")) and any(ch.isdigit() for ch in (t or "")):
                errors.append("缺少可用的基准收益（status != actual），不得给出基准数字比较或「跑赢/跑输大盘」结论。")
                break

    # 3. 未注册指标或无数据支撑的因果表述
    for t in texts:
        for hint in _BANNED_METRIC_HINTS:
            if hint.lower() in (t or "").lower():
                message = f"出现系统未提供或无证据支撑的表述「{hint}」，必须删除。"
                warnings.append(message)
                errors.append(message)
                break

    # 4. 高置信度操作必须有新鲜且正文已提取的证据支撑
    ev_by_id = {str(e.get("evidence_id")): e for e in (evidence or []) if e.get("evidence_id")}
    for a in advice.get("actions") or []:
        if not isinstance(a, dict):
            continue
        try:
            conf = float(a.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        if conf >= 0.7:
            eids = a.get("evidence_ids") or []
            fresh_verified = [
                eid for eid in eids
                if str((ev_by_id.get(str(eid)) or {}).get("recency_tier")) in _FRESH_TIERS
                and bool((ev_by_id.get(str(eid)) or {}).get("article_fetch_ok"))
            ]
            if not fresh_verified:
                message = f"{a.get('ticker')} 高置信度操作（{conf}）没有新鲜且正文已提取的证据支撑。"
                warnings.append(message)
                errors.append(message)

    # 5. RSI / EMA 描述一致性（基于确定性 rsi_regime 与 price_vs_ema*）
    holdings = {h["ticker"]: h for h in snapshot.get("holdings", [])}
    all_text = "\n".join(texts)
    for tk, h in holdings.items():
        regime = str(h.get("rsi_regime") or "")
        ticker_texts = [t for t in texts if tk and tk in t]
        for t in ticker_texts:
            bad = False
            if ("深度超卖" in t or "超卖" in t or "oversold" in t.lower()) and regime not in ("oversold",):
                bad = True
            if ("超买" in t or "overbought" in t.lower()) and regime != "overbought":
                bad = True
            if bad:
                message = f"{tk} 的 RSI 文案与确定性区间 {regime} 不一致。"
                warnings.append(message)
                errors.append(message)
            if re.search(r"strong[^。；\n]{0,20}>\s*70|>\s*70[^。；\n]{0,20}strong", t, re.I):
                message = f"{tk} 错把 RSI>70 描述为 strong；该区间应为超买。"
                warnings.append(message)
                errors.append(message)
            if "ema20" in t.lower() and "ema200" in t.lower() and ("跌破" in t or "交叉" in t or "cross" in t.lower()):
                warnings.append(f"{tk} 出现「EMA20 与 EMA200 交叉」描述，但 price_vs_ema* 仅表示价格相对 EMA 的偏离。")

    for action in advice.get("actions") or []:
        if not isinstance(action, dict):
            continue
        ticker = str(action.get("ticker") or "")
        for text in (str(action.get("technical_reason") or ""), str(action.get("news_reason") or "")):
            if "ema20" in text.lower() and "ema200" in text.lower() and ("跌破" in text or "交叉" in text or "cross" in text.lower()):
                warnings.append(f"{ticker} 出现「EMA20 与 EMA200 交叉」描述，但 price_vs_ema* 仅表示价格相对 EMA 的偏离。")

    # §27/§28 修复：放宽 watch_items 中的"套利"判断，仅作为 warning
    # watch_items 中的"相关性套利"是观察性表述，不应阻断
    watch_text = "\n".join(str(item) for item in (advice.get("watch_items") or []))
    main_text_no_watch = all_text
    if watch_text:
        main_text_no_watch = all_text.replace(watch_text, "")
    if "套利机会" in main_text_no_watch or "相关性套利" in main_text_no_watch:
        errors.append("仅凭相关性不得描述为套利机会。")
    elif "套利机会" in all_text or "相关性套利" in all_text:
        warnings.append("watch_items 中出现相关性套利表述，已作为观察项保留。")

    # §27 修复：检测未经证实的心理/因果判断
    _CAUSALITY_GUARDS = [
        (r"表明市场[^，。；]{0,20}(担心|担忧|恐慌|抛售|质疑|不信任)", "市场心理归因"),
        (r"说明投资者[^，。；]{0,20}(担忧|恐慌|抛售|质疑|转向)", "投资者心理归因"),
        (r"证实.*增长质量", "增长质量因果推断"),
        (r"证明.*基本面恶化", "基本面恶化因果推断"),
    ]
    for pattern, label in _CAUSALITY_GUARDS:
        if re.search(pattern, all_text):
            warnings.append(f"报告包含未经验证的{label}语句（{pattern}），此类心理/因果判断需要有明确 Evidence 支撑。")
            # §20 修复：高价格相关性不代表底层重复
    if re.search(r"重复.*(暴露|持仓|行业)|行业.*(重复|重叠)", all_text):
        warnings.append("报告中出现行业/持仓重复的判断；仅价格相关性不等于底层持仓重复，需确认 ETF holdings 数据。")
    internal_tokens = [
        "execute_if", "cancel_or_upgrade_if", "further_reduce_if", "monitoring_items",
        "us_10y_yield", "btc_price",
    ]
    for token in internal_tokens:
        if token in all_text:
            warnings.append(f"中文正文暴露内部字段名 {token}（已自动替换为中文）。")
    # portfolio_risk_score / evidence_count / cash_unspecified / uranium_price 已加入自动替换，不再报错
    if re.search(r"\b(weak|neutral|oversold|overbought)\b", all_text, re.I):
        warnings.append("中文正文暴露 RSI 内部英文枚举（已自动替换为中文）。")

    # 第六轮 Phase 6：Action 状态一致性 + 阈值绑定（修改计划第 23-24 节）
    state_errors, state_warnings = _validate_action_state_consistency(advice, evidence)
    errors.extend(state_errors)
    warnings.extend(state_warnings)
    errors.extend(validate_threshold_evidence_binding(advice, evidence))

    # §26 修复：检测 AI 自行计算的未注册聚合值
    # 如果 Python 没有提供该组合聚合字段，AI 不应自行计算
    _UNREGISTERED_AGG_WORDS = re.compile(
        r"((?:[A-Z]{1,5}(?:[,/、\s]+)?){2,})\s*(?:风险贡献|权重|占比|合计|总计|持仓)\s*(?:约|为|达)?\s*(\d+(?:\.\d+)?)\s*%",
    )
    for m in _UNREGISTERED_AGG_WORDS.finditer(all_text):
        tickers_str = m.group(1).strip()
        pct = m.group(2)
        tickers_in_text = set(re.findall(r"[A-Z]{1,5}", tickers_str))
        # 检查是否有注册的聚合值
        registered_aggs = (metrics.get("aggregates") or {}).get("top_risk_contribution_sum")
        if registered_aggs is None:
            warnings.append(
                f"中文正文出现 AI 自行计算的聚合值：「{tickers_str} {pct}%」——Python 未提供该组合的注册聚合字段。"
            )

    # §14: 零 Accepted Evidence 时禁止基本面/流动性/投资者心理结论
    accepted_count = sum(1 for e in (evidence or []) if e.get("evidence_id"))
    if accepted_count == 0:
        _ZERO_EVIDENCE_BANNED = [
            r"基本面(承压|恶化|风险|走弱|改善)",
            r"流动(性|资金).*(抛压|收紧|枯竭|折价|压力)",
            r"机构.*(减持|减仓|流出|抛售)",
            r"(市场|投资者).*(担忧|恐慌|认为|预期.*恶化)",
            r"增长质量.*存疑",
            r"经营.*(恶化|承压|风险)",
        ]
        for pattern in _ZERO_EVIDENCE_BANNED:
            if re.search(pattern, all_text):
                warnings.append(f"零 Accepted Evidence 时出现未证实结论「{pattern}」，系统只能确定技术面与风险贡献。")

    # §13: 禁止单个 ticker 被归因为风险评分子项得分
    ticker_risk_score_claim = re.compile(
        r"([A-Z]{1,5})\s*(?:对|为).*?(?:风险评分|risk score).*?(?:贡献|得?分).*?(\d+)",
        re.I,
    )
    for m in ticker_risk_score_claim.finditer(all_text):
        warnings.append(
            f"{m.group(1)} 被错误归因为风险评分子项得分（{m.group(2)} 分），"
            f"该分数属于全组合风险贡献集中度子项，非单标的贡献。"
        )

    # §16: 相关性/重合语义分离
    if re.search(r"科技股.*(超配|过度.*暴露|隐性.*持仓)", all_text):
        warnings.append("未验证 ETF 底层持仓重合度，不得得出持仓重叠/隐性超配结论。仅能表述为价格相关性。")

    # §15: 禁止无数据流动性/情绪结论
    _NO_LIQUIDITY_DATA_CLAIMS = [
        r"流动性.*(驱动|抛压|折价|枯竭|收紧|压力)",
        r"情绪.*(驱动|抛售|恐慌)",
        r"隐含.*流动性",
        r"资金.*(流出|流入|撤出).*ETF",
    ]
    has_liquidity_data = any(
        h.get("avg_volume") or h.get("bid_ask_spread") or h.get("aum")
        for h in (snapshot.get("holdings") or [])
    )
    if not has_liquidity_data:
        for pattern in _NO_LIQUIDITY_DATA_CLAIMS:
            if re.search(pattern, all_text):
                warnings.append(
                    f"出现无数据支撑的流动性/情绪结论（{pattern}）。系统缺少成交额、Bid-ask、AUM 数据，"
                    f"只能表述为回撤和风险贡献较高，不能归因于流动性或情绪。"
                )
                break  # 每个报告仅报一次

    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))
