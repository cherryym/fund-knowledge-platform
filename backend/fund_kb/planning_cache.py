"""Bounded reuse of public, source-free plans, never final answers or authority.

The caller MUST freshly authorize the new request and validate the originating
completed run before using a hit. No query normalization, semantic matching,
hidden reasoning, model switching, persistence, or background generation.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict

MAX_ENTRIES = 32
MAX_BYTES = 2 * 1024 * 1024
TTL_SECONDS = 600.0


def planning_cache_key(*, namespace, owner_id, space_id, request, connection, prompt_version,
                       system_sha256, planning_instruction, max_output_tokens, business_day):
    # Whitelist excludes api_key, cookies, callbacks and any opaque credentials.
    model = {key: connection.get(key) for key in ("id", "model_id", "provider_id", "protocol", "base_url",
        "revision", "auth_epoch", "owner_user_id", "allow_document_transfer", "supported_parameters")}
    if not all(model.get(key) for key in ("id", "model_id", "revision")):
        return None
    payload = {"v": 1, "namespace": namespace, "owner": owner_id, "space": space_id,
        "request": request, "model": model, "prompt": prompt_version, "system": system_sha256,
        "planning_instruction": planning_instruction, "max_output_tokens": max_output_tokens, "business_day": business_day}
    try:
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False,
            separators=(",", ":")).encode()).hexdigest()
    except (TypeError, ValueError, UnicodeError):
        return None  # An optional optimization cannot make a valid request fail.


class PlanningCache:
    def __init__(self, *, clock=time.monotonic, max_entries=MAX_ENTRIES, max_bytes=MAX_BYTES, ttl=TTL_SECONDS):
        self.clock, self.max_entries, self.max_bytes, self.ttl = clock, max_entries, max_bytes, ttl
        self._lock = threading.RLock()
        self._rows = OrderedDict()
        self._bytes = 0

    def _drop(self, key):
        row = self._rows.pop(key, None)
        if row: self._bytes -= len(row[1])

    def discard(self, key):
        with self._lock: self._drop(key)

    def clear(self):
        with self._lock:
            self._rows.clear(); self._bytes = 0

    def get(self, key):
        if not key: return None
        with self._lock:
            row = self._rows.get(key)
            if row is None: return None
            age = self.clock() - row[0]
            if age < 0 or age >= self.ttl:
                self._drop(key); return None
            self._rows.move_to_end(key)
            value = json.loads(row[1])
            return {**value, "age_ms": round(age * 1000, 3)}

    def put(self, key, *, plan, source_run_id):
        if not key or not isinstance(source_run_id, str) or not isinstance(plan, dict): return False
        if (not isinstance(plan.get("initial_assessment"), str) or not plan["initial_assessment"].strip()
                or not isinstance(plan.get("search_queries"), list)
                or any(not isinstance(q, str) or not q.strip() for q in plan["search_queries"])):
            return False
        try:
            raw = json.dumps({"plan": plan, "source_run_id": source_run_id}, ensure_ascii=False,
                sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
        except (ValueError, TypeError, UnicodeError): return False
        if len(raw) > self.max_bytes: return False
        with self._lock:
            now = self.clock()
            for old, row in list(self._rows.items()):
                if now - row[0] >= self.ttl: self._drop(old)
            self._drop(key)
            while self._rows and (len(self._rows) >= self.max_entries or self._bytes + len(raw) > self.max_bytes):
                self._drop(next(iter(self._rows)))
            self._rows[key] = (now, raw); self._bytes += len(raw)
        return True


planning_cache = PlanningCache()


def validated_plan(db, actor, space_id, request, key, entry):
    """Fresh transaction supplied by caller. A stale origin is a miss, not an
    authority decision for the new run. Never return prior source/final text.
    """
    from . import models as m
    from . import services as svc
    from .reference_evidence import evidence_signature, reference_evidence
    from .source_authority import authority_stamp
    from .source_reading_policy import policy_stamp

    svc.space_access(db, actor, space_id)
    origin = db.get(m.ConsultationRun, entry["source_run_id"])
    if not origin or origin.state != "COMPLETED" or origin.invalidated_at or origin.request != request:
        return None
    thread = db.get(m.ConsultationThread, origin.thread_id)
    actor_id = actor if isinstance(actor, str) else actor.id
    if not thread or thread.deleted_at or thread.owner_id != actor_id or thread.space_id != space_id:
        return None
    binding = (origin.policy_snapshot or {}).get("planning_cache_binding", {})
    analysis = (origin.policy_snapshot or {}).get("question_analysis", {})
    if (binding.get("key") != key or analysis.get("source") != "model_prior_knowledge_unverified"
            or analysis.get("local_sources_loaded") != 0 or analysis.get("plan") != entry["plan"]
            or binding.get("plan_sha256") != svc.digest(entry["plan"])
            or binding.get("source_reading_policy_stamp") != policy_stamp(db, space_id)
            or (origin.model_snapshot or {}).get("source_authority_stamp") != authority_stamp(db, space_id)):
        return None
    frozen = origin.evidence_snapshot or []
    if not frozen:
        return None
    context = request.get("context", {})
    ids = {row["version_id"] for row in frozen}
    values = reference_evidence(db, actor, space_id, context, reading=True, version_ids=ids) \
        if request.get("answer_scope", "formal") == "reference" \
        else svc.eligible_evidence(db, actor, space_id, context, version_ids=ids)
    admitted = {evidence_signature(row) for row in values}
    if any(evidence_signature(row) not in admitted for row in frozen):
        return None
    return entry["plan"]
