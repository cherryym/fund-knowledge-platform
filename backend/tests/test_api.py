"""Real SQLAlchemy/HTTP integration tests. Model calls are disabled; jobs use pure local adapters."""
import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fund_kb import models as m
from fund_kb.api import create_app
from fund_kb.services import check_frozen_hash, eligible_evidence, uid
from fund_kb.settings import Settings

EDITOR = "00000000-0000-4000-8000-000000000001"
REVIEWER = "00000000-0000-4000-8000-000000000002"
READER = "00000000-0000-4000-8000-000000000003"
OUTSIDER = "00000000-0000-4000-8000-000000000004"
SPACE = "00000000-0000-4000-8000-000000000101"


class API:
    def __init__(self, app, client):
        self.app, self.client, self.csrf = app, client, None

    def login(self, who=EDITOR):
        r = self.client.post("/api/v1/auth/demo", json={"user_id": who}, headers={"Origin": "http://testserver"})
        assert r.status_code == 200, r.text
        self.csrf = r.json()["csrf_token"]
        return r

    def call(self, method, path, data=None, etag=None, key=None, headers=None, raw=None):
        h = {"Origin": "http://testserver", "X-CSRF-Token": self.csrf or "", "Idempotency-Key": key or str(uuid4())}
        if etag is not None:
            h["If-Match"] = etag
        h.update(headers or {})
        kwargs = {"headers": h}
        if raw is not None:
            kwargs["content"] = raw
        elif data is not None:
            kwargs["json"] = data
        return self.client.request(method, "/api/v1" + path, **kwargs)

    def resource(self, kind="knowledge", name="测试知识"):
        r = self.call("POST", "/resources", {"space_id": SPACE, "kind": kind, "name": name})
        assert r.status_code == 201, r.text
        return r.json()

    def draft(self, resource, base=None):
        body = {"title": resource["name"], "change_reason": "测试版本"}
        if base:
            body["base_version_id"] = base
        r = self.call("POST", f"/resources/{resource['id']}/versions", body)
        assert r.status_code == 201, r.text
        return r

    def edit(self, version, *, text="核对业务日期和来源版本，记录差异。", citations=None, required=None,
        valid_from="2026-01-01", valid_to=None, legal_status="NOT_APPLICABLE", applicability=None):
        data = {"title": "测试知识", "knowledge_type": version.json()["knowledge_type"],
            "applicability": applicability or {}, "required_facts": required or [], "legal_status": legal_status,
            "valid_from": valid_from, "valid_to": valid_to,
            "blocks": [{"block_id": str(uuid4()), "ordinal": 0, "block_type": "paragraph",
                "data": {"text": text}, "locator": {"label": "测试条款"}, "citations": citations or []}]}
        r = self.call("PATCH", f"/versions/{version.json()['id']}", data, version.headers["etag"])
        assert r.status_code == 200, r.text
        return r

    def approve(self, version):
        submitted = self.call("POST", f"/versions/{version.json()['id']}/submit", etag=version.headers["etag"])
        assert submitted.status_code == 200, submitted.text
        self.login(REVIEWER)
        body = {"decision": "APPROVE", "reviewed_sha256": submitted.json()["content_sha256"], "comment": "独立复核"}
        if submitted.json()["knowledge_type"] == "source":
            body["source_verified"] = True
        reviewed = self.call("POST", f"/versions/{version.json()['id']}/reviews", body, submitted.headers["etag"])
        assert reviewed.status_code == 201, reviewed.text
        return self.call("GET", f"/versions/{version.json()['id']}")

    def publish(self, version):
        v = self.approve(version)
        pub = self.call("POST", f"/versions/{v.json()['id']}/publish", etag=v.headers["etag"])
        assert pub.status_code == 202, pub.text
        done = self.call("GET", f"/jobs/{pub.json()['id']}")
        assert done.status_code == 200, done.text
        assert done.json()["state"] == "SUCCEEDED", done.text
        return self.call("GET", f"/versions/{v.json()['id']}")


@pytest.fixture
def api(tmp_path):
    settings = Settings(app_env="development", auth_mode="demo", storage_dir=tmp_path / "objects",
        database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}", allowed_origins=["http://testserver"],
        qdrant_path=tmp_path / "vectors", llm_provider="evidence")
    app = create_app(settings)
    app.state.raise_test_errors = True
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            for who, subject in ((EDITOR, "demo:admin"), (REVIEWER, "demo:2"), (READER, "demo:3"), (OUTSIDER, "demo:4")):
                db.add(m.User(id=who, external_subject=subject, display_name=subject, active=True))
            db.add(m.Space(id=SPACE, name="测试空间"))
            db.flush()
            for who, rr in ((EDITOR, ["reader", "editor", "admin"]), (REVIEWER, ["reader", "reviewer", "publisher"]), (READER, ["reader"])):
                for role in rr:
                    db.add(m.SpaceMember(space_id=SPACE, user_id=who, role=role))
            db.commit()
        api = API(app, client)
        api.login()
        yield api


@pytest.fixture
def jobs(api):
    import traceback

    from fund_kb.jobs import JobDispatcher
    from fund_kb.retrieval import VectorIndex
    vector = VectorIndex(api.app.state.settings)
    api.app.state.vector_index = vector
    dispatcher = JobDispatcher(api.app.state.settings, api.app.state.session_factory, vector)
    original_failure = dispatcher._fail
    def report_synthetic_failure(job_id, attempt, exc):
        # Only synthetic fixtures: expose worker failures to pytest instead of an opaque INTERNAL_ERROR.
        traceback.print_exception(exc)
        return original_failure(job_id, attempt, exc)
    dispatcher._fail = report_synthetic_failure
    api.app.state.job_dispatcher = dispatcher.run
    yield dispatcher
    dispatcher.close()
    vector.close()


def test_exact_61_operations_and_unconfigured_oidc(api):
    spec = api.app.openapi()
    expected = {v["operationId"] for item in spec["paths"].values() for k, v in item.items() if k in {"get", "put", "post", "patch", "delete"}}
    actual = {route.operation_id for route in api.app.routes if getattr(route, "operation_id", None)}
    assert expected == actual
    extensions = {module.__name__ for module in api.app.state.extension_modules}
    core = {f"fund_kb.api_{name}" for name in ("models", "wiki", "libraries", "retention", "documents")}
    assert core <= extensions <= core | {"fund_kb.api_oauth", "fund_kb.api_local_wiki", "fund_kb.api_admin_review",
        "fund_kb.api_wiki_reader", "fund_kb.api_wiki_maintenance", "fund_kb.api_retrieval", "fund_kb.api_source_authority",
        "fund_kb.api_capabilities", "fund_kb.api_agent_access"}
    assert len(actual) == 61 + sum(len(module.HANDLERS) for module in api.app.state.extension_modules)
    assert api.call("GET", "/health").status_code == 200
    assert api.call("GET", "/auth/login").json()["code"] == "OIDC_NOT_CONFIGURED"
    assert api.call("GET", "/auth/callback?code=fake&state=fake").status_code == 503
    assert api.call("GET", "/me").json()["id"] == EDITOR
    assert api.call("GET", "/auth/demo").status_code == 200
    assert api.call("POST", "/auth/logout").status_code == 204
    assert api.call("GET", "/me").status_code == 401


def test_csrf_origin_and_idempotency_are_required(api):
    payload = {"space_id": SPACE, "kind": "knowledge", "name": "测试"}
    assert api.call("POST", "/resources", payload, headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert api.call("POST", "/resources", payload, headers={"Origin": "https://untrusted.invalid"}).status_code == 403
    assert api.call("POST", "/resources", payload, headers={"Idempotency-Key": ""}).status_code in {400, 422}
    key = str(uuid4())
    first = api.call("POST", "/resources", payload, key=key)
    second = api.call("POST", "/resources", payload, key=key)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert api.call("POST", "/resources", {**payload, "name": "其他"}, key=key).status_code == 409
    with api.app.state.session_factory() as db:
        assert len(db.scalars(select(m.Resource)).all()) == 1


def test_space_member_etag_last_admin_and_deployment_roles(api):
    assert api.call("GET", "/spaces").json()[0]["id"] == SPACE
    new = api.call("POST", "/spaces", {"name": "临时空间"})
    assert new.status_code == 201
    changed = api.call("PATCH", f"/spaces/{new.json()['id']}", {"name": "修改空间"}, new.headers["etag"])
    assert changed.status_code == 200
    assert api.call("DELETE", f"/spaces/{new.json()['id']}", etag=changed.headers["etag"]).status_code == 204
    members = api.call("GET", f"/spaces/{SPACE}/members")
    assert api.call("PUT", f"/spaces/{SPACE}/members", [{"user_id": EDITOR, "roles": ["editor"]}], members.headers["etag"]).status_code == 409
    same = api.call("PUT", f"/spaces/{SPACE}/members", members.json(), members.headers["etag"])
    assert same.status_code == 200 and same.headers["etag"] != members.headers["etag"]
    api.login(REVIEWER)
    assert api.call("POST", "/spaces", {"name": "越权"}).status_code == 403


def test_resource_visibility_etag_trash_restore(api):
    r = api.resource()
    initial = api.call("GET", f"/resources/{r['id']}")
    assert api.call("PATCH", f"/resources/{r['id']}", {"name": "新名"}).status_code == 428
    changed = api.call("PATCH", f"/resources/{r['id']}", {"name": "新名"}, initial.headers["etag"])
    assert changed.status_code == 200
    assert api.call("PATCH", f"/resources/{r['id']}", {"name": "旧请求"}, initial.headers["etag"]).status_code == 412
    api.login(READER)
    assert api.call("GET", f"/resources/{r['id']}").status_code == 404
    assert api.call("POST", "/resources", {"space_id": SPACE, "kind": "knowledge", "name": "越权"}).status_code == 403
    api.login()
    assert api.call("DELETE", f"/resources/{r['id']}", etag=changed.headers["etag"]).status_code == 204
    assert api.call("GET", f"/resources/{r['id']}").status_code == 404
    trash = api.call("GET", f"/resources?space_id={SPACE}&trash=true").json()["items"][0]
    restored = api.call("POST", f"/resources/{r['id']}/restore", etag=f'"{trash["revision"]}"')
    assert restored.status_code == 200 and restored.json()["suspended"] is True
    with api.app.state.session_factory() as db:
        assert len(db.scalars(select(m.Job).where(m.Job.kind == "INVALIDATE")).all()) == 2
        assert len(db.scalars(select(m.Outbox)).all()) == 2


def test_draft_freeze_self_review_hash_and_relations(api):
    r = api.resource()
    v = api.edit(api.draft(r))
    with api.app.state.session_factory() as db:
        block = db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == v.json()["id"])).first()
        assert block.content_sha256 == hashlib.sha256(block.search_text.encode()).hexdigest()
        db.add(m.SpaceMember(space_id=SPACE, user_id=EDITOR, role="reviewer"))
        db.commit()
    submitted = api.call("POST", f"/versions/{v.json()['id']}/submit", etag=v.headers["etag"])
    assert submitted.status_code == 200
    review = {"decision": "APPROVE", "reviewed_sha256": submitted.json()["content_sha256"], "comment": "自审"}
    assert api.call("POST", f"/versions/{v.json()['id']}/reviews", review, submitted.headers["etag"]).status_code == 403
    content = {k: v.json()[k] for k in ("title", "knowledge_type", "applicability", "required_facts", "legal_status", "valid_from", "valid_to", "blocks")}
    assert api.call("PATCH", f"/versions/{v.json()['id']}", content, submitted.headers["etag"]).status_code == 409
    assert api.call("PUT", f"/versions/{v.json()['id']}/relations", [], submitted.headers["etag"]).status_code == 409
    api.login(REVIEWER)
    bad = api.call("POST", f"/versions/{v.json()['id']}/reviews", {**review, "reviewed_sha256": "0" * 64}, submitted.headers["etag"])
    assert bad.status_code == 409
    assert api.call("POST", f"/versions/{v.json()['id']}/reviews", review, submitted.headers["etag"]).status_code == 201
    assert len(api.call("GET", f"/versions/{v.json()['id']}/reviews").json()) == 1
    assert api.call("GET", "/review-queue").status_code == 200


def test_internal_default_date_is_frozen_before_review(api):
    v = api.edit(api.draft(api.resource()), valid_from=None)
    submitted = api.call("POST", f"/versions/{v.json()['id']}/submit", etag=v.headers["etag"])
    assert submitted.status_code == 200
    assert submitted.json()["valid_from"] is not None
    with api.app.state.session_factory() as db:
        obj = db.get(m.ResourceVersion, v.json()["id"])
        assert obj.content_sha256 == check_frozen_hash(db, obj)


def test_draft_discard_and_schema_rejection(api):
    v = api.draft(api.resource())
    assert api.call("DELETE", f"/versions/{v.json()['id']}", etag=v.headers["etag"]).status_code == 204
    r = api.resource()
    v = api.draft(r)
    raw = {"title": "x", "knowledge_type": "faq", "applicability": {"or": []}, "required_facts": [],
        "legal_status": "NOT_APPLICABLE", "valid_from": None, "valid_to": None, "blocks": []}
    assert api.call("PATCH", f"/versions/{v.json()['id']}", raw, v.headers["etag"]).status_code == 422


def test_publish_job_and_current_read_are_persistent(api, jobs):
    r = api.resource()
    v = api.publish(api.edit(api.draft(r)))
    current = api.call("GET", f"/resources/{r['id']}").json()
    assert current["active_version_id"] == v.json()["id"]
    assert api.call("GET", f"/resources/{r['id']}/versions").json()["items"][0]["id"] == v.json()["id"]
    content = api.call("GET", f"/versions/{v.json()['id']}/content?representation=html")
    assert content.status_code == 200 and "no-store" in content.headers["cache-control"]
    assert api.call("GET", "/jobs").status_code == 200
    api.login(READER)
    assert api.call("GET", f"/versions/{v.json()['id']}").status_code == 200
    assert api.call("POST", f"/versions/{v.json()['id']}/publish", etag=v.headers["etag"]).status_code == 403


def test_upload_resume_integrity_complete_and_content(api, jobs):
    r = api.resource("document")
    v = api.draft(r)
    raw = "核对资料原件和业务日期。".encode()
    created = api.call("POST", "/uploads", {"version_id": v.json()["id"], "filename": "测试.txt",
        "size_bytes": len(raw), "expected_sha256": hashlib.sha256(raw).hexdigest()})
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    path = f"/uploads/{upload_id}/parts/1"
    part = api.call("PUT", path, raw=raw, headers={"Content-Type": "application/octet-stream"})
    assert part.status_code == 200, part.text
    assert api.call("PUT", path, raw=raw, headers={"Content-Type": "application/octet-stream"}).json() == part.json()
    assert api.call("PUT", path, raw=b"x" * len(raw), headers={"Content-Type": "application/octet-stream"}).status_code == 409
    assert api.call("GET", f"/uploads/{upload_id}").json()["completed_parts"] == [part.json()]
    complete = api.call("POST", f"/uploads/{upload_id}/complete", {"parts": [part.json()]})
    assert complete.status_code == 202, complete.text
    done = api.call("GET", f"/jobs/{complete.json()['id']}")
    assert done.json()["state"] == "SUCCEEDED", done.text
    metadata = api.call("GET", f"/versions/{v.json()['id']}").json()
    assert metadata["source_filename"].endswith(".txt") and metadata["mime_type"] == "text/plain"
    assert api.call("PUT", path, raw=raw, headers={"Content-Type": "application/octet-stream"}).status_code == 409
    preview = api.call("GET", f"/versions/{v.json()['id']}/content?representation=preview")
    assert preview.status_code == 200, preview.text
    source = api.call("GET", f"/versions/{v.json()['id']}/content?representation=source", headers={"Range": "bytes=0-2"})
    assert source.status_code == 206 and source.content == raw[:3]
    assert source.headers["content-disposition"].startswith("inline")
    download = api.call("GET", f"/versions/{v.json()['id']}/content?representation=source&download=true")
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["x-content-type-options"] == "nosniff" and "no-store" in download.headers["cache-control"]


def test_upload_cancel_closes_session(api):
    v = api.draft(api.resource("document"))
    u = api.call("POST", "/uploads", {"version_id": v.json()["id"], "filename": "x.txt", "size_bytes": 1}).json()
    assert api.call("DELETE", f"/uploads/{u['id']}").status_code == 204
    assert api.call("GET", f"/uploads/{u['id']}").json()["state"] == "CANCELLED"


def test_date_version_selection_future_and_cutoff(api, jobs):
    resource = api.resource()
    first = api.publish(api.edit(api.draft(resource), valid_from="2026-01-01"))
    api.login()
    newer = api.draft(resource, first.json()["id"])
    second = api.publish(api.edit(newer, text="未来版本", valid_from="2027-01-01", legal_status="FUTURE"))
    with api.app.state.session_factory() as db:
        reader = db.get(m.User, READER)
        old = eligible_evidence(db, reader, SPACE, {"business_date": "2026-06-01"})
        future = eligible_evidence(db, reader, SPACE, {"business_date": "2027-01-01"})
        before = eligible_evidence(db, reader, SPACE, {"business_date": "2026-06-01", "knowledge_cutoff": "2020-01-01T00:00:00Z"})
        assert {e["version_id"] for e in old} == {first.json()["id"]}
        assert {e["version_id"] for e in future} == {second.json()["id"]}
        assert before == []


def test_cross_space_acl_no_leak_and_idempotency_replay(api, jobs):
    r = api.resource()
    v = api.publish(api.edit(api.draft(r)))
    api.login()
    perm = api.call("GET", f"/resources/{r['id']}/permissions")
    new = {"restricted": True, "classification": "RESTRICTED", "grants": [
        {"user_id": EDITOR, "permission": "manage"}, {"user_id": EDITOR, "permission": "read"},
        {"user_id": EDITOR, "permission": "edit"}]}
    assert api.call("PUT", f"/resources/{r['id']}/permissions", new, perm.headers["etag"]).status_code == 200
    api.login(READER)
    assert api.call("GET", f"/resources/{r['id']}").status_code == 404
    assert api.call("GET", f"/versions/{v.json()['id']}").status_code == 404
    assert api.call("GET", f"/resources?space_id={SPACE}").json()["items"] == []
    result = api.call("POST", "/search", {"space_id": SPACE, "query": "核对"})
    assert result.status_code == 200 and result.json()["items"] == []
    api.login(OUTSIDER)
    assert api.call("GET", f"/resources?space_id={SPACE}").status_code == 404


def test_dependency_acl_revalidated_before_worker_invalidation(api, jobs):
    src = api.resource()
    sv = api.publish(api.edit(api.draft(src)))
    api.login()
    target = api.resource()
    draft = api.draft(target)
    linked = api.edit(draft, citations=[{"version_id": sv.json()["id"], "block_id": sv.json()["blocks"][0]["block_id"], "purpose": "INTERNAL_OPINION"}])
    edge = [{"target_resource_id": src["id"], "relation_type": "EXPLAINS", "conditions": {}}]
    linked = api.call("PUT", f"/versions/{linked.json()['id']}/relations", edge, linked.headers["etag"])
    published = api.publish(linked)
    assert api.call("GET", f"/versions/{published.json()['id']}/relations").json() == edge
    api.login()
    api.app.state.job_dispatcher = None
    source = api.call("GET", f"/resources/{src['id']}")
    assert api.call("DELETE", f"/resources/{src['id']}", etag=source.headers["etag"]).status_code == 204
    with api.app.state.session_factory() as db:
        assert eligible_evidence(db, db.get(m.User, READER), SPACE) == []
    api.login(READER)
    assert api.call("GET", f"/versions/{published.json()['id']}").status_code == 404


def test_consultation_private_parent_feedback_and_cases(api, jobs):
    api.publish(api.edit(api.draft(api.resource())))
    api.login(READER)
    thread = api.call("POST", "/threads", {"space_id": SPACE, "title": "私有咨询"}).json()
    run = api.call("POST", f"/threads/{thread['id']}/runs", {"question": "如何核对来源版本", "mode": "answer", "context": {}})
    assert run.status_code == 202, run.text
    completed = api.call("GET", f"/runs/{run.json()['id']}")
    assert completed.json()["state"] == "COMPLETED", completed.text
    assert api.call("GET", f"/runs/{run.json()['id']}/events").status_code == 200
    assert "summary" not in api.call("GET", f"/runs/{run.json()['id']}/events").text
    feedback = api.call("POST", f"/runs/{run.json()['id']}/feedback", {"kind": "WRONG", "comment": "请核对"})
    assert feedback.status_code == 201, feedback.text
    case_id = feedback.json()["case_id"]
    case = api.call("GET", f"/cases/{case_id}")
    assigned = api.call("PATCH", f"/cases/{case_id}", {"assignee_id": REVIEWER}, case.headers["etag"])
    assert assigned.status_code == 200, assigned.text
    parent = api.call("POST", f"/threads/{thread['id']}/runs", {"question": "补充日期", "mode": "answer",
        "context": {"business_date": "2026-06-01"}, "parent_run_id": run.json()["id"]})
    assert parent.status_code == 202
    assert len(api.call("GET", f"/threads/{thread['id']}").json()["items"]) == 2
    assert len(api.call("GET", "/threads").json()["items"]) == 1
    api.login(REVIEWER)
    assert api.call("GET", f"/runs/{run.json()['id']}").status_code == 404
    assert api.call("GET", f"/threads/{thread['id']}").status_code == 404
    assert api.call("PATCH", f"/cases/{case_id}", {"state": "RESOLVED", "resolution": "依据不足，应补齐证据"}, assigned.headers["etag"]).status_code == 200
    assert len(api.call("GET", "/cases").json()["items"]) == 1
    api.login(READER)
    assert api.call("GET", f"/runs/{run.json()['id']}").json()["answer"]["review_status"] != "EXPERT_REVIEWED"
    assert api.call("DELETE", f"/threads/{thread['id']}").status_code == 204
    assert api.call("GET", f"/threads/{thread['id']}").status_code == 404


def test_export_download_rechecks_revoked_sources(api, jobs):
    r = api.resource()
    v = api.publish(api.edit(api.draft(r)))
    api.login(READER)
    export = api.call("POST", "/exports", {"version_ids": [v.json()["id"]], "format": "markdown"})
    assert export.status_code == 202
    job_id = export.json()["id"]
    done = api.call("GET", f"/jobs/{job_id}")
    assert done.json()["state"] == "SUCCEEDED", done.text
    assert api.call("GET", f"/jobs/{job_id}/artifact").status_code == 200
    api.login()
    current = api.call("GET", f"/resources/{r['id']}")
    assert api.call("DELETE", f"/resources/{r['id']}", etag=current.headers["etag"]).status_code == 204
    api.login(READER)
    assert api.call("GET", f"/jobs/{job_id}/artifact").status_code == 404


def test_model_policy_requires_controlled_evaluation_and_audit(api):
    policy = api.call("GET", "/settings/model-policy")
    assert policy.status_code == 200
    candidate = {**policy.json(), "evaluation_id": "test-reviewed-policy"}
    assert api.call("PUT", "/settings/model-policy", candidate, policy.headers["etag"]).status_code == 409
    api.app.state.approved_model_policies = {"test-reviewed-policy": {k: value for k, value in candidate.items() if k != "evaluation_id"}}
    changed = api.call("PUT", "/settings/model-policy", candidate, policy.headers["etag"])
    assert changed.status_code == 200, changed.text
    assert api.call("GET", "/audit-events").json()["items"]
    api.login(REVIEWER)
    assert api.call("GET", "/settings/model-policy").status_code == 403
    assert api.call("GET", "/audit-events").status_code == 403


def test_job_cancel_retry_and_purge_retention(api):
    r = api.resource()
    v = api.edit(api.draft(r))
    approved = api.approve(v)
    queued = api.call("POST", f"/versions/{v.json()['id']}/publish", etag=approved.headers["etag"])
    job_id = queued.json()["id"]
    assert api.call("POST", f"/jobs/{job_id}/cancel").json()["state"] == "CANCELLED"
    assert api.call("POST", f"/jobs/{job_id}/retry").json()["state"] == "QUEUED"
    api.login()
    current = api.call("GET", f"/resources/{r['id']}")
    assert api.call("DELETE", f"/resources/{r['id']}", etag=current.headers["etag"]).status_code == 204
    trash = api.call("GET", f"/resources?space_id={SPACE}&trash=true").json()["items"][0]
    purge = api.call("POST", f"/resources/{r['id']}/purge", {"reason": "过期"}, f'"{trash["revision"]}"')
    assert purge.status_code == 409
    assert purge.json()["code"] == "RETENTION_UNDEFINED"
    configured = api.call("PUT", f"/spaces/{SPACE}/retention-policy", {
        "retention_days": 2, "approved": True, "purge_allowed_roles": ["admin"],
        "reason": "合成测试：明确批准两天策略后单独验证法律保全"}, '"0"')
    assert configured.status_code == 200, configured.text
    with api.app.state.session_factory() as db:
        resource = db.get(m.Resource, r["id"])
        resource.legal_hold = True
        db.commit()
    trash = api.call("GET", f"/resources?space_id={SPACE}&trash=true").json()["items"][0]
    assert api.call("POST", f"/resources/{r['id']}/purge", {"reason": "过期"}, f'"{trash["revision"]}"').status_code == 423


def test_clarification_candidates_keep_unknown_facts_not_unknown_law(api, jobs):
    from fund_kb.services import clarification_candidates
    r = api.resource()
    v = api.edit(api.draft(r), required=["share_class"], applicability={
        "all": [{"field": "share_class", "op": "eq", "values": ["A"]}]})
    api.publish(v)
    with api.app.state.session_factory() as db:
        reader = db.get(m.User, READER)
        assert eligible_evidence(db, reader, SPACE, {}) == []
        candidates = clarification_candidates(db, reader, SPACE, {})
        assert len(candidates) == 1 and candidates[0]["needs_context"] is True
        assert candidates[0]["missing_context_fields"] == ["share_class"]
        assert eligible_evidence(db, reader, SPACE, {"share_class": "A"})
        assert eligible_evidence(db, reader, SPACE, {"share_class": "B"}) == []
        version = db.get(m.ResourceVersion, v.json()["id"])
        version.legal_status = "PARTIAL"
        version.content_sha256 = check_frozen_hash(db, version)
        db.commit()
        assert clarification_candidates(db, reader, SPACE, {}) == []


def test_expired_complete_replacement_does_not_resurrect_old_rule(api, jobs):
    resource = api.resource()
    first = api.publish(api.edit(api.draft(resource), valid_from="2026-01-01"))
    api.login()
    second = api.publish(api.edit(api.draft(resource, first.json()["id"]),
        valid_from="2026-02-01", valid_to="2026-03-01"))
    with api.app.state.session_factory() as db:
        user = db.get(m.User, READER)
        assert eligible_evidence(db, user, SPACE, {"business_date": "2026-02-28"})[0]["version_id"] == second.json()["id"]
        assert eligible_evidence(db, user, SPACE, {"business_date": "2026-03-01"}) == []


def test_source_blob_quarantine_never_previews_or_enters_context(api):
    r = api.resource("document")
    v = api.draft(r)
    with api.app.state.session_factory() as db:
        blob = m.Blob(id=uid(), space_id=SPACE, object_key="blobs/quarantine/test.txt", sha256="0" * 64,
            size_bytes=1, mime_type="text/plain", scan_state="QUARANTINED")
        db.add(blob)
        db.flush()
        version = db.get(m.ResourceVersion, v.json()["id"])
        version.source_blob_id = blob.id
        db.commit()
    assert api.call("GET", f"/versions/{v.json()['id']}/content?representation=preview").status_code == 409
    assert api.call("GET", f"/versions/{v.json()['id']}/content?representation=source").status_code == 409


def test_compile_uses_real_scanned_source_without_overwriting_manual_work(api, jobs):
    r = api.resource("document", "来源资料")
    v = api.draft(r)
    raw = "核对基金运营资料和来源版本。".encode()
    u = api.call("POST", "/uploads", {"version_id": v.json()["id"], "filename": "source.txt", "size_bytes": len(raw)}).json()
    part = api.call("PUT", f"/uploads/{u['id']}/parts/1", raw=raw, headers={"Content-Type": "application/octet-stream"}).json()
    completed = api.call("POST", f"/uploads/{u['id']}/complete", {"parts": [part]})
    assert api.call("GET", f"/jobs/{completed.json()['id']}").json()["state"] == "SUCCEEDED"
    parsed = api.call("GET", f"/versions/{v.json()['id']}")
    attrs = {k: parsed.json()[k] for k in ("title", "knowledge_type", "applicability", "required_facts",
        "legal_status", "valid_from", "valid_to", "blocks")}
    attrs.update(legal_status="NOT_APPLICABLE", valid_from="2026-01-01")
    metadata = api.call("PATCH", f"/versions/{parsed.json()['id']}", attrs, parsed.headers["etag"])
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["blocks"] == parsed.json()["blocks"]
    published = api.publish(metadata)
    api.login()
    manual = api.edit(api.draft(api.resource(name="人工知识")), text="保留人工修订正文")
    compiled = api.call("POST", f"/versions/{published.json()['id']}/compile", {"target_space_id": SPACE, "knowledge_type": "sop"})
    assert compiled.status_code == 202, compiled.text
    result = api.call("GET", f"/jobs/{compiled.json()['id']}")
    assert result.json()["state"] == "SUCCEEDED", result.text
    assert result.json()["result"]["draft_version_id"] != manual.json()["id"]
    assert api.call("GET", f"/versions/{manual.json()['id']}").json()["blocks"] == manual.json()["blocks"]


def test_historical_answer_body_withheld_after_source_revocation(api, jobs):
    resource = api.resource()
    api.publish(api.edit(api.draft(resource)))
    api.login(READER)
    thread = api.call("POST", "/threads", {"space_id": SPACE, "title": "保密咨询"}).json()
    run = api.call("POST", f"/threads/{thread['id']}/runs", {"question": "核对来源版本", "mode": "answer", "context": {}}).json()
    assert api.call("GET", f"/runs/{run['id']}").json()["answer"] is not None
    api.login()
    current = api.call("GET", f"/resources/{resource['id']}/permissions")
    api.call("PUT", f"/resources/{resource['id']}/permissions", {"restricted": True,
        "classification": "RESTRICTED", "grants": [{"user_id": EDITOR, "permission": "manage"},
            {"user_id": EDITOR, "permission": "read"}]}, current.headers["etag"])
    api.login(READER)
    withheld = api.call("GET", f"/runs/{run['id']}")
    assert withheld.status_code == 200
    assert withheld.json()["answer"] is None and withheld.json()["invalidated"] is True
    assert api.call("GET", f"/threads/{thread['id']}").json()["items"][0]["answer"] is None


def test_case_is_private_and_cannot_reference_another_users_run(api):
    created = api.call("POST", "/cases", {"space_id": SPACE, "title": "内部问题", "description": "人工收集资料"})
    assert created.status_code == 201
    api.login(READER)
    assert api.call("GET", f"/cases/{created.json()['id']}").status_code == 404
    assert api.call("GET", "/cases").json()["items"] == []
    reader_thread = api.call("POST", "/threads", {"space_id": SPACE, "title": "私有"}).json()
    run = api.call("POST", f"/threads/{reader_thread['id']}/runs", {"question": "资料", "mode": "answer", "context": {}}).json()
    api.login()
    assert api.call("POST", "/cases", {"space_id": SPACE, "run_id": run["id"], "title": "不应创建", "description": "其他人私有咨询"}).status_code == 404


def test_queued_job_is_committed_before_dispatch_and_can_be_recovered(api):
    seen = []
    def observer(job_id):
        with api.app.state.session_factory() as db:
            job = db.get(m.Job, job_id)
            outbox = db.scalars(select(m.Outbox).where(m.Outbox.aggregate_id == job_id)).first()
            seen.append((job.kind, job.owner_id, bool(outbox)))
        raise RuntimeError("simulated transport outage")
    api.app.state.job_dispatcher = observer
    thread = api.call("POST", "/threads", {"space_id": SPACE, "title": "持久化"}).json()
    run = api.call("POST", f"/threads/{thread['id']}/runs", {"question": "材料", "mode": "answer", "context": {}})
    assert run.status_code == 202 and seen == [("ANSWER", EDITOR, True)]
    assert api.call("GET", f"/jobs/{run.json()['job_id']}").json()["state"] == "QUEUED"


def test_cached_export_request_cannot_reveal_revoked_content(api, jobs):
    r = api.resource()
    v = api.publish(api.edit(api.draft(r)))
    api.login(READER)
    key = str(uuid4())
    payload = {"version_ids": [v.json()["id"]], "format": "json"}
    first = api.call("POST", "/exports", payload, key=key)
    assert first.status_code == 202
    api.login()
    current = api.call("GET", f"/resources/{r['id']}")
    api.call("DELETE", f"/resources/{r['id']}", etag=current.headers["etag"])
    api.login(READER)
    assert api.call("POST", "/exports", payload, key=key).status_code == 404


def test_reader_projection_hides_other_authors_new_draft(api, jobs):
    r = api.resource()
    published = api.publish(api.edit(api.draft(r)))
    api.login()
    api.edit(api.draft(r, published.json()["id"]), text="尚未披露的新草稿")
    author = api.call("GET", f"/resources/{r['id']}").json()
    assert author["latest_version_no"] == 2 and author["latest_state"] == "DRAFT"
    assert author["created_at"] and author["updated_at"] and author["owner_name"]
    assert "editor" in api.call("GET", "/me").json()["spaces"][0]["roles"]
    api.login(READER)
    reader = api.call("GET", f"/resources/{r['id']}").json()
    assert reader["latest_version_no"] == 1 and reader["latest_state"] == "APPROVED"
    assert "尚未披露" not in str(reader)


def test_policy_binding_is_exact_and_loaded_from_settings(api):
    candidate = {"provider_ref": "evidence", "generation_model": "evidence-only", "extraction_model": "deterministic-extraction",
        "prompt_version": "v1", "enable_vector": False, "evaluation_id": "development-engineering-only"}
    bound = {k: v for k, v in candidate.items() if k != "evaluation_id"}
    api.app.state.settings.approved_model_policies = {candidate["evaluation_id"]: bound}
    other = create_app(api.app.state.settings)
    try:
        assert other.state.approved_model_policies == {candidate["evaluation_id"]: bound}
    finally:
        other.state.storage.close()
        other.state.engine.dispose()
    api.app.state.approved_model_policies = {candidate["evaluation_id"]: bound}
    current = api.call("GET", "/settings/model-policy")
    assert api.call("PUT", "/settings/model-policy", {**candidate, "enable_vector": True}, current.headers["etag"]).status_code == 409
    accepted = api.call("PUT", "/settings/model-policy", candidate, current.headers["etag"])
    assert accepted.status_code == 200
    assert api.call("GET", "/settings/model-policy").json() == candidate


def test_clarification_run_and_policy_snapshot_with_no_live_model(api, jobs):
    r = api.resource()
    api.publish(api.edit(api.draft(r), text="A类份额的资料核对流程", required=["share_class"],
        applicability={"all": [{"field": "share_class", "op": "eq", "values": ["A"]}]}))
    api.login()
    candidate = {"provider_ref": "evidence", "generation_model": "evidence-only", "extraction_model": "deterministic-extraction",
        "prompt_version": "v1", "enable_vector": False, "evaluation_id": "development-engineering-only"}
    api.app.state.approved_model_policies = {candidate["evaluation_id"]: {k: v for k, v in candidate.items() if k != "evaluation_id"}}
    current = api.call("GET", "/settings/model-policy")
    assert api.call("PUT", "/settings/model-policy", candidate, current.headers["etag"]).status_code == 200
    api.login(READER)
    thread = api.call("POST", "/threads", {"space_id": SPACE, "title": "需要澄清"}).json()
    run = api.call("POST", f"/threads/{thread['id']}/runs", {"question": "份额的资料核对流程", "mode": "answer", "context": {}}).json()
    done = api.call("GET", f"/runs/{run['id']}")
    assert done.status_code == 200 and done.json()["state"] == "COMPLETED", done.text
    assert done.json()["answer"]["status"] == "NEEDS_CLARIFICATION", done.text
    assert done.json()["question"] == "份额的资料核对流程" and done.json()["context"] == {}
    assert done.json()["created_at"]
    with api.app.state.session_factory() as db:
        saved = db.get(m.ConsultationRun, run["id"])
        assert saved.policy_snapshot["enable_vector"] is False
        assert saved.policy_snapshot["evaluation_id"] == candidate["evaluation_id"]


def text_format_payload(version, blocks):
    return {"title": "分段格式编辑", "knowledge_type": version.json()["knowledge_type"],
        "applicability": {}, "required_facts": [], "legal_status": "NOT_APPLICABLE",
        "valid_from": "2026-01-01", "valid_to": None, "blocks": blocks}


def text_format_block(kind="paragraph", text="**格式文字**", **data):
    return {"block_id": str(uuid4()), "ordinal": 0, "block_type": kind,
        "data": {"text": text, **({"level": 2} if kind == "heading" else {}), **data},
        "locator": {"label": "格式编辑测试"}, "citations": []}


def test_api_markdown_save_get_html_export_roundtrip_with_mixed_legacy_blocks(api):
    version = api.draft(api.resource())
    blocks = [
        text_format_block("heading", "**标题**", text_format="markdown"),
        text_format_block("paragraph", "第一段的 **粗体** 与 *斜体*", text_format="markdown"),
        text_format_block("warning", "**注意** [依据](https://example.invalid/rule)", text_format="markdown"),
        text_format_block("paragraph", "**旧字面量** [仅文字](https://example.invalid)"),
    ]
    for ordinal, block in enumerate(blocks):
        block["ordinal"] = ordinal
    saved = api.call("PATCH", f"/versions/{version.json()['id']}", text_format_payload(version, blocks), version.headers["etag"])
    assert saved.status_code == 200, saved.text
    loaded = api.call("GET", f"/versions/{version.json()['id']}")
    assert saved.json()["blocks"] == loaded.json()["blocks"] == blocks
    rendered = api.call("GET", f"/versions/{version.json()['id']}/content?representation=html")
    assert rendered.status_code == 200
    assert f'<h2 id="b-{blocks[0]["block_id"]}"><strong>标题</strong></h2>' in rendered.text
    assert "<strong>粗体</strong>" in rendered.text and "<em>斜体</em>" in rendered.text
    assert "<strong>注意</strong>" in rendered.text
    assert "**旧字面量** [仅文字](https://example.invalid)" in rendered.text
    assert '<p><p>' not in rendered.text
    exported = api.call("GET", f"/versions/{version.json()['id']}/content?representation=markdown")
    assert exported.status_code == 200
    assert exported.text.startswith("## **标题**\n\n第一段的 **粗体** 与 *斜体*")
    assert r"\*\*旧字面量\*\*" in exported.text and r"\[仅文字\]" in exported.text


@pytest.mark.parametrize("marker", ["html", "MARKDOWN", "richtext", "", None, False, {}, []])
def test_api_rejects_invalid_text_format_without_changing_draft(api, marker):
    version = api.draft(api.resource())
    block = text_format_block(text_format=marker)
    response = api.call("PATCH", f"/versions/{version.json()['id']}", text_format_payload(version, [block]), version.headers["etag"])
    assert response.status_code == 422, response.text
    unchanged = api.call("GET", f"/versions/{version.json()['id']}")
    assert unchanged.headers["etag"] == version.headers["etag"]
    assert unchanged.json()["blocks"] == []


@pytest.mark.parametrize("extra", [{"html": "<strong>旁路</strong>"}, {"rich_html": "<b>旁路</b>"},
    {"marks": ["bold"]}, {"document": {"type": "richtext"}}])
def test_api_markdown_marker_does_not_allow_rich_html_or_arbitrary_data(api, extra):
    version = api.draft(api.resource())
    block = text_format_block(text_format="markdown", **extra)
    response = api.call("PATCH", f"/versions/{version.json()['id']}", text_format_payload(version, [block]), version.headers["etag"])
    assert response.status_code == 422


@pytest.mark.parametrize("kind,data", [
    ("list", {"ordered": False, "items": ["正文"]}),
    ("table", {"columns": ["字段"], "rows": [["值"]]}),
    ("step", {"action": "核对", "owner_role": "复核", "output": "清单", "verification": "比较"}),
    ("formula", {"expression_text": "a+b", "unit": "元", "calculator_ref": None}),
    ("image", {"version_id": EDITOR, "caption": "图片"}),
    ("attachment", {"version_id": EDITOR, "caption": "附件"}),
])
def test_api_marker_is_rejected_on_every_other_block_type(api, kind, data):
    version = api.draft(api.resource())
    block = {"block_id": str(uuid4()), "ordinal": 0, "block_type": kind,
        "data": {**data, "text_format": "markdown"}, "locator": {}, "citations": []}
    response = api.call("PATCH", f"/versions/{version.json()['id']}", text_format_payload(version, [block]), version.headers["etag"])
    assert response.status_code == 422 and response.json()["code"] == "INVALID_BLOCK_DATA"


def test_format_marker_changes_frozen_version_hash_not_canonical_block_hash(api):
    version = api.draft(api.resource())
    block = text_format_block(text="**同一canonical文本**")
    path = f"/versions/{version.json()['id']}"
    plain = api.call("PATCH", path, text_format_payload(version, [block]), version.headers["etag"])
    assert plain.status_code == 200
    with api.app.state.session_factory() as db:
        v = db.get(m.ResourceVersion, version.json()["id"])
        plain_hash = check_frozen_hash(db, v)
        block_hash = db.get(m.ContentBlock, (v.id, block["block_id"])).content_sha256
    block["data"]["text_format"] = "markdown"
    marked = api.call("PATCH", path, text_format_payload(version, [block]), plain.headers["etag"])
    assert marked.status_code == 200
    with api.app.state.session_factory() as db:
        v = db.get(m.ResourceVersion, version.json()["id"])
        marked_hash = check_frozen_hash(db, v)
        assert marked_hash != plain_hash
        stored = db.get(m.ContentBlock, (v.id, block["block_id"]))
        assert stored.content_sha256 == block_hash == hashlib.sha256(block["data"]["text"].encode()).hexdigest()
        assert stored.search_text == block["data"]["text"]
    submitted = api.call("POST", path + "/submit", etag=marked.headers["etag"])
    assert submitted.status_code == 200 and submitted.json()["content_sha256"] == marked_hash
    block["data"]["text_format"] = "plain"
    assert api.call("PATCH", path, text_format_payload(version, [block]), submitted.headers["etag"]).status_code == 409


@pytest.mark.parametrize("text", ['<script>alert(1)</script>', '[危险](javascript:alert(1))'])
def test_markdown_api_keeps_existing_active_content_rejection(api, text):
    version = api.draft(api.resource())
    response = api.call("PATCH", f"/versions/{version.json()['id']}", text_format_payload(version, [
        text_format_block(text=text, text_format="markdown")]), version.headers["etag"])
    assert response.status_code == 422 and response.json()["code"] == "UNSAFE_CONTENT"


@pytest.mark.parametrize("text", ['![远程](https://example.invalid/track)',
    '![嵌入](data:image/png;base64,eA==)', '[编码](java&#x73;cript:alert(1))',
    '[数据](data:text/html;base64,PHNjcmlwdD4=)', '<b>原样HTML</b>'])
def test_api_markdown_render_sanitizes_images_encoded_urls_and_raw_html(api, text):
    version = api.draft(api.resource())
    path = f"/versions/{version.json()['id']}"
    response = api.call("PATCH", path, text_format_payload(version, [
        text_format_block(text=text, text_format="markdown")]), version.headers["etag"])
    assert response.status_code == 200, response.text
    assert api.call("GET", path).json()["blocks"][0]["data"]["text"] == text
    rendered = api.call("GET", path + "/content?representation=html")
    assert rendered.status_code == 200
    assert "<img" not in rendered.text and "<script" not in rendered.text and "<b>" not in rendered.text
    assert 'href="javascript:' not in rendered.text and 'href="data:' not in rendered.text


def test_format_edit_does_not_overwrite_original_or_original_preview(api, jobs):
    version = api.draft(api.resource("document", "原件保持不变"))
    version_id = version.json()["id"]
    raw = "**原件星号**和未编辑内容".encode()
    upload = api.call("POST", "/uploads", {"version_id": version_id, "filename": "literal.txt", "size_bytes": len(raw)}).json()
    part = api.call("PUT", f"/uploads/{upload['id']}/parts/1", raw=raw,
        headers={"Content-Type": "application/octet-stream"}).json()
    complete = api.call("POST", f"/uploads/{upload['id']}/complete", {"parts": [part]})
    assert api.call("GET", f"/jobs/{complete.json()['id']}").json()["state"] == "SUCCEEDED"
    preview_url = f"/versions/{version_id}/content?representation=preview"
    original_preview = api.call("GET", preview_url).content
    parsed = api.call("GET", f"/versions/{version_id}")
    formatted = text_format_payload(parsed, [text_format_block(text="**编辑后的内容**", text_format="markdown")])
    saved = api.call("PATCH", f"/versions/{version_id}", formatted, parsed.headers["etag"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["origin"] == "HUMAN"
    assert api.call("GET", f"/versions/{version_id}").json()["blocks"][0]["data"]["text"] == "**编辑后的内容**"
    knowledge = api.draft(api.resource("knowledge", "加工后的Wiki"))
    knowledge_payload = text_format_payload(knowledge, [text_format_block(text="**编辑后的内容**", text_format="markdown")])
    updated = api.call("PATCH", f"/versions/{knowledge.json()['id']}", knowledge_payload, knowledge.headers["etag"])
    assert updated.status_code == 200, updated.text
    rendered = api.call("GET", f"/versions/{knowledge.json()['id']}/content?representation=html")
    assert "<strong>编辑后的内容</strong>" in rendered.text
    assert api.call("GET", f"/versions/{version_id}/content?representation=source").content == raw
    assert "<strong>编辑后的内容</strong>" in api.call("GET", preview_url).text
    # The archived import preview itself is not rewritten by online editing.
    assert api.app.state.storage.local_path(f"previews/{version_id}.html").read_bytes() == original_preview
