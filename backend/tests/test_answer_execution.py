"""Offline budget/receipt regression. No credentials or provider calls."""
from types import SimpleNamespace

import pytest

from fund_kb.answer_execution import phase_limits, receipt
from fund_kb.api_consultation import _failure_diagnostic
from fund_kb.settings import Settings


@pytest.mark.parametrize("protocol", ["openai", "responses", "anthropic", "gemini", "ollama", "codex_app_server"])
def test_all_adapters_use_same_explicit_phase_limits(protocol):
    settings = Settings(app_env="test")
    connection = {"protocol": protocol, "http_timeout": 300}
    assert phase_limits(settings, connection, "planning") == {"phase": "planning", "total_seconds": 90,
        "connect_seconds": 10, "read_idle_seconds": 90, "max_output_tokens": 4096}
    assert phase_limits(settings, connection, "synthesis") == {"phase": "synthesis", "total_seconds": 180,
        "connect_seconds": 10, "read_idle_seconds": 180, "max_output_tokens": 8192}


def test_configured_bounds_remain_effective_not_unlimited():
    settings = Settings(app_env="test", answer_model_timeout_seconds=360,
        answer_max_output_tokens=16384, provider_read_idle_timeout_seconds=70)
    limits = phase_limits(settings, {"http_timeout": 240}, "synthesis")
    assert limits == {"phase": "synthesis", "total_seconds": 240, "connect_seconds": 10,
        "read_idle_seconds": 70, "max_output_tokens": 16384}
    assert phase_limits(settings, {"http_timeout": 30}, "synthesis")["total_seconds"] == 30


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0, -5])
def test_invalid_limits_are_rejected(bad):
    with pytest.raises(ValueError, match="INVALID_ANSWER_BUDGET"):
        phase_limits(Settings(app_env="test"), {"http_timeout": bad}, "synthesis")


def test_receipt_never_contains_content_private_reasoning_or_arbitrary_metadata():
    value = receipt({"choices": [{"finish_reason": "stop", "message": {"content": "合成答复",
        "reasoning_content": "PRIVATE_THINKING"}}], "api_key": "SECRET", "usage": {
        "completion_tokens": 50, "reasoning_tokens": 12, "prompt_tokens": True, "secret": "SECRET"}}, 1.234)
    assert value == {"duration_ms": 1234, "finish_reason": "stop", "response_chars": 4,
        "usage": {"completion_tokens": 50, "reasoning_tokens": 12}}


@pytest.mark.parametrize("code,category,phrase", [
    ("EXECUTION_OR_APPROVAL_UNVERIFIED", "validation", "已执行或已审批"),
    ("MODEL_OUTPUT_TRUNCATED_OR_TOOL_REQUESTED", "output_limit", "输出长度上限"),
    ("PROVIDER_TIMEOUT", "timeout", "超时"),
    ("PROVIDER_READ_IDLE_TIMEOUT", "timeout", "超时"),
    ("PROVIDER_CONNECTION_INTERRUPTED", "connection", "连接中断"),
    ("ANSWER_CONTENT_SCHEMA_INVALID", "output_format", "输出结构"),
])
def test_failures_distinguish_limits_transport_and_validation(code, category, phrase):
    run = SimpleNamespace(error_code="SYNTHESIS_OUTPUT_REJECTED", policy_snapshot={
        "generation_diagnostic": {"code": code, "phase": "synthesis", "text_preview": "PRIVATE_CANDIDATE"}})
    value = _failure_diagnostic(run)
    assert value["code"] == code and value["category"] == category and phrase in value["message"]
    assert value["retryable"] is False
    assert "PRIVATE_CANDIDATE" not in str(value)
