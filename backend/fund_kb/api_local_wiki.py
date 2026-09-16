"""Import one supplied local note as an unverified, document-linked Wiki draft.

Register this module with the central extension loader. No file, provider, model,
job, or publication API is used. Graph projection should recognize
wiki-provenance:<rid>.config.imported_local_note.citation_precision == DOCUMENT
and project the frozen source_snapshot as bibliographic links, never block cites.
"""
from __future__ import annotations

import copy
import re
import unicodedata
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from sqlalchemy import select

from . import models as m
from . import services as svc
from . import wiki
from .api_wiki import UUID_SCHEMA, _operation
from .ingestion import block_text, text_sha256

CATEGORY_ROOT = "估值与核算/本地知识迁移"
# solution_template belongs to kind=template in the existing content editor.
KNOWLEDGE_TYPES = ("faq", "rule", "sop", "scenario", "case", "term")
MAX_LOCAL_SOURCES = 32
WARNING = (
    "本地迁移待核验 Wiki：本文保留调用方提供的原 Markdown，仅供浏览、整理和编辑。"
    "全部来源均为书目级文档关联，段落锚点未核，不表示每段已准确引用或经专业复核；"
    "不可用于正式答疑，不可直接提交或发布。原文件哈希仅为调用方声明，未读取或核验原文件。"
)
HASH_SCHEMA = {"type": "string", "minLength": 64, "maxLength": 64, "pattern": "^[0-9a-fA-F]{64}$"}
UNICODE_TEXT = {"type": "string", "pattern": r"^[^\ud800-\udfff]*$"}
SCHEMAS = {
    "WikiLocalImportInput": {
        "type": "object", "additionalProperties": False,
        "required": ["space_id", "title", "markdown", "category", "knowledge_type",
                     "source_resource_ids", "note_sha256", "note_relative_path"],
        "properties": {
            "space_id": UUID_SCHEMA,
            "title": {**UNICODE_TEXT, "minLength": 1, "maxLength": 300},
            "markdown": {**UNICODE_TEXT, "minLength": 1, "maxLength": 60000},
            "category": {"type": "string", "maxLength": 200,
                         "pattern": r"^估值与核算/本地知识迁移(?:/[^/\ud800-\udfff]+)*$"},
            "knowledge_type": {"enum": list(KNOWLEDGE_TYPES)},
            "aliases": {"type": "array", "maxItems": 20, "uniqueItems": True,
                        "items": {**UNICODE_TEXT, "minLength": 1, "maxLength": 120}},
            "source_resource_ids": {"type": "array", "minItems": 1, "maxItems": MAX_LOCAL_SOURCES,
                                    "uniqueItems": True, "items": UUID_SCHEMA},
            "note_sha256": HASH_SCHEMA,
            "note_relative_path": {**UNICODE_TEXT, "minLength": 1, "maxLength": 1000},
        },
    },
    "WikiLocalImportResult": {
        "type": "object", "additionalProperties": False,
        "required": ["resource_id", "version_id", "state", "source_mode", "formal_evidence_allowed",
                     "citation_precision", "note_sha256", "markdown_sha256", "note_hash_verified",
                     "duplicate", "preserved", "input_changed", "warning"],
        "properties": {
            "resource_id": UUID_SCHEMA, "version_id": UUID_SCHEMA,
            "state": {"const": "DRAFT"}, "source_mode": {"const": "unverified_draft"},
            "formal_evidence_allowed": {"const": False}, "citation_precision": {"const": "DOCUMENT"},
            "note_sha256": HASH_SCHEMA, "markdown_sha256": HASH_SCHEMA,
            "note_hash_verified": {"const": False},
            **{key: {"type": "boolean"} for key in ("duplicate", "preserved", "input_changed")},
            "warning": {"const": WARNING},
        },
    },
}
PATHS = {"/wiki/local-imports": {"post": _operation(
    "createWikiLocalImport", "WikiLocalImportResult", status=201, body="WikiLocalImportInput")}}
PATHS["/wiki/local-imports"]["post"]["responses"]["200"] = copy.deepcopy(
    PATHS["/wiki/local-imports"]["post"]["responses"]["201"])
_INPUT_VALIDATOR = Draft202012Validator(SCHEMAS["WikiLocalImportInput"], format_checker=FormatChecker())


def _normal(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _input(data):
    # Keep the same fail-closed shape checks for direct handler invocations too.
    try:
        _INPUT_VALIDATOR.validate(data)
    except ValidationError:
        svc.fail(422, "SCHEMA_VALIDATION", "请求不符合本地知识迁移结构")
    data = copy.deepcopy(data)
    for value in [data["title"], data["markdown"], data["category"],
                  data["note_relative_path"], *data.get("aliases", [])]:
        try:
            value.encode("utf-8", errors="strict")
            wiki._safe_text(value)
        except (UnicodeError, wiki.WikiBuildError):
            # Do not relax the shared HTML check for ambiguous '<' formulas.
            svc.fail(422, "WIKI_LOCAL_UNSAFE_CONTENT", "文本含不安全或歧义标记，迁移暂停待人工检查")
        if not value.strip() or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            svc.fail(422, "WIKI_LOCAL_INVALID_TEXT", "迁移文本为空或包含控制字符")
    path = unicodedata.normalize("NFKC", data["note_relative_path"])
    # Metadata only: never resolve, open, stat, or follow this path. Reject URI,
    # Windows/UNC, percent-encoded, home-relative and ambiguous traversal forms.
    if (path.startswith(("/", "~")) or re.search(r"[\\:%\x00-\x1f\x7f]", path)
            or any(not part or part != part.strip() or part in {".", ".."} for part in path.split("/"))):
        svc.fail(422, "WIKI_LOCAL_INVALID_PATH", "仅接受无路径逃逸的本地笔记相对路径")
    data["category"] = wiki.category_path(data["category"])
    if data["category"] != CATEGORY_ROOT and not data["category"].startswith(CATEGORY_ROOT + "/"):
        svc.fail(422, "WIKI_LOCAL_INVALID_CATEGORY", "迁移分类须位于估值与核算/本地知识迁移下")
    for value in [data["title"], *data.get("aliases", [])]:
        if re.search(r"[\r\n\t\[\]|]", value):
            svc.fail(422, "WIKI_LOCAL_INVALID_TITLE", "标题或别名含歧义链接标记")
    data["title"] = data["title"].strip()
    data["aliases"] = [alias.strip() for alias in data.get("aliases", [])]
    if len({_normal(alias) for alias in data["aliases"]}) != len(data["aliases"]):
        svc.fail(422, "WIKI_LOCAL_DUPLICATE_ALIAS", "别名不可重复")
    data["space_id"] = str(UUID(data["space_id"]))
    data["source_resource_ids"] = sorted(str(UUID(rid)) for rid in data["source_resource_ids"])
    if len(set(data["source_resource_ids"])) != len(data["source_resource_ids"]):
        svc.fail(422, "WIKI_SOURCE_LIMIT", "来源文档不可重复")
    data["note_sha256"] = data["note_sha256"].lower()
    return data


def _receipt_name(space_id, owner_id, sha256):
    return "wiki-local-import-receipt:" + svc.digest([space_id, owner_id, sha256])


def _policy(db, name):
    return db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == name))


def _sources(ctx, data):
    snapshots, resources = [], []
    ids = data["source_resource_ids"]
    for offset in range(0, len(ids), 8):
        # An empty fragment list is valid for bibliographic association. Never
        # invent a source block to make an unparsed original appear anchored.
        _, frozen = wiki.choose_build_sources(ctx.db, ctx.user, data["space_id"], ids[offset:offset + 8],
                                             source_mode=wiki.DRAFT_SOURCE_MODE)
        snapshots.extend(copy.deepcopy(frozen))
    for snap in snapshots:
        resource = svc.resource_access(ctx.db, ctx.user, snap["resource_id"], "edit")
        version = svc.version_access(ctx.db, ctx.user, snap["version_id"], "edit")
        latest = ctx.db.scalar(select(m.ResourceVersion.id).where(m.ResourceVersion.resource_id == resource.id)
                               .order_by(m.ResourceVersion.version_no.desc()).limit(1))
        blob = ctx.db.get(m.Blob, version.source_blob_id)
        if (version.id != latest or version.state not in {"DRAFT", "IN_REVIEW"} or version.knowledge_type != "source"
                or not blob or blob.space_id != resource.space_id):
            svc.fail(409, "WIKI_LOCAL_SOURCE_NOT_CURRENT", "来源必须为有编辑权限的当前source文档草稿或待复核版本与对应原件")
        resources.append(resource)
    return snapshots, resources


def _existing(ctx, data, receipt, snapshots):
    config = receipt.config
    if (config.get("owner_id") != ctx.user.id or config.get("space_id") != data["space_id"]
            or config.get("note_sha256") != data["note_sha256"]):
        svc.fail(409, "WIKI_LOCAL_RECEIPT_INVALID", "迁移记录无法核验")
    result = config["result"]
    resource = svc.resource_access(ctx.db, ctx.user, result["resource_id"], "edit")
    version = svc.version_access(ctx.db, ctx.user, result["version_id"], "edit")
    provenance = _policy(ctx.db, f"wiki-provenance:{resource.id}")
    if (resource.owner_id != ctx.user.id or resource.space_id != data["space_id"] or resource.suspended
            or resource.active_release_id or version.resource_id != resource.id or version.state != "DRAFT"
            or not provenance or provenance.config.get("source_mode") != wiki.DRAFT_SOURCE_MODE
            or svc.digest(snapshots) != svc.digest(config.get("source_snapshot"))
            or svc.digest(snapshots) != svc.digest(provenance.config.get("source_snapshot"))):
        svc.fail(409, "WIKI_LOCAL_IMPORT_CHANGED", "原迁移页或来源已变化，不能重放旧迁移记录")
    return {**copy.deepcopy(result), "duplicate": True, "preserved": True,
            "input_changed": config.get("input_sha256") != svc.digest(data)}


def _markdown_chunks(markdown):
    # Preserve every character, including CRLF and whitespace, while respecting
    # the existing editor's 20,000-character paragraph limit. Prefer blank-line
    # boundaries; offsets below describe this note, never any source document.
    offset = 0
    while offset < len(markdown):
        end = min(offset + 20000, len(markdown))
        if end < len(markdown):
            boundary = markdown.rfind("\n\n", offset, end)
            if boundary > offset:
                end = boundary + 2
        yield offset, end, markdown[offset:end]
        offset = end


def create_import(ctx):
    data = _input(ctx.data)
    svc.space_access(ctx.db, ctx.user, data["space_id"], "editor")
    # Serialize this extension's competing titles on relational backends. SQLite
    # already holds BEGIN IMMEDIATE in the authenticated central dispatcher.
    ctx.db.scalar(select(m.Space).where(m.Space.id == data["space_id"]).with_for_update())
    snapshots, resources = _sources(ctx, data)
    receipt_name = _receipt_name(data["space_id"], ctx.user.id, data["note_sha256"])
    receipt = _policy(ctx.db, receipt_name)
    if receipt:
        return svc.Result(_existing(ctx, data, receipt, snapshots))

    title_key = _normal(data["title"])
    title_index = "wiki-local-import-title:" + svc.digest([data["space_id"], title_key])
    # Include hidden, deleted and suspended pages; return no identifying details.
    # An old name stays reserved by the index even if the imported page is renamed.
    occupied = _policy(ctx.db, title_index) is not None or any(
        _normal(name) == title_key for name in ctx.db.scalars(select(m.Resource.name).where(
            m.Resource.space_id == data["space_id"], m.Resource.kind == "knowledge")))
    if not occupied:
        # Version titles may differ from resource names after ordinary editing.
        occupied = any(_normal(title) == title_key for title in ctx.db.scalars(
            select(m.ResourceVersion.title).join(m.Resource, m.Resource.id == m.ResourceVersion.resource_id)
            .where(m.Resource.space_id == data["space_id"], m.Resource.kind == "knowledge")))
    if occupied:
        svc.fail(409, "WIKI_LOCAL_TITLE_CONFLICT", "同名知识页或迁移记录已存在，已保留原页，请人工核对并另拟标题")

    severity = {"PUBLIC": 0, "INTERNAL": 1, "CONFIDENTIAL": 2, "RESTRICTED": 3}
    resource = m.Resource(id=svc.uid(), space_id=data["space_id"], kind="knowledge", name=data["title"],
        category=data["category"], owner_id=ctx.user.id, restricted=any(r.restricted for r in resources),
        classification=max((r.classification for r in resources), key=severity.__getitem__),
        tags=["wiki", "local-import", "待核验", "unverified-sources", *["alias:" + a for a in data["aliases"]]])
    ctx.db.add(resource)
    ctx.db.flush()
    if resource.restricted:
        for permission in ("read", "edit", "manage"):
            ctx.db.add(m.ResourceGrant(resource_id=resource.id, user_id=ctx.user.id, permission=permission))
    version = m.ResourceVersion(id=svc.uid(), resource_id=resource.id, version_no=1, state="DRAFT",
        author_id=ctx.user.id, title=data["title"], knowledge_type=data["knowledge_type"], origin="COPY",
        source_verified=False, legal_status="UNKNOWN", applicability={}, required_facts=[],
        change_reason="本地已有Wiki迁移：书目级来源关联，原文与段落锚点待核验")
    ctx.db.add(version)
    ctx.db.flush()
    markdown_hash = text_sha256(data["markdown"])
    imported_note = {"sha256": data["note_sha256"], "relative_path": data["note_relative_path"],
                     "citation_precision": "DOCUMENT", "markdown_sha256": markdown_hash,
                     "sha256_scope": "CALLER_DECLARED_ORIGINAL_FILE", "note_hash_verified": False}
    ctx.db.add(m.RuntimePolicy(id=svc.uid(), name=f"wiki-provenance:{resource.id}", updated_by=ctx.user.id,
        config={"space_id": data["space_id"], "resource_id": resource.id, "created_version_id": version.id,
                "owner_id": ctx.user.id, "source_mode": wiki.DRAFT_SOURCE_MODE,
                "source_snapshot": copy.deepcopy(snapshots),
                "source_version_ids": sorted(s["version_id"] for s in snapshots), "reference_snapshot": [],
                "imported_local_note": imported_note}))
    blocks = [("warning", WARNING, {"label": "本地迁移待核验说明"})]
    blocks.extend(("paragraph", text, {"label": "本地笔记原文", "local_note_char_start": start,
                                      "local_note_char_end": end})
                  for start, end, text in _markdown_chunks(data["markdown"]))
    for ordinal, (typ, text, locator) in enumerate(blocks):
        block_data = {"text": text, "text_format": "markdown"}
        canonical = block_text({"block_type": typ, "data": block_data})
        ctx.db.add(m.ContentBlock(version_id=version.id, block_id=svc.uid(), ordinal=ordinal,
            block_type=typ, data=block_data, locator=locator, search_text=canonical,
            content_sha256=text_sha256(canonical)))
    ctx.db.flush()
    version.content_sha256 = svc.check_frozen_hash(ctx.db, version)
    result = {"resource_id": resource.id, "version_id": version.id, "state": "DRAFT",
              "source_mode": wiki.DRAFT_SOURCE_MODE, "formal_evidence_allowed": False,
              "citation_precision": "DOCUMENT", "note_sha256": data["note_sha256"],
              "markdown_sha256": markdown_hash, "note_hash_verified": False,
              "duplicate": False, "preserved": False, "input_changed": False, "warning": WARNING}
    ctx.db.add(m.RuntimePolicy(id=svc.uid(), name=receipt_name, updated_by=ctx.user.id,
        config={"space_id": data["space_id"], "owner_id": ctx.user.id, "note_sha256": data["note_sha256"],
                "input_sha256": svc.digest(data), "source_snapshot": copy.deepcopy(snapshots),
                "result": copy.deepcopy(result)}))
    ctx.db.add(m.RuntimePolicy(id=svc.uid(), name=title_index, updated_by=ctx.user.id,
        config={"space_id": data["space_id"], "resource_id": resource.id, "receipt_name": receipt_name}))
    svc.audit(ctx, "wiki.local_import.created", resource,
              {"version_id": version.id, "source_count": len(snapshots), "citation_precision": "DOCUMENT",
               "note_sha256": data["note_sha256"], "markdown_sha256": markdown_hash})
    return svc.Result(result, 201)


HANDLERS = {"createWikiLocalImport": create_import}


def replay_authority(ctx, cached):
    """HTTP idempotency must never expose a stale/revoked/deleted import result."""
    if ctx.operation not in HANDLERS:
        return False
    data = _input(ctx.data)
    svc.space_access(ctx.db, ctx.user, data["space_id"], "editor")
    snapshots, _ = _sources(ctx, data)
    receipt = _policy(ctx.db, _receipt_name(data["space_id"], ctx.user.id, data["note_sha256"]))
    if not receipt:
        svc.fail(409, "WIKI_LOCAL_RECEIPT_INVALID", "迁移记录无法核验")
    result = _existing(ctx, data, receipt, snapshots)
    body = cached.get("body") or {}
    if any(body.get(key) != result[key] for key in ("resource_id", "version_id", "note_sha256", "markdown_sha256")):
        svc.fail(409, "WIKI_LOCAL_RECEIPT_INVALID", "迁移重放记录不一致")
    return True
