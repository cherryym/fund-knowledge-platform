"""Shared transaction, authorization and evidence kernel. No model/network calls."""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import event, select, update

from . import models as m


def uid():
    return str(uuid.uuid4())


def now():
    return datetime.now(UTC)


def aware(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def primitive(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {k: primitive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [primitive(v) for v in value]
    return value


def digest(value):
    raw = json.dumps(primitive(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class APIError(Exception):
    def __init__(self, status, code, message, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details or {}
        super().__init__(message)


def fail(status, code, message, **details):
    raise APIError(status, code, message, details)


@dataclass
class Result:
    body: Any = None
    status: int = 200
    headers: dict = field(default_factory=dict)


@dataclass
class Context:
    request: Any
    db: Any
    user: Any
    data: Any
    query: dict
    operation: str
    dispatch: list = field(default_factory=list)

    @property
    def settings(self):
        return self.request.app.state.settings

    @property
    def id(self):
        return self.request.path_params.get("id")


def audit(ctx, action, obj=None, details=None, outcome="SUCCESS"):
    ctx.db.add(m.AuditEvent(id=uid(), actor_id=ctx.user.id if ctx.user else None,
        action=action, object_type=type(obj).__name__ if obj else "system",
        object_id=getattr(obj, "id", None), outcome=outcome,
        trace_id=ctx.request.state.trace_id, details=details or {}))


def active_user(db, user):
    # Scalar SELECT rechecks a deactivated identity even in a reused worker session.
    return bool(user and user.active and db.scalar(select(m.User.active).where(m.User.id == user.id)))


def roles(db, user, space_id):
    from .projection_read import memo
    key = (getattr(user, "id", None), bool(user and user.active), space_id)
    return set(memo(db, "roles", key, lambda: _roles_uncached(db, user, space_id)))


def _roles_uncached(db, user, space_id):
    if not active_user(db, user) or db.scalar(select(m.Space.id).where(m.Space.id == space_id)) is None:
        return set()
    from .libraries import ALL_ROLES, governance
    try:
        policy = governance(db, space_id)
    except APIError:
        return set()
    if policy and policy["kind"] == "personal":
        # No deployment admin, explicit member or resource grant can cross this boundary.
        return set(ALL_ROLES) if policy["owner_id"] == user.id else set()
    explicit = set(db.scalars(select(m.SpaceMember.role).where(
        m.SpaceMember.space_id == space_id, m.SpaceMember.user_id == user.id)).all())
    return explicit | {"reader"} if policy and policy["kind"] == "team" else explicit


def space_access(db, user, space_id, capability=None):
    space = db.get(m.Space, space_id)
    r = roles(db, user, space_id)
    if not space or not r:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if capability and capability not in r:
        fail(403, "FORBIDDEN", "缺少此操作所需角色")
    return space


def deployment_admin(user, settings):
    # Identity comes from controlled settings/OIDC mapping, never a space role.
    if not user or not user.active:
        return False
    configured = getattr(settings, "deployment_admin_subjects", []) or []
    if isinstance(configured, str):
        configured = [s.strip() for s in configured.split(",") if s.strip()]
    is_admin = getattr(user, "is_deployment_admin", False)
    return bool(is_admin or user.external_subject in configured or (
        settings.app_env == "development" and settings.auth_mode == "demo"
        and user.external_subject == "demo:admin"))


def grants(db, user, resource):
    from .projection_read import memo
    return set(memo(db, "grants", (user.id, resource.id), lambda: set(db.scalars(
        select(m.ResourceGrant.permission).where(m.ResourceGrant.resource_id == resource.id,
            m.ResourceGrant.user_id == user.id)).all())))


def resource_access(db, user, resource_or_id, action="read", allow_deleted=False):
    resource = _resource_acl(db, user, resource_or_id, action, allow_deleted)
    if action in {"read", "download", "edit", "review", "publish"} and resource.kind == "knowledge":
        from .wiki import guard_wiki_resource
        guard_wiki_resource(db, user, resource)
    return resource


def _resource_acl(db, user, resource_or_id, action="read", allow_deleted=False):
    """Shared identity/resource ACL only, not a grant to read version bodies.

    Normal callers use resource_access. The metadata catalog supplies its own
    recursive metadata provenance guard, then full version_access at body read.
    """
    resource = db.get(m.Resource, resource_or_id) if isinstance(resource_or_id, str) else resource_or_id
    if not resource:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    r = roles(db, user, resource.space_id)
    g = grants(db, user, resource) if r else set()
    if not r or (resource.restricted and action not in g):
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if resource.deleted_at and not allow_deleted:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    permits = {
        "read": bool(r), "download": bool(r) and (not resource.restricted or "download" in g),
        "edit": "editor" in r, "review": "reviewer" in r, "publish": "publisher" in r,
        "manage": "admin" in r or (bool(r & {"editor", "publisher"}) and "manage" in g),
    }
    if not permits.get(action, False):
        fail(403, "FORBIDDEN", "缺少此操作所需权限")
    if action in {"read", "download"} and not resource.deleted_at:
        # Metadata is also content: never expose an unpublished private draft
        # through a direct resource, graph edge, or idempotency response.
        if not resource.active_release_id and not unpublished_resource_access(db, user, resource, r):
            fail(404, "NOT_FOUND", "对象不存在或不可访问")
    return resource


def unpublished_resource_access(db, user, resource, rr=None):
    rr = roles(db, user, resource.space_id) if rr is None else rr
    if rr & {"reviewer", "publisher"}:
        return True
    if "editor" not in rr:
        return False
    from .libraries import team_library
    return resource.owner_id == user.id or team_library(db, resource.space_id) or bool(db.scalar(
        select(m.ResourceVersion.id).where(m.ResourceVersion.resource_id == resource.id,
            m.ResourceVersion.author_id == user.id).limit(1)))


def can_edit_draft(db, user, version):
    """api_content.draft/create_upload hook; preserves restricted-resource grants.

    Author ownership remains unchanged for legacy. Team editors share DRAFT only;
    the caller must still check If-Match and all original immutability constraints.
    """
    if not version or version.state != "DRAFT":
        return False
    try:
        resource = resource_access(db, user, version.resource_id, "edit")
        from .libraries import team_library
        return version.author_id == user.id or team_library(db, resource.space_id)
    except APIError:
        return False


def require_draft(ctx):
    """Drop-in body for api_content.draft(ctx), integrated by the main thread."""
    version = version_access(ctx.db, ctx.user, ctx.id, "edit", dependencies=False)
    require_etag(ctx, version)
    if version.state != "DRAFT":
        fail(409, "VERSION_FROZEN", "已提交版本不可修改；请复制为新草稿")
    if not can_edit_draft(ctx.db, ctx.user, version):
        fail(403, "DRAFT_OWNER_REQUIRED", "只能修改本人草稿或有编辑权限的团队草稿")
    return version


def version_contributor(db, user, version):
    """An editor cannot approve their own contribution under another author's ID."""
    if version.author_id == user.id:
        return True
    contributed = db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.object_type == "ResourceVersion",
        m.AuditEvent.object_id == version.id, m.AuditEvent.actor_id == user.id,
        m.AuditEvent.outcome == "SUCCESS", m.AuditEvent.action.in_(
            ["version.edited", "version.submitted", "relations.replaced"])).limit(1))
    uploaded = db.scalar(select(m.Upload.id).where(m.Upload.version_id == version.id,
        m.Upload.user_id == user.id, m.Upload.state == "SEALED").limit(1))
    return bool(contributed or uploaded)


def independent_review(db, version):
    """Find a hash-bound approval by somebody outside the version's contributors."""
    for review in db.scalars(select(m.ReviewDecision).where(m.ReviewDecision.version_id == version.id,
        m.ReviewDecision.decision == "APPROVE", m.ReviewDecision.reviewed_sha256 == version.content_sha256)):
        reviewer = db.get(m.User, review.reviewer_id)
        if reviewer and not version_contributor(db, reviewer, version):
            return review
    return None


def released(db, version_id, cutoff=None):
    stmt = select(m.Release).where(m.Release.version_id == version_id,
        m.Release.state.in_(["ACTIVE", "SUPERSEDED"]), m.Release.activated_at.is_not(None))
    if cutoff:
        stmt = stmt.where(m.Release.activated_at <= cutoff)
    return db.scalars(stmt).first()


def is_released(db, version, cutoff=None):
    """Release presence only; preserve ownership, state and recorded-time gates."""
    from .projection_read import memo
    return memo(db, "release-presence", (version.resource_id, version.id, cutoff),
                lambda: _is_released_uncached(db, version, cutoff))


def _is_released_uncached(db, version, cutoff=None):
    stmt = select(m.Release.id).where(m.Release.resource_id == version.resource_id,
        m.Release.version_id == version.id, m.Release.state.in_(["ACTIVE", "SUPERSEDED"]),
        m.Release.activated_at.is_not(None))
    if cutoff:
        stmt = stmt.where(m.Release.activated_at <= cutoff)
    return db.scalar(stmt.limit(1)) is not None


def version_access(db, user, version_or_id, action="read", dependencies=True):
    v = db.get(m.ResourceVersion, version_or_id) if isinstance(version_or_id, str) else version_or_id
    if not v:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    r = resource_access(db, user, v.resource_id, action)
    _version_acl(db, user, v, r, action)
    if dependencies:
        check_dependency_access(db, user, v)
    return v


def _version_acl(db, user, v, r, action="read"):
    """Version visibility only; caller must separately enforce provenance."""
    rr = roles(db, user, r.space_id)
    from .libraries import team_library
    if not is_released(db, v) and action in {"read", "download"} and not (
        rr & {"reviewer", "publisher"} or ("editor" in rr and (
            v.author_id == user.id or (v.state == "DRAFT" and team_library(db, r.space_id))))):
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if action == "edit" and v.state == "DRAFT" and not can_edit_draft(db, user, v):
        fail(403, "DRAFT_OWNER_REQUIRED", "只能修改本人草稿或有编辑权限的团队草稿")
    if action == "review" and v.state == "IN_REVIEW" and version_contributor(db, user, v):
        fail(403, "SELF_REVIEW_FORBIDDEN", "作者或共同编辑者不能审核自己的稿件")
    if action == "publish" and v.state == "APPROVED" and not independent_review(db, v):
        from .admin_review import confirmation
        if not confirmation(db, v):
            fail(409, "INDEPENDENT_REVIEW_REQUIRED", "缺少独立审核或明确的管理员确认")
    return v


def readable_resource(db, user, resource, trash=False):
    resource = resource_access(db, user, resource, "manage" if trash else "read", allow_deleted=trash)
    if not trash and resource.active_release_id:
        release = db.get(m.Release, resource.active_release_id)
        if not release or release.resource_id != resource.id:
            fail(404, "NOT_FOUND", "对象不存在或不可访问")
        version_access(db, user, release.version_id)
    return resource


def _dependency_structure(db, v):
    """Cache adjacency only within a Session, never permission decisions.

    Every access below still checks current resources, versions and ancestry.
    ORM flush, rollback and bulk DML invalidate this structural query cache.
    """
    name = "fkb_dependency_structure"
    if name not in db.info:
        db.info[name] = {}
        def invalidate(*_):
            db.info.get(name, {}).clear()
            db.info.pop("wiki_checked_content_hash", None)
        def on_statement(state):
            if state.is_insert or state.is_update or state.is_delete:
                invalidate()
        event.listen(db, "after_flush", invalidate)
        event.listen(db, "after_rollback", invalidate)
        event.listen(db, "do_orm_execute", on_statement)
    cache = db.info[name]
    if db.new or db.dirty or db.deleted:
        cache.clear()
    key = (v.id, v.revision)
    if key not in cache:
        ids = {link.to_version_id for link in evidence_links(db, v.id)}
        targets = set()
        for relation in relation_rows(db, v.id):
            evidence_id, target_id = relation.evidence_version_id, relation.target_resource_id
            if evidence_id:
                ids.add(evidence_id)
            targets.add(target_id)
        from .projection_read import active
        attachment_data = ((block.data for block in block_rows(db, v.id) if block.block_type in {"image", "attachment"})
            if active(db) else db.scalars(select(m.ContentBlock.data).where(m.ContentBlock.version_id == v.id,
                m.ContentBlock.block_type.in_(["image", "attachment"]))))
        for data in attachment_data:
            if data.get("version_id"):
                ids.add(data["version_id"])
        cache[key] = frozenset(ids), frozenset(targets)
    return cache[key]


def dependency_ids(db, v):
    ids = set(_dependency_structure(db, v)[0])
    resource = db.get(m.Resource, v.resource_id)
    if resource and resource.kind == "knowledge":
        from .wiki import _provenance_sources
        # Frozen generation lineage also participates in date/version eligibility
        # and publish checks, even after editable citation markers are removed.
        ids.update(_provenance_sources(db, resource) or [])
    return ids


def check_dependency_access(db, user, version, path=()):
    if version.id in path or len(path) >= 8:
        fail(409, "DEPENDENCY_CYCLE", "来源依赖成环或超过八层")
    for target_id in _dependency_structure(db, version)[1]:
        resource_access(db, user, target_id)
    for dependency_id in dependency_ids(db, version):
        dependency = version_access(db, user, dependency_id, dependencies=False)
        check_dependency_access(db, user, dependency, (*path, version.id))


def match_applicability(applicability, context):
    """Three-valued matching: True, False, None (decisive fact unknown)."""
    unknown = False
    for group in ("all", "none"):
        for term in applicability.get(group, []):
            value = context.get(term["field"])
            if value is None or value == "":
                unknown = True
                continue
            matches = str(value) in term["values"]
            if (group == "all" and not matches) or (group == "none" and matches):
                return False
    return None if unknown else True


def effective_date(context=None, business_date=None):
    value = business_date or (context or {}).get("business_date")
    if isinstance(value, date):
        return value
    return date.fromisoformat(value) if value else now().astimezone(timezone(timedelta(hours=8))).date()


def select_applicable_version(db, user, resource, context=None, business_date=None, for_answer=True):
    context = context or {}
    day = effective_date(context, business_date)
    cutoff = context.get("knowledge_cutoff")
    if isinstance(cutoff, str):
        cutoff = datetime.fromisoformat(cutoff)
    resource_access(db, user, resource)
    if resource.suspended or resource.deleted_at:
        return None
    candidates = db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.resource_id == resource.id)
        .order_by(m.ResourceVersion.version_no.desc())).all()
    for v in candidates:
        if v.state != "APPROVED" or not is_released(db, v, cutoff):
            continue
        if v.valid_from and day < v.valid_from:
            continue
        applicability = match_applicability(v.applicability or {}, context)
        if applicability is False:
            continue
        if v.valid_to and day >= v.valid_to:
            # Full replacement versions cannot resurrect an older unbounded rule after expiry.
            return None
        # Never fall back to an older rule when a newer applicable rule is unverified/unknown.
        if (for_answer and applicability is None) or (v.legal_status in {"UNKNOWN", "PARTIAL"}
            or (v.legal_status == "FUTURE" and v.valid_from is None)
            or (v.legal_status == "REPEALED" and v.valid_to is None)):
            return None
        if v.knowledge_type == "source" and not v.source_verified:
            return None
        from .source_authority import formal_excluded
        if formal_excluded(db, v, {**context, "business_date": day}):
            return None
        if resource.kind == "document" and not v.source_blob_id:
            return None
        if v.source_blob_id:
            blob = db.get(m.Blob, v.source_blob_id)
            if not blob or blob.scan_state != "CLEAN":
                return None
        if not v.content_sha256 or v.content_sha256 != check_frozen_hash(db, v):
            return None
        return v
    return None


def evidence_version_eligible(db, user, version, context=None, business_date=None, path=(), for_answer=True):
    if version.id in path or len(path) >= 8:
        return False
    try:
        resource = resource_access(db, user, version.resource_id)
        from .wiki import unverified_wiki
        if unverified_wiki(db, resource):
            return False
        selected = select_applicable_version(db, user, resource, context, business_date, for_answer)
        if not selected or selected.id != version.id:
            return False
        check_dependency_access(db, user, version)
        for dependency_id in dependency_ids(db, version):
            dep = db.get(m.ResourceVersion, dependency_id)
            if not dep or not evidence_version_eligible(db, user, dep, context, business_date,
                (*path, version.id), for_answer):
                return False
        for edge in db.scalars(select(m.RelationEdge).where(m.RelationEdge.source_version_id == version.id,
            m.RelationEdge.relation_type == "DEPENDS_ON")):
            target = db.get(m.Resource, edge.target_resource_id)
            dep = select_applicable_version(db, user, target, context, business_date, for_answer)
            if not dep or not evidence_version_eligible(db, user, dep, context, business_date,
                (*path, version.id), for_answer):
                return False
        return True
    except APIError:
        return False


def eligible_evidence(db, user, space_id, context=None, business_date=None, for_answer=True, *, version_ids=None):
    """Public worker helper: current ACL + published/date-valid sources + transitive dependencies.

    Returns rank_evidence/VectorIndex compatible records, never unvalidated model content.
    user can be an ORM User or its string UUID. Call again immediately before delivering a run.
    """
    if isinstance(user, str):
        user = db.get(m.User, user)
    space_access(db, user, space_id)
    records = []
    query = select(m.Resource).where(m.Resource.space_id == space_id, m.Resource.deleted_at.is_(None))
    if version_ids is not None:
        if not version_ids:
            return []
        query = query.where(m.Resource.id.in_(select(m.ResourceVersion.resource_id)
            .where(m.ResourceVersion.id.in_(version_ids))))
    for resource in db.scalars(query):
        try:
            v = select_applicable_version(db, user, resource, context, business_date, for_answer)
        except APIError:
            continue
        if not v or (version_ids is not None and v.id not in version_ids) or not evidence_version_eligible(db, user, v, context, business_date, for_answer=for_answer):
            continue
        blocks = list(db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == v.id)
            .order_by(m.ContentBlock.ordinal)))
        from .ingestion import block_text
        if any(b.content_sha256 != hashlib.sha256(b.search_text.encode("utf-8")).hexdigest()
            or b.search_text != block_text({"block_type": b.block_type, "data": b.data}) for b in blocks):
            continue
        step_ordinals = [b.ordinal for b in blocks if b.block_type == "step"]
        for b in blocks:
            records.append({"resource_id": resource.id, "version_id": v.id, "block_id": b.block_id,
                "title": v.title, "text": b.search_text, "locator": b.locator or {},
                "ordinal": b.ordinal, "version_step_ordinals": step_ordinals,
                "content_sha256": b.content_sha256, "block_type": b.block_type, "data": b.data,
                "resource_access_epoch": resource.access_epoch, "kind": resource.kind,
                "knowledge_type": v.knowledge_type, "required_facts": v.required_facts,
                "applicability": v.applicability, "valid_from": primitive(v.valid_from),
                "valid_to": primitive(v.valid_to), "source_verified": v.source_verified,
                "legal_status": v.legal_status,
                "needs_context": match_applicability(v.applicability or {}, context or {}) is None,
                "missing_context_fields": sorted({term["field"] for group in (v.applicability or {}).values()
                    for term in group if not (context or {}).get(term["field"])} | {
                        name for name in (v.required_facts or []) if not (context or {}).get(name)})})
    return records


def clarification_candidates(session, user, space_id, context=None):
    """Authorized, verified published candidates with unknown context; never a firm-answer grant.

    UNKNOWN/PARTIAL legal sources remain excluded. Worker should combine these with
    eligible_evidence only for deterministic clarification, before generation.
    """
    return [e for e in eligible_evidence(session, user, space_id, context, for_answer=False)
        if e["needs_context"] or e["missing_context_fields"]]


def block_dict(db, b):
    citations = [{"version_id": e.to_version_id, "block_id": e.to_block_id, "purpose": e.purpose}
        for e in db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id == b.version_id,
            m.EvidenceLink.from_block_id == b.block_id))]
    return {"block_id": b.block_id, "ordinal": b.ordinal, "block_type": b.block_type,
        "data": b.data, "locator": b.locator or {}, "citations": citations}


def block_rows(db, version_id):
    from .projection_read import memo
    return memo(db, "block-rows", version_id, lambda: list(db.scalars(select(m.ContentBlock)
        .where(m.ContentBlock.version_id == version_id).order_by(m.ContentBlock.ordinal))))


def evidence_links(db, version_id):
    from .projection_read import memo
    return memo(db, "evidence-link-rows", version_id, lambda: list(db.scalars(select(m.EvidenceLink)
        .where(m.EvidenceLink.from_version_id == version_id))))


def relation_rows(db, version_id):
    from .projection_read import memo
    return memo(db, "relation-rows", version_id, lambda: list(db.scalars(select(m.RelationEdge)
        .where(m.RelationEdge.source_version_id == version_id))))


def content_blocks(db, version_id):
    by_block = {}
    for e in evidence_links(db, version_id):
        by_block.setdefault(e.from_block_id, []).append({"version_id": e.to_version_id,
            "block_id": e.to_block_id, "purpose": e.purpose})
    return [{"block_id": b.block_id, "ordinal": b.ordinal, "block_type": b.block_type,
        "data": b.data, "locator": b.locator or {}, "citations": by_block.get(b.block_id, [])}
        for b in block_rows(db, version_id)]


version_blocks = content_blocks


def require_resource(session, user, resource_id, action="read", include_deleted=False):
    return resource_access(session, user, resource_id, action, allow_deleted=include_deleted)


def version_dict(db, v):
    keys = ("id", "resource_id", "version_no", "state", "revision", "author_id", "source_verified",
        "content_sha256", "title", "knowledge_type", "applicability", "required_facts", "legal_status",
        "valid_from", "valid_to", "origin", "base_version_id", "source_url")
    blob = db.get(m.Blob, v.source_blob_id) if v.source_blob_id else None
    resource = db.get(m.Resource, v.resource_id)
    if blob and (not resource or blob.space_id != resource.space_id):
        blob = None
    return primitive({**{k: getattr(v, k) for k in keys}, "blocks": content_blocks(db, v.id),
        "source_filename": Path(blob.object_key).name if blob else None,
        "mime_type": blob.mime_type if blob else None})


def resource_dict(db, r, user=None):
    keys = ("id", "space_id", "kind", "name", "category", "tags", "owner_id", "revision",
        "access_epoch", "restricted", "classification", "suspended", "deleted_at", "active_release_id")
    release = db.get(m.Release, r.active_release_id) if r.active_release_id else None
    view = {**{k: getattr(r, k) for k in keys}, "active_version_id": release.version_id if release else None,
        "created_at": r.created_at, "updated_at": r.updated_at}
    owner = db.get(m.User, r.owner_id)
    view["owner_name"] = owner.display_name if owner else None
    visible = []
    if user and not r.deleted_at:
        for v in db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.resource_id == r.id)
            .order_by(m.ResourceVersion.version_no.desc())):
            try:
                version_access(db, user, v)
                visible.append(v)
            except APIError:
                continue
    elif release and not r.deleted_at:
        candidate = db.get(m.ResourceVersion, release.version_id)
        if candidate:
            visible.append(candidate)
    latest = visible[0] if visible else None
    view.update(latest_version_id=latest.id if latest else None, latest_version_revision=latest.revision if latest else None,
        latest_version_no=latest.version_no if latest else None, latest_state=latest.state if latest else None,
        file_extension=None, mime_type=None)
    if latest and latest.source_blob_id:
        blob = db.get(m.Blob, latest.source_blob_id)
        if blob and blob.scan_state == "CLEAN":
            view.update(file_extension=Path(blob.object_key).suffix.lstrip(".").lower(), mime_type=blob.mime_type)
    return primitive(view)


def job_dict(j):
    result = primitive({k: getattr(j, k) for k in ("id", "kind", "state", "stage", "attempts", "error_code", "result")})
    if j.kind == "COMPILE" and (j.payload or {}).get("task") == "VECTOR_INDEX":
        result["task"] = "VECTOR_INDEX"
    return result


def tagged(body, obj, status=200):
    return Result(body, status, {"ETag": f'"{obj.revision}"'})


def require_etag(ctx, obj):
    raw = ctx.request.headers.get("if-match")
    if raw is None:
        fail(428, "PRECONDITION_REQUIRED", "缺少If-Match")
    if raw != f'"{obj.revision}"':
        fail(412, "REVISION_CONFLICT", "记录已被修改，请重新读取", revision=obj.revision)


def bump(db, obj, **fields):
    revision = obj.revision
    result = db.execute(update(type(obj)).where(type(obj).id == obj.id, type(obj).revision == revision)
        .values(revision=revision + 1, **fields).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        fail(412, "REVISION_CONFLICT", "并发修改冲突，请重新读取")
    db.refresh(obj)


def create_job(ctx, kind, payload=None, resource_id=None, version_id=None, run_id=None, dedupe_key=None):
    job_id = uid()
    j = m.Job(id=job_id, kind=kind, state="QUEUED", owner_id=ctx.user.id,
        resource_id=resource_id, version_id=version_id, run_id=run_id, stage="queued",
        dedupe_key=dedupe_key or f"{kind}:{job_id}", payload=payload or {}, attempts=0,
        cancel_requested=False)
    ctx.db.add(j)
    ctx.db.flush()
    ctx.db.add(m.Outbox(id=uid(), event_type="JOB_CREATED", aggregate_id=j.id,
        payload={"job_id": j.id, "kind": kind}))
    audit(ctx, "job.created", j, {"kind": kind})
    ctx.dispatch.append(j.id)
    return j


def paginate(items, query, key=lambda obj: (aware(obj.created_at).isoformat(), obj.id)):
    limit = int(query.get("limit", 20))
    cursor = query.get("cursor")
    ordered = sorted(items, key=key, reverse=True)
    if cursor:
        try:
            anchor = tuple(json.loads(base64.urlsafe_b64decode(cursor.encode()).decode()))
        except (ValueError, TypeError, UnicodeError):
            fail(400, "INVALID_CURSOR", "分页游标无效")
        ordered = [x for x in ordered if key(x) < anchor]
    page = ordered[:limit]
    nxt = base64.urlsafe_b64encode(json.dumps(key(page[-1])).encode()).decode() if len(ordered) > limit else None
    return page, nxt


def storage_path(settings, object_key):
    root = Path(settings.storage_dir).resolve()
    path = (root / object_key).resolve()
    if not path.is_relative_to(root):
        fail(409, "INVALID_STORAGE_KEY", "存储对象定位无效")
    return path


def check_frozen_hash(db, version):
    content = version_dict(db, version)
    for key in ("id", "resource_id", "version_no", "state", "revision", "source_verified", "content_sha256",
        "source_filename", "mime_type"):
        content.pop(key, None)
    for block in content["blocks"]:
        block["citations"] = sorted(block["citations"], key=lambda x: (x["version_id"], x["block_id"], x["purpose"]))
    content["source_url"] = version.source_url
    content["source_blob_id"] = version.source_blob_id
    blob = db.get(m.Blob, version.source_blob_id) if version.source_blob_id else None
    content["source_sha256"] = blob.sha256 if blob else None
    content["relations"] = sorted([relation_dict(e) for e in relation_rows(db, version.id)], key=digest)
    return digest(content)


def relation_dict(e):
    data = {"target_resource_id": e.target_resource_id, "relation_type": e.relation_type,
        "conditions": e.conditions or {}}
    if e.evidence_version_id:
        data.update(evidence_version_id=e.evidence_version_id, evidence_block_id=e.evidence_block_id)
    return data


def invalidate_dependents(ctx, resource, reason):
    """Persist the invalidation event; authority checks close the asynchronous projection gap."""
    ids = set(ctx.db.scalars(select(m.ResourceVersion.id).where(m.ResourceVersion.resource_id == resource.id)))
    affected = set(ids)
    for _ in range(8):
        dependents = set(ctx.db.scalars(select(m.EvidenceLink.from_version_id)
            .where(m.EvidenceLink.to_version_id.in_(affected)))) if affected else set()
        if dependents <= affected:
            break
        affected |= dependents
    if affected:
        run_ids = set(ctx.db.scalars(select(m.RunEvidence.run_id).where(m.RunEvidence.version_id.in_(affected))))
        if run_ids:
            ctx.db.execute(update(m.ConsultationRun).where(m.ConsultationRun.id.in_(run_ids))
                .values(invalidated_at=now()))
    return create_job(ctx, "INVALIDATE", {"resource_id": resource.id, "reason": reason}, resource_id=resource.id)
