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
