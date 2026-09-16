"""Schema extension; registration, transactions, CSRF and idempotency live in api.py."""
from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy import select

from . import documents as docs
from . import models as m
from . import services as svc

UUID = {"type": "string", "format": "uuid"}
TEXT = {"type": "string"}
PATH = {"type": "string", "minLength": 1, "maxLength": 200}
REVISION = {"type": "integer", "minimum": 0}


def obj(properties, required=None):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(properties) if required is None else required}


SCHEMAS = {
    "DocumentSourceProperties": obj({"source_url": {"type": ["string", "null"], "format": "uri", "maxLength": 2000}}),
    "DocumentSourcePropertiesResult": obj({"version_id": UUID, "revision": REVISION,
        "source_url": {"type": ["string", "null"]}}),
    "DocumentTaxonomy": obj({"space_id": UUID, "revision": REVISION, "total_visible": REVISION,
        "can_manage": {"type": "boolean"}, "categories": {"type": "array", "items": obj({
            "path": TEXT, "name": TEXT, "parent_path": {"type": ["string", "null"]},
            "count": REVISION, "direct_count": REVISION, "protected": {"type": "boolean"}})}}),
    "DocumentCategoryInput": obj({"space_id": UUID, "path": PATH}),
    "DocumentCategoryRename": obj({"space_id": UUID, "path": PATH, "new_path": PATH}),
    "DocumentCreate": obj({"space_id": UUID, "name": docs.TEXT_SCHEMA, "category": PATH,
        "tags": {"type": "array", "maxItems": 100, "uniqueItems": True,
                 "items": {"type": "string", "minLength": 1, "maxLength": 100}}}, ["space_id", "name", "category"]),
    "DocumentResource": {"type": "object", "required": ["id", "revision", "kind", "category"],
        "properties": {"id": UUID, "revision": REVISION, "kind": {"const": "document"}, "category": TEXT,
            "latest_version_id": {"type": ["string", "null"], "format": "uuid"},
            "latest_version_revision": {"type": ["integer", "null"], "minimum": 1},
            "latest_version_no": {"type": ["integer", "null"], "minimum": 1},
            "latest_state": {"type": ["string", "null"]}}},
    "DocumentList": obj({"items": {"type": "array", "items": {"$ref": "#/components/schemas/DocumentResource"}},
                         "next_cursor": {"type": ["string", "null"]}}),
    "DocumentMoveInput": obj({"space_id": UUID, "category": PATH, "items": {"type": "array",
        "minItems": 1, "maxItems": docs.MAX_MOVE, "items": obj({"id": UUID, "revision": {"type": "integer", "minimum": 1}})}}),
    "DocumentMoveResult": obj({"items": {"type": "array", "items": obj({"id": UUID, "revision": REVISION, "category": TEXT})},
                               "taxonomy_revision": REVISION}),
    "GuidanceNormalizeInput": obj({"source_version_id": UUID, "model_selection": obj({
        "connection_id": UUID, "model_id": {"type": "string", "minLength": 1, "maxLength": 200}}),
        "consent": {"const": True}}),
    "GuidanceJob": obj({"id": UUID, "kind": {"const": "COMPILE"}, "state": TEXT, "stage": TEXT,
        "attempts": REVISION, "error_code": {"type": ["string", "null"]}, "result": {"type": ["object", "null"]}}),
    "GuidanceNormalization": obj({"job": {"$ref": "#/components/schemas/GuidanceJob"}, "revision": REVISION,
        "suggestion": {"type": ["object", "null"]}, "applied": {"type": ["object", "null"]}}),
    "GuidanceApplyInput": obj({"reviewed": {"const": True}, "title": docs.TEXT_SCHEMA,
        "blocks": docs.SUGGESTION_SCHEMA["properties"]["blocks"],
        "review_note": {"type": "string", "minLength": 1, "maxLength": 2000}}),
    "GuidanceApplied": obj({"resource_id": UUID, "version_id": UUID, "state": {"const": "DRAFT"}, "revision": REVISION}),
}


def param(name, schema=TEXT, required=False, location="query"):
    return {"name": name, "in": location, "required": required, "schema": schema}


SPACE = param("space_id", UUID, True)
ID = param("id", UUID, True, "path")
ETAG = param("If-Match", TEXT, True, "header")


def operation(name, response, *, params=(), body=None, status=200):
    spec = {"operationId": name, "tags": ["Documents"], "parameters": list(params), "responses": {
        str(status): {"description": "成功", "headers": {"ETag": {"schema": TEXT}},
                     "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{response}"}}}}}}
    if body:
        spec["security"] = [{"cookieAuth": [], "csrf": []}]
        spec["parameters"] += [ETAG, {"$ref": "#/components/parameters/Idempotency"}]
        spec["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": {"$ref": f"#/components/schemas/{body}"}}}}
    return spec


PATHS = {
    "/document-versions/{id}/source-properties": {"patch": operation("updateDocumentSourceProperties",
        "DocumentSourcePropertiesResult", params=[ID], body="DocumentSourceProperties")},
    "/documents/taxonomy": {"get": operation("getDocumentTaxonomy", "DocumentTaxonomy", params=[SPACE])},
    "/documents/categories": {
        "post": operation("createDocumentCategory", "DocumentTaxonomy", body="DocumentCategoryInput", status=201),
        "patch": operation("renameDocumentCategory", "DocumentTaxonomy", body="DocumentCategoryRename"),
        "delete": operation("deleteDocumentCategory", "DocumentTaxonomy", body="DocumentCategoryInput")},
    "/documents": {"get": operation("listDocuments", "DocumentList", params=[SPACE,
        param("category", PATH), param("q", {"type": "string", "maxLength": 300}),
        param("tag", {"type": "string", "maxLength": 100}),
        param("limit", {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}), param("cursor")]),
        "post": operation("createDocument", "DocumentResource", body="DocumentCreate", status=201)},
    "/documents/classification-moves": {"post": operation("moveDocumentClassification", "DocumentMoveResult", body="DocumentMoveInput")},
    "/documents/{id}/normalizations": {"post": operation("createGuidanceNormalization", "GuidanceJob", params=[ID],
                                                        body="GuidanceNormalizeInput", status=202)},
    "/document-normalizations/{id}": {"get": operation("getGuidanceNormalization", "GuidanceNormalization", params=[ID])},
    "/document-normalizations/{id}/apply": {"post": operation("applyGuidanceNormalization", "GuidanceApplied", params=[ID],
                                                           body="GuidanceApplyInput", status=201)},
}


def get_taxonomy(ctx):
    body = docs.taxonomy(ctx.db, ctx.user, ctx.query["space_id"])
    return svc.Result(body, headers={"ETag": f'"{body["revision"]}"'})


def source_properties(ctx):
    version = svc.require_draft(ctx)
    resource = svc.resource_access(ctx.db, ctx.user, version.resource_id, "edit")
    if resource.kind != "document":
        svc.fail(422, "DOCUMENT_REQUIRED", "此接口仅更新文档来源属性")
    if ctx.db.scalar(select(m.Job.id).where(m.Job.version_id == version.id,
        m.Job.kind == "SCAN_PARSE", m.Job.state.in_(["QUEUED", "RUNNING"]))):
        svc.fail(409, "PARSING_IN_PROGRESS", "请等待原件解析完成")
    url = ctx.data["source_url"]
    if url:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            svc.fail(422, "INVALID_SOURCE_URL", "来源链接必须为不含凭据的HTTP或HTTPS链接")
    # Source metadata must not retransmit or rewrite a multi-megabyte manuscript.
    svc.bump(ctx.db, version, source_url=url, source_verified=False, content_sha256=None)
    ctx.db.flush()
    version.content_sha256 = svc.check_frozen_hash(ctx.db, version)
    svc.audit(ctx, "version.updated", version, {"fields": ["source_url"]})
    return svc.tagged({"version_id": version.id, "revision": version.revision, "source_url": url}, version)


HANDLERS = {"getDocumentTaxonomy": get_taxonomy, "listDocuments": docs.list_documents,
    "updateDocumentSourceProperties": source_properties,
    "createDocument": docs.create_document, "moveDocumentClassification": docs.move_documents,
    "createDocumentCategory": lambda ctx: docs.mutate_category(ctx, "create"),
    "renameDocumentCategory": lambda ctx: docs.mutate_category(ctx, "rename"),
    "deleteDocumentCategory": lambda ctx: docs.mutate_category(ctx, "delete"),
    "createGuidanceNormalization": docs.queue_guidance_normalize,
    "getGuidanceNormalization": docs.get_normalization, "applyGuidanceNormalization": docs.apply_normalization}


def replay_authority(ctx, cached):
    """Mandatory extension guard: old idempotency receipts never confer authority."""
    if ctx.operation not in HANDLERS:
        return False
    body = cached.get("body") or {}
    if ctx.operation == "updateDocumentSourceProperties":
        version = svc.version_access(ctx.db, ctx.user, body.get("version_id"), "edit", dependencies=False)
        if version.revision != body.get("revision") or version.source_url != body.get("source_url") or not svc.can_edit_draft(ctx.db, ctx.user, version):
            svc.fail(409, "DOCUMENT_REPLAY_STATE_CHANGED", "来源属性或权限已变化")
    elif ctx.operation in {"createDocumentCategory", "renameDocumentCategory", "deleteDocumentCategory"}:
        svc.space_access(ctx.db, ctx.user, ctx.data["space_id"], "admin")
        if docs.taxonomy(ctx.db, ctx.user, ctx.data["space_id"]) != body:
            svc.fail(409, "DOCUMENT_REPLAY_STATE_CHANGED", "目录或可见范围已变化，请重新读取")
    elif ctx.operation == "createDocument":
        resource = svc.resource_access(ctx.db, ctx.user, body.get("id"), "edit")
        docs._readable(ctx.db, ctx.user, resource)
        if svc.resource_dict(ctx.db, resource, ctx.user) != body:
            svc.fail(409, "DOCUMENT_REPLAY_STATE_CHANGED", "文档状态已变化，请重新读取")
    elif ctx.operation == "moveDocumentClassification":
        svc.space_access(ctx.db, ctx.user, ctx.data["space_id"], "editor")
        for item in body.get("items", []):
            resource = svc.resource_access(ctx.db, ctx.user, item["id"], "edit")
            docs._readable(ctx.db, ctx.user, resource)
            if resource.revision != item["revision"] or resource.category != item["category"]:
                svc.fail(409, "DOCUMENT_REPLAY_STATE_CHANGED", "文档状态已变化，请重新读取")
    elif ctx.operation == "createGuidanceNormalization":
        job = ctx.db.get(m.Job, body.get("id"))
        if not job:
            svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
        docs.guard_guidance_job(ctx.db, ctx.user, job, ctx.settings)
        docs._frozen_source(ctx.db, ctx.user, job.payload, edit=True)
    elif ctx.operation == "applyGuidanceNormalization":
        job, receipt = docs._suggestion_access(ctx)
        docs._frozen_source(ctx.db, ctx.user, job.payload, edit=True)
        if not receipt or receipt.config.get("applied") != body:
            svc.fail(409, "GUIDANCE_REPLAY_STATE_CHANGED", "建议采纳状态已变化")
        version = svc.version_access(ctx.db, ctx.user, body["version_id"])
        if version.revision != body["revision"] or version.state != body["state"]:
            svc.fail(409, "GUIDANCE_REPLAY_STATE_CHANGED", "派生草稿已变化，请打开最新版本")
    return True
