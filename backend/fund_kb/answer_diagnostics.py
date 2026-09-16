"""Read-time, ACL-filtered reasons for a zero-evidence, non-model answer.

This explains eligibility; it never promotes a source or changes an old answer.
Only aggregate counts and fixed text leave this module, never source identities.
"""
from collections import Counter
from datetime import datetime

from sqlalchemy import select

from . import models as m
from . import services as svc

LABELS = {
    "LEGAL_STATUS_UNDETERMINED": "效力状态尚未判定（UNKNOWN），不满足正式证据要求",
    "LEGAL_SCOPE_PARTIAL": "效力或适用范围尚需核对（PARTIAL）",
    "NOT_PUBLISHED": "当前版本尚未达到本轮时点的核验发布要求",
    "UNVERIFIED_SOURCE": "来源核验尚未完成",
    "UNVERIFIED_LINEAGE": "知识仍保留待核验来源链，不能自动作为正式依据",
    "CONTEXT_REQUIRED": "缺少判断适用范围所需的业务事实",
    "OUTSIDE_VALIDITY": "资料不适用于本轮业务日期或场景",
    "SOURCE_NOT_CLEAN": "原件尚未通过扫描或当前缺少可核对原件",
    "SNAPSHOT_INVALID": "快照或依赖未通过正式证据资格检查",
    "RESOURCE_SUSPENDED": "资料当前已停用",
}


def _reason(db, user, resource, version, context):
    """First applicable reason only; these mutually-exclusive counts are additive."""
    if resource.suspended:
        return "RESOURCE_SUSPENDED"
    cutoff = context.get("knowledge_cutoff")
    if isinstance(cutoff, str):
        cutoff = datetime.fromisoformat(cutoff)
    if version.state != "APPROVED" or not svc.released(db, version.id, cutoff):
        return "NOT_PUBLISHED"
    day = svc.effective_date(context)
    if (version.valid_from and day < version.valid_from) or (version.valid_to and day >= version.valid_to):
        return "OUTSIDE_VALIDITY"
    applicable = svc.match_applicability(version.applicability or {}, context)
    if applicable is False:
        return "OUTSIDE_VALIDITY"
    if version.legal_status == "UNKNOWN":
        return "LEGAL_STATUS_UNDETERMINED"
    if version.legal_status == "PARTIAL":
        return "LEGAL_SCOPE_PARTIAL"
    if ((version.legal_status == "FUTURE" and version.valid_from is None)
            or (version.legal_status == "REPEALED" and version.valid_to is None)):
        return "OUTSIDE_VALIDITY"
    if version.knowledge_type == "source" and not version.source_verified:
        return "UNVERIFIED_SOURCE"
    from .wiki import unverified_wiki
    if unverified_wiki(db, resource):
        return "UNVERIFIED_LINEAGE"
    if applicable is None:
        return "CONTEXT_REQUIRED"
    blob = db.get(m.Blob, version.source_blob_id) if version.source_blob_id else None
    if (resource.kind == "document" and not blob) or (blob and blob.scan_state != "CLEAN"):
        return "SOURCE_NOT_CLEAN"
    return "SNAPSHOT_INVALID"


def formal_eligibility_summary(db, user, space_id, context=None):
    """Recheck readable resources/versions now; hidden objects do not enter counts."""
    context = context or {}
    svc.space_access(db, user, space_id)
    visible = eligible = 0
    reasons = Counter()
    resources = db.scalars(select(m.Resource).where(m.Resource.space_id == space_id,
        m.Resource.deleted_at.is_(None)).order_by(m.Resource.id))
    for resource in resources:
        try:
            # select_applicable_version starts with the full resource/lineage
            # access check; repeating it here rescans every Wiki ancestry.
            selected = svc.select_applicable_version(db, user, resource, context)
            if selected and svc.evidence_version_eligible(db, user, selected, context):
                visible += 1
                eligible += 1
                continue
            # The active version is the public latest choice. Do not enumerate a
            # newer private draft merely because the resource itself is readable.
            release = db.get(m.Release, resource.active_release_id) if resource.active_release_id else None
            version = db.get(m.ResourceVersion, release.version_id) if release else db.scalar(
                select(m.ResourceVersion).where(m.ResourceVersion.resource_id == resource.id)
                .order_by(m.ResourceVersion.version_no.desc()).limit(1))
            if version is None:
                continue
            svc.version_access(db, user, version)
            code = _reason(db, user, resource, version, context)
        except svc.APIError:
            # Permission/lineage failures must not reveal even an excluded count.
            continue
        visible += 1
        reasons[code] += 1
    return {"visible_resource_count": visible, "eligible_resource_count": eligible,
        "excluded_resource_count": visible - eligible,
        "reasons": [{"code": code, "label": LABELS[code], "count": count}
                    for code, count in sorted(reasons.items())]}


def explain_empty_answer(ctx, run):
    answer = run.response or {}
    if (run.state != "COMPLETED" or answer.get("status") != "INSUFFICIENT_EVIDENCE"
            or (run.model_snapshot or {}).get("model_invoked") is not False
            or run.evidence_snapshot or answer.get("citations")):
        return None
    scope = (run.request or {}).get("answer_scope", "formal")
    base = {"scope": scope, "basis": "current_access", "observed_at": svc.primitive(svc.now()),
        "note": "以下按当前权限与资料状态检查，不是历史轮次的证据快照，也没有重新执行该问题。"}
    if scope == "reference":
        return {**base, "message": "资料辅助范围未取得可用引文，本轮没有进入模型生成。",
            "reasons": [{"code": "NO_MATCHING_REFERENCE_EVIDENCE",
                         "label": "尚未匹配到同时通过读取权限、原件扫描、快照与引用链检查的相关内容"}],
            "next_step": "请核对本题的来源与定位内容；资料辅助不自动放宽读取权限或跳过快照检查。"}
    thread = ctx.db.get(m.ConsultationThread, run.thread_id)
    context = (run.request or {}).get("context", {})
    # ctx is specific to this HTTP request. Never persist these counts in the
    # immutable run, an idempotency record, a connection pool or a global cache.
    cache = ctx.__dict__.setdefault("_answer_diagnostic_cache", {})
    key = (ctx.user.id, thread.space_id, svc.digest(context))
    if key not in cache:
        cache[key] = formal_eligibility_summary(ctx.db, ctx.user, thread.space_id, context)
    summary = cache[key]
    if not summary["visible_resource_count"]:
        message = "本轮没有进入模型生成；当前未找到可读取并核对的来源。"
        reasons = [{"code": "NO_READABLE_SOURCES", "label": "当前范围没有可读取并核对的来源"}]
    elif not summary["eligible_resource_count"]:
        message = "资料存在，但当前没有满足正式答疑资格的来源；本轮未取得引文，未进入模型生成。"
        reasons = summary["reasons"]
    else:
        message = "当前存在符合正式资格的来源，但该轮未取得相关可用引文，未进入模型生成。"
        reasons = [*summary["reasons"], {"code": "NO_MATCHING_ELIGIBLE_EVIDENCE",
            "label": "当前资格统计不等于与问题相关的检索结果，请核对检索范围和业务事实"}]
    return {**base, **summary, "message": message, "reasons": reasons,
        "next_step": "可明确选择资料辅助答疑进行参考解读，或按资料类别核对效力与来源资格；不会自动改变发布、效力或权限。"}
