"""Primary reading is a curated source constraint, not a citation-name rewrite."""
import copy
import re

import pytest
from sqlalchemy import select
from test_reference_review import make_version
from test_wiki_reader_job import base_env, execute, prepare  # noqa: F401
from test_wiki_reader_job import env as env  # noqa: PLC0414

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.ingestion import text_sha256
from fund_kb.jobs import JobError
from fund_kb.source_reading_policy import PREFIX, build_reading_plan, check_primary_citations
from fund_kb.wiki_catalog import build_catalog


def configured_source(env):
    vid=make_version(env,"document")
    with env.db.begin() as db:
        source=db.get(m.ResourceVersion,vid)
        source.title="指定估值处理标准"
        block=db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id==vid))
        body="第十二条 含投资者回售权品种的回售期间，应核对第三方估值全价。PRIMARY_CLAUSE"
        block.data={"text":body};block.search_text=body;block.content_sha256=text_sha256(body)
        rid,bid=source.resource_id,block.block_id
        policy=m.RuntimePolicy(id=svc.uid(),name=PREFIX+env.space,updated_by=env.owner,config={
            "schema_version":1,"space_id":env.space,"rules":[{
                "id":"synthetic-put-valuation","query_term_groups":[["回售"],["价格","估值"]],
                "not_before":"2023-03-31","resource_ids":[rid],"source_topic_terms":["回售"],"source_context_terms":[]}]})
        db.add(policy);policy_id=policy.id
    return rid,vid,bid,policy_id


def test_omitted_primary_source_is_read_before_synthesis_not_renamed_afterwards(env,monkeypatch):
    _rid,vid,bid,_=configured_source(env)
    source_name=env.source
    def responder(calls,_):
        if len(calls)==1:return "区分估值与会计结转，需要核对估值条款。"
        if len(calls)==2:
            # Deliberately choose only another source. The server must still
            # include the curated standard before asking for business synthesis.
            return "READ "+re.search(r"(W\d+) \| 来源文档 \| (?!指定估值处理标准)",calls[-1])[1]
        assert "PRIMARY_CLAUSE" in calls[-1]
        assert "不能以分录" in calls[-1]
        evidence=re.search(r"\[(E\d+)\]\n第十二条.*PRIMARY_CLAUSE",calls[-1])[1]
        return f"应区分估值价格与会计结转，核对标准直接条款。[{evidence}]"
    run_id,jid,_=prepare(env,monkeypatch,responder)
    with env.db.begin() as db:
        row=db.get(m.ConsultationRun,run_id)
        row.request={**row.request,"question":"债券回售价格应该如何处理？"}
    execute(env,jid)
    with env.db() as db:
        row=db.get(m.ConsultationRun,run_id)
        assert row.state=="COMPLETED"
        assert row.model_snapshot["primary_source_coverage"]["covered"] is True
        assert any(e["version_id"]==vid and e["block_id"]==bid for e in row.response["citations"])
        assert db.get(m.ResourceVersion,vid).legal_status=="UNKNOWN"
        assert source_name==env.source


def test_unavailable_primary_source_does_not_leak_its_identity_or_bypass_acl(env):
    rid,vid,_,_=configured_source(env)
    with env.db.begin() as db:db.get(m.Resource,rid).suspended=True
    with env.db() as db:
        user=db.get(m.User,env.owner);pages=build_catalog(db,user,env.space,scope="reference")
        plan=build_reading_plan(db,env.space,"债券回售价格",{},pages)
        assert plan["sources"]==[] and plan["warnings"]==["REQUIRED_SOURCE_NOT_ADMITTED"]
        assert rid not in str(plan) and vid not in str(plan) and "指定估值处理标准" not in str(plan)


def test_historical_and_accounting_only_questions_do_not_activate_current_price_rule(env):
    configured_source(env)
    with env.db() as db:
        pages=build_catalog(db,db.get(m.User,env.owner),env.space,scope="reference")
        assert build_reading_plan(db,env.space,"债券回售会计分录",{},pages)["sources"]==[]
        assert build_reading_plan(db,env.space,"债券回售价格",{"business_date":"2021-01-01"},pages)["sources"]==[]


def test_citation_coverage_requires_the_actual_topic_anchor_and_never_rewrites_sources():
    plan={"matched_rules":["put"],"warnings":[],"sources":[{"resource_id":"standard","version_id":"v",
        "topic_block_ids":["clause"],"anchor_block_ids":["clause","preamble"]}]}
    records=[{"version_id":"v","block_id":"clause"},{"version_id":"v","block_id":"preamble"}]
    answer={"citations":[{"version_id":"manual","block_id":"sample"}],"quality_warnings":[]}
    original=copy.deepcopy(answer["citations"])
    result=check_primary_citations(plan,records,answer)
    assert result["covered"] is False and answer["citations"]==original
    assert answer["quality_warnings"][0]["code"]=="PRIMARY_RULE_CITATION_MISSING"
    answer={"citations":[{"version_id":"v","block_id":"preamble"}],"quality_warnings":[]}
    assert check_primary_citations(plan,records,answer)["covered"] is False
    answer={"citations":[{"version_id":"v","block_id":"clause"}],"quality_warnings":[]}
    assert check_primary_citations(plan,records,answer)["covered"] is True


def test_changed_primary_policy_during_generation_prevents_stale_delivery(env,monkeypatch):
    _,_,_,policy_id=configured_source(env)
    def responder(calls,_):
        if len(calls)==1:return "先核对估值标准。"
        if len(calls)==2:return "READ "+re.search(r"(W\d+) \| 来源文档 \| 指定估值处理标准",calls[-1])[1]
        with env.db.begin() as db:
            policy=db.get(m.RuntimePolicy,policy_id)
            policy.revision+=1
        return "一段待核对的答复。"
    run_id,jid,_=prepare(env,monkeypatch,responder)
    with env.db.begin() as db:
        row=db.get(m.ConsultationRun,run_id);row.request={**row.request,"question":"债券回售价格"}
    with pytest.raises(JobError,match="SOURCE_READING_POLICY_CHANGED"):execute(env,jid)
    with env.db() as db:assert db.get(m.ConsultationRun,run_id).response is None
