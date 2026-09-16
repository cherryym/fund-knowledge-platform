"""Cancellable deadline-free Markdown: synthetic async HTTP and fake Codex only."""
import asyncio
import json
import math
import subprocess
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from fund_kb import ai_transport, codex_bridge, codex_text, providers
from fund_kb.codex_stability import RequestBudget
from test_codex_stream_backpressure import EngineHarness, terminal_events
from test_providers import response_fixture, snapshot


MARKDOWN = "# 合成答复\n\n这是完整的 **Markdown**，不是 JSON。\n\n- 保留适用条件。"
REAL_RESOLVE = providers._resolve_cancellable


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("No real network, subprocess or credentials")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(providers, "_master_key", forbidden)
    monkeypatch.setattr(codex_bridge, "_read_private", forbidden)
    monkeypatch.setattr(codex_text, "_read_private", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    async def dns(host, port):
        return ["127.0.0.1"] if host in providers.LOCAL_HOSTS else ["93.184.216.34"]
    monkeypatch.setattr(providers, "_resolve_cancellable", dns)
    yield
    assert not [t for t in threading.enumerate() if t.name == "fkb-cancellable-http"]


@pytest.mark.parametrize("protocol,brand,model", [
    ("responses", "minimax", "MiniMax-M3"), ("responses", "openai", "gpt-6-astra"),
    ("openai", "openai", "gpt-6-astra"), ("openai", "qwen", "qwen3.8-max"),
    ("anthropic", "anthropic", "claude-opus-5"), ("gemini", "google", "gemini-3.8-flash"),
    ("ollama", "ollama", "installed-synthetic"),
])
def test_markdown_wait_ignores_deployment_idle_caps_and_does_not_force_reasoning(protocol, brand, model):
    captured, checks = [], []
    async def respond(request):
        captured.append((json.loads(request.content), request.extensions["timeout"]))
        await asyncio.sleep(.03)  # Longer than BOTH snapshot limits below.
        return httpx.Response(200, json=response_fixture(protocol, MARKDOWN))
    connection = snapshot(protocol, brand, model_id=model, http_timeout=.001, read_idle_timeout=.001,
                          _cancel_check=lambda: checks.append(time.monotonic()))
    result = providers.complete(connection, [{"role": "user", "content": "Markdown answer"}],
        timeout=None, json_mode=False, output_schema=None, max_tokens=8192, transport=httpx.MockTransport(respond))
    assert result["choices"][0]["message"]["content"] == MARKDOWN
    assert result["choices"][0]["finish_reason"] == "stop"
    adaptive = protocol == "responses" and brand == "minimax"
    assert result["transport_meta"] == {"structured_strategy": "none", "reasoning_requested": True if adaptive else None}
    payload, limits = captured[0]
    assert len(captured) == 1 and 2 <= len(checks) <= 3
    assert limits == {"connect": 10, "read": None, "write": None, "pool": None}
    if adaptive:
        assert payload["reasoning"] == {"effort": "medium"}
    else:
        assert "reasoning" not in payload
    assert not ({"reasoning_effort", "enable_thinking", "tools", "response_format", "format"} & set(payload))
    assert "Return a valid JSON object" not in json.dumps(payload)
    assert "output_format" not in result


@pytest.mark.parametrize("stage", ["dns", "headers", "body"])
def test_cancel_during_silent_wait_closes_owned_request_before_return(monkeypatch, stage):
    active, closed, sent = threading.Event(), threading.Event(), []
    checks = []
    async def wait_forever():
        active.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    if stage == "dns":
        monkeypatch.setattr(providers, "_resolve_cancellable", lambda *a: wait_forever())
    class SilentBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await wait_forever()
            yield b""
        async def aclose(self):
            closed.set()
    async def respond(request):
        sent.append(request)
        if stage == "headers":
            await wait_forever()
        return httpx.Response(200, stream=SilentBody())
    def cancel():
        checks.append(time.monotonic())
        if active.is_set():
            raise providers.ProviderError("CANCELLED")
    started = time.monotonic()
    with pytest.raises(providers.ProviderError, match="^CANCELLED$"):
        providers.complete(snapshot(_cancel_check=cancel), [{"role": "user", "content": "Synthetic"}],
            json_mode=False, timeout=None, transport=httpx.MockTransport(respond))
    assert closed.is_set() and 0.8 <= time.monotonic() - started < 3
    assert len(checks) == 2 and checks[1] - checks[0] >= .9
    assert len(sent) == (0 if stage == "dns" else 1)


def test_unlimited_requires_a_live_cancellation_guard_before_request():
    with pytest.raises(providers.ProviderError, match="^CANCELLATION_CHECK_REQUIRED$"):
        providers.complete(snapshot(), [], timeout=None, json_mode=False)
    with pytest.raises(ai_transport.ProviderError, match="^CANCELLATION_CHECK_REQUIRED$"):
        ai_transport.post_json("https://synthetic.invalid", "chat/completions", {}, timeout=None)


def test_final_delivery_rechecks_cancellation_even_for_fast_response():
    returned = []
    async def respond(request):
        returned.append(True)
        return httpx.Response(200, json=response_fixture("responses", MARKDOWN))
    def cancel():
        if returned:
            raise providers.ProviderError("CONNECTION_REVISION_CHANGED")
    with pytest.raises(providers.ProviderError, match="^CONNECTION_REVISION_CHANGED$"):
        providers.complete(snapshot(_cancel_check=cancel), [{"role": "user", "content": "Synthetic"}],
            json_mode=False, timeout=None, transport=httpx.MockTransport(respond))
    assert returned == [True]


@pytest.mark.parametrize("kind", ["redirect", "encoding", "size", "secret", "tool", "partial_stream"])
def test_unlimited_keeps_transport_and_terminal_security_boundaries(kind):
    body = response_fixture("responses", MARKDOWN)
    connection = snapshot(_cancel_check=lambda: None)
    expected = {"redirect": "PROVIDER_REDIRECT_BLOCKED", "encoding": "PROVIDER_ENCODING_UNSUPPORTED",
        "size": "PROVIDER_RESPONSE_TOO_LARGE", "secret": "PROVIDER_SECRET_ECHO",
        "tool": "UNSUPPORTED_TOOL_CALL", "partial_stream": "PROVIDER_STREAM_INCOMPLETE"}[kind]
    async def respond(request):
        if kind == "redirect":
            return httpx.Response(307, headers={"Location": "https://must-not-follow.invalid"})
        if kind == "encoding":
            return httpx.Response(200, content=b"", headers={"Content-Encoding": "br"})
        if kind == "size":
            return httpx.Response(200, content=b"x" * 200)
        if kind == "secret":
            return httpx.Response(200, json=response_fixture("responses", connection["api_key"]))
        if kind == "tool":
            body["output"] = [{"type": "function_call", "name": "read_file"}]
        if kind == "partial_stream":
            return httpx.Response(200, content=b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=body)
    if kind == "size":
        connection["max_response_bytes"] = 100
    with pytest.raises(providers.ProviderError, match="^" + expected + "$"):
        providers.complete(connection, [{"role": "user", "content": "Synthetic"}], json_mode=False,
            timeout=None, transport=httpx.MockTransport(respond))


def test_legacy_unlimited_uses_same_cancellable_transport(monkeypatch):
    original, seen = httpx.AsyncClient, []
    async def respond(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=response_fixture("openai", MARKDOWN))
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw:
        original(**{**kw, "transport": httpx.MockTransport(respond)}))
    result = ai_transport.post_json("https://synthetic.invalid/v1", "chat/completions", {}, timeout=None,
        deployment_timeout=.001, read_idle_timeout=.001, cancel_check=lambda: None)
    assert result["choices"][0]["message"]["content"] == MARKDOWN
    assert seen == [{"connect": 10, "read": None, "write": None, "pool": None}]


def test_dns_child_is_killed_and_reaped_on_cancellation_without_real_process(monkeypatch):
    process = SimpleNamespace(returncode=None, killed=False, waited=0)
    async def read(limit):
        assert limit == 65537
        await asyncio.Event().wait()
    async def wait():
        process.waited += 1
        return process.returncode
    def kill():
        process.killed, process.returncode = True, -9
    process.stdout, process.wait, process.kill = SimpleNamespace(read=read), wait, kill
    async def spawn(*args, **kw):
        assert args[1:3] == ("-I", "-S") and set(kw["env"]) == {"PATH", "LANG"}
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    async def exercise():
        task = asyncio.create_task(REAL_RESOLVE("synthetic.invalid", 443))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(exercise())
    assert process.killed and process.waited == 1


def test_request_budget_none_has_no_deadline_and_throttles_guard():
    now, checked = [100.0], []
    def guard():
        checked.append(now[0])
        if now[0] > 10000:
            raise providers.ProviderError("CANCELLED")
    budget = RequestBudget(None, guard, lambda: now[0])
    for _ in range(50):
        budget.check()
        now[0] += .01
    assert len(checked) == 1 and math.isinf(budget.remaining())
    now[0] = 9000
    budget.check()
    assert len(checked) == 2
    now[0] = 10001
    with pytest.raises(providers.ProviderError, match="^CANCELLED$"):
        budget.check()


@pytest.mark.parametrize("cancel", [False, True])
def test_codex_unlimited_markdown_or_cancellation_keeps_signed_isolation(monkeypatch, cancel):
    h = EngineHarness(monkeypatch, terminal_events(MARKDOWN), step=900)
    def check():
        if cancel and h.clock.value > 100:
            raise providers.ProviderError("CANCELLED")
    h.snapshot["_cancel_check"] = check
    kwargs = {"max_tokens": 8192, "json_mode": False, "timeout": None}
    if cancel:
        with pytest.raises(providers.ProviderError, match="^CANCELLED$"):
            h.adapter.complete(h.snapshot, [{"role": "user", "content": "Synthetic"}], **kwargs)
    else:
        result = h.adapter.complete(h.snapshot, [{"role": "user", "content": "Synthetic"}], **kwargs)
        assert result["choices"][0]["message"]["content"] == MARKDOWN
        assert h.clock.value > 600
    assert h.closed and h.released
    h.assert_slot_released()
    assert codex_text.TURN_SAFETY == {"approvalPolicy": "never",
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}
    # Old signed profiles remain compatible; effort is no longer a universal
    # tool-isolation requirement for every future profile/model.
    assert codex_text.INSTRUCTION_CONTRACT["turn_safety"] == {
        "effort": "low", **codex_text.TURN_SAFETY}
