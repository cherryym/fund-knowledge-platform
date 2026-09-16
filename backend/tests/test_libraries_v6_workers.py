"""Actual export-worker final ACL rechecks with synthetic temporary storage only."""
from sqlalchemy import delete
from test_jobs import env as _worker_fixture
from test_jobs import get_job, job, resource

from fund_kb import ingestion, libraries
from fund_kb import models as m
from fund_kb.services import uid

env = _worker_fixture


def register_team(env):
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=uid(), name=libraries.POLICY_PREFIX + env.space, updated_by=env.user,
            config={"schema_version": 1, "space_id": env.space, "kind": "team", "owner_id": env.user}))


def test_export_worker_rechecks_team_editor_revocation_before_delivering_draft(env, monkeypatch):
    register_team(env)
    _rid, vid, _ = resource(env)
    original = ingestion.render_blocks
    calls = []

    def revoke_after_render(*args, **kwargs):
        result = original(*args, **kwargs)
        with env.db.begin() as db:
            db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == env.space,
                m.SpaceMember.user_id == env.user))
        calls.append("membership revoked in a separate committed transaction")
        return result

    monkeypatch.setattr(ingestion, "render_blocks", revoke_after_render)
    identity = job(env, "EXPORT", {"version_ids": [vid], "format": "markdown"})
    assert not env.d.run(identity)
    assert calls
    result = get_job(env, identity)
    assert result.state == "FAILED" and not result.result
    assert result.error_code == "NOT_FOUND"


def test_export_worker_rechecks_invalid_governance_before_delivering(env, monkeypatch):
    register_team(env)
    _rid, vid, _ = resource(env)
    original = ingestion.render_blocks

    def invalidate_after_render(*args, **kwargs):
        result = original(*args, **kwargs)
        from sqlalchemy import select
        with env.db.begin() as db:
            policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == libraries.POLICY_PREFIX + env.space))
            policy.config = {"schema_version": 1, "space_id": env.space, "kind": "UNKNOWN"}
        return result

    monkeypatch.setattr(ingestion, "render_blocks", invalidate_after_render)
    identity = job(env, "EXPORT", {"version_ids": [vid], "format": "markdown"})
    assert not env.d.run(identity)
    result = get_job(env, identity)
    assert result.state == "FAILED" and not result.result
    assert result.error_code == "NOT_FOUND"


def test_publish_worker_rejects_historical_coauthor_approval(env):
    register_team(env)
    rid, vid, _ = resource(env, approved=True)
    with env.db.begin() as db:
        db.add(m.AuditEvent(id=uid(), actor_id=env.reviewer, action="version.edited",
            object_type="ResourceVersion", object_id=vid, outcome="SUCCESS", trace_id=uid(), details={}))
    identity = job(env, "PUBLISH", {"version_id": vid}, resource_id=rid, version_id=vid)
    assert not env.d.run(identity)
    result = get_job(env, identity)
    assert result.state == "FAILED" and not result.result
    assert result.error_code == "INDEPENDENT_REVIEW_REQUIRED"
    with env.db() as db:
        assert db.get(m.Resource, rid).active_release_id is None
