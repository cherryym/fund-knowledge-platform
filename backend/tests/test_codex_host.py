"""Host opt-in/config regression. All credentials are synthetic."""
import io
import json
import queue
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from fund_kb.codex_bridge import SAFE_CONFIG, CodexBridge, _write_private
from fund_kb.codex_host import configured_bridge
from fund_kb.providers import ProviderError
from fund_kb.settings import Settings


def manifest(tmp_path):
    tmp_path.chmod(0o700)
    data = {"enabled": True, "auth_runtime_verified": True, "browser_callback_reachable": False,
        "expected_version": "codex-cli 0.153.0", "executable": str(Path(__file__).resolve()),
        "executable_sha256": "a" * 64, "runtime_root": str(tmp_path / "runtimes"),
        "archive_root": str(tmp_path / "archives"), "master_key_file": str(tmp_path / "archive.key")}
    _write_private(tmp_path / "archive.key", Fernet.generate_key())
    return tmp_path / "host.json", data


def test_default_settings_do_not_probe_credentials_or_start_bridge(monkeypatch):
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("unexpected read"))
    assert configured_bridge(Settings(app_env="test")) is None


def test_host_wires_only_explicit_private_new_store(tmp_path, monkeypatch):
    path, data = manifest(tmp_path)
    _write_private(path, json.dumps(data).encode())
    monkeypatch.setattr(CodexBridge, "unavailable_reason", property(lambda self: None))
    bridge = configured_bridge(Settings(codex_bridge_config_file=path))
    assert bridge.store.root == tmp_path / "runtimes"
    assert bridge.sessions == {}  # host setup never starts login
    assert not bridge.config.browser_callback_reachable
    bridge.close()


@pytest.mark.parametrize("change", ["unknown", "host_key", "permissions", "false_attestation", "bool_string"])
def test_host_rejects_unsafe_manifest_before_using_credentials(tmp_path, change):
    path, data = manifest(tmp_path)
    if change == "unknown": data["inference_enabled"] = True
    if change == "host_key": data["master_key_file"] = "/not-this-project/auth.json"
    if change == "false_attestation": data["auth_runtime_verified"] = False
    if change == "bool_string": data["enabled"] = "true"
    _write_private(path, json.dumps(data).encode())
    if change == "permissions": path.chmod(0o644)
    with pytest.raises(ProviderError):
        configured_bridge(Settings(codex_bridge_config_file=path))
    assert not (tmp_path / "runtimes").exists()


def test_config_does_not_emit_unsupported_tool_table():
    assert "[tools]" not in SAFE_CONFIG
    assert 'web_search = "disabled"' in SAFE_CONFIG
    assert 'shell_tool = false' in SAFE_CONFIG
    assert 'forced_login_method = "chatgpt"' in SAFE_CONFIG


def test_exited_process_wakes_pending_rpc_without_waiting_for_timeout():
    from fund_kb.codex_bridge import AuthStdioTransport
    from fund_kb.codex_bridge_config import CodexBridgeConfig

    class ExitedProcess:
        stdin = io.BytesIO()
        stdout = io.BytesIO()

        def poll(self):
            return 1

    transport = object.__new__(AuthStdioTransport)
    transport.config = CodexBridgeConfig()
    transport.process = ExitedProcess()
    transport.responses, transport.events = queue.Queue(), queue.Queue()
    transport.lock, transport.failed, transport.counter = threading.Lock(), False, 0
    transport._read()
    assert transport.failed
    assert transport.responses.get_nowait() == {"transport_closed": True}
