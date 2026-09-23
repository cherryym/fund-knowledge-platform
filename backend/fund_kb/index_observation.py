"""Read-only index generation and owner-scoped rebuild receipts.

Counts remain scoped to the current authorized catalog. A successful historical
job cannot certify a missing/mixed/stale current projection. No model inference,
database writes, raw payloads, source text or other users' jobs are exposed.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from . import models as m, services as svc

_COUNTS = ("total_versions", "completed_versions", "indexed_versions", "skipped_versions",
           "empty_versions", "failed_versions", "indexed_blocks", "indexed_chunks", "cleanup_pending_versions")


def _count(value):
    return type(value) is int and value >= 0


def operation_summary(job):
    raw = job.result or {}
    result = {key: raw[key] for key in _COUNTS if _count(raw.get(key))}
    if isinstance(raw.get("phase"), str):
        result["phase"] = raw["phase"]
    required = ("total_versions", "completed_versions", "indexed_versions", "skipped_versions", "failed_versions")
    complete = (job.state == "SUCCEEDED" and all(key in result for key in required)
        and result["failed_versions"] == 0 and result["completed_versions"] == result["total_versions"]
        and result["indexed_versions"] + result["skipped_versions"] == result["total_versions"])
    return {"id": job.id, "kind": job.kind, "task": "VECTOR_INDEX", "state": job.state,
        "stage": job.stage, "attempts": job.attempts, "error_code": job.error_code, "result": result,
        "force": (job.payload or {}).get("force") is True, "counts_verified": complete,
        "created_at": svc.primitive(svc.aware(job.created_at)),
        "completed_at": svc.primitive(svc.aware(job.updated_at)), "record_scope": "current_owner"}


def recent_operations(db, user, space_id, selection):
    """Streaming, no arbitrary history cutoff; match the exact frozen profile."""
    latest = rebuild = None
    query = select(m.Job).where(m.Job.owner_id == user.id, m.Job.kind == "COMPILE",
        m.Job.state.in_(["SUCCEEDED", "FAILED", "CANCELLED"])).order_by(m.Job.updated_at.desc(), m.Job.id.desc())
    for job in db.scalars(query.execution_options(yield_per=64)):
        payload = job.payload or {}
        if (payload.get("task") != "VECTOR_INDEX" or payload.get("space_id") != space_id
                or payload.get("retrieval_selection") != selection):
            continue
        if latest is None:
            latest = job
        if payload.get("force") is True:
            rebuild = job
            break
    return latest, rebuild


def observe_index(db, user, space_id, vector, pages, ready, backend, selection):
    latest, rebuild = recent_operations(db, user, space_id, selection)
    fingerprint = vector.embedding.fingerprint if vector is not None else None
    generation = svc.digest(["authorized-index-snapshot-v1", fingerprint,
        sorted((vid, row["projection_id"]) for vid, row in ready.items())]) if ready else None
    dates = []
    for row in ready.values():
        try:
            parsed = datetime.fromisoformat(row["indexed_at"].replace("Z", "+00:00"))
            dates.append(svc.aware(parsed))
        except (KeyError, AttributeError, TypeError, ValueError):
            pass
    available = backend.get("available") is True
    state = ("UNAVAILABLE" if not available else "EMPTY_CATALOG" if not pages
        else "CURRENT_CATALOG_INDEXED" if len(ready) == len(pages) else "SYNC_REQUIRED")
    rebuild_summary = operation_summary(rebuild) if rebuild is not None else None
    if rebuild_summary is not None:
        matched = sum(row.get("projection_id") == svc.digest(["vector-projection-v1", fingerprint, vid,
            row.get("metadata_signature"), rebuild.id, rebuild.attempts]) for vid, row in ready.items())
        applied = ("OPERATION_NOT_FULLY_SUCCESSFUL" if not rebuild_summary["counts_verified"]
            else "CURRENT_STATE_UNVERIFIED" if state in {"UNAVAILABLE", "SYNC_REQUIRED"}
            else "NO_CURRENT_CONTENT" if not pages
            else "CURRENT_GENERATION_VERIFIED" if matched == len(pages)
            else "CURRENT_GENERATION_DIFFERS")
        rebuild_summary.update(application_state=applied, matched_current_versions=matched,
            current_catalog_versions=len(pages))
    return {"version": 1, "checked_at": svc.primitive(svc.now()), "state": state,
        "generation": generation, "generation_scope": "current_authorized_catalog",
        "last_indexed_at": svc.primitive(max(dates)) if dates else None,
        "latest_job": operation_summary(latest) if latest is not None else None,
        "last_rebuild": rebuild_summary}
