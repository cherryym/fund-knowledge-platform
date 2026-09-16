"""Real worker/cache/source checks; synthetic DB/providers, zero paid calls."""
import copy
import re

import pytest
from test_adaptive_query_job import run
from test_reference_security_review import queued_run
from test_wiki import page
from test_wiki_reader_job import base_env, prepare  # noqa: F401
from test_wiki_reader_job import env as env  # noqa: PLC0414

from fund_kb import hybrid_retrieval, providers
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.jobs import JobError
from fund_kb.planning_cache import planning_cache
from fund_kb.query_path_cache import clear_query_path_cache


@pytest.fixture(autouse=True)
def reset_cache():
    planning_cache.clear(); clear_query_path_cache()
    yield
    planning_cache.clear(); clear_query_path_cache()


def setup(env, monkeypatch):
    source = page(env, "任意业务输入标准", "SOURCE_MARKER：请核对输入、条件与后续记录。", kind="document")
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "universal"})
    searches = []
    def search(db, user, space, query, *, pages, **kwargs):
        searches.append(query)
        p = next((p for p in pages.values() if p["version_id"] == source[1]), None)
        units, hits = [], []
        if p:
            block = db.get(m.ContentBlock, (source[1], source[2]))
            u = {"unit_id": "unit" + source[1], "page_id": p["id"], "resource_id": p["resource_id"],
                "version_id": p["version_id"], "kind": p["kind"], "text": block.search_text,
                "block_ids": [source[2]], "section_path": [], "score": .05, "rerank_score": 3., "channels": ["vector", "bm25"]}
            units = [u]
            hits = [{**{k: u[k] for k in ("page_id", "resource_id", "version_id", "score", "channels")},
                "matched_block_ids": [source[2]], "candidate_snippets": [u]}]
        return {"query": query, "mode": "hybrid_unit_rerank", "units": units, "hits": hits,
            "catalog_pages": len(pages), "indexed_catalog_pages": len(pages), "total_candidates": len(hits),
            "returned": len(hits), "warnings": [], "timing_ms": .1, "candidate_preview_stats": {}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    def respond(calls, connection):
        text = calls[-1]
        if text.startswith("先基于用户问题"):
            assert "SOURCE_MARKER" not in text
            return "ISSUE 核对业务输入和后续记录\nSEARCH 任意业务输入"
        assert "SOURCE_MARKER" in text
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", text)))
        return "## 处理说明\n\n核对输入和后续记录，保留未核验条件。" + "".join(f"[{eid}]" for eid in ids)
    rid, jid, calls = prepare(env, monkeypatch, respond)
    with env.db.begin() as db:
        row = db.get(m.ConsultationRun, rid)
        row.request = {**row.request, "question": "任意业务输入如何核对？"}
        request = copy.deepcopy(row.request)
    return source, rid, jid, calls, searches, request


def clone(env, request):
    rid, jid = queued_run(env, selection=request["model_selection"], question=request["question"])
    with env.db.begin() as db:
        db.get(m.ConsultationRun, rid).request = copy.deepcopy(request)
    return rid, jid


def test_repeat_reuses_only_plan_and_route_but_rereads_all_sources_and_regenerates(env, monkeypatch):
    _source, rid, jid, calls, searches, request = setup(env, monkeypatch)
    run(env, jid)
    with env.db() as db:
        first = db.get(m.ConsultationRun, rid)
        assert first.state == "COMPLETED" and len(calls) == 2
        first_evidence = copy.deepcopy(first.evidence_snapshot)
        assert first.policy_snapshot.get("planning_cache_binding")
    rid2, jid2 = clone(env, request)
    search_count = len(searches)
    run(env, jid2)
    with env.db() as db:
        second = db.get(m.ConsultationRun, rid2)
        assert second.state == "COMPLETED" and len(calls) == 3
        assert second.evidence_snapshot == first_evidence
        assert second.model_snapshot["planning_cache"]["hit"] is True
        assert second.model_snapshot["planning_cache"]["source_run_id"] == rid
        assert second.model_snapshot["planning_model_invoked"] is False
        assert second.model_snapshot["answer_model_invoked"] is True
        assert second.model_snapshot["model_request_count"] == 1
        assert second.model_snapshot["query_path"]["separate_planning_model_calls"] == 0
        assert second.model_snapshot["query_path"]["cache_hit"] is True
        assert len(searches) == search_count
        assert "SOURCE_MARKER" in calls[-1]
        assert second.response["run_id"] == rid2
        assert not second.policy_snapshot.get("planning_cache_binding")  # no TTL chaining


@pytest.mark.parametrize("change", ["deleted_thread", "invalidated", "failed_origin", "changed_plan", "source_epoch", "new_policy"])
def test_stale_origin_does_not_reuse_public_plan(env, monkeypatch, change):
    source, rid, jid, calls, _searches, request = setup(env, monkeypatch)
    run(env, jid)
    with env.db.begin() as db:
        origin = db.get(m.ConsultationRun, rid)
        if change == "deleted_thread": db.get(m.ConsultationThread, origin.thread_id).deleted_at = svc.now()
        elif change == "invalidated": origin.invalidated_at = svc.now()
        elif change == "failed_origin": origin.state = "FAILED"
        elif change == "changed_plan":
            analysis = copy.deepcopy(origin.policy_snapshot)
            analysis["question_analysis"]["plan"]["initial_assessment"] = "modified"
            origin.policy_snapshot = analysis
        elif change == "source_epoch": db.get(m.Resource, source[0]).access_epoch += 1
        elif change == "new_policy":
            analysis = copy.deepcopy(origin.policy_snapshot)
            analysis["planning_cache_binding"]["source_reading_policy_stamp"] = "stale"
            origin.policy_snapshot = analysis
    rid2, jid2 = clone(env, request)
    run(env, jid2)
    with env.db() as db:
        second = db.get(m.ConsultationRun, rid2)
        assert second.state == "COMPLETED" and len(calls) == 4
        assert second.model_snapshot["planning_cache"]["hit"] is False
        assert second.model_snapshot["planning_model_invoked"] is True


def test_fresh_connection_disable_prevents_cache_and_any_model_use(env, monkeypatch):
    _source, _rid, jid, calls, _searches, request = setup(env, monkeypatch)
    run(env, jid)
    _rid2, jid2 = clone(env, request)
    with env.db.begin() as db:
        policy = db.get(m.RuntimePolicy, request["model_selection"]["connection_id"])
        policy.config = {**policy.config, "enabled": False}
    with pytest.raises((JobError, svc.APIError)):
        run(env, jid2)
    assert len(calls) == 2


def test_failed_synthesis_cannot_seed_a_plan(env, monkeypatch):
    _source, rid, jid, calls, _searches, request = setup(env, monkeypatch)
    normal = providers.complete
    def fail(connection, messages, **kwargs):
        if "SOURCE_MARKER" in messages[1]["content"]:
            raise providers.ProviderError("PROVIDER_RESPONSE_FAILED")
        return normal(connection, messages, **kwargs)
    monkeypatch.setattr(providers, "complete", fail)
    with pytest.raises(JobError): run(env, jid)
    with env.db.begin() as db:
        db.get(m.ConsultationRun, rid).state = "FAILED"
        db.get(m.Job, jid).state = "FAILED"
    monkeypatch.setattr(providers, "complete", normal)
    rid2, jid2 = clone(env, request)
    run(env, jid2)
    with env.db() as db:
        assert db.get(m.ConsultationRun, rid2).model_snapshot["planning_cache"]["hit"] is False
    assert len(calls) == 3
