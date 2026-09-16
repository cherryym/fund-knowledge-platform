"""Wiki operations for the central authenticated/CSRF/idempotent API dispatcher."""
from __future__ import annotations

from functools import wraps

from . import models as m
from . import services as svc
from . import wiki
from .projection_read import projection_read


def _project_read(handler):
    @wraps(handler)
    def run(ctx):
        with projection_read(ctx.db):
            return handler(ctx)
    return run

UUID_SCHEMA = {"type": "string", "format": "uuid"}
TEXT = {"type": "string"}
CATEGORY = {"type": "object", "additionalProperties": False, "required": ["path", "name", "parent_path", "count"],
            "properties": {"path": TEXT, "name": TEXT, "parent_path": {"type": ["string", "null"]},
                           "count": {"type": "integer", "minimum": 0}}}
SCHEMAS = {
    "WikiTaxonomy": {"type": "object", "additionalProperties": False,
        "required": ["space_id", "revision", "categories", "truncated"],
        "properties": {"space_id": UUID_SCHEMA, "revision": {"type": "integer", "minimum": 0},
                       "categories": {"type": "array", "items": CATEGORY}, "truncated": {"type": "boolean"}}},
    "WikiCategoryInput": {"type": "object", "additionalProperties": False, "required": ["space_id", "path"],
        "properties": {"space_id": UUID_SCHEMA, "path": {"type": "string", "minLength": 1, "maxLength": 200}}},
    "WikiCategoryRename": {"type": "object", "additionalProperties": False, "required": ["space_id", "path", "new_path"],
        "properties": {"space_id": UUID_SCHEMA, "path": {"type": "string", "minLength": 1, "maxLength": 200},
                       "new_path": {"type": "string", "minLength": 1, "maxLength": 200}}},
    "WikiBuildInput": {"type": "object", "additionalProperties": False,
        "required": ["space_id", "source_resource_ids", "model_selection", "consent"],
        "properties": {"space_id": UUID_SCHEMA, "source_mode": {"enum": ["published", "unverified_draft"]},
          "generation_brief": {"type": "string", "minLength": 1, "maxLength": 1000},
          "granularity": {"enum": ["topic", "knowledge_points", "relations"]},
          "compilation_type": {"enum": list(wiki.compilation.TYPES),
              "description": "显式类型启用完整结构校验；省略保留旧调用格式，topic默认专题、knowledge_points默认原子规则。"},
          "reference_resource_ids": {"type": "array", "maxItems": 24, "uniqueItems": True, "items": UUID_SCHEMA},
          "source_block_ids": {"type": "array", "minItems": 1, "maxItems": 100, "uniqueItems": True, "items": UUID_SCHEMA},
          "source_resource_ids": {"type": "array", "minItems": 1, "maxItems": wiki.MAX_SOURCES,
                                  "uniqueItems": True, "items": UUID_SCHEMA},
          "model_selection": {"type": "object", "additionalProperties": False, "required": ["connection_id", "model_id"],
                              "properties": {"connection_id": UUID_SCHEMA, "model_id": {"type": "string", "minLength": 1, "maxLength": 200}}},
          "max_pages": {"type": "integer", "minimum": 1, "maximum": wiki.MAX_PAGES, "default": 3},
          "consent": {"const": True}}},
    "WikiWorkspace": {"type": "object", "required": ["pages", "categories", "tags", "stats", "mode", "truncated"],
        "properties": {"pages": {"type": "array", "items": {"type": "object", "required": ["id", "name", "kind", "knowledge_type",
                       "category", "tags", "version_id", "version_no", "state", "excerpt", "updated_at", "link_count", "backlink_count"]}},
                       "categories": {"type": "array", "items": CATEGORY}, "tags": {"type": "array"},
                       "stats": {"type": "object"}, "mode": {"const": "wiki"}, "truncated": {"type": "boolean"}}},
    "WikiPageLinks": {"type": "object", "required": ["outgoing", "incoming", "sources", "unresolved", "truncated"],
        "properties": {**{key: {"type": "array", "items": {"type": "object", "required": ["id", "name", "kind", "version_id", "relation_type", "status"]}}
                             for key in ("outgoing", "incoming", "sources")},
                       "unresolved": {"type": "array", "items": {"type": "object", "required": ["title"]}},
                       "truncated": {"type": "boolean"}}},
    "WikiResolve": {"type": "object", "required": ["resource_id", "version_id", "title"],
                    "properties": {"resource_id": UUID_SCHEMA, "version_id": UUID_SCHEMA, "title": TEXT}},
    "WikiGraph": {"type": "object", "required": ["nodes", "edges", "truncated", "total_visible_nodes", "matched_visible_nodes"],
        "properties": {"nodes": {"type": "array",
                                 "items": {"type": "object", "required": ["id", "label", "kind", "knowledge_type", "category", "state", "version_id"]}},
                       "edges": {"type": "array",
                                 "items": {"type": "object", "required": ["id", "source", "target", "type", "origin", "state"]}},
                       "truncated": {"type": "boolean"}, "total_visible_nodes": {"type": "integer", "minimum": 0},
                       "matched_visible_nodes": {"type": "integer", "minimum": 0}}},
    "WikiBuildJob": {"type": "object", "required": ["id", "kind", "state", "stage", "attempts", "error_code", "result"],
                     "properties": {"id": UUID_SCHEMA, "kind": {"const": "COMPILE"}, "state": TEXT, "stage": TEXT,
                                    "attempts": {"type": "integer"}, "error_code": {"type": ["string", "null"]},
                                    "result": {"type": ["object", "null"]}}},
    "WikiCompilationSpecs": {"type": "object", "additionalProperties": False,
        "required": ["spec_version", "default_compilation_type", "types", "legacy_defaults", "granularity_is_independent",
                     "automatic_generation", "source_policy", "completeness_notice"],
        "properties": {"spec_version": TEXT, "default_compilation_type": {"enum": list(wiki.compilation.TYPES)},
            "types": {"type": "array", "minItems": 4, "maxItems": 4, "items": {
                "type": "object", "required": ["compilation_type", "label", "purpose", "spec_version", "sections",
                    "tokens_per_page", "max_output_tokens", "max_source_utf8_bytes", "max_input_utf8_bytes",
                    "no_body_length_cap", "requires_review"],
                "properties": {"compilation_type": {"enum": list(wiki.compilation.TYPES)}, "label": TEXT, "purpose": TEXT,
                    "spec_version": TEXT, "knowledge_type": {"type": ["string", "null"]},
                    "sections": {"type": "array", "items": {"type": "object", "required": ["key", "label", "requirement", "required"],
                        "properties": {"key": TEXT, "label": TEXT, "requirement": TEXT, "required": {"const": True}}}},
                    **{key: {"type": "integer", "minimum": 1} for key in
                       ("tokens_per_page", "max_output_tokens", "max_source_utf8_bytes", "max_input_utf8_bytes")},
                    "no_body_length_cap": {"const": True}, "requires_review": {"const": True}}}},
            "legacy_defaults": {"type": "object"}, "granularity_is_independent": {"const": True},
            "automatic_generation": {"const": False}, "source_policy": {"type": "object"}, "completeness_notice": TEXT}},
}


def _param(name, schema=TEXT, required=False, location="query"):
    return {"name": name, "in": location, "required": required, "schema": schema}


def _operation(operation, response, *, status=200, params=(), body=None):
    result = {"operationId": operation, "tags": ["Wiki"], "parameters": list(params),
              "responses": {str(status): {"description": "成功", "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{response}"}}}}}}
    if body:
        result["security"] = [{"cookieAuth": [], "csrf": []}]
        result["parameters"].append({"$ref": "#/components/parameters/Idempotency"})
        result["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{body}"}}}}
    return result


SPACE = _param("space_id", UUID_SCHEMA, True)
DATE = _param("business_date", {"type": "string", "format": "date"})
ETAG = _param("If-Match", TEXT, True, "header")
PATHS = {
    "/wiki/compilation-specs": {"get": _operation("getWikiCompilationSpecs", "WikiCompilationSpecs")},
    "/wiki/workspace": {"get": _operation("getWikiWorkspace", "WikiWorkspace", params=[SPACE, DATE,
        _param("hydrate", {"type": "boolean", "default": False}),
        *[_param(name, {"type": "string", "maxLength": 300}) for name in ("q", "category", "tag", "kind", "status")]])},
    "/wiki/pages/{id}/links": {"get": _operation("getWikiPageLinks", "WikiPageLinks", params=[_param("id", UUID_SCHEMA, True, "path"), DATE])},
    "/wiki/resolve": {"get": _operation("resolveWikiTitle", "WikiResolve", params=[SPACE, DATE, _param("title", {"type": "string", "minLength": 1, "maxLength": 300}, True)])},
    "/wiki/graph": {"get": _operation("getWikiGraph", "WikiGraph", params=[SPACE, DATE, _param("focus_id", UUID_SCHEMA),
        _param("depth", {"type": "integer", "minimum": 0, "maximum": 3, "default": 1}),
        _param("limit", {"type": "integer", "minimum": 1}),
        _param("node_role", {"enum": ["", *wiki.semantic.ROLES]}),
        _param("category", {"type": "string", "maxLength": 200}), _param("q", {"type": "string", "maxLength": 300})])},
    "/wiki/taxonomy": {"get": _operation("getWikiTaxonomy", "WikiTaxonomy", params=[SPACE])},
    "/wiki/categories": {
        "post": _operation("createWikiCategory", "WikiTaxonomy", status=201, body="WikiCategoryInput"),
        "patch": _operation("renameWikiCategory", "WikiTaxonomy", params=[ETAG], body="WikiCategoryRename"),
        "delete": _operation("deleteWikiCategory", "WikiTaxonomy", params=[ETAG], body="WikiCategoryInput")},
    "/wiki/builds": {"post": _operation("createWikiBuild", "WikiBuildJob", status=202, body="WikiBuildInput")},
}


def _context(ctx):
    return {"business_date": ctx.query.get("business_date") or svc.effective_date().isoformat()}


@_project_read
def get_workspace(ctx):
    return svc.Result(wiki.workspace(ctx.db, ctx.user, ctx.query["space_id"], context=_context(ctx),
                      hydrate=ctx.query.get("hydrate", False),
                      **{key: ctx.query.get(key, "") for key in ("q", "category", "tag", "kind", "status")}))


@_project_read
def get_links(ctx):
    return svc.Result(wiki.page_links(ctx.db, ctx.user, ctx.id, context=_context(ctx)))


@_project_read
def resolve(ctx):
    return svc.Result(wiki.resolve_title(ctx.db, ctx.user, ctx.query["space_id"], ctx.query["title"], context=_context(ctx)))


@_project_read
def get_graph(ctx):
    return svc.Result(wiki.graph(ctx.db, ctx.user, ctx.query["space_id"], context=_context(ctx),
                      **{key: value for key, value in ctx.query.items() if key in {"focus_id", "depth", "limit", "category", "q", "node_role"}}))


@_project_read
def get_taxonomy(ctx):
    result = wiki.taxonomy(ctx.db, ctx.user, ctx.query["space_id"])
    return svc.Result(result, headers={"ETag": f'"{result["revision"]}"'})


def create_category(ctx):
    return wiki.mutate_category(ctx, "create")


def rename_category(ctx):
    return wiki.mutate_category(ctx, "rename")


def delete_category(ctx):
    return wiki.mutate_category(ctx, "delete")


def create_build(ctx):
    try:
        return wiki.queue_build(ctx)
    except wiki.WikiBuildError as exc:
        svc.fail(503, exc.code, "Wiki模型配置或构建服务尚不可用")


def get_compilation_specs(ctx):
    # Static, authenticated metadata only: no provider, corpus, or credentials.
    return svc.Result(wiki.compilation.public_specs())


HANDLERS = {"getWikiWorkspace": get_workspace, "getWikiPageLinks": get_links, "resolveWikiTitle": resolve,
            "getWikiGraph": get_graph, "getWikiTaxonomy": get_taxonomy, "createWikiCategory": create_category,
            "renameWikiCategory": rename_category, "deleteWikiCategory": delete_category, "createWikiBuild": create_build,
            "getWikiCompilationSpecs": get_compilation_specs}


def replay_authority(ctx, cached):
    """Called by the central replay guard before returning cached mutation output."""
    if ctx.operation not in HANDLERS:
        return False
    if ctx.operation in {"createWikiCategory", "renameWikiCategory", "deleteWikiCategory"}:
        svc.space_access(ctx.db, ctx.user, ctx.data["space_id"], "admin")
        current = wiki.taxonomy(ctx.db, ctx.user, ctx.data["space_id"])
        if current != cached.get("body"):
            svc.fail(409, "WIKI_REPLAY_STATE_CHANGED", "分类或当前可见范围已变化，请重新读取目录")
    elif ctx.operation == "createWikiBuild":
        svc.space_access(ctx.db, ctx.user, ctx.data["space_id"], "editor")
        job = ctx.db.get(m.Job, (cached.get("body") or {}).get("id"))
        if not job or job.owner_id != ctx.user.id:
            svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
        _, snapshots = wiki.choose_build_sources(ctx.db, ctx.user, job.payload["space_id"], job.payload["source_resource_ids"],
            source_mode=job.payload.get("source_mode", "published"))
        if svc.digest(snapshots) != svc.digest(job.payload["source_snapshot"]):
            svc.fail(409, "WIKI_REPLAY_STATE_CHANGED", "来源状态已变化，不能返回旧构建响应")
        for version_id in (job.result or {}).get("created_version_ids", []):
            svc.version_access(ctx.db, ctx.user, version_id)
    return True
