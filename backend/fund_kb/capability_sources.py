"""Exact published source reads for frozen capabilities, without latest-draft selection.

This does not change reference answers or formal eligibility. Source text is
returned only for explicitly bound IDs; recursive checks do not expand that set.
"""
from __future__ import annotations

from sqlalchemy import select

from . import models as m
from . import services as svc
from .ingestion import block_text, text_sha256


def _published_source(db, user, version_id, space_id, path=()):
    if version_id in path or len(path) >= 8:
        svc.fail(409, "DEPENDENCY_CYCLE", "来源引用依赖成环或超过八层")
    # Preserve the central version ACL and frozen Wiki provenance guards.
    version = svc.version_access(db, user, version_id, dependencies=False)
    resource = db.get(m.Resource, version.resource_id)
    if space_id is not None and resource.space_id != space_id:
        svc.fail(422, "CAPABILITY_SOURCE_SPACE_MISMATCH", "能力仅能绑定同一知识库中的来源")
    if resource.suspended or resource.kind not in {"document", "knowledge"}:
        svc.fail(409, "CAPABILITY_SOURCE_UNAVAILABLE", "绑定来源已停用或类型不可用")
    if version.state != "APPROVED" or not svc.is_released(db, version):
        svc.fail(409, "CAPABILITY_SOURCE_UNPUBLISHED", "绑定来源及其证据依赖须先完成原审核发布流程")
    blob = db.get(m.Blob, version.source_blob_id) if version.source_blob_id else None
    if (resource.kind == "document" and not blob) or (version.source_blob_id and not blob) \
            or (blob and blob.scan_state != "CLEAN"):
        svc.fail(409, "SOURCE_NOT_CLEAN", "来源原件尚未通过扫描")
    if db.scalar(select(m.Upload.id).where(m.Upload.version_id == version.id, m.Upload.state == "OPEN")) \
            or db.scalar(select(m.Job.id).where(m.Job.version_id == version.id,
                m.Job.state.in_(["QUEUED", "RUNNING"]))):
        svc.fail(409, "WORK_IN_PROGRESS", "绑定来源正在处理")
    actual = svc.check_frozen_hash(db, version)
    if not version.content_sha256 or version.content_sha256 != actual:
        svc.fail(409, "CAPABILITY_SOURCE_CHANGED", "绑定来源冻结内容已变化")
    blocks = svc.block_rows(db, version.id)
    if not blocks:
        svc.fail(409, "CAPABILITY_SOURCE_EMPTY", "绑定来源没有可用内容")
    if any(block.search_text != block_text({"block_type": block.block_type, "data": block.data})
            or block.content_sha256 != text_sha256(block.search_text) for block in blocks):
        svc.fail(409, "CAPABILITY_SOURCE_CHANGED", "绑定来源正文校验失败")

    dependencies = set(svc.dependency_ids(db, version))
    # All relationship targets retain ACL/space/suspension checks, including
    # non-evidence navigation links. Only real evidence dependencies recurse.
    for target_id in svc._dependency_structure(db, version)[1]:
        target = svc.resource_access(db, user, target_id)
        if target.space_id != resource.space_id:
            svc.fail(422, "CAPABILITY_SOURCE_SPACE_MISMATCH", "引用来源不在能力知识库内")
        if target.suspended:
            svc.fail(409, "CAPABILITY_SOURCE_UNAVAILABLE", "引用来源已停用")
    for edge in svc.relation_rows(db, version.id):
        if edge.relation_type != "DEPENDS_ON":
            continue
        pinned = db.get(m.ResourceVersion, edge.evidence_version_id) if edge.evidence_version_id else None
        if pinned and pinned.resource_id == edge.target_resource_id:
            dependencies.add(pinned.id)
            continue
        # Resource-only dependencies resolve through the published pointer. A
        # new draft must never shadow that version. Its identity is included in
        # the lineage signature, so a later release requires explicit rebinding.
        target = db.get(m.Resource, edge.target_resource_id)
        release = db.get(m.Release, target.active_release_id) if target.active_release_id else None
        if not release or release.state != "ACTIVE" or release.resource_id != target.id or not release.activated_at:
            svc.fail(409, "CAPABILITY_SOURCE_UNPUBLISHED", "依赖来源尚无当前已发布版本")
        dependencies.add(release.version_id)
    lineage = [(version.id, version.revision, resource.access_epoch, actual, blob.sha256 if blob else None)]
    for dependency_id in sorted(dependencies):
        _, _, _, child_lineage = _published_source(db, user, dependency_id, resource.space_id, (*path, version.id))
        lineage.extend(child_lineage)
    return version, resource, blocks, lineage


def reference_source(db, user, version_id, *, space_id=None, context=None):
    """Read one exact, published version and validate its complete source chain."""
    version, resource, blocks, lineage = _published_source(db, user, version_id, space_id)
    binding = {"version_id": version.id, "resource_id": resource.id, "space_id": resource.space_id,
        "revision": version.revision, "access_epoch": resource.access_epoch, "content_sha256": version.content_sha256,
        "title": version.title, "block_ids": [block.block_id for block in blocks],
        "reference_signature": svc.digest(sorted(set(lineage)))}
    match = svc.match_applicability(version.applicability or {}, context or {})
    records = [{"resource_id": resource.id, "version_id": version.id, "block_id": block.block_id,
        "title": version.title, "text": block.search_text, "content_sha256": block.content_sha256,
        "locator": block.locator or {}, "ordinal": block.ordinal, "legal_status": version.legal_status,
        "state": version.state, "source_verified": version.source_verified, "evidence_scope": "reference",
        "applicability": version.applicability or {}, "applicability_match": match,
        "required_facts": version.required_facts or [], "valid_from": svc.primitive(version.valid_from),
        "valid_to": svc.primitive(version.valid_to)} for block in blocks]
    return binding, records


def read_bound_sources(db, user, space_id, definition, bindings, inputs):
    ids = definition["source_version_ids"]
    if not ids:
        return []
    context_keys = {"business_date", "product_type", "fund_label", "share_class", "asset_type",
        "market", "business_event", "business_state"}
    context = {key: value for key, value in inputs.items() if key in context_keys}
    if definition["source_scope"] == "formal":
        from .capability_schema import validate_values
        validate_values([{"key": key, "type": "date" if key == "business_date" else "string", "required": False}
            for key in context_keys], context)
        records = svc.eligible_evidence(db, user, space_id, context, version_ids=set(ids))
    else:
        records = []
        for vid in ids:
            _, source_records = reference_source(db, user, vid, space_id=space_id, context=context)
            records.extend(source_records)
    expected = {(binding["version_id"], block_id) for binding in bindings for block_id in binding["block_ids"]}
    if {(row["version_id"], row["block_id"]) for row in records} != expected:
        svc.fail(409, "CAPABILITY_SOURCES_NOT_ADMITTED", "绑定来源当前不能按所选范围完整读取")
    # A CITES locator is navigable data only when both endpoints were admitted
    # in this exact read. Never disclose or fetch an unbound dependency here.
    by_pair = {(row["version_id"], row["block_id"]): row for row in records}
    for record in records:
        record["citations"] = []
    for start in range(0, len(ids), 400):
        links = db.scalars(select(m.EvidenceLink).where(m.EvidenceLink.from_version_id.in_(ids[start:start + 400]))
            .order_by(m.EvidenceLink.from_version_id, m.EvidenceLink.from_block_id,
                m.EvidenceLink.to_version_id, m.EvidenceLink.to_block_id, m.EvidenceLink.purpose))
        for link in links:
            origin = by_pair.get((link.from_version_id, link.from_block_id))
            target = by_pair.get((link.to_version_id, link.to_block_id))
            if origin is not None and target is not None:
                origin["citations"].append({key: target[key] for key in (
                    "resource_id", "version_id", "block_id", "locator", "content_sha256")}
                    | {"purpose": link.purpose})
    return records
