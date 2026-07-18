# -*- coding: utf-8 -*-
"""Portfolio 确定性风险发现（扩展版，对应修改计划 14）。

生成供 Agent 参考与报告展示的结构化风险发现。覆盖：集中度（持仓 / 账户组 /
行业 / 主题 / 资产类）、风险贡献、高 Beta、技术面广度、回撤、相关性、重复暴露、
数据质量。保持与旧版 ``generate_portfolio_rule_findings(snapshot, metrics, settings)``
调用兼容（新增可选 ``instrument_metadata`` 参数）。
"""
from __future__ import annotations

from typing import Any


def _holdings(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return snapshot.get("holdings", []) or []


def _weights(snapshot: dict[str, Any]) -> dict[str, float]:
    return {h["ticker"]: float(h.get("weight") or 0.0) for h in _holdings(snapshot)}


def _meta_for(snapshot: dict[str, Any], instrument_metadata: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    if instrument_metadata:
        return instrument_metadata
    # 在没有外部 metadata 时，从 holding 的 group 字段构造最小可用映射
    out = {}
    for h in _holdings(snapshot):
        out[h["ticker"]] = {
            "ticker": h["ticker"],
            "account_group": h.get("group"),
            "instrument_type": h.get("instrument_type"),
            "theme": h.get("theme"),
            "sector": h.get("sector"),
            "underlying_index": h.get("underlying_index"),
        }
    return out


def _group_weights(snapshot: dict[str, Any], key_fn) -> dict[str, float]:
    weights = _weights(snapshot)
    out: dict[str, float] = {}
    for h in _holdings(snapshot):
        t = h["ticker"]
        k = key_fn(h)
        if k:
            out[k] = out.get(k, 0.0) + weights.get(t, 0.0)
    return out


def generate_portfolio_rule_findings(
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    settings: dict[str, Any],
    instrument_metadata: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    max_position = float(settings.get("max_position_pct", 20.0) or 20.0) / 100.0
    max_group = float(settings.get("max_group_pct", 40.0) or 40.0) / 100.0
    meta = _meta_for(snapshot, instrument_metadata)

    top1 = metrics.get("top1_weight")
    top3 = metrics.get("top3_weight")
    if top1 is not None and top1 > max_position:
        findings.append({
            "risk_id": "CONCENTRATION_TOP1", "severity": "high",
            "title": "单只持仓集中度超限",
            "description": f"最大持仓权重 {top1:.1%}，高于设定的 {max_position:.1%}。",
            "affected_tickers": _top_tickers(snapshot, 1),
            "metric_refs": ["top1_weight"],
        })
    if top3 is not None and top3 > max_group:
        findings.append({
            "risk_id": "CONCENTRATION_TOP3", "severity": "medium",
            "title": "前三大持仓集中度偏高",
            "description": f"前三大持仓合计 {top3:.1%}，高于账户组上限 {max_group:.1%}。",
            "affected_tickers": _top_tickers(snapshot, 3),
            "metric_refs": ["top3_weight"],
        })

    hhi = metrics.get("hhi")
    effective = metrics.get("effective_holdings")
    if hhi is not None and effective is not None and effective < 5 and len(_holdings(snapshot)) >= 5:
        findings.append({
            "risk_id": "CONCENTRATION_EFFECTIVE", "severity": "medium",
            "title": "有效持仓数量偏低",
            "description": f"有效持仓数约 {effective:.1f}（HHI×1e4={hhi*10000:.0f}），分散度有限。",
            "affected_tickers": [],
            "metric_refs": ["effective_holdings", "hhi"],
        })

    # 账户组集中度（修改计划第三轮 32：账户分组是券商/账户维度，不是行业或市场风险因子）
    # 默认仅作信息展示，不计入市场风险评分；仅当用户显式启用 custody_risk 且某券商 >80% 时提示托管集中风险。
    custody_risk_enabled = bool(settings.get("custody_risk"))
    acct = _group_weights(snapshot, lambda h: (meta.get(h["ticker"], {}) or {}).get("account_group") or h.get("group"))
    for g, w in sorted(acct.items(), key=lambda x: -x[1]):
        if custody_risk_enabled and w > 0.80 and g:
            findings.append({
                "risk_id": f"CONCENTRATION_ACCOUNT_{_slug(g)}", "severity": "low",
                "title": f"券商账户「{g}」托管集中（>80%）",
                "description": f"账户分组 {g} 合计权重 {w:.1%}。注意：这是券商/账户托管维度，不是行业风险；"
                               f"仅在启用 custody_risk 时作为托管集中风险提示。",
                "affected_tickers": _tickers_in_group(snapshot, meta, g),
                "metric_refs": ["account_group_weight"],
            })

    # 主题 / 行业集中度
    for dim, label, rid in (("theme", "主题", "THEME"), ("sector", "行业", "SECTOR")):
        gw = _group_weights(snapshot, lambda h, d=dim: (meta.get(h["ticker"], {}) or {}).get(d))
        for k, w in sorted(gw.items(), key=lambda x: -x[1]):
            if k and w > max_group:
                findings.append({
                    "risk_id": f"CONCENTRATION_{rid}_{_slug(k)}", "severity": "medium",
                    "title": f"{label}「{k}」重复暴露",
                    "description": f"{label} {k} 合计权重 {w:.1%}，存在因子重复暴露，需关注组合层面相关性。",
                    "affected_tickers": _tickers_in_dim(snapshot, meta, dim, k),
                    "metric_refs": [f"{dim}_weight"],
                })

    # 资产类集中度
    aw = _group_weights(snapshot, lambda h: (meta.get(h["ticker"], {}) or {}).get("asset_class"))
    for k, w in sorted(aw.items(), key=lambda x: -x[1]):
        if k and w > 0.8:
            findings.append({
                "risk_id": f"CONCENTRATION_ASSET_{_slug(k)}", "severity": "low",
                "title": f"资产类「{k}」占主导",
                "description": f"资产类 {k} 合计权重 {w:.1%}，组合对单一资产类依赖度高。",
                "affected_tickers": _tickers_in_dim(snapshot, meta, "asset_class", k),
                "metric_refs": ["asset_class_weight"],
            })

    # 风险贡献（修改计划 14.2）
    for rc in metrics.get("risk_contributions", []) or []:
        w = float(rc.get("weight") or 0.0)
        rcv = float(rc.get("risk_contribution") or 0.0)
        gap = float(rc.get("risk_weight_gap") or 0.0)
        ticker = rc.get("ticker")
        if w > 0 and rcv > w * 1.5:
            findings.append({
                "risk_id": f"RISK_CONTRIB_{ticker}", "severity": "high",
                "title": f"{ticker} 风险贡献显著高于权重",
                "description": f"{ticker} 权重 {w:.1%}，但风险贡献约 {rcv:.1%}（约为权重的 {rcv/max(w,1e-6):.1f} 倍）。",
                "affected_tickers": [ticker],
                "metric_refs": ["risk_contribution", "risk_weight_gap"],
            })
        elif gap > 0.05:
            findings.append({
                "risk_id": f"RISK_CONTRIB_GAP_{ticker}", "severity": "medium",
                "title": f"{ticker} 风险贡献偏离权重",
                "description": f"{ticker} 风险贡献高于权重约 {gap:.1%}。",
                "affected_tickers": [ticker],
                "metric_refs": ["risk_weight_gap"],
            })

    # 高 Beta（修改计划 14.3）
    beta = metrics.get("portfolio_beta")
    if beta is not None and beta > 1.25:
        findings.append({
            "risk_id": "HIGH_BETA", "severity": "medium",
            "title": "组合 Beta 偏高",
            "description": f"估计组合 Beta 为 {beta:.2f}，对宽基基准的敏感度高于市场。",
            "affected_tickers": _high_beta_tickers(snapshot, threshold=1.5),
            "metric_refs": ["portfolio_beta"],
        })
    hb = _high_beta_tickers(snapshot, threshold=1.5)
    hb_weight = sum(_weights(snapshot).get(t, 0.0) for t in hb)
    if hb_weight > 0.4:
        findings.append({
            "risk_id": "HIGH_BETA_WEIGHT", "severity": "medium",
            "title": "高 Beta 持仓权重占比高",
            "description": f"Beta>1.5 的持仓合计权重约 {hb_weight:.1%}，组合对风险偏好变化更敏感。",
            "affected_tickers": hb,
            "metric_refs": ["holding_beta"],
        })

    # 技术面广度（修改计划 14.4）
    breadth = metrics.get("technical_breadth", {}) or {}
    if isinstance(breadth, dict):
        ema50 = breadth.get("above_ema50_weight")
        if ema50 is not None and _safe(ema50) < 0.4:
            findings.append({
                "risk_id": "BREADTH_EMA50", "severity": "medium",
                "title": "过半权重跌破 EMA50",
                "description": f"约 {_safe(ema50, 0)*100:.0f}% 的权重位于 EMA50 上方，技术面广度偏弱。",
                "affected_tickers": [],
                "metric_refs": ["technical_breadth"],
            })
        ema200 = breadth.get("above_ema200_weight")
        if ema200 is not None and _safe(ema200) < 0.3:
            findings.append({
                "risk_id": "BREADTH_EMA200", "severity": "medium",
                "title": "较多权重跌破 EMA200",
                "description": f"约 {_safe(ema200, 0)*100:.0f}% 的权重位于 EMA200 上方。",
                "affected_tickers": [],
                "metric_refs": ["technical_breadth"],
            })

    # 回撤（修改计划 14.5）
    dd63 = metrics.get("max_drawdown_63d")
    dd252 = metrics.get("max_drawdown_252d")
    if dd63 is not None and dd63 <= -15:
        findings.append({
            "risk_id": "DRAWDOWN_63D", "severity": "medium",
            "title": "组合 63 日回撤较大",
            "description": f"近 63 日最大回撤约 {dd63:.1f}%。",
            "affected_tickers": [],
            "metric_refs": ["max_drawdown_63d"],
        })
    if dd252 is not None and dd252 <= -25:
        findings.append({
            "risk_id": "DRAWDOWN_252D", "severity": "high",
            "title": "组合 252 日回撤深",
            "description": f"近 252 日最大回撤约 {dd252:.1f}%，提示组合经历显著下行。",
            "affected_tickers": [],
            "metric_refs": ["max_drawdown_252d"],
        })

    # 相关性（修改计划 14.6）
    max_corr = metrics.get("max_pairwise_correlation")
    if max_corr is not None and max_corr >= 0.85:
        pairs = metrics.get("high_correlation_pairs", []) or []
        findings.append({
            "risk_id": "CORRELATION_HIGH", "severity": "medium",
            "title": "存在高度相关持仓对",
            "description": f"最大两两相关系数 {max_corr:.2f}；高度相关会削弱表面分散效果。",
            "affected_tickers": _flatten_pairs(pairs),
            "metric_refs": ["max_pairwise_correlation", "high_correlation_pairs"],
        })

    # 重复暴露（修改计划 14.7）：ETF 与底层个股重叠
    dup = _duplicate_exposure(snapshot, meta)
    if dup:
        for d in dup:
            findings.append({
                "risk_id": f"DUP_EXPOSURE_{_slug(d['etf'])}", "severity": "medium",
                "title": f"{d['etf']} 与直接持仓重复暴露",
                "description": d["description"],
                "affected_tickers": d["tickers"],
                "metric_refs": ["duplicate_exposure"],
            })

    # 数据质量（保留）
    for field, label in (("missing_prices", "缺失价格"), ("missing_fx", "缺失 FX 汇率"), ("missing_history", "缺失历史")):
        values = snapshot.get("data_quality", {}).get(field) or []
        if values:
            findings.append({
                "risk_id": field.upper(), "severity": "medium",
                "title": f"数据质量限制：{label}",
                "description": "、".join(map(str, values[:12])),
                "affected_tickers": list(values[:12]),
                "metric_refs": ["data_quality"],
            })

    return findings


def _safe(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_]", "_", str(text or "x"))[:24]


def _top_tickers(snapshot: dict[str, Any], n: int) -> list[str]:
    weights = _weights(snapshot)
    return [t for t, _ in sorted(weights.items(), key=lambda x: -x[1])[:n]]


def _tickers_in_group(snapshot: dict[str, Any], meta: dict[str, dict[str, Any]], group: str) -> list[str]:
    out = []
    for h in _holdings(snapshot):
        g = (meta.get(h["ticker"], {}) or {}).get("account_group") or h.get("group")
        if g == group:
            out.append(h["ticker"])
    return out


def _tickers_in_dim(snapshot: dict[str, Any], meta: dict[str, dict[str, Any]], dim: str, value: str) -> list[str]:
    out = []
    for h in _holdings(snapshot):
        if (meta.get(h["ticker"], {}) or {}).get(dim) == value:
            out.append(h["ticker"])
    return out


def _high_beta_tickers(snapshot: dict[str, Any], threshold: float = 1.5) -> list[str]:
    out = []
    for h in _holdings(snapshot):
        try:
            b = float(h.get("beta") or 0.0)
        except (TypeError, ValueError):
            b = 0.0
        if b > threshold:
            out.append(h["ticker"])
    return out


def _flatten_pairs(pairs: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for p in pairs:
        out.append(p.get("ticker_a"))
        out.append(p.get("ticker_b"))
    return [t for t in out if t]


def _duplicate_exposure(snapshot: dict[str, Any], meta: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """检测 ETF / 指数基金 与直接持有的个股在主题/底层指数上的重复暴露。"""
    results: list[dict[str, Any]] = []
    equity_themes = set()
    equity_sectors = set()
    for h in _holdings(snapshot):
        m = meta.get(h["ticker"], {}) or {}
        if str(m.get("instrument_type") or "").upper() == "EQUITY":
            if m.get("theme"):
                equity_themes.add(str(m["theme"]).lower())
            if m.get("sector"):
                equity_sectors.add(str(m["sector"]).lower())
    for h in _holdings(snapshot):
        m = meta.get(h["ticker"], {}) or {}
        itype = str(m.get("instrument_type") or "").upper()
        if itype in {"ETF", "INDEX", "FUND"}:
            theme = str(m.get("theme") or "").lower()
            underlying = str(m.get("underlying_index") or "").lower()
            overlap = [t for t in equity_themes if t and (t in theme or theme in t or t in underlying or underlying in t)]
            if overlap:
                results.append({
                    "etf": h["ticker"],
                    "tickers": [h["ticker"]] + _tickers_in_dim(snapshot, meta, "theme", overlap[0]) if False else [h["ticker"]]
                        + [x["ticker"] for x in _holdings(snapshot) if (meta.get(x["ticker"], {}) or {}).get("theme") and str((meta.get(x["ticker"], {}) or {})["theme"]).lower() in overlap],
                    "description": f"{h['ticker']} 的底层主题/指数（{m.get('theme') or m.get('underlying_index')}）与直接持有的个股主题重叠，表面持仓数量较多但因子暴露仍集中。",
                })
    # 去重 tickers
    for r in results:
        r["tickers"] = list(dict.fromkeys(r["tickers"]))
    return results
