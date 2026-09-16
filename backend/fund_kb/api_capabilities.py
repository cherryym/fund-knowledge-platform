"""Capability authoring and guided-run API; no model or arbitrary tool execution."""
from . import capabilities as cap
from . import capability_runs as runs
from . import services as svc
from .agent_access import AGENT_OPERATIONS
from .capability_schema import DEFINITION, KEY, TEXT, starter

UUID = {"type": "string", "format": "uuid"}
SECURITY_SCHEMES = {"agentBearer": {"type": "http", "scheme": "bearer",
    "description": "个人单库的限定Agent凭据；仅显式列明的能力/运行接口接受，不允许人工审核或编辑发布。"}}
SCHEMAS = {"CapabilityDefinition": DEFINITION,
    "CapabilityCreate": {"type": "object", "additionalProperties": False, "required": ["space_id", "definition"],
        "properties": {"space_id": UUID, "definition": {"$ref": "#/components/schemas/CapabilityDefinition"}}},
    "CapabilityUpdate": {"type": "object", "additionalProperties": False, "required": ["version_id", "definition"],
        "properties": {"version_id": UUID, "definition": {"$ref": "#/components/schemas/CapabilityDefinition"}}},
    "CapabilityRunStart": {"type": "object", "additionalProperties": False, "required": ["version_id", "inputs", "mode"],
        "properties": {"version_id": UUID, "inputs": {"type": "object"}, "mode": {"enum": ["trial", "guided"]},
            "agent_label": {"type": "string", "maxLength": 200}}},
    "CapabilityStepReport": {"type": "object", "additionalProperties": False, "required": ["status", "outputs", "note"],
        "properties": {"status": {"enum": ["reported", "blocked", "failed"]}, "outputs": {"type": "object"}, "note": {"type": "string"}}},
    "CapabilityRunReview": {"type": "object", "additionalProperties": False, "required": ["step_id", "decision", "note"],
        "properties": {"step_id": KEY, "decision": {"enum": ["accept", "reject"]}, "note": TEXT, "outputs": {"type": "object"}}},
    "CapabilityCancel": {"type": "object", "additionalProperties": False, "required": ["reason"], "properties": {"reason": TEXT}},
    "CapabilityObject": {"type": "object"}}


def op(name, *, item=False, space=False, body=None, etag=False, status=200, step=False):
    spec = {"operationId": name, "tags": ["AgentCapabilities"], "parameters": [],
        "security": [{"cookieAuth": [], **({"csrf": []} if body else {})}],
        "responses": {str(status): {"description": "成功", "headers": {"ETag": {"schema": {"type": "string"}}},
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CapabilityObject"}}}}}}
    if item:
        spec["parameters"].append({"name": "id", "in": "path", "required": True, "schema": UUID})
    if space:
        spec["parameters"].append({"name": "space_id", "in": "query", "required": True, "schema": UUID})
    if step:
        spec["parameters"].append({"name": "step_id", "in": "path", "required": True, "schema": KEY})
    if body:
        spec["parameters"].append({"$ref": "#/components/parameters/Idempotency"})
        spec["security"] = [{"cookieAuth": [], "csrf": []}]
        spec["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/" + body}}}}
    if etag:
        spec["parameters"].append({"name": "If-Match", "in": "header", "required": True, "schema": {"type": "string"}})
    if name in AGENT_OPERATIONS:
        spec["security"].append({"agentBearer": []})
        spec["x-agent-scope"] = AGENT_OPERATIONS[name]
    return spec


PATHS = {
    "/capabilities": {"get": op("listCapabilities", space=True), "post": op("createCapability", body="CapabilityCreate", status=201)},
    "/capabilities/starter": {"get": op("getCapabilityStarter", space=True)},
    "/capabilities/{id}": {"get": op("getCapability", item=True), "put": op("updateCapability", item=True, body="CapabilityUpdate", etag=True)},
    "/capability-versions/{id}": {"get": op("getCapabilityVersion", item=True)},
    "/capability-versions/{id}/skill": {"get": op("exportCapabilitySkill", item=True)},
    "/capability-runs": {"get": op("listCapabilityRuns", space=True), "post": op("createCapabilityRun", body="CapabilityRunStart", status=201)},
    "/capability-runs/{id}": {"get": op("getCapabilityRun", item=True)},
    "/capability-runs/{id}/next": {"get": op("getCapabilityRunNext", item=True)},
    "/capability-runs/{id}/sources": {"get": op("getCapabilityRunSources", item=True)},
    "/capability-runs/{id}/steps/{step_id}": {"post": op("reportCapabilityStep", item=True, step=True, body="CapabilityStepReport", etag=True)},
    "/capability-runs/{id}/review": {"post": op("reviewCapabilityRun", item=True, body="CapabilityRunReview", etag=True)},
    "/capability-runs/{id}/cancel": {"post": op("cancelCapabilityRun", item=True, body="CapabilityCancel", etag=True)},
}


def result(value, status=200):
    return svc.Result(value, status=status, headers={"ETag": f'"{value["revision"]}"'} if "revision" in value else {})


def get_starter(ctx):
    svc.space_access(ctx.db, ctx.user, ctx.query["space_id"])
    return result({"definition": starter(), "state": "EXAMPLE_NOT_SAVED"})


def export_skill(ctx):
    from .capability_skill import skill_package
    detail = cap.version_detail(ctx, ctx.id)
    svc.version_access(ctx.db, ctx.user, ctx.id, "download")
    return result(skill_package(detail))


def replay_authority(ctx, cached):
    if ctx.operation in {"createCapability", "updateCapability"}:
        body = cached.get("body") or {}
        detail = cap.version_detail(ctx, body["version_id"])
        if detail["revision"] != body.get("revision") or not detail["permissions"]["can_edit"]:
            svc.fail(409, "CAPABILITY_REPLAY_STALE", "能力状态已变化")
    elif ctx.operation in {"createCapabilityRun", "reportCapabilityStep", "reviewCapabilityRun", "cancelCapabilityRun"}:
        row = runs._access(ctx, (cached.get("body") or {}).get("id"))
        if row.revision != cached["body"].get("revision"):
            svc.fail(409, "CAPABILITY_RUN_REPLAY_STALE", "运行状态已变化，请重新读取")
        if ctx.operation == "reviewCapabilityRun" and getattr(ctx.request.state, "agent_access", None):
            svc.fail(403, "CAPABILITY_HUMAN_REVIEW_REQUIRED", "Agent不能重放人工核对")
    else:
        svc.fail(403, "REPLAY_NOT_AUTHORIZED", "接口未授权重放")


HANDLERS = {"listCapabilities": lambda ctx: result(cap.list_capabilities(ctx)),
    "createCapability": lambda ctx: result(cap.create(ctx), 201), "getCapabilityStarter": get_starter,
    "getCapability": lambda ctx: result(cap.get(ctx)), "updateCapability": lambda ctx: result(cap.update(ctx)),
    "getCapabilityVersion": lambda ctx: result(cap.version_detail(ctx, ctx.id)), "exportCapabilitySkill": export_skill,
    "listCapabilityRuns": lambda ctx: result(runs.list_runs(ctx)), "createCapabilityRun": lambda ctx: result(runs.start(ctx), 201),
    "getCapabilityRun": lambda ctx: result(runs.get(ctx)), "getCapabilityRunNext": lambda ctx: result(runs.next_steps(ctx)),
    "getCapabilityRunSources": lambda ctx: result(runs.sources(ctx)), "reportCapabilityStep": lambda ctx: result(runs.report(ctx)),
    "reviewCapabilityRun": lambda ctx: result(runs.review(ctx)), "cancelCapabilityRun": lambda ctx: result(runs.cancel(ctx))}
