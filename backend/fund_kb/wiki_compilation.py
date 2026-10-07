"""Versioned, source-bound Wiki compilation contracts (no model or network IO).

The content type is independent of graph granularity. Legacy callers retain
their JSON envelope; explicit types opt in to section and SOP-step validation.
Completeness below concerns the frozen input scope, never legal correctness.
"""
from __future__ import annotations

import copy
import json
import re

SPEC_VERSION = "wiki-compilation/1.3"
TYPES = ("topic", "atomic_rule", "scenario", "sop")

# Length is an output budget, NOT a prose-length requirement. A small complete
# page is preferable to filling a quota; a long condition must not be cut off.
_SPECS = {
    "topic": {
        "label": "专题综述", "purpose": "组织完整主题的定义、规则体系、适用范围与关联知识，不把目录当作知识正文。",
        "knowledge_type": None, "tokens_per_page": 8192, "max_output_tokens": 32768,
        "max_source_utf8_bytes": 96000, "max_input_utf8_bytes": 128000,
        "sections": [
            ("scope", "专题与适用范围", "对象、主体、业务日期、边界；缺失条件写明需补充的具体事实。"),
            ("overview", "核心概念与全景", "关键定义及主题内部关系；区分来源事实与归纳解释。"),
            ("rule_map", "规则与方法体系", "规则分类、方法、输入输出、各自条件和来源；关联具体规则页。"),
            ("exceptions", "例外与冲突", "例外、冲突口径、旧版与新版差异；不推定法律优先级。"),
            ("sources_and_gaps", "来源、核对与缺口", "书目定位、未覆盖子主题及需补充原件；列出尚缺的原文或规定。"),
        ],
    },
    "atomic_rule": {
        "label": "原子规则", "purpose": "一个可独立引用的规则，完整保留触发条件、参数、例外和时效，不等于一句短摘录。",
        "knowledge_type": "rule", "tokens_per_page": 4096, "max_output_tokens": 24576,
        "max_source_utf8_bytes": 64000, "max_input_utf8_bytes": 96000,
        "sections": [
            ("rule", "规则陈述", "准确表达单条规则；不得省略限定词或扩展原文效力。"),
            ("applicability", "适用与触发条件", "资产、产品、主体、事件、时间及前置条件。"),
            ("method", "处理方法与输出", "计算或操作方法、步骤、输出；原文未规定时标明缺口。"),
            ("parameters", "参数与输入", "参数含义、口径、单位、数据来源与缺失值处理。"),
            ("exceptions", "例外与不适用", "例外条件、不适用范围、依赖及与相近规则的区别。"),
            ("review_and_sources", "出处与核验", "原文定位、版本、需复核事项及尚缺依据。"),
        ],
    },
    "scenario": {
        "label": "场景方案", "purpose": "围绕实际业务问题提供条件化判断路径与解决方案，不虚构输入事实或已执行结果。",
        "knowledge_type": "scenario", "tokens_per_page": 8192, "max_output_tokens": 49152,
        "max_source_utf8_bytes": 96000, "max_input_utf8_bytes": 128000,
        "sections": [
            ("question", "业务问题与边界", "场景目标、对象、前提与不在本场景内的事项。"),
            ("facts", "需确认的事实", "已知与未知分开，列出必须补充的业务输入。"),
            ("decision_path", "条件判断路径", "按如果/则分支解释选择依据、替代路径及依赖。"),
            ("recommended_handling", "建议处理方案", "各适用分支的操作、输入、预期输出与核对方式。"),
            ("exceptions_and_escalation", "异常与升级", "冲突、缺数据、例外和何时提交专家核验。"),
            ("evidence_and_gaps", "证据与未决事项", "支撑依据、适用限制、未覆盖事项；不是业务完成确认。"),
        ],
    },
    "sop": {
        "label": "标准作业程序", "purpose": "形成可复核的完整作业流程，明确责任、输入输出、控制点、异常与留痕。",
        "knowledge_type": "sop", "tokens_per_page": 12288, "max_output_tokens": 65536,
        "max_source_utf8_bytes": 128000, "max_input_utf8_bytes": 160000,
        "sections": [
            ("purpose_and_scope", "目的与适用范围", "工作目标、适用对象、角色边界及前置版本。"),
            ("prerequisites", "前置条件", "业务输入、权限、资料版本、参数及准备检查。"),
            ("steps", "操作步骤", "每步完整列明责任角色、输入、操作、输出、核对、异常和依赖；不得虚构完成状态。"),
            ("controls", "复核与控制", "制复核分工、控制点、验收条件与禁止事项。"),
            ("exceptions_and_escalation", "异常处理与升级", "失败、冲突、缺失数据和升级路径；未知责任不猜测。"),
            ("records_and_evidence", "留痕与依据", "留存资料、版本、记录、来源及尚待确认事项。"),
        ],
    },
}


def resolve(compilation_type=None, granularity="topic", contract_mode=None):
    if granularity not in {"topic", "knowledge_points", "relations"}:
        raise ValueError("WIKI_GRANULARITY_INVALID")
    explicit = compilation_type is not None if contract_mode is None else contract_mode == "typed"
    kind = compilation_type if compilation_type is not None else ("atomic_rule" if granularity == "knowledge_points" else "topic")
    if kind not in TYPES or contract_mode not in {None, "typed", "legacy"}:
        raise ValueError("WIKI_COMPILATION_TYPE_INVALID")
    return {"compilation_type": kind, "compilation_spec_version": SPEC_VERSION,
            "compilation_contract": "typed" if explicit else "legacy",
            "output_kind": "relations_only" if granularity == "relations" else "pages"}


def budget(config, max_pages):
    spec = _SPECS[config["compilation_type"]]
    if config["compilation_contract"] == "legacy":
        return {"max_output_tokens": min(4096, 800 * max_pages), "max_output_utf8_bytes": 128000,
                "max_source_utf8_bytes": 8500, "max_input_utf8_bytes": 16000}
    tokens = min(spec["max_output_tokens"], 1024 + spec["tokens_per_page"] * max_pages)
    if config["output_kind"] == "relations_only":
        tokens = min(tokens, 16384)
    return {"max_output_tokens": tokens, "max_output_utf8_bytes": max(128000, tokens * 12),
            "max_source_utf8_bytes": spec["max_source_utf8_bytes"], "max_input_utf8_bytes": spec["max_input_utf8_bytes"]}


def public_specs():
    specs = []
    for kind, raw in _SPECS.items():
        item = {key: copy.deepcopy(value) for key, value in raw.items() if key != "sections"}
        item.update(compilation_type=kind, spec_version=SPEC_VERSION,
                    sections=[{"key": key, "label": label, "requirement": description, "required": True}
                              for key, label, description in raw["sections"]],
                    no_body_length_cap=True, requires_review=True)
        specs.append(item)
    return {"spec_version": SPEC_VERSION, "default_compilation_type": "topic", "types": specs,
            "legacy_defaults": {"topic": "topic", "knowledge_points": "atomic_rule", "relations": "topic"},
            "granularity_is_independent": True, "automatic_generation": False,
            "source_policy": {"typed": "whole_semantic_passages_in_source_order",
                              "explicit_scope": "all_or_reject_before_model",
                              "legacy": "fragment_batches_with_explicit_coverage",
                              "max_units_per_batch": 32, "silent_truncation": False},
            "completeness_notice": "COMPLETE仅表示指定冻结范围的输入或结构覆盖；不表示穷尽知识、专业核验或正式发布。"}


def instruction(config):
    kind = config["compilation_type"]
    spec = _SPECS[kind]
    sections = "；".join(f"{key}（{label}）：{description}" for key, label, description in spec["sections"])
    content = (f"本次编译类型为{kind}（{spec['label']}），规范{SPEC_VERSION}。{spec['purpose']}"
        f"完整结构要求：{sections}。不统一限制正文字数或段落数，不能为满足页数或输出预算删减适用条件、例外和核对步骤。"
        "max_pages只是上限；优先生成较少但完整的页面，不能生成半页后冒充完整。来源不足时写明缺少哪份原文或哪项规定，禁止编造填充。"
        "仅覆盖本次输入窗口；目录和未送入正文不等于已经阅读，未覆盖知识和缺失来源写入gaps。"
        "输入中的review_notice只是平台对来源的管理信息（资料分类、审核状态、效力字段、归档标注），只用于识别历史版本、"
        "征求意见稿等材料性质；不要写进标题或正文，不写“法律状态未知”“效力状态待核验”“转录件待业务复核”“图像校对转录”"
        "“未完成正式审核”之类说明，页面与来源状态由平台另行显示。正文中的施行、废止与新旧替代只依据来源原文自身的条款。")
    if config["compilation_contract"] == "typed":
        content += ("每个block用section标注所属结构，support_status为SUPPORTED/GAP/NOT_APPLICABLE；所有必需结构都要有正文。"
            "每段保留本批evidence_ids，正文使用可读Markdown标题；缺口段也只关联其所核对的来源，不把该关联当作实质支撑。"
            "GAP应同时写入gaps。每个输入S编号必须有且只有一个source_dispositions，EXTRACTED必须有页面或关系引用。"
            "含多个段落的来源，每段前标有段落编号（如【S12.3】）：evidence_ids写直接支持本段内容的具体段落编号"
            "（如S12.3），不要用整节编号S12代替；只有本段确实概括了整节时才引用整节编号。"
            "页面标题写主题本身（资产、业务场景或规则名称），不加“待核验”“待核事项”“缺口”等状态说明；generation_brief"
            "指定了页面标题时照用。SUPPORTED段只写来源支持的内容，不夹带“材料未提供/未给出/未载明”之类的说法；确有缺口时"
            "写成独立的GAP段并写入gaps，具体写明缺少哪份原文或哪项规定。")
        if kind == "sop":
            content += ("steps的每个block还需step对象：step_id、owner_role、inputs、action、outputs、checks、exceptions、depends_on。"
                "这些字段须在该block的Markdown中完整呈现；责任未规定写待确认，依赖只用本页更早的step_id。")
    else:
        content += "旧调用兼容原JSON字段；这些结构作为编写规范，旧输出不宣称已经通过新结构验证。"
    if config["output_kind"] == "relations_only":
        content += "本次仅关系整理，pages必须为空；上述页面结构不要求生成，不改写参考节点。"
    return content


def output_schema(base, config, *, semantic_mode=False):
    schema = copy.deepcopy(base)
    page = schema["properties"]["pages"]["items"]
    block = page["properties"]["blocks"]["items"]
    # These are structural JSON objects, not a short-card format. Transport and
    # per-call output budgets reject incomplete responses without slicing them.
    block["properties"]["markdown"].pop("maxLength", None)
    page["properties"]["blocks"].pop("maxItems", None)
    schema["properties"]["gaps"].pop("maxItems", None)
    schema["properties"]["gaps"]["items"].pop("maxLength", None)
    if "relations" in schema["properties"]:
        schema["properties"]["relations"]["items"]["properties"]["explanation"].pop("maxLength", None)
        schema["properties"]["source_dispositions"]["items"]["properties"]["reason"].pop("maxLength", None)
    if config["compilation_contract"] != "typed":
        return schema
    spec = _SPECS[config["compilation_type"]]
    block["properties"]["section"] = {"enum": [item[0] for item in spec["sections"]]}
    block["properties"]["support_status"] = {"enum": ["SUPPORTED", "GAP", "NOT_APPLICABLE"]}
    block["required"] += ["section", "support_status"]
    # A single complete paragraph may cite more than eight source passages.
    block["properties"]["evidence_ids"].pop("maxItems", None)
    if spec["knowledge_type"]:
        page["properties"]["knowledge_type"] = {"const": spec["knowledge_type"]}
    if config["compilation_type"] == "sop":
        text = {"type": "string", "minLength": 1}
        values = {"type": "array", "minItems": 1, "items": text}
        block["properties"]["step"] = {"type": "object", "additionalProperties": False,
            "required": ["step_id", "owner_role", "inputs", "action", "outputs", "checks", "exceptions", "depends_on"],
            "properties": {"step_id": text, "owner_role": text, "inputs": values, "action": text,
                "outputs": values, "checks": values, "exceptions": values,
                "depends_on": {"type": "array", "uniqueItems": True, "items": text}}}
        block["allOf"] = [{"if": {"properties": {"section": {"const": "steps"}}},
                           "then": {"required": ["step"]}, "else": {"not": {"required": ["step"]}}}]
    if not semantic_mode:
        text = {"type": "string", "minLength": 1}
        schema["required"].append("source_dispositions")
        schema["properties"]["source_dispositions"] = {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["evidence_id", "disposition", "reason"],
            "properties": {"evidence_id": text, "disposition": {
                "enum": ["EXTRACTED", "SUPPORTING", "NO_REUSABLE_POINT", "NEEDS_REVIEW"]}, "reason": text}}}
    return schema


def validate_structure(parsed, config, selected):
    if config["compilation_contract"] != "typed":
        return
    sections = {key for key, _, _ in _SPECS[config["compilation_type"]]["sections"]}
    cited = {evidence_section(eid) for page in parsed["pages"] for block in page["blocks"] for eid in block["evidence_ids"]}
    cited.update(evidence_section(eid) for edge in parsed.get("relations", []) for eid in edge["evidence_ids"])
    for page in parsed["pages"]:
        if {block["section"] for block in page["blocks"]} != sections:
            raise ValueError("WIKI_COMPILATION_STRUCTURE_INCOMPLETE")
        if any(block["support_status"] == "GAP" for block in page["blocks"]) and not parsed["gaps"]:
            raise ValueError("WIKI_COMPILATION_GAP_UNDECLARED")
        known_steps = set()
        for block in page["blocks"]:
            step = block.get("step")
            if step:
                text_values = [step["step_id"], step["owner_role"], step["action"], *step["inputs"],
                               *step["outputs"], *step["checks"], *step["exceptions"], *step["depends_on"]]
                if any(not value.strip() for value in text_values):
                    raise ValueError("WIKI_SOP_STEP_INCOMPLETE")
                if step["step_id"] in known_steps or not set(step["depends_on"]) <= known_steps:
                    raise ValueError("WIKI_SOP_DEPENDENCY_INVALID")
                known_steps.add(step["step_id"])
    dispositions = parsed["source_dispositions"]
    ids = [item["evidence_id"] for item in dispositions]
    if len(ids) != len(set(ids)) or set(ids) != set(selected):
        raise ValueError("WIKI_SOURCE_DISPOSITION_INCOMPLETE")
    if any(item["disposition"] == "EXTRACTED" and item["evidence_id"] not in cited for item in dispositions):
        raise ValueError("WIKI_EXTRACTED_WITHOUT_EVIDENCE")


def semantic_passages(sources):
    """Keep original blocks whole; repair contiguous PDF line wrapping only.

    A heading stays with the following block. Paragraph blocks are not split at
    arbitrary character offsets. Unknown punctuation/oversize passages require
    a smaller explicit source selection, not a silently clipped excerpt.
    """
    groups = []
    for record in sources:
        previous = groups[-1] if groups else None
        last = previous.get("source_members", [previous])[-1] if previous else None
        continuation = bool(last and (last.get("block_type") == "heading" or
            (last.get("locator", {}).get("kind") == "pdf" and not re.search(r"[。！？.!?；;][\s’”'\"]*$", last["text"]))))
        if continuation and last["version_id"] == record["version_id"] and last["ordinal"] + 1 == record["ordinal"]:
            groups[-1] = {**previous, "text": previous["text"] + "\n" + record["text"],
                          "source_members": [*previous.get("source_members", [previous]), record]}
        else:
            groups.append({**record, "char_start": 0, "char_end": len(record["text"])})
    return groups


def join_members(members):
    """One passage record from consecutive source blocks of one version (a single block stays itself)."""
    if len(members) == 1:
        return {**members[0], "char_start": 0, "char_end": len(members[0]["text"])}
    return {**members[0], "text": "\n".join(member["text"] for member in members), "source_members": list(members)}


def section_passages(records, wanted=None):
    """Multi-batch builds: one passage per minimal complete source section (chapter, section, article or
    paragraph group, as recognized by source_sections v3 - the same complete sections the answer reader
    uses) that contains a wanted block. The whole section is taken; passages follow document structure,
    never a character-count window. records: every text block of the build's source versions, in source
    order; wanted: requested block ids (None = every block)."""
    from .source_sections import build_document_sections, select_sections_from_outline
    by_version = {}
    for record in records:
        by_version.setdefault(record["version_id"], []).append(record)
    passages = []
    for rows in by_version.values():
        rows = sorted(rows, key=lambda record: (record["ordinal"], record["block_id"]))
        index = {record["block_id"]: record for record in rows}
        anchors = set(index) if wanted is None else set(index) & set(wanted)
        if not anchors:
            continue
        outline = build_document_sections(rows, structure_version="v3")
        for section in select_sections_from_outline(outline, anchors):
            members = [index[block_id] for block_id in section["block_ids"] if block_id in index]
            if members:
                passages.append({**join_members(members), "section_title": section["title"]})
    return passages


PARAGRAPH_ID = re.compile(r"^(S\d+)\.(\d+)$")


def marked_item(record, number):
    """The model-facing item. A passage of several source blocks lists each block under its own paragraph id
    (【S12.3】), so a statement cites the paragraphs that support it rather than the whole section."""
    item = _item(record, number)
    members = record.get("source_members") or []
    if len(members) > 1:
        item["excerpt"] = "\n".join(f"【S{number}.{index}】{member['text']}" for index, member in enumerate(members, 1))
    return item


def evidence_section(eid):
    """The passage (input S id) an evidence id belongs to: S12.3 -> S12; a passage id is itself."""
    match = PARAGRAPH_ID.match(eid)
    return match[1] if match else eid


def evidence_records(selected, eid):
    """Source block records an evidence id stands for: the one block of a paragraph id (S12.3), or every block
    of a passage id (S12). KeyError for an id the build did not send."""
    record = selected[evidence_section(eid)]
    members = record.get("source_members") or [record]
    match = PARAGRAPH_ID.match(eid)
    if not match:
        return list(members)
    index = int(match[2]) - 1
    if not 0 <= index < len(members):
        raise KeyError(eid)
    return [join_members([members[index]])]


def known_evidence(selected, eid):
    try:
        evidence_records(selected, eid)
    except KeyError:
        return False
    return True


def _item(record, number):
    item = {"id": f"S{number}", "title": record["title"], "version_id": record["version_id"],
            "block_id": record["block_id"], "content_sha256": record["content_sha256"], "excerpt": record["text"],
            "char_start": record.get("char_start", 0), "char_end": record.get("char_end", len(record["text"]))}
    if record.get("review_notice"):
        item["review_notice"] = record["review_notice"]
    if record.get("source_members"):
        item["source_block_count"] = len(record["source_members"])
        item["source_blocks"] = [anchor(member) for member in record["source_members"]]
        # The excerpt spans multiple originals; the first block offsets
        # alone must never be presented as its whole source locator.
        item["locator_kind"] = "contiguous_source_blocks"
    return item


def _pending(sources, processed, key, strict_scope):
    pending = [record for record in sources if key(record) not in processed]
    already = len(sources) - len(pending)
    if strict_scope and pending:
        # An explicitly selected window must include its prior processed middle
        # paragraphs as context, even when only the tail is new.
        pending, already = list(sources), 0
    return pending, already


def all_passages(sources, processed, key, *, strict_scope=False):
    """Every pending passage, in source order, with a build-unique S id (multi-batch builds split later)."""
    pending, already = _pending(sources, processed, key, strict_scope)
    return [(_item(record, number), record) for number, record in enumerate(pending, 1)], already


def whole_batch(sources, processed, key, *, max_bytes, max_units=32, strict_scope=False):
    pending, already = _pending(sources, processed, key, strict_scope)
    chosen, used = [], 0
    for record in pending:
        item = _item(record, len(chosen) + 1)
        size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if used + size > max_bytes or len(chosen) >= max_units:
            if strict_scope:
                raise ValueError("WIKI_SCOPE_REQUIRES_SPLIT")
            if not chosen:
                raise ValueError("WIKI_PASSAGE_EXCEEDS_BATCH_BUDGET")
            break  # Never skip a middle paragraph to sample a later one.
        chosen.append((item, record))
        used += size
    return chosen, already


def batch_outline(parts, limit):
    """What earlier batches already wrote (section + opening of each supported block), at most `limit` chars."""
    lines = []
    for part in parts:
        for page in part["pages"]:
            for block in page["blocks"]:
                if block["support_status"] == "SUPPORTED":
                    lines.append(f"[{block['section']}] {block['markdown'].strip().splitlines()[0][:80]}")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit]


def merge_batches(parts, config):
    """One typed page from per-batch outputs of the same page.

    Sections keep the spec order. Within a section every batch's SUPPORTED blocks are kept in batch
    order (exact duplicates dropped); a section without supported text keeps its first NOT_APPLICABLE,
    else GAP, block. Gaps, dispositions and relations are unions; relation endpoints naming any batch's
    page title are renamed to the merged (first) title. Evidence ids are build-unique, so bindings hold.
    """
    pages = [page for part in parts for page in part["pages"]]
    gaps = list(dict.fromkeys(gap for part in parts for gap in part["gaps"]))
    merged = {"pages": [], "gaps": gaps}
    if any("source_dispositions" in part for part in parts):
        merged["source_dispositions"] = [item for part in parts for item in part.get("source_dispositions", [])]
    if pages:
        first = pages[0]
        blocks = []
        for key, _, _ in _SPECS[config["compilation_type"]]["sections"]:
            candidates = [block for page in pages for block in page["blocks"] if block["section"] == key]
            seen, kept = {}, []
            for block in candidates:
                text = block["markdown"].strip()
                if block["support_status"] != "SUPPORTED":
                    continue
                if text in seen:  # same paragraph from another batch: keep its citations too
                    seen[text]["evidence_ids"] = list(dict.fromkeys([*seen[text]["evidence_ids"], *block["evidence_ids"]]))
                    continue
                seen[text] = {**block, "evidence_ids": list(block["evidence_ids"])}
                kept.append(seen[text])
            if not kept:
                fallback = ([b for b in candidates if b["support_status"] == "NOT_APPLICABLE"]
                            or [b for b in candidates if b["support_status"] == "GAP"])
                kept = fallback[:1]
            # One section heading per section: later batches' repeated leading headings are dropped.
            for later in kept[1:]:
                lines = later["markdown"].lstrip().split("\n", 1)
                if lines[0].startswith("#") and len(lines) == 2 and lines[1].strip():
                    later["markdown"] = lines[1].lstrip("\n")
            blocks.extend(kept)
        page = {**first, "blocks": blocks,
                "aliases": list(dict.fromkeys(a for p in pages for a in p.get("aliases", [])))[:8],
                "links": list(dict.fromkeys(link for p in pages for link in p.get("links", [])))[:12]}
        merged["pages"] = [page]
    if any("relations" in part for part in parts):
        titles = {page["title"] for page in pages}
        target = pages[0]["title"] if pages else None
        relations, seen = [], {}
        for part in parts:
            for edge in part.get("relations", []):
                edge = {**edge, "evidence_ids": list(edge["evidence_ids"]),
                        "source_title": target if edge["source_title"] in titles else edge["source_title"],
                        "target_title": target if edge["target_title"] in titles else edge["target_title"]}
                identity = (edge["source_title"], edge["target_title"], edge["relation_type"])
                if edge["source_title"] == edge["target_title"]:
                    continue
                if identity in seen:  # same relation from another batch: keep its citations too
                    seen[identity]["evidence_ids"] = list(dict.fromkeys([*seen[identity]["evidence_ids"], *edge["evidence_ids"]]))
                    continue
                seen[identity] = edge
                relations.append(edge)
        merged["relations"] = relations
    # A passage whose only citing text did not survive the merge (e.g. a batch's GAP note for a section another
    # batch covered) is not claimed as extracted: it is marked for review instead of being re-attached elsewhere.
    mark_uncited(merged, "（分批合并后正文未保留引用此段的内容）")
    return merged


def mark_uncited(merged, note):
    """EXTRACTED passages no longer cited by any kept text or relation become NEEDS_REVIEW with `note`."""
    if "source_dispositions" not in merged:
        return
    cited = {evidence_section(eid) for page in merged["pages"] for block in page["blocks"] for eid in block["evidence_ids"]}
    cited.update(evidence_section(eid) for edge in merged.get("relations", []) for eid in edge["evidence_ids"])
    merged["source_dispositions"] = [
        {**item, "disposition": "NEEDS_REVIEW", "reason": item["reason"] + note}
        if item["disposition"] == "EXTRACTED" and item["evidence_id"] not in cited else item
        for item in merged["source_dispositions"]]


# How a batch tends to say "what this batch adds"; on the merged page that is simply the page.
BATCH_PHRASES = (("本页新增内容", "本页内容"), ("本页新增", "本页"), ("新增材料", "本页材料"), ("本批次", "本页"),
                 ("本批", "本页"))
# Sentences stating that the materials lack or only cover something: the ones a page-level review must see in full.
COVERAGE_CLAIM = re.compile(r"未提供|未给出|未载明|未涉及|未覆盖|未列明|没有给出|没有提供|没有载明|只覆盖|仅覆盖|只涉及|仅涉及"
                            r"|只提供|仅提供|只支持|仅支持|不能据此|缺少|尚无|待补充")
_EMPTY_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)、]|（\d+）)\s*$")


def clean_batch_phrases(merged):
    for page in merged["pages"]:
        for block in page["blocks"]:
            for old, new in BATCH_PHRASES:
                block["markdown"] = block["markdown"].replace(old, new)


def review_paragraphs(merged, shorten=None):
    """Numbered paragraphs of a merged page for the page-level review (text only; citations stay server-side).
    With `shorten`, paragraphs without a coverage claim are cut to that many characters to fit the channel;
    paragraphs that make a claim always stay whole."""
    rows = []
    for n, block in enumerate(merged["pages"][0]["blocks"], 1):
        text = block["markdown"]
        if shorten and not COVERAGE_CLAIM.search(text) and len(text) > shorten:
            text = text[:shorten] + "…"
        rows.append({"n": n, "section": block["section"], "markdown": text})
    return rows


def _body(markdown):
    return "\n".join(line for line in markdown.splitlines() if not line.lstrip().startswith("#")).strip()


def _paragraph_number(value, count, exclude=None):
    """A valid 1-based paragraph number from an int, a digit string or (covered_in) a list of them."""
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, str) and item.strip().isdigit():
            item = int(item.strip())
        if type(item) is int and 1 <= item <= count and item != exclude:
            return item
    return None


def _deletions_only(sentence, narrowed):
    """True when `narrowed` is `sentence` with characters deleted (an ordered subsequence): nothing new is added."""
    remaining = iter(sentence)
    return all(char in remaining for char in narrowed)


def apply_review(merged, removals):
    """Delete verbatim sentences that the page-level review found contradicted by the page's own paragraphs.

    Each removal must name another paragraph of the page that covers the content (covered_in), else it is ignored.
    A claim only partly covered may instead be narrowed: `narrowed_to` must be the sentence with characters deleted
    (an ordered subsequence, at least 6 characters), otherwise it is ignored. Nothing is rewritten or added.
    Headings stay; a sentence that is not found exactly is ignored. A paragraph
    left without text is dropped and its heading moves to the next paragraph of the same section, but the last
    text of a section is never removed, so the structure stays complete. Returns the removed sentences."""
    blocks = merged["pages"][0]["blocks"]
    dropped, removed = set(), []
    for item in removals:
        if not isinstance(item, dict) or isinstance(item.get("n"), list):
            continue
        n = _paragraph_number(item.get("n"), len(blocks))
        sentence = item.get("sentence").strip() if isinstance(item.get("sentence"), str) else ""
        if n is None or len(sentence) < 6 or "\n#" in "\n" + sentence:
            continue
        if _paragraph_number(item.get("covered_in"), len(blocks), exclude=n) is None:
            continue
        block = blocks[n - 1]
        if n in dropped or sentence not in block["markdown"]:
            continue
        narrowed = item.get("narrowed_to").strip() if isinstance(item.get("narrowed_to"), str) else ""
        if narrowed:
            if len(narrowed) < 6 or narrowed == sentence or not _deletions_only(sentence, narrowed):
                continue
            block["markdown"] = block["markdown"].replace(sentence, narrowed, 1)
            removed.append({"n": n, "section": block["section"], "sentence": sentence, "narrowed_to": narrowed})
            continue
        text = block["markdown"].replace(sentence, "", 1)
        text = "\n".join(line for line in text.splitlines() if not _EMPTY_ITEM.match(line))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not _body(text):
            if not any(other is not block and other["section"] == block["section"] and i + 1 not in dropped
                       and _body(other["markdown"]) for i, other in enumerate(blocks)):
                continue
            dropped.add(n)
        block["markdown"] = text
        removed.append({"n": n, "section": block["section"], "sentence": sentence})
    kept, carry = [], {}
    for n, block in enumerate(blocks, 1):
        if n in dropped:
            carry.setdefault(block["section"], []).extend(
                line for line in block["markdown"].splitlines() if line.lstrip().startswith("#"))
            continue
        if carry.get(block["section"]):
            block["markdown"] = "\n".join(carry.pop(block["section"])) + "\n\n" + block["markdown"]
        kept.append(block)
    merged["pages"][0]["blocks"] = kept
    return removed


def anchor(record):
    return {"resource_id": record["resource_id"], "version_id": record["version_id"],
            "block_id": record["block_id"], "char_start": record.get("char_start", 0),
            "char_end": record.get("char_end", len(record["text"])), "content_sha256": record["content_sha256"]}


def batch_coverage(fragments, selected, processed, used_ids, gaps, *, config, corpus_blocks, scoped_blocks,
                   unresolved=(), explicit_scope=False):
    """Enumerate every scope unit without embedding private document bodies."""
    from .wiki import _members, _source_key

    selected_keys = {_source_key(record): eid for eid, record in selected.items()}
    incorporated = set(processed) | {_source_key(selected[eid]) for eid in used_ids}
    units, counts = [], {"INCORPORATED": 0, "NOT_SENT": 0, "SENT_NOT_INCORPORATED": 0, "REVIEW_REQUIRED": 0}
    unresolved = set(unresolved)
    for record in fragments:
        key = _source_key(record)
        state = ("REVIEW_REQUIRED" if key in unresolved else "INCORPORATED" if key in incorporated
                 else "SENT_NOT_INCORPORATED" if key in selected_keys else "NOT_SENT")
        counts[state] += 1
        units.append({"unit_id": key, "evidence_id": selected_keys.get(key), "status": state,
                      "source_blocks": [anchor(member) for member in _members(record)]})
    return {"scope": "explicit_blocks" if explicit_scope else "selected_documents",
            "scope_status": "COMPLETE" if counts["INCORPORATED"] == len(fragments) and not gaps else "PARTIAL",
            "input_status": "COMPLETE" if len(selected) == len(fragments) else "NO_NEW_CONTENT" if not selected else "PARTIAL",
            "content_truncated": False if config["compilation_contract"] == "typed" else any(
                member.get("source_original_characters", len(member["text"])) > len(member["text"])
                for record in fragments for member in _members(record)),
            "source_policy": "whole_semantic_passages_in_source_order" if config["compilation_contract"] == "typed"
                             else "legacy_fragment_batches",
            "units": units, "counts": counts, "total_units": len(fragments),
            "outside_scope_blocks": corpus_blocks - scoped_blocks, "automatic_next_batch": False,
            "knowledge_completeness": "NOT_EVALUATED", "professional_accuracy": "NOT_EVALUATED"}


def page_metadata(page, config, batch):
    return {**copy.deepcopy(config), "structure_status": "VALIDATED" if config["compilation_contract"] == "typed"
            else "LEGACY_NOT_VALIDATED", "review_required": True,
            "sections": [{"block_ordinal": index + 1,
                          **{key: copy.deepcopy(block[key]) for key in ("section", "support_status", "step") if key in block}}
                         for index, block in enumerate(page["blocks"])],
            "batch_scope_status": batch["scope_status"], "batch_input_status": batch["input_status"],
            "knowledge_completeness": "NOT_EVALUATED"}


def step_markdown(step):
    """Keep required SOP fields visible, not just hidden in sidecar metadata."""
    fields = [("步骤编号", step["step_id"]), ("责任角色", step["owner_role"]),
              ("输入", "；".join(step["inputs"])), ("操作", step["action"]),
              ("预期输出", "；".join(step["outputs"])), ("核对与验收", "；".join(step["checks"])),
              ("异常处理", "；".join(step["exceptions"])), ("前置步骤", "、".join(step["depends_on"]) or "无")]
    return "\n\n".join(f"**{label}**：{value}" for label, value in fields)
