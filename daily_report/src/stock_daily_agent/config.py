from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional
import os


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    scripts_dir: Path
    skill_file: Path
    ticker_reference: Path
    notes_example: Path

    @classmethod
    def from_root(cls, root: Path) -> "ProjectPaths":
        root = root.resolve()
        return cls(
            root=root,
            scripts_dir=root / "scripts",
            skill_file=root / "skills" / "stock-daily-report" / "SKILL.md",
            ticker_reference=root / "references" / "ticker_formats.md",
            notes_example=root / "assets" / "notes_example.txt",
        )


@dataclass
class RunContext:
    paths: ProjectPaths
    ticker: str
    run_dir: Path
    months: int = 3
    report_date: str = date.today().isoformat()
    output_html: Optional[Path] = None
    min_notes: int = 10
    keep_intermediate: bool = False

    @property
    def safe_ticker(self) -> str:
        return self.ticker.upper().replace("-", "_").replace("^", "IDX_").replace(".", "_")

    @property
    def data_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_data.json"

    @property
    def chart_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_chart.html"

    @property
    def notes_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_notes.txt"

    @property
    def evidence_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_evidence.json"

    @property
    def articles_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_articles.json"

    @property
    def final_notes_json_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_final_notes.json"

    @property
    def candidates_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_research_candidates.json"

    @property
    def dashscope_sources_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_dashscope_sources.json"

    @property
    def raw_results_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_searxng_raw_search_results.json"

    @property
    def reranked_evidence_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_searxng_reranked_evidence.json"

    @property
    def serper_raw_results_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_serper_raw_search_results.json"

    @property
    def serper_reranked_evidence_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_serper_reranked_evidence.json"

    @property
    def combined_reranked_evidence_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_combined_reranked_evidence.json"

    @property
    def search_quality_report_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_search_quality_report.json"

    @property
    def audit_file(self) -> Path:
        return self.run_dir / f"{self.safe_ticker}_audit.json"

    @property
    def final_output_html(self) -> Path:
        if self.output_html is not None:
            return self.output_html
        return self.run_dir / f"{self.safe_ticker}_report_{self.report_date}.html"


def load_dotenv(project_root: Path) -> None:
    """Small .env loader so the project works without python-dotenv."""
    env_path = project_root / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_llm_cfg(model: str, provider: str = "dashscope") -> dict:
    """Build a Qwen-Agent llm_cfg for DashScope, DeepSeek, or OpenAI-compatible endpoints."""
    if provider == "dashscope":
        raw_api_override = os.environ.get("QWEN_AGENT_USE_RAW_API")
        if raw_api_override is None:
            model_lower = model.strip().lower()
            use_raw_api = model_lower.startswith("deepseek") or "qwen3-max" in model_lower or "qwen3-coder" in model_lower
        else:
            use_raw_api = raw_api_override.strip().lower() in {"1", "true", "yes"}
        cfg = {
            "model": model,
            "model_type": "qwen_dashscope",
            "generate_cfg": {
                "top_p": 0.8,
                "temperature": 0.2,
                # Preserve native tool calling for the DeepSeek model used by
                # the original v5.8 project. qwen-plus uses Qwen-Agent's local
                # protocol unless the deployment explicitly overrides it.
                "use_raw_api": use_raw_api,
                # V5.2: keep the main Agent's implicit web search disabled.
                # Search must flow through structured evidence-provider tools
                # (SearXNG / Serper / DashScope source objects) so final notes can be audited.
                "enable_search": False,
            },
        }
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        if api_key:
            cfg["api_key"] = api_key
        return cfg

    if provider == "deepseek":
        model_server = os.environ.get("DEEPSEEK_MODEL_SERVER", "https://api.deepseek.com/v1")
        api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("provider=deepseek 需要配置 DEEPSEEK_API_KEY，或改用 --provider dashscope。")
        return {
            "model": model,
            "model_type": "oai",
            "model_server": model_server,
            "api_key": api_key,
            "generate_cfg": {
                "top_p": 0.8,
                "temperature": 0.2,
            },
        }

    if provider == "openai_compatible":
        model_server = os.environ.get("QWEN_MODEL_SERVER") or os.environ.get("OPENAI_MODEL_SERVER") or "http://localhost:8000/v1"
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("QWEN_API_KEY") or "EMPTY"
        return {
            "model": model,
            "model_type": "oai",
            "model_server": model_server,
            "api_key": api_key,
            "generate_cfg": {
                "top_p": 0.8,
                "temperature": 0.2,
            },
        }

    raise ValueError(f"Unsupported provider: {provider}")
