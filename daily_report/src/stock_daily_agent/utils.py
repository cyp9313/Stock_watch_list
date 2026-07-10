from __future__ import annotations

import json
import json5
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


class ToolError(RuntimeError):
    pass


def parse_tool_params(params: Any) -> dict:
    """Qwen-Agent may pass tool args as a JSON string or as a dict."""
    if params is None:
        return {}
    if isinstance(params, dict):
        return params
    if isinstance(params, str):
        text = params.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            try:
                parsed = json5.loads(text)
            except Exception as exc:
                raise ToolError(f"Tool parameters are not valid JSON/JSON5: {exc}: {text[:300]}") from exc
            if not isinstance(parsed, dict):
                raise ToolError(f"Tool parameters must be a JSON object, got {type(parsed).__name__}")
            return parsed
    raise ToolError(f"Unsupported tool parameter type: {type(params)!r}")


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def run_python_script(script: Path, args: list[str], cwd: Path, timeout: int = 180) -> dict:
    cmd = [sys.executable, str(script), *args]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    result = {
        "command": " ".join(map(str, cmd)),
        "returncode": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }
    if proc.returncode != 0:
        raise ToolError(json_dumps(result))
    return result


def ensure_within_dir(path: Path, base: Path) -> Path:
    path = path.resolve()
    base = base.resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ToolError(f"Unsafe path outside run directory: {path}") from exc
    return path


def strip_markdown_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()
