"""Synthetic source closure: stable outline/chunks, no model/network/real data."""
import copy

import pytest

from fund_kb.source_associations import complete_source_context, locate_path
from fund_kb.source_sections import build_document_sections, select_sections_from_outline
from test_source_sections import block


def complete(texts, anchors, **kw):
    rows = [block(i, text) for i, text in enumerate(texts)]
    outline = build_document_sections(rows, structure_version="v3")
    before = copy.deepcopy((rows, outline))
    result = complete_source_context(outline, select_sections_from_outline(outline, anchors), blocks=rows, **kw)
    assert (rows, outline) == before
    return result, outline, rows


def bids(result):
    return {bid for section in result["sections"] for bid in section["block_ids"]}


def test_numbered_workflow_group_does_not_promote_to_whole_chapter():
    result, _, _ = complete(["第一章 任意业务", "一、状态变更", "（一）接收", "INPUT_REQUIRED。",
        "（二）登记", "REGISTER_REQUIRED。", "二、无关业务", "UNRELATED。"], ["b3"])
    assert bids(result) == {f"b{i}" for i in range(1, 6)}
    assert result["structural_additions"][0]["reason"] == "numbered_sibling_group"


def test_unrelated_chapter_siblings_are_not_invented_dependencies():
    result, _, _ = complete(["第一章 总体", "第一条 事项甲", "AAA。", "第二条 事项乙", "BBB。"], ["b2"])
    assert bids(result) == {"b1", "b2"}


def test_compound_reference_and_cycle_close_without_depth_limit():
    result, _, _ = complete(["第一条 入口", "请参照第三条第一项执行。", "第二条 无关", "UNRELATED。",
        "第三条 处理安排", "一、核对", "请依照第一条核对输入。", "二、后续", "OTHER。"], ["b1"])
    assert {"b0", "b1", "b5", "b6"} <= bids(result)
    assert "b3" not in bids(result)
    assert all(r["status"] == "resolved" for r in result["references"])


def test_unnumbered_natural_clause_reads_article_but_keeps_precision_gap():
    result, _, _ = complete(["第一条 入口", "依据第三条第一款处理。", "第三条 完整条件", "必须核对条件甲。",
        "同时核对条件乙。", "第四条 无关", "NOT_INCLUDED。"], ["b1"])
    assert {"b2", "b3", "b4"} <= bids(result) and "b6" not in bids(result)
    assert result["references"][0]["status"] == "broader_context"


@pytest.mark.parametrize("prefix", ["前条", "上一条"])
def test_relative_reference_within_same_numbering_scope(prefix):
    result, _, _ = complete(["第一章 甲", "第一条 条件", "YES。", "第二条 方法", f"依据{prefix}处理。",
        "第二章 乙", "第一条 其他", "NO。"], ["b4"])
    assert {"b1", "b2"} <= bids(result) and "b7" not in bids(result)
    assert result["references"][0]["status"] == "resolved"


def test_relative_missing_number_is_not_adjacent_section_guess():
    result, _, _ = complete(["第一条 内容", "ONE。", "第三条 方法", "按照前条执行。"], ["b3"])
    assert result["references"][0]["status"] == "unresolved"
    assert "b1" not in bids(result)


def test_ambiguous_duplicate_articles_not_silently_selected():
    _, outline, _ = complete(["第一章 甲", "第三条 事项", "A。", "第二章 乙", "第三条 事项", "B。"], ["b2"])
    assert locate_path(outline, "第三条")["status"] == "ambiguous"
    assert locate_path(outline, "第二章第三条")["status"] == "resolved"


def test_external_compound_spans_are_exact_across_original_blocks():
    result, _, rows = complete(["第一条 方法", "按《合成规则》第三条", "第一项办理。"], ["b1"])
    ref = result["references"][0]
    assert ref["target_title"] == "合成规则" and ref["status"] == "external"
    assert ref["locator"] == "第三条\n第一项"
    for span in ref["source_spans"]:
        source = next(r for r in rows if r["block_id"] == span["block_id"])["data"]["text"]
        assert source[span["start"]:span["end"]] == ref["text"][span["text_start"]:span["text_end"]]


def test_explicit_section_can_disable_sibling_lift_and_inputs_never_cross_versions():
    result, outline, rows = complete(["一、业务", "（一）输入", "A。", "（二）输出", "B。"], ["b2"], structural_groups=False)
    assert bids(result) == {"b1", "b2"}
    rows[0]["version_id"], rows[-1]["version_id"] = "v1", "v2"
    with pytest.raises(ValueError, match="one resource/version"):
        complete_source_context(outline, [outline[0]], blocks=rows)


def test_own_article_heading_is_not_reference_and_dates_are_not_dependencies():
    result, _, _ = complete(["第一条 方法", "2026年9月23日按1.5%核对。"], ["b1"])
    assert result["references"] == []


def test_parent_lead_in_reference_is_checked_without_reading_other_children():
    result, _, _ = complete(["第一章 方法", "共同前提见《合成前提》第二条。", "第一节 甲", "A。",
        "第二节 乙", "B。"], ["b3"])
    assert "b5" not in bids(result)
    assert result["references"][0]["target_title"] == "合成前提"


def test_quoted_own_document_title_is_not_an_external_dependency():
    rows = [block(0, "《合成指引》", kind="heading", level=1), block(1, "第一条 方法"), block(2, "内容。")]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, [outline[0]], blocks=rows)
    assert result["references"] == []


def test_quoted_other_title_does_not_steal_the_next_source_heading_as_locator():
    result, _, _ = complete(["第一条 方法", "请参阅《合成说明》", "第二条 其他", "BBB。"], ["b0"])
    assert result["references"][0]["locator"] == ""


def test_explicit_attachment_uses_real_heading_or_reports_gap():
    rows = [block(0, "第一条 方法"), block(1, "参见附件一。另按附件三核对。"),
        block(2, "附件一 核对清单", kind="heading", level=1), block(3, "CHECKLIST。")]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, select_sections_from_outline(outline, ["b1"]), blocks=rows)
    assert "b3" in bids(result)
    assert [r["status"] for r in result["references"]] == ["resolved", "unresolved"]
