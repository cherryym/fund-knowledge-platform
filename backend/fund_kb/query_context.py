"""Compact navigation packaging; every selected original text and E ID remains."""
from __future__ import annotations

import re


def graph_query_text(question):
    """Tokenized matching view only; keep the original user question untouched."""
    from .retrieval import query_words
    parts = re.split(r"[、，,；;。！？!?\n]|以及|同时|分别|[和与及]|\band\b", question, flags=re.IGNORECASE)
    return "；".join(" ".join(query_words(p)) for p in parts if p.strip()) or question


def compact_catalog(pages):
    from .source_authority import describe
    return "\n".join(f"{pid} | {'知识页' if p['kind']=='knowledge' else '来源文档'} | {p['title']} | "
        f"{p.get('block_count', 0)}块，正文未读" + (" | " + describe(p) if p.get("source_authority") else "")
        for pid, p in pages.items())


def compact_evidence(pages, requested, *, used_edges=()):
    from .source_authority import describe
    if not any(pages[pid].get("records") for pid in requested):
        return ""
    selected = set(requested)
    lines = ["以下是本轮实际读取的完整知识页和完整原文小节；仅这些E编号可用于引用。"]
    paths = []
    for edge in used_edges:
        if edge.get("source") in selected and edge.get("target") in selected:
            label = f"{edge['source']} —{edge.get('type', 'NAVIGATION')}→ {edge['target']}"
            if edge.get("verification_status") == "PROPOSED" or edge.get("origin") == "proposed":
                label += "（待核验关系，仅供比较）"
            if label not in paths:
                paths.append(label)
    if paths:
        lines.append("程序阅读路径（只表示导航，不证明业务适用性）：" + "；".join(paths))
    for pid in dict.fromkeys(requested):
        p = pages[pid]
        if not p.get("records"):
            continue
        kind = "知识页" if p["kind"] == "knowledge" else "来源文档"
        lines.append(f"\n## {pid} | {kind} | {p['title']}\n状态：{p['state']}；效力：{p['legal_status']}")
        if p.get("source_authority"):
            lines.append(describe(p))
        if p.get("full_text_loaded"):
            lines.append("阅读范围：全文。")
        else:
            lines.append(f"阅读范围：完整相关小节，{len(p['records'])}/{p['block_count']}原块；其余原文仍可READ_SECTION/READ_FULL。")
        starts = {s["block_ids"][0]: s for s in p.get("read_sections", []) if s.get("block_ids")}
        for row in p["records"]:
            if row["block_id"] in starts:
                section = starts[row["block_id"]]
                lines.append("小节：" + " / ".join([*section.get("parent_titles", []), section["title"]]))
            qualifier = " [适用条件不匹配，仅供对照]" if row.get("applicability_match") is False else ""
            lines.append(f"[{row['evidence_id']}]{qualifier}\n{row['text']}")
    return "\n".join(lines)
