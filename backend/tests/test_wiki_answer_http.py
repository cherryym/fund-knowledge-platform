"""HTTP response contract regression: long free-form planning must remain readable."""
from datetime import timedelta

import pytest

from test_wiki import env as http_env
from fund_kb import models as m, services as svc
from fund_kb.wiki_answer_content import build_narrative_answer


@pytest.mark.parametrize("state", ["RUNNING", "COMPLETED"])
def test_get_run_and_thread_deliver_full_freeform_plan_through_real_http(http_env, state):
    env = http_env
    env.login()
    rid, tid, jid = svc.uid(), svc.uid(), svc.uid()
    assessment = "这是公开的待验证问题研判，不是隐藏思维链。" * 250
    narrative = "# 完整答复\n\n" + "保留正文，不要求JSON格式。" * 500
    with env.db.begin() as db:
        db.add(m.ConsultationThread(id=tid, owner_id=env.owner, space_id=env.space, title="合成接口长研判"))
        db.flush()
        db.add(m.ConsultationRun(id=rid, thread_id=tid, state=state, mode="answer",
            request={"question":"合成问题", "mode":"answer", "context":{}, "answer_scope":"reference", "reasoning_strategy":"model_first"},
            model_snapshot={"answer_engine":"wiki_reader","model_invoked":True,"last_request":{
                "phase":"synthesis","state":"received","attempt":1,"limits":{
                    "total_seconds":None,"read_idle_seconds":None,"connect_seconds":10,"max_output_tokens":16384}}},
            evidence_snapshot=[], policy_snapshot={"question_analysis":{
                "source":"model_prior_knowledge_unverified","local_sources_loaded":0,"plan":{
                    "interpretation":"合成问题", "initial_assessment":assessment,"search_queries":["合成问题"],
                    "focus_terms":[],"decision_points":[],"missing_facts":[]}}},
            response=build_narrative_answer("合成问题","answer",{},[],narrative,rid) if state=="COMPLETED" else None))
        db.flush()
        db.add(m.Job(id=jid, run_id=rid, owner_id=env.owner, kind="ANSWER",
            state="SUCCEEDED" if state=="COMPLETED" else "RUNNING", stage="COMPLETED" if state=="COMPLETED" else "LOADING_WIKI_CATALOG",
            attempts=1, payload={"run_id":rid}, dedupe_key="http-reader:"+rid,
            lease_until=svc.now()+timedelta(minutes=5) if state=="RUNNING" else None))
    result = env.call("GET", "/runs/"+rid)
    assert result.status_code == 200, result.text
    assert result.json()["question_analysis"]["plan"]["initial_assessment"] == assessment
    assert result.json()["question_analysis"]["plan"]["focus_terms"] == []
    thread = env.call("GET", "/threads/"+tid)
    assert thread.status_code == 200, thread.text
    if state == "COMPLETED":
        assert result.json()["answer"]["narrative_markdown"] == narrative
        assert thread.json()["items"][0]["answer"]["narrative_markdown"] == narrative
    env.login(env.reader)
    hidden = env.call("GET", "/runs/"+rid)
    assert hidden.status_code == 404
    assert assessment not in hidden.text and narrative not in hidden.text

def test_http_rebinds_ranges_only_using_frozen_ids_without_mutating_stored_answer(http_env, monkeypatch):
    from test_reference_review import make_version
    from fund_kb.reference_evidence import reference_evidence
    from fund_kb.ingestion import text_sha256
    from fund_kb import providers
    env = http_env
    env.login()
    monkeypatch.setattr(providers, "complete", lambda *a, **k: pytest.fail("GET must never call a model"))
    vid = make_version(env, "document")
    with env.db.begin() as db:
        for ordinal in (1,2):
            text = f"第{ordinal}个中间原文片段"
            db.add(m.ContentBlock(version_id=vid, block_id=svc.uid(), ordinal=ordinal, block_type="paragraph",
                data={"text":text}, search_text=text, content_sha256=text_sha256(text), locator={"label":f"段落{ordinal}"}))
    rid, tid, jid = svc.uid(), svc.uid(), svc.uid()
    with env.db.begin() as db:
        rows = reference_evidence(db, db.get(m.User,env.owner), env.space, reading=True, version_ids={vid})
        for i,row in enumerate(rows,150): row["evidence_id"] = f"E{i}"
        assert len(rows) == 3
        answer = build_narrative_answer("合成问题","answer",{},rows,"核对全文。[E150]-[E152]",rid)
        answer["citations"] = [answer["citations"][0], answer["citations"][-1]]
        old_hash = svc.digest(answer)
        stamp = answer["generated_at"]
        db.add(m.ConsultationThread(id=tid, owner_id=env.owner, space_id=env.space, title="合成冻结编号"))
        db.flush()
        db.add(m.ConsultationRun(id=rid, thread_id=tid, state="COMPLETED", mode="answer",
            request={"question":"合成问题","mode":"answer","context":{},"answer_scope":"reference","reasoning_strategy":"model_first"},
            model_snapshot={"answer_engine":"wiki_reader","model_request_count":3}, response=answer,
            evidence_snapshot=[{k:r[k] for k in ("resource_id","version_id","block_id","content_sha256","reference_signature","evidence_id")} for r in rows]))
        db.flush()
        db.add(m.Job(id=jid, run_id=rid, owner_id=env.owner, kind="ANSWER", state="SUCCEEDED", stage="COMPLETED",
            attempts=1,payload={"run_id":rid},dedupe_key="frozen-http:"+rid))
    response = env.call("GET","/runs/"+rid)
    assert response.status_code == 200, response.text
    body=response.json()["answer"]
    assert [r["id"] for r in body["citations"]] == ["E150","E151","E152"]
    assert body["generated_at"] == stamp
    with env.db() as db:
        run=db.get(m.ConsultationRun,rid)
        assert svc.digest(run.response) == old_hash and run.model_snapshot["model_request_count"] == 3
    with env.db.begin() as db:
        db.get(m.Resource,db.get(m.ResourceVersion,vid).resource_id).suspended=True
    revoked=env.call("GET","/runs/"+rid)
    assert revoked.status_code==200 and revoked.json()["invalidated"] is True
    # Suspension is availability, not loss of the owner's historical read
    # permission. Preserve old text but do not create new current source links.
    assert [row["id"] for row in revoked.json()["answer"]["citations"]] == ["E150", "E152"]
    with env.db.begin() as db:
        db.get(m.User, env.owner).active = False
    denied=env.call("GET","/runs/"+rid)
    assert denied.status_code in {401,403} and "核对全文" not in denied.text
