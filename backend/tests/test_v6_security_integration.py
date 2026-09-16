"""v6 cross-module security regression: synthetic SQLite + actual HTTP wrapper.

Run from backend: .venv/bin/python -m pytest tests/test_v6_security_integration.py
Only these tests' tmp_path objects are used. No server, browser, real credentials,
external network, vector service, or host auth is needed. API helpers/identities
reuse test_api; settings deliberately exclude environment/dotenv sources.
No xfail/skip or production-handler replacements hide missing security controls.
"""
from __future__ import annotations

import hashlib
import io
import json
import queue
import socket
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_api import API, READER, SPACE
from test_api import EDITOR as ADMIN
from test_api import OUTSIDER as OWNER
from test_api import REVIEWER as COLLAB

from fund_kb import libraries, providers
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.api import create_app
from fund_kb.jobs import JobDispatcher
from fund_kb.settings import Settings

SECRET = "V6_SYNTHETIC_PRIVATE_7b12"  # gitleaks:allow -- nonfunctional synthetic test marker
MODEL = "v6-synthetic-model"


class SyntheticSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        return (init_settings,)


@pytest.fixture
def api(tmp_path, monkeypatch):
    attempts = []
    real_connect = socket.socket.connect

    def deny_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append("network")
            raise AssertionError("v6 security tests prohibit IP network connections")
        return real_connect(sock, address)

    def forbidden(*_args, **_kwargs):
        attempts.append("provider-or-credential")
        raise AssertionError("v6 security tests prohibit credentials and real provider calls")

    monkeypatch.setattr(socket.socket, "connect", deny_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(providers, "_master_key", forbidden)
    monkeypatch.setattr(providers, "_network", forbidden)
    monkeypatch.setattr(providers, "complete", forbidden)
    app = create_app(SyntheticSettings(_env_file=None, app_env="development", auth_mode="demo",
        database_url=f"sqlite:///{tmp_path / 'v6-security.sqlite3'}", storage_dir=tmp_path / "objects",
        qdrant_path=tmp_path / "unused-vectors", retrieval_mode="wiki", llm_provider="evidence",
        allowed_origins=["http://testserver"], job_backend="local", job_workers=1))
    app.state.raise_test_errors = True
    with TestClient(app) as client:
        with app.state.session_factory.begin() as db:
            for who in (ADMIN, OWNER, READER, COLLAB):
                db.add(m.User(id=who, external_subject="demo:admin" if who == ADMIN else f"demo:{who}",
                              display_name=f"synthetic-{who[-1]}", active=True))
            db.add(m.Space(id=SPACE, name="synthetic legacy"))
            db.flush()
            for who, roles in ((ADMIN, libraries.ALL_ROLES), (COLLAB, {"reader", "editor", "reviewer"}),
                               (READER, {"reader"})):
                for role in roles:
                    db.add(m.SpaceMember(space_id=SPACE, user_id=who, role=role))
        harness = API(app, client)
        harness.login(OWNER)
        yield harness
    assert attempts == [], "A prohibited network/credential path was attempted"


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response


def denied(response, statuses=(403, 404, 409)):
    assert response.status_code in statuses, response.text
    assert SECRET not in response.text
    return response


def library(api, kind="personal", who=OWNER):
    api.login(who)
    return ok(api.call("POST", "/libraries", {"kind": kind, "name": f"synthetic-{kind}"}), 201).json()


def members(api, sid, entries):
    path = f"/libraries/{sid}/members"
    current = ok(api.call("GET", path))
    return ok(api.call("PUT", path, {"items": entries}, current.headers["etag"]))


def team(api):
    space = library(api, "team")
    members(api, space["id"], [{"user_id": OWNER, "roles": sorted(libraries.ALL_ROLES)},
                               {"user_id": COLLAB, "roles": ["editor", "reviewer"]}])
    return space


def resource(api, sid, *, kind="knowledge", category=None):
    body = {"space_id": sid, "kind": kind, "name": SECRET}
    if category:
        body["category"] = category
    created = ok(api.call("POST", "/resources", body), 201)
    version = api.draft(created.json())
    if kind == "document":
        # Source bodies are immutable through HTTP. Seed the parsed-output boundary
        # with synthetic text, as ingestion fixtures do; never relax that guard.
        with api.app.state.session_factory.begin() as db:
            db.add(m.ContentBlock(version_id=version.json()["id"], block_id=svc.uid(), ordinal=0,
                block_type="paragraph", data={"text": SECRET}, locator={"label": "synthetic source"},
                search_text=SECRET, content_sha256=hashlib.sha256(SECRET.encode()).hexdigest()))
        version = ok(api.call("GET", f"/versions/{version.json()['id']}"))
    else:
        version = api.edit(version, text=SECRET)
    return ok(api.call("GET", f"/resources/{created.json()['id']}")).json(), version


def source(api, sid):
    """Synthetic parsed original setup; ingestion quality is outside this ACL test."""
    res, version = resource(api, sid, kind="document", category="内部指引")
    raw = SECRET.encode()
    key = f"v6-synthetic/{svc.uid()}.txt"
    api.app.state.storage.write_bytes(key, raw)
    with api.app.state.session_factory.begin() as db:
        blob = m.Blob(id=svc.uid(), space_id=sid, object_key=key,
            sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), mime_type="text/plain", scan_state="CLEAN")
        db.add(blob)
        db.flush()
        db.get(m.ResourceVersion, version.json()["id"]).source_blob_id = blob.id
    return res, ok(api.call("GET", f"/versions/{version.json()['id']}"))


def connection(api, sid, **changes):
    body = {"space_id": sid, "name": "synthetic private model", "provider_id": "ollama",
        "credential_mode": "none", "allow_document_transfer": True,
        "custom_models": [{"id": MODEL, "name": MODEL, "brand": "Synthetic"}], **changes}
    return ok(api.call("POST", "/model-connections", body), 201)


def run_job(api, job_id):
    """Drain only the explicitly selected synthetic job, without starting a service."""
    dispatcher = JobDispatcher(api.app.state.settings, api.app.state.session_factory, None)
    dispatcher.codex_bridge = getattr(api.app.state, "codex_bridge", None)
    try:
        dispatcher.run(job_id)
    finally:
        dispatcher.close()
    with api.app.state.session_factory() as db:
        return svc.job_dict(db.get(m.Job, job_id))


def normalize(api, res, version, model, key=None):
    body = {"source_version_id": version.json()["id"], "consent": True,
            "model_selection": {"connection_id": model.json()["id"], "model_id": MODEL}}
    response = ok(api.call("POST", f"/documents/{res['id']}/normalizations", body,
                           version.headers["etag"], key=key), 202)
    return response, body


def fake_completion(monkeypatch, calls, mutate=None):
    def complete(snapshot, messages, **kwargs):
        assert snapshot["api_key"] is None
        calls.append(snapshot["id"])
        if mutate:
            mutate()
        payload = {"title": "合成建议", "blocks": [{"markdown": SECRET, "evidence_ids": ["S1"]}], "gaps": []}
        return {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(payload)}}]}
    monkeypatch.setattr(providers, "complete", complete)


@pytest.mark.parametrize("intruder", [ADMIN, COLLAB, READER])
@pytest.mark.parametrize("forged", [False, True])
def test_personal_all_surfaces_ignore_admin_membership_and_resource_grants(api, intruder, forged):
    private = library(api)
    res, version = source(api, private["id"])
    rid, vid, sid = res["id"], version.json()["id"], private["id"]
    if forged:
        with api.app.state.session_factory.begin() as db:
            for role in libraries.ALL_ROLES:
                db.add(m.SpaceMember(space_id=sid, user_id=intruder, role=role))
            for permission in ("read", "download", "edit", "manage", "publish", "review"):
                db.add(m.ResourceGrant(resource_id=rid, user_id=intruder, permission=permission))
    api.login(intruder)
    for path in (f"/libraries/{sid}", f"/libraries/{sid}/members", f"/spaces/{sid}/members",
        f"/resources/{rid}", f"/resources/{rid}/permissions", f"/resources/{rid}/versions", f"/versions/{vid}",
        f"/versions/{vid}/content?representation=source&download=true",
        f"/versions/{vid}/content?representation=markdown", f"/documents?space_id={sid}",
        f"/documents/taxonomy?space_id={sid}", f"/wiki/graph?space_id={sid}", f"/wiki/workspace?space_id={sid}",
        f"/users/directory?space_id={sid}", f"/spaces/{sid}/retention-policy",
        f"/resources/{rid}/preservation", f"/resources/{rid}/purge-eligibility", f"/model-options?space_id={sid}"):
        denied(api.call("GET", path), (404,))
    for path in ("/libraries", "/me", "/spaces"):
        assert sid not in ok(api.call("GET", path)).text
    denied(api.call("POST", "/search", {"space_id": sid, "query": SECRET}), (404,))
    denied(api.call("PATCH", f"/resources/{rid}", {"name": "intruder edit"}, '"1"'), (404,))
    denied(api.call("DELETE", f"/resources/{rid}", etag='"1"'), (404,))
    denied(api.call("PUT", f"/libraries/{sid}/members", {"items": [
        {"user_id": intruder, "roles": ["admin"]}]}, '"1"'), (404,))


def test_personal_owner_cannot_open_a_backdoor_by_adding_members(api):
    space = library(api)
    for path, body in ((f"/libraries/{space['id']}/members", {"items": [
            {"user_id": ADMIN, "roles": ["admin", "editor"]}]}),
            (f"/spaces/{space['id']}/members", [{"user_id": ADMIN, "roles": ["admin", "editor"]}])):
        response = denied(api.call("PUT", path, body, '"1"'), (409,))
        assert response.json()["code"] == "PERSONAL_MEMBERS_IMMUTABLE"


def test_team_shared_draft_etag_and_contributor_cannot_self_review(api):
    space = team(api)
    _, first = resource(api, space["id"])
    api.login(READER)
    assert ok(api.call("GET", f"/libraries/{space['id']}")).json()["roles"] == ["reader"]
    denied(api.call("GET", f"/versions/{first.json()['id']}"), (404,))
    assert ok(api.call("GET", f"/resources?space_id={space['id']}")).json()["items"] == []
    api.login(COLLAB)
    ok(api.call("GET", f"/versions/{first.json()['id']}"))
    edited = api.edit(first, text="合成协作修改")
    api.login(OWNER)
    fields = {key: edited.json()[key] for key in ("title", "knowledge_type", "applicability",
              "required_facts", "legal_status", "valid_from", "valid_to", "blocks")}
    denied(api.call("PATCH", f"/versions/{first.json()['id']}", fields, first.headers["etag"]), (412,))
    submitted = ok(api.call("POST", f"/versions/{first.json()['id']}/submit", etag=edited.headers["etag"]))
    api.login(COLLAB)
    response = denied(api.call("POST", f"/versions/{first.json()['id']}/reviews", {
        "decision": "APPROVE", "reviewed_sha256": submitted.json()["content_sha256"], "comment": "协作者尝试自审"},
        submitted.headers["etag"]), (403,))
    assert response.json()["code"] == "SELF_REVIEW_FORBIDDEN"


def test_team_restricted_document_classification_move_rolls_back_visible_item(api):
    space = team(api)
    visible, _ = resource(api, space["id"], kind="document", category="未分类")
    hidden, _ = resource(api, space["id"], kind="document", category="未分类")
    with api.app.state.session_factory.begin() as db:
        db.get(m.Resource, hidden["id"]).restricted = True
    api.login(COLLAB)
    taxonomy = ok(api.call("GET", f"/documents/taxonomy?space_id={space['id']}"))
    result = denied(api.call("POST", "/documents/classification-moves", {
        "space_id": space["id"], "category": "内部指引", "items": [
            {"id": visible["id"], "revision": visible["revision"]},
            {"id": hidden["id"], "revision": hidden["revision"]}]},
        taxonomy.headers["etag"]), (404,))
    assert hidden["id"] not in result.text
    with api.app.state.session_factory() as db:
        assert db.get(m.Resource, visible["id"]).category == "未分类"
        assert db.get(m.Resource, visible["id"]).revision == visible["revision"]


def test_team_nonmember_reader_sees_published_version_but_not_restricted_content(api):
    shared = team(api)
    members(api, shared["id"], [{"user_id": OWNER, "roles": sorted(libraries.ALL_ROLES)},
        {"user_id": COLLAB, "roles": ["reviewer", "publisher"]}])
    res, version = resource(api, shared["id"])
    approved = api.approve(version)  # Independent COLLAB review; no authorship bypass.
    queued = ok(api.call("POST", f"/versions/{approved.json()['id']}/publish", etag=approved.headers["etag"]), 202)
    result = run_job(api, queued.json()["id"])
    assert result["state"] == "SUCCEEDED", result
    api.login(READER)
    assert SECRET in ok(api.call("GET", f"/versions/{version.json()['id']}")).text
    markdown = ok(api.call("GET", f"/versions/{version.json()['id']}/content?representation=markdown")).text
    assert SECRET.replace("_", "\\_") in markdown  # Plain-text underscores are correctly escaped in Markdown.
    with api.app.state.session_factory.begin() as db:
        db.get(m.Resource, res["id"]).restricted = True
    denied(api.call("GET", f"/resources/{res['id']}"), (404,))
    denied(api.call("GET", f"/versions/{version.json()['id']}"), (404,))
    assert SECRET not in ok(api.call("GET", f"/resources?space_id={shared['id']}")).text


def test_draft_edit_idempotency_replay_rechecks_revoked_team_editor(api):
    space = team(api)
    _, version = resource(api, space["id"])
    api.login(COLLAB)
    body = {key: version.json()[key] for key in ("title", "knowledge_type", "applicability",
            "required_facts", "legal_status", "valid_from", "valid_to", "blocks")}
    path, key = f"/versions/{version.json()['id']}", svc.uid()
    ok(api.call("PATCH", path, body, version.headers["etag"], key))
    api.login(OWNER)
    members(api, space["id"], [{"user_id": OWNER, "roles": sorted(libraries.ALL_ROLES)}])
    api.login(COLLAB)
    denied(api.call("PATCH", path, body, version.headers["etag"], key), (404,))


@pytest.mark.parametrize("intruder", [ADMIN, COLLAB, READER])
def test_models_owner_isolation_in_shared_space_and_legacy_quarantine(api, intruder):
    space = team(api)
    own = connection(api, space["id"])
    cid = own.json()["id"]
    with api.app.state.session_factory.begin() as db:
        old_id = svc.uid()
        config = {**db.get(m.RuntimePolicy, cid).config, "id": old_id}
        config.pop("owner_user_id")
        db.add(m.RuntimePolicy(id=old_id, name=f"model-connection:{old_id}", config=config, updated_by=OWNER))
    api.login(intruder)
    for target in (cid, old_id):
        denied(api.call("GET", f"/model-connections/{target}"), (404,))
        denied(api.call("PATCH", f"/model-connections/{target}", {"name": "takeover"}, '"1"'), (404,))
        denied(api.call("POST", f"/model-connections/{target}/test", {"model_id": MODEL}, '"1"'), (404,))
    assert ok(api.call("GET", "/model-connections")).json()["items"] == []
    assert ok(api.call("GET", f"/model-options?space_id={space['id']}")).json()["items"] == []
    api.login(OWNER)
    denied(api.call("GET", f"/model-connections/{old_id}"), (404,))


def test_own_model_created_elsewhere_can_normalize_accessible_team_source(api, monkeypatch):
    private = library(api)
    model = connection(api, private["id"])
    shared = team(api)
    res, version = source(api, shared["id"])
    options = ok(api.call("GET", f"/model-options?space_id={shared['id']}")).json()["items"]
    assert any(item["connection_id"] == model.json()["id"] for item in options)
    calls = []
    fake_completion(monkeypatch, calls)
    queued, _ = normalize(api, res, version, model)
    done = run_job(api, queued.json()["id"])
    assert done["state"] == "SUCCEEDED", done
    assert calls == [model.json()["id"]]
    suggestion = ok(api.call("GET", f"/document-normalizations/{done['id']}"))
    assert SECRET in suggestion.text
    assert ok(api.call("GET", f"/versions/{version.json()['id']}/content?representation=source&download=true")).content == SECRET.encode()


def test_model_selection_cannot_steal_other_owner_connection(api):
    shared = team(api)
    model = connection(api, shared["id"])
    res, version = source(api, shared["id"])
    api.login(COLLAB)
    response = api.call("POST", f"/documents/{res['id']}/normalizations", {
        "source_version_id": version.json()["id"], "consent": True,
        "model_selection": {"connection_id": model.json()["id"], "model_id": MODEL}}, version.headers["etag"])
    denied(response, (404,))
    with api.app.state.session_factory() as db:
        assert list(db.scalars(select(m.Job))) == []


@pytest.mark.parametrize("surface", ["detail", "list", "replay"])
def test_model_job_old_result_unreadable_after_connection_revision_change(api, surface):
    space = library(api)
    model = connection(api, space["id"])
    path, key = f"/model-connections/{model.json()['id']}/test", svc.uid()
    body = {"model_id": MODEL}
    queued = ok(api.call("POST", path, body, model.headers["etag"], key), 202)
    jid = queued.json()["id"]
    with api.app.state.session_factory.begin() as db:
        job = db.get(m.Job, jid)
        job.state, job.result = "SUCCEEDED", {"old_model_output": SECRET}
    ok(api.call("PATCH", f"/model-connections/{model.json()['id']}", {"enabled": False}, model.headers["etag"]))
    # Explicit v6 decision: same-owner terminal history may show this exact safe
    # projection. Mutation replay and cross-owner reads remain strict denials.
    expected_safe = {"connection_id": model.json()["id"], "state": "SUCCEEDED",
                     "details_unavailable_reason": "CONNECTION_REVISION_CHANGED"}
    if surface == "detail":
        response = ok(api.call("GET", f"/jobs/{jid}"))
        assert response.json()["result"] == expected_safe
        assert set(response.json()) == {"id", "kind", "state", "stage", "attempts", "error_code", "result"}
        assert SECRET not in response.text
    elif surface == "list":
        response = ok(api.call("GET", "/jobs"))
        assert SECRET not in response.text
        item = next(item for item in response.json()["items"] if item["id"] == jid)
        assert item["result"] == expected_safe
        assert set(item) == {"id", "kind", "state", "stage", "attempts", "error_code", "result"}
    else:
        denied(api.call("POST", path, body, model.headers["etag"], key), (409,))
    with api.app.state.session_factory() as db:
        assert db.get(m.Job, jid).result == {"old_model_output": SECRET}, "Safe projection mutated durable evidence"
    api.login(ADMIN)
    denied(api.call("GET", f"/jobs/{jid}"), (404,))


def test_cancel_and_its_replay_never_release_old_result_after_model_revocation(api):
    space = library(api)
    model = connection(api, space["id"])
    queued = ok(api.call("POST", f"/model-connections/{model.json()['id']}/test",
                         {"model_id": MODEL}, model.headers["etag"]), 202)
    jid = queued.json()["id"]
    with api.app.state.session_factory.begin() as db:
        db.get(m.Job, jid).result = {"old_attempt": SECRET}
    ok(api.call("PATCH", f"/model-connections/{model.json()['id']}", {"enabled": False}, model.headers["etag"]))
    path, key = f"/jobs/{jid}/cancel", svc.uid()
    for _ in range(2):
        response = ok(api.call("POST", path, key=key), 202)
        assert response.json()["result"] is None and SECRET not in response.text


@pytest.mark.parametrize("when", ["before_execution", "during_completion"])
def test_guidance_job_rechecks_revoked_model_before_and_after_completion(api, monkeypatch, when):
    private = library(api)
    model = connection(api, private["id"])
    res, version = source(api, private["id"])
    queued, _ = normalize(api, res, version, model)
    jid = queued.json()["id"]

    def revoke():
        ok(api.call("PATCH", f"/model-connections/{model.json()['id']}", {"enabled": False}, model.headers["etag"]))

    calls = []
    fake_completion(monkeypatch, calls, revoke if when == "during_completion" else None)
    if when == "before_execution":
        revoke()
    done = run_job(api, jid)
    assert done["state"] == "FAILED", done
    # Job checkpoints retain non-content counters in result; no suggestion or text
    # may survive a revoked completion. Counters alone are not a data disclosure.
    assert SECRET not in json.dumps(done) and "suggestion_id" not in (done["result"] or {}), done
    assert len(calls) == (1 if when == "during_completion" else 0)
    with api.app.state.session_factory() as db:
        assert db.scalar(select(m.RuntimePolicy.id).where(m.RuntimePolicy.name == f"document-guidance-suggestion:{jid}")) is None
    denied(api.call("GET", f"/document-normalizations/{jid}"), (409,))


@pytest.mark.parametrize("surface", ["detail", "replay", "cancel"])
def test_guidance_source_revocation_blocks_results_but_owner_can_cancel(api, surface):
    shared = team(api)
    res, version = source(api, shared["id"])
    api.login(COLLAB)
    model = connection(api, shared["id"])
    key = svc.uid()
    queued, body = normalize(api, res, version, model, key)
    jid = queued.json()["id"]
    with api.app.state.session_factory.begin() as db:
        db.get(m.Job, jid).result = {"old_attempt": SECRET}
    api.login(OWNER)
    members(api, shared["id"], [{"user_id": OWNER, "roles": sorted(libraries.ALL_ROLES)}])
    api.login(COLLAB)
    if surface == "cancel":
        response = ok(api.call("POST", f"/jobs/{jid}/cancel"), 202)
        assert response.json()["result"] is None and SECRET not in response.text
    elif surface == "detail":
        denied(api.call("GET", f"/jobs/{jid}"), (404,))
    else:
        denied(api.call("POST", f"/documents/{res['id']}/normalizations", body, version.headers["etag"], key), (404,))


def retention_policy(api, sid, **changes):
    path = f"/spaces/{sid}/retention-policy"
    current = ok(api.call("GET", path))
    return ok(api.call("PUT", path, {"retention_days": 2, "approved": True,
        "reason": "synthetic explicit two-day approval", **changes}, current.headers["etag"]))


def expired_trash(api, sid):
    res, version = resource(api, sid, kind="document")
    ok(api.call("DELETE", f"/resources/{res['id']}", etag=f'"{res["revision"]}"'), 204)
    # Advance only synthetic timestamps; finish this fixture's invalidation marker.
    with api.app.state.session_factory.begin() as db:
        stored = db.get(m.Resource, res["id"])
        stored.deleted_at, stored.retain_until = svc.now() - timedelta(days=3), svc.now() - timedelta(days=1)
        for job in db.scalars(select(m.Job).where(m.Job.resource_id == stored.id, m.Job.kind == "INVALIDATE")):
            job.state, job.result = "SUCCEEDED", {"synthetic_setup": True}
        revision = stored.revision
    return res, version, f'"{revision}"'


def request_purge(api, rid, etag, key=None):
    return api.call("POST", f"/resources/{rid}/purge", {"reason": "synthetic purge request"}, etag, key)


def test_unconfigured_retention_refuses_purge_even_with_expired_timestamp(api):
    api.login(ADMIN)
    res, _, etag = expired_trash(api, SPACE)
    result = denied(request_purge(api, res["id"], etag), (409,))
    assert result.json()["code"] == "RETENTION_UNDEFINED"
    with api.app.state.session_factory() as db:
        assert not list(db.scalars(select(m.Job).where(m.Job.kind == "PURGE")))


@pytest.mark.parametrize("kind", ["personal", "team"])
def test_new_library_has_two_day_floor_and_preserves_longer_hold(api, kind):
    space = library(api, kind)
    policy = ok(api.call("GET", f"/spaces/{space['id']}/retention-policy")).json()
    assert policy["approved"] and policy["retention_days"] == 2 and not policy["automatic_purge"]
    res, _ = resource(api, space["id"], kind="document")
    future = svc.now() + timedelta(days=30)
    held = ok(api.call("PUT", f"/resources/{res['id']}/preservation", {
        "retain_until": svc.primitive(future), "reason": "synthetic longer hold"}, f'"{res["revision"]}"'))
    ok(api.call("DELETE", f"/resources/{res['id']}", etag=held.headers["etag"]), 204)
    with api.app.state.session_factory() as db:
        stored = db.get(m.Resource, res["id"])
        assert svc.aware(stored.retain_until) == future
    eligibility = ok(api.call("GET", f"/resources/{res['id']}/purge-eligibility")).json()
    assert not eligibility["eligible"]
    assert "RETENTION_NOT_EXPIRED" in {item["code"] for item in eligibility["reasons"]}


@pytest.mark.parametrize("blocker", ["withdrawn", "legal_hold", "revoked_owner", "snapshot"])
def test_purge_worker_rechecks_new_blocker_after_queue_without_deleting(api, blocker):
    space = library(api)
    res, version, etag = expired_trash(api, space["id"])
    queued = ok(request_purge(api, res["id"], etag), 202)
    jid = queued.json()["id"]
    if blocker == "withdrawn":
        retention_policy(api, space["id"], approved=False)
        expected = "RETENTION_UNAPPROVED"
    elif blocker == "legal_hold":
        ok(api.call("PUT", f"/resources/{res['id']}/preservation", {
            "legal_hold": True, "reason": "synthetic legal preservation"}, etag))
        expected = "LEGAL_HOLD"
    elif blocker == "revoked_owner":
        with api.app.state.session_factory.begin() as db:
            db.get(m.User, OWNER).active = False
        expected = "AUTH_REVOKED"
    else:
        snapshot_dependency(api, space["id"], res, version)
        expected = "INBOUND_DEPENDENCIES"
    done = run_job(api, jid)
    assert done["state"] == "FAILED" and done["result"] is None, done
    if blocker != "revoked_owner":
        assert done["error_code"] == expected, done
    with api.app.state.session_factory() as db:
        assert db.get(m.Resource, res["id"]) is not None
        assert db.get(m.ResourceVersion, version.json()["id"]) is not None
        assert not list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "resource.purged")))


def snapshot_dependency(api, sid, res, version):
    """Frozen audit snapshot without a live index row (e.g. restored sparse history)."""
    with api.app.state.session_factory.begin() as db:
        thread = m.ConsultationThread(id=svc.uid(), owner_id=OWNER, space_id=sid, title="synthetic frozen history")
        db.add(thread)
        db.flush()
        db.add(m.ConsultationRun(id=svc.uid(), thread_id=thread.id, state="FAILED", mode="answer", request={},
            evidence_snapshot=[{"resource_id": res["id"], "version_id": version.json()["id"],
                                "block_id": version.json()["blocks"][0]["block_id"],
                                "content_sha256": hashlib.sha256(SECRET.encode()).hexdigest()}]))


def test_purge_request_refuses_snapshot_only_dependency(api):
    space = library(api)
    res, version, etag = expired_trash(api, space["id"])
    snapshot_dependency(api, space["id"], res, version)
    response = denied(request_purge(api, res["id"], etag), (409,))
    assert response.json()["code"] == "INBOUND_DEPENDENCIES"


def test_purge_idempotency_replay_rechecks_withdrawn_policy(api):
    space = library(api)
    res, _, etag = expired_trash(api, space["id"])
    key = svc.uid()
    queued = ok(request_purge(api, res["id"], etag, key), 202)
    retention_policy(api, space["id"], approved=False)
    response = denied(request_purge(api, res["id"], etag, key), (409,))
    assert response.json()["code"] == "RETENTION_UNAPPROVED"
    with api.app.state.session_factory() as db:
        assert len(list(db.scalars(select(m.Job).where(m.Job.kind == "PURGE")))) == 1
        assert db.get(m.Job, queued.json()["id"]).state == "QUEUED"


class SyntheticEnvironment:
    def __init__(self, values=None, *, forbid=False):
        self.values, self.reads, self.forbid = values or {}, [], forbid

    def get(self, name, default=None):
        self.reads.append(name)
        assert not self.forbid, "Environment existence/value probed before reference authorization"
        assert name in self.values, "Only explicitly synthetic environment references may be read"
        return self.values.get(name, default)


@pytest.mark.parametrize("surface", ["create", "detail", "picker", "resolve"])
def test_environment_reference_guess_is_rejected_before_any_environment_read(api, monkeypatch, surface):
    private = library(api)
    env_name = "FKB_V6_SYNTHETIC_NOT_ASSIGNED"
    env = SyntheticEnvironment(forbid=True)
    monkeypatch.setattr(providers, "os", SimpleNamespace(environ=env))
    if surface == "create":
        response = api.call("POST", "/model-connections", {"space_id": private["id"],
            "name": "synthetic guessed env", "provider_id": "ollama", "credential_mode": "env", "api_key_env": env_name})
        denied(response, (403,))
        assert response.json()["code"] == "CREDENTIAL_REFERENCE_FORBIDDEN"
    else:
        model = connection(api, private["id"])
        cid = model.json()["id"]
        with api.app.state.session_factory.begin() as db:
            policy = db.get(m.RuntimePolicy, cid)
            policy.config = {**policy.config, "credential_mode": "env", "api_key_env": env_name}
        if surface == "detail":
            response = denied(api.call("GET", f"/model-connections/{cid}"), (403,))
            assert response.json()["code"] == "CREDENTIAL_REFERENCE_FORBIDDEN"
        elif surface == "picker":
            options = ok(api.call("GET", f"/model-options?space_id={private['id']}")).json()["items"]
            assert not any(item.get("selectable") for item in options)
        else:
            with api.app.state.session_factory() as db, pytest.raises(svc.APIError) as error:
                providers.resolve_connection(db, db.get(m.User, OWNER), private["id"], cid, MODEL, api.app.state.settings)
            assert error.value.code == "CREDENTIAL_REFERENCE_FORBIDDEN"
    assert env.reads == []


@pytest.mark.parametrize("surface", ["job", "normalization", "replay"])
def test_guidance_env_allowlist_revocation_blocks_old_results(api, monkeypatch, surface):
    from fund_kb.codex_bridge_config import configure_model_policy

    private = library(api)
    env_name = "FKB_V6_SYNTHETIC_OWN_KEY"
    env = SyntheticEnvironment({env_name: "synthetic-test-key"})
    monkeypatch.setattr(providers, "os", SimpleNamespace(environ=env))
    configure_model_policy(api.app.state.settings, env_refs_by_user={OWNER: [env_name]})
    model = connection(api, private["id"], credential_mode="env", api_key_env=env_name)
    res, version = source(api, private["id"])
    key = svc.uid()
    queued, body = normalize(api, res, version, model, key)
    jid = queued.json()["id"]
    with api.app.state.session_factory.begin() as db:
        db.get(m.Job, jid).result = {"old_attempt": SECRET}
    configure_model_policy(api.app.state.settings, env_refs_by_user={})
    env.forbid = True
    if surface == "job":
        denied(api.call("GET", f"/jobs/{jid}"), (403,))
    elif surface == "normalization":
        denied(api.call("GET", f"/document-normalizations/{jid}"), (403,))
    else:
        denied(api.call("POST", f"/documents/{res['id']}/normalizations", body, version.headers["etag"], key), (403,))


class MemoryAuthStore:
    """Fake transport storage: names only, no filesystem or real auth material."""
    def __init__(self, root):
        self.root, self.identities, self.persisted = root, {}, []

    def materialize(self, owner, connection, epoch):
        home = self.root / svc.uid()
        self.identities[home] = (owner, connection, epoch)
        return home

    def release(self, home):
        self.identities.pop(home)

    def persist(self, home):
        assert home in self.identities
        self.persisted.append(self.identities[home])

    def forget(self, owner, connection):
        pass


class SyntheticAuthRPC:
    def __init__(self, config, home):
        self.home, self.calls, self.events = home, [], []
        self.closed, self.account = False, None
        self.login_id, self.code = svc.uid(), "V6-FAKE-CODE"

    def call(self, method, params=None):
        from fund_kb.codex_bridge import AUTH_METHODS
        assert method in AUTH_METHODS
        self.calls.append((method, params))
        if method == "account/read":
            return {"account": self.account}
        if method == "account/login/start":
            return {"loginId": self.login_id, "verificationUrl": "https://auth.openai.com/codex/device",
                    "userCode": self.code}
        if method == "account/logout":
            self.account = None
        return {}

    def notifications(self):
        events, self.events = self.events, []
        return events

    def close(self):
        self.closed = True


@pytest.fixture
def auth_bridge(tmp_path):
    from fund_kb.codex_bridge import CodexBridge
    from fund_kb.codex_bridge_config import CodexBridgeConfig

    bridge = CodexBridge(CodexBridgeConfig(enabled=True), store=MemoryAuthStore(tmp_path),
                         transport_factory=SyntheticAuthRPC)
    yield bridge
    bridge.close()


def codex_connection(api, sid):
    return connection(api, sid, provider_id="chatgpt-codex", credential_mode="chatgpt_oauth", custom_models=[])


@pytest.mark.parametrize("intruder", [ADMIN, COLLAB, READER])
def test_oauth_all_endpoints_owner_only_even_for_other_admin(api, intruder):
    shared = team(api)
    model = codex_connection(api, shared["id"])
    base = f"/model-connections/{model.json()['id']}/oauth"
    api.login(intruder)
    denied(api.call("GET", base), (404,))
    denied(api.call("GET", base + f"/challenge?attempt_id={svc.uid()}"), (404,))
    for suffix, body in (("start", {"flow": "device_code"}), ("refresh", {}),
                          ("cancel", {"attempt_id": svc.uid()}), ("logout", {})):
        denied(api.call("POST", base + "/" + suffix, {"connection_revision": 1, **body}, '"1"'), (404,))


def test_oauth_default_disabled_and_inference_http_always_blocked(api):
    model = codex_connection(api, library(api)["id"])
    cid = model.json()["id"]
    state = ok(api.call("GET", f"/model-connections/{cid}/oauth"))
    assert state.json()["capabilities"]["inference"] is False
    assert state.json()["capabilities"]["auth"] is False
    response = denied(api.call("POST", f"/model-connections/{cid}/oauth/start", {
        "connection_revision": 1, "flow": "device_code"}, state.headers["etag"]), (409,))
    assert response.json()["code"] == "CODEX_BRIDGE_DISABLED"
    response = denied(api.call("POST", f"/model-connections/{cid}/test", {}, model.headers["etag"]), (409,))
    assert response.json()["code"] == "CODEX_TEXT_ISOLATION_UNVERIFIED"
    with api.app.state.session_factory() as db:
        assert list(db.scalars(select(m.Job))) == []


def test_oauth_fake_challenge_cancel_epoch_replay_and_no_durable_login_code(api, auth_bridge):
    api.app.state.codex_bridge = auth_bridge
    model = codex_connection(api, library(api)["id"])
    cid = model.json()["id"]
    path, key = f"/model-connections/{cid}/oauth/start", svc.uid()
    body = {"connection_revision": 1, "flow": "device_code"}
    queued = ok(api.call("POST", path, body, '"1"', key), 202)
    done = run_job(api, queued.json()["id"])
    assert done["state"] == "SUCCEEDED", done
    state = ok(api.call("GET", f"/model-connections/{cid}/oauth"))
    attempt = state.json()["attempt_id"]
    challenge = ok(api.call("GET", f"/model-connections/{cid}/oauth/challenge?attempt_id={attempt}"))
    assert challenge.json()["user_code"] == "V6-FAKE-CODE"
    assert "no-store" in challenge.headers["cache-control"]
    assert challenge.headers["referrer-policy"] == "no-referrer"
    cancel = ok(api.call("POST", f"/model-connections/{cid}/oauth/cancel", {
        "connection_revision": 1, "attempt_id": attempt}, state.headers["etag"]), 202)
    denied(api.call("GET", f"/model-connections/{cid}/oauth/challenge?attempt_id={attempt}"), (409,))
    denied(api.call("POST", path, body, '"1"', key), (409,))
    assert run_job(api, cancel.json()["id"])["state"] == "SUCCEEDED"
    assert (OWNER, cid) not in auth_bridge.sessions
    with api.app.state.session_factory() as db:
        for cls, field in ((m.Job, "result"), (m.Job, "payload"), (m.IdempotencyRecord, "response"),
                           (m.AuditEvent, "details"), (m.RuntimePolicy, "config")):
            for row in db.scalars(select(cls)):
                assert "V6-FAKE-CODE" not in json.dumps(getattr(row, field))


def test_bridge_identity_binding_and_late_success_cannot_cross_owner_or_cancel(auth_bridge):
    cid, attempt = svc.uid(), svc.uid()
    auth_bridge.start(OWNER, cid, 1, 1, attempt, "device_code")
    session = auth_bridge.sessions[OWNER, cid]
    for args in ((ADMIN, cid, 1, 1, attempt), (OWNER, svc.uid(), 1, 1, attempt),
                 (OWNER, cid, 2, 1, attempt), (OWNER, cid, 1, 2, attempt), (OWNER, cid, 1, 1, svc.uid())):
        with pytest.raises(providers.ProviderError) as error:
            auth_bridge.challenge(*args)
        assert error.value.code == "CODEX_LOGIN_STALE"
    auth_bridge.cancel(ADMIN, cid)
    assert auth_bridge.sessions[OWNER, cid] is session and not session.rpc.closed
    auth_bridge.cancel(OWNER, cid)
    session.rpc.account = {"type": "chatgpt", "planType": "pro"}
    session.rpc.events.append({"method": "account/login/completed", "params": {"loginId": session.login_id, "success": True}})
    with pytest.raises(providers.ProviderError):
        auth_bridge.refresh(OWNER, cid, 1, 1)
    assert session.rpc.closed and auth_bridge.store.persisted == []


@pytest.mark.parametrize("url", ["http://auth.openai.com/codex/device", "javascript:alert(1)",
    "https://auth.openai.com.evil.invalid/codex/device", "https://auth.openai.com@evil.invalid/",
    "https://evil.invalid@auth.openai.com/", "https://auth.openai.com:444/codex/device",
    "https://auth.openai.com/codex/device#token", "https://auth.openai.com\\@evil.invalid/",
    "https://auth.openai.com:notaport/codex/device"])
def test_bridge_rejects_untrusted_login_urls_with_safe_error(url):
    from fund_kb.codex_bridge import official_login_url
    with pytest.raises(providers.ProviderError) as error:
        official_login_url(url, device=True)
    assert error.value.code == "CODEX_LOGIN_URL_INVALID"


@pytest.mark.parametrize("method,params", [("thread/start", {}), ("turn/start", {}),
    ("command/exec", {}), ("fs/readFile", {}), ("account/login/start", {"type": "chatgptAuthTokens"})])
def test_auth_transport_forbids_arbitrary_rpc_before_io(method, params):
    from fund_kb.codex_bridge import AuthStdioTransport
    transport = object.__new__(AuthStdioTransport)
    with pytest.raises(providers.ProviderError) as error:
        transport.call(method, params)
    assert error.value.code == "CODEX_RPC_FORBIDDEN"


@pytest.mark.parametrize("engine_present,use_transport", [(False, False), (False, True), (True, True)],
                         ids=["missing-engine", "missing-engine-with-transport", "engine-with-transport"])
def test_codex_completion_gate_precedes_transport_or_any_message_handling(monkeypatch, engine_present, use_transport):
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("called")
        raise AssertionError("Isolation must reject before messages, credentials, engine or transport")

    monkeypatch.setattr(providers, "_messages", forbidden)
    monkeypatch.setattr(providers, "_master_key", forbidden)
    monkeypatch.setattr(providers, "_network", forbidden)
    snapshot = {"protocol": "codex_app_server"}
    if engine_present:
        snapshot["_codex_engine"] = SimpleNamespace(complete=forbidden)
    with pytest.raises(providers.ProviderError) as error:
        providers.complete(snapshot, object(), transport=forbidden if use_transport else None)
    assert error.value.code == "CODEX_TEXT_ISOLATION_UNVERIFIED"
    assert calls == []


def test_unknown_server_rpc_request_rejects_and_terminates_auth_process():
    from fund_kb.codex_bridge import AuthStdioTransport
    from fund_kb.codex_bridge_config import CodexBridgeConfig

    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b'{"id":7,"method":"command/exec","params":{}}\n')
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    transport = object.__new__(AuthStdioTransport)
    transport.config, transport.process = CodexBridgeConfig(), Process()
    transport.responses, transport.events = queue.Queue(), queue.Queue()
    transport.lock, transport.failed, transport.counter = threading.Lock(), False, 0
    transport._read()
    assert transport.failed
    assert transport.process.terminated, "Unknown server execution request left auth process running"


def test_production_bridge_cannot_enable_without_runtime_and_version_attestation():
    from fund_kb.codex_bridge import CodexBridge
    from fund_kb.codex_bridge_config import CodexBridgeConfig
    # Metadata only: never instantiate the transport or execute this path.
    bridge = CodexBridge(CodexBridgeConfig(enabled=True, executable=Path(__file__),
        auth_runtime_verified=False, expected_version="", executable_sha256=""), store=object())
    assert bridge.unavailable_reason is not None, "Unattested production auth transport reported available"


@pytest.mark.parametrize("mutation", ["owner", "connection", "epoch"])
def test_encrypted_synthetic_auth_archive_rejects_binding_copy(tmp_path, mutation):
    from cryptography.fernet import Fernet

    from fund_kb.codex_bridge import EncryptedAuthStore, _write_private
    from fund_kb.codex_bridge_config import CodexBridgeConfig

    # Fresh test-only identity and bytes; never opens a host/runtime credential.
    root = tmp_path.resolve()
    store = EncryptedAuthStore(CodexBridgeConfig(runtime_root=root / "runtimes", archive_root=root / "archives"),
                               Fernet.generate_key())
    cid, other = svc.uid(), svc.uid()
    home = store.materialize(OWNER, cid, 1)
    _write_private(home / "auth.json", b'{"synthetic": "not-a-real-auth-token"}')
    store.persist(home)
    encrypted = (store.archive / f"{OWNER}.{cid}.enc").read_bytes()
    store.release(home)
    target_owner, target_cid, target_epoch = OWNER, cid, 1
    if mutation == "owner":
        target_owner = ADMIN
    elif mutation == "connection":
        target_cid = other
    else:
        target_epoch = 2
    if mutation != "epoch":
        _write_private(store.archive / f"{target_owner}.{target_cid}.enc", encrypted)
    with pytest.raises(providers.ProviderError) as error:
        store.materialize(target_owner, target_cid, target_epoch, restore=True)
    assert error.value.code == "CODEX_AUTH_BINDING_INVALID"
    assert store.runtimes == {}


@pytest.mark.parametrize("field", ["owner_user_id", "connection_id"])
def test_forged_model_job_payload_cannot_borrow_other_owner(api, field):
    shared = team(api)
    own = connection(api, shared["id"])
    queued = ok(api.call("POST", f"/model-connections/{own.json()['id']}/test", {}, own.headers["etag"]), 202)
    api.login(COLLAB)
    other = connection(api, shared["id"])
    api.login(OWNER)
    with api.app.state.session_factory.begin() as db:
        job = db.get(m.Job, queued.json()["id"])
        job.payload = {**job.payload, field: COLLAB if field == "owner_user_id" else other.json()["id"]}
        job.result = {"old_attempt": SECRET}
    denied(api.call("GET", f"/jobs/{queued.json()['id']}"), (404,))
    assert SECRET not in ok(api.call("GET", "/jobs")).text
    result = run_job(api, queued.json()["id"])
    assert result["state"] == "FAILED", result
