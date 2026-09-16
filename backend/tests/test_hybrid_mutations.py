"""Automatic index lifecycle through real handlers and temporary SQLite/Qdrant.

Only the scheduler boundary is held: mutations commit real jobs/outbox records,
then tests explicitly execute those jobs. Hashing is a deterministic test encoder,
not a model/semantic-quality claim. No environment credentials or runtime data.
"""
from __future__ import annotations

import copy
import socket
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_wiki import WikiClient, page

from fund_kb import ai_transport, codex_bridge, codex_text, providers, retrieval
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.api import create_app
from fund_kb.jobs import JobDispatcher
from fund_kb.settings import Settings
from fund_kb.vector_indexing import receipt_name, records_for_version
from fund_kb.wiki_catalog import build_catalog


class SyntheticSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        return (init_settings,)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    attempts = []
    connect = socket.socket.connect
    embed = retrieval.EmbeddingProvider.embed

    def forbidden(*_args, **_kwargs):
        attempts.append("external-io-or-model")
        raise AssertionError("Hybrid mutation tests prohibit network, models, processes and credentials")

    def local_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            return forbidden()
        return connect(sock, address)

    def hashing_only(encoder, texts, **kwargs):
        if encoder.mode != "hashing":
            return forbidden()
        return embed(encoder, texts, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(providers, "complete", forbidden)
    monkeypatch.setattr(providers, "_network", forbidden)
    monkeypatch.setattr(providers, "_master_key", forbidden)
    monkeypatch.setattr(codex_bridge, "_read_private", forbidden)
    monkeypatch.setattr(codex_text, "_read_private", forbidden)
    monkeypatch.setattr(ai_transport, "post_json", forbidden)
    monkeypatch.setattr(retrieval, "post_json", forbidden)
    monkeypatch.setattr(retrieval.EmbeddingProvider, "embed", hashing_only)
    yield
    assert attempts == []


@pytest.fixture
def env(tmp_path, offline_only):
    # Demo login is explicitly scoped to these generated identities. Settings do
    # not consult the parent environment, dotenv files or secret-file providers.
    settings = SyntheticSettings(_env_file=None, app_env="development", auth_mode="demo",
        database_url=f"sqlite:///{tmp_path / 'hybrid-mutations.sqlite'}",
        storage_dir=tmp_path / "objects", qdrant_path=tmp_path / "qdrant",
        allowed_origins=["http://testserver"], retrieval_mode="hybrid",
        hybrid_index_auto_sync=True, embedding_mode="hashing", embedding_model="synthetic-hashing",
        embedding_dimensions=32, embedding_allow_downloads=False, llm_provider="evidence")
    app = create_app(settings)
    app.state.raise_test_errors = True
    assert Path(app.state.engine.url.database).resolve().is_relative_to(tmp_path.resolve())
    assert settings.storage_dir.resolve().is_relative_to(tmp_path.resolve())
    assert settings.qdrant_path.resolve().is_relative_to(tmp_path.resolve())
    vector = retrieval.VectorIndex(settings)
    app.state.vector_index = vector
    scheduled, older_vectors = [], []
    app.state.job_dispatcher = scheduled.append
    dispatcher = JobDispatcher(settings, app.state.session_factory, vector)
    try:
        with TestClient(app) as client:
            owner, reader, space = svc.uid(), svc.uid(), svc.uid()
            with app.state.session_factory.begin() as db:
                db.add_all([m.User(id=owner, external_subject="demo:mutation-owner", display_name="合成编辑者"),
                    m.User(id=reader, external_subject="demo:mutation-reader", display_name="合成读者"),
                    m.Space(id=space, name="合成变更空间")])
                db.flush()
                for actor, roles in ((owner, ["reader", "editor", "reviewer", "publisher", "admin"]),
                                     (reader, ["reader"])):
                    for role in roles:
                        db.add(m.SpaceMember(space_id=space, user_id=actor, role=role))
            api = WikiClient(app, client, owner, reader, space)
            api.vector, api.dispatcher = vector, dispatcher
            api.scheduled, api.older_vectors = scheduled, older_vectors
            api.login()
            yield api
    finally:
        dispatcher.close()
        for older in reversed(older_vectors):
            older.close()
        vector.close()


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response


def jobs(env, *, state=None):
    with env.db() as db:
        return [{**svc.job_dict(job), "payload": copy.deepcopy(job.payload),
                 "version_id": job.version_id, "resource_id": job.resource_id}
                for job in db.scalars(select(m.Job).order_by(m.Job.created_at, m.Job.id))
                if job.kind == "COMPILE" and job.payload.get("task") == "VECTOR_INDEX"
                and (state is None or job.state == state)]


def outbox_count(env, job_id):
    with env.db() as db:
        return db.scalar(select(func.count()).select_from(m.Outbox).where(
            m.Outbox.aggregate_id == job_id, m.Outbox.event_type == "JOB_CREATED"))


def run_job(env, job_id):
    env.dispatcher.run(job_id)
    with env.db() as db:
        value = svc.job_dict(db.get(m.Job, job_id))
    assert value["state"] == "SUCCEEDED", value
    return value


def index_all(env):
    value = ok(env.call("POST", "/retrieval/index-jobs", {"space_id": env.space, "force": False}), 202).json()
    return run_job(env, value["id"])


def receipt(env, version_id):
    with env.db() as db:
        row = db.scalar(select(m.RuntimePolicy).where(
            m.RuntimePolicy.name == receipt_name(env.vector.embedding.fingerprint, version_id)))
        assert row is not None
        return copy.deepcopy(row.config)


def points(vector, version_id):
    """Inspect the actual temporary store, including stale/staging points."""
    from qdrant_client import models
    if not vector._shared.client.collection_exists(vector.collection):
        return []
    result, offset = [], None
    while True:
        batch, offset = vector._shared.client.scroll(vector.collection, offset=offset, limit=256,
            scroll_filter=models.Filter(must=[models.FieldCondition(
                key="version_id", match=models.MatchValue(value=version_id))]),
            with_payload=True, with_vectors=False)
        result.extend(batch)
        if offset is None:
            return result


def search(env, query):
    return ok(env.call("POST", "/retrieval/search", {
        "space_id": env.space, "query": query, "scope": "reference", "limit": 12})).json()


def hit_ids(value):
    return {hit["resource_id"] for hit in value["hits"]}


def edit_body(env, version_id, text):
    current = ok(env.call("GET", f"/versions/{version_id}"))
    body = {key: copy.deepcopy(current.json()[key]) for key in (
        "title", "knowledge_type", "applicability", "required_facts", "legal_status",
        "valid_from", "valid_to", "blocks")}
    body["blocks"][0]["data"] = {"text": text, "text_format": "markdown"}
    return ok(env.call("PATCH", f"/versions/{version_id}", body, etag=current.headers["etag"]))


@pytest.mark.parametrize("kind", ["document", "knowledge"])
@pytest.mark.parametrize("mutation", ["body", "category"])
def test_mutation_queues_automatic_incremental_job_and_durable_outbox(env, kind, mutation):
    item = page(env, "合成可编辑资料", kind=kind, category="内部指引", state="DRAFT")
    assert jobs(env) == []
    if mutation == "body":
        edit_body(env, item[1], "syntheticbodyrevision 合成正文变更。")
    else:
        current = ok(env.call("GET", f"/resources/{item[0]}"))
        changed = ok(env.call("PATCH", f"/resources/{item[0]}", {"category": "未分类"},
                              etag=current.headers["etag"]))
        assert changed.json()["category"] == "未分类"
    queued = jobs(env, state="QUEUED")
    assert len(queued) == 1, queued
    job = queued[0]
    assert job["payload"] == {"task": "VECTOR_INDEX", "space_id": env.space,
        "resource_ids": [item[0]], "force": False, "automatic": True}
    assert job["version_id"] is None and job["resource_id"] is None
    assert outbox_count(env, job["id"]) == 1
    assert env.scheduled == [job["id"]]


def test_body_edit_hides_stale_hit_before_sync_and_replaces_projection(env):
    item = page(env, "合成文稿", kind="document", state="DRAFT", text="retiredorchid 原正文。")
    control = page(env, "合成保留资料", text="controlmarker 对照内容。")
    index_all(env)
    before, control_before = receipt(env, item[1]), receipt(env, control[1])
    assert hit_ids(search(env, "retiredorchid")) == {item[0]}
    edit_body(env, item[1], "updatedcobalt 新正文。")
    queued = jobs(env, state="QUEUED")
    assert len(queued) == 1
    assert points(env.vector, item[1]), "Must prove exclusion while old vectors still exist"
    assert item[0] not in hit_ids(search(env, "retiredorchid"))
    assert hit_ids(search(env, "updatedcobalt")) == set()
    outcome = run_job(env, queued[0]["id"])
    assert outcome["result"]["indexed_versions"] == 1
    assert outcome["result"]["failed_versions"] == 0
    after = receipt(env, item[1])
    assert after["projection_id"] != before["projection_id"]
    found = search(env, "updatedcobalt")
    assert hit_ids(found) == {item[0]}
    assert "bm25" in found["hits"][0]["channels"]
    assert hit_ids(search(env, "retiredorchid")) == set()
    stored = points(env.vector, item[1])
    assert {point.payload["projection_id"] for point in stored} == {after["projection_id"]}
    assert all("retiredorchid" not in point.payload["text"] for point in stored)
    assert receipt(env, control[1]) == control_before
    assert hit_ids(search(env, "controlmarker")) == {control[0]}


@pytest.mark.parametrize("mutation", ["delete", "revoke"])
def test_source_mutation_immediately_hides_source_and_dependent_with_old_vectors_present(env, mutation):
    source = page(env, "合成受控来源", kind="document", text="mutationsentinel 原始依据。")
    dependent = page(env, "合成依赖知识", text="mutationsentinel 引用来源。", cites=[source])
    control = page(env, "合成独立知识", text="mutationsentinel 独立依据。")
    index_all(env)
    old_points = {vid: {point.id for point in points(env.vector, vid)} for vid in (source[1], dependent[1])}
    assert all(old_points.values())
    env.login(env.reader)
    assert hit_ids(search(env, "mutationsentinel")) == {source[0], dependent[0], control[0]}
    env.login()
    if mutation == "delete":
        current = ok(env.call("GET", f"/resources/{source[0]}"))
        ok(env.call("DELETE", f"/resources/{source[0]}", etag=current.headers["etag"]), 204)
    else:
        current = ok(env.call("GET", f"/resources/{source[0]}/permissions"))
        ok(env.call("PUT", f"/resources/{source[0]}/permissions", {
            "restricted": True, "classification": "RESTRICTED", "grants": [
                {"user_id": env.owner, "permission": permission} for permission in ("read", "manage", "edit")]},
            etag=current.headers["etag"]))
    with env.db() as db:
        invalidations = list(db.scalars(select(m.Job).where(m.Job.kind == "INVALIDATE")))
        assert len(invalidations) == 1 and invalidations[0].state == "QUEUED"
        assert invalidations[0].payload["reason"] == ("DELETED" if mutation == "delete" else "PERMISSIONS_CHANGED")
    for vid, ids in old_points.items():
        assert {point.id for point in points(env.vector, vid)} == ids
    env.login(env.reader)
    found = search(env, "mutationsentinel")
    assert hit_ids(found) == {control[0]}, found
    assert found["catalog_pages"] == 1
    assert all(item[1] not in str(found) and item[0] not in str(found) for item in (source, dependent))
    for item in (source, dependent):
        assert env.call("GET", f"/versions/{item[1]}").status_code == 404


@pytest.mark.parametrize("reuse_space_wide", [False, True], ids=["new-targeted-job", "reuse-space-wide-job"])
def test_discard_cleans_draft_projections_and_preserves_survivors(env, reuse_space_wide):
    released = page(env, "合成已发布原文", kind="document", text="releasedmarker 保留原文。")
    draft = page(env, "合成待丢弃草稿", kind="document", state="DRAFT", resource_id=released[0],
                 text="discardedmarker 待丢弃正文。")
    control = page(env, "合成独立保留页", text="survivormarker 保留知识。")
    index_all(env)
    survivors = {vid: {point.id for point in points(env.vector, vid)} for vid in (released[1], control[1])}
    assert points(env.vector, draft[1])
    # A removed version must also disappear from older model collections, while
    # unrelated versions in the same temporary store remain intact.
    older = retrieval.VectorIndex(env.settings.model_copy(update={"embedding_dimensions": 16}))
    env.older_vectors.append(older)
    with env.db() as db:
        actor = db.get(m.User, env.owner)
        for vid in (draft[1], control[1]):
            records, _, _ = records_for_version(db, actor, env.space, vid)
            older.upsert(records)
    old_control = {point.id for point in points(older, control[1])}
    assert old_control and points(older, draft[1])
    queued_id = None
    if reuse_space_wide:
        queued_id = ok(env.call("POST", "/retrieval/index-jobs", {
            "space_id": env.space, "force": False}), 202).json()["id"]
        assert jobs(env, state="QUEUED")[0]["payload"]["resource_ids"] == []
    current = ok(env.call("GET", f"/versions/{draft[1]}"))
    ok(env.call("DELETE", f"/versions/{draft[1]}", etag=current.headers["etag"]), 204)
    assert env.call("GET", f"/versions/{draft[1]}").status_code == 404
    assert hit_ids(search(env, "discardedmarker")) == set()
    assert points(env.vector, draft[1]) and points(older, draft[1])
    pending = jobs(env, state="QUEUED")
    assert len(pending) == 1, pending
    cleanup = pending[0]
    assert cleanup["payload"]["removed_version_ids"] == [draft[1]]
    assert cleanup["version_id"] is None
    if reuse_space_wide:
        assert cleanup["id"] == queued_id
        assert cleanup["payload"]["resource_ids"] == []
    else:
        assert cleanup["payload"]["automatic"] is True
        assert cleanup["payload"]["resource_ids"] == [released[0]]
    assert outbox_count(env, cleanup["id"]) == 1
    outcome = run_job(env, cleanup["id"])
    assert outcome["result"]["retired_versions"] == 1
    assert points(env.vector, draft[1]) == []
    assert points(older, draft[1]) == []
    for vid, ids in survivors.items():
        assert {point.id for point in points(env.vector, vid)} == ids
    assert {point.id for point in points(older, control[1])} == old_control
    assert hit_ids(search(env, "releasedmarker")) == {released[0]}
    assert hit_ids(search(env, "survivormarker")) == {control[0]}


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING"])
def test_automatic_index_job_does_not_mark_document_version_busy(env, state):
    item = page(env, "合成文档可读性", kind="document", state="DRAFT")
    edit_body(env, item[1], "readablemarker 索引期间仍可阅读。")
    job = jobs(env, state="QUEUED")[0]
    if state == "RUNNING":
        assert env.dispatcher._claim(job["id"]) == 1
    current = jobs(env, state=state)
    assert len(current) == 1 and current[0]["version_id"] is None
    with env.db() as db:
        actor = db.get(m.User, env.owner)
        pages = build_catalog(db, actor, env.space, scope="reference")
        assert {entry["version_id"] for entry in pages.values()} == {item[1]}
        records, _, _ = records_for_version(db, actor, env.space, item[1])
        assert len(records) == 1 and "readablemarker" in records[0]["text"]
    assert env.call("GET", f"/versions/{item[1]}").status_code == 200


def test_running_job_keeps_one_queued_successor_for_continuous_edits(env):
    item = page(env, "合成连续编辑", kind="document", state="DRAFT")
    edit_body(env, item[1], "firstrevision 第一版正文。")
    running = jobs(env, state="QUEUED")[0]
    # Claim a real RUNNING lease, but hold execution to test queue admission
    # deterministically without relying on thread timing or replacing a handler.
    attempt = env.dispatcher._claim(running["id"])
    assert attempt == 1
    edit_body(env, item[1], "secondrevision 第二版正文。")
    successor = jobs(env, state="QUEUED")
    assert len(successor) == 1 and successor[0]["id"] != running["id"]
    queued_id = successor[0]["id"]
    edit_body(env, item[1], "finalrevision 最后一次正文。")
    assert [row["id"] for row in jobs(env, state="QUEUED")] == [queued_id]
    assert len(jobs(env)) == 2
    assert jobs(env, state="RUNNING")[0]["payload"] == running["payload"]
    assert outbox_count(env, running["id"]) == outbox_count(env, queued_id) == 1
    assert env.scheduled == [running["id"], queued_id]
    assert successor[0]["payload"]["automatic"] is True
    assert successor[0]["payload"]["force"] is False
    assert successor[0]["payload"]["resource_ids"] == [item[0]]
    assert env.dispatcher.run_claimed(running["id"], attempt)
    run_job(env, queued_id)
    assert hit_ids(search(env, "finalrevision")) == {item[0]}
    assert hit_ids(search(env, "firstrevision")) == hit_ids(search(env, "secondrevision")) == set()


def test_disabling_auto_sync_keeps_mutation_without_creating_index_jobs(env):
    settings = env.settings.model_copy(update={"hybrid_index_auto_sync": False})
    env.app.state.settings = settings
    item = page(env, "合成关闭自动同步", kind="document", state="DRAFT")
    changed = edit_body(env, item[1], "disabledsyncmarker 正文照常保存。")
    assert "disabledsyncmarker" in str(changed.json()["blocks"])
    assert jobs(env) == []
    assert env.scheduled == []
