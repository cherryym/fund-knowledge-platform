"""Reasoning core path: library map before planning, planned READ, no question-type rules, GAPs."""
import copy
import re

from sqlalchemy import select
from test_adaptive_query_job import run
from test_wiki import page
from test_wiki_reader_job import base_env, prepare  # noqa: F401
from test_wiki_reader_job import env as env  # noqa: PLC0414

from fund_kb import hybrid_retrieval, providers
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.ingestion import block_text, text_sha256
from fund_kb.library_map import build, build_fitting
from fund_kb.source_metadata import extract_space, store
from fund_kb.source_reading_policy import PREFIX as READING_PREFIX
from fund_kb.wiki_reader import REASONING_PROMPT_VERSION, REASONING_SYSTEM


def _library(env):
    new = page(env, "某品种估值指引（2024年修订版）", "ALPHA_RULE：持有该品种应按第三方估值价格估值。本指引自2024年3月1日起施行。",
               kind="document", category="估值与核算/估值业务规则")
    old = page(env, "某品种估值指引（2019年版）", "BETA_OLD：旧版按成本估值。", kind="document",
               category="估值与核算/历史参考与征求意见")
    wiki = page(env, "某品种估值要点", "WIKI_NOTE：估值时先确认适用版本。", cites=[new])
    with env.db() as db:
        entries = extract_space(db, env.space)
    with env.db.begin() as db:
        store(db, entries, env.owner, trace_id="test")
    return new, old, wiki


def _search_only(monkeypatch, item, queries):
    def search(db, user, space, query, *, pages, **kwargs):
        queries.append(query)
        p = next(p for p in pages.values() if p["version_id"] == item[1])
        block = db.get(m.ContentBlock, (item[1], item[2]))
        unit = {"unit_id": "u" + item[1], "page_id": p["id"], "resource_id": p["resource_id"], "version_id": p["version_id"],
                "kind": p["kind"], "text": block.search_text, "block_ids": [item[2]], "section_path": [], "score": .05,
                "rerank_score": 2.0, "channels": ["vector", "bm25"]}
        hit = {"page_id": p["id"], "resource_id": p["resource_id"], "version_id": p["version_id"], "score": .05,
               "channels": ["vector", "bm25"], "matched_block_ids": [item[2]], "candidate_snippets": [unit]}
        return {"query": query, "mode": "hybrid_unit_rerank", "units": [unit], "hits": [hit], "catalog_pages": len(pages),
                "indexed_catalog_pages": len(pages), "total_candidates": 1, "returned": 1, "warnings": [],
                "timing_ms": 0.1, "candidate_preview_stats": {"verified_snippets": 1, "source_blocks_checked": 1}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)


def _page_id(prompt, title):
    return re.search(r"(W\d+) \| " + re.escape(title), prompt).group(1)


def test_planner_sees_library_map_reads_planned_source_and_records_gaps(env, monkeypatch):
    new, _old, wiki = _library(env)
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "reasoning"})
    queries = []
    _search_only(monkeypatch, wiki, queries)
    def respond(calls, _):
        if len(calls) == 1:
            prompt = calls[0]
            assert "库地图" in prompt and "概念体系" in prompt and "某品种估值指引（2024年修订版）" in prompt
            assert "预抽：有更新版本" in prompt and "施行2024-03-01" in prompt
            assert "ALPHA_RULE" not in prompt and "BETA_OLD" not in prompt and "WIKI_NOTE" not in prompt
            target = _page_id(prompt, "某品种估值指引（2024年修订版）")
            return (f"ISSUE 确认适用版本与估值价格\nREAD {target}\nSEARCH 某品种估值价格\n"
                    "GAP 估值机构曲线编制说明｜核对价格口径\nGAP 产品合同估值条款｜确认合同是否另有约定")
        assert "ALPHA_RULE" in calls[-1] and "WIKI_NOTE" in calls[-1]
        assert "说明所采用依据的层级与效力判断" in calls[-1]
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return ("## 结论\n\n按现行版本采用第三方估值价格。" + "".join(f"[{i}]" for i in ids)
                + "\n\n**GAP** 某品种估值指引2025年修订版｜核对是否调整估值方法\nGAP 托管协议复核约定｜确认托管人复核分工")
    rid, jid, calls = prepare(env, monkeypatch, respond)
    with env.db.begin() as db:
        r = db.get(m.ConsultationRun, rid)
        r.request = {**r.request, "question": "持有某品种应该如何估值？"}
    run(env, jid)
    with env.db() as db:
        result = db.get(m.ConsultationRun, rid)
        snapshot = result.model_snapshot
        assert result.state == "COMPLETED" and len(calls) == 2
        assert snapshot["prompt_version"] == REASONING_PROMPT_VERSION
        assert snapshot["query_path"]["strategy"] == "reasoning_core"
        assert snapshot["query_path"]["reading_plan_source"] == "model_plan_with_library_map"
        assert snapshot["library_map"]["level"] == "sources" and snapshot["library_map"]["sources"] >= 2
        assert snapshot["library_map"]["sources_with_candidates"] >= 2
        assert snapshot["library_map"]["body_blocks_loaded"] == 0
        assert snapshot["query_path"]["planned_reads"]
        assert snapshot["coverage_gaps"] == {"planning": ["估值机构曲线编制说明｜核对价格口径"],
                                             "answer": ["某品种估值指引2025年修订版｜核对是否调整估值方法"],
                                             "case_materials": ["产品合同估值条款｜确认合同是否另有约定",
                                                                "托管协议复核约定｜确认托管人复核分工"],
                                             "source": "model_declared_unverified"}
        markdown = result.response["narrative_markdown"]
        assert "### 还需补充的资料" in markdown and "某品种估值指引2025年修订版" in markdown and "**GAP**" not in markdown
        assert "### 落地前需核对的个案材料" in markdown and "托管协议复核约定" in markdown
        assert result.policy_snapshot["question_analysis"]["source"] == "model_with_library_map_metadata"
        assert result.policy_snapshot["question_analysis"]["local_sources_loaded"] == 0
        assert any(r.version_id == new[1] for r in db.scalars(select(m.RunEvidence).where(m.RunEvidence.run_id == rid)))
        assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.object_id == jid,
                                                   m.AuditEvent.action == "answer.coverage_gaps_recorded")) is not None


def test_reasoning_ignores_question_type_reading_rules(env, monkeypatch):
    new, _, wiki = _library(env)
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=svc.uid(), name=READING_PREFIX + env.space, updated_by=env.owner, config={
            "schema_version": 1, "space_id": env.space, "business_profile": {"label": "专题", "core_categories": [
                "估值与核算/估值业务规则"], "foundation_sources": []},
            "rules": [{"id": "topic-rule", "query_term_groups": [["某品种"]], "resource_ids": [new[0]],
                       "source_topic_terms": ["估值"], "source_context_terms": []}]}))
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "reasoning"})
    _search_only(monkeypatch, wiki, [])
    systems = []
    def respond(calls, _):
        if len(calls) == 1:
            return "ISSUE 确认估值方法"
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return "## 结论\n\n依据已读资料作答。" + "".join(f"[{i}]" for i in ids)
    rid, jid, calls = prepare(env, monkeypatch, respond)
    original = providers.complete
    def capture(connection, messages, **kwargs):
        systems.append(messages[0]["content"])
        return original(connection, messages, **kwargs)
    monkeypatch.setattr(providers, "complete", capture)
    with env.db.begin() as db:
        r = db.get(m.ConsultationRun, rid)
        r.request = {**r.request, "question": "某品种如何估值？"}
    run(env, jid)
    with env.db() as db:
        result = db.get(m.ConsultationRun, rid)
        assert result.state == "COMPLETED"
        assert result.model_snapshot["source_reading_plan"]["required_sources"] == []
        assert "本题主来源核对要求" not in calls[-1]
        assert systems and all(s == REASONING_SYSTEM for s in systems)
        assert "domain_retrieval" not in result.model_snapshot["query_path"]


def test_synthesis_effort_only_where_supported(env, monkeypatch):
    _, _, wiki = _library(env)
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "reasoning"})
    _search_only(monkeypatch, wiki, [])
    def respond(calls, _):
        if len(calls) == 1:
            return "ISSUE 确认估值方法"
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return "## 结论\n\n依据已读资料作答。" + "".join(f"[{i}]" for i in ids)
    rid, jid, _calls = prepare(env, monkeypatch, respond)
    efforts = []
    original = providers.complete
    def capture(connection, messages, **kwargs):
        efforts.append(kwargs.get("reasoning_effort"))
        return original(connection, messages, **kwargs)
    monkeypatch.setattr(providers, "complete", capture)
    resolved = providers.resolve_connection
    monkeypatch.setattr(providers, "resolve_connection", lambda *a, **kw: {
        **copy.deepcopy(resolved(*a, **kw)), "provider_id": "openai", "protocol": "responses"})
    run(env, jid)
    with env.db() as db:
        assert db.get(m.ConsultationRun, rid).state == "COMPLETED"
        started = [e.details for e in db.scalars(select(m.AuditEvent).where(m.AuditEvent.object_id == jid,
            m.AuditEvent.action == "answer.model_invocation_started"))]
    assert efforts == [None, "high"]
    assert [d["reasoning_effort_requested"] for d in started] == [None, "high"]


def test_map_hides_links_outside_catalog_and_degrades_to_fit():
    pages = {"W1": {"id": "W1", "kind": "document", "title": "甲指引（2025年修订版）", "version_id": "v1", "state": "APPROVED",
                    "legal_status": "UNKNOWN", "category": "估值", "source_authority": [
                        {"validation_state": "VALID", "scope": "full", "effective_from": "2025-01-01", "successor_page_id": "W9"}]},
             "W2": {"id": "W2", "kind": "knowledge", "title": "甲要点", "version_id": "v2", "state": "DRAFT",
                    "legal_status": "UNKNOWN", "category": "估值/甲", "knowledge_type": "rule"}}
    candidates = {"v1": {"tier": "行业自律规则", "issuer": {"value": "中国证券投资基金业协会"}, "status_hints": [],
                         "effective_statements": [{"date": "2025-01-01"}],
                         "links": {"abolished_by": ["hidden-version"], "older_versions": ["v2"]}}}
    full = build(pages, candidates, level="full")
    assert "已确认：自2025-01-01起被W9整体替代" in full["text"]
    assert "hidden-version" not in full["text"] and "被" not in full["text"].split("W1 |")[1].split("\n")[0].replace(
        "被W9整体替代", "")
    assert "W2规则[草稿]:甲要点" in full["text"]
    compact = build(pages, candidates, level="compact")
    assert "甲要点" not in compact["text"] and compact["stats"]["level"] == "compact"
    chosen = build_fitting(pages, candidates, lambda text: len(text) <= len(compact["text"]), lambda text: text)
    assert chosen["stats"]["level"] == "compact"
    assert build_fitting(pages, candidates, lambda text: False, lambda text: text) is None


def test_map_lists_topic_pages_by_title_at_every_level():
    pages = {"W1": {"id": "W1", "kind": "knowledge", "title": "同业存单估值与核算专题", "version_id": "v1", "state": "DRAFT",
                    "legal_status": "UNKNOWN", "category": "估值/固收", "compilation_type": "topic"},
             "W2": {"id": "W2", "kind": "knowledge", "title": "存单术语要点", "version_id": "v2", "state": "APPROVED",
                    "legal_status": "UNKNOWN", "category": "估值/固收", "knowledge_type": "term"}}
    for level in ("sources", "compact"):
        text = build(pages, {}, level=level)["text"]
        assert "W1专题[草稿]:同业存单估值与核算专题" in text
        assert "存单术语要点" not in text and "（2页；" in text


def test_reasoning_review_data_rule_limits_items_to_this_answer():
    from fund_kb.wiki_reader import UNIVERSAL_SYSTEM
    assert "逐项处理数据列出的" in UNIVERSAL_SYSTEM and "逐项处理数据列出的" not in REASONING_SYSTEM
    assert "与本题无关的不写" in REASONING_SYSTEM


def test_gap_line_forms_and_inline_mentions():
    from fund_kb.wiki_reader import gap_requests, move_gap_lines
    text = "正文\nGAP｜货币基金特则原文｜支撑细节\n- GAP|合同条款｜确认估值方法\n行内说明（GAP｜不作为命令）\n```\nGAP 示例\n```"
    assert gap_requests(text) == ["货币基金特则原文｜支撑细节", "合同条款｜确认估值方法"]
    moved, gaps, case = move_gap_lines(text)
    assert gaps == ["货币基金特则原文｜支撑细节"] and case == ["合同条款｜确认估值方法"]
    assert "行内说明（GAP｜不作为命令）" in moved and "GAP 示例" in moved and "\nGAP｜" not in moved
    assert moved.count("### 还需补充的资料") == 1 and moved.count("### 落地前需核对的个案材料") == 1


def test_gaps_naming_a_held_document_are_dropped_and_case_materials_set_apart():
    from fund_kb.wiki_reader import move_gap_lines, sort_gaps
    titles = ["企业会计准则第22号——金融工具确认和计量", "证券投资基金会计核算业务指引"]
    gaps = ["《企业会计准则第22号》关于初始确认和终止确认的条款｜确定完整分录",  # held: read it, not missing
            "《证券投资基金会计核算业务指引》2012年修订版｜核对新版科目",  # another version than the one held
            "证监会计字〔2007〕21号正式全文｜确认当时依据",
            "本基金托管协议估值复核条款｜确认托管人复核分工"]
    assert sort_gaps(gaps, titles) == (gaps[1:3], gaps[3:])
    moved, missing, case = move_gap_lines("结论。\n" + "\n".join("GAP " + gap for gap in gaps), titles)
    assert (missing, case) == (gaps[1:3], gaps[3:]) and "企业会计准则第22号" not in moved
    assert "本库暂缺以下资料" in moved and "需结合实际文本核对" in moved
    assert move_gap_lines("GAP " + gaps[0], titles) == ("", [], [])


def test_gaps_describing_a_held_standard_by_topic_are_dropped():
    from fund_kb.wiki_reader import sort_gaps
    titles = ["企业会计准则第39号——公允价值计量", "关于证券投资基金估值业务的指导意见"]
    gaps = ["公允价值计量准则中层次划分及相关披露条款｜核验层次判断",  # the held CAS 39, described by topic
            "公允价值计量及层次披露的相关准则全文（仅涉及模型估值时）｜核实输入值层次",
            "公允价值计量准则2025年修订征求意见稿｜对照拟修订内容",  # another edition than the one held
            "公允价值计量的行业估值指引｜确定行业做法",  # a different document kind than the held standard
            "中国结算可转债转股登记结算规定｜确定日期衔接"]
    assert sort_gaps(gaps, titles) == (gaps[2:], [])


def test_planning_reads_accept_tabulated_read_section_only():
    from fund_kb.wiki_reader import planning_read_requests
    pages = {pid: {} for pid in ("W941", "W991", "W999", "W910", "W5")}
    text = ("## 一、维度\n上位原则：CAS 22（W5）\n## 三、READ 行：直接读取\n| 类型 | 编号 |\n|---|---|\n| 专项 | W991 |\n"
            "| 上位 | W941 |\n- W999 货币基金\n- W404 不存在\n## 四、SEARCH 检索\n- W910 仅是检索示例\n```\nREAD W910\n```\nREAD W999")
    assert planning_read_requests(text, pages) == ["W999", "W991", "W941"]


def test_gap_numbering_and_template_echo_are_not_materials():
    from fund_kb.wiki_reader import gap_requests
    text = "GAP 1｜具体基金合同估值条款｜确认估值方法\nGAP ②：托管协议｜复核分工\nGAP 缺少的资料｜用途\nGAP 2024年版手册｜对照"
    assert gap_requests(text) == ["具体基金合同估值条款｜确认估值方法", "托管协议｜复核分工", "2024年版手册｜对照"]


def test_public_prose_names_pages_instead_of_w_ids():
    from fund_kb.wiki_reader import name_page_ids
    pages = {"W976": {"title": "基金中基金估值业务指引（试行）"}, "W910": {"title": "《估值指导意见》"}}
    text = "依据W976二、（三）1及W910；W404不在目录。\n```\nREAD W976\n贷：证券清算款 [E1、W910规则]\n```\nAW976不替换"
    named, count = name_page_ids(text, pages)
    assert count == 3 and "《基金中基金估值业务指引（试行）》二、（三）1及《估值指导意见》" in named
    assert "W404" in named and "READ W976" in named and "AW976" in named and "[E1、《估值指导意见》规则]" in named


def _big_document(env, title, blocks):
    rid, vid, first = page(env, title, "BIG_0：总则。", kind="document", category="估值与核算/会计计量与净值核算")
    ids = [first]
    with env.db.begin() as db:
        for index in range(1, blocks):
            bid = svc.uid()
            text = f"BIG_{index}：第{index}条 普通条款。" if index != 150 else "BIG_TARGET：持有该品种按估值技术确定公允价值。"
            data = {"text": text, "text_format": "markdown"}
            canonical = block_text({"block_type": "paragraph", "data": data})
            db.add(m.ContentBlock(version_id=vid, block_id=bid, ordinal=index, block_type="paragraph", data=data,
                                  locator={"label": f"第{index}条"}, search_text=canonical,
                                  content_sha256=text_sha256(canonical)))
            ids.append(bid)
        db.flush()
        version = db.get(m.ResourceVersion, vid)
        version.content_sha256 = svc.check_frozen_hash(db, version)
    return rid, vid, ids


def test_explicit_reads_are_located_small_whole_large_by_restricted_search(env, monkeypatch):
    small = page(env, "某品种估值指引（2024年修订版）", "SMALL_WHOLE：本指引适用于公募基金。", kind="document",
                 category="估值与核算/估值业务规则")
    _, big_vid, big_ids = _big_document(env, "某大型会计手册", 200)
    wiki = page(env, "某品种估值要点", "WIKI_NOTE：先确认适用版本。")
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "reasoning"})
    restricted = []
    def search(db, user, space, query, *, pages, **kwargs):
        big = next((p for p in pages.values() if p["version_id"] == big_vid), None)
        target = (big_vid, big_ids[150]) if big is not None and len(pages) == 1 else wiki[1:]
        if big is not None and len(pages) == 1:
            restricted.append(query)
        p = next(p for p in pages.values() if p["version_id"] == target[0])
        text = db.get(m.ContentBlock, target).search_text
        unit = {"unit_id": "u" + target[1], "page_id": p["id"], "resource_id": p["resource_id"], "version_id": target[0],
                "kind": p["kind"], "text": text, "block_ids": [target[1]], "section_path": [], "score": .05,
                "rerank_score": 2.0, "channels": ["vector"]}
        hit = {"page_id": p["id"], "resource_id": p["resource_id"], "version_id": target[0], "score": .05,
               "channels": ["vector"], "matched_block_ids": [target[1]], "candidate_snippets": [unit]}
        return {"query": query, "mode": "hybrid_unit_rerank", "units": [unit], "hits": [hit], "catalog_pages": len(pages),
                "indexed_catalog_pages": len(pages), "total_candidates": 1, "returned": 1, "warnings": [],
                "timing_ms": 0.1, "candidate_preview_stats": {"verified_snippets": 1, "source_blocks_checked": 1}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    def respond(calls, _):
        if len(calls) == 1:
            return (f"ISSUE 估值方法\nREAD {_page_id(calls[0], '某品种估值指引（2024年修订版）')}\n"
                    f"READ {_page_id(calls[0], '某大型会计手册')}")
        # A flat synthetic source is one section; real sources read the clause's enclosing section.
        assert "SMALL_WHOLE" in calls[-1] and "BIG_TARGET" in calls[-1]
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return "## 结论\n\n按估值技术确定公允价值。" + "".join(f"[{i}]" for i in ids)
    rid, jid, _calls = prepare(env, monkeypatch, respond)
    with env.db.begin() as db:
        r = db.get(m.ConsultationRun, rid)
        r.request = {**r.request, "question": "持有某品种如何估值？"}
    run(env, jid)
    with env.db() as db:
        assert db.get(m.ConsultationRun, rid).state == "COMPLETED"
        read = {row.version_id for row in db.scalars(select(m.RunEvidence).where(m.RunEvidence.run_id == rid))}
    assert small[1] in read and big_vid in read and restricted == ["持有某品种如何估值？"]


def _sized_document(env, title, tag, blocks, target=None):
    rid, vid, first = page(env, title, f"{tag}_0：总则。", kind="document", category="估值与核算/估值业务规则")
    ids = [first]
    with env.db.begin() as db:
        for index in range(1, blocks):
            bid = svc.uid()
            text = (f"第{index}条 {tag}_TARGET：持有该品种按估值技术确定公允价值。" if index == target
                    else f"第{index}条 {tag}_{index}：" + "本条规定估值业务的一般处理要求。" * 12)
            data = {"text": text, "text_format": "markdown"}
            canonical = block_text({"block_type": "paragraph", "data": data})
            db.add(m.ContentBlock(version_id=vid, block_id=bid, ordinal=index, block_type="paragraph", data=data,
                                  locator={"label": f"第{index}条"}, search_text=canonical,
                                  content_sha256=text_sha256(canonical)))
            ids.append(bid)
        db.flush()
        version = db.get(m.ResourceVersion, vid)
        version.content_sha256 = svc.check_frozen_hash(db, version)
    return rid, vid, ids


def test_whole_reads_beyond_one_request_budget_are_read_located(env, monkeypatch):
    _, a_vid, _ = _sized_document(env, "甲估值指引", "A", 55)
    _, b_vid, b_ids = _sized_document(env, "乙估值指引", "B", 60, target=40)
    wiki = page(env, "某品种估值要点", "WIKI_NOTE：先确认适用版本。")
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "reasoning"})
    restricted = []
    def search(db, user, space, query, *, pages, **kwargs):
        only_b = len(pages) == 1 and next(iter(pages.values()))["version_id"] == b_vid
        target = (b_vid, b_ids[40]) if only_b else wiki[1:]
        if only_b:
            restricted.append(query)
        p = next(p for p in pages.values() if p["version_id"] == target[0])
        text = db.get(m.ContentBlock, target).search_text
        unit = {"unit_id": "u" + target[1], "page_id": p["id"], "resource_id": p["resource_id"], "version_id": target[0],
                "kind": p["kind"], "text": text, "block_ids": [target[1]], "section_path": [], "score": .05,
                "rerank_score": 2.0, "channels": ["vector"]}
        hit = {"page_id": p["id"], "resource_id": p["resource_id"], "version_id": target[0], "score": .05,
               "channels": ["vector"], "matched_block_ids": [target[1]], "candidate_snippets": [unit]}
        return {"query": query, "mode": "hybrid_unit_rerank", "units": [unit], "hits": [hit], "catalog_pages": len(pages),
                "indexed_catalog_pages": len(pages), "total_candidates": 1, "returned": 1, "warnings": [],
                "timing_ms": 0.1, "candidate_preview_stats": {"verified_snippets": 1, "source_blocks_checked": 1}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    def respond(calls, _):
        if len(calls) == 1:
            return f"ISSUE 估值方法\nREAD {_page_id(calls[0], '甲估值指引')}\nREAD {_page_id(calls[0], '乙估值指引')}"
        assert "A_54" in calls[-1] and "B_TARGET" in calls[-1] and "B_7：" not in calls[-1]
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return "## 结论\n\n按估值技术确定公允价值。" + "".join(f"[{i}]" for i in ids)
    rid, jid, calls = prepare(env, monkeypatch, respond)
    # Each document is ~36-39KB as model text: one fits a 90KB request with the prompts, two do not.
    resolve = providers.resolve_connection
    monkeypatch.setattr(providers, "resolve_connection", lambda *a, **kw: {**resolve(*a, **kw), "max_request_bytes": 90000})
    with env.db.begin() as db:
        r = db.get(m.ConsultationRun, rid)
        r.request = {**r.request, "question": "持有某品种如何估值？"}
    run(env, jid)
    with env.db() as db:
        r = db.get(m.ConsultationRun, rid)
        assert r.state == "COMPLETED"
        assert r.model_snapshot["read_budget"]["located_by_budget"] == 1
        read = {row.version_id for row in db.scalars(select(m.RunEvidence).where(m.RunEvidence.run_id == rid))}
    assert a_vid in read and b_vid in read and restricted == ["持有某品种如何估值？"] and len(calls) == 2


def test_short_prose_read_intent_becomes_one_read_round(env, monkeypatch):
    _, old, wiki = _library(env)
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "reasoning"})
    _search_only(monkeypatch, wiki, [])
    target = {}
    def respond(calls, _):
        if len(calls) == 1:
            target["id"] = _page_id(calls[0], "某品种估值指引（2019年版）")
            return "ISSUE 估值方法"
        if len(calls) == 2:
            assert "BETA_OLD" not in calls[-1]
            return f"我需要先补读{target['id']}（旧版指引）的适用条款，然后再综合作答。"
        assert "BETA_OLD" in calls[-1]
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return "## 结论\n\n按现行指引估值。" + "".join(f"[{i}]" for i in ids)
    rid, jid, calls = prepare(env, monkeypatch, respond)
    run(env, jid)
    with env.db() as db:
        result = db.get(m.ConsultationRun, rid)
        assert result.state == "COMPLETED" and len(calls) == 3
        assert "按现行指引估值" in result.response["narrative_markdown"]
        assert any(r.version_id == old[1] for r in db.scalars(select(m.RunEvidence).where(m.RunEvidence.run_id == rid)))


def test_copied_format_words_are_removed_from_queries():
    from fund_kb.wiki_reader import clean_search_queries
    assert clean_search_queries(["原题", "检索表达", "检索表达 含权债估值", "检索语句：违约债券", "原题"]) == [
        "原题", "含权债估值", "违约债券"]


def test_display_titles_drop_attachment_paths_and_redundant_handles():
    from fund_kb.wiki_reader import display_title, name_page_ids
    long = "关于发布《关于固定收益品种的估值处理标准》的通知—附件：附件：关于固定收益品种的估值处理标准.docx"
    assert display_title(long) == "关于固定收益品种的估值处理标准"
    named, count = name_page_ids("《关于固定收益品种的估值处理标准》（W996）第十二条", {"W996": {"title": long}})
    assert named == "《关于固定收益品种的估值处理标准》第十二条" and count == 1


def test_sources_cited_by_a_read_concept_page_are_located_not_fully_expanded(env, monkeypatch):
    _, big_vid, big_ids = _sized_document(env, "某大型指引", "G", 60, target=30)
    concept = page(env, "某品种估值专题", "CONCEPT_NOTE：本专题汇总该品种的估值口径。")
    with env.db.begin() as db:  # the concept page cites every clause of the big source
        for bid in big_ids:
            db.add(m.EvidenceLink(id=svc.uid(), from_version_id=concept[1], from_block_id=concept[2],
                                  to_version_id=big_vid, to_block_id=bid, purpose="FACT"))
        db.flush()
        version = db.get(m.ResourceVersion, concept[1])
        version.content_sha256 = svc.check_frozen_hash(db, version)
        db.add(m.RuntimePolicy(id=svc.uid(), name=f"wiki-compilation:{concept[1]}", updated_by=env.owner,
                               config={"compilation_type": "topic"}))  # listed by title in the planning map
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "reasoning"})
    restricted = []
    def search(db, user, space, query, *, pages, **kwargs):
        only_big = len(pages) == 1 and next(iter(pages.values()))["version_id"] == big_vid
        target = (big_vid, big_ids[30]) if only_big else concept[1:]
        if only_big:
            restricted.append(query)
        p = next(p for p in pages.values() if p["version_id"] == target[0])
        text = db.get(m.ContentBlock, target).search_text
        unit = {"unit_id": "u" + target[1], "page_id": p["id"], "resource_id": p["resource_id"], "version_id": target[0],
                "kind": p["kind"], "text": text, "block_ids": [target[1]], "section_path": [], "score": .05,
                "rerank_score": 2.0, "channels": ["vector"]}
        hit = {"page_id": p["id"], "resource_id": p["resource_id"], "version_id": target[0], "score": .05,
               "channels": ["vector"], "matched_block_ids": [target[1]], "candidate_snippets": [unit]}
        return {"query": query, "mode": "hybrid_unit_rerank", "units": [unit], "hits": [hit], "catalog_pages": len(pages),
                "indexed_catalog_pages": len(pages), "total_candidates": 1, "returned": 1, "warnings": [],
                "timing_ms": 0.1, "candidate_preview_stats": {"verified_snippets": 1, "source_blocks_checked": 1}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    def respond(calls, _):
        if len(calls) == 1:
            return f"ISSUE 估值方法\nREAD {re.search(r'(W[0-9]+)专题[^:：]*[:：]某品种估值专题', calls[0]).group(1)}"
        assert "CONCEPT_NOTE" in calls[-1] and "G_TARGET" in calls[-1] and "G_7：" not in calls[-1]
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return "## 结论\n\n按估值技术确定公允价值。" + "".join(f"[{i}]" for i in ids)
    rid, jid, calls = prepare(env, monkeypatch, respond)
    with env.db.begin() as db:
        r = db.get(m.ConsultationRun, rid)
        r.request = {**r.request, "question": "持有某品种如何估值？"}
    run(env, jid)
    with env.db() as db:
        r = db.get(m.ConsultationRun, rid)
        assert r.state == "COMPLETED" and r.model_snapshot["read_budget"]["wiki_citations_located"] == 1
    assert restricted == ["持有某品种如何估值？"]


def test_material_beyond_one_request_is_trimmed_by_rank_not_note_taken(env, monkeypatch):
    _, a_vid, _ = _sized_document(env, "甲估值指引", "A", 55)
    c_rid, c_vid, c_first = page(env, "丙参考手册", "C_0 参考说明。", kind="document", category="估值与核算/估值业务规则")
    c_ids = [c_first]
    with env.db.begin() as db:  # one unstructured section larger than the request: a search hit anchors all of it
        for index in range(1, 60):
            bid = svc.uid()
            data = {"text": f"C_{index} " + "参考手册的一般说明。" * 60, "text_format": "markdown"}
            canonical = block_text({"block_type": "paragraph", "data": data})
            db.add(m.ContentBlock(version_id=c_vid, block_id=bid, ordinal=index, block_type="paragraph", data=data,
                                  locator={}, search_text=canonical, content_sha256=text_sha256(canonical)))
            c_ids.append(bid)
        db.flush()
        version = db.get(m.ResourceVersion, c_vid)
        version.content_sha256 = svc.check_frozen_hash(db, version)
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "reasoning"})
    def search(db, user, space, query, *, pages, **kwargs):
        p = next(p for p in pages.values() if p["version_id"] == c_vid) if any(p["version_id"] == c_vid for p in pages.values()) else None
        if p is None:
            return {"query": query, "mode": "hybrid_unit_rerank", "units": [], "hits": [], "catalog_pages": len(pages),
                    "indexed_catalog_pages": len(pages), "total_candidates": 0, "returned": 0, "warnings": [],
                    "timing_ms": 0.1, "candidate_preview_stats": {"verified_snippets": 0, "source_blocks_checked": 0}}
        text = db.get(m.ContentBlock, (c_vid, c_ids[5])).search_text
        unit = {"unit_id": "uc", "page_id": p["id"], "resource_id": p["resource_id"], "version_id": c_vid, "kind": "document",
                "text": text, "block_ids": [c_ids[5]], "section_path": [], "score": .05, "rerank_score": 0.5, "channels": ["bm25"]}
        hit = {"page_id": p["id"], "resource_id": p["resource_id"], "version_id": c_vid, "score": .05, "channels": ["bm25"],
               "matched_block_ids": [c_ids[5]], "candidate_snippets": [unit]}
        return {"query": query, "mode": "hybrid_unit_rerank", "units": [unit], "hits": [hit], "catalog_pages": len(pages),
                "indexed_catalog_pages": len(pages), "total_candidates": 1, "returned": 1, "warnings": [],
                "timing_ms": 0.1, "candidate_preview_stats": {"verified_snippets": 1, "source_blocks_checked": 1}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    def respond(calls, _):
        if len(calls) == 1:
            return f"ISSUE 估值方法\nREAD {_page_id(calls[0], '甲估值指引')}\nSEARCH 参考手册 一般说明"
        assert "A_54" in calls[-1] and "C_30 " not in calls[-1]
        assert "超出本次单次综合的容量" in calls[-1] and "丙参考手册" in calls[-1]
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return "## 结论\n\n按估值技术确定公允价值。" + "".join(f"[{i}]" for i in ids)
    rid, jid, calls = prepare(env, monkeypatch, respond)
    resolve = providers.resolve_connection
    monkeypatch.setattr(providers, "resolve_connection", lambda *a, **kw: {**resolve(*a, **kw), "max_request_bytes": 90000})
    with env.db.begin() as db:
        r = db.get(m.ConsultationRun, rid)
        r.request = {**r.request, "question": "持有某品种如何估值？"}
    run(env, jid)
    with env.db() as db:
        r = db.get(m.ConsultationRun, rid)
        assert r.state == "COMPLETED" and r.model_snapshot["read_budget"]["context_trimmed_pages"] == 1
        reading = r.model_snapshot["wiki_reading"]
        assert reading["trimmed_page_titles"] == ["丙参考手册"] and "丙参考手册" not in reading["page_titles"]
    assert len(calls) == 2  # planning + one synthesis, no note-taking passes
    assert "不要列为GAP或资料缺口" in calls[-1]


def test_page_too_large_for_the_request_keeps_its_most_relevant_sections(env, monkeypatch):
    from types import SimpleNamespace

    from fund_kb.jobs import JobDispatcher
    _sized_document(env, "乙估值指引", "B", 60, target=40)
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "reasoning"})
    def search(db, user, space, query, *, pages, **kwargs):
        return {"query": query, "mode": "hybrid_unit_rerank", "units": [], "hits": [], "catalog_pages": len(pages),
                "indexed_catalog_pages": len(pages), "total_candidates": 0, "returned": 0, "warnings": [],
                "timing_ms": 0.1, "candidate_preview_stats": {"verified_snippets": 0, "source_blocks_checked": 0}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    def respond(calls, _):
        if len(calls) == 1:
            return f"ISSUE 估值方法\nREAD_FULL {_page_id(calls[0], '乙估值指引')}"
        # The whole guideline does not fit: its relevant articles go in whole, unrelated ones stay out.
        assert "B_TARGET" in calls[-1] and "B_1：" in calls[-1] and "B_30：" not in calls[-1]
        assert "只放入了与本题最相关的完整小节" in calls[-1] and "并非整本已读" in calls[-1]
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return "## 结论\n\n按估值技术确定公允价值。" + "".join(f"[{i}]" for i in ids)
    rid, jid, calls = prepare(env, monkeypatch, respond)
    resolve = providers.resolve_connection
    monkeypatch.setattr(providers, "resolve_connection", lambda *a, **kw: {**resolve(*a, **kw), "max_request_bytes": 30000})
    with env.db.begin() as db:
        r = db.get(m.ConsultationRun, rid)
        r.request = {**r.request, "question": "持有某品种如何估值？"}
    scored = []
    def rerank(query, texts):
        scored.extend(texts)
        return [5.0 if "B_TARGET" in text else 2.0 if "B_1：" in text else -3.0 for text in texts]
    worker = JobDispatcher(env.settings, env.db, None)
    worker.vector_index = SimpleNamespace(rerank=rerank, model_runtime=None, embedding=SimpleNamespace(fingerprint="f"),
                                          settings=SimpleNamespace(retrieval_strategy=None, reranker_model="fake"))
    try:
        worker._answer(jid, 1)
    finally:
        worker.close()
    with env.db() as db:
        r = db.get(m.ConsultationRun, rid)
        assert r.state == "COMPLETED"
        reading = r.model_snapshot["wiki_reading"]
        assert reading["partial_page_titles"] == ["乙估值指引"] and reading["trimmed_page_titles"] == []
        assert "乙估值指引" in reading["page_titles"] and r.model_snapshot["read_budget"]["context_partial_pages"] == 1
    assert len(calls) == 2  # planning + one synthesis
    assert sum("B_TARGET" in text for text in scored) == 1  # each article scored once, as its own unit


def test_sources_go_before_knowledge_pages_when_one_request_cannot_hold_both(env, monkeypatch):
    from types import SimpleNamespace

    from fund_kb.jobs import JobDispatcher
    _sized_document(env, "甲估值指引", "A", 20, target=5)
    topic = page(env, "某品种估值专题", "K_TOPIC " + "专题综合说明各类情形。" * 600)
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=svc.uid(), name=f"wiki-compilation:{topic[1]}", updated_by=env.owner,
                               config={"compilation_type": "topic"}))  # listed by title in the planning map
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "reasoning"})
    def search(db, user, space, query, *, pages, **kwargs):
        return {"query": query, "mode": "hybrid_unit_rerank", "units": [], "hits": [], "catalog_pages": len(pages),
                "indexed_catalog_pages": len(pages), "total_candidates": 0, "returned": 0, "warnings": [],
                "timing_ms": 0.1, "candidate_preview_stats": {"verified_snippets": 0, "source_blocks_checked": 0}}
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", search)
    sizes = {}
    def respond(calls, _):
        if len(calls) == 1:
            topic_id = re.search(r'(W[0-9]+)专题[^:：]*[:：]某品种估值专题', calls[0]).group(1)
            return f"ISSUE 估值方法\nREAD_FULL {_page_id(calls[0], '甲估值指引')}\nREAD {topic_id}"
        sizes["prompt"] = len(calls[-1].encode())
        # The guideline goes in whole; the topic page (navigation, scored higher) yields the remaining room.
        assert "A_1：" in calls[-1] and "A_TARGET" in calls[-1] and "K_TOPIC" not in calls[-1]
        ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return "## 结论\n\n按估值技术确定公允价值。" + "".join(f"[{i}]" for i in ids)
    rid, jid, calls = prepare(env, monkeypatch, respond)
    resolve = providers.resolve_connection
    monkeypatch.setattr(providers, "resolve_connection", lambda *a, **kw: {**resolve(*a, **kw), "max_request_bytes": 40000})
    with env.db.begin() as db:
        r = db.get(m.ConsultationRun, rid)
        r.request = {**r.request, "question": "持有某品种如何估值？"}
    worker = JobDispatcher(env.settings, env.db, None)
    worker.vector_index = SimpleNamespace(
        rerank=lambda query, texts: [5.0 if "K_TOPIC" in text else 2.0 for text in texts], model_runtime=None,
        embedding=SimpleNamespace(fingerprint="f"), settings=SimpleNamespace(retrieval_strategy=None, reranker_model="fake"))
    try:
        worker._answer(jid, 1)
    finally:
        worker.close()
    with env.db() as db:
        r = db.get(m.ConsultationRun, rid)
        assert r.state == "COMPLETED", r.error_code
        reading = r.model_snapshot["wiki_reading"]
        assert reading["trimmed_page_titles"] == ["某品种估值专题"] and reading["partial_page_titles"] == []
    assert len(calls) == 2


def _rerank_worker(env, rerank):
    from types import SimpleNamespace

    from fund_kb.jobs import JobDispatcher
    worker = JobDispatcher(env.settings, env.db, None)
    worker.vector_index = SimpleNamespace(rerank=rerank, model_runtime=None, embedding=SimpleNamespace(fingerprint="f"),
                                          settings=SimpleNamespace(retrieval_strategy=None, reranker_model="fake"))
    return worker


def _no_hits(db, user, space, query, *, pages, **kwargs):
    return {"query": query, "mode": "hybrid_unit_rerank", "units": [], "hits": [], "catalog_pages": len(pages),
            "indexed_catalog_pages": len(pages), "total_candidates": 0, "returned": 0, "warnings": [],
            "timing_ms": 0.1, "candidate_preview_stats": {"verified_snippets": 0, "source_blocks_checked": 0}}


def test_a_round_after_a_trimmed_synthesis_is_trimmed_again_from_everything_read(env, monkeypatch):
    _sized_document(env, "甲估值指引", "A", 120, target=50)  # larger than the request on its own
    _sized_document(env, "乙补充规定", "B", 6, target=3)
    env.settings = env.settings.model_copy(update={"wiki_query_strategy": "reasoning"})
    monkeypatch.setattr(hybrid_retrieval, "search_catalog", _no_hits)
    ids = {}
    def respond(calls, _):
        if len(calls) == 1:
            ids["B"] = _page_id(calls[0], "乙补充规定")
            return f"ISSUE 估值方法\nREAD_FULL {_page_id(calls[0], '甲估值指引')}"
        if len(calls) == 2:
            assert "A_TARGET" in calls[-1] and "B_TARGET" not in calls[-1]
            return f"READ_FULL {ids['B']}"  # the model asks for more, as the instruction says: command lines only
        # The second synthesis still carries the first round's material (trimmed again), plus the new page.
        assert "A_TARGET" in calls[-1] and "B_TARGET" in calls[-1] and "此前阅读提要" not in calls[-1]
        cited = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", calls[-1])))
        return "## 结论\n\n按估值技术确定公允价值。" + "".join(f"[{i}]" for i in cited)
    rid, jid, calls = prepare(env, monkeypatch, respond)
    resolve = providers.resolve_connection
    monkeypatch.setattr(providers, "resolve_connection", lambda *a, **kw: {**resolve(*a, **kw), "max_request_bytes": 40000})
    with env.db.begin() as db:
        r = db.get(m.ConsultationRun, rid)
        r.request = {**r.request, "question": "持有某品种如何估值？"}
    worker = _rerank_worker(env, lambda query, texts: [5.0 if "TARGET" in text else 2.0 for text in texts])
    try:
        worker._answer(jid, 1)
    finally:
        worker.close()
    with env.db() as db:
        assert db.get(m.ConsultationRun, rid).state == "COMPLETED"
    assert len(calls) == 3  # planning, a synthesis that asked to read more, the final synthesis
