"""Offline local Responses stand-in: inspect tools sent by the exact official binary.

Fresh empty identity; no real credentials; synthetic prompt; localhost only.
Does not execute any model-produced tools or enable the application's inference.
"""
import argparse
import hashlib
import json
import os
import selectors
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from fund_kb.codex_bridge import _private_directory, _write_private
from fund_kb.codex_text import POLICY_SHA256, TEXT_CONFIG, restricted_catalog

parser = argparse.ArgumentParser()
parser.add_argument("--executable", required=True)
parser.add_argument("--catalog")
parser.add_argument("--model", default="gpt-5.4-mini")
parser.add_argument("--attack", choices=["apply_patch", "view_image", "exec_command"])
args = parser.parse_args()
captures = []
canary = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw)
        captures.append({"path": self.path, "tools": body.get("tools"), "tool_choice": body.get("tool_choice")})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        msg = {"id":"msg_probe","type":"message","role":"assistant","status":"completed",
            "content":[{"type":"output_text","text":"{\"ok\":true}","annotations":[]}]}
        if args.attack and len(captures) == 1:
            msg = {"id":"call_probe", "call_id":"call_probe", "name":args.attack,
                "type":"custom_tool_call" if args.attack == "apply_patch" else "function_call",
                "status":"completed"}
            if args.attack == "apply_patch":
                msg['input'] = f'*** Begin Patch\n*** Update File: {canary}\n@@\n-UNCHANGED\n+CHANGED\n*** End Patch\n'
            else:
                msg['arguments'] = json.dumps({'path':str(canary)} if args.attack == 'view_image' else
                    {'cmd':f'printf CHANGED > {canary}'})
        events = [{"type":"response.created","response":{"id":"resp_probe","status":"in_progress","output":[]}},
            {"type":"response.output_item.added","output_index":0,"item":{**msg,"status":"in_progress","content":[]}},
            {"type":"response.output_text.delta","item_id":"msg_probe","output_index":0,"content_index":0,"delta":"{\"ok\":true}"},
            {"type":"response.output_item.done","output_index":0,"item":msg},
            {"type":"response.completed","response":{"id":"resp_probe","status":"completed","output":[msg],
                "usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}]
        if msg['type'] != 'message':
            events = [event for event in events if event['type'] != 'response.output_text.delta']
        for event in events:
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
        self.wfile.flush()


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
root = Path(tempfile.mkdtemp(prefix="fkb-text-surface-", dir="/private/tmp"))
canary = root / 'canary.txt'
_write_private(canary, b'UNCHANGED\n')
for name in ("workspace", "tmp"):
    _private_directory(root / name, create=True)
catalog_config = ''
if args.catalog:
    catalog = json.loads(Path(args.catalog).read_text())
    catalog = restricted_catalog(catalog['models'])
    _write_private(root / 'models.json', json.dumps(catalog).encode())
    catalog_config = 'model_catalog_json = ' + json.dumps(str(root / 'models.json')) + '\n'
config = (f'model = "{args.model}"\nmodel_provider = "offline_probe"\n' +
    TEXT_CONFIG.replace('forced_login_method = "chatgpt"\n', '') +
    '\n[model_providers.offline_probe]\nname = "Offline synthetic probe"\n' +
    f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n' +
    'wire_api = "responses"\nrequires_openai_auth = false\nrequest_max_retries = 0\nstream_max_retries = 0\n')
_write_private(root / "config.toml", config.encode())
p = subprocess.Popen([args.executable, "app-server", "--strict-config", "--listen", "stdio://"],
    cwd=root / "workspace", env={"PATH":"/usr/bin:/bin", "CODEX_HOME":str(root), "TMPDIR":str(root / "tmp"), "LANG":"en_US.UTF-8"},
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def send(value):
    p.stdin.write((json.dumps(value) + "\n").encode())
    p.stdin.flush()


send({"id":1,"method":"initialize","params":{"clientInfo":{"name":"fund_kb_offline_probe","version":"1.0"}}})
selector = selectors.DefaultSelector()
selector.register(p.stdout, selectors.EVENT_READ, "stdout")
selector.register(p.stderr, selectors.EVENT_READ, "stderr")
buffers = {"stdout": b"", "stderr": b""}
end = time.monotonic() + 35
completed = False
while time.monotonic() < end and p.poll() is None and not completed:
    for key, _ in selector.select(1):
        data = os.read(key.fileobj.fileno(), 65536)
        if not data:
            selector.unregister(key.fileobj)
            continue
        buffers[key.data] += data
        if key.data == "stderr":
            continue
        while b"\n" in buffers["stdout"]:
            line, buffers["stdout"] = buffers["stdout"].split(b"\n", 1)
            event = json.loads(line)
            if "error" in event:
                print({"rpc_error":event["error"]})
                completed = True
            if event.get("id") == 1 and "result" in event:
                send({"method":"initialized","params":{}})
                send({"id":2,"method":"thread/start","params":{"model":args.model, "modelProvider":"offline_probe",
                    "cwd":str(root / "workspace"),"ephemeral":True,"approvalPolicy":"never","sandbox":"read-only"}})
            if event.get("id") == 2 and "result" in event:
                send({"id":3,"method":"turn/start","params":{"threadId":event["result"]["thread"]["id"],
                    "input":[{"type":"text","text":"Return JSON ok true. No tools."}]}})
            if event.get("method") == "turn/completed":
                completed = True
p.terminate()
try:
    p.wait(3)
except subprocess.TimeoutExpired:
    p.kill()
    p.wait()
server.shutdown()
print(json.dumps({"root":str(root),"completed":completed,"captures":captures,
    "canary_unchanged":canary.read_bytes() == b'UNCHANGED\n',
    "policy_sha256":POLICY_SHA256, "catalog_sha256":hashlib.sha256((root/'models.json').read_bytes()).hexdigest(),
    "executable_sha256":hashlib.sha256(Path(args.executable).read_bytes()).hexdigest(),
    "startup_errors":buffers["stderr"].decode(errors="replace")[:2000]}, ensure_ascii=False))
