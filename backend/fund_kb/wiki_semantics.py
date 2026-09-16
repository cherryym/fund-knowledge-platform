"""Source-bound proposed semantic edges, separate from authoritative rule dependencies.

An LLM relationship must not become a publication/dependency rule. Immutable
proposal receipts project into the graph only while both endpoints, reference
context and every source snapshot remain readable and unchanged.
"""
from __future__ import annotations

import copy
import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from . import models as m
from . import services as svc
from .ingestion import text_sha256

ROLES = ("concept", "asset", "rule", "method", "parameter", "condition", "exception", "procedure")
RELATIONS = ("EXPLAINS", "APPLIES_TO", "REQUIRES", "EXCEPTION_OF", "DEPENDS_ON")
MODES = ("knowledge_points", "relations")


def node_role(resource):
    return next((t.split(":", 1)[1] for t in resource.tags or [] if t.startswith("node-role:")
                 and t.split(":", 1)[1] in ROLES), None)


def output_schema(base, mode):
    schema = copy.deepcopy(base)
    page = schema["properties"]["pages"]["items"]
    page["required"].append("node_role")
    page["properties"]["node_role"] = {"enum": list(ROLES)}
    schema["required"] += ["relations", "source_dispositions"]
    if mode == "relations":
        schema["properties"]["pages"]["maxItems"] = 0
    title = {"type": "string", "minLength": 1, "maxLength": 200}
    schema["properties"]["relations"] = {"type": "array", "maxItems": 36, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["source_title", "target_title", "relation_type", "evidence_ids", "explanation"],
        "properties": {"source_title": title, "target_title": title, "relation_type": {"enum": list(RELATIONS)},
            "evidence_ids": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True, "items": title},
            "explanation": {"type": "string", "minLength": 4, "maxLength": 240}}}}
    schema["properties"]["source_dispositions"] = {"type": "array", "maxItems": 32, "items": {
        "type": "object", "additionalProperties": False, "required": ["evidence_id", "disposition", "reason"],
        "properties": {"evidence_id": title,
            "disposition": {"enum": ["EXTRACTED", "SUPPORTING", "NO_REUSABLE_POINT", "NEEDS_REVIEW"]},
            "reason": {"type": "string", "minLength": 2, "maxLength": 200}}}}
    return schema


def references(db, user, space_id, ids, *, expected=None, atomic=False):
    from . import wiki

    snapshots, items = [], []
    for rid in ids:
        resource = svc.resource_access(db, user, rid)
        version = wiki._visible_version(db, user, resource, {})
        if resource.space_id != space_id or resource.kind != "knowledge" or version is None:
            raise wiki.WikiBuildError("WIKI_REFERENCE_NOT_VISIBLE")
        provenance = wiki._policy(db, f"wiki-provenance:{rid}")
        if atomic and provenance and provenance.config.get("reference_snapshot"):
            # Keep generative reference ancestry shallow. Cross-link passes may
            # examine all atomic nodes without creating another generation tier.
            raise wiki.WikiBuildError("WIKI_REFERENCE_USE_TOPIC_ENTRY")
        snapshots.append({"resource_id": rid, "version_id": version.id,
                          "content_sha256": wiki._checked_hash(db, version), "access_epoch": resource.access_epoch})
        body = "\n".join(b.search_text for b in wiki._blocks(db, version) or [] if b.block_type != "warning")
        items.append({"title": version.title, "node_role": node_role(resource),
                      "summary": body[:100] if not atomic else "专题入口（只用于导航，不作为事实证据）"})
    if expected is not None and svc.digest(snapshots) != svc.digest(expected):
        raise wiki.WikiBuildError("WIKI_REFERENCE_CHANGED")
    return snapshots, items


def check_references(db, user, snapshots):
    from . import wiki

    for snap in snapshots:
        version = svc.version_access(db, user, snap["version_id"])
        # version_access already performs resource and full dependency access.
        resource = db.get(m.Resource, version.resource_id)
        if (resource.suspended or resource.deleted_at or version.resource_id != resource.id
                or resource.id != snap["resource_id"]
                or resource.access_epoch != snap["access_epoch"]
                or wiki._checked_hash(db, version) != snap["content_sha256"]):
            raise wiki.WikiBuildError("WIKI_REFERENCE_CHANGED")


def check_source_snapshots(db, user, snapshots):
    from . import wiki

    for snap in snapshots:
        version = svc.version_access(db, user, snap["version_id"])
        resource = db.get(m.Resource, version.resource_id)
        blob = db.get(m.Blob, version.source_blob_id) if version.source_blob_id else None
        if (not resource or resource.id != snap["resource_id"] or resource.deleted_at or resource.suspended
                or resource.access_epoch != snap["access_epoch"] or not blob or blob.scan_state != "CLEAN"
                or blob.sha256 != snap.get("source_blob_sha256")
                or (version.content_sha256 is not None and version.content_sha256 != snap["content_sha256"])
                or wiki._checked_hash(db, version) != snap["content_sha256"]):
            raise wiki.WikiBuildError("WIKI_SOURCE_CHANGED")


def validate_output(parsed, selected, reference_titles):
    from . import wiki

    available = {wiki._norm(t) for t in reference_titles} | {wiki._norm(p["title"]) for p in parsed["pages"]}
    cited = {eid for page in parsed["pages"] for b in page["blocks"] for eid in b["evidence_ids"]}
    for relation in parsed["relations"]:
        for key in ("source_title", "target_title", "explanation"):
            wiki._safe_text(relation[key])
        if (wiki._norm(relation["source_title"]) not in available
                or wiki._norm(relation["target_title"]) not in available
                or wiki._norm(relation["source_title"]) == wiki._norm(relation["target_title"])
                or not set(relation["evidence_ids"]).issubset(selected)):
            raise wiki.WikiBuildError("WIKI_SEMANTIC_ENDPOINT_OR_EVIDENCE_INVALID")
        cited.update(relation["evidence_ids"])
    dispositions = parsed["source_dispositions"]
    ids = [d["evidence_id"] for d in dispositions]
    if len(ids) != len(set(ids)) or set(ids) != set(selected):
        raise wiki.WikiBuildError("WIKI_SOURCE_DISPOSITION_INCOMPLETE")
    for d in dispositions:
        wiki._safe_text(d["reason"])
        if d["disposition"] == "EXTRACTED" and d["evidence_id"] not in cited:
            raise wiki.WikiBuildError("WIKI_EXTRACTED_WITHOUT_EVIDENCE")


def persist(db, user, payload, job_id, parsed, selected, created_ids):
    from . import wiki

    allowed_ids = set(created_ids) | set(payload.get("reference_resource_ids", []))
    by_title = {}
    for rid in allowed_ids:
        r = svc.resource_access(db, user, rid)
        v = wiki._visible_version(db, user, r, {})
        if v:
            by_title.setdefault(wiki._norm(v.title), []).append((r, v))
    proposals, skipped, used = [], [], set()
    for edge in parsed.get("relations", []):
        left = by_title.get(wiki._norm(edge["source_title"]), [])
        right = by_title.get(wiki._norm(edge["target_title"]), [])
        if len(left) != 1 or len(right) != 1:
            skipped.append({"reason": "ENDPOINT_NOT_CREATED_OR_AMBIGUOUS", **edge})
            continue
        endpoints = []
        for r, v in (left[0], right[0]):
            endpoints.append({"resource_id": r.id, "version_id": v.id,
                "content_sha256": wiki._checked_hash(db, v), "access_epoch": r.access_epoch})
        anchors = []
        for eid in edge["evidence_ids"]:
            for record in wiki._members(selected[eid]):
                anchors.append({"resource_id": record["resource_id"], "version_id": record["version_id"],
                    "block_id": record["block_id"], "char_start": record.get("char_start", 0),
                    "char_end": record.get("char_end", len(record["text"])),
                    "excerpt_sha256": text_sha256(record["text"])})
            used.add(eid)
        proposals.append({"endpoints": endpoints, "relation_type": edge["relation_type"], "anchors": anchors,
                          "explanation": edge["explanation"], "verification_status": "PROPOSED"})
    if proposals:
        db.add(m.RuntimePolicy(id=svc.uid(), name=f"wiki-semantic:{payload['space_id']}:{job_id}", updated_by=user.id,
            config={"space_id": payload["space_id"], "owner_id": user.id, "job_id": job_id,
                    "source_snapshot": copy.deepcopy(payload["source_snapshot"]),
                    "reference_snapshot": copy.deepcopy(payload.get("reference_snapshot", [])), "proposals": proposals}))
        db.add(m.AuditEvent(id=svc.uid(), actor_id=user.id, action="wiki.semantic.proposed", object_type="Job",
            object_id=job_id, outcome="SUCCESS", trace_id=job_id,
            details={"proposals": len(proposals), "formal_evidence_allowed": False}))
    return len(proposals), skipped, used


def visible_edges(db, user, pages):
    from . import wiki

    edges = {}
    for sid in {p["resource"].space_id for p in pages.values()}:
        for policy in db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like(f"wiki-semantic:{sid}:%"))):
            config = policy.config
            if config.get("space_id") != sid:
                continue
            try:
                check_source_snapshots(db, user, config["source_snapshot"])
                check_references(db, user, config.get("reference_snapshot", []))
            except (svc.APIError, wiki.WikiBuildError, KeyError):
                continue
            for proposal in config.get("proposals", []):
                a, b = proposal["endpoints"]
                if a["resource_id"] not in pages or b["resource_id"] not in pages:
                    continue
                if any(pages[s["resource_id"]]["version"].id != s["version_id"]
                       or pages[s["resource_id"]]["resource"].access_epoch != s["access_epoch"]
                       or wiki._checked_hash(db, pages[s["resource_id"]]["version"]) != s["content_sha256"]
                       for s in (a, b)):
                    continue
                key = [a["resource_id"], a["version_id"], b["resource_id"], b["version_id"], proposal["relation_type"], "semantic"]
                eid = str(uuid5(NAMESPACE_URL, json.dumps(key)))
                item = edges.setdefault(eid, {"id": eid, "source": a["resource_id"], "target": b["resource_id"],
                    "type": proposal["relation_type"], "origin": "semantic", "state": "DRAFT",
                    "verification_status": "PROPOSED", "evidence_count": 0, "explanation": proposal["explanation"]})
                item["evidence_count"] += len(proposal["anchors"])
    return list(edges.values())
