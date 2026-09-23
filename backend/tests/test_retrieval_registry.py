"""Real registry/Qdrant with synthetic embeddings and temporary storage only."""
from __future__ import annotations

import hashlib
import json
import math
import os
import socket
from concurrent.futures import ThreadPoolExecutor

import pytest

from fund_kb import retrieval
from fund_kb.retrieval_registry import RetrievalProfileError, RetrievalRegistry, registry_for
from fund_kb.settings import Settings


@pytest.fixture(autouse=True)
def synthetic_backend(monkeypatch):
    from fund_kb import codex_bridge, codex_text, providers

    for name in tuple(os.environ):
        if name.startswith("FKB_"):
            monkeypatch.delenv(name)

    def forbidden(*args, **kwargs):
        pytest.fail("Network, private credentials and real model loading/generation are forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(providers, "complete", forbidden)
    monkeypatch.setattr(codex_bridge, "_read_private", forbidden)
    monkeypatch.setattr(codex_text, "_read_private", forbidden)
    monkeypatch.setattr(retrieval.EmbeddingProvider, "_local_transformer", forbidden)

    def embed(provider, texts, *, query=False):
        vectors = []
        for text in texts:
            values = [0.0] * provider.dimension
            for char in text or " ":
                index = int.from_bytes(hashlib.sha256(char.encode()).digest()[:4], "big") % len(values)
                values[index] += 1.0
            norm = math.sqrt(sum(value * value for value in values))
            vectors.append([value / norm for value in values])
        return vectors

    monkeypatch.setattr(retrieval.EmbeddingProvider, "embed", embed)
    monkeypatch.setattr(retrieval.EmbeddingProvider, "token_count", lambda self, text: len(text.encode()) + 2)
    monkeypatch.setattr(retrieval.EmbeddingProvider, "query_token_count", lambda self, text: len(text.encode()) + 2)
    original = retrieval.VectorIndex
    created = []

    class TrackedVector(original):
        def __init__(self, settings):
            assert settings.qdrant_url is None and settings.embedding_allow_downloads is False
            super().__init__(settings)
            self.close_calls = 0
            created.append(self)

        def close(self):
            self.close_calls += 1
            super().close()

    monkeypatch.setattr(retrieval, "VectorIndex", TrackedVector)
    yield created
    for vector in created:
        original.close(vector)


def make_settings(root, *, default_id="qwen3-4b", profile_updates=None, **updates):
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for ident, model, dimensions in (("qwen3-4b", "Qwen/Qwen3-Embedding-4B", 2560),
                                      ("bge-m3", "BAAI/bge-m3", 1024)):
        values = {"schema_version": 1, "embedding_mode": "transformers", "embedding_model": model,
            "embedding_dimensions": dimensions, "embedding_revision": "synthetic-fixed-revision",
            "embedding_model_path": str(root / ident / "model"), "embedding_cache_dir": str(root / "cache"),
            "embedding_chunk_strategy": "semantic_sections_v3", "embedding_max_tokens": 768,
            "embedding_model_max_tokens": 8192, "embedding_device": "cpu",
            "embedding_dtype": "bfloat16" if ident == "qwen3-4b" else "float32"}
        values.update(profile_updates or {})
        (root / f"{ident}.json").write_text(json.dumps(values), encoding="utf-8")
        entries.append({"id": ident, "name": f"Synthetic {ident}", "profile_file": f"{ident}.json"})
    manifest = root / "retrieval-profiles.json"
    manifest.write_text(json.dumps({"schema_version": 1, "default_profile_id": default_id,
                                   "profiles": entries}), encoding="utf-8")
    return Settings(**{"app_env": "test", "retrieval_warmup_mode": "disabled", "storage_dir": root, "database_url": f"sqlite:///{root / 'test.sqlite'}",
        "qdrant_path": root / "vectors", "retrieval_profiles_file": manifest,
        "retrieval_profile": root / f"{default_id}.json", "allowed_origins": ["http://testserver"],
        "auth_mode": "demo", "job_workers": 1, "llm_provider": "evidence", **updates})


@pytest.fixture
def configured(tmp_path, synthetic_backend):
    return make_settings(tmp_path / "data")


def test_default_explicit_profile_namespaces_and_concurrent_resolve(configured, synthetic_backend):
    registry = RetrievalRegistry(configured)
    assert registry.default_id == "qwen3-4b"
    assert registry.enabled_ids() == ("qwen3-4b", "bge-m3")
    with ThreadPoolExecutor(max_workers=4) as pool:
        runtimes = list(pool.map(registry.resolve, [None, "bge-m3"] * 8))
    qwen, bge = runtimes[:2]
    assert all(runtime is (qwen if index % 2 == 0 else bge) for index, runtime in enumerate(runtimes))
    assert len(synthetic_backend) == 2
    assert qwen.vector.collection != bge.vector.collection
    assert qwen.vector._shared is bge.vector._shared  # one local store, two actual collections
    qwen.vector.prepare_collection()
    assert not bge.vector.status()["collection_exists"]
    bge.vector.prepare_collection()
    assert qwen.vector.status()["embedding_dimensions"] == 2560
    assert bge.vector.status()["embedding_dimensions"] == 1024
    assert registry.freeze({"profile_id": "bge-m3"}) == bge.selection()
    assert configured.embedding_model == "Qwen/Qwen3-Embedding-4B"
    registry.close()
    registry.close()
    assert qwen.vector.close_calls == bge.vector.close_calls == 1
    with pytest.raises(RetrievalProfileError, match="RETRIEVAL_REGISTRY_CLOSED"):
        registry.resolve()


@pytest.mark.parametrize("fingerprint", ["0" * 64, "secret/path", 4])
def test_stale_fingerprint_rejected_before_opening_vector(configured, synthetic_backend, fingerprint):
    registry = RetrievalRegistry(configured)
    with pytest.raises(RetrievalProfileError, match="RETRIEVAL_PROFILE_CHANGED"):
        registry.freeze({"profile_id": "qwen3-4b", "fingerprint": fingerprint})
    assert not synthetic_backend


@pytest.mark.parametrize("selection,code", [
    ({"profile_id": "missing"}, "RETRIEVAL_PROFILE_NOT_FOUND"),
    ({"profile_id": "bge-m3", "dimensions": 2560}, "RETRIEVAL_PROFILE_CHANGED"),
    ({"profile_id": "bge-m3", "model": "Qwen/Qwen3-Embedding-4B"}, "RETRIEVAL_PROFILE_CHANGED"),
    ({"profile_id": "bge-m3", "embedding_model_path": "/private/model"}, "RETRIEVAL_SELECTION_INVALID"),
    ({"profile_id": []}, "RETRIEVAL_SELECTION_INVALID"),
    ({}, "RETRIEVAL_SELECTION_INVALID"), ([], "RETRIEVAL_SELECTION_INVALID"),
])
def test_invalid_selections_are_not_replaced_by_default(configured, selection, code):
    registry = RetrievalRegistry(configured)
    try:
        with pytest.raises(RetrievalProfileError, match=code):
            registry.freeze(selection)
    finally:
        registry.close()


@pytest.mark.parametrize("change", ["copy", "backend", "vector"])
def test_registry_cache_is_bound_to_settings_and_injected_vector(configured, change):
    original = retrieval.VectorIndex(configured)
    first = registry_for(configured, original)
    assert first.resolve().vector is original
    assert registry_for(configured) is first
    candidate = configured.model_copy() if change != "vector" else configured
    if change == "backend":
        candidate = configured.model_copy(update={"qdrant_path": configured.storage_dir / "other-vectors"})
    injected = retrieval.VectorIndex(candidate) if change == "vector" else None
    second = registry_for(candidate, injected)
    try:
        assert second is not first
        assert second.settings is candidate
        assert second.resolve().vector is not original
        if injected is not None:
            assert second.resolve().vector is injected
        if change == "backend":
            assert second.resolve().vector._shared is not original._shared
        second.close()
        assert not first.closed and not original._closed
    finally:
        second.close()
        first.close()


@pytest.mark.parametrize("change", ["closed", "backend", "reranker"])
def test_borrowed_vector_requires_live_matching_execution_configuration(configured, change):
    borrowed = retrieval.VectorIndex(configured)
    if change == "closed":
        borrowed.close()
        candidate = configured
    else:
        updates = ({"qdrant_path": configured.storage_dir / "other-vectors"} if change == "backend"
                   else {"reranker_batch_size": 4})
        candidate = configured.model_copy(update=updates)
        if change == "reranker":
            path = configured.storage_dir / "qwen3-4b.json"
            values = json.loads(path.read_text())
            path.write_text(json.dumps({**values, **updates}))
    registry = RetrievalRegistry(candidate, borrowed)
    try:
        runtime = registry.resolve()
        assert runtime.vector is not borrowed
        assert not runtime.vector._closed
        assert runtime.vector.settings.qdrant_path == candidate.qdrant_path
    finally:
        registry.close()
    assert borrowed.close_calls == (1 if change == "closed" else 0)


def test_close_borrowed_vector_and_restart_registry(configured):
    borrowed = retrieval.VectorIndex(configured)
    first = registry_for(configured, borrowed)
    assert first.resolve().vector is borrowed
    owned = first.resolve("bge-m3").vector
    first.close()
    assert borrowed.close_calls == 0 and owned.close_calls == 1
    borrowed.close()
    second = registry_for(configured)
    assert second is not first and second.resolve().vector is not borrowed
    second.close()


def test_disabled_profile_is_not_listed_or_resolvable(configured):
    path = configured.retrieval_profiles_file
    manifest = json.loads(path.read_text())
    manifest["profiles"][1]["enabled"] = False
    path.write_text(json.dumps(manifest))
    registry = RetrievalRegistry(configured)
    assert registry.enabled_ids() == ("qwen3-4b",)
    with pytest.raises(RetrievalProfileError, match="RETRIEVAL_PROFILE_NOT_FOUND"):
        registry.freeze({"profile_id": "bge-m3"})


def test_closed_cached_runtime_is_rejected(configured):
    registry = RetrievalRegistry(configured)
    registry.resolve().vector.close()
    with pytest.raises(RetrievalProfileError, match="RETRIEVAL_REGISTRY_CLOSED"):
        registry.resolve()
    registry.close()


@pytest.mark.parametrize("failed_profile", ["qwen3-4b", "bge-m3"])
def test_close_attempts_all_owned_vectors_when_one_close_fails(configured, monkeypatch, failed_profile):
    registry = RetrievalRegistry(configured)
    vectors = {ident: registry.resolve(ident).vector for ident in registry.enabled_ids()}

    def fail_close():
        raise RuntimeError("synthetic close failure")

    monkeypatch.setattr(vectors[failed_profile], "close", fail_close)
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        registry.close()
    assert registry.closed
    assert all(vector._closed for ident, vector in vectors.items() if ident != failed_profile)


def test_no_manifest_and_wiki_only_remain_lazy(tmp_path, synthetic_backend):
    settings = Settings(app_env="test", storage_dir=tmp_path, retrieval_mode="hybrid")
    assert registry_for(settings) is None
    registry = RetrievalRegistry(settings)
    assert registry.freeze()["profile_id"] == "default"
    registry.close()
    synthetic_backend.clear()
    wiki = settings.model_copy(update={"retrieval_mode": "wiki", "retrieval_profiles_file": tmp_path / "missing.json"})
    assert registry_for(wiki) is None
    assert not synthetic_backend


@pytest.mark.parametrize("attack", ["outside", "traversal", "symlink", "parent_symlink", "loop",
    "manifest_symlink", "manifest_outside", "manifest_relative", "manifest_oversized", "profile_oversized",
    "unknown_field", "duplicate_id", "disabled_default", "unknown_default", "bad_json",
    "model_path", "cache_path", "reranker_path", "secret", "http", "downloads", "bad_budget", "qwen_dimension"])
def test_malicious_manifest_and_profile_configuration_rejected(configured, tmp_path, attack):
    manifest_path = configured.retrieval_profiles_file
    manifest = json.loads(manifest_path.read_text())
    profile_path = configured.storage_dir / "qwen3-4b.json"
    profile = json.loads(profile_path.read_text())
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(profile))
    if attack in {"outside", "traversal"}:
        manifest["profiles"][0]["profile_file"] = str(outside) if attack == "outside" else "../outside.json"
    elif attack in {"symlink", "loop"}:
        link = configured.storage_dir / "link.json"
        link.symlink_to(outside if attack == "symlink" else link)
        manifest["profiles"][0]["profile_file"] = str(link)
    elif attack == "parent_symlink":
        link = configured.storage_dir / "link-dir"
        link.symlink_to(tmp_path, target_is_directory=True)
        manifest["profiles"][0]["profile_file"] = str(link / "outside.json")
    elif attack == "manifest_symlink":
        link = configured.storage_dir / "linked-manifest.json"
        link.symlink_to(manifest_path)
        configured = configured.model_copy(update={"retrieval_profiles_file": link})
    elif attack == "manifest_outside":
        outside.write_text(json.dumps(manifest))
        configured = configured.model_copy(update={"retrieval_profiles_file": outside})
    elif attack == "manifest_relative":
        configured = configured.model_copy(update={"retrieval_profiles_file": "retrieval-profiles.json"})
    elif attack == "unknown_field":
        manifest["api_key"] = "synthetic-private-sentinel"
    elif attack == "duplicate_id":
        manifest["profiles"].append(manifest["profiles"][0])
    elif attack == "disabled_default":
        manifest["profiles"][0]["enabled"] = False
    elif attack == "unknown_default":
        manifest["default_profile_id"] = "missing"
    else:
        updates = {"model_path": {"embedding_model_path": str(tmp_path / "model")},
            "cache_path": {"embedding_cache_dir": str(tmp_path / "cache")},
            "reranker_path": {"reranker_model_path": str(tmp_path / "reranker")},
            "secret": {"embedding_api_key": "synthetic-private-sentinel"},
            "http": {"embedding_mode": "http", "embedding_base_url": "https://invalid.example"},
            "downloads": {"embedding_allow_downloads": True}, "bad_budget": {"embedding_max_tokens": 64},
            "qwen_dimension": {"embedding_dimensions": 1024}}
        profile.update(updates.get(attack, {}))
    manifest_path.write_text("{" if attack == "bad_json" else json.dumps(manifest) +
                             (" " * 65536 if attack == "manifest_oversized" else ""))
    profile_path.write_text(json.dumps(profile) + (" " * 16384 if attack == "profile_oversized" else ""))
    with pytest.raises(RetrievalProfileError) as caught:
        registry = RetrievalRegistry(configured)
        try:
            registry.resolve()
        finally:
            registry.close()
    assert caught.value.code == "RETRIEVAL_REGISTRY_INVALID"
    assert "synthetic-private-sentinel" not in str(caught.value)
    assert str(tmp_path) not in caught.value.message
