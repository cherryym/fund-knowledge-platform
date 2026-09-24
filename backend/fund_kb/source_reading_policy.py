"""Curated, space-scoped primary reading requirements, never legal-effect overrides.

Policies bind resource IDs selected by the knowledge owner, not filenames or
similarity scores. Current catalog admission remains authoritative for ACL,
version, scope and source availability; content is verified by the normal reader.
"""
from __future__ import annotations

import unicodedata
from datetime import date

from sqlalchemy import select

from . import models as m
from . import services as svc

PREFIX = "answer-source-reading:"


def _normalized(value):
    return unicodedata.normalize("NFKC", value).casefold()


def policy_stamp(db, space_id):
    row = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == PREFIX + space_id))
    legacy = svc.digest([row.id, row.revision, row.config]) if row else None
    from .source_authority import authority_stamp
    governance = authority_stamp(db, space_id)
    return svc.digest([legacy, governance]) if governance else legacy


def build_reading_plan(db, space_id, question, context, pages):
    policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == PREFIX + space_id))
    result = {"policy_stamp": policy_stamp(db, space_id), "sources": [], "matched_rules": [], "warnings": []}
    if not policy:
        return result
    config = policy.config
    if config.get("schema_version") != 1 or config.get("space_id") != space_id or not isinstance(config.get("rules"), list):
        raise ValueError("SOURCE_READING_POLICY_INVALID")
    by_resource = {page["resource_id"]: page for page in pages.values() if page["kind"] == "document"}
    query = _normalized(question)
    day = svc.effective_date(context)
    for rule in config["rules"]:
        groups = rule.get("query_term_groups", [])
        if not groups or any(not isinstance(group, list) or not group or
                any(not isinstance(term, str) or not term for term in group) for group in groups):
            raise ValueError("SOURCE_READING_POLICY_INVALID")
        if not all(any(_normalized(term) in query for term in group) for group in groups):
            continue
        if rule.get("not_before") and day < date.fromisoformat(rule["not_before"]):
            continue
        result["matched_rules"].append(rule["id"])
        for rid in rule.get("resource_ids", []):
            page = by_resource.get(rid)
            if page is None:
                # Do not disclose an inaccessible source's ID or title.
                result["warnings"].append("REQUIRED_SOURCE_NOT_ADMITTED")
                continue
            rows = list(db.execute(select(m.ContentBlock.block_id, m.ContentBlock.search_text)
                .where(m.ContentBlock.version_id == page["version_id"]).order_by(m.ContentBlock.ordinal)))
            topical = [bid for bid, text in rows if any(_normalized(term) in _normalized(text)
                for term in rule.get("source_topic_terms", []))]
            contexts = [bid for bid, text in rows if any(_normalized(term) in _normalized(text)
                for term in rule.get("source_context_terms", []))]
            if not topical:
                result["warnings"].append("REQUIRED_SOURCE_TOPIC_NOT_LOCATED")
            result["sources"].append({"page_id": page["id"], "resource_id": rid, "version_id": page["version_id"],
                "title": page["title"], "role": "valuation_rule", "rule_id": rule["id"],
                "topic_block_ids": topical, "anchor_block_ids": list(dict.fromkeys([*topical, *contexts]))})
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result


def plan_instructions(plan):
    if not plan["matched_rules"]:
        return ""
    lines = ["\n本题主来源核对要求（仅决定本题阅读组织，不宣告法规效力或扩大权限）：",
        "以当前业务直接适用的估值准则/指引为核心，交易规则只解释事件条件，会计手册说明核算衔接。新旧资料有差异时核对业务日期、适用主体和效力，不能混用。"]
    if any(source.get("role") == "valuation_rule" for source in plan["sources"]):
        lines += ["先辨明用户问的是日常估值取价还是合同兑付金额。估值取价应依据适用估值标准的直接条款；会计手册只解释核算衔接，不能以分录、兑付金额或脚注倒推估值规则。",
                  "合同回售价、第三方估值价格、会计结转金额应区分。若问合同金额，需核对发行条款和公告，不把估值标准误说成合同定价规则。"]
    for source in plan["sources"]:
        if source.get("role") == "domain_core":
            lines.append(f"核心目录检索候选 {source['page_id']} | {source['title']}：相关性及目录归类不证明其适用于本题。"
                "核对主体、资产、阶段和业务日期；候选可以是替代或参考，不要求把每份候选都当作直接主依据。")
        elif source.get("role") == "domain_foundation":
            lines.append(f"基础规则 {source['page_id']} | {source['title']}：核对适用的基本原则，不能替代具体品种/事件的直接条款。")
        else:
            lines.append(f"必须核对 {source['page_id']} | {source['title']} 的本题直接条款、适用范围和实施时间。"
                "综合答案对估值价格口径的说明应引用该直接原文，不能只引用手册或第三方技术说明。")
    if plan["warnings"]:
        lines.append("有必需主来源未准入或未定位到主题条款：" + "、".join(plan["warnings"]) +
            "。应明确证据缺口，不能把其他来源当作已核对的主标准。")
    return "\n".join(lines)


def check_primary_citations(plan, records, answer):
    actual = {(row["version_id"], row["block_id"]) for row in records}
    cited = {(row["version_id"], row["block_id"]) for row in answer.get("citations", [])}
    covered = []
    for source in plan["sources"]:
        topics = {(source["version_id"], bid) for bid in source["topic_block_ids"]}
        covered.append({"resource_id": source["resource_id"], "version_id": source["version_id"],
            "read": bool(topics) and topics <= actual, "cited": bool(topics & cited)})
    required = [row for row, source in zip(covered, plan["sources"]) if source.get("role") != "domain_core"]
    candidates = [row for row, source in zip(covered, plan["sources"]) if source.get("role") == "domain_core"]
    # Explicit owner-bound standards remain mandatory; retrieved core candidates
    # form an alternative pool, not a demand to cite every possibly relevant file.
    complete = (not plan["warnings"] and bool(covered)
        and all(row["read"] and row["cited"] for row in required)
        and (not candidates or any(row["read"] and row["cited"] for row in candidates)))
    if plan["matched_rules"] and not complete:
        answer["quality_warnings"].append({"code": "PRIMARY_RULE_CITATION_MISSING",
            "message": "本题直接估值标准尚未完整核对或未被正确引用，不能把当前答复作为确定取价依据。"})
    return {"required": bool(plan["matched_rules"]), "covered": complete, "sources": covered,
            "warnings": plan["warnings"]}
