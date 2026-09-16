"""Public release selection stays in the existing isolated control directory."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("dual_profiles", [False, True])
def test_release_launcher_preserves_auth_parent_and_environment(tmp_path, monkeypatch, dual_profiles):
    script = Path(__file__).resolve().parents[2] / "scripts/serve-backend.py"
    spec = importlib.util.spec_from_file_location("release_launcher_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "project"
    (root / "data/universal-v1").mkdir(parents=True)
    (root / "backend").mkdir()
    control = tmp_path / "control"
    control.mkdir()
    profile = {"embedding_model": "synthetic", "embedding_dimensions": 512,
        "embedding_model_path": str(root / "data/model"), "embedding_cache_dir": str(root / "data/cache"),
        "embedding_revision": "test"}
    content = json.dumps(profile)
    (root / "data/retrieval-profile.json").write_text(content)
    registry_path = root / "data/retrieval-profiles.json"
    if dual_profiles:
        registry_path.write_text(json.dumps({"schema_version": 1, "default_profile_id": "synthetic",
            "profiles": [{"id": "synthetic", "name": "Synthetic", "profile_file": "retrieval-profile.json"}]}))
    text_profile = control / "new.json"
    text_profile.write_text('{"synthetic":true}')
    release = {"retrieval_profile_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "codex_text_profile_file": str(text_profile),
        "codex_text_profile_sha256": hashlib.sha256(text_profile.read_bytes()).hexdigest()}
    record = root / "data/universal-v1/active-release.json"
    record.write_text(json.dumps(release))
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module.os, "environ", {"FKB_CODEX_TEXT_PROFILE_FILE": str(control / "old.json"),
                                              "SYNTHETIC_KEEP": "yes"})
    captured = []
    monkeypatch.setattr(module.os, "execve", lambda executable, args, env: captured.append(dict(env)))
    monkeypatch.chdir(root)
    module.main()
    assert captured[-1]["FKB_CODEX_TEXT_PROFILE_FILE"] == str(text_profile)
    assert captured[-1]["SYNTHETIC_KEEP"] == "yes"
    assert captured[-1].get("FKB_RETRIEVAL_PROFILES_FILE") == (str(registry_path) if dual_profiles else None)
    assert captured[-1]["FKB_EMBEDDING_MODEL"] == "synthetic"
    # A hash-valid public artifact in the business data directory still cannot
    # become an auth-control profile; don't relax codex_host's directory gate.
    other = root / "data/universal-v1/wrong.json"
    other.write_bytes(text_profile.read_bytes())
    record.write_text(json.dumps({**release, "codex_text_profile_file": str(other)}))
    with pytest.raises(RuntimeError, match="VERIFIED_TEXT_PROFILE_MISMATCH"):
        module.main()
    assert len(captured) == 1
