"""Read-only library map (metadata of the caller's authorized catalog; no bodies)."""
from . import services as svc

UUID = {"type": "string", "format": "uuid"}
SCHEMAS = {"LibraryMap": {"type": "object", "required": ["text", "sha256", "stats", "pages"]}}
PATHS = {"/library-map": {"get": {"operationId": "getLibraryMap", "tags": ["Knowledge"],
    "parameters": [{"name": "space_id", "in": "query", "required": True, "schema": UUID},
                   {"name": "level", "in": "query", "required": False,
                    "schema": {"enum": ["full", "sources", "compact"], "default": "full"}}],
    "responses": {"200": {"description": "成功", "content": {"application/json": {
        "schema": {"$ref": "#/components/schemas/LibraryMap"}}}}}}}}


def library_map(ctx):
    from .agent_access import authorize_agent_space
    from .library_map import build
    from .source_metadata import load
    from .wiki_catalog import build_catalog
    space_id = ctx.query["space_id"]
    authorize_agent_space(getattr(ctx, "request", None), space_id)
    svc.space_access(ctx.db, ctx.user, space_id)
    pages = build_catalog(ctx.db, ctx.user, space_id, {}, scope="reference")
    candidates = load(ctx.db, [p["version_id"] for p in pages.values() if p["kind"] == "document"])
    result = build(pages, candidates, level=ctx.query.get("level") or "full")
    # W-ids are per-map handles; version_id is the stable identifier for reads.
    result["pages"] = [{"page_id": pid, **{key: page.get(key) for key in (
        "resource_id", "version_id", "title", "kind", "category", "state", "legal_status")}} for pid, page in pages.items()]
    return svc.Result(result, headers={"Cache-Control": "private, no-store"})


HANDLERS = {"getLibraryMap": library_map}
