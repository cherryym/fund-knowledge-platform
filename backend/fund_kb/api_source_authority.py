"""Source replacement fact governance; no inference, deletion, or publication."""
from . import services as svc
from . import source_authority as authority

UUID = {"type": "string", "format": "uuid"}
REASON = {"type": "string", "minLength": 1, "maxLength": 2000, "pattern": r"\S"}
SCHEMAS = {
    "SourceAuthorityInput": {"type": "object", "additionalProperties": False,
        "required": ["space_id", *authority.VERSION_KEYS, "effective_from", "scope", "reason"],
        "properties": {**{key: UUID for key in ["space_id", *authority.VERSION_KEYS]},
            "effective_from": {"type": "string", "format": "date"}, "scope": {"enum": ["full", "partial"]}, "reason": REASON,
            "expected_sources": {"type": "array", "minItems": 2, "maxItems": 3, "uniqueItems": True,
                "items": {"type": "object", "additionalProperties": False,
                    "required": ["version_id", "revision", "access_epoch", "content_sha256"],
                    "properties": {"version_id": UUID, "revision": {"type": "integer", "minimum": 1},
                        "access_epoch": {"type": "integer", "minimum": 1},
                        "content_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}}}}},
    "SourceAuthorityRevoke": {"type": "object", "additionalProperties": False,
        "required": ["reason"], "properties": {"reason": REASON}},
    "SourceAuthority": {"type": "object", "required": ["id", "revision", "state", "validation_state"]},
    "SourceAuthorityList": {"type": "object", "required": ["items", "can_manage", "notes"]},
    "SourceAuthoritySuggestions": {"type": "object", "required": ["items", "can_manage", "notes"]},
}


def _op(name, output, *, body=None, item=False, create=False):
    spec = {"operationId": name, "tags": ["SourceAuthority"], "parameters": [],
        "responses": {"201" if create else "200": {"description": "成功", "headers": {"ETag": {"schema": {"type": "string"}}},
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{output}"}}}}}}
    if item:
        spec["parameters"].append({"name": "id", "in": "path", "required": True, "schema": UUID})
    elif not body:
        spec["parameters"].append({"name": "space_id", "in": "query", "required": True, "schema": UUID})
    if body:
        spec["parameters"].append({"$ref": "#/components/parameters/Idempotency"})
        if not create:
            spec["parameters"].append({"name": "If-Match", "in": "header", "required": True, "schema": {"type": "string"}})
        spec["security"] = [{"cookieAuth": [], "csrf": []}]
        spec["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": {"$ref": f"#/components/schemas/{body}"}}}}
    return spec


PATHS = {
    "/source-authority": {"get": _op("listSourceAuthority", "SourceAuthorityList"),
        "post": _op("createSourceAuthority", "SourceAuthority", body="SourceAuthorityInput", create=True)},
    "/source-authority/suggestions": {"get": _op("listSourceAuthoritySuggestions", "SourceAuthoritySuggestions")},
    "/source-authority/{id}": {"get": _op("getSourceAuthority", "SourceAuthority", item=True)},
    "/source-authority/{id}/revoke": {"post": _op("revokeSourceAuthority", "SourceAuthority",
        body="SourceAuthorityRevoke", item=True)},
}


def _result(body, status=200):
    return svc.Result(body, status=status, headers={"ETag": f'"{body["revision"]}"'})


def list_records(ctx):
    return svc.Result(authority.list_records(ctx.db, ctx.user, ctx.query["space_id"]))


def get_record(ctx):
    return _result(authority.get_record(ctx.db, ctx.user, ctx.id)[1])


def create_record(ctx):
    return _result(authority.create_record(ctx), 201)


def suggestions(ctx):
    from .source_authority_suggestions import suggestions as read
    return svc.Result(read(ctx.db, ctx.user, ctx.query["space_id"]), headers={"Cache-Control": "private, no-store"})


def revoke_record(ctx):
    return _result(authority.revoke_record(ctx))


def replay_authority(ctx, cached):
    record_id = (cached.get("body") or {}).get("id")
    if not record_id or ctx.operation not in {"createSourceAuthority", "revokeSourceAuthority"}:
        svc.fail(403, "REPLAY_NOT_AUTHORIZED", "接口未授权幂等重放")
    row, current = authority.get_record(ctx.db, ctx.user, record_id, manage=True)
    if row.revision != cached["body"]["revision"] or current != cached["body"]:
        svc.fail(409, "SOURCE_AUTHORITY_CHANGED", "治理记录已变更，请刷新后操作")


HANDLERS = {"listSourceAuthority": list_records, "getSourceAuthority": get_record,
    "createSourceAuthority": create_record, "revokeSourceAuthority": revoke_record,
    "listSourceAuthoritySuggestions": suggestions}
