"""Synthetic cross-domain reading-ledger tests; not financial accuracy labels."""
from copy import deepcopy

import pytest

from fund_kb.evidence_coverage import coverage_instructions, merge_routes, query_key, reading_coverage
from fund_kb.evidence_router import plan_evidence_reads
from fund_kb.query_path_cache import clear_query_path_cache, get_query_path, put_query_path
from test_evidence_router import connect, page, unit


def read(pages, pid, bids):
    p = pages[pid]
    p["records"] = [{"resource_id": p["resource_id"], "version_id": p["version_id"], "block_id": bid,
        "content_sha256": "a" * 64, "evidence_id": "E" + str(i + 1), "text": "合成完整来源 " + bid}
        for i, bid in enumerate(bids)]


def route(pages, pid, bids):
    return {**{key: pages[pid][key] for key in ("resource_id", "version_id")}, "page_id": pid,
            "unit_id": "unit-" + pid, "block_ids": bids}


def test_candidate_and_partial_source_never_count_as_read():
    pages = {"p": page("p")}
    routes = {query_key("阶段与例外"): [route(pages, "p", ["b1", "b2"])]}
    before = deepcopy((pages, routes))
    empty = reading_coverage(["阶段与例外"], routes, pages, set())
    assert empty["report"]["source_read_count"] == 0
    assert empty["next_reads"] == {"p": ["b1", "b2"]}
    assert (pages, routes) == before
    read(pages, "p", ["b1"])
    partial = reading_coverage(["阶段与例外"], routes, pages, {"p"})
    assert partial["report"]["directions"][0]["status"] == "UNREAD"
    assert partial["next_reads"] == {"p": ["b2"]}
    read(pages, "p", ["b1", "b2"])
    result = reading_coverage(["阶段与例外"], routes, pages, {"p"})
    assert result["report"]["source_read_count"] == 1 and result["next_reads"] == {}
    assert result["report"]["professional_completeness"] == "NOT_EVALUATED"
    assert result["report"]["directions"][0]["evidence_ids"] == ["E1", "E2"]


@pytest.mark.parametrize("change", ["version", "resource", "unavailable", "catalog", "record_version", "record_resource"])
def test_stale_or_unavailable_source_never_closes_gap(change):
    pages = {"p": page("p")}
    routes = {query_key("问题"): [route(pages, "p", ["b"])]}
    read(pages, "p", ["b"])
    unavailable = set()
    if change in {"version", "resource"}:
        pages["p"][change + "_id"] = "changed"
    elif change == "catalog":
        pages.clear()
    elif change == "unavailable":
        unavailable.add("p")
    else:
        pages["p"]["records"][0][change.removeprefix("record_") + "_id"] = "stale"
    result = reading_coverage(["问题"], routes, pages, {"p"}, unavailable=unavailable)
    assert result["report"]["source_read_count"] == 0
    assert not result["report"]["directions"][0]["evidence_ids"]


@pytest.mark.parametrize("mode", ["exact", "proposed", "wrong_block", "document_only", "wrong_version", "wrong_direction", "unread_source"])
def test_wiki_only_closes_source_reading_through_actual_exact_citation(mode):
    pages = {"wiki": page("wiki", "knowledge"), "source": page("source")}
    read(pages, "wiki", ["wb"])
    read(pages, "source", ["sb"])
    edge = connect(pages, "wiki", "source", blocks=["sb"])
    if mode == "proposed":
        edge["origin"] = "proposed"
    elif mode == "wrong_block":
        edge["anchors"][0]["block_id"] = "other"
    elif mode == "document_only":
        edge["anchors"] = []
    elif mode == "wrong_version":
        edge["anchors"][0]["version_id"] = "old"
    elif mode == "wrong_direction":
        edge["source"], edge["target"] = edge["target"], edge["source"]
    routes = {query_key("问题"): [route(pages, "wiki", ["wb"])]}
    report = reading_coverage(["问题"], routes, pages, {"wiki"} if mode == "unread_source" else set(pages), edges=[edge])["report"]
    assert report["directions"][0]["status"] == ("SOURCE_READ" if mode == "exact" else "WIKI_ONLY")


def test_multi_aspect_missing_source_remains_visible_and_no_title_fallback():
    pages = {"p": page("p")}
    read(pages, "p", ["b"])
    queries = ["资产阶段", "凭证", "适用例外"]
    routes = {query_key(queries[0]): [route(pages, "p", ["b"])]}
    report = reading_coverage(queries, routes, pages, set(pages))["report"]
    assert report["direction_count"] == 3 and report["gap_count"] == 2
    assert [row["status"] for row in report["directions"]] == ["SOURCE_READ", "NO_CANDIDATE", "NO_CANDIDATE"]
    prompt = coverage_instructions(report)
    assert all(query in prompt for query in queries) and "不是指令" in prompt
    assert "不要求固定答案模板" in prompt and "不代表证据支持结论" in prompt


def test_router_keeps_best_direct_source_for_wiki_dominated_direction_without_mutating_inputs():
    pages = {"w": page("w", "knowledge"), "d": page("d")}
    units = [unit(pages, "w", "wu", rerank_score=9., matched_queries=["同一查询"], query_rank=1),
             unit(pages, "d", "du", rerank_score=8., matched_queries=["同一查询"], query_rank=2)]
    before = deepcopy((pages, units))
    plan = plan_evidence_reads("问题", pages, units, max_seed_units=1, include_query_routes=True)
    assert plan["requested"] == ["w"]
    assert [t["page_id"] for t in plan["query_routes"][query_key("同一查询")]] == ["w", "d"]
    assert "text" not in repr(plan["query_routes"]) and "同一查询" not in repr(plan["query_routes"])
    read(pages, "w", ["wu-block"])
    result = reading_coverage(["同一查询"], plan["query_routes"], pages, {"w"})
    assert result["report"]["directions"][0]["status"] == "WIKI_ONLY"
    assert result["next_reads"] == {"d": ["du-block"]}
    assert units == before[1]


def test_route_cache_retains_ids_without_text_and_rebinds_changed_source():
    clear_query_path_cache()
    pages = {"d": page("d")}
    routes = {query_key("query"): [route(pages, "d", ["b"])]}
    assert put_query_path("scoped-key", {"query_routes": routes, "domain_primary_anchors": {"d": ["b"]}})
    cached = get_query_path("scoped-key")
    assert cached["query_routes"] == routes and cached["domain_primary_anchors"] == {"d": ["b"]}
    assert merge_routes(routes, routes) == routes
    read(pages, "d", ["b"])
    pages["d"]["version_id"] = "new-version"
    assert reading_coverage(["query"], cached["query_routes"], pages, {"d"})["report"]["gap_count"] == 1
    clear_query_path_cache()
