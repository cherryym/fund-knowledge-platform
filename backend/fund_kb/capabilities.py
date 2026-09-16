"""Versioned Agent capabilities inside existing governed template resources."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

from sqlalchemy import select
from starlette.datastructures import Headers

from . import api_content
from . import models as m
from . import services as svc
from .capability_schema import FORMAT, LABEL, validate_definition, validate_json
from .capability_sources import reference_source


def _child(ctx, data, identity=None):
    request = SimpleNamespace(app=ctx.request.app, headers=ctx.request.headers, state=ctx.request.state,
        path_params={"id": identity} if identity else {})
    return svc.Context(request, ctx.db, ctx.user, data, {}, ctx.operation, ctx.dispatch)


def _agent_space(ctx, space_id):
    from .agent_access import authorize_agent_space
    authorize_agent_space(ctx.request, space_id)


def _bindings(db, user, ids, *, space_id=None):
    result, citations = [], []
    for vid in ids:
        binding, records = reference_source(db, user, vid, space_id=space_id)
        result.append(binding)
        citations.extend({"version_id": vid, "block_id": record["block_id"], "purpose": "INTERNAL_OPINION"}
            for record in records)
    return result, citations


def manifest(db, user, version):
    blocks = svc.block_rows(db, version.id)
    definitions = [block for block in blocks if block.block_type == "paragraph" and (block.locator or {}).get("label") == LABEL]
    if len(definitions) != 1:
        svc.fail(409, "CAPABILITY_NOT_STRUCTURED", "该方案尚未沉淀为Agent能力定义")
    try:
        value = json.loads(definitions[0].data["text"])
    except (ValueError, KeyError, TypeError):
        svc.fail(409, "CAPABILITY_DEFINITION_INVALID", "能力定义无法解析")
    if not isinstance(value, dict) or set(value) != {"format", "definition", "source_bindings"} or value["format"] != FORMAT:
        svc.fail(409, "CAPABILITY_DEFINITION_INVALID", "能力格式不匹配")
    validate_json(value, status=409, code="CAPABILITY_DEFINITION_INVALID")
    validate_definition(value["definition"])
    from .ingestion import block_text, text_sha256
    if any(block.search_text != block_text(svc.block_dict(db, block)) or text_sha256(block.search_text) != block.content_sha256 for block in blocks):
        svc.fail(409, "CAPABILITY_CONTENT_CHANGED", "能力正文已变化")
    actual = svc.check_frozen_hash(db, version)
    if version.content_sha256 and version.content_sha256 != actual:
        svc.fail(409, "CAPABILITY_CONTENT_CHANGED", "能力冻结内容已变化")
    resource = db.get(m.Resource, version.resource_id)
    fresh, _ = _bindings(db, user, value["definition"]["source_version_ids"], space_id=resource.space_id)
    if fresh != value["source_bindings"]:
        svc.fail(409, "CAPABILITY_SOURCE_CHANGED", "能力绑定来源已变更，需创建修订并重新核对")
    return value, actual


def version_detail(ctx, version_id):
    version = svc.version_access(ctx.db, ctx.user, version_id)
    resource = svc.resource_access(ctx.db, ctx.user, version.resource_id)
    _agent_space(ctx, resource.space_id)
    if resource.kind != "template":
        svc.fail(404, "NOT_FOUND", "对象不是Agent能力")
    if resource.suspended:
        svc.fail(409, "CAPABILITY_UNAVAILABLE", "能力已停用")
    value, content_hash = manifest(ctx.db, ctx.user, version)
    rr = svc.roles(ctx.db, ctx.user, resource.space_id)
    published = version.state == "APPROVED" and svc.is_released(ctx.db, version)
    can_export = False
    if not getattr(ctx.request.state, "agent_access", None):
        try:
            svc.version_access(ctx.db, ctx.user, version, "download")
            can_export = True
        except svc.APIError as exc:
            if exc.status not in {403, 404}:
                raise
    return {"resource_id": resource.id, "space_id": resource.space_id, "version_id": version.id,
        "revision": version.revision, "version_no": version.version_no, "state": version.state, "name": version.title,
        "definition": value["definition"], "manifest_sha256": svc.digest(value), "content_sha256": content_hash,
        "source_bindings": value["source_bindings"], "permissions": {
            "can_edit": bool(not getattr(ctx.request.state, "agent_access", None) and version.state == "DRAFT" and svc.can_edit_draft(ctx.db, ctx.user, version)),
            "can_trial": bool(rr & {"editor", "admin"}), "can_run": bool(published), "can_export": can_export},
        "notes": ["能力指导不授予外部系统操作权限；Agent报告和人工核对分开记录。",
            "绑定资料的专业适用性仍须按本次业务日期核对。",
            *(["资料辅助范围可含UNKNOWN或未专业核验来源，不等同正式执行许可。"]
                if value["definition"]["source_scope"] == "reference" else [])]}


def get(ctx):
    resource = svc.resource_access(ctx.db, ctx.user, ctx.id)
    _agent_space(ctx, resource.space_id)
    if resource.kind != "template":
        svc.fail(404, "NOT_FOUND", "对象不是Agent能力")
    for version in ctx.db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.resource_id == resource.id)
            .order_by(m.ResourceVersion.version_no.desc())):
        try:
            return version_detail(ctx, version.id)
        except svc.APIError as exc:
            if exc.status != 404:
                raise
    svc.fail(404, "NOT_FOUND", "没有当前可读的能力版本")


def list_capabilities(ctx):
    space_id = ctx.query["space_id"]
    _agent_space(ctx, space_id)
    svc.space_access(ctx.db, ctx.user, space_id)
    items, legacy = [], []
    for resource in ctx.db.scalars(select(m.Resource).where(m.Resource.space_id == space_id,
            m.Resource.kind == "template", m.Resource.deleted_at.is_(None), m.Resource.suspended.is_(False))):
        try:
            detail = get(_child(ctx, {}, resource.id))
            items.append(detail)
        except svc.APIError as exc:
            if exc.code == "CAPABILITY_NOT_STRUCTURED":
                legacy.append({"resource_id": resource.id, "name": resource.name})
    return {"items": sorted(items, key=lambda item: (item["name"], item["resource_id"])), "legacy_items": legacy,
        "can_edit": "editor" in svc.roles(ctx.db, ctx.user, space_id) and not getattr(ctx.request.state, "agent_access", None),
        "notes": ["未结构化的旧方案保留在普通模板中，不自动改写。"]}


def _save(ctx, version, definition, *, existing=None):
    validate_definition(definition)
    resource = ctx.db.get(m.Resource, version.resource_id)
    bindings, citations = _bindings(ctx.db, ctx.user, definition["source_version_ids"], space_id=resource.space_id)
    if any(binding["space_id"] != resource.space_id for binding in bindings):
        svc.fail(422, "CAPABILITY_SOURCE_SPACE_MISMATCH", "能力仅能绑定同一知识库中的来源")
    if existing:
        previous = {item["version_id"]: item for item in existing["source_bindings"]}
        if any(item["version_id"] in previous and item != previous[item["version_id"]] for item in bindings):
            svc.fail(409, "CAPABILITY_SOURCE_CHANGED", "仍为相同来源ID但内容或权限已变化，请重新核对来源版本")
    value = {"format": FORMAT, "definition": copy.deepcopy(definition), "source_bindings": bindings}
    body = {"title": definition["name"], "knowledge_type": "solution_template", "applicability": {},
        "required_facts": [], "legal_status": "NOT_APPLICABLE", "valid_from": None, "valid_to": None,
        "blocks": [{"block_id": svc.uid(), "ordinal": 0, "block_type": "paragraph",
            "data": {"text": json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)}, "locator": {"label": LABEL}, "citations": citations}]}
    # Existing guarded draft write also validates source publication, block
    # integrity, dependency cycles and the full frozen-content review boundary.
    request = _child(ctx, body, version.id)
    request.request.headers = Headers(ctx.request.headers).mutablecopy()
    request.request.headers["if-match"] = f'"{version.revision}"'
    api_content.edit_version(request)
    svc.audit(ctx, "capability.definition_saved", version, {"manifest_sha256": svc.digest(value), "source_count": len(bindings)})
    return version_detail(ctx, version.id)


def create(ctx):
    from . import api_catalog
    definition = validate_definition(ctx.data["definition"])
    space_id = ctx.data["space_id"]
    _agent_space(ctx, space_id)
    svc.space_access(ctx.db, ctx.user, space_id, "editor")
    child = _child(ctx, {"space_id": space_id, "kind": "template", "name": definition["name"], "category": "Agent能力", "tags": ["agent-capability"]})
    child.operation = "createResource"
    resource = api_catalog.resources(child)
    child = _child(ctx, {"title": definition["name"], "change_reason": "沉淀为Agent能力草稿"}, resource.body["id"])
    child.operation = "createDraftVersion"
    created = api_content.versions(child)
    version = ctx.db.get(m.ResourceVersion, created.body["id"])
    return _save(ctx, version, definition)


def update(ctx):
    resource = svc.resource_access(ctx.db, ctx.user, ctx.id, "edit")
    _agent_space(ctx, resource.space_id)
    version = svc.version_access(ctx.db, ctx.user, ctx.data["version_id"], "edit")
    if version.resource_id != resource.id or resource.kind != "template" or version.state != "DRAFT":
        svc.fail(409, "CAPABILITY_DRAFT_REQUIRED", "请先创建该能力的可编辑修订")
    svc.require_etag(ctx, version)
    existing, _ = manifest(ctx.db, ctx.user, version)
    return _save(ctx, version, ctx.data["definition"], existing=existing)
