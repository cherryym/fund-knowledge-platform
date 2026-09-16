"""Real extension HTTP/SQLite/dispatcher; synthetic data and mock completion only.

No real credentials, external network, running service or PURGE job is used.
"""
import copy
import hashlib
import json
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select

from fund_kb import documents as docs
from fund_kb import models as m
from fund_kb import providers
from fund_kb import services as svc
from fund_kb.api import create_app
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.jobs import JobDispatcher
from fund_kb.settings import Settings

REAL_COMPLETE = providers.complete


class Harness:
    def login(self, who=None):
        response = self.client.post("/api/v1/auth/demo", json={"user_id": who or self.owner},
                                    headers={"Origin": "http://testserver"})
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf_token"]

    def call(self, method, path, data=None, etag=None, key=None, headers=None):
        header = {"Origin": "http://testserver", "X-CSRF-Token": self.csrf, "Idempotency-Key": key or str(uuid4())}
        if etag is not None:
            header["If-Match"] = f'"{etag}"' if isinstance(etag, int) else etag
        header.update(headers or {})
        return self.client.request(method, "/api/v1" + path, json=data, headers=header)

    def taxonomy(self):
        result = self.call("GET", f"/documents/taxonomy?space_id={self.space}")
        assert result.status_code == 200, result.text
        return result

    def category(self, path, mode="POST", new_path=None, etag=None):
        body = {"space_id": self.space, "path": path}
        if new_path:
            body["new_path"] = new_path
        return self.call(mode, "/documents/categories", body,
                         self.taxonomy().headers["etag"] if etag is None else etag)

    def source(self, category="内部指引", owner=None, restricted=False, kind="document", deleted=False):
        object_key = f"test/{uuid4()}.txt"
        self.app.state.storage.write_bytes(object_key, self.original)
        with self.factory.begin() as db:
            resource = m.Resource(id=str(uuid4()), space_id=self.space, kind=kind, name="合成来源文档",
                category=category, tags=[], owner_id=owner or self.owner, restricted=restricted,
                deleted_at=svc.now() if deleted else None)
            blob = m.Blob(id=str(uuid4()), space_id=self.space, object_key=object_key,
                sha256=self.original_hash, size_bytes=len(self.original), mime_type="text/plain", scan_state="CLEAN")
            db.add_all([resource, blob]); db.flush()
            version = m.ResourceVersion(id=str(uuid4()), resource_id=resource.id, version_no=1, state="DRAFT",
                author_id=owner or self.owner, title="合成内部指引", origin="UPLOAD", source_blob_id=blob.id,
                knowledge_type="source", applicability={}, required_facts=[], legal_status="UNKNOWN")
            db.add(version); db.flush()
            data = {"text": self.original.decode()}
            text = block_text({"block_type": "paragraph", "data": data})
            block = m.ContentBlock(version_id=version.id, block_id=str(uuid4()), ordinal=0, block_type="paragraph",
                data=data, locator={"label": "合成原文第1段"}, search_text=text, content_sha256=text_sha256(text))
            db.add(block); db.flush()
            return SimpleNamespace(id=resource.id, version_id=version.id, blob_id=blob.id,
                                   object_key=object_key, block_id=block.block_id, revision=resource.revision,
                                   version_revision=version.revision)

    def queue(self, source=None, *, key=None):
        source = source or self.doc
        return self.call("POST", f"/documents/{source.id}/normalizations", {
            "source_version_id": source.version_id, "model_selection": {"connection_id": self.connection, "model_id": "synthetic"},
            "consent": True}, source.version_revision, key=key)

    def run(self, response):
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        self.dispatcher.run(job_id)
        with self.factory() as db:
            return copy.deepcopy(svc.job_dict(db.get(m.Job, job_id)))

    def ready(self):
        job = self.run(self.queue())
        assert job["state"] == "SUCCEEDED", job
        result = self.call("GET", f'/document-normalizations/{job["id"]}')
        assert result.status_code == 200, result.text
        return job["id"], result

    def apply(self, job_id, response, *, key=None, title="经人工预览的合成指引"):
        return self.call("POST", f"/document-normalizations/{job_id}/apply", {"reviewed": True,
            "title": title, "blocks": response.json()["suggestion"]["blocks"], "review_note": "合成审阅记录，待独立复核"},
            response.headers["etag"], key=key)


@pytest.fixture
def h(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("FKB_"):
            monkeypatch.delenv(name)
    original_connect = socket.socket.connect
    def no_network(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("No real network in document tests")
        return original_connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(providers, "_master_key", lambda *_a, **_k: pytest.fail("No credential reads"))
    settings = Settings(_env_file=None, app_env="development", auth_mode="demo", auto_create_schema=True,
        database_url=f"sqlite:///{tmp_path / 'documents.sqlite3'}", storage_dir=tmp_path / "objects",
        allowed_origins=["http://testserver"], retrieval_mode="wiki", llm_provider="evidence", job_workers=1)
    harness = Harness()
    harness.app = create_app(settings)
    harness.app.state.raise_test_errors = True
    harness.factory = harness.app.state.session_factory
    harness.original = "每日核对业务日期及估值数据。发现差异时记录问题并提交复核；处理时限尚待确认。".encode()
    harness.original_hash = hashlib.sha256(harness.original).hexdigest()
    harness.calls = []
    harness.open_transactions = {}
    harness.behavior = None
    harness.owner, harness.peer, harness.reader, harness.outsider, harness.admin = [str(uuid4()) for _ in range(5)]
    harness.space, harness.connection = str(uuid4()), str(uuid4())
    engine = harness.factory.kw["bind"]
    event.listen(engine, "begin", lambda conn: harness.open_transactions.__setitem__(id(conn), threading.get_ident()))
    for action in ("commit", "rollback"):
        event.listen(engine, action, lambda conn: harness.open_transactions.pop(id(conn), None))
    def complete(snapshot, messages, **options):
        assert threading.get_ident() not in harness.open_transactions.values(), "Provider called in transaction"
        assert options["max_tokens"] == 4096 and 0 < options["timeout"] <= 45 and options["json_mode"] is True
        assert snapshot["model_id"] == "synthetic"
        harness.calls.append(copy.deepcopy(messages))
        if harness.behavior:
            output = harness.behavior()
            if output is not None:
                return output
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "title": "合成规范化指引", "blocks": [{"markdown": "## 日常核对\n核对业务日期及估值数据，记录差异并提交复核。",
            "evidence_ids": ["S1"]}], "gaps": ["处理时限尚待确认。"]}, ensure_ascii=False)}}]}
    monkeypatch.setattr(providers, "complete", complete)
    with TestClient(harness.app) as client:
        harness.client = client
        with harness.factory.begin() as db:
            for who in (harness.owner, harness.peer, harness.reader, harness.outsider, harness.admin):
                db.add(m.User(id=who, external_subject="demo:" + who, display_name="合成用户", active=True))
            db.add(m.Space(id=harness.space, name="文档专项合成空间")); db.flush()
            for who, roles in [(harness.owner, ["reader", "editor", "admin"]), (harness.peer, ["reader", "editor"]),
                               (harness.reader, ["reader"]), (harness.admin, ["reader", "editor", "admin"])]:
                for role in roles:
                    db.add(m.SpaceMember(space_id=harness.space, user_id=who, role=role))
            db.add(m.RuntimePolicy(id=harness.connection, name=f"model-connection:{harness.connection}", updated_by=harness.owner,
                config={"id": harness.connection, "owner_user_id": harness.owner, "space_id": harness.space,
                    "name": "合成无密钥本地连接", "provider_id": "ollama", "protocol": "ollama",
                    "base_url": "http://127.0.0.1:11434", "credential_mode": "none", "enabled": True,
                    "allow_document_transfer": True, "custom_models": [{"id": "synthetic", "name": "合成测试模型"}]}))
        harness.doc = harness.source()
        harness.original_file = harness.app.state.storage.local_path(harness.doc.object_key)
        harness.dispatcher = JobDispatcher(settings, harness.factory, None)
        harness.app.state.job_dispatcher = None
        harness.login()
        yield harness
        harness.dispatcher.close()
        assert harness.original_file.read_bytes() == harness.original
        with harness.factory() as db:
            assert not db.scalar(select(m.Job.id).where(m.Job.kind == "PURGE"))


def test_taxonomy_only_visible_documents_defaults_and_wiki_separate(h):
    h.source("业务/估值")
    h.source("只在秘密原件出现/保密", owner=h.peer, restricted=True)
    h.source("知识专属", kind="knowledge")
    result = h.taxonomy().json()
    paths = {row["path"]: row for row in result["categories"]}
    assert result["total_visible"] == 2
    assert paths["业务"]["count"] == paths["业务/估值"]["count"] == 1
    assert paths["业务"]["direct_count"] == 0
    assert {"未分类", "内部指引"}.issubset(paths)
    assert not any("秘密" in path or "知识专属" in path for path in paths)
    assert h.category("新业务/估值").status_code == 201
    with h.factory() as db:
        assert docs._policy(db, f"document-taxonomy:{h.space}")
        assert not docs._policy(db, f"wiki-taxonomy:{h.space}")
    h.login(h.outsider)
    assert h.call("GET", f"/documents/taxonomy?space_id={h.space}").status_code == 404


def test_document_list_contains_authorized_version_metadata_without_bodies(h):
    response = h.call("GET", f"/documents?space_id={h.space}")
    assert response.status_code == 200, response.text
    document = next(row for row in response.json()["items"] if row["id"] == h.doc.id)
    assert document["latest_version_id"] == h.doc.version_id
    assert document["latest_version_revision"] == h.doc.version_revision
    assert document["latest_version_no"] == 1 and document["latest_state"] == "DRAFT"
    assert "blocks" not in document and "content" not in document
    h.login(h.outsider)
    assert h.call("GET", f"/documents?space_id={h.space}").status_code == 404


def test_wrapper_csrf_etag_schema_idempotency_and_revoked_replay(h):
    body = {"space_id": h.space, "path": "并发类别"}
    assert h.call("POST", "/documents/categories", body, 0, headers={"X-CSRF-Token": ""}).status_code == 403
    assert h.call("POST", "/documents/categories", {**body, "unexpected": True}, 0).status_code == 422
    assert h.call("POST", "/documents/categories", body).status_code in {422, 428}
    key = str(uuid4())
    first = h.call("POST", "/documents/categories", body, 0, key)
    assert first.status_code == 201, first.text
    assert h.call("POST", "/documents/categories", body, 0, key).json() == first.json()
    assert h.category("另一类别", etag=0).status_code == 412
    with h.factory.begin() as db:
        db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == h.space, m.SpaceMember.user_id == h.owner,
                                               m.SpaceMember.role == "admin"))
    assert h.call("POST", "/documents/categories", body, 0, key).status_code == 403


@pytest.mark.parametrize("path", ["", "A//B", "../B", "A\\B", "A/%", "A/_", "A/\x00B", "/A", "A/", "A/B/C/D/E/F/G"])
def test_category_invalid_paths(h, path):
    result = h.call("POST", "/documents/categories", {"space_id": h.space, "path": path}, 0)
    assert result.status_code == 422


def test_rename_subtree_retained_docs_and_only_empty_delete(h):
    source = h.source("业务/估值/债券")
    retained = h.source("业务/估值/历史", deleted=True)
    wiki = h.source("业务/估值/知识", kind="knowledge")
    assert h.category("业务/估值/空").status_code == 201
    renamed = h.category("业务/估值", "PATCH", "运营/估值")
    assert renamed.status_code == 200, renamed.text
    with h.factory() as db:
        assert db.get(m.Resource, source.id).category == "运营/估值/债券"
        assert db.get(m.Resource, retained.id).category == "运营/估值/历史"
        assert db.get(m.Resource, wiki.id).category == "业务/估值/知识"
    assert h.category("运营/估值", "DELETE").status_code == 409
    assert h.category("运营/估值/空", "DELETE").status_code == 200
    assert h.category("内部指引", "DELETE").status_code == 409
    rows = h.call("GET", f"/documents?space_id={h.space}&category=运营").json()["items"]
    assert [row["id"] for row in rows] == [source.id]


def test_hidden_retained_blocks_empty_delete_and_atomic_rename(h):
    assert h.category("受控/子类").status_code == 201
    visible = h.source("受控/子类")
    h.source("受控/秘密", owner=h.peer, restricted=True, deleted=True)
    revision = h.taxonomy().headers["etag"]
    result = h.category("受控", "PATCH", "改名", etag=revision)
    assert result.status_code == 409
    assert "秘密" not in result.text
    assert h.taxonomy().headers["etag"] == revision
    with h.factory() as db:
        assert db.get(m.Resource, visible.id).category == "受控/子类"
    assert h.category("受控", "DELETE").status_code == 409


def test_atomic_move_all_permissions_etags_and_replay(h):
    assert h.category("目标").status_code == 201
    other = h.source()
    hidden = h.source(owner=h.peer, restricted=True)
    body = {"space_id": h.space, "category": "目标", "items": [{"id": h.doc.id, "revision": 1}, {"id": hidden.id, "revision": 1}]}
    assert h.call("POST", "/documents/classification-moves", body, 1).status_code == 404
    with h.factory() as db:
        assert db.get(m.Resource, h.doc.id).category == "内部指引"
    body["items"][1] = {"id": other.id, "revision": 99}
    assert h.call("POST", "/documents/classification-moves", body, 1).status_code == 412
    body["items"][1]["revision"] = 1
    key = str(uuid4())
    result = h.call("POST", "/documents/classification-moves", body, 1, key)
    assert result.status_code == 200, result.text
    assert len(result.json()["items"]) == 2
    assert h.call("POST", "/documents/classification-moves", body, 1, key).json() == result.json()
    body["items"] = [body["items"][0]] * 2
    assert h.call("POST", "/documents/classification-moves", body, 1).status_code == 422


def test_upload_creation_checks_category_and_revision(h):
    result = h.call("POST", "/documents", {"space_id": h.space, "name": "合成上传.txt", "category": "内部指引"}, 0)
    assert result.status_code == 201, result.text
    assert result.json()["category"] == "内部指引" and result.json()["kind"] == "document"
    assert h.category("新增分类").status_code == 201
    assert h.call("POST", "/documents", {"space_id": h.space, "name": "合成上传.txt", "category": "内部指引"}, 0).status_code == 412
    assert h.call("POST", "/documents", {"space_id": h.space, "name": "合成上传.txt", "category": "不存在"}, 1).status_code == 409


def test_real_dispatcher_suggestion_review_apply_once_immutable_source(h):
    h.app.state.job_dispatcher = h.dispatcher.run
    with h.factory() as db:
        before = svc.version_dict(db, db.get(m.ResourceVersion, h.doc.version_id))
    queued = h.queue()
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["id"]
    response = h.call("GET", f"/document-normalizations/{job_id}")
    assert response.status_code == 200, response.text
    assert response.json()["job"]["state"] == "SUCCEEDED"
    assert response.json()["suggestion"]["gaps"]
    assert len(h.calls) == 1
    key = str(uuid4())
    applied = h.apply(job_id, response, key=key)
    assert applied.status_code == 201, applied.text
    assert h.apply(job_id, response, key=key).json() == applied.json()
    assert h.apply(job_id, response).status_code == 412
    fresh = h.call("GET", f"/document-normalizations/{job_id}")
    assert h.apply(job_id, fresh).status_code == 409
    with h.factory() as db:
        after = svc.version_dict(db, db.get(m.ResourceVersion, h.doc.version_id))
        assert before == after
        version = db.get(m.ResourceVersion, applied.json()["version_id"])
        assert version.state == "DRAFT" and version.origin == "AI_DRAFT" and not version.source_verified
        assert version.source_blob_id is None and version.legal_status == "UNKNOWN"
        assert db.get(m.Blob, h.doc.blob_id).sha256 == h.original_hash
        assert len(list(db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == version.id)))) == 1
        receipt = docs._policy(db, f"document-guidance-suggestion:{job_id}")
        assert receipt.config["original_suggestion"]["title"] == "合成规范化指引"
        assert receipt.config["review"]["accepted_content"]["title"] == "经人工预览的合成指引"
        assert docs._policy(db, f"wiki-provenance:{version.resource_id}")
    assert h.call("GET", f'/versions/{applied.json()["version_id"]}').status_code == 200
    original = h.call("GET", f"/versions/{h.doc.version_id}/content?representation=source&download=true")
    assert original.status_code == 200 and original.content == h.original


@pytest.mark.parametrize("change,expected", [("source", "GUIDANCE_SOURCE_CHANGED"), ("acl", "NOT_FOUND"),
    ("model", "CONNECTION_REVISION_CHANGED"), ("inactive", "GUIDANCE_OWNER_UNAVAILABLE"),
    ("cancel", "GUIDANCE_CANCELLED"), ("lease", "GUIDANCE_LEASE_EXPIRED")])
def test_reauthorize_after_model_response_no_receipt_or_draft(h, change, expected):
    queued = h.queue(); job_id = queued.json()["id"]
    def revoke():
        with h.factory.begin() as db:
            if change == "source":
                version = db.get(m.ResourceVersion, h.doc.version_id); version.title = "并发修改标题"
            elif change == "acl":
                db.execute(delete(m.SpaceMember).where(m.SpaceMember.user_id == h.owner))
            elif change == "model":
                policy = db.get(m.RuntimePolicy, h.connection); policy.config = {**policy.config, "name": "变更"}
            elif change == "inactive":
                db.get(m.User, h.owner).active = False
            elif change == "cancel":
                db.get(m.Job, job_id).cancel_requested = True
            else:
                db.get(m.Job, job_id).lease_until = svc.now() - timedelta(seconds=1)
    h.behavior = revoke
    h.dispatcher.run(job_id)
    with h.factory() as db:
        job = db.get(m.Job, job_id)
        assert job.state != "SUCCEEDED"
        if change != "lease":
            assert job.error_code == expected or change == "cancel" and job.state == "CANCELLED", svc.job_dict(job)
        assert not docs._policy(db, f"document-guidance-suggestion:{job_id}")
        assert not db.scalar(select(m.Resource.id).where(m.Resource.kind == "knowledge"))


@pytest.mark.parametrize("mode", ["unowned", "other_owner", "disabled", "no_transfer", "no_models", "wrong_category", "bad_block_hash", "limit"])
def test_prepare_blocks_missing_configuration_and_bad_sources_without_model_call(h, mode):
    with h.factory.begin() as db:
        policy = db.get(m.RuntimePolicy, h.connection)
        if mode in {"unowned", "other_owner", "disabled", "no_transfer", "no_models"}:
            config = dict(policy.config)
            if mode == "unowned": config.pop("owner_user_id")
            if mode == "other_owner": config["owner_user_id"] = h.peer
            if mode == "disabled": config["enabled"] = False
            if mode == "no_transfer": config["allow_document_transfer"] = False
            if mode == "no_models": config["custom_models"] = []
            policy.config = config
        elif mode == "wrong_category": db.get(m.Resource, h.doc.id).category = "未分类"
        else:
            block = db.get(m.ContentBlock, (h.doc.version_id, h.doc.block_id))
            if mode == "bad_block_hash": block.search_text = "被篡改"
            else:
                block.data = {"text": "大" * 42000}; block.search_text = "大" * 42000
                block.content_sha256 = text_sha256(block.search_text)
    result = h.queue()
    assert result.status_code in {404, 409, 422}, result.text
    assert not h.calls
    with h.factory() as db:
        assert not db.scalar(select(m.Job.id))


@pytest.mark.parametrize("malformed", [
    {"title": "建议", "blocks": [{"markdown": "正文", "evidence_ids": ["S99"]}], "gaps": []},
    {"title": "建议", "blocks": [{"markdown": "正文", "evidence_ids": []}], "gaps": []},
    {"title": "建议", "blocks": [{"markdown": "<script>bad</script>", "evidence_ids": ["S1"]}], "gaps": []},
    {"title": "建议", "blocks": [], "gaps": [], "publish": True},
    'not-json', '{"title":"a","title":"b","blocks":[],"gaps":[]}', "x" * 50000,
])
def test_strict_model_output_rejected(h, malformed):
    content = json.dumps(malformed) if isinstance(malformed, dict) else malformed
    h.behavior = lambda: {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    job = h.run(h.queue())
    assert job["state"] == "FAILED" and job["error_code"].startswith("GUIDANCE_"), job
    with h.factory() as db:
        assert not docs._policy(db, f'document-guidance-suggestion:{job["id"]}')


def test_revoked_result_and_apply_replay_denied_even_after_citations_removed(h):
    job_id, response = h.ready()
    key = str(uuid4()); result = h.apply(job_id, response, key=key)
    assert result.status_code == 201, result.text
    version_id = result.json()["version_id"]
    with h.factory.begin() as db:
        db.execute(delete(m.EvidenceLink).where(m.EvidenceLink.from_version_id == version_id))
        db.get(m.Resource, h.doc.id).restricted = True
    assert h.call("GET", f"/versions/{version_id}").status_code == 404
    assert h.call("GET", f"/document-normalizations/{job_id}").status_code == 404
    assert h.call("GET", f"/jobs/{job_id}").status_code == 404
    assert h.apply(job_id, response, key=key).status_code == 404
    jobs = h.call("GET", "/jobs").json()["items"]
    assert job_id not in {job["id"] for job in jobs}


@pytest.mark.parametrize("kind", ["personal", "team"])
def test_governed_permission_matrix(h, kind):
    with h.factory.begin() as db:
        db.add(m.RuntimePolicy(id=str(uuid4()), name=f"space-governance:{h.space}", updated_by=h.owner,
            config={"schema_version": 1, "space_id": h.space, "kind": kind, "owner_id": h.owner}))
    h.login(h.owner); assert h.taxonomy().json()["total_visible"] == 1
    for who in (h.peer, h.reader, h.outsider, h.admin):
        h.login(who)
        result = h.call("GET", f"/documents/taxonomy?space_id={h.space}")
        assert result.status_code == (404 if kind == "personal" else 200)
        if kind == "team":
            assert result.json()["total_visible"] == (1 if who in {h.peer, h.admin} else 0)
        assert h.queue().status_code in {403, 404}


def test_receipt_recovers_without_second_model_call(h):
    job_id, _ = h.ready()
    with h.factory.begin() as db:
        job = db.get(m.Job, job_id); job.state = "RUNNING"; job.attempts = 2
        job.lease_until = svc.now() + timedelta(seconds=120)
    result = docs.execute_guidance_normalize(h.app.state.settings, h.factory, job_id, 2, lambda *_: None)
    assert result["suggestion_id"] == job_id and len(h.calls) == 1


def test_concurrent_taxonomy_cas_one_winner(h):
    # Two independent DB transactions race the first taxonomy revision.
    def worker(path):
        with h.factory() as db:
            user = db.get(m.User, h.owner)
            request = SimpleNamespace(headers={"if-match": '"0"'}, state=SimpleNamespace(trace_id=str(uuid4())))
            ctx = svc.Context(request, db, user, {"space_id": h.space, "path": path}, {}, "createDocumentCategory")
            try:
                docs.mutate_category(ctx, "create"); db.commit(); return "OK"
            except svc.APIError as exc:
                db.rollback(); return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(worker, ["并发A", "并发B"]))
    assert sorted(result) == ["OK", "REVISION_CONFLICT"]


def test_real_native_adapter_with_mock_http_transport(h, monkeypatch):
    requests = []
    monkeypatch.setattr(providers, "_dns_addresses", lambda *_: ["127.0.0.1"])
    def transport(request):
        assert not h.open_transactions, "Native HTTP adapter called while DB transaction is open"
        assert request.url.path == "/api/chat" and "authorization" not in request.headers
        payload = json.loads(request.content)
        assert payload["model"] == "synthetic" and payload["options"]["num_predict"] == 4096
        requests.append(payload)
        content = {"title": "原生adapter合成建议", "blocks": [{"markdown": "核对业务日期并记录差异。", "evidence_ids": ["S1"]}], "gaps": []}
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(content)},
            "done": True, "done_reason": "stop", "prompt_eval_count": 10, "eval_count": 20})
    def native(snapshot, messages, **kwargs):
        assert snapshot["max_response_bytes"] <= 65536 and snapshot["max_request_bytes"] <= 67584
        return REAL_COMPLETE(snapshot, messages, **kwargs, transport=httpx.MockTransport(transport))
    monkeypatch.setattr(providers, "complete", native)
    job = h.run(h.queue())
    assert job["state"] == "SUCCEEDED", job
    assert len(requests) == 1


def test_changed_model_before_execution_blocks_without_call(h):
    queued = h.queue()
    with h.factory.begin() as db:
        policy = db.get(m.RuntimePolicy, h.connection)
        policy.config = {**policy.config, "name": "执行前变更"}
    job = h.run(queued)
    assert job["state"] == "FAILED" and job["error_code"] == "CONNECTION_REVISION_CHANGED"
    assert not h.calls


def test_concurrent_apply_one_new_draft(h):
    job_id, response = h.ready()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: h.apply(job_id, response).status_code, range(2)))
    assert sorted(results) == [201, 412]
    with h.factory() as db:
        assert len(list(db.scalars(select(m.Resource.id).where(m.Resource.kind == "knowledge")))) == 1


def test_target_hidden_retained_category_cannot_be_merged_by_rename(h):
    assert h.category("业务源").status_code == 201
    source = h.source("业务源")
    h.source("目标保留/秘密", restricted=True, owner=h.peer, deleted=True)
    response = h.category("业务源", "PATCH", "目标保留")
    assert response.status_code == 409 and "秘密" not in response.text
    with h.factory() as db:
        assert db.get(m.Resource, source.id).category == "业务源"


def test_apply_rejects_unreviewed_and_invented_citation(h):
    job_id, response = h.ready()
    body = {"reviewed": False, "title": "合成采纳", "blocks": response.json()["suggestion"]["blocks"], "review_note": "尚未预览"}
    assert h.call("POST", f"/document-normalizations/{job_id}/apply", body, response.headers["etag"]).status_code == 422
    body["reviewed"] = True
    body["blocks"][0]["evidence_ids"] = ["S9"]
    assert h.call("POST", f"/document-normalizations/{job_id}/apply", body, response.headers["etag"]).status_code == 422
    with h.factory() as db:
        assert not db.scalar(select(m.Resource.id).where(m.Resource.kind == "knowledge"))


def test_env_allowlist_revocation_blocks_apply_without_reading_value(h, monkeypatch):
    from fund_kb.codex_bridge_config import configure_model_policy
    name = "FKB_DOCUMENT_SYNTHETIC_KEY"
    class Environment(dict):
        forbidden = False
        def get(self, key, default=None):
            assert not self.forbidden, "Revoked environment value must never be read"
            return super().get(key, default)
        def __getitem__(self, key):
            assert not self.forbidden, "Revoked environment value must never be read"
            return super().__getitem__(key)
    environment = Environment({name: "synthetic-document-test-key"})
    monkeypatch.setattr(providers, "os", SimpleNamespace(environ=environment))
    configure_model_policy(h.app.state.settings, env_refs_by_user={h.owner: [name]})
    with h.factory.begin() as db:
        policy = db.get(m.RuntimePolicy, h.connection)
        policy.config = {**policy.config, "credential_mode": "env", "api_key_env": name}
    job_id, response = h.ready()
    with h.factory() as db:
        revision = db.get(m.RuntimePolicy, h.connection).revision
    configure_model_policy(h.app.state.settings, env_refs_by_user={})
    environment.forbidden = True
    result = h.apply(job_id, response)
    assert result.status_code == 403 and result.json()["code"] == "CREDENTIAL_REFERENCE_FORBIDDEN"
    with h.factory() as db:
        assert db.get(m.RuntimePolicy, h.connection).revision == revision
        assert not db.scalar(select(m.Resource.id).where(m.Resource.kind == "knowledge"))


def test_uploaded_draft_can_edit_online_without_changing_original_file(h):
    response = h.call("GET", f"/versions/{h.doc.version_id}")
    version = response.json()
    body = {key: version[key] for key in ("title", "knowledge_type", "blocks", "applicability", "required_facts",
                                         "legal_status", "valid_from", "valid_to")}
    body["blocks"][0]["data"] = {"text": "**在线编辑的正文**", "text_format": "markdown"}
    result = h.call("PATCH", f"/versions/{h.doc.version_id}", body, response.headers["etag"])
    assert result.status_code == 200, result.text
    assert result.json()["origin"] == "HUMAN"
    assert result.json()["source_verified"] is False
    assert h.call("GET", f"/versions/{h.doc.version_id}/content?representation=source").content == h.original
    preview = h.call("GET", f"/versions/{h.doc.version_id}/content?representation=preview")
    assert "<strong>在线编辑的正文</strong>" in preview.text
    with h.factory() as db:
        assert db.get(m.ResourceVersion, h.doc.version_id).source_blob_id == h.doc.blob_id
        assert db.get(m.Blob, h.doc.blob_id).sha256 == h.original_hash


def online_draft(h):
    current = h.call("GET", f"/versions/{h.doc.version_id}")
    if current.json()["state"] == "DRAFT":
        submitted = h.call("POST", f"/versions/{h.doc.version_id}/submit", etag=current.headers["etag"])
        assert submitted.status_code == 200, submitted.text
    result = h.call("POST", f"/resources/{h.doc.id}/versions", {
        "title": "线上修订验收", "change_reason": "修正排版并保留原件", "base_version_id": h.doc.version_id})
    assert result.status_code == 201, result.text
    assert result.json()["origin"] == "COPY"
    assert result.json()["base_version_id"] == h.doc.version_id
    return result


def online_body(response):
    return {key: copy.deepcopy(response.json()[key]) for key in ("title", "knowledge_type", "blocks",
        "applicability", "required_facts", "legal_status", "valid_from", "valid_to", "source_url")}


def test_document_online_revision_saves_renders_and_retains_raw_snapshot(h):
    draft = online_draft(h)
    original = h.call("GET", f"/versions/{h.doc.version_id}").json()
    body = online_body(draft)
    body["blocks"][0]["data"] = {"text": "## 线上核对\n**加粗核对项**与新增说明。", "text_format": "markdown"}
    saved = h.call("PATCH", f"/versions/{draft.json()['id']}", body, draft.headers["etag"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["source_verified"] is False and saved.json()["content_sha256"] is None
    assert saved.json()["blocks"][0]["locator"] == {"label": "线上修订内容", "base_version_id": h.doc.version_id}
    reloaded = h.call("GET", f"/versions/{draft.json()['id']}")
    assert reloaded.json()["blocks"] == saved.json()["blocks"]
    h.app.state.storage.write_bytes(f"previews/{draft.json()['id']}.html", b"STALE_ORIGINAL_PREVIEW")
    preview = h.call("GET", f"/versions/{draft.json()['id']}/content?representation=preview")
    assert preview.status_code == 200 and "<strong>加粗核对项</strong>" in preview.text
    assert "STALE_ORIGINAL_PREVIEW" not in preview.text
    assert "sandbox" in preview.headers["content-security-policy"]
    raw = h.call("GET", f"/versions/{draft.json()['id']}/content?representation=source")
    assert raw.content == h.original
    assert h.call("GET", f"/versions/{h.doc.version_id}").json() == original
    conflict = h.call("PATCH", f"/versions/{draft.json()['id']}", body, draft.headers["etag"])
    assert conflict.status_code == 412
    with h.factory() as db:
        assert db.get(m.ResourceVersion, draft.json()["id"]).source_blob_id == h.doc.blob_id
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == "version.edited",
            m.AuditEvent.object_id == draft.json()["id"]))


def test_online_document_hundred_blocks_roundtrip_without_duplicate_drafts(h):
    with h.factory.begin() as db:
        for ordinal in range(1, 100):
            text = f"合成第{ordinal + 1}段"
            db.add(m.ContentBlock(version_id=h.doc.version_id, block_id=str(uuid4()), ordinal=ordinal,
                block_type="paragraph", data={"text": text}, locator={"source_page": ordinal},
                search_text=text, content_sha256=text_sha256(text)))
    draft = online_draft(h)
    assert len(draft.json()["blocks"]) == 100
    body = online_body(draft)
    body["blocks"][49]["data"] = {"text": "**第50段的富文本修改**", "text_format": "markdown"}
    saved = h.call("PATCH", f"/versions/{draft.json()['id']}", body, draft.headers["etag"])
    assert saved.status_code == 200, saved.text
    assert len(saved.json()["blocks"]) == 100
    assert saved.json()["blocks"][48] == draft.json()["blocks"][48]
    assert saved.json()["blocks"][49]["locator"]["label"] == "线上修订内容"
    assert [b["block_id"] for b in saved.json()["blocks"]] == [b["block_id"] for b in draft.json()["blocks"]]
    duplicate = h.call("POST", f"/resources/{h.doc.id}/versions", {
        "title": "重复草稿", "change_reason": "测试", "base_version_id": h.doc.version_id})
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "DRAFT_EXISTS"


def test_online_document_keeps_editor_access_and_frozen_version_guards(h):
    draft = online_draft(h)
    body = online_body(draft)
    for who, expected in [(h.reader, 403), (h.outsider, 404)]:
        h.login(who)
        result = h.call("PATCH", f"/versions/{draft.json()['id']}", body, draft.headers["etag"])
        assert result.status_code in {expected, 404}, result.text
    h.login()
    submitted = h.call("POST", f"/versions/{draft.json()['id']}/submit", etag=draft.headers["etag"])
    assert submitted.status_code == 200, submitted.text
    result = h.call("PATCH", f"/versions/{draft.json()['id']}", body, submitted.headers["etag"])
    assert result.status_code == 409 and result.json()["code"] == "VERSION_FROZEN"


def test_preview_fallback_only_uses_authorized_clean_nonempty_blocks(h):
    preview = h.call("GET", f"/versions/{h.doc.version_id}/content?representation=preview")
    assert preview.status_code == 200 and h.original.decode() in preview.text
    h.login(h.outsider)
    assert h.call("GET", f"/versions/{h.doc.version_id}/content?representation=preview").status_code == 404
    h.login()
    with h.factory.begin() as db:
        db.get(m.Blob, h.doc.blob_id).scan_state = "QUARANTINED"
    assert h.call("GET", f"/versions/{h.doc.version_id}/content?representation=preview").status_code == 409
    with h.factory.begin() as db:
        db.get(m.Blob, h.doc.blob_id).scan_state = "CLEAN"
        db.execute(delete(m.ContentBlock).where(m.ContentBlock.version_id == h.doc.version_id))
    result = h.call("GET", f"/versions/{h.doc.version_id}/content?representation=preview")
    assert result.status_code == 409 and result.json()["code"] == "PREVIEW_NOT_READY"


def test_active_parsing_blocks_online_edit(h):
    current = h.call("GET", f"/versions/{h.doc.version_id}")
    with h.factory.begin() as db:
        db.add(m.Job(id=str(uuid4()), kind="SCAN_PARSE", state="RUNNING", stage="parse", payload={},
            dedupe_key=str(uuid4()), resource_id=h.doc.id, version_id=h.doc.version_id, owner_id=h.owner))
    result = h.call("PATCH", f"/versions/{h.doc.version_id}", online_body(current), current.headers["etag"])
    assert result.status_code == 409 and result.json()["code"] == "PARSING_IN_PROGRESS"
