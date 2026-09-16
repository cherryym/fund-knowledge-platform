"""Revocable public paragraphs, never an answer or a durable evidence record.

Callbacks receive a cumulative text snapshot (None revokes it), not tokens.
Memory and update frequency are bounded. Separate API/worker processes simply
have no preview; final delivery remains the existing database-backed contract.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from .wiki_answer_content import _decoded, _private_reasoning, _redact, _system_claims

MAX_PREVIEW_BYTES = 65536
PREVIEW_INTERVAL = .75
PREVIEW_NOTICE = "生成中，尚未完成核验"


def public_paragraphs(text):
    """Inspect the WHOLE prefix, so split tags/secrets cannot cross a sanitizer.

The unfinished paragraph is withheld. HTML and code-bearing outputs are left
to the final renderer, including multiline/encoded tags. This intentionally
supports a smaller surface than final Markdown, and creates no source links.
    """
    if not isinstance(text, str) or len(text.encode()) > MAX_PREVIEW_BYTES:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text, _ = _private_reasoning(text)
    text, _ = _redact(text)
    text = _system_claims(text, lambda _: None)
    view, _ = _decoded(text)
    if re.search(r"<\s*[/!?A-Za-z]|```|~~~", view):
        return ""
    if re.search(r"(?im)^\s*(?:READ(?:_FULL|_SECTION)?|SEARCH|CATALOG)\b", view):
        return ""
    boundaries = list(re.finditer(r"\n[ \t]*\n", text))
    return text[:boundaries[-1].start()].strip() if boundaries else ""


class PublicTextBuffer:
    """One bounded prefix, no incremental queue and no callback per token.

Only transport-classified public text may enter this buffer. A completed item
can reconcile its text here; a terminal response must never manufacture a
stream by splitting the completed answer into callbacks.
    """
    def __init__(self, callback, *, max_bytes=MAX_PREVIEW_BYTES, secrets=(), clock=None):
        self.callback = callback
        self.limit = min(max_bytes, MAX_PREVIEW_BYTES)
        self.clock = clock or time.monotonic
        self.secrets = tuple(value for value in secrets if value)
        self.raw = ""
        self.size = 0
        self.sent = ""
        self.last = float("-inf")
        self.disabled = False

    def replace(self, text):
        if self.disabled:
            return
        if not isinstance(text, str) or len(text.encode()) > self.limit:
            self.revoke()
            return
        self.raw, self.size = text, len(text.encode())

    def append(self, delta):
        if self.disabled:
            return
        if not isinstance(delta, str) or self.size + len(delta.encode()) > self.limit:
            self.revoke()
            return
        self.raw += delta
        self.size += len(delta.encode())

    def flush(self):
        if self.disabled or self.clock() - self.last < PREVIEW_INTERVAL:
            return
        self.last = self.clock()
        # Check cumulative text too: a credential may be split across SSE/RPC
        # deltas even though each individual event passed the secret-echo check.
        if any(secret in self.raw for secret in self.secrets):
            self.revoke()
            return
        text = public_paragraphs(self.raw)
        if text != self.sent:
            self.sent = text
            # Preserve the already observed paragraph boundary for the consumer.
            self.callback(text + "\n\n" if text else None)

    def revoke(self):
        self.disabled = True
        self.raw, self.sent, self.size = "", "", 0
        revoke_callback(self.callback)


def revoke_callback(callback):
    """Cleanup cannot replace the original provider/authority failure."""
    if callable(callback):
        try:
            callback(None)
        except Exception:
            pass


@dataclass
class PreviewEntry:
    run_id: str
    job_id: str
    attempt: int
    owner_id: str
    request_number: int
    request_hash: str
    evidence_hash: str
    model: dict
    catalog_stamp: str | None
    policy_stamp: str
    text: str = ""
    revision: int = 0
    updated: float = 0


class PreviewStore:
    """Process-local, bounded, identity-bound staging; never persists prose."""
    def __init__(self, *, capacity=64, ttl=30, clock=None):
        self.capacity, self.ttl = capacity, ttl
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.entries = OrderedDict()

    def _prune(self):
        for entry in self.entries.values():
            if entry.text and self.clock() - entry.updated > self.ttl:
                # Expire BODY only, not an active generation's registration.
                # Long thinking/pauses must still allow the next real paragraph.
                entry.text = ""
                entry.revision += 1

    def begin(self, **binding):
        with self.lock:
            self._prune()
            entry = PreviewEntry(**binding, updated=self.clock())
            self.entries[entry.run_id] = entry
            self.entries.move_to_end(entry.run_id)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)
            return entry

    def update(self, entry, text):
        with self.lock:
            if self.entries.get(entry.run_id) is not entry:
                return  # A revoked/evicted/older attempt cannot resurrect it.
            if text is None:
                entry.text = ""
            else:
                # Defense in depth for custom/synthetic completion providers.
                entry.text = public_paragraphs(text)
            entry.revision += 1
            entry.updated = self.clock()

    def get(self, run_id):
        with self.lock:
            self._prune()
            entry = self.entries.get(run_id)
            if entry is None or not entry.text:
                return None
            from dataclasses import replace
            return replace(entry)

    def current(self, entry):
        with self.lock:
            current = self.entries.get(entry.run_id)
            return current is not None and current.job_id == entry.job_id and current.attempt == entry.attempt \
                and current.request_number == entry.request_number and current.revision == entry.revision

    def discard(self, run_id, *, job_id=None, attempt=None):
        with self.lock:
            entry = self.entries.get(run_id)
            if entry and (job_id is None or entry.job_id == job_id) and (attempt is None or entry.attempt == attempt):
                del self.entries[run_id]

    def discard_job(self, job_id, attempt):
        with self.lock:
            for key, entry in list(self.entries.items()):
                if entry.job_id == job_id and entry.attempt == attempt:
                    del self.entries[key]


previews = PreviewStore()


def readable_preview(ctx, run, job, *, invalidated, fresh_records=None, timings=None):
    """GET projection rechecks the same run, job lease, identity and source chain.

No credentials are resolved/materialized and no database state is changed.
The store's hashes are bindings, not a substitute for fresh authorization.
    """
    from . import models as m, providers, services as svc
    from .reference_evidence import evidence_signature, reference_evidence
    from .source_reading_policy import policy_stamp
    from .wiki_catalog import build_catalog, catalog_signature

    entry = previews.get(run.id)
    if entry is None:
        return None
    try:
        thread = ctx.db.get(m.ConsultationThread, run.thread_id)
        last = (run.model_snapshot or {}).get("last_request") or {}
        if (invalidated or run.state != "RUNNING" or run.response or run.error_code or not ctx.user.active
                or not thread or thread.deleted_at or thread.owner_id != ctx.user.id or entry.owner_id != ctx.user.id
                or job.id != entry.job_id or job.attempts != entry.attempt or job.cancel_requested
                or job.state != "RUNNING" or job.lease_until is None or job.lease_until <= svc.now()
                or last.get("phase") != "synthesis" or last.get("attempt") != entry.attempt
                or (run.model_snapshot or {}).get("model_request_count") != entry.request_number
                or svc.digest(run.request) != entry.request_hash
                or svc.digest(run.evidence_snapshot) != entry.evidence_hash):
            raise ValueError("stale preview")
        svc.space_access(ctx.db, ctx.user, thread.space_id)
        providers.authorize_model_snapshot(ctx.db, ctx.user, entry.model, ctx.settings)
        context, scope = (run.request or {}).get("context", {}), (run.request or {}).get("answer_scope", "formal")
        started = time.monotonic()
        # Direct synthesis contains compact_evidence for read sources only,
        # source-free planning and source-policy instructions. The worker may
        # explicitly attest that footprint; all its sources/edges/section titles
        # are covered by the fresh full source hashes below. Other paths (notes,
        # outlines, old reader) retain the full metadata-catalog fence.
        if entry.catalog_stamp is not None and catalog_signature(build_catalog(
                ctx.db, ctx.user, thread.space_id, context, scope=scope)) != entry.catalog_stamp:
            raise ValueError("source metadata changed")
        if policy_stamp(ctx.db, thread.space_id) != entry.policy_stamp:
            raise ValueError("source policy changed")
        if timings is not None:
            timings["catalog_and_policy_ms"] = (time.monotonic() - started) * 1000
            timings["catalog_check"] = "full_metadata" if entry.catalog_stamp is not None else "synthesis_sources_only"
            timings["reused_fresh_records"] = fresh_records is not None
        started = time.monotonic()
        if fresh_records is None:
            ids = {row["version_id"] for row in run.evidence_snapshot or []}
            fresh_records = reference_evidence(ctx.db, ctx.user, thread.space_id, context, reading=True, version_ids=ids) \
                if scope == "reference" else svc.eligible_evidence(ctx.db, ctx.user, thread.space_id, context, version_ids=ids)
        allowed = {evidence_signature(row) for row in fresh_records}
        if not run.evidence_snapshot or any(evidence_signature(row) not in allowed for row in run.evidence_snapshot):
            raise ValueError("source body changed")
        if timings is not None:
            timings["additional_source_check_ms"] = (time.monotonic() - started) * 1000
        if not previews.current(entry):
            return None
        return {"state": "pending", "notice": PREVIEW_NOTICE, "text": entry.text,
            "run_id": run.id, "attempt": entry.attempt, "revision": entry.revision,
            "source_check": "current_access", "final_validation": "pending"}
    except (svc.APIError, providers.ProviderError, ValueError, KeyError, TypeError):
        previews.discard(run.id, job_id=entry.job_id, attempt=entry.attempt)
        return None


def measure_preview_read(ctx, run, job, *, invalidated=False, fresh_records=None):
    """Optional read-only cost probe for integration; returns no answer text.

Use with the API's read-only session/projection scope. Passing that projection's
fresh_records measures incremental preview overhead instead of rereading them.
    """
    from .projection_read import projection_read
    timings = {}
    started = time.monotonic()
    with projection_read(ctx.db):
        result = readable_preview(ctx, run, job, invalidated=invalidated, fresh_records=fresh_records, timings=timings)
    return {**timings, "elapsed_ms": (time.monotonic() - started) * 1000, "preview_available": result is not None}
