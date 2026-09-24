"""Synthetic reading counterexamples, not financial/regulatory accuracy tests.

Only public pure entrypoints are exercised; no app, DB, corpus, model or network.
Upstream scope/receipt defects are ordinary regression tests after integration.
"""
import copy
import itertools
import re
import socket

import pytest
from test_semantic_embedding import char_tokens, record

from fund_kb.evidence_context import bind_context_receipts, context_instructions, context_plan, order_context_groups
from fund_kb.semantic_embedding import build_semantic_units
from fund_kb.source_associations import complete_source_context
from fund_kb.source_sections import (
    build_document_sections,
    resolve_section_references,
    select_sections_from_outline,
)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("synthetic structure audit forbids network/model access")
    for target, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                         (socket.socket, "connect"), (socket.socket, "connect_ex")):
        monkeypatch.setattr(target, name, forbidden)


def read_context(rows, anchors=(), *, locators=()):
    frozen = copy.deepcopy(rows)
    outline = build_document_sections(rows, structure_version="v3")
    original_outline = copy.deepcopy(outline)
    result = complete_source_context(outline, select_sections_from_outline(outline, set(anchors)),
                                     blocks=rows, locators=locators)
    assert rows == frozen and outline == original_outline
    ids = {bid for section in result["sections"] for bid in section["block_ids"]}
    assert all(section in outline for section in result["sections"])
    assert_reference_spans(rows, result)
    return result, [row for row in rows if row["block_id"] in ids]


def assert_reference_spans(rows, result):
    source = {row["block_id"]: row for row in rows}
    for ref in result["references"]:
        fragments = []
        for span in ref["source_spans"]:
            row = source[span["block_id"]]
            value = row
            for key in span["text_field"].split("."):
                value = value[key]
            fragment = value[span["start"]:span["end"]]
            assert fragment == ref["text"][span["text_start"]:span["text_end"]]
            assert span["content_sha256"] == row["content_sha256"]
            fragments.append(fragment)
        assert "\n".join(fragments) == ref["text"]


def semantic_units(rows, *, budget=48, overlap=8):
    return build_semantic_units(rows, token_count=char_tokens, max_tokens=budget,
                                overlap_tokens=overlap, context_tokens=0, structure_version="v3")


def synthetic_rules():
    texts = ["第一条 合成入口", "请参照第二条第一项执行。", "第二条 合成规则", "一、条件",
        "条件A：ALLOW_IF flag_a。检查项参见附表C。", "二、例外", "但例外B：DENY_IF flag_b。",
        "第三条 无关", "UNRELATED_SOURCE。", "附表C 检查表",
        "检查项\t合格值\n" + "\n".join(f"check_{i}\ttrue" for i in range(8)),
        "注C：REQUIRE_ALL_ROWS。表中任一检查项不合格时，不允许动作。"]
    rows = [record(i, text, title="合成逻辑规则") for i, text in enumerate(texts)]
    rows[9].update(block_type="heading", data={"text": texts[9], "level": 1})
    rows[10]["block_type"] = "table"
    rows[11]["block_type"] = "footnote"
    return rows


def formal_decision(evidence, values):
    """Test-only bounded grammar interpreter; missing premises produce UNKNOWN.

    This does not execute source code or infer law. The independently specified
    truth table below tests the conjunction, exception veto and ALL quantifier,
    not merely how many characters survived chunking.
    """
    text = "\n".join(row["text"] for row in evidence)
    allow = re.findall(r"ALLOW_IF (\w+)", text)
    deny = re.findall(r"DENY_IF (\w+)", text)
    tables = [r["text"] for r in evidence if r["block_type"] == "table"
              and r["text"].startswith("检查项\t合格值\n")]
    if len(allow) != 1 or len(deny) != 1 or len(tables) != 1 or "REQUIRE_ALL_ROWS" not in text:
        return None
    criteria = [line.split("\t") for line in tables[0].splitlines()[1:]]
    assert criteria and all(len(c) == 2 and c[1] == "true" for c in criteria)
    return values[allow[0]] and not values[deny[0]] and all(values[key] for key, _ in criteria)


@pytest.mark.parametrize("anchor", ["b1", "b4", "b6"])
def test_condition_a_exception_b_annex_c_truth_table_from_actual_semantic_hits(anchor):
    rows = synthetic_rules()
    units = semantic_units(rows)
    hit = next(unit for unit in units if anchor in unit["block_ids"])
    assert not {"b4", "b6", "b10", "b11"} <= set(hit["block_ids"])
    frozen_units = copy.deepcopy(units)
    result, evidence = read_context(rows, hit["block_ids"])
    assert {"b4", "b6", "b10", "b11"} <= {row["block_id"] for row in evidence}
    assert "b8" not in {row["block_id"] for row in evidence}
    assert all(ref["status"] == "resolved" for ref in result["references"])
    for a, b, all_c in itertools.product((False, True), repeat=3):
        values = {"flag_a": a, "flag_b": b, **{f"check_{i}": True for i in range(8)}}
        values["check_7"] = all_c
        assert formal_decision(evidence, values) is (a and not b and all_c)
    # A missing veto, header, condition or note must be UNKNOWN even if replaced
    # by the same number of characters; length/character coverage cannot pass.
    for missing in ("b4", "b6", "b10", "b11"):
        damaged = copy.deepcopy(evidence)
        row = next(row for row in damaged if row["block_id"] == missing)
        row["text"] = "X" * len(row["text"])
        assert formal_decision(damaged, values) is None
    assert semantic_units(rows) == frozen_units  # No splitter/config/ID mutation.


@pytest.mark.parametrize("exception", ["但例外B成立时不得操作。", "例外B成立的情形除外。", "但是B成立时禁止操作。"])
def test_main_rule_and_exception_split_across_chunks_rejoin_in_the_complete_section(exception):
    rows = [record(0, "第一条 合成规则"), record(1, "条件A成立时允许操作。" + "必要说明。" * 20),
        record(2, exception), record(3, "第二条 无关"), record(4, "UNRELATED。")]
    units = semantic_units(rows)
    hit = next(unit for unit in units if "条件A" in unit["text"])
    assert exception not in hit["text"]
    _, evidence = read_context(rows, hit["block_ids"])
    assert {row["block_id"] for row in evidence} == {"b0", "b1", "b2"}
    assert evidence[-1]["text"] == exception


def test_every_oversized_table_hit_recovers_header_all_rows_and_footnote():
    rows = synthetic_rules()
    hits = [unit for unit in semantic_units(rows) if "b10" in unit["block_ids"]]
    assert len(hits) > 2 and any("b11" not in unit["block_ids"] for unit in hits)
    for hit in hits:
        _, evidence = read_context(rows, hit["block_ids"])
        assert {row["block_id"] for row in evidence} == {"b9", "b10", "b11"}
        table = next(row for row in evidence if row["block_type"] == "table")
        assert table["text"].splitlines()[0] == "检查项\t合格值"
        assert table["text"].splitlines()[1:] == [f"check_{i}\ttrue" for i in range(8)]
        assert "REQUIRE_ALL_ROWS" in evidence[-1]["text"]


def test_physical_continuation_keeps_table_header_and_typed_footnote_across_pages():
    rows = [record(0, "检查项\t合格值\ncheck_a\ttrue", kind="table", page=1),
        record(1, "check_b\ttrue", kind="table", page=2),
        record(2, "1 每项均必须合格。", kind="footnote", page=2),
        record(3, "独立页面的完整文字。", page=3)]
    for row in rows[:2]:
        row["data"]["table_id"] = "synthetic-C"
    # An explicit different table establishes a boundary on the following page.
    rows[3]["block_type"] = "table"
    rows[3]["data"]["table_id"] = "synthetic-D"
    _, evidence = read_context(rows, ["b1"])
    assert {row["block_id"] for row in evidence} == {"b0", "b1", "b2"}


def test_separated_footnote_keeps_exact_spans_and_transitively_loads_its_annex():
    rows = [record(0, "第一条 主规则"), record(1, "动作受[^constraint]限定。"),
        record(2, "脚注说明", kind="heading", level=1),
        record(3, "[^constraint]: 条件A：仅附表合格时允许。参见附表C。", kind="footnote"),
        record(4, "附表C 清单", kind="heading", level=1),
        record(5, "检查项\t合格值\ncheck_a\ttrue", kind="table"),
        record(6, "其他", kind="heading", level=1), record(7, "UNRELATED。")]
    hit = next(u for u in semantic_units(rows) if "b1" in u["block_ids"])
    result, evidence = read_context(rows, hit["block_ids"])
    assert {r["block_id"] for r in evidence} == {f"b{i}" for i in range(6)}
    assert [(r["text"], r["status"]) for r in result["references"]] == [
        ("[^constraint]", "resolved"), ("参见附表C", "resolved")]


def catalog_page(rows):
    return {"kind": "document", "title": rows[0]["title"], "resource_id": rows[0]["resource_id"],
            "version_id": rows[0]["version_id"], "records": []}


def load_page(page, rows, anchors=(), *, locators=()):
    result, evidence = read_context(rows, anchors, locators=locators)
    incoming = bind_context_receipts(page, result["incoming"], build_document_sections(rows, structure_version="v3"), evidence)
    page.update(records=evidence, reading_dependencies=result["references"], incoming_context=incoming,
                context_structural_additions=result["structural_additions"])
    return result


def test_exact_cross_document_attachment_closes_only_after_its_precise_read():
    source = [record(0, "第一条 入口", resource="A", title="规则甲"),
        record(1, "参见《规则乙》附表C。", resource="A", title="规则甲")]
    target = [record(0, "附表C 合成表", resource="B", title="规则乙", kind="heading", level=1),
        record(1, "CELL_C。", resource="B", title="规则乙"),
        record(2, "无关部分", resource="B", title="规则乙", kind="heading", level=1),
        record(3, "UNRELATED。", resource="B", title="规则乙")]
    pages = {"W1": catalog_page(source), "W2": catalog_page(target)}
    load_page(pages["W1"], source, ["b1"])
    plan = context_plan(pages, {"W1"})
    assert plan["requests"] == {"W2": {"附表C"}} and plan["report"]["status"] == "PENDING"
    load_page(pages["W2"], target, locators=plan["requests"]["W2"])
    assert [r["block_id"] for r in pages["W2"]["records"]] == ["b0", "b1"]
    final = context_plan(pages, set(pages))
    assert final["requests"] == {} and final["report"]["status"] == "OBSERVED_REFERENCES_CLOSED"
    assert final["report"]["professional_completeness"] == "NOT_EVALUATED"
    assert "不代表全部语义条件或专业结论完整" in context_instructions(final["report"])
    denied = context_plan(pages, set(pages), unavailable={"W2"})
    assert denied["report"]["status"] == "GAPS_REMAIN"
    assert denied["report"]["references"][0]["status"] == "unavailable"


@pytest.mark.parametrize("catalog_titles,reference,status", [
    ([], "《规则乙》第三条", "not_in_authorized_catalog"),
    (["规则乙"], "《规则乙》", "locator_required"),
    (["规则乙", "规则乙"], "《规则乙》第三条", "ambiguous"),
    (["规则乙"], "《规则乙》（2020年版）第三条", "not_in_authorized_catalog"),
])
def test_absent_locator_authority_or_unique_edition_never_causes_whole_book_read(catalog_titles, reference, status):
    source = [record(0, "第一条 入口", title="规则甲"), record(1, f"参见{reference}。", title="规则甲")]
    pages = {"W1": catalog_page(source)}
    load_page(pages["W1"], source, ["b1"])
    for i, title in enumerate(catalog_titles, 2):
        # The two matching titles intentionally represent different editions.
        rows = [record(0, "第三条 内容", title=title, resource="other", version=f"v{i}")]
        pages[f"W{i}"] = catalog_page(rows)
    plan = context_plan(pages, {"W1"})
    assert plan["requests"] == {} and plan["report"]["status"] == "GAPS_REMAIN"
    assert plan["report"]["references"][0]["status"] == status
    assert all(not page["records"] for pid, page in pages.items() if pid != "W1")


def test_explicit_edition_selects_only_its_exact_authorized_title():
    source = [record(0, "第一条 入口", title="规则甲"),
        record(1, "参见《规则乙》（版本甲）第三条。", title="规则甲")]
    old = [record(0, "第三条 指定版本", title="规则乙（版本甲）", resource="B", version="old")]
    current = [record(0, "第三条 当前版本", title="规则乙", resource="B", version="new")]
    pages = {"W1": catalog_page(source), "W2": catalog_page(old), "W3": catalog_page(current)}
    load_page(pages["W1"], source, ["b1"])
    assert context_plan(pages, {"W1"})["requests"] == {"W2": {"第三条"}}


def test_long_dependency_cycle_terminates_without_a_depth_cutoff_or_duplicate_references():
    count = 41
    rows = []
    for i in range(count):
        rows += [record(2 * i, f"第{i + 1}条 环节"), record(2 * i + 1, f"参照第{(i + 1) % count + 1}条核对。")]
    rows += [record(2 * count, "第一百条 无关"), record(2 * count + 1, "UNRELATED。")]
    result, evidence = read_context(rows, ["b1"])
    assert len(evidence) == count * 2 and len(result["references"]) == count
    assert all(r["status"] == "resolved" for r in result["references"])
    again, _ = read_context(list(reversed(rows)), ["b1"])
    assert again == result


def test_cross_document_cycle_reaches_closure_and_emits_each_page_once():
    a = [record(0, "第一条 条件", title="规则甲", resource="A"),
         record(1, "按《规则乙》第二条处理。", title="规则甲", resource="A")]
    b = [record(0, "第二条 例外", title="规则乙", resource="B"),
         record(1, "见《规则甲》第一条。", title="规则乙", resource="B")]
    pages = {"W1": catalog_page(a), "W2": catalog_page(b)}
    load_page(pages["W1"], a, ["b1"])
    plan = context_plan(pages, {"W1"})
    load_page(pages["W2"], b, locators=plan["requests"]["W2"])
    plan = context_plan(pages, set(pages))
    assert plan["requests"] == {"W1": {"第一条"}}
    load_page(pages["W1"], a, locators=plan["requests"]["W1"])
    final = context_plan(pages, set(pages))
    assert final["requests"] == {} and final["report"]["resolved_count"] == 2
    order, receipt = order_context_groups("合成条件", pages, set(pages), final["edges"], None, {})
    assert order == ["W1", "W2"] and receipt["dropped_pages"] == 0
    assert receipt["groups"] == [["W1", "W2"], ["W2", "W1"]]


def test_reused_block_ids_do_not_mix_semantic_versions_and_mixed_reading_input_is_rejected():
    old = [record(0, "第一条 条件", version="old"), record(1, "OLD_RULE。", version="old")]
    new = [record(0, "第一条 条件", version="new"), record(1, "NEW_RULE。", version="new")]
    units = semantic_units([*old, *new])
    assert {u["unit_id"] for u in semantic_units(old)}.isdisjoint(u["unit_id"] for u in semantic_units(new))
    assert all(not ("OLD_RULE" in u["text"] and "NEW_RULE" in u["text"]) for u in units)
    outline = build_document_sections(old, structure_version="v3")
    with pytest.raises(ValueError, match="one resource/version"):
        complete_source_context(outline, outline, blocks=[old[0], new[1]])
    with pytest.raises(ValueError, match="same complete source"):
        complete_source_context(outline, outline, blocks=old[:-1])


def test_compound_natural_paragraph_remains_a_precision_gap():
    rows = [record(0, "第一条 入口"), record(1, "参照第二条第一款。"),
        record(2, "第二条 规则"), record(3, "完整条文及其自然段。")]
    result, _ = read_context(rows, ["b1"])
    assert result["references"][0]["status"] == "broader_context"
    page = catalog_page(rows)
    page["reading_dependencies"] = result["references"]
    plan = context_plan({"W1": page}, {"W1"})
    assert plan["report"]["gap_count"] == 1 and plan["report"]["status"] == "GAPS_REMAIN"


def test_standalone_natural_paragraph_must_not_resolve_to_a_numbered_item():
    rows = [record(0, "第一条 入口"), record(1, "根据第一款处理。"),
        record(2, "一、编号项"), record(3, "这不是自然款。")]
    # Reproduce directly in the prohibited upstream module, before associations.
    result = resolve_section_references(rows, ["b1"], structure_version="v3")
    assert result["references"][0]["status"] != "resolved"


def test_natural_paragraph_scope_must_not_be_guessed_from_numbered_item_parent():
    rows = [record(0, "第一条 入口"), record(1, "一、编号事项"), record(2, "按照本款第一项执行。"),
            record(3, "（一）编号子项"), record(4, "编号层级不证明自然款归属。")]
    result = resolve_section_references(rows, ["b2"], structure_version="v3")
    assert result["references"][0]["status"] != "resolved"


def test_a_cached_incoming_receipt_without_a_target_read_cannot_close_the_dependency():
    source = [record(0, "第一条 入口", title="规则甲"), record(1, "参见《规则乙》第三条。", title="规则甲")]
    target = [record(0, "第三条 目标", title="规则乙", resource="B")]
    pages = {"W1": catalog_page(source), "W2": catalog_page(target)}
    load_page(pages["W1"], source, ["b1"])
    load_page(pages["W2"], target, locators=["第三条"])
    pages["W2"]["records"] = []
    plan = context_plan(pages, {"W1"})
    assert plan["report"]["status"] != "OBSERVED_REFERENCES_CLOSED"


def test_old_version_receipt_cannot_certify_a_replaced_catalog_version():
    source = [record(0, "第一条 入口", title="规则甲"), record(1, "参见《规则乙》第三条。", title="规则甲")]
    target = [record(0, "第三条 旧条件", title="规则乙", resource="B", version="old")]
    pages = {"W1": catalog_page(source), "W2": catalog_page(target)}
    load_page(pages["W1"], source, ["b1"])
    load_page(pages["W2"], target, locators=["第三条"])
    pages["W2"]["version_id"] = "new"
    assert pages["W2"]["records"][0]["version_id"] != pages["W2"]["version_id"]
    plan = context_plan(pages, set(pages))
    assert plan["report"]["status"] != "OBSERVED_REFERENCES_CLOSED"
