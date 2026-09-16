"""All 61 OpenAPI operations, with a single audited transaction and HTTP guard layer."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

import yaml
from anyio import CapacityLimiter, to_thread
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from jsonschema import Draft202012Validator, FormatChecker, RefResolver, ValidationError
from sqlalchemy import literal, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError

from . import auth
from . import models as m
from .agent_access import authenticate_agent
from .api_catalog import HANDLERS as CATALOG
from .api_consultation import HANDLERS as CONSULTATION
from .api_content import HANDLERS as CONTENT
from .api_tasks import HANDLERS as TASKS
from .db import Base, build_engine, database_capabilities, make_session_factory, normalize_database_url
from .services import *
from .settings import Settings, get_settings
from .storage import Storage, StorageError

log = logging.getLogger(__name__)
CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "contracts"
READ_ONLY_POST = frozenset({"searchKnowledge", "searchHybridKnowledge"})
WRITE_EXCEPTIONS = {"logout", "uploadPart", "createAgentAccess", *READ_ONLY_POST}
PUBLIC = {"getHealth", "beginLogin", "finishLogin"}
ASYNC_OIDC_OPERATIONS = frozenset({"beginLogin", "finishLogin"})
# Capabilities are explicit, including the read-only POST search. A newly added
# GET must be reviewed here instead of silently inheriting a writable Session.
READ_ONLY_OPERATIONS = frozenset({
    "downloadExport", "getAdminReview", "getCase", "getDocumentTaxonomy", "getGuidanceNormalization",
    "getHealth", "getJob", "getLibrary", "getMe", "getModelConnection", "getModelOAuthChallenge",
    "getModelOAuthState", "getModelPolicy", "getPermissions", "getPurgeEligibility", "getRelations",
    "getResource", "getResourcePreservation", "getRetentionPolicy", "getReviewQueue", "getRun",
    "getRunProgress", "getThread", "getUpload", "getVersion", "getWikiGraph", "getWikiPageLinks",
    "getWikiTaxonomy", "getWikiWorkspace", "listCases", "listDocuments", "listJobs", "listLibraries",
    "listLibraryMembers", "listLibraryUsers", "listMembers", "listModelConnections",
    "listModelCredentialReferences", "listModelOptions", "listModelProviders", "listResources", "listSpaces",
    "listThreads", "listVersionReviews", "listVersions", "queryAudit", "readVersionContent",
    "resolveWikiTitle", "searchKnowledge", "getWikiCompilationSpecs", "getWikiReaderCatalog",
    "getWikiEntryMaintenance", "resolveWikiMaintenanceAlias", "listWikiMaintenanceProposals", "getWikiMaintenanceProposal",
    "getRetrievalStatus", "getRetrievalProfiles", "searchHybridKnowledge",
    "listSourceAuthority", "getSourceAuthority", "listSourceAuthoritySuggestions",
    "listCapabilities", "getCapabilityStarter", "getCapability", "getCapabilityVersion",
    "exportCapabilitySkill", "listCapabilityRuns", "getCapabilityRun", "getCapabilityRunNext",
    "getCapabilityRunSources", "listAgentAccess",
})


def deref(document, value):
    while "$ref" in value and value["$ref"].startswith("#/"):
        target = document
        for part in value["$ref"][2:].split("/"):
            target = target[part]
        value = target
    return value


def validator(schema, document):
    return Draft202012Validator(schema, format_checker=FormatChecker(),
        resolver=RefResolver(base_uri=(CONTRACT_ROOT / "openapi.yaml").as_uri(), referrer=document,
            store={(CONTRACT_ROOT / "answer.schema.json").as_uri(): json.loads((CONTRACT_ROOT / "answer.schema.json").read_text())}))


def validate_input(schema, data, document):
    try:
        validator(schema, document).validate(data)
    except ValidationError as exc:
        # Do not echo input values, credentials, private excerpts, or the jsonschema repr.
        fail(422, "SCHEMA_VALIDATION", "请求不符合接口结构", field=".".join(str(x) for x in exc.absolute_path))


def request_input(request, spec, path_spec, document):
    query = {}
    for value in path_spec.get("parameters", []) + spec.get("parameters", []):
        p = deref(document, value)
        location, name = p["in"], p["name"]
        if location == "header":
            raw = request.headers.get(name)
        elif location == "path":
            raw = request.path_params.get(name)
        else:
            raw = request.query_params.get(name)
        if raw is None:
            if p.get("required"):
                fail(428 if name == "If-Match" else 400, "PRECONDITION_REQUIRED" if name == "If-Match" else "MISSING_PARAMETER",
                    "缺少必需请求参数", field=name)
            if location == "query" and "default" in p.get("schema", {}):
                query[name] = p["schema"]["default"]
            continue
        typ = p.get("schema", {}).get("type")
        try:
            if typ == "integer":
                raw = int(raw)
            elif typ == "boolean":
                if raw not in {"true", "false"}:
                    raise ValueError()
                raw = raw == "true"
        except ValueError:
            fail(422, "INVALID_PARAMETER", "请求参数类型无效", field=name)
        validate_input(p.get("schema", {}), raw, document)
        if location == "query":
            query[name] = raw
    content_spec = spec.get("requestBody", {}).get("content")
    if not content_spec:
        return {}, query
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    optional_body = spec.get("requestBody", {}).get("required", False) is False
    if content_type not in content_spec and not (optional_body and not content_type):
        fail(415, "UNSUPPORTED_MEDIA_TYPE", "请求Content-Type不符合接口约定")
    limit = 8388608 if content_type == "application/octet-stream" else 1048576
    body = getattr(request.state, "prefetched_request_body", None)
    if body is None:
        raise RuntimeError("Request body must be buffered before opening a Session")
    if len(body) > limit:
        fail(413, "REQUEST_TOO_LARGE", "请求超过接口大小限制")
    if not body and optional_body:
        return {}, query
    if content_type not in content_spec:
        fail(415, "UNSUPPORTED_MEDIA_TYPE", "请求Content-Type不符合接口约定")
    if content_type == "application/octet-stream":
        return bytes(body), query
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        fail(400, "INVALID_JSON", "请求JSON无效")
    validate_input(content_spec[content_type].get("schema", {}), data, document)
    return data, query


def replay_authority(ctx, cached):
    """An idempotency key never restores a revoked read/management capability."""
    from .api_catalog import replay_authority as catalog_replay_authority
    from .api_consultation import case_access, run_access, run_sources_readable
    from .api_tasks import job_access

    if ctx.operation == "createSpace" and not deployment_admin(ctx.user, ctx.settings):
        fail(403, "DEPLOYMENT_ADMIN_REQUIRED", "缺少部署级管理权限")
    if catalog_replay_authority(ctx, cached):
        return

    for module in getattr(ctx.request.app.state, "extension_modules", []):
        if ctx.operation in module.HANDLERS:
            guard = getattr(module, "replay_authority", None)
            if guard is None:
                fail(403, "REPLAY_NOT_AUTHORIZED", "扩展接口未声明幂等重放授权检查")
            guard(ctx, cached)
            if module.__name__ in {"fund_kb.api_libraries", "fund_kb.api_retention", "fund_kb.api_documents", "fund_kb.api_oauth", "fund_kb.api_local_wiki", "fund_kb.api_admin_review", "fund_kb.api_wiki_maintenance", "fund_kb.api_retrieval", "fund_kb.api_source_authority", "fund_kb.api_capabilities", "fund_kb.api_agent_access"}:
                return

    path = ctx.request.url.path
    body = cached.get("body")
    if "/spaces/" in path:
        space_access(ctx.db, ctx.user, ctx.id, "admin")
    elif "/resources/" in path:
        r = ctx.db.get(m.Resource, ctx.id)
        if r:
            resource_access(ctx.db, ctx.user, r, "manage" if r.deleted_at else "read", allow_deleted=bool(r.deleted_at))
        else:
            fail(404, "NOT_FOUND", "对象不存在或不可访问")
    elif "/versions/" in path:
        version_access(ctx.db, ctx.user, ctx.id)
    elif "/threads/" in path:
        t = ctx.db.get(m.ConsultationThread, ctx.id)
        if not t or t.owner_id != ctx.user.id:
            fail(404, "NOT_FOUND", "对象不存在或不可访问")
        space_access(ctx.db, ctx.user, t.space_id)
    elif "/cases/" in path:
        case_access(ctx)
    elif "/runs/" in path:
        run_sources_readable(ctx, run_access(ctx))
    elif "/jobs/" in path:
        job_access(ctx)
    elif "/uploads/" in path:
        from .api_tasks import upload_access
        upload_access(ctx)
    if not isinstance(body, dict):
        return
    if "active_release_id" in body:
        resource_access(ctx.db, ctx.user, body["id"])
    elif "version_no" in body:
        version_access(ctx.db, ctx.user, body["id"])
    elif "job_id" in body:
        run_sources_readable(ctx, run_access(ctx, body["id"]))
    elif "attempts" in body:
        job_access(ctx, body["id"])
    elif "description" in body and "assignee_id" in body:
        case_access(ctx, body["id"])
    if ctx.operation in {"createSpace", "setModelPolicy"} and not deployment_admin(ctx.user, ctx.settings):
        fail(403, "DEPLOYMENT_ADMIN_REQUIRED", "缺少部署级管理权限")


def begin_idempotency(ctx):
    key = ctx.request.headers.get("idempotency-key")
    if not key or not 8 <= len(key) <= 128:
        fail(400, "IDEMPOTENCY_REQUIRED", "需要8至128字符的Idempotency-Key")
    identity = (ctx.user.id, ctx.request.method, ctx.request.url.path, key)
    record = ctx.db.get(m.IdempotencyRecord, identity)
    sha = digest({"body": ctx.data, "query": ctx.query})
    if record and aware(record.expires_at) <= now():
        ctx.db.delete(record)
        ctx.db.flush()
        record = None
    if record:
        if record.request_sha256 != sha:
            fail(409, "IDEMPOTENCY_CONFLICT", "同一幂等键不能用于不同请求内容")
        if record.state != "COMPLETED":
            fail(409, "REQUEST_IN_PROGRESS", "该请求正在处理中")
        replay_authority(ctx, record.response)
        return record, Result(record.response["body"], record.status_code, record.response.get("headers", {}))
    record = m.IdempotencyRecord(actor_id=identity[0], http_method=identity[1], route=identity[2], key=key,
        request_sha256=sha, state="STARTED", expires_at=now() + timedelta(hours=24))
    ctx.db.add(record)
    ctx.db.flush()
    return record, None


def response_for(result):
    if isinstance(result, Response):
        return result
    if result.status == 204:
        return Response(status_code=204, headers=result.headers)
    return JSONResponse(primitive(result.body), status_code=result.status, headers=result.headers)


def error_response(exc, trace_id):
    return JSONResponse({"code": exc.code, "message": exc.message, "trace_id": trace_id,
        "details": exc.details}, status_code=exc.status, headers={"Cache-Control": "private, no-store"})


def create_app(settings: Settings | None = None):
    settings = settings or get_settings()
    database_url = normalize_database_url(settings.database_url)
    if database_url.get_backend_name() == "sqlite" and database_url.database in {None, "", ":memory:"}:
        # Independent connections need one named in-memory database. The writer
        # pool keeps it alive; separate create_app calls never share its name.
        database_url = database_url.set(database=f"file:fund-kb-{uid()}",
            query={"mode": "memory", "cache": "shared", "uri": "true"})
    engine = build_engine(database_url)
    # SQLite requires write intent before the first authentication SELECT; otherwise a
    # worker commit can make the request's read snapshot impossible to upgrade safely.
    api_engine = engine.execution_options(sqlite_transaction_mode="IMMEDIATE") if engine.dialect.name == "sqlite" else engine
    factory = make_session_factory(api_engine)
    read_engine = build_engine(database_url, read_only=True)
    read_factory = make_session_factory(read_engine, read_only=True)
    storage = Storage(settings)
    document = yaml.safe_load((CONTRACT_ROOT / "openapi.yaml").read_text())
    baseline_operations = {spec["operationId"] for path in document["paths"].values()
        for method, spec in path.items() if method in {"get", "post", "put", "patch", "delete"}}
    extension_modules = []
    for name in ("api_models", "api_wiki", "api_libraries", "api_retention", "api_documents", "api_oauth", "api_local_wiki", "api_admin_review", "api_wiki_reader", "api_wiki_maintenance", "api_retrieval", "api_source_authority", "api_capabilities", "api_agent_access"):
        try:
            module = importlib.import_module(f"fund_kb.{name}")
        except ModuleNotFoundError as exc:
            if exc.name == f"fund_kb.{name}":
                continue
            raise
        if set(module.PATHS) & set(document["paths"]):
            raise RuntimeError("Extension route collides with baseline")
        document["paths"].update(module.PATHS)
        document["components"]["schemas"].update(module.SCHEMAS)
        document["components"].setdefault("securitySchemes", {}).update(getattr(module, "SECURITY_SCHEMES", {}))
        extension_modules.append(module)

    @asynccontextmanager
    async def lifespan(app):
        if settings.app_env in {"development", "test"} and settings.auto_create_schema:
            Base.metadata.create_all(engine)
        try:
            yield
        finally:
            storage.close()
            read_engine.dispose()
            engine.dispose()

    app = FastAPI(title=document["info"]["title"], version=document["info"]["version"], lifespan=lifespan)
    app.state.settings, app.state.engine, app.state.session_factory = settings, engine, factory
    app.state.read_engine, app.state.read_session_factory = read_engine, read_factory
    # Independent admission budgets; background jobs use their own executor and
    # the writer pool. Cancellation waits for worker-owned Session cleanup.
    app.state.request_read_limiter = CapacityLimiter(4)
    app.state.request_write_limiter = CapacityLimiter(2)
    app.state.request_oidc_limiter = CapacityLimiter(1)
    app.state.storage, app.state.vector_index, app.state.job_dispatcher = storage, None, None
    app.state.oidc_pending = {}
    app.state.extension_modules = extension_modules
    app.state.calculator_registry = {}
    app.state.approved_model_policies = dict(getattr(settings, "approved_model_policies", {}) or {})
    app.state.answer_validator = Draft202012Validator(json.loads((CONTRACT_ROOT / "answer.schema.json").read_text()),
        format_checker=FormatChecker())

    def health(ctx):
        ctx.db.execute(select(literal(1)))
        return Result({"status": "ready" if ctx.request.app.state.job_dispatcher else "degraded"})

    handlers = {**auth.HANDLERS, **CATALOG, **CONTENT, **TASKS, **CONSULTATION, "getHealth": health}
    for module in extension_modules:
        if set(handlers) & set(module.HANDLERS):
            raise RuntimeError("Extension operation collides with baseline")
        handlers.update(module.HANDLERS)

    async def buffer_body(request, spec):
        # No Session exists while receiving bytes from ASGI.
        content = spec.get("requestBody", {}).get("content", {})
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        body = bytearray()
        if content_type in content:
            maximum = 8388608 if content_type == "application/octet-stream" else 1048576
            async for chunk in request.stream():
                if len(body) + len(chunk) > maximum:
                    fail(413, "REQUEST_TOO_LARGE", "请求超过接口大小限制")
                body.extend(chunk)
        request.state.prefetched_request_body = bytes(body)

    async def offload(function, *args, read_only=False, oidc=False):
        limiter = (app.state.request_oidc_limiter if oidc else
                   app.state.request_read_limiter if read_only else app.state.request_write_limiter)
        return await to_thread.run_sync(partial(function, *args), limiter=limiter, abandon_on_cancel=False)

    def completed_response(response, request):
        response.headers.setdefault("Cache-Control", "private, no-store")
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    def execute_request(request, spec, path_spec, operation):
        """The entire synchronous transaction, including its error path, owns one thread."""
        read_only = operation in READ_ONLY_OPERATIONS
        request_factory = read_factory if read_only else factory
        dispatch, user_id = [], None
        with request_factory() as db:
            try:
                # Bearer credentials are an explicit, narrow identity. An invalid
                # credential never falls back to an ambient browser Cookie.
                agent_user = authenticate_agent(request, db, operation)
                user = agent_user if agent_user is not None else (None if operation in PUBLIC else auth.authenticate(request, db))
                user_id = user.id if user else None
                mutating = request.method != "GET" and operation not in READ_ONLY_POST
                if mutating and agent_user is None:
                    auth.check_csrf(request, settings)
                elif operation in READ_ONLY_POST and agent_user is None:
                    auth.check_origin(request, settings)
                data, query = request_input(request, spec, path_spec, document)
                ctx = Context(request, db, user, data, query, operation, dispatch)
                record = None
                if mutating and operation not in WRITE_EXCEPTIONS:
                    record, replay = begin_idempotency(ctx)
                    if replay is not None:
                        return completed_response(response_for(replay), request), []
                if operation in ASYNC_OIDC_OPERATIONS:
                    # Only these two handlers may await. Their HTTP clients and
                    # coroutine run on this worker's own loop; no ASGI receive is
                    # used here. The public handlers do no SQL before network I/O.
                    # One OIDC admission token preserves oidc_pending serialization.
                    result = asyncio.run(handlers[operation](ctx))
                else:
                    result = handlers[operation](ctx)
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise RuntimeError("Synchronous operation returned an awaitable")
                if mutating:
                    from .vector_indexing import follow_mutation
                    follow_mutation(ctx,result)
                if isinstance(result, Result):
                    schema = deref(document, spec["responses"].get(str(result.status), {})).get(
                        "content", {}).get("application/json", {}).get("schema")
                    if schema:
                        validator(schema, document).validate(primitive(result.body))
                if record:
                    if not isinstance(result, Result):
                        raise RuntimeError("Idempotent writes must return a structured Result")
                    record.state, record.status_code = "COMPLETED", result.status
                    record.response = primitive({"body": result.body, "headers": result.headers})
                if read_only and (db.new or db.dirty or db.deleted or dispatch):
                    raise RuntimeError("Read-only operation attempted a mutation or dispatch")
                # Serialize on the owner thread too; no ORM object or open Session
                # reaches the ASGI loop. A serialization failure rolls back writes.
                response = response_for(result)
                db.commit()
            except APIError as exc:
                db.rollback()
                if user_id and request.method != "GET" and operation not in READ_ONLY_POST:
                    try:
                        db.add(m.AuditEvent(id=uid(), actor_id=user_id, action=operation, object_type="request",
                            object_id=None, outcome="DENIED" if exc.status in {401, 403, 404} else "FAILED",
                            trace_id=request.state.trace_id, details={"code": exc.code}))
                        db.commit()
                    except Exception:  # noqa: BLE001 - preserve the original denial if auditing fails.
                        db.rollback()
                return error_response(exc, request.state.trace_id), []
            except (IntegrityError, StaleDataError):
                db.rollback()
                return error_response(APIError(409, "CONCURRENT_OR_DEPENDENCY_CONFLICT",
                    "并发写入或对象依赖冲突"), request.state.trace_id), []
            except (OperationalError, StorageError, FileNotFoundError):
                db.rollback()
                if getattr(app.state, "raise_test_errors", False):
                    raise
                return error_response(APIError(503, "BACKEND_UNAVAILABLE",
                    "存储或数据库暂不可用"), request.state.trace_id), []
            except Exception as exc:
                db.rollback()
                log.error("API failure operation=%s trace=%s type=%s",
                          operation, request.state.trace_id, type(exc).__name__)
                if getattr(app.state, "raise_test_errors", False):
                    raise
                return error_response(APIError(500, "INTERNAL_ERROR",
                    "处理失败，请凭trace_id联系管理员"), request.state.trace_id), []
        return completed_response(response, request), dispatch

    def make_endpoint(spec, path_spec):
        operation = spec["operationId"]
        read_only = operation in READ_ONLY_OPERATIONS
        oidc = operation in ASYNC_OIDC_OPERATIONS
        if inspect.iscoroutinefunction(handlers[operation]) != oidc:
            raise RuntimeError(f"Operation must declare its synchronous/OIDC execution contract: {operation}")

        async def endpoint(request: Request):
            request.state.trace_id = uid()
            try:
                await buffer_body(request, spec)
            except APIError as exc:
                return error_response(exc, request.state.trace_id)
            response, dispatch = await offload(execute_request, request, spec, path_spec, operation,
                                               read_only=read_only, oidc=oidc)
            # The business, audit and outbox transaction has closed. Even the
            # synchronous enqueue/outbox-delivery write must not block ASGI.
            dispatcher = app.state.job_dispatcher
            if dispatcher:
                for job_id in dispatch:
                    try:
                        outcome = await offload(dispatcher, job_id)
                        if inspect.isawaitable(outcome):
                            await outcome
                    except Exception:  # noqa: BLE001 - the committed outbox retains retry authority.
                        log.warning("Job dispatch deferred to durable outbox job=%s", job_id)
            return response

        endpoint.__name__ = operation
        return endpoint

    operations = []
    for path, path_spec in document["paths"].items():
        for method, spec in path_spec.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation = spec["operationId"]
            if operation not in handlers:
                raise RuntimeError(f"Missing operation implementation: {operation}")
            if method == "get" and operation not in READ_ONLY_OPERATIONS | ASYNC_OIDC_OPERATIONS:
                raise RuntimeError(f"GET operation has no reviewed read/OIDC capability: {operation}")
            if operation in READ_ONLY_OPERATIONS and method != "get" and not (
                operation in READ_ONLY_POST and method == "post"
            ):
                raise RuntimeError(f"Read capability registered for an unexpected HTTP method: {operation}")
            if operation in ASYNC_OIDC_OPERATIONS and method != "get":
                raise RuntimeError(f"OIDC operation registered for an unexpected HTTP method: {operation}")
            app.add_api_route(settings.api_prefix + path, make_endpoint(spec, path_spec), methods=[method.upper()],
                operation_id=operation, name=operation)
            operations.append(operation)
    if len(baseline_operations) != 61 or len(set(operations)) != len(operations):
        raise RuntimeError("Expected 61 unique baseline operations plus unique extensions")

    def execute_special(request, operation):
        read_only = operation != "demoLogin"
        with (read_factory if read_only else factory)() as db:
            try:
                if operation == "demoUsers":
                    response = JSONResponse(auth.demo_users(request, db))
                elif operation == "demoLogin":
                    data = json.loads(request.state.prefetched_request_body)
                    response = auth.demo_login(request, db, data)
                else:
                    auth.authenticate(request, db)
                    vector = app.state.vector_index
                    response = JSONResponse({"database": database_capabilities(engine),
                        "auth_mode": settings.auth_mode, "app_env": settings.app_env,
                        "model": {"provider": settings.llm_provider, "configured": settings.llm_provider == "evidence"
                            or bool(settings.llm_base_url and settings.llm_model), "live_model_verified": False},
                        "retrieval_mode": settings.retrieval_mode,
                        "answer_scopes": ["formal", "reference"],
                        "vector": vector.status() if vector is not None else {"state": "disabled", "mode": "wiki"},
                        "dispatcher_ready": app.state.job_dispatcher is not None,
                        "baseline_operations": len(baseline_operations), "total_operations": len(operations),
                        "extensions": [module.__name__ for module in extension_modules]})
                db.commit()
                return completed_response(response, request)
            except APIError as exc:
                db.rollback()
                return error_response(exc, request.state.trace_id)
            except (ValueError, TypeError):
                db.rollback()
                if operation != "demoLogin":
                    raise
                return error_response(APIError(422, "INVALID_INPUT", "演示登录请求无效"), request.state.trace_id)
            except (OperationalError, StorageError, FileNotFoundError):
                db.rollback()
                if getattr(app.state, "raise_test_errors", False):
                    raise
                return error_response(APIError(503, "BACKEND_UNAVAILABLE", "存储或数据库暂不可用"), request.state.trace_id)

    @app.get(settings.api_prefix + "/auth/demo", include_in_schema=False)
    async def get_demo(request: Request):
        request.state.trace_id = uid()
        return await offload(execute_special, request, "demoUsers", read_only=True)

    @app.post(settings.api_prefix + "/auth/demo", include_in_schema=False)
    async def post_demo(request: Request):
        request.state.trace_id = uid()
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        try:
            await buffer_body(request, {"requestBody": {"content": {content_type: {}}}})
        except APIError as exc:
            return error_response(exc, request.state.trace_id)
        return await offload(execute_special, request, "demoLogin")

    @app.get(settings.api_prefix + "/system/status", include_in_schema=False)
    async def system_status(request: Request):
        request.state.trace_id = uid()
        return await offload(execute_special, request, "systemStatus", read_only=True)

    # Preserve the source contract for generated clients rather than replacing it with Request-only schemas.
    app.openapi = lambda: document
    return app
