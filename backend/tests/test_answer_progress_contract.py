"""Synthetic WAL database phase checks; never invokes an external model."""
import sqlite3

import pytest
from sqlalchemy import select

from fund_kb import models as m
from fund_kb import reference_evidence as reference
from fund_kb import services as svc
from fund_kb.db import build_engine, make_session_factory
from fund_kb.jobs import JobDispatcher, JobError
from test_reference_security_review import env as memory_env, callback_provider, queued_run  # noqa: F401


@pytest.fixture
def env(memory_env, tmp_path):
    # Multiple independent observers require separate connections. The shared
    # in-memory fixture cannot model concurrent readers and a cancellation writer.
    path = tmp_path / "synthetic-progress.sqlite3"
    with sqlite3.connect(path, isolation_level=None) as target:
        with memory_env.db.kw["bind"].connect() as source:
            source.connection.driver_connection.backup(target)
        assert target.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    engine = build_engine(f"sqlite:///{path}")
    memory_env.db = make_session_factory(engine)
    try:
        yield memory_env
    finally:
        engine.dispose()


def test_retrieval_state_is_committed_before_work_and_model_phases_are_truthful(env, monkeypatch):
    selection, model_calls = callback_provider(env, monkeypatch)
    run_id, job_id = queued_run(env, selection=selection)
    with env.db.begin() as db:
        run = db.get(m.ConsultationRun, run_id)
        run.completed_at = svc.now()  # Previous-attempt UI fields must be cleared.
        run.model_snapshot = {**run.model_snapshot, "model_invoked": True,
            "execution_mode": "generation_rejected", "evidence_count": 0}

    original = reference.reference_evidence
    observations = []

    def observing(db, user, space_id, context=None, *, version_ids=None):
        if version_ids is None:
            # This checks the read-transaction contract, not a latency benchmark.
            assert db.get_bind().get_execution_options()["sqlite_transaction_mode"] == "DEFERRED"
            assert db.autoflush is False
            with env.db() as observer:
                run, job = observer.get(m.ConsultationRun, run_id), observer.get(m.Job, job_id)
                observations.append((run.state, job.stage))
                assert run.state == "RUNNING" and job.stage == "RETRIEVING"
                assert run.completed_at is None
                assert run.model_snapshot["model_invoked"] is False
                assert run.model_snapshot["execution_mode"] is None
                assert run.model_snapshot["attempt"] == 1
        return original(db, user, space_id, context, version_ids=version_ids)

    monkeypatch.setattr(reference, "reference_evidence", observing)
    dispatcher = JobDispatcher(env.settings, env.db, None)
    try:
        dispatcher._answer(job_id, 1)
    finally:
        dispatcher.close()
    assert observations == [("RUNNING", "RETRIEVING")]
    assert len(model_calls) == 1
    with env.db() as db:
        run, job = db.get(m.ConsultationRun, run_id), db.get(m.Job, job_id)
        assert run.state == "COMPLETED" and job.state == "SUCCEEDED"
        assert run.model_snapshot["model_invoked"] is True
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.object_id == job_id)
            .order_by(m.AuditEvent.created_at)))
        milestones = [event.details.get("stage") if event.action == "job.stage" else event.action for event in events]
        required = ["answer.retrieval_started", "SELECTING_SOURCES", "PREPARING_ANSWER",
            "answer.evidence_frozen", "answer.model_invocation_started", "VALIDATING_ANSWER", "job.succeeded"]
        assert [item for item in milestones if item in required] == required


def test_require_model_prevents_silent_extractive_success(env, monkeypatch):
    run_id, job_id = queued_run(env)
    with env.db.begin() as db:
        run = db.get(m.ConsultationRun, run_id)
        run.request = {**run.request, "require_model": True}
    calls = []
    monkeypatch.setattr(reference, "reference_evidence", lambda *a, **k: calls.append(True) or [])
    dispatcher = JobDispatcher(env.settings, env.db, None)
    try:
        with pytest.raises(JobError, match="MODEL_REQUIRED"):
            dispatcher._answer(job_id, 1)
    finally:
        dispatcher.close()
    assert calls == []
    with env.db() as db:
        assert db.get(m.ConsultationRun, run_id).response is None
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.object_id == job_id,
            m.AuditEvent.action == "answer.model_invocation_started")) is None


def test_cancel_during_retrieval_blocks_model_send(env, monkeypatch):
    selection, model_calls = callback_provider(env, monkeypatch)
    _, job_id = queued_run(env, selection=selection)
    original = reference.reference_evidence

    def cancel_after_read(db, user, space_id, context=None, *, version_ids=None):
        rows = original(db, user, space_id, context, version_ids=version_ids)
        if version_ids is None:
            with env.db.begin() as writer:
                writer.get(m.Job, job_id).cancel_requested = True
        return rows

    monkeypatch.setattr(reference, "reference_evidence", cancel_after_read)
    dispatcher = JobDispatcher(env.settings, env.db, None)
    try:
        with pytest.raises(JobError, match="CANCELLED"):
            dispatcher._answer(job_id, 1)
    finally:
        dispatcher.close()
    assert model_calls == []
