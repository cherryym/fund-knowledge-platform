from fund_kb.citation_map import public_citation_map


def rows():
    return [{"evidence_id": f"E{i}", "version_id": "v1", "resource_id": "r1", "block_id": f"b{i}",
             "content_sha256": str(i) * 64} for i in range(1, 4)]


def test_grouped_references_are_bound_to_real_spans_not_counted_as_independent_votes():
    text = "## 答复\n\n结合两个步骤可推导该建议。[E1]-[E3]\n\n以下仍需业务确认。"
    result = public_citation_map(text, rows())
    linked = [p for p in result["paragraphs"] if p["sources"]]
    assert len(linked) == 1 and len(linked[0]["sources"]) == 3
    assert linked[0]["independent_source_versions"] == 1
    assert result["semantic_entailment"] == "NOT_EVALUATED"
    assert result["answer_rewritten"] is False


def test_unknown_reference_is_not_repaired_with_a_similar_source():
    result = public_citation_map("业务判断[E99]", rows())
    assert result["linked_paragraphs"] == 0 and "UNKNOWN_CITATION" in result["integrity_warnings"]


def test_conflicting_frozen_id_is_not_accepted():
    result = public_citation_map("业务判断[E1]", rows() + [{**rows()[0], "version_id": "other"}])
    assert result["linked_paragraphs"] == 0 and "AMBIGUOUS_EVIDENCE_ID" in result["integrity_warnings"]
