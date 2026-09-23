import copy
from types import SimpleNamespace

import pytest

from fund_kb.evidence_context import context_plan, order_context_groups


def page(pid, title):
    return {"title": title, "kind": "document", "records": [{"version_id": "v" + pid,
        "block_id": "b" + pid, "evidence_id": "E" + pid, "content_sha256": "a" * 64, "text": "SOURCE_" + pid}]}


def reference(title="规则乙", locator="第三条"):
    return {"text": f"《{title}》{locator}", "target_title": title, "locator": locator, "status": "external", "source_spans": []}


def test_exact_cross_source_request_only_resolves_after_authorized_read():
    pages = {"W1": page("1", "规则甲"), "W2": page("2", "规则乙")}
    pages["W1"]["reading_dependencies"] = [reference()]
    plan = context_plan(pages, {"W1"})
    assert plan["requests"] == {"W2": {"第三条"}}
    assert plan["report"]["status"] == "PENDING"
    pages["W2"]["incoming_context"] = {"第三条": {"status": "resolved", "target_section_ids": ["s1"]}}
    plan = context_plan(pages, set(pages))
    assert not plan["requests"] and plan["report"]["resolved_count"] == 1
    assert plan["report"]["professional_completeness"] == "NOT_EVALUATED"
    assert context_plan(pages, set(pages), {"W2"})["report"]["gap_count"] == 1


@pytest.mark.parametrize("targets,locator,status", [([], "第三条", "not_in_authorized_catalog"),
    (["规则乙", "规则乙"], "第三条", "ambiguous"), (["规则乙"], "", "locator_required"),
    (["新规则乙"], "第三条", "not_in_authorized_catalog")])
def test_missing_ambiguous_or_no_locator_never_read_whole_book(targets, locator, status):
    pages = {"W1": page("1", "规则甲"), **{f"W{i+2}": page(str(i+2), title) for i, title in enumerate(targets)}}
    pages["W1"]["reading_dependencies"] = [reference(locator=locator)]
    plan = context_plan(pages, {"W1"})
    assert not plan["requests"] and plan["report"]["references"][0]["status"] == status
    assert len(plan["searches"]) == (1 if status == "not_in_authorized_catalog" and locator else 0)


def test_bundle_rerank_keeps_low_score_dependency_and_all_original_evidence():
    pages = {f"W{i}": page(str(i), f"来源{i}") for i in range(1, 4)}
    before = copy.deepcopy(pages)
    seen = []
    def rerank(query, texts):
        seen.append(texts)
        assert "SOURCE_1" in texts[0] and "SOURCE_2" not in texts[0]
        return [4. if "SOURCE_1" in text else -100. if "SOURCE_2" in text else 2. for text in texts]
    vector = SimpleNamespace(rerank=rerank, settings=SimpleNamespace(reranker_model="Qwen/Qwen3-Reranker-4B"))
    cache = {}
    order, receipt = order_context_groups("问题", pages, set(pages), [("W1", "W2")], vector, cache)
    assert order == ["W1", "W2", "W3"] and receipt["dropped_pages"] == 0
    assert pages == before and receipt["status"] == "scored"
    assert order_context_groups("问题", pages, set(pages), [("W1", "W2")], vector, cache)[1]["cache_hit"]
    assert len(seen) == 1
    pages["W1"]["records"][0]["text"] += " changed"
    order_context_groups("问题", pages, set(pages), [("W1", "W2")], vector, cache)
    assert len(seen) == 2


@pytest.mark.parametrize("scores", [[1.], [float("nan"), 1.], [True, 1.], None])
def test_invalid_or_disabled_rerank_preserves_every_source(scores):
    pages = {"W1": page("1", "甲"), "W2": page("2", "乙")}
    vector = SimpleNamespace(rerank=lambda *args: scores)
    order, receipt = order_context_groups("问", pages, set(pages), [("W1", "W2"), ("W2", "W1")], vector, {})
    assert order == list(pages) and receipt["dropped_pages"] == 0
    assert receipt["status"] == ("not_enabled" if scores is None else "unavailable_preserved_order")


def test_shared_dependency_never_boosts_unrelated_root_and_each_source_is_scored_once():
    pages = {f"W{i}": page(str(i), f"来源{i}") for i in range(1, 5)}
    calls = []
    scores = {"1": -10., "2": 5., "3": 10., "4": 0.}
    def rerank(query, texts):
        calls.append(list(texts))
        assert all(text.count("SOURCE_") == 1 for text in texts)
        return [next(score for key, score in scores.items() if "SOURCE_" + key in text) for text in texts]
    vector = SimpleNamespace(rerank=rerank)
    cache = {}
    order, report = order_context_groups("问题", pages, set(pages), [("W1", "W3"), ("W2", "W3")], vector, cache)
    assert order == ["W3", "W2", "W4", "W1"]
    assert report["groups"][0] == ["W1", "W3"] and report["groups"][1] == ["W2", "W3"]
    assert report["scored_sources"] == 4 and report["dropped_pages"] == 0
    pages["W4"]["records"][0]["text"] += " change"
    _, report = order_context_groups("问题", pages, set(pages), [("W1", "W3"), ("W2", "W3")], vector, cache)
    assert report["scored_sources"] == 1 and report["reused_source_scores"] == 3 and len(calls[-1]) == 1
    # A different question or model identity must not reuse prior scores.
    _, report = order_context_groups("另一问题", pages, set(pages), [], vector, cache)
    assert report["scored_sources"] == 4
