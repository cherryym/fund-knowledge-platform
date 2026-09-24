"""Synthetic end-to-end generation and permission-sensitive read projections."""
import copy
import re

from test_context_completion_job import document, setup
from test_reference_security_review import view
from test_wiki_reader_job import base_env  # noqa: F401
from test_wiki_reader_job import env as env  # noqa: PLC0414

from fund_kb import models as m


def test_pre_generation_constraints_and_post_generation_findings_do_not_add_model_calls(env, monkeypatch):
    source,ids=document(env,"合成办理与会计边界",["第一条", "第二办理日", "以乙模式办理的，当日为核对日。",
        "结转余额。", "贷：科目甲", "贷：科目乙", "下一事项。"])
    supplied=[]
    def respond(calls,_):
        if len(calls)==1:
            return "ISSUE 核对办理日期与分录\nSEARCH 办理边界"
        assert "程序来源核对数据" in calls[-1]
        assert "SOURCE_JOURNAL_SIDE_ANOMALY" in calls[-1]
        assert '"day_number":2' in calls[-1]
        eids=list(dict.fromkeys(re.findall(r'\[(E\d+)\]',calls[-1])))
        text="第一办理日：乙模式下当日完成核对"+''.join('['+e+']' for e in eids)+"。\n\n```\n贷：科目甲\n贷：科目乙\n```"
        supplied.append(text)
        return text
    rid,jid,calls,_,worker=setup(env,monkeypatch,source,ids[2],respond)
    try:
        worker._answer(jid,1)
    finally:
        worker.close()
    with env.db() as db:
        run=db.get(m.ConsultationRun,rid)
        stored=copy.deepcopy(run.response)
        snapshot=copy.deepcopy(run.model_snapshot)
        assert run.state=='COMPLETED' and len(calls)==2
        assert run.response['narrative_markdown']==supplied[0]
        assert run.response['review_status']=='REQUIRES_EXPERT'
        assert {'ANSWER_EVENT_DAY_CONFLICT','ANSWER_JOURNAL_SIDE_ANOMALY'} <= {w['code'] for w in run.response['quality_warnings']}
    visible=view(env,rid)
    assert visible['model_snapshot']['evidence_review']['critical_count']>=2
    assert visible['model_snapshot']['evidence_review']['observation_origin']=='current_authorized_read_projection'
    with env.db() as db:
        assert db.get(m.ConsultationRun,rid).response==stored
        assert db.get(m.ConsultationRun,rid).model_snapshot==snapshot
    with env.db.begin() as db:
        db.get(m.Resource,source[0]).suspended=True
    hidden=view(env,rid)
    assert hidden['invalidated']
    assert 'evidence_review' not in hidden['model_snapshot']


def test_unambiguous_good_source_does_not_get_an_automatic_business_pass(env, monkeypatch):
    source,ids=document(env,"合成正常办理",["第一条", "第二办理日", "当日为核对日。"])
    def respond(calls,_):
        if len(calls)==1:
            return "SEARCH 合成正常办理"
        eids=list(dict.fromkeys(re.findall(r'\[(E\d+)\]',calls[-1])))
        return "第二办理日完成核对"+''.join('['+e+']' for e in eids)+"。"
    rid,jid,calls,_,worker=setup(env,monkeypatch,source,ids[2],respond)
    try:
        worker._answer(jid,1)
    finally:
        worker.close()
    answer=view(env,rid)
    assert len(calls)==2 and answer['state']=='COMPLETED'
    assert answer['model_snapshot']['evidence_review']['status']=='NO_STRUCTURAL_FINDING'
    assert answer['model_snapshot']['evidence_review']['semantic_entailment']=='NOT_EVALUATED'
    assert answer['answer']['review_status']=='REQUIRES_EXPERT'
