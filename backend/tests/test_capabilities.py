"""Real capability HTTP routes over synthetic SQLite; no services, models or real data."""
import copy
import json
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_api import API, EDITOR, OUTSIDER, READER, REVIEWER, SPACE

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.api import create_app
from fund_kb.capability_schema import FORMAT, starter
from fund_kb.ingestion import text_sha256
from fund_kb.settings import Settings


@pytest.fixture
def api(tmp_path, monkeypatch):
    # Do not let any ambient application settings select a real DB, model,
    # credential file, vector profile or storage path. Values are never read.
    for key in list(os.environ):
        if key.startswith("FKB_"):
            monkeypatch.delenv(key)

    def forbidden(*args, **kwargs):
        pytest.fail("Capability tests must not access a network, provider or service")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    from fund_kb import wiki
    monkeypatch.setattr(wiki, "_provider_module", forbidden)
    settings = Settings(app_env="development", auth_mode="demo", storage_dir=tmp_path / "objects",
        database_url=f"sqlite:///{tmp_path / 'capabilities.sqlite3'}", allowed_origins=["http://testserver"],
        qdrant_path=tmp_path / "vectors", retrieval_mode="wiki", llm_provider="evidence")
    app = create_app(settings)
    app.state.raise_test_errors = True
    with TestClient(app) as client:
        with app.state.session_factory.begin() as db:
            for who in (EDITOR, REVIEWER, READER, OUTSIDER):
                db.add(m.User(id=who, external_subject="demo:" + who, display_name="合成" + who, active=True))
            db.add(m.Space(id=SPACE, name="合成能力库"))
            db.flush()
            for who, roles in ((EDITOR, ["reader", "editor", "admin"]),
                    (REVIEWER, ["reader", "reviewer", "publisher"]), (READER, ["reader"])):
                db.add_all(m.SpaceMember(space_id=SPACE, user_id=who, role=role) for role in roles)
        harness = API(app, client)
        harness.login()
        yield harness


def definition(*, ids=(), scope="reference", human=True, fields=None):
    result = starter()
    result.update(source_version_ids=list(ids), source_scope=scope, inputs=fields or [])
    result["steps"] = [result["steps"][0], result["steps"][-1]] if human else [result["steps"][0]]
    if human:
        result["steps"][-1]["depends_on"] = ["evidence"]
    return result


def field(key="payload", typ="json", required=True):
    return {"key": key, "label": "合成要素", "type": typ, "required": required, "description": "只用于离线测试"}


def create(api, value=None, *, space=SPACE):
    response = api.call("POST", "/capabilities", {"space_id": space, "definition": value or definition()})
    assert response.status_code == 201, response.text
    assert response.headers["etag"] == f'"{response.json()["revision"]}"'
    return response.json()


def start(api, cap, *, inputs=None, mode="trial", key=None):
    response = api.call("POST", "/capability-runs", {"version_id": cap["version_id"],
        "inputs": inputs or {}, "mode": mode}, key=key)
    assert response.status_code == 201, response.text
    return response.json()


def report(api, run, *, step="evidence", outputs=None, status="reported", note="合成报告", **kwargs):
    return api.call("POST", f'/capability-runs/{run["id"]}/steps/{step}',
        {"status": status, "outputs": outputs if outputs is not None else {"evidence_summary": "合成底稿"}, "note": note},
        etag=f'"{run["revision"]}"', **kwargs)


def activate_fixture(db, version):
    """Synthetic published fixture only; no job dispatcher or deployment."""
    version.content_sha256 = svc.check_frozen_hash(db, version)
    version.state = "APPROVED"
    release = m.Release(id=svc.uid(), resource_id=version.resource_id, version_id=version.id,
        state="ACTIVE", publisher_id=REVIEWER, activated_at=svc.now())
    db.add(release)
    db.flush()
    db.get(m.Resource, version.resource_id).active_release_id = release.id


def source(api, *, kind="document", space=SPACE, verified=False, legal="UNKNOWN", cites=(), count=2, published=True):
    rid, vid, blob_id = svc.uid(), svc.uid(), svc.uid() if kind == "document" else None
    block_ids = [svc.uid() for _ in range(count)]
    with api.app.state.session_factory.begin() as db:
        db.add(m.Resource(id=rid, space_id=space, kind=kind, name="合成来源", owner_id=EDITOR))
        if blob_id:
            db.add(m.Blob(id=blob_id, space_id=space, object_key=f"synthetic/{blob_id}.txt", sha256="a" * 64,
                size_bytes=20, mime_type="text/plain", scan_state="CLEAN"))
        db.flush()
        version = m.ResourceVersion(id=vid, resource_id=rid, version_no=1, title="合成来源V1", author_id=EDITOR,
            origin="UPLOAD" if kind == "document" else "HUMAN", source_blob_id=blob_id,
            knowledge_type="source" if kind == "document" else "rule", source_verified=verified,
            legal_status=legal, valid_from=date(2020, 1, 1))
        db.add(version)
        db.flush()
        for index, bid in enumerate(block_ids):
            text = f"合成来源第{index + 1}条，保留条件与证据定位。"
            db.add(m.ContentBlock(version_id=vid, block_id=bid, ordinal=index, block_type="paragraph",
                data={"text": text}, search_text=text, content_sha256=text_sha256(text),
                locator={"page": index + 1, "label": f"条款{index + 1}"}))
        db.flush()
        for target in cites:
            db.add(m.EvidenceLink(id=svc.uid(), from_version_id=vid, from_block_id=block_ids[0],
                to_version_id=target.version_id, to_block_id=target.block_ids[0], purpose="RULE"))
        db.flush()
        if published:
            activate_fixture(db, version)
    return SimpleNamespace(resource_id=rid, version_id=vid, block_ids=block_ids, blob_id=blob_id)


def publish_capability_fixture(api, cap):
    # Exercise the actual submit/review endpoints. Only publication activation
    # is seeded; a publication worker is intentionally outside these tests.
    version = api.call("GET", "/versions/" + cap["version_id"])
    api.approve(version)
    with api.app.state.session_factory.begin() as db:
        activate_fixture(db, db.get(m.ResourceVersion, cap["version_id"]))
    api.login(EDITOR)
    response = api.call("GET", "/capability-versions/" + cap["version_id"])
    assert response.status_code == 200, response.text
    return response.json()


def another_space(api):
    identity = svc.uid()
    with api.app.state.session_factory.begin() as db:
        db.add(m.Space(id=identity, name="合成另一库"))
        db.flush()
        db.add_all(m.SpaceMember(space_id=identity, user_id=EDITOR, role=role) for role in ("reader", "editor", "admin"))
    return identity


def bearer(api, *, space=SPACE):
    response = api.call("POST", "/agent-access", {"request_id": svc.uid(), "space_id": space, "name": "合成Agent",
        "scopes": ["capabilities:read", "runs:write", "sources:read"],
        "expires_at": svc.primitive(svc.now() + timedelta(days=1))})
    assert response.status_code == 201, response.text
    return {"Authorization": "Bearer " + response.json()["token"], "X-CSRF-Token": "", "Origin": ""}


def test_crud_etag_hash_copy_and_published_guided_run(api):
    origin = source(api, verified=True, legal="EFFECTIVE")
    cap = create(api, definition(ids=[origin.version_id]))
    assert cap["state"] == "DRAFT" and cap["permissions"]["can_trial"] and not cap["permissions"]["can_run"]
    initial = copy.deepcopy(cap)
    changed = copy.deepcopy(cap["definition"])
    changed["steps"][0]["instructions"] += "新增实际核对要点。"
    path = "/capabilities/" + cap["resource_id"]
    body = {"version_id": cap["version_id"], "definition": changed}
    assert api.call("PUT", path, body).status_code == 428
    assert api.call("PUT", path, body, etag='"0"').status_code == 412
    updated = api.call("PUT", path, body, etag=f'"{cap["revision"]}"')
    assert updated.status_code == 200, updated.text
    cap = updated.json()
    assert cap["revision"] == initial["revision"] + 1
    assert cap["manifest_sha256"] != initial["manifest_sha256"]
    assert cap["content_sha256"] != initial["content_sha256"]
    with api.app.state.session_factory() as db:
        version = db.get(m.ResourceVersion, cap["version_id"])
        block = svc.block_rows(db, version.id)[0]
        assert json.loads(block.data["text"])["definition"] == changed
        assert len(svc.evidence_links(db, version.id)) == len(origin.block_ids)
    published = publish_capability_fixture(api, cap)
    assert published["permissions"]["can_run"] and not published["permissions"]["can_edit"]
    assert api.call("PUT", path, body, etag=f'"{published["revision"]}"').status_code == 409
    copied = api.call("POST", f'/resources/{cap["resource_id"]}/versions', {"title": published["name"],
        "change_reason": "合成能力V2", "base_version_id": published["version_id"]})
    assert copied.status_code == 201, copied.text
    assert copied.json()["origin"] == "COPY"
    draft = api.call("GET", "/capability-versions/" + copied.json()["id"]).json()
    assert draft["state"] == "DRAFT" and draft["version_no"] == 2 and draft["definition"] == changed
    assert api.call("GET", path).json()["version_id"] == draft["version_id"]
    run = start(api, published, mode="guided")
    assert run["version_id"] == published["version_id"] and run["state"] == "WAITING_AGENT"
    api.login(READER)
    assert api.call("GET", path).json()["version_id"] == published["version_id"]
    assert api.call("GET", "/capability-versions/" + draft["version_id"]).status_code == 404
    assert start(api, published, mode="guided")["owner_id"] == READER


def test_exact_source_new_draft_does_not_shadow_published_bound_version(api):
    origin = source(api, count=19)
    cap = create(api, definition(ids=[origin.version_id]))
    run = start(api, cap)
    copied = api.call("POST", f"/resources/{origin.resource_id}/versions", {"title": "合成来源V2草稿",
        "change_reason": "验证最新草稿不遮盖绑定V1", "base_version_id": origin.version_id})
    assert copied.status_code == 201, copied.text
    fresh = api.call("GET", "/capability-versions/" + cap["version_id"])
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["source_bindings"] == cap["source_bindings"]
    read = api.call("GET", f'/capability-runs/{run["id"]}/sources')
    assert read.status_code == 200, read.text
    assert len(read.json()["records"]) == 19
    assert {row["version_id"] for row in read.json()["records"]} == {origin.version_id}
    assert all(row["legal_status"] == "UNKNOWN" and row["source_verified"] is False for row in read.json()["records"])
    assert any("UNKNOWN" in note for note in read.json()["notes"])
    assert report(api, run).status_code == 200
    # Binding the old exact release remains possible even after V2 exists.
    assert create(api, definition(ids=[origin.version_id]))["source_bindings"] == cap["source_bindings"]


@pytest.mark.parametrize("scope", ["reference", "formal"])
def test_empty_bound_ids_never_expand_to_whole_library(api, scope):
    source(api, verified=True, legal="EFFECTIVE")
    run = start(api, create(api, definition(scope=scope)))
    response = api.call("GET", f'/capability-runs/{run["id"]}/sources')
    assert response.status_code == 200, response.text
    assert response.json()["records"] == [] and response.json()["source_bindings"] == []
    assert any("尚未绑定" in note for note in response.json()["notes"])


@pytest.mark.parametrize("scope", ["reference", "formal"])
def test_source_citations_have_exact_locators_only_within_bound_scope(api, scope):
    origin = source(api, verified=True, legal="EFFECTIVE")
    wiki = source(api, kind="knowledge", verified=True, legal="NOT_APPLICABLE", cites=[origin])
    for ids, expected in (([wiki.version_id], []), ([wiki.version_id, origin.version_id], [origin])):
        run = start(api, create(api, definition(ids=ids, scope=scope)))
        response = api.call("GET", f'/capability-runs/{run["id"]}/sources')
        assert response.status_code == 200, response.text
        records = response.json()["records"]
        assert {record["version_id"] for record in records} == set(ids)
        row = next(record for record in records if record["version_id"] == wiki.version_id and record["ordinal"] == 0)
        assert len(row["citations"]) == len(expected)
        if expected:
            target = next(record for record in records if record["block_id"] == origin.block_ids[0])
            assert row["citations"] == [{key: target[key] for key in (
                "resource_id", "version_id", "block_id", "locator", "content_sha256")} | {"purpose": "RULE"}]
        assert row["valid_from"] == "2020-01-01" and row["valid_to"] is None


@pytest.mark.parametrize("change", ["acl", "deleted", "suspended", "scan", "blob_hash", "body", "search", "block_hash",
    "revision", "epoch", "unpublished", "rejected", "job", "upload"])
def test_current_source_guards_block_read_start_report_and_human_completion(api, change):
    origin = source(api)
    cap = create(api, definition(ids=[origin.version_id]))
    run = start(api, cap)
    human_run = report(api, start(api, cap)).json()
    with api.app.state.session_factory.begin() as db:
        resource = db.get(m.Resource, origin.resource_id)
        version = db.get(m.ResourceVersion, origin.version_id)
        block = db.get(m.ContentBlock, (version.id, origin.block_ids[0]))
        if change == "acl": resource.restricted = True
        elif change == "deleted": resource.deleted_at = svc.now()
        elif change == "suspended": resource.suspended = True
        elif change == "scan": db.get(m.Blob, origin.blob_id).scan_state = "REJECTED"
        elif change == "blob_hash": db.get(m.Blob, origin.blob_id).sha256 = "b" * 64
        elif change == "body": block.data = {"text": "已被篡改正文"}
        elif change == "search": block.search_text = "伪造检索正文"
        elif change == "block_hash": block.content_sha256 = "c" * 64
        elif change == "revision": version.revision += 1
        elif change == "epoch": resource.access_epoch += 1
        elif change == "unpublished": db.get(m.Release, resource.active_release_id).state = "FAILED"
        elif change == "rejected": version.state = "REJECTED"
        elif change == "job":
            db.add(m.Job(id=svc.uid(), kind="SCAN_PARSE", state="QUEUED", owner_id=EDITOR,
                resource_id=resource.id, version_id=version.id, stage="queued", dedupe_key=svc.uid(), payload={}))
        elif change == "upload":
            db.add(m.Upload(id=svc.uid(), version_id=version.id, user_id=EDITOR, filename="synthetic.txt",
                declared_size=1, part_size=1, part_count=1, state="OPEN", expires_at=svc.now() + timedelta(hours=1)))
    for path in ("/capability-versions/" + cap["version_id"], f'/capability-runs/{run["id"]}',
            f'/capability-runs/{run["id"]}/sources', f'/capability-runs/{run["id"]}/next'):
        denied = api.call("GET", path)
        assert denied.status_code in {404, 409}, denied.text
        assert "合成来源第" not in denied.text
    assert api.call("POST", "/capability-runs", {"version_id": cap["version_id"], "inputs": {}, "mode": "trial"}).status_code in {404, 409}
    assert report(api, run).status_code in {404, 409}
    assert api.call("POST", f'/capability-runs/{human_run["id"]}/review', {"step_id": "review", "decision": "accept",
        "note": "合成核对"}, etag=f'"{human_run["revision"]}"').status_code in {404, 409}
    with api.app.state.session_factory() as db:
        assert db.get(m.RuntimePolicy, run["id"]).revision == run["revision"]
        assert db.get(m.RuntimePolicy, human_run["id"]).config["state"] == "WAITING_HUMAN"


@pytest.mark.parametrize("change", ["scan", "body", "epoch", "acl", "deleted", "suspended"])
def test_transitive_cited_source_guard_is_not_lost_when_only_wiki_is_bound(api, change):
    origin = source(api)
    wiki = source(api, kind="knowledge", cites=[origin])
    cap = create(api, definition(ids=[wiki.version_id]))
    run = start(api, cap)
    with api.app.state.session_factory.begin() as db:
        resource = db.get(m.Resource, origin.resource_id)
        if change == "scan": db.get(m.Blob, origin.blob_id).scan_state = "QUARANTINED"
        elif change == "body": db.get(m.ContentBlock, (origin.version_id, origin.block_ids[0])).search_text = "伪造正文"
        elif change == "epoch": resource.access_epoch += 1
        elif change == "acl": resource.restricted = True
        elif change == "deleted": resource.deleted_at = svc.now()
        elif change == "suspended": resource.suspended = True
    assert api.call("GET", f'/capability-runs/{run["id"]}/sources').status_code in {404, 409}
    assert report(api, run).status_code in {404, 409}


def test_formal_requires_actual_eligible_sources_and_rechecks_on_completion(api):
    unknown = source(api)
    cap = create(api, definition(ids=[unknown.version_id], scope="formal"))
    denied = api.call("POST", "/capability-runs", {"version_id": cap["version_id"], "inputs": {}, "mode": "trial"})
    assert denied.status_code == 409 and denied.json()["code"] == "CAPABILITY_SOURCES_NOT_ADMITTED"
    origin = source(api, verified=True, legal="EFFECTIVE")
    cap = create(api, definition(ids=[origin.version_id], scope="formal", human=False, fields=[field("business_date", "date")]))
    run = start(api, cap, inputs={"business_date": "2026-09-13"})
    with api.app.state.session_factory.begin() as db:
        # Deliberately retain revision to test the formal gate independently of
        # the frozen binding. source_verified is excluded from the body hash.
        db.execute(update(m.ResourceVersion).where(m.ResourceVersion.id == origin.version_id).values(source_verified=False))
    denied = report(api, run)
    assert denied.status_code == 409 and denied.json()["code"] == "CAPABILITY_SOURCES_NOT_ADMITTED"


def test_cross_space_binding_and_agent_boundaries_download_permissions(api):
    second = another_space(api)
    other_source = source(api, space=second)
    response = api.call("POST", "/capabilities", {"space_id": SPACE, "definition": definition(ids=[other_source.version_id])})
    assert response.status_code == 422 and response.json()["code"] == "CAPABILITY_SOURCE_SPACE_MISMATCH"
    cap = create(api)
    foreign = create(api, space=second)
    foreign_run = start(api, foreign)
    headers = bearer(api)
    read = api.call("GET", "/capability-versions/" + cap["version_id"], headers=headers)
    assert read.status_code == 200 and read.json()["permissions"]["can_export"] is False
    assert api.call("GET", f'/capability-versions/{cap["version_id"]}/skill', headers=headers).status_code == 403
    for path in ("/capabilities?space_id=" + second, "/capabilities/" + foreign["resource_id"],
            "/capability-versions/" + foreign["version_id"], "/capability-runs?space_id=" + second,
            f'/capability-runs/{foreign_run["id"]}', f'/capability-runs/{foreign_run["id"]}/sources'):
        assert api.call("GET", path, headers=headers).status_code == 404
    assert api.call("POST", "/capability-runs", {"version_id": foreign["version_id"], "inputs": {}, "mode": "trial"}, headers=headers).status_code == 404
    with api.app.state.session_factory.begin() as db:
        db.get(m.Resource, cap["resource_id"]).restricted = True
        db.add(m.ResourceGrant(resource_id=cap["resource_id"], user_id=EDITOR, permission="read"))
    read = api.call("GET", "/capability-versions/" + cap["version_id"])
    assert read.status_code == 200 and read.json()["permissions"]["can_export"] is False
    assert api.call("GET", f'/capability-versions/{cap["version_id"]}/skill').status_code == 404
    with api.app.state.session_factory.begin() as db:
        db.add(m.ResourceGrant(resource_id=cap["resource_id"], user_id=EDITOR, permission="download"))
    assert api.call("GET", "/capability-versions/" + cap["version_id"]).json()["permissions"]["can_export"] is True
    assert api.call("GET", f'/capability-versions/{cap["version_id"]}/skill').status_code == 200


@pytest.mark.parametrize("mutation,code", [("duplicate_step", "CAPABILITY_STEP_DUPLICATE"),
    ("duplicate_input", "CAPABILITY_FIELD_DUPLICATE"), ("duplicate_output", "CAPABILITY_FIELD_DUPLICATE"),
    ("missing", "CAPABILITY_DEPENDENCY_MISSING"), ("cycle", "CAPABILITY_DEPENDENCY_CYCLE"),
    ("self", "CAPABILITY_DEPENDENCY_CYCLE"), ("external_write", "CAPABILITY_HUMAN_GATE_REQUIRED"),
    ("financial_action", "CAPABILITY_HUMAN_GATE_REQUIRED")])
def test_definition_semantic_guards_are_real_api_validation(api, mutation, code):
    value = definition()
    if mutation == "duplicate_step": value["steps"].append(copy.deepcopy(value["steps"][0]))
    elif mutation == "duplicate_input": value["inputs"] = [field(), field()]
    elif mutation == "duplicate_output": value["steps"][0]["outputs"] *= 2
    elif mutation == "missing": value["steps"][0]["depends_on"] = ["absent"]
    elif mutation == "cycle": value["steps"][0]["depends_on"] = ["review"]
    elif mutation == "self": value["steps"][0]["depends_on"] = ["evidence"]
    else: value["steps"][0]["risk"] = mutation
    response = api.call("POST", "/capabilities", {"space_id": SPACE, "definition": value})
    assert response.status_code == 422 and response.json()["code"] == code, response.text
    with api.app.state.session_factory() as db:
        assert db.scalar(select(m.Resource.id)) is None


@pytest.mark.parametrize("typ,value,valid", [("number", True, False), ("number", 1.5, True),
    ("integer", 1.5, False), ("integer", 2, True), ("boolean", 0, False), ("boolean", False, True),
    ("date", "2026-02-29", False), ("date", "2024-02-29", True), ("string", "  ", False),
    ("string", "合成输入", True), ("json", {"rows": [None, True, 1.5]}, True)])
def test_input_and_output_types(api, typ, value, valid):
    schema = definition(fields=[field(typ=typ)], human=False)
    schema["steps"][0]["outputs"] = [field(typ=typ)]
    cap = create(api, schema)
    response = api.call("POST", "/capability-runs", {"version_id": cap["version_id"], "inputs": {"payload": value}, "mode": "trial"})
    assert response.status_code == (201 if valid else 422), response.text
    if valid:
        assert report(api, response.json(), outputs={"payload": value}).json()["state"] == "COMPLETED"
    else:
        with api.app.state.session_factory() as db:
            assert db.scalar(select(m.RuntimePolicy.id).where(m.RuntimePolicy.name.startswith("capability-run:"))) is None


@pytest.mark.parametrize("raw_value", ["NaN", "Infinity", "-Infinity", "1e999", '{"nested":[NaN]}'])
def test_nonfinite_json_inputs_and_all_step_statuses_roll_back(api, raw_value):
    schema = definition(fields=[field()])
    schema["steps"][0]["outputs"] = [field()]
    cap = create(api, schema)
    raw = '{"version_id":"' + cap["version_id"] + '","inputs":{"payload":' + raw_value + '},"mode":"trial"}'
    response = api.call("POST", "/capability-runs", raw=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    run = start(api, cap, inputs={"payload": None})
    for status in ("reported", "blocked", "failed"):
        raw = '{"status":"' + status + '","outputs":{"payload":' + raw_value + '},"note":"synthetic"}'
        response = api.call("POST", f'/capability-runs/{run["id"]}/steps/evidence', raw=raw,
            etag=f'"{run["revision"]}"', headers={"Content-Type": "application/json"})
        assert response.status_code == 422, response.text
    assert api.call("GET", f'/capability-runs/{run["id"]}').json()["revision"] == run["revision"]


@pytest.mark.parametrize("envelope", [None, True, 3, "text", [], [{}], {"format": FORMAT}])
def test_nonobject_or_incomplete_manifest_is_controlled_error_not_500(api, envelope):
    cap = create(api)
    with api.app.state.session_factory.begin() as db:
        block = svc.block_rows(db, cap["version_id"])[0]
        text = json.dumps(envelope)
        block.data, block.search_text, block.content_sha256 = {"text": text}, text, text_sha256(text)
    response = api.call("GET", "/capability-versions/" + cap["version_id"])
    assert response.status_code == 409 and response.json()["code"] == "CAPABILITY_DEFINITION_INVALID", response.text


def test_run_readiness_reports_human_checkpoint_and_terminal_etag(api):
    cap = create(api)
    run = start(api, cap)
    path = f'/capability-runs/{run["id"]}'
    ready = api.call("GET", path + "/next").json()
    assert [step["id"] for step in ready["ready_steps"]] == ["evidence"] and ready["waiting_human_steps"] == []
    review = {"step_id": "review", "decision": "accept", "note": "合成核对"}
    assert api.call("POST", path + "/review", review, etag='"1"').status_code == 409
    assert report(api, run, step="review", outputs={}).status_code == 422
    assert report(api, run, outputs={}).status_code == 422
    assert api.call("POST", path + "/steps/evidence", {"status": "reported", "outputs": {}, "note": "合成"}).status_code == 428
    assert report(api, run, headers={"X-CSRF-Token": ""}).status_code == 403
    key = svc.uid()
    reported = report(api, run, key=key)
    assert reported.status_code == 200, reported.text
    assert report(api, run, key=key).json() == reported.json()
    run = reported.json()
    assert run["state"] == "WAITING_HUMAN"
    assert run["steps"][0]["state"] == "REPORTED" and run["steps"][0]["reported_by"] == EDITOR
    assert report(api, run).status_code == 409
    headers = bearer(api)
    assert api.call("POST", path + "/review", review, etag=f'"{run["revision"]}"', headers=headers).status_code == 403
    assert api.call("POST", path + "/review", review, etag='"1"').status_code == 412
    reviewed = api.call("POST", path + "/review", review, etag=f'"{run["revision"]}"')
    assert reviewed.status_code == 200 and reviewed.json()["state"] == "COMPLETED", reviewed.text
    assert reviewed.json()["steps"][1]["reviewed_by"] == EDITOR
    assert api.call("GET", path + "/next").json()["ready_steps"] == []
    assert report(api, reviewed.json()).status_code == 409
    assert api.call("POST", path + "/cancel", {"reason": "合成取消"}, etag=reviewed.headers["etag"]).status_code == 409
    with api.app.state.session_factory() as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.object_id == run["id"], m.AuditEvent.outcome == "SUCCESS")))
        assert len([event for event in events if event.action == "capability_step.reported"]) == 1
        assert next(event.details for event in events if event.action == "capability_step.reported")["external_execution_verified"] is False


def test_human_reject_keeps_report_immutable_and_explains_new_run(api):
    run = report(api, start(api, create(api))).json()
    path = f'/capability-runs/{run["id"]}'
    rejected = api.call("POST", path + "/review", {"step_id": "review", "decision": "reject", "note": "依据不足"}, etag=f'"{run["revision"]}"')
    assert rejected.status_code == 200 and rejected.json()["state"] == "BLOCKED"
    assert any("新建运行" in note for note in rejected.json()["notes"])
    assert report(api, rejected.json()).status_code == 409
    assert api.call("GET", path + "/next").json()["ready_steps"] == []


def test_block_retry_failure_cancel_owner_and_idempotency_staleness(api):
    cap = create(api)
    run = start(api, cap)
    blocked = report(api, run, status="blocked", outputs={}, note="合成缺少资料")
    assert blocked.status_code == 200 and blocked.json()["state"] == "BLOCKED"
    assert api.call("GET", f'/capability-runs/{run["id"]}/next').json()["ready_steps"][0]["id"] == "evidence"
    assert report(api, blocked.json()).json()["state"] == "WAITING_HUMAN"
    key = svc.uid()
    run = start(api, cap, key=key)
    api.login(REVIEWER)
    assert api.call("GET", f'/capability-runs/{run["id"]}').status_code == 404
    assert api.call("GET", "/capability-runs?space_id=" + SPACE).json()["items"] == []
    api.login(EDITOR)
    cancelled = api.call("POST", f'/capability-runs/{run["id"]}/cancel', {"reason": "合成取消"}, etag=f'"{run["revision"]}"')
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "CANCELLED"
    assert report(api, cancelled.json()).status_code == 409
    replay = api.call("POST", "/capability-runs", {"version_id": cap["version_id"], "inputs": {}, "mode": "trial"}, key=key)
    assert replay.status_code == 409 and replay.json()["code"] == "CAPABILITY_RUN_REPLAY_STALE"
    failed = report(api, start(api, cap), status="failed", outputs={}, note="合成失败")
    assert failed.status_code == 200 and failed.json()["state"] == "FAILED"
    assert api.call("GET", f'/capability-runs/{failed.json()["id"]}/next').json()["ready_steps"] == []


def test_concurrent_step_reports_only_one_revision_wins(api):
    run = start(api, create(api, definition(human=False)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: report(api, run), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 412]
    current = api.call("GET", f'/capability-runs/{run["id"]}').json()
    assert current["revision"] == run["revision"] + 1 and current["state"] == "COMPLETED"
