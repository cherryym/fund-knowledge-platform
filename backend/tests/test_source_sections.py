"""Pure synthetic source-section tests: no real data, app, DB, model or network."""

import copy
import random

import pytest

from fund_kb.source_sections import build_document_sections, select_document_sections


def block(ordinal, text="", *, kind="paragraph", page=None, level=None, data=None, locator=None, block_id=None):
    payload = {"text": text} if data is None else data
    if level is not None:
        payload = {**payload, "level": level}
    location = dict(locator or {})
    if page is not None:
        location.update(source_page=page, label=f"第{page}页")
    return {"block_id": block_id or f"b{ordinal}", "ordinal": ordinal, "block_type": kind,
            "data": payload, "search_text": text, "locator": location}


def selected_ids(blocks, anchors):
    return [section["block_ids"] for section in select_document_sections(blocks, anchors)]


def assert_coverage(blocks, sections):
    source = {row["block_id"]: row for row in blocks}
    covered = set()
    assert len({section["section_id"] for section in sections}) == len(sections)
    for section in sections:
        assert set(section) == {"section_id", "title", "block_ids", "start_ordinal", "end_ordinal",
                                "parent_titles", "boundary"}
        assert isinstance(section["section_id"], str) and section["section_id"]
        assert isinstance(section["title"], str) and section["title"]
        assert isinstance(section["boundary"], str) and section["boundary"]
        assert isinstance(section["parent_titles"], list)
        assert all(isinstance(title, str) for title in section["parent_titles"])
        ids = section["block_ids"]
        assert ids and all(isinstance(item, str) for item in ids)
        assert len(set(ids)) == len(ids)
        assert all(item in source for item in ids)
        assert ids == sorted(ids, key=lambda item: (source[item]["ordinal"], item))
        assert section["start_ordinal"] == source[ids[0]]["ordinal"]
        assert section["end_ordinal"] == source[ids[-1]]["ordinal"]
        covered.update(ids)
    assert covered == set(source)


def test_explicit_heading_ranges_are_complete_and_smallest_selection_has_parent_titles():
    rows = [block(0, "会计手册", kind="heading", level=1), block(1, "适用说明。"),
            block(2, "债券", kind="heading", level=2), block(3, "债券说明。"),
            block(4, "回售", kind="heading", level=3), block(5, "确认回售条件，"),
            block(6, "并保留申请与撤销条件。"), block(7, "全部例外如下。"),
            block(8, "赎回", kind="heading", level=3), block(9, "赎回正文。"),
            block(10, "股票", kind="heading", level=2), block(11, "股票正文。")]
    sections = build_document_sections(rows)
    assert_coverage(rows, sections)
    assert sections[0]["block_ids"] == [f"b{i}" for i in range(12)]
    assert sections[1]["block_ids"] == [f"b{i}" for i in range(2, 10)]
    chosen = select_document_sections(rows, ["b6", "b5", "b6"])
    assert len(chosen) == 1
    assert chosen[0]["title"] == "回售"
    assert chosen[0]["block_ids"] == ["b4", "b5", "b6", "b7"]
    assert chosen[0]["parent_titles"] == ["会计手册", "债券"]
    assert chosen[0]["start_ordinal"] == 4 and chosen[0]["end_ordinal"] == 7


def test_sibling_anchors_do_not_promote_to_chapter_and_explicit_parent_is_deduplicated():
    rows = [block(0, "第三章 债券"), block(1, "一、回售"), block(2, "回售正文。"),
            block(3, "二、赎回"), block(4, "赎回正文。")]
    assert selected_ids(rows, ["b4", "b2", "b2"]) == [["b1", "b2"], ["b3", "b4"]]
    assert selected_ids(rows, ["b2", "b0", "b4"]) == [["b0", "b1", "b2", "b3", "b4"]]


def test_chinese_hierarchy_chapter_section_article_item_and_parenthesized_item():
    rows = [block(0, "第一编 基础业务"), block(1, "第三章 债券"), block(2, "第二节 回售"),
            block(3, "第十二条 回售安排"), block(4, "一、申请条件"), block(5, "（一）申报日期"),
            block(6, "日期按公告执行。"), block(7, "(二) 撤销日期"), block(8, "撤销按公告执行。"),
            block(9, "二、资金到账"), block(10, "完整到账要求。"), block(11, "第十三条 其他安排"),
            block(12, "其他条款。"), block(13, "第三节 赎回"), block(14, "赎回内容。"),
            block(15, "第四章 股票"), block(16, "股票内容。")]
    assert_coverage(rows, build_document_sections(rows))
    result = select_document_sections(rows, ["b6"])
    assert result[0]["block_ids"] == ["b5", "b6"]
    assert result[0]["parent_titles"] == ["第一编 基础业务", "第三章 债券", "第二节 回售",
                                           "第十二条 回售安排", "一、申请条件"]
    assert selected_ids(rows, ["b10"]) == [["b9", "b10"]]
    assert selected_ids(rows, ["b12"]) == [["b11", "b12"]]
    assert selected_ids(rows, ["b14"]) == [["b13", "b14"]]


@pytest.mark.parametrize("marker", ["第十二条", "第12条", "第 一百零二 条", "第壹拾贰条", "第１２条"])
def test_article_with_inline_body_retains_every_sentence_and_continuation(marker):
    rows = [block(0, marker + " 本基金管理费按日计提，具体比例以公告为准。"),
            block(1, "（一）管理费按前一日基金资产净值的0.3%计算。"),
            block(2, "（二）托管费按日计提"), block(3, "但遇特殊情况，应按以下要求执行。"),
            block(4, "第九百条 其他事项"), block(5, "后续条款。")]
    section = select_document_sections(rows, ["b2"])[0]
    assert section["title"] == marker
    assert section["block_ids"] == ["b0", "b1", "b2", "b3"]


@pytest.mark.parametrize("text", [
    "0.15%", "1. 管理费按日计提。", "1、管理费按日计提", "1.2 费用比例为0.3%", "2026年9月11日",
    "一、管理费为0.15%", "（一）管理费按日计提", "(一) 管理人应在当日处理", "一、本基金支付费用",
    "（二）投资者可以在回售期内提出申请", "第三章规定了债券业务。", "第十条规定的费率适用本基金。",
    "按第十条规定执行", "第二节中的相关要求", "33", "131 贷方红字。", "第四章 债券……........22",
])
def test_numbers_expenses_references_toc_and_footnotes_are_body_not_titles(text):
    rows = [block(0, "完整正文。", page=34), block(1, text, page=34), block(2, "完整末尾。", page=34)]
    sections = build_document_sections(rows)
    assert len(sections) == 1
    assert sections[0]["boundary"] == "physical_page"
    assert sections[0]["block_ids"] == ["b0", "b1", "b2"]


@pytest.mark.parametrize("text", ["一、费用计提", "（一）回售申报", "(一) 回售及撤销条件", "第 三 章 债券"])
def test_nominal_chinese_titles_are_still_recognized(text):
    rows = [block(0, text), block(1, "完整正文。")]
    section = build_document_sections(rows)[0]
    assert section["title"] == text
    assert section["boundary"].startswith("chinese_")


def test_arabic_nominal_titles_select_smaller_complete_sections_under_fvtpl():
    for title, boundary in [("1、业务确认", "arabic_item"), ("1. 初始确认", "arabic_item"),
                            ("（1）回售申报", "arabic_subitem"), ("(1) 回售申报", "arabic_subitem"),
                            ("1、管理费", "arabic_item")]:
        rows = [block(0, "第三章 债券"), block(1, "一、债券核算"), block(2, "（一）FVTPL债券"),
                block(3, "本类债券共同适用的确认原则。"), block(4, title),
                block(5, "本款完整正文。"), block(6, "本款的例外条件。"),
                block(7, "（二）其他债券"), block(8, "另一类债券正文。")]
        result = select_document_sections(rows, ["b5", "b6", "b5"])
        assert len(result) == 1
        assert result[0]["title"] == title
        assert result[0]["boundary"] == boundary
        assert result[0]["block_ids"] == ["b4", "b5", "b6"]
        assert result[0]["parent_titles"] == ["第三章 债券", "一、债券核算", "（一）FVTPL债券"]
        assert_coverage(rows, build_document_sections(rows))


def test_arabic_parenthesized_children_and_siblings_keep_their_complete_ranges():
    rows = [block(0, "（一）FVTPL债券"), block(1, "1、业务确认"), block(2, "业务确认的通用前提。"),
            block(3, "（1）回售申报"), block(4, "完整申报条件。"), block(5, "全部申报例外。"),
            block(6, "（2）回售撤销"), block(7, "完整撤销条件。"),
            block(8, "2. 后续计量"), block(9, "后续计量条件。"), block(10, "（二）其他债券")]
    result = select_document_sections(rows, ["b7", "b4", "b9", "b5"])
    assert [s["block_ids"] for s in result] == [["b3", "b4", "b5"], ["b6", "b7"], ["b8", "b9"]]
    assert result[0]["parent_titles"] == result[1]["parent_titles"] == ["（一）FVTPL债券", "1、业务确认"]
    assert result[2]["parent_titles"] == ["（一）FVTPL债券"]
    assert selected_ids(rows, ["b1", "b4", "b7"]) == [[f"b{i}" for i in range(1, 8)]]
    assert_coverage(rows, build_document_sections(rows))


def test_arabic_quantities_dates_footnotes_prose_and_toc_remain_in_the_enclosing_section():
    body = ["1.5%", "1.5%管理费", "1.2 费用比例为0.3%", "2026年9月11日", "2026.09.11",
            "2026. 09. 11", "2026-09-11", "2026/09/11", "1. 2026. 9. 11", "131 贷方红字。",
            "131 贷方红字", "131. 贷方红字。", "1、管理费按日计提", "（1）本基金支付费用",
            "1. 投资者应在当天申报", "1、业务确认……........22", "（1）回售申报……34"]
    rows = [block(0, "（一）FVTPL债券")]
    rows.extend(block(i, text) for i, text in enumerate(body, 1))
    rows.append(block(len(rows), "（二）其他债券"))
    sections = build_document_sections(rows)
    assert len(sections) == 2
    assert selected_ids(rows, [f"b{i}" for i in range(1, len(rows) - 1)]) == [
        [f"b{i}" for i in range(len(rows) - 1)]
    ]
    assert_coverage(rows, sections)


def test_arabic_section_keeps_cross_page_continuation_and_deduplicates_line_anchors():
    rows = [block(0, "（一）FVTPL债券", page=34), block(1, "1、回售申报", page=34),
            block(2, "回售申报所需的", page=34), block(3, "33", page=34),
            block(4, "条件及例外应完整核对。", page=35), block(5, "131 贷方红字。", page=35),
            block(6, "2. 回售撤销", page=35), block(7, "撤销条件。", page=35)]
    frozen = copy.deepcopy(rows)
    result = select_document_sections(rows, ["b4", "b2", "b4", None, "invalid"])
    assert len(result) == 1
    assert result[0]["block_ids"] == ["b1", "b2", "b3", "b4", "b5"]
    assert result[0]["start_ordinal"] == 1 and result[0]["end_ordinal"] == 5
    assert result == select_document_sections(list(reversed(rows)), ["b2"])
    assert rows == frozen
    assert_coverage(rows, build_document_sections(rows))


def test_arabic_subsection_preserves_whole_table_and_parent_intro_remains_explicitly_readable():
    rows = [block(0, "（一）FVTPL债券"), block(1, "所有子节共同适用的前提。"),
            block(2, "1、业务确认"), block(3, "全部业务确认子款的前提。"), block(4, "（1）回售申报"),
            block(5, kind="table", data={"columns": ["条件"], "rows": [[str(i)] for i in range(1000)]}),
            block(6, "完整表后说明和例外。"), block(7, "（2）回售撤销"), block(8, "撤销正文。")]
    sections = build_document_sections(rows)
    selected = select_document_sections(rows, ["b5"])[0]
    assert selected["block_ids"] == ["b4", "b5", "b6"]
    assert selected["parent_titles"] == ["（一）FVTPL债券", "1、业务确认"]
    assert sections[0]["block_ids"] == [f"b{i}" for i in range(9)]
    assert sections[1]["block_ids"] == [f"b{i}" for i in range(2, 9)]
    assert len(rows[5]["data"]["rows"]) == 1000
    assert_coverage(rows, sections)


def test_native_levels_do_not_flatten_chinese_hierarchy_when_importer_marks_everything_h2():
    rows = [block(0, "第三章 债券", kind="heading", level=2),
            block(1, "第二节 回售", kind="heading", level=2),
            block(2, "一、回售申报", kind="heading", level=2), block(3, "完整正文。"),
            block(4, "二、回售撤销", kind="heading", level=2), block(5, "撤销正文。")]
    section = select_document_sections(rows, ["b3"])[0]
    assert section["parent_titles"] == ["第三章 债券", "第二节 回售"]
    assert section["block_ids"] == ["b2", "b3"]


def test_mixed_pdf_chapter_and_native_heading_peers_close_inferred_children():
    rows = [block(0, "第三章 债券"), block(1, "回售", kind="heading", level=2),
            block(2, "第十二条 回售安排"), block(3, "回售条件。"),
            block(4, "赎回", kind="heading", level=2), block(5, "赎回条件。")]
    result = select_document_sections(rows, ["b3", "b5"])
    assert result[0]["parent_titles"] == ["第三章 债券", "回售"]
    assert result[0]["block_ids"] == ["b2", "b3"]
    assert result[1]["parent_titles"] == ["第三章 债券"]
    assert result[1]["block_ids"] == ["b4", "b5"]


def test_toc_dot_leaders_never_start_body_sections_even_if_marked_as_heading():
    rows = [block(0, "目 录", page=1),
            block(1, "第三章 债券……........22", page=1, kind="heading", level=1),
            block(2, "一、回售……………………34", page=1), block(3, "二、赎回", page=1),
            block(4, "第三章 债券", page=22), block(5, "完整章导言。", page=22),
            block(6, "一、回售", page=34), block(7, "应在申报期间完成", page=34),
            block(8, "33", page=34), block(9, "申报，撤销条件如下。", page=35),
            block(10, "（一）投资者应在当天办理。", page=35), block(11, "131 贷方红字。", page=35),
            block(12, "二、赎回", page=35), block(13, "赎回正文。", page=35)]
    sections = build_document_sections(rows)
    assert_coverage(rows, sections)
    result = select_document_sections(rows, ["b7", "b9", "b10"])
    assert len(result) == 1
    assert result[0]["title"] == "一、回售"
    assert result[0]["parent_titles"] == ["第三章 债券"]
    assert result[0]["block_ids"] == [f"b{i}" for i in range(6, 12)]
    assert all("........" not in section["title"] for section in sections)
    assert sections[0]["block_ids"] == ["b0", "b1", "b2", "b3"]


@pytest.mark.parametrize("leader", ["........22", "… … … 22", "．．．．２２", "····22", "⋯⋯22", "……22–24"])
def test_toc_leader_variants_are_not_headings(leader):
    rows = [block(0, "第三章 债券" + leader, page=1), block(1, "完整说明。", page=1)]
    assert build_document_sections(rows)[0]["boundary"] == "physical_page"


def test_unstructured_native_source_pages_have_full_page_coverage_and_source_page_wins():
    rows = [block(0, "本页第一行，", page=34, locator={"page": 999, "bbox": [10, 10, 100, 20]}),
            block(1, "本页最后一行。", page=34), block(2, "33", page=34),
            block(3, "另一页的全部正文。", page=35), block(4, "34", page=35)]
    sections = build_document_sections(rows)
    assert_coverage(rows, sections)
    assert [s["title"] for s in sections] == ["第34页", "第35页"]
    assert selected_ids(rows, ["b0", "b1", "b2"]) == [["b0", "b1", "b2"]]


@pytest.mark.parametrize("locator", [{"source_page": "34"}, {"page": 34}, {"page_number": 34}, {"label": "第34页"}])
def test_page_locator_compatibility(locator):
    rows = [block(0, "第一页。", locator=locator), block(1, "下一页。", page=35)]
    assert [s["title"] for s in build_document_sections(rows)] == ["第34页", "第35页"]


def test_cross_page_unfinished_sentence_skips_footer_and_footnote_without_discarding_them():
    rows = [block(0, "回售确认需要考虑的", page=34),
            block(1, "131 贷方红字。", page=34, locator={"bbox": [20, 760, 200, 780]}),
            block(2, "33", page=34), block(3, "条件包括申报与撤销。", page=35), block(4, "34", page=35),
            block(5, "新页独立正文。", page=36)]
    sections = build_document_sections(rows)
    assert_coverage(rows, sections)
    assert sections[0]["boundary"] == "physical_pages_continuation"
    assert sections[0]["title"] == "第34–35页"
    assert selected_ids(rows, ["b3"]) == [["b0", "b1", "b2", "b3", "b4"]]
    assert selected_ids(rows, ["b5"]) == [["b5"]]


@pytest.mark.parametrize("tail,head", [
    ("前页逗号，", "完整续句。"), ("完整说明：", "后续条件。"), ("第一项；", "第二项。"),
    ("完整说明。", "但存在以下例外。"), ("金额为3.", "14元。"), ("参见下页。", "续表：到账情况。"),
])
def test_conservative_cross_page_continuations(tail, head):
    rows = [block(0, tail, page=1), block(1, "1", page=1), block(2, head, page=2)]
    assert selected_ids(rows, ["b2"]) == [["b0", "b1", "b2"]]


def test_closed_quoted_sentence_allows_whole_page_boundary():
    rows = [block(0, "规定为“本页结束。”", page=1), block(1, "下一页独立内容。", page=2)]
    assert selected_ids(rows, ["b0"]) == [["b0"]]


def test_missing_physical_pages_do_not_invent_continuity():
    rows = [block(0, "本页未完，", page=1), block(1, "缺少第二页的另一段。", page=3)]
    assert len(build_document_sections(rows)) == 2


def test_empty_page_and_unlocated_blocks_cannot_cut_an_unfinished_sentence():
    rows = [block(0, "前置块。"), block(1, "未完成的", page=1), block(2, " ", page=2),
            block(3, "句子。", page=3), block(4, ""), block(5, "下一页。", page=4)]
    assert selected_ids(rows, ["b3"]) == [["b0", "b1", "b2", "b3", "b4"]]
    assert_coverage(rows, build_document_sections(rows))


def test_long_table_is_atomic_and_readable_in_full_without_source_mutation():
    table = {"columns": ["条款", "费用"], "rows": [[str(i), "中文明细" * 10] for i in range(20_000)]}
    rows = [block(0, "第三章 债券"), block(1, "一、回售"), block(2, "完整表格如下。"),
            block(3, kind="table", data=table), block(4, "表后完整说明。"),
            block(5, "二、赎回"), block(6, "其他内容。")]
    frozen = copy.deepcopy(rows)
    assert selected_ids(rows, ["b3"]) == [["b1", "b2", "b3", "b4"]]
    assert rows == frozen
    assert len(rows[3]["data"]["rows"]) == 20_000
    assert_coverage(rows, build_document_sections(rows))


def test_cross_page_table_pieces_keep_all_pages_and_rows():
    rows = [block(0, "下表列示完整条件：", page=1),
            block(1, kind="table", page=1, data={"columns": ["条件"], "rows": [["首行"]]}),
            block(2, "1", page=1),
            block(3, kind="table", page=2, data={"columns": ["条件"], "rows": [["末行"]]}),
            block(4, "2", page=2), block(5, "独立内容。", page=3)]
    assert selected_ids(rows, ["b3"]) == [["b0", "b1", "b2", "b3", "b4"]]
    assert_coverage(rows, build_document_sections(rows))


def test_explicit_continuation_after_table_caption_joins_pages():
    rows = [block(0, kind="table", page=1, data={"rows": [["首行"]]}),
            block(1, "表1（续）", page=2), block(2, kind="table", page=2, data={"rows": [["尾行"]]})]
    assert selected_ids(rows, ["b2"]) == [["b0", "b1", "b2"]]


def test_explicit_distinct_table_ids_allow_a_boundary():
    rows = [block(0, kind="table", page=1, data={"table_id": "A", "rows": [["第一表"]]}),
            block(1, kind="table", page=2, data={"table_id": "B", "rows": [["第二表"]]})]
    assert selected_ids(rows, ["b0"]) == [["b0"]]


def test_pdf_appended_table_blocks_still_join_table_continuation_pages():
    # PDF extraction emits all text lines first, then the full tables on a page.
    rows = [block(0, "回售明细表", page=1, locator={"bbox": [20, 620, 250, 640]}),
            block(1, "甲类条件", page=1, locator={"bbox": [20, 640, 250, 660]}),
            block(2, "1", page=1, locator={"bbox": [200, 800, 220, 815]}),
            block(3, kind="table", page=1, data={"rows": [["甲类条件"]]},
                  locator={"bbox": [20, 640, 250, 660]}),
            block(4, "乙类条件", page=2, locator={"bbox": [20, 30, 250, 50]}),
            block(5, "完整表后说明。", page=2, locator={"bbox": [20, 100, 250, 120]}),
            block(6, "2", page=2, locator={"bbox": [200, 800, 220, 815]}),
            block(7, kind="table", page=2, data={"rows": [["乙类条件"]]},
                  locator={"bbox": [20, 30, 250, 50]}),
            block(8, "下一页独立内容。", page=3)]
    assert selected_ids(rows, ["b4"]) == [[f"b{i}" for i in range(8)]]
    assert_coverage(rows, build_document_sections(rows))


def test_midpage_appended_table_does_not_hide_the_actual_unfinished_bottom_line():
    rows = [block(0, "本页表格。", page=1, locator={"bbox": [20, 40, 200, 60]}),
            block(1, "前页未完成的", page=1, locator={"bbox": [20, 700, 200, 720]}),
            block(2, kind="table", page=1, data={"rows": [["表格"]]},
                  locator={"bbox": [20, 40, 200, 60]}),
            block(3, "跨页句子。", page=2)]
    assert selected_ids(rows, ["b3"]) == [["b0", "b1", "b2", "b3"]]


def test_distinct_complete_midpage_tables_do_not_merge_independent_pages():
    rows = []
    for page in (1, 2):
        rows.extend([block(len(rows), "本页独立正文。", page=page, locator={"bbox": [20, 20, 200, 40]}),
                     block(len(rows) + 1, "本页结束。", page=page, locator={"bbox": [20, 700, 200, 720]}),
                     block(len(rows) + 2, kind="table", page=page, data={"rows": [["明细"]]},
                           locator={"bbox": [20, 200, 200, 300]})])
    assert selected_ids(rows, ["b0"]) == [["b0", "b1", "b2"]]


def test_table_text_and_lists_with_numbered_items_never_become_headings():
    rows = [block(0, "完整正文。", page=1),
            block(1, "第一章 回售", kind="table", page=1, data={"rows": [["第一章 回售"]]}),
            block(2, "一、申报", kind="list", page=1, data={"items": ["一、申报", "二、撤销"]})]
    assert len(build_document_sections(rows)) == 1
    assert selected_ids(rows, ["b2"]) == [["b0", "b1", "b2"]]


def test_no_structure_or_page_metadata_returns_every_block_without_a_character_limit():
    rows = [block(0, "完整无标题正文" * 30_000), block(1, "完整末尾。"), block(2, "")]
    result = build_document_sections(rows)
    assert_coverage(rows, result)
    assert len(result) == 1
    assert result[0]["boundary"] == "unstructured_document"
    assert result[0]["block_ids"] == ["b0", "b1", "b2"]


def test_empty_input_empty_text_and_blank_heading_are_not_lost():
    assert build_document_sections([]) == []
    assert select_document_sections([], ["b0"]) == []
    rows = [block(0, ""), block(1, "  \n\t", kind="heading", level=1),
            block(2, data=None), block(3, kind="table", data={"rows": []})]
    assert_coverage(rows, build_document_sections(rows))
    assert selected_ids(rows, ["b1"]) == [["b0", "b1", "b2", "b3"]]


def test_search_text_fallback_and_missing_data_are_supported_without_mutation():
    rows = [block(0, "第三章 债券", data={}), block(1, "一、回售", data={}), block(2, "正文。")]
    rows[0]["data"] = None
    rows[2]["locator"] = None
    frozen = copy.deepcopy(rows)
    assert selected_ids(rows, ["b2"]) == [["b1", "b2"]]
    assert rows == frozen


@pytest.mark.parametrize("anchors", [None, [], (), set(), "", "unknown", ["unknown"],
                                      [None, 1, False, {}, [], "", "  "], 7])
def test_no_valid_anchor_returns_no_sections(anchors):
    assert select_document_sections([block(0, "正文。")], anchors) == []


def test_mixed_valid_invalid_anchors_and_a_single_string_anchor():
    rows = [block(0, "一、回售"), block(1, "正文。"), block(2, "二、赎回"), block(3, "其他。")]
    assert selected_ids(rows, ["b1", {}, [], None, "missing", "b1"]) == [["b0", "b1"]]
    assert selected_ids(rows, "b1") == [["b0", "b1"]]
    assert selected_ids(rows, (anchor for anchor in ["b3", "b1"])) == [["b0", "b1"], ["b2", "b3"]]


def test_stable_ids_source_order_ordinals_with_gaps_and_duplicate_titles():
    rows = [block(10, "一、回售"), block(30, "第一段。"), block(50, "二、其他"), block(90, "其他。"),
            block(120, "一、回售"), block(140, "另一处回售。")]
    frozen = copy.deepcopy(rows)
    expected = build_document_sections(rows)
    assert_coverage(rows, expected)
    assert expected == build_document_sections(list(reversed(rows)))
    assert expected == build_document_sections(copy.deepcopy(rows))
    assert rows == frozen
    assert expected[0]["section_id"] != expected[2]["section_id"]
    renumbered = copy.deepcopy(rows)
    for index, row in enumerate(renumbered):
        row["ordinal"] = index
    assert [s["section_id"] for s in expected] == [s["section_id"] for s in build_document_sections(renumbered)]
    assert selected_ids(rows, ["b140", "b30"]) == [["b10", "b30"], ["b120", "b140"]]


def test_independent_preceding_section_does_not_change_existing_section_id():
    rows = [block(10, "一、回售"), block(11, "原有正文。")]
    before = build_document_sections(rows)[0]
    after = build_document_sections([block(0, "一、其他"), block(1, "其他正文。"), *rows])[-1]
    assert before == after


def test_every_block_can_be_anchored_including_front_matter_blank_blocks_and_final_heading():
    rows = [block(0, "前言。", page=1), block(1, "", page=1),
            block(2, "第三章 债券", page=2), block(3, "一、回售", page=2),
            block(4, "", page=2), block(5, "正文。", page=2), block(6, "第四章 股票", page=3)]
    assert_coverage(rows, build_document_sections(rows))
    for row in rows:
        result = select_document_sections(rows, [row["block_id"]])
        assert len(result) == 1
        assert row["block_id"] in result[0]["block_ids"]
    assert selected_ids(rows, ["b6"]) == [["b6"]]


@pytest.mark.parametrize("bad_id", [None, "", "   ", 12])
def test_invalid_source_identity_fails_explicitly_instead_of_omitting_a_source_block(bad_id):
    row = block(0, "原文。")
    row["block_id"] = bad_id
    with pytest.raises(ValueError, match="block_id"):
        build_document_sections([row])


def test_duplicate_source_ids_and_invalid_ordinals_are_rejected():
    with pytest.raises(ValueError, match="block_id"):
        build_document_sections([block(0), block(1, block_id="b0")])
    for value in [None, "1", True, 1.5]:
        row = block(0)
        row["ordinal"] = value
        with pytest.raises(ValueError, match="ordinal"):
            build_document_sections([row])


def test_ten_thousand_block_manual_selects_only_complete_putback_subsection():
    rows = [block(0, "会计实务手册", kind="heading", level=1)]
    target_ids = []
    for chapter in range(33):
        rows.append(block(len(rows), f"第{chapter + 1}章 业务", page=chapter * 10 + 1))
        for section in range(10):
            start = len(rows)
            title = "回售" if (chapter, section) == (17, 3) else f"事项{section}"
            rows.append(block(len(rows), f"第{section + 1}节 {title}", page=chapter * 10 + section + 1))
            for line in range(29):
                rows.append(block(len(rows), f"完整原文行{line}，含条件和例外。",
                                  page=chapter * 10 + section + 1))
            if (chapter, section) == (17, 3):
                target_ids = [row["block_id"] for row in rows[start:]]
    while len(rows) < 10_000:
        rows.append(block(len(rows), "完整附加说明。", page=331))
    assert len(rows) == 10_000
    sections = build_document_sections(rows)
    assert_coverage(rows, sections)
    result = select_document_sections(rows, target_ids[2:20] + target_ids[5:12])
    assert len(result) == 1
    assert result[0]["title"] == "第4节 回售"
    assert result[0]["parent_titles"] == ["会计实务手册", "第18章 业务"]
    assert result[0]["block_ids"] == target_ids
    assert len(result[0]["block_ids"]) == 30


def test_synthetic_randomized_coverage_and_anchor_completeness():
    rng = random.Random(20260911)
    for _ in range(20):
        rows = []
        for ordinal in range(120):
            choice = rng.randrange(8)
            if choice < 3:
                row = block(ordinal, f"合成标题{ordinal}", kind="heading", level=choice + 1)
            elif choice == 3:
                row = block(ordinal, kind="table", data={"columns": ["值"], "rows": [[ordinal]]})
            else:
                row = block(ordinal, "" if choice == 4 else f"原始正文{ordinal}。", page=ordinal // 10 + 1)
            rows.append(row)
        sections = build_document_sections(rows)
        assert_coverage(rows, sections)
        anchors = [rows[i]["block_id"] for i in rng.sample(range(len(rows)), 20)]
        chosen = select_document_sections(rows, anchors)
        assert set(anchors) <= {item for section in chosen for item in section["block_ids"]}
        assert len({s["section_id"] for s in chosen}) == len(chosen)
        for section in chosen:
            assert section in sections
        rng.shuffle(rows)
        assert build_document_sections(rows) == sections
