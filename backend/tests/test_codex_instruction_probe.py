"""Synthetic wire-capture and candidate-profile tests; never launches Codex."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from fund_kb.codex_text import (
    INSTRUCTION_CONTRACT_SHA256,
    POLICY_SHA256,
    PROFILE_VERSION,
    CodexTextEngine,
    check_text_request_budget,
    restricted_catalog,
    text_request_params,
    text_request_sizes,
)


@pytest.fixture
def probe():
    path = Path(__file__).resolve().parents[2] / "scripts" / "probe-codex-instruction-channels.py"
    spec = importlib.util.spec_from_file_location("instruction_probe_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wire_fixture(probe, base_role="developer"):
    thread, turn = text_request_params(probe.PROBE_MESSAGES, "synthetic", "/empty", "test-id")
    body = {"input": [
        {"role":base_role, "content":[{"type":"input_text", "text":thread["baseInstructions"]}]},
        {"role":"developer", "content":[{"type":"input_text", "text":thread["developerInstructions"]}]},
        {"role":"user", "content":[{"type":"input_text", "text":turn["input"][0]["text"]}]},
    ], "tools": []}
    return body, thread, turn


def test_51544_utf8_case_with_long_system_and_four_sources_exceeds_only_the_old_wrong_sum(probe):
    assert len(json.dumps(probe.PROBE_MESSAGES, ensure_ascii=False).encode()) == 51544
    assert len(probe.LONG_SYSTEM) > 8712
    _, thread, turn = wire_fixture(probe)
    sizes = text_request_sizes(thread, turn)
    assert sizes["semantic_utf8_bytes"] < 65536 < sizes["rpc_total_bytes"]
    assert sizes["thread_rpc_bytes"] < 262144 and sizes["turn_rpc_bytes"] < 262144
    assert check_text_request_budget(thread, turn) == sizes["rpc_total_bytes"]
    for index in range(1, 5):
        assert f"SYNTHETIC_PRIMARY_BLOCK_{index}" in turn["input"][0]["text"]
        assert f"SOURCE_{index}_END" in turn["input"][0]["text"]
        assert f"SOURCE_{index}_END" not in thread["baseInstructions"]
        assert f"SOURCE_{index}_END" not in thread["developerInstructions"]


@pytest.mark.parametrize("base_role", ["developer", "system", "instructions"])
def test_capture_accepts_only_full_native_privileged_channels(probe, base_role):
    body, thread, turn = wire_fixture(probe, "system" if base_role == "instructions" else base_role)
    if base_role == "instructions":
        body["instructions"] = body["input"].pop(0)["content"][0]["text"]
    result = probe.inspect_capture(body, thread, turn, "unsent-canary")
    assert result["instruction_channels_verified"]
    assert result["base_channel"] == base_role
    assert result["base_sha256"] == probe.digest(thread["baseInstructions"])
    long = result["long_system"]
    assert long["expected_chars"] > 8712
    assert long["expected_cjk_chars"] > 8000
    assert long["observed_chars"] == long["expected_chars"]
    assert long["observed_utf8_bytes"] == long["expected_utf8_bytes"]
    assert long["observed_sha256"] == long["expected_sha256"]
    assert long["full_text_equal"] and long["tail_marker_verified"]


@pytest.mark.parametrize("fault", ["head_tail_only", "middle_changed", "paragraph_missing", "tail_missing"])
def test_long_system_verification_requires_every_character_not_only_markers(probe, fault):
    body, thread, turn = wire_fixture(probe)
    observed = body["input"][0]["content"][0]["text"]
    if fault == "head_tail_only":
        observed = probe.LONG_SYSTEM_HEAD + "\n" + probe.LONG_SYSTEM_TAIL
    elif fault == "middle_changed":
        observed = observed.replace("第0115段", "第9999段")
    elif fault == "paragraph_missing":
        observed = "\n".join(line for line in observed.split("\n") if "第0115段" not in line)
    else:
        observed = observed.replace(probe.LONG_SYSTEM_TAIL, "")
    body["input"][0]["content"][0]["text"] = observed
    result = probe.inspect_capture(body, thread, turn, "unsent-canary")
    assert not result["instruction_channels_verified"]
    assert not result["long_system"]["full_text_equal"]
    assert result["long_system"]["observed_sha256"] != result["long_system"]["expected_sha256"]
    if fault != "tail_missing":
        assert result["long_system"]["tail_marker_verified"]


@pytest.mark.parametrize("fault", ["base_tail", "developer_tail", "user_tail", "base_demoted", "developer_demoted",
                                   "user_promoted", "canary_leak", "quoted_roles_only"])
def test_wire_verifier_catches_demotion_truncation_promotion_and_file_leak(probe, fault):
    body, thread, turn = wire_fixture(probe)
    if fault in {"base_tail", "developer_tail", "user_tail"}:
        index = {"base_tail":0, "developer_tail":1, "user_tail":2}[fault]
        body["input"][index]["content"][0]["text"] = body["input"][index]["content"][0]["text"][:-1]
    elif fault in {"base_demoted", "developer_demoted"}:
        body["input"][0 if fault == "base_demoted" else 1]["role"] = "user"
    elif fault == "user_promoted":
        body["input"][2]["role"] = "developer"
    elif fault == "canary_leak":
        body["input"].append({"role":"user", "content":"unsent-canary"})
    else:
        body["input"] = [{"role":"user", "content":json.dumps(body["input"])}]
    assert not probe.inspect_capture(body, thread, turn, "unsent-canary")["instruction_channels_verified"]


def test_probe_refuses_unpinned_binary_before_starting_server(probe, tmp_path, monkeypatch):
    executable = tmp_path / "not-codex"
    executable.write_text("synthetic")
    monkeypatch.setattr(probe, "ThreadingHTTPServer", lambda *a, **k: pytest.fail("listener started"))
    with pytest.raises(probe.ProviderError, match="CODEX_VERSION_UNSUPPORTED"):
        probe.run_probe(executable, b'{"models":[]}', "synthetic")


def test_probe_requires_os_network_isolation(probe, monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "unsupported")
    with pytest.raises(probe.ProviderError, match="OFFLINE_NETWORK_SANDBOX_REQUIRED"):
        probe.sandbox_command(Path("/binary"), 12345)


def test_profile_generation_refuses_existing_destination_without_reading_catalog(probe, tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    sentinel = active / "text-profile.json"
    sentinel.write_bytes(b"old-profile-never-read-or-overwritten")
    with pytest.raises(SystemExit) as error:
        probe.main(["--executable", "/unused", "--catalog", "/must-not-read",
                    "--models", "synthetic", "--profile-root", str(active)])
    assert error.value.code == 2
    assert sentinel.read_bytes() == b"old-profile-never-read-or-overwritten"


def test_new_candidate_requires_complete_matrix_and_loads_through_real_gate(probe, tmp_path, monkeypatch):
    catalog = tmp_path / "synthetic-catalog.json"
    catalog.write_text(json.dumps(restricted_catalog([{"slug":"one"}, {"slug":"two"}])))
    calls = []

    def fake_probe(executable, catalog_bytes, model, attack=None):
        calls.append((model, attack))
        return {"model":model, "attack":attack, "passed":True, "profile_version":PROFILE_VERSION,
                "instruction_contract_sha256":INSTRUCTION_CONTRACT_SHA256, "policy_sha256":POLICY_SHA256,
                "executable_sha256":probe.PINNED_EXECUTABLE_SHA256, "catalog_sha256":probe.digest(catalog_bytes),
                "completed":True, "canary_unchanged":True, "instruction_channels_verified":True,
                "request_rpc_bytes":123, "messages_utf8_bytes":100, "root":"/synthetic-only",
                "request_sizes":{}, "budget_layers_verified":True,
                "captures":[{"tools":[], "instruction_channels_verified":True}],
                "startup_errors":"unsupported call: " + str(attack)}

    monkeypatch.setattr(probe, "run_probe", fake_probe)
    candidate = tmp_path / "new-candidate"
    assert probe.main(["--executable", "/unused", "--catalog", str(catalog), "--models", "one,two",
                       "--profile-root", str(candidate)]) == 0
    assert calls == [("one", None), ("two", None), ("one", "apply_patch"), ("one", "view_image"), ("one", "exec_command")]
    profile = json.loads((candidate / "text-profile.json").read_text())
    assert profile["profile_version"] == PROFILE_VERSION
    assert len(profile["probes"]) == 5
    assert profile["probe_suite_version"] == probe.PROBE_SUITE_VERSION
    report = json.loads((candidate / "verification-report.json").read_text())
    assert report["instruction_contract"] == probe.INSTRUCTION_CONTRACT
    assert len(report["cases"]) == 5
    assert (candidate.stat().st_mode & 0o777) == 0o700
    adapter = CodexTextEngine(probe.SimpleNamespace(config=probe.SimpleNamespace(
        executable_sha256=probe.PINNED_EXECUTABLE_SHA256)), candidate / "text-profile.json")
    assert adapter.models == {"one", "two"}
    failed = copy.deepcopy(profile["probes"][0])
    failed["passed"] = False
    monkeypatch.setattr(probe, "run_probe", lambda *a, **k: failed)
    rejected = tmp_path / "not-generated"
    assert probe.main(["--executable", "/unused", "--catalog", str(catalog), "--models", "one,two",
                       "--profile-root", str(rejected)]) == 1
    assert not rejected.exists()
