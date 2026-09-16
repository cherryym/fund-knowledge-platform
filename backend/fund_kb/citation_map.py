"""Map public answer spans to frozen evidence, independently from generation.

This is association/integrity telemetry, NOT semantic entailment certification.
Never rewrites prose, rejects an answer, invokes a model, or fabricates sources.
"""
from __future__ import annotations

import hashlib
import re

from .wiki_answer_content import _reference_ids


def public_citation_map(markdown, records):
    available = {}
    for row in records:
        eid = row.get("evidence_id")
        if isinstance(eid, str) and re.fullmatch(r"E[1-9]\d*", eid):
            available.setdefault(eid, []).append(row)
    paragraphs, issues = [], set()
    for match in re.finditer(r"[^\n]+(?:\n(?!\s*\n|[|#>\-*]).+)*", markdown):
        text = match[0]
        if not text.strip() or re.fullmatch(r"\s*[#|: \-]+\s*", text):
            continue
        ids, _ = _reference_ids(text, available, issues.add)
        sources = []
        for eid in ids:
            rows = available[eid]
            signatures = {(r.get("resource_id"), r.get("version_id"), r.get("block_id"), r.get("content_sha256")) for r in rows}
            if len(signatures) != 1:
                issues.add("AMBIGUOUS_EVIDENCE_ID")
                continue
            r = rows[0]
            sources.append({"evidence_id": eid, **{k: r.get(k) for k in
                ("resource_id", "version_id", "block_id", "content_sha256")}})
        paragraphs.append({"start": match.start(), "end": match.end(),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "sources": sources, "independent_source_versions": len({s["version_id"] for s in sources}),
            "association": "SOURCE_LINKED" if sources else "UNLINKED",
            "support_verification": "NOT_EVALUATED"})
    return {"schema_version": 1, "paragraphs": paragraphs, "integrity_warnings": sorted(issues),
            "linked_paragraphs": sum(bool(p["sources"]) for p in paragraphs),
            "source_versions": len({s["version_id"] for p in paragraphs for s in p["sources"]}),
            "semantic_entailment": "NOT_EVALUATED", "answer_rewritten": False}
