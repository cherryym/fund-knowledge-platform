"""Provider-independent answer budgets and safe operational receipts.

These limits are ceilings, not a requested answer length or a promise of model
latency. No credentials, source text or private model reasoning belong here.
"""
from __future__ import annotations

import math


def phase_limits(settings, connection, phase):
    if phase not in {"planning", "synthesis"}:
        raise ValueError("INVALID_ANSWER_PHASE")
    connection = connection or {}
    planning = phase == "planning"
    seconds = float(getattr(settings, "answer_planning_timeout_seconds", 90) if planning
                    else getattr(settings, "answer_model_timeout_seconds", 180))
    ceiling = float(connection.get("http_timeout", getattr(settings, "provider_http_timeout_seconds", 300)))
    idle = float(getattr(settings, "provider_read_idle_timeout_seconds", 180))
    tokens = getattr(settings, "answer_planning_max_output_tokens", 4096) if planning else getattr(
        settings, "answer_max_output_tokens", 8192)
    if (any(not math.isfinite(value) or value <= 0 for value in (seconds, ceiling, idle))
            or type(tokens) is not int or not 256 <= tokens <= 131072):
        raise ValueError("INVALID_ANSWER_BUDGET")
    total = min(seconds, ceiling, 600.0)
    return {"phase": phase, "total_seconds": total, "connect_seconds": min(10.0, total),
            "read_idle_seconds": min(idle, total), "max_output_tokens": tokens}


def receipt(raw, elapsed):
    """Allowlist only telemetry; normalized content is counted, never copied."""
    raw = raw if isinstance(raw, dict) else {}
    choices = raw.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    transport = raw.get("transport_meta") if isinstance(raw.get("transport_meta"), dict) else {}
    safe_transport = {}
    if type(transport.get("reasoning_requested")) is bool:
        safe_transport["reasoning_requested"] = transport["reasoning_requested"]
    if transport.get("structured_strategy") in {"none", "native_schema", "data_envelope", "prompt_json"}:
        safe_transport["structured_strategy"] = transport["structured_strategy"]
    return {"duration_ms": max(0, round(elapsed * 1000)), **safe_transport,
        "finish_reason": choice.get("finish_reason") if choice.get("finish_reason") in {"stop", "length"} else "unknown",
        "response_chars": len(content) if isinstance(content, str) else 0,
        "usage": {key: value for key, value in usage.items() if key in {
            "prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"}
            and type(value) is int and value >= 0}}
