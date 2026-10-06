"""Actual app wrapper/handlers, exclusively test_api's synthetic SQLite fixture."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_api import EDITOR, READER, SPACE
from test_api import api as api  # noqa: PLC0414

from fund_kb import agent_access
from fund_kb import models as m
from fund_kb import services as svc


def token_input(*, space_id=SPACE, scopes=None):
    return {"request_id": str(uuid4()), "name": "合成HTTP Agent", "space_id": space_id,
        "scopes": sorted(agent_access.SCOPES) if scopes is None else scopes,
        "expires_at": svc.primitive(svc.now() + timedelta(days=1))}


def issue(api, *, space_id=SPACE, scopes=None):
    res = api.call("POST", "/agent-access", token_input(space_id=space_id, scopes=scopes),
        headers={"Idempotency-Key": ""})
    assert res.status_code == 201, res.text
    return res.json()


def bearer(api, token, method, path, data=None, **kwargs):
    return api.call(method, path, data, headers={"Authorization": "Bearer " + token,
        "X-CSRF-Token": "", "Origin": ""}, **kwargs)


def capability(api, space_id=SPACE):
    definition = {"schema_version": 1, "name": "合成安全验证", "description": "验证权限边界",
        "triggers": ["离线测试"], "limitations": ["无金融任务执行"], "inputs": [],
        "steps": [{"id": "collect", "title": "合成Agent步骤", "kind": "agent", "instructions": "返回合成底稿",
            "depends_on": [], "required_tools": [], "outputs": [{"key": "work", "label": "底稿", "type": "string",
                "required": True, "description": "合成内容"}], "checks": ["结构核对"], "risk": "read_only"},
            {"id": "review", "title": "人工核对", "kind": "human", "instructions": "仅网页用户",
                "depends_on": ["collect"], "required_tools": [], "outputs": [], "checks": ["人工确认"], "risk": "read_only"}],
        "deliverables": ["合成底稿"], "source_version_ids": [], "source_scope": "reference"}
    res = api.call("POST", "/capabilities", {"space_id": space_id, "definition": definition})
    assert res.status_code == 201, res.text
    return res.json()


def run(api, cap, token=None, key=None):
    body = {"version_id": cap["version_id"], "inputs": {}, "mode": "trial"}
    response = bearer(api, token, "POST", "/capability-runs", body, key=key) if token else api.call(
        "POST", "/capability-runs", body, key=key)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_once_no_plaintext_in_generic_idempotency_or_audit_and_revoke_replay(api):
    data = token_input()
    response = api.call("POST", "/agent-access", data, headers={"Idempotency-Key": ""})
    assert response.status_code == 201, response.text
    created = response.json()
    assert "no-store" in response.headers["cache-control"]
    duplicate = api.call("POST", "/agent-access", data)
    assert duplicate.status_code == 409 and "token" not in duplicate.json()
    assert duplicate.json()["details"]["access_id"] == created["access"]["id"]
    listed = api.call("GET", "/agent-access?space_id=" + SPACE)
    assert listed.status_code == 200 and listed.json()["items"] == [created["access"]]
    assert listed.json()["base_url"] == "http://testserver/api/v1"
    with api.app.state.session_factory() as db:
        receipts = db.scalars(select(m.IdempotencyRecord)).all()
        assert not any(row.route == "/api/v1/agent-access" for row in receipts)
        persisted = json.dumps([db.scalars(select(m.RuntimePolicy.config)).all(),
            db.scalars(select(m.AuditEvent.details)).all(), [row.response for row in receipts]])
        assert created["token"] not in persisted and created["token"].split(".")[1] not in persisted
    path = "/agent-access/" + created["access"]["id"] + "/revoke"
    assert api.call("POST", path, {"reason": "合成撤销"}, etag='"0"').status_code == 412
    key = str(uuid4())
    first = api.call("POST", path, {"reason": "合成撤销"}, etag='"1"', key=key)
    again = api.call("POST", path, {"reason": "合成撤销"}, etag='"1"', key=key)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    assert first.headers["etag"] == '"2"'
    assert bearer(api, created["token"], "GET", "/capabilities?space_id=" + SPACE).status_code == 401


def test_cookie_csrf_stays_required_and_any_bearer_blocks_token_management(api):
    assert api.call("POST", "/agent-access", token_input(), headers={"X-CSRF-Token": ""}).status_code == 403
    created = issue(api)
    path = "/agent-access/" + created["access"]["id"] + "/revoke"
    assert api.call("POST", path, {"reason": "合成"}, etag='"1"', headers={"X-CSRF-Token": ""}).status_code == 403
    for method, route, data in (("GET", "/agent-access?space_id=" + SPACE, None),
            ("POST", "/agent-access", token_input()), ("POST", path, {"reason": "合成"})):
        assert bearer(api, created["token"], method, route, data, etag='"1"').status_code == 403
    assert api.call("GET", "/capabilities?space_id=" + SPACE, headers={"Authorization": ""}).status_code == 401


def test_real_bearer_only_can_read_start_report_cancel_but_never_human_review(api):
    cap = capability(api)
    created = issue(api)
    token = created["token"]
    api.client.cookies.clear()  # No hidden Cookie-auth success in this test.
    listed = bearer(api, token, "GET", "/capabilities?space_id=" + SPACE)
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1
    assert bearer(api, token, "GET", "/capabilities/" + cap["resource_id"]).status_code == 200
    assert bearer(api, token, "GET", "/capability-versions/" + cap["version_id"]).status_code == 200
    value = run(api, cap, token)
    root = "/capability-runs/" + value["id"]
    for suffix in ("", "/next", "/sources"):
        assert bearer(api, token, "GET", root + suffix).status_code == 200
    sources = bearer(api, token, "GET", root + "/sources").json()
    assert sources["records"] == []  # No bindings must never mean all sources.
    assert bearer(api, token, "GET", "/capability-runs?space_id=" + SPACE).status_code == 200
    wrong = bearer(api, token, "POST", root + "/steps/collect", {"status": "reported", "outputs": {"work": "合成"}, "note": ""}, etag='"0"')
    assert wrong.status_code == 412
    submitted = bearer(api, token, "POST", root + "/steps/collect",
        {"status": "reported", "outputs": {"work": "合成"}, "note": ""}, etag='"1"')
    assert submitted.status_code == 200 and submitted.json()["state"] == "WAITING_HUMAN"
    assert submitted.json()["steps"][0]["report_channel"] == "agent_token"
    review = {"step_id": "review", "decision": "accept", "note": "试图替代人工"}
    assert bearer(api, token, "POST", root + "/review", review, etag='"2"').status_code == 403
    assert bearer(api, token, "POST", root + "/steps/review",
        {"status": "reported", "outputs": {}, "note": ""}, etag='"2"').status_code == 422
    cancelled = bearer(api, token, "POST", root + "/cancel", {"reason": "合成取消"}, etag='"2"')
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "CANCELLED"


def test_bearer_cannot_replay_cookie_human_review_receipt(api):
    cap = capability(api)
    token = issue(api)["token"]
    value = run(api, cap)
    root = "/capability-runs/" + value["id"]
    assert api.call("POST", root + "/steps/collect", {"status": "reported", "outputs": {"work": "合成"}, "note": ""}, etag='"1"').status_code == 200
    key = str(uuid4())
    data = {"step_id": "review", "decision": "accept", "note": "合成Cookie人工确认"}
    assert api.call("POST", root + "/review", data, etag='"2"', key=key).status_code == 200
    assert bearer(api, token, "POST", root + "/review", data, etag='"2"', key=key).status_code == 403


def test_cross_space_queries_object_reads_writes_and_idempotent_replay_are_denied(api):
    token = issue(api)["token"]
    library = api.call("POST", "/libraries", {"name": "合成另一个个人库", "kind": "personal"})
    assert library.status_code == 201
    other = library.json()["id"]
    cap = capability(api, other)
    key = str(uuid4())
    value = run(api, cap, key=key)
    root = "/capability-runs/" + value["id"]
    for path in ("/capabilities?space_id=" + other, "/capabilities/" + cap["resource_id"],
            "/capability-versions/" + cap["version_id"], "/capability-runs?space_id=" + other,
            root, root + "/next", root + "/sources"):
        assert bearer(api, token, "GET", path).status_code == 404
    for request_key in (key, str(uuid4())):
        assert bearer(api, token, "POST", "/capability-runs",
            {"version_id": cap["version_id"], "inputs": {}, "mode": "trial"}, key=request_key).status_code == 404
    assert bearer(api, token, "POST", root + "/cancel", {"reason": "不能跨库"}, etag='"1"').status_code == 404


def test_other_users_cannot_read_or_cancel_owner_run_or_revoke_owner_credential(api):
    cap = capability(api)
    created = issue(api)
    value = run(api, cap)
    api.login(READER)
    other_token = issue(api)["token"]
    assert api.call("GET", "/agent-access?space_id=" + SPACE).json()["items"][0]["id"] != created["access"]["id"]
    path = "/agent-access/" + created["access"]["id"] + "/revoke"
    assert api.call("POST", path, {"reason": "不能撤销他人"}, etag='"1"').status_code == 404
    root = "/capability-runs/" + value["id"]
    for suffix in ("", "/next", "/sources"):
        assert bearer(api, other_token, "GET", root + suffix).status_code == 404
    assert bearer(api, other_token, "POST", root + "/cancel", {"reason": "不能取消他人"}, etag='"1"').status_code == 404


@pytest.mark.parametrize("change", ["disabled", "expired", "revoked", "scope"])
def test_http_next_request_rechecks_user_lifecycle_and_scope(api, change):
    created = issue(api)
    assert bearer(api, created["token"], "GET", "/capabilities?space_id=" + SPACE).status_code == 200
    with api.app.state.session_factory.begin() as db:
        row = db.get(m.RuntimePolicy, created["access"]["id"])
        if change == "disabled": db.get(m.User, EDITOR).active = False
        elif change == "expired": row.config = {**row.config, "created_at": "2000-01-01T00:00:00Z", "expires_at": "2000-01-02T00:00:00Z"}
        elif change == "revoked": row.config = {**row.config, "revoked_at": svc.primitive(svc.now())}
        else: row.config = {**row.config, "scopes": ["sources:read"]}
    assert bearer(api, created["token"], "GET", "/capabilities?space_id=" + SPACE).status_code == (403 if change == "scope" else 401)


def test_sources_scope_does_not_authorize_general_document_read_or_other_operations(api):
    cap = capability(api)
    value = run(api, cap)
    created = issue(api, scopes=["sources:read"])
    token = created["token"]
    assert bearer(api, token, "GET", "/capability-runs/" + value["id"] + "/sources").status_code == 200
    paths = ["/me", "/health", "/capabilities/starter?space_id=" + SPACE,
        "/capability-versions/" + cap["version_id"] + "/skill", "/versions/" + cap["version_id"] + "/content"]
    for path in paths:
        assert bearer(api, token, "GET", path).status_code == 403
    assert bearer(api, token, "POST", "/resources", {"space_id": SPACE, "kind": "template", "name": "越权编辑"}).status_code == 403


def test_knowledge_read_scope_reads_map_and_versions_only_inside_token_space(api):
    resource = api.resource(name="合成Agent可读知识")
    version = api.draft(resource)
    api.edit(version, text="AGENT_READABLE：核对估值价格来源。")
    library = api.call("POST", "/libraries", {"name": "合成另一个个人库", "kind": "personal"})
    other = library.json()["id"]
    foreign = api.call("POST", "/resources", {"space_id": other, "kind": "knowledge", "name": "他库知识"}).json()
    foreign_version = api.draft(foreign)
    token = issue(api, scopes=["knowledge:read"])["token"]
    mapped = bearer(api, token, "GET", "/library-map?space_id=" + SPACE)
    assert mapped.status_code == 200, mapped.text
    body = mapped.json()
    assert "库地图" in body["text"] and body["stats"]["body_blocks_loaded"] == 0
    assert all(set(p) >= {"page_id", "version_id", "title", "kind"} for p in body["pages"])
    read = bearer(api, token, "GET", "/versions/" + version.json()["id"])
    assert read.status_code == 200 and "AGENT_READABLE" in read.text
    assert bearer(api, token, "GET", "/library-map?space_id=" + other).status_code == 404
    assert bearer(api, token, "GET", "/versions/" + foreign_version.json()["id"]).status_code == 404
    assert bearer(api, token, "GET", "/coverage-gaps?space_id=" + SPACE).status_code == 200
    capability_only = issue(api, scopes=["capabilities:read"])["token"]
    denied = bearer(api, capability_only, "GET", "/library-map?space_id=" + SPACE)
    assert denied.status_code == 403 and denied.json()["code"] == "AGENT_SCOPE_REQUIRED"


def test_consult_scope_creates_owner_threads_in_token_space_only(api):
    token = issue(api, scopes=["consult:write"])["token"]
    thread = bearer(api, token, "POST", "/threads", {"space_id": SPACE, "title": "Agent发起的合成咨询"})
    assert thread.status_code == 201, thread.text
    run = bearer(api, token, "POST", f"/threads/{thread.json()['id']}/runs",
                 {"question": "如何核对价格来源？", "mode": "answer", "context": {}, "answer_scope": "reference"})
    assert run.status_code == 202, run.text
    assert bearer(api, token, "GET", "/runs/" + run.json()["id"]).status_code == 200
    # The owner sees the Agent-created thread in the normal history.
    assert thread.json()["id"] in api.call("GET", "/threads").text
    library = api.call("POST", "/libraries", {"name": "合成另一个个人库", "kind": "personal"}).json()["id"]
    assert bearer(api, token, "POST", "/threads", {"space_id": library, "title": "跨库"}).status_code == 404
    own_other = api.call("POST", "/threads", {"space_id": library, "title": "网页在他库创建"}).json()
    assert bearer(api, token, "POST", f"/threads/{own_other['id']}/runs",
                  {"question": "跨库提问", "mode": "answer", "context": {}}).status_code == 404
    knowledge_only = issue(api, scopes=["knowledge:read"])["token"]
    assert bearer(api, knowledge_only, "POST", "/threads", {"space_id": SPACE, "title": "无权限"}).status_code == 403
