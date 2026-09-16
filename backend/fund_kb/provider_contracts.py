"""Pure provider budgets and local schema contracts. No I/O or credentials."""
import json
import math

from jsonschema import Draft202012Validator
from referencing import Registry


def seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 600:
        raise ValueError("INVALID_TIMEOUT")
    return float(value)


def timeout_budget(timeout, deployment=None):
    if timeout is None:
        return None  # Explicit cancellable mode; not a deployment-default request.
    caller = seconds(timeout)
    return min(caller, seconds(deployment)) if deployment is not None else caller


class ThrottledCheck:
    """No thread or timer: callers poll, and the authority callback runs at most once/s."""
    def __init__(self, check, clock):
        self.check, self.clock, self.last = check, clock, float("-inf")

    def __call__(self):
        if self.clock() - self.last >= 1:
            return self.force()

    def force(self):
        self.last = self.clock()
        return self.check()


def schema_validator(schema):
    if not isinstance(schema, dict):
        raise ValueError("INVALID_OUTPUT_SCHEMA")
    # Freeze caller input and reject cycles, NaN and external reference retrieval.
    frozen = json.loads(json.dumps(schema, ensure_ascii=False, allow_nan=False))
    if len(json.dumps(frozen, ensure_ascii=False).encode()) > 65536:
        raise ValueError("INVALID_OUTPUT_SCHEMA")
    pending = [(frozen, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise ValueError("INVALID_OUTPUT_SCHEMA")
        if isinstance(item, dict):
            for key in ("$ref", "$dynamicRef"):
                if key in item and (not isinstance(item[key], str) or not item[key].startswith("#")):
                    raise ValueError("INVALID_OUTPUT_SCHEMA")
            pending.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, list):
            pending.extend((value, depth + 1) for value in item)
    Draft202012Validator.check_schema(frozen)
    # Empty registry explicitly forbids network resolution even for malformed refs.
    return frozen, Draft202012Validator(frozen, registry=Registry())


def _native_subset(schema, require_all=False):
    """Conservative native subset; never drop constraints to make a schema fit."""
    allowed = {"type", "properties", "required", "additionalProperties", "items", "enum", "description", "title"}
    if not isinstance(schema, dict) or set(schema) - allowed:
        return False
    if schema.get("type") == "object":
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is not False:
            return False
        if require_all and set(schema.get("required", [])) != set(properties):
            return False
        return all(_native_subset(child, require_all) for child in properties.values())
    if schema.get("type") == "array":
        return _native_subset(schema.get("items"), require_all)
    return isinstance(schema.get("type"), str) and schema["type"] in {"string", "number", "integer", "boolean", "null"}


def structured_mode(snapshot, schema):
    if schema is None:
        return "none"
    protocol, provider, model = snapshot.get("protocol"), snapshot.get("provider_id"), snapshot.get("model_id", "")
    if protocol == "responses" and provider == "minimax":
        return "data_envelope"  # Existing, verified data-only return contract.
    if provider == "openai" and protocol in {"responses", "openai"} and model.startswith(
            ("gpt-6", "gpt-5", "gpt-4o-2024-08-06", "gpt-4o-2024-11-20", "gpt-4o-mini", "o3", "o4")):
        if _native_subset(schema, require_all=True):
            return "native_schema"
    if provider == "anthropic" and protocol == "anthropic" and model in {
            "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"}:
        if _native_subset(schema):
            return "native_schema"
    if protocol == "ollama" and provider == "ollama" and _native_subset(schema):
        return "native_schema"
    # Unknown model / schema dialect: prompt + full LOCAL validation, never a retry.
    return "prompt_json"


def safe_usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: count for key, count in value.items() if key in {
        "prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"}
        and type(count) is int and count >= 0}


def safe_diagnostic(value):
    if not isinstance(value, dict):
        return {}
    enums = {
        "output_shape": {"object", "array", "fenced", "other"},
        "response_phase": {"http_json", "normalization", "stream", "schema"},
        "response_status": {"completed", "incomplete", "failed", "unknown"},
        "outcome": {"completed", "incomplete", "refusal", "empty", "failed", "protocol_error"},
        "finish_reason": {"stop", "length", "incomplete", "unknown"},
        "incomplete_reason": {"max_output_tokens", "content_filter", "unknown"},
        "structured_mode": {"none", "native_schema", "data_envelope", "prompt_json"},
    }
    counts = {"response_chars", "response_bytes", "json_line", "json_column", "message_items", "reasoning_items",
              "function_items", "completion_tokens", "duration_ms", "first_chunk_ms", "http_status"}
    result = {key: item for key, item in value.items()
              if (key in counts and type(item) is int and item >= 0)
              or (key in enums and type(item) is str and item in enums[key])}
    if "usage" in value:
        result["usage"] = safe_usage(value["usage"])
    return result
