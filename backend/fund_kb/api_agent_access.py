"""Cookie-only credential management extension, integrated by the main worker.

createAgentAccess MUST be in api.WRITE_EXCEPTIONS. Its request_id uniqueness is
implemented by agent_access and it must never use the generic response ledger.
"""
from . import agent_access as access
from . import models as m
from . import services as svc

UUID = {"type": "string", "format": "uuid"}
TIMESTAMP = {"type": "string", "format": "date-time"}
SCOPE_LIST = {"type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True,
    "items": {"type": "string", "enum": sorted(access.SCOPES)}}
SCHEMAS = {
    "AgentAccessMetadata": {"type": "object", "additionalProperties": False,
        "required": ["id", "name", "space_id", "scopes", "expires_at", "revoked_at", "created_at", "revision"],
        "properties": {"id": UUID, "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "space_id": UUID, "scopes": SCOPE_LIST, "expires_at": TIMESTAMP,
            "revoked_at": {"type": ["string", "null"], "format": "date-time"}, "created_at": TIMESTAMP,
            "revision": {"type": "integer", "minimum": 1}}},
    "AgentAccessCreate": {"type": "object", "additionalProperties": False,
        "required": ["request_id", "name", "space_id", "scopes", "expires_at"],
        "properties": {"request_id": UUID, "space_id": UUID, "scopes": SCOPE_LIST,
            "name": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"\S"},
            "expires_at": TIMESTAMP}},
    "AgentAccessCreated": {"type": "object", "additionalProperties": False, "required": ["access", "token"],
        "properties": {"access": {"$ref": "#/components/schemas/AgentAccessMetadata"},
            "token": {"type": "string", "pattern": r"^fkb_agent_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$"}}},
    "AgentAccessList": {"type": "object", "additionalProperties": False,
        "required": ["items", "can_create", "base_url", "notes"],
        "properties": {"items": {"type": "array", "items": {"$ref": "#/components/schemas/AgentAccessMetadata"}},
            "can_create": {"type": "boolean"}, "base_url": {"type": "string"},
            "notes": {"type": "array", "items": {"type": "string"}}}},
    "AgentAccessRevoke": {"type": "object", "additionalProperties": False, "required": ["reason"],
        "properties": {"reason": {"type": "string", "minLength": 1, "maxLength": 2000, "pattern": r"\S"}}},
}


def _op(operation, output, *, body=None, create=False):
    spec = {"operationId": operation, "tags": ["AgentAccess"], "parameters": [],
        "security": [{"cookieAuth": [], **({"csrf": []} if body else {})}],
        "responses": {"201" if create else "200": {"description": "成功",
            "headers": {"Cache-Control": {"schema": {"type": "string"}}},
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{output}"}}}}}}
    if body:
        spec["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": {"$ref": f"#/components/schemas/{body}"}}}}
        if not create:
            spec["parameters"] += [{"$ref": "#/components/parameters/Idempotency"},
                {"name": "If-Match", "in": "header", "required": True, "schema": {"type": "string"}}]
    return spec


PATHS = {
    "/agent-access": {"get": _op("listAgentAccess", "AgentAccessList"),
        "post": _op("createAgentAccess", "AgentAccessCreated", body="AgentAccessCreate", create=True)},
    "/agent-access/{id}/revoke": {"parameters": [{"name": "id", "in": "path", "required": True, "schema": UUID}],
        "post": _op("revokeAgentAccess", "AgentAccessMetadata", body="AgentAccessRevoke")},
}
PATHS["/agent-access"]["get"]["parameters"] = [
    {"name": "space_id", "in": "query", "required": True, "schema": UUID}]


def list_records(ctx):
    return svc.Result(access.list_access(ctx), headers={"Cache-Control": "private, no-store"})


def create_record(ctx):
    # Fail closed if the main integration accidentally routes creation through
    # the generic ledger: even a STARTED entry must not get a plaintext result.
    from .api import WRITE_EXCEPTIONS
    if "createAgentAccess" not in WRITE_EXCEPTIONS:
        svc.fail(503, "AGENT_ACCESS_UNSAFE_INTEGRATION", "凭据创建的单次返回保护尚未接入")
    key = ctx.request.headers.get("idempotency-key")
    if key and ctx.db.get(m.IdempotencyRecord, (ctx.user.id, ctx.request.method, ctx.request.url.path, key)):
        svc.fail(503, "AGENT_ACCESS_UNSAFE_INTEGRATION", "凭据创建不能进入通用幂等回执")
    return svc.Result(access.create_access(ctx), status=201, headers={"Cache-Control": "private, no-store"})


def revoke_record(ctx):
    body = access.revoke_access(ctx)
    return svc.Result(body, headers={"Cache-Control": "private, no-store", "ETag": f'"{body["revision"]}"'})


def replay_authority(ctx, cached):
    if ctx.operation != "revokeAgentAccess":
        svc.fail(403, "REPLAY_NOT_AUTHORIZED", "此接口不允许幂等回放")
    body = cached.get("body") if isinstance(cached, dict) else None
    if not isinstance(body, dict) or body.get("id") != ctx.id:
        svc.fail(403, "REPLAY_NOT_AUTHORIZED", "此接口不允许幂等回放")
    row = access.owned_access(ctx, ctx.id)
    current = access.metadata(row)
    if current["revoked_at"] is None or current != body:
        svc.fail(409, "AGENT_ACCESS_CHANGED", "凭据状态已变化，请刷新后操作")
    return True


HANDLERS = {"listAgentAccess": list_records, "createAgentAccess": create_record, "revokeAgentAccess": revoke_record}
