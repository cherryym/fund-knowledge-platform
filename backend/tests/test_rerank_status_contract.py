"""Real temporary HTTP endpoint to frontend contract; never load model weights."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from fund_kb import vector_indexing
from test_dual_retrieval_api import applications, env, synthetic_backend  # noqa: F401


@pytest.mark.parametrize("state", ["configured", "loaded", "disabled"])
def test_real_http_preserves_selected_reranker_without_loading_it(env, monkeypatch, state):
    from fund_kb import local_encoders
    def forbidden(*args, **kwargs):
        raise AssertionError("Status GET must not load or invoke a reranker")
    monkeypatch.setattr(local_encoders, "create_reranker", forbidden)
    for ident in env.registry.enabled_ids():
        runtime = env.registry.resolve(ident)
        name = "Qwen/Qwen3-Reranker-4B" if ident == "qwen3-4b" else "BAAI/bge-reranker-v2-m3"
        for settings in (runtime.settings, runtime.vector.settings):
            monkeypatch.setattr(settings, "reranker_mode", "disabled" if state == "disabled" else "local")
            monkeypatch.setattr(settings, "reranker_model", name)
            monkeypatch.setattr(settings, "reranker_revision", "synthetic-pinned-revision")
        marker = SimpleNamespace(_model=object(), close=lambda: None) if state == "loaded" else None
        monkeypatch.setattr(runtime.vector, "_reranker", marker)
        response = env.call("GET", f"/retrieval/status?space_id={env.space}&profile_id={ident}")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        expected = {"mode": "disabled" if state == "disabled" else "local", "model": name,
            "revision": "synthetic-pinned-revision", "loaded": state == "loaded"}
        assert response.json()["vector"]["reranking"] == expected
        assert runtime.vector._reranker is marker
        directory = os.environ.get("RERANK_STATUS_CONTRACT_DIR")
        if directory and ident == "qwen3-4b":
            # Explicit test-only artifact for React rendering of the ACTUAL
            # backend response, not a handwritten mock of the intended fields.
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            with (target / (state + ".json")).open("x") as handle:
                json.dump(response.json(), handle, ensure_ascii=False, indent=2)


def test_http_nested_projection_drops_paths_secrets_and_unknown_fields(env, monkeypatch):
    runtime = env.registry.resolve("qwen3-4b")
    original = runtime.vector.status()
    monkeypatch.setattr(runtime.vector, "status", lambda: {**original, "reranking": {
        "mode": "local", "model": "Qwen/Qwen3-Reranker-4B", "revision": "pin", "loaded": False,
        "model_path": "/DO_NOT_EXPOSE/runtime", "api_key": "DO_NOT_EXPOSE", "diagnostic": {"raw": "DO_NOT_EXPOSE"}}})
    response = env.call("GET", f"/retrieval/status?space_id={env.space}&profile_id=qwen3-4b")
    assert response.status_code == 200 and "DO_NOT_EXPOSE" not in response.text
    assert set(response.json()["vector"]["reranking"]) == {"mode", "model", "revision", "loaded"}


@pytest.mark.parametrize("invalid", [None, "true", 1, 0, [], {}])
def test_missing_or_malformed_load_state_is_unknown_not_false(invalid):
    result = vector_indexing._public_reranking({"reranking": {"mode": "local", "model": "test", "loaded": invalid}})
    assert "loaded" not in result["reranking"]


@pytest.mark.parametrize("raw", [None, False, [], "local", {}, {"loaded": "false"}])
def test_legacy_status_does_not_invent_disabled_or_loaded(raw):
    assert vector_indexing._public_reranking({"reranking": raw}) == {}
