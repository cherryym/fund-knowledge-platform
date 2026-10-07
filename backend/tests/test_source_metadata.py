"""Rule-extracted source metadata candidates: synthetic texts, no model calls."""
from sqlalchemy import select
from test_wiki import env, page  # noqa: F401

from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.source_metadata import PREFIX, STATUS, extract, extract_space, link, load, store


def _entry(title, category, text, vid):
    entry = extract(title, category, [("b1", text)])
    entry.update(title=title, version_id=vid)
    return entry


def test_extracts_issuer_tier_effective_abolition_and_derived_date():
    text = ("中国证券监督管理委员会公告\n〔2017〕13号\n现公布《中国证监会关于证券投资基金估值业务的指导意见》，自公布之日起施行，"
            "原《关于证券投资基金执行〈企业会计准则〉估值业务及份额净值计价有关事项的通知》（证监会计字〔2007〕21号）和"
            "《关于进一步规范证券投资基金估值业务的指导意见》（证监会公告〔2008〕38号）同时废止。\n中国证监会\n2017年9月5日")
    entry = extract("证监会公告〔2017〕13号：施行公告", "估值与核算/法律法规", [("b1", text)])
    assert entry["issuer"]["value"] == "中国证监会"
    assert entry["tier"] == "证监会/人民银行规章及规范性文件"
    assert entry["effective_statements"][0]["mode"] == "on_publication"
    assert entry["effective_statements"][0]["derived_date"] == "2017-09-05"
    abolished = [t["title"] for t in entry["abolition_statements"][0]["targets"]]
    # The published (new) guidance is not an abolished target; titles containing "执行" stay whole.
    assert abolished == ["关于证券投资基金执行〈企业会计准则〉估值业务及份额净值计价有关事项的通知",
                         "关于进一步规范证券投资基金估值业务的指导意见"]
    assert entry["abolition_statements"][0]["targets"][1]["document_number"] == "证监会公告〔2008〕38号"
    assert [p["title"] for p in entry["publishes"]] == ["中国证监会关于证券投资基金估值业务的指导意见"]


def test_dated_effective_clause_and_no_false_abolition():
    entry = extract("企业会计准则第22号——金融工具确认和计量", "估值与核算/会计计量",
                    [("b1", "本准则自 2018 年 1 月 1 日起施行。"), ("b2", "套期会计不再适用；复核期间相关措施不停止执行。")])
    assert entry["tier"] == "会计准则及财政部会计规定"
    assert entry["effective_statements"] == [{"text": "自2018年1月1日起施行", "block_id": "b1",
                                              "date": "2018-01-01", "mode": "date"}]
    assert entry["abolition_statements"] == []


def test_links_abolition_publication_and_comparable_versions_only():
    entries = {
        "ann": _entry("证监会公告〔2017〕13号", "法规", "现公布《中国证监会关于证券投资基金估值业务的指导意见》，自公布之日起施行，"
                      "原《关于进一步规范证券投资基金估值业务的指导意见》（证监会公告〔2008〕38号）同时废止。", "ann"),
        "new": _entry("中国证券监督管理委员会关于证券投资基金估值业务的指导意见", "法规", "正文", "new"),
        "old": _entry("关于进一步规范证券投资基金估值业务的指导意见", "历史参考与征求意见", "正文", "old"),
        "v18": _entry("非上市公司股权估值指引（2018年版）", "估值", "正文", "v18"),
        "v25": _entry("非上市公司股权估值指引（2025年修订版）", "估值", "正文", "v25"),
        "sse26": _entry("上海证券交易所交易规则（2026年修订）", "交易规则",
                        "《上海证券交易所交易规则（2023年修订）》（上证发〔2023〕32号）同时废止。", "sse26"),
    }
    link(entries)
    assert entries["ann"]["links"]["abolishes"] == ["old"]
    assert entries["old"]["links"]["abolished_by"] == ["ann"]
    assert entries["ann"]["links"]["publishes"] == ["new"]
    assert entries["v18"]["links"]["newer_versions"] == ["v25"]
    assert entries["v25"]["links"]["older_versions"] == ["v18"]
    # A reference to an explicitly different version never matches the library's newer version.
    assert entries["sse26"]["links"]["abolishes"] == []
    assert "资料分类：历史参考与征求意见" in entries["old"]["status_hints"]


def test_store_is_idempotent_and_changed_content_hides_candidate(env):  # noqa: F811
    _, vid, _ = page(env, "关于发布某估值指引的通知", "本指引自2024年3月1日起施行。", kind="document",
                     category="估值与核算/估值业务规则")
    with env.db() as db:
        entries = extract_space(db, env.space)
    assert vid in entries and entries[vid]["status"] == STATUS
    with env.db.begin() as db:
        assert store(db, entries, env.owner, trace_id="test") == (1, 0, 0)
    with env.db.begin() as db:
        assert store(db, entries, env.owner, trace_id="test") == (0, 0, 1)
    with env.db() as db:
        loaded = load(db, [vid])
        assert loaded[vid]["effective_statements"][0]["date"] == "2024-03-01"
        audit = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "source_metadata.extracted")).all()
        assert audit[-1].details["legal_status_changed"] is False and audit[-1].details["model_calls"] == 0
        assert db.get(m.ResourceVersion, vid).legal_status == "NOT_APPLICABLE"
    with env.db.begin() as db:
        db.get(m.ResourceVersion, vid).content_sha256 = "0" * 64
    with env.db() as db:
        assert load(db, [vid]) == {}
        assert db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == PREFIX + vid)) is not None
        assert svc.digest({}) is not None


def test_transition_deadline_is_not_expiry_and_title_subjects_are_hints():
    from fund_kb.library_map import _effective
    entry = extract("关于固定收益品种的估值处理标准", "估值", [("b1", "第二十七条 本标准自发布之日起至2023年3月31日实施完毕。")])
    assert entry["effective_statements"] == [{"text": "至2023年3月31日实施完毕", "block_id": "b1", "date": "2023-03-31",
                                              "mode": "transition_deadline"}]
    assert _effective(entry) == "过渡期至2023-03-31实施完毕（不是失效日期）"
    assert extract("证券公司金融工具估值指引", "估值", [("b1", "正文")])["subjects"] == ["证券公司"]
    assert extract("公开募集证券投资基金侧袋机制指引（试行）", "估值", [("b1", "正文")])["subjects"] == ["公募基金", "证券投资基金"]


def test_own_identity_ignores_website_chrome_and_citations():
    # A rule-library page: the metadata table names the number; the footer names only the website owner.
    page_blocks = [("b1", "关于基金投资非公开发行股票等流通受限证券有关问题的通知"), ("b2", "投资者之家"),
                   ("b3", "文件名称\t关于基金投资非公开发行股票等流通受限证券有关问题的通知\t\t\n效力状态\t\t失效日期\t\n"
                          "发文单位\t\t文号\t证监基金字[2006]141号\n发文日期\t2006-07-20\t实施日期\t"),
                   ("b4", "本通知自公布之日起施行。"), ("b5", "© 版权所有：中国证券投资基金业协会"), ("b6", "京ICP备16045718号")]
    entry = extract("关于基金投资非公开发行股票等流通受限证券有关问题的通知", "法规", page_blocks)
    assert entry["own_document_number"] == "证监基金字[2006]141号"
    assert entry["issuer"] == {"value": "中国证监会", "located": "document_number"}
    assert entry["tier"] == "证监会/人民银行规章及规范性文件"
    assert entry["effective_statements"][0]["derived_date"] == "2006-07-20"
    # A parenthetical attached to a cited title is the cited document's record, not this one's.
    cited = extract("关于发布某自律指引的公告", "法规", [("b1", "根据《银行间债券市场债券估值业务管理办法》（中国人民银行公告"
                                                          "〔2023〕第19号发布），本会制定本指引。")])
    assert cited["own_document_number"] is None and cited["issuer"] is None
    # Body sentences naming another body ("报中国证券投资基金业协会备案") never make it the issuer.
    body = extract("资产管理产品相关会计处理规定", "会计", [("b1", "附件："), ("b2", "为进一步明确适用《中国人民银行 中国银行保险"
                                                                     "监督管理委员会 中国证券监督管理委员会 国家外汇管理局关于规范金融机构资产管理业务的指导意见》")])
    assert body["issuer"] is None and body["tier"] == "会计准则及财政部会计规定"


def test_header_record_signature_lines_and_revision_dates():
    record = extract("广州期货交易所交易规则", "交易规则", [("b1", "广州期货交易所交易规则"),
                     ("b2", "（2025 年 5 月 9 日广期所发〔2025〕170 号文件发布，"), ("b3", "自发布之日起实施）"),
                     ("b4", "第九十三条 本规则自发布之日起实施。")])
    assert record["own_document_number"] == "广期所发〔2025〕170号"
    assert {item.get("derived_date") for item in record["effective_statements"]} == {"2025-05-09"}
    approved = extract("某自律指引（试行）", "规则", [("b1", "（2024年12 月20日，经交易商协会第四届理事会第十八次会议审议通过，"
                                                    "交易商协会〔2025〕2号公告发布）"), ("b2", "本指引自发布之日起施行。")])
    assert approved["own_document_number"] == "交易商协会〔2025〕2号"
    assert approved["issuer"]["value"] == "中国银行间市场交易商协会"
    assert "derived_date" not in approved["effective_statements"][0]  # an approval date is not the publication date
    # A revision's "自发布之日起施行" never borrows the original edition's date from the abolition clause.
    revised = extract("上海证券交易所可转换公司债券交易实施细则（2025年3月修订）", "交易规则",
                      [("b1", "第四十六条  本细则自发布之日起施行。本所于2022年7月29日发布的《上海证券交易所可转换公司债券交易"
                              "实施细则》（上证发〔2022〕117号）同时废止。")])
    assert "derived_date" not in revised["effective_statements"][0]
    signed = extract("某联合通知", "法规", [("b1", "本通知自公布之日起施行。"), ("b2", "中国证券业协会\xa0\xa0中国证券投资基金业协会"),
                                       ("b3", "2025年11月3日")])
    assert signed["issuer"] == {"value": "中国证券业协会", "located": "block", "block_id": "b2"}
    assert signed["effective_statements"][0]["derived_date"] == "2025-11-03"


def test_joint_titles_tiers_and_issuer_names():
    joint = extract("财政部 国家税务总局 证监会关于深港股票市场交易互联互通机制试点有关税收政策的通知", "税收",
                    [("b1", "财税〔2016〕127号")])
    assert joint["issuer"]["value"] == "财政部" and joint["tier"] == "财税规范性文件"
    assert joint["own_document_number"] == "财税〔2016〕127号"
    assert extract("中华人民共和国增值税法实施条例", "税收", [("b1", "正文")])["tier"] == "行政法规"
    assert extract("中华人民共和国增值税法", "税收", [("b1", "正文")])["tier"] == "法律"
    vendor = extract("中债资信信用债一般债券估值定价方法体系（2023年10月版）（正文）", "估值", [("b1", "正文")])
    assert vendor["issuer"]["value"] == "中债资信评估" and vendor["tier"] == "估值服务机构方法与数据说明"
    # Fund guidelines without a named issuer: valuation/accounting and operating rules are self-regulatory,
    # other fund guidelines and decisions are CSRC documents.
    assert extract("公开募集证券投资基金侧袋机制指引（试行）", "规则", [("b1", "正文")])["tier"] == "证监会/人民银行规章及规范性文件"
    assert extract("证券投资基金侧袋机制操作细则（试行）", "规则", [("b1", "正文")])["tier"] == "行业自律规则"
    assert extract("证券投资基金参与同业存单会计核算和估值业务指引（试行）", "规则", [("b1", "正文")])["tier"] == "行业自律规则"
    assert extract("关于证券投资基金执行《企业会计准则》估值业务及份额净值计价有关事项的通知", "法规",
                   [("b1", "正文")])["tier"] == "证监会/人民银行规章及规范性文件"
    assert extract("本地估值作业SOP笔记：估值服务商数据接收校验", "内部", [("b1", "正文")])["tier"] == "实务手册与案例"
    assert extract("上交所债券估值与收益率曲线服务说明", "估值", [("b1", "正文")])["tier"] == "估值服务机构方法与数据说明"


def test_partial_container_and_excepted_abolition_and_declared_publication():
    text = ("《财政部 税务总局关于全面推开营业税改征增值税试点的通知》（财税〔2016〕36号）附件3《营业税改征增值税试点过渡政策的规定》"
            "第一条第（二十四）款规定的中小企业信用担保增值税免税政策自2018年1月1日起停止执行。"
            "除本公告和《财政部 税务总局关于个人销售住房增值税政策的公告》外，在2025年12月31日前制发文件规定的国内环节增值税"
            "优惠政策同时停止执行。财政部于2017年修订印发了《企业会计准则第22号——金融工具确认和计量》。")
    entry = extract("财政部 税务总局关于某增值税政策的通知", "税收", [("b1", text)])
    assert [s["targets"] for s in entry["abolition_statements"]] == [
        [{"title": "营业税改征增值税试点过渡政策的规定", "partial": True}]]
    assert entry["publishes"] == []
    assert entry["effective_statements"] == []  # "自2018年1月1日起停止执行" ends a rule, it is no effective date
    entries = {
        "notice": {**entry, "title": "财政部 税务总局关于某增值税政策的通知", "version_id": "notice"},
        "annex3": _entry("财政部 国家税务总局关于全面推开营业税改征增值税试点的通知—附件：营业税改征增值税试点过渡政策的规定",
                         "税收", "正文", "annex3"),
        "annex1": _entry("财政部 国家税务总局关于全面推开营业税改征增值税试点的通知—附件：营业税改征增值税试点实施办法",
                         "税收", "正文", "annex1"),
    }
    link(entries)
    assert entries["notice"]["links"]["partially_abolishes"] == ["annex3"]
    assert entries["annex3"]["links"]["partially_abolished_by"] == ["notice"]
    assert entries["annex1"]["links"]["abolished_by"] == [] and entries["notice"]["links"]["abolishes"] == []


def test_editions_never_publish_each_other_and_unversioned_titles_take_their_year():
    entries = {
        "g13": _entry("公开募集证券投资基金参与国债期货交易指引", "规则",
                      "现公布《公开募集证券投资基金参与国债期货交易指引》，自公布之日起施行。\n中国证监会\n2013年9月3日", "g13"),
        "g22": _entry("公开募集证券投资基金参与国债期货交易指引（2022年修正）", "规则", "正文", "g22"),
        "cas17": _entry("企业会计准则第22号——金融工具确认和计量", "会计", "本准则自2018年1月1日起施行。", "cas17"),
        "cas06": _entry("企业会计准则第22号——金融工具确认和计量（2006年版）", "会计", "正文", "cas06"),
        "annex": _entry("关于修订发布《非上市公司股权估值指引》的通知—附件：附件1：《证券公司金融工具估值指引（2025年修订）》",
                        "估值", "正文", "annex"),
    }
    link(entries)
    assert entries["g13"]["links"]["publishes"] == [] and entries["g13"]["links"]["newer_versions"] == ["g22"]
    assert entries["cas06"]["links"]["newer_versions"] == ["cas17"]
    assert entries["annex"]["publishes"] == [] and entries["annex"]["own_document_number"] is None
