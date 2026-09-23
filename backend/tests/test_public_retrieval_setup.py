"""Public setup creates only fresh non-secret profiles, without loading models."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def setup_script():
    path = Path(__file__).resolve().parents[2] / "scripts/configure-retrieval.py"
    spec = importlib.util.spec_from_file_location("public_retrieval_setup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("device", ["cpu", "mps", "auto"])
def test_profiles_are_separate_valid_and_preserve_semantic_settings(setup_script, tmp_path, device):
    values = setup_script.profiles(tmp_path, device, "qwen3-4b")
    qwen = values["retrieval-profiles/qwen3-4b.json"]
    bge = values["retrieval-profiles/bge-m3.json"]
    assert qwen["embedding_dimensions"] == 2560 and bge["embedding_dimensions"] == 1024
    assert qwen["embedding_model"] != bge["embedding_model"]
    assert values["retrieval-profile.json"] == qwen
    assert qwen["embedding_allow_downloads"] is False
    assert qwen["embedding_query_instruction"] and qwen["retrieval_strategy"] == "unit_rerank"
    assert qwen["embedding_chunk_strategy"] == "semantic_sections_v3"
    assert qwen["embedding_dtype"] == ("float32" if device == "cpu" else "bfloat16")
    assert qwen["reranker_model"] == bge["reranker_model"] == "Qwen/Qwen3-Reranker-4B"
    assert qwen["reranker_dtype"] == ("float32" if device == "cpu" else "bfloat16")
    assert qwen["reranker_max_tokens"] == 2048


def test_explicit_legacy_reranker_remains_available(setup_script, tmp_path):
    values = setup_script.profiles(tmp_path, "cpu", "qwen3-4b", "bge-m3")
    assert values["retrieval-profile.json"]["reranker_model"] == "BAAI/bge-reranker-v2-m3"


def test_default_plan_writes_nothing(setup_script, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(setup_script, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["configure-retrieval.py"])
    setup_script.main()
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "plan" and report["models_downloaded"] is False
    assert not (tmp_path / "data").exists()


def test_new_profiles_are_written_but_never_overwritten(setup_script, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(setup_script, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["configure-retrieval.py", "--write"])
    setup_script.main()
    capsys.readouterr()
    originals = {p: p.read_bytes() for p in (tmp_path / "data").rglob("*.json")}
    assert len(originals) == 4
    with pytest.raises(RuntimeError, match="PROFILE_ALREADY_EXISTS"):
        setup_script.main()
    assert all(p.read_bytes() == raw for p, raw in originals.items())


def test_symlink_data_root_is_not_followed(setup_script, monkeypatch, tmp_path):
    other = tmp_path / "other"; other.mkdir()
    (tmp_path / "data").symlink_to(other, target_is_directory=True)
    monkeypatch.setattr(setup_script, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["configure-retrieval.py", "--write"])
    with pytest.raises(RuntimeError, match="DATA_SYMLINK_FORBIDDEN"):
        setup_script.main()
    assert list(other.iterdir()) == []
