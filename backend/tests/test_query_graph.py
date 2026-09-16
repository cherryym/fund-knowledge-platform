"""Synthetic navigation only: no real corpus, database, services or models."""
import copy
import json

import pytest

from fund_kb.query_graph import _Relevance, _subject_terms, plan_graph_reads


def page(pid, title, kind="knowledge", **extra):
    return {"id": pid, "resource_id": "resource-" + pid, "version_id": "version-" + pid,
            "title": title, "kind": kind, "knowledge_type": "rule" if kind == "knowledge" else "source",
            "aliases": [], "category": "", "applicability": {}, "block_count": 100,
            "records": [], "body_loaded": False, "links": [], "relations": [], **extra}


def connect(pages, source, target, kind="CITES", blocks=(), *, origin="registered", anchors=()):
    edge = {"source": source, "target": target, "type": kind, "origin": origin,
            "verification_status": "PROPOSED" if origin == "proposed" else "REGISTERED_NOT_BUSINESS_VERIFIED",
            "conditions": {}, "explanation": "", "source_pages": [target],
            "anchors": [{"version_id": pages[target]["version_id"], "block_id": b} for b in blocks] + list(anchors)}
    pages[source]["relations"].append(copy.deepcopy(edge))
    pages[target]["relations"].append(copy.deepcopy(edge))
    pages[source]["links"].append(target)
    pages[target]["links"].append(source)
    return edge


def hit(pid, text="", blocks=(), score=1.0, channels=("vector", "bm25")):
    return {"page_id": pid, "score": score, "channels": list(channels), "matched_block_ids": list(blocks),
            "candidate_snippets": [{"text": text, "block_ids": list(blocks), "section_path": []}]}


def assert_contract(result, pages):
    assert set(result) == {"requested", "anchors", "reasons", "used_edges", "deferred_pages", "warnings", "stats"}
    requested, deferred = set(result["requested"]), set(result["deferred_pages"])
    assert len(result["requested"]) == len(requested)
    assert requested.isdisjoint(deferred)
    assert requested | deferred == set(pages)
    assert set(result["anchors"]) == requested
    assert set(result["reasons"]) <= set(pages)
    assert all(len(ids) == len(set(ids)) for ids in result["anchors"].values())
    for edge in result["used_edges"]:
        assert edge["source"] in requested and edge["target"] in requested
        assert edge["navigation_only"] is True
    stats = result["stats"]
    assert stats["requested_count"] == len(requested)
    assert stats["visited_count"] == len(requested)
    assert stats["seed_count"] + stats["graph_count"] == len(requested)
    assert stats["source_count"] + stats["wiki_count"] == len(requested)
    assert stats["business_accuracy"] == "NOT_EVALUATED"
    assert stats["coverage_status"] == "NOT_VERIFIED"
    assert stats["recommendation"] in {"direct", "expanded"}
    json.dumps(result, ensure_ascii=False, allow_nan=False)


def test_document_hit_recovers_wiki_by_exact_citation_even_with_opaque_title():
    pages = {"S": page("S", "操作汇编", "document"), "W": page("W", "条目甲")}
    connect(pages, "W", "S", blocks=["located", "whole-unit-tail"])
    result = plan_graph_reads("晶核合约日末定值", pages, [hit("S", "晶核合约日末定值", ["located"])])
    assert result["requested"] == ["S", "W"]
    assert "REVERSE_CITES_EXACT" in result["reasons"]["W"]
    assert result["anchors"]["S"] == ["located", "whole-unit-tail"]
    assert result["stats"]["recommendation"] == "expanded"
    assert_contract(result, pages)


def test_reverse_cites_significant_topic_without_exact_overlap():
    pages = {"S": page("S", "汇编", "document"), "W": page("W", "晶核合约日末定值")}
    connect(pages, "W", "S", blocks=["other-real-section"])
    result = plan_graph_reads("晶核合约日末定值", pages, [hit("S", "晶核合约日末定值", ["hit-block"])])
    assert "REVERSE_CITES_RELEVANT" in result["reasons"]["W"]
    assert result["anchors"]["S"] == ["other-real-section"]
    assert "EXACT_CITES_ANCHORS_PREFERRED" in result["reasons"]["S"]


def test_close_ranks_keep_direct_wiki_and_both_business_aspects_with_exact_sources():
    pages = {"S": page("S", "操作汇编", "document"), "X": page("X", "交接资料", "document"),
             "W-day": page("W-day", "晶核合约多向持仓日末定值"),
             "W-receipt": page("W-receipt", "晶核合约交接收件日定值"),
             "noise": page("noise", "海盐交易所合约广告", "document")}
    connect(pages, "W-day", "S", blocks=["day", "day-tail"])
    connect(pages, "W-receipt", "S", blocks=["receipt"])
    connect(pages, "W-receipt", "X", blocks=["handover", "handover-condition"])
    hits = [hit("S", "晶核合约多向持仓日末定值", ["day"], 1.0),
            hit("noise", "海盐合约交易所宣传资料", ["ad"], .999)]
    for n in range(10):
        pid = f"copy-{n}"
        pages[pid] = page(pid, "晶核合约日末定值备忘", "document")
        hits.append(hit(pid, "晶核合约日末定值", ["note"], .98 - n * .001))
    hits += [hit("W-day", "多向持仓日末定值", ["wiki-day"], .96),
             hit("W-receipt", "交接收件日定值", ["wiki-receipt"], .95)]
    result = plan_graph_reads("晶核合约交接时多向持仓日末定值与收件日如何处理？", pages, hits)
    assert {"S", "X", "W-day", "W-receipt"} <= set(result["requested"])
    assert "noise" in result["deferred_pages"]
    assert result["stats"]["seed_wiki_count"] >= 1
    assert set(result["anchors"]["S"]) >= {"day", "day-tail", "receipt"}
    assert set(result["anchors"]["X"]) == {"handover", "handover-condition"}
    assert_contract(result, pages)


def test_all_citation_anchors_and_condition_exception_dependency_closure():
    pages = {pid: page(pid, "条目" + pid, "document" if pid.startswith("S") else "knowledge")
             for pid in ("W", "requires", "depends", "applies", "exception", "S1", "S2", "S3")}
    all_blocks = [f"full-section-{n}" for n in range(250)]
    connect(pages, "W", "S1", blocks=all_blocks)
    connect(pages, "W", "requires", "REQUIRES")
    connect(pages, "requires", "depends", "DEPENDS_ON")
    connect(pages, "depends", "applies", "APPLIES_TO")
    connect(pages, "exception", "W", "EXCEPTION_OF")
    connect(pages, "exception", "S2", blocks=["exception-tail"])
    connect(pages, "applies", "S3", blocks=["applicability"])
    result = plan_graph_reads("晶核合约", pages, [hit("W", "晶核合约")])
    assert set(result["requested"]) == set(pages)
    assert set(result["anchors"]["S1"]) == set(all_blocks)
    assert result["anchors"]["S2"] == ["exception-tail"]
    assert "INCOMING_EXCEPTION" in result["reasons"]["exception"]
    assert_contract(result, pages)


def test_typed_and_canonical_cycles_converge_without_depth_or_tail_cutoff():
    pages = {f"W{n}": page(f"W{n}", "条件节点") for n in range(85)}
    pages["S"] = page("S", "原始记录", "document")
    for n in range(84):
        connect(pages, f"W{n}", f"W{n + 1}", ("REQUIRES", "DEPENDS_ON", "APPLIES_TO")[n % 3])
    connect(pages, "W84", "W0", "REQUIRES")
    pages["W0"]["canonical_page_id"] = "W45"
    pages["W45"]["canonical_page_id"] = "W0"
    connect(pages, "W84", "S", blocks=["last-real-block"])
    result = plan_graph_reads("晶核合约", pages, [hit("W0", "晶核合约")])
    assert set(result["requested"]) == set(pages)
    assert result["anchors"]["S"] == ["last-real-block"]
    assert result["stats"]["visited_count"] == 86
    assert_contract(result, pages)


def test_source_hub_does_not_expand_all_reverse_citations_or_launder_new_anchors():
    pages = {"S": page("S", "操作汇编", "document"), "W": page("W", "条目甲")}
    connect(pages, "W", "S", blocks=["exact", "second"])
    for n in range(1200):
        pid = f"unrelated-{n}"
        pages[pid] = page(pid, "海盐仓储操作流程")
        connect(pages, pid, "S", blocks=["second" if n == 0 else f"other-{n}"])
    result = plan_graph_reads("晶核合约日末定值", pages, [hit("S", "晶核合约日末定值", ["exact"])])
    assert result["requested"] == ["S", "W"]
    assert len(result["deferred_pages"]) == 1200
    assert result["stats"]["graph_edge_count"] == 1
    assert_contract(result, pages)


def test_weak_repeated_question_word_does_not_unlock_source_hub():
    pages = {"S": page("S", "汇编", "document"), "noise": page("noise", "海盐合约")}
    connect(pages, "noise", "S", blocks=["unrelated"])
    result = plan_graph_reads("晶核合约的日末定值，合约收件日定值", pages,
                              [hit("S", "晶核合约日末定值", ["actual"])])
    assert "noise" in result["deferred_pages"]


def test_inline_follows_real_relevance_and_per_link_context_not_broad_links():
    pages = {"W": page("W", "晶核交接", records=[{"text": "前提须先阅读[[资格门槛]]。另见[[海盐行情]]。"}]),
             "related": page("related", "晶核交接收件日"), "prerequisite": page("prerequisite", "资格门槛"),
             "noise": page("noise", "海盐行情"), "bogus": page("bogus", "晶核交接辅助事项"),
             "S": page("S", "来源甲", "document"), "P": page("P", "来源乙", "document")}
    pages["W"]["links"].append("bogus")  # Catalog links combine directions and types; not real inline input.
    connect(pages, "related", "S", blocks=["receipt-source"])
    connect(pages, "prerequisite", "P", blocks=["permission-source"])
    result = plan_graph_reads("晶核交接收件日", pages, [hit("W", "晶核交接")],
                              inline_links={"W": ["related", "prerequisite", "noise"]})
    assert {"related", "prerequisite", "S", "P"} <= set(result["requested"])
    assert {"noise", "bogus"} <= set(result["deferred_pages"])
    assert "INLINE_CONTEXT_NAVIGATION" in result["reasons"]["prerequisite"]
    assert {e["target"] for e in result["used_edges"] if e["type"] == "INLINE"} == {"related", "prerequisite"}
    assert "INLINE_LINKS_NAVIGATION_ONLY" in result["warnings"]
    assert_contract(result, pages)


def test_inline_semantically_redundant_network_remains_available_for_later_read():
    pages = {f"W{n}": page(f"W{n}", "晶核交接流程说明") for n in range(60)}
    links = {"W0": ["W1"], **{f"W{n}": [f"W{n + 1}"] for n in range(1, 59)}, "W59": ["W0"]}
    result = plan_graph_reads("晶核交接流程", pages, [hit("W0", "晶核交接流程")], inline_links=links)
    assert result["requested"] == ["W0", "W1"]
    assert "W2" in result["deferred_pages"] and "W59" in result["deferred_pages"]
    assert "INLINE_RELATED_CANDIDATES_DEFERRED" in result["warnings"]


def test_inline_context_chain_not_cut_off_by_redundancy_or_fixed_depth():
    pages = {f"P{n}": page(f"P{n}", "条件节点", knowledge_type="condition") for n in range(40)}
    links = {f"P{n}": [f"P{(n + 1) % 40}"] for n in range(40)}
    result = plan_graph_reads("晶核交接流程", pages, [hit("P0", "晶核交接流程")], inline_links=links)
    assert set(result["requested"]) == set(pages)
    assert_contract(result, pages)


def test_proposed_cites_and_requires_only_navigation_never_source_coverage():
    pages = {"W": page("W", "晶核交接"), "S": page("S", "晶核交接来源", "document"),
             "P": page("P", "晶核交接条件"), "noise": page("noise", "海盐仓储")}
    connect(pages, "W", "S", blocks=["comparison"], origin="proposed")
    connect(pages, "W", "P", "REQUIRES", origin="proposed")
    connect(pages, "W", "noise", "REQUIRES", origin="proposed")
    result = plan_graph_reads("晶核交接", pages, [hit("W", "晶核交接")])
    assert set(result["requested"]) == {"W", "S", "P"}
    assert all(edge["verification_status"] == "PROPOSED" for edge in result["used_edges"])
    assert "PROPOSED_NAVIGATION" in result["reasons"]["S"]
    assert "WIKI_SOURCE_CHAIN_MISSING" in result["warnings"]
    assert "PROPOSED_RELATIONS_NAVIGATION_ONLY" in result["warnings"]
    assert_contract(result, pages)


def test_every_mandatory_source_and_anchor_survives_low_rank_and_duplicates():
    pages = {f"S{n}": page(f"S{n}", "人工指定来源", "document") for n in range(15)}
    pages["W"] = page("W", "晶核交接")
    required = [{"page_id": pid, "anchor_block_ids": [f"start-{pid}", f"tail-{pid}"]}
                for pid in pages if pid != "W"]
    required += [{"page_id": "S0", "anchor_block_ids": ["another-unit"]}]
    result = plan_graph_reads("晶核交接", pages, [hit("W", "晶核交接", score=999), hit("S0", score=.00001)],
                              required_sources={"sources": required})
    assert set(result["requested"]) == set(pages)
    for source in required:
        assert set(source["anchor_block_ids"]) <= set(result["anchors"][source["page_id"]])
    assert result["stats"]["required_count"] == 15
    assert result["stats"]["seed_count"] == 16
    assert_contract(result, pages)


def test_required_topic_anchors_do_not_treat_shared_preamble_as_reverse_hit():
    pages = {"S": page("S", "汇编", "document"), "W": page("W", "章节甲"), "N": page("N", "章节乙")}
    connect(pages, "W", "S", blocks=["topic"])
    connect(pages, "N", "S", blocks=["preamble"])
    result = plan_graph_reads("晶核交接", pages, [], required_sources=[{
        "page_id": "S", "topic_block_ids": ["topic"], "anchor_block_ids": ["topic", "preamble", "last"]}])
    assert set(result["requested"]) == {"S", "W"}
    assert set(result["anchors"]["S"]) == {"topic", "preamble", "last"}


def test_adaptive_seed_budget_covers_explicit_multiple_aspects():
    aspects = ["quasar", "nebula", "pulsar", "comet", "orbit", "asteroid", "spectrum", "gravity", "photon", "plasma"]
    pages = {name: page(name, name, "document") for name in aspects}
    result = plan_graph_reads(", ".join(aspects), pages, [hit(name, name, ["complete-unit"]) for name in aspects])
    assert set(result["requested"]) == set(pages)
    assert result["stats"]["seed_limit"] > 8
    assert result["stats"]["covered_facet_count"] == 10


def test_unknown_ids_stale_versions_and_unadmitted_anchor_versions_cannot_escape():
    pages = {"W": page("W", "晶核交接", canonical_page_id="secret-canonical"),
             "S": page("S", "晶核交接来源", "document")}
    connect(pages, "W", "S", blocks=["real"], anchors=[{"version_id": "secret-version", "block_id": "secret-block"}])
    pages["W"]["relations"].append({"source": "W", "target": "secret-endpoint", "type": "CITES"})
    rows = [hit("W", "晶核交接"), hit("secret-hit"), {**hit("S", "stale", ["stale-hit"]), "version_id": "old"}]
    result = plan_graph_reads("晶核交接", pages, rows, inline_links={"W": ["secret-inline"]}, required_sources=[
        {"page_id": "secret-required", "anchor_block_ids": ["secret-anchor"]},
        {"page_id": "S", "version_id": "old", "anchor_block_ids": ["stale-required"]}])
    wire = json.dumps(result)
    assert "secret-" not in wire and "stale-" not in wire
    assert result["anchors"]["S"] == ["real"]
    assert "REQUIRED_SOURCE_NOT_ADMITTED" in result["warnings"]
    assert "REQUIRED_SOURCE_IDENTITY_MISMATCH" in result["warnings"]
    assert_contract(result, pages)


def test_no_relationship_is_invented_from_titles_or_shared_words():
    pages = {"W": page("W", "晶核交接"), "S": page("S", "晶核交接最高法定标准", "document")}
    result = plan_graph_reads("晶核交接", pages, [hit("W", "晶核交接"), hit("S", "晶核交接")])
    assert result["used_edges"] == []
    assert result["anchors"] == {"W": [], "S": []}
    assert "WIKI_SOURCE_CHAIN_MISSING" in result["warnings"]
    assert result["stats"]["business_accuracy"] == "NOT_EVALUATED"


def test_single_document_is_never_reported_as_sufficient_coverage():
    pages = {"S": page("S", "晶核交接", "document"), "W": page("W", "晶核交接流程")}
    connect(pages, "W", "S", blocks=["actual"])
    result = plan_graph_reads("晶核交接", pages, [hit("S", "晶核交接", ["actual"])])
    assert "W" in result["requested"]
    assert "SINGLE_DOCUMENT_COVERAGE_RISK" in result["warnings"]
    assert result["stats"]["escalation_required"] is True
    assert result["stats"]["coverage_status"] != "PASS"


def test_provenance_without_exact_blocks_requests_source_but_warns_about_locator():
    pages = {"W": page("W", "晶核交接"), "S": page("S", "来源", "document")}
    connect(pages, "W", "S", origin="provenance", anchors=[{"version_id": "version-S"}])
    result = plan_graph_reads("晶核交接", pages, [hit("W", "晶核交接")])
    assert result["anchors"]["S"] == []
    assert "SOURCE_ANCHORS_MISSING" in result["warnings"]


def test_relation_anchor_can_locate_third_authorized_page_without_faking_a_citation():
    pages = {"W": page("W", "晶核交接"), "P": page("P", "准入条件"),
             "S": page("S", "条件原始记录", "document")}
    connect(pages, "W", "P", "REQUIRES", anchors=[{"version_id": "version-S", "block_id": "condition-source"}])
    result = plan_graph_reads("晶核交接", pages, [hit("W", "晶核交接")])
    assert set(result["requested"]) == set(pages)
    assert result["anchors"]["S"] == ["condition-source"]
    assert [(e["source"], e["target"], e["type"]) for e in result["used_edges"]] == [("W", "P", "REQUIRES")]


def test_input_text_and_all_late_snippets_anchors_remain_intact_and_result_is_deterministic():
    pages = {"W": page("W", "晶核交接"), "S": page("S", "来源", "document")}
    text = "原文保留。" * 10000 + "前提需要完整处理。"
    pages["W"]["records"] = [{"text": text, "block_id": "whole-wiki"}]
    connect(pages, "W", "S", blocks=["first", "last"])
    rows = [hit("W", "晶核交接", ["initial"])]
    rows[0]["candidate_snippets"] += [{"text": text, "block_ids": ["very-last"]}]
    original = copy.deepcopy((pages, rows))
    first = plan_graph_reads("晶核交接", pages, {"hits": rows})
    assert first == plan_graph_reads("晶核交接", pages, rows)
    assert (pages, rows) == original
    assert set(first["anchors"]["W"]) == {"initial", "very-last"}
    first["anchors"]["S"].append("changed-output")
    assert (pages, rows) == original


@pytest.mark.parametrize("score", [None, "invalid", float("nan"), float("inf"), -1])
def test_invalid_rank_score_does_not_break_lexical_discovery(score):
    pages = {"W": page("W", "晶核交接")}
    result = plan_graph_reads("晶核交接", pages, [hit("W", "晶核交接", score=score)])
    assert result["requested"] == ["W"]
    assert_contract(result, pages)


def test_empty_or_weak_discovery_has_explicit_escalation_and_deferred_reads():
    empty = plan_graph_reads("未知主题", {}, [])
    assert "NO_READ_CANDIDATES" in empty["warnings"]
    assert_contract(empty, {})
    pages = {f"S{n}": page(f"S{n}", "无关汇编", "document") for n in range(20)}
    weak = plan_graph_reads("未知主题", pages, [hit(pid, channels=("vector",)) for pid in pages])
    assert weak["stats"]["seed_count"] == 1
    assert len(weak["deferred_pages"]) == 19
    assert "LOW_RELEVANCE_SEEDS_REQUIRE_REVIEW" in weak["warnings"]
    assert_contract(weak, pages)


def test_disconnected_procedure_and_rank24_context_collaborate_without_source_hub_fanout():
    pages = {
        "procedure": page("procedure", "晶核合约交接细则", "document"),
        "day": page("day", "晶核合约交接多向持仓收件日日末定值"),
        "special": page("special", "晶核合约多向持仓特别处理"),
        "manual": page("manual", "操作汇编", "document"),
        "timing": page("timing", "收件日时点"),
        "method": page("method", "定值方法指引", "document"),
    }
    exact = ["daily-value", "special-treatment", "receipt-time"]
    for pid in ("day", "special"):
        connect(pages, pid, "timing", "APPLIES_TO")
        connect(pages, pid, "manual", blocks=exact)
        connect(pages, pid, "manual", origin="provenance", anchors=[{"version_id": "version-manual"}])
    for n in range(93):
        pid = f"hub-{n}"
        pages[pid] = page(pid, "海盐仓储附录")
        connect(pages, pid, "manual", blocks=["late-penalty" if n == 0 else f"unrelated-{n}"])
    rows = [hit("procedure", "晶核合约交接细则封面", ["cover"], 1),
            hit("day", "晶核合约交接多向持仓收件日日末定值", ["day-wiki"], .99),
            hit("special", "晶核合约多向持仓特别处理", ["special-wiki"], .97),
            hit("manual", "交接延误处罚说明", ["late-penalty"], .95)]
    for n in range(19):
        pid = f"generic-{n}"
        pages[pid] = page(pid, "谷物合约定值公告", "document")
        rows.append(hit(pid, "谷物合约定值交易所宣传", ["irrelevant"], .94 - n * .01))
    rows.append(hit("timing", "收件日时点", ["timing-wiki"], .5))  # 24th candidate.
    rows.append(hit("method", "定值方法指引", ["method-clause"], .3))
    result = plan_graph_reads("晶核合约在交接过程中，多向持仓日末定值与收件日如何确定？", pages, rows)
    assert {"procedure", "day", "special", "timing", "manual", "method"} <= set(result["requested"])
    assert not any(pid.startswith(("hub-", "generic-")) for pid in result["requested"])
    assert set(result["anchors"]["manual"]) == set(exact)
    assert result["anchors"]["procedure"] == ["cover"]
    assert "RETRIEVAL_ANCHORS_DEFERRED" in result["reasons"]["manual"]
    assert not any("procedure" in (edge["source"], edge["target"]) for edge in result["used_edges"])
    assert not any("method" in (edge["source"], edge["target"]) for edge in result["used_edges"])
    assert result["stats"]["role_counts"]["method_reference"] == 1
    assert result["stats"]["role_counts"]["procedure_reference"] == 1
    assert result["stats"]["blocking_warnings"] == []
    assert_contract(result, pages)


def test_required_anchors_survive_exact_cites_preference_even_when_not_topical():
    pages = {"W": page("W", "晶核交接"), "S": page("S", "汇编", "document")}
    connect(pages, "W", "S", blocks=["exact"])
    result = plan_graph_reads("晶核交接", pages, [hit("W", "晶核交接"), hit("S", "无关脚注", ["footnote"])],
                              required_sources=[{"page_id": "S", "anchor_block_ids": ["mandatory-tail"]}])
    assert set(result["anchors"]["S"]) == {"exact", "mandatory-tail"}


def test_source_discovery_additional_question_aspect_survives_citation_preference():
    pages = {"W": page("W", "晶核交接"), "S": page("S", "操作汇编", "document"),
             "other": page("other", "章节乙")}
    connect(pages, "W", "S", blocks=["handover"])
    connect(pages, "other", "S", blocks=["calibration"])
    result = plan_graph_reads("晶核交接与光谱校准", pages,
                              [hit("W", "晶核交接"), hit("S", "光谱校准", ["calibration"])])
    assert set(result["anchors"]["S"]) == {"handover", "calibration"}
    assert "other" in result["requested"]


def test_advisory_coverage_and_blocking_warnings_have_distinct_cache_contracts():
    pages = {"S": page("S", "晶核交接流程", "document")}
    result = plan_graph_reads("晶核交接流程的定值方法", pages, [hit("S", "晶核交接流程", ["cover"])])
    stats = result["stats"]
    assert {"READ_REVALIDATION_REQUIRED", "BUSINESS_ACCURACY_NOT_EVALUATED"} <= set(stats["advisory_warnings"])
    assert "SINGLE_DOCUMENT_COVERAGE_RISK" in stats["coverage_warnings"]
    assert {"related_wiki", "method_reference_for_comparison"} <= set(stats["role_gaps"])
    assert stats["blocking_warnings"] == []
    assert set(stats["advisory_warnings"] + stats["coverage_warnings"] + stats["blocking_warnings"]) == set(result["warnings"])
    broken = plan_graph_reads("晶核交接", pages, [], required_sources=[{"page_id": "not-admitted"}])
    assert "REQUIRED_SOURCE_NOT_ADMITTED" in broken["stats"]["blocking_warnings"]


def test_action_words_taxonomy_and_disclaimers_cannot_unlock_manual_hub():
    question = "晶核合约交接业务的估值与会计处理如何核算？"
    shared_category = "估值与核算/估值指引核心/知识点/晶核合约交接"
    pages = {"S": page("S", "操作汇编", "document", category=shared_category),
             "W": page("W", "晶核合约交接估值", category=shared_category)}
    connect(pages, "W", "S", blocks=["exact-topic"])
    rows = [hit("W", "晶核合约交接估值", ["wiki"]), hit("S", "晶核合约交接", ["exact-topic"], .9)]
    # These unrelated titles reproduce shared ACTIONS without any true subject.
    for n, subject in enumerate(["海盐", "林木", "山泉", "果汁", "绢布", "陶瓷"] * 12):
        pid = f"noise-{n}"
        pages[pid] = page(pid, subject + "业务估值与会计处理", category=shared_category)
        connect(pages, pid, "S", blocks=["shared-general-introduction"])
        rows.append(hit(pid, subject + "估值业务处理。免责声明：本页晶核合约交接资料待核验。", ["noise"], .999))
    result = plan_graph_reads(question, pages, rows)
    assert set(result["requested"]) == {"S", "W"}
    assert result["stats"]["seed_wiki_count"] == 1
    assert result["stats"]["graph_edge_count"] == 1
    assert result["stats"]["role_counts"].get("method_reference", 0) == 0
    assert_contract(result, pages)


def test_shared_real_hit_block_is_not_an_exact_backlink_pass_for_all_hub_wikis():
    pages = {"S": page("S", "操作汇编", "document"), "W": page("W", "晶核合约交接估值")}
    connect(pages, "W", "S", blocks=["shared-preamble", "specific"])
    for n in range(93):
        pid = f"other-{n}"
        pages[pid] = page(pid, "海盐业务估值处理")
        connect(pages, pid, "S", blocks=["shared-preamble"])
    result = plan_graph_reads("晶核合约交接业务估值处理", pages,
                              [hit("S", "晶核合约交接业务估值处理", ["shared-preamble"])])
    assert set(result["requested"]) == {"S", "W"}
    assert "SHARED_SOURCE_ANCHORS_REQUIRE_TOPIC_MATCH" in result["warnings"]
    assert "DEFERRED_SHARED_SOURCE_ANCHOR" in result["reasons"]["other-0"]


def test_direct_asset_and_event_wiki_outranks_generic_contract_action_hits():
    pages = {"specific": page("specific", "晶核合约交接估值")}
    rows = [hit("specific", "晶核合约交接业务处理", ["wiki"], .98)]
    for n in range(24):
        pid = f"generic-{n}"
        pages[pid] = page(pid, "谷物合约业务估值处理")
        rows.insert(0, hit(pid, "合约业务估值处理核算", ["generic"], .999))
    result = plan_graph_reads("晶核合约交接如何估值与会计处理", pages, rows)
    assert result["requested"] == ["specific"]
    assert_contract(result, pages)


def test_seed_selection_stops_without_filling_eight_redundant_relevant_documents():
    pages = {"W": page("W", "晶核交接"), "S": page("S", "晶核交接细则", "document")}
    rows = [hit("W", "晶核交接", ["wiki"]), hit("S", "晶核交接", ["source"], .99)]
    for n in range(30):
        pid = f"copy-{n}"
        pages[pid] = page(pid, "晶核交接细则参考", "document")
        rows.append(hit(pid, "晶核交接", ["source"], .98))
    result = plan_graph_reads("晶核交接", pages, rows)
    assert set(result["requested"]) == {"W", "S"}
    assert result["stats"]["seed_count"] == 2
    assert result["stats"]["seed_limit"] == 8
    assert len(result["deferred_pages"]) == 30


def test_separated_subject_words_remain_strong_despite_compact_query_word_order():
    query = "晶核合约交接业务如何估值与核算处理？"
    titles = {"procedure": "晶核合约交接细则", "wiki": "合约实物交接多向收件日晶核日终估值",
              "generic": "谷物合约估值处理"}
    ranking = _Relevance(query, titles)
    assert ranking.strong["wiki"]
    assert ranking.scores["wiki"] > ranking.scores["generic"]
    assert not ranking.strong["generic"]
    pages = {pid: page(pid, title, "document" if pid == "procedure" else "knowledge")
             for pid, title in titles.items()}
    result = plan_graph_reads(query, pages, [hit("procedure", titles["procedure"], ["cover"], 1),
                              hit("wiki", titles["wiki"], ["rule"], .99),
                              hit("generic", titles["generic"], ["noise"], .98)])
    assert set(result["requested"]) == {"procedure", "wiki"}


@pytest.mark.parametrize("query", ["国债 期货 交割 业务 估值 处理", "国债 期货 交割 业务；估值 核算 处理"])
def test_reported_real_titles_use_atomic_subjects_independently_of_word_order(query):
    # User-provided names, not fetched business data or invented financial rules.
    titles = {
        "delivery": "中国金融期货交易所国债期货交割细则",
        "daily": "期货实物交割多头收券日国债日终估值",
        "special": "期货实物交割多头特别会计处理",
        "commodity": "上期所期货交割基准用途",
        "generic": "上期所期货业务估值处理",
    }
    ranking = _Relevance(query, titles)
    assert _subject_terms(query) == {"国债", "期货", "交割"}
    assert ranking.subject_matches["daily"] == {"国债", "期货", "交割"}
    assert ranking.strong["daily"]
    assert ranking.scores["daily"] > ranking.scores["commodity"] > ranking.scores["generic"]
    assert not ranking.strong["generic"]


@pytest.mark.parametrize("manual_first", [False, True])
@pytest.mark.parametrize("query", ["国债期货交割业务如何估值与核算处理？",
                                  "国债 期货 交割 业务 估值 处理", "国债 期货 交割 业务；估值 核算 处理"])
def test_reported_real_names_preserve_five_key_units_and_disconnected_procedure(manual_first, query):
    category = "估值与核算/估值指引核心/知识点/固定收益/衍生品"
    pages = {
        "delivery": page("delivery", "中国金融期货交易所国债期货交割细则", "document", category=category),
        "daily": page("daily", "期货实物交割多头收券日国债日终估值", category=category),
        "special": page("special", "期货实物交割多头特别会计处理", category=category),
        "timing": page("timing", "收券日时点", category=category),
        "manual": page("manual", "基金会计实务手册", "document", category=category),
        "method": page("method", "证券公司金融工具指引", "document", category=category),
    }
    exact = ["cited-1", "cited-2", "cited-3"]
    for pid in ("daily", "special"):
        connect(pages, pid, "timing", "APPLIES_TO")
        connect(pages, pid, "manual", blocks=exact)
        connect(pages, pid, "manual", origin="provenance", anchors=[{"version_id": "version-manual"}])
    rows = [hit("delivery", pages["delivery"]["title"], ["cover"], 1),
            hit("daily", pages["daily"]["title"] + "。待核验，业务估值与核算资料仅供参考。", ["daily-unit"], .99),
            hit("special", pages["special"]["title"], ["special-unit"], .97),
            hit("manual", "交割违约", ["unrelated-footnote"], .95),
            hit("method", "证券公司金融工具估值指引", ["method-unit"], .4)]
    for n in range(19):
        pid = f"other-{n}"
        pages[pid] = page(pid, "上期所期货业务估值处理", category=category)
        rows.append(hit(pid, "上期所期货业务估值处理", ["generic-unit"], .98))
        connect(pages, pid, "manual", blocks=["generic-preamble"])
    rows.append(hit("timing", "收券日时点", ["timing-unit"], .2))
    # A required/first-selected manual must not consume the procedure role.
    required = [{"page_id": "manual", "anchor_block_ids": ["policy-context"]}] if manual_first else ()
    result = plan_graph_reads(query, pages, rows,
                              required_sources=required, inline_links={"daily": ["timing"], "special": ["timing"]})
    assert {"delivery", "daily", "special", "timing", "manual"} <= set(result["requested"])
    assert not any(pid.startswith("other-") for pid in result["requested"])
    assert "HYBRID_SEED" in result["reasons"]["delivery"]
    assert result["stats"]["role_counts"]["procedure_reference"] == 1
    assert set(result["anchors"]["manual"]) == set(exact + (["policy-context"] if manual_first else []))
    assert not any("delivery" in (edge["source"], edge["target"]) for edge in result["used_edges"])
    assert len(result["requested"]) in (5, 6)
    assert_contract(result, pages)


@pytest.mark.parametrize("bad_anchor,code", [
    ({"version_id": "unadmitted-version", "block_id": "unadmitted-block"}, "ANCHOR_NOT_ADMITTED"),
    ("malformed-anchor", "INVALID_ANCHOR"),
])
def test_unused_catalog_anchor_errors_are_advisory_and_do_not_change_route(bad_anchor, code):
    pages = {"W": page("W", "晶核交接"), "S": page("S", "原始资料", "document"),
             "U": page("U", "海盐仓储"), "V": page("V", "仓储资料", "document")}
    connect(pages, "W", "S", blocks=["real"])
    baseline = plan_graph_reads("晶核 交接", pages, [hit("W", "晶核交接")])
    connect(pages, "U", "V", anchors=[bad_anchor])
    result = plan_graph_reads("晶核 交接", pages, [hit("W", "晶核交接")])
    for key in ("requested", "anchors", "used_edges", "deferred_pages"):
        assert result[key] == baseline[key]
    assert code not in result["warnings"]
    assert "CATALOG_UNUSED_" + code in result["stats"]["advisory_warnings"]
    assert result["stats"]["blocking_warnings"] == []
    assert result["stats"]["escalation_required"] == baseline["stats"]["escalation_required"]
    assert "unadmitted-" not in json.dumps(result)

    # The same edge becomes blocking if a mandatory read actually uses it.
    required = plan_graph_reads("晶核 交接", pages, [hit("W", "晶核交接")],
                                required_sources=[{"page_id": "U"}])
    assert code in required["stats"]["blocking_warnings"]
    assert "CATALOG_UNUSED_" + code not in required["warnings"]
    assert "unadmitted-" not in json.dumps(required)
    assert_contract(required, pages)


def test_invalid_anchor_on_unused_edge_between_selected_pages_is_not_blocking():
    pages = {"W": page("W", "晶核交接"), "S": page("S", "原始资料", "document")}
    connect(pages, "W", "S", blocks=["real"])
    connect(pages, "W", "S", "EXPLAINS", anchors=[{"version_id": "unadmitted-version"}])
    result = plan_graph_reads("晶核 交接", pages, [hit("W", "晶核交接")])
    assert set(result["requested"]) == set(pages)
    assert {edge["type"] for edge in result["used_edges"]} == {"CITES"}
    assert result["stats"]["blocking_warnings"] == []
    assert "CATALOG_UNUSED_ANCHOR_NOT_ADMITTED" in result["stats"]["advisory_warnings"]
