"""Central-dispatcher extension for reviewable Wiki maintenance.

Main integration registers PATHS/SCHEMAS/HANDLERS and READ_ONLY_OPERATIONS. No
standalone bypass routes; authentication, CSRF, idempotency and transactions
remain owned by api.py.
"""
from __future__ import annotations

from functools import wraps

from . import services as svc
from . import wiki_maintenance as maintenance
from .projection_read import projection_read

UUID = {"type": "string", "format": "uuid"}
TEXT = {"type": "string"}
NULL_TEXT = {"type": ["string", "null"]}
NULL_UUID = {"type": ["string", "null"], "format": "uuid"}
REASON = {"type": "string", "minLength": 1, "maxLength": 2000}
LABEL = {"type": "string", "minLength": 1, "maxLength": 300}
ALIASES = {"type": "array", "uniqueItems": True, "items": LABEL}
IDS = {"type": "array", "minItems": 1, "uniqueItems": True, "items": UUID}


def obj(properties, required=None):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(properties) if required is None else required}


ENTRY = obj({"resource_id": UUID, "space_id": UUID, "version_id": UUID, "title": TEXT,
    "canonical_key": NULL_TEXT, "aliases": ALIASES, "canonical_resource_id": NULL_UUID,
    "canonical_available": {"type": "boolean"}, "is_canonical": {"type": "boolean"},
    "revision": {"type": "integer", "minimum": 0}, "can_edit": {"type": "boolean"}, "etag": TEXT, "notice": TEXT})
SOURCE_CHANGE = obj({"source_resource_id": UUID, "title": TEXT, "old_version_id": UUID,
    "old_version_no": {"type": "integer", "minimum": 1}, "new_version_id": UUID,
    "new_version_no": {"type": "integer", "minimum": 1}, "new_state": TEXT,
    "change_type": {"const": "NEW_SOURCE_VERSION"}, "review_required": {"const": True}})
REVIEW = obj({"decision": {"enum": ["ACCEPT", "REJECT"]}, "comment": TEXT, "reviewed_by": UUID,
    "reviewed_at": {"type": "string", "format": "date-time"}, "snapshot_digest": TEXT,
    "business_verification": {"const": "NOT_EVALUATED"}})
RESOLUTION = obj({"comment": TEXT, "resolved_by": UUID, "resolved_at": {"type": "string", "format": "date-time"},
    "snapshot_digest": TEXT, "business_verification": {"const": "NOT_EVALUATED"}})
APPLIED = {"anyOf": [{"type": "null"}, obj({"action": {"const": "REVISION_DRAFT_CREATED"},
    "resource_id": UUID, "base_version_id": UUID, "new_version_id": UUID, "state": {"const": "DRAFT"},
    "requires_edit": {"const": True}, "original_preserved": {"const": True}, "compiled_candidate_applied": {"type": "boolean"}}),
    obj({"action": {"const": "CANONICAL_NAVIGATION_MAPPED"}, "canonical_resource_id": UUID,
         "mapped_resource_ids": IDS, "original_preserved": {"const": True}}),
    obj({"action": {"const": "CONFLICT_RECORDED"}, "formal_evidence_allowed": {"const": False}})]}
PROPOSAL = obj({"id": UUID, "space_id": UUID, "kind": {"enum": list(maintenance.KINDS)},
    "status": {"enum": list(maintenance.STATUSES)}, "resource_ids": IDS,
    "entries": {"type": "array", "items": obj({"resource_id": UUID, "title": TEXT, "version_id": UUID})},
    "target_resource_id": NULL_UUID, "draft_title": NULL_TEXT, "reason": TEXT,
    "origin": {"enum": ["HUMAN", "SCAN", "COMPILER"]},
    "verification_status": {"enum": ["HUMAN_PROPOSAL", "HEURISTIC_SUGGESTION", "COMPILED_DRAFT_UNVERIFIED"]},
    "detection": {"anyOf": [{"type": "null"}, obj({"methods": {"type": "array", "items": TEXT},
        "title_similarity": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "title_similarity_threshold": {"type": "number", "minimum": 0, "maximum": 1}, "body_sha256": NULL_TEXT})]},
    "created_at": {"type": "string", "format": "date-time"}, "created_by": UUID,
    "revision": {"type": "integer", "minimum": 1}, "snapshot_current": {"type": "boolean"},
    "snapshot_digest": TEXT, "source_changes": {"type": "array", "items": SOURCE_CHANGE},
    "review": {"anyOf": [REVIEW, {"type": "null"}]}, "result": APPLIED,
    "conflict_state": {"enum": [None, "OPEN", "RESOLVED"]},
    "resolution": {"anyOf": [RESOLUTION, {"type": "null"}]},
    "can_review": {"type": "boolean"}, "etag": TEXT, "notice": TEXT,
    "has_compiled_candidate": {"type": "boolean"}, "candidate_block_count": {"type": "integer", "minimum": 0},
    "application_blockers": {"type": "array", "items": TEXT}})
PROPOSAL["properties"]["compiled_revision"] = {"type": ["object", "null"]}
SCHEMAS = {
    "WikiEntryMaintenance": ENTRY,
    "WikiEntryMaintenanceInput": obj({"canonical_key": {"type": ["string", "null"], "minLength": 1, "maxLength": 300},
                                       "aliases": ALIASES, "reason": REASON}),
    "WikiMaintenanceResolve": obj({"resource_id": UUID, "version_id": UUID, "title": TEXT,
                                   "matched_resource_ids": IDS, "navigation_only": {"const": True}}),
    "WikiMaintenanceProposal": PROPOSAL,
    "WikiMaintenanceProposalList": obj({"space_id": UUID, "items": {"type": "array", "items": PROPOSAL},
                                        "total": {"type": "integer", "minimum": 0}, "truncated": {"const": False}, "notice": TEXT}),
    "WikiMaintenanceProposalInput": obj({"space_id": UUID, "kind": {"enum": list(maintenance.KINDS)},
        "resource_ids": IDS, "reason": REASON, "target_resource_id": UUID, "draft_title": LABEL},
        ["space_id", "kind", "resource_ids", "reason"]),
    "WikiMaintenanceReviewInput": obj({"decision": {"enum": ["ACCEPT", "REJECT"]}, "comment": REASON}),
    "WikiMaintenanceConflictResolutionInput": obj({"comment": REASON}),
    "WikiMaintenanceScanInput": obj({"space_id": UUID, "resource_ids": IDS,
        "kinds": {"type": "array", "minItems": 1, "uniqueItems": True,
                  "items": {"enum": ["duplicates", "source_changes"]}}}, ["space_id"]),
    "WikiMaintenanceScan": obj({"id": UUID, "space_id": UUID, "resource_count": {"type": "integer", "minimum": 0},
        "created_count": {"type": "integer", "minimum": 0}, "existing_count": {"type": "integer", "minimum": 0},
        "items": {"type": "array", "items": PROPOSAL}, "truncated": {"const": False},
        "model_invoked": {"const": False}, "verification_status": {"const": "HEURISTIC_SUGGESTION"}, "notice": TEXT}),
}


def _param(name, schema=TEXT, required=False, location="query"):
    return {"name": name, "in": location, "required": required, "schema": schema}


def _operation(operation, response, *, status=200, params=(), body=None):
    result = {"operationId": operation, "tags": ["Wiki维护"], "parameters": list(params),
        "responses": {str(status): {"description": "成功", "content": {"application/json": {
            "schema": {"$ref": f"#/components/schemas/{response}"}}}}}}
    if body:
        result["security"] = [{"cookieAuth": [], "csrf": []}]
        result["parameters"].append({"$ref": "#/components/parameters/Idempotency"})
        result["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": {"$ref": f"#/components/schemas/{body}"}}}}
    return result


ID = _param("id", UUID, True, "path")
SPACE = _param("space_id", UUID, True)
ETAG = _param("If-Match", TEXT, True, "header")
PATHS = {
    "/wiki/entries/{id}/maintenance": {
        "get": _operation("getWikiEntryMaintenance", "WikiEntryMaintenance", params=[ID]),
        "put": _operation("saveWikiEntryMaintenance", "WikiEntryMaintenance", params=[ID, ETAG], body="WikiEntryMaintenanceInput")},
    "/wiki/maintenance/resolve": {"get": _operation("resolveWikiMaintenanceAlias", "WikiMaintenanceResolve",
        params=[SPACE, _param("title", LABEL, True)])},
    "/wiki/maintenance/proposals": {
        "get": _operation("listWikiMaintenanceProposals", "WikiMaintenanceProposalList", params=[SPACE,
            _param("kind", {"enum": list(maintenance.KINDS)}), _param("status", {"enum": list(maintenance.STATUSES)}),
            _param("resource_id", UUID)]),
        "post": _operation("createWikiMaintenanceProposal", "WikiMaintenanceProposal", status=201,
                           body="WikiMaintenanceProposalInput")},
    "/wiki/maintenance/proposals/{id}": {"get": _operation("getWikiMaintenanceProposal", "WikiMaintenanceProposal",
        params=[ID, _param("include_candidate", {"type": "boolean", "default": False})])},
    "/wiki/maintenance/scans": {"post": _operation("scanWikiMaintenance", "WikiMaintenanceScan", body="WikiMaintenanceScanInput")},
    "/wiki/maintenance/proposals/{id}/review": {"post": _operation("reviewWikiMaintenanceProposal", "WikiMaintenanceProposal",
        params=[ID, ETAG], body="WikiMaintenanceReviewInput")},
    "/wiki/maintenance/proposals/{id}/resolve": {"post": _operation("resolveWikiMaintenanceConflict", "WikiMaintenanceProposal",
        params=[ID, ETAG], body="WikiMaintenanceConflictResolutionInput")},
}


def _read(handler):
    @wraps(handler)
    def run(ctx):
        with projection_read(ctx.db):
            return handler(ctx)
    return run


@_read
def get_entry(ctx):
    body = maintenance.entry_metadata(ctx.db, ctx.user, ctx.id)
    return svc.Result(body, headers={"ETag": body["etag"]})


@_read
def get_proposal(ctx):
    body = maintenance.proposal_view(ctx.db, ctx.user, ctx.id, include_candidate=ctx.query.get("include_candidate", False))
    return svc.Result(body, headers={"ETag": body["etag"]})


@_read
def resolve_alias(ctx):
    return svc.Result(maintenance.resolve_alias(ctx.db, ctx.user, ctx.query["space_id"], ctx.query["title"]))


HANDLERS = {
    "getWikiEntryMaintenance": get_entry, "saveWikiEntryMaintenance": maintenance.save_entry,
    "resolveWikiMaintenanceAlias": resolve_alias, "listWikiMaintenanceProposals": _read(maintenance.list_proposals),
    "getWikiMaintenanceProposal": get_proposal, "createWikiMaintenanceProposal": maintenance.create_proposal,
    "scanWikiMaintenance": maintenance.scan, "reviewWikiMaintenanceProposal": maintenance.review_proposal,
    "resolveWikiMaintenanceConflict": maintenance.resolve_conflict,
}
READ_ONLY_OPERATIONS = frozenset({"getWikiEntryMaintenance", "resolveWikiMaintenanceAlias",
                                  "listWikiMaintenanceProposals", "getWikiMaintenanceProposal"})


def replay_authority(ctx, cached):
    if ctx.operation not in HANDLERS:
        return False
    return maintenance.replay_authority(ctx, cached)
