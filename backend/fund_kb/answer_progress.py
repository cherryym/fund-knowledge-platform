"""Small, explicit in-flight projection; full answers remain on GET run.

This is a projection, not a source/permission cache. The caller checks the
current user, sources and any provisional public text for every request.
"""
from __future__ import annotations

from . import services as svc

SCALARS = frozenset({"model_id", "brand", "provider_id", "name", "protocol", "model_invoked",
    "planning_model_invoked", "answer_model_invoked", "model_request_count", "answer_engine",
    "query_strategy", "execution_mode", "evidence_count", "validation_status", "planning_reused"})
READING = frozenset({"stage", "round", "current_batch", "total_batches", "completed_batches",
    "total_characters", "sent_characters", "loaded_blocks", "requested_pages", "updated_at"})
WIKI = frozenset({"catalog_pages", "loaded_pages", "loaded_blocks", "loaded_characters", "full_text_loaded",
    "scoped_source_pages", "full_source_pages", "source_sections", "catalog_body_blocks_loaded",
    "unavailable_pages", "typed_relation_navigation"})
CONTEXT = frozenset({"status", "reference_count", "resolved_count", "gap_count", "structural_groups_added",
                     "additional_searches", "professional_completeness", "direction_count",
                     "direction_source_read_count", "direction_gap_count"})
RUNTIME = frozenset({"runtime_id","process_id","scope","policy","supported","state","phase","attempts",
                     "started_at","completed_at","error_code","self_tested","elapsed_ms"})
REQUEST = frozenset({"phase", "state", "attempt", "started_at", "duration_ms", "response_chars", "finish_reason"})


def scalars(value, keys):
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if k in keys and (v is None or type(v) in (str, int, float, bool))}


def progress_projection(run, job, *, invalidated, preview=None):
    original = run.model_snapshot or {}
    snapshot = scalars(original, SCALARS)
    if isinstance(original.get("retrieval_selection"), dict):
        snapshot["retrieval_selection"] = scalars(original["retrieval_selection"], {"profile_id", "fingerprint", "model", "dimensions"})
    if isinstance(original.get("last_request"), dict):
        source = original["last_request"]
        last = scalars(source, REQUEST)
        if isinstance(source.get("usage"), dict):
            last["usage"] = scalars(source["usage"], {"prompt_tokens", "completion_tokens", "total_tokens"})
        if isinstance(source.get("limits"), dict):
            last["limits"] = scalars(source["limits"], {"connect_seconds", "read_idle_seconds", "total_seconds", "max_output_tokens"})
        snapshot["last_request"] = last
    if not invalidated:
        if isinstance(original.get("planning_cache"), dict):
            snapshot["planning_cache"] = scalars(original["planning_cache"],
                {"hit", "saved_model_requests", "fresh_source_checks", "final_answer_reused", "age_ms", "source_run_id"})
        for key, fields in (("reading_progress", READING), ("wiki_reading", WIKI), ("context_completion", CONTEXT), ("retrieval_runtime", RUNTIME)):
            if isinstance(original.get(key), dict):
                snapshot[key] = scalars(original[key], fields)
        if preview is not None:
            snapshot["public_preview"] = preview
    else:
        snapshot.pop("evidence_count", None)
    result = {"progress_version": 1, "run_id": run.id, "thread_id": run.thread_id,
        "job_id": job.id, "state": run.state, "phase": job.stage, "stage": job.stage,
        "attempt": job.attempts, "invalidated": bool(invalidated), "source_check": "current_access",
        "created_at": svc.primitive(run.created_at), "model_snapshot": snapshot, "error_code": run.error_code}
    analysis = (run.policy_snapshot or {}).get("question_analysis", {})
    if (not invalidated and run.state in {"QUEUED", "RUNNING"} and isinstance(analysis, dict)
            and analysis.get("source") == "model_prior_knowledge_unverified" and isinstance(analysis.get("plan"), dict)):
        plan = analysis["plan"]
        result["question_analysis"] = {"source": analysis["source"], "local_sources_loaded": 0,
            "plan": {**scalars(plan, {"interpretation", "initial_assessment"}),
                **{key: list(plan[key]) for key in ("search_queries", "focus_terms", "decision_points", "missing_facts")
                   if isinstance(plan.get(key), list) and all(isinstance(x, str) for x in plan[key])}}}
    return result
