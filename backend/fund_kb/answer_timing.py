"""Monotonic phase observations. Durations are not time limits or quality scores."""
from __future__ import annotations

import math
import time
from contextlib import contextmanager

PHASES = frozenset({"model_planning", "model_synthesis", "model_wiki_index", "model_wiki_notes",
    "local_model_preparation", "catalog_and_policy", "retrieval_and_navigation", "source_reading",
    "context_reranking"})


class AnswerTimings:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.phases = {}

    def add(self, phase, elapsed):
        if phase not in PHASES or type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("INVALID_TIMING_PHASE")
        row = self.phases.setdefault(phase, {"elapsed_ms": 0.0, "calls": 0})
        row["elapsed_ms"] += elapsed * 1000
        row["calls"] += 1

    @contextmanager
    def measure(self, phase):
        start = self.clock()
        try:
            yield
        finally:
            self.add(phase, self.clock() - start)

    def snapshot(self):
        return {"version": "answer_timing_v1", "execution_elapsed_ms": round((self.clock() - self.started) * 1000, 3),
            "phases": {name: {"elapsed_ms": round(row["elapsed_ms"], 3), "calls": row["calls"]}
                       for name, row in self.phases.items()},
            "scope": "execution_observation_not_deadline", "durations_additive": False,
            "first_visible_answer_ms": None, "first_visible_answer_status": "NOT_MEASURED",
            "model_phase_includes_network_queue_and_rechecks": True}


def persist_timings(dispatcher, job_id, attempt, timings):
    """Best-effort diagnostics cannot replace an answer, exception or stale lease.

    No source bodies/IDs, queries, credentials or private model output exist in
    this projection. Publication is still behind the normal owner/source gate.
    """
    from sqlalchemy import select

    from . import models as m
    try:
        with dispatcher.session_factory.begin() as db:
            job = db.scalar(select(m.Job).where(m.Job.id == job_id).with_for_update())
            if job is None or job.attempts != attempt or not job.run_id:
                return
            run = db.get(m.ConsultationRun, job.run_id)
            if run is not None:
                run.model_snapshot = {**(run.model_snapshot or {}), "pipeline_timing": timings.snapshot()}
    except Exception:  # noqa: BLE001 - diagnostic persistence must never replace the job outcome.
        # Missing observation remains missing, never zero or an invented PASS.
        # Metrics storage must not hide the original job outcome.
        return
