"""v6 library governance on RuntimePolicy; no migration or retention side effects.

The Space revision is the public ETag for names, governance and membership.
Only absence of a governance row means legacy. Invalid rows fail closed.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select

from . import models as m
from . import services as svc

ALL_ROLES = frozenset({"reader", "editor", "reviewer", "publisher", "admin"})
POLICY_PREFIX = "space-governance:"


def governance(db, space_id):
    # Read columns to avoid an ORM identity-map snapshot restoring revoked ACLs.
    row = db.execute(select(m.RuntimePolicy.config).where(
        m.RuntimePolicy.name == POLICY_PREFIX + space_id)).first()
    if row is None:
        return None
    config = row[0]
    if not isinstance(config, dict) or type(config.get("schema_version")) is not int or config["schema_version"] != 1 \
        or config.get("space_id") != space_id or config.get("kind") not in {"personal", "team"}:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    owner = config.get("owner_id")
    try:
        if not isinstance(owner, str) or str(UUID(owner)) != owner:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if db.scalar(select(m.User.id).where(m.User.id == owner)) is None:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    return {"schema_version": 1, "space_id": space_id, "kind": config["kind"], "owner_id": owner}


def team_library(db, space_id):
    config = governance(db, space_id)
    return bool(config and config["kind"] == "team")


def library_dict(db, user, space):
    effective = svc.roles(db, user, space.id)
    if not effective:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    config = governance(db, space.id)
    return {"id": space.id, "name": space.name, "revision": space.revision,
        "roles": sorted(effective), "kind": config["kind"] if config else "legacy",
        "owner_id": config["owner_id"] if config else None, "governed": config is not None}


def visible_libraries(db, user):
    return [library_dict(db, user, space) for space in db.scalars(
        select(m.Space).order_by(m.Space.created_at, m.Space.id)) if svc.roles(db, user, space.id)]


def _policy(ctx, space, kind):
    policy = m.RuntimePolicy(id=svc.uid(), name=POLICY_PREFIX + space.id, revision=1,
        config={"schema_version": 1, "space_id": space.id, "kind": kind, "owner_id": ctx.user.id},
        updated_by=ctx.user.id)
    ctx.db.add(policy)
    ctx.db.flush()
    return policy


def permissions_changed(ctx, space):
    ctx.db.add(m.Outbox(id=svc.uid(), event_type="SPACE_PERMISSIONS_CHANGED", aggregate_id=space.id,
        payload={"space_id": space.id, "revision": space.revision}))


def create_library(ctx):
    if not svc.active_user(ctx.db, ctx.user):
        svc.fail(401, "AUTH_REQUIRED", "请先登录")
    name = ctx.data["name"].strip()
    if not name or len(name) > 200 or ctx.data["kind"] not in {"personal", "team"}:
        svc.fail(422, "INVALID_LIBRARY", "库名称或类型无效")
    space = m.Space(id=svc.uid(), name=name, revision=1)
    ctx.db.add(space)
    ctx.db.flush()
    _policy(ctx, space, ctx.data["kind"])
    # Team capabilities remain explicit; its owner_id records the creator.
    # Personal capabilities are derived exclusively from owner_id by roles().
    for role in ALL_ROLES:
        ctx.db.add(m.SpaceMember(space_id=space.id, user_id=ctx.user.id, role=role))
    ctx.db.flush()
    from .retention import create_default_policy
    create_default_policy(ctx, space)
    permissions_changed(ctx, space)
    svc.audit(ctx, "library.created", space, {"kind": ctx.data["kind"]})
    return svc.tagged(library_dict(ctx.db, ctx.user, space), space, 201)


def update_library(ctx):
    space = svc.space_access(ctx.db, ctx.user, ctx.id, "admin")
    svc.require_etag(ctx, space)
    if set(ctx.data) != {"name"} or not ctx.data["name"].strip():
        svc.fail(422, "INVALID_LIBRARY", "只允许修改库名称")
    svc.bump(ctx.db, space, name=ctx.data["name"].strip())
    svc.audit(ctx, "library.updated", space)
    return svc.tagged(library_dict(ctx.db, ctx.user, space), space)


def govern_library(ctx):
    space = svc.space_access(ctx.db, ctx.user, ctx.id, "admin")
    svc.require_etag(ctx, space)
    if ctx.data != {"kind": "team"}:
        svc.fail(422, "INVALID_GOVERNANCE", "仅允许显式将旧库登记为团队库")
    if governance(ctx.db, space.id) is not None:
        svc.fail(409, "ALREADY_GOVERNED", "已治理库不能变更类型或所有者")
    explicit = ctx.db.scalar(select(m.SpaceMember.user_id).where(m.SpaceMember.space_id == space.id,
        m.SpaceMember.user_id == ctx.user.id, m.SpaceMember.role == "admin"))
    if explicit is None:
        svc.fail(403, "FORBIDDEN", "需要该库的显式管理员权限")
    svc.bump(ctx.db, space)
    _policy(ctx, space, "team")
    permissions_changed(ctx, space)
    svc.audit(ctx, "library.governed", space, {"previous_kind": "legacy", "kind": "team"})
    return svc.tagged(library_dict(ctx.db, ctx.user, space), space)


def member_items(db, space):
    config = governance(db, space.id)
    if config and config["kind"] == "personal":
        owner = db.get(m.User, config["owner_id"])
        return [{"user_id": owner.id, "display_name": owner.display_name, "roles": sorted(ALL_ROLES)}]
    result = {}
    for member, user in db.execute(select(m.SpaceMember, m.User).join(
        m.User, m.User.id == m.SpaceMember.user_id).where(m.SpaceMember.space_id == space.id)
        .order_by(m.User.display_name, m.User.id, m.SpaceMember.role)):
        item = result.setdefault(user.id, {"user_id": user.id, "display_name": user.display_name, "roles": []})
        item["roles"].append(member.role)
    return list(result.values())


def replace_members(ctx, space, items):
    """Shared by the old array route and v6 envelope route; never bypass personal ACL."""
    svc.space_access(ctx.db, ctx.user, space.id, "admin")
    svc.require_etag(ctx, space)
    config = governance(ctx.db, space.id)
    if config and config["kind"] == "personal":
        svc.fail(409, "PERSONAL_MEMBERS_IMMUTABLE", "个人库仅所有者可访问，不能添加或变更成员")
    if len({item["user_id"] for item in items}) != len(items):
        svc.fail(422, "DUPLICATE_MEMBER", "成员不能重复")
    if not any("admin" in item["roles"] for item in items):
        svc.fail(409, "LAST_ADMIN", "必须保留至少一个有效管理员")
    for item in items:
        if not item["roles"] or len(set(item["roles"])) != len(item["roles"]) \
            or set(item["roles"]) - ALL_ROLES:
            svc.fail(422, "INVALID_ROLE", "成员角色无效")
        if not svc.active_user(ctx.db, ctx.db.get(m.User, item["user_id"])):
            svc.fail(422, "INVALID_MEMBER", "成员身份无效")
    svc.bump(ctx.db, space)
    ctx.db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == space.id))
    for item in items:
        for role in item["roles"]:
            ctx.db.add(m.SpaceMember(space_id=space.id, user_id=item["user_id"], role=role))
    ctx.db.flush()
    permissions_changed(ctx, space)
    svc.audit(ctx, "members.replaced", space)


def directory(ctx):
    space = svc.space_access(ctx.db, ctx.user, ctx.query["space_id"], "admin")
    config = governance(ctx.db, space.id)
    stmt = select(m.User.id, m.User.display_name).where(m.User.active.is_(True))
    if config and config["kind"] == "personal":
        stmt = stmt.where(m.User.id == config["owner_id"])
    return svc.Result({"items": [{"id": user_id, "display_name": name}
        for user_id, name in ctx.db.execute(stmt.order_by(m.User.display_name, m.User.id))]})
