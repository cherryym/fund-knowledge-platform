"""Synthetic SQLite/HTTP only. No provider, real credential or production writes."""
import copy
import pytest
from sqlalchemy import func, select

from test_wiki import env as env  # noqa: PLC0414
from test_wiki import page

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.answer_diagnostics import formal_eligibility_summary
from fund_kb.jobs import JobDispatcher


def unknown_source(env, name="合成会计手册", restricted=False):
    source = page(env, name, "合成股票计量资料", kind="document", restricted=restricted)
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, source[1])
        version.legal_status = "UNKNOWN"
        version.content_sha256 = svc.check_frozen_hash(db, version)
    return source


def empty_run(env, scope="formal", actor=None):
    env.login(actor or env.owner)
    thread = env.call("POST", "/threads", {"space_id": env.space, "title": "合成零证据诊断"}).json()
    payload = {"question": "合成股票买入如何估值", "mode": "answer", "context": {}}
    if scope is not None:
        payload["answer_scope"] = scope
    result = env.call("POST", f"/threads/{thread['id']}/runs", payload)
    assert result.status_code == 202, result.text
    run = result.json()
    dispatcher = JobDispatcher(env.settings, env.db, None)
    try:
        attempt = dispatcher._claim(run["job_id"])
        dispatcher._answer(run["job_id"], attempt)
    finally:
        dispatcher.close()
    response = env.call("GET", "/runs/" + run["id"])
    assert response.status_code == 200, response.text
    return response.json()


def test_published_unknown_is_not_missing_content_and_get_does_not_mutate_snapshots(env):
    source = unknown_source(env)
    run = empty_run(env)
    assert run["answer_scope"] == "formal" and run["answer_scope_origin"] == "explicit"
    assert run["answer"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert run["model_snapshot"]["model_invoked"] is False
    diagnostic = run["evidence_diagnostic"]
    assert diagnostic["basis"] == "current_access"
    assert diagnostic["visible_resource_count"] == 1
    assert diagnostic["eligible_resource_count"] == 0
    assert diagnostic["excluded_resource_count"] == 1
    assert diagnostic["reasons"][0]["code"] == "LEGAL_STATUS_UNDETERMINED"
    assert "资料存在" in diagnostic["message"]
    assert "不是历史" in diagnostic["note"]
    with env.db() as db:
        obj = db.get(m.ConsultationRun, run["id"])
        snapshot = copy.deepcopy((obj.request, obj.response, obj.model_snapshot, obj.evidence_snapshot))
        before_audit = db.scalar(select(func.count()).select_from(m.AuditEvent))
        version = db.get(m.ResourceVersion, source[1])
        before_source = (version.content_sha256, version.legal_status, version.state)
    again = env.call("GET", "/runs/" + run["id"])
    assert again.status_code == 200
    with env.db() as db:
        obj = db.get(m.ConsultationRun, run["id"])
        assert (obj.request, obj.response, obj.model_snapshot, obj.evidence_snapshot) == snapshot
        assert "evidence_diagnostic" not in obj.request and "evidence_diagnostic" not in obj.model_snapshot
        version = db.get(m.ResourceVersion, source[1])
        assert (version.content_sha256, version.legal_status, version.state) == before_source
        assert db.scalar(select(func.count()).select_from(m.AuditEvent)) == before_audit


def test_current_diagnostic_counts_do_not_reveal_hidden_sources_and_drop_after_revocation(env):
    source = unknown_source(env, "可读来源")
    secret = unknown_source(env, "不可泄漏来源名称", restricted=True)
    run = empty_run(env, actor=env.reader)
    diagnostic = run["evidence_diagnostic"]
    assert diagnostic["visible_resource_count"] == 1
    assert str(secret[0]) not in str(diagnostic) and "不可泄漏" not in str(diagnostic)
    with env.db.begin() as db:
        db.get(m.Resource, source[0]).restricted = True
    response = env.call("GET", "/runs/" + run["id"])
    assert response.status_code == 200
    fresh = response.json()["evidence_diagnostic"]
    assert fresh["visible_resource_count"] == 0 and fresh["excluded_resource_count"] == 0
    assert fresh["reasons"][0]["code"] == "NO_READABLE_SOURCES"
    assert "LEGAL_STATUS_UNDETERMINED" not in str(fresh)


def test_unpublished_private_versions_are_not_counted_for_reader(env):
    page(env, "私有草稿元数据", kind="document", state="DRAFT")
    run = empty_run(env, actor=env.reader)
    assert run["evidence_diagnostic"]["visible_resource_count"] == 0
    assert "私有草稿" not in str(run["evidence_diagnostic"])


def test_readable_published_version_is_not_shadowed_by_a_new_private_draft(env):
    source = page(env, "合成正常来源", kind="document")
    page(env, "合成正常来源修订", kind="document", resource_id=source[0], state="DRAFT")
    with env.db() as db:
        summary = formal_eligibility_summary(db, db.get(m.User, env.reader), env.space)
    assert summary == {"visible_resource_count": 1, "eligible_resource_count": 1,
        "excluded_resource_count": 0, "reasons": []}


@pytest.mark.parametrize("change,code", [
    ("partial", "LEGAL_SCOPE_PARTIAL"), ("unverified", "UNVERIFIED_SOURCE"),
    ("scan", "SOURCE_NOT_CLEAN"), ("hash", "SNAPSHOT_INVALID"),
    ("context", "CONTEXT_REQUIRED"), ("expired", "OUTSIDE_VALIDITY"),
])
def test_specific_fixed_reasons_without_source_text(env, change, code):
    source = page(env, "合成来源", "不得回显这一段正文", kind="document")
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, source[1])
        if change == "partial":
            version.legal_status = "PARTIAL"
        elif change == "unverified":
            version.source_verified = False
        elif change == "scan":
            db.get(m.Blob, version.source_blob_id).scan_state = "QUARANTINED"
        elif change == "hash":
            version.content_sha256 = "a" * 64
        elif change == "context":
            version.applicability = {"all": [{"field": "market", "values": ["SSE"]}]}
        else:
            from datetime import date
            version.valid_to = date(2021, 1, 1)
        if change != "hash":
            version.content_sha256 = svc.check_frozen_hash(db, version)
    with env.db() as db:
        summary = formal_eligibility_summary(db, db.get(m.User, env.owner), env.space)
    assert summary["excluded_resource_count"] == 1
    assert summary["reasons"][0]["code"] == code
    assert "不得回显" not in str(summary) and source[0] not in str(summary)


def test_historical_origin_remains_unknown_not_reconstructed_from_current_picker(env):
    run = empty_run(env)
    with env.db.begin() as db:
        obj = db.get(m.ConsultationRun, run["id"])
        request = dict(obj.request)
        del request["answer_scope_origin"]
        obj.request = request
    view = env.call("GET", "/runs/" + run["id"]).json()
    assert view["answer_scope"] == "formal"
    assert view["answer_scope_origin"] == "historical_unknown"


def test_scope_origin_is_server_derived_and_parent_scope_cannot_be_changed(env):
    thread = env.call("POST", "/threads", {"space_id": env.space, "title": "合成范围来源"}).json()
    path = f"/threads/{thread['id']}/runs"
    payload = {"question": "合成问题", "mode": "answer", "context": {}}
    legacy = env.call("POST", path, payload)
    assert legacy.status_code == 202 and legacy.json()["answer_scope_origin"] == "legacy_default"
    parent = env.call("POST", path, {**payload, "answer_scope": "reference"}).json()
    child = env.call("POST", path, {**payload, "parent_run_id": parent["id"]})
    assert child.status_code == 202
    assert child.json()["answer_scope"] == "reference" and child.json()["answer_scope_origin"] == "inherited"
    bad = env.call("POST", path, {**payload, "parent_run_id": parent["id"], "answer_scope": "formal"})
    assert bad.status_code == 409 and bad.json()["code"] == "ANSWER_SCOPE_MISMATCH"
    forged = env.call("POST", path, {**payload, "answer_scope_origin": "explicit"})
    assert forged.status_code == 422


def test_reference_empty_diagnostic_does_not_apply_formal_legal_gate(env):
    run = empty_run(env, "reference")
    diagnostic = run["evidence_diagnostic"]
    assert diagnostic["scope"] == "reference"
    assert diagnostic["reasons"][0]["code"] == "NO_MATCHING_REFERENCE_EVIDENCE"
    assert "visible_resource_count" not in diagnostic


@pytest.mark.parametrize("change", ["invoked", "unknown_invocation", "invalidated"])
def test_no_not_invoked_diagnostic_when_history_cannot_support_it(env, change):
    run = empty_run(env)
    with env.db.begin() as db:
        obj = db.get(m.ConsultationRun, run["id"])
        if change == "invalidated":
            obj.invalidated_at = svc.now()
        else:
            snapshot = dict(obj.model_snapshot)
            if change == "invoked":
                snapshot["model_invoked"] = True
            else:
                snapshot.pop("model_invoked", None)
            obj.model_snapshot = snapshot
    response = env.call("GET", "/runs/" + run["id"])
    assert response.status_code == 200 and "evidence_diagnostic" not in response.json()


def test_other_users_cannot_request_diagnostics_for_a_private_run(env):
    run = empty_run(env)
    env.login(env.reader)
    assert env.call("GET", "/runs/" + run["id"]).status_code == 404
