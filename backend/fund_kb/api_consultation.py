"""ACL-filtered retrieval, private consultations, feedback and expert case workflow."""
import json

from fastapi.responses import Response
from jsonschema import ValidationError
from sqlalchemy import select

from . import models as m
from .services import *


def search(ctx):
    from .retrieval import rank_evidence

    data = ctx.data
    context = dict(data.get("context") or {})
    if data.get("business_date") and context.get("business_date") and data["business_date"] != context["business_date"]:
        fail(422, "AMBIGUOUS_DATE", "两个业务日期字段不一致")
    if data.get("business_date"):
        context["business_date"] = data["business_date"]
    evidence = eligible_evidence(ctx.db, ctx.user, data["space_id"], context, for_answer=False)
    if data.get("kind"):
        evidence = [e for e in evidence if e["kind"] == data["kind"]]
    ranked = rank_evidence(data["query"], evidence, vector_index=ctx.request.app.state.vector_index,
        limit=max(100, len(evidence)))
    page, nxt = paginate(ranked, data, key=lambda x: (float(x.get("score", 0)), x["version_id"], x["block_id"]))
    # Recheck every candidate, including source dependencies, immediately before serializing excerpts.
    result = []
    for e in page:
        version_access(ctx.db, ctx.user, e["version_id"])
        result.append({"resource_id": e["resource_id"], "version_id": e["version_id"],
            "block_id": e["block_id"], "title": e["title"], "excerpt": e["text"], "locator": e["locator"]})
    return Result({"items": result, "next_cursor": nxt})


def thread_access(ctx, thread_id=None):
    t = ctx.db.get(m.ConsultationThread, thread_id or ctx.id)
    if not t or t.owner_id != ctx.user.id or t.deleted_at:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    space_access(ctx.db, ctx.user, t.space_id)
    return t


def thread_dict(t):
    return primitive({k: getattr(t, k) for k in ("id", "space_id", "title", "created_at")})


def run_access(ctx, run_id=None):
    r = ctx.db.get(m.ConsultationRun, run_id or ctx.id)
    if not r:
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    thread_access(ctx, r.thread_id)
    return r


def run_sources_readable(ctx, run, user=None):
    user = user or ctx.user
    evidence = list(ctx.db.scalars(select(m.RunEvidence).where(m.RunEvidence.run_id == run.id)))
    version_ids = {e.version_id for e in evidence}
    source_free_planning = ((run.request or {}).get("reasoning_strategy") == "model_first"
        and run.state in {"QUEUED", "RUNNING"} and not run.evidence_snapshot and not run.response
        and not (run.policy_snapshot or {}).get("question_analysis"))
    if not source_free_planning:
        version_ids.update((run.request or {}).get("attachment_version_ids", []))
    for cite in (run.response or {}).get("citations", []):
        version_ids.add(cite["version_id"])
    for source in run.evidence_snapshot or []:
        if source.get("version_id"):
            version_ids.add(source["version_id"])
    for version_id in version_ids:
        version_access(ctx.db, user, version_id)
    return version_ids


def _failure_diagnostic(run):
    import re
    detail = (run.policy_snapshot or {}).get("generation_diagnostic") or {}
    if not isinstance(detail, dict):
        detail = {}
    code = detail.get("code") or run.error_code or "UNKNOWN_FAILURE"
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", code):
        code = "UNKNOWN_FAILURE"
    errors = detail.get("schema_errors", [])
    if not isinstance(errors, list):
        errors = []
    allowed_path_fields = {"status", "summary", "claims", "analysis", "interpretation", "checks", "branches", "title", "reason",
        "condition", "action", "evidence_ids", "missing_facts", "field", "question", "why_needed", "solution",
        "goal", "preconditions", "materials", "steps", "completion_checks", "escalation", "id", "owner_role", "inputs",
        "output", "verification", "depends_on", "limitations", "required_sources", "citations", "resource_id", "version_id",
        "block_id", "source_title", "excerpt", "locator", "content_sha256", "run_id", "mode", "scope", "facts", "review_status",
        "generated_at", "initial_assessment", "search_queries", "focus_terms", "decision_points"}
    def safe_path(path):
        return re.sub(r"\.([A-Za-z_<>]+)", lambda match: "." + (match[1] if match[1] in allowed_path_fields else "<field>"), path)
    schema_errors = [dict(path=safe_path(item["path"]), rule=item["rule"]) for item in errors[:8]
        if isinstance(item, dict) and isinstance(item.get("path"), str) and len(item["path"]) <= 240
        and re.fullmatch(r"\$(?:\.[A-Za-z_<>]+|\[[0-9]+\])*", item["path"])
        and item.get("rule") in {"type", "required", "additionalProperties", "enum", "const", "minItems", "maxItems",
            "minLength", "maxLength", "pattern", "uniqueItems", "oneOf", "anyOf", "format", "minimum", "maximum"}]
    truncated = code in {"MODEL_OUTPUT_TRUNCATED_OR_TOOL_REQUESTED", "PLANNING_OUTPUT_TRUNCATED", "PROVIDER_OUTPUT_TRUNCATED"}
    transport = code in {'PROVIDER_CONNECTION_INTERRUPTED', 'PROVIDER_READ_FAILED', 'PROVIDER_CONNECTION_FAILED'}
    service_messages = {
        "PROVIDER_EMPTY_OUTPUT": "服务商已结束响应，但没有返回可见答案；不是本地资料缺失或业务判断被否定。",
        "PROVIDER_REFUSAL": "服务商拒绝了本次请求，未返回可交付答案。不会伪装为成功或自动换模型。",
        "PROVIDER_RATE_LIMITED": "服务商当前限流，本次没有完成答复。请稍后再试；不会自动重复计费调用。",
        "PROVIDER_AUTH_FAILED": "模型连接的身份验证失败，请在“我的模型”检查该连接。",
        "PROVIDER_ACCESS_DENIED": "当前模型账号没有本次调用所需的服务权限。",
        "PROVIDER_MODEL_NOT_FOUND": "服务商无法找到或使用当前选择的型号，请核对连接与模型标识。",
        "PROVIDER_RESPONSE_INCOMPLETE": "模型响应没有完整结束，不能将半截输出当作完整答案。",
        "PROVIDER_RESPONSE_FAILED": "服务商返回了处理失败状态，本次没有完整答复。",
        "PROVIDER_STREAM_FAILED": "服务商在流式返回期间报告失败，本次没有完整答复。",
    }
    message = service_messages.get(code) or ("模型达到输出长度上限，答复尚未完整生成；思考内容也可能占用输出额度。" if truncated else
        "回答中存在尚无依据的已执行或已审批表述，未作为业务结论交付。" if code == "EXECUTION_OR_APPROVAL_UNVERIFIED" else
        "模型返回内容不符合输出结构，尚未形成可交付答案。" if "SCHEMA" in code else
        "模型服务连接中断，尚未收到完整答复。" if code in {'PROVIDER_CONNECTION_INTERRUPTED', 'PROVIDER_READ_FAILED', 'PROVIDER_CONNECTION_FAILED'} else
        "模型服务未返回完整可解析的响应，尚未形成答复；这不等于资料或业务判断被否定。" if code in {'PROVIDER_INVALID_RESPONSE', 'PROVIDER_INVALID_JSON_OBJECT', 'PROVIDER_STREAM_INCOMPLETE', 'PROVIDER_STREAM_INVALID'} else
        "模型响应超时，未生成可交付答案。" if "TIMEOUT" in code else
        "模型输出被安全或证据检查拒绝，未作为业务答案交付。" if any(t in code for t in ("REJECTED", "INVALID", "SUPPORT", "FORBIDDEN")) else
        "本次处理未完成，请核对执行阶段和模型连接。")
    category = "output_limit" if truncated else "timeout" if "TIMEOUT" in code else "connection" if transport else "provider" if code in service_messages else (
        "output_format" if "SCHEMA" in code or code in {"PROVIDER_INVALID_JSON_OBJECT", "PROVIDER_INVALID_RESPONSE"} else "validation")
    next_step = ("可手动重试；已核验的问题研判将复用，来源和权限会重新检查。系统不会自动重复计费调用。" if transport else
        "请核对执行记录中的时间与输出预算后再重试；系统不会无限等待或自动重复调用。" if truncated or "TIMEOUT" in code else
        "保留任务记录并修正具体原因后再重试；不会自动重复调用模型。")
    return {"code": code, "message": message, "category": category,
        "phase": detail.get("phase") if detail.get("phase") in {"planning", "synthesis"} else "unknown",
        "schema_errors": schema_errors, "retryable": False,
        "next_step": next_step}


def run_dict(ctx, run):
    from .projection_read import projection_read
    with projection_read(ctx.db):
        return _run_projection(ctx, run)


def _run_projection(ctx, run):
    job = ctx.db.scalars(select(m.Job).where(m.Job.run_id == run.id, m.Job.kind == "ANSWER")
        .order_by(m.Job.created_at)).first()
    if not job:
        fail(409, "RUN_JOB_MISSING", "咨询任务记录不完整")
    answer, invalidated = run.response if run.state == "COMPLETED" else None, bool(run.invalidated_at)
    fresh_records = None
    try:
        ids = run_sources_readable(ctx, run)
        if (run.request or {}).get("answer_scope", "formal") == "reference":
            from .reference_evidence import evidence_signature, reference_evidence
            thread = ctx.db.get(m.ConsultationThread, run.thread_id)
            snapshots = run.evidence_snapshot or []
            fresh_records = reference_evidence(ctx.db, ctx.user, thread.space_id,
                (run.request or {}).get("context", {}), version_ids={e["version_id"] for e in snapshots},
                **({"reading": True} if (run.model_snapshot or {}).get("answer_engine") == "wiki_reader" else {}))
            fresh = {evidence_signature(e) for e in fresh_records}
            if any(evidence_signature(e) not in fresh for e in snapshots):
                invalidated = True
        else:
            for version_id in ids:
                v = ctx.db.get(m.ResourceVersion, version_id)
                r = ctx.db.get(m.Resource, v.resource_id)
                selected = select_applicable_version(ctx.db, ctx.user, r, (run.request or {}).get("context", {}))
                if r.suspended or not selected or selected.id != version_id:
                    invalidated = True
    except APIError:
        answer, invalidated = None, True
    if answer is not None:
        try:
            ctx.request.app.state.answer_validator.validate(answer)
            # Human review is a separate audited snapshot, never inferred from a closed case.
            if answer.get("review_status") == "EXPERT_REVIEWED":
                verified = ctx.db.scalars(select(m.AuditEvent).where(m.AuditEvent.object_id == run.id,
                    m.AuditEvent.action == "answer.expert_reviewed", m.AuditEvent.outcome == "SUCCESS")).all()
                if not any((e.details or {}).get("response_sha256") == digest(answer) for e in verified):
                    answer = {**answer, "review_status": "REQUIRES_EXPERT"}
        except (ValidationError, KeyError, TypeError, ValueError):
            answer, invalidated = None, True
    diagnostic = None
    if not invalidated and answer is not None:
        from .answer_diagnostics import explain_empty_answer
        diagnostic = explain_empty_answer(ctx, run)
        if answer.get("format") == "wiki_markdown" and any(not row.get("evidence_id") for row in run.evidence_snapshot or []):
            # The first reader prototype did not freeze E identifiers. Never
            # rebuild those historical IDs from a later changing global catalog.
            answer = {**answer, "quality_warnings": [*(answer.get("quality_warnings") or []), {
                "code": "HISTORICAL_CITATION_MAP_INCOMPLETE",
                "message": "此历史运行未冻结完整引用编号映射，仅保留当时已成功绑定的来源；未绑定的范围引用不能据当前目录猜补。正文保留可读。"}]}
        elif answer.get("format") == "wiki_markdown" and fresh_records is not None:
            # Re-render links from THIS run's frozen IDs, not today's catalog.
            # No model call, record mutation or invented generation timestamp.
            from .wiki_answer_content import build_narrative_answer
            frozen_ids = {(row["version_id"], row["block_id"]): row["evidence_id"] for row in run.evidence_snapshot or []}
            records = [{**row, "evidence_id": frozen_ids[(row["version_id"], row["block_id"])]}
                for row in fresh_records if (row["version_id"], row["block_id"]) in frozen_ids]
            rendered = build_narrative_answer((run.request or {}).get("question", ""), answer["mode"],
                (run.request or {}).get("context", {}), records, answer["narrative_markdown"], run.id)
            retained_codes = {"SENSITIVE_INFORMATION_REDACTED", "PRIVATE_REASONING_REMOVED", "SYSTEM_ACTION_CLAIM_REMOVED", "MODEL_OUTPUT_INCOMPLETE", "PRIMARY_RULE_CITATION_MISSING", "SOURCE_AUTHORITY_COVERAGE_GAP", "SOURCE_CONTEXT_GAPS", "DOMAIN_CORE_CITATION_MISSING", "READING_COVERAGE_GAPS"}
            warnings = {warning["code"]: warning for warning in rendered["quality_warnings"]}
            warnings.update({warning["code"]: warning for warning in answer.get("quality_warnings", []) if warning["code"] in retained_codes})
            answer = {**answer, "citations": rendered["citations"], "grounding_status": rendered["grounding_status"],
                "quality_warnings": list(warnings.values())}
        from .source_authority import historical_warning
        thread = ctx.db.get(m.ConsultationThread, run.thread_id)
        warning = historical_warning(ctx.db, ctx.user, thread.space_id, (run.request or {}).get("context", {}),
            answer.get("citations", []), (run.model_snapshot or {}).get("source_authority_stamp"))
        if warning:
            answer = {**answer, "quality_warnings": [*(answer.get("quality_warnings") or []), warning]}
    from .answer_preview import readable_preview
    preview = readable_preview(ctx, run, job, invalidated=invalidated, fresh_records=fresh_records)
    return {"id": run.id, "thread_id": run.thread_id, "job_id": job.id, "state": run.state,
        "phase": job.stage, "attempt": job.attempts,
        **({"question_analysis": run.policy_snapshot["question_analysis"]} if not invalidated and
            (run.policy_snapshot or {}).get("question_analysis", {}).get("source") == "model_prior_knowledge_unverified" else {}),
        **({"failure_diagnostic": _failure_diagnostic(run)} if not invalidated and run.state == "FAILED" else {}),
        "answer_scope": (run.request or {}).get("answer_scope", "formal"),
        "answer_scope_origin": (run.request or {}).get("answer_scope_origin", "historical_unknown"),
        **({"retrieval_selection": run.request["retrieval_selection"]} if (run.request or {}).get("retrieval_selection") is not None else {}),
        **({"evidence_diagnostic": diagnostic} if diagnostic is not None else {}),
        "invalidated": invalidated, "answer": answer, "error_code": run.error_code,
        "question": (run.request or {}).get("question", ""), "context": (run.request or {}).get("context", {}),
        "created_at": primitive(run.created_at), "model_snapshot": {
            **{key: value for key, value in (run.model_snapshot or {}).items() if key != "public_preview"
                and (not invalidated or key not in {"wiki_reading", "reading_progress", "hybrid_retrieval", "source_reading_plan", "primary_source_coverage", "query_path", "citation_integrity", "source_authority_coverage", "source_authority_stamp", "context_completion"})},
            **({"public_preview": preview} if preview is not None else {})}}


def threads(ctx):
    if ctx.operation == "createThread":
        space_access(ctx.db, ctx.user, ctx.data["space_id"])
        t = m.ConsultationThread(id=uid(), owner_id=ctx.user.id, **ctx.data)
        ctx.db.add(t)
        ctx.db.flush()
        audit(ctx, "thread.created", t)
        return Result(thread_dict(t), 201)
    values = [t for t in ctx.db.scalars(select(m.ConsultationThread).where(
        m.ConsultationThread.owner_id == ctx.user.id, m.ConsultationThread.deleted_at.is_(None)))
        if roles(ctx.db, ctx.user, t.space_id)]
    page, nxt = paginate(values, ctx.query)
    return Result({"items": [thread_dict(t) for t in page], "next_cursor": nxt})


def get_thread(ctx):
    t = thread_access(ctx)
    runs = ctx.db.scalars(select(m.ConsultationRun).where(m.ConsultationRun.thread_id == t.id)).all()
    page, nxt = paginate(runs, ctx.query)
    return Result({"thread": thread_dict(t), "items": [run_dict(ctx, r) for r in page], "next_cursor": nxt})


def delete_thread(ctx):
    t = thread_access(ctx)
    t.deleted_at = now()
    for r in ctx.db.scalars(select(m.ConsultationRun).where(m.ConsultationRun.thread_id == t.id)):
        from .answer_preview import previews
        previews.discard(r.id)
        for j in ctx.db.scalars(select(m.Job).where(m.Job.run_id == r.id, m.Job.state.in_(["QUEUED", "RUNNING"]))):
            j.cancel_requested = True
    audit(ctx, "thread.deleted", t)
    return Result(status=204)


def ask(ctx):
    t = thread_access(ctx)
    parent_id = ctx.data.get("parent_run_id")
    strategy = ctx.data.get("reasoning_strategy", "model_first" if ctx.data.get("model_selection") or ctx.data.get("require_model") else "evidence_first")
    if parent_id:
        parent = run_access(ctx, parent_id)
        if parent.thread_id != t.id:
            fail(404, "NOT_FOUND", "父轮次不属于本人的当前会话")
        if strategy != "model_first":
            run_sources_readable(ctx, parent)
    for version_id in ctx.data.get("attachment_version_ids", []) if strategy != "model_first" else []:
        v = version_access(ctx.db, ctx.user, version_id)
        blob = ctx.db.get(m.Blob, v.source_blob_id) if v.source_blob_id else None
        if not blob or blob.scan_state != "CLEAN":
            fail(409, "ATTACHMENT_NOT_CLEAN", "咨询附件必须已通过扫描")
    context = dict(ctx.data["context"])
    # A prior immutable request supplies facts; newly supplied facts win without changing that request.
    if parent_id:
        context = {**parent.request.get("context", {}), **context}
    answer_scope = ctx.data.get("answer_scope", (parent.request or {}).get("answer_scope", "formal") if parent_id else "formal")
    if parent_id and answer_scope != (parent.request or {}).get("answer_scope", "formal"):
        fail(409, "ANSWER_SCOPE_MISMATCH", "补充事实不能改变原轮次的答疑范围，请新建问题")
    scope_origin = "explicit" if "answer_scope" in ctx.data else "inherited" if parent_id else "legacy_default"
    payload = {**ctx.data, "context": context, "answer_scope": answer_scope, "answer_scope_origin": scope_origin,
        "reasoning_strategy": strategy}
    from .vector_indexing import freeze_retrieval_selection, retrieval_registry_context
    retrieval_choice = ctx.data.get("retrieval_selection")
    if retrieval_choice is not None and (not isinstance(retrieval_choice, dict)
            or set(retrieval_choice) - {"profile_id", "fingerprint"}):
        fail(422, "RETRIEVAL_SELECTION_INVALID", "检索选择仅接受方案标识和可选指纹")
    inherited_retrieval = False
    if retrieval_choice is None and parent_id:
        parent_request_binding = (parent.request or {}).get("retrieval_selection")
        parent_model_binding = (parent.model_snapshot or {}).get("retrieval_selection")
        if parent_request_binding is not None and parent_model_binding is not None and parent_request_binding != parent_model_binding:
            fail(409, "RETRIEVAL_BINDING_CHANGED", "父轮检索绑定不一致，请明确选择检索方案，不猜测继承")
        retrieval_choice = parent_request_binding if parent_request_binding is not None else parent_model_binding
        inherited_retrieval = retrieval_choice is not None
    with retrieval_registry_context(ctx) as registry:
        retrieval_binding = freeze_retrieval_selection(registry, retrieval_choice, require_frozen=inherited_retrieval)
    if retrieval_binding is not None:
        payload["retrieval_selection"] = retrieval_binding
    model_snapshot = {}
    if selection := ctx.data.get("model_selection"):
        from .providers import ProviderError, public_snapshot, resolve_connection
        try:
            resolved = resolve_connection(ctx.db, ctx.user, t.space_id, selection["connection_id"],
                selection["model_id"], ctx.settings, require_transfer=True)
        except ProviderError as exc:
            fail(409, exc.code, "所选模型连接不可用，请核对配置、启用状态和资料传输授权")
        model_snapshot = public_snapshot(resolved)
    if retrieval_binding is not None:
        model_snapshot["retrieval_selection"] = dict(retrieval_binding)
    policy = ctx.db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name == "model-policy")).first()
    r = m.ConsultationRun(id=uid(), thread_id=t.id, parent_run_id=parent_id, state="QUEUED",
        mode=ctx.data["mode"], request=payload, evidence_snapshot=[], policy_snapshot=dict(policy.config) if policy else {},
        model_snapshot=model_snapshot)
    ctx.db.add(r)
    ctx.db.flush()
    create_job(ctx, "ANSWER", {"run_id": r.id,
        **({"manual_retry_only": True} if ctx.data.get("automatic_retry") is False else {})}, run_id=r.id)
    audit(ctx, "run.created", r)
    return Result(run_dict(ctx, r), 202)


def get_run(ctx):
    return Result(run_dict(ctx, run_access(ctx)))


def run_progress(ctx):
    from .answer_preview import readable_preview
    from .answer_progress import progress_projection
    from .projection_read import projection_read
    with projection_read(ctx.db):
        r = run_access(ctx)
        job = ctx.db.scalars(select(m.Job).where(m.Job.run_id == r.id, m.Job.kind == "ANSWER")
            .order_by(m.Job.created_at)).first()
        if not job:
            fail(409, "RUN_JOB_MISSING", "咨询任务记录不完整")
        invalidated = bool(r.invalidated_at)
        try:
            run_sources_readable(ctx, r)
        except APIError:
            invalidated = True
        # No answer validation, citation re-render or huge diagnostic arrays on
        # status polls. Public text still gets the existing full source fence;
        # when there is no text, only status metadata crosses this endpoint.
        preview = readable_preview(ctx, r, job, invalidated=invalidated)
        data = progress_projection(r, job, invalidated=invalidated, preview=preview)
    event = "completed" if r.state == "COMPLETED" else ("error" if r.state in {"FAILED", "CANCELLED"} else "stage")
    event_id = f"{job.attempts}:{r.state}:{job.stage}:{digest(data)[:24]}"
    # Bounded SSE snapshot; reconnect returns current state, GET run is the authoritative fallback.
    body = "retry: 1500\n"
    if ctx.request.headers.get("last-event-id") != event_id:
        body += f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data)}\n\n"
    else:
        body += ": unchanged; full run is fetched on a terminal event\n\n"
    return Response(body, media_type="text/event-stream", headers={"Cache-Control": "private, no-store",
        "X-Accel-Buffering": "no"})


def case_access(ctx, case_id=None):
    c = ctx.db.get(m.IssueCase, case_id or ctx.id)
    if not c or not roles(ctx.db, ctx.user, c.space_id):
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if ctx.user.id not in {c.creator_id, c.assignee_id} and "admin" not in roles(ctx.db, ctx.user, c.space_id):
        fail(404, "NOT_FOUND", "对象不存在或不可访问")
    if c.run_id:
        run = ctx.db.get(m.ConsultationRun, c.run_id)
        run_sources_readable(ctx, run)
    return c


def case_dict(c):
    return {k: getattr(c, k) for k in ("id", "space_id", "title", "description", "state", "resolution", "assignee_id", "revision")}


def new_case(ctx, payload):
    space_access(ctx.db, ctx.user, payload["space_id"])
    if payload.get("run_id"):
        run = run_access(ctx, payload["run_id"])
        thread = ctx.db.get(m.ConsultationThread, run.thread_id)
        if thread.space_id != payload["space_id"]:
            fail(422, "CASE_SPACE_MISMATCH", "问题单空间与咨询不一致")
        run_sources_readable(ctx, run)
    c = m.IssueCase(id=uid(), creator_id=ctx.user.id, state="OPEN", revision=1, resolution="", **payload)
    ctx.db.add(c)
    ctx.db.flush()
    audit(ctx, "case.created", c)
    return c


def feedback(ctx):
    run = run_access(ctx)
    run_sources_readable(ctx, run)
    feedback = m.Feedback(id=uid(), run_id=run.id, user_id=ctx.user.id, **ctx.data)
    ctx.db.add(feedback)
    case = None
    if ctx.data["kind"] in {"WRONG", "MISSING", "OUTDATED"}:
        case = ctx.db.scalars(select(m.IssueCase).where(m.IssueCase.run_id == run.id,
            m.IssueCase.creator_id == ctx.user.id, m.IssueCase.state.in_(["OPEN", "ASSIGNED"]))).first()
        if not case:
            thread = ctx.db.get(m.ConsultationThread, run.thread_id)
            case = new_case(ctx, {"space_id": thread.space_id, "run_id": run.id,
                "title": f"咨询反馈：{ctx.data['kind']}", "description": ctx.data["comment"] or "请复核本轮咨询的证据与结论"})
    audit(ctx, "feedback.created", feedback, {"kind": ctx.data["kind"]})
    return Result({"id": feedback.id, "case_id": case.id if case else None}, 201)


def cases(ctx):
    if ctx.operation == "createExpertCase":
        c = new_case(ctx, ctx.data)
        return tagged(case_dict(c), c, 201)
    visible = []
    for c in ctx.db.scalars(select(m.IssueCase)):
        try:
            case_access(ctx, c.id)
            visible.append(c)
        except APIError:
            continue
    page, nxt = paginate(visible, ctx.query)
    return Result({"items": [case_dict(c) for c in page], "next_cursor": nxt})


def get_case(ctx):
    c = case_access(ctx)
    return tagged(case_dict(c), c)


def update_case(ctx):
    c = case_access(ctx)
    require_etag(ctx, c)
    rr = roles(ctx.db, ctx.user, c.space_id)
    changes = dict(ctx.data)
    if "assignee_id" in changes:
        if ctx.user.id != c.creator_id and "admin" not in rr:
            fail(403, "CASE_ASSIGN_FORBIDDEN", "仅创建人或授权管理人员可指派")
        if changes["assignee_id"]:
            expert = ctx.db.get(m.User, changes["assignee_id"])
            if "reviewer" not in roles(ctx.db, expert, c.space_id):
                fail(422, "EXPERT_ROLE_REQUIRED", "指派对象需具备复核角色")
            if c.run_id:
                run_sources_readable(ctx, ctx.db.get(m.ConsultationRun, c.run_id), expert)
            changes.setdefault("state", "ASSIGNED")
        elif c.state == "ASSIGNED":
            changes.setdefault("state", "OPEN")
    if "resolution" in changes or changes.get("state") == "RESOLVED":
        if ctx.user.id != c.assignee_id or "reviewer" not in rr:
            fail(403, "EXPERT_REQUIRED", "只有被指派复核者可提交专业解决意见")
        if not changes.get("resolution", c.resolution).strip():
            fail(422, "RESOLUTION_REQUIRED", "解决意见不能为空")
    target = changes.get("state", c.state)
    allowed = {"OPEN": {"OPEN", "ASSIGNED", "CLOSED"}, "ASSIGNED": {"OPEN", "ASSIGNED", "RESOLVED", "CLOSED"},
        "RESOLVED": {"RESOLVED", "CLOSED", "OPEN"}, "CLOSED": {"CLOSED", "OPEN"}}
    if target not in allowed[c.state]:
        fail(409, "CASE_STATE_CONFLICT", "问题单状态转换无效")
    if target == "ASSIGNED" and not changes.get("assignee_id", c.assignee_id):
        fail(422, "ASSIGNEE_REQUIRED", "已指派状态需要明确处理人")
    bump(ctx.db, c, **changes)
    audit(ctx, "case.updated", c, {"state": c.state})
    return tagged(case_dict(c), c)


HANDLERS = {"searchKnowledge": search, "listThreads": threads, "createThread": threads, "getThread": get_thread,
    "deleteThread": delete_thread, "askOrContinue": ask, "getRun": get_run, "getRunProgress": run_progress,
    "submitFeedback": feedback, "listCases": cases, "createExpertCase": cases, "getCase": get_case,
    "resolveOrAssignCase": update_case}
