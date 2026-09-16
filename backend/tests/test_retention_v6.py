"""Synthetic SQLite/HTTP checks only. Never opens a business DB or runs a purge worker."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from fund_kb import api_retention, models as m, retention as rt, services as svc
from fund_kb.api import create_app
from fund_kb.settings import Settings
from test_api import API, EDITOR as ADMIN, OUTSIDER, READER, REVIEWER, SPACE

PERSONAL = "00000000-0000-4000-8000-000000000111"
TEAM = "00000000-0000-4000-8000-000000000112"
LEGACY_OTHER = "00000000-0000-4000-8000-000000000113"
EDITOR = "00000000-0000-4000-8000-000000000005"


@pytest.fixture
def api(tmp_path):
    app = create_app(Settings(_env_file=None, app_env="development", auth_mode="demo",
        database_url=f"sqlite:///{tmp_path / 'retention-only.sqlite3'}", storage_dir=tmp_path / "objects",
        qdrant_path=tmp_path / "vectors", allowed_origins=["http://testserver"], llm_provider="evidence"))
    app.state.raise_test_errors = True
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            for who in (ADMIN, OUTSIDER, READER, REVIEWER, EDITOR):
                db.add(m.User(id=who, external_subject="demo:admin" if who == ADMIN else f"demo:{who}",
                              display_name="合成身份", active=True))
            for sid in (SPACE, PERSONAL, TEAM, LEGACY_OTHER):
                db.add(m.Space(id=sid, name="合成保留测试库"))
            db.flush()
            for sid in (SPACE, TEAM, LEGACY_OTHER):
                db.add(m.SpaceMember(space_id=sid, user_id=ADMIN, role="admin"))
                db.add(m.SpaceMember(space_id=sid, user_id=ADMIN, role="editor"))
            for who, role in ((READER, "reader"), (EDITOR, "editor"), (REVIEWER, "reviewer")):
                for sid in (SPACE, TEAM):
                    db.add(m.SpaceMember(space_id=sid, user_id=who, role=role))
            for sid, kind, owner in ((PERSONAL, "personal", EDITOR), (TEAM, "team", ADMIN)):
                db.add(m.RuntimePolicy(name=f"space-governance:{sid}", updated_by=owner,
                    config={"schema_version": 1, "space_id": sid, "kind": kind, "owner_id": owner}))
            db.commit()
        harness = API(app, client)
        harness.login(ADMIN)
        yield harness


def body(**kw):
    return {"retention_days": 2, "approved": True, "reason": "合成测试：用户明确确认保留2天", **kw}


def approve(api, sid=SPACE, **kw):
    path = f"/spaces/{sid}/retention-policy"
    get = api.call("GET", path)
    assert get.status_code == 200, get.text
    response = api.call("PUT", path, body(**kw), get.headers["etag"])
    assert response.status_code == 200, response.text
    return response


def resource(api, *, sid=SPACE, owner=ADMIN, deleted=True, until=None, hold=False, restricted=False):
    with api.app.state.session_factory() as db:
        r = m.Resource(id=svc.uid(), space_id=sid, kind="document", name="合成测试原件",
            owner_id=owner, deleted_at=svc.now() - timedelta(days=3) if deleted else None,
            retain_until=until, legal_hold=hold, restricted=restricted)
        db.add(r)
        db.commit()
        return r


def context(api, db, user=ADMIN, rid=SPACE, data=None, revision=0, operation="setRetentionPolicy"):
    return svc.Context(SimpleNamespace(path_params={"id": rid}, headers={"if-match": f'"{revision}"'},
        state=SimpleNamespace(trace_id="synthetic-retention"), app=api.app), db, db.get(m.User, user),
        data or {}, {}, operation)


def eligibility(api, r):
    response = api.call("GET", f"/resources/{r.id}/purge-eligibility")
    assert response.status_code == 200, response.text
    return response.json()


def codes(value):
    return {r["code"] for r in value["reasons"]}


def test_default_get_is_read_only_and_legacy_expired_timestamp_never_authorizes(api):
    r = resource(api, until=svc.now() - timedelta(days=1))
    response = api.call("GET", f"/spaces/{SPACE}/retention-policy")
    assert response.json()["status"] == "UNCONFIGURED"
    assert response.json()["retention_days"] == 2
    assert response.headers["etag"] == '"0"'
    assert codes(eligibility(api, r)) == {"RETENTION_UNDEFINED"}
    with api.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(m.RuntimePolicy).where(m.RuntimePolicy.name.like("retention-policy:%"))) == 0
        assert db.scalar(select(func.count()).select_from(m.Job)) == 0


def test_approve_only_target_trash_extend_audit_and_preserve_files(api, tmp_path):
    sentinel = tmp_path / "objects" / "retention-sentinel.bin"
    sentinel.parent.mkdir(exist_ok=True)
    sentinel.write_bytes(b"synthetic-original-do-not-delete")
    missing = resource(api)
    short = resource(api, until=svc.now() - timedelta(days=20))
    future = resource(api, until=svc.now() + timedelta(days=100), hold=True)
    live = resource(api, deleted=False)
    other = resource(api, sid=LEGACY_OTHER)
    result = approve(api)
    assert result.json()["backfilled_count"] == 2
    assert result.json()["approved"] is True
    with api.app.state.session_factory() as db:
        for r in (missing, short):
            stored = db.get(m.Resource, r.id)
            assert stored.retain_until == r.deleted_at + timedelta(days=2)
            assert stored.revision == 2
        assert db.get(m.Resource, future.id).retain_until == future.retain_until
        assert db.get(m.Resource, future.id).legal_hold is True
        assert db.get(m.Resource, live.id).retain_until is None
        assert db.get(m.Resource, other.id).retain_until is None
        actions = list(db.scalars(select(m.AuditEvent.action)))
        assert actions.count("retention.trash_extended") == 2
        assert actions.count("retention.policy_approved") == 1
        assert db.scalar(select(func.count()).select_from(m.Job)) == 0
        assert len(list(db.scalars(select(m.Resource)))) == 5
    assert sentinel.read_bytes() == b"synthetic-original-do-not-delete"


@pytest.mark.parametrize("days", [0, 1, -1, 2.5, True, "2", 36501])
def test_invalid_retention_days_rejected(api, days):
    response = api.call("PUT", f"/spaces/{SPACE}/retention-policy", body(retention_days=days), '"0"')
    assert response.status_code == 422, response.text


def test_http_etag_csrf_schema_idempotency(api):
    path = f"/spaces/{SPACE}/retention-policy"
    assert api.call("PUT", path, body()).status_code == 428
    assert api.call("PUT", path, body(), '"0"', headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert api.call("PUT", path, body(), '"0"', headers={"Idempotency-Key": ""}).status_code == 422
    assert api.call("PUT", path, body(approved_by=ADMIN), '"0"').status_code == 422
    assert api.call("PUT", path, body(reason="   "), '"0"').status_code == 422
    first = api.call("PUT", path, body(), '"0"', key="retention-replay-001")
    assert first.status_code == 200, first.text
    repeated = api.call("PUT", path, body(), '"0"', key="retention-replay-001")
    assert repeated.json() == first.json()
    assert api.call("PUT", path, body(), '"0"').status_code == 412
    assert api.call("PUT", path, body(approved=False), '"1"', key="retention-replay-001").status_code == 409
    with api.app.state.session_factory() as db:
        db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == SPACE, m.SpaceMember.user_id == ADMIN, m.SpaceMember.role == "admin"))
        db.commit()
    assert api.call("PUT", path, body(), '"0"', key="retention-replay-001").status_code == 403


@pytest.mark.parametrize("who", [EDITOR, READER, REVIEWER])
def test_non_managers_cannot_self_grant_or_approve(api, who):
    api.login(who)
    get = api.call("GET", f"/spaces/{SPACE}/retention-policy")
    assert get.status_code == 200
    assert get.json()["permissions"]["can_configure"] is False
    response = api.call("PUT", f"/spaces/{SPACE}/retention-policy", body(purge_allowed_roles=["editor"]), '"0"')
    assert response.status_code == 403


def test_personal_owner_only_even_with_admin_membership_and_team_reader_fallback(api):
    private = resource(api, sid=PERSONAL, owner=EDITOR)
    with api.app.state.session_factory() as db:
        db.add(m.SpaceMember(space_id=PERSONAL, user_id=ADMIN, role="admin"))
        db.commit()
    for who in (ADMIN, OUTSIDER, READER):
        api.login(who)
        assert api.call("GET", f"/spaces/{PERSONAL}/retention-policy").status_code == 404
        assert api.call("PUT", f"/spaces/{PERSONAL}/retention-policy", body(), '"0"').status_code == 404
        assert api.call("GET", f"/resources/{private.id}/purge-eligibility").status_code == 404
    api.login(EDITOR)
    assert approve(api, PERSONAL).json()["permissions"]["can_configure"]
    assert eligibility(api, private)["eligible"]
    api.login(OUTSIDER)
    assert api.call("GET", f"/spaces/{SPACE}/retention-policy").status_code == 404
    assert api.call("GET", f"/spaces/{TEAM}/retention-policy").json()["permissions"]["can_configure"] is False
    assert api.call("PUT", f"/spaces/{TEAM}/retention-policy", body(), '"0"').status_code == 403


@pytest.mark.parametrize("sid,who", [(PERSONAL, EDITOR), (TEAM, ADMIN)])
def test_new_library_default_hook_is_approved_audited_and_does_not_overwrite(api, sid, who):
    with api.app.state.session_factory() as db:
        ctx = context(api, db, who)
        value = rt.create_default_policy(ctx, db.get(m.Space, sid))
        assert value["retention_days"] == 2 and value["approved"]
        assert rt.create_default_policy(ctx, db.get(m.Space, sid))["revision"] == value["revision"]
        db.commit()
    with api.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(m.AuditEvent.action == "retention.policy_approved")) == 1
        assert not db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == rt.POLICY_PREFIX + LEGACY_OTHER))
        with pytest.raises(svc.APIError, match="旧空间"):
            rt.create_default_policy(context(api, db), db.get(m.Space, LEGACY_OTHER))


def test_48_hours_boundary_and_longer_retainuntil(api):
    r = resource(api)
    approve(api)
    expiry = r.deleted_at + timedelta(days=2)
    with api.app.state.session_factory() as db:
        policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == rt.POLICY_PREFIX + SPACE))
        policy.config = {**policy.config, "approved_at": svc.primitive(r.deleted_at - timedelta(days=1))}
        db.commit()
    with api.app.state.session_factory() as db:
        user = db.get(m.User, ADMIN)
        assert "RETENTION_NOT_EXPIRED" in codes(rt.purge_eligibility(db, user, r.id, at=expiry - timedelta(microseconds=1)))
        assert rt.assert_purge_allowed(db, user, r.id, at=expiry)["eligible"]
        db.get(m.Resource, r.id).retain_until = svc.now() + timedelta(days=60)
        db.commit()
    assert "RETENTION_NOT_EXPIRED" in codes(eligibility(api, r))


def test_missing_retainuntil_still_uses_deleted_at_floor_and_restoration_blocks(api):
    approve(api, backfill_trash=False)
    r = resource(api)
    with api.app.state.session_factory() as db:
        stored = db.get(m.Resource, r.id)
        stored.deleted_at = svc.now()
        db.commit()
    value = eligibility(api, r)
    assert value["retain_until"] is None and value["expiry"]
    assert "RETENTION_NOT_EXPIRED" in codes(value)
    with api.app.state.session_factory() as db:
        stored = db.get(m.Resource, r.id)
        stored.deleted_at = None
        db.commit()
    assert "TRASH_REQUIRED" in codes(eligibility(api, r))


@pytest.mark.parametrize("change,expected", [({"approved": False}, "RETENTION_UNAPPROVED"),
    ({"retention_days": 0}, "RETENTION_POLICY_INVALID"), ({"approved_by": None}, "RETENTION_POLICY_INVALID"),
    ({"approval_expires_at": "2000-01-01T00:00:00Z"}, "RETENTION_POLICY_EXPIRED")])
def test_invalid_unapproved_and_expired_policy_fail_closed(api, change, expected):
    r = resource(api)
    approve(api)
    with api.app.state.session_factory() as db:
        p = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == rt.POLICY_PREFIX + SPACE))
        p.config = {**p.config, **change}
        db.commit()
    assert expected in codes(eligibility(api, r))


def test_delegation_requires_explicit_role_and_resource_manage_grant(api):
    r = resource(api)
    approve(api, purge_allowed_roles=["editor"])
    api.login(EDITOR)
    assert "PURGE_PERMISSION_DENIED" in codes(eligibility(api, r))
    with api.app.state.session_factory() as db:
        db.add(m.ResourceGrant(resource_id=r.id, user_id=EDITOR, permission="manage"))
        db.commit()
    assert eligibility(api, r)["eligible"]
    assert api.call("PUT", f"/resources/{r.id}/preservation", {"legal_hold": True, "reason": "编辑者不可配置"}, '"2"').status_code == 403


def test_restricted_resource_needs_explicit_grants(api):
    r = resource(api, restricted=True)
    approve(api)
    assert api.call("GET", f"/resources/{r.id}/purge-eligibility").status_code == 404
    with api.app.state.session_factory() as db:
        db.add(m.ResourceGrant(resource_id=r.id, user_id=ADMIN, permission="read"))
        db.commit()
    assert "PURGE_PERMISSION_DENIED" in codes(eligibility(api, r))


def test_preservation_etag_release_confirmation_and_no_shortening(api):
    r = resource(api, until=svc.now() + timedelta(days=30))
    approve(api)
    path = f"/resources/{r.id}/preservation"
    get = api.call("GET", path)
    hold = api.call("PUT", path, {"legal_hold": True, "reason": "合成保全审批"}, get.headers["etag"])
    assert hold.status_code == 200, hold.text
    assert "LEGAL_HOLD" in codes(eligibility(api, r))
    assert api.call("PUT", path, {"legal_hold": False, "reason": "解除"}, hold.headers["etag"]).status_code == 409
    assert api.call("PUT", path, {"retain_until": svc.primitive(svc.now()), "reason": "非法缩短"}, hold.headers["etag"]).status_code == 409
    assert api.call("PUT", path, {"retain_until": None, "reason": "移除期限"}, hold.headers["etag"]).status_code == 422
    released = api.call("PUT", path, {"legal_hold": False, "confirm_release": True,
        "reason": "合成解除保全", "release_reason": "已人工核验解除保全的依据并批准解除"}, hold.headers["etag"])
    assert released.status_code == 200, released.text
    assert released.json()["retain_until"] == svc.primitive(r.retain_until)
    assert api.call("PUT", path, {"legal_hold": True, "reason": "旧版本"}, hold.headers["etag"]).status_code == 412
    with api.app.state.session_factory() as db:
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == "preservation.hold_released"))


def test_execution_rechecks_revocation_hold_policy_and_dependencies(api):
    r = resource(api)
    approve(api)
    with api.app.state.session_factory() as db:
        user = db.get(m.User, ADMIN)
        assert rt.assert_purge_allowed(db, user, r.id)["eligible"]
        job = svc.create_job(context(api, db), "PURGE", {"resource_id": r.id, "reason": "只测检查不执行"}, resource_id=r.id)
        db.commit()
        assert rt.assert_purge_allowed(db, user, r.id, phase="execution", job_id=job.id)["eligible"]
        db.get(m.Resource, r.id).legal_hold = True
        db.commit()
        with pytest.raises(svc.APIError) as blocked:
            rt.assert_purge_allowed(db, user, r.id, phase="execution", job_id=job.id)
        assert blocked.value.code == "LEGAL_HOLD"
        db.get(m.Resource, r.id).legal_hold = False
        db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == SPACE, m.SpaceMember.user_id == ADMIN, m.SpaceMember.role == "admin"))
        db.commit()
        with pytest.raises(svc.APIError) as revoked:
            rt.assert_purge_allowed(db, user, r.id, phase="execution", job_id=job.id)
        assert revoked.value.status == 403


def test_pending_jobs_and_frozen_wiki_provenance_block(api):
    r = resource(api)
    approve(api)
    with api.app.state.session_factory() as db:
        job = svc.create_job(context(api, db), "COMPILE", {"resource_id": r.id}, resource_id=r.id)
        db.commit()
    assert "DEPENDENT_JOB_RUNNING" in codes(eligibility(api, r))
    with api.app.state.session_factory() as db:
        db.get(m.Job, job.id).state = "SUCCEEDED"
        version = m.ResourceVersion(id=svc.uid(), resource_id=r.id, version_no=1, title="合成版本", author_id=ADMIN, origin="HUMAN")
        db.add(version)
        db.flush()
        target = svc.uid()
        db.add(m.RuntimePolicy(name="wiki-provenance:" + target, updated_by=ADMIN,
            config={"resource_id": target, "source_version_ids": [version.id]}))
        db.commit()
    assert "INBOUND_DEPENDENCIES" in codes(eligibility(api, r))


def test_trash_hook_sets_floor_and_keeps_longer_hold(api):
    approve(api)
    r = resource(api, hold=True)
    with api.app.state.session_factory() as db:
        stored = db.get(m.Resource, r.id)
        ctx = context(api, db)
        assert rt.apply_trash_retention(ctx, stored)
        assert stored.retain_until == stored.deleted_at + timedelta(days=2)
        assert stored.legal_hold
        assert not rt.apply_trash_retention(ctx, stored)
        db.commit()


def test_extension_replay_guard_rechecks_preservation_permission(api):
    r = resource(api)
    approve(api)
    with api.app.state.session_factory() as db:
        ctx = context(api, db, rid=r.id, operation="setResourcePreservation")
        api_retention.replay_authority(ctx, {})
        db.get(m.User, ADMIN).active = False
        db.commit()
        with pytest.raises(svc.APIError):
            api_retention.replay_authority(ctx, {})


def test_guard_prioritizes_legal_hold_over_missing_policy(api):
    r = resource(api, hold=True)
    value = eligibility(api, r)
    assert codes(value) == {"RETENTION_UNDEFINED", "LEGAL_HOLD"}
    with api.app.state.session_factory() as db:
        with pytest.raises(svc.APIError) as denied:
            rt.assert_purge_allowed(db, db.get(m.User, ADMIN), r.id)
        assert (denied.value.status, denied.value.code) == (423, "LEGAL_HOLD")
        assert codes(denied.value.details) == codes(value)


@pytest.mark.parametrize("prefix", ["wiki-provenance:", "document-guidance-provenance:", "wiki-build-receipt:"])
def test_frozen_lineage_without_editable_citations_uses_wiki_helper(api, prefix, monkeypatch):
    from fund_kb import wiki
    source = resource(api)
    derived = resource(api, deleted=False)
    approve(api)
    with api.app.state.session_factory() as db:
        db.get(m.Resource, derived.id).kind = "knowledge"
        version = m.ResourceVersion(id=svc.uid(), resource_id=source.id, version_no=1,
            title="冻结来源", author_id=ADMIN, origin="HUMAN")
        db.add(version)
        db.flush()
        config = {"resource_id": derived.id, "source_version_ids": [version.id]}
        if prefix == "wiki-build-receipt:":
            config = {"result": {"created_resource_ids": [derived.id], "source_version_ids": [version.id]}}
        db.add(m.RuntimePolicy(name=prefix + derived.id, updated_by=ADMIN, config=config))
        db.commit()
        assert db.scalar(select(func.count()).select_from(m.EvidenceLink)) == 0
    original = wiki._provenance_sources
    visited = []
    def spy(db, r):
        visited.append(r.id)
        return original(db, r)
    monkeypatch.setattr(wiki, "_provenance_sources", spy)
    assert "INBOUND_DEPENDENCIES" in codes(eligibility(api, source))
    assert derived.id in visited
    with api.app.state.session_factory() as db:
        with pytest.raises(svc.APIError) as denied:
            rt.assert_purge_allowed(db, db.get(m.User, ADMIN), source.id)
        assert denied.value.code == "INBOUND_DEPENDENCIES"


def test_execution_refreshes_policy_and_receipt_cache(api):
    source = resource(api)
    derived = resource(api, deleted=False)
    approve(api)
    with api.app.state.session_factory() as db:
        v = m.ResourceVersion(id=svc.uid(), resource_id=source.id, version_no=1,
            title="来源", author_id=ADMIN, origin="HUMAN")
        db.add(v)
        db.flush()
        job = svc.create_job(context(api, db), "PURGE", {"resource_id": source.id, "reason": "测试复核"}, resource_id=source.id)
        db.commit()
        user = db.get(m.User, ADMIN)
        assert rt.assert_purge_allowed(db, user, source.id, phase="execution", job_id=job.id)["eligible"]
        db.info["wiki_receipt_provenance_ids_v1"] = {}
        db.add(m.RuntimePolicy(name="wiki-build-receipt:" + svc.uid(), updated_by=ADMIN,
            config={"result": {"created_resource_ids": [derived.id], "source_version_ids": [v.id]}}))
        db.commit()
        with pytest.raises(svc.APIError) as denied:
            rt.assert_purge_allowed(db, user, source.id, phase="execution", job_id=job.id)
        assert denied.value.code == "INBOUND_DEPENDENCIES"
        p = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == rt.POLICY_PREFIX + SPACE))
        p.config = {**p.config, "approved": False}
        db.commit()
        with pytest.raises(svc.APIError) as unapproved:
            rt.assert_purge_allowed(db, user, source.id, phase="execution", job_id=job.id)
        assert unapproved.value.code == "RETENTION_UNAPPROVED"


def test_malformed_frozen_sources_fail_closed(api):
    source = resource(api)
    approve(api)
    with api.app.state.session_factory() as db:
        db.add(m.ResourceVersion(resource_id=source.id, version_no=1, title="来源", author_id=ADMIN, origin="HUMAN"))
        target = svc.uid()
        db.add(m.RuntimePolicy(name="document-guidance-provenance:" + target, updated_by=ADMIN,
            config={"resource_id": target, "source_version_ids": ["not-a-uuid"]}))
        db.commit()
    assert "FROZEN_PROVENANCE_INVALID" in codes(eligibility(api, source))


def test_preservation_replay_works_for_manage_without_read(api):
    r = resource(api, restricted=True)
    with api.app.state.session_factory() as db:
        db.add(m.ResourceGrant(resource_id=r.id, user_id=ADMIN, permission="manage"))
        db.commit()
    path = f"/resources/{r.id}/preservation"
    data = {"legal_hold": True, "reason": "合成独立管理授权"}
    first = api.call("PUT", path, data, '"1"', key="preservation-manage-only")
    assert first.status_code == 200, first.text
    repeated = api.call("PUT", path, data, '"1"', key="preservation-manage-only")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == first.json()


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED", "COMPLETED", "RUNNING", "QUEUED"])
@pytest.mark.parametrize("reference", ["version_id", "resource_id", "both"])
def test_snapshot_only_history_blocks_all_run_states_without_index(api, state, reference):
    r = resource(api)
    approve(api)
    with api.app.state.session_factory() as db:
        version = m.ResourceVersion(id=svc.uid(), resource_id=r.id, version_no=1,
            title="合成冻结版本", author_id=ADMIN, origin="HUMAN")
        # A deleted thread in a different space must not hide its historical dependency.
        thread = m.ConsultationThread(id=svc.uid(), owner_id=ADMIN, space_id=LEGACY_OTHER,
            title="合成稀疏恢复历史", deleted_at=svc.now())
        db.add_all([version, thread])
        db.flush()
        snapshot = {"version_id": version.id} if reference == "version_id" else {"resource_id": r.id}
        if reference == "both":
            snapshot["version_id"] = version.id
        db.add(m.ConsultationRun(thread_id=thread.id, state=state, mode="answer", request={},
            response={} if state == "COMPLETED" else None, evidence_snapshot=[snapshot]))
        db.commit()
        assert db.scalar(select(func.count()).select_from(m.RunEvidence)) == 0
    value = eligibility(api, r)
    assert "INBOUND_DEPENDENCIES" in codes(value)
    assert value["eligible"] is False
    request = api.call("POST", f"/resources/{r.id}/purge", {"reason": "验证稀疏历史依赖"}, f'"{value["revision"]}"')
    assert (request.status_code, request.json()["code"]) == (409, "INBOUND_DEPENDENCIES")
    with api.app.state.session_factory() as db:
        assert db.get(m.Resource, r.id)
        assert not db.scalar(select(m.Job.id).where(m.Job.kind == "PURGE"))


@pytest.mark.parametrize("snapshot", [["not-an-object"], [{"version_id": "invalid"}], [{}]])
def test_malformed_consultation_snapshot_is_unknown_not_no_dependency(api, snapshot):
    r = resource(api)
    approve(api)
    with api.app.state.session_factory() as db:
        thread = m.ConsultationThread(id=svc.uid(), owner_id=ADMIN, space_id=SPACE, title="合成损坏历史")
        db.add(thread)
        db.flush()
        db.add(m.ConsultationRun(thread_id=thread.id, state="FAILED", mode="answer", request={}, evidence_snapshot=snapshot))
        db.commit()
    assert "CONSULTATION_SNAPSHOT_INVALID" in codes(eligibility(api, r))
