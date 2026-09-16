"""Narrow, user-owned Agent credentials; no models, jobs, or external access.

RuntimePolicy supplies a portable unique request key and optimistic revision.
Only the create response ever contains the plaintext credential. Reads and
authentication deliberately do not update last_seen or create audit receipts.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from . import models as m
from . import services as svc

POLICY_PREFIX = "agent-access:v1:"
TOKEN_PREFIX = "fkb_agent_"
SCOPES = frozenset({"capabilities:read", "runs:write", "sources:read"})
# This is an operation allowlist, never a URL-prefix or GET-wide exemption.
# Resource/owner/source authorization remains mandatory inside the handlers.
AGENT_OPERATIONS = {
    "listCapabilities": "capabilities:read",
    "getCapability": "capabilities:read",
    "getCapabilityVersion": "capabilities:read",
    "listCapabilityRuns": "capabilities:read",
    "getCapabilityRun": "capabilities:read",
    "getCapabilityRunNext": "capabilities:read",
    "createCapabilityRun": "runs:write",
    "reportCapabilityStep": "runs:write",
    "cancelCapabilityRun": "runs:write",
    "getCapabilityRunSources": "sources:read",
}
WRITE_OPERATIONS = frozenset({"createCapabilityRun", "reportCapabilityStep", "cancelCapabilityRun"})
_TOKEN = re.compile(r"fkb_agent_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})", re.ASCII)
_CONFIG_KEYS = frozenset({"schema_version", "owner_id", "request_id", "request_sha256", "name", "space_id",
    "scopes", "expires_at", "revoked_at", "created_at", "token_sha256"})
_METADATA_KEYS = ("name", "space_id", "scopes", "expires_at", "revoked_at", "created_at")


def _uuid(value):
    if not isinstance(value, str):
        raise TypeError("UUID required")
    return str(UUID(value))


def _datetime(value):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Timestamp required")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp timezone required")
    return parsed.astimezone(UTC)


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_name(owner_id, request_id):
    return f"{POLICY_PREFIX}{owner_id}:{request_id}"


def _validate_config(row):
    """Corrupt/unknown credential records never confer access."""
    try:
        config = row.config
        if not isinstance(config, dict) or set(config) != _CONFIG_KEYS:
            raise ValueError()
        if type(config["schema_version"]) is not int or config["schema_version"] != 1:
            raise ValueError()
        for key in ("owner_id", "request_id", "space_id"):
            if _uuid(config[key]) != config[key]:
                raise ValueError()
        if row.name != _request_name(config["owner_id"], config["request_id"]):
            raise ValueError()
        if row.updated_by != config["owner_id"] or type(row.revision) is not int or row.revision < 1:
            raise ValueError()
        if not isinstance(config["name"], str) or not config["name"].strip() or len(config["name"]) > 200:
            raise ValueError()
        scopes = config["scopes"]
        if not isinstance(scopes, list) or not scopes or any(not isinstance(s, str) for s in scopes) \
                or len(scopes) != len(set(scopes)) or set(scopes) - SCOPES:
            raise ValueError()
        for key in ("token_sha256", "request_sha256"):
            if not isinstance(config[key], str) or not re.fullmatch(r"[a-f0-9]{64}", config[key]):
                raise ValueError()
        created, expires = _datetime(config["created_at"]), _datetime(config["expires_at"])
        if expires <= created:
            raise ValueError()
        if config["revoked_at"] is not None and _datetime(config["revoked_at"]) < created:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        svc.fail(401, "AGENT_ACCESS_INVALID", "Agent访问凭据无效或已失效")
    return config


def metadata(row):
    config = _validate_config(row)
    return {"id": row.id, **{key: list(config[key]) if key == "scopes" else config[key]
        for key in _METADATA_KEYS}, "revision": row.revision}


def _current_space_readable(db, user, space_id):
    # Bypass projection memoization; column SELECTs recheck governance/membership
    # inside this HTTP request's transaction, never an identity-map grant.
    return bool(svc._roles_uncached(db, user, space_id))


def authenticate_agent(request, db, operation):
    """Return User, or None *only* when Authorization is absent.

    The API wrapper must call this before Cookie auth (also for public routes),
    and skip CSRF only after a successful result. Invalid Bearer credentials must
    never fall back to a Cookie. Each HTTP call must own a new DB transaction.
    """
    request.state.agent_access = None
    values = request.headers.getlist("authorization")
    if not values:
        return None
    if len(values) != 1:
        svc.fail(401, "AGENT_ACCESS_INVALID", "Agent访问凭据无效或已失效")
    authorization = values[0]
    scheme, separator, raw = authorization.partition(" ")
    match = _TOKEN.fullmatch(raw) if separator and scheme.lower() == "bearer" else None
    if not match:
        svc.fail(401, "AGENT_ACCESS_INVALID", "Agent访问凭据无效或已失效")
    required_scope = AGENT_OPERATIONS.get(operation)
    if required_scope is None or request.method != ("POST" if operation in WRITE_OPERATIONS else "GET"):
        svc.fail(403, "AGENT_OPERATION_FORBIDDEN", "Agent凭据不允许调用此接口")
    # Column projection prevents a previously loaded ORM record from restoring
    # revoked scopes or credentials. No plaintext is retained on request.state.
    row = db.execute(select(m.RuntimePolicy.id, m.RuntimePolicy.name, m.RuntimePolicy.config,
        m.RuntimePolicy.revision, m.RuntimePolicy.updated_by).where(
        m.RuntimePolicy.id == str(UUID(hex=match[1])))).first()
    if row is None:
        svc.fail(401, "AGENT_ACCESS_INVALID", "Agent访问凭据无效或已失效")
    config = _validate_config(row)
    if not secrets.compare_digest(config["token_sha256"], _sha(raw)) \
            or config["revoked_at"] is not None or _datetime(config["expires_at"]) <= svc.now():
        svc.fail(401, "AGENT_ACCESS_INVALID", "Agent访问凭据无效或已失效")
    user = db.scalars(select(m.User).where(m.User.id == config["owner_id"], m.User.active.is_(True))
        .execution_options(populate_existing=True)).first()
    if user is None:
        svc.fail(401, "AGENT_ACCESS_INVALID", "Agent访问凭据无效或已失效")
    if required_scope not in config["scopes"]:
        svc.fail(403, "AGENT_SCOPE_REQUIRED", "Agent凭据缺少此操作范围")
    if not _current_space_readable(db, user, config["space_id"]):
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    request.state.agent_access = metadata(row)
    return user


def authorize_agent_space(request, space_id):
    """Apply the token's space boundary after loading the real target object.

    Cookie calls are unchanged. The business handler still checks resource ACL,
    exact bound source versions/hashes, and run ownership, including on replay.
    """
    access = getattr(request.state, "agent_access", None)
    if access is None and not request.headers.getlist("authorization"):
        return
    if not isinstance(access, dict):
        svc.fail(401, "AGENT_ACCESS_INVALID", "Agent访问凭据无效或已失效")
    if access.get("space_id") != space_id:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")


def _human(ctx):
    # Defense in depth for handlers/replays invoked outside the primary wrapper.
    if ctx.request.headers.getlist("authorization") or getattr(ctx.request.state, "agent_access", None) is not None:
        svc.fail(403, "HUMAN_SESSION_REQUIRED", "凭据管理仅限用户登录会话")
    from .auth import authenticate
    user = authenticate(ctx.request, ctx.db)
    if ctx.user is None or user.id != ctx.user.id or not svc.active_user(ctx.db, ctx.user):
        svc.fail(401, "AUTH_REQUIRED", "请先登录")


def _create_input(data):
    try:
        if not isinstance(data, dict) or set(data) != {"request_id", "name", "space_id", "scopes", "expires_at"}:
            raise ValueError()
        request_id, space_id = _uuid(data["request_id"]), _uuid(data["space_id"])
        name, scopes = data["name"], data["scopes"]
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError()
        if not isinstance(scopes, list) or not scopes or any(not isinstance(s, str) for s in scopes) \
                or len(scopes) != len(set(scopes)) or set(scopes) - SCOPES:
            raise ValueError()
        expires = _datetime(data["expires_at"])
        if expires <= svc.now():
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        svc.fail(422, "INVALID_AGENT_ACCESS", "需要有效名称、UUID、非重复授权范围及未来的带时区到期时间")
    return {"request_id": request_id, "name": name.strip(), "space_id": space_id,
        "scopes": sorted(scopes), "expires_at": svc.primitive(expires)}


def create_access(ctx):
    _human(ctx)
    data = _create_input(ctx.data)
    if not _current_space_readable(ctx.db, ctx.user, data["space_id"]):
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    name = _request_name(ctx.user.id, data["request_id"])
    previous = ctx.db.execute(select(m.RuntimePolicy.id, m.RuntimePolicy.config).where(
        m.RuntimePolicy.name == name)).first()
    if previous:
        svc.fail(409, "AGENT_ACCESS_REQUEST_USED", "此创建请求已处理；请查列表，明文凭据不能再次显示",
            access_id=previous.id)
    access_id = svc.uid()
    raw = f"{TOKEN_PREFIX}{UUID(access_id).hex}.{secrets.token_urlsafe(32)}"
    config = {"schema_version": 1, "owner_id": ctx.user.id, **data,
        "request_sha256": svc.digest(data), "token_sha256": _sha(raw),
        "created_at": svc.primitive(svc.now()), "revoked_at": None}
    row = m.RuntimePolicy(id=access_id, name=name, config=config, updated_by=ctx.user.id, revision=1)
    ctx.db.add(row)
    # The DB's UNIQUE(name) is the concurrency guard; the wrapper maps a racing
    # insert's IntegrityError to 409 and rolls back, never replays plaintext.
    ctx.db.flush()
    svc.audit(ctx, "agent_access.created", row, {"space_id": data["space_id"], "scopes": data["scopes"],
        "expires_at": data["expires_at"], "request_id": data["request_id"]})
    return {"access": metadata(row), "token": raw}


def list_access(ctx):
    _human(ctx)
    try:
        space_id = _uuid(ctx.query["space_id"])
    except (KeyError, TypeError, ValueError, AttributeError):
        svc.fail(422, "INVALID_AGENT_ACCESS", "需要有效的知识库UUID")
    # SQL filters ownership before loading credential records. No global listing.
    rows = ctx.db.execute(select(m.RuntimePolicy.id, m.RuntimePolicy.name, m.RuntimePolicy.config,
        m.RuntimePolicy.revision, m.RuntimePolicy.updated_by).where(
        m.RuntimePolicy.updated_by == ctx.user.id,
        m.RuntimePolicy.name.startswith(f"{POLICY_PREFIX}{ctx.user.id}:")))
    items = []
    for row in rows:
        config = _validate_config(row)
        if config["owner_id"] == ctx.user.id and config["space_id"] == space_id:
            items.append(metadata(row))
    can_create = _current_space_readable(ctx.db, ctx.user, space_id)
    if not can_create and not items:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    items.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
    # Derive the mounted API base from the actual management endpoint, not a
    # caller-supplied redirect/forwarded URL. Deployment host validation is the
    # app/reverse proxy's responsibility; this value carries no credential.
    base_url = str(ctx.request.url).split("?", 1)[0].rsplit("/agent-access", 1)[0]
    return {"items": items, "can_create": can_create, "base_url": base_url,
        "notes": ["明文凭据只在创建成功时显示一次；响应不确定时请查列表，不要自动重新创建。",
            "凭据只授权指定知识库的列明接口，每次请求重验当前权限；不能进行人工审核或知识编辑发布。",
            "凭据记录本身不能证明外部Agent已连接；导出或离线测试不代表金融任务验收。"]}


def owned_access(ctx, access_id):
    _human(ctx)
    row = ctx.db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.id == access_id,
        m.RuntimePolicy.updated_by == ctx.user.id,
        m.RuntimePolicy.name.startswith(f"{POLICY_PREFIX}{ctx.user.id}:"))
        .execution_options(populate_existing=True)).first()
    if row is None or _validate_config(row)["owner_id"] != ctx.user.id:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    # An owner may still revoke their credential after losing library access.
    # This grants no content access and no ability to manage others' credentials.
    return row


def revoke_access(ctx):
    row = owned_access(ctx, ctx.id)
    svc.require_etag(ctx, row)
    reason = ctx.data.get("reason") if isinstance(ctx.data, dict) else None
    if not isinstance(ctx.data, dict) or set(ctx.data) != {"reason"} \
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        svc.fail(422, "INVALID_AGENT_ACCESS", "撤销需要非空原因，最多2000字符")
    if row.config["revoked_at"] is None:
        row.config = {**row.config, "revoked_at": svc.primitive(svc.now())}
        ctx.db.flush()
        svc.audit(ctx, "agent_access.revoked", row, {"reason_sha256": _sha(reason)})
    return metadata(row)
