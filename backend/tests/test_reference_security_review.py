"""Independent reference security review: synthetic memory DB, zero network/model.

Regression guards for independently reproduced source, transfer and receipt defects.
Model invocation means an adapter call attempt, including invalid-output fallback.
"""
import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_reference_review import env as memory_env
from test_reference_review import make_version

from fund_kb import ai, ai_transport, api_consultation, providers
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.jobs import JobDispatcher, JobError
from fund_kb.reference_evidence import evidence_signature, reference_evidence
from fund_kb.settings import Settings


@pytest.fixture
def env(monkeypatch, tmp_path):
    # Reuse only the synthetic memory fixture; no app startup or runtime config.
    for fixture in memory_env.__wrapped__(monkeypatch):
        fixture.settings = Settings.model_construct(storage_dir=tmp_path / "objects",
            database_url="sqlite:///:memory:", retrieval_mode="wiki", llm_provider="evidence")
        yield fixture


def records(env, version_ids=None, actor=None):
    with env.db() as db:
        return reference_evidence(db, actor or env.owner, env.space, version_ids=version_ids)


def queued_run(env, *, scope="reference", selection=None, question="如何核对价格来源与业务日期"):
    tid, rid, jid = svc.uid(), svc.uid(), svc.uid()
    with env.db.begin() as db:
        db.add(m.ConsultationThread(id=tid, owner_id=env.owner, space_id=env.space, title="合成安全复核"))
        db.flush()
        db.add(m.ConsultationRun(id=rid, thread_id=tid, state="QUEUED", mode="answer",
            request={"question": question, "mode": "answer", "context": {}, "answer_scope": scope,
                     **({"model_selection": selection} if selection else {})},
            model_snapshot={"revision": 1} if selection else {}))
        db.flush()
        db.add(m.Job(id=jid, run_id=rid, owner_id=env.owner, kind="ANSWER", state="RUNNING",
            stage="GENERATING", attempts=1, lease_until=svc.now() + timedelta(minutes=2),
            payload={"run_id": rid}, dedupe_key=f"synthetic-review:{rid}"))
    return rid, jid


def execute(env, jid):
    dispatcher = JobDispatcher(env.settings, env.db, None)
    try:
        dispatcher._answer(jid, 1)
    finally:
        dispatcher.close()


def view(env, rid):
    with env.db() as db:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(answer_validator=ai.answer_validator())))
        ctx = svc.Context(request, db, db.get(m.User, env.owner), {}, {}, "getRun")
        return api_consultation.run_dict(ctx, db.get(m.ConsultationRun, rid))


def callback_provider(env, monkeypatch, *, invalid_output=False):
    selection = {"connection_id": svc.uid(), "model_id": "synthetic-review-only"}
    snapshot = {"id": selection["connection_id"], "model_id": selection["model_id"],
        "base_url": "", "protocol": "codex_app_server", "revision": 1,
        "owner_user_id": env.owner, "provider_id": "chatgpt-codex", "brand": "openai", "kind": "subscription"}
    calls = []

    def complete(connection, messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        from answer_content_fixture import reply_for
        answer = {} if invalid_output else reply_for(payload)
        if not invalid_output and answer.get("citations") and not answer["claims"]:
            first = answer["citations"][0]
            answer["claims"] = [{"id": "C1", "text": first["excerpt"], "evidence_ids": [first["id"]]}]
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}

    monkeypatch.setattr(providers, "resolve_connection", lambda *a, **kw: snapshot)
    monkeypatch.setattr(providers, "complete", complete)
    return selection, calls


def relate(env, left, right_resource, kind="EXPLAINS"):
    with env.db.begin() as db:
        db.add(m.RelationEdge(id=svc.uid(), source_version_id=left,
            target_resource_id=right_resource, relation_type=kind, conditions={}))


def test_ordinary_bidirectional_links_do_not_recurse_or_fingerprint_neighbor_body(env):
    left, right = make_version(env, "knowledge"), make_version(env, "knowledge")
    with env.db() as db:
        left_rid = db.get(m.ResourceVersion, left).resource_id
        right_rid = db.get(m.ResourceVersion, right).resource_id
    relate(env, left, right_rid)
    relate(env, right, left_rid)
    first = records(env, {left, right})
    assert {row["version_id"] for row in first} == {left, right}
    left_signature = next(evidence_signature(row) for row in first if row["version_id"] == left)
    with env.db.begin() as db:
        db.get(m.ResourceVersion, right).title = "邻居页面的新标题"
    assert [evidence_signature(row) for row in records(env, {left})] == [left_signature]


def test_real_depends_on_cycle_is_rejected(env):
    left, right = make_version(env, "knowledge"), make_version(env, "knowledge")
    with env.db() as db:
        left_rid = db.get(m.ResourceVersion, left).resource_id
        right_rid = db.get(m.ResourceVersion, right).resource_id
    relate(env, left, right_rid, "DEPENDS_ON")
    relate(env, right, left_rid, "DEPENDS_ON")
    assert records(env, {left, right}) == []


def test_depends_on_target_without_a_version_fails_closed(env):
    root = make_version(env, "knowledge")
    target_id = svc.uid()
    with env.db.begin() as db:
        db.add(m.Resource(id=target_id, space_id=env.space, kind="document",
            name="尚无原件或版本的依赖资料", owner_id=env.owner))
    relate(env, root, target_id, "DEPENDS_ON")
    assert records(env, {root}) == []


@pytest.mark.parametrize("change", ["acl", "reject", "body"])
def test_source_change_before_completion_callback_prevents_external_transfer(env, monkeypatch, change):
    selection, calls = callback_provider(env, monkeypatch)
    rid, jid = queued_run(env, selection=selection)
    original = ai.generate_answer

    def mutate_before_generation(*args, **kwargs):
        # Deterministic concurrent change after worker preflight, before payload construction.
        with env.db.begin() as db:
            source = db.get(m.ResourceVersion, env.source)
            if change == "acl":
                db.get(m.Resource, source.resource_id).restricted = True
            elif change == "reject":
                source.content_sha256 = svc.check_frozen_hash(db, source)
                source.state = "REJECTED"
            else:
                source.title = "模型回调前已变更的来源标题"
        return original(*args, **kwargs)

    monkeypatch.setattr(ai, "generate_answer", mutate_before_generation)
    with pytest.raises((svc.APIError, JobError)):
        execute(env, jid)
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.response is None and run.model_snapshot["model_invoked"] is False
        assert db.scalar(select(m.AuditEvent.id).where(
            m.AuditEvent.action == "answer.model_invocation_started")) is None
    # Discarding the final answer must not be mistaken for preventing disclosure.
    assert len(calls) == 0, f"provider callback received stale text {len(calls)} time(s)"


def test_configured_http_callback_uses_an_existing_timeout_setting(env, monkeypatch):
    env.settings = env.settings.model_copy(update={"llm_provider": "http",
        "llm_model": "synthetic-review-only", "llm_base_url": "https://example.invalid/v1"})
    calls = []

    def transport(base_url, operation, payload, *args, read_idle_timeout=None):
        assert args[-1] == env.settings.answer_model_timeout_seconds
        assert read_idle_timeout == min(env.settings.provider_read_idle_timeout_seconds, args[-1])
        calls.append(payload)
        from answer_content_fixture import reply_for
        content = reply_for(json.loads(payload["messages"][1]["content"]))
        if content.get("citations") and not content["claims"]:
            first = content["citations"][0]
            content["claims"] = [{"id": "C1", "text": first["excerpt"], "evidence_ids": [first["id"]]}]
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}

    monkeypatch.setattr(ai_transport, "post_json", transport)
    rid, jid = queued_run(env)
    execute(env, jid)
    assert len(calls) == 1 and view(env, rid)["model_snapshot"]["model_invoked"] is True


def test_failed_run_preserves_the_actual_completion_callback_receipt(env, monkeypatch):
    selection, calls = callback_provider(env, monkeypatch)
    completion = providers.complete

    def change_during_completion(*args, **kwargs):
        result = completion(*args, **kwargs)
        with env.db.begin() as db:
            source = db.get(m.ResourceVersion, env.source)
            db.get(m.Resource, source.resource_id).suspended = True
        return result

    monkeypatch.setattr(providers, "complete", change_during_completion)
    rid, jid = queued_run(env, selection=selection)
    dispatcher = JobDispatcher(env.settings, env.db, None)
    try:
        with pytest.raises((svc.APIError, JobError)) as caught:
            dispatcher._answer(jid, 1)
        dispatcher._fail(jid, 1, caught.value)
    finally:
        dispatcher.close()
    assert len(calls) == 1
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.state == "FAILED" and run.response is None
        assert run.model_snapshot.get("model_invoked") is True
        receipts = db.scalars(select(m.AuditEvent).where(
            m.AuditEvent.action == "answer.model_invocation_started")).all()
        assert len(receipts) == 1


@pytest.mark.parametrize("mode,expected", [("valid", 1), ("invalid_output", 1), ("no_evidence", 0),
                                          ("missing_fact", 0), ("formal", 0)])
def test_model_invoked_matches_actual_completion_callback_count(env, monkeypatch, mode, expected):
    selection, calls = callback_provider(env, monkeypatch, invalid_output=mode == "invalid_output")
    if mode == "missing_fact":
        with env.db.begin() as db:
            db.get(m.ResourceVersion, env.source).required_facts = ["business_date"]
    rid, jid = queued_run(env, selection=selection, scope="formal" if mode == "formal" else "reference",
        question="zzqvnxopaque" if mode == "no_evidence" else "如何核对价格来源与业务日期")
    if mode == "invalid_output":
        with pytest.raises(JobError, match="SYNTHESIS_OUTPUT_REJECTED") as caught:
            execute(env, jid)
        dispatcher = JobDispatcher(env.settings, env.db, None)
        try:
            dispatcher._fail(jid, 1, caught.value)
        finally:
            dispatcher.close()
    else:
        execute(env, jid)
    response = view(env, rid)
    assert len(calls) == expected
    assert response["model_snapshot"]["model_invoked"] is bool(expected)
    if mode == "invalid_output":
        assert response["state"] == "FAILED" and response["answer"] is None
        assert response["model_snapshot"]["execution_mode"] == "generation_rejected"
    elif mode == "formal":
        assert response["answer"]["review_status"] == "REQUIRES_EXPERT"
        assert response["answer"]["status"] == "INSUFFICIENT_EVIDENCE"
        assert response["answer"]["citations"] == []
    else:
        assert response["answer"]["review_status"] == "REQUIRES_EXPERT"
        assert response["answer_scope"] == "reference"
        assert response["answer"]["limitations"][0].startswith("资料辅助答疑")
    with env.db() as db:
        receipts = db.scalars(select(m.AuditEvent).where(
            m.AuditEvent.action == "answer.model_invocation_started")).all()
        assert len(receipts) == expected
        assert db.get(m.ResourceVersion, env.source).source_verified is False
        assert db.scalar(select(m.ReviewDecision.id)) is None
        assert db.scalar(select(m.Release.id)) is None


@pytest.mark.parametrize("change", ["epoch", "reject", "quarantine", "body", "suspend", "delete", "acl"])
def test_finished_reference_run_detects_mutation_and_hides_revoked_content(env, change):
    rid, jid = queued_run(env)
    execute(env, jid)
    assert view(env, rid)["invalidated"] is False
    with env.db.begin() as db:
        source = db.get(m.ResourceVersion, env.source)
        resource = db.get(m.Resource, source.resource_id)
        if change == "epoch":
            resource.access_epoch += 1
        elif change == "reject":
            source.content_sha256 = svc.check_frozen_hash(db, source)
            source.state = "REJECTED"
        elif change == "quarantine":
            db.get(m.Blob, source.source_blob_id).scan_state = "QUARANTINED"
        elif change == "body":
            source.title = "正文快照已改变"
        elif change == "suspend":
            resource.suspended = True
        elif change == "delete":
            resource.deleted_at = svc.now()
        else:
            resource.restricted = True
    response = view(env, rid)
    assert response["invalidated"] is True
    if change in {"delete", "acl"}:
        assert response["answer"] is None
