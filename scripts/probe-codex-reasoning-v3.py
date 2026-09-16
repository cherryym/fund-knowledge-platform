"""V3 fixed-binary probes against an OS-confined loopback mock only.

Reuse only the v2 synthetic fixture/capture inspector, never its runner or
profile builder. No account RPC, identity store, credential, or business input.
All writable artifacts belong to one new project candidate directory.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import selectors
import socketserver
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
from fund_kb.codex_bridge import VERIFIED_VERSION, _read_private, _write_private
from fund_kb.codex_text import (
    CONFIGURABLE_INSTRUCTION_CONTRACT,
    CONFIGURABLE_INSTRUCTION_CONTRACT_SHA256,
    CONFIGURABLE_PROFILE_VERSION,
    POLICY_SHA256,
    SAFE_ITEMS,
    TEXT_CONFIG,
    CodexTextEngine,
    check_text_request_budget,
    configured_text_request_params,
    restricted_catalog,
    text_request_sizes,
)
from fund_kb.providers import ProviderError

PINNED_EXECUTABLE_SHA256 = "a29d9e86eef88cbbd69f97ce8c590b1d0a287c8f77424f5eef226b883d7eaa22"
PINNED_V2_HELPER_SHA256 = "922509f26719e1154f783ef1c5ab018a00f739c81a45fa1f775713afeeda1877"
ATTACKS = (None, "apply_patch", "view_image", "exec_command")
PROBE_SUITE_VERSION = "reasoning-v3-loopback-matrix-1"
INITIALIZE = {"id": 1, "method": "initialize", "params": {
    "clientInfo": {"name": "fund_kb_reasoning_v3_probe", "version": "3.0"},
    "capabilities": {"experimentalApi": False, "optOutNotificationMethods": ["item/agentMessage/delta"]}}}


def digest(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode()


def load_helpers():
    path = Path(__file__).with_name("probe-codex-instruction-channels.py")
    if digest(path.read_bytes()) != PINNED_V2_HELPER_SHA256:
        raise ProviderError("V2_SYNTHETIC_HELPER_CHANGED")
    spec = importlib.util.spec_from_file_location("fkb_v2_pure_capture_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checked_path(path):
    path = Path(path)
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        raise ProviderError("OFFLINE_PATH_UNSAFE")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ProviderError("OFFLINE_PATH_UNSAFE")
    return path


def controller_fences(root, executable, catalog):
    """Audit the controller too; only child sandbox-exec may launch a process."""
    readable_files = {executable, catalog}
    code_roots = (PROJECT_ROOT / "backend", PROJECT_ROOT / "scripts", Path(sys.base_prefix), Path(sys.prefix))

    def inside(path):
        return path == root or root in path.parents

    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyaddr", "os.system", "os.exec"}:
            raise ProviderError("OFFLINE_CONTROLLER_EXTERNAL_IO_FORBIDDEN")
        if event == "socket.bind" and args[1][0] != "127.0.0.1":
            raise ProviderError("OFFLINE_LOOPBACK_BIND_REQUIRED")
        if event == "subprocess.Popen":
            command, cwd, environment = args[1], args[2], args[3]
            if command[:2] != ["/usr/bin/sandbox-exec", "-f"] or not inside(Path(command[2])) \
                    or not inside(Path(cwd)) or set(environment) != {"PATH", "CODEX_HOME", "TMPDIR", "LANG", "TZ"}:
                raise ProviderError("OFFLINE_SANDBOX_REQUIRED")
        if event in {"os.mkdir", "os.remove", "os.rmdir"} and not inside(Path(args[0]).absolute()):
            raise ProviderError("OFFLINE_WRITE_OUTSIDE_CANDIDATE")
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = Path(os.fsdecode(args[0])).absolute()
            writing = isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            if inside(path):
                return
            if writing:
                raise ProviderError("OFFLINE_WRITE_OUTSIDE_CANDIDATE")
            if path in readable_files:
                return
            if path.suffix in {".py", ".pyc", ".so", ".dylib"} and any(
                    path == parent or parent in path.parents for parent in code_roots):
                return
            raise ProviderError("OFFLINE_READ_OUTSIDE_ALLOWLIST")
    sys.addaudithook(audit)


def sandbox_policy(executable, port, runtime):
    """Deny file contents/writes globally, then allow only synthetic runtime.

    System library data and exact diagnostic executables are non-user inputs.
    Mach lookup is denied, including keychain/authentication services.
    """
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise ProviderError("OFFLINE_NETWORK_SANDBOX_REQUIRED")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ProviderError("OFFLINE_LOOPBACK_PORT_INVALID")
    literals = [executable, "/usr/bin/curl", "/bin/cat", "/usr/bin/touch",
                "/System", "/System/Volumes", "/System/Volumes/Preboot",
                "/private/etc/ssl/openssl.cnf"]
    return ('(version 1)\n(allow default)\n(deny network*)\n'
            f'(allow network-outbound (remote ip "localhost:{port}"))\n'
            '(deny mach-lookup)\n(deny file-read-data file-write*)\n'
            # Apple's local dyld-support.sb requires opening "/" as an
            # openat root. This literal never grants recursive content access.
            '(allow file-read-data (literal "/") (subpath "/System/Library") '
            '(subpath "/System/Cryptexes") (subpath "/System/Volumes/Preboot/Cryptexes") '
            '(subpath "/usr/lib") (subpath "/usr/share")\n'
            + " ".join(f"(literal {json.dumps(str(path), ensure_ascii=False)})" for path in literals) + ')\n'
            f'(allow file-read-data file-write* (subpath {json.dumps(str(runtime), ensure_ascii=False)}))\n'
            '(allow file-read-data file-write-data (literal "/dev/null") (literal "/dev/random") '
            '(literal "/dev/urandom") (literal "/dev/zero"))\n')


class LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer's default reverse lookup is unnecessary and prohibited.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]
        self.accepted_connections = 0

    def get_request(self):
        result = super().get_request()
        self.accepted_connections += 1
        return result


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"FKB_OFFLINE_MOCK_READY")


def boundary_check(command, environment, runtime, port, denied_port, denied_file):
    """Test both positive and negative controls; never contact an external IP."""
    def run(arguments):
        return subprocess.run([*command, *arguments], cwd=runtime / "workspace", env=environment,
                              capture_output=True, timeout=5, check=False)
    allowed = run(["/usr/bin/curl", "-q", "--noproxy", "*", "-fsS", "--max-time", "3",
                   f"http://127.0.0.1:{port}/_offline_ready"])
    network = run(["/usr/bin/curl", "-q", "--noproxy", "*", "-vfsS", "--max-time", "3",
                   f"http://127.0.0.1:{denied_port}/_denied"])
    read = run(["/bin/cat", str(denied_file)])
    write = run(["/usr/bin/touch", str(denied_file.parent / "must-not-be-created")])
    positive = run(["/bin/cat", str(runtime / "canary.txt")])
    alias = run(["/bin/cat", "/System/Volumes/Data" + str(denied_file)])
    denied = lambda result: result.returncode != 0 and bool(re.search(
        rb"operation not permitted|permission denied", result.stderr, re.IGNORECASE))
    checks = {
        "exact_mock_port_reachable": allowed.returncode == 0 and allowed.stdout == b"FKB_OFFLINE_MOCK_READY",
        "other_live_loopback_port_denied": network.returncode == 7 and denied(network)
            and b"connect" in network.stderr.lower() and str(denied_port).encode() in network.stderr,
        "outside_synthetic_file_read_denied": denied(read) and not read.stdout,
        "outside_synthetic_file_write_denied": denied(write),
        "data_volume_alias_read_denied": denied(alias) and not alias.stdout,
        "own_synthetic_file_readable": positive.returncode == 0 and positive.stdout == (runtime / "canary.txt").read_bytes(),
    }
    receipt = {"checks": checks, "passed": all(checks.values()),
               "return_codes": {name: result.returncode for name, result in (
                   ("allowed_http", allowed), ("denied_port", network), ("denied_read", read),
                   ("denied_write", write), ("allowed_read", positive), ("denied_alias", alias))},
               "synthetic_control_stderr": {name: result.stderr.decode(errors="replace")[:4000]
                   for name, result in (("allowed_http", allowed), ("denied_port", network),
                       ("denied_read", read), ("denied_write", write), ("allowed_read", positive), ("denied_alias", alias))}}
    _write_private(runtime / "boundary-check.json", json_bytes(receipt))
    if not receipt["passed"]:
        raise ProviderError("OFFLINE_BOUNDARY_VERIFICATION_FAILED")
    return receipt


def capture_verdict(helpers, body, thread, turn, canary, model, expected_effort):
    capture = helpers.inspect_capture(body, thread, turn, canary)
    observed = body.get("reasoning")
    observed_effort = observed.get("effort") if isinstance(observed, dict) else None
    capture.update(turn_reasoning={key: turn[key] for key in ("effort",) if key in turn},
                   provider_reasoning_effort=observed_effort,
                   reasoning_verified=observed_effort == expected_effort,
                   model_verified=body.get("model") == model)
    return capture


def run_probe(executable, catalog_bytes, model, effort, attack, runtime, helpers, denied_file):
    # Hash the reused binary before every process run; never resolve a host CLI.
    if digest(executable.read_bytes()) != PINNED_EXECUTABLE_SHA256:
        raise ProviderError("CODEX_VERSION_UNSUPPORTED")
    runtime.mkdir(mode=0o700)
    for name in ("workspace", "tmp"):
        (runtime / name).mkdir(mode=0o700)
    canary = "UNSENT_V3_CANARY_" + runtime.name
    _write_private(runtime / "canary.txt", canary.encode())
    _write_private(runtime / "workspace" / "AGENTS.md", canary.encode())
    _write_private(runtime / "models.json", catalog_bytes)
    thread, turn = configured_text_request_params(helpers.PROBE_MESSAGES, model, runtime / "workspace",
                                                  reasoning_effort=effort)
    info = next(item for item in json.loads(catalog_bytes)["models"] if item["slug"] == model)
    expected = info["default_reasoning_level"] if effort is None else effort
    captures, wire_bodies, rpc_writes, errors, final_text = [], [], [], [], []
    policy_path = runtime / "isolation.sb"

    class Handler(QuietHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if self.path != "/v1/responses" or not 0 < size <= 1048576 \
                    or self.headers.get("Authorization") is not None or self.headers.get("Proxy-Authorization") is not None:
                errors.append("UNEXPECTED_OR_AUTHENTICATED_MOCK_REQUEST")
                self.send_error(400)
                return
            body = json.loads(self.rfile.read(size))
            sent = [row["params"] for row in rpc_writes if row["method"] == "turn/start"]
            if len(sent) != 1:
                errors.append("TURN_WIRE_NOT_CAPTURED")
                self.send_error(400)
                return
            observed = capture_verdict(helpers, body, thread, sent[0], canary, model, expected)
            captures.append({"path": self.path, "http_body_bytes": size, **observed})
            wire_bodies.append({"body": body, "authorization_present": False, "path": self.path})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            message = {"id": "msg_probe", "type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": '{"ok":true}', "annotations": []}]}
            if attack and len(captures) == 1:
                message = {"id": "call_probe", "call_id": "call_probe", "name": attack, "status": "completed",
                           "type": "custom_tool_call" if attack == "apply_patch" else "function_call"}
                if attack == "apply_patch":
                    message["input"] = f"*** Begin Patch\n*** Update File: {runtime / 'canary.txt'}\n@@\n+CHANGED\n*** End Patch\n"
                else:
                    message["arguments"] = json.dumps({"path": str(runtime / "canary.txt")} if attack == "view_image"
                        else {"cmd": f"printf CHANGED > {runtime / 'canary.txt'}"})
            events = [
                {"type": "response.created", "response": {"id": "resp_probe", "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**message, "status": "in_progress", "content": []}},
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.completed", "response": {"id": "resp_probe", "status": "completed", "output": [message],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
            ]
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()

    server, blocked = LoopbackServer(("127.0.0.1", 0), Handler), LoopbackServer(("127.0.0.1", 0), QuietHandler)
    workers = [threading.Thread(target=item.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
               for item in (server, blocked)]
    for worker in workers:
        worker.start()
    process, completed, boundary = None, False, None
    selector = selectors.DefaultSelector()
    buffers = {"stdout": b"", "stderr": b""}
    policy = sandbox_policy(executable, server.server_port, runtime)
    _write_private(policy_path, policy.encode())
    command = ["/usr/bin/sandbox-exec", "-f", str(policy_path)]
    environment = {"PATH": "/usr/bin:/bin", "CODEX_HOME": str(runtime), "TMPDIR": str(runtime / "tmp"),
                   "LANG": "en_US.UTF-8", "TZ": "UTC"}
    try:
        boundary = boundary_check(command, environment, runtime, server.server_port, blocked.server_port, denied_file)
        if blocked.accepted_connections or (denied_file.parent / "must-not-be-created").exists():
            raise ProviderError("OFFLINE_BOUNDARY_VERIFICATION_FAILED")
        config = (f'model = {json.dumps(model)}\nmodel_provider = "offline_probe"\n'
                  + TEXT_CONFIG.replace('forced_login_method = "chatgpt"\n', '')
                  + '\n[model_providers.offline_probe]\nname = "Offline synthetic probe"\n'
                  + f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
                  + 'wire_api = "responses"\nrequires_openai_auth = false\nrequest_max_retries = 0\nstream_max_retries = 0\n')
        _write_private(runtime / "config.toml", config.encode())
        version = subprocess.run([*command, str(executable), "--version"], cwd=runtime / "workspace", env=environment,
                                 capture_output=True, timeout=5, check=False)
        if version.returncode or version.stdout.decode().strip() != VERIFIED_VERSION:
            raise ProviderError("CODEX_VERSION_UNSUPPORTED")
        process = subprocess.Popen([*command, str(executable), "app-server", "--strict-config", "--listen", "stdio://"],
                                   cwd=runtime / "workspace", env=environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def send(value):
            payload = (json.dumps(value, separators=(",", ":")) + "\n").encode()
            rpc_writes.append(json.loads(payload))
            process.stdin.write(payload)
            process.stdin.flush()

        send(INITIALIZE)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        end = time.monotonic() + 30
        while time.monotonic() < end and process.poll() is None and not completed and not errors:
            for key, _ in selector.select(.1):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                buffers[key.data] += data
                if len(buffers[key.data]) > 1048576:
                    errors.append("PROBE_OUTPUT_TOO_LARGE")
                    break
                if key.data == "stderr":
                    continue
                while b"\n" in buffers["stdout"]:
                    line, buffers["stdout"] = buffers["stdout"].split(b"\n", 1)
                    event = json.loads(line)
                    if "error" in event or ("id" in event and "method" in event):
                        errors.append("RPC_FAILED_OR_UNSOLICITED")
                        break
                    if event.get("id") == 1:
                        send({"method": "initialized", "params": {}})
                        send({"id": 3, "method": "thread/start", "params": thread})
                    elif event.get("id") == 3:
                        turn["threadId"] = event["result"]["thread"]["id"]
                        check_text_request_budget(thread, turn)
                        send({"id": 4, "method": "turn/start", "params": turn})
                    params = event.get("params", {})
                    if event.get("method") in {"item/started", "item/completed"}:
                        item = params.get("item", {})
                        if item.get("type") not in SAFE_ITEMS:
                            errors.append("UNEXPECTED_NON_TEXT_ITEM")
                        if event["method"] == "item/completed" and item.get("type") == "agentMessage":
                            final_text.append(item.get("text"))
                    if event.get("method") == "error" and not params.get("willRetry"):
                        errors.append("TURN_ERROR")
                    if event.get("method") == "turn/completed":
                        completed = params.get("turn", {}).get("status") == "completed"
                        if not completed:
                            errors.append("TURN_NOT_COMPLETED")
    finally:
        if process is not None:
            process.terminate()
            try:
                _, tail = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                _, tail = process.communicate(timeout=3)
            buffers["stderr"] += tail
        selector.close()
        for item in (server, blocked):
            item.shutdown()
            item.server_close()
        for worker in workers:
            worker.join(timeout=2)
    stderr = buffers["stderr"].decode(errors="replace")
    unsupported = bool(attack and any("unsupported" in line and attack in line for line in stderr.splitlines()))
    sizes = text_request_sizes(thread, turn)
    checks = {
        "completed": completed and not errors,
        "final_mock_output": final_text == ['{"ok":true}'],
        "canary_unchanged": (runtime / "canary.txt").read_text() == canary,
        "instruction_channels": bool(captures) and all(row["instruction_channels_verified"] for row in captures),
        "reasoning": bool(captures) and all(row["reasoning_verified"] for row in captures),
        "model": bool(captures) and all(row["model_verified"] for row in captures),
        "zero_tools": bool(captures) and all(not row["tools"] for row in captures),
        "attack_rejected": attack is None or unsupported,
        "capture_count": len(captures) == (1 if attack is None else 2),
        "layered_budget": sizes["semantic_utf8_bytes"] <= 65536 < sizes["rpc_total_bytes"]
                          and max(sizes["thread_rpc_bytes"], sizes["turn_rpc_bytes"]) <= 262144,
        "no_account_rpc": [row["method"] for row in rpc_writes] == ["initialize", "initialized", "thread/start", "turn/start"],
        "boundary": boundary is not None and boundary["passed"] and blocked.accepted_connections == 0,
        "no_auth_file": not (runtime / "auth.json").exists(),
    }
    row = {"model": model, "reasoning_effort": effort, "attack": attack, "passed": all(checks.values()),
           "profile_version": CONFIGURABLE_PROFILE_VERSION,
           "instruction_contract_sha256": CONFIGURABLE_INSTRUCTION_CONTRACT_SHA256,
           "policy_sha256": POLICY_SHA256, "catalog_sha256": digest(catalog_bytes),
           "executable_sha256": PINNED_EXECUTABLE_SHA256, "executable_version": VERIFIED_VERSION,
           "probe_suite_version": PROBE_SUITE_VERSION, "probe_fixture_sha256": helpers.PROBE_FIXTURE_SHA256,
           "completed": checks["completed"], "canary_unchanged": checks["canary_unchanged"],
           "instruction_channels_verified": checks["instruction_channels"], "captures": captures,
           "startup_errors": "unsupported call: " + attack if unsupported else "", "errors": errors, "checks": checks,
           "request_sizes": sizes, "root": str(runtime), "os_policy_sha256": digest(policy),
           "mock_port": server.server_port, "denied_control_port": blocked.server_port,
           "live_model_calls": 0, "credential_files_read": 0}
    _write_private(runtime / "rpc-writes.json", json_bytes(rpc_writes))
    _write_private(runtime / "mock-http-captures.json", json_bytes(wire_bodies))
    _write_private(runtime / "synthetic-stderr.txt", stderr.encode())
    _write_private(runtime / "result.json", json_bytes(row))
    return row


def matrix_for(models, extra_efforts):
    pairs = [(model["slug"], None) for model in models]
    by_id = {model["slug"]: model for model in models}
    if len(by_id) != len(models) or not models or len(models) > 50 or any(
            type(model) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", model) for model in by_id):
        raise ProviderError("INVALID_PROBE_MODEL_SELECTION")
    for entry in extra_efforts:
        model, separator, effort = entry.partition("=")
        if separator != "=" or model not in by_id or effort not in {
                row["effort"] for row in by_id[model]["supported_reasoning_levels"]} or (model, effort) in pairs:
            raise ProviderError("INVALID_PROBE_REASONING_SELECTION")
        pairs.append((model, effort))
    return [(model, effort, attack) for model, effort in pairs for attack in ATTACKS]


def verify_bundle(root):
    """Read-only recomputation from frozen wire artifacts; never starts Codex."""
    root = checked_path(root)
    manifest = json.loads(_read_private(root / "artifact-manifest.json", 1048576))
    for relative, expected in manifest["sha256"].items():
        path = checked_path(root / relative)
        if root not in path.parents or digest(path.read_bytes()) != expected:
            raise ProviderError("CANDIDATE_ARTIFACT_CHANGED")
    profile = json.loads(_read_private(root / "text-profile.json", 1048576))
    report = json.loads(_read_private(root / "verification-report.json", 1048576))
    catalog = json.loads(_read_private(root / "text-models.json", 1048576))
    helpers = load_helpers()
    if report["status"] != "PASS" or report["probes"] != profile["probes"]:
        raise ProviderError("CANDIDATE_REPORT_INCONSISTENT")
    for row in profile["probes"]:
        runtime = checked_path(Path(row["root"]))
        if root not in runtime.parents:
            raise ProviderError("CANDIDATE_ARTIFACT_CHANGED")
        rpc = json.loads((runtime / "rpc-writes.json").read_bytes())
        if rpc[0] != INITIALIZE or [item["method"] for item in rpc] != ["initialize", "initialized", "thread/start", "turn/start"]:
            raise ProviderError("CANDIDATE_RPC_CHANGED")
        thread, turn = configured_text_request_params(helpers.PROBE_MESSAGES, row["model"], runtime / "workspace",
            rpc[-1]["params"]["threadId"], reasoning_effort=row["reasoning_effort"])
        if rpc[-2]["params"] != thread or rpc[-1]["params"] != turn:
            raise ProviderError("CANDIDATE_RPC_CHANGED")
        info = next(model for model in catalog["models"] if model["slug"] == row["model"])
        expected = info["default_reasoning_level"] if row["reasoning_effort"] is None else row["reasoning_effort"]
        wire = json.loads((runtime / "mock-http-captures.json").read_bytes())
        if len(wire) != len(row["captures"]):
            raise ProviderError("CANDIDATE_CAPTURE_CHANGED")
        for actual, recorded in zip(wire, row["captures"], strict=True):
            result = capture_verdict(helpers, actual["body"], thread, turn,
                "UNSENT_V3_CANARY_" + runtime.name, row["model"], expected)
            if actual["authorization_present"] or actual["path"] != "/v1/responses" or any(
                    recorded[key] != value for key, value in result.items()):
                raise ProviderError("CANDIDATE_CAPTURE_CHANGED")
    engine = CodexTextEngine(SimpleNamespace(config=SimpleNamespace(executable_sha256=PINNED_EXECUTABLE_SHA256)),
                             root / "text-profile.json")
    defaults = {model: engine.reasoning_configuration({"model_id": model}) for model in sorted(engine.models)}
    overrides = [{**engine.reasoning_configuration({"model_id": row["model"]}, row["reasoning_effort"]), "model": row["model"]}
                 for row in profile["probes"] if row["attack"] is None and row["reasoning_effort"] is not None]
    return {"status": "PASS", "case_count": len(profile["probes"]), "default_resolution": defaults,
            "verified_overrides": overrides, "live_model_calls": 0, "candidate_root": str(root)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--effort", action="append", default=[], help="Explicit extra model=effort pair; no blanket override")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    if args.verify_only:
        print(json.dumps(verify_bundle(args.root), ensure_ascii=False))
        return 0
    root = checked_path(args.root)
    if root.parent != PROJECT_ROOT / "data" or not root.name.startswith("codex-text-v3-candidate-") \
            or root.exists() or not args.executable or not args.catalog:
        parser.error("Use a NEW project data/codex-text-v3-candidate-* directory and explicit public executable/catalog.")
    executable, catalog_path = checked_path(args.executable), checked_path(args.catalog)
    if not executable.is_file() or digest(executable.read_bytes()) != PINNED_EXECUTABLE_SHA256:
        raise ProviderError("CODEX_VERSION_UNSUPPORTED")
    if catalog_path.name != "text-models.json":
        parser.error("Only the explicit public text-models.json catalog is accepted.")
    catalog_bytes = _read_private(catalog_path, 1048576)
    models = json.loads(catalog_bytes)["models"]
    if restricted_catalog(models) != {"models": models}:
        raise ProviderError("CODEX_TEXT_PROFILE_UNVERIFIED")
    matrix = matrix_for(models, args.effort)
    helpers = load_helpers()
    controller_fences(root, executable, catalog_path)
    root.mkdir(mode=0o700)
    (root / "runs").mkdir(mode=0o700)
    _write_private(root / "denied-synthetic-control.txt", b"UNSENT_OUTSIDE_RUNTIME_CONTROL")
    rows = []
    try:
        for index, (model, effort, attack) in enumerate(matrix, 1):
            runtime = root / "runs" / f"{index:02d}-{model}-{effort or 'default'}-{attack or 'normal'}"
            row = run_probe(executable, catalog_bytes, model, effort, attack, runtime, helpers,
                            root / "denied-synthetic-control.txt")
            rows.append(row)
            print(json.dumps({"case": index, "total": len(matrix), "model": model, "effort": effort,
                              "attack": attack, "passed": row["passed"],
                              "failed_checks": [key for key, value in row["checks"].items() if not value]}), flush=True)
            if not row["passed"]:
                raise ProviderError("V3_OFFLINE_PROBE_FAILED")
        if digest(executable.read_bytes()) != PINNED_EXECUTABLE_SHA256 or catalog_path.read_bytes() != catalog_bytes:
            raise ProviderError("PUBLIC_INPUT_CHANGED")
        profile = {"profile_version": CONFIGURABLE_PROFILE_VERSION,
            "instruction_contract": CONFIGURABLE_INSTRUCTION_CONTRACT,
            "instruction_contract_sha256": CONFIGURABLE_INSTRUCTION_CONTRACT_SHA256, "policy_sha256": POLICY_SHA256,
            "executable_sha256": PINNED_EXECUTABLE_SHA256, "catalog_sha256": digest(catalog_bytes), "probes": rows}
        report = {**profile, "status": "PASS", "created_at": datetime.now(UTC).isoformat(),
            "probe_suite_version": PROBE_SUITE_VERSION, "probe_fixture_sha256": helpers.PROBE_FIXTURE_SHA256,
            "case_count": len(rows), "http_capture_count": sum(len(row["captures"]) for row in rows),
            "public_executable_reused": str(executable), "public_catalog_reused": str(catalog_path),
            "catalog_bytes_unchanged": True, "host_authentication_used": False,
            "live_model_calls": 0, "live_inference_tested": False, "deployed": False}
        _write_private(root / "text-models.json", catalog_bytes)
        _write_private(root / "text-profile.json", json_bytes(profile))
        _write_private(root / "verification-report.json", json_bytes(report))
        artifacts = ["text-models.json", "text-profile.json", "verification-report.json", "denied-synthetic-control.txt"]
        for row in rows:
            relative = Path(row["root"]).relative_to(root)
            artifacts.extend(str(relative / name) for name in (
                "result.json", "rpc-writes.json", "mock-http-captures.json", "boundary-check.json",
                "isolation.sb", "config.toml", "models.json", "canary.txt", "workspace/AGENTS.md", "synthetic-stderr.txt"))
        manifest = {"sha256": {name: digest((root / name).read_bytes()) for name in artifacts},
                    "sources": {str(path.relative_to(PROJECT_ROOT)): digest(path.read_bytes()) for path in (
                        Path(__file__), Path(__file__).with_name("configure-codex-text-v3.py"),
                        Path(__file__).with_name("probe-codex-instruction-channels.py"),
                        PROJECT_ROOT / "backend/fund_kb/codex_text.py")}}
        _write_private(root / "artifact-manifest.json", json_bytes(manifest))
        verified = verify_bundle(root)
        _write_private(root / "independent-verification.json", json_bytes(verified))
        print(json.dumps({**verified, "profile_sha256": digest((root / "text-profile.json").read_bytes()),
                          "report_sha256": digest((root / "verification-report.json").read_bytes()),
                          "manifest_sha256": digest((root / "artifact-manifest.json").read_bytes())}, ensure_ascii=False))
        return 0
    except (ProviderError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        code = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
        _write_private(root / "failure-report.json", json_bytes({"status": "FAIL", "error": code,
            "completed_cases": len(rows), "probes": rows, "live_model_calls": 0, "deployed": False}))
        print(json.dumps({"status": "FAIL", "error": code, "candidate_root": str(root)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
