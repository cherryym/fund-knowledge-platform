"""Space membership, resources, permissions and recoverable lifecycle."""
from sqlalchemy import delete, select

from . import models as m
from . import libraries as lib
from . import services as svc
from .services import *


def space_dict(s, user_roles=None):
    return {"id": s.id, "name": s.name, "revision": s.revision, "roles": sorted(user_roles or [])}


def list_spaces(ctx):
    return Result(lib.visible_libraries(ctx.db, ctx.user))


def create_space(ctx):
    if not deployment_admin(ctx.user, ctx.settings):
        fail(403, "DEPLOYMENT_ADMIN_REQUIRED", "创建空间需要部署级管理员")
    s = m.Space(id=uid(), name=ctx.data["name"], revision=1)
    ctx.db.add(s)
    ctx.db.flush()
    ctx.db.add(m.SpaceMember(space_id=s.id, user_id=ctx.user.id, role="admin"))
    audit(ctx, "space.created", s)
    ctx.db.flush()
    return tagged(lib.library_dict(ctx.db, ctx.user, s), s, 201)


def update_space(ctx):
    s = space_access(ctx.db, ctx.user, ctx.id, "admin")
    require_etag(ctx, s)
    bump(ctx.db, s, name=ctx.data["name"])
    audit(ctx, "space.updated", s)
    return tagged(lib.library_dict(ctx.db, ctx.user, s), s)


def delete_space(ctx):
    s = space_access(ctx.db, ctx.user, ctx.id, "admin")
    require_etag(ctx, s)
    for cls in (m.Resource, m.ConsultationThread, m.IssueCase, m.Blob):
        if ctx.db.scalars(select(cls).where(cls.space_id == s.id)).first():
            fail(409, "SPACE_NOT_EMPTY", "空间含保留对象，不能删除")
    ctx.db.execute(delete(m.SpaceMember).where(m.SpaceMember.space_id == s.id))
    audit(ctx, "space.deleted", s)
    ctx.db.delete(s)
    return Result(status=204)


def member_set(db, space_id):
    result = {}
    for member in db.scalars(select(m.SpaceMember).where(m.SpaceMember.space_id == space_id)):
        result.setdefault(member.user_id, []).append(member.role)
    return [{"user_id": key, "roles": sorted(value)} for key, value in sorted(result.items())]


def members(ctx):
    s = space_access(ctx.db, ctx.user, ctx.id, "admin")
    if ctx.operation == "replaceMembers":
        lib.replace_members(ctx, s, ctx.data)
    return tagged([{"user_id": item["user_id"], "roles": item["roles"]}
        for item in lib.member_items(ctx.db, s)], s)


def readable_resource(ctx, r, trash=False):
    return svc.readable_resource(ctx.db, ctx.user, r, trash)


def resources(ctx):
    if ctx.operation == "createResource":
        space_access(ctx.db, ctx.user, ctx.data["space_id"], "editor")
        data = dict(ctx.data)
        if data["kind"] == "document":
            from .documents import validate_document_category_write
            data["category"] = validate_document_category_write(ctx.db, ctx.user, data["space_id"],
                data.get("category", "未分类"))
        r = m.Resource(id=uid(), owner_id=ctx.user.id, **data)
        ctx.db.add(r)
        ctx.db.flush()
        audit(ctx, "resource.created", r)
        return tagged(resource_dict(ctx.db, r, ctx.user), r, 201)
    space_access(ctx.db, ctx.user, ctx.query["space_id"])
    trash = ctx.query.get("trash", False)
    candidates = []
    for r in ctx.db.scalars(select(m.Resource).where(m.Resource.space_id == ctx.query["space_id"])):
        if bool(r.deleted_at) != trash:
            continue
        if any(ctx.query.get(key) and getattr(r, key) != ctx.query[key] for key in ("kind", "category")):
            continue
        if ctx.query.get("tag") and ctx.query["tag"] not in (r.tags or []):
            continue
        if ctx.query.get("q") and ctx.query["q"].casefold() not in r.name.casefold():
            continue
        try:
            readable_resource(ctx, r, trash)
            candidates.append(r)
        except APIError:
            continue
    page, nxt = paginate(candidates, ctx.query)
    return Result({"items": [resource_dict(ctx.db, r, ctx.user) for r in page], "next_cursor": nxt})


def get_resource(ctx):
    r = resource_access(ctx.db, ctx.user, ctx.id)
    readable_resource(ctx, r)
    return tagged(resource_dict(ctx.db, r, ctx.user), r)


def update_resource(ctx):
    r = resource_access(ctx.db, ctx.user, ctx.id, "publish" if "suspended" in ctx.data else "edit")
    if set(ctx.data) - {"suspended"}:
        resource_access(ctx.db, ctx.user, r, "edit")
    require_etag(ctx, r)
    if set(ctx.data) & {"space_id", "owner_id", "kind"}:
        fail(422, "RESOURCE_BOUNDARY_IMMUTABLE", "不能通过资源更新改变所属库、所有者或类型")
    if ctx.data.get("suspended") is False:
        if not r.active_release_id:
            fail(409, "NO_ACTIVE_RELEASE", "没有可重新启用的发布版本")
        rel = ctx.db.get(m.Release, r.active_release_id)
        v = ctx.db.get(m.ResourceVersion, rel.version_id)
        # Evaluate a temporary in-memory view; roll back before writing the authorized transition.
        was = r.suspended
        r.suspended = False
        with ctx.db.no_autoflush:
            valid = evidence_version_eligible(ctx.db, ctx.user, v)
        r.suspended = was
        if not valid:
            fail(409, "SOURCE_NOT_ELIGIBLE", "当前日期的来源、适用性或依赖尚未有效")
    changes = dict(ctx.data)
    if r.kind == "document" and "category" in changes:
        from .documents import validate_document_category_write
        changes["category"] = validate_document_category_write(ctx.db, ctx.user, r.space_id, changes["category"])
    if "suspended" in changes:
        changes["access_epoch"] = r.access_epoch + 1
    bump(ctx.db, r, **changes)
    if "suspended" in ctx.data:
        invalidate_dependents(ctx, r, "SUSPENSION_CHANGED")
    audit(ctx, "resource.updated", r, {"fields": sorted(ctx.data)})
    from .vector_indexing import queue_resource_index
    queue_resource_index(ctx, r.id)
    return tagged(resource_dict(ctx.db, r, ctx.user), r)


def lifecycle_authority(ctx, r):
    rr = roles(ctx.db, ctx.user, r.space_id)
    if r.owner_id == ctx.user.id and not r.active_release_id and "editor" in rr:
        return resource_access(ctx.db, ctx.user, r, "edit", allow_deleted=True)
    return resource_access(ctx.db, ctx.user, r, "manage", allow_deleted=True)


def trash_or_restore(ctx):
    r = ctx.db.get(m.Resource, ctx.id)
    if not r:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    lifecycle_authority(ctx, r)
    require_etag(ctx, r)
    restore = ctx.operation == "restoreResource"
    if restore and not r.deleted_at:
        fail(409, "NOT_IN_TRASH", "资源未进入回收站")
    if not restore and r.deleted_at:
        fail(409, "ALREADY_DELETED", "资源已在回收站")
    bump(ctx.db, r, deleted_at=None if restore else now(), suspended=True,
        access_epoch=r.access_epoch + 1)
    if not restore:
        from .retention import apply_trash_retention
        apply_trash_retention(ctx, r)
    invalidate_dependents(ctx, r, "RESTORED" if restore else "DELETED")
    audit(ctx, "resource.restored" if restore else "resource.trashed", r)
    return tagged(resource_dict(ctx.db, r, ctx.user), r) if restore else Result(status=204)


def purge(ctx):
    r = resource_access(ctx.db, ctx.user, ctx.id, "manage", allow_deleted=True)
    require_etag(ctx, r)
    from .retention import assert_purge_allowed
    assert_purge_allowed(ctx.db, ctx.user, r, phase="request")
    vids = list(ctx.db.scalars(select(m.ResourceVersion.id).where(m.ResourceVersion.resource_id == r.id)))
    refs = bool(ctx.db.scalars(select(m.RelationEdge).where(m.RelationEdge.target_resource_id == r.id)).first())
    if vids:
        refs |= bool(ctx.db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.to_version_id.in_(vids),
            m.EvidenceLink.from_version_id.not_in(vids))).first())
        refs |= bool(ctx.db.scalars(select(m.RunEvidence).where(m.RunEvidence.version_id.in_(vids))).first())
    if refs:
        fail(409, "INBOUND_DEPENDENCIES", "仍有入向引用或咨询留存依赖")
    j = create_job(ctx, "PURGE", {"resource_id": r.id, "reason": ctx.data["reason"]}, resource_id=r.id)
    return Result(job_dict(j), 202)


def permissions(ctx):
    r = resource_access(ctx.db, ctx.user, ctx.id, "manage")
    space_access(ctx.db, ctx.user, r.space_id, "admin")
    if ctx.operation == "setPermissions":
        require_etag(ctx, r)
        keys = {(x["user_id"], x["permission"]) for x in ctx.data["grants"]}
        if len(keys) != len(ctx.data["grants"]):
            fail(422, "DUPLICATE_GRANT", "授权条目不能重复")
        for user_id, permission in keys:
            user = ctx.db.get(m.User, user_id)
            if not roles(ctx.db, user, r.space_id):
                fail(422, "INVALID_GRANTEE", "资源授权对象必须是有效空间成员")
        ctx.db.execute(delete(m.ResourceGrant).where(m.ResourceGrant.resource_id == r.id))
        for user_id, permission in keys:
            ctx.db.add(m.ResourceGrant(resource_id=r.id, user_id=user_id, permission=permission))
        bump(ctx.db, r, restricted=ctx.data["restricted"], classification=ctx.data["classification"],
            access_epoch=r.access_epoch + 1)
        invalidate_dependents(ctx, r, "PERMISSIONS_CHANGED")
        audit(ctx, "permissions.replaced", r)
        return tagged(resource_dict(ctx.db, r, ctx.user), r)
    values = [{"user_id": x.user_id, "permission": x.permission} for x in ctx.db.scalars(
        select(m.ResourceGrant).where(m.ResourceGrant.resource_id == r.id))]
    return tagged({"restricted": r.restricted, "classification": r.classification, "grants": values}, r)


def replay_authority(ctx, cached):
    """Main-thread hook: invoke at the start of api.replay_authority(ctx,cached).

    Library implicit-reader access must not replay previously held management
    privileges. Collection creates also need a current object/space check.
    """
    body = cached.get("body")
    if ctx.operation == "createThread":
        thread = ctx.db.get(m.ConsultationThread, body.get("id")) if isinstance(body, dict) else None
        if not thread or thread.owner_id != ctx.user.id or thread.deleted_at:
            fail(404, "NOT_FOUND", "对象不存在或不可访问")
        space_access(ctx.db, ctx.user, thread.space_id)
        return True
    if ctx.operation in {"createSpace", "updateSpace", "replaceMembers", "deleteEmptySpace"}:
        identity = body.get("id") if ctx.operation == "createSpace" and isinstance(body, dict) else ctx.id
        space = space_access(ctx.db, ctx.user, identity, "admin")
        if cached.get("headers", {}).get("ETag") != f'"{space.revision}"':
            fail(409, "ACL_REPLAY_STATE_CHANGED", "库或成员状态已变化，请重新读取")
        return True
    if ctx.operation not in {"createResource", "updateResourceMetadata", "trashResource", "restoreResource",
        "requestPurge", "setPermissions"}:
        return False
    identity = body.get("id") if ctx.operation == "createResource" and isinstance(body, dict) else ctx.id
    r = ctx.db.get(m.Resource, identity)
    if not r:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if ctx.operation in {"trashResource", "restoreResource"}:
        lifecycle_authority(ctx, r)
    elif ctx.operation in {"requestPurge", "setPermissions"}:
        resource_access(ctx.db, ctx.user, r, "manage", allow_deleted=ctx.operation == "requestPurge")
        if ctx.operation == "setPermissions":
            space_access(ctx.db, ctx.user, r.space_id, "admin")
        else:
            from .retention import assert_purge_allowed
            assert_purge_allowed(ctx.db, ctx.user, r, phase="request", job_id=body.get("id"))
    else:
        if set(ctx.data) - {"suspended"}:
            resource_access(ctx.db, ctx.user, r, "edit")
        if "suspended" in ctx.data:
            resource_access(ctx.db, ctx.user, r, "publish")
    if isinstance(body, dict) and "active_release_id" in body:
        if resource_dict(ctx.db, r, ctx.user) != body:
            fail(409, "ACL_REPLAY_STATE_CHANGED", "资源或当前可见版本已变化，请重新读取")
    return True


HANDLERS = {"listSpaces": list_spaces, "createSpace": create_space, "updateSpace": update_space,
    "deleteEmptySpace": delete_space, "listMembers": members, "replaceMembers": members,
    "listResources": resources, "createResource": resources, "getResource": get_resource,
    "updateResourceMetadata": update_resource, "trashResource": trash_or_restore, "restoreResource": trash_or_restore,
    "requestPurge": purge, "getPermissions": permissions, "setPermissions": permissions}
