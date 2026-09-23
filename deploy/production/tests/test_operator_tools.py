"""Optional actual CLI parsers; never start containers or contact production."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def tool(name):
    executable = os.environ.get(name)
    if not executable or not Path(executable).is_file():
        pytest.skip(name + " not supplied; no automatic tool downloads")
    return executable


def test_real_compose_resolves_project_and_keeps_services_private(tmp_path):
    executable = tool("FKB_TEST_COMPOSE_BIN")
    (tmp_path / "broker.env").write_text("RABBITMQ_DEFAULT_PASS=synthetic$literal\n")
    (tmp_path / "qdrant.env").write_text("QDRANT__SERVICE__API_KEY=synthetic-only\n")
    environment = dict(os.environ, FKB_CONFIG_DIR=str(tmp_path), COMPOSE_DISABLE_ENV_FILE="1")
    result = subprocess.run([executable, "--project-directory", str(ROOT), "--env-file",
                             str(ROOT / "deploy/production/images.env"), "-f",
                             str(ROOT / "deploy/production/compose.yaml"), "config", "--format", "json"],
                            capture_output=True, text=True, env=environment, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert services["api"]["build"]["context"] == str(ROOT)
    assert services["web"]["build"]["context"] == str(ROOT)
    # Canonical Compose config escapes '$' as '$$' so re-reading the rendered
    # configuration does not interpolate it. The original suffix must survive.
    assert services["broker"]["environment"]["RABBITMQ_DEFAULT_PASS"] == "synthetic$$literal"
    assert all("ports" not in value for name, value in services.items() if name != "web")


def test_actual_powershell_parser():
    executable = tool("FKB_TEST_PWSH_BIN")
    path = ROOT / "deploy/production/Deploy.ps1"
    # Path is a known repository fixture, not untrusted command text.
    command = ('$tokens=$null; $errors=$null; '
               '[System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$tokens,[ref]$errors) | Out-Null; '
               'if ($errors.Count -gt 0) {$errors | ForEach-Object {$_.Message}; exit 1}')
    script = 'param([string]$Source)\n' + command.replace('$args[0]', '$Source')
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", "& { " + script + " }", str(path)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("tamper", [False, True])
def test_actual_powershell_verify_detects_tampering(tmp_path, tamper):
    executable = tool("FKB_TEST_PWSH_BIN")
    script = tmp_path / "Deploy.ps1"
    shutil.copy2(ROOT / "deploy/production/Deploy.ps1", script)
    (tmp_path / "SHA256SUMS").write_text(hashlib.sha256(script.read_bytes()).hexdigest() + "  Deploy.ps1\n")
    if tamper:
        script.write_text(script.read_text() + "\n# changed after manifest\n")
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-File", str(script), "-Action", "Verify"],
                            capture_output=True, text=True, timeout=30, check=False)
    assert (result.returncode == 0) is not tamper


def test_actual_powershell_manifest_path_escape_rejected(tmp_path):
    executable = tool("FKB_TEST_PWSH_BIN")
    script = tmp_path / "Deploy.ps1"
    shutil.copy2(ROOT / "deploy/production/Deploy.ps1", script)
    (tmp_path / "SHA256SUMS").write_text("0" * 64 + "  ../outside\n")
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-File", str(script), "-Action", "Verify"],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode != 0
    assert "Unsafe checksum path" in result.stderr


@pytest.fixture
def operator(tmp_path):
    executable = tool("FKB_TEST_PWSH_BIN")
    bundle = tmp_path / "bundle with spaces"
    bundle.mkdir()
    script = bundle / "Deploy.ps1"
    shutil.copy2(ROOT / "deploy/production/Deploy.ps1", script)
    (bundle / "SHA256SUMS").write_text(hashlib.sha256(script.read_bytes()).hexdigest() + "  Deploy.ps1\n")
    config = tmp_path / "protected configuration"
    (config / "tls").mkdir(parents=True)
    for name in ("broker.env", "qdrant.env", "tls/fullchain.pem", "tls/privkey.pem"):
        (config / name).write_text("SYNTHETIC_ONLY")
    (config / "settings.json").write_text(json.dumps({"site_url": "https://kb.example.invalid"}))
    trace = tmp_path / "calls.jsonl"
    def run(action, *arguments):
        if trace.exists():
            trace.unlink()
        result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-File",
                                 str(ROOT / "deploy/production/tests/mock-docker.ps1"), "-Installer", str(script),
                                 "-Action", action, "-ConfigDir", str(config), "-CapturePath", str(trace), *arguments],
                                env=dict(os.environ, FKB_TEST_DOCKER_STUB="1"),
                                capture_output=True, text=True, timeout=30, check=False)
        calls = [json.loads(line) for line in trace.read_text(encoding="utf-8-sig").splitlines()] if trace.exists() else []
        return result, calls
    return run, tmp_path


@pytest.mark.parametrize("action", ["Build", "Check", "Services", "Doctor", "Start", "Status", "Stop"])
def test_powershell_operator_actions_synthetic_cli(operator, action):
    run, _ = operator
    result, calls = run(action)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls and all("--project-name" in call for call in calls if call[0] == "compose" and "version" not in call)
    if action == "Stop":
        assert any(call[-4:] == ["stop", "web", "api", "worker"] for call in calls)
    if action == "Start":
        assert any(call[-1] == "ready" for call in calls)
        assert any("--no-build" in call and "never" in call for call in calls)


def test_powershell_migration_ack_synthetic_cli(operator):
    run, _ = operator
    denied, calls = run("Migrate")
    assert denied.returncode != 0
    assert not any("migrate" in call for call in calls)
    approved, calls = run("Migrate", "-Ack")
    assert approved.returncode == 0, approved.stdout + approved.stderr
    assert any(call[-2:] == ["migrate", "--ack-empty-or-backed-up-schema"] for call in calls)


def test_powershell_image_export_load_control_flow_synthetic_cli(operator):
    run, temp = operator
    images = temp / "image handoff"
    exported, _ = run("ExportImages", "-ImageDirectory", str(images))
    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert (images / "offline.compose.yaml").is_file()
    records = json.loads((images / "image-manifest.json").read_text())
    assert len(records) == 5 and len({row["id"] for row in records}) == 5
    loaded, calls = run("LoadImages", "-ImageDirectory", str(images))
    assert loaded.returncode == 0, loaded.stdout + loaded.stderr
    assert len([call for call in calls if call[0] == "image"]) == 5
    started, calls = run("Start", "-ImageDirectory", str(images))
    assert started.returncode == 0, started.stdout + started.stderr
    assert all(str(images / "offline.compose.yaml") in call
               for call in calls if call[0] == "compose" and "version" not in call)
