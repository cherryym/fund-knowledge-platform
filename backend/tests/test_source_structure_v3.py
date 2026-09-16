"""Offline structural/navigation regressions with independent exact-span checks."""

import copy
import random
import socket

import pytest
from test_semantic_embedding import byte_tokens, char_tokens, check_units, record
from test_source_sections import assert_coverage, block

from fund_kb.semantic_embedding import build_semantic_units
from fund_kb.source_sections import (
    build_document_sections,
    expand_reading_sections,
    resolve_section_references,
    select_document_sections,
    select_parent_document_sections,
    select_sections_from_outline,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("structure tests cannot use network/model services")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def outline(rows):
    result = build_document_sections(rows, structure_version="v3")
    assert_coverage(rows, result)
    return result


def selected(rows, anchors):
    return select_document_sections(rows, anchors, structure_version="v3")


def ids(sections):
    return [s["block_ids"] for s in sections]


@pytest.mark.parametrize("kind", ["paragraph", "heading"])
def test_chinese_sections_enclose_articles_and_next_section_closes_previous_article(kind):
    rows = [block(0, "一、总则", kind=kind, level=2), block(1, "完整导语。"),
            block(2, "第一条 本规则适用于完整范围。"), block(3, "全部条款例外。"),
            block(4, "第二条 操作要求"), block(5, "完整要求。"),
            block(6, "二、执行程序", kind=kind, level=2), block(7, "第三条 操作顺序"),
            block(8, "全部程序。"), block(9, "三、附则", kind=kind, level=2),
            block(10, "第四条 完整说明。")]
    sections = outline(rows)
    assert sections[0]["block_ids"] == [f"b{i}" for i in range(6)]
    assert selected(rows, "b3")[0]["parent_titles"] == ["一、总则"]
    assert selected(rows, "b8")[0]["parent_titles"] == ["二、执行程序"]
    assert selected(rows, "b10")[0]["parent_titles"] == ["三、附则"]
    assert ids(selected(rows, "b5")) == [["b4", "b5"]]


def test_article_children_keep_their_own_numbering_beneath_mixed_container():
    rows = [block(0, "一、总则"), block(1, "第一条 操作要求"), block(2, "一、适用范围"),
            block(3, "范围正文。"), block(4, "二、处理方式"), block(5, "方式正文。"),
            block(6, "第二条 复核要求"), block(7, "复核正文。"), block(8, "二、附则"),
            block(9, "第三条 完整附则。")]
    sections = outline(rows)
    assert selected(rows, "b3")[0]["parent_titles"] == ["一、总则", "第一条 操作要求"]
    assert ids(selected(rows, "b5")) == [["b4", "b5"]]
    assert selected(rows, "b9")[0]["parent_titles"] == ["二、附则"]
    assert sections[0]["block_ids"] == [f"b{i}" for i in range(8)]


def test_real_chapter_and_native_peer_close_an_article_and_all_its_descendants():
    rows = [block(0, "第一章 操作"), block(1, "一、总则"), block(2, "第一条 范围"),
            block(3, "（一）子项"), block(4, "子项原文。"), block(5, "第二章 后续"),
            block(6, "后续标题", kind="heading", level=2), block(7, "第二条 后续条款。"),
            block(8, "下个标题", kind="heading", level=2), block(9, "完整后续。")]
    sections = outline(rows)
    assert sections[0]["block_ids"] == [f"b{i}" for i in range(5)]
    assert selected(rows, "b7")[0]["parent_titles"] == ["第二章 后续", "后续标题"]
    assert selected(rows, "b9")[0]["parent_titles"] == ["第二章 后续"]


@pytest.mark.parametrize("page", [None, 1])
@pytest.mark.parametrize("suffix", ["", " 1", "\t12", "………12"])
def test_toc_without_pages_or_with_body_on_same_page_is_not_a_body_heading(page, suffix):
    rows = [block(0, "目录", kind="heading", page=page),
            block(1, "一、总则" + suffix, kind="heading", page=page),
            block(2, "二、程序" + suffix, page=page),
            block(3, "一、总则", page=page), block(4, "第一条 适用完整范围。", page=page),
            block(5, "二、程序", page=page), block(6, "第二条 适用完整程序。", page=page)]
    sections = outline(rows)
    assert sections[0]["boundary"].startswith("preamble")
    assert sections[0]["block_ids"] == ["b0", "b1", "b2"]
    assert [s["title"] for s in sections[1:]] == ["一、总则", "第一条", "二、程序", "第二条"]


def test_toc_metadata_and_markerless_page_suffixes_preserve_all_source_rows():
    rows = [block(0, "第一章 操作 1"), block(1, "第二章 复核 8"),
            block(2, "并非目录中的真实标题", kind="heading", locator={"role": "toc_entry"}),
            block(3, "第一章 操作"), block(4, "操作正文。"),
            block(5, "第二章 复核"), block(6, "复核正文。")]
    sections = outline(rows)
    assert sections[0]["block_ids"] == ["b0", "b1", "b2"]
    assert ids(selected(rows, "b4")) == [["b3", "b4"]]


def test_unlisted_real_heading_after_toc_is_retained_and_plain_numeric_title_is_not_toc():
    rows = [block(0, "目录"), block(1, "第一章 操作………1"),
            block(2, "前言", kind="heading", level=1), block(3, "前言完整说明。"),
            block(4, "第一章 操作"), block(5, "1、ISO 9001"), block(6, "标准原文。")]
    assert ids(selected(rows, "b3")) == [["b2", "b3", "b4", "b5", "b6"]]
    assert selected(rows, "b6")[0]["title"] == "1、ISO 9001"
    outline(rows)


@pytest.mark.parametrize("title", [
    "1、2026年9月11日样品入库", "1. 2026-09-11服务上线", "（1）2026/9/11资料归档",
    "(1) 2026.09.11现场核验", "（1）T+1日检查", "(2) T－2日整理", "（1）T+1日",
    "一、2026年9月11日迁移流程", "（一）系统切换（2026年9月11日）",
])
def test_dated_process_headings_do_not_depend_on_asset_or_business_names(title):
    rows = [block(0, "第一章 操作"), block(1, title), block(2, "完整正文及例外。")]
    assert selected(rows, "b2")[0]["title"] == title
    outline(rows)


@pytest.mark.parametrize("text", ["1. 2026. 9. 11", "2026年9月11日", "2026.09.11", "0.15%",
                                      "1. 管理费按日计提。", "1、金额为25元", "131 贷方红字。",
                                      "1、2026年9月11日应当处理，保留原件。"])
def test_dates_quantities_prose_and_footnotes_remain_body(text):
    rows = [block(0, "一、操作"), block(1, text), block(2, "完整正文。")]
    assert len(outline(rows)) == 1


def process_rows():
    texts = ["第一章 通用程序", "（一）常规流程", "4、资料准备", "准备原文。", "5、核对记录", "核对原文。",
             "6、发布流程", "事件共同前提。", "（1）2026年9月11日试运行", "  参照4、", "5处理。",
             "借：完整事项", "贷：对应事项", "事项\t数值\n甲\t1\n乙\t2", "431 原始脚注的完整条件。",
             "(2) T+1日复核", "全部复核条件。", "7、其他流程", "其他原文。",
             "第二章 其他安排", "4、外域准备", "外域原文。", "5、外域核对", "另一段外域原文。"]
    rows = [record(i, text, title="通用操作说明", page=1 + i // 12) for i, text in enumerate(texts)]
    rows[13]["block_type"] = "table"
    rows[13]["data"].update(columns=["事项", "数值"], rows=[["甲", "1"], ["乙", "2"]])
    rows[14]["locator"]["role"] = "footnote"
    return rows


def test_complete_parent_event_keeps_entries_table_footnote_and_other_stages():
    rows = process_rows()
    frozen = copy.deepcopy(rows)
    assert ids(select_parent_document_sections(rows, ["b10", "b14", "b16"])) == [
        [f"b{i}" for i in range(6, 17)]]
    assert ids(select_parent_document_sections(rows, "b10", levels=0)) == [[f"b{i}" for i in range(8, 15)]]
    assert ids(select_parent_document_sections(rows, "b10", levels=2)) == [[f"b{i}" for i in range(1, 19)]]
    assert ids(select_parent_document_sections(rows, "b10", levels=100)) == [[f"b{i}" for i in range(19)]]
    assert select_parent_document_sections(rows, [None, "missing", {}]) == []
    assert rows == frozen


def assert_reference_spans(rows, result):
    by_id = {r["block_id"]: r for r in rows}
    for ref in result["references"]:
        pieces = []
        for span in ref["source_spans"]:
            row = by_id[span["block_id"]]
            value = row
            for key in span["text_field"].split("."):
                value = value[key]
            fragment = value[span["start"]:span["end"]]
            assert fragment == ref["text"][span["text_start"]:span["text_end"]]
            assert span["content_sha256"] == row.get("content_sha256")
            pieces.append(fragment)
        assert "\n".join(pieces) == ref["text"]


def test_explicit_cross_block_4_5_reference_resolves_only_in_its_actual_scope():
    rows = process_rows()
    result = resolve_section_references(rows, "b10")
    assert ids(result["sections"]) == [["b2", "b3"], ["b4", "b5"]]
    assert result["warnings"] == []
    assert len(result["references"]) == 1
    assert result["references"][0]["numbers"] == [4, 5]
    assert result["references"][0]["status"] == "resolved"
    assert_reference_spans(rows, result)
    sections = outline(rows)
    chosen = selected(rows, "b10")
    expanded = expand_reading_sections(sections, chosen, blocks=rows, purpose="parent_and_dependencies")
    assert ids(expanded["sections"]) == [["b2", "b3"], ["b4", "b5"], [f"b{i}" for i in range(6, 17)]]


@pytest.mark.parametrize("reference,numbers", [
    ("参照4、5处理", [4, 5]), ("参照第4、5项处理", [4, 5]), ("参照四、五处理", [4, 5]),
    ("依据第４、５项规定", [4, 5]), ("参照4至5处理", [4, 5]),
])
def test_numbered_references_are_format_agnostic(reference, numbers):
    rows = [block(0, "一、操作"), block(1, "4、准备"), block(2, "正文。"), block(3, "5、核对"),
            block(4, "正文。"), block(5, "6、执行"), block(6, reference)]
    result = resolve_section_references(rows, "b6")
    assert ids(result["sections"]) == [["b1", "b2"], ["b3", "b4"]]
    assert result["references"][0]["numbers"] == numbers
    assert_reference_spans(rows, result)


@pytest.mark.parametrize("reference", ["参照第四条规定处理。", "依照本节第4条办理。", "第四条规定适用本次操作。"])
def test_explicit_article_references_and_heading_self_reference_are_distinguished(reference):
    rows = [block(0, "第一章 总体"), block(1, "第一节 操作"), block(2, "第四条 准备要求。"),
            block(3, "第五条 执行要求。"), block(4, reference),
            block(5, "第二章 其他"), block(6, "第四条 另一处条款。")]
    result = resolve_section_references(rows, "b4")
    assert ids(result["sections"]) == [["b2"]]
    assert len(result["references"]) == 1
    assert_reference_spans(rows, result)


@pytest.mark.parametrize("reference", ["参照《其他办法》第四条处理。", "参照上一章第4项处理。",
                                      "参照附件第4项处理。"])
def test_foreign_document_chapter_or_attachment_never_resolves_locally(reference):
    rows = [block(0, "一、操作"), block(1, "第四条 完整规则。"), block(2, "第六条 操作要求。"),
            block(3, "4、准备"), block(4, "准备原文。"), block(5, "6、执行"), block(6, reference)]
    result = resolve_section_references(rows, "b6")
    assert result["sections"] == []
    assert result["warnings"]
    assert all(r["status"] == "external" for r in result["references"])


def test_duplicate_numbers_are_ambiguous_and_missing_targets_cannot_escape_scope():
    rows = [block(0, "第一章 操作"), block(1, "4、甲准备"), block(2, "4、乙准备"),
            block(3, "5、核对"), block(4, "6、执行"), block(5, "参照4、5处理。"),
            block(6, "第二章 其他"), block(7, "4、唯一准备"), block(8, "5、唯一核对")]
    result = resolve_section_references(rows, "b5")
    assert result["sections"] == []
    assert result["references"][0]["status"] == "ambiguous"
    rows[1] = block(1, "3、甲准备")
    rows[2] = block(2, "2、乙准备")
    result = resolve_section_references(rows, "b5")
    assert result["sections"] == []
    assert result["references"][0]["status"] == "unresolved"


@pytest.mark.parametrize("text", ["参照2026年规定办理。", "按照4.5%处理。", "参照4、5元处理。", "金额4、5，合计9。"])
def test_numerical_quantities_are_not_dependency_links(text):
    rows = [block(0, "4、准备"), block(1, "5、核对"), block(2, "6、执行"), block(3, text)]
    result = resolve_section_references(rows, "b3")
    assert result == {"sections": [], "references": [], "warnings": []}


def test_dependency_closure_follows_long_chains_and_terminates_cycles_without_mutation():
    rows = [block(0, "一、流程")]
    for number in range(1, 13):
        rows.extend([block(len(rows), f"{number}、环节"),
                     block(len(rows) + 1, f"参照{number % 12 + 1}处理。")])
    sections = outline(rows)
    chosen = selected(rows, "b2")
    frozen = copy.deepcopy((rows, sections, chosen))
    result = expand_reading_sections(sections, chosen, blocks=rows)
    assert len(result["sections"]) == len(result["references"]) == 12
    assert result["warnings"] == []
    assert (rows, sections, chosen) == frozen
    assert_reference_spans(rows, result)
    assert expand_reading_sections(sections, chosen, blocks=list(reversed(rows))) == result


def test_parent_expansion_without_blocks_and_dependency_missing_blocks_are_explicit():
    rows = process_rows()
    sections, chosen = outline(rows), selected(rows, "b10")
    result = expand_reading_sections(sections, chosen, purpose="parent")
    assert ids(result["sections"]) == [[f"b{i}" for i in range(6, 17)]]
    assert result["warnings"] == []
    result = expand_reading_sections(sections, chosen)
    assert result["sections"] == chosen
    assert result["warnings"] == [{"code": "blocks_required"}]


def test_expansion_rejects_stale_selection_mixed_versions_and_incomplete_source():
    rows = process_rows()
    sections, chosen = outline(rows), selected(rows, "b10")
    stale = copy.deepcopy(chosen)
    stale[0]["block_ids"] = ["b10"]
    with pytest.raises(ValueError, match="selected"):
        expand_reading_sections(sections, stale, blocks=rows)
    with pytest.raises(ValueError, match="same complete source"):
        expand_reading_sections(sections, chosen, blocks=rows[:-1])
    rows[0]["version_id"] = "another-version"
    with pytest.raises(ValueError, match="one resource/version"):
        expand_reading_sections(sections, chosen, blocks=rows)


@pytest.mark.parametrize("value", [None, 2, 3, "v1", "V3", [], {}])
def test_unknown_structure_version_does_not_silently_fall_back(value):
    with pytest.raises(ValueError, match="structure_version"):
        build_document_sections([], structure_version=value)
    with pytest.raises(ValueError, match="structure_version"):
        build_semantic_units([], token_count=len, structure_version=value)


@pytest.mark.parametrize("levels", [None, True, 1.5, -1])
def test_invalid_parent_depth_is_rejected(levels):
    with pytest.raises(ValueError, match="levels"):
        select_parent_document_sections([], [], levels=levels)


def test_v2_default_groups_and_span_contract_are_retained_and_v3_ids_are_distinct():
    rows = [record(0, "一、总则"), record(1, "第一条 完整适用范围。"),
            record(2, "二、操作流程"), record(3, "第二条 完整流程。")]
    legacy = build_semantic_units(rows, token_count=char_tokens)
    # Frozen from the pre-change module, not recomputed using the new algorithm.
    assert [u["unit_id"] for u in legacy] == [
        "semantic_7db88181ab610e7b54b6e9c7d51c1f8748c3afece4017ee3f340d119f378078f",
        "semantic_9842863dc50b365e8bc58916982f203a5cbf001eba2854c3a718f250d4935727",
        "semantic_e28910f58dc6a806ea1a7bef1adb0849db6f6841a56490c6d4c535d3443c14a9",
        "semantic_0675c69502c4beaa4df5dcc3d5f1b632e7efed1387793101a301ef1a1839ba7c",
    ]
    assert legacy == build_semantic_units(rows, token_count=char_tokens, structure_version="v2")
    assert build_document_sections(rows) == build_document_sections(rows, structure_version="v2")
    assert legacy[2]["section_path"] == ["第一条", "二、操作流程"]
    units = build_semantic_units(rows, token_count=char_tokens, structure_version="v3")
    assert units[1]["section_path"] == ["一、总则", "第一条"]
    assert units[3]["section_path"] == ["二、操作流程", "第二条"]
    assert {u["unit_id"] for u in legacy}.isdisjoint(u["unit_id"] for u in units)
    check_units(rows, legacy)
    check_units(rows, units)
    plain = [record(0, "相同完整原文。")]
    v2 = build_semantic_units(plain, token_count=char_tokens)[0]
    v3 = build_semantic_units(plain, token_count=char_tokens, structure_version="v3")[0]
    assert v2["source_spans"] == v3["source_spans"]
    assert v2["unit_id"] != v3["unit_id"]


@pytest.mark.parametrize("budget,counter", [(480, char_tokens), (90, char_tokens), (64, byte_tokens)])
def test_v3_every_source_character_hash_and_span_remains_reversible(budget, counter):
    rows = process_rows()
    frozen = copy.deepcopy(rows)
    units = build_semantic_units(rows, token_count=counter, max_tokens=budget, overlap_tokens=16,
                                 context_tokens=24, structure_version="v3")
    check_units(rows, units, counter, budget=budget, context=24)
    assert rows == frozen
    if budget == 480:
        unit = next(u for u in units if "b11" in u["block_ids"])
        assert {"b8", "b9", "b10", "b11", "b12", "b13", "b14"} <= set(unit["block_ids"])
        assert unit["section_path"][-2:] == ["6、发布流程", "（1）2026年9月11日试运行"]


def test_v3_randomized_laminar_coverage_order_and_selection():
    rng = random.Random(20260911)
    for _ in range(15):
        rows = []
        for i in range(150):
            title = rng.choice([f"第{i + 1}章 章标题", f"第{i + 1}条 完整条款。", "一、总则", "二、流程",
                                "（一）子项", "1、T+1日检查", "2、2026年9月11日核验", "正文。", ""])
            rows.append(record(i, title))
        sections = outline(rows)
        result = expand_reading_sections(sections, select_sections_from_outline(sections, {"b25", "b80"}),
                                         purpose="parent")
        assert {"b25", "b80"} <= {bid for s in result["sections"] for bid in s["block_ids"]}
        rng.shuffle(rows)
        assert outline(rows) == sections


@pytest.mark.parametrize("kind", ["journal", "table"])
def test_fitting_entry_or_table_with_footnote_moves_whole_to_next_unit(kind):
    rows = [record(0, "1、完整流程"), record(1, "完整的前提。" * 5), record(2, "以下记录：")]
    if kind == "journal":
        rows.extend([record(3, "借：完整借方及金额"), record(4, "贷：完整贷方及金额")])
    else:
        rows.append(record(3, "项目\t金额\n甲项\t10\n乙项\t10", kind="table"))
    rows.append(record(len(rows), "431 完整正负号脚注。", kind="footnote"))
    budget = 65
    units = build_semantic_units(rows, token_count=char_tokens, max_tokens=budget, overlap_tokens=0,
                                 context_tokens=32, structure_version="v3")
    assert len(units) >= 2
    assert any({r["block_id"] for r in rows[2:]} <= set(u["block_ids"]) for u in units)
    check_units(rows, units, budget=budget, context=32)


def test_oversized_v3_table_keeps_rows_and_repeated_exact_header_spans():
    header = "事项\t说明"
    values = [f"事项{i:03d}\t完整条件" for i in range(40)]
    rows = [record(0, "\n".join([header, *values]), kind="table")]
    units = build_semantic_units(rows, token_count=char_tokens, max_tokens=90,
                                 context_tokens=0, overlap_tokens=20, structure_version="v3")
    assert len(units) > 2
    assert all(u["text"].startswith(header + "\n") for u in units)
    assert all(any(row in u["text"] for u in units) for row in values)
    check_units(rows, units, budget=90, context=0)


def test_oversized_entry_remains_lossless_and_fully_readable():
    rows = [record(0, "1、完整流程"), record(1, "借：" + "原始借方金额" * 100),
            record(2, "贷：" + "原始贷方金额" * 100), record(3, "431 完整脚注。", kind="footnote")]
    units = build_semantic_units(rows, token_count=byte_tokens, max_tokens=90,
                                 context_tokens=16, overlap_tokens=16, structure_version="v3")
    check_units(rows, units, byte_tokens, budget=90, context=16)
    assert ids(selected(rows, "b2")) == [["b0", "b1", "b2", "b3"]]


def test_dotted_body_references_do_not_swallow_a_later_final_heading():
    rows = [block(0, "（一）原文"), block(1, "1、目录式引用………4"), block(2, "2、目录式引用………5"),
            block(3, "（二）下一节")]
    assert [s["title"] for s in outline(rows)] == ["（一）原文", "（二）下一节"]


def test_qualified_clause_path_is_not_falsely_resolved_as_two_sibling_numbers():
    rows = [block(0, "一、规则"), block(1, "第三条 原始要求。"), block(2, "第五条 后续要求。"),
            block(3, "1、同号子项"), block(4, "不应误选的原文。"), block(5, "2、当前子项"),
            block(6, "参照第三条第一款处理。")]
    result = resolve_section_references(rows, "b6")
    assert result["sections"] == []
    assert len(result["references"]) == 1
    assert result["references"][0]["text"] == "第三条第一款"
    assert result["references"][0]["status"] == "unresolved"
    assert_reference_spans(rows, result)


def test_toc_inside_a_document_title_range_does_not_create_reference_edges():
    rows = [block(0, "合成手册", kind="heading", level=1), block(1, "目录"),
            block(2, "第一章 操作 1"), block(3, "第二章 复核 2"),
            block(4, "第一章 操作"), block(5, "完整操作。"), block(6, "第二章 复核"), block(7, "完整复核。")]
    sections = outline(rows)
    result = expand_reading_sections(sections, [sections[0]], blocks=rows)
    assert result["references"] == result["warnings"] == []


def test_source_block_wrapping_cannot_turn_a_foreign_article_into_a_local_one():
    rows = [block(0, "第一节 操作"), block(1, "第四条 准备要求。"), block(2, "第五条 执行要求。"),
            block(3, "参照《另一文档》"), block(4, "第四条规定办理。")]
    result = resolve_section_references(rows, "b4")
    assert result["sections"] == []
    assert result["references"][0]["status"] == "external"


def test_reference_before_next_article_heading_is_not_a_compound_citation():
    rows = [block(0, "第一节 操作"), block(1, "第四条 准备要求。"), block(2, "第五条 执行要求。"),
            block(3, "参照第四条"), block(4, "第六条 复核要求。")]
    result = resolve_section_references(rows, "b0")
    assert ids(result["sections"]) == [["b1"]]
    assert result["warnings"] == []


def test_compound_citation_at_paragraph_start_remains_in_its_original_article():
    rows = [block(0, "第一节 操作"), block(1, "第三条 原始规则。"), block(2, "第五条 后续规则。"),
            block(3, "第三条第一款规定适用本次操作。")]
    assert ids(selected(rows, "b3")) == [["b2", "b3"]]
    result = resolve_section_references(rows, "b3")
    assert result["sections"] == []
    assert result["references"][0]["status"] == "unresolved"
