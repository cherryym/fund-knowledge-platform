"""Reference scope never publishes sources or relaxes the formal-answer gate."""
import copy
import json

import pytest
from sqlalchemy import select
from test_wiki import env as env  # noqa: PLC0414
from test_wiki import provider as provider  # noqa: PLC0414
from test_wiki_unverified import build_draft, draft_source

from fund_kb import models as m
from fund_kb import providers
from fund_kb import services as svc
from fund_kb.jobs import JobDispatcher, JobError
from fund_kb.reference_evidence import rank_reference_evidence, reference_evidence


def records(env, actor=None):
    with env.db() as db:
        return reference_evidence(db, actor or env.owner, env.space)


def run_answer(env, scope="reference", selection=None):
    thread = env.call("POST", "/threads", {"space_id": env.space, "title": "资料辅助验收"}).json()
    response = env.call("POST", f"/threads/{thread['id']}/runs", {"question": "估值时如何核对价格来源和输入参数",
        "mode": "answer", "context": {}, "answer_scope": scope, "reasoning_strategy": "evidence_first",
        **({"model_selection": selection} if selection else {})})
    assert response.status_code == 202, response.text
    run = response.json()
    dispatcher = JobDispatcher(env.settings, env.db, None)
    try:
        attempt = dispatcher._claim(run["job_id"])
        dispatcher._answer(run["job_id"], attempt)
    finally:
        dispatcher.close()
    response = env.call("GET", "/runs/" + run["id"])
    assert response.status_code == 200, response.text
    return response.json()


def test_reference_reads_unverified_sources_and_wiki_without_formal_promotion(env, provider):
    source = draft_source(env)
    build_draft(env, source)
    values = records(env)
    assert {e["kind"] for e in values} == {"document", "knowledge"}
    assert all(e["evidence_scope"] == "reference" and e["reference_signature"] for e in values)
    assert any(e["source_verified"] is False for e in values)
    with env.db() as db:
        assert svc.eligible_evidence(db, env.owner, env.space) == []
        assert db.get(m.ResourceVersion, source[1]).state == "DRAFT"
        assert not list(db.scalars(select(m.Release)))
    assert records(env, env.reader) == []


def test_reference_submit_http_keeps_source_and_wiki_readable_for_reference_only(env, provider):
    source = draft_source(env)
    _, built = build_draft(env, source)
    vids = [source[1], *built["created_version_ids"]]
    for vid in vids:
        before = env.call("GET", "/versions/" + vid)
        submitted = env.call("POST", f"/versions/{vid}/submit", {"review_scope": "reference"}, etag=before.headers["etag"])
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["state"] == "IN_REVIEW"
    values = records(env)
    assert {e["version_id"] for e in values} == set(vids)
    assert all(e["state"] == "IN_REVIEW" for e in values)
    with env.db() as db:
        assert svc.eligible_evidence(db, env.owner, env.space) == []
        assert not list(db.scalars(select(m.Release)))


@pytest.mark.parametrize("change", ["delete", "suspend", "quarantine", "reject", "hash", "restricted"])
def test_unavailable_sources_never_enter_reference_evidence(env, change):
    source = draft_source(env)
    with env.db.begin() as db:
        version = db.get(m.ResourceVersion, source[1]); resource = db.get(m.Resource, source[0])
        if change == "delete": resource.deleted_at = svc.now()
        if change == "suspend": resource.suspended = True
        if change == "quarantine": db.get(m.Blob, version.source_blob_id).scan_state = "QUARANTINED"
        if change == "reject":
            version.content_sha256 = svc.check_frozen_hash(db, version)
            version.state = "REJECTED"
        if change == "hash": version.content_sha256 = "a" * 64
        if change == "restricted": resource.restricted = True
    assert records(env) == []


def test_reference_worker_uses_documents_in_wiki_mode_and_marks_run(env):
    draft_source(env)
    result = run_answer(env)
    assert result["state"] == "COMPLETED" and result["answer_scope"] == "reference"
    assert result["answer"]["citations"] and not result["invalidated"]
    assert result["answer"]["review_status"] == "REQUIRES_EXPERT"
    assert "资料辅助答疑" in " ".join(result["answer"]["limitations"])
    assert result["model_snapshot"]["model_invoked"] is False
    assert result["model_snapshot"]["evidence_count"] > 0
    formal = run_answer(env, "formal")
    assert formal["answer"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert not formal["answer"]["citations"]


def test_reference_run_invalidates_on_body_metadata_or_permissions_change(env):
    source = draft_source(env)
    run = run_answer(env)
    with env.db.begin() as db:
        db.get(m.ResourceVersion, source[1]).revision += 1
    assert env.call("GET", "/runs/" + run["id"]).json()["invalidated"]
    with env.db.begin() as db:
        db.get(m.Resource, source[0]).restricted = True
    hidden = env.call("GET", "/runs/" + run["id"]).json()
    assert hidden["invalidated"] and hidden["answer"] is None


def test_followup_cannot_silently_change_scope(env):
    draft_source(env)
    run = run_answer(env)
    rejected = env.call("POST", f"/threads/{run['thread_id']}/runs", {"question": "补充事实", "mode": "answer",
        "context": {}, "parent_run_id": run["id"], "answer_scope": "formal"})
    assert rejected.status_code == 409 and rejected.json()["code"] == "ANSWER_SCOPE_MISMATCH"
    inherited = env.call("POST", f"/threads/{run['thread_id']}/runs", {"question": "补充事实", "mode": "answer",
        "context": {}, "parent_run_id": run["id"]})
    assert inherited.status_code == 202 and inherited.json()["answer_scope"] == "reference"


def test_selected_oauth_callback_reaches_reference_worker(env, monkeypatch):
    source = draft_source(env)
    selection = {"connection_id": env.connection, "model_id": "synthetic-oauth"}
    calls = []
    def resolved(*args, **kwargs):
        return {"id": env.connection, "model_id": selection["model_id"], "base_url": "",
            "protocol": "codex_app_server", "revision": 1, "owner_user_id": env.owner,
            "brand": "openai", "kind": "subscription", "provider_id": "chatgpt-codex"}
    def complete(connection, messages, **kwargs):
        assert kwargs["timeout"] == env.settings.answer_model_timeout_seconds == 180
        assert env.settings.model_timeout_seconds == 60  # Wiki/default provider budget is independent.
        data = json.loads(messages[1]["content"]); calls.append(data)
        from answer_content_fixture import reply_for
        result = reply_for(data)
        if not result["claims"]:
            citation = result["citations"][0]
            result["claims"] = [{"id": "C1", "text": citation["excerpt"], "evidence_ids": [citation["id"]]}]
        result["limitations"] = []
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}
    monkeypatch.setattr(providers, "resolve_connection", resolved)
    monkeypatch.setattr(providers, "complete", complete)
    run = run_answer(env, selection=selection)
    assert len(calls) == 1 and calls[0]["answer_scope"] == "reference"
    assert run["model_snapshot"]["model_invoked"] is True
    assert run["model_snapshot"]["execution_mode"] == "reference_grounded"
    assert run["answer"]["citations"] and not run["invalidated"]
    assert run["answer"]["review_status"] == "REQUIRES_EXPERT"
    assert "MODEL_NOT_CONFIGURED" not in " ".join(run["answer"]["limitations"])
    with env.db() as db:
        assert db.get(m.ResourceVersion, source[1]).source_verified is False


def test_mutation_during_reference_generation_blocks_delivery(env, monkeypatch):
    source = draft_source(env)
    def complete(*args, **kwargs):
        with env.db.begin() as db: db.get(m.Resource, source[0]).suspended = True
        return {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
    monkeypatch.setattr(providers, "resolve_connection", lambda *a, **kw: {
        "id": env.connection, "model_id": "synthetic-oauth", "base_url": "", "revision": 1,
        "protocol": "codex_app_server"})
    monkeypatch.setattr(providers, "complete", complete)
    with pytest.raises(JobError, match="EVIDENCE_ACCESS_CHANGED"):
        run_answer(env, selection={"connection_id": env.connection, "model_id": "synthetic-oauth"})


def test_bounded_reference_ranking_keeps_wiki_and_whole_source_blocks(env):
    draft_source(env)
    sample = records(env)[0]
    values = [{**sample, "kind": "document", "version_id": svc.uid(), "block_id": svc.uid()} for _ in range(650)]
    knowledge = {**sample, "kind": "knowledge", "version_id": svc.uid(), "block_id": svc.uid()}
    values.append(knowledge)
    original = copy.deepcopy(values)
    ranked = rank_reference_evidence("如何核对价格来源", values)
    assert ranked and ranked[0]["kind"] == "knowledge"
    assert {e["kind"] for e in ranked} == {"document", "knowledge"}
    assert len(ranked) <= 12 and sum(len(e["text"].encode()) for e in ranked) <= 12000
    assert values == original


def test_local_bibliographic_note_is_not_reference_evidence(env, provider):
    source = draft_source(env)
    _, built = build_draft(env, source)
    with env.db.begin() as db:
        provenance = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == "wiki-provenance:" + built["created_resource_ids"][0]))
        provenance.config = {**provenance.config, "imported_local_note": {"citation_precision": "DOCUMENT"}}
    assert built["created_version_ids"][0] not in {e["version_id"] for e in records(env)}
