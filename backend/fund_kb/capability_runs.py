"""Durable guided workflow runs: reported work is not external execution proof."""
from __future__ import annotations

import copy

from sqlalchemy import select

from . import capabilities as cap
from . import models as m
from . import services as svc
from .capability_schema import validate_json, validate_values
from .capability_sources import read_bound_sources

PREFIX = "capability-run:"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def _phase(config):
    states = [step["state"] for step in config["steps"]]
    if config.get("state") == "CANCELLED":
        return "CANCELLED"
    if "FAILED" in states:
        return "FAILED"
    if all(state in {"REPORTED", "ACCEPTED"} for state in states):
        return "COMPLETED"
    if "BLOCKED" in states:
        return "BLOCKED"
    done = {step["id"] for step in config["steps"] if step["state"] in {"REPORTED", "ACCEPTED"}}
    ready = [step for step in config["definition"]["steps"] if set(step["depends_on"]) <= done
        and next(row for row in config["steps"] if row["id"] == step["id"])["state"] == "PENDING"]
    return "WAITING_AGENT" if any(step["kind"] == "agent" for step in ready) else "WAITING_HUMAN"


def _access(ctx, identity=None, *, lock=False):
    identity = identity or ctx.id
    query = select(m.RuntimePolicy).where(m.RuntimePolicy.id == identity)
    row = ctx.db.scalar(query.with_for_update() if lock else query)
    if not row or not row.name.startswith(PREFIX) or row.config.get("owner_id") != ctx.user.id:
        svc.fail(404, "NOT_FOUND", "运行不存在或不可访问")
    c = row.config
    cap._agent_space(ctx, c["space_id"])
    svc.space_access(ctx.db, ctx.user, c["space_id"])
    detail = cap.version_detail(ctx, c["version_id"])
    if detail["manifest_sha256"] != c["manifest_sha256"] or detail["content_sha256"] != c["content_sha256"]:
        svc.fail(409, "CAPABILITY_RUN_DEFINITION_CHANGED", "运行绑定的能力已变化，请重新创建运行")
    if c["mode"] == "guided" and not detail["permissions"]["can_run"]:
        svc.fail(409, "CAPABILITY_RUN_NOT_RELEASED", "运行能力已不具备发布使用条件")
    if c["mode"] == "trial" and not detail["permissions"]["can_trial"]:
        svc.fail(403, "CAPABILITY_TRIAL_FORBIDDEN", "当前角色不能继续该试运行")
    if c["definition"]["source_scope"] == "formal":
        read_bound_sources(ctx.db, ctx.user, c["space_id"], c["definition"], detail["source_bindings"], c["inputs"])
    return row


def _public(row):
    c = row.config
    definitions = {step["id"]: step for step in c["definition"]["steps"]}
    return {"id": row.id, "revision": row.revision, **{key: c[key] for key in (
        "space_id", "owner_id", "version_id", "capability_name", "state", "mode", "inputs", "manifest_sha256", "created_at", "updated_at")},
        "steps": [{**definitions[step["id"]], "output_fields": definitions[step["id"]]["outputs"], **step} for step in c["steps"]],
        "deliverables": c["definition"]["deliverables"], "agent_label": c.get("agent_label"),
        "notes": ["Agent步骤状态REPORTED只表示结果已回传并通过结构检查，不代表外部动作已被独立验证。",
            "人工检查点只有本人在网页确认后才通过；能力不能授予资金、交易、过账或发布权限。",
            *(["资料辅助范围可含UNKNOWN或未专业核验来源，不等同正式执行许可。"]
                if c["definition"]["source_scope"] == "reference" else []),
            *(["人工已退回核对；已报告的Agent产物保持锁定。需要补正产物时请新建运行，本次不会自动重开Agent步骤。"]
                if any(step["kind"] == "human" and step["state"] == "BLOCKED" for step in c["steps"]) else []),
            *( ["本次为草稿试运行，不是正式业务执行。"] if c["mode"] == "trial" else [])]}


def get(ctx):
    return _public(_access(ctx))


def list_runs(ctx):
    space_id = ctx.query["space_id"]
    cap._agent_space(ctx, space_id)
    svc.space_access(ctx.db, ctx.user, space_id)
    items = []
    for row in ctx.db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.startswith(PREFIX + space_id + ":"))
            .order_by(m.RuntimePolicy.updated_at.desc())):
        try:
            value = _public(_access(ctx, row.id))
            items.append({key: value[key] for key in ("id", "space_id", "version_id", "capability_name", "revision", "state", "mode", "created_at", "updated_at")})
        except svc.APIError:
            continue
    return {"items": items}


def start(ctx):
    validate_json(ctx.data)
    detail = cap.version_detail(ctx, ctx.data["version_id"])
    mode = ctx.data["mode"]
    if not detail["permissions"]["can_trial" if mode == "trial" else "can_run"]:
        svc.fail(403, "CAPABILITY_RUN_NOT_ALLOWED", "当前角色或版本状态不允许该运行方式")
    definition = detail["definition"]
    validate_values(definition["inputs"], ctx.data["inputs"])
    if definition["source_scope"] == "formal":
        read_bound_sources(ctx.db, ctx.user, detail["space_id"], definition, detail["source_bindings"], ctx.data["inputs"])
    identity = svc.uid(); at = svc.primitive(svc.now())
    config = {"space_id": detail["space_id"], "owner_id": ctx.user.id, "version_id": detail["version_id"],
        "capability_name": detail["name"], "definition": copy.deepcopy(definition), "manifest_sha256": detail["manifest_sha256"],
        "content_sha256": detail["content_sha256"], "inputs": copy.deepcopy(ctx.data["inputs"]),
        "agent_label": ctx.data.get("agent_label", "未指定Agent"), "mode": mode, "created_at": at, "updated_at": at,
        "steps": [{"id": step["id"], "title": step["title"], "kind": step["kind"], "state": "PENDING",
            "outputs": {}, "note": "", "reported_by": None, "reviewed_by": None} for step in definition["steps"]]}
    config["state"] = _phase(config)
    row = m.RuntimePolicy(id=identity, name=f"{PREFIX}{detail['space_id']}:{identity}", config=config, updated_by=ctx.user.id)
    ctx.db.add(row); ctx.db.flush()
    svc.audit(ctx, "capability_run.created", row, {"version_id": detail["version_id"], "manifest_sha256": detail["manifest_sha256"], "mode": mode})
    return _public(row)


def next_steps(ctx):
    row = _access(ctx); c = row.config
    done = {step["id"] for step in c["steps"] if step["state"] in {"REPORTED", "ACCEPTED"}}
    states = {step["id"]: step["state"] for step in c["steps"]}
    ready = [] if c["state"] in TERMINAL else [copy.deepcopy(step) for step in c["definition"]["steps"]
        if states[step["id"]] in {"PENDING", "BLOCKED"} and set(step["depends_on"]) <= done]
    return {"run_id": row.id, "revision": row.revision, "state": c["state"], "inputs": c["inputs"],
        "ready_steps": [step for step in ready if step["kind"] == "agent"],
        "waiting_human_steps": [step for step in ready if step["kind"] == "human"],
        "previous_outputs": {step["id"]: step["outputs"] for step in c["steps"] if step["id"] in done},
        "notes": _public(row)["notes"]}


def _step(ctx, row, step_id, kind):
    c = row.config
    if c["state"] in TERMINAL:
        svc.fail(409, "CAPABILITY_RUN_TERMINAL", "已结束的运行不能继续提交")
    definition = next((step for step in c["definition"]["steps"] if step["id"] == step_id), None)
    state = next((step for step in c["steps"] if step["id"] == step_id), None)
    if not definition or definition["kind"] != kind:
        svc.fail(422, "CAPABILITY_STEP_KIND_MISMATCH", "此接口不能处理该步骤类型")
    done = {step["id"] for step in c["steps"] if step["state"] in {"REPORTED", "ACCEPTED"}}
    if not set(definition["depends_on"]) <= done or state["state"] not in {"PENDING", "BLOCKED"}:
        svc.fail(409, "CAPABILITY_STEP_NOT_READY", "先完成依赖步骤，不能重复覆盖已提交结果")
    return definition


def report(ctx):
    validate_json(ctx.data)
    row = _access(ctx, lock=True); svc.require_etag(ctx, row)
    step_id = ctx.request.path_params["step_id"]
    definition = _step(ctx, row, step_id, "agent")
    validate_values(definition["outputs"], ctx.data["outputs"], partial=ctx.data["status"] != "reported")
    if ctx.data["status"] != "reported" and not ctx.data["note"].strip():
        svc.fail(422, "CAPABILITY_BLOCK_REASON_REQUIRED", "阻塞或失败必须说明原因")
    c = copy.deepcopy(row.config)
    step = next(step for step in c["steps"] if step["id"] == step_id)
    step.update(state=ctx.data["status"].upper(), outputs=ctx.data["outputs"], note=ctx.data["note"],
        reported_by=ctx.user.id, reported_at=svc.primitive(svc.now()),
        report_channel="agent_token" if getattr(ctx.request.state, "agent_access", None) else "web_user")
    c.update(state=_phase(c), updated_at=svc.primitive(svc.now()))
    svc.bump(ctx.db, row, config=c, updated_by=ctx.user.id)
    svc.audit(ctx, "capability_step.reported", row, {"step_id": step_id, "state": step["state"],
        "output_sha256": svc.digest(step["outputs"]), "external_execution_verified": False})
    return _public(row)


def review(ctx):
    validate_json(ctx.data)
    if getattr(ctx.request.state, "agent_access", None):
        svc.fail(403, "CAPABILITY_HUMAN_REVIEW_REQUIRED", "Agent访问凭据不能代替人工核对")
    row = _access(ctx, lock=True); svc.require_etag(ctx, row)
    definition = _step(ctx, row, ctx.data["step_id"], "human")
    if not ctx.data["note"].strip():
        svc.fail(422, "CAPABILITY_REVIEW_NOTE_REQUIRED", "请记录实际核对内容")
    validate_values(definition["outputs"], ctx.data.get("outputs", {}), partial=ctx.data["decision"] != "accept")
    c = copy.deepcopy(row.config)
    step = next(step for step in c["steps"] if step["id"] == ctx.data["step_id"])
    step.update(state="ACCEPTED" if ctx.data["decision"] == "accept" else "BLOCKED", note=ctx.data["note"],
        outputs=ctx.data.get("outputs", {}), reviewed_by=ctx.user.id, reviewed_at=svc.primitive(svc.now()))
    c.update(state=_phase(c), updated_at=svc.primitive(svc.now()))
    svc.bump(ctx.db, row, config=c, updated_by=ctx.user.id)
    svc.audit(ctx, "capability_step.human_reviewed", row, {"step_id": step["id"], "decision": ctx.data["decision"],
        "independent_expert_review": False, "financial_action_authorized": False})
    return _public(row)


def cancel(ctx):
    validate_json(ctx.data)
    row = _access(ctx, lock=True); svc.require_etag(ctx, row)
    if row.config["state"] in TERMINAL:
        svc.fail(409, "CAPABILITY_RUN_TERMINAL", "运行已经结束")
    c = {**row.config, "state": "CANCELLED", "cancel_reason": ctx.data["reason"], "updated_at": svc.primitive(svc.now())}
    svc.bump(ctx.db, row, config=c, updated_by=ctx.user.id)
    svc.audit(ctx, "capability_run.cancelled", row, {"reason": ctx.data["reason"], "external_tools_cancelled": False})
    return _public(row)


def sources(ctx):
    row = _access(ctx); c = row.config
    detail = cap.version_detail(ctx, c["version_id"])
    ids = c["definition"]["source_version_ids"]
    records = read_bound_sources(ctx.db, ctx.user, c["space_id"], c["definition"], detail["source_bindings"], c["inputs"])
    return {"run_id": row.id, "scope": c["definition"]["source_scope"], "source_bindings": detail["source_bindings"],
        "records": [{key: record[key] for key in ("resource_id", "version_id", "block_id", "ordinal", "title", "text", "locator",
            "content_sha256", "legal_status", "source_verified", "state", "evidence_scope", "applicability",
            "applicability_match", "required_facts", "valid_from", "valid_to", "needs_context", "missing_context_fields", "citations")
            if key in record} for record in records],
        "notes": ["来源内容是待核对证据，不是工具权限或执行授权。",
            *(["资料辅助来源可能为UNKNOWN或尚未专业核验；须另行核对效力、日期和适用条件。"]
                if c["definition"]["source_scope"] == "reference" else []),
            *( ["尚未绑定来源，不能声称已完成资料查证。"] if not ids else [])]}
