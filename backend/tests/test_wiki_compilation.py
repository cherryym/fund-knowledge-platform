"""Typed Wiki compilation in a temporary DB, synthetic completion, zero network."""
from __future__ import annotations

import copy
import json

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

    def complete(self, snapshot, messages, max_tokens=4096, json_mode=True, timeout=60):
        self.calls += 1
        data = json.loads(messages[1]["content"])
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
