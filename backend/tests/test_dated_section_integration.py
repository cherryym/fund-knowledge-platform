from fund_kb.source_sections import build_document_sections


def test_dated_parenthetical_stage_and_footnote_are_heading_not_body_colon():
    text = ["三、项目业务", "（三）处理流程", "6、材料交接",
            "（1）意向申报日（T 日：减少待交接数量431）", "第一阶段正文。",
            "（2）交接空头资料（T+1 日：交付日）", "第二阶段正文。",
            "（3）缴款交收（T+2 日：配对缴款日）", "第三阶段正文。",
            "（4）多头资料处理：（T+3 日：接收日）", "第四阶段正文。",
            "7、其他事项", "（1）付款：应在T+1日进行，并确认结果。"]
    blocks = [{"block_id": str(i), "ordinal": i, "block_type": "paragraph", "search_text": t}
              for i, t in enumerate(text)]
    result = build_document_sections(blocks, structure_version="v3")
    assert all(any(s["start_ordinal"] == i and s["end_ordinal"] == i + 1 for s in result)
               for i in [3, 5, 7, 9])
    assert not any(s["start_ordinal"] == 12 for s in result)
    assert not any(s["start_ordinal"] in [3, 5, 7, 9] for s in build_document_sections(blocks))
