"""v6 retention extension: register PATHS/SCHEMAS/HANDLERS in the shared wrapper."""
from . import retention
from . import services as svc

TEXT = {"type": "string"}
DATE = {"type": "string", "format": "date-time"}
REASON = {"type": "string", "minLength": 1, "maxLength": 500, "pattern": r"\S"}
SCHEMAS = {
    "RetentionPolicyInput": {"type": "object", "additionalProperties": False,
        "required": ["retention_days", "approved", "reason"],
        "properties": {"retention_days": {"type": "integer", "minimum": 2, "maximum": 36500},
            "approved": {"type": "boolean"}, "reason": REASON,
            "purge_allowed_roles": {"type": "array", "uniqueItems": True, "maxItems": 5,
                "items": {"enum": sorted(retention.PURGE_ROLES)}},
            "approval_expires_at": {"type": ["string", "null"], "format": "date-time"},
            "backfill_trash": {"type": "boolean"}}},
    "RetentionPolicy": {"type": "object", "required": ["space_id", "revision", "status", "approved",
            "retention_days", "permissions", "automatic_purge"],
        "properties": {"revision": {"type": "integer", "minimum": 0}, "approved": {"type": "boolean"},
            "status": {"enum": ["UNCONFIGURED", "UNAPPROVED", "APPROVED", "EXPIRED", "INVALID"]},
            "automatic_purge": {"const": False}}},
    "PurgeEligibility": {"type": "object", "required": ["resource_id", "eligible", "reasons", "expiry",
            "policy_status", "checked_at", "automatic_purge"],
        "properties": {"eligible": {"type": "boolean"}, "expiry": {"type": ["string", "null"], "format": "date-time"},
            "reasons": {"type": "array", "items": {"type": "object", "required": ["code", "message"],
                "properties": {"code": TEXT, "message": TEXT}}}, "automatic_purge": {"const": False}}},
    "PreservationInput": {"type": "object", "additionalProperties": False, "required": ["reason"],
        "anyOf": [{"required": ["legal_hold"]}, {"required": ["retain_until"]}],
        "properties": {"reason": REASON, "legal_hold": {"type": "boolean"}, "retain_until": DATE,
            "confirm_release": {"type": "boolean"}, "release_reason": {"type": "string", "maxLength": 500}}},
    "Preservation": {"type": "object", "required": ["resource_id", "revision", "legal_hold", "retain_until",
            "can_configure", "release_requires_confirmation"],
        "properties": {"revision": {"type": "integer", "minimum": 1}, "legal_hold": {"type": "boolean"},
            "retain_until": {"type": ["string", "null"], "format": "date-time"}}},
}


def _operation(name, output, body=None):
    result = {"operationId": name, "tags": ["Retention"],
        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}],
        "responses": {"200": {"description": "成功", "headers": {"ETag": {"schema": TEXT}},
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{output}"}}}}}}
    if body:
        result["parameters"] += [{"name": "If-Match", "in": "header", "required": True, "schema": TEXT},
            {"$ref": "#/components/parameters/Idempotency"}]
        result["security"] = [{"cookieAuth": [], "csrf": []}]
        result["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": {"$ref": f"#/components/schemas/{body}"}}}}
    return result


PATHS = {
    "/spaces/{id}/retention-policy": {
        "get": _operation("getRetentionPolicy", "RetentionPolicy"),
        "put": _operation("setRetentionPolicy", "RetentionPolicy", "RetentionPolicyInput")},
    "/resources/{id}/purge-eligibility": {"get": _operation("getPurgeEligibility", "PurgeEligibility")},
    "/resources/{id}/preservation": {
        "get": _operation("getResourcePreservation", "Preservation"),
        "put": _operation("setResourcePreservation", "Preservation", "PreservationInput")},
}


def _result(body):
    return svc.Result(body, headers={"ETag": f'"{body["revision"]}"'})


def get_policy(ctx):
    return _result(retention.retention_policy(ctx.db, ctx.user, ctx.id))


def put_policy(ctx):
    return _result(retention.set_retention_policy(ctx))


def get_eligibility(ctx):
    return _result(retention.purge_eligibility(ctx.db, ctx.user, ctx.id))


def get_preservation(ctx):
    return _result(retention.preservation(ctx.db, ctx.user, ctx.id))


def put_preservation(ctx):
    return _result(retention.set_preservation(ctx))


def replay_authority(ctx, cached):
    """The parent wrapper must return after this extension-specific guard succeeds."""
    if ctx.operation == "setRetentionPolicy":
        retention.require_policy_manager(ctx.db, ctx.user, ctx.id)
    elif ctx.operation == "setResourcePreservation":
        resource, _, _, _, _ = retention._resource_context(ctx.db, ctx.user, ctx.id, manage=True)
        retention.require_policy_manager(ctx.db, ctx.user, resource.space_id)
    else:
        svc.fail(403, "REPLAY_NOT_AUTHORIZED", "接口未授权幂等重放")


HANDLERS = {"getRetentionPolicy": get_policy, "setRetentionPolicy": put_policy,
    "getPurgeEligibility": get_eligibility, "getResourcePreservation": get_preservation,
    "setResourcePreservation": put_preservation}
