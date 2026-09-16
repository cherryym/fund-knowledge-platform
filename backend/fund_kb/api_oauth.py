"""v6 OAuth extension. Durable jobs contain references only, never login challenges."""
from __future__ import annotations

import copy
from datetime import UTC, datetime

from sqlalchemy import select

from . import models as m
from .api_models import _ID, _JOB_REF, _UUID, _op
from .codex_bridge import capabilities
from .codex_bridge_config import bridge_for
from .providers import ProviderError, authorize_model_job, load_connection, update_connection_status
from .services import (
    Result,
    audit,
    bump,
    create_job,
    fail,
    job_dict,
    now,
    primitive,
    require_etag,
    tagged,
    uid,
)

NAMESPACE = "model-oauth:"
OAUTH_TASKS = {"CODEX_LOGIN_START", "CODEX_AUTH_REFRESH", "CODEX_LOGIN_CANCEL", "CODEX_LOGOUT"}
SCHEMAS = {
    "ModelOAuthState": {"type": "object", "required": ["connection_id", "owner_user_id", "connection_revision",
        "oauth_revision", "auth_epoch", "state", "capabilities"], "properties": {
            "connection_id": _UUID, "owner_user_id": _UUID, "connection_revision": {"type": "integer"},
            "oauth_revision": {"type": "integer"}, "auth_epoch": {"type": "integer"},
            "state": {"type": "string"}, "capabilities": {"type": "object"}}},
    "ModelOAuthChallenge": {"type": "object", "required": ["attempt_id", "flow", "local_expires_at"],
        "properties": {"attempt_id": _UUID, "flow": {"enum": ["device_code", "browser"]},
            "local_expires_at": {"type": "string", "format": "date-time"},
            "authorization_url": {"type": ["string", "null"]}, "verification_url": {"type": ["string", "null"]},
            "user_code": {"type": ["string", "null"]}}},
}
PATHS = {
    "/model-connections/{id}/oauth": {"parameters": _ID,
        "get": _op("getModelOAuthState", {"$ref": "#/components/schemas/ModelOAuthState"}, etag=True)},
    "/model-connections/{id}/oauth/challenge": {"parameters": _ID,
        "get": _op("getModelOAuthChallenge", {"$ref": "#/components/schemas/ModelOAuthChallenge"},
            query=[{"name": "attempt_id", "in": "query", "required": True, "schema": _UUID}])},
}
OPERATIONS = {"startModelOAuthLogin": "CODEX_LOGIN_START", "refreshModelOAuthState": "CODEX_AUTH_REFRESH",
    "cancelModelOAuthLogin": "CODEX_LOGIN_CANCEL", "logoutModelOAuth": "CODEX_LOGOUT"}
for _suffix, _operation in (("start", "startModelOAuthLogin"), ("refresh", "refreshModelOAuthState"),
    ("cancel", "cancelModelOAuthLogin"), ("logout", "logoutModelOAuth")):
    _fields = {"connection_revision": {"type": "integer", "minimum": 1}}
    _required = ["connection_revision"]
    if _suffix == "start":
        _fields["flow"] = {"enum": ["device_code", "browser"]}
        _required.append("flow")
    if _suffix == "cancel":
        _fields["attempt_id"] = _UUID
        _required.append("attempt_id")
    PATHS[f"/model-connections/{{id}}/oauth/{_suffix}"] = {"parameters": copy.deepcopy(_ID),
        "post": _op(_operation, _JOB_REF, write=True, status="202", etag=True,
            body={"type": "object", "additionalProperties": False, "required": _required, "properties": _fields})}


def create_state(ctx, connection):
    state = m.RuntimePolicy(id=uid(), name=NAMESPACE + connection.id, updated_by=ctx.user.id, config={
        "connection_id": connection.id, "owner_user_id": ctx.user.id, "auth_epoch": 0,
        "connection_revision": connection.revision, "state": "SIGNED_OUT", "attempt_id": None,
        "active_job_id": None, "account": None, "checked_at": None, "state_source": "not_checked",
        "last_error_code": None})
    ctx.db.add(state)
    ctx.db.flush()
    return state


def state_policy(db, connection):
    if connection.config.get("protocol") != "codex_app_server":
        fail(409, "OAUTH_NOT_APPLICABLE", "该连接不使用官方 ChatGPT 登录")
    state = db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name == NAMESPACE + connection.id)).first()
    if not state or state.config.get("owner_user_id") != connection.config.get("owner_user_id"):
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    return state


def public_state(ctx, connection, state):
    value = state.config
    bridge = bridge_for(ctx)
    result = {k: primitive(value.get(k)) for k in ("state", "attempt_id", "active_job_id", "account",
        "checked_at", "state_source", "last_error_code")}
    result.update(connection_id=connection.id, owner_user_id=ctx.user.id, connection_revision=connection.revision,
        oauth_revision=state.revision, auth_epoch=value["auth_epoch"], capabilities=capabilities(bridge))
    if not connection.config.get("enabled") or connection.config.get("deleted_at"):
        result.update(state="DISABLED", account=None)
    elif value.get("connection_revision") != connection.revision:
        result.update(state="ERROR", account=None, last_error_code="CONNECTION_REVISION_CHANGED")
    elif value["state"] in {"AUTHENTICATED", "PENDING"}:
        session = bridge.sessions.get((ctx.user.id, connection.id)) if bridge else None
        if not session or session.epoch != value["auth_epoch"] or session.revision != connection.revision:
            result.update(state="ERROR", account=None, last_error_code="CODEX_SESSION_UNAVAILABLE")
    return result


def _get(ctx):
    connection = load_connection(ctx.db, ctx.user, ctx.id, ctx.settings)
    return connection, state_policy(ctx.db, connection)


def read_state(ctx):
    connection, state = _get(ctx)
    result = tagged(public_state(ctx, connection, state), state)
    result.headers["Cache-Control"] = "private, no-store"
    return result


def _required_bridge(ctx):
    bridge = bridge_for(ctx)
    reason = capabilities(bridge)["auth_blocked_reason"]
    if reason:
        fail(409, reason, "官方登录适配未就绪，请检查独立运行目录、可执行文件和加密托管配置")
    return bridge


def challenge(ctx):
    connection, state = _get(ctx)
    if state.config["state"] != "PENDING" or not connection.config.get("enabled"):
        fail(409, "CODEX_LOGIN_STALE", "此登录请求已失效，请重新读取登录状态")
    try:
        value = _required_bridge(ctx).challenge(ctx.user.id, connection.id, state.config["auth_epoch"],
            connection.revision, ctx.query["attempt_id"])
    except ProviderError as exc:
        fail(409, exc.code, "此登录请求已失效，请重新读取登录状态")
    value["local_expires_at"] = datetime.fromtimestamp(value["local_expires_at"], UTC).isoformat()
    return Result(value, headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})


def mutate(ctx):
    connection, state = _get(ctx)
    require_etag(ctx, state)
    if connection.revision != ctx.data["connection_revision"]:
        fail(409, "CONNECTION_REVISION_CHANGED", "连接已更新，请重新读取")
    if not connection.config.get("enabled"):
        fail(409, "CONNECTION_DISABLED", "连接已停用")
    bridge = _required_bridge(ctx)
    task = OPERATIONS[ctx.operation]
    config = dict(state.config)
    active = ctx.db.get(m.Job, config["active_job_id"]) if config.get("active_job_id") else None
    if task in {"CODEX_LOGIN_START", "CODEX_AUTH_REFRESH"} and active and active.state in {"QUEUED", "RUNNING"}:
        fail(409, "CODEX_LOGIN_IN_PROGRESS", "身份操作仍在执行")
    if task == "CODEX_LOGIN_START":
        if config["state"] in {"STARTING", "PENDING", "AUTHENTICATED", "SIGNING_OUT", "CANCELLING"}:
            fail(409, "CODEX_LOGIN_IN_PROGRESS", "请先完成、取消或退出当前登录")
        if ctx.data["flow"] == "browser" and not bridge.config.browser_callback_reachable:
            fail(409, "CODEX_BROWSER_CALLBACK_UNAVAILABLE", "此部署请使用设备码登录")
        config.update(auth_epoch=config["auth_epoch"] + 1, attempt_id=uid(), state="STARTING", account=None)
    elif task == "CODEX_LOGIN_CANCEL":
        if config["attempt_id"] != ctx.data["attempt_id"] or config["state"] not in {"STARTING", "PENDING"}:
            fail(409, "CODEX_LOGIN_STALE", "此登录请求已失效")
        config.update(auth_epoch=config["auth_epoch"] + 1, state="CANCELLING", account=None)
    elif task == "CODEX_LOGOUT":
        config.update(auth_epoch=config["auth_epoch"] + 1, state="SIGNING_OUT", account=None)
    config.update(connection_revision=connection.revision, last_error_code=None)
    payload = {"task": task, "owner_user_id": ctx.user.id, "connection_id": connection.id,
        "connection_revision": connection.revision, "auth_epoch": config["auth_epoch"],
        "attempt_id": config["attempt_id"]}
    if task == "CODEX_LOGIN_START":
        payload["flow"] = ctx.data["flow"]
    job = create_job(ctx, "COMPILE", payload)
    config["active_job_id"] = job.id
    bump(ctx.db, state, config=config, updated_by=ctx.user.id)
    audit(ctx, "model_oauth.requested", connection, {"task": task, "job_id": job.id})
    return Result(job_dict(job), 202, {"ETag": f'"{state.revision}"', "Cache-Control": "private, no-store"})


def invalidate_connection(ctx, connection):
    state = state_policy(ctx.db, connection)
    config = {**state.config, "state": "SIGNING_OUT", "account": None,
        "auth_epoch": state.config["auth_epoch"] + 1, "connection_revision": connection.revision,
        "last_error_code": None}
    job = create_job(ctx, "COMPILE", {"task": "CODEX_LOGOUT", "owner_user_id": ctx.user.id,
        "connection_id": connection.id, "connection_revision": connection.revision,
        "auth_epoch": config["auth_epoch"], "attempt_id": config.get("attempt_id")})
    config["active_job_id"] = job.id
    bump(ctx.db, state, config=config, updated_by=ctx.user.id)


def authorize_oauth_job(db, user, job, settings):
    connection = load_connection(db, user, job.payload.get("connection_id"), settings,
        include_deleted=job.payload.get("task") == "CODEX_LOGOUT")
    state = state_policy(db, connection)
    if state.config["auth_epoch"] != job.payload.get("auth_epoch") \
        or state.config.get("attempt_id") != job.payload.get("attempt_id"):
        fail(409, "CODEX_LOGIN_STALE", "登录身份或任务已失效")
    return state


def require_sync_authority(ctx, connection):
    _required_bridge(ctx)
    state = state_policy(ctx.db, connection)
    if public_state(ctx, connection, state)["state"] != "AUTHENTICATED":
        fail(409, "CODEX_AUTH_REQUIRED", "请先完成本人 ChatGPT 登录")
    return {"auth_epoch": state.config["auth_epoch"], "attempt_id": state.config.get("attempt_id")}


def _job_context(db, settings, job_id, attempt):
    job = db.get(m.Job, job_id)
    if not job or job.state != "RUNNING" or job.attempts != attempt or job.cancel_requested:
        raise ProviderError("MODEL_JOB_CANCELLED_OR_STALE")
    user = db.get(m.User, job.owner_id)
    connection = authorize_model_job(db, user, job, settings, action="execute")
    state = state_policy(db, connection)
    return job, user, connection, state


def run_oauth_job(settings, session_factory, job_id, attempt, checkpoint, *, bridge=None):
    checkpoint("CODEX_OAUTH_AUTHORIZATION", {})
    with session_factory() as db:
        job, user, connection, state = _job_context(db, settings, job_id, attempt)
        payload = dict(job.payload)
        owner, cid = user.id, connection.id
    if payload["task"] not in OAUTH_TASKS:
        raise ProviderError("INVALID_MODEL_JOB")
    result, error = None, None
    try:
        if not bridge or bridge.unavailable_reason:
            if payload["task"] == "CODEX_LOGOUT":
                raise ProviderError("CODEX_LOGOUT_UNCONFIRMED")
            raise ProviderError(capabilities(bridge)["auth_blocked_reason"])
        checkpoint("CODEX_OAUTH_RPC", {"connection_id": cid})
        args = (owner, cid, payload["auth_epoch"], payload["connection_revision"])
        if payload["task"] == "CODEX_LOGIN_START":
            result = bridge.start(*args, payload["attempt_id"], payload["flow"])
        elif payload["task"] == "CODEX_AUTH_REFRESH":
            result = bridge.refresh(*args)
        elif payload["task"] == "CODEX_LOGIN_CANCEL":
            result = bridge.cancel(owner, cid)
        else:
            result = bridge.logout(owner, cid)
    except ProviderError as exc:
        error = exc.code
    except Exception:  # noqa: BLE001 - transport errors must not expose RPC bodies, paths or credentials
        error = "CODEX_RPC_FAILED"
    try:
        checkpoint("CODEX_OAUTH_RECHECK", {"connection_id": cid})
        with session_factory.begin() as db:
            job, user, connection, state = _job_context(db, settings, job_id, attempt)
            config = {**state.config, **(result or {}), "checked_at": primitive(now()),
                "state_source": "app_server" if result else state.config.get("state_source", "not_checked"),
                "last_error_code": error, "active_job_id": None}
            if error:
                config.update(state="ERROR", account=None)
            # SQL compare-and-swap; never overwrite cancel/logout that won the race.
            bump(db, state, config=config, updated_by=user.id)
    except Exception:
        if bridge:
            bridge.discard_epoch(owner, cid, payload["auth_epoch"])
        raise
    if error:
        if bridge:
            bridge.discard_epoch(owner, cid, payload["auth_epoch"])
        raise ProviderError(error)
    return {"connection_id": cid, "state": result["state"], "auth_epoch": payload["auth_epoch"]}


def run_codex_model_sync(settings, session_factory, job_id, attempt, checkpoint, *, bridge=None):
    with session_factory() as db:
        job, user, connection, state = _job_context(db, settings, job_id, attempt)
        if job.payload["task"] not in {"MODEL_SYNC", "MODEL_TEST"}:
            raise ProviderError("CODEX_TEXT_ISOLATION_UNVERIFIED")
        payload = dict(job.payload)
        owner, cid = user.id, connection.id
        if state.config["state"] != "AUTHENTICATED":
            raise ProviderError("CODEX_AUTH_REQUIRED")
    if not bridge or bridge.unavailable_reason:
        raise ProviderError(capabilities(bridge)["auth_blocked_reason"])
    if payload["task"] == "MODEL_TEST":
        from .codex_text import text_snapshot
        from .providers import complete
        with session_factory() as db:
            job, user, connection, state = _job_context(db, settings, job_id, attempt)
            snapshot = text_snapshot(db, user, connection, payload.get("model_id"), settings, connection.config["space_id"])
        original_guard = snapshot["_authority_check"]
        def guard():
            original_guard()
            with session_factory() as db:
                _job_context(db, settings, job_id, attempt)
        snapshot["_authority_check"] = guard
        checkpoint("CODEX_TEXT_PROBE", {"connection_id": cid})
        import json
        error_code = None
        response = None
        try:
            response = complete(snapshot, [{"role":"user", "content":'Return exactly {"ok":true}.'}], max_tokens=1024,
                timeout=min(120, settings.model_timeout_seconds))
            if json.loads(response["choices"][0]["message"]["content"]) != {"ok": True}:
                raise ProviderError("MODEL_PROBE_INVALID_RESPONSE")
        except ProviderError as exc:
            error_code = exc.code
        checkpoint("CODEX_TEXT_PROBE_RECHECK", {"connection_id": cid})
        with session_factory.begin() as db:
            job, user, connection, state = _job_context(db, settings, job_id, attempt)
            update_connection_status(db, connection, {"status":"ERROR" if error_code else "TESTED", "last_error_code":error_code}, owner_id=owner)
        if error_code:
            raise ProviderError(error_code)
        return {"connection_id":cid, "model_id":payload["model_id"], "status":"TESTED",
            "inference_supported":True, "probe_ok":True, "usage":response["usage"]}
    checkpoint("CODEX_MODEL_LIST", {"connection_id": cid})
    rows, truncated = bridge.list_models(owner, cid, payload["auth_epoch"], payload["connection_revision"])
    checkpoint("CODEX_MODEL_LIST_RECHECK", {"connection_id": cid})
    with session_factory.begin() as db:
        job, user, connection, state = _job_context(db, settings, job_id, attempt)
        update_connection_status(db, connection, {"synced_models": rows, "models": rows,
            "last_synced_at": primitive(now()), "status": "PARTIAL_SYNC" if truncated else "SYNCED",
            "last_error_code": None}, owner_id=owner)
    return {"connection_id": cid, "model_count": len(rows), "truncated": truncated,
        "inference_supported": capabilities(bridge)["inference"]}


def replay_authority(ctx, cached):
    body = cached.get("body")
    if not isinstance(body, dict) or any(k in body for k in ("authUrl", "userCode", "accessToken",
        "authorization_url", "user_code", "verification_url", "credential_ciphertext")):
        fail(409, "CODEX_LOGIN_STALE", "登录挑战不允许幂等重放，请读取当前状态")
    job = ctx.db.get(m.Job, body.get("id"))
    authorize_model_job(ctx.db, ctx.user, job, ctx.settings, action="replay")
    # Keep the normal Job shape: the main wrapper continues its job_access fallback.
    cached["body"] = job_dict(job)
    cached.setdefault("headers", {})["Cache-Control"] = "private, no-store"


HANDLERS = {"getModelOAuthState": read_state, "getModelOAuthChallenge": challenge,
    **{name: mutate for name in OPERATIONS}}
