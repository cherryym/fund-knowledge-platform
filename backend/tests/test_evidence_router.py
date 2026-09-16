"""Synthetic, offline navigation regression; not a professional-answer gold set."""
import copy
import hashlib
import json
import socket

import pytest

from fund_kb.evidence_router import plan_evidence_reads
from fund_kb.query_graph import plan_graph_reads


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Evidence routing tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def page(pid, kind="document", **extra):
    return {"id": pid, "version_id": "v-" + pid, "resource_id": "r-" + pid,
            "kind": kind, "title": "通用资料 " + pid, "space_id": "local-space",
            "relations": [], "records": [], "block_count": 100, **extra}


def unit(pages, pid, uid, text="晶核交接核对", blocks=None, path=None, score=1.0, **extra):
    return {"unit_id": uid, "page_id": pid, "version_id": pages[pid]["version_id"],
            "resource_id": pages[pid]["resource_id"], "text": text,
            "block_ids": [uid + "-block"] if blocks is None else list(blocks),
            "section_path": ["交接", uid] if path is None else list(path),
            "score": score, "channels": ["bm25", "vector"], **extra}


def connect(pages, source, target, kind="CITES", blocks=(), **extra):
    edge = {"source": source, "target": target, "type": kind,
            "origin": "registered", "verification_status": "REGISTERED_NOT_BUSINESS_VERIFIED",
            "conditions": {}, "explanation": "真实输入关系",
            "anchors": [{"version_id": pages[target]["version_id"], "block_id": bid} for bid in blocks],
            "source_pages": [target], **extra}
    pages[source]["relations"].append(copy.deepcopy(edge))
    pages[target]["relations"].append(copy.deepcopy(edge))
    return edge


def assert_contract(result, pages):
    assert set(result) == {"requested", "anchors", "reasons", "used_edges", "deferred_pages", "warnings", "stats"}
    requested, deferred = set(result["requested"]), set(result["deferred_pages"])
    assert len(requested) == len(result["requested"])
    assert not requested & deferred
    assert requested | deferred == set(pages)
    assert set(result["anchors"]) == set(result["reasons"]) == requested
    assert all(len(blocks) == len(set(blocks)) for blocks in result["anchors"].values())
    for edge in result["used_edges"]:
        assert edge["source"] in requested and edge["target"] in requested
        assert edge["navigation_only"] is True
        actual = [row for page in pages.values() for row in page["relations"]
                  if all(row.get(key) == edge.get(key) and (key in row) == (key in edge)
                         for key in ("source", "target", "type", "origin", "verification_status"))]
        assert actual
        assert all(anchor in row.get("anchors", []) for anchor in edge.get("anchors", []) for row in actual[:1])
    stats = result["stats"]
    assert stats["seed_unit_count"] <= stats["seed_limit"]
    assert stats["selected_unit_count"] + stats["unselected_unit_count"] == stats["admitted_unit_count"]
    assert stats["selected_unit_count"] == stats["seed_unit_count"]
    assert stats["selected_page_count"] == stats["seed_count"]
    assert stats["selected_section_count"] == sum(stats["page_section_counts"].values())
    assert set(stats["page_section_counts"]) <= requested
    assert stats["selected_query_count"] + stats["unselected_query_count"] == stats["matched_query_count"]
    assert stats["seed_count"] + stats["graph_count"] == len(requested)
    assert stats["source_count"] + stats["wiki_count"] == len(requested)
    assert stats["visited_count"] == stats["requested_count"] == len(requested)
    assert stats["business_accuracy"] == "NOT_EVALUATED"
    assert stats["coverage_status"] == "NOT_VERIFIED"
    assert stats["source_count_status"] == "PROVENANCE_NOT_FACT_VOTES"
    assert stats["full_text_reachable"] is True
    assert stats["recommendation"] in {"direct", "expanded"}
    assert set(result["warnings"]) == set(stats["advisory_warnings"] + stats["coverage_warnings"]
                                          + stats["blocking_warnings"])
    json.dumps(result, ensure_ascii=False, allow_nan=False)


def test_retains_eight_stages_in_one_manual_and_all_unit_blocks():
    pages = {"manual": page("manual"), "other": page("other")}
    stages = ["准备", "接收", "初始确认", "后续复核", "费用", "对账", "差异", "终止"]
    units = [unit(pages, "manual", f"u{i}", text=f"晶核交接：{stage}",
                  path=["同一本手册", stage], blocks=[f"start-{i}", f"tail-{i}"], rerank_score=.95 - i * .01)
             for i, stage in enumerate(stages)]
    result = plan_evidence_reads("晶核交接完整处理", pages, units)
    assert result["requested"] == ["manual"]
    assert set(result["anchors"]["manual"]) == {bid for row in units for bid in row["block_ids"]}
    assert result["stats"]["seed_unit_ids"] == [row["unit_id"] for row in units]
    assert result["stats"]["source_resource_count"] == 1
    assert_contract(result, pages)


def test_same_section_path_is_not_a_hard_limit_and_empty_paths_do_not_collapse():
    pages = {"manual": page("manual")}
    units = [unit(pages, "manual", f"u{i}", path=["章", "节"] if i < 6 else [], rerank_score=10 - i)
             for i in range(9)]
    result = plan_evidence_reads("晶核交接", pages, units)
    assert len(result["anchors"]["manual"]) == 9
    assert result["stats"]["seed_unit_count"] == 9
    assert_contract(result, pages)


def test_section_diversity_precedes_repeated_windows_without_document_quota():
    pages = {"manual": page("manual")}
    units = [unit(pages, "manual", "a", path=["甲"], rerank_score=1),
             unit(pages, "manual", "a-tail", path=["甲"], rerank_score=.99),
             unit(pages, "manual", "b", path=["乙"], rerank_score=.98)]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=2)
    assert result["stats"]["seed_unit_ids"] == ["a", "b"]
    assert result["stats"]["deferred_unit_ids"] == ["a-tail"]
    assert_contract(result, pages)


def test_initial_budget_retains_tail_units_and_entire_catalog():
    pages = {"manual": page("manual"), **{f"p{i}": page(f"p{i}") for i in range(90)}}
    units = [unit(pages, "manual", f"u{i}", rerank_score=100 - i) for i in range(60)]
    before = copy.deepcopy(pages)
    small = plan_evidence_reads("晶核交接", pages, units, max_seed_units=1)
    complete = plan_evidence_reads("晶核交接", pages, units, max_seed_units=60)
    assert small["stats"]["seed_unit_ids"] == ["u0"]
    assert len(small["stats"]["deferred_unit_ids"]) == 59
    assert "u59" in small["stats"]["deferred_unit_ids"]
    assert "u59-block" in complete["anchors"]["manual"]
    assert len(small["deferred_pages"]) == 90
    assert pages == before
    assert_contract(small, pages)
    assert_contract(complete, pages)


@pytest.mark.parametrize("budget", [0, 1, 4, 40])
def test_budget_is_only_for_seeds_and_never_a_directory_limit(budget):
    pages = {f"s{i}": page(f"s{i}") for i in range(8)}
    units = [unit(pages, pid, pid, rerank_score=1) for pid in pages]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=budget)
    assert result["stats"]["seed_unit_count"] == min(budget, len(units))
    assert_contract(result, pages)


@pytest.mark.parametrize("budget", [-1, True, 1.5, "4", None])
def test_invalid_budget_is_an_explicit_caller_error(budget):
    with pytest.raises(ValueError, match="max_seed_units"):
        plan_evidence_reads("晶核交接", {}, [], max_seed_units=budget)


def test_reranker_wins_over_title_role_lexical_and_retrieval_score_seeds():
    pages = {"right": page("right", title="不透明编号"),
             "wrong": page("wrong", title="晶核交接处理标准", role="method_reference")}
    units = [unit(pages, "wrong", "wrong-u", score=99999, rerank_score=-5),
             unit(pages, "right", "right-u", text="封装的校验摘要与移交回执须一致。", score=.001, rerank_score=-.3)]
    old = {"requested": ["wrong"], "anchors": {"wrong": ["wrong-u-block"]},
           "reasons": {"wrong": ["ROLE_DIVERSITY", "QUESTION_COVERAGE"]}, "used_edges": [],
           "stats": {"coverage_status": "PASS", "business_accuracy": "PASS"}}
    result = plan_evidence_reads("晶核交接处理标准", pages, units, graph_plan=old, max_seed_units=1)
    assert result["requested"] == ["right"]
    assert result["anchors"] == {"right": ["right-u-block"]}
    assert result["stats"]["seed_unit_ids"] == ["right-u"]
    assert_contract(result, pages)


def test_old_real_citation_cannot_erase_a_reranked_different_stage():
    pages = {"s": page("s"), "w": page("w", "knowledge")}
    connect(pages, "w", "s", blocks=["day", "old-stage"])
    units = [unit(pages, "s", "day-u", blocks=["day"], rerank_score=1),
             unit(pages, "s", "late-u", blocks=["late-stage"], rerank_score=.98)]
    old = {"requested": ["w", "s"], "anchors": {"s": ["day", "old-stage"]}, "used_edges": []}
    result = plan_evidence_reads("晶核交接", pages, units, graph_plan=old)
    assert set(result["anchors"]["s"]) == {"day", "old-stage", "late-stage"}
    assert result["requested"] == ["s", "w"]
    assert_contract(result, pages)


def test_missing_rerank_is_not_treated_as_zero_above_negative_logits():
    pages = {"a": page("a"), "b": page("b")}
    units = [unit(pages, "a", "a", score=1e30), unit(pages, "b", "b", score=-9, rerank_score=-100)]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=1)
    assert result["requested"] == ["b"]
    assert_contract(result, pages)


def test_fallback_rejects_cross_domain_body_even_when_title_and_score_match():
    pages = {"a": page("a"), "b": page("b", title="晶核交接手册")}
    units = [unit(pages, "b", "wrong", text="海盐仓储的含水率测量。", score=999),
             unit(pages, "a", "right", text="晶核交接核对原始凭单。", score=.01)]
    result = plan_evidence_reads("晶核交接如何处理", pages, units)
    assert result["requested"] == ["a"]
    assert result["stats"]["deferred_unit_ids"] == ["wrong"]
    assert "CROSS_DOMAIN_UNITS_DEFERRED" in result["warnings"]
    assert_contract(result, pages)


@pytest.mark.parametrize("question", ["", " \n\t ", None])
def test_empty_question_does_not_read_even_with_units_and_graph_hints(question):
    pages = {"w": page("w", "knowledge"), "s": page("s")}
    edge = connect(pages, "w", "s", blocks=["real"])
    result = plan_evidence_reads(question, pages, [unit(pages, "w", "w", rerank_score=1)],
                                 graph_plan={"requested": ["w", "s"], "used_edges": [edge]})
    assert result["requested"] == []
    assert result["used_edges"] == []
    assert "EMPTY_QUESTION" in result["warnings"]
    assert_contract(result, pages)


def test_empty_candidates_have_no_title_only_or_legacy_seeds():
    pages = {"a": page("a", title="晶核交接核对")}
    result = plan_evidence_reads("晶核交接核对", pages, [], graph_plan={"requested": ["a"]})
    assert result["requested"] == []
    assert "NO_READ_CANDIDATES" in result["warnings"]
    assert_contract(result, pages)
    assert_contract(plan_evidence_reads("晶核交接", {}, []), {})


@pytest.mark.parametrize("field,value", [("page_id", "PRIVATE"), ("version_id", "obsolete-version"),
                                         ("resource_id", "foreign-resource"), ("space_id", "other-space"),
                                         ("kind", "knowledge")])
def test_wrong_identity_rejects_unit_and_never_leaks_its_locator(field, value):
    pages = {"s": page("s")}
    candidate = unit(pages, "s", "bad", blocks=["SECRET-BLOCK"], rerank_score=1)
    candidate[field] = value
    result = plan_evidence_reads("晶核交接", pages, [candidate])
    assert result["requested"] == []
    assert result["stats"]["rejected_unit_count"] == 1
    assert "SECRET-BLOCK" not in json.dumps(result)
    if field != "kind":
        assert value not in json.dumps(result)
    assert_contract(result, pages)


@pytest.mark.parametrize("field,value", [("unit_id", ""), ("text", " \n"), ("text", []),
                                         ("block_ids", []), ("block_ids", ["ok", None]),
                                         ("section_path", "not-a-path"), ("channels", [None]),
                                         ("score", float("nan")), ("score", True),
                                         ("rerank_score", float("inf"))])
def test_malformed_units_fail_closed(field, value):
    pages = {"s": page("s")}
    candidate = unit(pages, "s", "bad")
    candidate[field] = value
    result = plan_evidence_reads("晶核交接", pages, [candidate])
    assert result["requested"] == []
    assert result["stats"]["rejected_unit_count"] == 1
    assert_contract(result, pages)


def test_exact_duplicates_merge_channels_without_more_votes_or_seed_slots():
    pages = {"s": page("s")}
    first = unit(pages, "s", "x", rerank_score=.8)
    duplicate = dict(first, unit_id="same-passage-other-channel", channels=["dense"], score=1e9, rerank_score=.7)
    result = plan_evidence_reads("晶核交接", pages, [first, copy.deepcopy(first), duplicate,
                                  unit(pages, "s", "y", rerank_score=.6)], max_seed_units=2)
    assert result["stats"]["duplicate_unit_count"] == 2
    assert result["stats"]["seed_unit_ids"] == ["x", "y"]
    assert result["stats"]["source_resource_count"] == 1
    assert_contract(result, pages)


def test_same_unit_id_with_conflicting_text_is_rejected_atomically():
    pages = {"s": page("s")}
    first = unit(pages, "s", "same", text="晶核交接应确认。")
    conflict = dict(first, text="晶核交接不应确认。")
    result = plan_evidence_reads("晶核交接", pages, [first, conflict])
    assert result["requested"] == []
    assert "UNIT_ID_CONFLICT" in result["warnings"]
    assert result["stats"]["rejected_unit_count"] == 2
    assert_contract(result, pages)


def test_different_real_units_keep_negation_exception_and_distinct_versions():
    pages = {"old": page("old", resource_id="one-document"),
             "new": page("new", resource_id="one-document")}
    units = [unit(pages, "old", "old-rule", text="晶核交接应于当日确认。", rerank_score=.99),
             unit(pages, "new", "new-rule", text="晶核交接不应于当日确认。", rerank_score=.98),
             unit(pages, "new", "exception", text="晶核交接除核对不符外，应于次日确认。", rerank_score=.97)]
    result = plan_evidence_reads("晶核交接确认", pages, units)
    assert result["stats"]["seed_unit_count"] == 3
    assert result["stats"]["source_resource_count"] == 1
    assert result["stats"]["source_version_count"] == 2
    assert set(result["anchors"]["new"]) == {"new-rule-block", "exception-block"}
    assert_contract(result, pages)


def test_incoming_exception_conflict_and_prerequisite_closure_preserve_real_direction():
    pages = {pid: page(pid, "knowledge" if pid != "source" else "document")
             for pid in ("rule", "exception", "conflict", "prerequisite", "source")}
    connect(pages, "exception", "rule", "EXCEPTION_OF", conditions={"unless": "核对不符"})
    connect(pages, "conflict", "rule", "CONFLICTS_WITH")
    connect(pages, "rule", "prerequisite", "REQUIRES")
    connect(pages, "exception", "source", blocks=["exception-head", "exception-tail"])
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "rule", "r", rerank_score=1)], max_seed_units=1)
    assert set(result["requested"]) == set(pages)
    assert result["anchors"]["source"] == ["exception-head", "exception-tail"]
    assert ("exception", "rule", "EXCEPTION_OF") in {
        (edge["source"], edge["target"], edge["type"]) for edge in result["used_edges"]}
    exception = next(edge for edge in result["used_edges"] if edge["type"] == "EXCEPTION_OF")
    assert exception["conditions"] == {"unless": "核对不符"}
    assert_contract(result, pages)


def test_long_typed_cycle_and_all_anchors_are_not_cut_by_seed_budget():
    pages = {f"w{i}": page(f"w{i}", "knowledge") for i in range(90)}
    pages["s"] = page("s")
    for i in range(89):
        connect(pages, f"w{i}", f"w{i + 1}", "REQUIRES")
    connect(pages, "w89", "w0", "DEPENDS_ON")
    blocks = [f"b{i}" for i in range(300)]
    connect(pages, "w89", "s", blocks=blocks)
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "w0", "seed", rerank_score=1)], max_seed_units=1)
    assert set(result["requested"]) == set(pages)
    assert set(result["anchors"]["s"]) == set(blocks)
    assert len(result["used_edges"]) == 91
    assert_contract(result, pages)


def test_frozen_reverse_cites_does_not_launder_added_anchors_through_a_source_hub():
    pages = {"s": page("s"), "good": page("good", "knowledge")}
    connect(pages, "good", "s", blocks=["initial", "second"])
    for i in range(150):
        pid = f"unrelated-{i}"
        pages[pid] = page(pid, "knowledge")
        connect(pages, pid, "s", blocks=["second" if i == 0 else f"unrelated-block-{i}"])
    result = plan_evidence_reads("晶核交接", pages,
                                 [unit(pages, "s", "seed", blocks=["initial"], rerank_score=1)])
    assert result["requested"] == ["s", "good"]
    assert result["anchors"]["s"] == ["initial", "second"]
    assert len(result["deferred_pages"]) == 150
    assert_contract(result, pages)


def test_same_source_many_wikis_do_not_consume_all_seeds_or_increase_fact_votes():
    pages = {"s": page("s")}
    units = []
    for i in range(20):
        pid = f"w{i}"
        pages[pid] = page(pid, "knowledge")
        connect(pages, pid, "s", blocks=["same-original"])
        units.append(unit(pages, pid, pid, text=f"晶核交接说明{i}", rerank_score=1 - i * .01))
    units.append(unit(pages, "s", "different-stage", blocks=["later-original"], rerank_score=.7))
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=2)
    assert result["stats"]["seed_unit_ids"] == ["w0", "different-stage"]
    assert set(result["requested"]) == {"w0", "s"}
    assert set(result["anchors"]["s"]) == {"same-original", "later-original"}
    assert result["stats"]["source_resource_count"] == result["stats"]["source_version_count"] == 1
    assert_contract(result, pages)


def test_reverse_wiki_dedup_uses_rerank_and_keeps_distinct_context():
    pages = {pid: page(pid, "document" if pid == "s" else "knowledge")
             for pid in ("s", "low", "high", "special", "exception")}
    for pid in ("low", "high", "special"):
        connect(pages, pid, "s", blocks=["same"])
    connect(pages, "exception", "special", "EXCEPTION_OF")
    units = [unit(pages, "s", "s", blocks=["same"], rerank_score=1),
             unit(pages, "low", "low", rerank_score=.1), unit(pages, "high", "high", rerank_score=.9)]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=1)
    assert "high" in result["requested"]
    assert "low" in result["deferred_pages"]
    assert {"special", "exception"} <= set(result["requested"])
    assert_contract(result, pages)


def test_proposed_links_are_only_navigation_and_need_current_candidate_support():
    pages = {"w": page("w", "knowledge"), "s": page("s"), "noise": page("noise")}
    connect(pages, "w", "s", blocks=["real-proposed-anchor"], origin="proposed", verification_status="PROPOSED")
    connect(pages, "w", "noise", "REQUIRES", origin="proposed", verification_status="PROPOSED")
    units = [unit(pages, "w", "w", rerank_score=1), unit(pages, "s", "s", rerank_score=.9)]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=1)
    assert set(result["requested"]) == {"w", "s"}
    assert result["anchors"]["s"] == ["real-proposed-anchor"]
    assert {edge["verification_status"] for edge in result["used_edges"]} == {"PROPOSED"}
    assert "WIKI_SOURCE_CHAIN_MISSING" in result["warnings"]
    assert "PROPOSED_RELATIONS_NAVIGATION_ONLY" in result["warnings"]
    assert "PROPOSED_RELATIONS_DEFERRED" in result["warnings"]
    assert_contract(result, pages)


def test_proposed_reverse_citation_is_not_an_independent_wiki_source_chain():
    pages = {"s": page("s"), "w": page("w", "knowledge")}
    connect(pages, "w", "s", blocks=["same"], verification_status="PROPOSED")
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "s", "s", blocks=["same"], rerank_score=1)])
    assert result["requested"] == ["s"]
    assert result["used_edges"] == []
    assert_contract(result, pages)


def test_graph_plan_hints_can_follow_an_actual_additional_relation_type():
    pages = {"a": page("a", "knowledge"), "b": page("b", "knowledge"), "s": page("s")}
    edge = connect(pages, "a", "b", "EXPLAINS")
    connect(pages, "b", "s", blocks=["first", "tail"])
    old = {"requested": ["a", "b", "s"], "used_edges": [edge], "anchors": {"s": ["tail"]}}
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "a", "a", rerank_score=1)], graph_plan=old)
    assert set(result["requested"]) == set(pages)
    assert "GRAPH_PLAN_REAL_RELATION" in result["reasons"]["b"]
    assert result["anchors"]["s"] == ["first", "tail"]
    assert_contract(result, pages)


def test_actual_legacy_query_graph_plan_is_compatible_but_cannot_replace_unit_seeds():
    pages = {"s": page("s"), "w": page("w", "knowledge")}
    connect(pages, "w", "s", blocks=["seed-block", "citation-tail"])
    legacy = plan_graph_reads("晶核交接", pages, [{"page_id": "s", "score": 1,
                               "matched_block_ids": ["seed-block"], "candidate_snippets": [
                                   {"text": "晶核交接", "block_ids": ["seed-block"]}]}])
    units = [unit(pages, "s", "seed", blocks=["seed-block", "other-stage"], rerank_score=1)]
    result = plan_evidence_reads("晶核交接", pages, units, graph_plan=legacy, max_seed_units=1)
    assert result["requested"] == ["s", "w"]
    assert set(result["anchors"]["s"]) == {"seed-block", "citation-tail", "other-stage"}
    assert "GRAPH_EDGE_NOT_REGISTERED" not in result["warnings"]
    assert_contract(result, pages)


def test_forged_graph_edges_anchors_external_ids_and_stats_are_not_copied():
    pages = {"s": page("s"), "other": page("other", "knowledge")}
    bogus = {"source": "s", "target": "other", "type": "REQUIRES", "verification_status": "VERIFIED"}
    graph = {"requested": ["OUTSIDE", "other", "s"], "used_edges": [bogus],
             "anchors": {"s": ["invented-block"], "OUTSIDE": ["secret"]},
             "warnings": ["SECRET_WARNING"], "reasons": {"s": ["SECRET_REASON"]},
             "stats": {"business_accuracy": "PASS"}}
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "s", "real", rerank_score=1)], graph_plan=graph)
    assert result["requested"] == ["s"]
    assert result["anchors"] == {"s": ["real-block"]}
    assert result["used_edges"] == []
    output = json.dumps(result)
    assert not any(secret in output for secret in ("OUTSIDE", "invented-block", "SECRET_WARNING", "SECRET_REASON"))
    assert {"GRAPH_PAGE_NOT_ADMITTED", "GRAPH_EDGE_NOT_REGISTERED", "GRAPH_ANCHOR_NOT_ADMITTED"} <= set(result["warnings"])
    assert_contract(result, pages)


def test_real_edge_on_unrelated_page_cannot_supply_an_anchor_for_selected_source():
    pages = {"s": page("s"), "unrelated": page("unrelated", "knowledge")}
    connect(pages, "unrelated", "s", blocks=["unrelated-block"])
    graph = {"requested": ["s"], "anchors": {"s": ["unrelated-block"]}}
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "s", "real", rerank_score=1)], graph_plan=graph)
    assert result["anchors"] == {"s": ["real-block"]}
    assert result["used_edges"] == []
    assert_contract(result, pages)


@pytest.mark.parametrize("field,value", [("target", "OUTSIDE"), ("target_version_id", "old-v"),
                                         ("target_resource_id", "foreign-r"), ("source_pages", ["OUTSIDE"])])
def test_relation_endpoint_validation(field, value):
    pages = {"w": page("w", "knowledge"), "s": page("s")}
    edge = connect(pages, "w", "s", blocks=["bad-anchor"])
    edge[field] = value
    pages["w"]["relations"] = [edge]
    pages["s"]["relations"] = []
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "w", "w", rerank_score=1)])
    assert result["requested"] == ["w"]
    assert result["used_edges"] == []
    assert "bad-anchor" not in json.dumps(result)
    assert_contract(result, pages)


@pytest.mark.parametrize("anchor", [{"version_id": "old-version", "block_id": "bad"},
                                   {"version_id": "v-s", "page_id": "OUTSIDE", "block_id": "bad"},
                                   {"version_id": "v-s", "resource_id": "wrong-r", "block_id": "bad"},
                                   {"version_id": "v-s", "space_id": "wrong-space", "block_id": "bad"},
                                   {"page_id": "s", "block_id": "bad"},
                                   {"version_id": "v-s", "block_id": []}])
def test_stale_or_forged_relation_anchor_rejects_the_relation_atomically(anchor):
    pages = {"w": page("w", "knowledge"), "s": page("s")}
    connect(pages, "w", "s", anchors=[{"version_id": "v-s", "block_id": "valid"}, anchor])
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "w", "w", rerank_score=1)])
    assert result["requested"] == ["w"]
    assert result["used_edges"] == []
    assert "ANCHOR_NOT_ADMITTED" in result["warnings"]
    assert_contract(result, pages)


def test_ambiguous_version_anchor_needs_explicit_matching_page_id():
    pages = {"w": page("w", "knowledge"), "s": page("s"), "alias": page("alias", version_id="v-s")}
    connect(pages, "w", "s", blocks=["ambiguous"])
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "w", "w", rerank_score=1)])
    assert result["requested"] == ["w"]
    for pid in ("w", "s"):
        pages[pid]["relations"][0]["anchors"][0]["page_id"] = "s"
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "w", "w", rerank_score=1)])
    assert result["anchors"]["s"] == ["ambiguous"]
    assert "alias" in result["deferred_pages"]
    assert_contract(result, pages)


def test_real_relation_may_be_supported_by_a_third_authorized_source():
    pages = {"a": page("a", "knowledge"), "b": page("b", "knowledge"), "s": page("s")}
    connect(pages, "a", "b", "REQUIRES", source_pages=["s"],
            anchors=[{"page_id": "s", "version_id": "v-s", "resource_id": "r-s", "block_id": "proof"}])
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "a", "a", rerank_score=1)])
    assert set(result["requested"]) == set(pages)
    assert result["anchors"]["s"] == ["proof"]
    assert_contract(result, pages)


def test_document_level_provenance_does_not_fabricate_block_or_status():
    pages = {"w": page("w", "knowledge"), "s": page("s")}
    edge = {"source": "w", "target": "s", "type": "CITES", "anchors": [{"version_id": "v-s"}]}
    pages["w"]["relations"].append(edge)
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "w", "w", rerank_score=1)])
    assert result["anchors"]["s"] == []
    assert "verification_status" not in result["used_edges"][0]
    assert "origin" not in result["used_edges"][0]
    assert "SOURCE_ANCHORS_MISSING" in result["warnings"]
    assert "WIKI_SOURCE_CHAIN_MISSING" in result["warnings"]
    assert_contract(result, pages)


def test_registered_proposed_and_reverse_edges_keep_separate_real_statuses():
    pages = {"a": page("a", "knowledge"), "b": page("b", "knowledge")}
    connect(pages, "a", "b", "REQUIRES")
    connect(pages, "a", "b", "REQUIRES", origin="proposed", verification_status="PROPOSED")
    connect(pages, "b", "a", "REQUIRES", verification_status="REVIEW_REQUIRED")
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, pid, pid, rerank_score=1) for pid in pages])
    assert len(result["used_edges"]) == 3
    assert {edge["verification_status"] for edge in result["used_edges"]} == {
        "REGISTERED_NOT_BUSINESS_VERIFIED", "PROPOSED", "REVIEW_REQUIRED"}
    assert_contract(result, pages)


def test_inline_and_canonical_metadata_do_not_become_invented_graph_edges():
    pages = {"a": page("a", "knowledge", canonical_page_id="b", links=["b"]),
             "b": page("b", "knowledge")}
    graph = {"requested": ["a", "b"], "used_edges": [
        {"source": "a", "target": "b", "type": "INLINE", "verification_status": "NAVIGATION_ONLY"}]}
    result = plan_evidence_reads("晶核交接", pages,
                                 [unit(pages, "a", "a", text="晶核交接见[[b]]", rerank_score=1)], graph_plan=graph)
    assert result["requested"] == ["a"]
    assert result["used_edges"] == []
    assert_contract(result, pages)


def test_bad_catalog_ids_are_never_admitted_or_repaired_by_guessing():
    pages = {"ok": page("ok"), "bad": page("different-id"), "empty": {"id": "empty"}}
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "ok", "ok", rerank_score=1)])
    assert result["requested"] == ["ok"]
    assert result["deferred_pages"] == []
    assert "INVALID_CATALOG_ENTRY" in result["warnings"]
    assert result["stats"]["catalog_count"] == 1


def test_body_question_input_relations_and_result_mutation_are_isolated(monkeypatch):
    pages = {"s": page("s"), "w": page("w", "knowledge")}
    text = "不含当日；除核对不符外，建议核实。\n贷：甲\n贷：乙\n" + "完整原文尾部。" * 1000
    pages["s"]["records"] = [{"text": text, "block_id": "raw"}]
    connect(pages, "w", "s", blocks=["raw"], conditions={"nested": ["应保留", "不应改写"]})
    units = [unit(pages, "s", "s", text=text, blocks=["raw"], rerank_score=1)]
    graph = {"requested": ["s"], "anchors": {"s": ["raw"]}}
    question = "晶核交接应否确认？\n保留问题原文。"
    before = copy.deepcopy((pages, units, graph))
    digest = hashlib.sha256(text.encode()).hexdigest()

    def no_io(*args, **kwargs):
        raise AssertionError("The pure router must not open a file")

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", no_io)
        result = plan_evidence_reads(question, pages, units, graph_plan=graph)
    assert (pages, units, graph) == before
    assert hashlib.sha256(pages["s"]["records"][0]["text"].encode()).hexdigest() == digest
    assert text not in json.dumps(result, ensure_ascii=False)
    assert_contract(result, pages)
    result["used_edges"][0]["conditions"]["nested"].append("result-only")
    result["used_edges"][0]["anchors"][0]["block_id"] = "result-only"
    result["anchors"]["s"].append("result-only")
    assert (pages, units, graph) == before
    assert question == "晶核交接应否确认？\n保留问题原文。"


def test_output_is_deterministic_and_word_coverage_never_means_accuracy_pass():
    pages = {"s": page("s")}
    units = [unit(pages, "s", "s", text="晶核交接核对", rerank_score=1)]
    first = plan_evidence_reads("晶核交接核对", pages, units)
    second = plan_evidence_reads("晶核交接核对", copy.deepcopy(pages), copy.deepcopy(units))
    assert first == second
    assert "PASS" not in json.dumps(first)
    assert "BUSINESS_ACCURACY_NOT_EVALUATED" in first["warnings"]
    assert_contract(first, pages)


def test_multiple_searches_each_keep_best_stage_before_one_query_fills_budget():
    pages = {"s": page("s")}
    searches = ["晶核准备阶段", "晶核接收阶段", "晶核后续核对", "晶核异常处置"]
    units = [unit(pages, "s", f"first-query-{i}", text="晶核交接准备程序", rerank_score=100 - i,
                  matched_queries=[searches[0]], query_rank=i + 1) for i in range(20)]
    units += [unit(pages, "s", f"stage-{i}", text="晶核交接程序", rerank_score=.5 - i * .01,
                   matched_queries=[searches[i]], query_rank=1) for i in range(1, 4)]
    result = plan_evidence_reads("晶核全流程如何处理？", pages, units, max_seed_units=4)
    assert result["stats"]["seed_unit_ids"] == ["first-query-0", "stage-1", "stage-2", "stage-3"]
    assert len(result["anchors"]["s"]) == 4
    assert result["stats"]["matched_query_count"] == result["stats"]["selected_query_count"] == 4
    assert result["stats"]["selected_page_count"] == 1
    assert result["stats"]["selected_section_count"] == 4
    assert result["stats"]["unselected_unit_count"] == 19
    assert_contract(result, pages)


def test_query_rank_takes_precedence_within_its_query_even_when_scores_are_not_comparable():
    pages = {"s": page("s")}
    units = [unit(pages, "s", "q1-rank2", rerank_score=100, matched_queries=["接收"], query_rank=2),
             unit(pages, "s", "q1-rank1", rerank_score=-3, matched_queries=["接收"], query_rank=1),
             unit(pages, "s", "q2-rank1", rerank_score=.2, matched_queries=["复核"], query_rank=1)]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=2)
    assert result["stats"]["seed_unit_ids"] == ["q1-rank1", "q2-rank1"]
    assert "q1-rank2-block" not in result["anchors"]["s"]
    assert_contract(result, pages)


def test_duplicate_unit_queries_merge_but_keep_each_original_query_rank():
    pages = {"s": page("s")}
    shared = unit(pages, "s", "shared", rerank_score=30, matched_queries=["准备"], query_rank=2)
    other_search = dict(shared, rerank_score=-8, matched_queries=["复核"], query_rank=1)
    units = [shared, unit(pages, "s", "prep-best", rerank_score=40, matched_queries=["准备"], query_rank=1),
             other_search, unit(pages, "s", "review-second", rerank_score=100,
                                matched_queries=["复核"], query_rank=2)]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=2)
    assert result["stats"]["seed_unit_ids"] == ["prep-best", "shared"]
    assert result["stats"]["duplicate_unit_count"] == 1
    assert result["stats"]["matched_query_count"] == result["stats"]["selected_query_count"] == 2
    assert_contract(result, pages)


def test_best_shared_unit_covers_several_queries_without_consuming_multiple_slots():
    pages = {"s": page("s")}
    units = [unit(pages, "s", "shared", rerank_score=1,
                  matched_queries=["准备", "核对", "准备"], query_rank=1),
             unit(pages, "s", "exception", rerank_score=.7, matched_queries=["例外"], query_rank=1)]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=2)
    assert result["stats"]["seed_unit_count"] == 2
    assert result["stats"]["selected_query_count"] == result["stats"]["matched_query_count"] == 3
    assert_contract(result, pages)


def test_matching_a_second_query_does_not_displace_that_queries_best_unit():
    pages = {"s": page("s")}
    shared = unit(pages, "s", "shared", rerank_score=20, matched_queries=["准备"], query_rank=1)
    units = [shared, dict(shared, rerank_score=10, matched_queries=["异常"], query_rank=10),
             unit(pages, "s", "best-exception", rerank_score=30, matched_queries=["异常"], query_rank=1)]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=2)
    assert result["stats"]["seed_unit_ids"] == ["shared", "best-exception"]
    assert result["stats"]["selected_query_count"] == 2
    assert_contract(result, pages)


def test_small_budget_reports_unselected_query_best_even_when_lower_ranked_match_was_selected():
    pages = {"s": page("s")}
    shared = unit(pages, "s", "shared", rerank_score=20, matched_queries=["准备"], query_rank=1)
    units = [shared, dict(shared, matched_queries=["异常"], query_rank=10),
             unit(pages, "s", "best-exception", rerank_score=30, matched_queries=["异常"], query_rank=1)]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=1)
    assert result["stats"]["seed_unit_ids"] == ["shared"]
    assert result["stats"]["selected_query_count"] == 1
    assert result["stats"]["unselected_query_count"] == 1
    assert "QUERY_CANDIDATES_DEFERRED" in result["warnings"]
    assert "best-exception" in result["stats"]["deferred_unit_ids"]
    assert_contract(result, pages)


def test_query_best_sections_can_share_path_words_and_original_source():
    pages = {"s": page("s"), "w1": page("w1", "knowledge"), "w2": page("w2", "knowledge")}
    connect(pages, "w1", "s", blocks=["original"])
    connect(pages, "w2", "s", blocks=["original"])
    units = [unit(pages, "w1", "early", text="晶核交接", rerank_score=1,
                  matched_queries=["晶核初始环节"], query_rank=1),
             unit(pages, "w2", "late", text="晶核交接", rerank_score=.8,
                  matched_queries=["晶核终止环节"], query_rank=1)]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=2)
    assert result["stats"]["seed_unit_ids"] == ["early", "late"]
    assert result["stats"]["source_resource_count"] == 1
    assert len(result["used_edges"]) == 2
    assert_contract(result, pages)


def test_same_path_distinct_query_best_units_survive_before_section_diversity_fill():
    pages = {"s": page("s")}
    units = [unit(pages, "s", "early", path=["操作", "处理"], rerank_score=1,
                  matched_queries=["晶核初始环节"], query_rank=1),
             unit(pages, "s", "late", path=["操作", "处理"], rerank_score=.8,
                  matched_queries=["晶核终止环节"], query_rank=1)]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=2)
    assert result["stats"]["seed_unit_count"] == 2
    assert result["stats"]["selected_section_count"] == 1
    assert set(result["anchors"]["s"]) == {"early-block", "late-block"}
    assert_contract(result, pages)


def test_query_rank_is_optional_and_whole_wiki_already_reads_other_query_units():
    pages = {"w": page("w", "knowledge")}
    units = [unit(pages, "w", "early", rerank_score=1, matched_queries=["准备"]),
             unit(pages, "w", "late", rerank_score=.8, matched_queries=["终止"])]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=1)
    assert result["stats"]["seed_unit_count"] == 1
    assert result["stats"]["selected_query_count"] == 2
    assert_contract(result, pages)


def test_unreranked_fallback_can_use_actual_search_text_but_reports_its_limit():
    pages = {"s": page("s")}
    units = [unit(pages, "s", "archive", text="归档阶段应核对移交清单。", matched_queries=["归档阶段"])]
    result = plan_evidence_reads("晶核完整生命周期？", pages, units)
    assert result["requested"] == ["s"]
    assert "LEXICAL_FALLBACK_NOT_SEMANTIC_VALIDATION" in result["warnings"]
    assert_contract(result, pages)


@pytest.mark.parametrize("field,value", [("matched_queries", "string"), ("matched_queries", [""]),
                                         ("matched_queries", [None]), ("matched_queries", [{}]),
                                         ("query_rank", 0), ("query_rank", -1), ("query_rank", True),
                                         ("query_rank", "1"), ("query_rank", 1.5)])
def test_invalid_query_metadata_cannot_make_its_unit_an_anchor(field, value):
    pages = {"s": page("s")}
    candidate = unit(pages, "s", "bad", blocks=["not-validated"], rerank_score=1)
    candidate[field] = value
    result = plan_evidence_reads("晶核交接", pages, [candidate])
    assert result["requested"] == []
    assert "not-validated" not in json.dumps(result)
    assert result["stats"]["rejected_unit_count"] == 1
    assert_contract(result, pages)


@pytest.mark.parametrize("metadata", [{"verification": "NOT_CHECKED"}, {"verified": False},
                                      {"identity_verified": False}, {"source_spans": []}])
def test_explicit_unverified_or_incomplete_unit_never_supplies_anchors(metadata):
    pages = {"s": page("s")}
    candidate = unit(pages, "s", "bad", blocks=["not-validated"], rerank_score=1, **metadata)
    result = plan_evidence_reads("晶核交接", pages, [candidate])
    assert result["anchors"] == {}
    assert "UNIT_VERIFICATION_MISMATCH" in result["warnings"]
    assert "not-validated" not in json.dumps(result)
    assert_contract(result, pages)


def checked_span_unit(pages):
    text = "晶核交接核对"
    return unit(pages, "s", "checked", text=text, blocks=["checked-block"], rerank_score=1,
                verification="CURRENT_DB_BLOCK_SLICES", full_text_verified=False, is_answer_evidence=False,
                source_spans=[{"block_id": "checked-block", "version_id": "v-s", "resource_id": "r-s",
                               "ordinal": 1, "content_sha256": "a" * 64, "start": 5, "end": 5 + len(text),
                               "text_start": 0, "text_end": len(text)}])


def test_caller_checked_exact_span_is_valid_without_claiming_full_source_read():
    pages = {"s": page("s")}
    result = plan_evidence_reads("晶核交接", pages, [checked_span_unit(pages)])
    assert result["anchors"] == {"s": ["checked-block"]}
    assert_contract(result, pages)


@pytest.mark.parametrize("field,value", [("block_id", "injected-block"), ("version_id", "old-version"),
                                         ("resource_id", "wrong-resource"), ("ordinal", -1),
                                         ("content_sha256", "not-a-hash"), ("start", -1),
                                         ("text_end", 999), ("text_start", 1)])
def test_inconsistent_span_cannot_pass_valid_unit_identity_and_enter_anchor(field, value):
    pages = {"s": page("s")}
    candidate = checked_span_unit(pages)
    candidate["source_spans"][0][field] = value
    result = plan_evidence_reads("晶核交接", pages, [candidate])
    assert result["anchors"] == {}
    assert "UNIT_VERIFICATION_MISMATCH" in result["warnings"]
    assert_contract(result, pages)


@pytest.mark.parametrize("bad_units", [None, {}, [None], ["bad"]])
def test_bad_unit_container_or_rows_are_not_a_crash_or_discovery_fallback(bad_units):
    pages = {"s": page("s")}
    result = plan_evidence_reads("晶核交接", pages, bad_units)
    assert result["requested"] == []
    assert_contract(result, pages)


def test_huge_or_nonfinite_scores_and_non_json_relations_fail_closed():
    pages = {"s": page("s"), "w": page("w", "knowledge")}
    pages["w"]["relations"] = [{"source": "w", "target": "s", "type": "CITES", "conditions": {"set"}}]
    result = plan_evidence_reads("晶核交接", pages,
                                 [unit(pages, "s", "bad", score=10 ** 1000), unit(pages, "w", "w", rerank_score=1)])
    assert result["requested"] == ["w"]
    assert "INVALID_RELATION" in result["warnings"]
    assert "INVALID_UNIT" in result["warnings"]
    assert_contract(result, pages)


def test_unlocated_real_context_uses_only_checked_units_and_each_query_best():
    pages = {"w": page("w", "knowledge"), "s": page("s")}
    connect(pages, "w", "s", "REQUIRES", anchors=[])
    units = [unit(pages, "w", "seed", rerank_score=1),
             unit(pages, "s", "first-stage", rerank_score=.9, matched_queries=["准备"], query_rank=1),
             unit(pages, "s", "later-stage", rerank_score=.8, matched_queries=["终止"], query_rank=1),
             unit(pages, "s", "bad", blocks=["injected"], rerank_score=10, verified=False)]
    # Query-aware seeds would otherwise prefer s. The Wiki's query comes first.
    units[0]["matched_queries"] = ["总览"]
    units[0]["query_rank"] = 1
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=1)
    assert result["stats"]["seed_unit_ids"] == ["seed"]
    assert set(result["anchors"]["s"]) == {"first-stage-block", "later-stage-block"}
    assert "CONTEXT_VERIFIED_UNIT_ANCHORS" in result["reasons"]["s"]
    assert "injected" not in json.dumps(result)
    assert_contract(result, pages)


def test_graph_hint_cannot_upgrade_proposed_status_or_reverse_registered_direction():
    pages = {"w": page("w", "knowledge"), "s": page("s")}
    edge = connect(pages, "w", "s", "EXPLAINS", verification_status="PROPOSED")
    graph = {"requested": ["w", "s"], "used_edges": [dict(edge, verification_status="VERIFIED"),
                                                         dict(edge, source="s", target="w")]}
    result = plan_evidence_reads("晶核交接", pages, [unit(pages, "w", "w", rerank_score=1)], graph_plan=graph)
    assert result["requested"] == ["w"]
    assert result["used_edges"] == []
    assert "GRAPH_EDGE_NOT_REGISTERED" in result["warnings"]
    assert_contract(result, pages)


def test_search_without_eligible_candidates_is_still_reported_as_unselected():
    pages = {"s": page("s")}
    units = [unit(pages, "s", "unrelated", text="海盐仓储。", matched_queries=["晶核核对"])]
    result = plan_evidence_reads("晶核交接", pages, units)
    assert result["requested"] == []
    assert result["stats"]["matched_query_count"] == result["stats"]["unselected_query_count"] == 1
    assert "QUERY_CANDIDATES_DEFERRED" in result["warnings"]
    assert_contract(result, pages)


def test_span_metadata_with_invalid_extra_values_cannot_break_identity_validation():
    pages = {"s": page("s")}
    candidate = checked_span_unit(pages)
    candidate["source_spans"][0]["unsupported"] = {"a-set"}
    result = plan_evidence_reads("晶核交接", pages, [candidate])
    assert result["anchors"] == {}
    assert "UNIT_VERIFICATION_MISMATCH" in result["warnings"]
    assert_contract(result, pages)


def test_both_directions_of_real_revision_navigation_preserve_versions_without_inventing_effect():
    pages = {"old": page("old", resource_id="same-resource"),
             "new": page("new", resource_id="same-resource")}
    connect(pages, "new", "old", "SUPERSEDES", blocks=["historical"], conditions={"after": "指定日期"})
    units = [unit(pages, "old", "old-u", blocks=["historical"], rerank_score=1),
             unit(pages, "new", "new-u", rerank_score=.5)]
    result = plan_evidence_reads("晶核交接", pages, units, max_seed_units=1)
    assert set(result["requested"]) == {"old", "new"}
    assert result["anchors"]["new"] == ["new-u-block"]
    assert result["used_edges"][0]["source"] == "new"
    assert result["used_edges"][0]["target"] == "old"
    assert result["stats"]["source_resource_count"] == 1
    assert result["stats"]["source_version_count"] == 2
    assert_contract(result, pages)
