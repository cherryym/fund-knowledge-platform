"""Provider API extension; registered by the existing CSRF/idempotency/transaction wrapper."""
from __future__ import annotations

import copy

from sqlalchemy import select

from . import models as m
from .provider_catalog import PROTOCOLS, provider_by_id, public_catalog
from .providers import (
    NAMESPACE,
    ProviderError,
    allowed_env_references,
    authorize_env_reference,
    authorize_model_job,
    connection_models,
    encrypt_credential,
    load_connection,
    public_connection,
    require_connection_space,
    validate_base_url,
)
from .services import (
    Result,
    audit,
    bump,
    create_job,
    deployment_admin,
    fail,
    job_dict,
    now,
    primitive,
    require_etag,
    tagged,
    uid,
)

_UUID = {"type": "string", "format": "uuid"}
_MODEL = {"type": "object", "additionalProperties": False, "required": ["id", "name", "brand"],
    "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 512,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._:/+@-]*$"}, "name": {"type": "string", "minLength": 1, "maxLength": 300},
        "brand": {"type": "string", "minLength": 1, "maxLength": 100}}}
_FIELDS = {"space_id": _UUID, "name": {"type": "string", "minLength": 1, "maxLength": 200},
    "provider_id": {"type": "string", "minLength": 1, "maxLength": 100},
    "base_url": {"type": "string", "maxLength": 2048}, "protocol": {"enum": sorted(PROTOCOLS)},
    "enabled": {"type": "boolean"}, "credential_mode": {"enum": ["encrypted", "env", "none", "chatgpt_oauth"]},
    "api_key": {"type": "string", "minLength": 1, "maxLength": 8192, "writeOnly": True},
    "api_key_env": {"type": ["string", "null"], "maxLength": 105},
    "allow_document_transfer": {"type": "boolean", "default": False},
    "custom_models": {"type": "array", "maxItems": 1000, "items": _MODEL}}
SCHEMAS = {
    "ModelConnectionCreate": {"type": "object", "additionalProperties": False,
        "required": ["space_id", "name", "provider_id", "credential_mode"], "properties": _FIELDS},
    "ModelConnectionPatch": {"type": "object", "additionalProperties": False, "minProperties": 1, "properties": _FIELDS},
    "ModelConnection": {"type": "object", "additionalProperties": False,
        "required": ["id", "owner_user_id", "space_id", "name", "provider_id", "protocol", "base_url", "enabled", "credential_mode",
            "api_key_env", "credential_present", "allow_document_transfer", "revision", "status", "last_error_code", "last_synced_at", "models"],
        "properties": {"id": _UUID, "owner_user_id": _UUID, "space_id": _UUID,
            "capabilities": {"type": "object"}, "oauth_state": {"type": "object"},
            "name": {"type": "string"}, "provider_id": {"type": "string"},
            "protocol": {"enum": sorted(PROTOCOLS)}, "base_url": {"type": "string"}, "enabled": {"type": "boolean"},
            "credential_mode": {"enum": ["encrypted", "env", "none", "chatgpt_oauth"]}, "api_key_env": {"type": ["string", "null"]},
            "credential_present": {"type": "boolean"}, "allow_document_transfer": {"type": "boolean"},
            "revision": {"type": "integer"}, "status": {"type": "string"}, "last_error_code": {"type": ["string", "null"]},
            "last_synced_at": {"type": ["string", "null"], "format": "date-time"}, "models": {"type": "array", "items": {"type": "object"}}}},
}
_SPACE_QUERY = [{"name": "space_id", "in": "query", "required": True, "schema": _UUID}]
_ID = [{"$ref": "#/components/parameters/Id"}]
_WRITE = {"security": [{"cookieAuth": [], "csrf": []}], "parameters": [{"$ref": "#/components/parameters/Idempotency"}]}


def _response(schema, description="模型连接数据", etag=False):
    return {"description": description, **({"headers": {"ETag": {"$ref": "#/components/headers/ETag"}}} if etag else {}),
        "content": {"application/json": {"schema": schema}}}


def _op(operation_id, schema, *, write=False, status="200", body=None, etag=False, query=None):
    op = {"operationId": operation_id, "responses": {status: _response(schema, etag=etag),
        "default": {"$ref": "#/components/responses/Error"}}}
    if write:
        op.update(copy.deepcopy(_WRITE))
    if query:
        op["parameters"] = copy.deepcopy(query)
    if etag and write:
        op["parameters"].append({"$ref": "#/components/parameters/IfMatch"})
    if body:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    return op


_CONNECTION_REF = {"$ref": "#/components/schemas/ModelConnection"}
_JOB_REF = {"$ref": "#/components/schemas/Job"}
PATHS = {
    "/model-providers": {"get": _op("listModelProviders", {"type": "object", "required": ["items", "live_verified"],
        "properties": {"items": {"type": "array", "items": {"type": "object"}}, "live_verified": {"const": False}}})},
    "/model-connections": {
        "get": _op("listModelConnections", {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": _CONNECTION_REF}}}, query=[{**_SPACE_QUERY[0], "required": False}]),
        "post": _op("createModelConnection", _CONNECTION_REF, write=True, status="201", body={"$ref": "#/components/schemas/ModelConnectionCreate"})},
    "/model-connections/{id}": {"parameters": _ID,
        "get": _op("getModelConnection", _CONNECTION_REF, etag=True),
        "patch": _op("updateModelConnection", _CONNECTION_REF, write=True, etag=True, body={"$ref": "#/components/schemas/ModelConnectionPatch"}),
        "delete": {**copy.deepcopy(_WRITE), "operationId": "deleteModelConnection",
            "parameters": [*_WRITE["parameters"], {"$ref": "#/components/parameters/IfMatch"}],
            "responses": {"204": {"description": "连接软停用并清除凭证；保留历史审计"}, "default": {"$ref": "#/components/responses/Error"}}}},
    "/model-connections/{id}/sync-models": {"parameters": _ID,
        "post": _op("syncConnectionModels", _JOB_REF, write=True, etag=True, status="202")},
    "/model-connections/{id}/test": {"parameters": _ID,
        "post": _op("testModelConnection", _JOB_REF, write=True, etag=True, status="202", body={"type": "object", "additionalProperties": False,
            "properties": {"model_id": {"type": "string", "minLength": 1, "maxLength": 512}}})},
    "/model-options": {"get": _op("listModelOptions", {"type": "object", "required": ["items", "default"],
        "properties": {"items": {"type": "array", "items": {"type": "object"}}, "default": {"type": "null"}}}, query=_SPACE_QUERY)},
}
PATHS["/model-credential-references"] = {"get": _op("listModelCredentialReferences", {"type": "object"})}


def _safe_error(exc):
    fail(422 if exc.code.startswith(("INVALID_", "UNKNOWN_")) else 409, exc.code, "模型连接配置未通过校验")


def build_config(ctx, connection_id, previous=None):
    previous = previous or {}
    body = ctx.data
    space_id = body.get("space_id", previous.get("space_id"))
    if not previous:
        require_connection_space(ctx.db, ctx.user, space_id, ctx.settings)
    if previous and previous["space_id"] != space_id:
        fail(409, "CONNECTION_SPACE_IMMUTABLE", "连接不可跨空间移动；请新建连接")
    provider_id = body.get("provider_id", previous.get("provider_id"))
    provider = provider_by_id(provider_id)
    if not provider:
        fail(422, "UNKNOWN_PROVIDER", "提供商不在目录中")
    mode = body.get("credential_mode", previous.get("credential_mode"))
    protocol = body.get("protocol", previous.get("protocol") if previous.get("provider_id") == provider_id else provider["protocol"])
    base = body.get("base_url", previous.get("base_url") if previous.get("provider_id") == provider_id else provider["base_url"])
    is_codex = provider_id == "chatgpt-codex"
    if previous and (previous.get("provider_id") == "chatgpt-codex") != is_codex:
        fail(409, "CONNECTION_AUTH_TYPE_IMMUTABLE", "请为不同身份方式新建连接")
    if is_codex:
        if mode != "chatgpt_oauth" or protocol != "codex_app_server" or base != "" \
            or any(k in body for k in ("api_key", "api_key_env")) or body.get("custom_models"):
            raise ProviderError("INVALID_CODEX_CONNECTION")
    else:
        if protocol == "codex_app_server" or mode == "chatgpt_oauth":
            raise ProviderError("INVALID_CODEX_CONNECTION")
        base = validate_base_url(base, provider["kind"], getattr(ctx.settings, "provider_local_hosts", []))
    if protocol not in PROTOCOLS:
        fail(422, "UNSUPPORTED_PROTOCOL", "协议不受支持")
    config = {**previous, "id": connection_id, "owner_user_id": ctx.user.id, "space_id": space_id, "provider_id": provider_id,
        "name": body.get("name", previous.get("name")), "protocol": protocol, "base_url": base,
        "enabled": body.get("enabled", previous.get("enabled", True)), "credential_mode": mode,
        "allow_document_transfer": body.get("allow_document_transfer", previous.get("allow_document_transfer", False)),
        "custom_models": copy.deepcopy(body.get("custom_models", previous.get("custom_models", []))),
        "last_error_code": None, "status": "UNVERIFIED", "last_synced_at": previous.get("last_synced_at"),
        "api_key_env": None, "credential_ciphertext": None}
    changed_target = previous and any(config[k] != previous.get(k) for k in ("provider_id", "protocol", "base_url"))
    if mode == "chatgpt_oauth":
        pass
    elif mode == "encrypted":
        if body.get("api_key"):
            config["credential_ciphertext"] = encrypt_credential(ctx.settings, connection_id, space_id, body["api_key"], owner_user_id=ctx.user.id)
        elif previous.get("credential_mode") == "encrypted" and not changed_target:
            config["credential_ciphertext"] = previous.get("credential_ciphertext")
        elif config["enabled"]:
            raise ProviderError("CREDENTIAL_REENTRY_REQUIRED" if changed_target else "CREDENTIAL_MISSING")
        if body.get("api_key_env"):
            raise ProviderError("INVALID_CREDENTIAL_MODE")
    elif mode == "env":
        if "api_key" in body:
            raise ProviderError("INVALID_CREDENTIAL_MODE")
        config["api_key_env"] = authorize_env_reference(ctx.settings, ctx.user, body.get("api_key_env", previous.get("api_key_env")))
    elif mode == "none":
        if body.get("api_key") or body.get("api_key_env"):
            raise ProviderError("INVALID_CREDENTIAL_MODE")
    else:
        raise ProviderError("INVALID_CREDENTIAL_MODE")
    if changed_target:
        config["last_synced_at"], config["synced_models"] = None, []
    custom_ids = [model["id"] for model in config["custom_models"]]
    if len(custom_ids) != len(set(custom_ids)):
        fail(422, "DUPLICATE_CUSTOM_MODEL", "自定义模型ID不能重复")
    config["models"] = connection_models(config)
    if not config["enabled"]:
        config["status"] = "DISABLED"
    return config


def list_providers(ctx):
    return Result(public_catalog())


def _public(ctx, policy):
    result = public_connection(policy, ctx.settings, ctx.user)
    if policy.config.get("protocol") == "codex_app_server":
        from .api_oauth import public_state, state_policy
        result["oauth_state"] = public_state(ctx, policy, state_policy(ctx.db, policy))
        result["capabilities"] = result["oauth_state"]["capabilities"]
        result["credential_present"] = result["oauth_state"]["state"] == "AUTHENTICATED"
    return result


def credential_references(ctx):
    return Result({"items": [{"name": name} for name in allowed_env_references(ctx.settings, ctx.user)],
        "can_enter_env_reference": deployment_admin(ctx.user, ctx.settings)})


def list_connections(ctx):
    space_id = ctx.query.get("space_id")
    if space_id:
        require_connection_space(ctx.db, ctx.user, space_id, ctx.settings)
    items = []
    for policy in ctx.db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like(NAMESPACE + "%"))):
        if policy.config.get("owner_user_id") == ctx.user.id and not policy.config.get("deleted_at"):
            # Revoked references are not probed for presence; retain a safe editable row.
            try:
                load_connection(ctx.db, ctx.user, policy.id, ctx.settings)
            except Exception as exc:
                from .services import APIError
                if isinstance(exc, APIError) and exc.code == "CREDENTIAL_REFERENCE_FORBIDDEN":
                    continue
                raise
            items.append(_public(ctx, policy))
    return Result({"items": sorted(items, key=lambda x: (x["name"], x["id"]))})


def create_connection(ctx):
    id = uid()
    try:
        config = build_config(ctx, id)
    except ProviderError as exc:
        _safe_error(exc)
    policy = m.RuntimePolicy(id=id, name=NAMESPACE + id, config=config, updated_by=ctx.user.id)
    ctx.db.add(policy)
    ctx.db.flush()
    if config["protocol"] == "codex_app_server":
        from .api_oauth import create_state
        create_state(ctx, policy)
    audit(ctx, "model_connection.created", policy, {"space_id": config["space_id"], "provider_id": config["provider_id"]})
    return tagged(_public(ctx, policy), policy, 201)


def connection(ctx):
    policy = load_connection(ctx.db, ctx.user, ctx.id, ctx.settings, manage=ctx.operation != "getModelConnection")
    if ctx.operation == "getModelConnection":
        return tagged(_public(ctx, policy), policy)
    require_etag(ctx, policy)
    transfer_only = (ctx.operation == "updateModelConnection" and set(ctx.data) == {"allow_document_transfer"}
        and policy.config.get("protocol") == "codex_app_server")
    if ctx.operation == "deleteModelConnection":
        config = {**policy.config, "enabled": False, "credential_ciphertext": None,
            "api_key_env": None, "credential_mode": "none", "deleted_at": primitive(now()), "status": "DISABLED"}
    else:
        try:
            config = build_config(ctx, policy.id, policy.config)
        except ProviderError as exc:
            _safe_error(exc)
    if transfer_only:
        config["status"] = policy.config.get("status", "UNVERIFIED")
        config["last_error_code"] = policy.config.get("last_error_code")
    bump(ctx.db, policy, config=config, updated_by=ctx.user.id)
    if config.get("protocol") == "codex_app_server":
        from .api_oauth import invalidate_connection, state_policy
        if transfer_only:
            # Consent is an application policy, not a change of OAuth identity.
            # Old job snapshots still fail the changed connection revision.
            state = state_policy(ctx.db, policy)
            if state.config.get("state") in {"STARTING", "PENDING", "CANCELLING", "SIGNING_OUT"}:
                fail(409, "CODEX_LOGIN_IN_PROGRESS", "请先完成当前登录操作")
            bump(ctx.db, state, config={**state.config, "connection_revision": policy.revision}, updated_by=ctx.user.id)
        else:
            invalidate_connection(ctx, policy)
    audit(ctx, "model_connection.deleted" if ctx.operation == "deleteModelConnection" else "model_connection.updated",
        policy, {"space_id": config["space_id"], "provider_id": config["provider_id"]})
    return tagged(None, policy, 204) if ctx.operation == "deleteModelConnection" else tagged(_public(ctx, policy), policy)


def connection_job(ctx):
    policy = load_connection(ctx.db, ctx.user, ctx.id, ctx.settings, manage=True)
    require_etag(ctx, policy)
    if not policy.config.get("enabled"):
        fail(409, "CONNECTION_DISABLED", "连接已停用")
    task = "MODEL_SYNC" if ctx.operation == "syncConnectionModels" else "MODEL_TEST"
    payload = {"task": task, "connection_id": policy.id, "connection_revision": policy.revision,
        "owner_user_id": ctx.user.id}
    if policy.config["protocol"] == "codex_app_server":
        from .api_oauth import require_sync_authority
        if task == "MODEL_TEST":
            from .codex_bridge import capabilities
            from .codex_bridge_config import bridge_for
            if not capabilities(bridge_for(ctx))["inference"]:
                fail(409, "CODEX_TEXT_ISOLATION_UNVERIFIED", "严格文本隔离尚未验证，推理不可用")
        payload.update(require_sync_authority(ctx, policy))
    if ctx.data.get("model_id"):
        payload["model_id"] = ctx.data["model_id"]
    elif policy.config["protocol"] == "codex_app_server" and task == "MODEL_TEST":
        from .codex_bridge_config import bridge_for
        engine = getattr(bridge_for(ctx), "text_engine", None)
        available = [row["id"] for row in connection_models(policy.config) if engine and row["id"] in engine.models]
        if not available:
            fail(409, "MODEL_NOT_CONFIGURED", "没有可探测的已验证模型")
        payload["model_id"] = available[0]
    job = create_job(ctx, "COMPILE", payload)
    return Result(job_dict(job), 202)


def options(ctx):
    connections = list_connections(ctx).body["items"]
    result = []
    for config in connections:
        if not config["enabled"]:
            continue
        provider = provider_by_id(config["provider_id"])
        configured = config["credential_present"] or (config["credential_mode"] == "none" and provider["kind"] in {"local", "custom"})
        for model in config["models"]:
            codex = config["protocol"] == "codex_app_server"
            from .codex_bridge_config import bridge_for
            engine = getattr(bridge_for(ctx), "text_engine", None)
            text_ready = bool(engine and model["id"] in engine.models and (config.get("capabilities") or {}).get("inference"))
            result.append({"connection_id": config["id"], "connection_name": config["name"],
                "provider_id": config["provider_id"], "kind": provider["kind"], "protocol": config["protocol"],
                "model_id": model["id"], "model_name": model["name"], "brand": model["brand"],
                "flagship": bool(model.get("flagship")), "configured": configured,
                "owner_user_id": config["owner_user_id"], "connection_revision": config["revision"],
                "selectable": configured and (not codex or text_ready),
                "blocked_reason": "CODEX_TEXT_ISOLATION_UNVERIFIED" if codex and not text_ready else None,
                "allow_document_transfer": config["allow_document_transfer"]})
    return Result({"items": result, "default": None})


def replay_authority(ctx, cached):
    """Called before returning cached mutation results; it never decrypts a credential."""
    body = cached.get("body") or {}
    if ctx.operation in {"syncConnectionModels", "testModelConnection"}:
        authorize_model_job(ctx.db, ctx.user, ctx.db.get(m.Job, body.get("id")), ctx.settings, action="replay")
        cached["body"] = job_dict(ctx.db.get(m.Job, body.get("id")))
        return
    policy = load_connection(ctx.db, ctx.user, ctx.id or body.get("id"), ctx.settings,
        include_deleted=ctx.operation == "deleteModelConnection")
    if cached.get("headers", {}).get("ETag") != f'"{policy.revision}"':
        fail(409, "CONNECTION_REVISION_CHANGED", "连接已更新，旧结果不可重放")
    if body and body.get("owner_user_id") != ctx.user.id:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if ctx.query.get("space_id"):
        require_connection_space(ctx.db, ctx.user, ctx.query["space_id"], ctx.settings)


HANDLERS = {"listModelProviders": list_providers, "listModelConnections": list_connections,
    "createModelConnection": create_connection, "getModelConnection": connection,
    "updateModelConnection": connection, "deleteModelConnection": connection,
    "syncConnectionModels": connection_job, "testModelConnection": connection_job, "listModelOptions": options,
    "listModelCredentialReferences": credential_references}
