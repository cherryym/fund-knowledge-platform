"""Real stdio wire + official MCP client, against an ephemeral synthetic HTTP API."""
import asyncio
import json
import os
import selectors
import subprocess
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SCRIPT = Path(__file__).resolve().parents[1] / "server.py"
TOKEN = "fkb_agent_" + "0" * 32 + "." + "x" * 43
OBJECT = "10000000-0000-4000-8000-000000000001"
KEY = "20000000-0000-4000-8000-000000000001"
TOOL_NAMES = {"list_capabilities", "get_capability", "start_workflow", "get_next_steps", "report_step",
    "read_bound_sources", "get_workflow"}


@pytest.fixture
def fake_api():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append({"method": self.command, "path": self.path, "body": json.loads(raw) if raw else None,
                "authorization": self.headers.get("Authorization"), "cookie": self.headers.get("Cookie"),
                "key": self.headers.get("Idempotency-Key"), "etag": self.headers.get("If-Match")})
            status = 201 if self.command == "POST" and self.path == "/api/v1/capability-runs" else 200
            content = {"synthetic": True, "revision": 2, "state": "WAITING_HUMAN", "notes": ["离线合成；未执行金融任务"]}
            if "/sources" in self.path:
                content["items"] = [{"version_id": OBJECT, "text": "合成来源：保持日期及来源定位。"}]
            body = json.dumps(content, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"2"')
            self.send_header("Set-Cookie", "kb_session=synthetic-not-for-reuse; Path=/")
            self.end_headers()
            self.wfile.write(body)

        do_GET = respond
        do_POST = respond

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/api/v1", requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def child_env(base=None):
    # Do not inherit or inspect the user's configured FKB values or credentials.
    values = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"}
    if base:
        values.update(FKB_AGENT_API_URL=base, FKB_AGENT_TOKEN=TOKEN)
    return values


def test_official_sdk_initialize_ping_list_and_all_seven_tools_over_stdio(fake_api):
    base, requests = fake_api
    async def run():
        parameters = StdioServerParameters(command=sys.executable, args=["-B", str(SCRIPT)], env=child_env(base))
        async with stdio_client(parameters) as (read, write):  # noqa: SIM117 - show transport/session lifetimes
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session:
                initialized = await session.initialize()
                assert initialized.protocolVersion == "2025-11-25"
                assert initialized.serverInfo.name == "fundkb-mcp"
                capabilities = initialized.capabilities.model_dump(exclude_none=True)
                assert set(capabilities) == {"tools", "experimental"}
                assert not capabilities["experimental"]
                await session.send_ping()
                listing = await session.list_tools()
                assert {tool.name for tool in listing.tools} == TOOL_NAMES
                assert listing.nextCursor is None and requests == []
                for tool in listing.tools:
                    assert tool.inputSchema["additionalProperties"] is False
                calls = [
                    ("list_capabilities", {"space_id": OBJECT}),
                    ("get_capability", {"capability_id": OBJECT}),
                    ("start_workflow", {"version_id": OBJECT, "inputs": {}, "mode": "guided", "request_id": KEY}),
                    ("get_next_steps", {"run_id": OBJECT}),
                    ("report_step", {"run_id": OBJECT, "step_id": "collect", "revision": 1, "status": "reported",
                        "outputs": {"evidence_summary": "合成离线回传"}, "note": "待人工确认", "request_id": KEY}),
                    ("read_bound_sources", {"run_id": OBJECT}),
                    ("get_workflow", {"run_id": OBJECT}),
                ]
                for name, arguments in calls:
                    result = await session.call_tool(name, arguments=arguments)
                    assert result.isError is False
                    assert result.structuredContent["data"]["synthetic"] is True
                    assert json.loads(result.content[0].text) == result.structuredContent
                    assert TOKEN not in result.content[0].text
                count = len(requests)
                for name, args in [("human_review", {}), ("read_bound_sources", {"run_id": "../../me"}),
                        ("start_workflow", {"version_id": OBJECT, "inputs": {}, "mode": "guided"})]:
                    denied = await session.call_tool(name, arguments=args)
                    assert denied.isError and TOKEN not in denied.content[0].text
                assert len(requests) == count
    asyncio.run(run())
    assert len(requests) == 7
    assert all(r["authorization"] == "Bearer " + TOKEN and r["cookie"] is None for r in requests)
    assert requests[2]["key"] == "fkb-mcp-" + KEY and requests[4]["etag"] == '"1"'
    assert requests[4]["path"] == f"/api/v1/capability-runs/{OBJECT}/steps/collect"


def _send(proc, message):
    proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def _receive(proc):
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=10), "Timed out waiting for a protocol response"
    line = proc.stdout.readline()
    assert line, "stdio closed before a response"
    value = json.loads(line)
    assert value["jsonrpc"] == "2.0"
    return value


def test_raw_jsonrpc_negotiation_initialized_notification_errors_and_eof_shutdown(fake_api):
    base, requests = fake_api
    proc = subprocess.Popen([sys.executable, "-B", str(SCRIPT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", env=child_env(base))
    try:
        # Unsupported version: negotiate the server's supported version, per MCP.
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "1900-01-01", "capabilities": {}, "clientInfo": {"name": "offline-wire", "version": "1"}}})
        assert _receive(proc)["result"]["protocolVersion"] == "2025-11-25"
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = _receive(proc)
        assert listed["id"] == 2 and {t["name"] for t in listed["result"]["tools"]} == TOOL_NAMES
        # SDK 1.28's typed request decoder rejects unknown method variants with
        # INVALID_PARAMS; a known but unregistered method is METHOD_NOT_FOUND.
        for index, (method, code) in enumerate((("fundkb/private", -32602), ("shutdown", -32602),
                ("resources/list", -32601)), 3):
            _send(proc, {"jsonrpc": "2.0", "id": index, "method": method, "params": {}})
            denied = _receive(proc)
            assert denied["id"] == index and denied["error"]["code"] == code
        _send(proc, {"jsonrpc": "2.0", "id": 6, "method": "ping"})
        assert _receive(proc) == {"jsonrpc": "2.0", "id": 6, "result": {}}
        assert requests == []
        proc.stdin.close()  # MCP stdio has no invented shutdown method.
        assert proc.wait(timeout=10) == 0
        assert proc.stdout.read() == "" and TOKEN not in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()


def test_missing_environment_fails_before_network_without_reading_host_credentials():
    result = subprocess.run([sys.executable, "-B", str(SCRIPT)], input="", capture_output=True,
        text=True, env=child_env(), timeout=10, check=False)
    assert result.returncode == 2 and result.stdout == ""
    assert "CONFIG_REQUIRED" in result.stderr and TOKEN not in result.stderr
