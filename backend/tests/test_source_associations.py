"""Synthetic source closure: stable outline/chunks, no model/network/real data."""
import copy

import pytest
from test_source_sections import block

from fund_kb.source_associations import complete_source_context, locate_path
from fund_kb.source_sections import build_document_sections, select_sections_from_outline


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


def test_dependency_targets_receive_the_same_numbered_group_closure_as_seeds():
    result, _, _ = complete(["第一条 入口", "参照第二条第一项。", "第二条 条件", "一、主规则",
        "条件A满足时准许操作。", "二、例外", "但是例外B成立时禁止操作。", "第三条 无关", "UNRELATED。"], ["b1"])
    assert bids(result) == {f"b{i}" for i in range(7)}
    assert len(result["structural_additions"]) == 1


def test_numbered_group_closure_reaches_a_fixed_point_without_promoting_a_chapter():
    result, _, _ = complete(["第一章 合成范围", "第一条 入口", "参照第二条第一项第一项。",
        "第二条 规则组", "一、条件", "（一）动作", "ACTION。", "（二）内部例外", "INNER_EXCEPTION。",
        "二、外层例外", "OUTER_EXCEPTION。", "第三条 无关", "UNRELATED。"], ["b2"])
    assert bids(result) == {f"b{i}" for i in range(1, 11)}
    assert len(result["structural_additions"]) == 2
    assert len({(x["from_section_id"], x["to_section_id"]) for x in result["structural_additions"]}) == 2


def test_explicit_section_can_disable_group_closure_for_dependency_targets_too():
    result, _, _ = complete(["第一条 入口", "参照第二条第一项。", "第二条 条件", "一、主规则",
        "MAIN。", "二、例外", "EXCEPTION。"], ["b1"], structural_groups=False)
    assert bids(result) == {"b0", "b1", "b3", "b4"}
    assert result["structural_additions"] == []


@pytest.mark.parametrize("local_target", [True, False])
def test_unqualified_compound_reference_cannot_escape_its_numbering_scope(local_target):
    texts = ["第一章 甲", "第一条 入口", "按第三条第一项执行。"]
    if local_target:
        texts += ["第三条 处理", "一、内容", "RIGHT。"]
    texts += ["第二章 乙", "第三条 处理", "一、内容", "WRONG。"]
    result, _, rows = complete(texts, ["b2"])
    read = [r["data"]["text"] for r in rows if r["block_id"] in bids(result)]
    assert "WRONG。" not in read
    assert ("RIGHT。" in read) is local_target
    assert result["references"][0]["status"] == ("resolved" if local_target else "unresolved")


def test_explicit_chapter_qualifier_can_select_a_repeated_article():
    result, _, _ = complete(["第一章 甲", "第一条 入口", "按第二章第三条第一项执行。",
        "第三条 处理", "一、内容", "WRONG。", "第二章 乙", "第三条 处理", "一、内容", "RIGHT。"], ["b2"])
    assert "b9" in bids(result) and "b5" not in bids(result)
    assert result["references"][0]["status"] == "resolved"


@pytest.mark.parametrize("reference,heading", [("附表C", "附表C 核对"), ("附件1", "附件一 核对"),
    ("附录二", "附录 ２ 核对")])
def test_explicit_attachment_and_table_labels_have_exact_structural_locators(reference, heading):
    rows = [block(0, "第一条 入口"), block(1, f"参见{reference}。"),
        block(2, heading, kind="heading", level=1), block(3, "TARGET。"),
        block(4, "其他附件", kind="heading", level=1), block(5, "UNRELATED。")]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, select_sections_from_outline(outline, ["b1"]), blocks=rows)
    assert bids(result) == {"b0", "b1", "b2", "b3"}
    assert result["references"][0]["status"] == "resolved"
    assert locate_path(outline, reference) == {"status": "resolved", "target_section_ids": [outline[1]["section_id"]]}


def test_attachment_locator_with_article_path_is_scoped_inside_the_named_attachment():
    rows = [block(0, "附件一 甲", kind="heading", level=1), block(1, "第三条 目标"), block(2, "RIGHT。"),
        block(3, "附件二 乙", kind="heading", level=1), block(4, "第三条 重号"), block(5, "WRONG。")]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, [], blocks=rows, locators=["附件一第三条"])
    assert bids(result) == {"b1", "b2"}
    assert result["incoming"]["附件一第三条"]["status"] == "resolved"


@pytest.mark.parametrize("heading", ["附表CC 清单", "附表C1 清单", "附表C清单"])
def test_attachment_label_prefix_is_not_an_exact_heading_match(heading):
    rows = [block(0, heading, kind="heading", level=1), block(1, "NOT_AN_EXACT_LABEL。")]
    outline = build_document_sections(rows, structure_version="v3")
    assert locate_path(outline, "附表C")["status"] == "unresolved"


def test_duplicate_attachment_labels_keep_an_ambiguity_gap():
    rows = [block(0, "附件一 甲", kind="heading", level=1), block(1, "A。"),
        block(2, "附件1 乙", kind="heading", level=1), block(3, "B。")]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, [], blocks=rows, locators=["附件一"])
    assert result["incoming"]["附件一"] == {"status": "ambiguous", "target_section_ids": []}
    assert result["sections"] == []


def test_cross_document_attachment_reference_does_not_resolve_against_local_namesake():
    rows = [block(0, "第一条 入口"), block(1, "参见《合成规范》附表C。"),
        block(2, "附表C 本地同名", kind="heading", level=1), block(3, "WRONG_LOCAL。")]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, select_sections_from_outline(outline, ["b1"]), blocks=rows)
    assert bids(result) == {"b0", "b1"}
    assert [(r["status"], r.get("target_title"), r.get("locator")) for r in result["references"]] == [
        ("external", "合成规范", "附表C")]


@pytest.mark.parametrize("qualifier", ["（2020年版）", "(版本甲)", "（修订前）"])
def test_qualified_source_title_preserves_version_identity_and_does_not_steal_local_article(qualifier):
    result, _, _ = complete(["第一条 入口", f"依照《合成规范》{qualifier}第三条。",
        "第三条 本地同号", "WRONG_LOCAL。"], ["b1"])
    assert bids(result) == {"b0", "b1"}
    assert len(result["references"]) == 1
    ref = result["references"][0]
    assert ref["status"] == "external" and ref["target_title"] == "合成规范" + qualifier
    assert ref["locator"] == "第三条"


def test_compound_reference_cannot_consume_a_following_actual_heading():
    result, _, _ = complete(["第一章 合成范围", "第一条 入口", "参照第三条", "第二条 其他",
        "OTHER。", "第三条 目标", "TARGET。"], ["b0"])
    assert [(r["text"], r["status"]) for r in result["references"]] == [("第三条", "resolved")]


def test_wrapped_external_path_stops_before_the_next_actual_heading():
    result, _, _ = complete(["第一章 合成范围", "第一条 入口", "参照《合成规则》第三条", "第二条 后续",
        "OTHER。"], ["b0"])
    assert len(result["references"]) == 1
    assert result["references"][0]["locator"] == "第三条"


@pytest.mark.parametrize("locator", ["第一条、第三条", "第一、三条", "第一条至第三条"])
def test_external_enumeration_never_degrades_into_local_references(locator):
    result, _, _ = complete(["第二条 入口", f"参照《合成规则》{locator}。",
        "第一条 本地", "WRONG_ONE。", "第三条 本地", "WRONG_THREE。"], ["b1"])
    assert bids(result) == {"b0", "b1"}
    assert len(result["references"]) == 1
    assert result["references"][0]["status"] == "external"
    assert result["references"][0]["locator"] == locator


@pytest.mark.parametrize("label", ["C1", "AA2"])
def test_alphanumeric_attachment_label_is_not_truncated_to_its_letter_prefix(label):
    rows = [block(0, "第一条 入口"), block(1, f"参见附表{label}。"),
        block(2, f"附表{label.rstrip('12')} 前缀同名", kind="heading", level=1), block(3, "WRONG。"),
        block(4, f"附表{label} 精确目标", kind="heading", level=1), block(5, "RIGHT。")]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, select_sections_from_outline(outline, ["b1"]), blocks=rows)
    assert bids(result) == {"b0", "b1", "b4", "b5"}


def test_own_qualified_document_title_is_not_a_dependency():
    rows = [block(0, "《合成规范》（版本甲）", kind="heading", level=1), block(1, "第一条 条件"), block(2, "A。")]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, [outline[0]], blocks=rows)
    assert result["references"] == []


@pytest.mark.parametrize("reference,definition", [("脚注1", "1 条件A必须核验。"),
    ("脚注一", "１ 条件A必须核验。"), ("[^condition]", "[^condition]: 条件A必须核验。")])
def test_explicit_footnote_marker_finds_its_unique_definition_in_another_section(reference, definition):
    rows = [block(0, "第一条 主规则"), block(1, f"动作条件见{reference}。"),
        block(2, "脚注说明", kind="heading", level=1), block(3, definition, kind="footnote"),
        block(4, "其他部分", kind="heading", level=1), block(5, "UNRELATED。")]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, select_sections_from_outline(outline, ["b1"]), blocks=rows)
    assert bids(result) == {"b0", "b1", "b2", "b3"}
    assert [(r["text"], r["status"]) for r in result["references"]] == [(reference, "resolved")]
    assert result["references"][0]["target_block_ids"] == ["b3"]


@pytest.mark.parametrize("duplicates", [False, True])
def test_missing_or_duplicate_footnotes_are_explicit_gaps(duplicates):
    rows = [block(0, "第一条 主规则"), block(1, "条件见脚注1。"),
        block(2, "脚注说明", kind="heading", level=1)]
    if duplicates:
        rows += [block(3, "1 甲定义。", kind="footnote"), block(4, "1 乙定义。", kind="footnote")]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, select_sections_from_outline(outline, ["b1"]), blocks=rows)
    assert bids(result) == {"b0", "b1"}
    assert len(result["references"]) == 1
    assert result["references"][0]["status"] == ("ambiguous" if duplicates else "unresolved")


def test_typed_footnote_uses_same_page_scope_for_repeated_numbers():
    rows = [block(0, "第一条 主规则", page=1), block(1, "见脚注1。", page=1),
        block(2, "同页说明", kind="heading", level=1, page=1), block(3, "1 RIGHT。", kind="footnote", page=1),
        block(4, "另一页", kind="heading", level=1, page=2), block(5, "1 WRONG。", kind="footnote", page=2)]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, select_sections_from_outline(outline, ["b1"]), blocks=rows)
    assert "b3" in bids(result) and "b5" not in bids(result)


def test_footnote_definition_is_not_guessed_from_ordinary_numbered_body_text():
    result, _, _ = complete(["第一条 主规则", "见脚注1。", "第二条 其他", "1 普通正文。"], ["b1"])
    assert bids(result) == {"b0", "b1"}
    assert result["references"][0]["status"] == "unresolved"


def test_foreign_footnote_never_uses_a_local_same_number_definition():
    rows = [block(0, "第一条 入口"), block(1, "参见《规则乙》脚注1。"),
        block(2, "本地脚注", kind="heading", level=1), block(3, "1 WRONG_LOCAL。", kind="footnote")]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, select_sections_from_outline(outline, ["b1"]), blocks=rows)
    assert bids(result) == {"b0", "b1"}
    assert [(r["status"], r.get("target_title"), r.get("locator")) for r in result["references"]] == [
        ("external", "规则乙", "脚注1")]


@pytest.mark.parametrize("locator,definition", [("脚注1", "1 完整条件。"), ("[^a]", "[^a]: 完整条件。")])
def test_incoming_footnote_locator_requires_a_unique_actual_definition(locator, definition):
    rows = [block(0, "脚注说明", kind="heading", level=1), block(1, definition, kind="footnote"),
        block(2, "其他", kind="heading", level=1), block(3, "UNRELATED。")]
    outline = build_document_sections(rows, structure_version="v3")
    result = complete_source_context(outline, [], blocks=rows, locators=[locator])
    assert bids(result) == {"b0", "b1"}
    assert result["incoming"][locator]["status"] == "resolved"
    assert result["incoming"][locator]["target_block_ids"] == ["b1"]
