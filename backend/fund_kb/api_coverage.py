"""Read-only aggregate of model-declared evidence gaps; no writes or model calls."""
from . import services as svc
from .coverage_gaps import aggregate

UUID = {"type": "string", "format": "uuid"}
SCHEMAS = {"CoverageGaps": {"type": "object", "required": ["items", "scope", "runs_scanned", "notes"]}}
PATHS = {"/coverage-gaps": {"get": {"operationId": "listCoverageGaps", "tags": ["Coverage"],
    "parameters": [{"name": "space_id", "in": "query", "required": True, "schema": UUID}],
    "responses": {"200": {"description": "成功", "content": {"application/json": {
        "schema": {"$ref": "#/components/schemas/CoverageGaps"}}}}}}}}


def list_gaps(ctx):
    from .agent_access import authorize_agent_space
    authorize_agent_space(getattr(ctx, "request", None), ctx.query["space_id"])
    return svc.Result(aggregate(ctx.db, ctx.user, ctx.query["space_id"]), headers={"Cache-Control": "private, no-store"})


HANDLERS = {"listCoverageGaps": list_gaps}
