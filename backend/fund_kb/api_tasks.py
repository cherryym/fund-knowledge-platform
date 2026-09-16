"""Bounded uploads, durable task control, exports, audit and deployment policy."""
import hashlib
import math

from fastapi.responses import FileResponse
from sqlalchemy import select

from . import models as m
from .services import *
from .storage import StorageError, safe_filename


def part_dict(p):
    return {"part_no": p.part_no, "size_bytes": p.size_bytes, "sha256": p.sha256}


def upload_dict(db, u):
    return primitive({"id": u.id, "version_id": u.version_id, "state": u.state,
        "part_size": u.part_size, "part_count": u.part_count,
        "completed_parts": [part_dict(p) for p in db.scalars(select(m.UploadPart)
            .where(m.UploadPart.upload_id == u.id).order_by(m.UploadPart.part_no))], "expires_at": u.expires_at})


def upload_access(ctx, open_required=False):
    u = ctx.db.get(m.Upload, ctx.id)
    if not u or u.user_id != ctx.user.id:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    v = version_access(ctx.db, ctx.user, u.version_id, "edit", dependencies=False)
    if open_required and (u.state != "OPEN" or aware(u.expires_at) <= now()):
        fail(409, "UPLOAD_CLOSED", "上传会话已封闭、取消或过期")
    if open_required and v.state != "DRAFT":
        fail(409, "VERSION_FROZEN", "上传目标已冻结")
    return u, v


def create_upload(ctx):
    v = version_access(ctx.db, ctx.user, ctx.data["version_id"], "edit", dependencies=False)
    if not can_edit_draft(ctx.db, ctx.user, v):
        fail(409, "VERSION_FROZEN", "只能向本人或已获编辑权限的团队活动草稿上传")
    r = ctx.db.get(m.Resource, v.resource_id)
    if r.kind != "document":
        fail(422, "DOCUMENT_REQUIRED", "原件上传需绑定文档资源")
    if ctx.data["size_bytes"] > ctx.settings.max_file_bytes:
        fail(413, "FILE_TOO_LARGE", "文件超出配置上限")
    if v.source_blob_id and v.origin != "COPY":
        fail(409, "SOURCE_IMMUTABLE", "原件已绑定，请创建新的替换版本")
    existing = ctx.db.scalars(select(m.Upload).where(m.Upload.version_id == v.id,
        m.Upload.state.in_(["OPEN", "SEALED"]))).all()
    if any(u.state == "SEALED" or aware(u.expires_at) > now() for u in existing):
        fail(409, "UPLOAD_EXISTS", "此草稿已有上传或解析会话")
    for old in existing:
        old.state = "EXPIRED"
    u = m.Upload(id=uid(), version_id=v.id, user_id=ctx.user.id,
        filename=safe_filename(ctx.data["filename"]), declared_size=ctx.data["size_bytes"],
        expected_sha256=ctx.data.get("expected_sha256"), part_size=8388608,
        part_count=math.ceil(ctx.data["size_bytes"] / 8388608), state="OPEN", expires_at=now() + timedelta(hours=24))
    ctx.db.add(u)
    ctx.db.flush()
    audit(ctx, "upload.created", u)
    return Result(upload_dict(ctx.db, u), 201)


def get_upload(ctx):
    u, _ = upload_access(ctx)
    result = upload_dict(ctx.db, u)
    if u.state == "OPEN" and aware(u.expires_at) <= now():
        result["state"] = "EXPIRED"
    return Result(result)


def cancel_upload(ctx):
    u, _ = upload_access(ctx, True)
    u.state = "CANCELLED"
    audit(ctx, "upload.cancelled", u)
    return Result(status=204)


def upload_part(ctx):
    u, _v = upload_access(ctx, True)
    number = int(ctx.request.path_params["part_no"])
    if number > u.part_count:
        fail(422, "INVALID_PART", "片号超出上传会话范围")
    expected_size = u.part_size if number < u.part_count else u.declared_size - (number - 1) * u.part_size
    if len(ctx.data) != expected_size:
        fail(422, "PART_SIZE_MISMATCH", "片段长度不符合会话约定")
    sha = hashlib.sha256(ctx.data).hexdigest()
    p = ctx.db.get(m.UploadPart, (u.id, number))
    if p:
        if p.sha256 != sha:
            fail(409, "PART_HASH_CONFLICT", "已上传片段内容不同，禁止覆盖")
        return Result(part_dict(p))
    key = f"uploads/{u.id}/{number}"
    try:
        ctx.request.app.state.storage.write_bytes(key, ctx.data)
    except StorageError:
        fail(409, "PART_HASH_CONFLICT", "已存在片段内容不同")
    p = m.UploadPart(upload_id=u.id, part_no=number, object_key=key, sha256=sha, size_bytes=len(ctx.data))
    ctx.db.add(p)
    audit(ctx, "upload.part", u, {"part_no": number, "sha256": sha})
    return Result(part_dict(p))


def complete_upload(ctx):
    u, v = upload_access(ctx, True)
    parts = ctx.db.scalars(select(m.UploadPart).where(m.UploadPart.upload_id == u.id)
        .order_by(m.UploadPart.part_no)).all()
    if [p.part_no for p in parts] != list(range(1, u.part_count + 1)) \
        or ctx.data["parts"] != [part_dict(p) for p in parts]:
        fail(409, "INCOMPLETE_UPLOAD", "有序片段清单与服务器不一致")
    sha, length = hashlib.sha256(), 0
    for p in parts:
        content = ctx.request.app.state.storage.read_bytes(p.object_key)
        if len(content) != p.size_bytes or hashlib.sha256(content).hexdigest() != p.sha256:
            fail(409, "UPLOAD_INTEGRITY_FAILED", "存储片段校验失败")
        length += len(content)
        sha.update(content)
    if length != u.declared_size or (u.expected_sha256 and sha.hexdigest() != u.expected_sha256):
        fail(409, "UPLOAD_INTEGRITY_FAILED", "完整长度或hash不匹配")
    u.state = "SEALED"
    j = create_job(ctx, "SCAN_PARSE", {"upload_id": u.id}, resource_id=v.resource_id, version_id=v.id)
    audit(ctx, "upload.sealed", u, {"sha256": sha.hexdigest()})
    return Result(job_dict(j), 202)


def job_access(ctx, job_id=None):
    j = ctx.db.get(m.Job, job_id or ctx.id)
    if not j:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    r = ctx.db.get(m.Resource, j.resource_id) if j.resource_id else None
    admin = bool(r and "admin" in roles(ctx.db, ctx.user, r.space_id))
    if j.owner_id != ctx.user.id and not admin:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if ctx.operation == "cancelJob" and j.owner_id == ctx.user.id:
        # The owner can stop their work after source/space access is revoked.
        # jobs() returns only status and explicitly strips any previous result.
        return j
    if j.kind == "INVALIDATE" and not admin:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if r:
        resource_access(ctx.db, ctx.user, r, "manage" if r.deleted_at else "read", allow_deleted=bool(r.deleted_at))
    if j.version_id and not (r and r.deleted_at):
        version_access(ctx.db, ctx.user, j.version_id)
    if j.run_id:
        from .api_consultation import run_access
        run_access(ctx, j.run_id)
    if j.kind == "EXPORT":
        for version_id in j.payload["version_ids"]:
            version_access(ctx.db, ctx.user, version_id, "download")
    # Historical job output is a read surface too: revoked source/connection access
    # must not leak through task history. Owners can still cancel in-flight work.
    if ctx.operation != "cancelJob" and j.kind == "COMPILE":
        task = j.payload.get("task")
        if task == "WIKI_BUILD":
            from .wiki import authorize_build_result
            authorize_build_result(ctx.db, ctx.user, j)
        elif task == "VECTOR_INDEX":
            from .vector_indexing import authorize_index_job
            authorize_index_job(ctx.db, ctx.user, j)
        elif task == "GUIDANCE_NORMALIZE":
            from .documents import guard_guidance_job
            guard_guidance_job(ctx.db, ctx.user, j, ctx.settings)
        elif task in {"MODEL_SYNC", "MODEL_TEST", "CODEX_LOGIN_START", "CODEX_AUTH_REFRESH", "CODEX_LOGIN_CANCEL", "CODEX_LOGOUT"}:
            from .providers import authorize_model_job
            authorize_model_job(ctx.db, ctx.user, j, ctx.settings)
    return j


def jobs(ctx):
    if ctx.operation == "listJobs":
        visible = []
        for j in ctx.db.scalars(select(m.Job)):
            try:
                job_access(ctx, j.id)
                visible.append(j)
            except APIError:
                continue
        page, nxt = paginate(visible, ctx.query)
        return Result({"items": [job_dict(j) for j in page], "next_cursor": nxt})
    j = job_access(ctx)
    if ctx.operation == "retryJob":
        if j.state not in {"FAILED", "CANCELLED"}:
            fail(409, "JOB_NOT_RETRYABLE", "只允许重试终止任务")
        if j.error_code in {"FILE_REJECTED", "UNSUPPORTED_FORMAT", "PASSWORD_REQUIRED", "LEGAL_HOLD"}:
            fail(409, "PERMANENT_JOB_FAILURE", "任务需要处理原始原因，不能直接重试")
        if j.kind == "PUBLISH":
            version_access(ctx.db, ctx.user, j.version_id, "publish")
        if j.kind == "COMPILE":
            task = j.payload.get("task")
            if task in {"MODEL_SYNC", "MODEL_TEST", "CODEX_LOGIN_START", "CODEX_AUTH_REFRESH", "CODEX_LOGIN_CANCEL", "CODEX_LOGOUT"}:
                from .providers import authorize_model_job
                authorize_model_job(ctx.db, ctx.user, j, ctx.settings, action="retry")
            elif task == "GUIDANCE_NORMALIZE":
                from .documents import guard_guidance_job
                guard_guidance_job(ctx.db, ctx.user, j, ctx.settings)
            elif task == "VECTOR_INDEX":
                from .vector_indexing import authorize_index_job
                authorize_index_job(ctx.db, ctx.user, j)
            else:
                space_access(ctx.db, ctx.user, j.payload.get("space_id") if task == "WIKI_BUILD" else j.payload["target_space_id"], "editor")
        if j.kind == "SCAN_PARSE":
            version_access(ctx.db, ctx.user, j.version_id, "edit")
        if j.kind == "PURGE":
            from .retention import assert_purge_allowed
            assert_purge_allowed(ctx.db, ctx.user, j.payload["resource_id"])
        if j.kind == "INVALIDATE" and r_admin(ctx, j) is False:
            fail(403, "FORBIDDEN", "此任务需要管理权限")
        j.state, j.error_code, j.cancel_requested, j.lease_until = "QUEUED", None, False, None
        j.stage = "queued"
        if j.run_id:
            run = ctx.db.get(m.ConsultationRun, j.run_id)
            old_model = run.model_snapshot or {}
            audit(ctx, "answer.attempt_archived", run, {"attempt": j.attempts,
                "error_code": run.error_code,
                "diagnostic_code": (run.policy_snapshot or {}).get('generation_diagnostic', {}).get('code'),
                "model": {k: old_model.get(k) for k in ("model_id", "protocol", "model_invoked", "execution_mode", "evidence_count")},
                "response_sha256": digest(run.response) if run.response else None})
            if (run.policy_snapshot or {}).get('question_analysis', {}).get('source') == 'model_prior_knowledge_unverified':
                run.policy_snapshot = {**run.policy_snapshot, 'reuse_question_analysis': True}
            run.state, run.error_code, run.completed_at = "QUEUED", None, None
            run.model_snapshot = {**old_model, "model_invoked": False, "execution_mode": None,
                "evidence_count": None, "attempt": j.attempts + 1}
        ctx.db.add(m.Outbox(id=uid(), event_type="JOB_CREATED", aggregate_id=j.id, payload={"job_id": j.id, "kind": j.kind}))
        ctx.dispatch.append(j.id)
        audit(ctx, "job.retried", j)
    elif ctx.operation == "cancelJob":
        if j.state not in {"QUEUED", "RUNNING"}:
            fail(409, "JOB_TERMINAL", "任务已结束")
        j.cancel_requested = True
        if j.state == "QUEUED":
            j.state = "CANCELLED"
            if j.run_id:
                run = ctx.db.get(m.ConsultationRun, j.run_id)
                run.state = "CANCELLED"
        audit(ctx, "job.cancel_requested", j)
    result = job_dict(j)
    if ctx.operation == "cancelJob":
        # Cancellation remains available to the owner after a revocation, but
        # is not an alternate way to read a previous attempt's private output.
        result = {**result, "result": None}
    return Result(result, 200 if ctx.operation == "getJob" else 202)


def r_admin(ctx, job):
    resource = ctx.db.get(m.Resource, job.resource_id) if job.resource_id else None
    return bool(resource and "admin" in roles(ctx.db, ctx.user, resource.space_id))


def export(ctx):
    ids = ctx.data["version_ids"]
    if len(ids) != len(set(ids)):
        fail(422, "DUPLICATE_VERSION", "导出版本不能重复")
    for version_id in ids:
        version_access(ctx.db, ctx.user, version_id, "download")
    j = create_job(ctx, "EXPORT", ctx.data)
    return Result(job_dict(j), 202)


def download_export(ctx):
    j = job_access(ctx)
    if j.kind != "EXPORT" or j.state != "SUCCEEDED" or not j.result:
        fail(409, "EXPORT_NOT_READY", "导出尚未完成")
    expiry = j.result.get("expires_at")
    if not expiry or datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= now():
        fail(404, "EXPORT_EXPIRED", "导出已过期")
    for version_id in j.payload["version_ids"]:
        version_access(ctx.db, ctx.user, version_id, "download")
    path = ctx.request.app.state.storage.local_path(f"exports/{j.id}.zip")
    return FileResponse(path, media_type="application/zip", filename=j.result.get("filename", f"{j.id}.zip"),
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


def model_policy(ctx):
    if not deployment_admin(ctx.user, ctx.settings):
        fail(403, "DEPLOYMENT_ADMIN_REQUIRED", "模型策略需要部署级管理员")
    policy = ctx.db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name == "model-policy")).first()
    if ctx.operation == "getModelPolicy":
        if not policy:
            return Result({"provider_ref": "evidence", "generation_model": "evidence-only",
                "extraction_model": "deterministic-extraction", "enable_vector": False,
                "prompt_version": "v1", "evaluation_id": "NOT_EVALUATED"}, headers={"ETag": '"0"'})
        return tagged(policy.config, policy)
    if policy:
        require_etag(ctx, policy)
    else:
        if ctx.request.headers.get("if-match") is None:
            fail(428, "PRECONDITION_REQUIRED", "缺少If-Match")
        if ctx.request.headers.get("if-match") != '"0"':
            fail(412, "REVISION_CONFLICT", "策略版本不匹配", revision=0)
    allowed = {"evidence", "configured-http"}
    if ctx.data["provider_ref"] not in allowed:
        fail(422, "MODEL_NOT_ALLOWED", "模型连接不在受控允许清单")
    if ctx.data["provider_ref"] == "configured-http" and (ctx.settings.llm_provider != "http" or not ctx.settings.llm_base_url or not ctx.settings.llm_model):
        fail(409, "MODEL_NOT_CONFIGURED", "尚未配置HTTP模型连接")
    registry = ctx.request.app.state.approved_model_policies
    approved = registry.get(ctx.data["evaluation_id"])
    bound = {k: ctx.data[k] for k in ("provider_ref", "generation_model", "extraction_model", "prompt_version", "enable_vector")}
    if not isinstance(approved, dict) or any(approved.get(k) != value for k, value in bound.items()):
        fail(409, "EVALUATION_REQUIRED", "策略变更需要已通过的对应评测记录")
    if bound["extraction_model"] != "deterministic-extraction":
        fail(422, "EXTRACTION_NOT_SUPPORTED", "当前抽取适配仅支持deterministic-extraction")
    expected_generation = "evidence-only" if bound["provider_ref"] == "evidence" else ctx.settings.llm_model
    if bound["generation_model"] != expected_generation:
        fail(422, "MODEL_NOT_ALLOWED", "生成模型与受控连接配置不匹配")
    if policy:
        bump(ctx.db, policy, config=dict(ctx.data), updated_by=ctx.user.id)
    else:
        policy = m.RuntimePolicy(id=uid(), name="model-policy", revision=1, config=dict(ctx.data), updated_by=ctx.user.id)
        ctx.db.add(policy)
        ctx.db.flush()
    audit(ctx, "model_policy.updated", policy)
    return tagged(policy.config, policy)


def audit_events(ctx):
    spaces = {space.id for space in ctx.db.scalars(select(m.Space))
        if "admin" in roles(ctx.db, ctx.user, space.id)}
    if not spaces and not deployment_admin(ctx.user, ctx.settings):
        fail(403, "FORBIDDEN", "审计查询需要管理权限")
    events = []
    for event in ctx.db.scalars(select(m.AuditEvent)):
        if ctx.query.get("object_id") and event.object_id != ctx.query["object_id"]:
            continue
        allowed = event.actor_id == ctx.user.id
        obj_cls = {"Space": m.Space, "Resource": m.Resource, "ResourceVersion": m.ResourceVersion,
            "IssueCase": m.IssueCase, "Job": m.Job}.get(event.object_type)
        obj = ctx.db.get(obj_cls, event.object_id) if obj_cls and event.object_id else None
        if obj:
            space_id = obj.id if isinstance(obj, m.Space) else getattr(obj, "space_id", None)
            if isinstance(obj, m.ResourceVersion):
                resource = ctx.db.get(m.Resource, obj.resource_id)
                space_id = resource.space_id if resource else None
            allowed |= space_id in spaces
        if allowed:
            events.append(event)
    page, nxt = paginate(events, ctx.query)
    return Result({"items": [primitive({k: getattr(e, k) for k in
        ("id", "action", "object_type", "outcome", "created_at", "trace_id")}) for e in page], "next_cursor": nxt})


HANDLERS = {"createUpload": create_upload, "getUpload": get_upload, "cancelUpload": cancel_upload,
    "uploadPart": upload_part, "completeUpload": complete_upload, "listJobs": jobs, "getJob": jobs,
    "retryJob": jobs, "cancelJob": jobs, "exportKnowledge": export, "downloadExport": download_export,
    "getModelPolicy": model_policy, "setModelPolicy": model_policy, "queryAudit": audit_events}
