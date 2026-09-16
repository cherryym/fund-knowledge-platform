"""Observed owner/connection-specific invocation cost, never a speed guarantee."""
from __future__ import annotations

from statistics import median

from sqlalchemy import select

from . import models as m


def model_cost_hint(db, owner_id, choice, target_ms, connection=None):
    if not choice:
        return {"samples": 0, "status": "NO_CONNECTION_HISTORY", "guaranteed": False}
    rows = db.execute(select(m.AuditEvent.details, m.ConsultationRun.model_snapshot)
        .join(m.Job, m.Job.id == m.AuditEvent.object_id)
        .join(m.ConsultationRun, m.ConsultationRun.id == m.Job.run_id)
        .where(m.Job.owner_id == owner_id, m.Job.kind == "ANSWER",
            m.ConsultationRun.state == "COMPLETED",
            m.AuditEvent.action == "answer.model_response_received")
        .order_by(m.AuditEvent.created_at.desc(), m.AuditEvent.id.desc()).limit(64))
    durations = []
    for detail, snapshot in rows:
        detail, snapshot = detail or {}, snapshot or {}
        if (snapshot.get("id") != choice.get("connection_id") or snapshot.get("model_id") != choice.get("model_id")
                or detail.get("phase") != "synthesis"):
            continue
        if connection and any(key in connection and snapshot.get(key) != connection[key]
                              for key in ("revision", "auth_epoch")):
            continue
        value = detail.get("duration_ms")
        if type(value) in (int, float) and 0 < value < 86_400_000:
            durations.append(value)
    return {"samples": len(durations), "median_invocation_ms": round(median(durations), 3) if durations else None,
        "last_invocation_ms": durations[0] if durations else None,
        "single_call_already_over_target": bool(durations and median(durations) > target_ms),
        "status": "OBSERVED_NOT_A_GUARANTEE" if durations else "NO_CONNECTION_HISTORY",
        "includes_queue_transport_and_model": True, "guaranteed": False}
