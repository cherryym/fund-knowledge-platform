"""Typed Wiki compilation in a temporary DB, synthetic completion, zero network."""
from __future__ import annotations

import copy
import json
import re

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from fund_kb import api, api_wiki, models as m, services as svc, wiki
from fund_kb import wiki_compilation as compilation
from fund_kb.ingestion import text_sha256
from test_wiki import env as env, FakeProvider, execute, page  # noqa: PLC0414
from test_wiki_semantics import (append_block, build_input, change_record, content_snapshot, finish,
    isolated_configuration_and_network as isolated_configuration_and_network, queue)  # noqa: PLC0414
from test_wiki_unverified import draft_source


def typed_output(data, call=1):
    ids = [item["id"] for item in data["sources"]]
    schema = data["schema"]
    page_schema = schema["properties"]["pages"]["items"]
    block_schema = page_schema["properties"]["blocks"]["items"]
    kind = data.get("compilation_type", "topic")
    semantic = "relations" in schema["properties"]
    only_relations = schema["properties"]["pages"].get("maxItems") == 0
    sections = block_schema["properties"].get("section", {}).get("enum", [None])
    blocks = []
    for section in sections:
        block = {"markdown": f"## {section or '正文'}\n\n合成来源要求核对计量日期、输入和适用条件；本文仍待复核。",
                 "evidence_ids": ids}
        if section:
            block.update(section=section, support_status="SUPPORTED")
        if kind == "sop" and section == "steps":
            block["step"] = {"step_id": "step-1", "owner_role": "复核角色待确认", "inputs": ["日期与输入参数"],
                "action": "核对适用条件", "outputs": ["核对记录"], "checks": ["输入口径一致"],
                "exceptions": ["异常待专家核验"], "depends_on": []}
        blocks.append(block)
    knowledge_type = page_schema["properties"]["knowledge_type"].get("const", "faq")
    item = {"title": f"合成{kind}知识{call}", "category": "估值与核算/测试", "knowledge_type": knowledge_type,
            "aliases": [], "links": [], "blocks": blocks}
    if semantic:
        item["node_role"] = "procedure" if kind == "sop" else "rule"
    result = {"pages": [] if only_relations else [item], "gaps": []}
    if semantic:
        result["relations"] = [] if not only_relations else [{
            "source_title": data["reference_nodes"][0]["title"], "target_title": data["reference_nodes"][1]["title"],
            "relation_type": "APPLIES_TO", "evidence_ids": ids,
            "explanation": "合成来源明确提供条件依赖，关系仅为待核验提案。"}]
    if "source_dispositions" in schema["properties"]:
        result["source_dispositions"] = [{"evidence_id": eid, "disposition": "EXTRACTED", "reason": "合成内容已关联原始输入。"}
                                         for eid in ids]
    return result


class CompilationProvider(FakeProvider):
    def __init__(self, env):
        super().__init__(env)
        self.options, self.instructions = [], []
        self.finish_reason = "stop"
        self.reviews, self.review_output = [], lambda data: {"remove": []}

    def complete(self, snapshot, messages, max_tokens=4096, json_mode=True, timeout=60):
        self.calls += 1
        data = json.loads(messages[1]["content"])
        if "paragraphs" in data:  # page-level review after a multi-batch merge
            self.reviews.append(data)
            output = self.review_output(data)
            return {"choices": [{"finish_reason": "stop", "message": {"content": output if isinstance(output, str)
                                 else json.dumps(output, ensure_ascii=False)}}], "usage": {"prompt_tokens": 50}}
        self.requests.append(data)
        self.options.append({"max_tokens": max_tokens, "json_mode": json_mode, "timeout": timeout})
        self.instructions.append(messages[0]["content"])
        assert "最多160字" not in messages[0]["content"] and "1至2段" not in messages[0]["content"]
        output = typed_output(data, self.calls)
        if self.output_transform:
            output = self.output_transform(output, data)
        if self.after_call:
            self.after_call()
        return {"choices": [{"finish_reason": self.finish_reason, "message": {"content": json.dumps(output, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100}}


@pytest.fixture
def provider(env, monkeypatch):
    fake = CompilationProvider(env)
    monkeypatch.setattr(wiki, "_provider_module", lambda: fake)
    return fake


def request(env, sources, kind="topic", mode="topic", **extra):
    return build_input(env, sources, mode=mode, compilation_type=kind, **extra)


def test_specs_authenticated_read_only_shape_and_no_generation(env, provider):
    assert "getWikiCompilationSpecs" in api.READ_ONLY_OPERATIONS
    response = env.call("GET", "/wiki/compilation-specs")
    assert response.status_code == 200, response.text
    data = response.json()
    assert not list(Draft202012Validator(api_wiki.SCHEMAS["WikiCompilationSpecs"]).iter_errors(data))
    assert data["spec_version"] == compilation.SPEC_VERSION
    assert [item["compilation_type"] for item in data["types"]] == list(compilation.TYPES)
    assert data["automatic_generation"] is False
    assert all(len(item["sections"]) >= 5 and item["no_body_length_cap"] for item in data["types"])
    assert api_wiki.PATHS["/wiki/compilation-specs"]["get"]["operationId"] == "getWikiCompilationSpecs"
    with env.db() as db:
        assert list(db.scalars(select(m.Job))) == []
    assert provider.calls == 0
    env.client.cookies.clear()
    assert env.call("GET", "/wiki/compilation-specs").status_code == 401


@pytest.mark.parametrize("kind", compilation.TYPES)
@pytest.mark.parametrize("mode", ["topic", "knowledge_points", "relations"])
def test_explicit_types_work_with_page_and_semantic_modes(env, provider, kind, mode):
    source = draft_source(env)
    refs = [page(env, "合成参考甲"), page(env, "合成参考乙")] if mode == "relations" else []
    old_content = content_snapshot(env)
    jid = queue(env, request(env, [source], kind, mode, references=refs))
    assert provider.calls == 0
    result = finish(env, jid)
    assert result["compilation_type"] == kind and result["compilation_contract"] == "typed"
    assert result["compilation_spec_version"] == compilation.SPEC_VERSION
    assert result["batch"]["scope_status"] == "COMPLETE"
    assert result["batch"]["input_status"] == "COMPLETE"
    assert result["batch"]["knowledge_completeness"] == "NOT_EVALUATED"
    assert result["formal_evidence_allowed"] is False
    assert provider.options[0]["max_tokens"] == compilation.budget(compilation.resolve(kind, mode), 3)["max_output_tokens"]
    assert len(result["created_version_ids"]) == (0 if mode == "relations" else 1)
    assert result["semantic_relations_created"] == (1 if mode == "relations" else 0)
    now = content_snapshot(env)
    for table, rows in old_content.items():
        assert all(row in now[table] for row in rows), "existing source/reference rows must not change"
    for vid in result["created_version_ids"]:
        with env.db() as db:
            version = db.get(m.ResourceVersion, vid)
            assert (version.state, version.source_verified, version.legal_status) == ("DRAFT", False, "UNKNOWN")
            meta = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == f"wiki-compilation:{vid}")).config
            assert meta["structure_status"] == "VALIDATED" and meta["compilation_type"] == kind
            assert meta["batch_scope_status"] == "COMPLETE"
            assert meta["compiled_content_sha256"] == version.content_sha256
            if kind == "sop":
                assert version.knowledge_type == "sop"
                text = "\n".join(block.search_text for block in db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == vid)))
                assert "复核角色待确认" in text and "输入口径一致" in text and "异常待专家核验" in text
            provenance = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == f"wiki-provenance:{version.resource_id}")).config
            assert provenance["compilation_spec_version"] == compilation.SPEC_VERSION
            assert provenance["source_snapshot"][0]["resource_id"] == source[0]


@pytest.mark.parametrize("mode,expected", [("topic", "topic"), ("knowledge_points", "atomic_rule"), ("relations", "topic")])
def test_missing_type_keeps_legacy_envelope_and_inferred_metadata(env, provider, mode, expected):
    refs = [page(env, "旧关系甲"), page(env, "旧关系乙")] if mode == "relations" else []
    result = finish(env, queue(env, build_input(env, [draft_source(env)], mode=mode, references=refs)))
    assert result["compilation_type"] == expected
    assert result["compilation_contract"] == "legacy"
    assert "section" not in provider.requests[0]["schema"]["properties"]["pages"]["items"]["properties"]["blocks"]["items"]["properties"]
    assert provider.options[0]["max_tokens"] <= 4096
    if result["created_version_ids"]:
        with env.db() as db:
            meta = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == f"wiki-compilation:{result['created_version_ids'][0]}")).config
            assert meta["structure_status"] == "LEGACY_NOT_VALIDATED"


@pytest.mark.parametrize("kind,expected", [("topic", 32768), ("sop", 65536)])
def test_six_pages_have_type_budget_without_short_body_caps(kind, expected):
    config = compilation.resolve(kind)
    assert compilation.budget(config, 6)["max_output_tokens"] == expected
    schema = compilation.output_schema(wiki.BUILD_SCHEMA, config)
    blocks = schema["properties"]["pages"]["items"]["properties"]["blocks"]
    assert "maxItems" not in blocks
    assert "maxLength" not in blocks["items"]["properties"]["markdown"]
    assert "maxLength" not in schema["properties"]["gaps"]["items"]


def test_full_long_source_paragraph_and_long_output_persist_without_slicing(env, provider):
    text = "这一完整合成段落明确价格、日期、参数及例外条件。" * 220
    source = page(env, "合成长段落", kind="document", state="DRAFT", text=text)
    long_body = "完整的合成知识正文，应保留全部条件和参数。" * 280

    def transform(output, data):
        output["pages"][0]["blocks"][0]["markdown"] = long_body
        output["pages"][0]["blocks"].extend(copy.deepcopy(output["pages"][0]["blocks"][0]) for _ in range(9))
        return output
    provider.output_transform = transform
    result = finish(env, queue(env, request(env, [source])))
    sent = provider.requests[0]["sources"]
    assert len(sent) == 1 and sent[0]["excerpt"] == text
    assert sent[0]["char_start"] == 0 and sent[0]["char_end"] == len(text)
    assert result["batch"]["content_truncated"] is False
    with env.db() as db:
        blocks = list(db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == result["created_version_ids"][0])
                                .order_by(m.ContentBlock.ordinal)))
        assert len(blocks) > 12 and blocks[1].data["text"] == long_body
        span = blocks[1].locator["source_spans"][0]
        assert span["char_end"] == len(text) and span["excerpt_sha256"] == text_sha256(text)
    read = env.call("GET", f"/versions/{result['created_version_ids'][0]}")
    assert read.status_code == 200 and read.json()["blocks"][1]["data"]["text"] == long_body


@pytest.mark.parametrize("fault,code", [
    ("missing_section", "WIKI_COMPILATION_STRUCTURE_INCOMPLETE"),
    ("gap_omitted", "WIKI_COMPILATION_GAP_UNDECLARED"),
    ("unknown_citation", "WIKI_CITATION_INVALID"),
    ("disposition_omitted", "WIKI_SOURCE_DISPOSITION_INCOMPLETE"),
    ("unknown_section", "WIKI_OUTPUT_SCHEMA_INVALID"),
])
def test_incomplete_structure_or_invalid_source_rolls_back_without_partial_pages(env, provider, fault, code):
    source = draft_source(env)
    before = content_snapshot(env)
    def transform(output, data):
        blocks = output["pages"][0]["blocks"]
        if fault == "missing_section":
            blocks.pop()
        elif fault == "gap_omitted":
            blocks[0]["support_status"] = "GAP"
        elif fault == "unknown_citation":
            blocks[0]["evidence_ids"] = ["S999999"]
        elif fault == "disposition_omitted":
            output["source_dispositions"] = []
        elif fault == "unknown_section":
            blocks[0]["section"] = "invented-section"
        return output
    provider.output_transform = transform
    with pytest.raises(wiki.WikiBuildError) as caught:
        execute(env, queue(env, request(env, [source], "atomic_rule")))
    assert caught.value.code == code
    assert content_snapshot(env) == before
    with env.db() as db:
        assert list(db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like("wiki-compilation:%")))) == []


@pytest.mark.parametrize("fault,code", [("missing_role", "WIKI_OUTPUT_SCHEMA_INVALID"),
    ("self_dependency", "WIKI_SOP_DEPENDENCY_INVALID"), ("future_dependency", "WIKI_SOP_DEPENDENCY_INVALID"),
    ("blank_role", "WIKI_SOP_STEP_INCOMPLETE")])
def test_sop_steps_have_responsibility_and_acyclic_ordered_dependencies(env, provider, fault, code):
    def transform(output, data):
        step = next(b for b in output["pages"][0]["blocks"] if b["section"] == "steps")["step"]
        if fault == "missing_role":
            del step["owner_role"]
        elif fault == "blank_role":
            step["owner_role"] = "   "
        else:
            step["depends_on"] = ["step-1" if fault == "self_dependency" else "step-2"]
        return output
    provider.output_transform = transform
    with pytest.raises(wiki.WikiBuildError) as caught:
        execute(env, queue(env, request(env, [draft_source(env)], "sop")))
    assert caught.value.code == code


def test_explicit_gap_retains_draft_and_reports_partial_not_professional_complete(env, provider):
    def transform(output, data):
        output["pages"][0]["blocks"][0].update(markdown="来源未提供完整适用日期，待核验。", support_status="GAP")
        output["gaps"] = ["完整适用日期未提供。"]
        return output
    provider.output_transform = transform
    result = finish(env, queue(env, request(env, [draft_source(env)], "scenario")))
    assert result["batch"]["input_status"] == "COMPLETE"
    assert result["batch"]["scope_status"] == "PARTIAL" and result["coverage"]["status"] == "PARTIAL"
    assert result["batch"]["professional_accuracy"] == "NOT_EVALUATED"
    assert result["batch"]["counts"]["REVIEW_REQUIRED"] == 1


def test_repeated_no_new_content_does_not_erase_previous_review_gap(env, provider):
    source = draft_source(env)
    provider.output_transform = lambda output, data: {**output, "gaps": ["主体和日期仍待核验。"]}
    first = finish(env, queue(env, request(env, [source], "topic")))
    second = finish(env, queue(env, request(env, [source], "topic")))
    assert first["batch"]["scope_status"] == second["batch"]["scope_status"] == "PARTIAL"
    assert second["batch"]["input_status"] == "NO_NEW_CONTENT"
    assert second["batch"]["counts"]["REVIEW_REQUIRED"] == 1 and provider.calls == 1


def test_citation_does_not_turn_needs_review_disposition_into_complete(env, provider):
    def transform(output, data):
        output["source_dispositions"][0]["disposition"] = "NEEDS_REVIEW"
        return output
    provider.output_transform = transform
    result = finish(env, queue(env, request(env, [draft_source(env)], "topic")))
    assert result["coverage"]["status"] == result["batch"]["scope_status"] == "PARTIAL"
    assert result["batch"]["counts"]["REVIEW_REQUIRED"] == 1


def test_typed_published_source_still_produces_only_unverified_new_draft(env, provider):
    source = page(env, "已核验合成原件", kind="document")
    result = finish(env, queue(env, request(env, [source], "sop", source_mode="published")))
    assert result["source_mode"] == "published" and not result["formal_evidence_allowed"]
    with env.db() as db:
        version = db.get(m.ResourceVersion, result["created_version_ids"][0])
        assert version.state == "DRAFT" and not version.source_verified
        assert db.get(m.ResourceVersion, source[1]).state == "APPROVED"


@pytest.mark.parametrize("mode", ["topic", "knowledge_points"])
def test_whole_batches_preserve_order_and_enumerate_all_remaining_units(env, provider, mode):
    source = draft_source(env)
    ids = [source[2]] + [append_block(env, source, f"第{i}个独立合成段落，必须保留上下文和适用条件。", ordinal=i) for i in range(1, 35)]
    first = finish(env, queue(env, request(env, [source], mode=mode)))
    assert first["batch"]["scope_status"] == first["batch"]["input_status"] == "PARTIAL"
    assert len(first["batch"]["units"]) == 35 and first["batch"]["counts"]["NOT_SENT"] == 3
    assert [s["block_id"] for s in provider.requests[0]["sources"]] == ids[:32]
    remaining = [unit["source_blocks"][0]["block_id"] for unit in first["batch"]["units"] if unit["status"] == "NOT_SENT"]
    assert remaining == ids[32:]
    assert first["batch"]["automatic_next_batch"] is False
    assert provider.calls == 1
    second = finish(env, queue(env, request(env, [source], mode=mode)))
    assert [s["block_id"] for s in provider.requests[1]["sources"]] == ids[32:]
    assert second["batch"]["scope_status"] == "COMPLETE"
    assert second["batch"]["input_status"] == "PARTIAL"
    assert second["batch"]["knowledge_completeness"] == "NOT_EVALUATED"


@pytest.mark.parametrize("mode", ["topic", "knowledge_points"])
def test_explicit_whole_scope_over_unit_limit_still_rejects_without_model(env, provider, mode):
    source = draft_source(env)
    ids = [source[2]] + [append_block(env, source, f"第{i}个完整独立原段，需保留。", ordinal=i) for i in range(1, 34)]
    jid = queue(env, request(env, [source], mode=mode, source_block_ids=ids))
    with pytest.raises(wiki.WikiBuildError) as caught:
        execute(env, jid)
    assert caught.value.code == "WIKI_SCOPE_REQUIRES_SPLIT" and provider.calls == 0


@pytest.mark.parametrize("strict,expected", [(True, "WIKI_SCOPE_REQUIRES_SPLIT"), (False, "WIKI_PASSAGE_EXCEEDS_BATCH_BUDGET")])
def test_oversize_whole_paragraph_is_rejected_before_model_never_clipped(env, provider, monkeypatch, strict, expected):
    monkeypatch.setitem(compilation._SPECS["topic"], "max_source_utf8_bytes", 500)
    source = page(env, "合成不可拆段落", kind="document", state="DRAFT", text="完整规则和完整适用条件。" * 100)
    data = request(env, [source], **({"source_block_ids": [source[2]]} if strict else {}))
    with pytest.raises(wiki.WikiBuildError) as caught:
        execute(env, queue(env, data))
    assert caught.value.code == expected
    assert provider.calls == 0


def test_selected_contiguous_window_includes_processed_middle_as_context(env, provider):
    source = draft_source(env)
    middle = append_block(env, source, "先前已编译的中间条件。", ordinal=1)
    tail = append_block(env, source, "仍需新编译的尾部例外。", ordinal=2)
    first = finish(env, queue(env, request(env, [source], source_block_ids=[middle])))
    assert first["batch"]["outside_scope_blocks"] == 2
    second = finish(env, queue(env, request(env, [source], source_block_ids=[source[2], middle, tail])))
    assert [s["block_id"] for s in provider.requests[1]["sources"]] == [source[2], middle, tail]
    assert second["batch"]["input_status"] == "COMPLETE"


def test_pdf_line_wraps_and_heading_preserve_every_original_anchor():
    def block(i, text, kind="paragraph"):
        return {"resource_id": "r", "version_id": "v", "block_id": f"b{i}", "ordinal": i, "text": text,
                "block_type": kind, "locator": {"kind": "pdf"}, "content_sha256": text_sha256(text), "title": "合成源"}
    records = [block(0, "适用条件", "heading"), block(1, "仅适用于估值日期与"), block(2, "来源口径一致的情形。"), block(4, "另一个不连续段落。")]
    units = compilation.semantic_passages(records)
    assert len(units) == 2 and units[0]["text"] == "\n".join(r["text"] for r in records[:3])
    chosen, _ = compilation.whole_batch(units, set(), wiki._source_key, max_bytes=64000, strict_scope=True)
    first = chosen[0][0]
    assert first["locator_kind"] == "contiguous_source_blocks"
    assert [r["block_id"] for r in first["source_blocks"]] == ["b0", "b1", "b2"]
    assert [r["char_end"] for r in first["source_blocks"]] == [len(r["text"]) for r in records[:3]]


def test_incomplete_provider_output_is_not_a_partial_saved_wiki(env, provider):
    source = draft_source(env)
    before = content_snapshot(env)
    provider.finish_reason = "length"
    with pytest.raises(wiki.WikiBuildError) as caught:
        execute(env, queue(env, request(env, [source], "sop")))
    assert caught.value.code == "WIKI_MODEL_OUTPUT_INCOMPLETE"
    assert content_snapshot(env) == before


@pytest.mark.parametrize("old_state", ["DRAFT", "APPROVED"])
def test_compile_types_have_independent_ledgers_and_old_page_preservation(env, provider, old_state):
    source = draft_source(env)
    old = page(env, "既有人工页面", state=old_state, text="不得覆盖的人工内容。")
    topic = finish(env, queue(env, request(env, [source], "topic")))
    rule = finish(env, queue(env, request(env, [source], "atomic_rule")))
    assert provider.calls == 2 and topic["created_version_ids"] and rule["created_version_ids"]
    expected = {}
    def transform(output, data):
        output["pages"][0]["title"] = "既有人工页面"
        output["pages"][0]["blocks"][0]["markdown"] += "\n\n" + "完整候选中的条件与例外不得丢弃。" * 350
        expected["blocks"] = copy.deepcopy(output["pages"][0]["blocks"])
        return output
    provider.output_transform = transform
    before = content_snapshot(env)
    proposed = finish(env, queue(env, request(env, [source], "scenario")))
    assert proposed["created_resource_ids"] == proposed["created_version_ids"] == []
    proposal_id, = proposed["revision_proposal_ids"]
    assert proposed["skipped"][0]["reason"] == "REVISION_PROPOSED_ORIGINAL_PRESERVED"
    assert proposed["skipped"][0]["proposal_id"] == proposal_id
    assert proposed["batch"]["scope_status"] == "PARTIAL"
    assert proposed["batch"]["counts"]["REVIEW_REQUIRED"] == 1
    assert content_snapshot(env) == before
    with env.db() as db:
        assert db.get(m.ContentBlock, (old[1], old[2])).data["text"] == "不得覆盖的人工内容。"
        assert db.get(m.ResourceVersion, old[1]).state == old_state
        policy = db.get(m.RuntimePolicy, proposal_id)
        assert policy.config["resource_ids"] == [old[0]]
        assert policy.config["kind"] == "REVISION" and policy.config["status"] == "PROPOSED"
        assert policy.config["origin"] == "COMPILER"
        candidate = policy.config["compiled_revision"]["candidate"]
        assert [block["data"]["text"] for block in candidate["blocks"][1:]] == [block["markdown"] for block in expected["blocks"]]
        assert all(block["citations"] == [{"version_id": source[1], "block_id": source[2], "purpose": "FACT"}]
                   for block in candidate["blocks"][1:])
        assert policy.config["compiled_revision"]["source_snapshots"][0]["resource_id"] == source[0]
        assert policy.config["compilation_metadata"]["compilation_type"] == "scenario"
        assert policy.config["compilation_metadata"]["compilation_spec_version"] == compilation.SPEC_VERSION


def test_legacy_same_title_still_preserves_without_implicit_revision(env, provider):
    source = draft_source(env)
    page(env, "旧合同同名页", state="DRAFT", text="原版仍保留。")
    provider.output_transform = lambda output, data: {**output, "pages": [{**output["pages"][0], "title": "旧合同同名页"}]}
    before = content_snapshot(env)
    result = finish(env, queue(env, build_input(env, [source], mode="topic")))
    assert result["created_resource_ids"] == result["revision_proposal_ids"] == []
    assert result["skipped"][0]["reason"] == "EXISTING_VISIBLE_PAGE_PRESERVED"
    assert content_snapshot(env) == before


def test_source_document_same_title_never_creates_knowledge_revision(env, provider):
    source = page(env, "来源文档不能被知识候选修订", kind="document")
    provider.output_transform = lambda output, data: {**output, "pages": [{**output["pages"][0], "title": "来源文档不能被知识候选修订"}]}
    before = content_snapshot(env)
    result = finish(env, queue(env, request(env, [source], "topic", source_mode="published")))
    assert result["created_resource_ids"] == result["revision_proposal_ids"] == []
    assert result["skipped"][0]["reason"] == "EXISTING_SOURCE_TITLE_PRESERVED"
    assert content_snapshot(env) == before


def test_invalid_type_is_rejected_without_model(env, provider):
    response = env.call("POST", "/wiki/builds", request(env, [draft_source(env)], "invented-type"))
    assert response.status_code == 422 and provider.calls == 0


def test_receipt_replay_never_regenerates_or_replaces_draft(env, provider):
    jid = queue(env, request(env, [draft_source(env)], "topic"))
    first = execute(env, jid)
    before = content_snapshot(env)
    again = execute(env, jid)
    assert again["created_version_ids"] == first["created_version_ids"]
    assert provider.calls == 1 and content_snapshot(env) == before


@pytest.mark.parametrize("change", ["body", "epoch", "delete", "blob", "acl"])
def test_typed_compilation_rechecks_source_before_any_draft_commit(env, provider, change):
    source = draft_source(env)
    provider.after_call = lambda: change_record(env, source, change)
    with pytest.raises((svc.APIError, wiki.WikiBuildError)):
        execute(env, queue(env, request(env, [source], "topic")))
    with env.db() as db:
        assert list(db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.origin == "AI_DRAFT"))) == []
        assert list(db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like("wiki-compilation:%")))) == []


def test_multi_batch_compiles_whole_scope_in_channel_batches_into_one_page(env, provider):
    source = draft_source(env)
    for ordinal in range(1, 40):  # one article per block: 40 complete sections, more than one batch (32 units) carries
        append_block(env, source, f"第{ordinal}条 估值时核对价格来源、计量日期与输入参数之{ordinal}。", ordinal=ordinal)
    jid = queue(env, request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True))
    result = finish(env, jid)
    assert provider.calls == 3 and result["coverage"]["model_batches"] == 2  # two batches and one page review
    assert len(provider.reviews) == 1 and result["coverage"]["page_review"]["status"] == "APPLIED"
    first, second = provider.requests
    assert [s["batch_index"] for s in (first["source_scope"], second["source_scope"])] == [1, 2]
    assert first["prior_batches_outline"] == "" and "[scope]" in second["prior_batches_outline"]
    ids = [item["id"] for item in first["sources"]] + [item["id"] for item in second["sources"]]
    assert len(ids) == len(set(ids)) == 40  # build-unique evidence ids across batches
    assert len(result["created_version_ids"]) == 1 and result["coverage"]["uncited_selected_blocks"] == 0
    with env.db() as db:
        vid = result["created_version_ids"][0]
        version = db.get(m.ResourceVersion, vid)
        cited = {link.to_block_id for link in db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == vid))}
        source_blocks = set(db.scalars(select(m.ContentBlock.block_id).where(m.ContentBlock.version_id == source[1])))
    assert version.title == "合成topic知识1" and cited == source_blocks


def test_multi_batch_requires_one_typed_page(env, provider):
    source = draft_source(env)
    for data in (request(env, [source], "topic", max_pages=2, multi_batch=True),
                 build_input(env, [source], mode="topic", max_pages=1, multi_batch=True)):
        response = env.call("POST", "/wiki/builds", data)
        assert response.status_code == 422 and response.json()["code"] == "WIKI_MULTI_BATCH_REQUIRES_ONE_TYPED_PAGE"
    assert provider.calls == 0


def test_merge_batches_orders_sections_keeps_citations_and_renames_relations():
    config = compilation.resolve("topic", "knowledge_points")
    def part(title, texts, status="SUPPORTED", relations=()):
        blocks = [{"section": key, "markdown": texts.get(key, f"{key}待补"), "evidence_ids": [f"{title}-{key}"],
                   "support_status": status if key in texts else "GAP"} for key in ("sources_and_gaps", "scope", "overview",
                                                                                     "rule_map", "exceptions")]
        return {"pages": [{"title": title, "category": "c", "knowledge_type": "rule", "aliases": [title], "links": [],
                           "blocks": blocks}], "gaps": [f"{title}缺口"], "relations": list(relations),
                "source_dispositions": [{"evidence_id": f"{title}-x", "disposition": "SUPPORTING", "reason": "r"}]}
    one = part("页", {"scope": "范围甲", "rule_map": "同一规则"},
               relations=[{"source_title": "页", "target_title": "参考", "relation_type": "APPLIES_TO",
                           "evidence_ids": ["e"], "explanation": "说明"}])
    two = part("页（续）", {"scope": "范围乙", "rule_map": "同一规则", "exceptions": "例外乙"},
               relations=[{"source_title": "页（续）", "target_title": "参考", "relation_type": "APPLIES_TO",
                           "evidence_ids": ["f"], "explanation": "说明"}])
    merged = compilation.merge_batches([one, two], config)
    page = merged["pages"][0]
    assert [b["section"] for b in page["blocks"]] == ["scope", "scope", "overview", "rule_map", "exceptions", "sources_and_gaps"]
    assert [b["markdown"] for b in page["blocks"] if b["section"] == "scope"] == ["范围甲", "范围乙"]
    rule = next(b for b in page["blocks"] if b["section"] == "rule_map")
    assert rule["evidence_ids"] == ["页-rule_map", "页（续）-rule_map"]  # duplicate paragraph keeps both citations
    assert next(b for b in page["blocks"] if b["section"] == "overview")["support_status"] == "GAP"
    assert page["title"] == "页" and page["aliases"] == ["页", "页（续）"]
    assert merged["gaps"] == ["页缺口", "页（续）缺口"] and len(merged["source_dispositions"]) == 2
    assert merged["relations"] == [{**one["relations"][0], "evidence_ids": ["e", "f"]}]  # renamed; duplicate's citations kept


def test_merge_marks_extractions_whose_citing_text_was_dropped_for_review():
    config = compilation.resolve("topic", "knowledge_points")
    def part(title, overview_status, disposition):
        blocks = [{"section": key, "markdown": f"{title}{key}", "evidence_ids": [f"{title}-{key}"],
                   "support_status": overview_status if key == "overview" else "SUPPORTED"}
                  for key in ("scope", "overview", "rule_map", "exceptions", "sources_and_gaps")]
        return {"pages": [{"title": title, "category": "c", "knowledge_type": "rule", "aliases": [], "links": [], "blocks": blocks}],
                "gaps": ["缺口"], "source_dispositions": [{"evidence_id": f"{title}-overview", "disposition": disposition, "reason": "r"}]}
    merged = compilation.merge_batches([part("甲", "SUPPORTED", "EXTRACTED"), part("乙", "GAP", "EXTRACTED")], config)
    dispositions = {item["evidence_id"]: item["disposition"] for item in merged["source_dispositions"]}
    assert dispositions == {"甲-overview": "EXTRACTED", "乙-overview": "NEEDS_REVIEW"}
    compilation.validate_structure(merged, config, {"甲-overview", "乙-overview"})


@pytest.mark.parametrize("code", ["PROVIDER_TIMEOUT", "CODEX_MODEL_REQUEST_FAILED"])
def test_multi_batch_retries_only_the_failed_batch(env, provider, code, monkeypatch):
    from fund_kb.providers import ProviderError
    pauses = []
    monkeypatch.setattr(wiki, "_sleep", pauses.append)
    source = draft_source(env)
    for ordinal in range(1, 40):
        append_block(env, source, f"第{ordinal}条 估值时核对价格来源与计量日期之{ordinal}。", ordinal=ordinal)
    def flaky(output, data):
        if provider.calls == 2:  # the first attempt of batch 2 fails transiently
            raise ProviderError(code)
        return output
    provider.output_transform = flaky
    result = finish(env, queue(env, request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True)))
    assert provider.calls == 4 and result["coverage"]["model_batches"] == 2 and len(result["created_version_ids"]) == 1
    assert [r["source_scope"]["batch_index"] for r in provider.requests] == [1, 2, 2]
    assert pauses == [wiki.BATCH_RETRY_DELAY_SECONDS]  # a provider failure waits before the batch is re-sent


def test_multi_batch_takes_the_complete_section_around_a_requested_block(env, provider):
    source = draft_source(env)
    heading = append_block(env, source, "第一条 债券估值", ordinal=1)
    lines = [append_block(env, source, f"债券估值核对要点之{n}。", ordinal=1 + n) for n in range(1, 4)]
    append_block(env, source, "第二条 股票估值", ordinal=5)
    append_block(env, source, "股票估值核对要点。", ordinal=6)
    data = request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True, source_block_ids=[lines[1]])
    result = finish(env, queue(env, data))
    sources = provider.requests[0]["sources"]
    assert len(sources) == 1 and sources[0]["source_block_count"] == 4 and "股票" not in sources[0]["excerpt"]
    with env.db() as db:
        vid = result["created_version_ids"][0]
        cited = {link.to_block_id for link in db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == vid))}
    assert cited == {heading, *lines}  # the whole article, nothing from the next one


def test_multi_batch_splits_an_oversized_section_only_at_paragraph_boundaries(env, provider, monkeypatch):
    source = draft_source(env)
    texts = [f"段落{n}：" + "核对价格来源、计量日期与输入参数。" * 90 for n in range(1, 31)]
    for n, text in enumerate(texts, 1):  # no headings, no pages: one complete section larger than a request
        append_block(env, source, text, ordinal=n)
    resolve = provider.resolve_connection
    monkeypatch.setattr(provider, "resolve_connection", lambda *a, **k: {**resolve(*a, **k), "max_request_bytes": 60000})
    result = finish(env, queue(env, request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True)))
    excerpts = [item["excerpt"] for data in provider.requests for item in data["sources"]]
    assert len(excerpts) > 1 and result["coverage"]["model_batches"] == len(provider.requests)
    for text in texts:  # every paragraph whole, in exactly one passage (each line carries its paragraph id)
        assert sum([re.sub(r"^【S\d+\.\d+】", "", line) for line in excerpt.split("\n")].count(text)
                   for excerpt in excerpts) == 1


def test_merge_keeps_one_heading_per_section():
    config = compilation.resolve("topic", "knowledge_points")
    def part(title, scope_text):
        blocks = [{"section": key, "markdown": scope_text if key == "scope" else f"{title}{key}", "evidence_ids": [f"{title}-{key}"],
                   "support_status": "SUPPORTED"} for key in ("scope", "overview", "rule_map", "exceptions", "sources_and_gaps")]
        return {"pages": [{"title": title, "category": "c", "knowledge_type": "rule", "aliases": [], "links": [], "blocks": blocks}],
                "gaps": []}
    merged = compilation.merge_batches([part("甲", "## 适用范围\n\n第一部分"), part("乙", "## 适用范围\n\n第二部分")], config)
    scope = [b["markdown"] for b in merged["pages"][0]["blocks"] if b["section"] == "scope"]
    assert scope == ["## 适用范围\n\n第一部分", "第二部分"]


def test_multi_batch_drops_relations_that_cannot_exist_instead_of_failing(env, provider):
    source = draft_source(env)
    for ordinal in range(1, 5):
        append_block(env, source, f"第{ordinal}条 估值时核对价格来源之{ordinal}。", ordinal=ordinal)
    def with_bad_relation(output, data):
        ids = [item["id"] for item in data["sources"]]
        output["relations"] = [{"source_title": output["pages"][0]["title"], "target_title": "并不存在的页面",
                                "relation_type": "APPLIES_TO", "evidence_ids": ids[:1],
                                "explanation": "合成说明：关系仅为待核验提案。"}]
        return output
    provider.output_transform = with_bad_relation
    result = finish(env, queue(env, request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True)))
    assert provider.calls == 1 and len(result["created_version_ids"]) == 1 and result["semantic_relations_created"] == 0


def _review_page(*blocks):
    rows = [{"section": section, "markdown": text, "evidence_ids": [f"e{n}"], "support_status": "SUPPORTED"}
            for n, (section, text) in enumerate(blocks, 1)]
    return {"pages": [{"title": "债券", "category": "c", "knowledge_type": "rule", "aliases": [], "links": [], "blocks": rows}],
            "gaps": [], "source_dispositions": [{"evidence_id": f"e{n}", "disposition": "EXTRACTED", "reason": "合成。"}
                                                for n in range(1, len(rows) + 1)]}


def test_page_review_removes_only_verbatim_contradicted_sentences_and_keeps_structure():
    config = compilation.resolve("topic", "knowledge_points")
    merged = _review_page(
        ("scope", "## 适用范围\n\n本页新增内容适用于基金持有的债券。材料未给出债券买卖的会计分录。"),
        ("scope", "- 材料仅覆盖交易所市场。"),
        ("overview", "## 核心概念\n\n买入债券时借记债券投资—成本，银行间与交易所市场均适用。"),
        ("rule_map", "## 规则\n\n原文未给出重大差异的数值阈值。"),
        ("exceptions", "## 例外\n\n材料未提供违约债券的处理。"),
        ("exceptions", "违约债券停止计提利息。"),
        ("sources_and_gaps", "## 来源与缺口\n\n待补充原件核验。"))
    compilation.clean_batch_phrases(merged)
    removed = compilation.apply_review(merged, [
        {"n": 1, "sentence": "材料未给出债券买卖的会计分录。", "covered_in": 3},
        {"n": 2, "sentence": "材料仅覆盖交易所市场。", "covered_in": 3},  # whole paragraph: dropped, scope keeps paragraph 1
        {"n": 4, "sentence": "原文未给出重大差异的数值阈值。", "covered_in": 3},  # last text of its section: kept
        {"n": 5, "sentence": "材料未提供违约债券的处理。", "covered_in": 6},  # dropped; its heading moves to paragraph 6
        {"n": 7, "sentence": "待补充原件核验。"},  # no covering paragraph named: kept
        {"n": 7, "sentence": "待补充原件核验。", "covered_in": 7},  # a paragraph cannot cover itself: kept
        {"n": 3, "sentence": "并不存在于该段的句子。", "covered_in": 1}, {"n": 3, "sentence": "## 核心概念", "covered_in": 1},
        {"n": 99, "sentence": "越界的段落编号。", "covered_in": 1}])
    compilation.mark_uncited(merged, "（整页统稿删除了引用此段的句子）")
    texts = [block["markdown"] for block in merged["pages"][0]["blocks"]]
    assert [(item["n"], item["section"]) for item in removed] == [(1, "scope"), (2, "scope"), (5, "exceptions")]
    assert texts == ["## 适用范围\n\n本页内容适用于基金持有的债券。",
                     "## 核心概念\n\n买入债券时借记债券投资—成本，银行间与交易所市场均适用。",
                     "## 规则\n\n原文未给出重大差异的数值阈值。", "## 例外\n\n违约债券停止计提利息。",
                     "## 来源与缺口\n\n待补充原件核验。"]
    status = {item["evidence_id"]: item["disposition"] for item in merged["source_dispositions"]}
    assert status == {"e1": "EXTRACTED", "e2": "NEEDS_REVIEW", "e3": "EXTRACTED", "e4": "EXTRACTED",
                      "e5": "NEEDS_REVIEW", "e6": "EXTRACTED", "e7": "EXTRACTED"}
    compilation.validate_structure(merged, config, {f"e{n}" for n in range(1, 8)})


def test_page_review_shows_claim_paragraphs_whole_when_others_are_shortened():
    merged = _review_page(("scope", "概述" * 400), ("overview", "材料未提供估值价格的取得方式。" + "补充" * 400))
    shown = compilation.review_paragraphs(merged, 100)
    assert len(shown[0]["markdown"]) == 101 and shown[1]["markdown"] == merged["pages"][0]["blocks"][1]["markdown"]


def _forty_articles(env):
    source = draft_source(env)
    for ordinal in range(1, 40):
        append_block(env, source, f"第{ordinal}条 估值时核对价格来源与计量日期之{ordinal}。", ordinal=ordinal)
    return source


def test_multi_batch_page_review_removes_a_claim_the_page_itself_answers(env, provider):
    source = _forty_articles(env)
    claim = "材料未给出估值价格的取得方式。"
    def with_claim(output, data):
        if data["source_scope"]["batch_index"] == 1:
            output["pages"][0]["blocks"][0]["markdown"] += "\n\n本页新增内容覆盖价格来源。" + claim
        return output
    provider.output_transform = with_claim
    provider.review_output = lambda data: {"remove": [{"n": "x"}, {"n": next(p["n"] for p in data["paragraphs"]
                                                                          if claim in p["markdown"]),
                                                       "sentence": claim, "covered_in": 2}]}  # one malformed item
    result = finish(env, queue(env, request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True)))
    assert provider.calls == 3 and result["coverage"]["page_review"]["status"] == "APPLIED"
    assert result["coverage"]["page_review"]["removed_sentences"] == 1
    assert result["coverage"]["page_review"]["removed"][0]["sentence"] == claim
    assert all("本页新增" not in p["markdown"] for p in provider.reviews[0]["paragraphs"])  # cleaned before the review
    with env.db() as db:
        text = "".join(block.data["text"] for block in db.scalars(select(m.ContentBlock).where(
            m.ContentBlock.version_id == result["created_version_ids"][0])))
    assert claim not in text and "本页内容覆盖价格来源。" in text and "本页新增" not in text


def test_page_review_that_keeps_failing_leaves_the_merged_page(env, provider):
    source = _forty_articles(env)
    provider.review_output = lambda data: "不是JSON"
    result = finish(env, queue(env, request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True)))
    assert provider.calls == 2 + wiki.BATCH_ATTEMPTS and len(result["created_version_ids"]) == 1
    assert result["coverage"]["page_review"] == {"status": "SKIPPED_REVIEW_FAILED",
                                                 "error_code": "WIKI_MODEL_RESPONSE_INVALID", "removed_sentences": 0}


def test_page_review_reads_numbers_given_as_text_or_lists_and_skips_malformed_items():
    merged = _review_page(("scope", "## 适用范围\n\n材料未给出债券买卖的会计分录。本页适用于债券。"),
                          ("overview", "## 核心概念\n\n买入债券时借记债券投资—成本。"), ("rule_map", "## 规则\n\n规则正文。"),
                          ("exceptions", "## 例外\n\n例外正文。"), ("sources_and_gaps", "## 来源与缺口\n\n来源正文。"))
    removed = compilation.apply_review(merged, [
        "不是对象", {"n": [1], "sentence": "材料未给出债券买卖的会计分录。", "covered_in": 2},
        {"n": "1", "sentence": "材料未给出债券买卖的会计分录。", "covered_in": [1, "2"]}])
    assert [item["n"] for item in removed] == [1]
    assert merged["pages"][0]["blocks"][0]["markdown"] == "## 适用范围\n\n本页适用于债券。"


def test_page_review_narrows_a_partly_answered_claim_by_deleting_words_only():
    claim = "所读正文没有给出正常交易、停牌或退市整理期的估值方法。"
    merged = _review_page(("scope", "## 适用范围\n\n新增材料覆盖两类对象。" + claim),
                          ("overview", "## 核心概念\n\n正常交易按收盘价估值，停牌期间按指数收益法估值。"),
                          ("rule_map", "## 规则\n\n规则正文。"), ("exceptions", "## 例外\n\n例外正文。"),
                          ("sources_and_gaps", "## 来源与缺口\n\n来源正文。"))
    compilation.clean_batch_phrases(merged)
    narrowed = "所读正文没有给出退市整理期的估值方法。"
    removed = compilation.apply_review(merged, [
        {"n": 1, "sentence": claim, "covered_in": 2, "narrowed_to": "所读正文没有给出新增的退市整理期估值方法。"},  # adds words
        {"n": 1, "sentence": claim, "covered_in": 2, "narrowed_to": "退市整理期"},  # too short
        {"n": 1, "sentence": claim, "covered_in": 2, "narrowed_to": claim},  # not narrower
        {"n": 1, "sentence": claim, "covered_in": 2, "narrowed_to": narrowed}])
    assert removed == [{"n": 1, "section": "scope", "sentence": claim, "narrowed_to": narrowed}]
    assert merged["pages"][0]["blocks"][0]["markdown"] == "## 适用范围\n\n本页材料覆盖两类对象。" + narrowed


def test_single_typed_build_writes_batch_phrases_as_this_page(env, provider):
    def transform(output, data):
        output["pages"][0]["blocks"][0]["markdown"] += "\n\n### 本批材料列示的条件\n\n新增材料覆盖价格来源。"
        return output
    provider.output_transform = transform
    result = finish(env, queue(env, request(env, [draft_source(env)], "topic")))
    with env.db() as db:
        text = "".join(block.data["text"] for block in db.scalars(select(m.ContentBlock).where(
            m.ContentBlock.version_id == result["created_version_ids"][0])))
    assert "### 本页材料列示的条件" in text and "本页材料覆盖价格来源。" in text and "本批" not in text and "新增材料" not in text


def test_multi_batch_rebuild_of_the_same_topic_recompiles_its_whole_scope(env, provider):
    source = _forty_articles(env)
    data = request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True)
    first = finish(env, queue(env, data))
    second = finish(env, queue(env, copy.deepcopy(data)))
    assert first["created_version_ids"] and second["created_version_ids"]
    assert second["created_version_ids"] != first["created_version_ids"]
    assert second["coverage"]["already_processed_fragments"] == 0 and second["coverage"]["model_batches"] == 2


def _article_with_points(env):
    source = draft_source(env)
    heading = append_block(env, source, "第一条 债券估值", ordinal=1)
    lines = [append_block(env, source, f"债券估值核对要点之{n}。", ordinal=1 + n) for n in range(1, 4)]
    return source, heading, lines


def test_multi_batch_paragraph_ids_link_only_the_cited_paragraph(env, provider):
    source, heading, lines = _article_with_points(env)

    def cite_one_paragraph(output, data):
        for block in output["pages"][0]["blocks"]:
            block["evidence_ids"] = ["S1.3"]
        return output
    provider.output_transform = cite_one_paragraph
    result = finish(env, queue(env, request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True, source_block_ids=[lines[0]])))
    excerpt = provider.requests[0]["sources"][0]["excerpt"]
    assert excerpt.split("\n")[0] == "【S1.1】第一条 债券估值" and "【S1.4】债券估值核对要点之3。" in excerpt
    assert "S12.3" in provider.instructions[0] or "【S12.3】" in provider.instructions[0]
    with env.db() as db:
        vid = result["created_version_ids"][0]
        cited = {link.to_block_id for link in db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == vid))}
    assert cited == {lines[1]}  # S1.3: the third block of the article (heading, point 1, point 2)
    assert result["coverage"]["cited_blocks"] == 1
    # Dispositions stay per passage; citing one of its paragraphs counts as extracting it.
    assert [(d["evidence_id"], d["disposition"]) for d in result["coverage"]["source_dispositions"]] == [("S1", "EXTRACTED")]


def test_a_passage_id_still_links_its_whole_passage(env, provider):
    source, heading, lines = _article_with_points(env)
    result = finish(env, queue(env, request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True, source_block_ids=[lines[0]])))
    with env.db() as db:
        vid = result["created_version_ids"][0]
        cited = {link.to_block_id for link in db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == vid))}
    assert cited == {heading, *lines}


def test_a_paragraph_id_outside_its_passage_is_an_invalid_citation(env, provider):
    source, _, lines = _article_with_points(env)

    def cite_missing_paragraph(output, data):
        output["pages"][0]["blocks"][0]["evidence_ids"] = ["S1.9"]
        return output
    provider.output_transform = cite_missing_paragraph
    before = content_snapshot(env)
    with pytest.raises(wiki.WikiBuildError) as caught:
        execute(env, queue(env, request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True, source_block_ids=[lines[0]])))
    assert caught.value.code == "WIKI_CITATION_INVALID"
    assert content_snapshot(env) == before


def test_related_pages_line_carries_no_evidence(env, provider):
    source, _, lines = _article_with_points(env)
    other = page(env, "股票估值核对")

    def link_related(output, data):
        output["pages"][0]["links"] = ["股票估值核对"]
        return output
    provider.output_transform = link_related
    result = finish(env, queue(env, request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True, source_block_ids=[lines[0]])))
    with env.db() as db:
        vid = result["created_version_ids"][0]
        related = db.scalar(select(m.ContentBlock).where(m.ContentBlock.version_id == vid,
                                                         m.ContentBlock.search_text.like("相关知识%")))
        links = list(db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == vid,
                                                             m.EvidenceLink.from_block_id == related.block_id)))
    assert other and related is not None and links == []


def test_a_larger_model_channel_does_not_enlarge_compile_batches(env, provider, monkeypatch):
    source = draft_source(env)
    for n in range(1, 31):
        append_block(env, source, f"段落{n}：" + "核对价格来源、计量日期与输入参数。" * 90, ordinal=n)
    resolve = provider.resolve_connection
    counts = []
    for capacity in (65536, 196608):
        monkeypatch.setattr(provider, "resolve_connection",
                            lambda *a, capacity=capacity, **k: {**resolve(*a, **k), "max_request_bytes": capacity})
        before = len(provider.requests)
        finish(env, queue(env, request(env, [source], "topic", "knowledge_points", max_pages=1, multi_batch=True)))
        counts.append(len(provider.requests) - before)
    assert counts[0] > 1 and counts[0] == counts[1]
