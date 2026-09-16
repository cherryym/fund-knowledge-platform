"""Integrated Wiki/model HTTP tests: real app, SQLite, storage and job workers.

Only model completion is synthetic. No live provider, credentials or vector
service is used; passing these tests does not certify model/semantic quality.
"""
from __future__ import annotations

import copy
import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from fund_kb import models as m
from fund_kb import providers, retrieval
from fund_kb import services as svc
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.jobs import JobDispatcher
from fund_kb.settings import Settings


def seed_id(number):
    return f"00000000-0000-4000-8000-{number:012d}"


SPACE = seed_id(101)
SOURCE = seed_id(201)
SOURCE_VERSION = seed_id(1201)
MODEL_IDS = ("synthetic-wiki-alpha", "synthetic-wiki-beta")
PAGE_TITLES = ("集成验收估值依据", "集成验收估值复核")


@dataclass
class IntegratedClient:
    app: object
    client: TestClient
    connection_id: str = ""
    csrf: str = ""
    calls: list = field(default_factory=list)
    open_transactions: dict = field(default_factory=dict)
    network_attempts: list = field(default_factory=list)

    def login(self, number=1):
        response = self.client.post("/api/v1/auth/demo", json={"user_id": seed_id(number)},
                                    headers={"Origin": "http://testserver"})
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf_token"]

    def request(self, method, path, body=None, *, etag=None, key=None):
        headers = {"Origin": "http://testserver", "X-CSRF-Token": self.csrf,
                   "Idempotency-Key": key or str(uuid4())}
        if etag is not None:
            headers["If-Match"] = etag
        return self.client.request(method, "/api/v1" + path, json=body, headers=headers)

    def get_json(self, path):
        response = self.request("GET", path)
        assert response.status_code == 200, response.text
        return response.json()

    def await_job(self, response, *, expected="SUCCEEDED"):
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        deadline = time.monotonic() + 12
        latest = None
        while time.monotonic() < deadline:
            latest = self.get_json(f"/jobs/{job_id}")
            if latest["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                assert latest["state"] == expected, latest
                return latest
            time.sleep(.025)
        pytest.fail(f"Persistent dispatcher did not finish job: {latest}")

    def build(self, *, model_id=MODEL_IDS[1]):
        return self.await_job(self.request("POST", "/wiki/builds", {
            "space_id": SPACE, "source_resource_ids": [SOURCE], "max_pages": 2, "consent": True,
            "model_selection": {"connection_id": self.connection_id, "model_id": model_id},
        }))

    def await_run(self, response):
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        deadline = time.monotonic() + 12
        latest = None
        while time.monotonic() < deadline:
            latest = self.get_json(f"/runs/{run_id}")
            if latest["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                assert latest["state"] == "COMPLETED", json.dumps({"state": latest["state"],
                    "failure": latest.get("failure_diagnostic"), "error_code": latest.get("error_code"),
                    "last_request": latest.get("model_snapshot", {}).get("last_request")}, ensure_ascii=False)
                return latest
            time.sleep(.025)
        pytest.fail(f"Persistent answer job did not finish: {latest}")


@pytest.fixture
def integrated(tmp_path, monkeypatch):
    # Remove names only; do not read or reuse any real application credential.
    for name in tuple(os.environ):
        if name.startswith("FKB_"):
            monkeypatch.delenv(name)
    network_attempts = []
    real_connect = socket.socket.connect

    def no_external_connection(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            network_attempts.append("blocked")
            raise AssertionError("Integration tests must not open network connections")
        return real_connect(sock, address)

    def no_vector(*_args, **_kwargs):
        raise AssertionError("Wiki mode must not initialize a vector service")

    def no_master_key(*_args, **_kwargs):
        raise AssertionError("credential_mode=none must not create or read a master key")

    monkeypatch.setattr(socket.socket, "connect", no_external_connection)
    monkeypatch.setattr(retrieval, "VectorIndex", no_vector)
    monkeypatch.setattr(providers, "_master_key", no_master_key)
    settings = Settings(app_env="development", auth_mode="demo", auto_create_schema=True,
        database_url=f"sqlite:///{tmp_path / 'wiki-model.sqlite3'}", storage_dir=tmp_path / "objects",
        qdrant_path=tmp_path / "unused-vectors", retrieval_mode="wiki", llm_provider="evidence",
        allowed_origins=["http://testserver"], job_backend="local", job_workers=1, answer_engine="structured",
        job_recovery_interval_seconds=.2)
    from fund_kb.main import build_application

    app = build_application(settings)
    app.state.raise_test_errors = True
    test = IntegratedClient(app, None, network_attempts=network_attempts)

    def transaction_started(connection):
        test.open_transactions[id(connection)] = threading.get_ident()

    def transaction_ended(connection):
        test.open_transactions.pop(id(connection), None)

    event.listen(app.state.engine, "begin", transaction_started)
    event.listen(app.state.engine, "commit", transaction_ended)
    event.listen(app.state.engine, "rollback", transaction_ended)

    def complete(snapshot, messages, max_tokens=4096, json_mode=True, timeout=60, output_schema=None):
        assert threading.get_ident() not in test.open_transactions.values(), "Model call held a DB transaction"
        assert snapshot["api_key"] is None
        assert snapshot["protocol"] == "ollama" and snapshot["credential_mode"] == "none"
        assert snapshot["id"] == test.connection_id
        assert snapshot["model_id"] in MODEL_IDS
        # This file covers legacy structured model routing; the Markdown reader
        # has a separate whole-page worker/HTTP suite. Use current stage budgets
        # rather than the pre-20260909 universal 4096-token/60-second ceiling.
        assert json_mode
        content = messages[-1]["content"]
        payload = {"probe": True} if content == 'Return exactly {"ok":true}.' else json.loads(content)
        test.calls.append({"model": providers.public_snapshot(snapshot), "input": copy.deepcopy(payload)})
        if payload.get("probe"):
            assert max_tokens <= 4096 and timeout <= 60
            output = {"ok": True}
        elif "sources" in payload:
            assert max_tokens <= 4096 and timeout <= 60
            source = payload["sources"][0]
            output = {"pages": [{"title": title, "category": "集成验收/估值", "knowledge_type": "faq",
                "aliases": [title + "别名"], "links": [PAGE_TITLES[1 - index]],
                "blocks": [{"markdown": source["excerpt"], "evidence_ids": [source["id"]]}]}
                for index, title in enumerate(PAGE_TITLES)], "gaps": []}
        else:
            assert max_tokens <= 8192 and timeout <= settings.answer_model_timeout_seconds
            assert payload["evidence"]
            from answer_content_fixture import reply_for
            output = reply_for(payload)
            assert output["status"] == "ANSWERED"
            if "output_skeleton" not in payload:
                # Current compact model contract leaves citation identities and
                # review_status to the server. Exercise both source kinds using
                # only the registry IDs actually sent to this model callback.
                output["claims"] = [{"text": e["text"], "evidence_ids": [e["id"]]} for e in payload["evidence"]]
            # Deliberately distinct from fallback, still grounded in real input.
            output["summary"] = "请对照引用资料核对费用差异与复核要求。"
            output["limitations"] = ["合成响应仅验证接口整合，仍需专业复核。"]
            if "output_skeleton" in payload:
                output["review_status"] = "REQUIRES_EXPERT"
        return {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(output, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100}}

    monkeypatch.setattr(providers, "complete", complete)
    try:
        with TestClient(app) as client:
            test.client = client
            test.login()
            assert isinstance(app.state.job_dispatcher, JobDispatcher)
            assert app.state.settings.retrieval_mode == "wiki"
            assert app.state.vector_index is None and app.state.job_dispatcher.vector_index is None
            created = test.request("POST", "/model-connections", {
                "space_id": SPACE, "name": "无网络合成模型连接", "provider_id": "ollama",
                "base_url": "http://127.0.0.1:11434", "protocol": "ollama", "enabled": True,
                "credential_mode": "none", "allow_document_transfer": True,
                "custom_models": [{"id": model_id, "name": model_id, "brand": "test"} for model_id in MODEL_IDS],
            })
            assert created.status_code == 201, created.text
            test.connection_id = created.json()["id"]
            assert test.calls == []  # Saving configuration is not a model invocation.
            assert created.json()["credential_present"] is False
            yield test
        assert network_attempts == []
        assert not settings.qdrant_path.exists()
    finally:
        event.remove(app.state.engine, "begin", transaction_started)
        event.remove(app.state.engine, "commit", transaction_ended)
        event.remove(app.state.engine, "rollback", transaction_ended)


def assert_no_private_model_fields(value):
    if isinstance(value, dict):
        assert not {"api_key", "credential_ciphertext", "provider_master_key"}.intersection(value)
        for item in value.values():
            assert_no_private_model_fields(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_private_model_fields(item)


def test_wiki_build_api_dispatches_real_job_and_creates_cited_drafts_and_graph(integrated):
    env = integrated
    with env.app.state.session_factory() as db:
        source = db.get(m.ResourceVersion, SOURCE_VERSION)
        source_hash = svc.check_frozen_hash(db, source)
        blob = db.get(m.Blob, source.source_blob_id)
        original = env.app.state.storage.read_bytes(blob.object_key)
        assert source.state == "APPROVED" and source.source_verified and blob.scan_state == "CLEAN"
        assert db.scalar(select(m.ReviewDecision).where(m.ReviewDecision.version_id == source.id,
            m.ReviewDecision.reviewer_id != source.author_id, m.ReviewDecision.decision == "APPROVE"))
        source_key = blob.object_key
    job = env.build()
    result = job["result"]
    assert job["kind"] == "COMPILE" and job["attempts"] == 1
    assert len(env.calls) == 1 and env.calls[0]["model"]["model_id"] == MODEL_IDS[1]
    assert result["model"]["model_id"] == MODEL_IDS[1] and result["model"]["called"] is True
    assert result["coverage"]["professional_accuracy"] == "NOT_EVALUATED"
    assert len(result["created_resource_ids"]) == len(result["created_version_ids"]) == 2
    assert result["source_version_ids"] == [SOURCE_VERSION]
    sent_source = env.calls[0]["input"]["sources"][0]
    assert_no_private_model_fields(result)
    with env.app.state.session_factory() as db:
        persisted = db.get(m.Job, job["id"])
        assert persisted.state == "SUCCEEDED" and persisted.payload["task"] == "WIKI_BUILD"
        assert persisted.payload["model_selection"]["model_id"] == MODEL_IDS[1]
        assert db.scalar(select(m.Outbox).where(m.Outbox.aggregate_id == persisted.id)).dispatched_at
        for resource_id, version_id in zip(result["created_resource_ids"], result["created_version_ids"], strict=True):
            version = db.get(m.ResourceVersion, version_id)
            assert version.resource_id == resource_id and version.state == "DRAFT" and version.origin == "AI_DRAFT"
            assert version.source_verified is False and db.get(m.Resource, resource_id).active_release_id is None
            assert version.content_sha256 == svc.check_frozen_hash(db, version)
            links = list(db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == version.id)))
            assert links and {(e.to_version_id, e.to_block_id) for e in links} == {
                (SOURCE_VERSION, sent_source["block_id"])}
            for block in svc.version_blocks(db, version_id):
                assert db.get(m.ContentBlock, (version_id, block["block_id"])).content_sha256 == text_sha256(block_text(block))
        assert svc.check_frozen_hash(db, db.get(m.ResourceVersion, SOURCE_VERSION)) == source_hash
    assert env.app.state.storage.read_bytes(source_key) == original
    workspace = env.get_json(f"/wiki/workspace?space_id={SPACE}")
    visible = {page["id"] for page in workspace["pages"]}
    assert set(result["created_resource_ids"]) <= visible and SOURCE not in visible
    graph = env.get_json(f"/wiki/graph?space_id={SPACE}")
    assert {*result["created_resource_ids"], SOURCE} <= {node["id"] for node in graph["nodes"]}
    edges = {(edge["source"], edge["target"], edge["origin"]) for edge in graph["edges"]}
    left, right = result["created_resource_ids"]
    assert {(left, right, "wikilink"), (right, left, "wikilink"), (left, SOURCE, "citation")} <= edges
    linked = env.get_json(f"/wiki/pages/{left}/links")
    assert right in {item["id"] for item in linked["outgoing"]}
    assert SOURCE in {item["id"] for item in linked["sources"]}
    env.login(3)
    assert not set(result["created_resource_ids"]) & {
        page["id"] for page in env.get_json(f"/wiki/workspace?space_id={SPACE}")["pages"]}


@pytest.mark.parametrize("model_id", MODEL_IDS)
def test_consultation_selected_model_reaches_completion_and_public_snapshot(integrated, model_id):
    env = integrated
    options = env.get_json(f"/model-options?space_id={SPACE}")["items"]
    assert {item["model_id"] for item in options if item["connection_id"] == env.connection_id} == set(MODEL_IDS)
    thread = env.request("POST", "/threads", {"space_id": SPACE, "title": "所选模型费用差异咨询"})
    assert thread.status_code == 201, thread.text
    selection = {"connection_id": env.connection_id, "model_id": model_id}
    response = env.request("POST", f"/threads/{thread.json()['id']}/runs", {
        "question": "费用差异排查与复核要求", "mode": "answer", "context": {}, "model_selection": selection,
        "reasoning_strategy": "evidence_first",  # explicit legacy route; Wiki reader is tested separately
    })
    run = env.await_run(response)
    assert len(env.calls) == 1 and env.calls[0]["model"]["model_id"] == model_id
    assert run["answer"]["status"] == "ANSWERED"
    assert run["answer"]["summary"] == "请对照引用资料核对费用差异与复核要求。"
    assert run["answer"]["review_status"] == "REQUIRES_EXPERT"
    assert "已降级为证据提取" not in " ".join(run["answer"]["limitations"])
    model = run["model_snapshot"]
    assert model["id"] == env.connection_id and model["model_id"] == model_id
    assert model["provider_id"] == "ollama" and model["protocol"] == "ollama"
    assert model["configured_model"] == model_id and model["execution_mode"] == "http_grounded"
    assert model["semantic_effectiveness"] == "NOT_EVALUATED"
    assert_no_private_model_fields(model)
    with env.app.state.session_factory() as db:
        persisted = db.get(m.ConsultationRun, run["id"])
        assert persisted.request["model_selection"] == selection
        assert persisted.model_snapshot == model
        assert persisted.policy_snapshot["source"] == "model-connection"
        assert persisted.policy_snapshot["retrieval_mode"] == "wiki"
        assert persisted.policy_snapshot["enable_vector"] is False
        job = db.scalar(select(m.Job).where(m.Job.run_id == persisted.id))
        assert job.kind == "ANSWER" and job.state == "SUCCEEDED" and job.attempts == 1
        assert "answer" not in job.result
        snapshot_pairs = {(e.version_id, e.block_id) for e in db.scalars(
            select(m.RunEvidence).where(m.RunEvidence.run_id == persisted.id))}
        kinds = set()
        for citation in run["answer"]["citations"]:
            pair = citation["version_id"], citation["block_id"]
            assert pair in snapshot_pairs
            block = db.get(m.ContentBlock, pair)
            assert citation["content_sha256"] == block.content_sha256
            assert citation["excerpt"] in block.search_text
            kinds.add(db.get(m.Resource, citation["resource_id"]).kind)
        assert "knowledge" in kinds
        registry = {citation["id"]: citation for citation in run["answer"]["citations"]}
        for record in env.calls[0]["input"]["evidence"]:
            # Model-facing records deliberately carry EIDs, not trusted UUIDs.
            registered = registry[record["id"]]
            version = db.get(m.ResourceVersion, registered["version_id"])
            assert (version.id, registered["block_id"]) in snapshot_pairs
            assert version.state == "APPROVED" and svc.released(db, version.id)


def test_wiki_job_get_hides_completed_result_after_source_read_permission_revoked(integrated):
    env = integrated
    completed = env.build()
    source = env.request("GET", f"/resources/{SOURCE}")
    assert source.status_code == 200, source.text
    changed = env.request("PUT", f"/resources/{SOURCE}/permissions", {
        "restricted": True, "classification": "INTERNAL", "grants": [
            {"user_id": seed_id(1), "permission": "manage"},
            {"user_id": seed_id(2), "permission": "read"},
        ],
    }, etag=source.headers["etag"])
    assert changed.status_code == 200, changed.text
    denied = env.request("GET", f"/jobs/{completed['id']}")
    assert denied.status_code == 404, denied.text
    assert all(resource_id not in denied.text for resource_id in completed["result"]["created_resource_ids"])
    assert completed["id"] not in {job["id"] for job in env.get_json("/jobs")["items"]}
    assert env.request("GET", f"/versions/{SOURCE_VERSION}").status_code == 404
    # Read denial hides the result; it must not erase job/evidence history.
    with env.app.state.session_factory() as db:
        persisted = db.get(m.Job, completed["id"])
        assert persisted.state == "SUCCEEDED" and persisted.result == completed["result"]
        assert persisted.result["source_version_ids"] == [SOURCE_VERSION]


@pytest.mark.parametrize("task,path", [("MODEL_SYNC", "sync-models"), ("MODEL_TEST", "test")])
def test_model_task_cancel_and_retry_uses_model_authority_not_compile_target_space(integrated, monkeypatch, task, path):
    env = integrated
    started, release = threading.Event(), threading.Event()
    original_complete = providers.complete
    sync_calls = []

    def holding_complete(snapshot, messages, **options):
        # Occupy the one real worker without mutating the model-connection revision.
        content = messages[-1]["content"]
        if content.startswith("{") and "sources" in json.loads(content):
            started.set()
            assert release.wait(8), "Test did not release its own blocked Wiki completion"
        return original_complete(snapshot, messages, **options)

    def list_synthetic_models(snapshot, *, limit, metadata):
        assert threading.get_ident() not in env.open_transactions.values()
        assert snapshot["api_key"] is None and snapshot["id"] == env.connection_id
        sync_calls.append(snapshot["id"])
        metadata["truncated"] = False
        return [{"id": model_id, "name": model_id, "brand": "test", "source": "synced"} for model_id in MODEL_IDS]

    monkeypatch.setattr(providers, "complete", holding_complete)
    # MODEL_SYNC lists a catalog rather than calling complete. Stub only that I/O
    # boundary; retain the real connection job, authorization, queue and writes.
    monkeypatch.setattr(providers, "_list_models", list_synthetic_models)
    blocker = env.request("POST", "/wiki/builds", {
        "space_id": SPACE, "source_resource_ids": [SOURCE], "max_pages": 2, "consent": True,
        "model_selection": {"connection_id": env.connection_id, "model_id": MODEL_IDS[1]},
    })
    assert blocker.status_code == 202, blocker.text
    try:
        assert started.wait(5), "Real dispatcher did not start the synthetic completion"
        queued = env.request("POST", f"/model-connections/{env.connection_id}/{path}",
                             {"model_id": MODEL_IDS[1]} if task == "MODEL_TEST" else {},
                             etag=env.request("GET", f"/model-connections/{env.connection_id}").headers["etag"])
        assert queued.status_code == 202, queued.text
        job_id = queued.json()["id"]
        with env.app.state.session_factory() as db:
            pending = db.get(m.Job, job_id)
            assert pending.state == "QUEUED" and pending.attempts == 0
            assert pending.payload["task"] == task and "target_space_id" not in pending.payload
            revision = pending.payload["connection_revision"]
        cancelled = env.request("POST", f"/jobs/{job_id}/cancel")
        assert cancelled.status_code == 202 and cancelled.json()["state"] == "CANCELLED", cancelled.text
        retried = env.request("POST", f"/jobs/{job_id}/retry")
        assert retried.status_code == 202, retried.text
        assert retried.json()["id"] == job_id and retried.json()["state"] == "QUEUED"
    finally:
        release.set()
    env.await_job(blocker)
    done = env.await_job(retried)
    assert done["attempts"] == 1 and done["error_code"] is None
    assert done["result"]["connection_id"] == env.connection_id
    assert done["result"]["status"] == ("SYNCED" if task == "MODEL_SYNC" else "TESTED")
    with env.app.state.session_factory() as db:
        stored = db.get(m.Job, job_id)
        assert stored.payload["connection_revision"] == revision
        assert "target_space_id" not in stored.payload
        actions = set(db.scalars(select(m.AuditEvent.action).where(m.AuditEvent.object_id == job_id)))
        assert {"job.cancel_requested", "job.retried", "job.succeeded"} <= actions
        events = list(db.scalars(select(m.Outbox).where(m.Outbox.aggregate_id == job_id)))
        assert len(events) >= 2  # Initial creation and retry remain auditable.
    if task == "MODEL_SYNC":
        assert sync_calls == [env.connection_id]
    else:
        probes = [call for call in env.calls if call["input"].get("probe")]
        assert len(probes) == 1 and probes[0]["model"]["model_id"] == MODEL_IDS[1]


@pytest.mark.parametrize("task,path", [("MODEL_SYNC", "sync-models"), ("MODEL_TEST", "test")])
def test_failed_model_task_retry_reports_connection_revision_not_missing_compile_field(integrated, monkeypatch, task, path):
    env = integrated

    def failed_io(*_args, **_kwargs):
        raise providers.ProviderError("SYNTHETIC_MODEL_UNAVAILABLE")

    target = "_list_models" if task == "MODEL_SYNC" else "complete"
    monkeypatch.setattr(providers, target, failed_io)
    failed = env.await_job(env.request("POST", f"/model-connections/{env.connection_id}/{path}",
        {"model_id": MODEL_IDS[1]} if task == "MODEL_TEST" else {},
        etag=env.request("GET", f"/model-connections/{env.connection_id}").headers["etag"]), expected="FAILED")
    assert failed["error_code"] == "SYNTHETIC_MODEL_UNAVAILABLE"
    with env.app.state.session_factory() as db:
        persisted = db.get(m.Job, failed["id"])
        assert persisted.payload["task"] == task and "target_space_id" not in persisted.payload
        connection = db.get(m.RuntimePolicy, env.connection_id)
        assert connection.config["status"] == "ERROR"
        assert connection.revision == persisted.payload["connection_revision"]
    # Status updates no longer mutate the configuration ETag. A real user edit
    # does: the frozen failed task must not retry against that changed snapshot.
    current = env.request("GET", f"/model-connections/{env.connection_id}")
    changed = env.request("PATCH", f"/model-connections/{env.connection_id}",
                          {"name": "合成测试：失败后变更连接"}, etag=current.headers["etag"])
    assert changed.status_code == 200, changed.text
    retry = env.request("POST", f"/jobs/{failed['id']}/retry")
    assert retry.status_code == 409, retry.text
    assert retry.json()["code"] == "CONNECTION_REVISION_CHANGED"
    with env.app.state.session_factory() as db:
        persisted = db.get(m.Job, failed["id"])
        assert persisted.state == "FAILED" and persisted.attempts == 1
        assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.object_id == persisted.id,
                                                   m.AuditEvent.action == "job.failed"))


def test_stripping_editable_citations_does_not_bypass_wiki_provenance_revocation(integrated):
    env = integrated
    completed = env.build()
    resource_id = completed["result"]["created_resource_ids"][0]
    version_id = completed["result"]["created_version_ids"][0]
    original = env.request("GET", f"/versions/{version_id}")
    assert original.status_code == 200, original.text
    body = {key: original.json()[key] for key in ("title", "knowledge_type", "applicability", "required_facts",
        "legal_status", "valid_from", "valid_to", "blocks")}
    before_text = [block["data"] for block in body["blocks"]]
    assert any(block["citations"] for block in body["blocks"])
    with env.app.state.session_factory() as db:
        provenance = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == f"wiki-provenance:{resource_id}"))
        assert provenance is not None
        provenance_digest = svc.digest(provenance.config)
    for block in body["blocks"]:
        block["citations"], block["locator"] = [], {}
    edited = env.request("PATCH", f"/versions/{version_id}", body, etag=original.headers["etag"])
    assert edited.status_code == 200, edited.text
    assert [block["data"] for block in edited.json()["blocks"]] == before_text
    assert all(block["citations"] == [] and block["locator"] == {} for block in edited.json()["blocks"])
    with env.app.state.session_factory() as db:
        assert not db.scalar(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == version_id))
        provenance = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == f"wiki-provenance:{resource_id}"))
        assert svc.digest(provenance.config) == provenance_digest
        assert SOURCE_VERSION in svc.dependency_ids(db, db.get(m.ResourceVersion, version_id))

    source = env.request("GET", f"/resources/{SOURCE}")
    revoked = env.request("PUT", f"/resources/{SOURCE}/permissions", {
        "restricted": True, "classification": "INTERNAL", "grants": [
            {"user_id": seed_id(1), "permission": "manage"},
            {"user_id": seed_id(2), "permission": "read"},
        ],
    }, etag=source.headers["etag"])
    assert revoked.status_code == 200, revoked.text
    for path in (f"/resources/{resource_id}", f"/versions/{version_id}", f"/jobs/{completed['id']}",
                 f"/versions/{version_id}/content?representation=html",
                 f"/versions/{version_id}/content?representation=markdown"):
        denied = env.request("GET", path)
        assert denied.status_code == 404, (path, denied.text)
        assert PAGE_TITLES[0] not in denied.text
    workspace = env.get_json(f"/wiki/workspace?space_id={SPACE}")
    assert resource_id not in {page["id"] for page in workspace["pages"]}
    with env.app.state.session_factory() as db:
        # Authorization withdrawal protects, but does not erase, the retained draft.
        assert [block["data"] for block in svc.version_blocks(db, version_id)] == before_text
        assert db.get(m.Job, completed["id"]).state == "SUCCEEDED"


def test_celery_wiki_runtime_initializes_and_closes_without_vector(integrated, monkeypatch):
    from fund_kb import celery_app
    env = integrated
    # Isolate the worker module cache from other tests; no broker is connected.
    monkeypatch.setattr(celery_app, "_runtime", None)
    settings = env.app.state.settings.model_copy(update={"job_backend": "celery"})
    try:
        worker = celery_app._worker_runtime(settings)
        assert isinstance(worker, JobDispatcher) and worker.mode == "celery"
        assert worker.vector_index is None
        assert celery_app._worker_runtime(settings) is worker
        assert celery_app._runtime[1] is None
        assert not settings.qdrant_path.exists()
        with worker.session_factory() as db:
            assert db.get(m.ResourceVersion, SOURCE_VERSION).source_verified
    finally:
        celery_app.close_worker_runtime()
    assert celery_app._runtime is None
