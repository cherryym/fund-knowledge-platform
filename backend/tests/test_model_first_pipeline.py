"""Actual ANSWER worker contract: model planning -> local reads -> synthesis.

Only synthetic fixtures and a tmp_path SQLite WAL file are used. The inherited
fixture rejects sockets; providers.complete is always replaced by an in-process
callback. Do not mock retrieval or the worker to manufacture the required order.
"""
import copy
import json
import re
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, literal_column, select, update
from sqlalchemy.exc import OperationalError

from answer_content_fixture import reply_for
from test_reference_security_review import env as memory_env, queued_run  # noqa: F401
from fund_kb import answer_planning, api_consultation, models as m, providers
from fund_kb import services as svc
from fund_kb.db import build_engine, make_session_factory
from fund_kb.jobs import JobDispatcher, JobError


PLAN = {
    "interpretation": "理解用户的业务问题，尚未读取本地资料。",
    "initial_assessment": "需要核对适用条件与价格来源，以下方向均待查证。",
    "search_queries": ["价格来源"],
    "focus_terms": ["价格来源"],
    "decision_points": [],
    "missing_facts": [],
}
CONTEXT = {"business_date": "2026-09-09", "asset_type": "股票"}
SOURCE_SELECT = re.compile(
    r'\b(?:from|join)\s+(?:["`\[]?\w+["`\]]?\s*\.\s*)?["`\[]?'
    r'(content_blocks|resources|resource_versions|evidence_links)\b', re.I,
)
UUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I)
HASH = re.compile(r"\b[0-9a-f]{64}\b", re.I)
INVOCATION_PHASES = {
    "answer.planning_model_invocation_started": "planning",
    "answer.model_invocation_started": "synthesis",
}


@pytest.fixture
def env(memory_env, tmp_path):
    """Independent callback observations must not share the worker transaction."""
    # This module preserves the legacy structured engine contract. The new
    # default Wiki reader has separate full-page/narrative worker tests.
    memory_env.settings = memory_env.settings.model_copy(update={"answer_engine": "structured"})
    path = tmp_path / "synthetic-model-first.sqlite3"
    with sqlite3.connect(path, isolation_level=None) as target:
        with memory_env.db.kw["bind"].connect() as source:
            source.connection.driver_connection.backup(target)
        assert target.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    engine = build_engine(f"sqlite:///{path}")
    observer_engine = build_engine(f"sqlite:///{path}")
    memory_env.db = make_session_factory(engine)
    memory_env.observer = make_session_factory(observer_engine)
    with memory_env.db() as db:
        memory_env.document_titles = set(db.scalars(select(m.Resource.name))) | set(
            db.scalars(select(m.ResourceVersion.title)))
    try:
        yield memory_env
    finally:
        observer_engine.dispose()
        engine.dispose()


@pytest.fixture
def trace(env, monkeypatch):
    observed = SimpleNamespace(events=[], calls=[], receipts_at_callback=[], snapshots_at_callback=[], armed=False)
    original_plan = answer_planning.plan_question

    def plan(*args, **kwargs):
        observed.events.append(("planning_enter", None))
        result = original_plan(*args, **kwargs)
        observed.events.append(("planning_return", None))
        return result

    def sql(connection, cursor, statement, parameters, context, executemany):
        if observed.armed and re.search(r"\bselect\b", statement, re.I):
            for table in SOURCE_SELECT.findall(statement):
                observed.events.append(("source_select", table.lower()))

    monkeypatch.setattr(answer_planning, "plan_question", plan)
    engines = [env.db.kw["bind"], env.observer.kw["bind"]]
    for engine in engines:
        event.listen(engine, "before_cursor_execute", sql)
    try:
        yield observed
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", sql)


def queue(env, selection=None, *, attachment=False):
    run_id, job_id = queued_run(env, selection=selection)
    with env.db.begin() as db:
        run = db.get(m.ConsultationRun, run_id)
        run.request = {**run.request, "reasoning_strategy": "model_first",
                       "context": copy.deepcopy(CONTEXT), "require_model": False,
                       **({"attachment_version_ids": [env.source]} if attachment else {})}
    return run_id, job_id


def invocation_receipts(db, job_id):
    return list(db.scalars(select(m.AuditEvent).where(
        m.AuditEvent.object_id == job_id,
        m.AuditEvent.action.in_(INVOCATION_PHASES),
    ).order_by(m.AuditEvent.created_at, m.AuditEvent.id)))


def provider(env, trace, monkeypatch, *, planning_invalid=False, after_planning_callback=None, protocol="openai", provider_id="minimax"):
    selection = {"connection_id": svc.uid(), "model_id": "synthetic-minimax-model-first"}
    snapshot = {"id": selection["connection_id"], "model_id": selection["model_id"],
                "base_url": "https://minimax.invalid/v1", "protocol": protocol,
                "revision": 1, "owner_user_id": env.owner, "provider_id": provider_id, "brand": provider_id}
    binding = {}

    def complete(connection, messages, **kwargs):
        # Check messages, not the trusted connection-routing object containing
        # the synthetic connection ID needed to authorize the provider itself.
        payload = json.loads(messages[1]["content"])
        number = len(trace.calls) + 1
        trace.calls.append({"messages": copy.deepcopy(messages), "payload": payload,
                            "max_tokens": kwargs.get("max_tokens"), "timeout": kwargs.get("timeout"),
                            "output_schema": copy.deepcopy(kwargs.get("output_schema"))})
        trace.events.append(("provider", number))
        with env.observer() as db:
            receipts = invocation_receipts(db, binding["job_id"])
            trace.receipts_at_callback.append([
                {"id": row.id, "action": row.action, "phase": INVOCATION_PHASES[row.action]}
                for row in receipts])
            trace.snapshots_at_callback.append(copy.deepcopy(
                db.get(m.ConsultationRun, binding["run_id"]).model_snapshot))
        if number == 1:
            if after_planning_callback:
                after_planning_callback()
            answer = {} if planning_invalid else PLAN
        else:
            assert number == 2, "model_first must not silently repair/retry with extra model calls"
            answer = reply_for(payload)
        trace.events.append(("provider_return", number))
        return {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}}]}

    monkeypatch.setattr(providers, "resolve_connection", lambda *args, **kwargs: copy.deepcopy(snapshot))
    monkeypatch.setattr(providers, "complete", complete)
    return selection, binding


def execute(env, trace, job_id, *, expect_failure=False,
            failure_types=(JobError, svc.APIError, answer_planning.PlanningError)):
    trace.armed = True
    dispatcher = JobDispatcher(env.settings, env.db, None)
    try:
        if expect_failure:
            with pytest.raises(failure_types) as caught:
                dispatcher._answer(job_id, 1)
            dispatcher._fail(job_id, 1, caught.value)
            return caught.value
        dispatcher._answer(job_id, 1)
    finally:
        dispatcher.close()
        trace.armed = False


def assert_planning_payload_clean(env, call):
    payload = call["payload"]
    assert set(payload) == {"question", "mode", "context"}
    assert payload == {"question": "如何核对价格来源与业务日期", "mode": "answer", "context": CONTEXT}
    wire = json.dumps(call["messages"], ensure_ascii=False)
    assert not UUID.search(wire) and not HASH.search(wire)
    assert all(title not in wire for title in env.document_titles)


def test_worker_plans_without_source_reads_then_synthesizes_compact_evidence(env, trace, monkeypatch):
    selection, binding = provider(env, trace, monkeypatch)
    run_id, job_id = queue(env, selection, attachment=True)
    binding.update(job_id=job_id, run_id=run_id)
    execute(env, trace, job_id)

    assert len(trace.calls) == 2
    assert_planning_payload_clean(env, trace.calls[0])
    assert trace.calls[0]["max_tokens"] == env.settings.answer_planning_max_output_tokens
    positions = {name: next(i for i, e in enumerate(trace.events) if e == value) for name, value in (
        ("first", ("provider", 1)), ("plan_done", ("planning_return", None)), ("second", ("provider", 2)))}
    source_positions = [i for i, e in enumerate(trace.events) if e[0] == "source_select"]
    assert source_positions, "synthesis must actually read local evidence after the plan"
    assert positions["first"] < positions["plan_done"] < min(source_positions) < positions["second"]

    second = trace.calls[1]["payload"]
    assert "output_skeleton" not in second and second["evidence"]
    assert all(re.fullmatch(r"E[1-9]\d*", row["id"]) and row["text"] for row in second["evidence"])
    assert {"summary", "claims", "analysis"} <= set(second["schema"]["required"])
    assert not ({"run_id", "facts", "scope", "citations"} & set(second["schema"]["properties"]))
    assert [len(rows) for rows in trace.receipts_at_callback] == [1, 2]
    assert [row["phase"] for row in trace.receipts_at_callback[0]] == ["planning"]
    assert {row["phase"] for row in trace.receipts_at_callback[1]} == {"planning", "synthesis"}
    assert len({row["id"] for row in trace.receipts_at_callback[1]}) == 2
    assert [s["model_request_count"] for s in trace.snapshots_at_callback] == [1, 2]
    assert trace.snapshots_at_callback[0]["planning_model_invoked"] is True
    assert trace.snapshots_at_callback[0]["answer_model_invoked"] is False
    assert trace.snapshots_at_callback[1]["planning_model_invoked"] is True
    assert trace.snapshots_at_callback[1]["answer_model_invoked"] is True
    with env.db() as db:
        run, job = db.get(m.ConsultationRun, run_id), db.get(m.Job, job_id)
        assert run.state == "COMPLETED" and job.state == "SUCCEEDED"
        assert run.request["reasoning_strategy"] == "model_first"
        assert run.model_snapshot["model_request_count"] == 2
        assert run.model_snapshot["model_invoked"] is True
        assert run.model_snapshot["planning_model_invoked"] is True
        assert run.model_snapshot["answer_model_invoked"] is True
        assert run.response["claims"] and run.response["citations"]
        assert run.response["review_status"] == "REQUIRES_EXPERT"
        assert len(invocation_receipts(db, job_id)) == 2
        # SQLite insertion order avoids ties in millisecond server timestamps.
        audits = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.object_id == job_id)
                                .order_by(literal_column("rowid"))))
        milestones = ["answer.planning_started", "answer.planning_model_invocation_started",
                      "answer.planning_completed", "answer.retrieval_started", "answer.model_invocation_started"]
        assert [r.action for r in audits if r.action in milestones] == milestones
        assert all(r.details["local_sources_loaded"] == 0 for r in audits if r.action in milestones[:3])


def test_auto_mode_is_normalized_before_source_free_planning(env, trace, monkeypatch):
    selection, binding = provider(env, trace, monkeypatch)
    run_id, job_id = queue(env, selection, attachment=True)
    with env.db.begin() as db:
        run = db.get(m.ConsultationRun, run_id)
        run.mode = "auto"
        run.request = {**run.request, "mode": "auto"}
    binding.update(job_id=job_id, run_id=run_id)

    original_plan = answer_planning.plan_question
    planner_modes = []

    def record_mode(question, mode, context, completion_client):
        planner_modes.append(mode)
        return original_plan(question, mode, context, completion_client)

    monkeypatch.setattr(answer_planning, "plan_question", record_mode)
    execute(env, trace, job_id)

    assert planner_modes == ["answer"]  # Normalize in the worker, before planner entry.
    assert len(trace.calls) == 2
    assert_planning_payload_clean(env, trace.calls[0])
    assert trace.calls[0]["max_tokens"] == env.settings.answer_planning_max_output_tokens
    first = trace.events.index(("provider", 1))
    plan_done = trace.events.index(("planning_return", None))
    second = trace.events.index(("provider", 2))
    source_positions = [i for i, e in enumerate(trace.events) if e[0] == "source_select"]
    assert source_positions and first < plan_done < min(source_positions) < second
    assert trace.calls[1]["payload"]["evidence"]
    assert all(re.fullmatch(r"E[1-9]\d*", row["id"])
               for row in trace.calls[1]["payload"]["evidence"])
    with env.db() as db:
        run, job = db.get(m.ConsultationRun, run_id), db.get(m.Job, job_id)
        assert run.request["mode"] == "auto"
        assert run.request["reasoning_strategy"] == "model_first"
        assert run.state == "COMPLETED" and job.state == "SUCCEEDED"
        assert run.model_snapshot["model_request_count"] == 2


@pytest.mark.parametrize("provider_id,protocol", [("openai", "responses"), ("minimax", "responses"),
    ("openrouter", "openai"), ("anthropic", "anthropic"), ("google", "gemini"),
    ("ollama", "ollama"), ("chatgpt", "codex_app_server")])
def test_worker_budget_schema_and_receipts_are_provider_independent(env, trace, monkeypatch, provider_id, protocol):
    selection, binding = provider(env, trace, monkeypatch, provider_id=provider_id, protocol=protocol)
    run_id, job_id = queue(env, selection)
    binding.update(job_id=job_id, run_id=run_id)
    execute(env, trace, job_id)
    assert [call["timeout"] for call in trace.calls] == [90, 180]
    assert [call["max_tokens"] for call in trace.calls] == [4096, 8192]
    assert trace.calls[0]["output_schema"] == answer_planning.PLANNING_SCHEMA
    assert trace.calls[1]["output_schema"] == trace.calls[1]["payload"]["schema"]
    with env.db() as db:
        run = db.get(m.ConsultationRun, run_id)
        request = run.model_snapshot["last_request"]
        assert request["state"] == "received" and request["finish_reason"] == "stop"
        assert request["limits"]["total_seconds"] == 180 and request["response_chars"] > 0
        assert run.model_snapshot["validation_status"] == "passed"
        assert run.model_snapshot["model_request_count"] == 2


@pytest.mark.parametrize("code,diagnostic,expected_state", [
    ("PROVIDER_CONNECTION_INTERRUPTED", {}, "failed"),
    ("PROVIDER_TIMEOUT", {}, "failed"),
    ("PROVIDER_OUTPUT_SCHEMA_INVALID", {"outcome": "completed", "finish_reason": "stop",
        "response_chars": 250, "usage": {"completion_tokens": 80}}, "received"),
])
def test_failed_attempt_keeps_truthful_response_receipt_without_silent_retry(env, trace, monkeypatch, code, diagnostic, expected_state):
    selection, binding = provider(env, trace, monkeypatch)
    original = providers.complete
    def completion(*args, **kwargs):
        raw = original(*args, **kwargs)
        if len(trace.calls) == 2:
            raise providers.ProviderError(code, diagnostic)
        return raw
    monkeypatch.setattr(providers, "complete", completion)
    run_id, job_id = queue(env, selection)
    binding.update(job_id=job_id, run_id=run_id)
    execute(env, trace, job_id, expect_failure=True)
    with env.db() as db:
        run, job = db.get(m.ConsultationRun, run_id), db.get(m.Job, job_id)
        assert job.state == "FAILED" and run.state == "FAILED"
        assert run.model_snapshot["last_request"]["state"] == expected_state
        assert run.model_snapshot["last_request"]["error_code"] == code
        assert run.model_snapshot["model_request_count"] == 2 and len(trace.calls) == 2
        assert run.response is None


def test_initial_planning_poll_reads_run_evidence_without_resolving_attachment_sources(env, monkeypatch):
    monkeypatch.setattr(providers, "complete", lambda *a, **kw: pytest.fail("Polling must not invoke a model"))
    run_id, _ = queue(env, attachment=True)
    engine = env.db.kw["bind"]

    for state in ("QUEUED", "RUNNING"):
        with env.db.begin() as db:
            db.get(m.ConsultationRun, run_id).state = state
        # A fresh session prevents an identity-map hit from hiding source reads.
        with env.db() as db:
            run = db.get(m.ConsultationRun, run_id)
            ctx = SimpleNamespace(db=db, user=db.get(m.User, env.owner))
            assert run.request["reasoning_strategy"] == "model_first"
            assert run.request["attachment_version_ids"] == [env.source]
            assert not run.evidence_snapshot and not run.response
            assert not (run.policy_snapshot or {}).get("question_analysis")
            statements = []

            def observe(connection, cursor, statement, parameters, context, executemany):
                if re.search(r"\bselect\b", statement, re.I):
                    statements.append(statement)

            event.listen(engine, "before_cursor_execute", observe)
            try:
                assert api_consultation.run_sources_readable(ctx, run) == set()
            finally:
                event.remove(engine, "before_cursor_execute", observe)

            assert any(re.search(
                rf'\b(?:from|join)\s+["`\[]?{re.escape(m.RunEvidence.__tablename__)}\b',
                statement, re.I) for statement in statements), state
            assert not any(SOURCE_SELECT.search(statement) for statement in statements), (state, statements)


def test_planning_failure_never_reads_sources_or_returns_extractive_fallback(env, trace, monkeypatch):
    selection, binding = provider(env, trace, monkeypatch, planning_invalid=True)
    run_id, job_id = queue(env, selection)
    binding.update(job_id=job_id, run_id=run_id)
    error = execute(env, trace, job_id, expect_failure=True)
    assert error.code == "PLANNING_SCHEMA_INVALID"
    assert len(trace.calls) == 1
    assert_planning_payload_clean(env, trace.calls[0])
    assert not [e for e in trace.events if e[0] == "source_select"]
    assert ("planning_return", None) not in trace.events
    with env.db() as db:
        run = db.get(m.ConsultationRun, run_id)
        assert run.state == "FAILED" and run.response is None
        assert run.model_snapshot["model_request_count"] == 1
        assert run.model_snapshot["planning_model_invoked"] is True
        assert run.model_snapshot["answer_model_invoked"] is False
        assert [INVOCATION_PHASES[r.action] for r in invocation_receipts(db, job_id)] == ["planning"]


@pytest.mark.parametrize("change", ["cancel", "revoke_space"])
def test_cancel_or_permission_revocation_after_planning_blocks_second_call(env, trace, monkeypatch, change):
    def mutate():
        # A separate WAL connection commits cancellation/revocation while the
        # synthetic first callback is active. No shared-session illusion.
        with env.observer.begin() as db:
            if change == "cancel":
                db.execute(update(m.Job).where(m.Job.id == binding["job_id"]).values(cancel_requested=True))
            else:
                db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == env.space,
                                                       m.SpaceMember.user_id == env.owner))

    selection, binding = provider(env, trace, monkeypatch, after_planning_callback=mutate)
    run_id, job_id = queue(env, selection)
    binding.update(job_id=job_id, run_id=run_id)
    execute(env, trace, job_id, expect_failure=True)
    assert len(trace.calls) == 1
    assert_planning_payload_clean(env, trace.calls[0])
    with env.db() as db:
        run = db.get(m.ConsultationRun, run_id)
        assert run.state in {"CANCELLED", "FAILED"} and run.response is None
        assert run.model_snapshot["model_request_count"] == 1
        assert [INVOCATION_PHASES[r.action] for r in invocation_receipts(db, job_id)] == ["planning"]


@pytest.mark.parametrize("after_call", [1, 2], ids=["after_planning", "after_synthesis"])
def test_database_error_after_a_model_call_does_not_enqueue_automatic_retry(env, trace, monkeypatch, after_call):
    selection, binding = provider(env, trace, monkeypatch)
    run_id, job_id = queue(env, selection)
    binding.update(job_id=job_id, run_id=run_id)
    injected = []

    def fail_next_worker_select(connection, cursor, statement, parameters, context, executemany):
        if (trace.armed and not injected and ("provider_return", after_call) in trace.events
                and re.search(r"\bselect\b", statement, re.I)):
            injected.append(True)
            raise OperationalError("SELECT synthetic", {}, RuntimeError("synthetic DB failure after model"))

    engine = env.db.kw["bind"]
    event.listen(engine, "before_cursor_execute", fail_next_worker_select)
    try:
        error = execute(env, trace, job_id, expect_failure=True,
                        failure_types=(OperationalError, JobError) if after_call == 1 else (OperationalError,))
    finally:
        event.remove(engine, "before_cursor_execute", fail_next_worker_select)
    if isinstance(error, JobError):
        # A DB failure while recording the first response is wrapped by planning.
        assert error.code == "PLANNING_COMPLETION_FAILED"
    assert injected == [True] and len(trace.calls) == after_call
    with env.db() as db:
        run, job = db.get(m.ConsultationRun, run_id), db.get(m.Job, job_id)
        assert job.state == "FAILED" and run.state == "FAILED" and run.response is None
        assert run.model_snapshot["model_request_count"] == after_call
        assert len(invocation_receipts(db, job_id)) == after_call
        assert db.scalar(select(m.Outbox.id).where(m.Outbox.aggregate_id == job_id,
                                                  m.Outbox.event_type == "JOB_RETRY")) is None


def test_model_first_without_a_model_is_model_required_before_any_source_read(env, trace, monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "complete", lambda *a, **kw: calls.append(True) or pytest.fail("No model configured"))
    run_id, job_id = queue(env)  # require_model=False must not enable fallback.
    error = execute(env, trace, job_id, expect_failure=True)
    assert error.code == "MODEL_REQUIRED"
    assert calls == [] and trace.calls == []
    assert not [e for e in trace.events if e[0] == "source_select"]
    with env.db() as db:
        run = db.get(m.ConsultationRun, run_id)
        assert run.state == "FAILED" and run.response is None
        assert run.model_snapshot.get("model_request_count", 0) == 0
        assert not invocation_receipts(db, job_id)
