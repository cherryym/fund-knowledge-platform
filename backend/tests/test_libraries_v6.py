"""Synthetic SQLite/HTTP ACL matrix; never reads a runtime DB or invokes a model.

Run: .venv/bin/python -m pytest tests/test_libraries_v6.py
Uses the actual extension loader and central HTTP wrapper, not a replacement API.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, update
from test_api import EDITOR, OUTSIDER, READER, REVIEWER, SPACE
from test_api import api as _api_fixture
from test_api import jobs as _api_jobs_fixture

from fund_kb import api_catalog, api_libraries, libraries
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.api import validator
from fund_kb.jobs import JobDispatcher

api = _api_fixture
jobs = _api_jobs_fixture


def create(api, kind="team", who=EDITOR):
    api.login(who)
    response = api.call("POST", "/libraries", {"name": "团队合成库" if kind == "team" else "个人合成库", "kind": kind})
    assert response.status_code == 201, response.text
    return response.json()


def members(api, library, items):
    current = api.call("GET", f"/libraries/{library['id']}/members")
    assert current.status_code == 200, current.text
    response = api.call("PUT", f"/libraries/{library['id']}/members", {"items": items}, current.headers["etag"])
    assert response.status_code == 200, response.text
    return response


def team(api):
    library = create(api)
    members(api, library, [{"user_id": EDITOR, "roles": sorted(libraries.ALL_ROLES)},
        {"user_id": REVIEWER, "roles": ["editor", "reviewer"]}])
    return library


def resource(api, library, kind="knowledge"):
    response = api.call("POST", "/resources", {"space_id": library["id"], "kind": kind, "name": "合成机密条款"})
    assert response.status_code == 201, response.text
    draft = api.draft(response.json())
    return response.json(), draft


def frozen_fixture(api, r, v):
    """Fixture only: install a frozen published row to exercise read ACL independently of review."""
    with api.app.state.session_factory.begin() as db:
        version = db.get(m.ResourceVersion, v.json()["id"])
        bid = svc.uid()
        text = "合成检索证据字段"
        db.add(m.ContentBlock(version_id=version.id, block_id=bid, ordinal=0, block_type="paragraph",
            data={"text": text}, locator={}, search_text=text, content_sha256=svc.hashlib.sha256(text.encode()).hexdigest()))
        db.flush()
        version.valid_from, version.legal_status = date(2020, 1, 1), "NOT_APPLICABLE"
        version.content_sha256 = svc.check_frozen_hash(db, version)
        version.state = "APPROVED"
        release = m.Release(id=svc.uid(), resource_id=r["id"], version_id=version.id,
            state="ACTIVE", activated_at=svc.now(), publisher_id=EDITOR)
        db.add(release)
        db.flush()
        db.get(m.Resource, r["id"]).active_release_id = release.id


def context(db, user, operation, obj_id=None, data=None, etag=None):
    request = SimpleNamespace(path_params={"id": obj_id}, headers={"if-match": etag} if etag else {},
        state=SimpleNamespace(trace_id=svc.uid()), app=SimpleNamespace(state=SimpleNamespace()))
    return svc.Context(request, db, user, data or {}, {}, operation)


@pytest.mark.parametrize("who", [EDITOR, REVIEWER, READER, OUTSIDER])
@pytest.mark.parametrize("kind", ["personal", "team"])
def test_every_active_identity_can_create_and_projection_matches(api, who, kind):
    library = create(api, kind, who)
    assert library["owner_id"] == who and library["kind"] == kind and library["governed"] is True
    assert set(library["roles"]) == libraries.ALL_ROLES
    actual = api.call("GET", "/libraries").json()["items"]
    assert library in actual
    assert api.call("GET", "/me").json()["spaces"] == actual
    assert api.call("GET", "/spaces").json() == actual
    assert api.call("GET", f"/libraries/{library['id']}").headers["etag"] == '"1"'
    if who != EDITOR:
        assert api.call("POST", "/spaces", {"name": "旧接口不开放"}).status_code == 403


@pytest.mark.parametrize("who", [EDITOR, REVIEWER, READER])
def test_personal_hard_isolation_even_admin_members_and_grants(api, who):
    library = create(api, "personal", OUTSIDER)
    r, v = resource(api, library)
    with api.app.state.session_factory.begin() as db:
        for role in libraries.ALL_ROLES:
            db.add(m.SpaceMember(space_id=library["id"], user_id=who, role=role))
        for grant in ["read", "download", "edit", "manage", "publish", "review"]:
            db.add(m.ResourceGrant(resource_id=r["id"], user_id=who, permission=grant))
        job_id = svc.uid()
        db.add(m.Job(id=job_id, kind="EXPORT", state="SUCCEEDED", owner_id=who,
            resource_id=r["id"], version_id=v.json()["id"], payload={"version_ids": [v.json()["id"]]},
            result={"private": "绝不返回"}, dedupe_key=job_id))
    api.login(who)
    assert library["id"] not in [item["id"] for item in api.call("GET", "/libraries").json()["items"]]
    for path in [f"/libraries/{library['id']}", f"/libraries/{library['id']}/members",
        f"/spaces/{library['id']}/members", f"/resources/{r['id']}", f"/versions/{v.json()['id']}",
        f"/resources/{r['id']}/versions", f"/versions/{v.json()['id']}/content?representation=markdown",
        f"/wiki/graph?space_id={library['id']}", f"/wiki/workspace?space_id={library['id']}",
        f"/users/directory?space_id={library['id']}", f"/jobs/{job_id}"]:
        denied = api.call("GET", path)
        assert denied.status_code == 404, (path, denied.text)
        assert "合成机密" not in denied.text and "绝不返回" not in denied.text
    assert api.call("POST", "/search", {"space_id": library["id"], "query": "合成"}).status_code == 404
    with api.app.state.session_factory() as db:
        user = db.get(m.User, who)
        assert svc.roles(db, user, library["id"]) == set()
        for action in ["read", "download", "edit", "manage", "publish", "review"]:
            with pytest.raises(svc.APIError) as exc:
                svc.resource_access(db, user, r["id"], action)
            assert exc.value.status == 404


@pytest.mark.parametrize("who, expected_roles, can_draft", [
    (EDITOR, libraries.ALL_ROLES, True), (REVIEWER, {"reader", "editor", "reviewer"}, True),
    (READER, {"reader"}, False), (OUTSIDER, {"reader"}, False)])
def test_team_matrix_and_draft_visibility(api, who, expected_roles, can_draft):
    library = team(api)
    r, v = resource(api, library)
    api.login(who)
    assert set(api.call("GET", f"/libraries/{library['id']}").json()["roles"]) == expected_roles
    assert api.call("GET", f"/versions/{v.json()['id']}").status_code == (200 if can_draft else 404)
    assert api.call("GET", f"/resources/{r['id']}").status_code == (200 if can_draft else 404)
    listed = api.call("GET", f"/resources?space_id={library['id']}").json()["items"]
    assert bool(listed) == can_draft
    graph = api.call("GET", f"/wiki/graph?space_id={library['id']}")
    assert graph.status_code == 200
    assert bool(graph.json()["nodes"]) == can_draft
    with api.app.state.session_factory() as db:
        assert svc.can_edit_draft(db, db.get(m.User, who), db.get(m.ResourceVersion, v.json()["id"])) == can_draft


def test_team_publication_is_readable_by_active_nonmember_but_restricted_is_extra_gate(api):
    library = team(api)
    r, v = resource(api, library)
    frozen_fixture(api, r, v)
    api.login(OUTSIDER)
    assert api.call("GET", f"/versions/{v.json()['id']}").status_code == 200
    search = api.call("POST", "/search", {"space_id": library["id"], "query": "合成"})
    assert search.status_code == 200 and len(search.json()["items"]) == 1, search.text
    with api.app.state.session_factory.begin() as db:
        db.get(m.Resource, r["id"]).restricted = True
    assert api.call("GET", f"/versions/{v.json()['id']}").status_code == 404
    assert api.call("POST", "/search", {"space_id": library["id"], "query": "合成"}).json()["items"] == []
    with api.app.state.session_factory.begin() as db:
        db.add(m.ResourceGrant(resource_id=r["id"], user_id=OUTSIDER, permission="read"))
    assert api.call("GET", f"/versions/{v.json()['id']}").status_code == 200
    assert api.call("GET", f"/versions/{v.json()['id']}/content?representation=source").status_code == 404


def test_legacy_stays_private_and_only_explicit_admin_can_govern(api):
    api.login(OUTSIDER)
    assert api.call("GET", f"/libraries/{SPACE}").status_code == 404
    assert api.call("POST", f"/libraries/{SPACE}/govern", {"kind": "team"}, '"1"').status_code == 404
    api.login(REVIEWER)
    assert api.call("POST", f"/libraries/{SPACE}/govern", {"kind": "team"}, '"1"').status_code == 403
    api.login()
    _r, v = resource(api, {"id": SPACE})
    with api.app.state.session_factory.begin() as db:
        db.add(m.SpaceMember(space_id=SPACE, user_id=READER, role="editor"))
    api.login(READER)
    assert api.call("GET", f"/versions/{v.json()['id']}").status_code == 404
    with api.app.state.session_factory() as db:
        assert not svc.can_edit_draft(db, db.get(m.User, READER), db.get(m.ResourceVersion, v.json()["id"]))
    api.login()
    before = api.call("GET", f"/libraries/{SPACE}")
    assert before.json()["kind"] == "legacy" and before.json()["owner_id"] is None
    assert api.call("PATCH", f"/libraries/{SPACE}", {"kind": "team"}, before.headers["etag"]).status_code == 422
    converted = api.call("POST", f"/libraries/{SPACE}/govern", {"kind": "team"}, before.headers["etag"])
    assert converted.status_code == 200 and converted.json()["governed"], converted.text
    assert converted.json()["revision"] == before.json()["revision"] + 1
    assert api.call("POST", f"/libraries/{SPACE}/govern", {"kind": "team"}, converted.headers["etag"]).status_code == 409
    api.login(OUTSIDER)
    assert api.call("GET", f"/libraries/{SPACE}").json()["roles"] == ["reader"]
    api.login(READER)
    assert api.call("GET", f"/versions/{v.json()['id']}").status_code == 200


def test_member_etag_last_admin_personal_immutability_and_directory_minimization(api):
    library = team(api)
    directory = api.call("GET", f"/users/directory?space_id={library['id']}")
    assert directory.status_code == 200 and len(directory.json()["items"]) == 4
    assert all(set(item) == {"id", "display_name"} for item in directory.json()["items"])
    current = api.call("GET", f"/libraries/{library['id']}/members")
    items = [{"user_id": item["user_id"], "roles": item["roles"]} for item in current.json()["items"]]
    stale = current.headers["etag"]
    changed = api.call("PUT", f"/libraries/{library['id']}/members", {"items": items}, stale)
    assert changed.status_code == 200
    assert api.call("PUT", f"/libraries/{library['id']}/members", {"items": items}, stale).status_code == 412
    assert api.call("PUT", f"/libraries/{library['id']}/members", {"items": []}, changed.headers["etag"]).json()["code"] == "LAST_ADMIN"
    api.login(OUTSIDER)
    assert api.call("GET", f"/users/directory?space_id={library['id']}").status_code == 403
    assert api.call("GET", "/users/directory").status_code == 400
    personal = create(api, "personal", OUTSIDER)
    directory = api.call("GET", f"/users/directory?space_id={personal['id']}").json()
    assert [item["id"] for item in directory["items"]] == [OUTSIDER]
    for path, payload in [(f"/libraries/{personal['id']}/members", {"items": items}),
                          (f"/spaces/{personal['id']}/members", items)]:
        rejected = api.call("PUT", path, payload, '"1"')
        assert rejected.status_code == 409 and rejected.json()["code"] == "PERSONAL_MEMBERS_IMMUTABLE"


def test_extensions_enforce_csrf_idempotency_schema_and_revision(api):
    assert api.client.post("/api/v1/libraries", json={"name": "测试", "kind": "team"},
        headers={"Origin": "http://testserver"}).status_code == 403
    assert api.call("POST", "/libraries", {"name": "测试", "kind": "team"},
        headers={"Idempotency-Key": ""}).status_code == 422
    for payload in [{"name": "   ", "kind": "team"}, {"name": "测试", "kind": "legacy"},
        {"name": "测试", "kind": "team", "owner_id": OUTSIDER}]:
        assert api.call("POST", "/libraries", payload).status_code == 422
    library = create(api)
    assert api.call("PATCH", f"/libraries/{library['id']}", {"name": "修改"}).status_code == 428
    changed = api.call("PATCH", f"/libraries/{library['id']}", {"name": "修改"}, '"1"')
    assert changed.status_code == 200 and changed.headers["etag"] == '"2"'
    assert api.call("PATCH", f"/libraries/{library['id']}", {"name": "覆盖"}, '"1"').status_code == 412
    document = api.app.openapi()
    validator(api_libraries.SCHEMAS["Library"], document).validate(changed.json())
    assert document["components"]["schemas"]["Space"] == api_libraries.SCHEMAS["Library"]


def test_replay_rechecks_state_governance_and_member_authority(api):
    payload = {"name": "重放库", "kind": "team"}
    key = svc.uid()
    first = api.call("POST", "/libraries", payload, key=key)
    library = first.json()
    replay = api.call("POST", "/libraries", payload, key=key)
    assert replay.status_code == 201 and replay.json() == library
    updated = api.call("PATCH", f"/libraries/{library['id']}", {"name": "新名称"}, first.headers["etag"])
    assert updated.status_code == 200
    assert api.call("POST", "/libraries", payload, key=key).json()["code"] == "LIBRARY_REPLAY_STATE_CHANGED"
    members_key = svc.uid()
    body = {"items": [{"user_id": EDITOR, "roles": ["admin", "editor"]},
        {"user_id": REVIEWER, "roles": ["admin"]}]}
    replaced = api.call("PUT", f"/libraries/{library['id']}/members", body, updated.headers["etag"], key=members_key)
    assert replaced.status_code == 200
    api.login(REVIEWER)
    members(api, library, [{"user_id": REVIEWER, "roles": ["admin"]}])
    api.login()
    denied = api.call("PUT", f"/libraries/{library['id']}/members", body, updated.headers["etag"], key=members_key)
    assert denied.status_code == 403


def test_malformed_policy_does_not_fall_back_to_legacy(api):
    library = create(api, "personal")
    with api.app.state.session_factory.begin() as db:
        policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == libraries.POLICY_PREFIX + library["id"]))
        policy.config = {"kind": "team"}
    assert api.call("GET", f"/libraries/{library['id']}").status_code == 404
    assert library["id"] not in [item["id"] for item in api.call("GET", "/me").json()["spaces"]]
    assert api.call("POST", f"/libraries/{library['id']}/govern", {"kind": "team"}, '"1"').status_code == 404


def test_personal_self_review_and_publish_without_review_remain_forbidden(api):
    library = create(api, "personal")
    _r, v = resource(api, library)
    edited = api.edit(v)
    submitted = api.call("POST", f"/versions/{v.json()['id']}/submit", etag=edited.headers["etag"])
    assert submitted.status_code == 200, submitted.text
    review = {"decision": "APPROVE", "reviewed_sha256": submitted.json()["content_sha256"], "comment": "自审不可用"}
    denied = api.call("POST", f"/versions/{v.json()['id']}/reviews", review, submitted.headers["etag"])
    assert denied.status_code == 403 and denied.json()["code"] == "SELF_REVIEW_FORBIDDEN"
    assert api.call("POST", f"/versions/{v.json()['id']}/publish", etag=submitted.headers["etag"]).status_code == 409


def test_team_shared_edit_etag_conflict_and_coauthor_self_review(api):
    library = team(api)
    _r, v = resource(api, library)
    api.login(REVIEWER)
    data = {key: v.json()[key] for key in ["title", "knowledge_type", "applicability", "required_facts",
        "legal_status", "valid_from", "valid_to", "blocks"]}
    data.update(title="共同编辑者的修改", legal_status="NOT_APPLICABLE", valid_from="2020-01-01",
        blocks=[{"block_id": svc.uid(), "ordinal": 0, "block_type": "paragraph", "data": {"text": "团队共同草稿"},
            "locator": {}, "citations": []}])
    edited = api.call("PATCH", f"/versions/{v.json()['id']}", data, v.headers["etag"])
    assert edited.status_code == 200, "Main thread must wire api_content.draft -> services.require_draft: " + edited.text
    api.login()
    conflict = api.call("PATCH", f"/versions/{v.json()['id']}", {**data, "title": "不能覆盖"}, v.headers["etag"])
    assert conflict.status_code == 412
    submitted = api.call("POST", f"/versions/{v.json()['id']}/submit", etag=edited.headers["etag"])
    assert submitted.status_code == 200
    api.login(REVIEWER)
    review = {"decision": "APPROVE", "reviewed_sha256": submitted.json()["content_sha256"], "comment": "共同编辑者不能自审"}
    denied = api.call("POST", f"/versions/{v.json()['id']}/reviews", review, submitted.headers["etag"])
    assert denied.status_code == 403 and denied.json()["code"] == "SELF_REVIEW_FORBIDDEN"
    api.login()
    members(api, library, [{"user_id": EDITOR, "roles": sorted(libraries.ALL_ROLES)},
        {"user_id": REVIEWER, "roles": ["editor", "reviewer"]},
        {"user_id": READER, "roles": ["reviewer"]}])
    api.login(READER)
    approved = api.call("POST", f"/versions/{v.json()['id']}/reviews", {**review, "comment": "未参与编辑的独立审核"}, submitted.headers["etag"])
    assert approved.status_code == 201, approved.text
    api.login()
    current = api.call("GET", f"/versions/{v.json()['id']}")
    published = api.call("POST", f"/versions/{v.json()['id']}/publish", etag=current.headers["etag"])
    assert published.status_code == 202, published.text  # queued, not a publication-success claim


def test_worker_rechecks_revoked_edit_and_deactivated_identity(api):
    library = team(api)
    _r, v = resource(api, library)
    # Exercise the real worker ACL kernel without starting a dispatcher/service.
    worker = object.__new__(JobDispatcher)
    with api.app.state.session_factory() as db:
        user = db.get(m.User, REVIEWER)
        assert worker._version(db, user, v.json()["id"], "edit")[0].id == v.json()["id"]
        db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == library["id"],
            m.SpaceMember.user_id == REVIEWER))
        with pytest.raises(svc.APIError) as denied:
            worker._version(db, user, v.json()["id"], "edit")
        assert denied.value.status == 403
        assert svc.roles(db, user, library["id"]) == {"reader"}
        db.execute(update(m.User).where(m.User.id == REVIEWER).values(active=False)
            .execution_options(synchronize_session=False))
        assert user.active is True  # ORM object is deliberately stale.
        assert svc.roles(db, user, library["id"]) == set()
        with pytest.raises(svc.APIError):
            svc.version_access(db, user, v.json()["id"])


def test_cross_library_move_rejected_in_http_and_catalog_direct_call(api):
    personal = create(api, "personal")
    r, _v = resource(api, personal)
    target = create(api, "team")
    path = f"/resources/{r['id']}"
    etag = api.call("GET", path).headers["etag"]
    assert api.call("PATCH", path, {"space_id": target["id"]}, etag).status_code == 422
    with api.app.state.session_factory() as db:
        ctx = context(db, db.get(m.User, EDITOR), "updateResourceMetadata", r["id"], {"space_id": target["id"]}, etag)
        with pytest.raises(svc.APIError) as denied:
            api_catalog.update_resource(ctx)
        assert denied.value.code == "RESOURCE_BOUNDARY_IMMUTABLE"
        assert db.get(m.Resource, r["id"]).space_id == personal["id"]


def test_default_retention_only_new_libraries_and_trash_never_shortens(api):
    from fund_kb.retention import POLICY_PREFIX
    personal = create(api, "personal")
    library = create(api, "team")
    with api.app.state.session_factory() as db:
        policies = db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like(POLICY_PREFIX + "%"))).all()
        assert {p.config["space_id"] for p in policies} == {personal["id"], library["id"]}
        assert all(p.config["retention_days"] == 2 and p.config["approved"] for p in policies)
    assert api.call("PATCH", f"/libraries/{SPACE}", {"name": "旧库更名"}, '"1"').status_code == 200
    assert api.call("POST", f"/libraries/{SPACE}/govern", {"kind": "team"}, '"2"').status_code == 200
    with api.app.state.session_factory() as db:
        assert db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == POLICY_PREFIX + SPACE)) is None
    r, _v = resource(api, personal)
    current = api.call("GET", f"/resources/{r['id']}")
    assert api.call("DELETE", f"/resources/{r['id']}", etag=current.headers["etag"]).status_code == 204
    with api.app.state.session_factory() as db:
        stored = db.get(m.Resource, r["id"])
        assert stored.retain_until - stored.deleted_at == timedelta(days=2)
    r2, _v2 = resource(api, library)
    longer = svc.now() + timedelta(days=30)
    with api.app.state.session_factory.begin() as db:
        db.get(m.Resource, r2["id"]).retain_until = longer
    current = api.call("GET", f"/resources/{r2['id']}")
    assert api.call("DELETE", f"/resources/{r2['id']}", etag=current.headers["etag"]).status_code == 204
    with api.app.state.session_factory() as db:
        assert svc.aware(db.get(m.Resource, r2["id"]).retain_until) == longer


def test_failed_default_retention_rolls_back_library_members_and_governance(api, monkeypatch):
    from fund_kb import retention
    def reject(*args):
        svc.fail(409, "SYNTHETIC_POLICY_FAILURE", "合成事务回滚测试")
    monkeypatch.setattr(retention, "create_default_policy", reject)
    denied = api.call("POST", "/libraries", {"name": "应完整回滚", "kind": "personal"})
    assert denied.status_code == 409
    with api.app.state.session_factory() as db:
        assert list(db.scalars(select(m.Space.id))) == [SPACE]
        assert list(db.scalars(select(m.RuntimePolicy.id))) == []
        assert set(db.scalars(select(m.SpaceMember.space_id))) == {SPACE}


def test_catalog_purge_configured_editor_role_still_requires_resource_manage(api):
    library = team(api)
    r, _v = resource(api, library)
    policy = api.call("GET", f"/spaces/{library['id']}/retention-policy")
    changed = api.call("PUT", f"/spaces/{library['id']}/retention-policy", {
        "retention_days": 2, "approved": True, "purge_allowed_roles": ["editor"],
        "backfill_trash": False, "reason": "合成测试指定编辑者须另有资源管理授权"}, policy.headers["etag"])
    assert changed.status_code == 200, changed.text
    with api.app.state.session_factory.begin() as db:
        stored = db.get(m.Resource, r["id"])
        stored.deleted_at = svc.now() - timedelta(days=3)
        stored.retain_until = svc.now() - timedelta(days=1)
        etag = f'"{stored.revision + 1}"'
    api.login(REVIEWER)
    denied = api.call("POST", f"/resources/{r['id']}/purge", {"reason": "合成过期请求"}, etag)
    assert denied.status_code == 403
    with api.app.state.session_factory.begin() as db:
        db.add(m.ResourceGrant(resource_id=r["id"], user_id=REVIEWER, permission="manage"))
        etag = f'"{db.get(m.Resource, r["id"]).revision}"'
    queued = api.call("POST", f"/resources/{r['id']}/purge", {"reason": "合成过期请求"}, etag)
    assert queued.status_code == 202, queued.text
    with api.app.state.session_factory() as db:
        assert db.get(m.Resource, r["id"]) is not None  # Queue only; no worker/deletion.
        assert db.get(m.Job, queued.json()["id"]).owner_id == REVIEWER


def test_catalog_document_category_hook_prevents_nonexistent_category(api):
    library = create(api)
    denied = api.call("POST", "/resources", {"space_id": library["id"], "kind": "document",
        "name": "未建分类", "category": "不存在的分类"})
    assert denied.status_code == 409 and denied.json()["code"] == "DOCUMENT_CATEGORY_UNAVAILABLE"
    r, _v = resource(api, library, "document")
    current = api.call("GET", f"/resources/{r['id']}")
    denied = api.call("PATCH", f"/resources/{r['id']}", {"category": "不存在的分类"}, current.headers["etag"])
    assert denied.status_code == 409 and denied.json()["code"] == "DOCUMENT_CATEGORY_UNAVAILABLE"
    assert api.call("GET", f"/resources/{r['id']}").json()["category"] == "未分类"


def test_transitive_personal_source_cannot_leak_through_team_version_search_or_graph(api):
    personal = create(api, "personal")
    source, sv = resource(api, personal)
    frozen_fixture(api, source, sv)
    library = team(api)
    r, v = resource(api, library)
    frozen_fixture(api, r, v)
    with api.app.state.session_factory.begin() as db:
        from_block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == v.json()["id"]))
        to_block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == sv.json()["id"]))
        db.add(m.EvidenceLink(id=svc.uid(), from_version_id=v.json()["id"], from_block_id=from_block.block_id,
            to_version_id=sv.json()["id"], to_block_id=to_block.block_id, purpose="FACT"))
        db.flush()
        stored = db.get(m.ResourceVersion, v.json()["id"])
        stored.content_sha256 = svc.check_frozen_hash(db, stored)
    api.login(OUTSIDER)
    assert api.call("GET", f"/versions/{v.json()['id']}").status_code == 404
    assert api.call("GET", f"/resources/{r['id']}").status_code == 404
    assert api.call("POST", "/search", {"space_id": library["id"], "query": "合成"}).json()["items"] == []
    assert api.call("GET", f"/wiki/graph?space_id={library['id']}").json()["nodes"] == []


def test_personal_owner_roles_survive_stale_missing_members_but_inactive_owner_is_denied(api):
    library = create(api, "personal", OUTSIDER)
    with api.app.state.session_factory.begin() as db:
        db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == library["id"]))
    assert set(api.call("GET", f"/libraries/{library['id']}").json()["roles"]) == libraries.ALL_ROLES
    with api.app.state.session_factory.begin() as db:
        db.get(m.User, OUTSIDER).active = False
    assert api.call("GET", "/libraries").status_code == 401
    assert api.call("POST", "/libraries", {"name": "失效会话", "kind": "team"}).status_code == 401


def test_old_permission_replay_cannot_use_team_implicit_reader_after_admin_revoked(api):
    library = team(api)
    r, v = resource(api, library)
    frozen_fixture(api, r, v)
    current = api.call("GET", f"/resources/{r['id']}/permissions")
    key = svc.uid()
    body = {"restricted": False, "classification": "INTERNAL", "grants": []}
    first = api.call("PUT", f"/resources/{r['id']}/permissions", body, current.headers["etag"], key=key)
    assert first.status_code == 200
    with api.app.state.session_factory.begin() as db:
        db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == library["id"], m.SpaceMember.user_id == EDITOR))
    denied = api.call("PUT", f"/resources/{r['id']}/permissions", body, current.headers["etag"], key=key)
    assert denied.status_code == 403, "Main thread must invoke api_catalog.replay_authority: " + denied.text


def test_thread_collection_replay_rechecks_revoked_legacy_space(api):
    api.login(READER)
    key = svc.uid()
    body = {"space_id": SPACE, "title": "历史私有咨询标题"}
    first = api.call("POST", "/threads", body, key=key)
    assert first.status_code == 201
    with api.app.state.session_factory.begin() as db:
        db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == SPACE, m.SpaceMember.user_id == READER))
    denied = api.call("POST", "/threads", body, key=key)
    assert denied.status_code == 404, "Main thread must invoke api_catalog.replay_authority: " + denied.text


def test_team_collaborative_upload_keeps_session_private_and_original_immutable(api, jobs):
    library = team(api)
    _r, v = resource(api, library, "document")
    api.login(REVIEWER)
    raw = "合成团队共同上传的来源原件".encode()
    created = api.call("POST", "/uploads", {"version_id": v.json()["id"],
        "filename": "team-source.txt", "size_bytes": len(raw)})
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    api.login()
    assert api.call("GET", f"/uploads/{upload_id}").status_code == 404
    assert api.call("PUT", f"/uploads/{upload_id}/parts/1", raw=raw,
        headers={"Content-Type": "application/octet-stream"}).status_code == 404
    api.login(REVIEWER)
    part = api.call("PUT", f"/uploads/{upload_id}/parts/1", raw=raw,
        headers={"Content-Type": "application/octet-stream"})
    assert part.status_code == 200, part.text
    complete = api.call("POST", f"/uploads/{upload_id}/complete", {"parts": [part.json()]})
    assert complete.status_code == 202, complete.text
    done = api.call("GET", f"/jobs/{complete.json()['id']}")
    assert done.status_code == 200 and done.json()["state"] == "SUCCEEDED", done.text
    assert api.call("GET", f"/versions/{v.json()['id']}/content?representation=source").content == raw
    denied = api.call("POST", "/uploads", {"version_id": v.json()["id"],
        "filename": "cannot-replace.txt", "size_bytes": len(raw)})
    assert denied.status_code == 409 and denied.json()["code"] == "SOURCE_IMMUTABLE"
    api.login()
    assert api.call("GET", f"/versions/{v.json()['id']}/content?representation=source").content == raw
    with api.app.state.session_factory() as db:
        version = db.get(m.ResourceVersion, v.json()["id"])
        assert version.author_id == EDITOR
        assert svc.version_contributor(db, db.get(m.User, REVIEWER), version)
