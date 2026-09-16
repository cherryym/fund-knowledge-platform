"""Space retention and resource preservation. Never deletes objects or dispatches jobs.

All callers own the transaction. Mutation/purge hooks acquire the space before the
resource lock; SQLite callers must use the existing IMMEDIATE transaction wrapper.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import or_, select

from . import models as m
from . import services as svc

DEFAULT_RETENTION_DAYS = 2
POLICY_PREFIX = "retention-policy:"
PURGE_ROLES = frozenset({"admin", "owner", "editor", "reviewer", "publisher"})
DEFAULT_PURGE_ROLES = ["admin", "owner"]


def _fresh(db, cls, identity, *, lock=False):
    stmt = select(cls).where(cls.id == identity).execution_options(populate_existing=True)
    return db.scalar(stmt.with_for_update() if lock else stmt)


def _policy(db, name):
    return db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == name)
                     .execution_options(populate_existing=True))


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(UTC)


def _space_context(db, user, space_id, *, lock=False):
    user = _fresh(db, m.User, user.id) if user else None
    space = _fresh(db, m.Space, space_id, lock=lock)
    if not user or not user.active or not space:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    from .libraries import governance
    config = governance(db, space_id)
    rr = set(svc.roles(db, user, space_id))
    kind = "legacy"
    if config:
        kind = config["kind"]
        if kind == "personal":
            if config["owner_id"] != user.id:
                svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
            rr.add("owner")
    if not rr:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    return space, user, rr, kind


def require_policy_manager(db, user, space_id, *, lock=False):
    context = _space_context(db, user, space_id, lock=lock)
    if not context[2] & {"admin", "owner"}:
        svc.fail(403, "RETENTION_MANAGER_REQUIRED", "仅空间管理员或个人库所有者可配置保留与清除权限")
    return context


def _resource_context(db, user, resource_or_id, *, lock=False, manage=False):
    identity = resource_or_id if isinstance(resource_or_id, str) else resource_or_id.id
    resource = _fresh(db, m.Resource, identity)
    if not resource:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    space, user, rr, kind = _space_context(db, user, resource.space_id, lock=lock)
    resource = _fresh(db, m.Resource, identity, lock=lock)
    if not resource or resource.space_id != space.id:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    grants = svc.grants(db, user, resource)
    action = "manage" if manage else "read"
    svc.resource_access(db, user, resource, action, allow_deleted=True)
    if resource.restricted and action not in grants:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    can_manage = bool(rr & {"admin", "owner"} or (rr & {"editor", "publisher"} and "manage" in grants))
    if resource.restricted and "manage" not in grants:
        can_manage = False
    if manage and not can_manage:
        svc.fail(403, "PURGE_PERMISSION_DENIED", "缺少资源管理权限")
    return resource, user, rr, kind, can_manage


def _policy_state(policy, space_id, at):
    if policy is None:
        return "UNCONFIGURED", None
    c = policy.config
    try:
        valid = (c.get("version") == 1 and c.get("space_id") == space_id
                 and type(c.get("retention_days")) is int and 2 <= c["retention_days"] <= 36500
                 and type(c.get("approved")) is bool
                 and isinstance(c.get("purge_allowed_roles"), list)
                 and all(isinstance(role, str) and role in PURGE_ROLES for role in c["purge_allowed_roles"])
                 and len(c["purge_allowed_roles"]) == len(set(c["purge_allowed_roles"])))
        if not valid:
            return "INVALID", None
        if not c["approved"]:
            return "UNAPPROVED", c
        UUID(c["approved_by"])
        if _timestamp(c["approved_at"]) > at:
            return "INVALID", c
        if c.get("approval_expires_at") is not None and _timestamp(c["approval_expires_at"]) <= at:
            return "EXPIRED", c
    except (ValueError, TypeError, KeyError, AttributeError):
        return "INVALID", None
    return "APPROVED", c


def retention_policy(db, user, space_id, *, at=None):
    _, _, rr, kind = _space_context(db, user, space_id)
    policy = _policy(db, POLICY_PREFIX + space_id)
    state, config = _policy_state(policy, space_id, at or svc.now())
    config = config or {}
    return svc.primitive({"space_id": space_id, "space_kind": kind,
        "revision": policy.revision if policy else 0, "status": state,
        "configured": policy is not None, "approved": state == "APPROVED",
        "retention_days": config.get("retention_days", DEFAULT_RETENTION_DAYS),
        "suggested_retention_days": DEFAULT_RETENTION_DAYS,
        "purge_allowed_roles": config.get("purge_allowed_roles", DEFAULT_PURGE_ROLES),
        "approved_by": config.get("approved_by"), "approved_at": config.get("approved_at"),
        "approval_expires_at": config.get("approval_expires_at"),
        "permissions": {"can_configure": bool(rr & {"admin", "owner"}),
                        "can_request_purge": state == "APPROVED" and bool(rr & set(config.get("purge_allowed_roles", []))),
                        "can_manage_preservation": bool(rr & {"admin", "owner"})},
        "automatic_purge": False})


def _expiry(resource, days):
    dates = [svc.aware(resource.retain_until)] if resource.retain_until else []
    if resource.deleted_at and days is not None:
        try:
            dates.append(svc.aware(resource.deleted_at) + timedelta(days=days))
        except OverflowError:
            svc.fail(409, "RETENTION_DATE_INVALID", "删除时间无法计算保留期限")
    return max(dates) if dates else None


def _extend(ctx, resource, days, action):
    until = _expiry(resource, days)
    if until is None or (resource.retain_until and svc.aware(resource.retain_until) >= until):
        return False
    previous = resource.retain_until
    svc.bump(ctx.db, resource, retain_until=until)
    svc.audit(ctx, action, resource, svc.primitive({"previous_retain_until": previous,
        "retain_until": until, "basis": "deleted_at", "retention_days": days}))
    return True


def set_retention_policy(ctx):
    require_policy_manager(ctx.db, ctx.user, ctx.id, lock=True)
    policy = _policy(ctx.db, POLICY_PREFIX + ctx.id)
    svc.require_etag(ctx, policy or SimpleNamespace(revision=0))
    # Also validate direct service calls, not only HTTP schema validation.
    from .api_retention import SCHEMAS
    from jsonschema import Draft202012Validator, FormatChecker
    if not Draft202012Validator(SCHEMAS["RetentionPolicyInput"], format_checker=FormatChecker()).is_valid(ctx.data):
        svc.fail(422, "SCHEMA_VALIDATION", "保留策略配置格式无效")
    at = svc.now()
    expires = ctx.data.get("approval_expires_at")
    if expires and _timestamp(expires) <= at:
        svc.fail(422, "APPROVAL_EXPIRY_INVALID", "审批失效时间必须晚于当前时间")
    _, old = _policy_state(policy, ctx.id, at)
    approved = ctx.data["approved"]
    config = {"version": 1, "space_id": ctx.id, "retention_days": int(ctx.data["retention_days"]),
        "approved": approved, "purge_allowed_roles": ctx.data.get("purge_allowed_roles", DEFAULT_PURGE_ROLES),
        "approved_by": ctx.user.id if approved else None,
        "approved_at": svc.primitive(at) if approved else None, "approval_expires_at": expires,
        "reason": ctx.data["reason"].strip()}
    previous = dict(policy.config) if policy else None
    if policy:
        svc.bump(ctx.db, policy, config=config, updated_by=ctx.user.id, updated_at=at)
    else:
        policy = m.RuntimePolicy(id=svc.uid(), name=POLICY_PREFIX + ctx.id, config=config,
                                 updated_by=ctx.user.id, revision=1)
        ctx.db.add(policy)
        ctx.db.flush()
    count = 0
    if approved and ctx.data.get("backfill_trash", True):
        # If replacing a longer valid policy, grandfather its trash floor as well.
        days = max(config["retention_days"], (old or {}).get("retention_days", 2))
        for resource in ctx.db.scalars(select(m.Resource).where(m.Resource.space_id == ctx.id,
                m.Resource.deleted_at.is_not(None)).order_by(m.Resource.id).with_for_update()
                .execution_options(populate_existing=True)):
            count += _extend(ctx, resource, days, "retention.trash_extended")
    svc.audit(ctx, "retention.policy_approved" if approved else "retention.policy_unapproved", policy,
              {"space_id": ctx.id, "before": previous, "after": config, "backfilled_count": count})
    return {**retention_policy(ctx.db, ctx.user, ctx.id), "backfilled_count": count}


def apply_trash_retention(ctx, resource) -> bool:
    """Call after setting deleted_at, in the same transaction. Missing policy keeps trash safe."""
    _space_context(ctx.db, ctx.user, resource.space_id, lock=True)
    policy = _policy(ctx.db, POLICY_PREFIX + resource.space_id)
    state, config = _policy_state(policy, resource.space_id, svc.now())
    if state != "APPROVED" or not resource.deleted_at:
        return False
    return _extend(ctx, resource, config["retention_days"], "retention.trash_extended")


def create_default_policy(ctx, space) -> dict:
    """Authorized v6 library-create hook; invoke after governance and membership flush.

    A pre-existing policy is never reapproved or replaced. Legacy spaces are never
    initialized here; their manager must use the explicit ETag-protected PUT API.
    """
    _, _, _, kind = require_policy_manager(ctx.db, ctx.user, space.id, lock=True)
    if kind not in {"personal", "team"}:
        svc.fail(409, "GOVERNED_LIBRARY_REQUIRED", "旧空间须通过保留策略接口显式配置")
    if _policy(ctx.db, POLICY_PREFIX + space.id):
        return retention_policy(ctx.db, ctx.user, space.id)
    request = SimpleNamespace(headers={"if-match": '"0"'}, path_params={"id": space.id},
        state=ctx.request.state, app=ctx.request.app)
    synthetic = svc.Context(request, ctx.db, ctx.user,
        {"retention_days": 2, "approved": True, "purge_allowed_roles": list(DEFAULT_PURGE_ROLES),
         "backfill_trash": False, "reason": "用户已明确授权：新建个人库与团队库删除后保留2天，更长期限与法律保全优先"},
        {}, "setRetentionPolicy", ctx.dispatch)
    return set_retention_policy(synthetic)


def _frozen_provenance_refs(db, resource, version_ids):
    """Use Wiki's actual lineage resolver, plus the independent guidance record.

    Receipts/lineage remain evidence even when edited citations or derived trash
    disappear. Malformed frozen lineage is UNKNOWN and blocks, never permits.
    """
    from .wiki import _provenance_sources
    refs, invalid = False, False
    sources = set(version_ids)
    derived_ids = set()
    # The Wiki helper caches only receipt identities. Worker rechecks must reload them.
    db.info.pop("wiki_receipt_provenance_ids_v1", None)
    records = db.scalars(select(m.RuntimePolicy).where(or_(
        m.RuntimePolicy.name.like("wiki-provenance:%"),
        m.RuntimePolicy.name.like("document-guidance-provenance:%"),
        m.RuntimePolicy.name.like("wiki-build-receipt:%")))
        .execution_options(populate_existing=True))
    for record in records:
        c = record.config
        try:
            if record.name.startswith("wiki-build-receipt:"):
                c = c["result"]
                targets = c["created_resource_ids"]
            else:
                target = str(UUID(record.name.split(":", 1)[1]))
                if c.get("resource_id") != target:
                    raise ValueError("lineage owner mismatch")
                targets = [target]
            if not isinstance(targets, list):
                raise ValueError("lineage targets invalid")
            targets = {str(UUID(target)) for target in targets} - {resource.id}
            if not targets:
                continue
            derived_ids.update(targets)
            # source_version_ids is the current writer contract. If an older
            # source_versions field is present, it must also be valid and retained.
            entries = c["source_version_ids"]
            if not isinstance(entries, list) or not entries:
                raise ValueError("lineage sources missing")
            frozen = {str(UUID(entry)) for entry in entries}
            if "source_versions" in c:
                if not isinstance(c["source_versions"], list):
                    raise ValueError("legacy sources invalid")
                frozen.update(str(UUID(entry)) for entry in c["source_versions"])
            refs |= bool(frozen & sources)
        except (KeyError, ValueError, TypeError, AttributeError):
            invalid = True
    for identity in derived_ids:
        derived = _fresh(db, m.Resource, identity)
        if derived is None:
            continue  # The retained record above still protects its sources.
        try:
            refs |= bool(set(_provenance_sources(db, derived) or []) & sources)
        except svc.APIError:
            invalid = True
    return refs, invalid


def _consultation_snapshot_refs(db, resource, version_ids):
    """Frozen run evidence is authoritative even when its RunEvidence index is lost.

    The writer stores list[dict] with resource_id/version_id/block_id/hash. Query
    only that column, with no run-state or space filter: failed, cancelled, deleted
    threads and cross-space historical references still require preservation.
    """
    refs, invalid = False, False
    versions = set(version_ids)
    for snapshot in db.scalars(select(m.ConsultationRun.evidence_snapshot)):
        if snapshot == []:
            continue
        if not isinstance(snapshot, list):
            invalid = True
            continue
        for source in snapshot:
            if not isinstance(source, dict):
                invalid = True
                continue
            found = False
            for key, targets in (("resource_id", {resource.id}), ("version_id", versions)):
                value = source.get(key)
                if value is None:
                    continue
                try:
                    identity = str(UUID(value))
                except (ValueError, TypeError, AttributeError):
                    invalid = True
                    continue
                found = True
                refs |= identity in targets
            if not found:
                invalid = True
    return refs, invalid


def _dependency_reasons(db, resource, job_id=None):
    codes = []
    ids = list(db.scalars(select(m.ResourceVersion.id).where(m.ResourceVersion.resource_id == resource.id)))
    refs = bool(db.scalar(select(m.RelationEdge.id).where(m.RelationEdge.target_resource_id == resource.id).limit(1)))
    snapshot_refs, invalid_snapshot = _consultation_snapshot_refs(db, resource, ids)
    refs |= snapshot_refs
    if invalid_snapshot:
        codes.append(("CONSULTATION_SNAPSHOT_INVALID", "存在无法核验的咨询冻结证据，须完成留存记录核验后再申请清除"))
    if ids:
        refs |= bool(db.scalar(select(m.EvidenceLink.id).where(m.EvidenceLink.to_version_id.in_(ids),
            m.EvidenceLink.from_version_id.not_in(ids)).limit(1)))
        refs |= bool(db.scalar(select(m.RunEvidence.run_id).where(m.RunEvidence.version_id.in_(ids)).limit(1)))
        refs |= bool(db.scalar(select(m.RelationEdge.id).where(m.RelationEdge.evidence_version_id.in_(ids),
            m.RelationEdge.source_version_id.not_in(ids)).limit(1)))
        for block in db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id.not_in(ids),
                m.ContentBlock.block_type.in_(["image", "attachment"]))):
            refs |= block.data.get("version_id") in ids
        if db.scalar(select(m.Upload.id).where(m.Upload.version_id.in_(ids), m.Upload.state == "OPEN").limit(1)):
            codes.append(("UPLOAD_IN_PROGRESS", "仍有未完成的文件上传"))
        frozen_refs, invalid = _frozen_provenance_refs(db, resource, ids)
        refs |= frozen_refs
        if invalid:
            codes.append(("FROZEN_PROVENANCE_INVALID", "存在无法核验的冻结来源记录，须完成来源核验后再申请清除"))
    if refs:
        codes.append(("INBOUND_DEPENDENCIES", "仍有资源引用、咨询留存或知识生成来源依赖"))
    for pending in db.scalars(select(m.Job).where(m.Job.state.in_(["QUEUED", "RUNNING"]))):
        if pending.id == job_id:
            continue
        p = pending.payload or {}
        if (pending.resource_id == resource.id or pending.version_id in ids
                or p.get("resource_id") == resource.id or p.get("version_id") in ids
                or set(p.get("version_ids") or []) & set(ids)
                or set(p.get("source_version_ids") or []) & set(ids)):
            codes.append(("DEPENDENT_JOB_RUNNING", "仍有排队或运行中的关联任务"))
            break
    return codes


def purge_eligibility(db, user, resource_or_id, *, at=None, job_id=None, lock=False, manage=False):
    resource, user, rr, _, can_manage = _resource_context(db, user, resource_or_id, lock=lock, manage=manage)
    at = svc.aware(at or svc.now())
    policy = _policy(db, POLICY_PREFIX + resource.space_id)
    state, config = _policy_state(policy, resource.space_id, at)
    reasons = []
    state_reasons = {"UNCONFIGURED": ("RETENTION_UNDEFINED", "空间尚未配置并批准保留策略"),
        "UNAPPROVED": ("RETENTION_UNAPPROVED", "空间保留策略尚未批准或已撤回批准"),
        "INVALID": ("RETENTION_POLICY_INVALID", "空间保留策略无效，需管理员重新核验"),
        "EXPIRED": ("RETENTION_POLICY_EXPIRED", "空间保留策略审批已失效")}
    if state in state_reasons:
        reasons.append(state_reasons[state])
    permitted = bool(can_manage and state == "APPROVED" and rr & set(config["purge_allowed_roles"]))
    if not can_manage or (state == "APPROVED" and not permitted):
        reasons.append(("PURGE_PERMISSION_DENIED", "当前用户不同时具备资源管理权限和获准的清除角色"))
    if not resource.deleted_at:
        reasons.append(("TRASH_REQUIRED", "资源尚未移入回收站"))
    expiry = _expiry(resource, config["retention_days"] if config else None)
    if resource.legal_hold:
        reasons.append(("LEGAL_HOLD", "资源处于法律保全中，保留期届满也不能清除"))
    if expiry and at < expiry:
        reasons.append(("RETENTION_NOT_EXPIRED", "删除后的保留期尚未届满"))
    reasons.extend(_dependency_reasons(db, resource, job_id))
    return svc.primitive({"resource_id": resource.id, "space_id": resource.space_id,
        "revision": resource.revision, "policy_revision": policy.revision if policy else 0,
        "policy_status": state, "eligible": not reasons, "can_request_purge": permitted,
        "deleted_at": resource.deleted_at, "retain_until": resource.retain_until,
        "expiry": expiry, "legal_hold": resource.legal_hold, "checked_at": at,
        "reasons": [{"code": code, "message": message} for code, message in reasons],
        "automatic_purge": False})


def assert_purge_allowed(db, user, resource_or_id, *, phase="request", job_id=None, at=None) -> dict:
    """Request/worker guard: fresh ACL + policy + preservation + dependencies; no commit/deletion.

    Caller must retain this transaction/locks through enqueue or worker side effects.
    APIError is intentional; workers may translate exc.code to their JobError.
    """
    if phase not in {"request", "execution"}:
        raise ValueError("phase must be request or execution")
    if phase == "execution" and not job_id:
        svc.fail(409, "PURGE_JOB_REQUIRED", "执行清除必须绑定任务并复核申请人")
    if job_id:
        job = _fresh(db, m.Job, job_id)
        identity = resource_or_id if isinstance(resource_or_id, str) else resource_or_id.id
        if (not job or job.kind != "PURGE" or job.owner_id != user.id
                or job.state not in {"QUEUED", "RUNNING"} or job.cancel_requested
                or job.payload.get("resource_id") != identity or not str(job.payload.get("reason", "")).strip()):
            svc.fail(409, "PURGE_JOB_INVALID", "清除任务的申请人、资源、状态或申请原因无法核验")
    result = purge_eligibility(db, user, resource_or_id, at=at, job_id=job_id, lock=True, manage=True)
    if not result["eligible"]:
        priority = {"LEGAL_HOLD": 0, "PURGE_PERMISSION_DENIED": 1}
        first = min(result["reasons"], key=lambda reason: priority.get(reason["code"], 2))
        status = 403 if first["code"] == "PURGE_PERMISSION_DENIED" else (
            423 if first["code"] in {"LEGAL_HOLD", "RETENTION_NOT_EXPIRED"} else 409)
        svc.fail(status, first["code"], first["message"], **result)
    return result


def preservation(db, user, resource_or_id):
    resource, _, rr, _, can_manage = _resource_context(db, user, resource_or_id)
    return svc.primitive({"resource_id": resource.id, "revision": resource.revision,
        "legal_hold": resource.legal_hold, "retain_until": resource.retain_until,
        "can_configure": can_manage and bool(rr & {"admin", "owner"}),
        "release_requires_confirmation": True})


def set_preservation(ctx):
    resource, _, rr, _, _ = _resource_context(ctx.db, ctx.user, ctx.id, lock=True, manage=True)
    if not rr & {"admin", "owner"}:
        svc.fail(403, "RETENTION_MANAGER_REQUIRED", "保全配置仅限空间管理员或个人库所有者")
    svc.require_etag(ctx, resource)
    from .api_retention import SCHEMAS
    from jsonschema import Draft202012Validator, FormatChecker
    if not Draft202012Validator(SCHEMAS["PreservationInput"], format_checker=FormatChecker()).is_valid(ctx.data):
        svc.fail(422, "SCHEMA_VALIDATION", "保全配置格式无效")
    release = resource.legal_hold and ctx.data.get("legal_hold") is False
    if release and (ctx.data.get("confirm_release") is not True or len(ctx.data.get("release_reason", "").strip()) < 10):
        svc.fail(409, "LEGAL_HOLD_RELEASE_CONFIRMATION_REQUIRED", "解除法律保全须明确确认并填写至少10字的解除依据")
    before = {"legal_hold": resource.legal_hold, "retain_until": svc.primitive(resource.retain_until)}
    changes = {}
    if "legal_hold" in ctx.data:
        changes["legal_hold"] = ctx.data["legal_hold"]
    if "retain_until" in ctx.data:
        until = _timestamp(ctx.data["retain_until"])
        state, config = _policy_state(_policy(ctx.db, POLICY_PREFIX + resource.space_id), resource.space_id, svc.now())
        floor = _expiry(resource, config["retention_days"] if state == "APPROVED" else None)
        if floor and until < floor:
            svc.fail(409, "RETENTION_CANNOT_SHORTEN", "不得缩短现有保留期限或已批准的删除后保留期")
        changes["retain_until"] = until
    svc.bump(ctx.db, resource, **changes)
    svc.audit(ctx, "preservation.hold_released" if release else "preservation.updated", resource,
        {"before": before, "after": svc.primitive({"legal_hold": resource.legal_hold, "retain_until": resource.retain_until}),
         "reason": ctx.data["reason"].strip(), "release_reason": ctx.data.get("release_reason") if release else None})
    # Management permission may exist without restricted read permission.
    return svc.primitive({"resource_id": resource.id, "revision": resource.revision,
        "legal_hold": resource.legal_hold, "retain_until": resource.retain_until,
        "can_configure": True, "release_requires_confirmation": True})
