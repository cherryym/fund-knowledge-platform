"""Unverified Wiki is readable/editable, source-bound, and never formal evidence."""
import copy
import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_wiki import env as env  # noqa: PLC0414
from test_wiki import execute, page
from test_wiki import provider as provider  # noqa: PLC0414

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb import wiki
from fund_kb.ingestion import block_text, text_sha256


def draft_source(env):
    result=page(env,'待核验估值源',kind='document',state='DRAFT',text='估值时需要核对价格来源、计量日期和输入参数，保留复核记录。')
    with env.db.begin() as db:
        v=db.get(m.ResourceVersion,result[1])
        v.source_verified=False
        # Actual uploaded/edited drafts have no review-time frozen hash yet.
        v.content_sha256=None
    return result


def build_draft(env,source):
    response=env.call('POST','/wiki/builds',{'space_id':env.space,'source_resource_ids':[source[0]],
        'source_mode':'unverified_draft','model_selection':{'connection_id':env.connection,'model_id':'synthetic-test-model'},
        'max_pages':3,'consent':True})
    assert response.status_code==202,response.text
    jid=response.json()['id']
    with env.db.begin() as db:
        job=db.get(m.Job,jid);job.state='RUNNING';job.attempts=1;job.lease_until=svc.now()+timedelta(minutes=2)
    result=execute(env,jid)
    with env.db.begin() as db:
        job=db.get(m.Job,jid);job.state='SUCCEEDED';job.result=result
    return jid,result


def test_draft_build_links_graph_edit_and_formal_guards(env,provider):
    source=draft_source(env)
    # Default path remains strict.
    denied=env.call('POST','/wiki/builds',{'space_id':env.space,'source_resource_ids':[source[0]],
        'model_selection':{'connection_id':env.connection,'model_id':'synthetic-test-model'},'consent':True})
    assert denied.status_code==409 and denied.json()['code']=='WIKI_SOURCE_NOT_READY'
    jid,result=build_draft(env,source)
    assert result['source_mode']=='unverified_draft' and not result['formal_evidence_allowed']
    rid,vid=result['created_resource_ids'][0],result['created_version_ids'][0]
    workspace=env.call('GET',f'/wiki/workspace?space_id={env.space}').json()
    assert workspace['pages'][0]['source_mode']=='unverified_draft'
    graph=env.call('GET',f'/wiki/graph?space_id={env.space}').json()
    assert {n['id'] for n in graph['nodes']}=={source[0],rid}
    assert any(e['source']==rid and e['target']==source[0] and e['type']=='CITES' for e in graph['edges'])
    links=env.call('GET',f'/wiki/pages/{rid}/links').json()
    assert links['sources'][0]['id']==source[0]
    current=env.call('GET',f'/versions/{vid}')
    body={k:current.json()[k] for k in ('title','knowledge_type','applicability','required_facts','legal_status','valid_from','valid_to','blocks')}
    body['blocks'][1]['data']['text']+=' 本人补充的待核对事项。'
    edited=env.call('PATCH',f'/versions/{vid}',body,etag=current.headers['etag'])
    assert edited.status_code==200,edited.text
    blocked=env.call('POST',f'/versions/{vid}/submit',etag=edited.headers['etag'])
    assert blocked.status_code==409 and blocked.json()['code']=='WIKI_UNVERIFIED_SOURCES'
    with env.db() as db:
        assert svc.eligible_evidence(db,db.get(m.User,env.owner),env.space)==[]
        assert db.get(m.ResourceVersion,source[1]).state=='DRAFT'
        assert not db.get(m.ResourceVersion,source[1]).source_verified
    assert env.call('GET',f'/jobs/{jid}').status_code==200


@pytest.mark.parametrize('change',['body','delete','suspend','epoch','blob','permissions'])
def test_source_changes_hide_draft_and_job_results_on_all_read_surfaces(env,provider,change):
    source=draft_source(env);jid,result=build_draft(env,source)
    rid,vid=result['created_resource_ids'][0],result['created_version_ids'][0]
    with env.db.begin() as db:
        r=db.get(m.Resource,source[0]);v=db.get(m.ResourceVersion,source[1])
        if change=='delete':r.deleted_at=svc.now()
        if change=='suspend':r.suspended=True
        if change=='epoch':r.access_epoch+=1
        if change=='permissions':r.restricted=True
        if change=='blob':db.get(m.Blob,v.source_blob_id).scan_state='QUARANTINED'
        if change=='body':
            b=db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id==v.id))
            b.data={'text':'完全不同的来源内容'};b.search_text=block_text({'block_type':b.block_type,'data':b.data});b.content_sha256=text_sha256(b.search_text)
            v.revision+=1;db.flush();v.content_sha256=svc.check_frozen_hash(db,v)
    for path in (f'/resources/{rid}',f'/versions/{vid}',f'/wiki/pages/{rid}/links',f'/jobs/{jid}'):
        assert env.call('GET',path).status_code in {403,404,409},path
    assert rid not in env.call('GET',f'/wiki/graph?space_id={env.space}').text


def test_removing_labels_or_citations_never_removes_frozen_provenance(env,provider):
    source=draft_source(env);_,result=build_draft(env,source);rid=result['created_resource_ids'][0];vid=result['created_version_ids'][0]
    resource=env.call('GET',f'/resources/{rid}')
    assert env.call('PATCH',f'/resources/{rid}',{'tags':[]},etag=resource.headers['etag']).status_code==200
    v=env.call('GET',f'/versions/{vid}')
    body={k:copy.deepcopy(v.json()[k]) for k in ('title','knowledge_type','applicability','required_facts','legal_status','valid_from','valid_to','blocks')}
    for b in body['blocks']:b['citations']=[];b['locator']={}
    result=env.call('PATCH',f'/versions/{vid}',body,etag=v.headers['etag'])
    assert result.status_code==200,result.text
    assert env.call('POST',f'/versions/{vid}/submit',etag=result.headers['etag']).status_code==409
    assert env.call('GET',f'/wiki/workspace?space_id={env.space}').json()['pages'][0]['source_mode']=='unverified_draft'
    env.login(env.reader)
    assert env.call('GET',f'/resources/{rid}').status_code==404


def test_mid_generation_source_change_cannot_commit_draft(env,provider):
    source=draft_source(env)
    def mutate():
        with env.db.begin() as db:db.get(m.Resource,source[0]).access_epoch+=1
    provider.after_call=mutate
    with pytest.raises(wiki.WikiBuildError,match='WIKI_SOURCE_CHANGED'):
        build_draft(env,source)
    with env.db() as db:
        assert not list(db.scalars(select(m.Resource).where(m.Resource.kind=='knowledge')))


def test_explicit_topic_and_block_scope_are_frozen_and_bounded(env, provider):
    first=draft_source(env)
    second=page(env,'另一条估值源',kind='document',state='DRAFT',text='第二资料的内容不应发送给本次模型。')
    request={'space_id':env.space,'source_resource_ids':[first[0],second[0]],
        'source_mode':'unverified_draft','source_block_ids':[first[2]],
        'generation_brief':'仅按核心指引构建估值治理知识，不生成第二资料主题。',
        'model_selection':{'connection_id':env.connection,'model_id':'synthetic-test-model'},'consent':True}
    response=env.call('POST','/wiki/builds',request)
    assert response.status_code==202,response.text
    jid=response.json()['id']
    with env.db.begin() as db:
        job=db.get(m.Job,jid)
        assert job.payload['generation_brief']==request['generation_brief']
        job.state='RUNNING';job.attempts=1;job.lease_until=svc.now()+timedelta(minutes=2)
    result=execute(env,jid)
    assert {s['block_id'] for s in provider.requests[-1]['sources']}=={first[2]}
    assert '第二资料的内容' not in json.dumps(provider.requests[-1],ensure_ascii=False)
    assert result['coverage']['explicit_block_scope']
    assert result['coverage']['corpus_source_blocks']==2
    assert result['coverage']['total_source_blocks']==1
    assert wiki._ledger_name(env.space,env.owner,'unverified_draft','治理') != wiki._ledger_name(env.space,env.owner,'unverified_draft','价格')
    request['source_resource_ids']=[second[0]]
    rejected=env.call('POST','/wiki/builds',request)
    assert rejected.status_code==422 and rejected.json()['code']=='WIKI_BLOCK_NOT_IN_SOURCES'


def test_coherent_pdf_passage_retains_every_original_evidence_link(env, provider):
    source=draft_source(env)
    second_id=svc.uid()
    with env.db.begin() as db:
        first=db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id==source[1]))
        first.locator={'kind':'pdf','source_page':1}
        text='没有可观察输入值时，需核对不可观察输入值的适用条件。'
        db.add(m.ContentBlock(version_id=source[1],block_id=second_id,ordinal=1,block_type='paragraph',
            data={'text':text},locator={'kind':'pdf','source_page':1},search_text=text,content_sha256=text_sha256(text)))
    _,result=build_draft(env,source)
    sent=provider.requests[-1]['sources']
    assert len(sent)==1 and sent[0]['source_block_count']==2
    assert '不可观察输入值' in sent[0]['excerpt']
    with env.db() as db:
        links=db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id==result['created_version_ids'][0])).all()
        assert {link.to_block_id for link in links}=={source[2],second_id}
    assert result['coverage']['cited_blocks']==2
    assert result['coverage']['cited_fragments']==1
