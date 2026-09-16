"""Explicit owner/admin attestation, separate from independent professional review.

Publishing makes an unchanged snapshot available. It does not invent legal
effective dates, remove unverified generation provenance or certify legislation.
"""
from sqlalchemy import select

from . import models as m
from . import services as svc
from .ingestion import block_text, text_sha256

MODE = "ADMIN_CONFIRMED"


def provenance_hash(db, resource_id):
    policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == f"wiki-provenance:{resource_id}"))
    return svc.digest(policy.config) if policy else None


def confirmation(db, version):
    """Validate attestation binding; source ACL/content guards are still separate."""
    policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == f"admin-review:{version.id}"))
    resource = db.get(m.Resource, version.resource_id)
    if not policy or not resource or resource.deleted_at or resource.suspended:
        return None
    c = policy.config
    if (c.get("mode") != MODE or c.get("version_id") != version.id
        or c.get("content_sha256") != version.content_sha256 or not version.content_sha256
        or c.get("resource_access_epoch") != resource.access_epoch
        or c.get("provenance_sha256") != provenance_hash(db, resource.id)):
        return None
    return c


def require_manager(db, user, version):
    resource = svc.resource_access(db, user, version.resource_id, "publish")
    svc.space_access(db, user, resource.space_id, "admin")
    svc.resource_access(db, user, resource, "read")
    svc.check_dependency_access(db, user, version)
    if resource.suspended:
        svc.fail(409, "RESOURCE_SUSPENDED", "停用资料不能确认发布")
    return resource


def source_dependencies(db, user, version):
    ids = set(svc.dependency_ids(db, version))
    policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == f"wiki-provenance:{version.resource_id}"))
    if policy:
        ids.update(s["version_id"] for s in policy.config.get("source_snapshot", []))
        ids.update(s["version_id"] for s in policy.config.get("reference_snapshot", []))
    for dep_id in sorted(ids):
        dep = svc.version_access(db, user, dep_id)
        target = db.get(m.Resource, dep.resource_id)
        if (target.suspended or target.deleted_at or dep.state != "APPROVED" or not svc.released(db, dep.id)
            or dep.content_sha256 != svc.check_frozen_hash(db, dep)
            or (dep.knowledge_type == "source" and not dep.source_verified)):
            svc.fail(409, "ADMIN_REVIEW_SOURCE_NOT_PUBLISHED", "请先确认并发布冻结来源，再确认此知识版本")
    return sorted(ids)


def approve(ctx):
    v = ctx.db.get(m.ResourceVersion, ctx.id)
    if not v:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    resource = require_manager(ctx.db, ctx.user, v)
    svc.require_etag(ctx, v)
    if v.state not in {"DRAFT", "IN_REVIEW", "APPROVED"}:
        svc.fail(409, "ADMIN_REVIEW_STATE_INVALID", "拒绝或撤回版本须先修订，不可直接确认")
    latest = ctx.db.scalar(select(m.ResourceVersion.id).where(m.ResourceVersion.resource_id == resource.id)
        .order_by(m.ResourceVersion.version_no.desc()).limit(1))
    if latest != v.id:
        svc.fail(409, "ADMIN_REVIEW_NOT_LATEST", "本次确认只适用于最新版本，历史版本保留原审核记录")
    if (ctx.data.get("confirm_valid_sources") is not True
        or ctx.data.get("acknowledge_not_independent") is not True):
        svc.fail(422, "ADMIN_CONFIRMATION_REQUIRED", "须明确确认资料有效，并确认不是独立人员复核")
    if ctx.db.scalar(select(m.Job.id).where(m.Job.version_id == v.id, m.Job.state.in_(["QUEUED", "RUNNING"])).limit(1)) \
        or ctx.db.scalar(select(m.Upload.id).where(m.Upload.version_id == v.id, m.Upload.state == "OPEN").limit(1)):
        svc.fail(409, "WORK_IN_PROGRESS", "等待上传或处理结束后再确认")
    frozen = svc.check_frozen_hash(ctx.db, v)
    if ctx.data["reviewed_sha256"] != frozen or (v.content_sha256 and v.content_sha256 != frozen):
        svc.fail(409, "REVIEW_HASH_MISMATCH", "确认必须绑定当前完整版本快照")
    blocks = svc.content_blocks(ctx.db, v.id)
    for b in ctx.db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == v.id)):
        if b.search_text != block_text({"block_type": b.block_type, "data": b.data}) or b.content_sha256 != text_sha256(b.search_text):
            svc.fail(409, "CONTENT_INTEGRITY_FAILED", "正文块校验失败")
    blob = ctx.db.get(m.Blob, v.source_blob_id) if v.source_blob_id else None
    if resource.kind == "document" or blob:
        storage = ctx.request.app.state.storage
        if not blob or blob.space_id != resource.space_id or blob.scan_state != "CLEAN":
            svc.fail(409, "SOURCE_NOT_CLEAN", "原件未通过扫描")
        if not storage.exists(blob.object_key) or text_sha256_bytes(storage.read_bytes(blob.object_key)) != blob.sha256:
            svc.fail(409, "ORIGINAL_INTEGRITY_FAILED", "原件完整性校验失败")
    if not blocks and not (resource.kind == "document" and blob and blob.size_bytes > 0):
        svc.fail(409, "EMPTY_CONTENT", "空白知识不能确认发布")
    dependencies = source_dependencies(ctx.db, ctx.user, v)
    existing = confirmation(ctx.db, v)
    if existing and v.state == "APPROVED":
        return svc.tagged({"version_id": v.id, "state": v.state, "confirmation": existing}, v)
    record = {"mode": MODE, "version_id": v.id, "actor_id": ctx.user.id,
        "confirmed_at": svc.primitive(svc.now()), "reason": ctx.data["reason"],
        "content_sha256": frozen, "resource_access_epoch": resource.access_epoch,
        "provenance_sha256": provenance_hash(ctx.db, resource.id), "dependency_version_ids": dependencies,
        "previous_state": v.state, "independent_review": False, "validity_attested_by_user": True,
        "legal_status_unchanged": v.legal_status, "parsed_content_available": bool(blocks)}
    name = f"admin-review:{v.id}"
    if ctx.db.scalar(select(m.RuntimePolicy.id).where(m.RuntimePolicy.name == name)):
        svc.fail(409, "ADMIN_CONFIRMATION_STALE", "已有确认与当前快照不一致，请新建修订版本")
    ctx.db.add(m.RuntimePolicy(id=svc.uid(), name=name, config=record, updated_by=ctx.user.id))
    svc.bump(ctx.db, v, state="APPROVED", content_sha256=frozen,
        source_verified=True if v.knowledge_type == "source" else v.source_verified)
    ctx.db.flush()
    svc.audit(ctx, "version.admin_confirmed", v, record)
    return svc.tagged({"version_id": v.id, "state": v.state, "confirmation": record}, v)


def text_sha256_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()
