"""Library extension for api.py's schema/CSRF/idempotency/transaction wrapper.

GET /libraries -> {items: Library[]}; POST {name,kind} -> Library (201).
GET/PATCH /libraries/{id}; PATCH accepts only {name}.
POST /libraries/{id}/govern {kind:"team"}: explicit legacy space admin only.
GET/PUT /libraries/{id}/members: PUT {items:[{user_id,roles}]}, response
{items:[{user_id,display_name,roles}],revision}; replaces explicit members only.
GET /users/directory?space_id=... -> {items:[{id,display_name}]} for managers.
All detail/member mutations require their Space ETag, CSRF and Idempotency-Key.
"""
from . import libraries as lib
from . import services as svc

UUID = {"type": "string", "format": "uuid"}
ROLES = {"type": "array", "uniqueItems": True, "items": {"enum": sorted(lib.ALL_ROLES)}}
NAME = {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"\S"}
REVISION = {"type": "integer", "minimum": 1}
SCHEMAS = {
    "Library": {"type": "object", "additionalProperties": False,
        "required": ["id", "name", "kind", "owner_id", "roles", "revision", "governed"],
        "properties": {"id": UUID, "name": {"type": "string"}, "kind": {"enum": ["personal", "team", "legacy"]},
            "owner_id": {"type": ["string", "null"], "format": "uuid"}, "roles": ROLES,
            "revision": REVISION, "governed": {"type": "boolean"}}},
    "LibraryList": {"type": "object", "additionalProperties": False, "required": ["items"],
        "properties": {"items": {"type": "array", "items": {"$ref": "#/components/schemas/Library"}}}},
    "LibraryCreate": {"type": "object", "additionalProperties": False, "required": ["name", "kind"],
        "properties": {"name": NAME, "kind": {"enum": ["personal", "team"]}}},
    "LibraryPatch": {"type": "object", "additionalProperties": False, "required": ["name"],
        "properties": {"name": NAME}},
    "LibraryGovern": {"type": "object", "additionalProperties": False, "required": ["kind"],
        "properties": {"kind": {"const": "team"}}},
    "LibraryMembersInput": {"type": "object", "additionalProperties": False, "required": ["items"],
        "properties": {"items": {"type": "array", "maxItems": 500, "items": {
            "type": "object", "additionalProperties": False, "required": ["user_id", "roles"],
            "properties": {"user_id": UUID, "roles": {**ROLES, "minItems": 1}}}}}},
    "LibraryMembers": {"type": "object", "additionalProperties": False, "required": ["items", "revision"],
        "properties": {"revision": REVISION, "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["user_id", "display_name", "roles"],
            "properties": {"user_id": UUID, "display_name": {"type": "string"}, "roles": ROLES}}}}},
    "LibraryUserDirectory": {"type": "object", "additionalProperties": False, "required": ["items"],
        "properties": {"items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["id", "display_name"], "properties": {"id": UUID, "display_name": {"type": "string"}}}}}},
}
# Update the public Space projection in memory through the extension loader.
# The checked-in baseline contract remains frozen.
SCHEMAS["Space"] = SCHEMAS["Library"]


def _operation(operation, response, *, body=None, etag=False, status=200):
    spec = {"operationId": operation, "tags": ["Libraries"], "parameters": [],
        "security": [{"cookieAuth": []}], "responses": {str(status): {"description": "成功",
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{response}"}}}}}}
    if body:
        spec["security"] = [{"cookieAuth": [], "csrf": []}]
        spec["parameters"].append({"$ref": "#/components/parameters/Idempotency"})
        spec["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": {"$ref": f"#/components/schemas/{body}"}}}}
    if etag:
        spec["parameters"].append({"name": "If-Match", "in": "header", "required": True,
            "schema": {"type": "string"}})
    if response in {"Library", "LibraryMembers"}:
        spec["responses"][str(status)]["headers"] = {"ETag": {"schema": {"type": "string"}}}
    return spec


ID = {"name": "id", "in": "path", "required": True, "schema": UUID}
PATHS = {
    "/libraries": {"get": _operation("listLibraries", "LibraryList"),
        "post": _operation("createLibrary", "Library", body="LibraryCreate", status=201)},
    "/libraries/{id}": {"parameters": [ID], "get": _operation("getLibrary", "Library"),
        "patch": _operation("updateLibrary", "Library", body="LibraryPatch", etag=True)},
    "/libraries/{id}/govern": {"parameters": [ID],
        "post": _operation("governLibrary", "Library", body="LibraryGovern", etag=True)},
    "/libraries/{id}/members": {"parameters": [ID], "get": _operation("listLibraryMembers", "LibraryMembers"),
        "put": _operation("replaceLibraryMembers", "LibraryMembers", body="LibraryMembersInput", etag=True)},
    "/users/directory": {"parameters": [{"name": "space_id", "in": "query", "required": True, "schema": UUID}],
        "get": _operation("listLibraryUsers", "LibraryUserDirectory")},
}


def list_libraries(ctx):
    return svc.Result({"items": lib.visible_libraries(ctx.db, ctx.user)})


def get_library(ctx):
    space = svc.space_access(ctx.db, ctx.user, ctx.id)
    return svc.tagged(lib.library_dict(ctx.db, ctx.user, space), space)


def members(ctx):
    space = svc.space_access(ctx.db, ctx.user, ctx.id, "admin")
    if ctx.operation == "replaceLibraryMembers":
        lib.replace_members(ctx, space, ctx.data["items"])
    return svc.tagged({"items": lib.member_items(ctx.db, space), "revision": space.revision}, space)


HANDLERS = {"listLibraries": list_libraries, "createLibrary": lib.create_library,
    "getLibrary": get_library, "updateLibrary": lib.update_library, "governLibrary": lib.govern_library,
    "listLibraryMembers": members, "replaceLibraryMembers": members, "listLibraryUsers": lib.directory}


def replay_authority(ctx, cached):
    if ctx.operation not in HANDLERS:
        return False
    body = cached.get("body") or {}
    space_id = body.get("id") if ctx.operation == "createLibrary" else ctx.id
    space = svc.space_access(ctx.db, ctx.user, space_id, "admin")
    config = lib.governance(ctx.db, space.id)
    if ctx.operation == "createLibrary" and (not config or config["owner_id"] != ctx.user.id):
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if space.revision != body.get("revision"):
        svc.fail(409, "LIBRARY_REPLAY_STATE_CHANGED", "库或成员状态已变化，请重新读取")
    current = {"items": lib.member_items(ctx.db, space), "revision": space.revision} \
        if ctx.operation == "replaceLibraryMembers" else lib.library_dict(ctx.db, ctx.user, space)
    if current != body:
        svc.fail(409, "LIBRARY_REPLAY_STATE_CHANGED", "库或当前权限已变化，请重新读取")
    return True
