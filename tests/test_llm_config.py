"""Provider routing tests for the AI Stock Report Agent."""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "daily_report" / "src"
if str(AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(AGENT_SRC))

from stock_daily_agent.config import (
    build_llm_cfg,
    dashscope_compatible_model_server,
    dashscope_native_api_server,
)


def test_deepseek_v41_flash_uses_compatible_chat_endpoint(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "workspace-123")
    monkeypatch.setenv("DASHSCOPE_REGION", "cn-beijing")

    cfg = build_llm_cfg("deepseek-v4.1-flash-1017", "dashscope")

    assert cfg["model_type"] == "oai"
    assert cfg["api_key"] == "test-key"
    assert cfg["model_server"] == "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    assert "use_raw_api" not in cfg["generate_cfg"]


def test_deepseek_v41_flash_respects_explicit_compatible_endpoint(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_COMPATIBLE_MODEL_SERVER", "https://example.test/v1/")

    cfg = build_llm_cfg("deepseek-v4.1-flash-1017", "dashscope")

    assert cfg["model_server"] == "https://example.test/v1"


def test_native_sdk_endpoint_is_derived_from_an_explicit_workspace_compatible_endpoint(monkeypatch):
    monkeypatch.setenv(
        "DASHSCOPE_COMPATIBLE_MODEL_SERVER",
        "https://llm-frankfurt.eu-central-1.maas.aliyuncs.com/compatible-mode/v1",
    )

    assert dashscope_native_api_server() == "https://llm-frankfurt.eu-central-1.maas.aliyuncs.com/api/v1"


def test_existing_dashscope_models_keep_native_qwen_agent_route(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    cfg = build_llm_cfg("deepseek-v4-flash-0731", "dashscope")

    assert cfg["model_type"] == "qwen_dashscope"
    assert cfg["api_key"] == "test-key"


def test_frankfurt_workspace_routes_all_dashscope_models_to_workspace_endpoint(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "llm-frankfurt")
    monkeypatch.setenv("DASHSCOPE_REGION", "frankfurt")

    cfg = build_llm_cfg("qwen-plus", "dashscope")

    assert cfg["model_type"] == "oai"
    assert cfg["model_server"] == "https://llm-frankfurt.eu-central-1.maas.aliyuncs.com/compatible-mode/v1"
    assert dashscope_native_api_server() == "https://llm-frankfurt.eu-central-1.maas.aliyuncs.com/api/v1"


def test_frankfurt_rejects_missing_workspace_id(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("DASHSCOPE_COMPATIBLE_MODEL_SERVER", raising=False)
    monkeypatch.delenv("QWEN_MODEL_SERVER", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_SERVER", raising=False)
    monkeypatch.setenv("DASHSCOPE_REGION", "eu-central-1")

    try:
        dashscope_compatible_model_server()
    except ValueError as exc:
        assert "WORKSPACE_ID" in str(exc)
    else:
        raise AssertionError("Frankfurt must require a workspace-specific endpoint")


def test_openai_compatible_can_reuse_dashscope_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("QWEN_MODEL_SERVER", "https://example.test/v1")

    cfg = build_llm_cfg("a-model", "openai_compatible")

    assert cfg["api_key"] == "test-key"
