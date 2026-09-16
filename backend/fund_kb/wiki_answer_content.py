"""Display-first Markdown answers with server-owned source links and notices.

No model, database, filesystem or legacy business-validator calls. The caller
owns current ACL/scan/hash checks for records and must repeat them at delivery.
Render narrative_markdown with HTML disabled and no external resource loading;
this module deliberately does not turn Markdown, HTML or formula text into HTML.
SOURCE_LINKED means association, not entailment or professional verification.
"""
from __future__ import annotations

import html
import math
import re
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime
from itertools import pairwise
from uuid import UUID

__all__ = ("SERVER_NOTICE", "build_narrative_answer")

SERVER_NOTICE = (
    "以下为尚未完成专业核验的模型综合说明，不代表现行制度、正式业务结论或执行凭证。"
    "本系统未据此执行交易、付款、过账、审批或发布；任何执行或审批状态须以真实业务记录另行核对。"
)
_HIDDEN = "[敏感信息已隐藏]"
_ACTION_NOTICE = "[执行/审批声明未核实；本系统未执行此操作]"
_EMPTY_NOTICE = "模型未提供可展示的公开正文；私有推理或敏感内容不能作为业务回答。"
_WARNINGS = {
    "BUSINESS_VERIFICATION_REQUIRED": "本模块未验证数值、适用条件、业务判断或执行结果；引文关联不等于业务正确性证明。",
    "NO_LOCAL_SOURCES": "本次未提供本地来源；以下文字没有本地证据支持，未经核验，不能作为正式结论。",
    "UNVERIFIED_NARRATIVE": "正文没有关联到可展示的有效引文，尚未核验，不能作为正式业务结论。",
    "UNKNOWN_CITATION": "正文含未识别或不可用的引文标记；已保留原文字，未创建来源链接。",
    "CITATION_RANGE_INCOMPLETE": "部分引用范围的端点或中间编号未登记或不可用；仅关联本次真实提供且可展示的来源，未补造缺失编号。",
    "CITATION_RANGE_REVERSED": "正文含逆序引用范围；已保留原文字，未交换端点或猜造来源链接。",
    "CITATION_RANGE_INVALID": "正文含不完整或非法的引用范围；已保留原文字，未猜补端点或编号。",
    "INVALID_SOURCE_METADATA": "部分来源缺少有效的服务器引文身份或定位信息，未用于创建引文。",
    "AMBIGUOUS_EVIDENCE_ID": "同一引文编号对应不一致的来源，未猜选其中一条或创建替代编号。",
    "SOURCE_CONTENT_NOT_DISPLAYED": "部分来源的可展示字段含敏感信息或私有推理，已省略该引文，未改造其原文或 hash。",
    "SENSITIVE_INFORMATION_REDACTED": "检测到的凭据或敏感信息已替换为隐藏标记。",
    "PRIVATE_REASONING_REMOVED": "私有推理内容已移除；移除并不代表剩余业务文字已通过安全或专业核验。",
    "SYSTEM_ACTION_CLAIM_REMOVED": "正文中明确声称系统或助手已执行业务或审批的语句已替换；本系统没有执行这些操作。",
    "EMPTY_PUBLIC_NARRATIVE": "未收到可展示的公开回答，已保留服务器提示而非编造业务内容。",
}
_ENTITY = re.compile(r"&(?:\#[xX][0-9a-fA-F]+|\#[0-9]+|[A-Za-z][A-Za-z0-9]+);?")
_THINK_TAG = re.compile(r"<\s*(/?)\s*(think|thinking|analysis|reasoning)\b[^>]*(?:>|$)", re.IGNORECASE)
_SECRETS = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"
    r"|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/-]{8,}=*"
    r"|https?://[^\s/@]+:[^\s/@]+@[^\s`<>，,；;]+"
    r"|(?:\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|password|secret|authorization)\b|密钥|密码|访问令牌)"
    r"[\"']?\s*[:=：]\s*(?:\"[^\"]*\"|'[^']*'|(?:Bearer|Basic)\s+[^\s`<>，,；;]+|[^\s`<>，,；;]+)", re.IGNORECASE)
_SECRET_FIELD = re.compile(r"api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|password|secret|authorization|密钥|密码|访问令牌",
                           re.IGNORECASE)
_REFERENCE = re.compile(r"(?<![A-Za-z0-9_./@])E[0-9]+(?![A-Za-z0-9_/@]|\.[A-Za-z0-9_])")
_RANGE_JOIN = re.compile(r"[ \t]*(?:[\]】][ \t]*)?(?:[-‐‑‒–—―－﹣−]{1,2}|至|到)[ \t]*(?:[\[【][ \t]*)?")
_FENCE = re.compile(r"^[ \t]{0,3}(?:>[ \t]*)?(`{3,}|~{3,})(.*)$")
_BACKTICKS = re.compile(r"`+")
_EVIDENCE_ID = re.compile(r"E[1-9][0-9]*\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_SELF_ACTION = re.compile(
    r"(?:本系统|本平台|本助手|本模型|我们|我)"
    r"(?P<bridge>[^，,。；;\n！？!?]{0,16}?)(?:已经|现已|确已|已)"
    r"(?:(?:为|替|帮)[你您](?:完成|办理)?|成功)?"
    r"(?:(?:完成|执行|办理)[^，,。；;\n！？!?]{0,12}?(?:付款|支付|过账|入账|交易|清算|交收|审批|发布)"
    r"|付款|支付|过账|入账|批准|审批|发布|确认(?:回售|赎回|付款|审批|交收))"
    r"(?:(?!但是|然而|不过|但)[^，,。；;\n！？!?])*")
_NONASSERTIVE = re.compile(r"如果|假如|假设|若|倘若|只要|一旦|除非|是否|不代表|不表示|不能|无法|不是|并非|没有|未曾|尚未|未")
_DOUBLE_NEGATIVE = re.compile(r"(?:不是|并非|没有)[^，,。；;\n]{0,12}(?:不|未|没有)|(?:不能|无法|不)否认")


def _decoded(text):
    """Inspection-only entity view plus original spans; preserve normal Markdown."""
    spans = [(i, i + 1) for i in range(len(text))]
    view = text
    for _ in range(8):
        chunks, mapped, start, changed = [], [], 0, False
        for match in _ENTITY.finditer(view):
            decoded = html.unescape(match[0])
            chunks.append(view[start:match.start()])
            mapped.extend(spans[start:match.start()])
            chunks.append(decoded)
            if decoded == match[0]:
                mapped.extend(spans[match.start():match.end()])
            else:
                changed = True
                original = (spans[match.start()][0], spans[match.end() - 1][1])
                mapped.extend([original] * len(decoded))
            start = match.end()
        if not changed:
            break
        chunks.append(view[start:])
        mapped.extend(spans[start:])
        view, spans = "".join(chunks), mapped
    return view, spans


def _replace_spans(text, intervals, replacement):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    result, cursor = [], 0
    for start, end in merged:
        result.extend((text[cursor:start], replacement))
        cursor = end
    result.append(text[cursor:])
    return "".join(result)


def _private_reasoning(text):
    view, spans = _decoded(text)
    stack, intervals, opened = [], [], None
    for match in _THINK_TAG.finditer(view):
        name = match[2].lower()
        if not match[1]:
            if not stack:
                opened = spans[match.start()][0]
            stack.append(name)
        elif stack:
            # A mismatched closing tag cannot prematurely expose private text.
            if name == stack[-1]:
                stack.pop()
                if not stack:
                    intervals.append((opened, spans[match.end() - 1][1]))
        else:
            # Truncated transports can omit the opening tag. Never display a
            # private prelude merely because only its closing tag survived.
            intervals.append((0, spans[match.end() - 1][1]))
    if stack:
        intervals.append((opened, len(text)))
    return _replace_spans(text, intervals, ""), bool(intervals)


def _redact(text):
    view, spans = _decoded(text)
    intervals = [(spans[m.start()][0], spans[m.end() - 1][1]) for m in _SECRETS.finditer(view)]
    return _replace_spans(text, intervals, _HIDDEN), bool(intervals)


def _clean(text, warn):
    text, removed = _private_reasoning(text)
    if removed:
        warn("PRIVATE_REASONING_REMOVED")
    text, redacted = _redact(text)
    if redacted:
        warn("SENSITIVE_INFORMATION_REDACTED")
    return text


def _system_claims(text, warn):
    """Only neutralize explicit first-person platform actions, not business prose."""
    intervals = []
    for match in _SELF_ACTION.finditer(text):
        prefix = re.split(r"[，,。；;\n！？!?]|但是|然而|不过|但", text[:match.start()])[-1]
        framing = prefix + match["bridge"]
        if _NONASSERTIVE.search(framing) and not _DOUBLE_NEGATIVE.search(framing):
            continue
        if re.search(r"(?:原文|资料|案例|凭据)(?:写道|记载|描述|提到)", prefix):
            continue  # Attributed text is not a claim of platform execution.
        intervals.append(match.span())
    if intervals:
        warn("SYSTEM_ACTION_CLAIM_REMOVED")
    return _replace_spans(text, intervals, _ACTION_NOTICE)


def _uuid(value):
    if type(value) is not str:
        return False
    try:
        UUID(value)
        return True
    except ValueError:
        return False


def _citation(record, warn):
    """Never repair missing identities, recompute a hash or execute source text."""
    if any(not _uuid(record.get(key)) for key in ("resource_id", "version_id", "block_id")):
        warn("INVALID_SOURCE_METADATA")
        return None
    digest = record.get("content_sha256")
    text, title = record.get("text"), record.get("title")
    locator = record.get("locator") or {}
    if (type(digest) is not str or not _HASH.fullmatch(digest) or type(text) is not str or not text.strip()
            or (title is not None and type(title) is not str) or type(locator) is not dict):
        warn("INVALID_SOURCE_METADATA")
        return None
    label = locator.get("label")
    if label is not None and type(label) is not str:
        warn("INVALID_SOURCE_METADATA")
        return None
    position = {"label": label or "内容块 " + record["block_id"]}
    for key in ("source_page", "sheet", "cell"):
        if key not in locator:
            continue
        value = locator[key]
        if ((key == "source_page" and (type(value) is not int or value <= 0))
                or (key != "source_page" and type(value) is not str)):
            warn("INVALID_SOURCE_METADATA")
            return None
        position[key] = value
    citation = {"id": record["evidence_id"], **{key: record[key] for key in
        ("resource_id", "version_id", "block_id", "content_sha256")},
        "source_title": title or "未命名资料", "excerpt": text, "locator": position}
    for value in (text, citation["source_title"], *[v for v in position.values() if type(v) is str]):
        without_private, private = _private_reasoning(value)
        _, sensitive = _redact(without_private)
        if private or sensitive:
            # Rewriting a quote would no longer be an exact source excerpt.
            warn("SOURCE_CONTENT_NOT_DISPLAYED")
            return None
    return citation


def _reference_view(text):
    """Mask fenced/inline code for detection only; never edit the displayed body."""
    text, _ = _decoded(text)

    def mask(value):
        # Non-whitespace prevents a range from bridging across a code span.
        return "".join(c if c in "\r\n" else "\x00" for c in value)

    chunks, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = _FENCE.match(line.rstrip("\r\n"))
        if fence is not None:
            chunks.append(mask(line))
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]))
            chunks.append(mask(line))
        else:
            chunks.append(line)
    view = "".join(chunks)
    runs = list(_BACKTICKS.finditer(view))
    following, pairs = {}, {}
    for index in range(len(runs) - 1, -1, -1):
        length = len(runs[index][0])
        if length in following:
            pairs[index] = following[length]
        following[length] = index
    chunks, start = [], 0
    for index, opening in enumerate(runs):
        if opening.start() < start or index not in pairs:
            continue
        end = runs[pairs[index]].end()
        chunks.extend((view[start:opening.start()], mask(view[opening.start():end])))
        start = end
    chunks.append(view[start:])
    return "".join(chunks)


def _evidence_order(identifier):
    # Numeric ordering without converting model-controlled, arbitrarily long
    # integers. Canonical registered IDs never have leading zeroes.
    digits = identifier[1:]
    return len(digits), digits


def _consecutive(left, right):
    """Check two registered IDs for a gap without enumerating missing numbers."""
    a, b = left[1:], right[1:]
    index = len(a) - 1
    while index >= 0 and a[index] == "9":
        index -= 1
    if index < 0:
        return len(b) == len(a) + 1 and b[0] == "1" and all(c == "0" for c in b[1:])
    return (len(a) == len(b) and a[:index] == b[:index]
            and ord(b[index]) == ord(a[index]) + 1 and all(c == "0" for c in b[index + 1:]))


def _reference_ids(narrative, available, warn):
    """Resolve single/range mentions against caller-owned IDs using bisect.

    Runtime is bounded by text/registered records, never by the numeric width
    of a model range. Missing endpoints/gaps yield warnings and only existing
    members; reversed/invalid ranges yield no members at all.
    """
    ordered = sorted(available, key=_evidence_order)
    keys = [_evidence_order(eid) for eid in ordered]
    gaps = [0]
    for previous, current in pairwise(ordered):
        gaps.append(gaps[-1] + (not _consecutive(previous, current)))
    selected, intervals, expanded = {}, [], set()
    view, cursor = _reference_view(narrative), 0
    while match := _REFERENCE.search(view, cursor):
        first = match[0]
        cursor = match.end()
        join = _RANGE_JOIN.match(view, cursor)
        if join is None:
            if first in available:
                selected.setdefault(first, None)
            else:
                warn("UNKNOWN_CITATION")
            continue
        last = _REFERENCE.match(view, join.end())
        if last is None:
            cursor = join.end()
            warn("CITATION_RANGE_INVALID")
            warn("UNKNOWN_CITATION")
            continue
        cursor = last.end()
        if not _EVIDENCE_ID.fullmatch(first) or not _EVIDENCE_ID.fullmatch(last[0]):
            warn("CITATION_RANGE_INVALID")
            warn("UNKNOWN_CITATION")
            continue
        low, high = _evidence_order(first), _evidence_order(last[0])
        if low > high:
            warn("CITATION_RANGE_REVERSED")
            warn("UNKNOWN_CITATION")
            continue
        left, right = bisect_left(keys, low), bisect_right(keys, high)
        if first not in available or last[0] not in available or (right > left and gaps[right - 1] > gaps[left]):
            warn("CITATION_RANGE_INCOMPLETE")
            warn("UNKNOWN_CITATION")
        intervals.append((low, high))
        if (left, right) not in expanded:
            expanded.add((left, right))
            for eid in ordered[left:right]:
                selected.setdefault(eid, None)
    return list(selected), intervals


def build_narrative_answer(question, mode, context, records, markdown, run_id):
    """Build a server-owned display envelope; Markdown is never json.loads'ed.

    Business rules are advisory here, not a mechanism to discard the answer.
    No legacy validator is called: this path promises source linking, NOT
    mechanical/professional validation of amounts, formulas or execution claims.
    Caller-owned input errors can raise; model text/schema shape cannot.
    """
    if type(question) is not str or type(markdown) is not str:
        raise ValueError("NARRATIVE_INPUT_MUST_BE_TEXT")
    if type(context) is not dict or type(records) not in (list, tuple):
        raise ValueError("INVALID_NARRATIVE_INPUT")
    if not _uuid(run_id):
        raise ValueError("INVALID_RUN_ID")
    if mode not in ("answer", "solution", "auto"):
        raise ValueError("INVALID_ANSWER_MODE")
    warnings, codes = [], set()

    def warn(code):
        if code not in codes:
            codes.add(code)
            warnings.append({"code": code, "message": _WARNINGS[code]})

    narrative = _system_claims(_clean(markdown, warn), warn)
    has_public_text = bool(narrative.strip())
    if not has_public_text:
        narrative = _EMPTY_NOTICE
        warn("EMPTY_PUBLIC_NARRATIVE")
    # Index identities only. Source content is inspected only for mentioned,
    # registered IDs, including the real members of a range.
    available = {}
    for record in records:
        if type(record) is not dict:
            warn("INVALID_SOURCE_METADATA")
            continue
        identifier = record.get("evidence_id")
        if type(identifier) is not str or not _EVIDENCE_ID.fullmatch(identifier):
            warn("INVALID_SOURCE_METADATA")
            continue
        available.setdefault(identifier, []).append(record)
    markers, intervals = _reference_ids(narrative, available, warn)
    registry, invalid = {}, set()
    for identifier in markers:
        for record in available[identifier]:
            citation = _citation(record, warn)
            if citation is None:
                registry.pop(identifier, None)
                invalid.add(identifier)
            elif identifier in registry and registry[identifier] != citation:
                registry.pop(identifier)
                invalid.add(identifier)
                warn("AMBIGUOUS_EVIDENCE_ID")
            elif identifier not in invalid:
                registry[identifier] = citation
    citations = [registry[eid] for eid in markers if eid in registry]
    if any(eid not in registry for eid in markers):
        warn("UNKNOWN_CITATION")
    invalid_keys = sorted(_evidence_order(eid) for eid in invalid)
    if any(bisect_left(invalid_keys, low) < bisect_right(invalid_keys, high) for low, high in intervals):
        warn("CITATION_RANGE_INCOMPLETE")
    grounding = "SOURCE_LINKED" if citations else "UNVERIFIED" if records else "NO_LOCAL_SOURCES"
    if grounding == "UNVERIFIED":
        warn("UNVERIFIED_NARRATIVE")
    elif grounding == "NO_LOCAL_SOURCES":
        warn("NO_LOCAL_SOURCES")
    warn("BUSINESS_VERIFICATION_REQUIRED")

    scope, facts = {}, []
    for key, value in context.items():
        if type(key) is not str or type(value) not in (str, int, float, bool, type(None)):
            continue
        if type(value) is float and not math.isfinite(value):
            continue
        name = _clean(key, warn)
        if not name:
            continue
        if _SECRET_FIELD.fullmatch(key):
            value = _HIDDEN
            warn("SENSITIVE_INFORMATION_REDACTED")
        elif type(value) is str:
            value = _clean(value, warn)
        scope[name] = str(value) if value is not None else None
        if value is not None:
            facts.append({"name": name, "value": value, "origin": "USER", "certainty": "PROVIDED"})
    return {"run_id": run_id, "generated_at": datetime.now(UTC).isoformat(),
        "status": "ANSWERED" if has_public_text else "INSUFFICIENT_EVIDENCE",
        "mode": "answer" if mode == "auto" else mode,
        "summary": "模型综合说明", "scope": scope, "facts": facts, "missing_facts": [],
        "claims": [], "citations": citations, "solution": None,
        "limitations": [SERVER_NOTICE, *[item["message"] for item in warnings]],
        "required_sources": [], "review_status": "REQUIRES_EXPERT", "format": "wiki_markdown",
        "narrative_markdown": narrative, "grounding_status": grounding,
        "quality_warnings": warnings, "server_notice": SERVER_NOTICE}
