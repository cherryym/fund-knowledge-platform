"""Synthetic citation punctuation regressions; no real answers or runtime I/O."""
import copy
import socket

import pytest
from test_wiki_answer_content import build, codes, record

from fund_kb.citation_map import public_citation_map
from fund_kb.wiki_answer_content import _reference_ids


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("citation syntax tests cannot call network/model services")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def citation_codes(answer):
    return {code for code in codes(answer) if code == "UNKNOWN_CITATION" or code.startswith("CITATION_RANGE_")}


@pytest.mark.parametrize("wrapper", ["[{}]", "【{}】", "［{}］", "〔{}〕"])
@pytest.mark.parametrize("separator", ["—", "——", "–", "--", "-", "－", "−", "至", "到"])
def test_closed_citation_followed_by_prose_is_a_single_reference(wrapper, separator):
    rows = [record("E41", text="合成条件甲。"), record("E42", text="无关的合成条件。")]
    before = copy.deepcopy(rows)
    markdown = f"合成说明{wrapper.format('E41')}{separator}即下一段解释。"
    answer = build(markdown, rows)
    assert [item["id"] for item in answer["citations"]] == ["E41"]
    assert answer["grounding_status"] == "SOURCE_LINKED" and citation_codes(answer) == set()
    assert answer["narrative_markdown"] == markdown and rows == before
    assert answer["citations"][0]["content_sha256"] == rows[0]["content_sha256"]
    assert answer["citations"][0]["excerpt"] == rows[0]["text"]


@pytest.mark.parametrize("tail", ["synthetic explanation.", "Explanation follows.", "（补充解释）",
    "“合成补充”。", "随后另见[E43]。", "English E43 is another citation."])
def test_prose_after_closed_citation_does_not_swallow_later_independent_mentions(tail):
    markdown = f"[ E41 ] — {tail}"
    answer = build(markdown, [record(f"E{i}") for i in (41, 42, 43)])
    expected = ["E41", "E43"] if "E43" in tail else ["E41"]
    assert [item["id"] for item in answer["citations"]] == expected
    assert citation_codes(answer) == set() and answer["narrative_markdown"] == markdown


@pytest.mark.parametrize("marker", ["[E41]-[E43]", "【E41】——【E43】", "[ E41 ] – [ E43 ]",
    "［E41］—［E43］", "〔E41〕至〔E43〕", "【E41】—E43", "E41—【E43】", "E41——E43", "E41到E43"])
def test_real_range_endpoints_still_include_every_registered_member(marker):
    answer = build(marker, [record(f"E{i}") for i in (43, 41, 42)])
    assert [item["id"] for item in answer["citations"]] == ["E41", "E42", "E43"]
    assert citation_codes(answer) == set() and answer["narrative_markdown"] == marker


@pytest.mark.parametrize("marker,warning", [
    ("【E43】——【E41】", "CITATION_RANGE_REVERSED"),
    ("【E41】——【E043】", "CITATION_RANGE_INVALID"),
    ("【E041】——【E43】", "CITATION_RANGE_INVALID"),
    ("【E41】——【E0】", "CITATION_RANGE_INVALID"),
    ("【E41】——【】", "CITATION_RANGE_INVALID"),
    ("【E41】——【E】", "CITATION_RANGE_INVALID"),
    ("【E41】——【invalid】", "CITATION_RANGE_INVALID"),
    ("【E41】——E43suffix", "CITATION_RANGE_INVALID"),
    ("【E41】——E", "CITATION_RANGE_INVALID"),
    ("【E41】——43", "CITATION_RANGE_INVALID"),
    ("【E41】——", "CITATION_RANGE_INVALID"),
    ("【E41】——。", "CITATION_RANGE_INVALID"),
    ("[E41]—.", "CITATION_RANGE_INVALID"),
    ("[E41]—E.", "CITATION_RANGE_INVALID"),
    ("【E41——】", "CITATION_RANGE_INVALID"),
    ("[E41—missing]", "CITATION_RANGE_INVALID"),
    ("E41——即合成说明。", "CITATION_RANGE_INVALID"),
])
def test_explicit_or_dangling_invalid_ranges_keep_warnings_without_binding_first_endpoint(marker, warning):
    answer = build(marker, [record(f"E{i}") for i in (41, 42, 43)])
    assert answer["citations"] == []
    assert {warning, "UNKNOWN_CITATION"} <= citation_codes(answer)
    assert answer["narrative_markdown"] == marker


@pytest.mark.parametrize("registered", [(41, 43), (42, 43), (41, 42), (42,), ()])
def test_closed_bracket_range_with_missing_registered_endpoint_or_interior_stays_incomplete(registered):
    answer = build("【E41】——【E43】", [record(f"E{i}") for i in registered])
    assert [item["id"] for item in answer["citations"]] == [f"E{i}" for i in registered]
    assert citation_codes(answer) == {"CITATION_RANGE_INCOMPLETE", "UNKNOWN_CITATION"}


def test_unknown_single_citation_before_explanatory_dash_is_still_unknown():
    answer = build("【E99】——合成说明。", [record("E41")])
    assert answer["citations"] == [] and citation_codes(answer) == {"UNKNOWN_CITATION"}


@pytest.mark.parametrize("markdown", ["&#12304;E41&#12305;&mdash;&mdash;合成说明。",
    "&#91;E41&#93;&#8212;Explanation follows."])
def test_entity_decoding_preserves_punctuation_semantics_without_rewriting_public_markdown(markdown):
    answer = build(markdown, [record("E41")])
    assert [item["id"] for item in answer["citations"]] == ["E41"]
    assert citation_codes(answer) == set() and answer["narrative_markdown"] == markdown


@pytest.mark.parametrize("code", ["`【E99】——【E101】`", "``【E99】——`bad` E101``",
    "```text\n【E99】——【E101】\n```", "~~~\n【E99】——\n~~~", "> ```\n> E99—E101\n> ```"])
def test_code_citations_remain_masked_next_to_real_citation_with_explanatory_dash(code):
    markdown = "【E41】——合成说明。\n\n" + code
    answer = build(markdown, [record("E41")])
    assert [item["id"] for item in answer["citations"]] == ["E41"]
    assert citation_codes(answer) == set() and answer["narrative_markdown"] == markdown


def test_closed_bracket_range_does_not_bridge_masked_code_to_later_reference():
    answer = build("【E41】—`not an endpoint` E43", [record(f"E{i}") for i in (41, 42, 43)])
    assert [item["id"] for item in answer["citations"]] == ["E43"]
    assert citation_codes(answer) == {"CITATION_RANGE_INVALID", "UNKNOWN_CITATION"}


def test_parser_reports_no_range_interval_for_a_closed_single_followed_by_prose():
    warnings = []
    markers, intervals = _reference_ids("【E41】——合成解释。", {"E41": []}, warnings.append)
    assert markers == ["E41"] and intervals == [] and warnings == []


def test_public_citation_map_consumes_the_fixed_parser_without_a_separate_warning_override():
    source = record("E41", text="合成说明。")
    result = public_citation_map("【E41】——合成解释。", [source])
    assert result["integrity_warnings"] == [] and result["linked_paragraphs"] == 1
    assert result["paragraphs"][0]["sources"] == [{key: source[key] for key in
        ("evidence_id", "resource_id", "version_id", "block_id", "content_sha256")}]
    assert result["answer_rewritten"] is False and result["semantic_entailment"] == "NOT_EVALUATED"
