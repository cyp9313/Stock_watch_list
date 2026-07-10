from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
import re
import os

VALID_TAGS = {"BULL", "BEAR", "MIX"}
TECHNICAL_EVIDENCE_PREFIXES = {"TECH"}


@dataclass
class NewsNote:
    tag: str
    title: str
    fact: str
    logic: str
    investment_meaning: str
    source: str = ""
    source_date: str = ""
    url: str = ""
    evidence_id: str = ""
    evidence_url: str = ""
    evidence_title: str = ""
    evidence_method: str = ""
    source_domain: str = ""
    evidence_grade: str = ""
    evidence_origin: str = ""
    evidence_allowed_uses: str = ""
    evidence_support_excerpt: str = ""

    def render(self) -> str:
        title = self.title.strip("【】 ")
        body = "".join([
            self.fact.strip(),
            " ",
            self.logic.strip(),
            " ",
            self.investment_meaning.strip(),
        ]).strip()
        if self.source or self.source_date:
            src = " / ".join(x for x in [self.source.strip(), self.source_date.strip()] if x)
            body += f" 来源：{src}。"
        # build_report.py expects a single line after [BULL]/[BEAR]/[MIX]
        body = re.sub(r"\s+", " ", body)
        return f"[{self.tag}] 【{title}】{body}"

    @property
    def is_technical(self) -> bool:
        eid = (self.evidence_id or "").upper()
        if any(eid.startswith(p) for p in TECHNICAL_EVIDENCE_PREFIXES):
            return True
        return self.source.lower().startswith("fetch_and_calc.py") or "yfinance 技术指标" in self.source


def _coerce_items(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        if "items" in payload and isinstance(payload["items"], list):
            return payload["items"]
        if "notes" in payload and isinstance(payload["notes"], list):
            return payload["notes"]
    if isinstance(payload, list):
        return payload
    raise ValueError("Expected JSON object with items:[...] or a list of note objects.")


def parse_notes_payload(payload: Any) -> list[NewsNote]:
    items = _coerce_items(payload)
    notes: list[NewsNote] = []
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Item {i} is not an object.")
        tag = str(item.get("tag", "")).upper().strip()
        note = NewsNote(
            tag=tag,
            title=str(item.get("title", "")).strip(),
            fact=str(item.get("fact", item.get("evidence", ""))).strip(),
            logic=str(item.get("logic", "")).strip(),
            investment_meaning=str(item.get("investment_meaning", item.get("meaning", ""))).strip(),
            source=str(item.get("source", "")).strip(),
            source_date=str(item.get("source_date", item.get("date", ""))).strip(),
            url=str(item.get("url", "")).strip(),
            evidence_id=str(item.get("evidence_id", item.get("source_id", ""))).strip(),
        )
        notes.append(note)
    return notes


def validate_notes(notes: list[NewsNote], min_items: int = 10) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if len(notes) < min_items:
        errors.append(f"notes 数量不足：{len(notes)} 条，至少需要 {min_items} 条。")

    counts = {tag: 0 for tag in VALID_TAGS}
    for idx, note in enumerate(notes, start=1):
        if note.tag not in VALID_TAGS:
            errors.append(f"第 {idx} 条 tag 非法：{note.tag!r}，只能是 BULL/BEAR/MIX。")
        else:
            counts[note.tag] += 1
        if not note.title:
            errors.append(f"第 {idx} 条缺少 title。")
        if not note.fact or len(note.fact) < 20:
            errors.append(f"第 {idx} 条 fact/evidence 过短，需要具体数据或事件。")
        if not note.logic or len(note.logic) < 20:
            errors.append(f"第 {idx} 条 logic 过短，需要解释影响链条。")
        if not note.investment_meaning or len(note.investment_meaning) < 15:
            errors.append(f"第 {idx} 条 investment_meaning 过短，需要说明投资含义。")
        rendered_len = len(note.render())
        if rendered_len < 80:
            errors.append(f"第 {idx} 条过短：{rendered_len} 字符，建议 80-260 字。")
        if "\n" in note.render():
            errors.append(f"第 {idx} 条包含换行，build_report.py 需要单行 notes。")

    if len(notes) >= min_items:
        if counts["BULL"] < 3:
            errors.append("BULL 数量偏少，建议约 5 条。")
        if counts["BEAR"] < 3:
            errors.append("BEAR 数量偏少，建议约 4 条。")
        if counts["MIX"] < 1:
            errors.append("MIX 数量偏少，建议至少 1-2 条。")

    max_unknown = int(os.environ.get("NOTES_MAX_UNKNOWN_SOURCE_DATE", os.environ.get("EVIDENCE_MAX_UNKNOWN_DATE", "3")))
    unknown_count = sum(1 for n in notes if n.source and (not n.source_date or n.source_date.lower() in {"unknown", "none", "null"}))
    if max_unknown >= 0 and unknown_count > max_unknown:
        errors.append(f"source_date=unknown 的 notes 过多：{unknown_count} 条，最多允许 {max_unknown} 条；请优先使用正文抓取后的发布日期或公司IR/SEC等可核验来源。")

    return not errors, errors


def render_notes_text(notes: list[NewsNote]) -> str:
    return "\n\n".join(note.render() for note in notes) + "\n"


def notes_to_jsonable(notes: list[NewsNote]) -> list[dict]:
    return [asdict(n) for n in notes]
