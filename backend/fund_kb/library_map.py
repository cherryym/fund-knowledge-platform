"""Library map for reasoning: metadata of the actor's authorized catalog, no bodies.

It tells the model what exists before planning: a concept outline, every
source document with its tier/issuer/effective hints (rule-extracted,
unconfirmed) and administrator-confirmed authority facts, and the knowledge
page directory (topic pages by title at every level). It grants nothing:
every READ still passes scoped reading, ACL and hash checks, and only read
[E] ids may be cited.
"""
from __future__ import annotations

from collections import defaultdict
from hashlib import sha256

from .ontology import load as load_ontology
from .ontology import outline_text
from .source_metadata import TIER_ORDER

MAP_VERSION = "library-map-v2-20261001"
LEVELS = ("full", "sources", "compact")
_KINDS = {"rule": "规则", "term": "术语", "sop": "SOP", "scenario": "场景", "faq": "问答", "topic": "专题",
          "atomic_rule": "规则", "framework": "框架"}
_STATES = {"DRAFT": "草稿", "IN_REVIEW": "待审", "APPROVED": ""}


def _effective(entry):
    parts = []
    for item in entry.get("effective_statements", []):
        if item.get("mode") == "transition_deadline":
            parts.append(f"过渡期至{item['date']}实施完毕（不是失效日期）")
        elif not any(p.startswith(("施行", "自公布")) for p in parts):
            parts.append(f"施行{item['date']}" if item.get("date") else
                         f"自公布之日起施行（落款约{item['derived_date']}）" if item.get("derived_date") else "自公布之日起施行")
    return "；".join(parts)


def _source_line(page, entry, by_version, level):
    parts = [page["id"], page["title"] if level != "compact" else page["title"][:40]]
    if entry and level != "compact":
        if entry.get("issuer"):
            parts.append("发布：" + entry["issuer"]["value"])
        effective = _effective(entry)
        if effective:
            parts.append(effective)
        if entry.get("subjects"):
            parts.append("标题所示对象：" + "、".join(entry["subjects"]))
        hints = [h for h in entry.get("status_hints", []) if not h.startswith("资料分类")]
        if hints:
            parts.append("、".join(hints))
    if page.get("legal_status") not in (None, "", "UNKNOWN", "NOT_APPLICABLE"):
        parts.append("效力字段：" + page["legal_status"])
    if page.get("valid_from") and level != "compact":
        parts.append("生效字段：" + str(page["valid_from"])[:10])
    for fact in page.get("source_authority", []):
        if fact.get("validation_state") == "VALID":
            mode = "整体替代" if fact["scope"] == "full" else "部分替代"
            parts.append(f"已确认：自{fact['effective_from']}起被{fact['successor_page_id']}{mode}")
        else:
            parts.append("已登记替代关系待重新核对")
    if entry:
        links = entry.get("links", {})
        for key, label in (("abolished_by", "预抽：被{}声明废止"), ("abolishes", "预抽：声明废止{}"),
                           ("newer_versions", "预抽：有更新版本{}"), ("older_versions", "预抽：旧版本{}"),
                           ("published_by", "预抽：由{}发布")):
            targets = [by_version[vid] for vid in links.get(key, []) if vid in by_version]
            if targets:
                parts.append(label.format("、".join(targets)))
    return " | ".join(parts)


def build(pages, candidates, *, level="full"):
    """pages: authorized catalog (W-id -> page). candidates: version_id -> metadata candidate."""
    if level not in LEVELS:
        raise ValueError("LIBRARY_MAP_LEVEL")
    by_version = {p["version_id"]: pid for pid, p in pages.items()}
    sources = [p for p in pages.values() if p["kind"] == "document"]
    knowledge = [p for p in pages.values() if p["kind"] == "knowledge"]
    lines = [(f"【库地图 {MAP_VERSION}】以下只是本库当前授权目录的元数据，不是正文或依据；需READ读取后才能引用E编号。"
              "“预抽”项由规则从原文抽取、未经确认，使用前须读到对应原文；“已确认”项为管理员登记的来源治理事实。")]
    lines.append(outline_text() if level != "compact" else
                 "概念维度：" + "；".join(d["name"] for d in load_ontology()["dimensions"]))
    tiers = defaultdict(list)
    for page in sources:
        entry = candidates.get(page["version_id"])
        tiers[(entry or {}).get("tier", "未抽取层级")].append((page, entry))
    lines.append(f"\n一、来源文档（{len(sources)}份，按预抽层级分组；同组内层级不代表效力高低已确认）")
    for tier in [*TIER_ORDER, "未抽取层级"]:
        rows = sorted(tiers.get(tier, []), key=lambda item: item[0]["title"])
        if rows:
            lines.append(f"### {tier}（{len(rows)}）")
            lines.extend(_source_line(page, entry, by_version, level) for page, entry in rows)
    categories = defaultdict(list)
    for page in knowledge:
        categories[page.get("category") or "未分类"].append(page)
    lines.append(f"\n二、知识页目录（{len(knowledge)}页，按分类）")
    for category in sorted(categories):
        rows = sorted(categories[category], key=lambda p: p["title"])
        if level == "full":
            items = "；".join(f"{p['id']}{_KINDS.get(p.get('compilation_type') or p.get('knowledge_type'), '')}"
                             f"{('[' + _STATES[p['state']] + ']') if _STATES.get(p['state']) else ''}:{p['title']}"
                             for p in rows)
            lines.append(f"### {category}（{len(rows)}）{items}")
        else:
            # Topic pages compile several sources into one navigable page; list them so the
            # planner can READ them by id. Atomic pages stay counted only to keep the map small.
            topics = "；".join(f"{p['id']}专题{('[' + _STATES[p['state']] + ']') if _STATES.get(p['state']) else ''}:"
                              f"{p['title'] if level != 'compact' else p['title'][:40]}"
                              for p in rows if p.get("compilation_type") == "topic")
            lines.append(f"### {category}（{len(rows)}页；{'专题：' + topics + '；' if topics else ''}"
                         "可SEARCH检索或CATALOG查看标题）")
    text = "\n".join(lines)
    stats = {"map_version": MAP_VERSION, "level": level, "sources": len(sources), "knowledge_pages": len(knowledge),
             "sources_with_candidates": sum(1 for p in sources if p["version_id"] in candidates),
             "confirmed_authority_facts": sum(len(p.get("source_authority", [])) for p in sources),
             "characters": len(text), "body_blocks_loaded": 0,
             "ontology_version": load_ontology()["ontology_version"]}
    return {"text": text, "sha256": sha256(text.encode("utf-8")).hexdigest(), "stats": stats}


PLANNING_LEVELS = ("sources", "compact")


def build_fitting(pages, candidates, fits, render, *, levels=LEVELS):
    """Richest allowed level whose rendered request fits; None when none fits."""
    for level in levels:
        result = build(pages, candidates, level=level)
        if fits(render(result["text"])):
            return result
    return None
