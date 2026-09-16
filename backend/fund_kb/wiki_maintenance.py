"""Reviewable Wiki maintenance, not an automatic rewrite or legal determination.

Portable RuntimePolicy sidecars; transactions/CSRF/idempotency are owned by the
central dispatcher. No model, vector, file, network or background-job dependency.
The catalog helper deliberately consumes a caller-admitted ID set, not bodies.
"""
from __future__ import annotations

import copy
import itertools
import unicodedata
from dataclasses import replace
from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy import select

from . import models as m
from . import services as svc

ENTRY_PREFIX = "wiki-maint-entry:"
PROPOSAL_PREFIX = "wiki-maint-proposal:"
SCAN_PREFIX = "wiki-maint-scan:"
REVISION_PREFIX = "wiki-maint-revision:"
KINDS = ("REVISION", "CONFLICT", "CONSOLIDATION")
STATUSES = ("PROPOSED", "ACCEPTED", "REJECTED")
TITLE_SIMILARITY_THRESHOLD = 0.82
NOTICE = "维护建议不是语义重复、法规冲突或业务效力的事实认定；不会自动发布或删除资料。"


def _not_found():
    svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")


def _normal(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _label(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        svc.fail(422, "INVALID_WIKI_NAME", "主条目标识或别名须为文本")
    value = unicodedata.normalize("NFKC", value).strip()
    if not value or len(value) > 300 or any(unicodedata.category(c) in {"Cc", "Cf"} for c in value) \
            or any(c in value for c in "<>[]|"):
        svc.fail(422, "INVALID_WIKI_NAME", "主条目标识或别名须为1至300字符的纯文本名称")
    return value


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (TypeError, ValueError, AttributeError):
        svc.fail(422, "INVALID_IDENTIFIER", "对象标识无效")
    return value


def _reason(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        svc.fail(422, "MAINTENANCE_REASON_REQUIRED", "请填写1至2000字符的维护原因或审阅说明")
    return value.strip()


def _policy(db, name):
    return db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == name)
                     .execution_options(populate_existing=True))


def _entry_config(resource, policy=None):
    if policy is None:
        return {"schema_version": 1, "space_id": resource.space_id, "resource_id": resource.id,
                "canonical_key": None, "aliases": list(dict.fromkeys(
                    tag.partition(":")[2].strip() for tag in resource.tags or []
                    if isinstance(tag, str) and tag.startswith("alias:") and tag.partition(":")[2].strip())),
                "canonical_resource_id": resource.id}
    data = policy.config
    if not isinstance(data, dict) or data.get("schema_version") != 1 \
            or data.get("resource_id") != resource.id or data.get("space_id") != resource.space_id \
            or not isinstance(data.get("aliases"), list) \
            or not all(isinstance(x, str) for x in data["aliases"]) \
            or not isinstance(data.get("canonical_resource_id"), str) \
            or data.get("canonical_key") is not None and not isinstance(data["canonical_key"], str):
        _not_found()
    return copy.deepcopy(data)


def _chunks(values):
    values = list(values)
    for start in range(0, len(values), 400):
        yield values[start:start + 400]  # SQL bind batches, not a result limit.


def _root(resource_id, configs):
    seen, current = set(), resource_id
    while current in configs:
        if current in seen:
            return None
        seen.add(current)
        target = configs[current]["canonical_resource_id"]
        if target == current:
            return current
        current = target
    return None


def catalog_metadata(db, user, space_id, authorized_resource_ids):
    """Metadata for *this call's already-authorized* IDs; never a body grant.

    The catalog owner checks CatalogAuthority and visible endpoints. This helper
    neither re-admits a whole corpus nor reads ContentBlock or lineage bodies.
    No per-user/global cross-request cache is used. Unlisted canonical targets,
    chains through hidden nodes, and their keys/titles are not exposed.
    """
    svc.space_access(db, user, space_id)
    resources, policies = {}, {}
    for batch in _chunks(set(authorized_resource_ids)):
        for r in db.scalars(select(m.Resource).where(m.Resource.id.in_(batch), m.Resource.space_id == space_id,
                m.Resource.kind == "knowledge", m.Resource.deleted_at.is_(None), m.Resource.suspended.is_(False))
                .execution_options(populate_existing=True)):
            resources[r.id] = r
        names = [ENTRY_PREFIX + rid for rid in batch]
        for p in db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.in_(names))
                            .execution_options(populate_existing=True)):
            policies[p.name.removeprefix(ENTRY_PREFIX)] = p
    configs = {}
    for rid, resource in resources.items():
        try:
            configs[rid] = _entry_config(resource, policies.get(rid))
        except svc.APIError:
            continue  # Invalid sidecar must never turn into a routing grant.
    result = {}
    for rid, data in configs.items():
        root = _root(rid, configs)
        result[rid] = {"resource_id": rid, "canonical_key": data["canonical_key"],
            "aliases": data["aliases"], "canonical_resource_id": root,
            "is_canonical": root == rid, "canonical_available": root is not None,
            "revision": policies[rid].revision if rid in policies else 0}
    return result


def _live_version(db, user, resource, *, exact=None):
    svc.resource_access(db, user, resource)
    if resource.suspended:
        _not_found()
    versions = ([db.get(m.ResourceVersion, exact)] if exact else db.scalars(select(m.ResourceVersion)
        .where(m.ResourceVersion.resource_id == resource.id).order_by(m.ResourceVersion.version_no.desc())
        .execution_options(populate_existing=True)))
    for v in versions:
        if v is None or v.resource_id != resource.id:
            _not_found()
        try:
            svc.version_access(db, user, v)
        except svc.APIError:
            if exact:
                raise
            continue
        if v.source_blob_id:
            blob = db.get(m.Blob, v.source_blob_id)
            if not blob or blob.space_id != resource.space_id or blob.scan_state != "CLEAN":
                if exact:
                    _not_found()
                continue
        return v
    _not_found()


def _entry(db, user, resource_id, *, space_id=None, edit=False):
    resource = db.scalar(select(m.Resource).where(m.Resource.id == resource_id)
                         .execution_options(populate_existing=True))
    if resource is None or resource.kind != "knowledge" or (space_id and resource.space_id != space_id):
        _not_found()
    version = _live_version(db, user, resource)
    if edit:
        svc.resource_access(db, user, resource, "edit")
        # Legacy editors cannot edit another author's unreleased private draft.
        if not resource.active_release_id and not svc.unpublished_resource_access(db, user, resource):
            _not_found()
        if version.state == "DRAFT" and not svc.can_edit_draft(db, user, version):
            svc.fail(403, "DRAFT_OWNER_REQUIRED", "只能维护本人草稿或有编辑权限的团队草稿")
    return resource, version


def _entries(db, user, space_id, ids=None, *, edit=False):
    svc.space_access(db, user, space_id, "editor" if edit else None)
    specified = ids is not None
    ids = list(dict.fromkeys(ids)) if specified else list(db.scalars(select(m.Resource.id)
        .where(m.Resource.space_id == space_id, m.Resource.kind == "knowledge",
               m.Resource.deleted_at.is_(None), m.Resource.suspended.is_(False)).order_by(m.Resource.id)))
    result = {}
    for rid in ids:
        try:
            result[rid] = _entry(db, user, rid, space_id=space_id, edit=edit)
        except svc.APIError:
            if specified:
                raise
    return result


def _resolve_from_entries(db, user, space_id, title, entries):
    metadata = catalog_metadata(db, user, space_id, entries)
    matched = []
    for rid, (r, v) in entries.items():
        data = metadata.get(rid)
        if data and _normal(title) in {_normal(x) for x in [r.name, v.title, data["canonical_key"], *data["aliases"]] if x}:
            matched.append(rid)
    if not matched:
        _not_found()
    targets = {metadata[rid]["canonical_resource_id"] for rid in matched}
    if None in targets:
        _not_found()
    if len(targets) != 1:
        svc.fail(409, "WIKI_TITLE_AMBIGUOUS", "名称匹配多个当前可见主条目，请指定条目")
    target = next(iter(targets))
    resource, version = entries[target]
    return {"resource_id": resource.id, "version_id": version.id, "title": version.title,
            "matched_resource_ids": sorted(matched), "navigation_only": True}


def resolve_alias(db, user, space_id, title, authorized_resource_ids=None):
    title = _label(title)
    if authorized_resource_ids is None:
        entries = _entries(db, user, space_id)
    else:
        # Caller already admitted the IDs. Only metadata here, no full text read.
        svc.space_access(db, user, space_id)
        entries = {}
        for batch in _chunks(set(authorized_resource_ids)):
            for r in db.scalars(select(m.Resource).where(m.Resource.id.in_(batch), m.Resource.space_id == space_id,
                    m.Resource.kind == "knowledge", m.Resource.deleted_at.is_(None), m.Resource.suspended.is_(False))):
                # Only version *visibility* is rechecked; returned ID is not a
                # grant to use its body. CatalogAuthority rechecks endpoints.
                for v in db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.resource_id == r.id)
                                    .order_by(m.ResourceVersion.version_no.desc())):
                    try:
                        svc._version_acl(db, user, v, r)
                        entries[r.id] = r, v
                        break
                    except svc.APIError:
                        continue
    return _resolve_from_entries(db, user, space_id, title, entries)


def _lock_space(ctx, space_id):
    svc.space_access(ctx.db, ctx.user, space_id, "editor")
    space = ctx.db.scalar(select(m.Space).where(m.Space.id == space_id).with_for_update()
                          .execution_options(populate_existing=True))
    svc.space_access(ctx.db, ctx.user, space_id, "editor")
    return space


def _lock_resources(db, ids):
    for rid in sorted(set(ids)):
        db.scalar(select(m.Resource).where(m.Resource.id == rid).with_for_update()
                  .execution_options(populate_existing=True))


def _lock_snapshot(db, snapshot):
    _lock_resources(db, (x["resource_id"] for x in snapshot["resources"]))
    ids = {x["version_id"] for x in snapshot["versions"]} | set(snapshot["latest_version_ids"].values())
    for vid in sorted(ids):
        db.scalar(select(m.ResourceVersion).where(m.ResourceVersion.id == vid).with_for_update()
                  .execution_options(populate_existing=True))
    # A first authorization pass may have checked Wiki provenance before the
    # locks were acquired. Do not reuse that pre-lock hash/adjacency result.
    db.info.pop("wiki_checked_content_hash", None)
    if "fkb_dependency_structure" in db.info:
        db.info["fkb_dependency_structure"].clear()


def _resource_stamp(db, r):
    # Hash the permission rows, never return another user's grant/identity list.
    grants = sorted(tuple(row) for row in db.execute(select(m.ResourceGrant.user_id, m.ResourceGrant.permission)
                                                    .where(m.ResourceGrant.resource_id == r.id)))
    return {"resource_id": r.id, "revision": r.revision, "access_epoch": r.access_epoch,
            "classification": r.classification, "restricted": r.restricted,
            "active_release_id": r.active_release_id, "grants_digest": svc.digest(grants)}


def _snapshot(db, user, space_id, resource_ids, *, extra_versions=(), deep=False):
    """Freeze explicit inputs and all exact source dependencies, never bodies.

    Deep hashes are computed only for maintenance review, not catalog_metadata.
    Reads return no partial proposal if any source/input becomes inaccessible.
    """
    from . import wiki
    space = svc.space_access(db, user, space_id)
    entries = _entries(db, user, space_id, resource_ids)
    pending = [v.id for _, v in entries.values()] + list(extra_versions)
    versions, resources, metadata, latest = {}, {}, {}, {}
    # Include canonical navigation chains in the snapshot, using ordinary ACLs.
    queue, seen = list(entries), set()
    while queue:
        rid = queue.pop()
        if rid in seen:
            continue
        seen.add(rid)
        r, v = _entry(db, user, rid, space_id=space_id)
        p = _policy(db, ENTRY_PREFIX + rid)
        data = _entry_config(r, p)
        metadata[rid] = {"revision": p.revision if p else 0, "digest": svc.digest(data)}
        latest[rid] = v.id
        pending.append(v.id)
        target = data["canonical_resource_id"]
        if target != rid:
            queue.append(target)
    while pending:
        vid = pending.pop()
        if vid in versions:
            continue
        v = svc.version_access(db, user, vid)
        r = svc.resource_access(db, user, v.resource_id)
        # Cross-space sources may exist in old Wiki; maintenance does not copy
        # their metadata into this space's proposals or alter their bindings.
        if r.space_id != space_id or r.suspended:
            _not_found()
        blob = db.get(m.Blob, v.source_blob_id) if v.source_blob_id else None
        if v.source_blob_id and (not blob or blob.space_id != space_id or blob.scan_state != "CLEAN"):
            _not_found()
        if deep:
            blocks = wiki._blocks(db, v)
            content_hash = wiki._checked_hash(db, v)
            if blocks is None or v.content_sha256 and content_hash != v.content_sha256:
                svc.fail(409, "MAINTENANCE_SOURCE_CHANGED", "维护输入的正文或来源快照校验失败")
        else:
            content_hash = v.content_sha256
        versions[v.id] = {"version_id": v.id, "resource_id": r.id, "revision": v.revision,
            "version_no": v.version_no, "state": v.state, "content_sha256": content_hash,
            "source_blob_sha256": blob.sha256 if blob else None}
        resources[r.id] = _resource_stamp(db, r)
        if r.id not in latest:
            latest[r.id] = _live_version(db, user, r).id
        dependencies = set(svc.dependency_ids(db, v))
        for edge in svc.relation_rows(db, v.id):
            target = svc.resource_access(db, user, edge.target_resource_id)
            if target.space_id != space_id or target.suspended:
                _not_found()
            resources[target.id] = _resource_stamp(db, target)
            latest[target.id] = _live_version(db, user, target).id
            if edge.relation_type == "DEPENDS_ON":
                dependencies.add(latest[target.id])
        pending.extend(dependencies - versions.keys())
    governance = _policy(db, "space-governance:" + space_id)
    return svc.primitive({"space_id": space_id, "space_revision": space.revision,
        "governance_digest": svc.digest(governance.config if governance else None),
        "resources": sorted(resources.values(), key=lambda x: x["resource_id"]),
        "versions": sorted(versions.values(), key=lambda x: x["version_id"]),
        "latest_version_ids": dict(sorted(latest.items())), "metadata": dict(sorted(metadata.items()))})


def _etag(kind, revision, snapshot):
    return f'"{kind}-{revision}-{svc.digest(snapshot)}"'


def _match(ctx, etag):
    supplied = ctx.request.headers.get("if-match")
    if supplied is None:
        svc.fail(428, "PRECONDITION_REQUIRED", "请先读取当前对象并提供If-Match")
    if supplied != etag:
        svc.fail(412, "REVISION_CONFLICT", "维护对象或其来源已经变化，请重新读取")


def entry_metadata(db, user, resource_id):
    resource, version = _entry(db, user, resource_id)
    own = _policy(db, ENTRY_PREFIX + resource.id)
    data = _entry_config(resource, own)
    # A missing/revoked target should not suppress this independent entry; it
    # must also not disclose that target's ID, title or aliases.
    configs, current, seen = {resource.id: data}, resource.id, set()
    while current not in seen:
        seen.add(current)
        target = configs[current]["canonical_resource_id"]
        if target == current:
            break
        try:
            r, _ = _entry(db, user, target, space_id=resource.space_id)
            configs[target] = _entry_config(r, _policy(db, ENTRY_PREFIX + target))
        except svc.APIError:
            break
        current = target
    root = _root(resource.id, configs)
    snapshot = _snapshot(db, user, resource.space_id, [resource.id]) if root else {
        "resource": _resource_stamp(db, resource), "version": [version.id, version.revision],
        "metadata": svc.digest(data), "canonical_available": False}
    can_edit = True
    try:
        _entry(db, user, resource.id, edit=True)
    except svc.APIError:
        can_edit = False
    revision = own.revision if own else 0
    return {"resource_id": resource.id, "space_id": resource.space_id, "title": version.title,
        "version_id": version.id, "canonical_key": data["canonical_key"], "aliases": data["aliases"],
        "canonical_resource_id": root, "canonical_available": root is not None, "is_canonical": root == resource.id,
        "revision": revision, "can_edit": can_edit, "etag": _etag("wme", revision, snapshot), "notice": NOTICE}


def _collision_check(db, user, space_id, resource_id, canonical_key, aliases, *, joined=()):
    entries = _entries(db, user, space_id)
    metadata = catalog_metadata(db, user, space_id, entries)
    names = {_normal(x) for x in [canonical_key, *aliases] if x}
    root = metadata.get(resource_id, {}).get("canonical_resource_id")
    for rid, (r, v) in entries.items():
        data = metadata.get(rid)
        if not data or rid == resource_id or rid in joined or root and data["canonical_resource_id"] == root:
            continue
        competing = {_normal(x) for x in [r.name, v.title, data["canonical_key"], *data["aliases"]] if x}
        if names & competing:
            svc.fail(409, "WIKI_ALIAS_CONFLICT", "主条目标识或别名与当前可见条目冲突；请先审阅归并建议")


def _save_entry(ctx, resource, data):
    policy = _policy(ctx.db, ENTRY_PREFIX + resource.id)
    if policy:
        svc.bump(ctx.db, policy, config=copy.deepcopy(data), updated_by=ctx.user.id)
    else:
        policy = m.RuntimePolicy(id=svc.uid(), name=ENTRY_PREFIX + resource.id,
                                 config=copy.deepcopy(data), updated_by=ctx.user.id)
        ctx.db.add(policy)
    ctx.db.flush()
    return policy


def save_entry(ctx):
    resource, _ = _entry(ctx.db, ctx.user, ctx.id, edit=True)
    _lock_space(ctx, resource.space_id)
    _lock_resources(ctx.db, [resource.id])
    current = entry_metadata(ctx.db, ctx.user, resource.id)
    _match(ctx, current["etag"])
    reason = _reason(ctx.data.get("reason"))
    canonical_key = _label(ctx.data.get("canonical_key"), nullable=True)
    aliases = ctx.data.get("aliases")
    if not isinstance(aliases, list):
        svc.fail(422, "INVALID_WIKI_ALIASES", "别名须为文本列表")
    aliases = [_label(x) for x in aliases]
    if len({_normal(x) for x in aliases}) != len(aliases):
        svc.fail(422, "DUPLICATE_WIKI_ALIAS", "标准化后的别名不能重复")
    if canonical_key and _normal(canonical_key) in {_normal(x) for x in aliases}:
        svc.fail(422, "DUPLICATE_WIKI_ALIAS", "主条目标识无需重复登记为别名")
    _collision_check(ctx.db, ctx.user, resource.space_id, resource.id, canonical_key, aliases)
    policy = _policy(ctx.db, ENTRY_PREFIX + resource.id)
    data = _entry_config(resource, policy)
    data.update(canonical_key=canonical_key, aliases=aliases)
    _save_entry(ctx, resource, data)
    svc.audit(ctx, "wiki.maintenance.metadata_saved", resource,
              {"reason": reason, "aliases_count": len(aliases), "navigation_only": True})
    body = entry_metadata(ctx.db, ctx.user, resource.id)
    return svc.Result(body, headers={"ETag": body["etag"]})


def _proposal(db, user, proposal_id):
    policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.id == proposal_id)
                       .execution_options(populate_existing=True))
    if not policy or not policy.name.startswith(PROPOSAL_PREFIX):
        _not_found()
    data = policy.config
    if data.get("schema_version") != 1 or data.get("kind") not in KINDS or data.get("status") not in STATUSES:
        _not_found()
    svc.space_access(db, user, data["space_id"])
    _entries(db, user, data["space_id"], data["resource_ids"])
    # Stored snapshots/reasons may name every dependency, including old inputs.
    for version in data["base_snapshot"]["versions"]:
        svc.version_access(db, user, version["version_id"])
    return policy


def _current_proposal_snapshot(db, user, data):
    return _snapshot(db, user, data["space_id"], data["resource_ids"],
                     extra_versions=data.get("extra_version_ids", []), deep=True)


def _candidate_blockers(db, user, data):
    if not data.get("compiled_revision"):
        return []
    from . import wiki
    resource = db.get(m.Resource, data["resource_ids"][0])
    for snapshot in data["compiled_revision"]["source_snapshots"]:
        vid = snapshot["version_id"]
        if svc.is_released(db, db.get(m.ResourceVersion, vid)):
            continue
        try:
            if wiki.draft_reference_allowed(db, user, resource, vid):
                continue
        except svc.APIError:
            pass
        return ["COMPILED_REVISION_SOURCE_NOT_ADMITTED"]
    return []


def proposal_view(db, user, proposal_id, *, include_candidate=False):
    policy = _proposal(db, user, proposal_id)
    data = policy.config
    current = _current_proposal_snapshot(db, user, data)
    expected = data.get("applied_snapshot") or data["base_snapshot"]
    entries = _entries(db, user, data["space_id"], data["resource_ids"])
    can_review = True
    try:
        _entries(db, user, data["space_id"], data["resource_ids"], edit=True)
    except svc.APIError:
        can_review = False
    # Historical source changes remain viewable only when *both* snapshots can
    # still be read. A new user never inherits the proposing user's visibility.
    changes = copy.deepcopy(data.get("source_changes", []))
    for change in changes:
        for key in ("old_version_id", "new_version_id"):
            svc.version_access(db, user, change[key])
    result = {"id": policy.id, "space_id": data["space_id"], "kind": data["kind"], "status": data["status"],
        "resource_ids": data["resource_ids"], "entries": [{"resource_id": rid, "title": v.title, "version_id": v.id}
            for rid, (_, v) in sorted(entries.items())],
        "target_resource_id": data.get("target_resource_id"), "draft_title": data.get("draft_title"),
        "reason": data["reason"], "origin": data["origin"], "detection": copy.deepcopy(data.get("detection")),
        "verification_status": "HEURISTIC_SUGGESTION" if data["origin"] == "SCAN" else
            "COMPILED_DRAFT_UNVERIFIED" if data["origin"] == "COMPILER" else "HUMAN_PROPOSAL",
        "created_at": data["created_at"], "created_by": data["created_by"], "revision": policy.revision,
        "snapshot_current": svc.digest(current) == svc.digest(expected), "snapshot_digest": svc.digest(expected),
        "source_changes": changes, "review": copy.deepcopy(data.get("review")),
        "result": copy.deepcopy(data.get("result")), "conflict_state": data.get("conflict_state"),
        "resolution": copy.deepcopy(data.get("resolution")), "can_review": can_review,
        "has_compiled_candidate": bool(data.get("compiled_revision")),
        "candidate_block_count": len((data.get("compiled_revision") or {}).get("candidate", {}).get("blocks", [])),
        "application_blockers": _candidate_blockers(db, user, data),
        "etag": _etag("wmp", policy.revision, current), "notice": NOTICE}
    if result["result"] and result["result"].get("new_version_id"):
        svc.version_access(db, user, result["result"]["new_version_id"])
    if include_candidate:
        result["compiled_revision"] = copy.deepcopy(data.get("compiled_revision"))
    return svc.primitive(result)


def _new_proposal(ctx, data, *, origin="HUMAN", source_changes=(), scan_key=None, detection=None, compiled_revision=None):
    space_id, kind = _uuid(data["space_id"]), data.get("kind")
    if kind not in KINDS:
        svc.fail(422, "INVALID_MAINTENANCE_KIND", "维护类型无效")
    reason = _reason(data.get("reason"))
    ids = data.get("resource_ids")
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        svc.fail(422, "INVALID_MAINTENANCE_ENTRIES", "请指定不重复的知识条目")
    ids = sorted(_uuid(x) for x in ids)
    if kind == "REVISION" and len(ids) != 1 or kind in {"CONFLICT", "CONSOLIDATION"} and len(ids) < 2:
        svc.fail(422, "INVALID_MAINTENANCE_ENTRIES", "修订选一个条目，冲突或归并至少选两个条目")
    _lock_space(ctx, space_id)
    _lock_resources(ctx.db, ids)
    _entries(ctx.db, ctx.user, space_id, ids, edit=True)
    target = data.get("target_resource_id")
    if kind == "CONSOLIDATION":
        if target not in ids:
            svc.fail(422, "CANONICAL_TARGET_REQUIRED", "请从选中条目中指定保留的导航主条目")
        configs = {rid: _entry_config(_entry(ctx.db, ctx.user, rid)[0], _policy(ctx.db, ENTRY_PREFIX + rid)) for rid in ids}
        if configs[target]["canonical_resource_id"] != target:
            svc.fail(409, "CANONICAL_TARGET_NOT_ROOT", "目标已指向其他主条目，请重新选择，不能形成导航循环")
    elif target is not None:
        svc.fail(422, "INVALID_CANONICAL_TARGET", "只有归并建议可指定导航主条目")
    draft_title = data.get("draft_title")
    if draft_title is not None:
        if kind != "REVISION":
            svc.fail(422, "INVALID_DRAFT_TITLE", "只有修订建议可指定草稿标题")
        draft_title = _label(draft_title)
    extra_versions = sorted({change[key] for change in source_changes for key in ("old_version_id", "new_version_id")} |
        {s["version_id"] for s in (compiled_revision or {}).get("source_snapshots", [])})
    preliminary = _snapshot(ctx.db, ctx.user, space_id, ids, extra_versions=extra_versions)
    _lock_snapshot(ctx.db, preliminary)
    snapshot = _snapshot(ctx.db, ctx.user, space_id, ids, extra_versions=extra_versions, deep=True)
    current_metadata = _snapshot(ctx.db, ctx.user, space_id, ids, extra_versions=extra_versions)
    if svc.digest(preliminary) != svc.digest(current_metadata):
        svc.fail(409, "MAINTENANCE_SNAPSHOT_CHANGED", "登记期间输入发生并发变化，请重新提出建议")
    if compiled_revision:
        versions = {x["version_id"]: x for x in snapshot["versions"]}
        resources = {x["resource_id"]: x for x in snapshot["resources"]}
        for source in compiled_revision["source_snapshots"]:
            current = versions[source["version_id"]]
            if current["content_sha256"] != source["content_sha256"] \
                    or current["source_blob_sha256"] != source["source_blob_sha256"] \
                    or resources[source["resource_id"]]["access_epoch"] != source["access_epoch"]:
                svc.fail(409, "COMPILED_SOURCE_SNAPSHOT_CHANGED", "编译候选来源在登记期间变化，请重新编译")
    content = {"schema_version": 1, "space_id": space_id, "kind": kind, "status": "PROPOSED",
        "resource_ids": ids, "target_resource_id": target, "draft_title": draft_title, "reason": reason,
        "origin": origin, "created_at": svc.primitive(svc.now()), "created_by": ctx.user.id,
        "base_snapshot": snapshot, "extra_version_ids": extra_versions, "source_changes": list(source_changes),
        "detection": copy.deepcopy(detection), "compiled_revision": copy.deepcopy(compiled_revision)}
    fingerprint = svc.digest({"key": scan_key, "space_id": space_id, "kind": kind, "ids": ids, "target": target,
                              "snapshot": snapshot}) if scan_key else svc.uid()
    name = f"{PROPOSAL_PREFIX}{space_id}:{fingerprint}"
    existing = _policy(ctx.db, name)
    if existing:
        _proposal(ctx.db, ctx.user, existing.id)
        return existing, False
    policy = m.RuntimePolicy(id=svc.uid(), name=name, config=content, updated_by=ctx.user.id)
    ctx.db.add(policy)
    ctx.db.flush()
    svc.audit(ctx, "wiki.maintenance.proposed", policy,
              {"kind": kind, "origin": origin, "resource_ids": ids, "automatic_changes": False})
    return policy, True


def create_proposal(ctx):
    policy, _ = _new_proposal(ctx, ctx.data)
    body = proposal_view(ctx.db, ctx.user, policy.id)
    return svc.Result(body, 201, {"ETag": body["etag"]})


def propose_compiled_revision(ctx, resource_id, candidate, source_snapshots, *, compilation_job_id=None):
    """Bridge a full compiled page to a *proposal*, never silently title-skip.

    `ctx` is the usual services.Context in the caller-owned write transaction.
    `candidate` has title, complete native ContentBlocks and optional
    knowledge_type/applicability/required_facts. Exact citations are native
    version_id/block_id pairs, not model EIDs. All cited sources must be in the
    provided frozen snapshots. Return the same public proposal envelope as POST.
    No real model call; newly unadmitted draft sources remain previewable but
    require a future properly governed recompile before they can be applied.
    """
    from . import api_content
    from . import wiki

    resource, base = _entry(ctx.db, ctx.user, resource_id, edit=True)
    _lock_space(ctx, resource.space_id)
    _lock_resources(ctx.db, [resource_id])
    if not isinstance(candidate, dict) or set(candidate) - {"title", "blocks", "knowledge_type", "applicability", "required_facts"}:
        svc.fail(422, "INVALID_COMPILED_CANDIDATE", "候选稿须为完整原生内容块，不接受未解析模型输出")
    title = _label(candidate.get("title"))
    blocks = candidate.get("blocks")
    if not isinstance(blocks, list) or not blocks or not isinstance(source_snapshots, list) or not source_snapshots:
        svc.fail(422, "INVALID_COMPILED_CANDIDATE", "候选稿须提供完整正文及冻结来源")
    admitted, frozen = {}, []
    for snapshot in source_snapshots:
        if not isinstance(snapshot, dict) or not {"resource_id", "version_id", "content_sha256", "access_epoch"} <= snapshot.keys():
            svc.fail(422, "COMPILED_SOURCE_SNAPSHOT_REQUIRED", "来源需绑定版本、正文hash及权限epoch")
        vid = _uuid(snapshot["version_id"])
        if vid in admitted:
            svc.fail(422, "DUPLICATE_COMPILED_SOURCE", "冻结来源版本不能重复")
        v = svc.version_access(ctx.db, ctx.user, vid)
        source = svc.resource_access(ctx.db, ctx.user, v.resource_id)
        blob = ctx.db.get(m.Blob, v.source_blob_id) if v.source_blob_id else None
        content_hash = svc.check_frozen_hash(ctx.db, v)
        if source.space_id != resource.space_id or source.id != snapshot["resource_id"] or source.suspended \
                or source.access_epoch != snapshot["access_epoch"] or content_hash != snapshot["content_sha256"] \
                or v.content_sha256 and v.content_sha256 != content_hash or wiki._blocks(ctx.db, v) is None \
                or blob and (blob.scan_state != "CLEAN" or blob.space_id != resource.space_id) \
                or snapshot.get("source_blob_sha256") is not None and (not blob or blob.sha256 != snapshot["source_blob_sha256"]):
            svc.fail(409, "COMPILED_SOURCE_SNAPSHOT_CHANGED", "编译候选的来源快照已变化，不能登记为当前候选稿")
        admitted[vid] = v
        frozen.append({"resource_id": source.id, "version_id": vid, "content_sha256": content_hash,
                       "access_epoch": source.access_epoch, "source_blob_sha256": blob.sha256 if blob else None})
    expected_block_fields = {"block_id", "ordinal", "block_type", "data", "locator", "citations"}
    allowed_types = {"heading", "paragraph", "list", "table", "step", "warning", "formula", "image", "attachment"}
    structural = copy.deepcopy(blocks)
    citation_count = 0
    for block in structural:
        if not isinstance(block, dict) or set(block) != expected_block_fields \
                or not isinstance(block["block_type"], str) or block["block_type"] not in allowed_types \
                or type(block["ordinal"]) is not int or not isinstance(block["data"], dict) \
                or not isinstance(block["locator"], dict) or not isinstance(block["citations"], list):
            svc.fail(422, "INVALID_COMPILED_CANDIDATE", "候选正文块结构不完整")
        _uuid(block["block_id"])
        seen = set()
        for citation in block["citations"]:
            if not isinstance(citation, dict) or set(citation) != {"version_id", "block_id", "purpose"} \
                    or not isinstance(citation["purpose"], str) \
                    or citation["purpose"] not in {"RULE", "INTERNAL_OPINION", "FACT", "CASE", "CALCULATION"}:
                svc.fail(422, "INVALID_COMPILED_CITATION", "候选引用字段无效")
            key = (_uuid(citation["version_id"]), _uuid(citation["block_id"]), citation["purpose"])
            if key[0] not in admitted or key in seen \
                    or not ctx.db.get(m.ContentBlock, (key[0], key[1])):
                svc.fail(422, "INVALID_COMPILED_CITATION", "候选引用必须唯一并精确落在本次冻结来源中")
            seen.add(key)
            citation_count += 1
        # Native structural/XSS/data checks, without misusing the publication
        # gate to erase a reviewable candidate containing a new unreviewed source.
        block["citations"] = []
    if not citation_count:
        svc.fail(422, "COMPILED_CITATIONS_REQUIRED", "编译候选稿至少需要一个真实来源定位")
    probe = m.ResourceVersion(id=svc.uid(), resource_id=resource.id, state="DRAFT")
    api_content.validate_blocks(ctx, probe, structural)
    kt = candidate.get("knowledge_type", base.knowledge_type)
    if kt not in {"faq", "rule", "sop", "scenario", "case", "term"}:
        svc.fail(422, "RESOURCE_KIND_MISMATCH", "候选知识类型无效")
    applicability = copy.deepcopy(candidate.get("applicability", base.applicability or {}))
    api_content.validate_conditions(applicability)
    required_facts = copy.deepcopy(candidate.get("required_facts", base.required_facts or []))
    if not isinstance(required_facts, list) or any(not isinstance(x, str) or x not in api_content.CONTEXT_FIELDS for x in required_facts):
        svc.fail(422, "INVALID_REQUIRED_FACT", "候选缺失事实字段无效")
    compiled = {"candidate": {"title": title, "blocks": copy.deepcopy(blocks), "knowledge_type": kt,
        "applicability": applicability, "required_facts": required_facts}, "source_snapshots": frozen,
        "compilation_job_id": compilation_job_id, "verification_status": "UNVERIFIED"}
    policy, _ = _new_proposal(ctx, {"space_id": resource.space_id, "kind": "REVISION", "resource_ids": [resource_id],
        "draft_title": title, "reason": "同名知识已有新的完整编译候选，需人工预览审阅；接受仅创建同条目的新草稿，不覆盖现有版本。"},
        origin="COMPILER", compiled_revision=compiled, scan_key="compiled:" + svc.digest(compiled))
    return proposal_view(ctx.db, ctx.user, policy.id)


def list_proposals(ctx):
    space_id = ctx.query["space_id"]
    svc.space_access(ctx.db, ctx.user, space_id)
    if ctx.query.get("resource_id"):
        _entry(ctx.db, ctx.user, ctx.query["resource_id"], space_id=space_id)
    items = []
    for policy in ctx.db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.like(f"{PROPOSAL_PREFIX}{space_id}:%"))
                                .order_by(m.RuntimePolicy.updated_at.desc(), m.RuntimePolicy.id)):
        data = policy.config
        if any(ctx.query.get(key) and ctx.query[key] != data.get(key) for key in ("kind", "status")) \
                or ctx.query.get("resource_id") and ctx.query["resource_id"] not in data.get("resource_ids", []):
            continue
        try:
            items.append(proposal_view(ctx.db, ctx.user, policy.id))
        except svc.APIError:
            continue
    return svc.Result({"space_id": space_id, "items": items, "total": len(items), "truncated": False, "notice": NOTICE})


class _RequestView:
    def __init__(self, request, resource_id, etag=None):
        self._request, self.path_params = request, {"id": resource_id}
        if etag is not None:
            self.headers = {**dict(request.headers), "if-match": etag}

    def __getattr__(self, key):
        return getattr(self._request, key)


def _apply_revision(ctx, data):
    from . import api_content
    rid = data["resource_ids"][0]
    resource, _ = _entry(ctx.db, ctx.user, rid, edit=True)
    base_id = data["base_snapshot"]["latest_version_ids"][rid]
    base = svc.version_access(ctx.db, ctx.user, base_id)
    provenance = _policy(ctx.db, "wiki-provenance:" + rid)
    original_provenance = copy.deepcopy(provenance.config) if provenance else None
    # Reuse existing version/duplicate-draft/audit/citation invariants. There is
    # no direct UPDATE to a frozen version and no publication operation here.
    request = _RequestView(ctx.request, rid)
    create_ctx = replace(ctx, request=request, operation="createDraftVersion", data={
        "base_version_id": base.id, "title": data.get("draft_title") or base.title,
        "change_kind": "UPDATE", "change_reason": data["reason"]})
    response = api_content.versions(create_ctx)
    new_id = response.body["id"]
    compiled = data.get("compiled_revision")
    if compiled:
        candidate = compiled["candidate"]
        version = ctx.db.get(m.ResourceVersion, new_id)
        payload = {"title": candidate["title"], "knowledge_type": candidate["knowledge_type"],
            "applicability": copy.deepcopy(candidate["applicability"]), "required_facts": candidate["required_facts"],
            "legal_status": "UNKNOWN", "valid_from": svc.primitive(version.valid_from),
            "valid_to": svc.primitive(version.valid_to), "blocks": copy.deepcopy(candidate["blocks"])}
        edit_ctx = replace(ctx, request=_RequestView(ctx.request, new_id, f'"{version.revision}"'),
                           operation="editVersion", data=payload)
        api_content.edit_version(edit_ctx)
        # Accepting a machine-generated candidate is NOT source or business
        # approval. This changes only the newly created draft, never its base.
        svc.bump(ctx.db, version, origin="AI_DRAFT", source_verified=False, legal_status="UNKNOWN")
        if data.get("compilation_metadata") is not None:
            metadata = copy.deepcopy(data["compilation_metadata"])
            if not isinstance(metadata, dict):
                svc.fail(409, "INVALID_COMPILATION_METADATA", "编译规范元数据无效，未应用修订")
            version.content_sha256 = svc.check_frozen_hash(ctx.db, version)
            metadata.update(resource_id=rid, version_id=new_id, space_id=resource.space_id,
                compiled_content_sha256=version.content_sha256)
            ctx.db.add(m.RuntimePolicy(id=svc.uid(), name="wiki-compilation:" + new_id,
                                      updated_by=ctx.user.id, config=metadata))
    ctx.db.add(m.RuntimePolicy(id=svc.uid(), name=REVISION_PREFIX + new_id, updated_by=ctx.user.id,
        config={"schema_version": 1, "space_id": resource.space_id, "resource_id": rid,
            "new_version_id": new_id, "base_version_id": base.id, "proposal_id": ctx.id,
            "base_snapshot": copy.deepcopy(data["base_snapshot"]), "original_provenance": original_provenance,
            "source_changes": copy.deepcopy(data.get("source_changes", [])), "requires_edit": True,
            "compiled_revision_digest": svc.digest(compiled) if compiled else None,
            "compiled_source_snapshots": copy.deepcopy(compiled["source_snapshots"]) if compiled else [],
            "created_at": svc.primitive(svc.now()), "formal_evidence_allowed": False}))
    ctx.db.flush()
    return {"action": "REVISION_DRAFT_CREATED", "resource_id": rid, "base_version_id": base.id,
            "new_version_id": new_id, "state": "DRAFT", "requires_edit": True, "original_preserved": True,
            "compiled_candidate_applied": bool(compiled)}


def _apply_consolidation(ctx, data):
    ids, target = data["resource_ids"], data["target_resource_id"]
    entries = _entries(ctx.db, ctx.user, data["space_id"], ids, edit=True)
    configs = {rid: _entry_config(r, _policy(ctx.db, ENTRY_PREFIX + rid)) for rid, (r, _) in entries.items()}
    if configs[target]["canonical_resource_id"] != target:
        svc.fail(409, "CANONICAL_TARGET_NOT_ROOT", "导航目标已变化，不能形成主条目循环")
    for rid, config in configs.items():
        _collision_check(ctx.db, ctx.user, data["space_id"], rid, config["canonical_key"], config["aliases"], joined=ids)
    for rid, (resource, _) in entries.items():
        if rid == target:
            continue
        configs[rid]["canonical_resource_id"] = target
        _save_entry(ctx, resource, configs[rid])
        svc.audit(ctx, "wiki.maintenance.navigation_mapped", resource,
                  {"target_resource_id": target, "proposal_id": ctx.id, "documents_deleted": False})
    return {"action": "CANONICAL_NAVIGATION_MAPPED", "canonical_resource_id": target,
            "mapped_resource_ids": [rid for rid in ids if rid != target], "original_preserved": True}


def review_proposal(ctx):
    policy = _proposal(ctx.db, ctx.user, ctx.id)
    data = copy.deepcopy(policy.config)
    _lock_space(ctx, data["space_id"])
    _lock_snapshot(ctx.db, data["base_snapshot"])
    _entries(ctx.db, ctx.user, data["space_id"], data["resource_ids"], edit=True)
    policy = _proposal(ctx.db, ctx.user, ctx.id)
    data = copy.deepcopy(policy.config)
    body = proposal_view(ctx.db, ctx.user, ctx.id)
    _match(ctx, body["etag"])
    if data["status"] != "PROPOSED":
        svc.fail(409, "MAINTENANCE_ALREADY_REVIEWED", "建议已审阅，不能重复执行")
    comment = _reason(ctx.data.get("comment"))
    decision = ctx.data.get("decision")
    if decision not in {"ACCEPT", "REJECT"}:
        svc.fail(422, "INVALID_MAINTENANCE_DECISION", "请选择接受或拒绝")
    if decision == "ACCEPT":
        if not body["snapshot_current"]:
            svc.fail(409, "MAINTENANCE_SNAPSHOT_CHANGED", "输入或来源已变化，请重新扫描或提出新的维护建议")
        if body["application_blockers"]:
            svc.fail(409, "COMPILED_REVISION_SOURCE_NOT_ADMITTED",
                     "候选稿包含未发布且不在原冻结来源中的新来源；请先完成来源治理再重提，不会覆盖旧稿或悄悄丢弃候选")
        result = (_apply_revision(ctx, data) if data["kind"] == "REVISION" else
                  _apply_consolidation(ctx, data) if data["kind"] == "CONSOLIDATION" else
                  {"action": "CONFLICT_RECORDED", "formal_evidence_allowed": False})
        data.update(status="ACCEPTED", result=result)
        if data["kind"] == "CONFLICT":
            data["conflict_state"] = "OPEN"
    else:
        data["status"] = "REJECTED"
    data["review"] = {"decision": decision, "comment": comment, "reviewed_by": ctx.user.id,
                      "reviewed_at": svc.primitive(svc.now()), "snapshot_digest": body["snapshot_digest"],
                      "business_verification": "NOT_EVALUATED"}
    data["applied_snapshot"] = _current_proposal_snapshot(ctx.db, ctx.user, data)
    svc.bump(ctx.db, policy, config=data, updated_by=ctx.user.id)
    svc.audit(ctx, "wiki.maintenance.reviewed", policy, {"kind": data["kind"], "decision": decision,
        "snapshot_digest": body["snapshot_digest"], "formal_evidence_allowed": False})
    body = proposal_view(ctx.db, ctx.user, policy.id)
    return svc.Result(body, headers={"ETag": body["etag"]})


def resolve_conflict(ctx):
    policy = _proposal(ctx.db, ctx.user, ctx.id)
    data = copy.deepcopy(policy.config)
    _lock_space(ctx, data["space_id"])
    _lock_snapshot(ctx.db, data["base_snapshot"])
    _entries(ctx.db, ctx.user, data["space_id"], data["resource_ids"], edit=True)
    policy = _proposal(ctx.db, ctx.user, ctx.id)
    data = copy.deepcopy(policy.config)
    body = proposal_view(ctx.db, ctx.user, ctx.id)
    _match(ctx, body["etag"])
    if data["kind"] != "CONFLICT" or data["status"] != "ACCEPTED" or data.get("conflict_state") != "OPEN":
        svc.fail(409, "CONFLICT_NOT_OPEN", "仅已接受且未关闭的冲突可登记处理说明")
    data["conflict_state"] = "RESOLVED"
    data["applied_snapshot"] = _current_proposal_snapshot(ctx.db, ctx.user, data)
    data["resolution"] = {"comment": _reason(ctx.data.get("comment")), "resolved_by": ctx.user.id,
        "resolved_at": svc.primitive(svc.now()), "snapshot_digest": svc.digest(data["applied_snapshot"]),
        "business_verification": "NOT_EVALUATED"}
    svc.bump(ctx.db, policy, config=data, updated_by=ctx.user.id)
    svc.audit(ctx, "wiki.maintenance.conflict_resolved", policy,
              {"snapshot_digest": data["resolution"]["snapshot_digest"], "formal_evidence_allowed": False})
    body = proposal_view(ctx.db, ctx.user, policy.id)
    return svc.Result(body, headers={"ETag": body["etag"]})


def _source_changes(db, user, space_id, resource_id):
    snapshot = _snapshot(db, user, space_id, [resource_id])
    changes = []
    for old in snapshot["versions"]:
        resource = db.get(m.Resource, old["resource_id"])
        if resource.kind != "document":
            continue
        current = _live_version(db, user, resource)
        if current.version_no <= old["version_no"]:
            continue
        changes.append({"source_resource_id": resource.id, "title": current.title,
            "old_version_id": old["version_id"], "old_version_no": old["version_no"],
            "new_version_id": current.id, "new_version_no": current.version_no,
            "new_state": current.state, "change_type": "NEW_SOURCE_VERSION", "review_required": True})
    return sorted(changes, key=lambda x: (x["source_resource_id"], x["old_version_id"]))


def _scan_scope_fingerprint(db, user, space_id, entries):
    # An empty previous result is also invalidated by new aliases or a newly
    # published source version; resource/version IDs alone would miss both.
    return svc.digest(_snapshot(db, user, space_id, sorted(entries)))


def _title_tokens(title):
    # Preserve words/numbers, normalize width/case and disregard presentation
    # punctuation. Similarity is a candidate signal, never semantic equality.
    return "".join(c for c in _normal(title) if not c.isspace() and not unicodedata.category(c).startswith("P"))


def _duplicate_candidates(db, entries, metadata):
    from . import wiki

    by_name, by_body, groups, signals = {}, {}, {}, {}
    for rid, (r, v) in entries.items():
        item = metadata.get(rid)
        if not item or not item["is_canonical"]:
            continue
        for label in [r.name, v.title, item["canonical_key"], *item["aliases"]]:
            if label:
                by_name.setdefault(_normal(label), set()).add(rid)
        blocks = wiki._blocks(db, v)
        if blocks is None:
            svc.fail(409, "MAINTENANCE_SOURCE_CHANGED", "扫描输入正文完整性校验失败，请核对原知识条目")
        # Complete ordered body, all blocks and full data, no excerpt/first-N
        # digest. Citations and applicability are deliberately NOT called equal.
        if blocks and any(b.search_text.strip() for b in blocks):
            digest = svc.digest([[b.block_type, b.data] for b in blocks])
            by_body.setdefault(digest, set()).add(rid)
        groups.setdefault((_normal(r.category), v.knowledge_type), []).append(rid)

    def add(pair, method, **details):
        if metadata[pair[0]]["canonical_resource_id"] == metadata[pair[1]]["canonical_resource_id"]:
            return
        signal = signals.setdefault(pair, {"methods": [], "title_similarity": None,
            "title_similarity_threshold": TITLE_SIMILARITY_THRESHOLD, "body_sha256": None})
        signal["methods"].append(method)
        signal.update(details)

    for ids in by_name.values():
        for pair in itertools.combinations(sorted(ids), 2):
            if "NORMALIZED_NAME_OVERLAP" not in signals.get(pair, {}).get("methods", []):
                add(pair, "NORMALIZED_NAME_OVERLAP")
    for digest, ids in by_body.items():
        for pair in itertools.combinations(sorted(ids), 2):
            add(pair, "EXACT_COMPLETE_BODY", body_sha256=digest)
    for group in groups.values():
        # Every pair in each compatible metadata group, not a top-K shortlist.
        for pair in itertools.combinations(sorted(group), 2):
            left, right = (_title_tokens(entries[rid][1].title) for rid in pair)
            similarity = SequenceMatcher(None, left, right, autojunk=False).ratio()
            if left and right and similarity >= TITLE_SIMILARITY_THRESHOLD:
                add(pair, "TITLE_SIMILARITY_SAME_CATEGORY_TYPE", title_similarity=round(similarity, 6))
    return signals


def scan(ctx):
    data = ctx.data
    space_id = data["space_id"]
    _lock_space(ctx, space_id)
    entries = _entries(ctx.db, ctx.user, space_id, data.get("resource_ids"), edit=True)
    kinds = data.get("kinds", ["duplicates", "source_changes"])
    if not isinstance(kinds, list) or not kinds or set(kinds) - {"duplicates", "source_changes"}:
        svc.fail(422, "INVALID_SCAN_KIND", "扫描仅支持重复候选和来源版本变化")
    metadata = catalog_metadata(ctx.db, ctx.user, space_id, entries)
    proposals, created = {}, 0
    if "duplicates" in kinds:
        candidates = _duplicate_candidates(ctx.db, entries, metadata)
        for (left, right), detection in sorted(candidates.items()):
            candidate = {"space_id": space_id, "kind": "CONSOLIDATION", "resource_ids": [left, right],
                "target_resource_id": left, "reason": "标题或别名相交、同分类同类型标题近似，或完整正文相同；仅为重复候选，请核对适用范围、日期、引用和例外后决定是否仅归并导航。"}
            policy, is_new = _new_proposal(ctx, candidate, origin="SCAN", scan_key="duplicate-candidates-v2", detection=detection)
            proposals[policy.id] = policy
            created += int(is_new)
    if "source_changes" in kinds:
        for rid in sorted(entries):
            changes = _source_changes(ctx.db, ctx.user, space_id, rid)
            if not changes:
                continue
            candidate = {"space_id": space_id, "kind": "REVISION", "resource_ids": [rid],
                "reason": "已登记的精确来源已有更新版本。请核对新旧来源对本知识条目的影响；接受后仅创建保留原引用的修订工作稿，不自动替换条款。"}
            policy, is_new = _new_proposal(ctx, candidate, origin="SCAN", source_changes=changes, scan_key="source-version-v1")
            proposals[policy.id] = policy
            created += int(is_new)
    receipt = m.RuntimePolicy(id=svc.uid(), name=SCAN_PREFIX + svc.uid(), updated_by=ctx.user.id,
        config={"schema_version": 1, "space_id": space_id, "resource_ids": data.get("resource_ids"),
                "kinds": kinds, "scope_fingerprint": _scan_scope_fingerprint(ctx.db, ctx.user, space_id, entries),
                "proposal_ids": sorted(proposals), "owner_id": ctx.user.id})
    ctx.db.add(receipt)
    ctx.db.flush()
    items = [proposal_view(ctx.db, ctx.user, pid) for pid in sorted(proposals)]
    svc.audit(ctx, "wiki.maintenance.scanned", receipt, {"visible_resource_count": len(entries),
        "proposal_count": len(items), "created_count": created, "model_invoked": False})
    return svc.Result({"id": receipt.id, "space_id": space_id, "resource_count": len(entries),
        "created_count": created, "existing_count": len(items) - created, "items": items,
        "truncated": False, "model_invoked": False, "verification_status": "HEURISTIC_SUGGESTION", "notice": NOTICE})


def replay_authority(ctx, cached):
    """Idempotency returns no stale decision, revoked metadata or old body."""
    body = cached.get("body") or {}
    if ctx.operation == "saveWikiEntryMaintenance":
        _entry(ctx.db, ctx.user, ctx.id, edit=True)
        current = entry_metadata(ctx.db, ctx.user, ctx.id)
    elif ctx.operation == "scanWikiMaintenance":
        receipt = ctx.db.get(m.RuntimePolicy, body.get("id"))
        if not receipt or not receipt.name.startswith(SCAN_PREFIX) or receipt.config.get("owner_id") != ctx.user.id:
            _not_found()
        config = receipt.config
        entries = _entries(ctx.db, ctx.user, config["space_id"], config.get("resource_ids"), edit=True)
        if _scan_scope_fingerprint(ctx.db, ctx.user, config["space_id"], entries) != config["scope_fingerprint"]:
            svc.fail(409, "MAINTENANCE_REPLAY_CHANGED", "当前授权扫描范围已变化，请发起新的扫描")
        current = {**body, "items": [proposal_view(ctx.db, ctx.user, pid) for pid in config["proposal_ids"]]}
    else:
        policy = _proposal(ctx.db, ctx.user, body.get("id"))
        _entries(ctx.db, ctx.user, policy.config["space_id"], policy.config["resource_ids"], edit=True)
        current = proposal_view(ctx.db, ctx.user, policy.id)
    if svc.digest(current) != svc.digest(body):
        svc.fail(409, "MAINTENANCE_REPLAY_CHANGED", "维护建议、输入或当前权限已变化，不能重放旧结果")
    return True
