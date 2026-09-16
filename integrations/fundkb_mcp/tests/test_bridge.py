"""Offline HTTP transport/security checks; no real endpoint or credential."""
import asyncio
import importlib.util
import json
import math
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

_SPEC = importlib.util.spec_from_file_location("fundkb_mcp_sidecar", Path(__file__).resolve().parents[1] / "server.py")
server = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = server
_SPEC.loader.exec_module(server)

TOKEN = "fkb_agent_" + "0" * 32 + "." + "x" * 43  # Deliberately unissued, syntactically valid.
BASE = "http://127.0.0.1:12345/api/v1"
OBJECT = "10000000-0000-4000-8000-000000000001"
KEY = "20000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("name,args,method,path,body", [
    ("list_capabilities", {"space_id": OBJECT}, "GET", "/capabilities", None),
    ("get_capability", {"capability_id": OBJECT}, "GET", "/capabilities/" + OBJECT, None),
    ("start_workflow", {"version_id": OBJECT, "inputs": {"业务日期": "2026-09-12"}, "mode": "guided", "request_id": KEY,
        "agent_label": "合成Agent"}, "POST", "/capability-runs", {"version_id": OBJECT,
        "inputs": {"业务日期": "2026-09-12"}, "mode": "guided", "agent_label": "合成Agent"}),
    ("get_next_steps", {"run_id": OBJECT}, "GET", f"/capability-runs/{OBJECT}/next", None),
    ("report_step", {"run_id": OBJECT, "step_id": "collect", "revision": 3, "status": "reported",
        "outputs": {"evidence": "合成引用"}, "note": "尚需人工", "request_id": KEY}, "POST",
        f"/capability-runs/{OBJECT}/steps/collect", {"status": "reported", "outputs": {"evidence": "合成引用"}, "note": "尚需人工"}),
    ("read_bound_sources", {"run_id": OBJECT}, "GET", f"/capability-runs/{OBJECT}/sources", None),
    ("get_workflow", {"run_id": OBJECT}, "GET", f"/capability-runs/{OBJECT}", None),
])
def test_exact_tool_to_http_mapping(name, args, method, path, body):
    requests = []
    def handler(req):
        requests.append(req)
        return httpx.Response(201 if name == "start_workflow" else 200,
            json={"marker": "合成结果", "revision": 4}, headers={"ETag": '"4"'})
    bridge = server.HttpBridge(server.Config(BASE, TOKEN), transport=httpx.MockTransport(handler))
    result = asyncio.run(bridge.call(name, args))
    assert result == {"data": {"marker": "合成结果", "revision": 4}, "etag": '"4"',
        "http_status": 201 if name == "start_workflow" else 200}
    req = requests[0]
    assert len(requests) == 1 and req.method == method and req.url.path == "/api/v1" + path
    assert req.headers["Authorization"] == "Bearer " + TOKEN and "cookie" not in req.headers
    assert TOKEN not in str(req.url)
    assert dict(req.url.params) == ({"space_id": OBJECT} if name == "list_capabilities" else {})
    assert (json.loads(req.content) if req.content else None) == body
    if method == "POST": assert req.headers["Idempotency-Key"] == "fkb-mcp-" + KEY
    else: assert "Idempotency-Key" not in req.headers
    if name == "report_step": assert req.headers["If-Match"] == '"3"'
    else: assert "If-Match" not in req.headers


@pytest.mark.parametrize("url", ["", "http://example.com/api/v1", "http://localhost/api/v1", "http://0.0.0.0/api/v1",
    "http://127.0.0.1.evil.invalid/api/v1", "ftp://127.0.0.1/api/v1", "file:///etc/passwd",
    "https://user:password@example.com/api/v1", "https://example.com/api/v1?token=private",
    "https://example.com/api/v1#fragment", "https://example.com/api/v1/../../agent-access", "https://example.com/api%2fv1",
    "https://example.com:bad/api/v1", "https://example.com:0/api/v1", "https://example.com/api/v1\n",
    "https://example.com/api/v1\\..", "https://example.com", "https://example.com/../api/v1"])
def test_bad_base_url_never_leaks_configuration(url):
    with pytest.raises(server.BridgeError) as err:
        server.Config(url, TOKEN)
    assert err.value.code == "CONFIG_REQUIRED"
    assert TOKEN not in str(err.value) and "password" not in str(err.value) and "private" not in str(err.value)


@pytest.mark.parametrize("url", [BASE, "http://[::1]:12345/api/v1", "https://kb.example.com/api/v1/",
    "https://kb.example.com/proxy/fund-kb/api/v1"])
def test_supported_fixed_origin_config(url):
    config = server.Config(url, TOKEN)
    assert config.api_url == url.rstrip("/") and TOKEN not in repr(config)


@pytest.mark.parametrize("token", ["", "cookie-value", TOKEN + "\n", "Bearer " + TOKEN, TOKEN + ";extra"])
def test_reject_invalid_token_format_without_echo(token):
    with pytest.raises(server.BridgeError) as err:
        server.Config(BASE, token)
    assert err.value.code == "CONFIG_REQUIRED" and TOKEN not in str(err.value)


@pytest.mark.parametrize("name,args", [
    ("reviewCapabilityRun", {}), ("create_token", {}), ("cancel_workflow", {"run_id": OBJECT}),
    ("get_workflow", {"run_id": "../../agent-access"}), ("get_capability", {"capability_id": "https://evil.invalid"}),
    ("read_bound_sources", {"run_id": OBJECT, "path": "/etc/passwd"}),
    ("read_bound_sources", {"run_id": OBJECT, "source_id": OBJECT}),
    ("list_capabilities", {"space_id": OBJECT, "url": "https://evil.invalid"}),
    ("start_workflow", {"version_id": OBJECT, "inputs": {}, "mode": "guided"}),
    ("start_workflow", {"version_id": OBJECT, "inputs": {}, "mode": "guided", "request_id": KEY, "owner_id": OBJECT}),
    ("get_workflow", None),
    ("report_step", {"run_id": OBJECT, "step_id": "../review", "revision": 1, "status": "reported",
        "outputs": {}, "note": "", "request_id": KEY}),
    ("report_step", {"run_id": OBJECT, "step_id": "collect\n", "revision": 1, "status": "reported",
        "outputs": {}, "note": "", "request_id": KEY}),
    ("report_step", {"run_id": OBJECT, "step_id": "review", "revision": True, "status": "reported",
        "outputs": {}, "note": "", "request_id": KEY}),
])
def test_untrusted_arguments_cannot_change_routes_or_add_tools(name, args):
    requests = []
    bridge = server.HttpBridge(server.Config(BASE, TOKEN), transport=httpx.MockTransport(lambda req: requests.append(req)))
    with pytest.raises(server.BridgeError) as err:
        asyncio.run(bridge.call(name, args))
    assert err.value.code in {"UNKNOWN_TOOL", "INVALID_ARGUMENTS"} and requests == []


def test_body_limit_nonfinite_values_and_no_request_on_validation_failure():
    requests = []
    bridge = server.HttpBridge(server.Config(BASE, TOKEN), transport=httpx.MockTransport(lambda req: requests.append(req)))
    args = {"version_id": OBJECT, "inputs": {}, "mode": "guided", "request_id": KEY}
    for value, code in ((math.nan, "INVALID_ARGUMENTS"), ("x" * (server.MAX_REQUEST_BYTES + 1), "REQUEST_TOO_LARGE")):
        with pytest.raises(server.BridgeError) as err:
            asyncio.run(bridge.call("start_workflow", {**args, "inputs": {"value": value}}))
        assert err.value.code == code
    assert not requests


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 403, 404, 409, 412, 422, 429, 500, 503])
def test_http_errors_and_redirects_do_not_retry_or_echo_sensitive_body(status):
    requests = []
    def handler(req):
        requests.append(req)
        return httpx.Response(status, text=TOKEN + " secret body", headers={"Location": "https://evil.invalid/?token=" + TOKEN})
    bridge = server.HttpBridge(server.Config(BASE, TOKEN), transport=httpx.MockTransport(handler))
    with pytest.raises(server.BridgeError) as err:
        asyncio.run(bridge.call("get_workflow", {"run_id": OBJECT}))
    assert len(requests) == 1 and err.value.http_status == status
    assert TOKEN not in str(err.value) and "secret body" not in str(err.value)


@pytest.mark.parametrize("body,headers", [(b"<html>private</html>", {"Content-Type": "text/html"}),
    (b"not-json", {"Content-Type": "application/json"}), (b"[]", {"Content-Type": "application/json"}),
    (b'{"x": NaN}', {"Content-Type": "application/json"})])
def test_invalid_upstream_responses_are_errors(body, headers):
    bridge = server.HttpBridge(server.Config(BASE, TOKEN), transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=body, headers=headers)))
    with pytest.raises(server.BridgeError) as err:
        asyncio.run(bridge.call("get_workflow", {"run_id": OBJECT}))
    assert err.value.code == "INVALID_RESPONSE"


def test_response_limit_never_truncates_success(monkeypatch):
    monkeypatch.setattr(server, "MAX_RESPONSE_BYTES", 100)
    bridge = server.HttpBridge(server.Config(BASE, TOKEN), transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"items": ["x" * 101]})))
    with pytest.raises(server.BridgeError) as err:
        asyncio.run(bridge.call("list_capabilities", {"space_id": OBJECT}))
    assert err.value.code == "RESPONSE_TOO_LARGE"


@pytest.mark.parametrize("value", [TOKEN, TOKEN.split(".", 1)[1], "Bearer " + TOKEN])
def test_even_success_response_cannot_echo_the_configured_credential(value):
    # JSON escape normalization must not evade the credential check.
    encoded = json.dumps({"unexpected": value}).replace("fkb", "\\u0066kb").encode()
    bridge = server.HttpBridge(server.Config(BASE, TOKEN), transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=encoded, headers={"Content-Type": "application/json"})))
    with pytest.raises(server.BridgeError) as err:
        asyncio.run(bridge.call("get_workflow", {"run_id": OBJECT}))
    assert err.value.code == "UNSAFE_RESPONSE" and TOKEN not in str(err.value)


def test_write_timeout_has_unknown_outcome_and_never_automatic_retry():
    requests = []
    def handler(req):
        requests.append(req)
        raise httpx.ReadTimeout(TOKEN, request=req)
    bridge = server.HttpBridge(server.Config(BASE, TOKEN), transport=httpx.MockTransport(handler))
    with pytest.raises(server.BridgeError) as err:
        asyncio.run(bridge.call("start_workflow", {"version_id": OBJECT, "inputs": {}, "mode": "trial", "request_id": KEY}))
    assert err.value.code == "HTTP_OUTCOME_UNKNOWN" and TOKEN not in str(err.value) and len(requests) == 1


def test_no_cookie_jar_proxy_or_env_credential_discovery(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/private-cert")
    requests = []
    def handler(req):
        requests.append(req)
        return httpx.Response(200, json={"items": []}, headers={"Set-Cookie": "kb_session=do-not-reuse; Path=/"})
    bridge = server.HttpBridge(server.Config(BASE, TOKEN), transport=httpx.MockTransport(handler))
    async def run():
        await bridge.call("get_workflow", {"run_id": OBJECT})
        await bridge.call("get_workflow", {"run_id": OBJECT})
    asyncio.run(run())
    assert len(requests) == 2 and all("cookie" not in req.headers for req in requests)


def test_same_caller_request_id_produces_same_body_headers_and_no_automatic_refresh():
    requests = []
    def handler(req):
        requests.append(req)
        return httpx.Response(200, json={"revision": 3})
    bridge = server.HttpBridge(server.Config(BASE, TOKEN), transport=httpx.MockTransport(handler))
    args = {"run_id": OBJECT, "step_id": "collect", "revision": 2, "status": "reported",
        "outputs": {}, "note": "", "request_id": str(uuid4())}
    async def run():
        await bridge.call("report_step", args)
        await bridge.call("report_step", args)
    asyncio.run(run())
    assert len(requests) == 2 and requests[0].content == requests[1].content
    assert requests[0].headers["idempotency-key"] == requests[1].headers["idempotency-key"]
    assert requests[0].headers["if-match"] == requests[1].headers["if-match"] == '"2"'
