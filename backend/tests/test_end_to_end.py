"""Real local DB/object/Qdrant/job/API integration, no external models or credentials."""
from __future__ import annotations

import hashlib
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from fund_kb.settings import Settings


def seed_id(number):
    return f"00000000-0000-4000-8000-{number:012d}"


@pytest.fixture
def client(tmp_path):
    from fund_kb.main import build_application
    settings = Settings(app_env="development", auth_mode="demo", auto_create_schema=True,
                        database_url=f"sqlite:///{tmp_path / 'integration.sqlite3'}",
                        storage_dir=tmp_path / "objects", qdrant_path=tmp_path / "qdrant",
                        allowed_origins=["http://testserver"], job_workers=1)
    app = build_application(settings)
    app.state.raise_test_errors = True
    app.state.database_errors = []

    @event.listens_for(app.state.engine, "handle_error")
    def capture_test_database_error(context):
        app.state.database_errors.append(str(context.original_exception))
    with TestClient(app) as client:
        client.headers["Origin"] = "http://testserver"
        login(client, 1)
        yield client


def login(client, number):
    response = client.post("/api/v1/auth/demo", json={"user_id": seed_id(number)})
    assert response.status_code == 200, response.text
    me = client.get("/api/v1/me")
    assert me.status_code == 200, me.text
    client.headers["X-CSRF-Token"] = me.json()["csrf_token"]


def write(client, method, path, body=None, revision=None, **options):
    headers = {"Idempotency-Key": str(uuid4())}
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return client.request(method, "/api/v1" + path, json=body, headers=headers, **options)


def finished_job(client, response):
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    until = time.monotonic() + 15
    while time.monotonic() < until:
        job = client.get(f"/api/v1/jobs/{job_id}")
        assert job.status_code == 200, job.text
        if job.json()["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            assert job.json()["state"] == "SUCCEEDED", job.text
            return job.json()
        time.sleep(0.03)
    pytest.fail(f"job {job_id} failed to reach terminal state in 15 seconds")


def test_demo_corpus_has_real_previews_and_separate_reviewers(client):
    listing = client.get("/api/v1/resources", params={"space_id": seed_id(101), "kind": "document"})
    assert listing.status_code == 200, listing.text
    assert len(listing.json()["items"]) == 5
    preview = client.get(f"/api/v1/versions/{seed_id(1201)}/content", params={"representation": "preview"})
    assert preview.status_code == 200, preview.text
    assert "估值" in preview.text and "演示" in preview.text
    raw = client.get(f"/api/v1/versions/{seed_id(1202)}/content", params={"representation": "source"})
    assert raw.status_code == 200 and raw.content.startswith(b"%PDF-")
    pending = client.get(f"/api/v1/versions/{seed_id(1203)}").json()
    rejected = write(client, "POST", f"/versions/{pending['id']}/reviews",
                     {"decision": "APPROVE", "reviewed_sha256": pending["content_sha256"], "comment": "self"},
                     pending["revision"])
    assert rejected.status_code == 403, rejected.text


def test_upload_edit_review_publish_search_delete_restore(client):
    taxonomy = client.get(f"/api/v1/documents/taxonomy?space_id={seed_id(101)}")
    assert taxonomy.status_code == 200, taxonomy.text
    category = write(client, "POST", "/documents/categories", {"space_id": seed_id(101), "path": "验收资料"},
                     taxonomy.json()["revision"])
    assert category.status_code == 201, category.text
    response = write(client, "POST", "/resources", {"space_id": seed_id(101), "kind": "document",
                     "name": "端到端上传特征资料", "category": "验收资料", "tags": ["integration"]})
    assert response.status_code == 201, response.text
    resource_id = response.json()["id"]
    response = write(client, "POST", f"/resources/{resource_id}/versions",
                     {"title": "端到端上传特征资料", "change_reason": "integration"})
    assert response.status_code == 201, response.text
    version_id = response.json()["id"]
    raw = "# 上传特征资料\n\nQAFLOW2026：先核对输入文件版本，再确认引用与复核状态。\n".encode()
    uploaded = write(client, "POST", "/uploads", {"version_id": version_id, "filename": "流程测试.md",
                     "size_bytes": len(raw), "expected_sha256": hashlib.sha256(raw).hexdigest()})
    assert uploaded.status_code == 201, uploaded.text
    upload_id = uploaded.json()["id"]
    part = client.put(f"/api/v1/uploads/{upload_id}/parts/1", content=raw,
                      headers={"Content-Type": "application/octet-stream"})
    assert part.status_code == 200, part.text
    finished_job(client, write(client, "POST", f"/uploads/{upload_id}/complete", {"parts": [part.json()]}))
    v = client.get(f"/api/v1/versions/{version_id}").json()
    assert len(v["blocks"]) >= 1
    payload = {k: v[k] for k in ("title", "knowledge_type", "applicability", "required_facts",
                                "legal_status", "valid_from", "valid_to", "blocks")}
    payload.update(legal_status="NOT_APPLICABLE", valid_from="2026-01-01")
    changed = write(client, "PATCH", f"/versions/{version_id}", payload, v["revision"])
    assert changed.status_code == 200, changed.text
    conflict = write(client, "PATCH", f"/versions/{version_id}", payload, v["revision"])
    assert conflict.status_code == 412, conflict.text
    submitted = write(client, "POST", f"/versions/{version_id}/submit", revision=changed.json()["revision"])
    assert submitted.status_code == 200, submitted.text
    login(client, 2)
    v = client.get(f"/api/v1/versions/{version_id}").json()
    approved = write(client, "POST", f"/versions/{version_id}/reviews",
                     {"decision": "APPROVE", "reviewed_sha256": v["content_sha256"], "comment": "验收来源",
                      "source_verified": True}, v["revision"])
    assert approved.status_code == 201, approved.text
    v = client.get(f"/api/v1/versions/{version_id}").json()
    finished_job(client, write(client, "POST", f"/versions/{version_id}/publish", revision=v["revision"]))
    login(client, 3)
    results = client.post("/api/v1/search", json={"space_id": seed_id(101), "query": "QAFLOW2026"})
    assert results.status_code == 200, results.text
    assert any(item["version_id"] == version_id for item in results.json()["items"])
    r = client.get(f"/api/v1/resources/{resource_id}").json()
    forbidden = write(client, "PATCH", f"/resources/{resource_id}", {"name": "reader mutation"}, r["revision"])
    assert forbidden.status_code == 403, forbidden.text
    login(client, 1)
    removed = write(client, "DELETE", f"/resources/{resource_id}", revision=r["revision"])
    assert removed.status_code == 204, removed.text
    results = client.post("/api/v1/search", json={"space_id": seed_id(101), "query": "QAFLOW2026"})
    assert all(item["version_id"] != version_id for item in results.json()["items"])
    trash = client.get("/api/v1/resources", params={"space_id": seed_id(101), "trash": True}).json()
    deleted = next(item for item in trash["items"] if item["id"] == resource_id)
    restored = write(client, "POST", f"/resources/{resource_id}/restore", revision=deleted["revision"])
    assert restored.status_code == 200 and restored.json()["suspended"] is True, (
        restored.text, client.app.state.database_errors)


def test_consultation_uses_real_citations_and_does_not_impersonate_model(client):
    thread = write(client, "POST", "/threads", {"space_id": seed_id(101), "title": "费用差异咨询"})
    assert thread.status_code == 201, thread.text
    request = write(client, "POST", f"/threads/{thread.json()['id']}/runs",
                    {"question": "费用差异排查与复核处理步骤", "mode": "solution", "context": {}})
    assert request.status_code == 202, request.text
    run_id = request.json()["id"]
    until = time.monotonic() + 15
    while time.monotonic() < until:
        response = client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200, response.text
        value = response.json()
        if value["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            break
        time.sleep(0.03)
    assert value["state"] == "COMPLETED", value
    answer = value["answer"]
    assert answer["review_status"] != "EXPERT_REVIEWED"
    assert answer["status"] in {"ANSWERED", "INSUFFICIENT_EVIDENCE", "NEEDS_CLARIFICATION"}
    if answer["status"] == "ANSWERED":
        assert answer["citations"] and answer["solution"]["steps"]
        for citation in answer["citations"]:
            source = client.get(f"/api/v1/versions/{citation['version_id']}")
            assert source.status_code == 200, source.text
            assert citation["block_id"] in {b["block_id"] for b in source.json()["blocks"]}
    else:
        assert answer["limitations"] or answer["required_sources"] or answer["missing_facts"]


def test_seeded_solution_keeps_one_sop_and_source_step_order(client):
    thread = write(client, "POST", "/threads", {"space_id": seed_id(101), "title": "步骤顺序验收"})
    response = write(client, "POST", f"/threads/{thread.json()['id']}/runs", {
        "question": "费用计提差异应如何排查，请给出可执行的核对步骤和来源依据。",
        "mode": "solution", "context": {},
    })
    assert response.status_code == 202, response.text
    until = time.monotonic() + 15
    while time.monotonic() < until:
        value = client.get(f"/api/v1/runs/{response.json()['id']}").json()
        if value["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            break
        time.sleep(0.03)
    assert value["state"] == "COMPLETED", value
    answer = value["answer"]
    assert answer["status"] == "ANSWERED", answer
    assert {c["version_id"] for c in answer["citations"]} == {seed_id(1301)}
    actions = [step["action"].split("：")[0] for step in answer["solution"]["steps"]]
    assert actions == ["核对适用范围", "核对输入版本", "复核费用计算", "提交复核材料"]
    assert answer["review_status"] != "EXPERT_REVIEWED"
