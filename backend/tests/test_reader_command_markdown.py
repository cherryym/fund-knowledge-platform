"""Generic read-command presentation compatibility; no business-specific strings."""
import pytest

from fund_kb.wiki_reader import (
    full_read_requests,
    read_requests,
    requests_catalog,
    search_requests,
    section_read_requests,
)


@pytest.mark.parametrize("verb", ["SEARCH", "search", "Search"])
@pytest.mark.parametrize("wrapper", ["{}", "**{}**", "__{}__", "`{}`"])
@pytest.mark.parametrize("prefix", ["", "- ", "* ", "+ ", "1. ", "2) ", "3、 ", "### "])
def test_command_styles_preserve_every_argument_character(verb, wrapper, prefix):
    argument = "甲事项；乙事项 **带强调** A:B _原样_"
    assert search_requests(prefix + wrapper.format(verb) + " " + argument) == [argument]


@pytest.mark.parametrize("text", ["**SEARCH 查询甲**", "__SEARCH：查询甲__", "`SEARCH 查询甲`", "**SEARCH：** 查询甲"])
def test_whole_command_and_colon_inside_markup(text):
    assert search_requests(text) == ["查询甲"]


def test_query_order_duplicates_and_semicolons_are_not_reinterpreted():
    text = "**SEARCH** 甲；乙\nSEARCH  丙\n__SEARCH__ 甲；乙\nSEARCH: 丁"
    assert search_requests(text) == ["甲；乙", "丙", "丁"]


@pytest.mark.parametrize("text", [
    "> **SEARCH** 引文不执行", "正文提到 **SEARCH** 但不是指令", "RESEARCH 不是指令",
    "```text\nSEARCH 不执行\n```", "~~~\n**SEARCH** 不执行\n~~~", "```\nSEARCH 未关闭也不执行",
    "````\n```\nSEARCH 外层仍未关闭\n````", "**SEARCH__ 不配对标记", "**SEARCH**word 无分隔符",
])
def test_examples_quotes_and_invalid_verbs_stay_inert(text):
    assert search_requests(text) == []


def test_all_read_verbs_still_accept_only_registered_pages():
    pages = {"W1": {}, "W2": {}}
    assert read_requests("- **READ** W1 W9999\n__阅读__ W2", pages) == ["W1", "W2"]
    assert full_read_requests("1. `READ_FULL` W2 W999", pages) == ["W2"]
    assert section_read_requests("**READ_SECTION** W1 S1 S2\nREAD_SECTION W888 S9", pages) == {"W1": ["S1", "S2"]}
    assert requests_catalog("**CATALOG**")
    assert not requests_catalog("```\n**CATALOG**\n```")
    assert not full_read_requests("> READ_FULL W1", pages)
    assert not read_requests("```\nREAD W1\n```", pages)
    assert read_requests("建议阅读 W2", pages, selection=True) == ["W2"]


def test_fenced_example_does_not_hide_later_real_command():
    assert search_requests("```text\nSEARCH 示例\n```\n- **SEARCH** 实际查询") == ["实际查询"]
