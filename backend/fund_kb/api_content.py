"""Frozen versions, content, exact citations, review and durable publication commands."""
import copy
import re

from fastapi.responses import FileResponse, Response
from sqlalchemy import delete, select

from . import models as m
from .services import *

CONTEXT_FIELDS = {"product_type", "fund_label", "share_class", "asset_type", "market", "business_event",
    "business_date", "business_state"}


def validate_conditions(value):
    if not isinstance(value, dict) or set(value) - {"all", "none"}:
        fail(422, "INVALID_APPLICABILITY", "适用条件只允许all/none")
    for terms in value.values():
        if not isinstance(terms, list) or len(terms) > 20:
            fail(422, "INVALID_APPLICABILITY", "适用条件列表最多20项")
        for item in terms:
            if not isinstance(item, dict) or set(item) != {"field", "op", "values"}:
                fail(422, "INVALID_APPLICABILITY", "适用条件字段无效")
            vals = item["values"]
            if item["field"] not in CONTEXT_FIELDS or item["op"] not in {"eq", "in"} or not isinstance(vals, list) \
                or not vals or not all(isinstance(x, str) and x for x in vals) or (item["op"] == "eq" and len(vals) != 1):
                fail(422, "INVALID_APPLICABILITY", "适用条件值无效")


def validate_blocks(ctx, version, blocks):
    from .ingestion import block_text

    if [x["ordinal"] for x in blocks] != list(range(len(blocks))) or len({x["block_id"] for x in blocks}) != len(blocks):
        fail(422, "INVALID_BLOCK_ORDER", "块ID需唯一且ordinal从0连续")
    for b in blocks:
        typ, d = b["block_type"], b["data"]
        fields = {"heading": {"level", "text"}, "paragraph": {"text"}, "warning": {"text"},
            "list": {"ordered", "items"}, "table": {"columns", "rows"},
            "step": {"action", "owner_role", "output", "verification"},
            "formula": {"expression_text", "unit", "calculator_ref"},
            "image": {"version_id", "caption"}, "attachment": {"version_id", "caption"}}[typ]
        optional = {"text_format"} if typ in {"heading", "paragraph", "warning"} else set()
        if not fields <= set(d) or set(d) - fields - optional:
            fail(422, "INVALID_BLOCK_DATA", "内容块字段与类型不一致")
        if typ in {"heading", "paragraph", "warning"}:
            text_format = d.get("text_format", "plain")
            if not isinstance(text_format, str) or text_format not in {"plain", "markdown"}:
                fail(422, "INVALID_TEXT_FORMAT", "文本格式仅支持plain或markdown")
            # Full compiled prose may exceed 20k characters in one semantic
            # block. HTTP/provider total byte budgets bound transport; never
            # impose a second arbitrary paragraph limit or clip its contents.
            if not isinstance(d["text"], str) or (typ == "heading" and len(d["text"]) > 500):
                fail(422, "INVALID_BLOCK_DATA", "文本字段无效或超过长度限制")
            if typ == "heading" and (not d["text"] or type(d["level"]) is not int or not 1 <= d["level"] <= 6):
                fail(422, "INVALID_BLOCK_DATA", "标题级别或内容无效")
        elif typ == "list":
            if type(d["ordered"]) is not bool or not isinstance(d["items"], list) or len(d["items"]) > 200 \
                or not all(isinstance(x, str) for x in d["items"]):
                fail(422, "INVALID_BLOCK_DATA", "列表内容无效")
        elif typ == "table":
            cols, rows = d["columns"], d["rows"]
            if not isinstance(cols, list) or not isinstance(rows, list) or len(cols) > 100 or len(rows) > 2000 \
                or not all(isinstance(c, str) for c in cols) \
                or not all(isinstance(row, list) and len(row) == len(cols) and all(isinstance(c, str) for c in row) for row in rows):
                fail(422, "INVALID_TABLE", "表格行列不一致或超出限制")
        elif typ == "step":
            if not all(isinstance(x, str) and x.strip() for x in d.values()):
                fail(422, "INVALID_STEP", "方案步骤必须含操作、岗位、输出和核对内容")
        elif typ == "formula":
            allowed = getattr(ctx.request.app.state, "calculator_registry", {})
            if not isinstance(d["expression_text"], str) or not isinstance(d["unit"], str) \
                or (d["calculator_ref"] is not None and d["calculator_ref"] not in allowed):
                fail(422, "UNKNOWN_CALCULATOR", "计算器未登记；公式仅用于展示")
        elif typ in {"image", "attachment"}:
            attachment = version_access(ctx.db, ctx.user, d["version_id"])
            blob = ctx.db.get(m.Blob, attachment.source_blob_id) if attachment.source_blob_id else None
            if not blob or blob.scan_state != "CLEAN":
                fail(409, "ATTACHMENT_NOT_CLEAN", "附件尚未通过扫描")
        raw_text = block_text(b)
        if re.search(r"<\s*(script|iframe|object|embed)\b|javascript\s*:|on(?:error|load)\s*=", raw_text, re.IGNORECASE):
            fail(422, "UNSAFE_CONTENT", "内容包含不允许的可执行标记")
        seen = set()
        for cite in b["citations"]:
            target = version_access(ctx.db, ctx.user, cite["version_id"])
            key = (cite["version_id"], cite["block_id"], cite["purpose"])
            if target.id == version.id or key in seen:
                fail(409, "DEPENDENCY_CYCLE", "不能自引或重复引用")
            seen.add(key)
            if not ctx.db.get(m.ContentBlock, (target.id, cite["block_id"])):
                fail(422, "CITATION_BLOCK_MISSING", "引用内容块不存在")
            if not released(ctx.db, target.id):
                from .wiki import draft_reference_allowed
                if not draft_reference_allowed(ctx.db, ctx.user, ctx.db.get(m.Resource, version.resource_id), target.id):
                    fail(409, "SOURCE_UNPUBLISHED", "引用来源须已发布；待核验Wiki仅可引用原冻结来源")


def store_blocks(ctx, v, blocks):
    from .ingestion import block_text

    ctx.db.execute(delete(m.EvidenceLink).where(m.EvidenceLink.from_version_id == v.id))
    ctx.db.execute(delete(m.ContentBlock).where(m.ContentBlock.version_id == v.id))
    for b in blocks:
        ctx.db.add(m.ContentBlock(version_id=v.id, block_id=b["block_id"], ordinal=b["ordinal"],
            block_type=b["block_type"], data=copy.deepcopy(b["data"]), locator=copy.deepcopy(b["locator"]),
            search_text=block_text(b), content_sha256=hashlib.sha256(block_text(b).encode("utf-8")).hexdigest()))
    ctx.db.flush()
    for b in blocks:
        for cite in b["citations"]:
            ctx.db.add(m.EvidenceLink(id=uid(), from_version_id=v.id, from_block_id=b["block_id"],
                to_version_id=cite["version_id"], to_block_id=cite["block_id"], purpose=cite["purpose"]))
    ctx.db.flush()
    check_dependency_access(ctx.db, ctx.user, v)


def draft(ctx):
    return require_draft(ctx)


def versions(ctx):
    resource = resource_access(ctx.db, ctx.user, ctx.id, "edit" if ctx.operation == "createDraftVersion" else "read")
    if ctx.operation == "listVersions":
        found = []
        for v in ctx.db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.resource_id == resource.id)):
            try:
                version_access(ctx.db, ctx.user, v)
                found.append(v)
            except APIError:
                continue
        page, nxt = paginate(found, ctx.query, key=lambda x: (x.version_no, x.id))
        return Result({"items": [version_dict(ctx.db, v) for v in page], "next_cursor": nxt})
    if ctx.db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.resource_id == resource.id,
        m.ResourceVersion.author_id == ctx.user.id, m.ResourceVersion.state == "DRAFT")).first():
        fail(409, "DRAFT_EXISTS", "此作者已有活动草稿")
    base_id = ctx.data.get("base_version_id")
    base = version_access(ctx.db, ctx.user, base_id) if base_id else None
    if base and base.resource_id != resource.id:
        fail(422, "INVALID_BASE", "基准版本必须属于同一资源")
    if base and resource.kind == "document":
        if ctx.db.scalar(select(m.Job.id).where(m.Job.version_id == base.id, m.Job.kind == "SCAN_PARSE",
            m.Job.state.in_(["QUEUED", "RUNNING"]))):
            fail(409, "PARSING_IN_PROGRESS", "请等待原件解析完成后创建线上修订")
    if resource.active_release_id and not base:
        fail(409, "BASE_REQUIRED", "更新已发布资源须指定基准版本")
    bump(ctx.db, resource)
    numbers = ctx.db.scalars(select(m.ResourceVersion.version_no).where(m.ResourceVersion.resource_id == resource.id)).all()
    v = m.ResourceVersion(id=uid(), resource_id=resource.id, version_no=max(numbers, default=0) + 1,
        author_id=ctx.user.id, origin="COPY" if base else "HUMAN", state="DRAFT", revision=1,
        title=ctx.data["title"], base_version_id=base_id, change_kind=ctx.data.get("change_kind", "UPDATE"),
        change_reason=ctx.data["change_reason"], knowledge_type="source" if resource.kind == "document" else
            ("solution_template" if resource.kind == "template" else "faq"), source_verified=False)
    if base:
        for key in ("source_blob_id", "source_url", "knowledge_type", "legal_status", "valid_from", "valid_to", "applicability", "required_facts"):
            setattr(v, key, copy.deepcopy(getattr(base, key)))
    ctx.db.add(v)
    ctx.db.flush()
    if base:
        store_blocks(ctx, v, content_blocks(ctx.db, base.id))
        for edge in ctx.db.scalars(select(m.RelationEdge).where(m.RelationEdge.source_version_id == base.id)):
            ctx.db.add(m.RelationEdge(id=uid(), source_version_id=v.id, **relation_dict(edge)))
    ctx.db.flush()
    audit(ctx, "version.created", v)
    from .vector_indexing import queue_resource_index
    queue_resource_index(ctx, resource.id)
    return tagged(version_dict(ctx.db, v), v, 201)


def get_version(ctx):
    v = version_access(ctx.db, ctx.user, ctx.id)
    return tagged(version_dict(ctx.db, v), v)


def edit_version(ctx):
    v = draft(ctx)
    if ctx.db.scalars(select(m.Job).where(m.Job.version_id == v.id, m.Job.kind == "SCAN_PARSE",
        m.Job.state.in_(["QUEUED", "RUNNING"]))).first():
        fail(409, "PARSING_IN_PROGRESS", "解析期间不能修改草稿")
    validate_conditions(ctx.data["applicability"])
    if any(x not in CONTEXT_FIELDS for x in ctx.data["required_facts"]):
        fail(422, "INVALID_REQUIRED_FACT", "缺失事实字段不在允许清单")
    r = ctx.db.get(m.Resource, v.resource_id)
    previous_blocks = content_blocks(ctx.db, v.id)
    document_body_changed = r.kind == "document" and ctx.data["blocks"] != previous_blocks
    kt = ctx.data["knowledge_type"]
    if (r.kind == "document" and kt != "source") or (r.kind == "template" and kt != "solution_template") \
        or (r.kind == "knowledge" and kt in {"source", "solution_template"}):
        fail(422, "RESOURCE_KIND_MISMATCH", "知识类型与资源类型不匹配")
    dates = {k: date.fromisoformat(ctx.data[k]) if ctx.data[k] else None for k in ("valid_from", "valid_to")}
    if dates["valid_from"] and dates["valid_to"] and dates["valid_to"] <= dates["valid_from"]:
        fail(422, "INVALID_VALIDITY", "有效区间须为左闭右开且结束晚于开始")
    validate_blocks(ctx, v, ctx.data["blocks"])
    fields = {k: copy.deepcopy(value) for k, value in ctx.data.items() if k not in {"blocks", "valid_from", "valid_to"}}
    if document_body_changed and v.origin == "UPLOAD":
        # The attachment remains immutable. The draft is now an online manuscript,
        # so previews must not return the old import rendering after a save.
        fields["origin"] = "HUMAN"
    bump(ctx.db, v, **fields, **dates, content_sha256=None, source_verified=False)
    blocks = copy.deepcopy(ctx.data["blocks"])
    if r.kind == "document" and v.origin != "UPLOAD":
        previous = {b["block_id"]: b for b in previous_blocks}
        for b in blocks:
            old = previous.get(b["block_id"])
            if not old or old["data"] != b["data"] or old["block_type"] != b["block_type"]:
                # Edited text is not a verbatim source page/paragraph locator.
                b["locator"] = {"label": "线上修订内容", "base_version_id": v.base_version_id}
    store_blocks(ctx, v, blocks)
    audit(ctx, "version.edited", v)
    from .vector_indexing import queue_resource_index
    queue_resource_index(ctx, r.id)
    return tagged(version_dict(ctx.db, v), v)


def discard(ctx):
    v = draft(ctx)
    checks = [select(m.Job).where(m.Job.version_id == v.id),
        select(m.Upload).where(m.Upload.version_id == v.id),
        select(m.EvidenceLink).where(m.EvidenceLink.to_version_id == v.id),
        select(m.RelationEdge).where(m.RelationEdge.evidence_version_id == v.id),
        select(m.ResourceVersion).where(m.ResourceVersion.base_version_id == v.id),
        select(m.RunEvidence).where(m.RunEvidence.version_id == v.id)]
    if any(ctx.db.scalars(stmt).first() for stmt in checks):
        fail(409, "DRAFT_HAS_DEPENDENCIES", "草稿仍有上传、任务或引用依赖")
    ctx.db.execute(delete(m.RelationEdge).where(m.RelationEdge.source_version_id == v.id))
    ctx.db.execute(delete(m.EvidenceLink).where(m.EvidenceLink.from_version_id == v.id))
    ctx.db.execute(delete(m.ContentBlock).where(m.ContentBlock.version_id == v.id))
    audit(ctx, "version.discarded", v)
    resource_id, version_id = v.resource_id, v.id
    ctx.db.delete(v)
    from .vector_indexing import queue_removed_version
    queue_removed_version(ctx,resource_id,version_id)
    return Result(status=204)


def content(ctx):
    representation = ctx.query["representation"]
    v = version_access(ctx.db, ctx.user, ctx.id,
        "download" if ctx.query.get("download") or representation == "source" else "read")
    storage = ctx.request.app.state.storage
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"}
    if v.source_blob_id:
        blob = ctx.db.get(m.Blob, v.source_blob_id)
        if not blob or blob.scan_state != "CLEAN":
            fail(409, "FILE_NOT_CLEAN", "原件尚未通过扫描")
    if representation == "source":
        if not v.source_blob_id:
            fail(404, "NO_SOURCE_FILE", "该版本无原始附件")
        return FileResponse(storage.local_path(blob.object_key), media_type=blob.mime_type,
            filename=Path(blob.object_key).name, headers=headers,
            content_disposition_type="attachment" if ctx.query.get("download") else "inline")
    if representation == "preview" and v.source_blob_id and v.origin == "UPLOAD":
        key = f"previews/{v.id}.html"
        if storage.exists(key):
            return FileResponse(storage.local_path(key), media_type="text/html", headers=headers)
        if not content_blocks(ctx.db, v.id):
            fail(409, "PREVIEW_NOT_READY", "预览尚未生成")
        # Reflow authorized parsed blocks if the optional preview artifact is absent.
        # COPY/HUMAN previews always render the current revision, never stale HTML.
    from .ingestion import render_blocks
    fmt = "markdown" if representation == "markdown" else "html"
    body = render_blocks(content_blocks(ctx.db, v.id), fmt)
    if ctx.query.get("download"):
        headers["Content-Disposition"] = f'attachment; filename="{v.id}.{"md" if fmt == "markdown" else "html"}"'
    return Response(body, media_type="text/markdown" if fmt == "markdown" else "text/html", headers=headers)


def submit(ctx):
    v = draft(ctx)
    from .wiki import _policy, assert_formal_wiki
    reference_review = (ctx.data or {}).get("review_scope") == "reference"
    # draft() has already checked current ACLs and frozen Wiki provenance.
    # Reference review freezes a pending snapshot; it grants no formal approval.
    provenance = _policy(ctx.db, f"wiki-provenance:{v.resource_id}")
    if not reference_review or (provenance and provenance.config.get("imported_local_note")):
        assert_formal_wiki(ctx.db, v)
    check_dependency_access(ctx.db, ctx.user, v)
    r = ctx.db.get(m.Resource, v.resource_id)
    blob = ctx.db.get(m.Blob, v.source_blob_id) if v.source_blob_id else None
    has_content = bool(content_blocks(ctx.db, v.id))
    raw_source_review = bool(reference_review and r.kind == "document" and not has_content
        and blob and blob.scan_state == "CLEAN" and blob.size_bytes > 0)
    if not has_content and not raw_source_review:
        fail(409, "EMPTY_CONTENT", "不能提交空正文")
    if ctx.db.scalars(select(m.Upload).where(m.Upload.version_id == v.id, m.Upload.state == "OPEN")).first() \
        or ctx.db.scalars(select(m.Job).where(m.Job.version_id == v.id, m.Job.state.in_(["QUEUED", "RUNNING"]))).first():
        fail(409, "WORK_IN_PROGRESS", "上传或后台处理尚未完成")
    if r.kind == "document":
        if not blob or blob.scan_state != "CLEAN":
            fail(409, "SOURCE_NOT_CLEAN", "来源原件尚未通过扫描")
    if v.legal_status not in {"NOT_APPLICABLE", "UNKNOWN"} and not v.valid_from:
        fail(409, "EFFECTIVE_DATE_REQUIRED", "规范版本需明确生效日期")
    # Default is resolved before freeze, never by mutating an approved snapshot.
    if v.legal_status == "NOT_APPLICABLE" and not v.valid_from:
        v.valid_from = effective_date()
        ctx.db.flush()
    h = check_frozen_hash(ctx.db, v)
    bump(ctx.db, v, state="IN_REVIEW", content_sha256=h)
    audit(ctx, "version.submitted", v, {"sha256": h, **({"review_scope": "reference"} if reference_review else {}),
        **({"raw_source_review": True, "parsed_content_available": False} if raw_source_review else {})})
    return tagged(version_dict(ctx.db, v), v)


def review_dict(r):
    return primitive({k: getattr(r, k) for k in ("id", "version_id", "reviewer_id", "decision", "reviewed_sha256", "comment", "created_at")})


def reviews(ctx):
    v = version_access(ctx.db, ctx.user, ctx.id, "review" if ctx.operation == "reviewVersion" else "read")
    if ctx.operation == "listVersionReviews":
        return Result([review_dict(r) for r in ctx.db.scalars(select(m.ReviewDecision).where(m.ReviewDecision.version_id == v.id))])
    require_etag(ctx, v)
    if v.state != "IN_REVIEW":
        fail(409, "NOT_IN_REVIEW", "仅待审快照可终局审核")
    if version_contributor(ctx.db, ctx.user, v):
        fail(403, "SELF_REVIEW_FORBIDDEN", "作者或共同编辑者不能审核自己的稿件")
    if ctx.data["reviewed_sha256"] != v.content_sha256 or check_frozen_hash(ctx.db, v) != v.content_sha256:
        fail(409, "REVIEW_HASH_MISMATCH", "审核必须绑定提交时的完整快照hash")
    if ctx.data["decision"] == "APPROVE":
        from .wiki import assert_formal_wiki
        assert_formal_wiki(ctx.db, v)
    source_verified = ctx.data.get("source_verified", False)
    if source_verified and (v.knowledge_type != "source" or ctx.data["decision"] != "APPROVE"):
        fail(422, "INVALID_SOURCE_REVIEW", "来源核验仅能用于批准来源版本")
    if source_verified:
        blob = ctx.db.get(m.Blob, v.source_blob_id) if v.source_blob_id else None
        if not blob or blob.scan_state != "CLEAN":
            fail(409, "SOURCE_NOT_CLEAN", "原件扫描未通过")
    decision = m.ReviewDecision(id=uid(), version_id=v.id, reviewer_id=ctx.user.id,
        decision=ctx.data["decision"], reviewed_sha256=v.content_sha256, comment=ctx.data["comment"])
    ctx.db.add(decision)
    bump(ctx.db, v, state="APPROVED" if ctx.data["decision"] == "APPROVE" else "REJECTED",
        source_verified=source_verified)
    ctx.db.flush()
    audit(ctx, "version.reviewed", v, {"decision": decision.decision, "sha256": v.content_sha256})
    return Result(review_dict(decision), 201)


def publish(ctx):
    v = version_access(ctx.db, ctx.user, ctx.id, "publish")
    from .wiki import assert_formal_wiki
    from .admin_review import confirmation
    admin_confirmed = confirmation(ctx.db, v)
    if not admin_confirmed:
        assert_formal_wiki(ctx.db, v)
    require_etag(ctx, v)
    if v.state != "APPROVED" or check_frozen_hash(ctx.db, v) != v.content_sha256:
        fail(409, "VERSION_NOT_APPROVED", "版本未获有效审核或快照hash不一致")
    review = independent_review(ctx.db, v)
    if (not review and not admin_confirmed) or (v.knowledge_type == "source" and not v.source_verified):
        fail(409, "APPROVAL_REQUIRED", "缺少独立审核或来源核验")
    if released(ctx.db, v.id):
        fail(409, "ALREADY_PUBLISHED", "版本已经发布")
    if ctx.db.scalars(select(m.Job).where(m.Job.version_id == v.id, m.Job.kind == "PUBLISH",
        m.Job.state.in_(["QUEUED", "RUNNING"]))).first():
        fail(409, "PUBLISH_IN_PROGRESS", "此版本正在发布")
    j = create_job(ctx, "PUBLISH", {"version_id": v.id}, resource_id=v.resource_id, version_id=v.id)
    return Result(job_dict(j), 202)


def compile_draft(ctx):
    v = version_access(ctx.db, ctx.user, ctx.id)
    space_access(ctx.db, ctx.user, ctx.data["target_space_id"], "editor")
    if not released(ctx.db, v.id) or not v.source_verified:
        fail(409, "SOURCE_UNVERIFIED", "整理来源须完成核验与发布")
    if ctx.data.get("template_version_id"):
        template = version_access(ctx.db, ctx.user, ctx.data["template_version_id"])
        if not released(ctx.db, template.id):
            fail(409, "TEMPLATE_UNPUBLISHED", "模板尚未发布")
    payload = {"version_id": v.id, **ctx.data}
    j = create_job(ctx, "COMPILE", payload, resource_id=v.resource_id, version_id=v.id)
    return Result(job_dict(j), 202)


def relations(ctx):
    v = draft(ctx) if ctx.operation == "replaceDraftRelations" else version_access(ctx.db, ctx.user, ctx.id)
    if ctx.operation == "replaceDraftRelations":
        if len(ctx.data) > 500 or len({digest(x) for x in ctx.data}) != len(ctx.data):
            fail(422, "INVALID_RELATIONS", "关系过多或重复")
        for edge in ctx.data:
            resource_access(ctx.db, ctx.user, edge["target_resource_id"])
            validate_conditions(edge["conditions"])
            if edge["target_resource_id"] == v.resource_id:
                fail(409, "DEPENDENCY_CYCLE", "关系不能指向自身资源")
            if bool(edge.get("evidence_version_id")) != bool(edge.get("evidence_block_id")):
                fail(422, "INCOMPLETE_ANCHOR", "关系出处需要版本和块成对提供")
            if edge.get("evidence_version_id"):
                version_access(ctx.db, ctx.user, edge["evidence_version_id"])
                if not ctx.db.get(m.ContentBlock, (edge["evidence_version_id"], edge["evidence_block_id"])):
                    fail(422, "INVALID_ANCHOR", "关系出处块无效")
        ctx.db.execute(delete(m.RelationEdge).where(m.RelationEdge.source_version_id == v.id))
        for edge in ctx.data:
            ctx.db.add(m.RelationEdge(id=uid(), source_version_id=v.id, **edge))
        bump(ctx.db, v, content_sha256=None)
        ctx.db.flush()
        check_dependency_access(ctx.db, ctx.user, v)
        audit(ctx, "relations.replaced", v)
        return tagged(version_dict(ctx.db, v), v)
    result = []
    for edge in ctx.db.scalars(select(m.RelationEdge).where(m.RelationEdge.source_version_id == v.id)):
        try:
            resource_access(ctx.db, ctx.user, edge.target_resource_id)
            if edge.evidence_version_id:
                version_access(ctx.db, ctx.user, edge.evidence_version_id)
            result.append(relation_dict(edge))
        except APIError:
            continue
    return tagged(result, v)


def review_queue(ctx):
    candidates = []
    for v in ctx.db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.state.in_(["IN_REVIEW", "APPROVED"]))):
        try:
            r = ctx.db.get(m.Resource, v.resource_id)
            if not roles(ctx.db, ctx.user, r.space_id) & {"reviewer", "publisher"}:
                continue
            version_access(ctx.db, ctx.user, v)
            candidates.append(v)
        except APIError:
            continue
    page, nxt = paginate(candidates, ctx.query)
    return Result({"items": [version_dict(ctx.db, v) for v in page], "next_cursor": nxt})


HANDLERS = {"listVersions": versions, "createDraftVersion": versions, "getVersion": get_version,
    "editDraftVersion": edit_version, "discardDraft": discard, "readVersionContent": content,
    "submitVersion": submit, "listVersionReviews": reviews, "reviewVersion": reviews,
    "publishVersion": publish, "compileKnowledgeDraft": compile_draft, "getRelations": relations,
    "replaceDraftRelations": relations, "getReviewQueue": review_queue}
