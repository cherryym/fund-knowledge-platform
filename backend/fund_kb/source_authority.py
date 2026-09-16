"""Audited, date-aware replacement facts; never mutate frozen source evidence.

No question keywords, model calls, implicit legality promotion, or cross-request
authorization cache. Catalog annotations do not alter vector receipt identities.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import select

from . import models as m
from . import services as svc

PREFIX = "source-authority:"
VERSION_KEYS = ("predecessor_version_id", "successor_version_id", "evidence_version_id")
NOTE = "仅核对来源间的替代事实；不等于整份文件现行有效或业务答案已通过专家审核。"


def _rows(db, space_id):
    return list(db.scalars(select(m.RuntimePolicy).where(
        m.RuntimePolicy.name.startswith(f"{PREFIX}{space_id}:")).order_by(m.RuntimePolicy.id)))


def _header(db, version):
    resource = db.get(m.Resource, version.resource_id)
    blob = db.get(m.Blob, version.source_blob_id) if version.source_blob_id else None
    return {"resource_id": resource.id, "version_id": version.id, "revision": version.revision,
        "access_epoch": resource.access_epoch, "state": version.state,
        "stored_content_sha256": version.content_sha256, "source_blob_id": version.source_blob_id,
        "source_blob_sha256": blob.sha256 if blob else None,
        "scan_state": blob.scan_state if blob else None}


def _capture(db, user, version_id, space_id):
    version = svc.version_access(db, user, version_id)
    resource = db.get(m.Resource, version.resource_id)
    if resource.space_id != space_id or resource.kind != "document" or resource.suspended:
        svc.fail(409, "SOURCE_AUTHORITY_DOCUMENT_REQUIRED", "请选择本空间可用的来源文档版本")
    from .reference_evidence import reference_evidence
    records = reference_evidence(db, user, space_id, {}, reading=True, version_ids={version.id})
    if not records:
        svc.fail(409, "SOURCE_AUTHORITY_EVIDENCE_UNAVAILABLE", "版本没有通过原件、正文与来源完整性检查")
    return {**_header(db, version), "content_sha256": svc.check_frozen_hash(db, version)}


def _visible(db, user, config):
    versions = []
    for key in VERSION_KEYS:
        version = svc.version_access(db, user, config[key])
        resource = db.get(m.Resource, version.resource_id)
        if resource.space_id != config["space_id"] or resource.suspended:
            svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
        versions.append(version)
    return versions


def _validation(db, config, versions):
    snapshots = config.get("snapshots", {})
    for version in versions:
        snap = snapshots.get(version.id, {})
        if not snap or any(snap.get(key) != value for key, value in _header(db, version).items()):
            return "STALE", "绑定的版本、发布状态或权限已变化，需要重新核对并登记，不能自动套用新版本。"
        if snap.get("scan_state") != "CLEAN":
            return "UNAVAILABLE", "绑定原件不可用。"
    return "VALID", NOTE


def _present(db, user, row):
    config = row.config
    versions = _visible(db, user, config)
    state, note = _validation(db, config, versions)
    public = {key: value for key, value in config.items() if key != "snapshots"}
    public.update(id=row.id, revision=row.revision, validation_state=state, validation_note=note)
    for name, version in zip(("predecessor_title", "successor_title", "evidence_title"), versions):
        public[name] = version.title
    return public


def list_records(db, user, space_id):
    svc.space_access(db, user, space_id)
    items = []
    for row in _rows(db, space_id):
        try:
            items.append(_present(db, user, row))
        except (svc.APIError, KeyError, TypeError):
            continue  # Hidden endpoint identities must not leak through a relation.
    return {"items": items, "can_manage": "admin" in svc.roles(db, user, space_id), "notes": [NOTE]}


def get_record(db, user, record_id, *, manage=False):
    row = db.get(m.RuntimePolicy, record_id)
    if not row or not row.name.startswith(PREFIX):
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    svc.space_access(db, user, row.config["space_id"], "admin" if manage else None)
    return row, _present(db, user, row)


def create_record(ctx):
    data, db = ctx.data, ctx.db
    space_id = data["space_id"]
    svc.space_access(db, ctx.user, space_id, "admin")
    db.scalar(select(m.Space).where(m.Space.id == space_id).with_for_update())
    old, new = data["predecessor_version_id"], data["successor_version_id"]
    if old == new:
        svc.fail(422, "SOURCE_AUTHORITY_SELF_REPLACEMENT", "新旧版本不能相同")
    # Validate an ISO calendar date even for direct service callers.
    date.fromisoformat(data["effective_from"])
    if data["scope"] not in {"full", "partial"} or not data["reason"].strip():
        svc.fail(422, "SOURCE_AUTHORITY_INPUT_INVALID", "请明确替代范围及核对理由")
    for vid in sorted({data[key] for key in VERSION_KEYS}):
        db.scalar(select(m.ResourceVersion).where(m.ResourceVersion.id == vid).with_for_update())
    snapshots = {vid: _capture(db, ctx.user, vid, space_id) for vid in {data[key] for key in VERSION_KEYS}}
    if data.get("expected_sources") is not None:
        keys = ("version_id", "revision", "access_epoch", "content_sha256")
        expected = {item["version_id"]: item for item in data["expected_sources"]}
        actual = {vid: {key: snap[key] for key in keys} for vid, snap in snapshots.items()}
        if len(expected) != len(data["expected_sources"]) or expected != actual:
            svc.fail(409, "SOURCE_AUTHORITY_PREFILL_STALE", "预填后来源内容、修订或权限已变化，请刷新建议再确认")
    edges = defaultdict(set)
    for row in _rows(db, space_id):
        c = row.config
        if c.get("state") != "ACTIVE":
            continue
        a, b = c["predecessor_version_id"], c["successor_version_id"]
        edges[a].add(b)
        if a == old and b == new and c["scope"] == data["scope"] and c["effective_from"] == data["effective_from"]:
            svc.fail(409, "SOURCE_AUTHORITY_DUPLICATE", "该替代事实已登记，请勿重复创建")
        if a == old and b != new and c["scope"] == data["scope"] == "full":
            svc.fail(409, "SOURCE_AUTHORITY_CONFLICT", "已有不同的完整替代后继，请先核对或撤销冲突记录")
    pending, visited = [new], set()
    while pending:
        current = pending.pop()
        if current == old:
            svc.fail(409, "SOURCE_AUTHORITY_CYCLE", "替代关系不能成环")
        if current not in visited:
            visited.add(current)
            pending.extend(edges[current])
    record_id = svc.uid()
    config = {**{key: value for key, value in data.items() if key != "expected_sources"},
        "schema_version": 1, "state": "ACTIVE", "snapshots": snapshots,
        "created_at": svc.primitive(svc.now()), "created_by": ctx.user.id,
        "revoked_at": None, "revoke_reason": None}
    row = m.RuntimePolicy(id=record_id, name=f"{PREFIX}{space_id}:{record_id}",
        updated_by=ctx.user.id, config=config)
    db.add(row)
    db.flush()
    svc.audit(ctx, "source_authority.confirmed", row, {"fact": {k: v for k, v in config.items() if k != "snapshots"},
        "snapshot_digest": svc.digest(snapshots), "professional_certification": False})
    return _present(db, ctx.user, row)


def revoke_record(ctx):
    row, _ = get_record(ctx.db, ctx.user, ctx.id, manage=True)
    ctx.db.scalar(select(m.Space).where(m.Space.id == row.config["space_id"]).with_for_update())
    ctx.db.refresh(row)
    svc.require_etag(ctx, row)
    if not ctx.data["reason"].strip():
        svc.fail(422, "SOURCE_AUTHORITY_INPUT_INVALID", "撤销必须填写原因")
    row.config = {**row.config, "state": "REVOKED", "revoked_at": svc.primitive(svc.now()),
        "revoke_reason": ctx.data["reason"], "revoked_by": ctx.user.id}
    row.revision += 1
    row.updated_by = ctx.user.id
    svc.audit(ctx, "source_authority.revoked", row, {"reason": ctx.data["reason"], "documents_deleted": False})
    return _present(ctx.db, ctx.user, row)


def authority_stamp(db, space_id):
    """Fresh governance + endpoint metadata fence, separate from embedding stamps."""
    rows = _rows(db, space_id)
    if not rows:
        return None
    ids = {row.config.get(key) for row in rows for key in VERSION_KEYS}
    headers = []
    for vid in sorted(ids - {None}):
        version = db.get(m.ResourceVersion, vid)
        resource = db.get(m.Resource, version.resource_id) if version else None
        headers.append([vid, _header(db, version) if resource else None,
            svc.primitive(resource.deleted_at) if resource else None, resource.suspended if resource else None])
    return svc.digest([[row.id, row.revision, row.config] for row in rows] + headers)


def _active_at(config, context):
    if config.get("state") != "ACTIVE" or date.fromisoformat(config["effective_from"]) > svc.effective_date(context):
        return False
    cutoff = context.get("knowledge_cutoff")
    if cutoff:
        cutoff = datetime.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
        created = datetime.fromisoformat(config["created_at"])
        if created > svc.aware(cutoff):
            return False
    return True


def formal_excluded(db, version, context):
    """A known replacement/partial amendment can only tighten formal admission.

Unavailable proof does not resurrect old law. Reference reading remains possible.
    """
    resource = db.get(m.Resource, version.resource_id)
    from .projection_read import memo
    rows = memo(db, "source-authority-rows", resource.space_id, lambda: _rows(db, resource.space_id))
    return any(row.config.get("predecessor_version_id") == version.id and _active_at(row.config, context)
               for row in rows)


def annotate_catalog(db, user, space_id, context, pages):
    """Metadata-only annotation, preserving every page and original graph edge."""
    by_version = {page["version_id"]: page for page in pages.values()}
    for row in _rows(db, space_id):
        c = row.config
        if not _active_at(c, context):
            continue
        old = by_version.get(c["predecessor_version_id"])
        if not old:
            continue
        item = {"fact_id": row.id, "scope": c["scope"], "effective_from": c["effective_from"]}
        try:
            versions = _visible(db, user, c)
            state, _ = _validation(db, c, versions)
            new, proof = (by_version.get(c[key]) for key in VERSION_KEYS[1:])
            successor = versions[1]
            if state != "VALID" or not new or not proof:
                item["validation_state"] = "STALE" if state == "STALE" else "UNAVAILABLE"
            elif ((successor.valid_from and successor.valid_from > svc.effective_date(context))
                  or (successor.valid_to and successor.valid_to <= svc.effective_date(context))
                  or (successor.legal_status == "FUTURE" and not successor.valid_from)
                  or (successor.legal_status == "REPEALED" and not successor.valid_to)
                  or svc.match_applicability(successor.applicability or {}, context) is False):
                item["validation_state"] = "CONFLICT"
            else:
                item.update(validation_state="VALID", successor_page_id=new["id"], evidence_page_id=proof["id"])
        except (svc.APIError, KeyError, TypeError):
            item["validation_state"] = "UNAVAILABLE"
        old.setdefault("source_authority", []).append(item)
    for page in pages.values():
        full = [fact for fact in page.get("source_authority", []) if fact["scope"] == "full"]
        if len({fact.get("successor_page_id") for fact in full if fact["validation_state"] == "VALID"}) > 1:
            for fact in full:
                fact["validation_state"] = "CONFLICT"
    return pages


def describe(page):
    facts = page.get("source_authority", [])
    if not facts:
        return ""
    lines = []
    for fact in facts:
        mode = "整体替代：原文仅作历史对照，不能作为当日现行主规则" if fact["scope"] == "full" else "部分替代：须逐条核对适用范围，不能整份沿用或排除"
        detail = (f"后继 {fact['successor_page_id']}；替代证据 {fact['evidence_page_id']}"
            if fact["validation_state"] == "VALID" else "治理依据待重新核对或当前不可访问；不得静默回退旧规则为现行")
        lines.append(f"来源治理（{fact['effective_from']}起）：{mode}；{detail}。不等于后继全文已核验。")
    return "\n".join(lines)


def extend_reads(pages, requested, anchors=None):
    """Follow selected sources (including Wiki CITES) then audited replacements.

No limit on chain length. Visited sets terminate cycles without hiding a gap.
Unlocated replacement/proof documents are read fully, not title-only evidence.
    """
    requested = list(dict.fromkeys(pid for pid in requested if pid in pages))
    pending, visited, full, requirements, warnings = list(requested), set(), set(), {}, []
    while pending:
        pid = pending.pop()
        if pid in visited or pid not in pages:
            continue
        visited.add(pid)
        page = pages[pid]
        for edge in page.get("relations", []):
            if edge.get("source") == pid and edge.get("type") == "CITES" and edge.get("origin") in {"registered", "provenance"}:
                pending.append(edge.get("target"))
        for fact in page.get("source_authority", []):
            requirements[fact["fact_id"]] = {**fact, "predecessor_page_id": pid}
            if fact["validation_state"] != "VALID":
                warnings.append("SOURCE_AUTHORITY_REVIEW_REQUIRED")
                continue
            for target in (fact["successor_page_id"], fact["evidence_page_id"]):
                if target not in requested:
                    requested.append(target)
                pending.append(target)
                if target == fact["evidence_page_id"] or not (anchors or {}).get(target):
                    full.add(target)
    return {"requested": requested, "full_pages": sorted(full), "requirements": list(requirements.values()),
        "warnings": sorted(set(warnings))}


def check_coverage(pages, requirements, records, answer):
    read = {row["version_id"] for row in records}
    cited = {row["version_id"] for row in answer.get("citations", [])}
    checks = []
    for fact in requirements:
        targets = [fact.get("successor_page_id"), fact.get("evidence_page_id")]
        versions = {pages[pid]["version_id"] for pid in targets if pid in pages}
        complete = fact["validation_state"] == "VALID" and len(versions) > 0 and versions <= read
        leaves, cycle = successor_leaves(pages, fact.get("successor_page_id"))
        complete = complete and not cycle
        successor_cited = bool(leaves) and all(pages[pid]["version_id"] in cited for pid in leaves)
        checks.append({"fact_id": fact["fact_id"], "read_complete": complete, "successor_cited": successor_cited})
    if checks and any(not item["read_complete"] or not item["successor_cited"] for item in checks):
        answer.setdefault("quality_warnings", []).append({"code": "SOURCE_AUTHORITY_COVERAGE_GAP",
            "message": "涉及已登记的后续规则，但后继或替代证据未完整核对/引用。请核对业务日期及新旧适用范围，不应据此直接执行。"})
    return {"checks": checks, "covered": all(c["read_complete"] and c["successor_cited"] for c in checks),
        "professional_verification": False}


def successor_leaves(pages, start):
    """Iterative DFS handles arbitrary chain length and explicit cycle failure."""
    colors, leaves, cycle = {}, set(), False
    pending = [(start, False)]
    while pending:
        pid, exit_node = pending.pop()
        if pid not in pages:
            continue
        if exit_node:
            colors[pid] = 2
            continue
        if colors.get(pid) == 1:
            cycle = True
            continue
        if colors.get(pid) == 2:
            continue
        colors[pid] = 1
        pending.append((pid, True))
        facts = [f for f in pages[pid].get("source_authority", []) if f["scope"] == "full"]
        if not facts:
            leaves.add(pid)
        else:
            for fact in facts:
                if fact["validation_state"] != "VALID":
                    cycle = True  # Unresolved chain, not permission to resurrect its parent.
                else:
                    pending.append((fact["successor_page_id"], False))
    return leaves, cycle


def historical_warning(db, user, space_id, context, citations, previous_stamp):
    """Read-time risk annotation only; never rewrite frozen answers or E IDs."""
    if authority_stamp(db, space_id) == previous_stamp:
        return None
    cited = {item.get("version_id") for item in citations}
    for row in _rows(db, space_id):
        c = row.config
        if c.get("predecessor_version_id") in cited and _active_at(c, context):
            # Check the cited endpoint only. Hidden successors/proof stay unnamed.
            svc.version_access(db, user, c["predecessor_version_id"])
            return {"code": "SOURCE_AUTHORITY_UPDATED", "message":
                "此答复引用的资料存在新增或变更的替代事实，请按业务日期核对后续规则。历史正文和引用未改写，不代表已重新完成模型或专家复核。"}
    return None


def verify_bindings(db, user, requirements):
    """Full evidence validation only after selection, repeated before delivery.

Even a draft proof without a stored version hash is bound to its actual content
at confirmation. Revisions alone must not admit a changed proof body.
    """
    checked = set()
    for fact in requirements:
        if fact["validation_state"] != "VALID":
            continue
        row = db.get(m.RuntimePolicy, fact["fact_id"])
        if not row or row.config.get("state") != "ACTIVE":
            svc.fail(409, "SOURCE_AUTHORITY_CHANGED", "来源替代事实已变化")
        versions = _visible(db, user, row.config)
        if _validation(db, row.config, versions)[0] != "VALID":
            svc.fail(409, "SOURCE_AUTHORITY_CHANGED", "来源替代事实的绑定版本已变化")
        for version in versions:
            snap = row.config["snapshots"][version.id]
            identity = (version.id, snap["content_sha256"])
            if identity not in checked:
                if svc.check_frozen_hash(db, version) != snap["content_sha256"]:
                    svc.fail(409, "SOURCE_AUTHORITY_CHANGED", "替代事实绑定的正文已变化，需要重新核对")
                checked.add(identity)
