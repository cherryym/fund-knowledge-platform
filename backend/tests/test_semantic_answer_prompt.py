"""Current semantic-RAG prompts, version receipts and safe planning reuse."""
import copy
from hashlib import sha256

import pytest
from sqlalchemy import select
from test_wiki_reader_job import base_env, execute, prepare  # noqa: F401
from test_wiki_reader_job import env as env  # noqa: PLC0414

from fund_kb import models as m
from fund_kb import providers
from fund_kb import services as svc
from fund_kb.jobs import JobDispatcher
from fund_kb.wiki_reader import (
    NOTES_INSTRUCTION,
    PLANNING_INSTRUCTION,
    PROMPT_VERSION,
    SELECTION_INSTRUCTION,
    SYNTHESIS_INSTRUCTION,
    SYSTEM,
    SYSTEM_SHA256,
)


def test_semantic_prompt_distinguishes_retrieval_evidence_and_business_scope():
    assert sha256(SYSTEM.encode()).hexdigest() == SYSTEM_SHA256
    assert "完整语义单元" in SYSTEM and "不等于原文支持该主张" in SYSTEM
    assert "不只是文件标题" in SYSTEM and "网页菜单" in SYSTEM
    assert "估值取价" in SYSTEM and "合同回售价、估值价格与会计结转金额" in SYSTEM
    assert "同一主问题" in SYSTEM and "不无差别读取全册" in SELECTION_INSTRUCTION
    assert "不是数量或时长限制" in PLANNING_INSTRUCTION
    assert "原问题本身会参与检索" in PLANNING_INSTRUCTION
    assert "E条款直接支持" in SYNTHESIS_INSTRUCTION and "提要不是新证据" in NOTES_INSTRUCTION
    assert "无需固定字段、段数或JSON" in SYSTEM


def test_live_worker_sends_the_versioned_system_prompt_in_each_phase(env, monkeypatch):
    def responder(calls, _):
        if len(calls) == 1:
            assert PLANNING_INSTRUCTION in calls[0]
            return "仅核对本题条件。"
        if len(calls) == 2:
            import re
            return "READ " + re.search(r"\bW\d+\b", calls[-1])[0]
        assert SYNTHESIS_INSTRUCTION in calls[-1]
        return "应按本题条件核对直接条款，不把推断当已确认规定。[E1]"
    rid, jid, _ = prepare(env, monkeypatch, responder)
    original = providers.complete
    systems = []
    def capture(connection, messages, **kwargs):
        assert messages[0] == {"role": "system", "content": SYSTEM}
        systems.append(messages[0]["content"])
        return original(connection, messages, **kwargs)
    monkeypatch.setattr(providers, "complete", capture)
    execute(env, jid)
    with env.db() as db:
        row = db.get(m.ConsultationRun, rid)
        assert row.state == "COMPLETED" and len(systems) == 3
        assert row.model_snapshot["prompt_version"] == PROMPT_VERSION
        assert row.model_snapshot["system_prompt_sha256"] == SYSTEM_SHA256
        assert row.policy_snapshot["question_analysis"]["local_sources_loaded"] == 0
        assert row.model_snapshot["last_request"]["limits"]["total_seconds"] is None
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.object_id == jid,
            m.AuditEvent.action == "answer.model_invocation_started")))
        assert len(events) == 3 and all(e.details["system_prompt_sha256"] == SYSTEM_SHA256 for e in events)


@pytest.mark.parametrize("same_prompt", [False, True])
def test_retry_does_not_reuse_a_plan_from_an_old_system_prompt(env, monkeypatch, same_prompt):
    import re
    phases = []
    def responder(calls, _):
        if PLANNING_INSTRUCTION in calls[-1]:
            phases.append("planning")
            return "重新按当前业务意图查证。"
        if "完整目录的一部分" in calls[-1]:
            return "READ " + re.search(r"\bW\d+\b", calls[-1])[0]
        return "本次说明需核对原文。[E1]"
    rid, jid, _ = prepare(env, monkeypatch, responder)
    plan = {"interpretation": "旧计划", "initial_assessment": "待核对", "search_queries": ["旧词"]}
    version = PROMPT_VERSION if same_prompt else "old-title-only-prompt"
    fingerprint = SYSTEM_SHA256 if same_prompt else "0" * 64
    with env.db.begin() as db:
        row = db.get(m.ConsultationRun, rid)
        row.policy_snapshot = {**row.policy_snapshot, "question_analysis": {
            "source": "model_prior_knowledge_unverified", "local_sources_loaded": 0,
            "plan": copy.deepcopy(plan), "prompt_version": version, "system_prompt_sha256": fingerprint}}
        job = db.get(m.Job, jid)
        job.attempts = 2
        db.add(m.AuditEvent(id=svc.uid(), actor_id=env.owner, object_type="Job", object_id=jid,
            action="answer.planning_completed", trace_id="synthetic", outcome="SUCCESS",
            details={"plan_sha256": svc.digest(plan), "system_prompt_sha256": fingerprint}))
    dispatcher = JobDispatcher(env.settings, env.db, None)
    try:
        dispatcher._answer(jid, 2)
    finally:
        dispatcher.close()
    with env.db() as db:
        row = db.get(m.ConsultationRun, rid)
        assert row.state == "COMPLETED"
        assert row.model_snapshot["planning_reused"] is same_prompt
        assert bool(phases) is not same_prompt
