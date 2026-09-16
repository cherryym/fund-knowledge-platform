"""Complete generic worker path, synthetic DB/providers only."""
import re

from test_adaptive_query_job import run
from test_wiki import page
from test_wiki_reader_job import base_env, prepare  # noqa: F401
from test_wiki_reader_job import env as env  # noqa: PLC0414

from fund_kb import hybrid_retrieval
from fund_kb import models as m
from fund_kb.wiki_reader import UNIVERSAL_PROMPT_VERSION


def test_model_plans_then_reads_units_wiki_and_sources_without_named_answer_rules(env, monkeypatch):
    first = page(env, "任意来源甲", "FIRST_SOURCE：本事项需先确认适用条件，再检查输入单据。", kind="document")
    second = page(env, "任意来源乙", "SECOND_SOURCE：后续处理要核对双方记录与余额，形成核对凭证。", kind="document")
    wiki = page(env, "复合事项知识入口", "WIKI_SOURCE：完整流程包含前后阶段，应联合两份原文核对。", cites=[first, second])
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "universal"})
    queries = []
    def search(db, user, space, query, *, pages, **kwargs):
        queries.append(query)
        units, hits = [], []
        for index, item in enumerate((first, second, wiki)):
            p = next(p for p in pages.values() if p["version_id"] == item[1])
            block = db.get(m.ContentBlock, (item[1], item[2]))
            u = {"unit_id": "unit" + item[1], "page_id": p["id"], "resource_id": p["resource_id"],
                 "version_id": p["version_id"], "kind": p["kind"], "text": block.search_text,
                 "block_ids": [item[2]], "section_path": [], "score": .05,
                 "rerank_score": 3.0 - index, "channels": ["vector", "bm25"]}
            units.append(u)
            hits.append({"page_id": p["id"], "resource_id": p["resource_id"], "version_id": p["version_id"],
                         "score": .05, "channels": ["vector", "bm25"], "matched_block_ids": [item[2]],
                         "candidate_snippets": [u]})
        return {"query": query, "mode": "hybrid_unit_rerank", "units": units, "hits": hits,
                "catalog_pages": len(pages), "indexed_catalog_pages": len(pages),
                "total_candidates": len(hits), "returned": len(hits), "warnings": [],
                "timing_ms": 0.1, "candidate_preview_stats": {"verified_snippets": 3, "source_blocks_checked": 3}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    def respond(calls, _):
        if len(calls) == 1:
            assert "FIRST_SOURCE" not in calls[0] and "SECOND_SOURCE" not in calls[0]
            return "ISSUE 确认适用条件与后续处理\nSEARCH 复合事项输入条件\nSEARCH 复合事项后续核对"
        assert all(text in calls[-1] for text in ["FIRST_SOURCE", "SECOND_SOURCE", "WIKI_SOURCE"])
        assert "初始查证事项" in calls[-1]
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return "## 处理说明\n\n先核对适用条件，再处理后续事项。" + "".join(f"[{i}]" for i in ids)
    rid, jid, calls = prepare(env, monkeypatch, respond)
    with env.db.begin() as db:
        r = db.get(m.ConsultationRun, rid)
        r.request = {**r.request, "question": "复合事项的完整流程如何处理？"}
    run(env, jid)
    with env.db() as db:
        result = db.get(m.ConsultationRun, rid)
        assert result.state == "COMPLETED" and len(calls) == 2 and len(queries) == 3
        assert result.model_snapshot["prompt_version"] == UNIVERSAL_PROMPT_VERSION
        assert result.model_snapshot["query_path"]["strategy"] == "universal_wiki_rag"
        assert result.model_snapshot["planning_model_invoked"] is True
        assert result.model_snapshot["query_path"]["selection_model_calls"] == 0
        assert result.model_snapshot["citation_integrity"]["source_versions"] == 3
        assert result.model_snapshot["citation_integrity"]["semantic_entailment"] == "NOT_EVALUATED"
        assert result.model_snapshot["last_request"]["limits"]["total_seconds"] is None
