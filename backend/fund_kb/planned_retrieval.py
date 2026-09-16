"""Plan-driven local lookup, called only AFTER the source-free model analysis.

SQL matching finds possible versions, never establishes read authority. The
existing reference admission and full source-lineage checks remain mandatory.
"""
import re
import unicodedata

from sqlalchemy import or_, select

from . import models as m, services as svc
from .reference_evidence import reference_evidence

_GENERIC = {"基金", "证券", "估值", "处理", "如何", "应该", "什么", "资料", "规则", "知识", "问题", "相关"}


def search_terms(question, plan):
    values = list(plan.get("focus_terms", []))
    for query in plan.get("search_queries", []):
        values.extend(re.split(r"[\s,，、;；：:]+", query))
    # Preserve the user's actual event even if a model-generated synonym misses.
    from .retrieval import tokenize
    values.extend(tokenize(question))
    result = []
    for raw in values:
        value = unicodedata.normalize("NFKC", str(raw)).strip()
        if 2 <= len(value) <= 160 and value not in _GENERIC and value not in result:
            result.append(value)
        if len(result) == 24:
            break
    return result


def planned_reference_evidence(db, user, space_id, context, question, plan):
    terms = search_terms(question, plan)
    if not terms:
        return [], {"plan_used": True, "authorized_versions": 0, "loaded_blocks": 0}
    predicates = [column.contains(term, autoescape=True) for term in terms
                  for column in (m.ResourceVersion.title, m.ContentBlock.search_text)]
    statement = (select(m.ResourceVersion.id).join(m.Resource, m.Resource.id == m.ResourceVersion.resource_id)
        .join(m.ContentBlock, m.ContentBlock.version_id == m.ResourceVersion.id)
        .where(m.Resource.space_id == space_id, m.Resource.deleted_at.is_(None),
               m.Resource.suspended.is_(False), or_(*predicates)).distinct())
    possible = set(db.scalars(statement))
    # No limit is applied before ACL checks: hidden high-scoring records cannot
    # starve a lower-scoring authorized source out of the candidate set.
    roots = reference_evidence(db, user, space_id, context, version_ids=possible)
    root_versions = {row["version_id"] for row in roots}
    dependencies, visited = set(), set()
    frontier = set(root_versions)
    for _ in range(8):
        if not frontier:
            break
        following = set()
        for vid in frontier - visited:
            version = svc.version_access(db, user, vid)
            following.update(svc.dependency_ids(db, version))
        visited.update(frontier)
        dependencies.update(following)
        frontier = following - visited
    extra = reference_evidence(db, user, space_id, context, version_ids=dependencies - root_versions)
    records = list({(row["version_id"], row["block_id"]): row for row in roots + extra}.values())
    pairs = {(row["version_id"], row["block_id"]) for row in records}
    citations = {}
    versions = sorted({vid for vid, _ in pairs})
    for start in range(0, len(versions), 400):
        links = db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id.in_(versions[start:start+400])))
        for link in links:
            key = (link.from_version_id, link.from_block_id)
            target = (link.to_version_id, link.to_block_id)
            if key in pairs and target in pairs:
                citations.setdefault(key, []).append({"version_id": target[0], "block_id": target[1]})
    for row in records:
        row["source_citations"] = citations.get((row["version_id"], row["block_id"]), [])
    return records, {"plan_used": True, "query_count": len(plan.get("search_queries", [])),
        "authorized_versions": len(versions), "loaded_blocks": len(records)}
