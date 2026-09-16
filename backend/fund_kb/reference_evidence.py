"""Explicit reference-only retrieval. It NEVER promotes material to formal evidence."""
from sqlalchemy import func, select

from . import models as m
from . import services as svc
from .ingestion import block_text, text_sha256


def _reference_lineage(db, user, version, path=(), memo=None, latest_versions=None, reading=False):
    if version.id in path or len(path) >= 8:
        svc.fail(409, "DEPENDENCY_CYCLE", "资料引用依赖成环或超过八层")
    if memo is not None and version.id in memo:
        result, height = memo[version.id]
        # Reusing a shared subtree must not hide a cycle or permit a deeper
        # ancestry than the uncached walk. These are request-local records only.
        if len(path) + height > 8 or any(row[0] in path for row in result):
            svc.fail(409, "DEPENDENCY_CYCLE", "资料引用依赖成环或超过八层")
        return result
    # Keep the central resource/version ACL and Wiki provenance guard. The
    # complete dependency walk below performs the same child-version and target
    # resource checks, plus scan/state/hash checks; do not walk it twice here.
    svc.version_access(db, user, version, dependencies=False)
    resource = db.get(m.Resource, version.resource_id)
    if resource.suspended or (not reading and version.state not in {"DRAFT", "IN_REVIEW", "APPROVED"}):
        svc.fail(409, "REFERENCE_NOT_AVAILABLE", "资料已停用或退回，不能用于辅助答疑")
    blob = db.get(m.Blob, version.source_blob_id) if version.source_blob_id else None
    if (resource.kind == "document" and not blob) or (blob and blob.scan_state != "CLEAN"):
        svc.fail(409, "SOURCE_NOT_CLEAN", "来源原件尚未通过扫描")
    if db.scalar(select(m.Upload.id).where(m.Upload.version_id == version.id, m.Upload.state == "OPEN")) \
        or db.scalar(select(m.Job.id).where(m.Job.version_id == version.id, m.Job.state.in_(["QUEUED", "RUNNING"]))):
        svc.fail(409, "WORK_IN_PROGRESS", "资料正在处理，尚不可用于答疑")
    current_hash = svc.check_frozen_hash(db, version)
    if version.content_sha256 and version.content_sha256 != current_hash:
        svc.fail(409, "SOURCE_CHANGED", "资料快照不一致")
    result = [(version.id, version.revision, resource.access_epoch, current_hash,
               blob.sha256 if blob else None)]
    dependencies = set(svc.dependency_ids(db, version))
    dependency_targets = set(db.scalars(select(m.RelationEdge.target_resource_id).where(
        m.RelationEdge.source_version_id == version.id, m.RelationEdge.relation_type == "DEPENDS_ON")))
    for target_id in svc._dependency_structure(db, version)[1]:
        target = svc.resource_access(db, user, target_id)
        if target.suspended:
            svc.fail(409, "REFERENCE_NOT_AVAILABLE", "引用资料已停用")
        if target_id not in dependency_targets:
            continue  # Ordinary Wiki backlinks may be cyclic; they are not evidence dependencies.
        latest = (latest_versions or {}).get(target.id)
        if latest is None:
            latest = db.scalar(select(m.ResourceVersion).where(m.ResourceVersion.resource_id == target.id)
                .order_by(m.ResourceVersion.version_no.desc()).limit(1))
        if not latest:
            svc.fail(409, "REFERENCE_NOT_AVAILABLE", "依赖资料尚无可核对版本")
        dependencies.add(latest.id)
    height = 1
    for dependency_id in sorted(dependencies):
        dependency = db.get(m.ResourceVersion, dependency_id)
        if not dependency:
            svc.fail(404, "NOT_FOUND", "引用资料不可访问")
        result.extend(_reference_lineage(db, user, dependency, (*path, version.id), memo, latest_versions, reading))
        if memo is not None:
            height = max(height, 1 + memo[dependency.id][1])
    if memo is not None:
        memo[version.id] = (result, height)
    return result


def _latest_versions(db, resources):
    """Batch only snapshot selection, not permission decisions or source bodies.

    The dictionary lives for one call. Every selected version still passes the
    central ACL, provenance and full reference-lineage checks before emission.
    GROUP BY + MAX keeps the query portable to Oracle and OceanBase.
    """
    result = {}
    ids = [resource.id for resource in resources]
    for start in range(0, len(ids), 400):
        latest = (select(m.ResourceVersion.resource_id,
                         func.max(m.ResourceVersion.version_no).label("latest_no"))
            .where(m.ResourceVersion.resource_id.in_(ids[start:start + 400]))
            .group_by(m.ResourceVersion.resource_id).subquery())
        statement = select(m.ResourceVersion).join(latest,
            (m.ResourceVersion.resource_id == latest.c.resource_id)
            & (m.ResourceVersion.version_no == latest.c.latest_no))
        for version in db.scalars(statement):
            result[version.resource_id] = version
    return result


def reference_evidence(db, user, space_id, context=None, *, version_ids=None, reading=False):
    """Only caller-readable, non-rejected latest snapshots; keep unverified flags intact.

    Call separately before sending model context and delivering results. A bounded
    version_ids set makes those rechecks independent of the size of the library.
    No vector index, published-status mutation, credential or external request.
    """
    if isinstance(user, str):
        user = db.get(m.User, user)
    svc.space_access(db, user, space_id)
    context = context or {}
    query = select(m.Resource).where(m.Resource.space_id == space_id,
        m.Resource.deleted_at.is_(None), m.Resource.suspended.is_(False),
        m.Resource.kind.in_(["document", "knowledge"]))
    if version_ids is not None:
        if not version_ids:
            return []
        query = query.where(m.Resource.id.in_(select(m.ResourceVersion.resource_id)
            .where(m.ResourceVersion.id.in_(version_ids))))
    resources = list(db.scalars(query))
    latest_versions = _latest_versions(db, resources)
    result, memo = [], {}
    for resource in resources:
        try:
            from .wiki import _policy
            provenance = _policy(db, f"wiki-provenance:{resource.id}") if resource.kind == "knowledge" else None
            if not reading and provenance and provenance.config.get("imported_local_note"):
                continue  # Bibliographic links alone do not establish paragraph-level source support.
            version = latest_versions.get(resource.id)
            if not version or (version_ids is not None and version.id not in version_ids):
                continue
            # _reference_lineage calls version_access, which checks resource
            # access (including private-draft and Wiki source guards). Calling
            # resource_access separately here repeated the same expensive walk.
            lineage = _reference_lineage(db, user, version, memo=memo, latest_versions=latest_versions, reading=reading)
            applicability_match = svc.match_applicability(version.applicability or {}, context)
            if not reading and applicability_match is False:
                continue
            signature = svc.digest(sorted(set(lineage)))
            blocks = list(db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == version.id)
                .order_by(m.ContentBlock.ordinal)))
            if any(b.content_sha256 != text_sha256(b.search_text) or b.search_text != block_text(
                    {"block_type": b.block_type, "data": b.data}) for b in blocks):
                continue
            steps = [b.ordinal for b in blocks if b.block_type == "step"]
            for block in blocks:
                result.append({"resource_id": resource.id, "version_id": version.id,
                    "block_id": block.block_id, "title": version.title, "text": block.search_text,
                    "content_sha256": block.content_sha256, "locator": block.locator or {},
                    "ordinal": block.ordinal, "version_step_ordinals": steps,
                    "block_type": block.block_type, "data": block.data,
                    "kind": resource.kind, "knowledge_type": version.knowledge_type,
                    "source_verified": version.source_verified, "legal_status": version.legal_status,
                    "state": version.state, "applicability": version.applicability or {},
                    "required_facts": version.required_facts or [],
                    "valid_from": svc.primitive(version.valid_from), "valid_to": svc.primitive(version.valid_to),
                    "resource_access_epoch": resource.access_epoch, "evidence_scope": "reference",
                    **({"reading_only": True, "applicability_match": applicability_match,
                        "imported_local_note": bool(provenance and provenance.config.get("imported_local_note"))} if reading else {}),
                    "reference_signature": signature})
        except svc.APIError:
            # Inaccessible/invalid material must not expose even its title or count.
            continue
    pairs = {(row["version_id"], row["block_id"]) for row in result}
    versions = sorted({row["version_id"] for row in result})
    citations = {}
    for start in range(0, len(versions), 400):
        for fv, fb, tv, tb in db.execute(select(m.EvidenceLink.from_version_id, m.EvidenceLink.from_block_id,
                m.EvidenceLink.to_version_id, m.EvidenceLink.to_block_id)
                .where(m.EvidenceLink.from_version_id.in_(versions[start:start + 400]))):
            if (fv, fb) in pairs and (tv, tb) in pairs:
                citations.setdefault((fv, fb), []).append({"version_id": tv, "block_id": tb})
    for row in result:
        row["source_citations"] = citations.get((row["version_id"], row["block_id"]), [])
    return result


def evidence_signature(record):
    return (record["version_id"], record["block_id"], record["content_sha256"], record.get("reference_signature"))


def rank_reference_evidence(question, candidates, limit=12):
    """Bound lexical tokenization and model context without altering any source block."""
    from .retrieval import rank_evidence, tokenize
    terms = set(tokenize(question))
    scored = []
    for candidate in candidates:
        text = candidate["text"].lower()
        title = candidate["title"].lower()
        score = sum((2 if term in title else 0) + (1 if term in text else 0) for term in terms)
        if score > 0 and len(text.encode()) <= 8000:
            scored.append((score, candidate))
    ordered = sorted(scored, key=lambda pair: (-pair[0], pair[1]["version_id"], pair[1]["block_id"]))
    # Wiki is the primary reading map. A large source corpus must not crowd all
    # synthesized knowledge out of the bounded lexical candidate pool.
    wiki = rank_evidence(question, [row for _, row in ordered if row["kind"] == "knowledge"][:300], None, limit=50)
    sources = rank_evidence(question, [row for _, row in ordered if row["kind"] == "document"][:300], None, limit=50)
    ranked = [row for pair in zip(wiki, sources) for row in pair]
    ranked.extend(wiki[min(len(wiki), len(sources)):])
    ranked.extend(sources[min(len(wiki), len(sources)):])
    result, per_version, used_bytes = [], {}, 0
    for row in ranked:
        count = per_version.get(row["version_id"], 0)
        size = len(row["text"].encode())
        if count >= 3 or used_bytes + size > 12000:
            continue
        result.append(row)
        per_version[row["version_id"]] = count + 1
        used_bytes += size
        if len(result) >= limit:
            break
    return result
