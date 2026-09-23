"""Whole worker with synthetic DB, captured providers and a local rerank stub."""
import re
from types import SimpleNamespace

import pytest

from fund_kb import hybrid_retrieval, models as m, services as svc
from fund_kb.ingestion import text_sha256
from fund_kb.jobs import JobDispatcher, JobError
from test_wiki import page
from test_wiki_reader_job import base_env, prepare  # noqa: F401
from test_wiki_reader_job import env as env  # noqa: PLC0414


def document(env, title, texts):
    result = page(env, title, texts[0], kind="document")
    ids = [result[2]]
    with env.db.begin() as db:
        for i, text in enumerate(texts[1:], 1):
            bid = svc.uid()
            ids.append(bid)
            db.add(m.ContentBlock(version_id=result[1], block_id=bid, ordinal=i,
                block_type="paragraph", data={"text": text}, search_text=text,
                content_sha256=text_sha256(text), locator={"label": f"合成第{i}段"}))
        db.flush()
        db.get(m.ResourceVersion, result[1]).content_sha256 = svc.check_frozen_hash(db, db.get(m.ResourceVersion, result[1]))
    return result, ids


def setup(env, monkeypatch, source, anchor, responder):
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "universal", "retrieval_mode": "hybrid"})
    queries = []
    def search(db, user, space, query, *, pages, **kw):
        queries.append(query)
        p = next(p for p in pages.values() if p["version_id"] == source[1])
        row = db.get(m.ContentBlock, (source[1], anchor))
        unit = {"unit_id": "synthetic-unit", "page_id": p["id"], "resource_id": p["resource_id"],
            "version_id": p["version_id"], "kind": "document", "text": row.search_text, "block_ids": [anchor],
            "section_path": [], "score": .05, "rerank_score": 5., "channels": ["bm25", "vector"]}
        return {"query": query, "mode": "hybrid_unit_rerank", "units": [unit], "hits": [{
            "page_id": p["id"], "resource_id": p["resource_id"], "version_id": p["version_id"], "score": .05,
            "channels": ["bm25", "vector"], "matched_block_ids": [anchor], "candidate_snippets": [unit]}],
            "catalog_pages": len(pages), "indexed_catalog_pages": len(pages), "total_candidates": 1,
            "returned": 1, "warnings": [], "timing_ms": .1, "candidate_preview_stats": {}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    rid, jid, calls = prepare(env, monkeypatch, responder)
    worker = JobDispatcher(env.settings, env.db, None)
    # Bypass no permissions: this stub only scores the source texts the worker
    # independently read/verified; its embedding identity is synthetic.
    worker.vector_index = SimpleNamespace(embedding=SimpleNamespace(fingerprint="synthetic-context"),
        settings=env.settings, rerank=lambda question, texts: [float(i) for i in range(len(texts))])
    return rid, jid, calls, queries, worker


def final(calls):
    ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
    return "## 合成答复\n核对所需条件与完整处理步骤。" + "".join(f"[{eid}]" for eid in ids)


def test_cross_document_cycle_closes_before_synthesis_and_unrelated_chapters_stay_unread(env, monkeypatch):
    source, source_ids = document(env, "合成甲", ["第一条 方法", "SOURCE_A。依照《合成乙》第三条核对。", "第二条 无关", "UNRELATED_A。"])
    target, _ = document(env, "合成乙", ["第三条 条件", "SOURCE_B。依照《合成甲》第一条执行。", "第四条 无关", "UNRELATED_B。"])
    def respond(calls, _):
        if len(calls) == 1:
            return "ISSUE 核对条件\nSEARCH 合成甲方法"
        assert "SOURCE_A" in calls[-1] and "SOURCE_B" in calls[-1]
        assert "UNRELATED_A" not in calls[-1] and "UNRELATED_B" not in calls[-1]
        return final(calls)
    rid, jid, calls, queries, worker = setup(env, monkeypatch, source, source_ids[1], respond)
    try:
        worker._answer(jid, 1)
    finally:
        worker.close()
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.state == "COMPLETED" and len(calls) == 2
        report = run.model_snapshot["context_completion"]
        assert report["gap_count"] == 0 and report["resolved_count"] == 2
        assert report["group_rerank"]["status"] == "scored"
        assert report["group_rerank"]["dropped_pages"] == 0
        assert {e["version_id"] for e in run.evidence_snapshot} == {source[1], target[1]}
        assert len({e["evidence_id"] for e in run.evidence_snapshot}) == len(run.evidence_snapshot)


def test_missing_explicit_source_is_searched_once_then_exposed_without_blocking_answer(env, monkeypatch):
    source, ids = document(env, "已知合成规则", ["第一条 方法", "SOURCE。依照《不存在的合成规则》第三条执行。"])
    def respond(calls, _):
        if len(calls) == 1:
            return "ISSUE 核对来源\nSEARCH 已知合成规则"
        assert "not_in_authorized_catalog" in calls[-1]
        return final(calls)
    rid, jid, calls, queries, worker = setup(env, monkeypatch, source, ids[1], respond)
    try:
        worker._answer(jid, 1)
    finally:
        worker.close()
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.state == "COMPLETED" and len(calls) == 2
        assert queries.count("不存在的合成规则 第三条") == 1
        assert run.model_snapshot["context_completion"]["gap_count"] == 1
        assert "SOURCE_CONTEXT_GAPS" in [w["code"] for w in run.response["quality_warnings"]]
    from test_reference_security_review import view
    public = view(env, rid)
    assert "SOURCE_CONTEXT_GAPS" in [w["code"] for w in public["answer"]["quality_warnings"]]


@pytest.mark.parametrize("change", ["revoke", "cancel", "hash"])
def test_authority_fence_after_group_reranker_wait(env, monkeypatch, change):
    source, ids = document(env, "合成来源", ["第一条 方法", "按《合成依赖》第三条执行。"])
    target, target_ids = document(env, "合成依赖", ["第三条 条件", "DEPENDENCY。"])
    def respond(calls, _):
        assert len(calls) == 1, "must not synthesize with revoked/stale sources"
        return "ISSUE 核对条件\nSEARCH 合成来源"
    rid, jid, calls, _, worker = setup(env, monkeypatch, source, ids[1], respond)
    def rerank(question, texts):
        with env.db.begin() as db:
            if change == "revoke":
                db.get(m.Resource, target[0]).suspended = True
            elif change == "cancel":
                db.get(m.Job, jid).cancel_requested = True
            else:
                db.get(m.ContentBlock, (target[1], target_ids[1])).content_sha256 = "0" * 64
        return [1.] * len(texts)
    worker.vector_index.rerank = rerank
    try:
        with pytest.raises((JobError, svc.APIError)):
            worker._answer(jid, 1)
    finally:
        worker.close()
    with env.db() as db:
        assert db.get(m.ConsultationRun, rid).response is None


def test_multihop_followup_retains_all_new_blocks_when_long_material_is_packetized(env, monkeypatch):
    from fund_kb import wiki_answer_job
    root, ids = document(env, "合成入口", ["第一条 起点", "ROOT。"])
    second, _ = document(env, "合成大段甲", ["第三条 细节", "BEGIN_SECOND。" + "完整合成甲文本。" * 1800 + "END_SECOND。见《合成大段乙》第四条。"])
    document(env, "合成大段乙", ["第四条 依赖", "BEGIN_THIRD。" + "完整合成乙文本。" * 1800 + "END_THIRD。"])
    captured = {}
    def respond(calls, _):
        if len(calls) == 1:
            return "ISSUE 核对全部细节\nSEARCH 合成入口"
        if len(calls) == 2:
            return "READ " + next(pid for pid, p in captured.items() if p["version_id"] == second[1])
        return final(calls)
    rid, jid, calls, _, worker = setup(env, monkeypatch, root, ids[1], respond)
    original_search = hybrid_retrieval.search_catalog
    def search(*args, **kw):
        captured.update(kw["pages"])
        return original_search(*args, **kw)
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    monkeypatch.setattr(wiki_answer_job, "fits_context", lambda system, body, connection, **kw: len(body) < 15000)
    try:
        worker._answer(jid, 1)
    finally:
        worker.close()
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.state == "COMPLETED"
    sent = "".join(calls[2:])
    assert all(marker in sent for marker in ["BEGIN_SECOND", "END_SECOND", "BEGIN_THIRD", "END_THIRD"])
    assert len(calls) > 3


def test_wiki_dominated_candidates_trigger_precise_source_read_before_synthesis(env, monkeypatch):
    source, ids = document(env, "合成处理标准", ["第一条 规则", "NEEDED_SOURCE_RULE。", "第二条 无关", "UNRELATED_RULE。"])
    wiki1 = page(env, "合成导航一", "NAVIGATION_ONE")
    wiki2 = page(env, "合成导航二", "NAVIGATION_TWO")
    def respond(calls, _):
        if len(calls) == 1:
            return "ISSUE 核对实际规则\nSEARCH 合成检索"
        assert "NEEDED_SOURCE_RULE" in calls[-1] and "UNRELATED_RULE" not in calls[-1]
        assert '"status": "SOURCE_READ"' in calls[-1]
        return final(calls)
    rid, jid, calls, _, worker = setup(env, monkeypatch, source, ids[1], respond)
    worker.settings.retrieval_seed_units = 1
    def search(db, user, space, query, *, pages, **kw):
        units, hits = [], []
        for rank, (record, anchor) in enumerate([(wiki1, wiki1[2]), (wiki2, wiki2[2]), (source, ids[1])], 1):
            p = next(p for p in pages.values() if p["version_id"] == record[1])
            row = db.get(m.ContentBlock, (record[1], anchor))
            u = {"unit_id": "synthetic-" + str(rank), "page_id": p["id"], "resource_id": p["resource_id"],
                "version_id": p["version_id"], "kind": p["kind"], "text": row.search_text, "block_ids": [anchor],
                "section_path": ["s" + str(rank)], "score": 1. / rank, "rerank_score": 10. - rank, "channels": ["vector"]}
            units.append(u)
            hits.append({**{key: u[key] for key in ("page_id", "resource_id", "version_id", "score", "channels")},
                "matched_block_ids": [anchor], "candidate_snippets": [u]})
        return {"query": query, "mode": "hybrid_unit_rerank", "units": units, "hits": hits,
            "catalog_pages": len(pages), "indexed_catalog_pages": len(pages), "total_candidates": 3,
            "returned": 3, "warnings": [], "timing_ms": .1, "candidate_preview_stats": {}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    try:
        worker._answer(jid, 1)
    finally:
        worker.close()
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.state == "COMPLETED" and len(calls) == 2
        ledger = run.model_snapshot["context_completion"]["reading_coverage"]
        assert ledger["source_read_count"] == ledger["direction_count"] == 2
        assert ledger["professional_completeness"] == "NOT_EVALUATED"
        assert source[1] in {r["version_id"] for r in run.evidence_snapshot}


def test_missing_query_direction_survives_as_warning_not_failed_or_silent_answer(env, monkeypatch):
    source, ids = document(env, "已知规则", ["第一条 条件", "KNOWN_RULE。"])
    def respond(calls, _):
        if len(calls) == 1:
            return "ISSUE 检查条件与例外\nSEARCH 已知条件\nSEARCH 未知例外"
        assert '"status": "NO_CANDIDATE"' in calls[-1]
        return final(calls)
    rid, jid, calls, queries, worker = setup(env, monkeypatch, source, ids[1], respond)
    original = hybrid_retrieval.search_catalog
    def search(db, user, space, query, **kw):
        result = original(db, user, space, query, **kw)
        if query == "未知例外":
            result.update(units=[], hits=[], total_candidates=0, returned=0)
        return result
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    try:
        worker._answer(jid, 1)
    finally:
        worker.close()
    with env.db() as db:
        run = db.get(m.ConsultationRun, rid)
        assert run.state == "COMPLETED" and len(calls) == 2
        assert run.model_snapshot["context_completion"]["direction_gap_count"] == 1
        assert "READING_COVERAGE_GAPS" in {w["code"] for w in run.response["quality_warnings"]}
    from test_reference_security_review import view
    public = view(env, rid)
    assert "READING_COVERAGE_GAPS" in {w["code"] for w in public["answer"]["quality_warnings"]}
    assert queries.count("未知例外") == 1
