"""Offline manual-retry contracts; only synthetic SQLite and provider callbacks.

Reuse the pipeline WAL/SQL-trace fixtures, real retry handler, claim and worker.
Connection/space/source authorization stays real; no app startup or credentials.
"""
import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from answer_content_fixture import reply_for
from test_model_first_pipeline import (
    env, memory_env, trace,  # noqa: F401 - fixture dependency chain
    execute, invocation_receipts, provider, queue,
)
from fund_kb import ai, answer_planning, api_tasks, models as m, providers
from fund_kb import services as svc
from fund_kb.ingestion import text_sha256
from fund_kb.jobs import JobDispatcher, JobError


@pytest.fixture
def failed_run(env, trace, monkeypatch):
    real_resolver = providers.resolve_connection
    selection, binding = provider(env, trace, monkeypatch)
    # Restore real authorization; use a credential-free synthetic custom model.
    monkeypatch.setattr(providers, "resolve_connection", real_resolver)
    monkeypatch.setattr(providers, "_master_key", lambda *a, **kw: pytest.fail("No credential reads"))
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=selection["connection_id"],
            name=providers.NAMESPACE + selection["connection_id"], revision=1, updated_by=env.owner,
            config={"id": selection["connection_id"], "owner_user_id": env.owner,
                "space_id": env.space, "name": "合成无凭据重试模型", "provider_id": "custom",
                "protocol": "responses", "base_url": "https://synthetic.invalid/v1",
                "credential_mode": "none", "enabled": True, "allow_document_transfer": True,
                "custom_models": [{"id": selection["model_id"]}]}))
    initial_complete = providers.complete

    def fail_first_synthesis(*args, **kwargs):
        raw = initial_complete(*args, **kwargs)
        if len(trace.calls) == 2:
            raise providers.ProviderError("PROVIDER_INVALID_RESPONSE")
        return raw

    monkeypatch.setattr(providers, "complete", fail_first_synthesis)
    run_id, job_id = queue(env, selection, attachment=True)
    binding.update(run_id=run_id, job_id=job_id)
    error = execute(env, trace, job_id, expect_failure=True)
    assert error.code == "SYNTHESIS_OUTPUT_REJECTED"
    with env.db() as db:
        run, job = db.get(m.ConsultationRun, run_id), db.get(m.Job, job_id)
        assert run.state == job.state == "FAILED" and run.response is None
        assert run.model_snapshot["model_request_count"] == 2
        analysis = copy.deepcopy(run.policy_snapshot["question_analysis"])
        assert analysis["source"] == "model_prior_knowledge_unverified"
        receipts = list(db.scalars(select(m.AuditEvent).where(
            m.AuditEvent.object_id == job_id, m.AuditEvent.action == "answer.planning_completed")))
        assert len(receipts) == 1 and receipts[0].details["plan_sha256"] == svc.digest(analysis["plan"])
        assert len(invocation_receipts(db, job_id)) == 2
    result = SimpleNamespace(run_id=run_id, job_id=job_id, selection=selection,
                             analysis=analysis, calls=[], callback_counts=[])

    def synthesis_only(connection, messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        result.calls.append(payload)
        assert len(result.calls) == 1, "Manual retry must send only one new synthesis request"
        assert payload["evidence"] and all(row["id"].startswith("E") for row in payload["evidence"])
        with env.observer() as db:
            result.callback_counts.append(db.get(m.ConsultationRun, run_id).model_snapshot["model_request_count"])
        return {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(reply_for(payload), ensure_ascii=False)}}]}

    monkeypatch.setattr(providers, "complete", synthesis_only)
    monkeypatch.setattr(answer_planning, "plan_question",
                        lambda *a, **kw: pytest.fail("A validated retry must not call planning again"))
    return result


def manual_retry(env, saved, actor=None):
    with env.db.begin() as db:
        request = SimpleNamespace(path_params={"id": saved.job_id}, state=SimpleNamespace(trace_id=svc.uid()),
                                  app=SimpleNamespace(state=SimpleNamespace(settings=env.settings)))
        ctx = svc.Context(request, db, db.get(m.User, actor or env.owner), {}, {}, "retryJob")
        result = api_tasks.jobs(ctx)  # Dispatch list only: no background producer is started.
        assert result.status == 202 and ctx.dispatch == [saved.job_id]
        run, job = db.get(m.ConsultationRun, saved.run_id), db.get(m.Job, saved.job_id)
        assert run.state == job.state == "QUEUED" and job.attempts == 1
        assert run.policy_snapshot["reuse_question_analysis"] is True
        assert run.model_snapshot["model_request_count"] == 2


def run_retry(env, trace, saved, expected_code=None):
    dispatcher = JobDispatcher(env.settings, env.db, None)
    trace.armed = True
    try:
        assert dispatcher._claim(saved.job_id) == 2
        if expected_code:
            with pytest.raises((JobError, svc.APIError, providers.ProviderError, answer_planning.PlanningError)) as caught:
                dispatcher._answer(saved.job_id, 2)
            assert caught.value.code == expected_code
            dispatcher._fail(saved.job_id, 2, caught.value)
        else:
            dispatcher._answer(saved.job_id, 2)
    finally:
        trace.armed = False
        dispatcher.close()
    if expected_code:
        assert saved.calls == []
        with env.db() as db:
            run, job = db.get(m.ConsultationRun, saved.run_id), db.get(m.Job, saved.job_id)
            assert run.state == job.state == "FAILED" and run.response is None
            assert run.error_code == job.error_code == expected_code
            assert run.model_snapshot["model_request_count"] == 2
            assert len(invocation_receipts(db, saved.job_id)) == 2
            assert db.scalar(select(m.Outbox.id).where(m.Outbox.aggregate_id == saved.job_id,
                                                       m.Outbox.event_type == "JOB_RETRY")) is None


def test_manual_retry_reuses_receipted_plan_and_adds_only_one_synthesis_call(env, trace, failed_run):
    saved = failed_run
    manual_retry(env, saved)
    trace.events.clear()
    run_retry(env, trace, saved)
    assert len(saved.calls) == 1 and saved.callback_counts == [3]
    assert not any(kind == "planning_enter" for kind, _ in trace.events)
    assert any(kind == "source_select" for kind, _ in trace.events), "Retry must freshly read local evidence"
    with env.db() as db:
        run, job = db.get(m.ConsultationRun, saved.run_id), db.get(m.Job, saved.job_id)
        assert run.state == "COMPLETED" and job.state == "SUCCEEDED" and job.attempts == 2
        assert run.response["claims"] and run.response["citations"]
        assert run.policy_snapshot["question_analysis"] == saved.analysis
        assert run.model_snapshot["model_request_count"] == 3
        assert run.model_snapshot["planning_reused"] is True
        assert run.model_snapshot["planning_model_invoked"] is False
        assert run.model_snapshot["answer_model_invoked"] is True
        receipts = invocation_receipts(db, saved.job_id)
        assert [row.action for row in receipts].count("answer.planning_model_invocation_started") == 1
        assert [row.action for row in receipts].count("answer.model_invocation_started") == 2
        reused = list(db.scalars(select(m.AuditEvent).where(
            m.AuditEvent.object_id == saved.job_id, m.AuditEvent.action == "answer.planning_reused")))
        assert len(reused) == 1 and reused[0].details["additional_model_calls"] == 0
        assert reused[0].details["plan_sha256"] == svc.digest(saved.analysis["plan"])
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.object_id == saved.run_id,
            m.AuditEvent.action == "answer.attempt_archived")) is not None


@pytest.mark.parametrize("tamper", ["missing", "wrong_hash", "different_job"])
def test_resume_requires_matching_planning_receipt_for_same_job(env, trace, failed_run, tamper):
    saved = failed_run
    with env.db.begin() as db:
        receipt = db.scalar(select(m.AuditEvent).where(
            m.AuditEvent.object_id == saved.job_id, m.AuditEvent.action == "answer.planning_completed"))
        if tamper == "missing":
            db.delete(receipt)
        elif tamper == "wrong_hash":
            receipt.details = {**receipt.details, "plan_sha256": "0" * 64}
        else:
            receipt.object_id = svc.uid()
    manual_retry(env, saved)
    run_retry(env, trace, saved, "PLANNING_RECEIPT_MISSING")


def test_resume_validates_saved_plan_structure_even_with_matching_hash(env, trace, failed_run):
    saved = failed_run
    with env.db.begin() as db:
        run = db.get(m.ConsultationRun, saved.run_id)
        invalid_plan = {**saved.analysis["plan"], "evidence": []}
        run.policy_snapshot = {**run.policy_snapshot, "question_analysis": {**saved.analysis, "plan": invalid_plan}}
        receipt = db.scalar(select(m.AuditEvent).where(
            m.AuditEvent.object_id == saved.job_id, m.AuditEvent.action == "answer.planning_completed"))
        receipt.details = {**receipt.details, "plan_sha256": svc.digest(invalid_plan)}
    manual_retry(env, saved)
    run_retry(env, trace, saved, "PLANNING_SCHEMA_INVALID")


def test_another_account_cannot_manually_retry_private_run(env, failed_run):
    with pytest.raises(svc.APIError) as caught:
        manual_retry(env, failed_run, actor=env.reader)
    assert caught.value.code == "NOT_FOUND" and failed_run.calls == []
    with env.db() as db:
        run, job = db.get(m.ConsultationRun, failed_run.run_id), db.get(m.Job, failed_run.job_id)
        assert run.state == job.state == "FAILED" and job.attempts == 1
        assert run.model_snapshot["model_request_count"] == 2
        assert not run.policy_snapshot.get("reuse_question_analysis")
        assert db.scalar(select(m.Outbox.id).where(m.Outbox.aggregate_id == job.id)) is None


@pytest.mark.parametrize("change,code", [
    ("connection_owner", "NOT_FOUND"), ("model_revision", "CONNECTION_REVISION_CHANGED"),
    # Config writes bump the real revision, which is checked before transfer permission.
    ("transfer_revoked", "CONNECTION_REVISION_CHANGED"), ("space_revoked", "NOT_FOUND"),
    ("source_acl_revoked", "NOT_FOUND"),
])
def test_resume_rechecks_current_connection_and_source_permissions(env, trace, failed_run, change, code):
    saved = failed_run
    manual_retry(env, saved)
    with env.db.begin() as db:
        policy = db.get(m.RuntimePolicy, saved.selection["connection_id"])
        if change == "connection_owner":
            policy.config = {**policy.config, "owner_user_id": env.reader}
        elif change == "model_revision":
            policy.revision += 1
        elif change == "transfer_revoked":
            policy.config = {**policy.config, "allow_document_transfer": False}
        elif change == "space_revoked":
            db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == env.space,
                                                    m.SpaceMember.user_id == env.owner))
        else:
            source = db.get(m.ResourceVersion, env.source)
            db.get(m.Resource, source.resource_id).restricted = True
    run_retry(env, trace, saved, code)


def test_resume_rechecks_evidence_changed_after_retrieval_before_synthesis(env, trace, failed_run, monkeypatch):
    saved = failed_run
    manual_retry(env, saved)
    original_generate = ai.generate_answer
    changed = []

    def change_source_before_callback(*args, **kwargs):
        with env.observer.begin() as db:
            block = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == env.source))
            text = "合成变更后的价格来源正文，不得沿用先前快照。"
            block.data, block.search_text, block.content_sha256 = {"text": text}, text, text_sha256(text)
        changed.append(True)
        return original_generate(*args, **kwargs)

    monkeypatch.setattr(ai, "generate_answer", change_source_before_callback)
    run_retry(env, trace, saved, "EVIDENCE_ACCESS_CHANGED")
    assert changed == [True]
