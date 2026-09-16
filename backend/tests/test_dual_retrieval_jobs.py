"""Frozen dual-profile jobs with a mock registry/vector and synthetic SQL only."""
from __future__ import annotations

import copy
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from test_wiki import env as http_env, page  # noqa: F401
from fund_kb import models as m, providers, services as svc, vector_indexing
from fund_kb.jobs import JobDispatcher
from fund_kb.retrieval_registry import RetrievalProfileError, RetrievalRegistry, RetrievalRuntime


class FakeVector:
    def __init__(self, settings, fingerprint):
        self.settings = settings
        self.embedding = SimpleNamespace(fingerprint=fingerprint, mode="transformers",
            model=settings.embedding_model, dimension=settings.embedding_dimensions)
        self.collection = "fkb_" + fingerprint[:24]
        self.present, self.closed = False, False
        self.staged, self.ready, self.deleted, self.calls = {}, {}, [], []
        self.after_stage = None

    def prepare_collection(self): self.present = True
    def status(self):
        return {"backend": "synthetic", "mode": "local", "available": True, "status": "READY",
            "collection_exists": self.present, "collection": self.collection, "embedding_mode": "transformers"}
    def stage_version(self, records, projection, *, checkpoint, progress):
        checkpoint()
        self.calls.append(copy.deepcopy(records))
        self.staged[records[0]["version_id"], projection] = len(records)
        if self.after_stage: self.after_stage()
        progress(len(records))
        return {"block_count": len(records), "chunk_count": len(records)}
    def activate_version(self, vid, projection, count):
        assert self.staged[vid, projection] == count
        self.ready[vid, projection] = count
    def projection_is_complete(self, vid, projection, count): return self.ready.get((vid, projection)) == count
    def complete_projection_ids(self, receipts):
        return {r["projection_id"] for r in receipts if self.projection_is_complete(r["version_id"], r["projection_id"], r["chunk_count"])}
    def prune_ready_projections(self, vid, keep):
        self.ready = {key: count for key, count in self.ready.items() if key[0] != vid or key[1] in keep}
    def discard_projection(self, vid, projection):
        self.staged.pop((vid, projection), None)
        self.ready.pop((vid, projection), None)
    def delete_versions(self, ids):
        self.deleted.extend(ids)
        self.ready = {key: count for key, count in self.ready.items() if key[0] not in ids}
    def close(self): self.closed = True


class FakeRegistry:
    # Real shallow-copy implementation; no profile file or encoder is loaded.
    scoped_dispatcher = RetrievalRegistry.scoped_dispatcher

    def __init__(self, settings):
        self.default_id, self.closed, self.close_calls = "qwen3-4b", False, 0
        self.runtimes = {}
        for ident, model, dimensions, fingerprint in (
            ("qwen3-4b", "Qwen/Qwen3-Embedding-4B", 2560, "b" * 64),
            ("bge-m3", "BAAI/bge-m3", 1024, "a" * 64),
        ):
            effective = settings.model_copy(update={"retrieval_mode": "hybrid", "answer_engine": "wiki_reader",
                "embedding_mode": "transformers", "embedding_model": model, "embedding_dimensions": dimensions,
                "hybrid_index_auto_sync": True})
            self.runtimes[ident] = RetrievalRuntime(ident, ident, effective, FakeVector(effective, fingerprint), fingerprint)
        self.default_vector = self.runtimes[self.default_id].vector

    def enabled_ids(self): return tuple(self.runtimes)
    def resolve(self, profile_id=None, *, fingerprint=None):
        if self.closed: raise RetrievalProfileError("RETRIEVAL_REGISTRY_CLOSED", "合成关闭")
        ident = self.default_id if profile_id is None else profile_id
        if ident not in self.runtimes: raise RetrievalProfileError("RETRIEVAL_PROFILE_NOT_FOUND", "合成未知方案")
        runtime = self.runtimes[ident]
        if fingerprint is not None and fingerprint != runtime.fingerprint:
            raise RetrievalProfileError("RETRIEVAL_PROFILE_CHANGED", "合成指纹变更")
        return runtime
    def freeze(self, selection=None):
        selection = selection or {}
        result = self.resolve(selection.get("profile_id"), fingerprint=selection.get("fingerprint")).selection()
        if any(selection.get(key, result[key]) != result[key] for key in ("model", "dimensions")):
            raise RetrievalProfileError("RETRIEVAL_PROFILE_CHANGED", "合成元数据变更")
        return result
    def change_fingerprint(self, ident):
        old = self.runtimes[ident]
        self.runtimes[ident] = RetrievalRuntime(ident, old.name, old.settings, FakeVector(old.settings, "c" * 64), "c" * 64)
    def close(self):
        if self.closed: return
        self.closed = True
        self.close_calls += 1
        for runtime in self.runtimes.values():
            if runtime.vector is not self.default_vector: runtime.vector.close()


@pytest.fixture(autouse=True)
def no_external_work(monkeypatch):
    from fund_kb import retrieval, codex_bridge, codex_text
    def forbidden(*args, **kwargs): pytest.fail("Only the mock registry/vector may execute")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(retrieval.EmbeddingProvider, "embed", forbidden)
    monkeypatch.setattr(providers, "complete", forbidden)
    monkeypatch.setattr(codex_bridge, "_read_private", forbidden)
    monkeypatch.setattr(codex_text, "_read_private", forbidden)


@pytest.fixture
def env(http_env):
    http_env.settings = http_env.settings.model_copy(update={"retrieval_mode": "hybrid", "answer_engine": "wiki_reader"})
    http_env.app.state.settings = http_env.settings
    http_env.registry = FakeRegistry(http_env.settings)
    registry = http_env.registry
    http_env.app.state.retrieval_registry = registry
    http_env.app.state.vector_index = registry.default_vector
    http_env.dispatcher = JobDispatcher(http_env.settings, http_env.db, registry.default_vector, retrieval_registry=registry)
    yield http_env
    http_env.dispatcher.close()
    registry.close()


def new_thread(env):
    result = env.call("POST", "/threads", {"space_id": env.space, "title": "合成双索引咨询"})
    assert result.status_code == 201, result.text
    return result.json()["id"]


def ask(env, tid=None, **extra):
    response = env.call("POST", f"/threads/{tid or new_thread(env)}/runs", {
        "question": "合成检索冻结问题", "context": {}, "mode": "answer", "answer_scope": "reference",
        "reasoning_strategy": "model_first", **extra})
    assert response.status_code == 202, response.text
    return response.json()


def context(env, db, *, data=None, query=None):
    return SimpleNamespace(db=db, user=db.get(m.User, env.owner), settings=env.settings,
        retrieval_registry=env.registry, data=data or {}, query=query or {}, dispatch=[],
        request=SimpleNamespace(app=env.app, state=SimpleNamespace(trace_id="synthetic-dual-index")))


def queue(env, ident=None, *, resources=None):
    with env.db.begin() as db:
        ctx = context(env, db, data={"space_id": env.space, **({"retrieval_selection": {"profile_id": ident}} if ident else {})})
        job = vector_indexing.queue_index(ctx, resource_ids=resources)
        return job.id


def test_api_freezes_default_and_explicit_actual_binding(env):
    for selection in (None, {"profile_id": "bge-m3"}, {"profile_id": "qwen3-4b", "fingerprint": "b" * 64}):
        result = ask(env, **({"retrieval_selection": selection} if selection else {}))
        expected = env.registry.freeze(selection)
        assert result["retrieval_selection"] == result["model_snapshot"]["retrieval_selection"] == expected
        with env.db() as db:
            assert db.get(m.ConsultationRun, result["id"]).request["retrieval_selection"] == expected


def test_idempotent_ask_replay_retains_original_default_binding(env):
    tid, key = new_thread(env), svc.uid()
    payload = {"question": "合成幂等冻结", "context": {}, "mode": "answer", "reasoning_strategy": "model_first"}
    first = env.call("POST", f"/threads/{tid}/runs", payload, key=key)
    assert first.status_code == 202
    env.registry.default_id = "bge-m3"
    replay = env.call("POST", f"/threads/{tid}/runs", payload, key=key)
    assert replay.status_code == 202 and replay.json()["id"] == first.json()["id"]
    assert replay.json()["retrieval_selection"] == first.json()["retrieval_selection"]
    assert replay.json()["retrieval_selection"]["profile_id"] == "qwen3-4b"


@pytest.mark.parametrize("selection,status", [({"profile_id": "unknown"}, 409),
    ({"profile_id": "bge-m3", "fingerprint": "b" * 64}, 409),
    ({"profile_id": "bge-m3", "dimensions": 2560}, 422),
    ({"profile_id": "bge-m3", "model": "/arbitrary/path"}, 422), ({}, 422)])
def test_unknown_or_client_fabricated_selection_rejected_before_queue(env, selection, status):
    tid = new_thread(env)
    with env.db() as db: before = db.scalar(select(func.count()).select_from(m.ConsultationRun))
    response = env.call("POST", f"/threads/{tid}/runs", {"question": "合成", "context": {}, "mode": "answer", "retrieval_selection": selection})
    assert response.status_code == status, response.text
    with env.db() as db: assert db.scalar(select(func.count()).select_from(m.ConsultationRun)) == before


def test_default_change_preserves_parent_and_queued_bindings_without_rewriting_history(env):
    env.registry.default_id = "bge-m3"
    parent = ask(env)
    env.registry.default_id = "qwen3-4b"
    child = ask(env, parent["thread_id"], parent_run_id=parent["id"])
    assert child["retrieval_selection"] == parent["retrieval_selection"]
    chosen = ask(env, parent["thread_id"], parent_run_id=parent["id"], retrieval_selection={"profile_id": "qwen3-4b"})
    assert chosen["retrieval_selection"]["profile_id"] == "qwen3-4b"
    with env.db.begin() as db:
        old = db.get(m.ConsultationRun, parent["id"])
        old.request = {key: value for key, value in old.request.items() if key != "retrieval_selection"}
        old.model_snapshot = {}
        historical = copy.deepcopy(old.request)
    child = ask(env, parent["thread_id"], parent_run_id=parent["id"])
    assert child["retrieval_selection"]["profile_id"] == "qwen3-4b"
    with env.db() as db:
        old = db.get(m.ConsultationRun, parent["id"])
        assert old.request == historical and "retrieval_selection" not in old.model_snapshot


def test_parent_fingerprint_change_is_rejected_without_silent_new_default(env):
    parent = ask(env, retrieval_selection={"profile_id": "bge-m3"})
    env.registry.change_fingerprint("bge-m3")
    result = env.call("POST", f"/threads/{parent['thread_id']}/runs", {
        "question": "补充", "context": {}, "mode": "answer", "parent_run_id": parent["id"],
        "reasoning_strategy": "model_first", "answer_scope": "reference"})
    assert result.status_code == 409 and result.json()["code"] == "RETRIEVAL_PROFILE_CHANGED"


def test_conflicting_parent_bindings_are_not_guessed_during_inheritance(env):
    parent = ask(env, retrieval_selection={"profile_id": "bge-m3"})
    with env.db.begin() as db:
        db.get(m.ConsultationRun, parent["id"]).model_snapshot = {"retrieval_selection": env.registry.freeze()}
    response = env.call("POST", f"/threads/{parent['thread_id']}/runs", {"question": "补充", "context": {},
        "mode": "answer", "answer_scope": "reference", "reasoning_strategy": "model_first", "parent_run_id": parent["id"]})
    assert response.status_code == 409 and response.json()["code"] == "RETRIEVAL_BINDING_CHANGED"
    # A newly explicit selection remains possible without modifying the parent.
    child = ask(env, parent["thread_id"], parent_run_id=parent["id"], retrieval_selection={"profile_id": "qwen3-4b"})
    assert child["retrieval_selection"]["profile_id"] == "qwen3-4b"


def test_concurrent_answers_use_private_views_without_changing_shared_dispatcher(env, monkeypatch):
    from fund_kb import wiki_answer_job
    runs = [ask(env), ask(env, retrieval_selection={"profile_id": "bge-m3"})]
    attempts = [env.dispatcher._claim(run["job_id"]) for run in runs]
    original_settings, original_vector, original_pool = env.dispatcher.settings, env.dispatcher.vector_index, env.dispatcher.pool
    env.registry.default_id = "bge-m3"
    barrier, seen = threading.Barrier(2), []
    def execute(view, jid, attempt):
        assert view is not env.dispatcher and view.pool is original_pool
        assert view.storage is env.dispatcher.storage and view._stop is env.dispatcher._stop
        with view.session_factory.begin() as db:
            job = view._fence(db, jid, attempt)
            run = db.get(m.ConsultationRun, job.run_id)
            binding = run.request["retrieval_selection"]
            assert view.vector_index.embedding.fingerprint == binding["fingerprint"]
            assert view.settings.embedding_dimensions == binding["dimensions"]
        barrier.wait(timeout=5)
        seen.append(binding["profile_id"])
        view.close()
        assert not env.dispatcher._stop.is_set()
    monkeypatch.setattr(wiki_answer_job, "run_wiki_answer", execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(env.dispatcher._answer, run["job_id"], attempt) for run, attempt in zip(runs, attempts)]
        for future in futures: future.result(timeout=10)
    assert set(seen) == {"qwen3-4b", "bge-m3"}
    assert env.dispatcher.settings is original_settings and env.dispatcher.vector_index is original_vector
    assert env.dispatcher._job_retrieval_binding is None and env.dispatcher.pool is original_pool


@pytest.mark.parametrize("change", ["fingerprint", "missing", "snapshot"])
def test_queued_answer_refuses_changed_or_unbound_configuration(env, monkeypatch, change):
    from fund_kb import wiki_answer_job
    result = ask(env, retrieval_selection={"profile_id": "qwen3-4b"})
    monkeypatch.setattr(wiki_answer_job, "run_wiki_answer", lambda *args: pytest.fail("Changed/unbound job executed"))
    if change == "fingerprint": env.registry.change_fingerprint("qwen3-4b")
    else:
        with env.db.begin() as db:
            run = db.get(m.ConsultationRun, result["id"])
            if change == "missing":
                run.request = {key: value for key, value in run.request.items() if key != "retrieval_selection"}
                run.model_snapshot = {}
            else: run.model_snapshot = {"retrieval_selection": env.registry.freeze({"profile_id": "bge-m3"})}
    assert not env.dispatcher.run(result["job_id"])
    with env.db() as db:
        job = db.get(m.Job, result["job_id"])
        assert job.state == "FAILED" and job.error_code.startswith("RETRIEVAL_")


def test_generator_model_snapshot_keeps_independent_retrieval_binding(env, monkeypatch):
    result = ask(env, retrieval_selection={"profile_id": "bge-m3"})
    fake = {"id": svc.uid(), "model_id": "synthetic-generator", "revision": 1, "base_url": "", "provider_id": "custom"}
    monkeypatch.setattr(providers, "resolve_connection", lambda *a, **k: fake)
    with env.db.begin() as db:
        run = db.get(m.ConsultationRun, result["id"])
        run.request = {**run.request, "model_selection": {"connection_id": fake["id"], "model_id": fake["model_id"]}}
        env.dispatcher._answer_policy(db, run)
        assert run.model_snapshot["retrieval_selection"] == result["retrieval_selection"]
        assert run.model_snapshot["model_id"] == "synthetic-generator"


def test_explicit_index_builds_only_selected_profile_and_status_filters_jobs(env):
    item = page(env, "合成双索引来源", kind="document")
    first, second = queue(env, "qwen3-4b"), queue(env, "bge-m3")
    assert first != second and queue(env, "qwen3-4b") == first
    with env.db() as db:
        left, right = db.get(m.Job, first), db.get(m.Job, second)
        assert "qwen3-4b" in left.dedupe_key and "bge-m3" in right.dedupe_key
        frozen = copy.deepcopy(left.payload)
        ctx = context(env, db, query={"space_id": env.space, "profile_id": "bge-m3"})
        runtime = env.registry.resolve("bge-m3")
        status = vector_indexing.status_for(ctx, vector=runtime.vector, settings=runtime.settings)
        assert status["active_job"]["id"] == second and status["embedding"]["dimensions"] == 1024
    env.registry.default_id = "bge-m3"
    assert env.dispatcher.run(first)
    qwen, bge = env.registry.resolve("qwen3-4b").vector, env.registry.resolve("bge-m3").vector
    assert qwen.calls and not bge.calls
    with env.db() as db:
        job = db.get(m.Job, first)
        assert job.payload == frozen and job.result["retrieval_selection"] == frozen["retrieval_selection"]
        receipt = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == vector_indexing.receipt_name("b" * 64, item[1])))
        assert receipt.config["fingerprint"] == "b" * 64


def test_concurrent_index_jobs_keep_receipts_and_projections_in_separate_profiles(env):
    _, vid, _ = page(env, "合成并发索引", kind="document")
    ids = [queue(env, ident) for ident in env.registry.enabled_ids()]
    attempts = [env.dispatcher._claim(jid) for jid in ids]
    barrier = threading.Barrier(2)
    for runtime in env.registry.runtimes.values(): runtime.vector.after_stage = lambda: barrier.wait(timeout=5)
    with env.db() as db: original = svc.check_frozen_hash(db, db.get(m.ResourceVersion, vid))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(env.dispatcher.run_claimed, jid, attempt) for jid, attempt in zip(ids, attempts)]
        assert all(future.result(timeout=10) for future in futures)
    with env.db() as db:
        receipts = list(db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like("vector-receipt:%"))))
        assert {row.config["fingerprint"] for row in receipts} == {"a" * 64, "b" * 64}
        assert len({row.config["projection_id"] for row in receipts}) == 2
        assert svc.check_frozen_hash(db, db.get(m.ResourceVersion, vid)) == original
        for row in receipts:
            runtime = next(item for item in env.registry.runtimes.values() if item.fingerprint == row.config["fingerprint"])
            assert runtime.vector.projection_is_complete(vid, row.config["projection_id"], row.config["chunk_count"])


def test_automatic_mutations_fan_out_and_running_profiles_get_separate_successors(env):
    rid, _, _ = page(env, "合成自动同步", kind="document", state="DRAFT")
    with env.db.begin() as db:
        ctx = context(env, db)
        vector_indexing.queue_resource_index(ctx, rid)
        first = list(ctx.dispatch)
        vector_indexing.queue_resource_index(ctx, rid)
        assert len(first) == len(ctx.dispatch) == 2
    attempts = [env.dispatcher._claim(jid) for jid in first]
    with env.db.begin() as db:
        ctx = context(env, db)
        vector_indexing.queue_resource_index(ctx, rid)
        assert len(ctx.dispatch) == 2 and not set(first) & set(ctx.dispatch)
        successor = [db.get(m.Job, jid) for jid in ctx.dispatch]
        assert {job.payload["retrieval_selection"]["profile_id"] for job in successor} == {"qwen3-4b", "bge-m3"}
        vector_indexing.queue_resource_index(ctx, rid)
        assert len(ctx.dispatch) == 2
    for jid, attempt in zip(first, attempts): assert env.dispatcher.run_claimed(jid, attempt)
    assert env.registry.resolve("bge-m3").vector.calls and env.registry.resolve("qwen3-4b").vector.calls


def test_real_content_patch_queues_both_profiles_through_mutation_hook(env):
    from test_hybrid_mutations import edit_body
    rid, vid, _ = page(env, "合成正文修改", kind="document", state="DRAFT")
    edit_body(env, vid, "修改后的完整合成段落。")
    with env.db() as db:
        jobs = list(db.scalars(select(m.Job).where(m.Job.kind == "COMPILE")))
        assert len(jobs) == 2
        assert {job.payload["retrieval_selection"]["profile_id"] for job in jobs} == {"qwen3-4b", "bge-m3"}
        assert all(job.payload["automatic"] and job.payload["resource_ids"] == [rid] for job in jobs)
        assert db.scalar(select(func.count()).select_from(m.Outbox)) == 2


def test_source_worker_success_schedules_both_profiles(env):
    rid, _, _ = page(env, "合成完成回调", kind="document", state="DRAFT")
    with env.db.begin() as db:
        job = svc.create_job(context(env, db), "SCAN_PARSE", {}, resource_id=rid)
        jid = job.id
    attempt = env.dispatcher._claim(jid)
    with env.db.begin() as db:
        env.dispatcher._succeed(db, env.dispatcher._fence(db, jid, attempt), {"resource_id": rid})
    with env.db() as db:
        jobs = list(db.scalars(select(m.Job).where(m.Job.kind == "COMPILE")))
        assert len(jobs) == 2
        assert {job.payload["retrieval_selection"]["profile_id"] for job in jobs} == {"qwen3-4b", "bge-m3"}


def test_auto_sync_checks_each_profile_flag_and_does_not_depend_on_default(env):
    rid, _, _ = page(env, "合成同步开关", kind="document", state="DRAFT")
    old = env.registry.runtimes["qwen3-4b"]
    env.registry.runtimes["qwen3-4b"] = RetrievalRuntime(old.id, old.name,
        old.settings.model_copy(update={"hybrid_index_auto_sync": False}), old.vector, old.fingerprint)
    with env.db.begin() as db:
        ctx = context(env, db)
        vector_indexing.queue_resource_index(ctx, rid)
        assert len(ctx.dispatch) == 1
        assert db.get(m.Job, ctx.dispatch[0]).payload["retrieval_selection"]["profile_id"] == "bge-m3"


def test_revoked_source_is_excluded_from_both_profile_status_views(env):
    rid, _, _ = page(env, "合成撤权来源", kind="document")
    for ident in env.registry.enabled_ids(): assert env.dispatcher.run(queue(env, ident))
    env.login(env.reader)
    for ident in env.registry.enabled_ids():
        result = env.call("GET", f"/retrieval/status?space_id={env.space}&profile_id={ident}")
        assert result.status_code == 200 and result.json()["coverage"]["indexed_pages"] == 1
    with env.db.begin() as db:
        resource = db.get(m.Resource, rid)
        resource.restricted = True
        resource.access_epoch += 1
    for ident in env.registry.enabled_ids():
        result = env.call("GET", f"/retrieval/status?space_id={env.space}&profile_id={ident}")
        assert result.status_code == 200 and result.json()["coverage"]["indexed_pages"] == 0
        assert result.json()["retrieval_selection"]["profile_id"] == ident


def test_removed_version_reaches_both_profile_jobs_even_when_reusing_queued_space_job(env):
    rid, _, _ = page(env, "保留来源", kind="document")
    bge = queue(env, "bge-m3")
    removed = svc.uid()
    with env.db.begin() as db:
        ctx = context(env, db)
        vector_indexing.queue_removed_version(ctx, rid, removed)
        rows = list(db.scalars(select(m.Job).where(m.Job.kind == "COMPILE")))
        assert len(rows) == 2 and any(row.id == bge for row in rows)
        assert all(row.payload["removed_version_ids"] == [removed] for row in rows)
        assert {row.payload["retrieval_selection"]["profile_id"] for row in rows} == {"qwen3-4b", "bge-m3"}


def test_index_retry_keeps_fingerprint_and_rejects_reconfigured_profile(env):
    page(env, "重试来源", kind="document")
    jid = queue(env, "qwen3-4b")
    with env.db.begin() as db:
        job = db.get(m.Job, jid)
        original = copy.deepcopy(job.payload)
        job.state, job.stage, job.error_code, job.attempts = "FAILED", "FAILED", "SYNTHETIC", 1
    env.registry.change_fingerprint("qwen3-4b")
    result = env.call("POST", f"/jobs/{jid}/retry", {})
    assert result.status_code in {200, 202}, result.text
    assert not env.dispatcher.run(jid)
    with env.db() as db:
        job = db.get(m.Job, jid)
        assert job.state == "FAILED" and job.error_code == "RETRIEVAL_PROFILE_CHANGED"
        assert job.payload == original and job.attempts == 2


def test_fingerprint_change_during_indexing_cannot_commit_receipt(env):
    item = page(env, "变更中索引来源", kind="document")
    jid = queue(env, "qwen3-4b")
    vector = env.registry.resolve("qwen3-4b").vector
    vector.after_stage = lambda: env.registry.change_fingerprint("qwen3-4b")
    assert not env.dispatcher.run(jid)
    with env.db() as db:
        assert db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == vector_indexing.receipt_name("b" * 64, item[1]))) is None
    assert not vector.ready


def test_invalidation_deletes_versions_from_each_registered_backend(env):
    rid, vid, _ = page(env, "待撤销来源", kind="document", state="DRAFT")
    with env.db.begin() as db:
        resource = db.get(m.Resource, rid)
        resource.deleted_at = svc.now()
        ctx = context(env, db)
        job = svc.create_job(ctx, "INVALIDATE", {"resource_id": rid, "reason": "DELETED"}, resource_id=rid)
        jid = job.id
    assert env.dispatcher.run(jid)
    assert all(vid in runtime.vector.deleted for runtime in env.registry.runtimes.values())


def test_no_registry_keeps_unbound_single_model_api_and_index_payload(env):
    env.registry = None
    env.app.state.retrieval_registry = None
    result = ask(env)
    assert "retrieval_selection" not in result and "retrieval_selection" not in result["model_snapshot"]
    with env.db.begin() as db:
        ctx = context(env, db, data={"space_id": env.space})
        job = vector_indexing.queue_index(ctx)
        assert "retrieval_selection" not in job.payload


def test_constructor_reuses_cached_registry_and_scoped_close_does_not_own_shared_resources(env, monkeypatch):
    from fund_kb import retrieval_registry as module
    settings = env.settings.model_copy(update={"retrieval_profiles_file": env.settings.storage_dir / "synthetic-manifest.json"})
    object.__setattr__(settings, "_retrieval_registry", env.registry)
    calls = []
    def factory(actual_settings, actual_vector):
        calls.append((actual_settings, actual_vector))
        return env.registry
    monkeypatch.setattr(module, "registry_for", factory)
    dispatcher = JobDispatcher(settings, env.db, env.registry.default_vector)
    assert calls == [(settings, env.registry.default_vector)]
    scoped = env.registry.scoped_dispatcher(dispatcher, env.registry.freeze({"profile_id": "bge-m3"}))
    scoped.close()
    assert not dispatcher._stop.is_set() and not env.registry.closed
    dispatcher.close()
    assert not env.registry.closed and not env.registry.default_vector.closed


@pytest.mark.parametrize("backend", ["local", "celery"])
def test_standalone_dispatcher_assembles_and_closes_only_its_owned_registry(env, monkeypatch, backend):
    from fund_kb import retrieval_registry as module
    settings = env.settings.model_copy(update={"job_backend": backend,
        "retrieval_profiles_file": env.settings.storage_dir / "synthetic-manifest.json"})
    owned = FakeRegistry(settings)
    calls = []
    def factory(actual_settings, vector):
        calls.append((actual_settings, vector))
        object.__setattr__(actual_settings, "_retrieval_registry", owned)
        return owned
    monkeypatch.setattr(module, "registry_for", factory)
    dispatcher = JobDispatcher(settings, env.db, owned.default_vector)
    assert dispatcher.retrieval_registry is owned and dispatcher.mode == backend
    assert calls == [(settings, owned.default_vector)]
    # A CLI queue context borrows the cached registry established by its worker.
    with env.db.begin() as db:
        ctx = SimpleNamespace(db=db, user=db.get(m.User, env.owner), settings=settings,
            data={"space_id": env.space}, dispatch=[], request=SimpleNamespace(state=SimpleNamespace(trace_id="synthetic-cli")))
        queued = vector_indexing.queue_index(ctx)
        assert queued.payload["retrieval_selection"] == owned.freeze()
        assert not owned.closed
    dispatcher.close()
    dispatcher.close()
    assert owned.close_calls == 1 and not owned.default_vector.closed
