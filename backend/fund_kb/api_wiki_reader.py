"""Public metadata-only catalog; existing version-content routes read bodies."""
from . import services as svc
from .wiki_catalog import build_catalog, catalog_signature

SCHEMAS = {"WikiReaderCatalog": {"type": "object", "additionalProperties": False,
    "required": ["space_id", "scope", "items", "total", "body_blocks_loaded", "truncated", "snapshot"],
    "properties": {"space_id": {"type": "string", "format": "uuid"}, "scope": {"enum": ["reference", "formal"]},
        "items": {"type": "array", "items": {"type": "object", "required": ["id", "resource_id", "version_id",
            "title", "kind", "state", "legal_status", "block_count", "relations", "aliases"]}},
        "total": {"type": "integer", "minimum": 0}, "body_blocks_loaded": {"const": 0},
        "truncated": {"const": False}, "snapshot": {"type": "string"}}}}
PATHS = {"/wiki/catalog": {"get": {"operationId": "getWikiReaderCatalog",
    "parameters": [{"name": "space_id", "in": "query", "required": True, "schema": {"type": "string", "format": "uuid"}},
        {"name": "scope", "in": "query", "schema": {"enum": ["reference", "formal"], "default": "reference"}},
        {"name": "business_date", "in": "query", "schema": {"type": "string", "format": "date"}}],
    "responses": {"200": {"description": "Full authorized catalog without bodies", "content": {"application/json": {
        "schema": {"$ref": "#/components/schemas/WikiReaderCatalog"}}}}}}}}


def get_catalog(ctx):
    space_id, scope = ctx.query["space_id"], ctx.query.get("scope", "reference")
    context = {"business_date": ctx.query["business_date"]} if ctx.query.get("business_date") else {}
    pages = build_catalog(ctx.db, ctx.user, space_id, context, scope=scope)
    snapshot = catalog_signature(pages)
    items = [{key: value for key, value in page.items() if key not in {"records", "_metadata_signature"}} for page in pages.values()]
    return svc.Result({"space_id": space_id, "scope": scope, "items": items, "total": len(items),
        "body_blocks_loaded": 0, "truncated": False, "snapshot": snapshot}, headers={"ETag": '"' + snapshot + '"'})


HANDLERS = {"getWikiReaderCatalog": get_catalog}
