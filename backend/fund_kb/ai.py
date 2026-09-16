"""Evidence extraction and bounded model adapters, not an assertion of business accuracy.

The worker MUST load current authorized and applicable source blocks before calling
these functions, and re-check permissions before delivering a run. Formal answers
use published evidence; reference markers require the caller's explicit ACL, scan,
hash and frozen-source checks and never promote source verification or publication.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, FormatChecker

from .ai_transport import ProviderError, post_json
from .ingestion import block_text, text_sha256
from .retrieval import lexical_scores

KNOWLEDGE_TYPES = {"source", "faq", "rule", "sop", "scenario", "case", "term", "solution_template"}
_REFERENCE_NOTICE = "资料辅助答疑：包含未核验/未发布资料，仅供参考，不代表现行制度或正式业务结论。"
_MODEL_FALLBACK_MARKERS = ("未调用生成模型", "MODEL_NOT_CONFIGURED", "已降级为证据提取")
_REFERENCE_REQUEST_BUDGET_BYTES = 60000
_UNSAFE = re.compile(r"<\s*/?\s*[a-z][a-z0-9]*\b|!\[[^\]]*\]\s*\(|"
                     r"(?:javascript|vbscript)\s*:|data\s*:\s*text/html|\bon\w+\s*=", re.IGNORECASE)
_FINANCIAL_LESS_THAN = re.compile(
    r'(?<![A-Za-z0-9_])(?:[A-Z][A-Za-z0-9_{}]{0,30}|\d+(?:\.\d+)?)\s*<\s*'
    r'[A-Z](?:_[A-Za-z0-9_{}]+|[0-9][A-Za-z0-9_]*)?(?=\s*(?:[\u3400-\u9fff，,；;。:：)）\]`$]|$))')


class AnswerValidationError(ValueError):
    def __init__(self, code, *, diagnostic=None):
        self.code = code
        self.diagnostic = diagnostic
        super().__init__(code)


@lru_cache(maxsize=1)
def answer_validator():
    schema_path = Path(__file__).resolve().parents[2] / "contracts" / "answer.schema.json"
    with schema_path.open(encoding="utf-8") as source:
        schema = json.load(source)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _unsafe_text(value):
    # Encoded markup is also rejected before a later Markdown/HTML renderer can
    # decode it; normal financial comparisons such as "NAV < 1" remain text.
    for _ in range(3):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    value = unicodedata.normalize('NFKC', value)
    # A narrowly recognized uppercase financial comparison is not a tag.
    # This view is ONLY for validation; the actual answer remains unchanged.
    # Attributes/closing brackets and multi-letter HTML tag names do not match.
    check = _FINANCIAL_LESS_THAN.sub(lambda match: match[0].replace('<', ' LESS_THAN '), value)
    return bool(_UNSAFE.search(check))


def _locator(record: dict) -> dict:
    original = record.get("locator") or {}
    # Answer v2 schema only admits these fields. Full locator stays on the block,
    # addressed by immutable version_id + block_id, including PDF/OOXML coordinates.
    return {"label": str(original.get("label") or "内容块 " + str(record["block_id"])),
            **{key: original[key] for key in ("source_page", "sheet", "cell") if key in original}}


def _check_answer_scope(answer_scope):
    if answer_scope not in ("formal", "reference"):
        raise ValueError("INVALID_ANSWER_SCOPE")


def _apply_answer_scope(answer, answer_scope):
    _check_answer_scope(answer_scope)
    if answer_scope == "reference":
        if not isinstance(answer.get("limitations"), list):
            raise AnswerValidationError("ANSWER_SCHEMA_INVALID")
        answer["review_status"] = "REQUIRES_EXPERT"
        answer["limitations"] = [_REFERENCE_NOTICE, *(
            item for item in answer["limitations"] if item != _REFERENCE_NOTICE)]
    return answer


def _usable_records(evidence, *, answer_scope="formal"):
    _check_answer_scope(answer_scope)
    records = []
    seen = set()
    for record in evidence:
        try:
            for key in ("resource_id", "version_id", "block_id"):
                UUID(str(record[key]))
            text = str(record.get("text", block_text(record)))
            if not text.strip() or record.get("content_sha256") != text_sha256(text):
                continue
            reference_record = record.get("evidence_scope") == "reference"
            if reference_record and answer_scope != "reference":
                continue
            is_source = (record.get("knowledge_type", "source") == "source"
                         or record.get("kind") == "document" or record.get("source_kind") == "document")
            if (is_source and record.get("source_verified") is False
                    and not (answer_scope == "reference" and reference_record)):
                continue
            if _unsafe_text(text) or _SECRETS.search(text):
                continue
            key = (record["version_id"], record["block_id"])
            if key not in seen:
                records.append({**record, "text": text})
                seen.add(key)
        except (ValueError, KeyError, TypeError):
            continue
    return records


_QUANTITY = re.compile(r"(?<![A-Za-z0-9.])(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*"
                       r"(%|bps|bp|基点|亿元|万元|元|个工作日|工作日|小时|分钟|天|日|年|份|倍)?", re.IGNORECASE)
_BUSINESS_UNIT = r"%|bps|bp|基点|亿元|万元|元|个工作日|工作日|小时|分钟|天|日|年|月|份|倍"
_STANDARD_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_])(?:IFRS|IAS|CAS|ASBE)\s*[-–]?\s*\d{1,3}(?!\d)"
    r"(?!\s*(?:" + _BUSINESS_UNIT + r"))", re.IGNORECASE)
_UNIT_QUANTITY = re.compile(r"(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*(" + _BUSINESS_UNIT + r")", re.IGNORECASE)
_DATES = re.compile(r"(?<!\d)(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?(?!\d)")
_SECRETS = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}|"
                      r"\b(?:api[_ -]?key|password|token)\s*[:=]\s*[^\s]{8,}", re.IGNORECASE)
_COMPLETED = re.compile(
    r"(?:已经|业已|确已|现已|已)(?:(?:成功|全部|实际|正式|确实)|(?:为|替|帮)[^，,。；;\n]{1,16}?)?"
    r"(?:完成|取得|获得|获批|获|批准|确认|执行|付款|支付|过账|入账|到账|发布|审批|审核|复核|核对|核验|授权|通过|记录)"
    r"|(?:付款|支付|过账|入账|审批|审核|复核|发布)(?:已经|已)?(?:完成|成功|通过)"
    r"|(?:完成|执行|支付|付款|过账|批准|确认)了"
    r"|(?:并非|不是|不能说)(?:没有|未曾|尚未|未|没)(?:完成|执行|支付|付款|过账|批准|确认)")
_COMPLETION_CLAUSE = re.compile(
    r"([，,。；;\n！？!?]|但是|然而|不过|(?<!不)但(?!书)|却|随后|然后|接着|因此|所以|那么|则|"
    r"而且|并且|以及|或者|且|或(?!许)|并(?=已|实际|确实|我|本助手))")
_COMPLETION_NEGATIVE = re.compile(
    r"不(?:表示|代表|意味着|等同于|等于|构成|属于|认为)|(?:不能|无法|不可|不得|不应|不足以)(?:够)?"
    r"(?:直接)?(?:被)?(?:作为|当作|当成|算作|视为|认定为?|证明|确认|证实|说明|表明)|"
    r"(?:没有|尚未|并未|未曾|未)(?:表示|声称|宣称|确认|证明|证实)|并非|不是")
_COMPLETION_DOUBLE_NEGATIVE = re.compile(
    r"(?:不是|并非|没有|不能说|不能认为)[^，,。；;\n]{0,12}(?:不能|不|未|没有|无法|并非)|"
    r"(?:不能|无法|不)否认|(?:不能|不得|不可)不(?:承认|认为|认定|表示)|无不|"
    r"难道[^，,。；;\n]{0,12}(?:不是|没有|未|不)")
_COMPLETION_REQUIREMENT = re.compile(
    r"(?:需要|必须|应当|务必|建议|计划|准备|等待|需|须|应|请|拟|待)(?:先|再|进一步|逐项)?"
    r"(?:核对|确认|检查|复核|核验|验证|确保|查明|证明|取得|提供)|"
    r"(?:^|[:：])\s*(?:先|再)?(?:核对|检查|复核|核验|验证|确认)(?!结果)|"
    r"(?:必须|须|需|要求)(?:先|事先)?$")
_COMPLETION_QUESTION = re.compile(r"是否|有没有|有无|(?:能否|可否)(?:确认|证明|证实)?")
_COMPLETION_CONDITION = re.compile(
    r"(?:^|[:：(（])\s*(?:如果|假如|假设|倘若|一旦|只要|除非|仅当|若|当(?!前|时|下|地|然)|如(?=已))|"
    r"(?:前提|条件)(?:是|为|包括|要求)?[:：]?$")
_COMPLETION_REPORT = re.compile(
    r"事实上|实际上|(?:实际|确实)(?=已经|已|完成|付款|支付|过账|批准)|确已|现已|"
    r"现状[:：]|结果表明|结果是|经核实|经确认|不仅|不但|难道")
_COMPLETION_SELF = re.compile(r"本助手|本模型|本系统|我(?:们|方)?")
_COMPLETION_ROLES = {"assertion", "condition", "requirement"}
_LIST_ORDINAL = re.compile(
    r"(?m)^[ \t]*\d{1,3}[.)、][ \t]+"
    r"(?!(?:%|bps?\b|基点|亿元|万元|元|个工作日|工作日|小时|分钟|天|日|年|份|倍))(?=\S)", re.IGNORECASE)


def _quantities(text):
    text = unicodedata.normalize("NFKC", text)
    # A line-leading list ordinal is formatting, not a source-backed amount.
    # Keep the list body (including any actual amounts/rates) under the same checks.
    text = _LIST_ORDINAL.sub("", text)
    try:
        values = {("date", date(*(int(part) for part in match)).isoformat()) for match in _DATES.findall(text)}
    except ValueError:
        raise AnswerValidationError("INVALID_DATE") from None
    text = _DATES.sub("", text)
    # Standard identifiers are names, not financial amounts: CAS 22 and CAS22
    # must not behave differently merely because a provider inserted a space.
    # This does NOT validate the named standard or give it regulatory authority;
    # the unchanged source/citation and professional review boundary still applies.
    # A business unit after the digits explicitly prevents identifier masking.
    text = _STANDARD_IDENTIFIER.sub("STANDARD_IDENTIFIER", text)
    units = {"亿元": (Decimal(100000000), "元"), "万元": (Decimal(10000), "元"),
             "bp": (Decimal("0.0001"), "ratio"), "bps": (Decimal("0.0001"), "ratio"),
             "基点": (Decimal("0.0001"), "ratio"), "%": (Decimal("0.01"), "ratio"),
             "个工作日": (Decimal(1), "工作日")}
    # Unit-bearing amounts also match next to a label (e.g. NAV22元), so a
    # reference-looking prefix cannot conceal an unsupported price or duration.
    for match in [*_QUANTITY.finditer(text), *_UNIT_QUANTITY.finditer(text)]:
        number, unit = match.groups()
        factor, canonical = units.get((unit or "").lower(), (Decimal(1), unit or "number"))
        try:
            values.add((canonical, Decimal(number.replace(",", "")) * factor))
        except InvalidOperation:
            raise AnswerValidationError("NUMERIC_VALUE_INVALID") from None
    return values


def _completion_view(text):
    # Interpretation only: never rewrite the delivered text or its evidence.
    return re.sub(r"[*_`]+", "", unicodedata.normalize("NFKC", text))


def _completion_statements(text, role="assertion"):
    """Small-clause speech acts, not a professional semantic/entailment check.

    A typed condition/check establishes an unverified requirement, NOT blanket
    permission for later sentences. Only explicit conjunctions can carry that
    role forward; contrasts, consequences and sentence boundaries reset it.
    Negation never licenses a later affirmative conjunct. Unknown forms remain
    assertions and require support, rather than guessing an exemption.
    """
    parts = _COMPLETION_CLAUSE.split(_completion_view(text))
    previous, speaker, first = None, False, True
    for index in range(0, len(parts), 2):
        clause = parts[index].strip()
        if not clause:
            continue
        separator = parts[index - 1] if index else ""
        if separator in {"。", ";", "；", "\n", "!", "?", "！", "？"}:
            speaker = False
        inherited = role if first else previous if separator in {
            "且", "并且", "以及", "或", "或者", "并"} and previous in {"condition", "requirement"} else "assertion"
        first = False
        matches = list(_COMPLETED.finditer(clause))
        # An earlier first-person clause can omit a completion marker entirely
        # ("本助手需要核对...，但已付款"). Keep its subject for an elided consequent.
        speaker |= bool(_COMPLETION_SELF.search(clause[:matches[0].start()] if matches else clause))
        previous = inherited
        for number, match in enumerate(matches):
            start = matches[number - 1].end() if number else 0
            prefix = clause[start:match.start()].strip()
            tail_end = matches[number + 1].start() if number + 1 < len(matches) else len(clause)
            assertion = clause[match.start():tail_end].strip().rstrip("。.;；!?！？")[:35]
            # Prefix-based state is local to this occurrence, not text anywhere
            # earlier in the answer. A double negative is never a disclaimer.
            negative = _COMPLETION_NEGATIVE.search(prefix)
            double = bool(_COMPLETION_DOUBLE_NEGATIVE.search(prefix + match[0]))
            denied = bool(negative and not double and len(prefix[negative.end():]) <= 40
                          and not re.search(r"不|未|没有|实际|确实", prefix[negative.end():]))
            question = bool(_COMPLETION_QUESTION.search(prefix))
            required = bool(_COMPLETION_REQUIREMENT.search(prefix))
            conditional = bool(_COMPLETION_CONDITION.search(prefix))
            # Temporal prerequisites need a normative consequence. "已付款后
            # 已过账" by itself is a report, not an unverified condition.
            conditional |= bool(re.search(r"(?:前|后|时)(?:方可|才可|才能|再|应|需|须)", clause[match.end():]))
            own = bool(_COMPLETION_SELF.search(prefix) or re.search(r"(?:已|已经)(?:为|替|帮)[你您]", match[0])
                       or (speaker and re.match(r"^(?:已经|业已|现已|确已|已)", clause)))
            speaker |= bool(_COMPLETION_SELF.search(prefix))
            reporting = bool(_COMPLETION_REPORT.search(prefix + match[0]))
            if denied:
                act = "negated"
            elif double:
                act = "assertion"
            elif (question or required) and not reporting:
                act = "requirement"
            elif own or reporting:
                act = "assertion"
            elif conditional:
                act = "condition"
            else:
                act = inherited if number == 0 else "assertion"
            yield assertion, prefix, act, own
            previous = act


def _check_completion(text, support, role):
    if role not in _COMPLETION_ROLES:
        raise ValueError("INVALID_COMPLETION_ROLE")
    supported = None
    for assertion, prefix, act, own in _completion_statements(text, role):
        if act != "assertion":
            continue
        if supported is None:
            # A source saying "must verify X" or "not X" is not proof of X.
            supported = [item for item, _, speech_act, _ in _completion_statements(support)
                         if speech_act == "assertion"]
        if own or not any(assertion in item for item in supported):
            raise AnswerValidationError("EXECUTION_OR_APPROVAL_UNVERIFIED", diagnostic={
                "assertion": assertion[:120], "prefix": prefix[-120:], "text_preview": text[:180]})


def _check_grounded_text(text, support, *, numeric=True, source_titles=(), completion=True, completion_role=None):
    if _SECRETS.search(text):
        raise AnswerValidationError("SENSITIVE_MODEL_OUTPUT")
    numeric_text = text
    for title in source_titles:
        for caption in re.findall(r'\d{4}年(?:\d{1,2}月)?(?:快照|修订版?|版)', str(title)):
            # Only the exact bibliographic phrase supplied in the bound title
            # is ignored as an amount. Never whitelist its year as a financial
            # duration or its month number as a price, rate or other quantity.
            numeric_text = numeric_text.replace(caption, '资料版本')
    if numeric and not _quantities(numeric_text).issubset(_quantities(support)):
        # New amounts/units/calculations require a registered deterministic result,
        # supplied by the calling worker as a verified fact, not model arithmetic.
        raise AnswerValidationError("NUMERIC_UNIT_SUPPORT_MISSING", diagnostic={
            "unsupported_quantities": sorted([f"{value} {unit}" for unit, value in _quantities(numeric_text) - _quantities(support)])[:12],
            "text_preview": text[:180]})
    # completion=False remains compatible with older condition callers, but no
    # longer disables the guard for the rest of a model-controlled field.
    _check_completion(text, support, completion_role or ("assertion" if completion else "condition"))


def _grounded_support(records, context):
    pieces = []
    for record in records:
        pieces.append(record["text"])
        pieces.extend(str(record[key]) for key in ("valid_from", "valid_to") if record.get(key))
    pieces.extend(str(v) for v in context.values() if isinstance(v, (str, int, float)))
    return "\n".join(pieces)


_NAMED_VALUATION_METHODS = re.compile(r'指数收益法|可比公司法|市场价格模型法|现金流量折现法|现金流折现法|AAP模型|H模型', re.I)


def _check_named_method_support(text, source_text):
    """A scope exclusion cannot invent permission/prohibition of a named method.

    This is a narrow deterministic grounding guard, not a semantic proof. A
    method supplied only in a user's context or preliminary plan is not source
    support. Materials/limitations may identify missing methodology separately.
    """
    source = re.sub(r'\s+', '', source_text).casefold()
    for clause in re.split(r'[。；\n]', text):
        for method in _NAMED_VALUATION_METHODS.findall(clause):
            if method.casefold() in source:
                continue
            gap = re.search(r'未(?:提供|包含|涉及|给出|涵盖)|缺少|缺乏|证据不足|材料不足|无法确认|不能确认|无法判断', clause)
            directive = re.search(r'必须|不应(?:直接)?(?:采用|使用|套用)|不宜(?:采用|使用)|禁止(?:采用|使用)|不得(?:采用|使用|套用)|不能(?:直接)?(?:采用|使用|套用)|应(?:当)?(?:采用|使用|选用|改用)|可以(?:采用|使用)', clause)
            inference_limit = re.search(r'不(?:能|应)(?:仅)?(?:据此|由此).{0,24}(?:推导|推断|推出|认定)|不(?:意味着|等于|代表).{0,35}(?:禁止|禁用)|不应.{0,16}(?:推断|认定)|不得.{0,16}(?:推论|认定)', clause)
            positive_advice = re.search(r'(?:但|然而|因此|所以).{0,12}(?:应(?:当)?采用|应使用|可以采用|必须使用)', clause)
            if inference_limit and not positive_advice:
                continue  # "This exclusion cannot prove method X prohibited" is not a prohibition.
            if gap and re.search(r'(?:不应|不能|不得).{0,8}直接', clause) and not positive_advice:
                continue  # Do not directly choose an unevidenced method until it is verified.
            if not directive:
                continue  # Naming a method or unresolved applicability alone is not a pricing instruction.
            raise AnswerValidationError('NAMED_METHOD_SUPPORT_MISSING', diagnostic={'method_guard': 'unsupported_method_assertion',
                'method': method, 'text_preview': clause[:300]})


def validate_answer(answer: dict, evidence: list[dict], mode: str = "extractive",
                    context: dict | None = None, *, answer_scope: str = "formal") -> None:
    """Validate structure/provenance, not professional semantic correctness.

    extractive requires verbatim claims and registered steps. grounded permits
    evidence-based explanation and plan composition; numeric/context guardrails
    are deterministic, while entailment and professional decisions need review.
    answer_scope must match generation: reference admits caller-marked reference
    material and requires the server-owned notice and expert-review status. It
    does not replace the caller's authorization or frozen-source checks.
    """
    _check_answer_scope(answer_scope)
    if mode not in {"extractive", "grounded"}:
        raise ValueError("INVALID_VALIDATION_MODE")
    context = context or {}
    if answer.get("format") == "wiki_markdown":
        raise AnswerValidationError("SERVER_OWNED_NARRATIVE_FORMAT")
    errors = list(answer_validator().iter_errors(answer))
    if errors:
        from .answer_content import safe_schema_errors
        raise AnswerValidationError("ANSWER_SCHEMA_INVALID", diagnostic={"schema_errors": safe_schema_errors(errors)})
    if answer_scope == "reference" and (answer["review_status"] != "REQUIRES_EXPERT"
            or not answer["limitations"] or answer["limitations"][0] != _REFERENCE_NOTICE):
        raise AnswerValidationError("REFERENCE_SAFEGUARDS_REQUIRED")
    if any(_unsafe_text(s) for s in _strings(answer)):
        raise AnswerValidationError("UNSAFE_MODEL_OUTPUT")
    records = {(r["version_id"], r["block_id"]): r
               for r in _usable_records(evidence, answer_scope=answer_scope)}
    citations = {}
    for citation in answer["citations"]:
        if citation["id"] in citations:
            raise AnswerValidationError("DUPLICATE_CITATION_ID")
        record = records.get((citation["version_id"], citation["block_id"]))
        if (not record or citation["resource_id"] != record["resource_id"]
                or citation["content_sha256"] != record["content_sha256"]
                or citation["excerpt"] not in record["text"]
                or citation["source_title"] != (record.get("title") or "未命名资料")
                or citation["locator"] != _locator(record)):
            raise AnswerValidationError("CITATION_PROVENANCE_INVALID")
        citations[citation["id"]] = record
    claim_ids = set()
    for claim in answer["claims"]:
        if claim["id"] in claim_ids:
            raise AnswerValidationError("DUPLICATE_CLAIM_ID")
        claim_ids.add(claim["id"])
        refs = claim["evidence_ids"]
        if any(ref not in citations for ref in refs):
            raise AnswerValidationError("CLAIM_CITATION_MISSING")
        if mode == "extractive" and not any(claim["text"] in citations[ref]["text"] for ref in refs):
            raise AnswerValidationError("CLAIM_SUPPORT_UNVERIFIED")
        if mode == "grounded":
            support = _grounded_support([citations[ref] for ref in refs], context)
            _check_grounded_text(claim["text"], support, source_titles=[citations[ref].get('title','') for ref in refs])
            _check_named_method_support(claim['text'], '\n'.join(citations[ref]['text'] for ref in refs))
    solution = answer["solution"]
    if solution is not None:
        steps = solution["steps"]
        ids = [s["id"] for s in steps]
        if len(ids) != len(set(ids)):
            raise AnswerValidationError("DUPLICATE_STEP_ID")
        previous = set()
        extractive_sources = []
        for step in steps:
            if not set(step["depends_on"]).issubset(previous):
                raise AnswerValidationError("STEP_DEPENDENCY_INVALID")
            previous.add(step["id"])
            refs = step["evidence_ids"]
            if any(ref not in citations for ref in refs):
                raise AnswerValidationError("STEP_CITATION_MISSING")
            sources = [citations[ref] for ref in refs if citations[ref].get("block_type") == "step"]
            if mode == "extractive" and not any(all(step[k] == (source.get("data") or {}).get(k)
                           for k in ("action", "owner_role", "output", "verification")) for source in sources):
                raise AnswerValidationError("STEP_NOT_REGISTERED")
            if mode == "extractive":
                matched = [source for source in sources if _registered_sop_step(source) and all(
                    step[k] == source["data"][k] for k in ("action", "owner_role", "output", "verification"))]
                if len({(s["version_id"], s["block_id"]) for s in matched}) != 1:
                    raise AnswerValidationError("STEP_NOT_FROM_SINGLE_SOP")
                extractive_sources.append(matched[0])
            if mode == "grounded":
                support = _grounded_support([citations[ref] for ref in refs], context)
                for key in ("action", "owner_role", "output", "verification", "inputs"):
                    for text in _strings(step[key]):
                        _check_grounded_text(text, support,
                            completion_role="requirement" if key in {"verification", "output", "inputs"} else "assertion")
        if mode == "extractive":
            if len({(s["resource_id"], s["version_id"]) for s in extractive_sources}) != 1:
                raise AnswerValidationError("EXTRACTIVE_SOP_MIXED_VERSIONS")
            ordered = _order_sop_steps(extractive_sources)
            if ordered is None or [s["block_id"] for s in ordered] != [s["block_id"] for s in extractive_sources]:
                raise AnswerValidationError("EXTRACTIVE_SOP_ORDER_INVALID")
            complete, _ = _sop_coverage(extractive_sources, extractive_sources)
            if answer["status"] == "ANSWERED" and complete is not True:
                raise AnswerValidationError("EXTRACTIVE_SOP_INCOMPLETE")
    if mode == "grounded":
        support = _grounded_support(list(citations.values()), context)
        source_titles = [record.get('title','') for record in citations.values()]
        _check_named_method_support(answer['summary'], '\n'.join(record['text'] for record in citations.values()))
        if analysis := answer.get("analysis"):
            _check_grounded_text(analysis["interpretation"], support, source_titles=source_titles)
            for item in [*analysis["checks"], *analysis["branches"]]:
                if any(ref not in citations for ref in item["evidence_ids"]):
                    raise AnswerValidationError("ANALYSIS_CITATION_MISSING")
                basis = _grounded_support([citations[ref] for ref in item["evidence_ids"]], context)
                for key in ("title", "reason", "condition", "action"):
                    if key in item:
                        _check_grounded_text(item[key], basis, source_titles=[citations[ref].get('title','') for ref in item['evidence_ids']],
                            completion_role="condition" if key == "condition" else "assertion")
                        _check_named_method_support(item[key], '\n'.join(citations[ref]['text'] for ref in item['evidence_ids']))
        for text in _strings({k: answer[k] for k in ("summary", "scope", "limitations")}):
            _check_grounded_text(text, support, source_titles=source_titles)
        if solution:
            for branch in solution['branches']:
                _check_grounded_text(branch['condition'], support, source_titles=source_titles, completion_role="condition")
                _check_grounded_text(branch['action'], support, source_titles=source_titles)
            for key in ("goal", "preconditions", "materials", "completion_checks", "escalation"):
                for text in _strings(solution[key]):
                    _check_grounded_text(text, support, source_titles=source_titles,
                        completion_role="requirement" if key in {"preconditions", "materials", "completion_checks"} else "assertion")
        for fact in answer["facts"]:
            if fact["origin"] == "USER":
                if fact["name"] not in context or fact["value"] != context[fact["name"]]:
                    raise AnswerValidationError("KNOWN_FACT_CHANGED")
                if fact["certainty"] == "VERIFIED":
                    raise AnswerValidationError("USER_FACT_VERIFICATION_INVENTED")
            elif str(fact["value"]) not in support:
                raise AnswerValidationError("FACT_SUPPORT_MISSING")
            if fact["origin"] == "CALCULATION":
                raise AnswerValidationError("REGISTERED_CALCULATION_REQUIRED")
        for key, value in answer["scope"].items():
            if key in context and str(value) != str(context[key]):
                raise AnswerValidationError("KNOWN_CONTEXT_CHANGED")
        selected, missing = _required_facts(list(citations.values()), context)
        if answer["status"] == "ANSWERED" and (missing or len(selected) != len(citations)):
            raise AnswerValidationError("SOURCE_APPLICABILITY_UNRESOLVED")
        if answer["review_status"] == "EXPERT_REVIEWED":
            raise AnswerValidationError("EXPERT_REVIEW_RECORD_REQUIRED")


def compile_knowledge(blocks: list[dict], source_version_id: str, title: str,
                      knowledge_type: str = "sop") -> dict:
    UUID(source_version_id)
    if knowledge_type not in KNOWLEDGE_TYPES:
        raise ValueError("UNSUPPORTED_KNOWLEDGE_TYPE")
    if not title.strip() or len(title) > 300:
        raise ValueError("INVALID_TITLE")
    if len(blocks) > 9999:
        raise ValueError("DOCUMENT_LIMIT")
    notice = {"block_id": str(uuid4()), "ordinal": 0, "block_type": "warning",
              "data": {"text": "EXTRACTIVE：以下内容按原始资料提取整理，未调用生成模型、未进行语义编译。"
                                "分类不代表规范效力；须人工校对来源、适用条件和步骤并独立复核后发布。"},
              "locator": {"compilation_method": "extractive", "semantic_compile": False}, "citations": []}
    output = [notice]
    for source in blocks:
        UUID(str(source["block_id"]))
        block = copy.deepcopy(source)
        block["ordinal"] = len(output)
        block["citations"] = [{"version_id": source_version_id, "block_id": source["block_id"], "purpose": "FACT"}]
        block["locator"] = {**(block.get("locator") or {}), "source_version_id": source_version_id,
                            "source_block_id": source["block_id"], "source_content_sha256": text_sha256(block_text(source))}
        output.append(block)
    return {"title": title, "knowledge_type": knowledge_type, "applicability": {}, "required_facts": [],
            "legal_status": "UNKNOWN", "valid_from": None, "valid_to": None, "blocks": output}


def _envelope(question, mode, context, run_id, *, answer_scope="formal"):
    UUID(str(run_id))
    result = {"run_id": run_id, "status": "INSUFFICIENT_EVIDENCE", "mode": mode, "summary": "",
            "scope": {k: str(v) if v is not None else None for k, v in context.items()
                      if isinstance(v, (str, int, float, bool)) or v is None},
            "facts": [{"name": k, "value": v, "origin": "USER", "certainty": "PROVIDED"}
                      for k, v in context.items() if isinstance(v, (str, int, float, bool))],
            "missing_facts": [], "claims": [], "citations": [], "solution": None,
            "limitations": ["未调用生成模型；输出为资料原文抽取与已登记步骤整理，不代表业务正确性或执行完成。",
                            "MACHINE_CHECKED仅表示结构、引文与步骤检查；专业适用性仍需复核。"],
            "required_sources": [], "review_status": "MACHINE_CHECKED",
            "generated_at": datetime.now(UTC).isoformat()}
    return _apply_answer_scope(result, answer_scope)


def _required_facts(records, context):
    required = set()
    applicable = []
    for record in records:
        local_required = set()
        declared = record.get("required_facts", [])
        if declared is None:
            local_required.add("source_required_facts")
        else:
            local_required.update(field for field in declared if isinstance(field, str))
        applies = True
        conditions = record.get("applicability") or {}
        if "applicability" in record and record["applicability"] is None:
            local_required.add("source_applicability")
        for clause, negate in (("all", False), ("none", True)):
            for condition in conditions.get(clause, []):
                if (not isinstance(condition, dict) or condition.get("op") not in {"eq", "in"}
                        or not condition.get("values")):
                    local_required.add("source_applicability")
                    continue
                field = condition.get("field")
                if field not in context or context[field] in (None, ""):
                    if field:
                        local_required.add(field)
                else:
                    matches = str(context[field]) in condition.get("values", [])
                    if (not negate and not matches) or (negate and matches):
                        applies = False
        business_date = context.get("business_date")
        if business_date:
            if record.get("valid_from") and str(business_date) < str(record["valid_from"]):
                applies = False
            if record.get("valid_to") and str(business_date) >= str(record["valid_to"]):
                applies = False
        if applies:
            applicable.append(record)
            required.update(local_required)
    missing = sorted(field for field in required if context.get(field) in (None, ""))
    return applicable, missing


def _citation(record, index):
    # Quote at most one bounded source segment, retaining the hash of the ENTIRE block.
    excerpt = record["text"][:1600]
    return {"id": f"E{index}", "resource_id": record["resource_id"], "version_id": record["version_id"],
            "block_id": record["block_id"], "source_title": record.get("title") or "未命名资料",
            "excerpt": excerpt, "locator": _locator(record), "content_sha256": record["content_sha256"]}


def _is_sop(record):
    return record.get("knowledge_type") == "sop" and record.get("kind") != "template"


def _registered_sop_step(record):
    data = record.get("data") or {}
    return (_is_sop(record) and record.get("block_type") == "step"
            and all(isinstance(data.get(k), str) and data[k].strip()
                    for k in ("action", "owner_role", "output", "verification"))
            and block_text(record) == record.get("text"))


def _ordinal(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _order_sop_steps(records):
    """Recover source order only. Never use ranking position or UUID as a sequence."""
    if len(records) <= 1:
        return list(records)
    if all(_ordinal(r.get("ordinal")) for r in records):
        if len({r["ordinal"] for r in records}) != len(records):
            return None
        return sorted(records, key=lambda r: r["ordinal"])
    locators = [r.get("locator") or {} for r in records]
    for field in ("ordinal", "step_index", "step_no", "line_start", "source_char_start"):
        if all(_ordinal(loc.get(field)) for loc in locators) and len({loc[field] for loc in locators}) == len(records):
            return sorted(records, key=lambda r: r["locator"][field])
    positions = []
    for loc in locators:
        match = re.search(r"(?:步骤|step)\s*(\d+)|第\s*(\d+)\s*步", str(loc.get("label", "")), re.IGNORECASE)
        positions.append(int(next(g for g in match.groups() if g is not None)) if match else None)
    if all(p is not None for p in positions) and len(set(positions)) == len(records):
        return [r for _, r in sorted(zip(positions, records, strict=True), key=lambda pair: pair[0])]
    if (all(isinstance(loc.get("node_path"), str) and loc["node_path"] for loc in locators)
            and len({loc.get("part") for loc in locators}) == 1):
        paths = [loc["node_path"] for loc in locators]
        if len(set(paths)) == len(records):
            def path_key(record):
                return tuple((1, int(p)) if p.isdigit() else (0, p)
                             for p in re.split(r"(\d+)", record["locator"]["node_path"]))
            return sorted(records, key=path_key)
    # Distinct PDF pages can establish order; multiple steps on one page also
    # require unambiguous boxes in the same coordinate system.
    if all(_ordinal(loc.get("source_page")) for loc in locators):
        if len({loc["source_page"] for loc in locators}) == len(records):
            return sorted(records, key=lambda r: r["locator"]["source_page"])
        if all(loc.get("coordinate_space") in {"top_left_points", "top_left_pixels"}
               and isinstance(loc.get("bbox"), list) and len(loc["bbox"]) == 4
               and all(isinstance(n, (int, float)) for n in loc["bbox"]) for loc in locators):
            positions = [(loc["source_page"], loc["bbox"][1], loc["bbox"][0]) for loc in locators]
            if len({loc["coordinate_space"] for loc in locators}) == 1 and len(set(positions)) == len(records):
                return [r for _, r in sorted(zip(positions, records, strict=True), key=lambda pair: pair[0])]
    return None


def _sop_coverage(version_records, used_steps):
    """Authority-provided manifest detects omitted steps, never authorizes fetching them.

    Returns (True, 0) for full coverage, (False, missing_count) for a known gap,
    (None, None) when completeness cannot be established or metadata conflicts.
    Non-step block ordinal gaps are valid and must not be invented as missing steps.
    """
    manifests = [r["version_step_ordinals"] for r in version_records if "version_step_ordinals" in r]
    if not manifests or not all(isinstance(m, list) and m and all(_ordinal(n) for n in m)
                                and len(set(m)) == len(m) for m in manifests):
        return None, None
    expected = set(manifests[0])
    if any(set(m) != expected for m in manifests):
        return None, None
    if not all(_ordinal(s.get("ordinal")) for s in used_steps):
        return None, None
    seen = {s["ordinal"] for s in used_steps}
    if len(seen) != len(used_steps) or not seen.issubset(expected):
        return None, None
    return not (expected - seen), len(expected - seen)


def _extractive_solution(question, context, evidence, run_id, *, answer_scope="formal"):
    result = _envelope(question, "solution", context, run_id, answer_scope=answer_scope)
    groups = {}
    for record in _usable_records(evidence, answer_scope=answer_scope):
        if _is_sop(record):
            groups.setdefault((record["resource_id"], record["version_id"]), []).append(record)
    eligible_groups = {}
    for key, records in groups.items():
        applicable, missing = _required_facts(records, context)
        if applicable:
            eligible_groups[key] = (applicable, missing)
    groups = eligible_groups
    # Rank versions as documents, using each title once; a large number of hits
    # from an unrelated SOP cannot win just by contributing more retrieved steps.
    keys = sorted(groups)
    documents = [{"title": groups[key][0][0].get("title", ""),
                  "text": "\n".join(r["text"] for r in groups[key][0])} for key in keys]
    scores = lexical_scores(question, documents)
    ranked = [(score, key) for score, key in zip(scores, keys, strict=True) if score > 0]
    if not ranked:
        # Relevant source facts can still require clarification even when no
        # executable SOP was retrieved. Do not turn a missing date into "no evidence".
        preflight = _evidence_answer(question, "answer", context,
                                     [r for r in evidence if r.get("kind") != "template"
                                      and r.get("knowledge_type") != "solution_template"], run_id,
                                     single_sop=False, answer_scope=answer_scope)
        if preflight["status"] in {"NEEDS_CLARIFICATION", "CONFLICT"}:
            preflight["mode"] = "solution"
            return preflight
        result.update(summary="未找到与问题相关且可用于处理的已发布SOP；FAQ和方案模板不能替代实际操作规程。",
                      required_sources=["与该问题对应、经审核发布的操作SOP及其步骤原文"], review_status="REQUIRES_EXPERT")
        return result
    _, selected_key = min(ranked, key=lambda item: (-item[0], item[1]))
    records, missing = groups[selected_key]
    title = records[0].get("title") or "未命名SOP"
    if missing:
        result.update(status="NEEDS_CLARIFICATION", summary=f"已选中《{title}》，但判断适用性仍需补充事实。",
                      missing_facts=[{"field": field, "question": f"请补充{field}。",
                                      "why_needed": "该字段来自选中SOP的适用条件或必需事实。"} for field in missing])
        return result
    if any(r.get("conflict_group") for r in records):
        result.update(status="CONFLICT", summary=f"《{title}》标记了尚未裁定的冲突，不能直接作为处理方案。",
                      review_status="REQUIRES_EXPERT")
        return result
    valid_steps = [r for r in records if _registered_sop_step(r)]
    ordered = _order_sop_steps(valid_steps)
    if not valid_steps or ordered is None:
        result.update(summary=f"已选中《{title}》，但步骤字段或原始顺序缺失、冲突，暂不能组装有序方案。",
                      required_sources=[f"《{title}》同一版本的完整步骤字段、原始序号或明确定位"],
                      review_status="REQUIRES_EXPERT")
        return result
    # Same input evidence only: preserve low-scoring siblings already authorized
    # by the caller, but never query the vector store/DB or expand to another SOP.
    used = ordered[:12]
    complete, missing_count = _sop_coverage(records, used)
    result["citations"] = [_citation(record, i) for i, record in enumerate(used, 1)]
    # The schema needs at least one claim for ANSWERED; avoid repeating every
    # full source block above the same steps. Each step retains its own citation.
    result["claims"] = [{"id": "C1", "text": used[0]["data"]["action"], "evidence_ids": ["E1"]}]
    steps = [{"id": f"S{i}", "action": record["data"]["action"], "owner_role": record["data"]["owner_role"],
              "inputs": [], "output": record["data"]["output"], "verification": record["data"]["verification"],
              "evidence_ids": [f"E{i}"], "depends_on": []} for i, record in enumerate(used, 1)]
    result["solution"] = {"goal": question, "preconditions": [], "materials": [], "steps": steps,
                          "branches": [], "completion_checks": [s["verification"] for s in steps], "escalation": []}
    result["limitations"].append(f"步骤仅来自《{title}》同一版本，按原文顺序列出；未登记的输入、依赖、异常分支和升级条件仍需核对。")
    if complete:
        result.update(status="ANSWERED", summary=f"已按《{title}》的原始顺序列出该版本登记的全部{len(used)}项步骤；未调用生成模型，步骤尚未执行。")
    elif missing_count is not None:
        result.update(summary=f"《{title}》当前命中{len(used)}项步骤，另有{missing_count}项未命中，方案不完整；以下仅为按原文排序的步骤子集。",
                      required_sources=[f"《{title}》同一版本尚未命中的{missing_count}项步骤原文"],
                      review_status="REQUIRES_EXPERT")
    else:
        result.update(summary=f"《{title}》当前可展示{len(used)}项有序步骤，但完整性未确认；以下仅为步骤子集，不能作为完整执行方案。",
                      required_sources=[f"《{title}》同一版本的完整步骤清单及可核对的原始序号"], review_status="REQUIRES_EXPERT")
    return result


def _evidence_answer(question, mode, context, evidence, run_id, *, single_sop=True, answer_scope="formal"):
    if mode == "solution" and single_sop:
        return _extractive_solution(question, context, evidence, run_id, answer_scope=answer_scope)
    result = _envelope(question, mode, context, run_id, answer_scope=answer_scope)
    records = _usable_records(evidence, answer_scope=answer_scope)
    relevance = lexical_scores(question, records)
    records = [record for record, score in zip(records, relevance, strict=True)
               if score > 0 or (record.get("context_group") and "document_structure" in record.get("retrieval_channels", []))
               or ("vector" in record.get("retrieval_channels", [])
                                and record.get("embedding_mode") in {"fastembed", "http"})]
    records, missing = _required_facts(records, context)
    if missing:
        result.update(status="NEEDS_CLARIFICATION", summary="现有资料要求补充以下事实，才能判断适用性。",
                      missing_facts=[{"field": field, "question": f"请补充{field}。",
                                      "why_needed": "该字段来自检索到的知识适用条件或必需事实。"} for field in missing])
        return result
    if any(record.get("conflict_group") for record in records):
        result.update(status="CONFLICT", summary="资料标记了尚未裁定的冲突，需要专业复核。",
                      review_status="REQUIRES_EXPERT")
        result["citations"] = [_citation(r, i) for i, r in enumerate(records[:12], 1)]
        return result
    if not records:
        result.update(summary="当前没有足够的、可核对出处的相关资料来回答该问题。",
                      required_sources=["与问题直接相关、已核验且适用于该业务日期的来源资料"],
                      review_status="REQUIRES_EXPERT")
        return result
    records = records[:32 if any(record.get("context_group") for record in records) else 12]
    result["citations"] = [_citation(record, i) for i, record in enumerate(records, 1)]
    result["claims"] = [{"id": f"C{i}", "text": cite["excerpt"], "evidence_ids": [cite["id"]]}
                        for i, cite in enumerate(result["citations"], 1)]
    if mode == "answer":
        result.update(status="ANSWERED", summary="以下为与问题相关的资料原文摘录。未生成新的专业判断，请核对来源及适用条件。")
        return result
    steps = []
    ordered = sorted(enumerate(records), key=lambda pair: (pair[1]["version_id"], pair[1].get("ordinal", pair[0])))
    for index, record in ordered:
        data = record.get("data") or {}
        if record.get("block_type") != "step" or not all(isinstance(data.get(k), str) and data[k].strip()
                  for k in ("action", "owner_role", "output", "verification")):
            continue
        # No inferred inputs, dependencies, approval or completion: only explicit registered fields.
        steps.append({"id": f"S{len(steps) + 1}", "action": data["action"], "owner_role": data["owner_role"],
                      "inputs": [], "output": data["output"], "verification": data["verification"],
                      "evidence_ids": [f"E{index + 1}"], "depends_on": []})
    if not steps:
        result.update(summary="已找到相关资料，但其中没有可核对的结构化处理步骤，暂不能形成处理方案。",
                      required_sources=["经审核的SOP/方案step块：操作、责任岗位、输出、核对方法"],
                      review_status="REQUIRES_EXPERT")
    else:
        result.update(status="ANSWERED", summary="以下为已登记SOP步骤的提取整理；未调用生成模型，步骤尚未执行。",
                      solution={"goal": question, "preconditions": [], "materials": [], "steps": steps,
                                "branches": [], "completion_checks": [s["verification"] for s in steps], "escalation": []})
        result["limitations"].append("仅列出本次命中的已登记步骤；资料未登记的输入、依赖、异常分支与升级条件保留为空，不能当作完整SOP已核验。")
    return result


def generate_answer(question: str, mode: str, context: dict, evidence: list[dict], settings, run_id: str,
                    completion_client=None, *, answer_scope: str = "formal", diagnostics=None, source_analysis=None,
                    compact_output=False) -> dict:
    """Generate a bounded answer from caller-authorized formal or reference evidence."""
    _check_answer_scope(answer_scope)
    if not isinstance(question, str) or not question.strip() or len(question) > 20000:
        raise ValueError("INVALID_QUESTION")
    if mode not in {"answer", "solution", "auto"}:
        raise ValueError("INVALID_ANSWER_MODE")
    mode = "answer" if mode == "auto" else mode
    provider = str(getattr(settings, "llm_provider", "evidence"))
    # Grounded HTTP keeps its existing multi-source context and composition path.
    # Only delivered extractive solutions (including HTTP fallback) use one SOP.
    baseline = _evidence_answer(question, mode, context, evidence, run_id,
                                single_sop=provider != "http", answer_scope=answer_scope)
    if provider == "evidence" or baseline["status"] in {"NEEDS_CLARIFICATION", "CONFLICT"}:
        validate_answer(baseline, evidence, answer_scope=answer_scope)
        return baseline
    if provider != "http":
        raise ValueError("UNSUPPORTED_LLM_PROVIDER")
    # Resolved callbacks (including Codex OAuth) do not need an HTTP base URL.
    if (not getattr(settings, "llm_model", None)
            or (not completion_client and not getattr(settings, "llm_base_url", None))):
        if mode == "solution":
            baseline = _extractive_solution(question, context, evidence, run_id, answer_scope=answer_scope)
        baseline["limitations"].append("MODEL_NOT_CONFIGURED：HTTP生成服务未配置；本结果使用证据提取，未发送模型请求。")
        validate_answer(baseline, evidence, answer_scope=answer_scope)
        return baseline
    if not baseline["citations"]:
        validate_answer(baseline, evidence, answer_scope=answer_scope)
        return baseline
    try:
        from .answer_prompt import ANSWER_PROMPT_VERSION, build_answer_system_prompt
        from .answer_retrieval import context_manifest
        instruction = build_answer_system_prompt(answer_scope)
        if answer_scope == "reference":
            selected_ids = {(c["version_id"], c["block_id"]) for c in baseline["citations"]}
            # A private list of whole blocks, in retrieval order. Rebinding also
            # fences all later validation and fallbacks to the retained evidence.
            evidence = [record for record in _usable_records(evidence, answer_scope=answer_scope)
                        if (record["version_id"], record["block_id"]) in selected_ids]
        attempts = len(evidence) if answer_scope == "reference" else 1
        for _ in range(attempts):
            selected_ids = {(c["version_id"], c["block_id"]) for c in baseline["citations"]}
            allowed_context = [{key: record[key] for key in ("resource_id", "version_id", "block_id", "title",
                               "text", "content_sha256", "locator", "block_type", "data", "required_facts",
                               "applicability", "valid_from", "valid_to", "knowledge_type", "kind", "source_kind",
                               "source_verified", "evidence_scope", "draft", "state", "legal_status",
                               "context_group", "retrieval_role", "section_path", "source_role") if key in record}
                               for record in _usable_records(evidence, answer_scope=answer_scope)
                               if (record["version_id"], record["block_id"]) in selected_ids]
            if answer_scope == "reference":
                for record in allowed_context:
                    if record.get("context_group") and record.get("block_type") in {"paragraph", "heading", "warning"}:
                        record.pop("data", None)  # Text is already present, preserve original internal record for verification.
                    # Preserve supplied metadata; absent state is unknown, never implicitly published.
                    record.setdefault("draft", record["state"] == "DRAFT" if record.get("state") else None)
                    record.setdefault("state", "UNKNOWN")
                    record.setdefault("legal_status", "UNKNOWN")
            elif len(json.dumps(allowed_context, ensure_ascii=False)) > 100000:
                raise ProviderError("MODEL_CONTEXT_BUDGET_EXCEEDED")
            # The deterministic fallback is not a description of a generated answer.
            # Do not seed model output with its no-invocation / machine-check labels.
            output_skeleton = copy.deepcopy(baseline)
            output_skeleton.update(summary="以下内容依据所给资料整理，专业适用性仍需复核。",
                                   limitations=["生成内容需核对证据和适用条件；专业结论仍需复核。"],
                                   review_status="REQUIRES_EXPERT")
            _apply_answer_scope(output_skeleton, answer_scope)
            knowledge_context = context_manifest(evidence, (source_analysis or {}).get("plan", {})) if source_analysis else None
            if knowledge_context:
                # Do not anchor generation to a pre-filled list of verbatim claims.
                # Citations remain an exact source registry, synthesis uses empty claims.
                output_skeleton["claims"] = []
                output_skeleton["summary"] = "请在此综合回答原问题，不列举检索结果。"
            payload = {"model": settings.llm_model, "stream": False, "temperature": 0,
                       "max_tokens": 6000, "response_format": {"type": "json_object"},
                       "messages": [{"role": "system", "content": instruction},
                                    {"role": "user", "content": json.dumps({"question": question, "mode": mode,
                                     "context": context, "answer_scope": answer_scope, "evidence": allowed_context,
                                     **({"source_analysis": knowledge_context["plan"], "knowledge_context": knowledge_context} if knowledge_context else {}),
                                     "schema": answer_validator().schema, "output_skeleton": output_skeleton}, ensure_ascii=False)}]}
            content_manifest = None
            if compact_output:
                from .answer_content import request as content_request
                payload["messages"], content_manifest = content_request(question, mode, context, evidence,
                    baseline["citations"], {**(source_analysis or {}), "plan": knowledge_context["plan"]} if knowledge_context else source_analysis,
                    answer_scope, answer_validator().schema)
                instruction = payload["messages"][0]["content"]
            # Match Codex text's ensure_ascii=False UTF-8 serialization, counting
            # the entire payload (including duplicated text/data, schema, skeleton
            # and escaping). Leave room for the adapter's extra JSON instruction.
            if (answer_scope != "reference"
                    or len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= _REFERENCE_REQUEST_BUDGET_BYTES):
                break
            if len(evidence) <= 1:
                raise ProviderError("MODEL_CONTEXT_BUDGET_EXCEEDED")
            last_group = evidence[-1].get("context_group")
            reduced = [row for row in evidence if row.get("context_group") != last_group] if last_group else evidence[:-1]
            if not reduced:
                raise ProviderError("MODEL_CONTEXT_BUDGET_EXCEEDED")
            evidence = reduced
            baseline = _evidence_answer(question, mode, context, evidence, run_id,
                                        single_sop=False, answer_scope=answer_scope)
            if baseline["status"] in {"NEEDS_CLARIFICATION", "CONFLICT"} or not baseline["citations"]:
                validate_answer(baseline, evidence, answer_scope=answer_scope)
                return baseline
        else:
            raise ProviderError("MODEL_CONTEXT_BUDGET_EXCEEDED")
        if diagnostics is not None:
            diagnostics["request_manifest"] = {"prompt_version": ANSWER_PROMPT_VERSION,
                "system_prompt_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
                "system_prompt_chars": len(instruction), "system_prompt_utf8_bytes": len(instruction.encode()),
                "request_utf8_bytes": len(json.dumps(payload, ensure_ascii=False).encode()),
                "evidence_blocks": len(allowed_context), "roles": [m["role"] for m in payload["messages"]],
                **({"knowledge_context": knowledge_context} if knowledge_context else {})}
            if content_manifest:
                diagnostics["request_manifest"].update(content_manifest)
        raw = completion_client(payload) if completion_client else post_json(
            settings.llm_base_url, "chat/completions", payload,
            getattr(settings, "llm_api_key", None), getattr(settings, "llm_timeout_seconds",
            min(30, getattr(settings, "model_timeout_seconds", 30))))
        if raw["choices"][0].get("finish_reason") != "stop":
            raise AnswerValidationError("MODEL_OUTPUT_TRUNCATED_OR_TOOL_REQUESTED")
        message = raw["choices"][0]["message"]
        if message.get("tool_calls") or message.get("function_call"):
            raise AnswerValidationError("MODEL_TOOL_CALL_FORBIDDEN")
        candidate = json.loads(message["content"])
        if compact_output:
            from .answer_content import assemble
            candidate = assemble(candidate, baseline, baseline["citations"], answer_validator().schema)
        # Server-owned fields cannot be promoted by the provider.
        candidate.update(run_id=baseline["run_id"], generated_at=baseline["generated_at"],
                         review_status="REQUIRES_EXPERT")
        if candidate.get("mode") != mode:
            raise AnswerValidationError("ANSWER_MODE_MISMATCH")
        _apply_answer_scope(candidate, answer_scope)
        selected_evidence = [record for record in evidence
                             if (record["version_id"], record["block_id"]) in selected_ids]
        validate_answer(candidate, selected_evidence, mode="grounded", context=context, answer_scope=answer_scope)
        # Runtime state belongs to the server. Remove model-authored fallback
        # labels only AFTER full validation, so they cannot mask an invalid answer.
        candidate["limitations"] = [item for item in candidate["limitations"]
                                     if not any(marker in item for marker in _MODEL_FALLBACK_MARKERS)]
        candidate["limitations"].append("HTTP生成结果通过结构、真实引文、已知事实与数值单位检查；"
                                         "这些检查不证明专业语义正确，需专业复核。")
        return candidate
    except (ProviderError, AnswerValidationError, ValueError, KeyError, TypeError, IndexError) as exc:
        code = getattr(exc, "code", "MODEL_RESPONSE_INVALID")
        if diagnostics is not None:
            diagnostics.update(code=code, **(getattr(exc, "diagnostic", None) or {}))
        if source_analysis is not None or compact_output:
            # A source-routed synthesis is not interchangeable with a list of
            # excerpts. Preserve diagnostics and let the worker report failure.
            raise AnswerValidationError("SYNTHESIS_OUTPUT_REJECTED", diagnostic={"cause": code}) from exc
        if mode == "solution":
            baseline = _extractive_solution(question, context, evidence, run_id, answer_scope=answer_scope)
        baseline["limitations"][1 if answer_scope == "reference" else 0] = (
            "本结果使用资料原文抽取与已登记步骤；HTTP模型未产生可交付的已验证结果。")
        baseline["limitations"].append(f"{code}：HTTP模型未提供可交付的已验证结果；已降级为证据提取。")
        _apply_answer_scope(baseline, answer_scope)
        validate_answer(baseline, evidence, answer_scope=answer_scope)
        return baseline
