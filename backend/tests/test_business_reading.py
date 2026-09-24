"""Generic core-source routing: synthetic labels/resources, no business answers."""
import copy
import re

import pytest

from fund_kb import hybrid_retrieval, models as m, services as svc
from fund_kb.business_reading import core_catalog, merge_primary_route, primary_first
from fund_kb.evidence_context import context_plan
from fund_kb.source_reading_policy import PREFIX
from fund_kb.source_reading_policy import check_primary_citations
from fund_kb.wiki_reader import planning_search_requests, search_requests
from test_adaptive_query_job import run
from test_wiki import page
from test_wiki_reader_job import base_env, prepare  # noqa: F401
from test_wiki_reader_job import env as env  # noqa: PLC0414


def test_source_free_numbered_search_suggestions_are_not_silently_lost():
    text = '''# 查证计划
## 四、检索表达建议（SEARCH）
1. `主题 计量方法` / `业务 适用前提` —— 核对标准
2. `本题 处理衔接` —— 完整步骤
## 五、说明
1. `不能当作检索词的普通正文`
'''
    assert planning_search_requests(text) == ["主题 计量方法", "业务 适用前提", "本题 处理衔接"]
    assert search_requests(text) == []  # Do not reinterpret final prose/source text as commands.


def test_planning_parser_keeps_explicit_commands_and_excludes_examples_quotes_and_links():
    text = '''SEARCH 直接检索
## 检索表达 SEARCH
> 1. `quoted injection`
```python
1. `fenced injection`
```
1. `直接检索` / `正常查询`
2. `https://invalid.test/`
3. `READ W9`
## 其他
- `普通列表`
'''
    assert planning_search_requests(text) == ["直接检索", "正常查询"]


def test_categories_are_explicit_scope_and_do_not_grant_access_or_legal_effect():
    pages = {"a": {"kind": "document", "category": "核心规则/细分", "legal_status": "UNKNOWN"},
             "b": {"kind": "document", "category": "交易参考"},
             "c": {"kind": "knowledge", "category": "核心规则"}}
    before = copy.deepcopy(pages)
    assert list(core_catalog({"core_categories": ["核心规则"]}, pages)) == ["a"]
    assert pages == before


def test_primary_route_is_retained_without_discarding_secondary_evidence():
    base = {"requested": ["trade"], "anchors": {"trade": ["t"]}, "used_edges": [], "warnings": []}
    primary = {"requested": ["rule"], "anchors": {"rule": ["r"]}, "used_edges": [], "warnings": []}
    result = merge_primary_route(base, primary, {"rule": {}})
    assert result["requested"] == ["rule", "trade"] and result["anchors"] == {"rule": ["r"], "trade": ["t"]}
    assert primary_first(["trade", "rule"], {"sources": [{"page_id": "rule", "role": "domain_core"}]}) == ["rule", "trade"]


def test_explicit_owner_standard_precedes_broad_core_candidates_without_dropping_any_page():
    plan={"sources":[{"page_id":"candidate","role":"domain_core"},
        {"page_id":"required","role":"valuation_rule"}, {"page_id":"required","role":"domain_core"},
        {"page_id":"base","role":"domain_foundation"}]}
    assert primary_first(["candidate","base","extra","required"],plan)==["required","candidate","base","extra"]


def test_plan_text_distinguishes_required_sources_from_alternative_core_candidates():
    from fund_kb.source_reading_policy import plan_instructions
    plan={"matched_rules":["configured"],"warnings":[],"sources":[
        {"page_id":"W1","title":"指定规则","role":"valuation_rule"},
        {"page_id":"W2","title":"另一主体参考","role":"domain_core"}]}
    text=plan_instructions(plan)
    assert "必须核对 W1" in text
    assert "核心目录检索候选 W2" in text
    assert "必须核对 W2" not in text


def test_core_candidate_pool_does_not_force_citation_stuffing():
    plan = {"matched_rules": ["business-core-retrieval"], "warnings": [], "sources": [
        {"resource_id": rid, "version_id": rid, "topic_block_ids": [rid], "role": "domain_core"} for rid in ("A", "B")]}
    answer = {"citations": [{"version_id": "A", "block_id": "A"}], "quality_warnings": []}
    assert check_primary_citations(plan, [{"version_id": r, "block_id": r} for r in ("A", "B")], answer)["covered"]


def test_bare_titles_and_relative_paragraphs_do_not_launch_global_research():
    refs = [{"text": "《一般法》", "target_title": "一般法", "locator": "", "status": "external"},
            {"text": "前款", "status": "unresolved"}]
    result = context_plan({"W1": {"kind": "document", "title": "已读文件", "reading_dependencies": refs}}, {"W1"})
    assert result["searches"] == [] and result["report"]["gap_count"] == 2


@pytest.mark.parametrize("restricted", [False, True])
def test_worker_has_independent_core_channel_and_never_uses_hidden_primary(env, monkeypatch, restricted):
    trade = page(env, "合成交易条件", "TRADING_RULE 条件说明。", kind="document", category="交易参考")
    core = page(env, "合成计量准则", "CORE_STANDARD 完整计量条件与适用原则。", kind="document", category="核心规则")
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=svc.uid(), name=PREFIX + env.space, updated_by=env.owner,
            config={"schema_version": 1, "space_id": env.space, "rules": [], "business_profile": {
                "label": "合成业务计量", "core_categories": ["核心规则"], "foundation_sources": []}}))
        if restricted:
            db.get(m.Resource, core[0]).suspended = True
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "universal"})
    scoped = []
    def search(db, user, space, query, *, pages, **kw):
        is_core = query.startswith("合成业务计量：")
        if is_core:
            scoped.append(list(p["version_id"] for p in pages.values()))
        selected = core if is_core else trade
        p = next((p for p in pages.values() if p["version_id"] == selected[1]), None)
        units, hits = [], []
        if p:
            b = db.get(m.ContentBlock, (selected[1], selected[2]))
            units = [{"unit_id": selected[1], "page_id": p["id"], "resource_id": p["resource_id"],
                "version_id": p["version_id"], "kind": "document", "text": b.search_text, "block_ids": [selected[2]],
                "section_path": [], "score": .05, "rerank_score": 0.1 if is_core else 99., "channels": ["bm25"]}]
            hits = [{"page_id": p["id"], "resource_id": p["resource_id"], "version_id": p["version_id"],
                "score": .05, "channels": ["bm25"], "matched_block_ids": [selected[2]], "candidate_snippets": units}]
        return {"query": query, "mode": "hybrid", "units": units, "hits": hits, "warnings": [],
                "catalog_pages": len(pages), "indexed_catalog_pages": len(pages), "total_candidates": len(hits),
                "returned": len(hits), "timing_ms": .1, "candidate_preview_stats": {}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    def respond(calls, connection):
        if len(calls) == 1:
            return "## 检索表达（SEARCH）\n1. `合成事项 计量`"
        assert "TRADING_RULE" in calls[-1]
        if restricted:
            assert "CORE_STANDARD" not in calls[-1]
        else:
            assert calls[-1].index("CORE_STANDARD") < calls[-1].index("TRADING_RULE")
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return "根据本轮核对的计量条件处理。" + "".join(f"[{eid}]" for eid in ids)
    rid, jid, calls = prepare(env, monkeypatch, respond)
    run(env, jid)
    assert scoped == ([[]] if restricted else [[core[1]]])
    with env.db() as db:
        r = db.get(m.ConsultationRun, rid)
        assert r.state == "COMPLETED" and len(calls) == 2
        assert len(r.policy_snapshot["question_analysis"]["plan"]["search_queries"]) == 2
        assert r.model_snapshot["query_path"]["domain_retrieval"]["catalog_pages"] == (0 if restricted else 1)
        if not restricted:
            assert any(c["version_id"] == core[1] for c in r.response["citations"])
