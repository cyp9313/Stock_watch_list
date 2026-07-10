from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    body: str
    raw: str


def load_skill(skill_path: Path) -> SkillSpec:
    text = skill_path.read_text(encoding="utf-8")
    name = "stock-daily-report"
    description = ""
    body = text

    if text.startswith("---"):
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
        if match:
            front, body = match.groups()
            for line in front.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip().strip('"').strip("'")
                if key == "name":
                    name = value
                elif key == "description":
                    description = value
    return SkillSpec(name=name, description=description, body=body.strip(), raw=text)


def compact_skill_for_prompt(spec: SkillSpec, max_chars: int = 9000) -> str:
    """Keep the full rules visible while preventing huge system prompts."""
    text = spec.raw.strip()
    if len(text) <= max_chars:
        return text
    keep_head = int(max_chars * 0.72)
    keep_tail = max_chars - keep_head
    return text[:keep_head] + "\n\n...[SKILL 被截断：保留末尾关键规则]...\n\n" + text[-keep_tail:]
