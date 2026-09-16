"""Authorized complete Wiki pages and precisely located complete source sections.

This is a model-context view, never a document edit or a replacement for the
full-document reader. Frozen hashes/ACL are checked before selecting sections.
"""
from __future__ import annotations

from collections import defaultdict

from . import services as svc
from .reference_evidence import reference_evidence
from .source_sections import build_document_sections, select_sections_from_outline


def source_anchors(pages, requested, discovered=None):
    """Only anchors from selected pages' registered relations or current retrieval."""
    anchors = defaultdict(set)
    versions = {page["version_id"]: pid for pid, page in pages.items()}
    for pid in requested:
        if pid not in pages:
            continue
        for bid in (discovered or {}).get(pid, ()):
            anchors[pid].add(bid)
        for relation in pages[pid].get("relations", []):
            if relation.get("source") != pid:
                continue
            for anchor in relation.get("anchors", []):
                target = versions.get(anchor.get("version_id"))
                if target and anchor.get("block_id"):
                    anchors[target].add(anchor["block_id"])
    return anchors


def read_scoped_pages(db, user, space_id, pages, requested, *, context=None, scope="reference",
                      anchors=None, sections=None, full_pages=(), next_evidence=1,
                      structure_version="v2", expand_dependencies=False, expand_parents=False):
    wanted = list(dict.fromkeys(pid for pid in requested if pid in pages))
    versions = {pages[pid]["version_id"] for pid in wanted}
    records = reference_evidence(db, user, space_id, context or {}, reading=True, version_ids=versions) \
        if scope == "reference" else svc.eligible_evidence(db, user, space_id, context or {}, version_ids=versions)
    grouped = defaultdict(list)
    for row in records:
        grouped[row["version_id"]].append(row)
    fresh, unavailable, outlines = [], [], []
    for pid in wanted:
        page = pages[pid]
        rows = sorted(grouped.get(page["version_id"], []), key=lambda row: (row["ordinal"], row["block_id"]))
        if not rows or len(rows) != page["block_count"]:
            unavailable.append(pid)
            continue
        existing = {row["block_id"]: row for row in page.get("records", [])}
        if page["kind"] == "knowledge" or pid in full_pages:
            chosen, chosen_sections = rows, []
        else:
            blocks = [{"block_id": row["block_id"], "ordinal": row["ordinal"],
                "block_type": row.get("block_type", "paragraph"), "data": row.get("data", {}),
                "search_text": row["text"], "locator": row.get("locator", {}),
                "content_sha256": row.get("content_sha256")} for row in rows]
            outline = build_document_sections(blocks, structure_version=structure_version)
            page["source_outline"] = outline
            requested_ids = set((sections or {}).get(pid, ()))
            chosen_sections = [section for section in outline if section["section_id"] in requested_ids]
            chosen_sections += select_sections_from_outline(outline, set((anchors or {}).get(pid, ())))
            allow_parent = expand_parents and not requested_ids
            if expand_dependencies or allow_parent:
                from .source_sections import expand_reading_sections
                purpose = ("parent_and_dependencies" if expand_dependencies and allow_parent else
                           "parent" if allow_parent else "dependencies")
                expansion = expand_reading_sections(outline, chosen_sections, blocks=blocks, purpose=purpose)
                chosen_sections = expansion["sections"]
                page["reading_dependencies"] = expansion["references"]
                page["reading_dependency_warnings"] = expansion["warnings"]
            # A genuinely unsegmented document remains a complete readable unit.
            # A large structured source with no locator instead exposes its outline.
            if not chosen_sections and len(outline) == 1:
                chosen_sections = outline
            chosen_sections = list({section["section_id"]: section for section in chosen_sections}.values())
            ids = {bid for section in chosen_sections for bid in section["block_ids"]}
            # A leaf clause can depend on its parent's introductory conditions.
            # Keep each ancestor's heading + lead-in, not every sibling chapter.
            ordinals = {row["block_id"]: row["ordinal"] for row in rows}
            for chosen_section in chosen_sections:
                for parent in outline:
                    if parent["start_ordinal"] >= chosen_section["start_ordinal"] \
                            or parent["end_ordinal"] < chosen_section["end_ordinal"]:
                        continue
                    child_starts = [section["start_ordinal"] for section in outline
                        if parent["start_ordinal"] < section["start_ordinal"] <= parent["end_ordinal"]
                        and section["end_ordinal"] <= parent["end_ordinal"]]
                    if child_starts:
                        first_child = min(child_starts)
                        ids.update(bid for bid in parent["block_ids"] if ordinals[bid] < first_child)
            chosen = [row for row in rows if row["block_id"] in ids]
            if not chosen:
                outlines.append(pid)
                continue
        for row in chosen:
            if row["block_id"] in existing:
                continue
            row["evidence_id"] = f"E{next_evidence}"
            next_evidence += 1
            existing[row["block_id"]] = row
            fresh.append(row)
        page["records"] = sorted(existing.values(), key=lambda row: (row["ordinal"], row["block_id"]))
        page["characters"] = sum(len(row["text"]) for row in page["records"])
        page["body_loaded"] = bool(page["records"])
        page["full_text_loaded"] = len(page["records"]) == len(rows)
        page["read_scope"] = "full" if page["full_text_loaded"] else "sections"
        previous = {section["section_id"]: section for section in page.get("read_sections", [])}
        previous.update({section["section_id"]: section for section in chosen_sections})
        page["read_sections"] = list(previous.values())
    return fresh, unavailable, next_evidence, outlines


def outline_text(pages, ids):
    lines = [("以下来源尚未指定可核验的具体章节，正文没有自动展开成全册。请选择 READ_SECTION W编号 章节编号；"
              "只有确需全册阅读时才用 READ_FULL W编号。")]
    for pid in ids:
        page = pages[pid]
        lines.append(f"\n{pid} | {page['title']} | 完整来源共 {page['block_count']} 块")
        for section in page.get("source_outline", []):
            parents = " / ".join(section.get("parent_titles", []))
            lines.append(f"{section['section_id']} | {parents} / {section['title']} | {len(section['block_ids'])} 块")
    return "\n".join(lines)
