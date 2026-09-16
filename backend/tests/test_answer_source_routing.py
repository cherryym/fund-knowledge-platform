"""Synthetic source routing/adjacency/citation tests. No DB, model or network."""
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from fund_kb.ai import AnswerValidationError, generate_answer
from fund_kb.answer_prompt import build_answer_system_prompt
from fund_kb.answer_retrieval import context_manifest, question_plan, retrieve_answer_context
from fund_kb.ingestion import text_sha256


def uid():
    return str(uuid4())


def rows(title, texts, kind="document"):
    rid, vid = uid(), uid()
    return [{"resource_id": rid, "version_id": vid, "block_id": uid(), "ordinal": i,
        "title": title, "text": text, "block_type": "paragraph", "data": {"text": text},
        "content_sha256": text_sha256(text), "kind": kind, "knowledge_type": "source" if kind == "document" else "rule",
        "state": "IN_REVIEW", "legal_status": "UNKNOWN", "source_verified": False,
        "evidence_scope": "reference", "locator": {"source_page": 8, "label": "合成来源"},
        "applicability": {}, "required_facts": []} for i, text in enumerate(texts)]


def manual():
    return rows("证券投资基金会计核算操作实务手册", [
        "第二章 股票投资业务", "三、主要账务处理", "（一）初始确认",
        "买入股票资产时，于交易日按公允价值进行初始计量，取",
        "得时发生的相关交易费用计入当期损益。", "（二）后续计量",
        "在基金估值日，基金投资的股票以公允价值计量，按当日",
        "与上一日公允价值的差额计入公允价值变动损益。",
        "（三）终止确认", "当基金终止确认时，核对相关条件。"])


def test_purchase_stock_routes_to_manual_and_complete_initial_and_subsequent_passages():
    source = manual()
    noise = rows("证券交易所股票交易规则", ["买入股票股票股票如何交易股票。"] * 600)
    before = copy.deepcopy(source)
    selected, manifest = retrieve_answer_context("买入股票应该如何估值", noise + source)
    assert manifest["plan"]["intent"] == "fund_accounting_practice"
    assert manifest["plan"]["primary_version_id"] == source[0]["version_id"]
    assert manifest["plan"]["uncovered_dimensions"] == []
    assert {row["ordinal"] for row in selected if row["version_id"] == source[0]["version_id"]} == {3, 4, 6, 7}
    assert all("股票投资业务" in " / ".join(row["section_path"]) for row in selected)
    assert all(row["retrieval_role"] == "primary" for row in selected)
    assert sum(edge["type"] == "CONTINUES" for edge in manifest["relations"]) == 2
    assert not any(edge["type"] == "CITES" for edge in manifest["relations"])
    assert source == before


def test_actual_wiki_citation_is_followed_but_keyword_similarity_is_not_a_citation():
    source = manual()
    linked = rows("股票买入与后续估值衔接", ["初始计量与后续计量属于不同阶段。"], "knowledge")
    linked[0]["source_citations"] = [{"version_id": source[3]["version_id"], "block_id": source[3]["block_id"]}]
    unrelated = rows("股票相关知识", ["买入股票应该如何估值。"], "knowledge")
    selected, manifest = retrieve_answer_context("买入股票应该如何估值", source + linked + unrelated)
    assert linked[0]["block_id"] in {row["block_id"] for row in selected}
    assert unrelated[0]["block_id"] not in {row["block_id"] for row in selected}
    assert [edge["from"] for edge in manifest["relations"] if edge["type"] == "CITES"] == [linked[0]["block_id"]]


def test_validity_and_specialist_questions_do_not_always_prefer_manual():
    source = manual()
    law = rows("证券投资基金估值业务指导意见", ["本规范的适用日期与效力需要核对。"])
    _, manifest = retrieve_answer_context("证券投资基金估值业务指导意见是否仍然有效", source + law)
    assert manifest["plan"]["intent"] == "rule_validity"
    assert manifest["plan"]["primary_version_id"] == law[0]["version_id"]
    specialist = rows("流通受限股票估值指引", ["AAP模型与流动性折扣参数需要核对。"])
    _, manifest = retrieve_answer_context("流通受限股票AAP模型如何计算", source + specialist)
    assert manifest["plan"]["intent"] == "specialist_valuation"
    assert manifest["plan"]["primary_version_id"] == specialist[0]["version_id"]


def test_missing_manual_is_a_gap_not_a_claim_of_reading_an_inaccessible_document():
    public = rows("基金股票基础资料", ["买入股票需要核对交易事实。"])
    _, manifest = retrieve_answer_context("买入股票应该如何估值", public)
    assert manifest["plan"]["gaps"]
    assert all(item["title"] == "基金股票基础资料" for item in manifest["sources"])


def test_toc_and_other_asset_chapters_do_not_replace_the_stock_chapter():
    source = rows("证券投资基金会计核算操作实务手册", [
        "第二章 股票投资业务..................................7", "第三章 债券投资业务",
        "买入股票资产时，于交易日按公允价值进行初始计量。",
        "第二章 股票投资业务", "（一）初始确认", "买入股票资产时，按公允价值进行初始计量。",
        "（二）后续计量", "在基金估值日，以公允价值进行后续计量。"])
    chosen, _ = retrieve_answer_context("买入股票应该如何估值", source)
    assert {row["ordinal"] for row in chosen} == {5, 7}


def test_context_manifest_pruning_never_claims_removed_coverage():
    chosen, context = retrieve_answer_context("买入股票应该如何估值", manual())
    kept = [row for row in chosen if row["answer_dimension"] == "initial_measurement"]
    pruned = context_manifest(kept, context["plan"])
    assert pruned["plan"]["uncovered_dimensions"] == ["subsequent_measurement"]
    assert {bid for group in pruned["passages"] for bid in group["ordered_block_ids"]} == {row["block_id"] for row in kept}


def test_full_prompt_and_continuation_reach_the_synthesis_callback():
    selected, manifest = retrieve_answer_context("买入股票应该如何估值", manual())
    diagnostics, calls = {}, []
    def complete(payload):
        calls.append(payload)
        body = json.loads(payload["messages"][1]["content"])
        assert payload["messages"][0]["content"] == build_answer_system_prompt("reference")
        assert body["knowledge_context"]["plan"]["primary_version_id"] == selected[0]["version_id"]
        assert len(body["evidence"]) == 4  # Both non-matching continuation lines survive the second filter.
        assert body["output_skeleton"]["claims"] == []
        answer = copy.deepcopy(body["output_skeleton"])
        answer["summary"] = "资料辅助答疑：需要区分买入初始计量与持有期间的后续计量，交易费用单独考虑。"
        answer["claims"] = [{"id": "C1", "text": "买入阶段按公允价值初始计量，相关交易费用进入当期损益。",
                              "evidence_ids": [c["id"] for c in answer["citations"][:2]]}]
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer, ensure_ascii=False)}}]}
    answer = generate_answer("买入股票应该如何估值", "answer", {}, selected,
        SimpleNamespace(llm_provider="http", llm_model="synthetic", llm_base_url=""), uid(),
        completion_client=complete, answer_scope="reference", diagnostics=diagnostics, source_analysis=manifest)
    assert len(calls) == 1 and answer["status"] == "ANSWERED"
    assert "已降级" not in " ".join(answer["limitations"])
    assert diagnostics["request_manifest"]["prompt_version"] == "fund-accounting-v2"
    assert diagnostics["request_manifest"]["request_utf8_bytes"] <= 60000


def test_question_plan_does_not_treat_metadata_as_legal_authority():
    plan = question_plan("买入债券如何入账", {"asset_type": "债券"})
    assert plan["intent"] == "fund_accounting_practice" and "债券" in plan["assets"]
    assert "不表示效力更高" in plan["authority_note"]


def test_rejected_synthesis_is_not_returned_as_a_successful_excerpt_list():
    selected, manifest = retrieve_answer_context("买入股票应该如何估值", manual())
    diagnostics = {}
    def invalid(_payload):
        return {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
    with pytest.raises(AnswerValidationError, match="SYNTHESIS_OUTPUT_REJECTED"):
        generate_answer("买入股票应该如何估值", "answer", {}, selected,
            SimpleNamespace(llm_provider="http", llm_model="synthetic", llm_base_url=""), uid(),
            completion_client=invalid, answer_scope="reference", diagnostics=diagnostics, source_analysis=manifest)
    assert diagnostics["code"] == "ANSWER_MODE_MISMATCH"
    assert diagnostics["request_manifest"]["evidence_blocks"] == 4
