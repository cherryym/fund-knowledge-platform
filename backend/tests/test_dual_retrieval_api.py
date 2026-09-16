"""HTTP/worker/lifespan integration, using real temporary SQL and Qdrant only."""
from __future__ import annotations

import copy
import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_retrieval_registry import make_settings
from test_retrieval_registry import synthetic_backend as synthetic_backend  # noqa: PLC0414 - pytest fixture
from test_wiki import WikiClient, page

from fund_kb import models as m
from fund_kb import retrieval
from fund_kb import services as svc
from fund_kb.jobs import JobDispatcher
from fund_kb.retrieval_registry import RetrievalProfileError, registry_for


@pytest.fixture
def applications(tmp_path, monkeypatch, synthetic_backend):
    from fund_kb import codex_host, jobs, seed
    from fund_kb import settings as settings_module

    # main's module-level application must also use temporary storage on first import.
    safe = make_settings(tmp_path / "import-only", retrieval_mode="wiki")
    monkeypatch.setattr(settings_module, "get_settings", lambda: safe)
    main = importlib.import_module("fund_kb.main")
    bridges, dispatchers = [], []

    class Bridge:
        def __init__(self):
            self.close_calls = 0
            bridges.append(self)

        def close(self):
            self.close_calls += 1

    class Dispatcher(JobDispatcher):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            dispatchers.append(self)

        def close(self):
            self.close_calls += 1
            super().close()

    monkeypatch.setattr(codex_host, "configured_bridge", lambda settings: Bridge())
    monkeypatch.setattr(jobs, "JobDispatcher", Dispatcher)
    monkeypatch.setattr(Dispatcher, "recover", lambda self: None)
    monkeypatch.setattr(seed, "seed_demo", lambda *args: {"synthetic": True})
    return SimpleNamespace(build=main.build_application, bridge=Bridge, dispatcher=Dispatcher,
                           bridges=bridges, dispatchers=dispatchers)


@pytest.fixture
def env(tmp_path, applications, request):
    settings = make_settings(tmp_path / "api", app_env="development",
        profile_updates={"retrieval_strategy": getattr(request, "param", "version_rrf")})
    app = applications.build(settings)
    app.state.raise_test_errors = True
    owner, reader, space = svc.uid(), svc.uid(), svc.uid()
    with TestClient(app) as client:
        with app.state.session_factory.begin() as db:
            db.add_all([m.User(id=owner, external_subject="demo:synthetic-owner", display_name="合成编辑"),
                m.User(id=reader, external_subject="demo:synthetic-reader", display_name="合成读者"),
                m.Space(id=space, name="合成双索引空间")])
            db.flush()
            for actor, roles in ((owner, ["reader", "editor", "admin"]), (reader, ["reader"])):
                for role in roles:
                    db.add(m.SpaceMember(space_id=space, user_id=actor, role=role))
        result = WikiClient(app, client, owner, reader, space)
        result.registry = app.state.retrieval_registry
        result.dispatcher = app.state.job_dispatcher
        # Keep HTTP-created jobs queued until the test explicitly executes one.
        app.state.job_dispatcher = None
        result.login()
        yield result


@pytest.fixture
def local_profile_files(env, monkeypatch):
    """Tiny synthetic pinned files, never real model weights or inference."""
    from fund_kb import local_encoders, qwen_model_spec

    specs = (qwen_model_spec.QWEN4B_SPEC, local_encoders.MODEL_SPECS["embedding"])
    paths = {}
    for ident, spec in zip(env.registry.enabled_ids(), specs, strict=True):
        runtime = env.registry.resolve(ident)
        monkeypatch.setitem(spec, "revision", runtime.settings.embedding_revision)
        monkeypatch.setitem(spec, "files", {name: (9, pin) for name, (_, pin) in spec["files"].items()})
        paths[ident] = runtime.settings.embedding_model_path
        for name in spec["files"]:
            path = paths[ident] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic")
    original = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *args, **kwargs:
        object() if name in {"transformers", "torch", "safetensors"} else original(name, *args, **kwargs))
    return paths


def list_profiles(env):
    response = env.call("GET", f"/retrieval/profiles?space_id={env.space}")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    for item in response.json()["items"]:
        assert item["model_available"] == item["model_ready"]
        assert item["index_available"] == item["index_ready"]
    return {item["id"]: item for item in response.json()["items"]}


def request_index(env, selection=None):
    response = env.call("POST", "/retrieval/index-jobs", {"space_id": env.space,
        **({"retrieval_selection": selection} if selection is not None else {})})
    assert response.status_code == 202, response.text
    return response.json()


def ask(env, *, tid=None, **extra):
    if tid is None:
        response = env.call("POST", "/threads", {"space_id": env.space, "title": "合成冻结咨询"})
        assert response.status_code == 201, response.text
        tid = response.json()["id"]
    response = env.call("POST", f"/threads/{tid}/runs", {"question": "合成费用核对问题", "context": {},
        "mode": "answer", "answer_scope": "reference", "reasoning_strategy": "model_first", **extra})
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.parametrize("env", ["version_rrf", "unit_rerank"], indirect=True)
def test_actual_profiles_status_search_and_independent_receipts(env, monkeypatch):
    strategy = env.settings.retrieval_strategy
    source = page(env, "合成费用核对", kind="document", text="核对费用基数、费率及适用业务日期。")
    listing = env.call("GET", f"/retrieval/profiles?space_id={env.space}")
    assert listing.status_code == 200, listing.text
    assert listing.headers["cache-control"] == "private, no-store"
    assert listing.json()["default_profile_id"] == "qwen3-4b"
    assert {item["id"] for item in listing.json()["items"]} == {"qwen3-4b", "bge-m3"}
    assert all(not item["available"] for item in listing.json()["items"])
    for item in listing.json()["items"]:
        selected = env.registry.freeze({"profile_id": item["id"]})
        assert {key: item[key] for key in ("model", "dimensions", "fingerprint")} == {
            key: selected[key] for key in ("model", "dimensions", "fingerprint")}
        assert item["is_default"] == (item["id"] == "qwen3-4b") and item["bm25"] is True

    with env.db() as db:
        original_hash = svc.check_frozen_hash(db, db.get(m.ResourceVersion, source[1]))
    for ident in env.registry.enabled_ids():
        selection = env.registry.freeze({"profile_id": ident})
        runtime = env.registry.resolve(ident)
        job = request_index(env, None if ident == "qwen3-4b" else {"profile_id": ident})
        assert job["result"]["retrieval_selection"] == selection
        assert env.dispatcher.run(job["id"])
        status = env.call("GET", f"/retrieval/status?space_id={env.space}&profile_id={ident}")
        assert status.status_code == 200, status.text
        result = status.json()
        assert result["retrieval_selection"] == selection
        assert result["embedding"]["model"] == selection["model"]
        assert result["embedding"]["dimensions"] == selection["dimensions"]
        assert result["vector"]["collection"] == runtime.vector.collection
        assert result["coverage"]["indexed_pages"] == 1 and result["coverage"]["dirty_pages"] == 0
        if ident == "qwen3-4b":
            other = env.call("GET", f"/retrieval/status?space_id={env.space}&profile_id=bge-m3").json()
            assert other["coverage"]["indexed_pages"] == 0
        calls = []
        method = "search_many" if strategy == "unit_rerank" else "search"
        original_search = getattr(runtime.vector, method)

        def recorded_search(*args, _calls=calls, _vector=runtime.vector, _search=original_search, **kwargs):
            _calls.append((_vector.collection, _vector.embedding.fingerprint))
            return _search(*args, **kwargs)

        # A correct response label alone cannot prove the selected vector ran.
        with monkeypatch.context() as tracking:
            tracking.setattr(runtime.vector, method, recorded_search)
            searched = env.call("POST", "/retrieval/search", {"space_id": env.space, "query": "费用基数",
                "retrieval_selection": {"profile_id": ident, "fingerprint": selection["fingerprint"]}})
        assert searched.status_code == 200, searched.text
        assert calls == [(runtime.vector.collection, selection["fingerprint"])]
        body = searched.json()
        assert body["retrieval_selection"] == selection
        assert body["mode"] == ("hybrid_unit_rerank" if strategy == "unit_rerank" else "hybrid")
        assert body["indexed_catalog_pages"] == 1
        hit = next(hit for hit in body["hits"] if hit["version_id"] == source[1])
        assert {"bm25", "vector"} <= set(hit["channels"])
        assert ("batch_execution" in body) == (strategy == "unit_rerank")
    assert env.call("GET", f"/retrieval/status?space_id={env.space}").json()["retrieval_selection"] == env.registry.freeze()
    assert env.call("POST", "/retrieval/search", {"space_id": env.space, "query": "费用"}).json()["retrieval_selection"] == env.registry.freeze()
    # Synthetic embedding success and a real Qdrant collection do not prove
    # local production model files exist. This was the original false READY.
    for item in list_profiles(env).values():
        assert item["index_ready"] and not item["model_ready"] and not item["available"]
        assert item["state"] == "NOT_READY"
        assert item["embedding_status"] in {"LOCAL_MODEL_UNAVAILABLE", "DEPENDENCY_UNAVAILABLE"}
    with env.db() as db:
        receipts = list(db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like("vector-receipt:%"))))
        assert len(receipts) == 2 and len({row.config["fingerprint"] for row in receipts}) == 2
        assert len({row.config["projection_id"] for row in receipts}) == 2
        assert svc.check_frozen_hash(db, db.get(m.ResourceVersion, source[1])) == original_hash


def test_profiles_model_ready_without_index_is_manageable_and_read_only(env, local_profile_files, monkeypatch):
    from fund_kb import local_encoders, qwen_embedding

    def forbidden(*args, **kwargs):
        pytest.fail("Listing profiles must not load models, tokenize, hash or read model file contents")

    monkeypatch.setattr(local_encoders, "verify_model_file", forbidden)
    monkeypatch.setattr(qwen_embedding, "verify_model_file", forbidden)
    monkeypatch.setattr(local_encoders._LocalEncoder, "__init__", forbidden)
    monkeypatch.setattr(qwen_embedding.QwenEmbedding, "__init__", forbidden)
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if any(path.is_relative_to(root) for root in local_profile_files.values()):
            forbidden()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    for _ in range(2):
        for ident, item in list_profiles(env).items():
            assert item["model_ready"] and not item["index_ready"] and not item["available"]
            assert item["can_index"] and item["index_state"] == "NOT_BUILT"
            assert item["embedding_status"] == "LOCAL_FILES_PRESENT_NOT_VERIFIED"
            assert item["readiness_check"] == "prerequisites_only"
            assert env.registry.resolve(ident).vector.embedding._model is None
            assert not env.registry.resolve(ident).vector.status()["collection_exists"]
    with env.db() as db:
        assert db.scalar(select(func.count()).select_from(m.Job)) == 0
        assert not list(db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like("vector-receipt:%"))))
    job = request_index(env, {"profile_id": "bge-m3"})
    assert job["state"] == "QUEUED"
    assert job["result"]["retrieval_selection"] == env.registry.freeze({"profile_id": "bge-m3"})
    env.login(env.reader)
    assert all(item["model_ready"] and not item["can_index"] for item in list_profiles(env).values())


@pytest.mark.parametrize("ident", ["qwen3-4b", "bge-m3"])
def test_profiles_require_model_and_ready_chunks_then_recheck_removed_files(env, local_profile_files, ident):
    from fund_kb import local_encoders, qwen_model_spec

    vector = env.registry.resolve(ident).vector
    vector.prepare_collection()
    item = list_profiles(env)[ident]
    assert item["model_ready"] and not item["index_ready"] and item["index_state"] == "EMPTY"
    assert not item["available"] and item["can_index"]
    page(env, "合成索引就绪", kind="document")
    assert env.dispatcher.run(request_index(env, {"profile_id": ident})["id"])
    item = list_profiles(env)[ident]
    assert item["model_ready"] and item["index_ready"] and item["available"] and item["state"] == "READY"
    other = "bge-m3" if ident == "qwen3-4b" else "qwen3-4b"
    assert not list_profiles(env)[other]["index_ready"]
    weight = (qwen_model_spec.QWEN4B_SPEC["weights"][-1] if ident == "qwen3-4b"
              else local_encoders.MODEL_SPECS["embedding"]["weight"])
    (local_profile_files[ident] / weight).unlink()
    item = list_profiles(env)[ident]
    assert not item["model_ready"] and item["index_ready"] and not item["available"] and not item["can_index"]
    assert item["embedding_status"] == "LOCAL_MODEL_FILES_INCOMPLETE"


@pytest.mark.parametrize("failure", ["dependency", "wrong_revision", "short_file", "symlink", "reranker"])
def test_profiles_fail_closed_on_model_prerequisite_failures(env, local_profile_files, monkeypatch, failure):
    runtime = env.registry.resolve("qwen3-4b")
    root = local_profile_files["qwen3-4b"]
    if failure == "dependency":
        original = importlib.util.find_spec
        monkeypatch.setattr(importlib.util, "find_spec", lambda name, *args, **kwargs:
            None if name == "torch" else original(name, *args, **kwargs))
        expected = "DEPENDENCY_UNAVAILABLE"
    elif failure == "wrong_revision":
        monkeypatch.setattr(runtime.settings, "embedding_revision", "different-revision")
        expected = "LOCAL_MODEL_IDENTITY_NOT_PINNED"
    elif failure == "short_file":
        (root / "config.json").write_bytes(b"partial")
        expected = "LOCAL_MODEL_FILES_INCOMPLETE"
    elif failure == "symlink":
        (root / "config.json").rename(root / "original-config.json")
        (root / "config.json").symlink_to(root / "original-config.json")
        expected = "LOCAL_MODEL_FILES_INCOMPLETE"
    else:
        monkeypatch.setattr(runtime.settings, "reranker_mode", "local")
        expected = "LOCAL_MODEL_IDENTITY_NOT_PINNED"
    item = list_profiles(env)["qwen3-4b"]
    assert not item["available"] and not item["model_ready"] and item["state"] == "NOT_READY"
    assert item["reranker_status" if failure == "reranker" else "embedding_status"] == expected
    assert item["can_index"] == (failure == "reranker")  # Indexing needs only the embedding model.
    assert str(root) not in json.dumps(item)


@pytest.mark.parametrize("status", [
    {"available": False, "status": "UNAVAILABLE", "collection_exists": True, "ready_chunks": 2},
    {"available": True, "closed": True, "collection_exists": True, "ready_chunks": 2},
    {"available": True, "collection_exists": True},
    {"available": True, "collection_exists": True, "ready_chunks": 0, "staging_chunks": 2},
    {"available": True, "ready_chunks": 2},
])
def test_profiles_do_not_promote_missing_or_unavailable_index(env, local_profile_files, monkeypatch, status):
    vector = env.registry.resolve("qwen3-4b").vector
    monkeypatch.setattr(vector, "status", lambda: {"embedding_status": "EXPLICIT_LOCAL_FILES_NOT_VERIFIED", **status})
    item = list_profiles(env)["qwen3-4b"]
    assert item["model_ready"] and not item["index_ready"] and not item["available"]
    assert item["can_index"] == (status["available"] and not status.get("closed", False))


@pytest.mark.parametrize("embedding_status", [None, "LOCAL_MODEL_UNAVAILABLE", "DEPENDENCY_UNAVAILABLE",
    "NOT_CONFIGURED", "CONFIGURED_NOT_VERIFIED", "MODEL_LOAD_FAILED", "UNKNOWN"])
def test_profiles_preserve_provider_failure_even_with_complete_files_and_index(
        env, local_profile_files, monkeypatch, embedding_status):
    vector = env.registry.resolve("qwen3-4b").vector
    backend = {"available": True, "collection_exists": True, "ready_chunks": 2}
    if embedding_status is not None:
        backend["embedding_status"] = embedding_status
    monkeypatch.setattr(vector, "status", lambda: backend)
    item = list_profiles(env)["qwen3-4b"]
    assert item["index_available"] and not item["model_available"]
    assert not item["available"] and not item["can_index"] and item["state"] == "NOT_READY"
    assert item["embedding_status"] == (embedding_status or "UNKNOWN")


@pytest.mark.parametrize("endpoint", ["profiles", "status", "search", "index-jobs"])
def test_selected_runtime_rejects_mislabeled_vector_before_execution(env, monkeypatch, endpoint):
    runtime = env.registry.resolve("bge-m3")
    # Simulate a corrupt runtime which labels the default vector as BGE.
    monkeypatch.setattr(runtime.vector.embedding, "fingerprint", env.registry.resolve().fingerprint)
    if endpoint in {"profiles", "status"}:
        response = env.call("GET", f"/retrieval/{endpoint}?space_id={env.space}&profile_id=bge-m3")
    else:
        data = {"space_id": env.space, "retrieval_selection": {"profile_id": "bge-m3"}}
        if endpoint == "search":
            data["query"] = "合成"
        response = env.call("POST", f"/retrieval/{endpoint}", data)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "RETRIEVAL_RUNTIME_MISMATCH"
    with env.db() as db:
        assert db.scalar(select(func.count()).select_from(m.Job)) == 0


@pytest.mark.parametrize("endpoint", ["/retrieval/search", "/retrieval/index-jobs"])
@pytest.mark.parametrize("selection,code", [
    ({"profile_id": "bge-m3", "fingerprint": "0" * 64}, "RETRIEVAL_PROFILE_CHANGED"),
    ({"profile_id": "missing"}, "RETRIEVAL_PROFILE_NOT_FOUND"),
])
def test_rejected_selection_has_no_job_side_effect(env, endpoint, selection, code):
    with env.db() as db:
        count = db.scalar(select(func.count()).select_from(m.Job))
    data = {"space_id": env.space, "retrieval_selection": selection}
    if endpoint.endswith("search"):
        data["query"] = "合成"
    response = env.call("POST", endpoint, data)
    assert response.status_code == 409 and response.json()["code"] == code
    with env.db() as db:
        assert db.scalar(select(func.count()).select_from(m.Job)) == count


@pytest.mark.parametrize("extra", [{"dimensions": 8}, {"model": "attacker"},
    {"profile_file": "/outside/profile"}, {"base_url": "https://invalid.example"}])
def test_browser_cannot_supply_execution_configuration(env, extra):
    response = env.call("POST", "/retrieval/search", {"space_id": env.space, "query": "合成",
        "retrieval_selection": {"profile_id": "bge-m3", **extra}})
    assert response.status_code == 422, response.text


def test_lists_and_status_require_space_access_and_search_rechecks_revocation(env):
    item = page(env, "合成撤权费用", kind="document")
    for ident in env.registry.enabled_ids():
        assert env.dispatcher.run(request_index(env, {"profile_id": ident})["id"])
    env.login(env.reader)
    for path in ("profiles", "status"):
        response = env.call("GET", f"/retrieval/{path}?space_id={svc.uid()}")
        assert response.status_code in {403, 404}
    assert env.call("POST", "/retrieval/index-jobs", {"space_id": env.space}).status_code == 403
    with env.db.begin() as db:
        resource = db.get(m.Resource, item[0])
        resource.restricted = True
        resource.access_epoch += 1
    for ident in env.registry.enabled_ids():
        status = env.call("GET", f"/retrieval/status?space_id={env.space}&profile_id={ident}").json()
        assert status["coverage"]["indexed_pages"] == 0
        response = env.call("POST", "/retrieval/search", {"space_id": env.space, "query": "费用",
            "retrieval_selection": {"profile_id": ident}})
        assert response.status_code == 200 and response.json()["hits"] == []
        assert response.json()["retrieval_selection"]["profile_id"] == ident
        assert item[0] not in response.text and item[1] not in response.text


def test_running_and_queued_selection_remains_frozen_across_new_default(env, monkeypatch):
    from fund_kb import wiki_answer_job

    runs = [ask(env), ask(env, retrieval_selection={"profile_id": "bge-m3"})]
    original = [(copy.deepcopy(run["retrieval_selection"]), run["id"]) for run in runs]
    attempts = [env.dispatcher._claim(run["job_id"]) for run in runs]
    boundary = threading.Barrier(3)
    released = threading.Event()
    views = []

    def execute(view, job_id, attempt):
        views.append(view)
        boundary.wait(timeout=10)
        assert released.wait(10)
        with view.session_factory.begin() as db:
            job = view._fence(db, job_id, attempt)
            binding = db.get(m.ConsultationRun, job.run_id).request["retrieval_selection"]
            assert view._retrieval_selection == binding
            assert view.vector_index.embedding.fingerprint == binding["fingerprint"]
            assert view.settings.embedding_model == binding["model"]
        assert view.pool is env.dispatcher.pool and view.storage is env.dispatcher.storage
        assert view.codex_bridge is env.dispatcher.codex_bridge
        view.close()
        assert not env.dispatcher._stop.is_set() and not env.registry.closed

    monkeypatch.setattr(wiki_answer_job, "run_wiki_answer", execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(env.dispatcher._answer, run["job_id"], attempt) for run, attempt in zip(runs, attempts)]
        try:
            boundary.wait(timeout=10)
            env.registry.default_id = "bge-m3"
            assert ask(env)["retrieval_selection"]["profile_id"] == "bge-m3"
            assert env.call("GET", f"/retrieval/status?space_id={env.space}").json()["retrieval_selection"]["profile_id"] == "bge-m3"
            assert env.call("POST", "/retrieval/search", {"space_id": env.space, "query": "合成"}).json()["retrieval_selection"]["profile_id"] == "bge-m3"
            inherited = ask(env, tid=runs[0]["thread_id"], parent_run_id=runs[0]["id"])
            assert inherited["retrieval_selection"] == runs[0]["retrieval_selection"]
        finally:
            released.set()
        for future in futures:
            future.result(timeout=10)
    assert len({id(view.vector_index) for view in views}) == 2
    assert env.dispatcher.vector_index.embedding.model == "Qwen/Qwen3-Embedding-4B"
    with env.db() as db:
        for binding, run_id in original:
            run = db.get(m.ConsultationRun, run_id)
            assert run.request["retrieval_selection"] == run.model_snapshot["retrieval_selection"] == binding


def test_queued_fingerprint_change_after_registry_reload_fails_closed(env):
    result = ask(env, retrieval_selection={"profile_id": "bge-m3"})
    path = env.settings.storage_dir / "bge-m3.json"
    values = json.loads(path.read_text())
    path.write_text(json.dumps({**values, "embedding_revision": "synthetic-next-revision"}))
    env.registry.close()
    replacement = registry_for(env.settings, env.dispatcher.vector_index)
    env.dispatcher.retrieval_registry = replacement
    env.app.state.retrieval_registry = replacement
    try:
        assert not env.dispatcher.run(result["job_id"])
        with env.db() as db:
            job = db.get(m.Job, result["job_id"])
            assert job.state == "FAILED" and job.error_code == "RETRIEVAL_PROFILE_CHANGED"
            assert db.get(m.ConsultationRun, result["id"]).model_snapshot["retrieval_selection"] == result["retrieval_selection"]
    finally:
        replacement.close()


@pytest.mark.parametrize("mode", ["wiki", "hybrid"])
def test_no_manifest_api_compatibility(tmp_path, applications, mode):
    settings = make_settings(tmp_path / mode, app_env="development", retrieval_profiles_file=None, retrieval_mode=mode)
    (settings.storage_dir / "retrieval-profiles.json").rename(settings.storage_dir / "unregistered-manifest.json")
    app = applications.build(settings)
    with TestClient(app) as client:
        assert app.state.retrieval_registry is None
        with app.state.session_factory.begin() as db:
            owner, space = svc.uid(), svc.uid()
            db.add_all([m.User(id=owner, external_subject="demo:compatibility", display_name="合成兼容"),
                m.Space(id=space, name="合成兼容空间")])
            db.flush()
            db.add(m.SpaceMember(space_id=space, user_id=owner, role="reader"))
        http = WikiClient(app, client, owner, owner, space)
        http.login()
        listing = http.call("GET", f"/retrieval/profiles?space_id={space}")
        assert listing.status_code == 200 and listing.json() == {"default_profile_id": None, "items": [], "enabled": False}
        status = http.call("GET", f"/retrieval/status?space_id={space}")
        assert status.status_code == 200 and status.json()["mode"] == mode
        searched = http.call("POST", "/retrieval/search", {"space_id": space, "query": "合成"})
        assert searched.status_code == 200 and "retrieval_selection" not in searched.json()
        rejected = http.call("POST", "/retrieval/search", {"space_id": space, "query": "合成",
            "retrieval_selection": {"profile_id": "qwen3-4b"}})
        assert rejected.status_code == 409 and rejected.json()["code"] == "RETRIEVAL_PROFILES_NOT_ENABLED"


def test_wiki_only_does_not_open_manifest_or_vector(tmp_path, applications, synthetic_backend):
    settings = make_settings(tmp_path / "wiki", retrieval_mode="wiki")
    settings.retrieval_profiles_file.write_text("invalid manifest ignored in wiki-only mode")
    with TestClient(applications.build(settings)) as client:
        assert client.app.state.vector_index is None and client.app.state.retrieval_registry is None
    assert not synthetic_backend


def test_application_restart_uses_fresh_owned_resources(tmp_path, applications):
    app = applications.build(make_settings(tmp_path / "restart"))
    previous = None
    for _ in range(2):
        with TestClient(app):
            current = (app.state.vector_index, app.state.retrieval_registry, app.state.codex_bridge)
            vector, registry, bridge = current
            other = registry.resolve("bge-m3").vector
            assert not vector._closed and registry.resolve().vector is vector
            assert not registry.closed and bridge.close_calls == 0
            assert app.state.job_dispatcher.retrieval_registry is registry
            if previous:
                assert all(new is not old for new, old in zip(current, previous))
        assert vector.close_calls == other.close_calls == bridge.close_calls == 1
        assert app.state.vector_index is None and app.state.retrieval_registry is None
        assert app.state.codex_bridge is None and app.state.job_dispatcher is None
        previous = current


def test_two_applications_do_not_share_a_registry_from_settings_cache(tmp_path, applications):
    settings = make_settings(tmp_path / "shared-settings")
    first, second = applications.build(settings), applications.build(settings)
    with TestClient(first):
        registry = first.state.retrieval_registry
        vector = first.state.vector_index
        with TestClient(second):
            assert second.state.retrieval_registry is not registry
            assert second.state.retrieval_registry.resolve().vector is second.state.vector_index
            assert second.state.job_dispatcher.retrieval_registry is second.state.retrieval_registry
        assert not vector._closed and not registry.closed
        assert first.state.job_dispatcher.retrieval_registry is registry


def test_injected_vector_and_bridge_are_borrowed(tmp_path, applications):
    settings = make_settings(tmp_path / "injected")
    app = applications.build(settings)
    vector, bridge = retrieval.VectorIndex(settings), applications.bridge()
    app.state.vector_index, app.state.codex_bridge = vector, bridge
    with TestClient(app):
        registry = app.state.retrieval_registry
        assert registry.resolve().vector is vector
        other = registry.resolve("bge-m3").vector
    assert not vector._closed and bridge.close_calls == 0
    assert registry.closed and other.close_calls == 1


def test_injected_same_fingerprint_wrong_backend_is_rejected(tmp_path, applications):
    settings = make_settings(tmp_path / "wrong-backend")
    app = applications.build(settings)
    borrowed = retrieval.VectorIndex(settings.model_copy(update={"qdrant_path": tmp_path / "other-vectors"}))
    app.state.vector_index = borrowed
    with pytest.raises(RuntimeError, match="RETRIEVAL_DEFAULT_CONFIGURATION_MISMATCH"), TestClient(app):
        pytest.fail("Mismatched injected default vector was admitted")
    assert borrowed.close_calls == 0 and not borrowed._closed


@pytest.mark.parametrize("stage", ["vector", "registry", "default_mismatch", "dispatcher", "seed", "recover", "shutdown"])
def test_lifespan_failure_closes_all_successfully_created_resources(tmp_path, applications, synthetic_backend, monkeypatch, stage):
    from fund_kb import jobs, seed

    settings = make_settings(tmp_path / stage)
    app = applications.build(settings)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    if stage == "vector":
        monkeypatch.setattr(retrieval, "VectorIndex", fail)
    elif stage == "registry":
        settings.retrieval_profiles_file.write_text("{}")
    elif stage == "default_mismatch":
        manifest = json.loads(settings.retrieval_profiles_file.read_text())
        settings.retrieval_profiles_file.write_text(json.dumps({**manifest, "default_profile_id": "bge-m3"}))
    elif stage == "dispatcher":
        monkeypatch.setattr(jobs, "JobDispatcher", fail)
    elif stage == "seed":
        monkeypatch.setattr(seed, "seed_demo", fail)
    elif stage == "recover":
        monkeypatch.setattr(applications.dispatcher, "recover", fail)
    else:
        original = applications.dispatcher.close

        def close_then_fail(dispatcher):
            original(dispatcher)
            fail()

        monkeypatch.setattr(applications.dispatcher, "close", close_then_fail)
    with pytest.raises((RuntimeError, RetrievalProfileError)), TestClient(app):
        assert stage == "shutdown"
        app.state.retrieval_registry.resolve("bge-m3")
    assert all(bridge.close_calls == 1 for bridge in applications.bridges)
    assert all(vector._closed and vector.close_calls == 1 for vector in synthetic_backend)
    assert all(dispatcher.close_calls == 1 for dispatcher in applications.dispatchers)
    assert app.state.vector_index is None
    assert getattr(app.state, "retrieval_registry", None) is None
    assert getattr(app.state, "codex_bridge", None) is None
    assert app.state.job_dispatcher is None
