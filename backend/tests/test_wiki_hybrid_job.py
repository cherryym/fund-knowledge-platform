"""Fusion-to-whole-page flow with real temporary Qdrant and stub generators."""
from types import SimpleNamespace
import re

from test_wiki_reader_job import env, base_env, prepare
from test_reference_review import make_version
from fund_kb import models as m, services as svc
from fund_kb.ingestion import text_sha256
from fund_kb.jobs import JobDispatcher
from fund_kb.retrieval import VectorIndex
from fund_kb.vector_indexing import queue_index
from fund_kb.wiki_reader import search_requests, requests_catalog


def test_hybrid_previews_verified_candidates_then_reads_the_full_source(env,monkeypatch):
    env.settings=env.settings.model_copy(update={"retrieval_mode":"hybrid","embedding_mode":"fastembed",
        "embedding_model":"synthetic-semantic","embedding_dimensions":8,
        "qdrant_path":env.settings.storage_dir/"synthetic-qdrant"})
    vid=make_version(env,"document")
    with env.db.begin() as db:
        version=db.get(m.ResourceVersion,vid)
        version.title="FOF被投基金估值完整说明"
        for ordinal in range(1,101):
            text=f"第{ordinal}段完整原文：被投基金净值应核对时点和来源。"
            db.add(m.ContentBlock(version_id=vid,block_id=svc.uid(),ordinal=ordinal,block_type="paragraph",
                data={"text":text},search_text=text,content_sha256=text_sha256(text),locator={"label":f"第{ordinal}段"}))
    vector=VectorIndex(env.settings)
    # Toy vectors validate the dataflow, not Chinese semantic accuracy.
    monkeypatch.setattr(vector.embedding,"embed",lambda texts,**kwargs:[
        [1.,0.,0.,0.,0.,0.,0.,0.] if any(term in text for term in ("基金","FOF"))
        else [0.,1.,0.,0.,0.,0.,0.,0.] for text in texts])
    dispatcher=JobDispatcher(env.settings,env.db,vector)
    try:
        with env.db.begin() as db:
            ctx=SimpleNamespace(db=db,user=db.get(m.User,env.owner),settings=env.settings,
                data={"space_id":env.space},dispatch=[],request=SimpleNamespace(state=SimpleNamespace(trace_id="synthetic-hybrid-index")))
            job=queue_index(ctx); index_id=job.id
        dispatcher.run(index_id)
        with env.db() as db:
            assert db.get(m.Job,index_id).state == "SUCCEEDED",db.get(m.Job,index_id).error_code
        def responder(calls,_):
            if len(calls)==1:
                assert "第100段" not in calls[0]
                return "先核对所持其他基金的类型和净值时点。"
            if len(calls)==2:
                assert "检索发现的候选页" in calls[-1]
                assert "检索片段（非已核验全文，仅供选读）" in calls[-1]
                # Selection now receives a verified hit, not a title-only map.
                # This is not all 100 paragraphs or final E-numbered evidence.
                excerpts=set(re.findall(r"第(\d+)段完整原文",calls[-1]))
                assert 0 < len(excerpts) < 100
                assert "[E1]" not in calls[-1]
                target=re.search(r"(W\d+) \| 来源文档 \| FOF被投基金估值完整说明",calls[-1])
                assert target
                return "READ "+target[1]
            assert "第1段完整原文" in calls[-1] and "第100段完整原文" in calls[-1]
            return "## 处理说明\n应核对净值时点及来源。[E1]"
        rid,jid,calls=prepare(env,monkeypatch,responder)
        dispatcher._answer(jid,1)
        with env.db() as db:
            run=db.get(m.ConsultationRun,rid)
            assert run.state == "COMPLETED" and len(calls)==3
            trace=run.model_snapshot["hybrid_retrieval"]
            assert trace["strategy"] == "wiki_rag_rrf"
            assert trace["candidate_snippets_loaded_for_discovery"] > 0
            assert trace["source_blocks_checked_for_discovery"] >= trace["candidate_snippets_loaded_for_discovery"]
            assert run.policy_snapshot["question_analysis"]["local_sources_loaded"] == 0
            assert trace["queries"][0]["mode"] == "hybrid"
            assert run.model_snapshot["wiki_reading"]["loaded_blocks"] >= 101
            assert run.response["review_status"] == "REQUIRES_EXPERT"
    finally:
        dispatcher.close();vector.close()


def test_search_directives_are_local_optional_and_ignore_examples():
    assert search_requests('SEARCH 停牌股票\nSEARCH: 基金估值\nSEARCH 停牌股票') == ["停牌股票","基金估值"]
    assert search_requests('普通说明\n```text\nSEARCH 不应执行\n```') == []
    assert requests_catalog("CATALOG")
    assert not requests_catalog("```\nCATALOG\n```")
