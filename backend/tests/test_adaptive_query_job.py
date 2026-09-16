"""One-call graph-grounded worker, exact sources and route-cache isolation."""
import copy

import pytest
from sqlalchemy import select
from test_wiki import page
from test_wiki_reader_job import base_env, prepare  # noqa: F401
from test_wiki_reader_job import env as env  # noqa: PLC0414

from fund_kb import models as m
from fund_kb import providers
from fund_kb import services as svc
from fund_kb.jobs import JobDispatcher
from fund_kb.query_context import compact_evidence
from fund_kb.query_path_cache import clear_query_path_cache, get_query_path, put_query_path
from fund_kb.wiki_reader import ADAPTIVE_PROMPT_VERSION, ADAPTIVE_SYSTEM


@pytest.fixture(autouse=True)
def clean_routes():
    clear_query_path_cache()
    yield
    clear_query_path_cache()


def cooperative_fixture(env):
    exchange = page(env, "国债期货交割规则", "合成交割安排：需要核对交付时点。EXCHANGE_SOURCE", kind="document")
    manual = page(env, "基金会计实务依据", "合成估值口径：先核对持仓阶段及价格来源。ACCOUNTING_SOURCE", kind="document")
    condition = page(env, "国债期货收券前提", "须核对是否实际收券。CONDITION_WIKI", cites=[exchange])
    knowledge = page(env, "国债期货交割估值", "国债期货交割估值需区分[[国债期货收券前提]]，结合核算依据。VALUATION_WIKI", cites=[exchange, manual])
    with env.db.begin() as db:
        db.add(m.RelationEdge(id=svc.uid(), source_version_id=knowledge[1], target_resource_id=condition[0],
            relation_type="APPLIES_TO", conditions={}))
        db.flush()
        v = db.get(m.ResourceVersion, knowledge[1])
        v.content_sha256 = svc.check_frozen_hash(db, v)
    return exchange, manual, condition, knowledge


def setup_run(env, monkeypatch, responder):
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "adaptive"})
    rid, jid, calls = prepare(env, monkeypatch, responder)
    with env.db.begin() as db:
        row = db.get(m.ConsultationRun, rid)
        row.request = {**row.request, "question": "国债期货交割估值如何处理？"}
    return rid, jid, calls


def run(env, jid):
    worker = JobDispatcher(env.settings, env.db, None)
    try:
        worker._answer(jid, 1)
    finally:
        worker.close()


def fake_retrieval(monkeypatch, exchange, counter):
    from fund_kb import hybrid_retrieval
    def search(db, user, space, question, *, pages, **kwargs):
        counter.append(question)
        source = next((p for p in pages.values() if p["version_id"] == exchange[1]), None)
        hits = [] if not source else [{"page_id": source["id"], "version_id": source["version_id"],
            "resource_id": source["resource_id"], "title": source["title"], "kind": "document",
            "score": .05, "channels": ["vector", "bm25"], "matched_block_ids": [exchange[2]],
            "candidate_snippets": [{"text": "国债期货交割规则与估值", "block_ids": [exchange[2]]}]}]
        return {"query": question, "mode": "hybrid", "hits": hits, "warnings": [], "catalog_pages": len(pages),
            "indexed_catalog_pages": len(pages), "total_candidates": len(hits), "returned": len(hits),
            "timing_ms": 1.0, "candidate_preview_stats": {"verified_snippets": len(hits), "source_blocks_checked": len(hits)}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)


def test_one_call_reads_vector_wiki_graph_and_both_original_sources(env, monkeypatch):
    exchange, manual, condition, knowledge = cooperative_fixture(env)
    searches = []
    fake_retrieval(monkeypatch, exchange, searches)
    def respond(calls, _):
        assert len(calls) == 1
        assert all(t in calls[0] for t in ["EXCHANGE_SOURCE", "ACCOUNTING_SOURCE", "CONDITION_WIKI", "VALUATION_WIKI"])
        assert "CITES" in calls[0] and "APPLIES_TO" in calls[0]
        return "应核对交付阶段及估值依据，以下仅作合成测试说明。[E1]"
    rid, jid, calls = setup_run(env, monkeypatch, respond)
    original = providers.complete
    def capture(connection, messages, **kwargs):
        assert messages[0]["content"] == ADAPTIVE_SYSTEM
        return original(connection, messages, **kwargs)
    monkeypatch.setattr(providers, "complete", capture)
    run(env, jid)
    with env.db() as db:
        r = db.get(m.ConsultationRun, rid)
        assert r.state == "COMPLETED" and len(calls) == 1 and len(searches) == 1
        assert r.model_snapshot["prompt_version"] == ADAPTIVE_PROMPT_VERSION
        assert r.model_snapshot["planning_model_invoked"] is False
        assert not r.policy_snapshot["question_analysis"]
        path = r.model_snapshot["query_path"]
        assert path["model_requests"] == 1 and path["selection_model_calls"] == 0
        assert path["loaded_wiki_pages"] >= 2 and path["loaded_source_pages"] >= 2
        assert path["request_fits_single_synthesis"] is True
        assert path["within_target"] is True  # synthetic callback, not a live SLA
        cited_versions = set(db.scalars(select(m.RunEvidence.version_id).where(m.RunEvidence.run_id == rid)))
        assert {exchange[1], manual[1], condition[1], knowledge[1]} <= cited_versions


def test_repeated_question_reuses_identifiers_but_rereads_all_current_sources(env, monkeypatch):
    exchange, manual, *_ = cooperative_fixture(env)
    searches = []
    fake_retrieval(monkeypatch, exchange, searches)
    def respond(calls, _):
        assert "EXCHANGE_SOURCE" in calls[0] and "ACCOUNTING_SOURCE" in calls[0]
        return "合成参考答复。[E1]"
    first, jid, _ = setup_run(env, monkeypatch, respond)
    run(env, jid)
    second, next_job, _ = setup_run(env, monkeypatch, respond)
    run(env, next_job)
    with env.db() as db:
        assert db.get(m.ConsultationRun, first).model_snapshot["query_path"]["cache_hit"] is False
        assert db.get(m.ConsultationRun, second).model_snapshot["query_path"]["cache_hit"] is True
        assert len(searches) == 1
        evidence = set(db.scalars(select(m.RunEvidence.version_id).where(m.RunEvidence.run_id == second)))
        assert exchange[1] in evidence and manual[1] in evidence


def test_cache_only_retains_routes_not_candidate_text_or_answers(monkeypatch):
    import fund_kb.query_path_cache as cache
    now = [10.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: now[0])
    value = {"requested": ["W1"], "anchors": {"W1": ["block"]}, "stats": {"sources": 1},
        "candidate_text": "NOT_A_CACHE_FIELD", "answer": "NOT_AN_ANSWER_CACHE"}
    original = copy.deepcopy(value)
    assert put_query_path("scope-a", value)
    assert get_query_path("scope-b") is None
    found = get_query_path("scope-a")
    assert "candidate_text" not in found and "answer" not in found
    found["requested"].append("W2")
    assert get_query_path("scope-a")["requested"] == ["W1"] and value == original
    now[0] += 301
    assert get_query_path("scope-a") is None


def test_compact_pack_keeps_every_original_character_and_evidence_identifier():
    records = [{"block_id": "b1", "evidence_id": "E1", "text": "  第一段\n不丢空白。"},
        {"block_id": "b2", "evidence_id": "E2", "text": "第二段：前提与例外。"}]
    pages = {"W1": {"title": "完整小节", "kind": "document", "state": "APPROVED", "legal_status": "UNKNOWN",
        "records": records, "block_count": 100, "full_text_loaded": False}}
    text = compact_evidence(pages, ["W1"])
    assert all(r["text"] in text and f"[{r['evidence_id']}]" in text for r in records)
    assert "2/100" in text and "READ_SECTION/READ_FULL" in text
