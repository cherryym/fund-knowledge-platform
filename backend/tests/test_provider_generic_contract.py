"""Generic provider contracts: synthetic fixtures only, no credentials or network."""
import copy
import json
import socket

import httpx
import pytest

from fund_kb import ai_transport, providers
from fund_kb.provider_stream import ResponseStream, StreamError
from test_providers import response_fixture, snapshot


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
MESSAGES = [{"role": "user", "content": "Synthetic contract only"}]
CASES = [
    ("responses", "openai", "gpt-6-astra", "native_schema"),
    ("openai", "openai", "gpt-6-astra", "native_schema"),
    ("anthropic", "anthropic", "claude-opus-5", "native_schema"),
    ("gemini", "google", "gemini-3.8-flash", "prompt_json"),
    ("ollama", "ollama", "installed-synthetic", "native_schema"),
    ("openai", "custom", "unknown-synthetic", "prompt_json"),
    ("responses", "custom", "unknown-synthetic", "prompt_json"),
    ("openai", "minimax", "MiniMax-M3", "prompt_json"),
    ("responses", "minimax", "MiniMax-M3", "data_envelope"),
]


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("Real network or credential access is forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(providers, "_master_key", forbidden)
    monkeypatch.setattr(providers, "_dns_addresses", lambda host, port:
                        ["127.0.0.1"] if host in providers.LOCAL_HOSTS else ["93.184.216.34"])


@pytest.mark.parametrize("protocol,brand,model,mode", CASES)
@pytest.mark.parametrize("valid", [True, False])
def test_all_protocols_accept_schema_and_locally_validate_even_native(protocol, brand, model, mode, valid):
    sent = []
    body = response_fixture(protocol, '{"ok":true}' if valid else '{"ok":"wrong-type-private-marker"}')
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body)
    connection = snapshot(protocol, brand, model_id=model, http_timeout=300)
    if brand == "custom":
        connection["base_url"] = "https://synthetic.invalid/v1"
    before = copy.deepcopy(connection)
    if valid:
        result = providers.complete(connection, MESSAGES, output_schema=SCHEMA, max_tokens=8192,
            timeout=180, read_idle_timeout=37, transport=httpx.MockTransport(respond))
        assert result["transport_meta"]["structured_strategy"] == mode
        assert result["provider_meta"]["outcome"] == "completed"
        assert result["provider_meta"]["finish_reason"] == "stop"
        assert json.loads(result["choices"][0]["message"]["content"]) == {"ok": True}
        if brand == "minimax" and protocol == "responses":
            assert sent[0]["reasoning"] == {"effort": "low"}
            assert sent[0]["stream"] is True
            assert result["transport_meta"]["reasoning_requested"] is True
    else:
        with pytest.raises(providers.ProviderError, match="^PROVIDER_OUTPUT_SCHEMA_INVALID$") as caught:
            providers.complete(connection, MESSAGES, output_schema=SCHEMA, transport=httpx.MockTransport(respond))
        assert "private-marker" not in json.dumps(caught.value.diagnostic)
    assert connection == before and len(sent) == 1
    wire = sent[0]
    if mode == "native_schema":
        value = (wire["text"]["format"]["schema"] if protocol == "responses" else
                 wire["response_format"]["json_schema"]["schema"] if protocol == "openai" else
                 wire["output_config"]["format"]["schema"] if protocol == "anthropic" else wire["format"])
        assert value == SCHEMA
    elif mode == "prompt_json":
        assert "tools" not in wire and "output_config" not in wire


def test_complex_schema_uses_prompt_without_dropping_constraints():
    schema = {"type": "object", "properties": {"x": {"type": ["string", "null"], "minLength": 4}},
              "required": ["x"], "additionalProperties": False}
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=response_fixture("responses", '{"x":"long"}'))
    result = providers.complete(snapshot(), MESSAGES, output_schema=schema, transport=httpx.MockTransport(respond))
    assert result["transport_meta"]["structured_strategy"] == "prompt_json"
    assert '"minLength": 4' in sent[0]["input"][-1]["content"]
    assert sent[0]["text"]["format"] == {"type": "json_object"}


@pytest.mark.parametrize("schema", [{"$ref": "https://must-not-fetch.invalid/schema"},
    {"type": "object", "properties": {"x": {"$ref": "file:///must-not-read"}}}, {"type": "invalid-type"}])
def test_invalid_or_external_schema_rejected_before_any_request(schema):
    with pytest.raises(providers.ProviderError, match="^INVALID_OUTPUT_SCHEMA$"):
        providers.complete(snapshot(), MESSAGES, output_schema=schema,
            transport=httpx.MockTransport(lambda r: pytest.fail("Schema must be checked before transmission")))


def test_local_schema_reference_needs_no_network():
    schema = {"type": "object", "$defs": {"b": {"type": "boolean"}},
              "properties": {"ok": {"$ref": "#/$defs/b"}}, "required": ["ok"]}
    result = providers.complete(snapshot(), MESSAGES, output_schema=schema,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response_fixture("responses"))))
    assert result["output_format"]["json_object"] is True


@pytest.mark.parametrize("caller,cap,expected", [(180, 300, 180), (500, 300, 300), (600, 600, 600)])
def test_provider_no_hidden_120s_cap_and_read_idle_is_independent(caller, cap, expected):
    seen = []
    def respond(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=response_fixture("responses"))
    providers.complete(snapshot(http_timeout=cap, read_idle_timeout=19), MESSAGES, timeout=caller,
                       transport=httpx.MockTransport(respond))
    assert seen[0]["write"] == pytest.approx(expected, abs=1)
    assert seen[0]["read"] == 19 and 0 < seen[0]["connect"] <= 10


def test_provider_missing_read_idle_defaults_to_total_not_60():
    seen = []
    def respond(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=response_fixture("responses"))
    providers.complete(snapshot(http_timeout=300), MESSAGES, timeout=180, transport=httpx.MockTransport(respond))
    assert seen[0]["read"] == pytest.approx(180, abs=1)


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), 601, "180"])
def test_invalid_total_budget_rejected_before_request(timeout):
    with pytest.raises(providers.ProviderError, match="^INVALID_TIMEOUT$"):
        providers.complete(snapshot(), MESSAGES, timeout=timeout)
    with pytest.raises(ai_transport.ProviderError, match="^INVALID_TIMEOUT$"):
        ai_transport.post_json("https://synthetic.invalid", "chat/completions", {}, timeout=timeout)


def test_legacy_no_hidden_45s_cap(monkeypatch):
    real_client, seen = httpx.Client, []
    def respond(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(ai_transport.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(respond)))
    assert ai_transport.post_json("https://synthetic.invalid", "chat/completions", {}, timeout=180,
        deployment_timeout=300, read_idle_timeout=29) == {"ok": True}
    assert seen[0]["write"] == pytest.approx(180, abs=1)
    assert seen[0]["read"] == 29 and seen[0]["connect"] <= 10


def sse(kind, **payload):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **payload}) + "\n\n").encode()


@pytest.mark.parametrize("case", ["no_blank_line", "mismatched_event", "partial_utf8", "unknown_tool"])
def test_stream_boundaries_never_promote_partial_or_ambiguous_terminal(case):
    raw = sse("response.completed", response=response_fixture("responses"))
    if case == "no_blank_line":
        raw = raw.rstrip(b"\n")
    elif case == "mismatched_event":
        raw = raw.replace(b"event: response.completed", b"event: response.failed")
    elif case == "partial_utf8":
        raw += b"\xe4\xb8"
    else:
        raw = sse("response.output_item.added", item={"type": "function_call", "name": "read_file"}) + raw
    collector = ResponseStream(50000, allow_data_envelope=True)
    with pytest.raises(StreamError):
        collector.feed(raw)
        collector.finish()


def test_secret_echo_in_ignored_sse_event_is_still_blocked():
    connection = snapshot()
    encoded = "".join("\\u%04x" % ord(c) for c in connection["api_key"])
    raw = ('data: {"type":"response.reasoning_text.delta","delta":"' + encoded + '"}\n\n').encode()
    raw += sse("response.completed", response=response_fixture("responses"))
    with pytest.raises(providers.ProviderError, match="^PROVIDER_SECRET_ECHO$"):
        providers.complete(connection, MESSAGES, transport=httpx.MockTransport(lambda r:
            httpx.Response(200, content=raw, headers={"Content-Type": "text/event-stream"})))


def test_non_responses_protocol_does_not_use_responses_sse_parser():
    with pytest.raises(providers.ProviderError, match="^UNSUPPORTED_STREAM_PROTOCOL$"):
        providers.complete(snapshot("openai"), MESSAGES, transport=httpx.MockTransport(lambda r:
            httpx.Response(200, content=sse("response.completed", response=response_fixture("responses")),
                           headers={"Content-Type": "text/event-stream"})))


def test_streaming_heartbeats_do_not_reset_total_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(providers.time, "monotonic", lambda: now[0])
    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(4):
                now[0] += 0.4
                yield b": heartbeat\n\n"
            yield sse("response.completed", response=response_fixture("responses"))
    with pytest.raises(providers.ProviderError, match="^PROVIDER_TIMEOUT$"):
        providers._request_json(snapshot(), "POST", "https://api.openai.com/v1/responses", {}, {}, 1,
            httpx.MockTransport(lambda r: httpx.Response(200, stream=SlowStream(), headers={"Content-Type": "text/event-stream"})))


@pytest.mark.parametrize("protocol", ["responses", "openai", "anthropic", "gemini", "ollama"])
def test_length_terminal_is_receipted_but_never_stop(protocol):
    body = response_fixture(protocol, "")
    if protocol == "responses":
        body.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    elif protocol == "openai":
        body["choices"][0]["finish_reason"] = "length"
    elif protocol == "anthropic":
        body["stop_reason"] = "max_tokens"
    elif protocol == "gemini":
        body["candidates"][0]["finishReason"] = "MAX_TOKENS"
    else:
        body["done_reason"] = "length"
    result = providers.normalize_response(protocol, body, "synthetic")
    assert result["choices"][0]["finish_reason"] == "length"
    assert result["provider_meta"]["outcome"] == "incomplete"


@pytest.mark.parametrize("protocol", ["responses", "openai", "anthropic", "gemini", "ollama"])
def test_empty_completed_result_has_own_error(protocol):
    with pytest.raises(providers.ProviderError, match="^PROVIDER_EMPTY_OUTPUT$") as caught:
        providers.normalize_response(protocol, response_fixture(protocol, ""), "synthetic")
    assert caught.value.diagnostic["outcome"] == "empty"


def test_responses_incomplete_message_preserves_length_only_with_incomplete_top_status():
    body = response_fixture("responses", '{"ok":')  # Real truncation need not be valid JSON.
    body.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    body["output"][1]["status"] = "incomplete"
    connection = snapshot("responses", "minimax", model_id="MiniMax-M3")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    result = providers.complete(connection, MESSAGES, output_schema=SCHEMA, transport=transport)
    assert result["choices"][0]["finish_reason"] == "length"
    assert result["provider_meta"]["outcome"] == "incomplete"
    assert result["provider_meta"]["response_status"] == "incomplete"
    assert result["provider_meta"]["incomplete_reason"] == "max_output_tokens"
    assert result["usage"] == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert "output_format" not in result  # No successful JSON/schema validation receipt.

    body["status"] = "completed"
    body.pop("incomplete_details")
    with pytest.raises(providers.ProviderError, match="^PROVIDER_RESPONSE_INCOMPLETE$"):
        providers.complete(connection, MESSAGES, output_schema=SCHEMA, transport=transport)


def test_codex_accepts_schema_without_new_tools_or_protocol_changes():
    calls = []
    class Engine:
        def complete(self, connection, messages, **kwargs):
            calls.append((messages, kwargs))
            return response_fixture("openai")
    result = providers.complete({"protocol": "codex_app_server", "provider_id": "chatgpt-codex",
        "model_id": "gpt-6-astra", "_codex_engine": Engine()}, MESSAGES, output_schema=SCHEMA)
    assert len(calls) == 1 and calls[0][1]["json_mode"] is True
    # This synthetic engine did not return reasoning telemetry. The provider
    # must not invent a "requested=True" receipt from the protocol name.
    assert result["transport_meta"] == {"structured_strategy": "prompt_json"}


@pytest.mark.parametrize("failure,code", [
    ("json", "PROVIDER_INVALID_JSON_OBJECT"),
    ("schema", "PROVIDER_OUTPUT_SCHEMA_INVALID"),
    ("schema_resolution", "PROVIDER_OUTPUT_SCHEMA_INVALID"),
])
def test_returned_but_rejected_preserves_safe_completed_response_receipt(failure, code):
    content = '  {"ok": "private-candidate-marker"' if failure == "json" else '  {"ok":"private-candidate-marker"}  '
    schema = (SCHEMA if failure != "schema_resolution" else {
        "type": "object", "properties": {"ok": {"$ref": "#/$defs/missing"}}})
    body = response_fixture("responses", content)
    body["usage"]["output_tokens_details"] = {"reasoning_tokens": 2}
    body["output"][0]["content"] = [{"type": "reasoning_text", "text": "private-reasoning-marker"}]
    with pytest.raises(providers.ProviderError, match="^" + code + "$") as caught:
        providers.complete(snapshot(), MESSAGES, output_schema=schema,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    detail = caught.value.diagnostic
    assert detail["outcome"] == "completed" and detail["finish_reason"] == "stop"
    assert detail["response_status"] == "completed"
    assert detail["response_chars"] == len(content)
    assert detail["usage"] == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7, "reasoning_tokens": 2}
    assert not any(marker in json.dumps(detail) for marker in ("private-candidate-marker", "private-reasoning-marker", content))
