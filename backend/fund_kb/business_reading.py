"""Owner-configured business focus and an independent primary-source channel.

Categories/IDs select a search scope, never confer legal effect or access. No
asset/question-specific answer branches. All candidates still use normal exact
source reading, applicability, lifecycle and authority checks.
"""
from __future__ import annotations

from datetime import date
from sqlalchemy import select

from . import models as m, services as svc
from .source_reading_policy import PREFIX


def business_profile(db, space_id):
    policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == PREFIX + space_id))
    value = policy.config.get("business_profile") if policy else None
    if value is None:
        return None
    if (not isinstance(value, dict) or not isinstance(value.get("label"), str) or not value["label"].strip()
            or not isinstance(value.get("core_categories"), list)
            or not value["core_categories"]
            or any(not isinstance(s, str) or not s.strip() for s in value["core_categories"])
            or not isinstance(value.get("foundation_sources", []), list)):
        raise ValueError("BUSINESS_READING_PROFILE_INVALID")
    return value


def profile_instructions(profile):
    if not profile:
        return ""
    return (f"\n本知识库的默认业务视角是：{profile['label']}。用户省略背景时按此视角理解，不把资产事件默认解释为交易所交易机制问答。"
        "用户明确限定其他业务时遵循其实际范围，不编造用户事实。以直接适用的核心业务准则/指引为主要依据；"
        "交易规则说明交易或事件条件，不能替代估值处理标准；其他主体适用的标准也不能直接套用。"
        "先解决用户的估值/核算决策、适用分支和必要操作，不先长篇罗列交易所规则。"
        "若关键估值依据尚未阅读，应先SEARCH/READ补查，不能把已知缺口包装成长篇最终答案。")


def core_catalog(profile, pages):
    if not profile:
        return {}
    categories = profile["core_categories"]
    return {pid: p for pid, p in pages.items() if p["kind"] == "document"
        and any(p.get("category", "") == c or p.get("category", "").startswith(c + "/") for c in categories)}


def add_foundations(db, profile, pages, plan, context=None):
    if not profile:
        return
    by_resource = {p["resource_id"]: p for p in pages.values() if p["kind"] == "document"}
    for config in profile.get("foundation_sources", []):
        if not isinstance(config, dict):
            raise ValueError("BUSINESS_FOUNDATION_INVALID")
        if config.get("not_before") and svc.effective_date(context or {}) < date.fromisoformat(config["not_before"]):
            continue
        page = by_resource.get(config.get("resource_id"))
        if page is None:
            plan["warnings"].append("DOMAIN_FOUNDATION_NOT_ADMITTED")
            continue
        terms = config.get("anchor_terms", [])
        if not isinstance(terms, list) or not terms or any(not isinstance(t, str) or not t for t in terms):
            raise ValueError("BUSINESS_FOUNDATION_ANCHORS_INVALID")
        rows = db.execute(select(m.ContentBlock.block_id, m.ContentBlock.search_text)
            .where(m.ContentBlock.version_id == page["version_id"]).order_by(m.ContentBlock.ordinal))
        anchors = [bid for bid, text in rows if any(t in text for t in terms)]
        if not anchors:
            plan["warnings"].append("DOMAIN_FOUNDATION_NOT_LOCATED")
            continue
        plan["sources"].append({"page_id": page["id"], "resource_id": page["resource_id"],
            "version_id": page["version_id"], "title": page["title"], "role": "domain_foundation",
            "rule_id": "business-foundation", "topic_block_ids": anchors, "anchor_block_ids": anchors})
    plan["matched_rules"].append("business-foundation")
    plan["warnings"] = list(dict.fromkeys(plan["warnings"]))


def merge_primary_route(base, primary, primary_pages):
    """Keep base candidates/dependencies; guarantee the independent core route."""
    merged = dict(base)
    merged["requested"] = list(dict.fromkeys([*primary["requested"], *base["requested"]]))
    merged["anchors"] = {pid: list(dict.fromkeys([*primary["anchors"].get(pid, []), *base["anchors"].get(pid, [])]))
                         for pid in merged["requested"]}
    merged["used_edges"] = [*primary.get("used_edges", []), *base.get("used_edges", [])]
    merged["warnings"] = list(dict.fromkeys([*primary.get("warnings", []), *base.get("warnings", [])]))
    merged["domain_primary_anchors"] = {pid: primary["anchors"][pid] for pid in primary["requested"] if pid in primary_pages and primary["anchors"].get(pid)}
    return merged


def bind_primary_route(plan, route, pages):
    """ID-only cached routes are re-bound to THIS freshly authorized catalog."""
    for pid, anchors in route.get("domain_primary_anchors", {}).items():
        if pid not in pages or not anchors:
            continue
        page = pages[pid]
        plan["sources"].append({"page_id": pid, "resource_id": page["resource_id"], "version_id": page["version_id"],
            "title": page["title"], "role": "domain_core", "rule_id": "business-core-retrieval",
            "topic_block_ids": list(anchors), "anchor_block_ids": list(anchors)})
    if route.get("domain_primary_anchors"):
        plan["matched_rules"].append("business-core-retrieval")


def primary_first(ids, plan):
    primary = {p["page_id"] for p in plan["sources"] if p["role"] in {"domain_core", "domain_foundation", "valuation_rule"}}
    return [pid for pid in ids if pid in primary] + [pid for pid in ids if pid not in primary]
