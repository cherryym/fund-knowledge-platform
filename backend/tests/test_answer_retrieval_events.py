"""Event-focus regression tests: synthetic source text, no model/network/DB writes."""
import copy
from hashlib import sha256
from uuid import uuid4

import pytest

from fund_kb.answer_retrieval import context_manifest, question_plan, retrieve_answer_context, source_structure


def source(title, texts, **metadata):
    rid, vid = str(uuid4()), str(uuid4())
    return [{"resource_id": rid, "version_id": vid, "block_id": str(uuid4()), "ordinal": i,
             "title": title, "text": text, "data": {"text": text}, "block_type": "paragraph",
             "kind": "document", "content_sha256": sha256(text.encode()).hexdigest(),
             "locator": {"source_page": i + 1}, "legal_status": "UNKNOWN", "state": "APPROVED",
             "source_verified": False, **metadata} for i, text in enumerate(texts)]


def principles():
    return source("基金资产估值业务指导意见", [
        "【原件第1页 · 待业务复核】\n\n一、估值业务基本要求",
        "本指导意见适用于公开募集基金对金融资产的估值。",
        "管理人应建立估值委员会，制定估值业务管理制度。",
        "管理人应保持估值技术的一致性，建立定期复核和审阅机制。",
        "管理人与托管人应协商估值技术，托管人审阅估值结果。",
        "变更估值技术应履行披露义务并及时发布临时公告。",
        "估值技术重大变化应咨询会计师事务所的专业意见。",
        "【原件第2页 · 待业务复核】\n\n二、估值原则",
        "估值日无报价且最近交易日之后未发生重大事件的，采用最近交易日的报价；",
        "有证据表明报价不能真实反映公允价值的，应调整报价。",
        "不存在活跃市场的，应采用适当估值技术并优先使用可观察输入值。",
        "重大事件导致潜在估值调整对基金净值的影响达到规定条件时，应调整公允价值。",
        "参考服务机构估值不能免除管理人的相关责任。",
    ])


def excluded_restricted():
    return source("流通受限股票估值指引", [
        "一、适用范围", "（一）本指引所称流通受限股票，是指发行时明确限售期的股票，不包括停",
        "牌、新发行未上市、回购交易中的质押券等流通受限股票。",
        "二、估值技术", "流通受限股票的波动率数据若因停牌无法获取，可参考行业指数。",
        "AAP模型可计算流通受限股票的流动性折扣。",
    ])


def put_method():
    return source("含投资人回售权与调息权债券估值方法", [
        "一、适用范围", "本方法适用于含投资人回售权和发行人票面利率调整权的固定利率债券。",
        "二、行权与报价", "投资者已确认回售与未申报回售时，须区分行权状态及相应现金流。",
        "根据条款计算均衡利率和行权后票面利率，再计算看长估值和看短估值。",
        "若满足条款规定的行权条件，采用看短估值；否则采用看长估值。",
    ])


@pytest.mark.parametrize("question,event,asset", [
    ("股票停牌如何处理", "trading_suspension", "股票"),
    ("债券回售应该如何估值", "bond_put", "债券"),
    ("市值法债券回售", "bond_put", "债券"),
])
def test_three_questions_identify_event_asset_and_measurement(question, event, asset):
    plan = question_plan(question)
    assert plan["intent"] == "specialist_valuation"
    assert event in plan["events"] and asset in plan["assets"]
    assert "subsequent_measurement" in plan["dimensions"]
    assert plan["interpretation"]["task"] == "基金持仓估值"
    assert bool(plan["interpretation"]["assumptions"]) == (event == "trading_suspension")


def test_suspension_uses_pricing_conditions_exclusion_and_governance_not_purchase():
    rules, exclusion = principles(), excluded_restricted()
    purchase = source("证券投资基金会计核算业务指引", ["买入股票时按公允价值进行初始计量。"] * 30)
    old = source("停牌股票估值历史版本（已废止）", ["停牌股票使用估值技术确定公允价值。"] * 40)
    candidates = rules + exclusion + purchase + old
    before = copy.deepcopy(candidates)
    selected, manifest = retrieve_answer_context("股票停牌如何估值，能否套用流通受限股票指引？", candidates)
    assert manifest["plan"]["primary_version_id"] == rules[0]["version_id"]
    assert manifest["plan"]["coverage"]["ready_for_synthesis"]
    assert manifest["plan"]["uncovered_dimensions"] == []
    chosen = {r["block_id"] for r in selected}
    assert {r["block_id"] for r in rules[1:7] + rules[8:]} <= chosen
    assert {r["block_id"] for r in exclusion[1:3]} <= chosen
    assert not chosen.intersection(r["block_id"] for r in exclusion[4:] + purchase + old)
    assert all(s["legal_status"] == "UNKNOWN" for s in manifest["sources"])
    assert manifest["plan"]["authority"]["unverified_version_ids"]
    assert candidates == before


@pytest.mark.parametrize("question", ["债券回售应该如何估值", "市值法债券回售"])
def test_put_focus_beats_sppi_purchase_redemption_and_default(question):
    method = put_method()
    noise = source("上海清算所SPPI现金流量特征测试", ["回售权债券估值公允价值本金利息测试。"] * 70)
    noise += source("中证SPPI产品说明", ["回售债券产品范围。"] * 80)
    noise += source("基金会计核算手册", ["买入FVTPL债券，以公允价值初始计量。", "基金赎回份额的会计核算。"])
    noise += source("债务违约处置规则", ["债券违约应核对债务重组。"])
    selected, manifest = retrieve_answer_context(question, noise + method)
    assert manifest["plan"]["primary_version_id"] == method[0]["version_id"]
    assert all(r["version_id"] == method[0]["version_id"] for r in selected)
    assert {"subsequent_measurement", "exercise_state", "quote_selection"} <= set(manifest["plan"]["covered_dimensions"])
    assert "governance" in manifest["plan"]["uncovered_dimensions"]
    assert manifest["plan"]["coverage"]["ready"]
    assert not manifest["plan"]["coverage"]["complete"]


def test_put_missing_state_or_quote_branch_is_a_real_gap():
    method = put_method()[:5]
    # Remove state rather than synthesize it from a pricing date or document title.
    method[3]["text"] = "计算日至行权日的时间与行权日之后的利息应核对。"
    selected, manifest = retrieve_answer_context("市值法债券回售", method)
    assert selected
    assert {"exercise_state", "quote_selection"} <= set(manifest["plan"]["uncovered_dimensions"])
    assert manifest["plan"]["coverage"]["missing_facets"]["quote_selection"] == ["quote_conditions"]


def test_explicit_trading_question_routes_exchange_rules():
    exchange = source("证券交易所股票交易规则", ["股票停牌期间不接受限价申报，复牌申报按规则执行。"])
    selected, manifest = retrieve_answer_context("股票停牌期间能否委托申报", principles() + exchange)
    assert manifest["plan"]["intent"] == "exchange_rules"
    assert manifest["plan"]["primary_version_id"] == exchange[0]["version_id"]
    assert all(r["version_id"] == exchange[0]["version_id"] for r in selected)
    assert not manifest["plan"]["interpretation"]["assumptions"]


def test_empty_focus_never_falls_back_to_same_asset_or_sppi():
    candidates = source("基金会计核算实务手册", ["买入股票时按公允价值进行初始计量。"])
    candidates += source("SPPI方法论", ["回售权债券现金流量特征测试。"])
    for question in ("股票停牌如何处理", "债券回售应该如何估值", "市值法债券回售"):
        selected, manifest = retrieve_answer_context(question, candidates)
        assert selected == []
        assert manifest["plan"]["coverage"]["status"] == "EMPTY"
        assert not manifest["plan"]["coverage"]["ready_for_synthesis"]
        assert manifest["plan"]["gaps"]


def test_negative_applicability_cannot_become_primary_or_valuation_method():
    selected, manifest = retrieve_answer_context("股票停牌能否套用流通受限股票估值指引", excluded_restricted())
    assert selected and manifest["plan"]["primary_version_id"] is None
    assert manifest["plan"]["covered_dimensions"] == ["inapplicability"]
    assert all(r["answer_dimension"] == "inapplicability" for r in selected)


def test_formula_fragments_cannot_supply_an_isolated_recommendation():
    method = source("含回售权债券估值方法", [
        "一、适用范围", "本方法适用于含回售权债券。", "第 2 步：确定行权后票面利率",
        "（1）若均衡利率低于", "C", "x", "，则推荐看短估值。",
        "（2）若C大于约定利率，则推荐看长估值。",
        "第 3 步：计算估值", "根据行权后票面利率计算债券估值全价。",
    ])
    selected, manifest = retrieve_answer_context("债券回售应该如何估值", method)
    assert not any("推荐" in r["text"] for r in selected)
    assert manifest["plan"]["primary_version_id"] == method[0]["version_id"]
    assert any("公式碎片" in gap for gap in manifest["plan"]["gaps"])
    assert "quote_selection" in manifest["plan"]["uncovered_dimensions"]


def test_budget_is_atomic_and_pruning_retracts_partial_group_coverage():
    method = source("含回售权债券估值方法", [
        "本方法适用于含回售权和调息权的", "固定利率债券。",
        "已确认回售的，应按条款核对行权状态。",
    ])
    selected, manifest = retrieve_answer_context("债券回售应该如何估值", method, limit=1)
    assert len(selected) <= 1
    assert "applicability" in manifest["plan"]["uncovered_dimensions"]
    selected, manifest = retrieve_answer_context("债券回售应该如何估值", method)
    kept = [r for r in selected if r["ordinal"] != 1]
    pruned = context_manifest(kept, manifest["plan"])
    assert "applicability" in pruned["plan"]["uncovered_dimensions"]
    assert pruned["plan"]["coverage"]["incomplete_groups"]
    assert not pruned["plan"]["coverage"]["complete"]


def test_missing_authorized_block_does_not_join_or_claim_continuation():
    method = source("含回售权债券估值方法", [
        "本方法适用于含回售权的", "固定利率", "债券估值。",
    ])
    selected, manifest = retrieve_answer_context("债券回售应该如何估值", [method[0], method[2]])
    assert "applicability" in manifest["plan"]["uncovered_dimensions"]
    assert not any(e["type"] == "CONTINUES" for e in manifest["relations"])
    fake_same_group = [{**r, "context_group": "test"} for r in [method[0], method[2]]]
    assert not any(e["type"] == "CONTINUES" for e in context_manifest(fake_same_group)["relations"])


def test_long_clauses_are_not_headings_and_ocr_heading_has_original_anchor():
    rows = source("基金估值规则", [
        "【原件第1页 · 待业务复核】\n\n一、估值基本原则",
        "（一）不存在活跃市场的投资品种，应采用估值技术确定公允价值。",
        "第 2 步：确定行权后票面利率，判断推荐方向",
    ])
    rows[1]["block_type"] = "heading"  # Even bad ingestion metadata cannot make a sentence a title.
    _, structure = source_structure(rows)
    meta = structure[(rows[1]["version_id"], rows[1]["block_id"])]
    assert not meta["heading"]
    assert meta["section_path"] == ["一、估值基本原则"]
    assert meta["section_anchor_ids"] == [rows[0]["block_id"]]
    assert structure[(rows[2]["version_id"], rows[2]["block_id"])]["heading"]


def test_event_citations_are_exact_and_do_not_supply_missing_dimensions():
    rules = principles()
    wiki = source("相关知识", ["基金估值说明。"], kind="knowledge")
    wiki[0]["source_citations"] = [{"version_id": rules[9]["version_id"], "block_id": rules[9]["block_id"]}]
    unrelated = source("停牌估值百科", ["股票停牌估值。"], kind="knowledge")
    selected, manifest = retrieve_answer_context("股票停牌如何处理", rules + wiki + unrelated)
    assert wiki[0]["block_id"] in {r["block_id"] for r in selected}
    assert unrelated[0]["block_id"] not in {r["block_id"] for r in selected}
    cites = [r for r in manifest["relations"] if r["type"] == "CITES"]
    assert len(cites) == 1 and cites[0]["to"] == rules[9]["block_id"]
    assert "inapplicability" not in manifest["plan"]["dimensions"]


def test_general_suspension_question_does_not_turn_excluded_lockup_rules_into_answer_theme():
    rules, exclusion = principles(), excluded_restricted()
    selected, manifest = retrieve_answer_context('股票停牌应该如何处理', rules + exclusion)
    assert selected and manifest['plan']['primary_version_id'] == rules[0]['version_id']
    assert not {r['block_id'] for r in selected}.intersection(r['block_id'] for r in exclusion)
    assert 'inapplicability' not in manifest['plan']['dimensions']
    assert manifest['plan']['coverage']['ready_for_synthesis'] is True


def test_title_and_uuid_do_not_designate_a_unique_method():
    method = put_method()
    for row in method:
        row["title"] = "某估值机构含回售权债券专业资料"
    selected, manifest = retrieve_answer_context("市值法债券回售", method)
    assert selected and manifest["plan"]["primary_version_id"] == method[0]["version_id"]
    assert "subsequent_measurement" in manifest["plan"]["covered_dimensions"]


def test_partial_explanation_readiness_does_not_claim_complete_or_confirm_facts():
    method = put_method()[:5]
    _, manifest = retrieve_answer_context("债券回售应该如何估值", method)
    coverage = manifest["plan"]["coverage"]
    assert coverage["ready"] == coverage["ready_for_synthesis"] is True
    assert coverage["status"] == "PARTIAL" and coverage["complete"] is False
    assert coverage["missing_critical_dimensions"] == ["quote_selection"]
    assert coverage["requires_qualification"]
    assert manifest["plan"]["scenario"]["facts_to_confirm"]


def test_article_parent_and_put_branch_remain_one_group_without_false_exclusion():
    rules = source("基金固定收益估值处理标准", [
        "第十二条 已上市的含权固定收益品种（另有规定的除外）选取相应估值全价。",
        "对于含投资者回售权的品种，行使回售权的，在回售登记日至实际收款期间采用推荐估值全价。"
        "未行使回售权的建议按照长待偿期所对应的价格估值。",
        "第十三条 可转债根据交易状态选取收盘价。",
    ])
    selected, manifest = retrieve_answer_context("市值法债券回售", rules)
    assert {r["ordinal"] for r in selected} == {0, 1}
    assert len({r["context_group"] for r in selected}) == 1
    assert not any(r["answer_dimension"] == "inapplicability" for r in selected)
    assert {"exercise_state", "quote_selection"} <= set(manifest["plan"]["covered_dimensions"])


def test_explicit_asset_subtype_is_allowed_but_not_assumed_for_generic_put():
    convertible = source("可转债含回售权估值方法", ["可转债含投资者回售权的，采用估值方法确定公允价值。"])
    selected, _ = retrieve_answer_context("可转债回售如何估值", convertible)
    assert selected
    selected, _ = retrieve_answer_context("债券回售如何估值", convertible)
    assert not selected


def test_duration_and_sppi_are_not_put_quote_rules():
    duration = source("含回售权债券估值方法", [
        "本方法适用于含投资人回售权债券。",
        "当日收益率变化时，到期估值对应的修正久期与行权估值对应结果一致。",
    ])
    _, manifest = retrieve_answer_context("市值法债券回售", duration)
    assert not manifest["plan"]["coverage"]["ready"]
    assert "quote_selection" in manifest["plan"]["uncovered_dimensions"]


def test_non_heading_unpunctuated_obligation_and_proven_source_gaps():
    rows = source("估值资料", ["一、股票投资", "（一）基金管理人应制定估值管理制度", "债券业务"])
    _, structure = source_structure(rows)
    assert not structure[(rows[1]["version_id"], rows[1]["block_id"])]["heading"]
    _, structure = source_structure([rows[0], rows[2]])
    assert structure[(rows[2]["version_id"], rows[2]["block_id"])]["section_anchor_ids"] == []
