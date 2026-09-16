from __future__ import annotations

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from fund_kb import ai, ai_transport
from fund_kb.ai import AnswerValidationError, compile_knowledge, generate_answer, validate_answer
from fund_kb.ai_transport import ProviderError
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.retrieval import EmbeddingProvider, VectorIndex, rank_evidence, tokenize


def settings(tmp_path, **kw):
    defaults = {"app_env": "development", "qdrant_path": tmp_path / "qdrant", "qdrant_url": None,
                "qdrant_api_key": None, "embedding_mode": "hashing", "embedding_model": "development",
                "embedding_dimensions": 384, "llm_provider": "evidence", "llm_base_url": None,
                "llm_model": None, "llm_api_key": None}
    return SimpleNamespace(**(defaults | kw))


def record(text="核对基金合同版本与业务日期。", version_id=None, **kw):
    result = {"resource_id": str(uuid4()), "version_id": version_id or str(uuid4()), "block_id": str(uuid4()),
              "title": "基金运营资料", "text": text, "locator": {"label": "第2页", "source_page": 2},
              "block_type": "paragraph", "data": {"text": text}, "knowledge_type": "source",
              "required_facts": [], "applicability": {}, "source_verified": True, "ordinal": 0}
    result.update(kw)
    result["content_sha256"] = text_sha256(result["text"])
    return result


def step_record(version_id=None, **kw):
    data = {"action": "核对基金合同版本及适用日期", "owner_role": "运营复核岗",
            "output": "基金合同版本核对记录", "verification": "记录版本号与已核验原件一致"}
    data.update(kw.pop("data", {}))
    return record(block_text({"block_type": "step", "data": data}), version_id,
                  block_type="step", data=data, knowledge_type=kw.pop("knowledge_type", "sop"),
                  version_step_ordinals=kw.pop("version_step_ordinals", [kw.get("ordinal", 0)]), **kw)


def test_chinese_terms_and_exact_numbers_are_preserved():
    tokens = tokenize("核对基金管理费与第12条 ABC-123")
    assert "基金" in tokens and "管理费" in tokens and "abc-123" in tokens


def test_real_qdrant_local_vector_rank_filter_delete_and_reopen(tmp_path):
    index = VectorIndex(settings(tmp_path))
    a = record("基金管理费按合同核对费用计提。")
    b = record("仓库库存盘点入库数量。")
    secret = record("基金管理费最高匹配的受限资料。")
    index.upsert([a, b, secret])
    hits = index.search("基金管理费", [a["version_id"], b["version_id"]], limit=2)
    assert hits[0]["block_id"] == a["block_id"]
    assert secret["block_id"] not in {h["block_id"] for h in hits}
    assert index.search("基金", []) == []
    assert hits[0]["block_type"] == "paragraph" and hits[0]["data"] == a["data"]
    index.upsert([a])
    assert index.status()["indexed_blocks"] == 3  # idempotent point IDs
    index.close()
    reopened = VectorIndex(settings(tmp_path))
    assert reopened.search("基金管理费", [a["version_id"]])[0]["block_id"] == a["block_id"]
    reopened.delete_versions([a["version_id"]])
    assert reopened.search("基金管理费", [a["version_id"]]) == []
    assert reopened.status()["indexed_blocks"] == 2
    reopened.close()


def test_empty_acl_performs_no_embedding_or_database_query(tmp_path, monkeypatch):
    index = VectorIndex(settings(tmp_path))
    monkeypatch.setattr(index.embedding, "embed", lambda *a, **kw: pytest.fail("must not embed"))
    assert index.search("受限资料", []) == []
    index.close()


def test_qdrant_status_distinguishes_engine_and_semantic_evidence(tmp_path):
    index = VectorIndex(settings(tmp_path))
    result = index.status()
    assert result["backend"] == "qdrant" and result["database_kind"] == "QdrantLocal"
    assert result["development_only"] is True
    assert result["semantic_effectiveness"] == "NOT_EVALUATED"
    assert result["available"] is True
    index.close()
    assert index.status()["status"] == "CLOSED"


def test_shared_local_instances_and_api_worker_threads(tmp_path):
    first, second = VectorIndex(settings(tmp_path)), VectorIndex(settings(tmp_path))
    assert first._shared is second._shared
    records = [record(f"基金合同版本核对记录{i}。") for i in range(10)]
    def write_and_read(r):
        first.upsert([r])
        return second.search("基金合同版本", [r["version_id"]])[0]["block_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(write_and_read, records)) == {r["block_id"] for r in records}
    first.close()
    assert second.status()["indexed_blocks"] == 10
    second.close()


def test_hashing_is_deterministic_and_not_allowed_in_production(tmp_path):
    a = EmbeddingProvider(settings(tmp_path))
    b = EmbeddingProvider(settings(tmp_path))
    assert a.embed(["基金管理费核对"])[0] == b.embed(["基金管理费核对"])[0]
    assert sum(v * v for v in a.embed(["基金管理费核对"])[0]) == pytest.approx(1)
    with pytest.raises(ValueError, match="DEVELOPMENT_EMBEDDING_FORBIDDEN"):
        VectorIndex(settings(tmp_path, app_env="production"))


def test_different_embedding_dimension_uses_different_collection(tmp_path):
    one = VectorIndex(settings(tmp_path, embedding_dimensions=384))
    two = VectorIndex(settings(tmp_path, embedding_dimensions=256))
    assert one.collection != two.collection
    one.close()
    two.close()


def test_delete_versions_covers_previous_embedding_generations(tmp_path):
    old = VectorIndex(settings(tmp_path, embedding_dimensions=256))
    current = VectorIndex(settings(tmp_path, embedding_dimensions=384))
    source = record()
    old.upsert([source])
    current.upsert([source])
    current.delete_versions([source["version_id"]])
    assert old.search("基金合同", [source["version_id"]]) == []
    assert current.search("基金合同", [source["version_id"]]) == []
    old.close()
    current.close()


def test_real_qdrant_cosine_matches_independent_known_vectors(tmp_path, monkeypatch):
    from qdrant_client import models
    index = VectorIndex(settings(tmp_path, embedding_dimensions=8))
    a, b, secret = record(), record(), record()
    vectors = [[1.0, 0.0] + [0.0] * 6, [0.8, 0.6] + [0.0] * 6, [1.0, 0.0] + [0.0] * 6]
    with index._shared.lock:
        index._ensure(create=True)
        index._shared.client.upsert(index.collection, points=[models.PointStruct(id=i + 1, vector=v, payload=r)
                                   for i, (v, r) in enumerate(zip(vectors, [a, b, secret], strict=True))], wait=True)
    monkeypatch.setattr(index.embedding, "embed", lambda *a, **kw: [[1.0, 0.0] + [0.0] * 6])
    hits = index.search("fixture vector", [a["version_id"], b["version_id"]])
    assert [h["block_id"] for h in hits] == [a["block_id"], b["block_id"]]
    assert [h["score"] for h in hits] == pytest.approx([1.0, 0.8], abs=1e-6)
    index.close()


def test_bad_hash_or_dimensions_rejected(tmp_path, monkeypatch):
    index = VectorIndex(settings(tmp_path))
    bad = record()
    bad["content_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="CONTENT_HASH_MISMATCH"):
        index.upsert([bad])
    index.close()
    provider = EmbeddingProvider(settings(tmp_path, embedding_mode="http", embedding_base_url="https://example.invalid/v1",
                                           embedding_model="institution-embedding"))
    monkeypatch.setattr("fund_kb.retrieval.post_json", lambda *a, **kw: {"data": [{"index": 0, "embedding": [1.0, 2.0]}]})
    with pytest.raises(ProviderError, match="DIMENSION_MISMATCH"):
        provider.embed(["基金合同"])


def test_fastembed_missing_local_files_never_starts_download(tmp_path):
    provider = EmbeddingProvider(settings(tmp_path, embedding_mode="fastembed", embedding_model="multilingual-model"))
    with pytest.raises(ProviderError, match="LOCAL_MODEL_UNAVAILABLE"):
        provider.embed(["基金合同"])


def test_rank_fusion_returns_only_current_eligible_records(tmp_path):
    index = VectorIndex(settings(tmp_path))
    good, secret = record("核对基金管理费计提基数。"), record("基金管理费受限条款。")
    unrelated = record("仓库库存盘点。", title="仓储资料")
    index.upsert([good, secret, unrelated])
    ranked = rank_evidence("基金管理费", [good, unrelated], index)
    assert ranked[0]["block_id"] == good["block_id"]
    assert ranked[0]["retrieval_channels"] == ["lexical", "vector"]
    assert all(r["block_id"] != secret["block_id"] for r in ranked)
    changed = {**good, "content_sha256": "f" * 64}
    assert "vector" not in rank_evidence("基金管理费", [changed], index)[0]["retrieval_channels"]
    index.close()


def test_lexical_search_keeps_working_when_vector_channel_fails():
    good = record("基金管理费计提。")
    class FailedIndex:
        def search(self, *a, **kw):
            raise ConnectionError()
    ranked = rank_evidence("管理费", [good], FailedIndex())
    assert ranked[0]["retrieval_channels"] == ["lexical"]
    assert ranked[0]["retrieval_warnings"] == ["VECTOR_CHANNEL_UNAVAILABLE"]


def test_extractive_answer_has_real_citation_and_no_generation_claim(tmp_path):
    source = record()
    answer = generate_answer("基金合同如何核对", "answer", {}, [source], settings(tmp_path), str(uuid4()))
    validate_answer(answer, [source])
    assert answer["status"] == "ANSWERED"
    assert answer["claims"][0]["text"] == source["text"]
    assert answer["citations"][0]["content_sha256"] == source["content_sha256"]
    assert "未调用生成模型" in "".join(answer["limitations"])
    assert answer["review_status"] == "MACHINE_CHECKED"


def test_unrelated_or_bad_hash_evidence_never_becomes_an_answer(tmp_path):
    unrelated = record("仓库库存盘点。", title="仓储资料")
    bad = record()
    bad["content_sha256"] = "0" * 64
    for evidence in ([], [unrelated], [bad]):
        answer = generate_answer("基金管理费费率是多少", "answer", {}, evidence, settings(tmp_path), str(uuid4()))
        assert answer["status"] == "INSUFFICIENT_EVIDENCE"
        assert answer["required_sources"]


def test_decisive_facts_are_taken_from_registered_conditions(tmp_path):
    source = record(required_facts=["business_date", "share_class"])
    answer = generate_answer("基金合同如何核对", "solution", {"share_class": "A"}, [source], settings(tmp_path), str(uuid4()))
    assert answer["status"] == "NEEDS_CLARIFICATION"
    assert [x["field"] for x in answer["missing_facts"]] == ["business_date"]


def test_known_inapplicable_versions_are_excluded(tmp_path):
    source = record(applicability={"all": [{"field": "share_class", "op": "eq", "values": ["A"]}]})
    answer = generate_answer("基金合同如何核对", "answer", {"share_class": "C"}, [source], settings(tmp_path), str(uuid4()))
    assert answer["status"] == "INSUFFICIENT_EVIDENCE"
    dated = record(valid_from="2026-01-01", valid_to="2026-08-01")
    answer = generate_answer("基金合同如何核对", "answer", {"business_date": "2026-09-07"}, [dated], settings(tmp_path), str(uuid4()))
    assert answer["status"] == "INSUFFICIENT_EVIDENCE"


def test_solution_uses_registered_steps_not_a_generic_template(tmp_path):
    source = step_record()
    answer = generate_answer("基金合同如何核对", "solution", {}, [source], settings(tmp_path), str(uuid4()))
    validate_answer(answer, [source])
    assert answer["status"] == "ANSWERED"
    step = answer["solution"]["steps"][0]
    for name in ("action", "owner_role", "output", "verification"):
        assert step[name] == source["data"][name]
    assert step["inputs"] == [] and step["depends_on"] == []
    assert answer["solution"]["branches"] == []
    ordinary = generate_answer("基金合同如何核对", "solution", {}, [record()], settings(tmp_path), str(uuid4()))
    assert ordinary["status"] == "INSUFFICIENT_EVIDENCE" and ordinary["solution"] is None


SOP_QUESTION = "费用计提差异应如何排查，请给出可执行的核对步骤和来源依据。"


def sop_fixture():
    version, resource = str(uuid4()), str(uuid4())
    ordinals = [2, 5, 8, 11]  # headings/paragraphs can occur between steps
    actions = ["核对适用范围：确认基金、份额类别和业务日期。",
               "核对输入版本：比较管理人与托管方的持仓、费用参数和文件版本。",
               "复核费用计算：核对费用计提基数、费率、期间与舍入口径。",
               "提交复核材料：整理费用计提差异、来源依据和待处理事项。"]
    return [step_record(version, resource_id=resource, title="费用计提差异排查与复核", ordinal=ordinal,
                        version_step_ordinals=ordinals, locator={"label": f"处理步骤 {index + 1}"},
                        data={"action": action}) for index, (ordinal, action) in enumerate(zip(ordinals, actions, strict=True))]


def test_solution_selects_one_sop_and_preserves_original_order_despite_rank_and_templates(tmp_path):
    source = sop_fixture()
    faq = step_record(title=SOP_QUESTION, knowledge_type="faq", score=999)
    template = step_record(title=SOP_QUESTION, knowledge_type="solution_template", kind="template", score=999)
    disguised_template = step_record(title=SOP_QUESTION, knowledge_type="sop", kind="template", score=999)
    other_sop = step_record(title="仓库盘点步骤", data={"action": "核对货架库存与仓储记录。"}, score=0.001)
    evidence = [faq, template, source[2], other_sop, source[0], disguised_template, source[3], source[1]]
    answer = generate_answer(SOP_QUESTION, "solution", {}, evidence, settings(tmp_path), str(uuid4()))
    assert answer["status"] == "ANSWERED"
    assert [s["action"] for s in answer["solution"]["steps"]] == [r["data"]["action"] for r in source]
    assert {c["version_id"] for c in answer["citations"]} == {source[0]["version_id"]}
    assert len(answer["citations"]) == 4 and len(answer["claims"]) == 1
    assert [s["evidence_ids"] for s in answer["solution"]["steps"]] == [[f"E{i}"] for i in range(1, 5)]
    validate_answer(answer, evidence)


def test_partial_sop_uses_only_authorized_input_and_reports_manifest_gap(tmp_path):
    source = sop_fixture()
    evidence = [source[2], source[0]]
    answer = generate_answer(SOP_QUESTION, "solution", {}, evidence, settings(tmp_path), str(uuid4()))
    assert answer["status"] == "INSUFFICIENT_EVIDENCE"
    assert [s["action"] for s in answer["solution"]["steps"]] == [source[0]["data"]["action"], source[2]["data"]["action"]]
    assert {c["block_id"] for c in answer["citations"]} == {r["block_id"] for r in evidence}
    assert "2项" in answer["summary"] and "不完整" in answer["summary"]
    assert answer["required_sources"] and answer["review_status"] == "REQUIRES_EXPERT"


def test_sop_without_manifest_never_claims_complete(tmp_path):
    source = sop_fixture()
    for record_ in source:
        record_.pop("version_step_ordinals")
    answer = generate_answer(SOP_QUESTION, "solution", {}, source[::-1], settings(tmp_path), str(uuid4()))
    assert answer["status"] == "INSUFFICIENT_EVIDENCE"
    assert "完整性未确认" in answer["summary"]
    assert answer["required_sources"]


def test_legacy_locator_order_is_numeric_not_rank_or_string_order(tmp_path):
    source = sop_fixture()[:3]
    for record_, number in zip(source, [1, 2, 10], strict=True):
        record_.pop("ordinal")
        record_.pop("version_step_ordinals")
        record_["locator"] = {"label": f"处理步骤 {number}"}
    answer = generate_answer(SOP_QUESTION, "solution", {}, [source[2], source[1], source[0]], settings(tmp_path), str(uuid4()))
    assert [s["action"] for s in answer["solution"]["steps"]] == [r["data"]["action"] for r in source]
    assert answer["status"] == "INSUFFICIENT_EVIDENCE"


def test_sop_missing_or_conflicting_order_does_not_fall_back_to_retrieval_rank(tmp_path):
    for missing in (True, False):
        source = sop_fixture()[:2]
        for record_ in source:
            if missing:
                record_.pop("ordinal")
            else:
                record_["ordinal"] = 0
            record_["locator"] = {"label": "第2页", "source_page": 2}
        answer = generate_answer(SOP_QUESTION, "solution", {}, source[::-1], settings(tmp_path), str(uuid4()))
        assert answer["status"] == "INSUFFICIENT_EVIDENCE" and answer["solution"] is None
        assert "顺序" in answer["summary"]


@pytest.mark.parametrize("knowledge_type,kind", [("faq", "knowledge"), ("solution_template", "template"), ("sop", "template")])
def test_faq_and_templates_never_become_an_extractive_sop(tmp_path, knowledge_type, kind):
    record_ = step_record(knowledge_type=knowledge_type, kind=kind)
    answer = generate_answer("基金合同如何核对", "solution", {}, [record_], settings(tmp_path), str(uuid4()))
    assert answer["status"] == "INSUFFICIENT_EVIDENCE"
    assert answer["solution"] is None and answer["claims"] == []


def test_sop_extraction_keeps_low_match_steps_already_present_in_same_authorized_version(tmp_path):
    source = sop_fixture()
    source[0]["text"] = block_text({"block_type": "step", "data": source[0]["data"]})
    answer = generate_answer("复核费用计算", "solution", {}, [source[2], source[0], source[3], source[1]], settings(tmp_path), str(uuid4()))
    assert answer["status"] == "ANSWERED"
    assert [s["action"] for s in answer["solution"]["steps"]] == [r["data"]["action"] for r in source]


def test_extractive_validator_rejects_reordered_or_cross_version_registered_steps(tmp_path):
    source = sop_fixture()
    answer = generate_answer(SOP_QUESTION, "solution", {}, source, settings(tmp_path), str(uuid4()))
    answer["solution"]["steps"] = answer["solution"]["steps"][::-1]
    with pytest.raises(AnswerValidationError, match="EXTRACTIVE_SOP_ORDER_INVALID"):
        validate_answer(answer, source)
    answer["solution"]["steps"] = answer["solution"]["steps"][::-1]
    other = step_record()
    other_answer = generate_answer("基金合同如何核对", "solution", {}, [other], settings(tmp_path), str(uuid4()))
    citation = {**other_answer["citations"][0], "id": "OTHER"}
    answer["citations"].append(citation)
    answer["solution"]["steps"].append({**other_answer["solution"]["steps"][0], "id": "S5", "evidence_ids": ["OTHER"]})
    with pytest.raises(AnswerValidationError, match="EXTRACTIVE_SOP_MIXED_VERSIONS"):
        validate_answer(answer, [*source, other])


def test_sop_conflicting_manifest_or_invalid_step_stays_incomplete(tmp_path):
    source = sop_fixture()
    source[0]["version_step_ordinals"] = [2, 5, 8]
    answer = generate_answer(SOP_QUESTION, "solution", {}, source, settings(tmp_path), str(uuid4()))
    assert answer["status"] == "INSUFFICIENT_EVIDENCE" and "完整性未确认" in answer["summary"]
    source = sop_fixture()
    source[1]["data"]["owner_role"] = ""
    source[1]["text"] = block_text(source[1])
    source[1]["content_sha256"] = text_sha256(source[1]["text"])
    answer = generate_answer(SOP_QUESTION, "solution", {}, source, settings(tmp_path), str(uuid4()))
    assert answer["status"] == "INSUFFICIENT_EVIDENCE" and "1项未命中" in answer["summary"]
    assert len(answer["solution"]["steps"]) == 3


def test_missing_http_service_fallback_is_single_sop_not_old_mixed_skeleton(tmp_path):
    source = sop_fixture()
    faq = step_record(knowledge_type="faq", title=SOP_QUESTION)
    answer = generate_answer(SOP_QUESTION, "solution", {}, [faq, *source[::-1]],
                             settings(tmp_path, llm_provider="http"), str(uuid4()))
    assert answer["status"] == "ANSWERED"
    assert {c["version_id"] for c in answer["citations"]} == {source[0]["version_id"]}
    assert [s["action"] for s in answer["solution"]["steps"]] == [r["data"]["action"] for r in source]


@pytest.mark.parametrize("tamper,code", [
    (lambda a: a["citations"][0].update(excerpt="编造费率0.5%"), "CITATION_PROVENANCE_INVALID"),
    (lambda a: a["citations"][0].update(content_sha256="0" * 64), "CITATION_PROVENANCE_INVALID"),
    (lambda a: a["citations"][0].update(locator={"label": "不存在的页码", "source_page": 99}), "CITATION_PROVENANCE_INVALID"),
    (lambda a: a["claims"][0].update(text="已获得托管人确认，可以立即执行。"), "CLAIM_SUPPORT_UNVERIFIED"),
    (lambda a: a["claims"][0].update(evidence_ids=["fake"]), "CLAIM_CITATION_MISSING"),
    (lambda a: a["claims"][0].update(text="<script>alert(1)</script>"), "UNSAFE_MODEL_OUTPUT"),
])
def test_citation_and_claim_validation_blocks_fabrications(tmp_path, tamper, code):
    source = record()
    answer = generate_answer("基金合同如何核对", "answer", {}, [source], settings(tmp_path), str(uuid4()))
    tamper(answer)
    with pytest.raises(AnswerValidationError, match=code):
        validate_answer(answer, [source])


def test_step_cycles_and_unregistered_actions_rejected(tmp_path):
    source = step_record()
    answer = generate_answer("基金合同如何核对", "solution", {}, [source], settings(tmp_path), str(uuid4()))
    answer["solution"]["steps"][0]["depends_on"] = ["S1"]
    with pytest.raises(AnswerValidationError, match="STEP_DEPENDENCY_INVALID"):
        validate_answer(answer, [source])
    answer["solution"]["steps"][0]["depends_on"] = []
    answer["solution"]["steps"][0]["action"] = "立即付款"
    with pytest.raises(AnswerValidationError, match="STEP_NOT_REGISTERED"):
        validate_answer(answer, [source])


def test_compile_is_extractive_draft_with_exact_source_links():
    source = {"block_id": str(uuid4()), "ordinal": 0, "block_type": "step",
              "data": {"action": "核对合同", "owner_role": "复核岗", "output": "核对记录", "verification": "版本一致"},
              "locator": {"label": "步骤1"}, "citations": []}
    before = copy.deepcopy(source)
    version = str(uuid4())
    compiled = compile_knowledge([source], version, "合同核对SOP")
    assert source == before
    assert compiled["legal_status"] == "UNKNOWN"
    assert compiled["blocks"][0]["locator"]["semantic_compile"] is False
    assert compiled["blocks"][1]["data"] == source["data"]
    assert compiled["blocks"][1]["citations"] == [{"version_id": version, "block_id": source["block_id"], "purpose": "FACT"}]


def test_missing_http_configuration_does_not_send_request(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "post_json", lambda *a, **kw: pytest.fail("unexpected request"))
    answer = generate_answer("基金合同如何核对", "answer", {}, [record()], settings(tmp_path, llm_provider="http"), str(uuid4()))
    assert any("MODEL_NOT_CONFIGURED" in text for text in answer["limitations"])


def test_http_candidate_preserves_grounded_paraphrase_but_cannot_promote_review(tmp_path, monkeypatch):
    source = record()
    run_id = str(uuid4())
    candidate = generate_answer("基金合同如何核对", "answer", {}, [source], settings(tmp_path), run_id)
    candidate.update(review_status="EXPERT_REVIEWED", summary="先核实基金合同版本，再确认业务日期与该版本相符。")
    candidate["claims"][0]["text"] = "合同核对需要同时考虑版本与业务日期。"
    def fake(*args, **kwargs):
        assert args[1] == "chat/completions"
        assert args[2]["stream"] is False
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(candidate, ensure_ascii=False)}}]}
    monkeypatch.setattr(ai, "post_json", fake)
    output = generate_answer("基金合同如何核对", "answer", {}, [source],
                             settings(tmp_path, llm_provider="http", llm_model="institution", llm_base_url="https://example.invalid/v1"), run_id)
    assert output["review_status"] == "REQUIRES_EXPERT"
    assert output["summary"] == candidate["summary"]
    assert output["claims"][0]["text"] == candidate["claims"][0]["text"]
    assert "HTTP生成结果" in output["limitations"][-1]


def test_http_bad_claim_falls_back_without_false_generation_success(tmp_path, monkeypatch):
    source = record()
    candidate = generate_answer("基金合同如何核对", "answer", {}, [source], settings(tmp_path), str(uuid4()))
    candidate["claims"][0]["text"] = "管理费率0.99%，已获批准。"
    monkeypatch.setattr(ai, "post_json", lambda *a, **kw: {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(candidate)}}]})
    output = generate_answer("基金合同如何核对", "answer", {}, [source],
                             settings(tmp_path, llm_provider="http", llm_model="institution", llm_base_url="https://example.invalid/v1"), str(uuid4()))
    assert output["claims"][0]["text"] == source["text"]
    assert any("NUMERIC_UNIT_SUPPORT_MISSING" in x for x in output["limitations"])


def test_published_knowledge_does_not_require_source_verified_flag(tmp_path):
    knowledge = step_record(source_verified=False)
    answer = generate_answer("基金合同如何核对", "solution", {}, [knowledge], settings(tmp_path), str(uuid4()))
    assert answer["status"] == "ANSWERED"
    assert answer["solution"]["steps"][0]["action"] == knowledge["data"]["action"]
    source = record(source_verified=False)
    answer = generate_answer("基金合同如何核对", "answer", {}, [source], settings(tmp_path), str(uuid4()))
    assert answer["status"] == "INSUFFICIENT_EVIDENCE"


def test_unknown_applicability_and_fact_metadata_request_clarification(tmp_path):
    for fields in ({"applicability": None}, {"required_facts": None},
                   {"applicability": {"all": [{"field": "share_class", "op": "eq", "values": ["A"]}]}}):
        answer = generate_answer("基金合同如何核对", "answer", {}, [record(**fields)], settings(tmp_path), str(uuid4()))
        assert answer["status"] == "NEEDS_CLARIFICATION"
        assert answer["missing_facts"]


def test_grounded_http_real_adapter_contract_delivers_composed_plan_without_network(tmp_path, monkeypatch):
    source = record("运营岗核对基金合同的版本和生效日期。复核岗确认份额类别及合同适用性，记录核对结果；存在差异时提交复核。")
    rid = str(uuid4())
    baseline = generate_answer("基金合同如何核对", "answer", {"share_class": "A"}, [source], settings(tmp_path), rid)
    candidate = copy.deepcopy(baseline)
    candidate.update(status="ANSWERED", mode="solution", summary="可以先核对版本，再复核适用条件；发现差异应先提交复核。", required_sources=[],
                     claims=[{"id": "C1", "text": "版本、生效日期和份额类别应一起用于判断合同适用性。", "evidence_ids": ["E1"]}],
                     solution={"goal": "确认基金合同适用性", "preconditions": ["取得待核对的基金合同"], "materials": ["基金合同"],
                     "steps": [{"id": "S1", "action": "对照基金合同确认版本及生效日期", "owner_role": "运营岗",
                                "inputs": ["基金合同"], "output": "版本及日期核对记录", "verification": "记录与合同一致",
                                "evidence_ids": ["E1"], "depends_on": []},
                               {"id": "S2", "action": "结合份额类别复核合同适用性", "owner_role": "复核岗",
                                "inputs": ["版本及日期核对记录"], "output": "适用性复核记录", "verification": "份额类别与适用条件相符",
                                "evidence_ids": ["E1"], "depends_on": ["S1"]}],
                     "branches": [{"condition": "发现版本或适用条件差异", "action": "提交复核"}],
                     "completion_checks": ["核对结果已经记录且差异已经复核"], "escalation": ["存在差异时提交复核"]},
                     limitations=["方案组合需要业务专家复核，尚未执行。"], review_status="REQUIRES_EXPERT")
    # Keep the original past-tense criterion: the typed field expresses a future
    # verification requirement, not an assertion that this work was performed.
    real_client = httpx.Client
    observed = []
    def respond(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "institution-contract-fixture", "choices": [{"finish_reason": "stop",
                              "message": {"content": json.dumps(candidate, ensure_ascii=False)}}]})
    monkeypatch.setattr(ai_transport.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(respond)))
    result = generate_answer("基金合同如何核对", "solution", {"share_class": "A"}, [source],
                             settings(tmp_path, llm_provider="http", llm_base_url="https://example.invalid/v1", llm_model="institution-contract-fixture"), rid)
    assert observed and observed[0]["stream"] is False
    assert result["status"] == "ANSWERED"
    assert result["summary"] == candidate["summary"]
    assert result["solution"]["steps"][1]["depends_on"] == ["S1"]
    assert result["solution"]["steps"][0]["action"] == candidate["solution"]["steps"][0]["action"]
    assert result["solution"]["completion_checks"] == ["核对结果已经记录且差异已经复核"]
    assert result["review_status"] == "REQUIRES_EXPERT"
    validate_answer(result, [source], mode="grounded", context={"share_class": "A"})


@pytest.mark.parametrize("claim", ["基金管理费率为0.5%。", "金额为120万元。", "已获托管确认，可执行付款。"])
def test_grounded_mode_blocks_unsupported_numbers_units_and_approvals(tmp_path, claim):
    source = record("基金合同记载金额120元。")
    answer = generate_answer("基金合同金额", "answer", {}, [source], settings(tmp_path), str(uuid4()))
    answer["claims"][0]["text"] = claim
    with pytest.raises(AnswerValidationError):
        validate_answer(answer, [source], mode="grounded")


def test_grounded_mode_cannot_change_known_facts_or_applicable_date(tmp_path):
    source = record(valid_from="2026-01-01", valid_to="2027-01-01")
    answer = generate_answer("基金合同如何核对", "answer", {"share_class": "A"}, [source], settings(tmp_path), str(uuid4()))
    answer["facts"][0]["value"] = "C"
    with pytest.raises(AnswerValidationError, match="KNOWN_FACT_CHANGED"):
        validate_answer(answer, [source], mode="grounded", context={"share_class": "A"})
    answer["facts"] = []
    answer["scope"] = {}
    with pytest.raises(AnswerValidationError, match="APPLICABILITY_UNRESOLVED"):
        validate_answer(answer, [source], mode="grounded", context={"business_date": "2027-02-01"})


def test_grounded_date_check_does_not_allow_permuting_month_and_day(tmp_path):
    source = record("基金合同于2026-09-07生效，金额为100元。")
    answer = generate_answer("基金合同生效日期", "answer", {}, [source], settings(tmp_path), str(uuid4()))
    answer["claims"][0]["text"] = "基金合同于2026年9月7日生效，金额折合0.01万元。"
    validate_answer(answer, [source], mode="grounded")
    answer["claims"][0]["text"] = "基金合同于2026-07-09生效。"
    with pytest.raises(AnswerValidationError, match="NUMERIC_UNIT_SUPPORT_MISSING"):
        validate_answer(answer, [source], mode="grounded")


@pytest.mark.parametrize("text", ['![跟踪](https://example.invalid/track)', '&lt;img src="https://example.invalid/track"&gt;'])
def test_grounded_output_cannot_embed_tracking_images(tmp_path, text):
    source = record()
    answer = generate_answer("基金合同如何核对", "answer", {}, [source], settings(tmp_path), str(uuid4()))
    answer["summary"] = text
    with pytest.raises(AnswerValidationError, match="UNSAFE_MODEL_OUTPUT"):
        validate_answer(answer, [source], mode="grounded")


def test_http_transport_offline_mock_enforces_timeout_no_redirect_no_env(monkeypatch):
    real_client = httpx.Client
    observed = {}
    def respond(request):
        observed["request"] = request
        return httpx.Response(200, json={"ok": True})
    def client(**kwargs):
        observed["kwargs"] = kwargs
        return real_client(**kwargs, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(ai_transport.httpx, "Client", client)
    assert ai_transport.post_json("https://example.invalid/v1", "embeddings", {"input": ["合成"]}, timeout=2) == {"ok": True}
    assert observed["kwargs"]["trust_env"] is False
    assert observed["kwargs"]["follow_redirects"] is False
    assert observed["kwargs"]["timeout"].read == 2
    assert observed["request"].url.path == "/v1/embeddings"


def test_http_total_deadline_does_not_wait_for_provider_to_finish(monkeypatch):
    def slow_fixture(*args):
        time.sleep(0.2)
        return {"late": True}
    monkeypatch.setattr(ai_transport, "_post_request", slow_fixture)
    start = time.monotonic()
    with pytest.raises(ProviderError, match="PROVIDER_TIMEOUT"):
        ai_transport.post_json("https://example.invalid/v1", "embeddings", {}, timeout=0.05)
    assert time.monotonic() - start < 0.17


@pytest.mark.parametrize("url", ["http://external.invalid/v1", "https://user:secret@example.invalid/v1", "file:///tmp/model", "https://example.invalid?key=x"])
def test_http_transport_rejects_unsafe_configuration_before_any_request(url):
    with pytest.raises(ProviderError):
        ai_transport.endpoint(url, "embeddings")
