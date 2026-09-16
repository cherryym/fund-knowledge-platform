"""No model/network: serialized budgets preserve Unicode and do not divide by three."""
import json

import pytest

from fund_kb.answer_packing import fits_context, pack_text
from fund_kb.providers import ProviderError


@pytest.mark.parametrize("protocol", ["openai", "responses", "anthropic", "codex_app_server"])
def test_real_message_budget_packs_large_chinese_context_without_arbitrary_third(protocol):
    connection={"protocol":protocol,"model_id":"synthetic","max_request_bytes":65536}
    body="完整条款和上下文。\n"*1200
    prefix,suffix="前置研判\n","\n请综合答复。"
    fits=lambda text:fits_context("合成系统约定",text,connection)
    packets=pack_text(body,prefix,suffix,fits)
    assert len(body.encode())>22000
    assert packets==[body]
    assert fits(prefix+body+suffix)


@pytest.mark.parametrize("capacity", [4096,16384,65536])
def test_every_character_and_json_escape_is_preserved(capacity):
    connection={"protocol":"codex_app_server","model_id":"synthetic","max_request_bytes":capacity}
    text=('中文🙂 "引号" \\路径\n'+"甲乙丙"*30)*400
    prefix,suffix="说明\n","\n后缀"
    fits=lambda value:fits_context("仅处理所给资料",value,connection)
    pieces=pack_text(text,prefix,suffix,fits)
    assert "".join(pieces)==text
    assert all(fits(prefix+p+suffix) for p in pieces)
    for piece in pieces:
        assert len(json.dumps([{"role":"system","content":"仅处理所给资料"},
            {"role":"user","content":prefix+piece+suffix}],ensure_ascii=False).encode())<=capacity


def test_too_small_instruction_budget_fails_without_dropping_instructions():
    with pytest.raises(ProviderError,match="PROVIDER_CONTEXT_CAPACITY_REQUIRED"):
        pack_text("正文","不能丢弃的完整系统说明","结束",lambda text:len(text)<4)


def test_no_empty_packet_or_empty_tail_is_manufactured():
    assert pack_text("abcdef","","",lambda text:len(text)<=3)==["abc","def"]
    assert pack_text("","前","后",lambda text:len(text)<=2)==[""]


def test_evidence_label_moves_with_its_body_without_changing_a_character():
    text = "第一段全文\n[E308]\n第二段全文。\n第三段全文。\n"
    parts = pack_text(text, "", "", lambda s: len(s) <= len("第一段全文\n[E308]\n"))
    assert parts[0] == "第一段全文\n"
    assert parts[1].startswith("[E308]\n第二段")
    assert "".join(parts) == text


@pytest.mark.parametrize("capacity", [20, 21, 31, 99, 501])
def test_many_eids_unicode_and_long_lines_are_losslessly_packed(capacity):
    text = "".join(f"[E{i}]\n" + "完整条款🙂。" * (i + 1) + "\n" for i in range(40))
    parts = pack_text(text, "前", "后", lambda s: len(s) <= capacity)
    assert "".join(parts) == text
    assert all(len(p) + 2 <= capacity and p for p in parts)
    import re
    assert not any(re.search(r"(?:^|\n)\[E\d+\]\n$", p) for p in parts)


def test_label_that_cannot_fit_with_body_fails_instead_of_spinning():
    with pytest.raises(ProviderError, match="PROVIDER_CONTEXT_CAPACITY_REQUIRED"):
        pack_text("[E999]\n正文", "", "", lambda s: len(s) <= 7)
