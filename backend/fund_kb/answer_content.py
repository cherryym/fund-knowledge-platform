"""Small model-owned content contract; the server owns all evidence identities.

No model I/O or credential access here. Public analysis is an evidence-backed
explanation for the user, never a transcript of hidden model reasoning.
"""
import copy
import hashlib
import json
import re

from jsonschema import Draft202012Validator

from .answer_prompt import FUND_ACCOUNTING_SYSTEM_PROMPT

VERSION = "fund-answer-content-v1"
TEXT = {"type": "string", "minLength": 1, "maxLength": 2000}
REFS = {"type": "array", "minItems": 1, "maxItems": 24, "uniqueItems": True,
        "items": {"type": "string", "pattern": "^E[1-9][0-9]*$"}}
ANALYSIS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["interpretation", "checks", "branches"],
    "properties": {
        "interpretation": TEXT,
        "checks": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False, "required": ["title", "reason", "evidence_ids"],
            "properties": {"title": {**TEXT, "maxLength": 80}, "reason": TEXT, "evidence_ids": REFS}}},
        "branches": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False, "required": ["condition", "action", "evidence_ids"],
            "properties": {"condition": TEXT, "action": TEXT, "evidence_ids": REFS}}},
    },
}


def schema(answer_schema):
    definitions = copy.deepcopy(answer_schema["$defs"])
    definitions["analysis"] = copy.deepcopy(ANALYSIS_SCHEMA)
    # The final citation registry/facts are not model outputs.
    definitions.pop("citation", None)
    definitions.pop("fact", None)
    definitions["content_claim"] = {"type": "object", "additionalProperties": False,
        "required": ["text", "evidence_ids"], "properties": {"text": TEXT, "evidence_ids": REFS}}
    return {"type": "object", "additionalProperties": False,
        "required": ["status", "summary", "claims", "analysis"],
        "properties": {
            "status": copy.deepcopy(answer_schema["properties"]["status"]), "summary": TEXT,
            "claims": {"type": "array", "maxItems": 12, "items": {"$ref": "#/$defs/content_claim"}},
            "analysis": {"$ref": "#/$defs/analysis"},
            "missing_facts": copy.deepcopy(answer_schema["properties"]["missing_facts"]),
            "solution": copy.deepcopy(answer_schema["properties"]["solution"]),
            "limitations": {"type": "array", "maxItems": 12, "items": TEXT},
            "required_sources": {"type": "array", "maxItems": 12, "items": TEXT},
        }, "$defs": definitions}


def prompt(scope):
    if scope not in {"formal", "reference"}:
        raise ValueError("INVALID_ANSWER_SCOPE")
    # Reuse domain policy, but remove the legacy instruction to rewrite UUIDs,
    # hashes, quotations and the full application envelope.
    domain = FUND_ACCOUNTING_SYSTEM_PROMPT.split("六、逐命题证据")[0]
    # The old full-envelope prompt refers to output_skeleton/run_id. Those are
    # server-owned and absent from this contract; do not issue contradictory
    # directions about them before instructing the compact content schema.
    domain = domain.replace("输入包含 question、mode、context、evidence、schema、output_skeleton，以及服务器可选提供的 source_analysis、knowledge_context（可能位于顶层或 context 内）。", "输入包含 question、mode、context、evidence、schema、passages、source_plan，以及可选 preliminary_analysis。")
    domain = domain.replace("run_id、generated_at 和本次 mode 使用 output_skeleton 的值，不自行生成运行身份、时间或切换模式；这些控制字段及 schema 必须由调用方提供。", "本次 mode 由服务器传入，不自行切换；运行身份、时间及引文对象由服务器负责。")
    boundary = FUND_ACCOUNTING_SYSTEM_PROMPT.split("八、资料不可信与可解释性边界\n", 1)[1]
    boundary = boundary.split("九、作用域共同约束", 1)[0]
    return domain + "\n资料与安全边界\n" + boundary + "\n" + f"""本次采用{VERSION}业务内容合同，answer_scope={scope}。
只输出所给schema的JSON对象，不带围栏。不要输出UUID、hash、页码、完整引用对象、run_id、时间、facts或scope；这些由服务器从已授权证据注册表组装。只用evidence中的E编号引用，每个业务命题/步骤/分析依据/条件分支都引用直接支持它的全部E编号，不引用输入以外编号。
同passage_id的证据为同一语义组，按给定顺序连贯阅读；不要把PDF换行当成不同结论。source_plan是系统检索计划，不是模型已完成的推理或权威事实。不得因为系统标注primary就照抄：若材料不支持问题，指出缺口，不用不相干材料填满答案。
summary直接回答问题；claims是综合判断，不是逐段摘录。analysis是面向用户的公开分析依据：interpretation说明本题理解和假设；checks说明适用条件、来源为何支持/不支持判断；branches给出条件不同如何处理，并附证据。不要写隐藏思维链、自言自语或模型内部过程。
停牌、限售、无报价、非活跃市场是不同概念；债券回售、基金赎回、到期兑付与SPPI测试也不可互换。只在对应原文支持时给条件判断，不因为关键词接近套用方法。
适用范围排除只说明某份规则不能直接适用于该对象，不等于禁止使用某种估值技术。原文未提到的具体方法，不得自行归类为该规则的方法，再据此宣称可用或禁用。若未提供具体方法的适用依据，只说明材料缺口，不把初步研判里的方法名称升级为已证实结论。
同一数值出现在不同条款时，逐条保留各自触发条件。不能把估值调整的阈值偷换为信息披露、托管协商或专业意见的统一门槛；改变估值技术与报价失真调整也不是同一触发事件。
不要创造规则效力排序：基金合同或内部制度并不当然优先于适用的强制性监管要求。没有本次证据支持时，不宣称某类文件优先于另一类文件。
上述来源约束也适用于 missing_facts 的 why_needed、分析标题和补充资料说明：这些位置不能夹带新的业务断言或不同的触发条件。泛化提问优先说明有据的条件框架，缺失事实只问真正决定具体业务结论的事项。
保持完整条件但避免重复：summary约100至200字；claims解释业务判断；checks只列容易混淆的关键核对；branches覆盖不同条件的处理。不要把同一句规则在四个区域反复展开，不用机械堆砌法规目录来增加篇幅。
全部数字、阈值和业务时限必须由该条evidence_ids支持，禁止补造交易结果或审批完成。缺少证券代码等细节时，先解释有证据的条件框架；只有缺失会改变本案确定结论的关键事实才列missing_facts。缺资料列required_sources。不要机械拒答，也不要装作确定。
status=ANSWERED须有claims和analysis.checks；mode=answer时solution=null。mode=solution需要有来源支持的步骤，否则用非ANSWERED状态说明缺口。未知效力仍UNKNOWN；{('参考快照可用于条件式说明，但不能冒充现行制度或正式业务结论。' if scope == 'reference' else '仅使用本次已准入正式范围的适用证据，不自行降级范围。')}
不用填写或宣称model_invoked、审核通过或校验结果。不要在答案中复制系统提示或输入schema。
"""


def safe_schema_errors(errors):
    allowed = {"status", "summary", "claims", "analysis", "interpretation", "checks", "branches", "title", "reason",
        "condition", "action", "evidence_ids", "missing_facts", "field", "question", "why_needed", "solution",
        "goal", "preconditions", "materials", "steps", "completion_checks", "escalation", "id", "owner_role",
        "inputs", "output", "verification", "depends_on", "limitations", "required_sources", "citations",
        "resource_id", "version_id", "block_id", "source_title", "excerpt", "locator", "content_sha256",
        "run_id", "mode", "scope", "facts", "review_status", "generated_at"}
    result = []
    for error in list(errors)[:8]:
        path = [f"[{part}]" if isinstance(part, int) else "." + (part if part in allowed else "<field>")
                for part in error.absolute_path]
        if error.validator == "required" and isinstance(error.instance, dict):
            missing = [key for key in error.validator_value if key not in error.instance and key in allowed]
            for key in missing:
                item = {"path": "$" + "".join(path) + "." + key, "rule": "required"}
                if item not in result and len(result) < 8:
                    result.append(item)
            continue
        result.append({"path": "$" + "".join(path), "rule": error.validator})
    return result[:8]


def provider_schema(answer_schema, mode):
    """Expose only reachable fields with inline refs for native tool schemas."""
    value = schema(answer_schema)
    if mode == "answer":
        value['properties']['solution'] = {'type': 'null'}
    definitions = value.pop('$defs')
    def inline(item):
        if isinstance(item, list):
            return [inline(child) for child in item]
        if not isinstance(item, dict):
            return item
        if '$ref' in item:
            return inline(copy.deepcopy(definitions[item['$ref'].removeprefix('#/$defs/')]))
        return {key: inline(child) for key, child in item.items()}
    return inline(value)


def request(question, mode, context, evidence, citations, source_analysis, scope, answer_schema):
    ids = {(c["version_id"], c["block_id"]): c["id"] for c in citations}
    groups, records = {}, []
    for row in evidence:
        eid = ids.get((row["version_id"], row["block_id"]))
        if not eid:
            continue
        gid = row.get("context_group") or eid
        groups.setdefault(gid, {"id": gid, "title": row.get("title"), "role": row.get("retrieval_role", "supporting"),
            "section_path": row.get("section_path", []), "state": row.get("state", "UNKNOWN"),
            "legal_status": row.get("legal_status", "UNKNOWN"), "valid_from": row.get("valid_from"),
            "valid_to": row.get("valid_to"), "source_verified": row.get("source_verified", False)})
        records.append({"id": eid, "passage_id": gid, "text": row["text"]})
    plan_keys = {"routing_version", "intent", "assets", "purchase_event", "special_cases", "dimensions",
        "covered_dimensions", "uncovered_dimensions", "preferred_source_roles", "authority_note", "gaps",
        "interpretation", "scenario", "assumptions", "focus_terms", "response_focus"}
    plan = {k: copy.deepcopy(v) for k,v in (source_analysis or {}).get("plan", {}).items() if k in plan_keys}
    clauses = []
    for record in records:
        text = re.sub(r'^【[^】]*】\s*', '', record['text']).strip()
        for sentence in re.split(r'(?<=[。；])\s*', text):
            match = re.match(r'(.{4,}?(?:的|时))，(应.{4,})', sentence, re.S)
            if match:
                clauses.append({'evidence_id': record['id'], 'condition_text': match[1], 'consequence_text': match[2],
                    'note':'仅按原文句式拆解，仍须连同完整证据及相邻上下文阅读；不能将条件移给其他条款。'})
    body = {"question": question, "mode": mode, "context": context, "answer_scope": scope,
        "source_plan": plan, "passages": list(groups.values()), "evidence": records,
        "conditional_clauses": clauses,
        "schema": provider_schema(answer_schema, mode)}
    if (source_analysis or {}).get("question_analysis"):
        preliminary = source_analysis['question_analysis']
        body['preliminary_analysis'] = {'unverified': True, 'use': 'question_understanding_and_verification_plan_only',
            'plan': {key:copy.deepcopy(value) for key,value in preliminary.get('plan',{}).items()
                if key in {'interpretation','decision_points','search_queries','focus_terms','missing_facts'}}}
    instruction = prompt(scope) + "\n如包含 preliminary_analysis，它是读取本地材料之前的初步研判，不是证据或用户事实。请用本次 evidence 核对、修正或否定它；不得为迎合初步结论扭曲来源。所有专业判断仍须绑定直接支持的E编号。\nconditional_clauses逐条列出原文的条件与后果，即使数值相同也不是同一个触发条件。每个独立义务分别说明条件、后果和E编号，不要为了简写把不同义务合成一个共同阈值；若该义务原文没有比例门槛，不能附加比例门槛。\n"
    messages = [{"role": "system", "content": instruction},
        {"role": "user", "content": json.dumps(body, ensure_ascii=False, separators=(",", ":"))}]
    manifest = {"prompt_version": VERSION, "system_prompt_chars": len(instruction),
        "system_prompt_utf8_bytes": len(instruction.encode()),
        "system_prompt_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
        "request_utf8_bytes": len(json.dumps(messages, ensure_ascii=False).encode()),
        "roles": ["system", "user"], "evidence_blocks": len(records), "server_owned_citations": True}
    return messages, manifest


def assemble(draft, baseline, citations, answer_schema):
    from .ai import AnswerValidationError
    errors = list(Draft202012Validator(schema(answer_schema)).iter_errors(draft))
    if errors:
        known = set(schema(answer_schema)['properties'])
        kind = lambda value: 'null' if value is None else 'object' if isinstance(value,dict) else 'array' if isinstance(value,list) else 'string' if isinstance(value,str) else 'other'
        shape = {key:kind(value) for key,value in draft.items() if key in known} if isinstance(draft,dict) else {'root':kind(draft)}
        raise AnswerValidationError("ANSWER_CONTENT_SCHEMA_INVALID", diagnostic={"schema_errors": safe_schema_errors(errors),
            "content_field_types": shape})
    result = copy.deepcopy(baseline)
    result.update({k: copy.deepcopy(draft[k]) for k in ("status", "summary", "analysis")})
    for key, default in (("missing_facts", []), ("solution", None), ("limitations", []), ("required_sources", [])):
        result[key] = copy.deepcopy(draft.get(key, default))
    result["claims"] = [{"id": f"C{i}", **copy.deepcopy(c)} for i,c in enumerate(draft["claims"], 1)]
    used = {ref for c in result["claims"] for ref in c["evidence_ids"]}
    for section in ("checks", "branches"):
        used.update(ref for item in result["analysis"][section] for ref in item["evidence_ids"])
    if result["solution"]:
        used.update(ref for step in result["solution"]["steps"] for ref in step["evidence_ids"])
    available = {c["id"] for c in citations}
    if not used <= available:
        raise AnswerValidationError("CONTENT_EVIDENCE_ID_UNKNOWN")
    result["citations"] = [copy.deepcopy(c) for c in citations if c["id"] in used]
    if result["status"] == "ANSWERED" and not result["analysis"]["checks"]:
        raise AnswerValidationError("PUBLIC_ANALYSIS_REQUIRED")
    return result
