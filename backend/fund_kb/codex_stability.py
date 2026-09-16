"""Process-local admission, cooperative deadlines and content-free diagnostics.

No identity, prompt, model, RPC payload or exception text belongs in this module.
It does not retry, pool processes, read configuration or access storage/network.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar

from .providers import ProviderError
from .provider_contracts import ThrottledCheck

POLL_SECONDS = .1
RPC_BUDGET = ContextVar("codex_text_rpc_budget", default=None)
LOGGER = logging.getLogger("fund_kb.codex_stability")
# Uvicorn need not configure the root/application logger. Own this exact named
# logger only; never enable RPC/body logging or alter root/uvicorn handlers.
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
if not LOGGER.handlers:
    _handler = logging.StreamHandler()
    _handler.setLevel(logging.INFO)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    LOGGER.addHandler(_handler)
SAFE_CODES = frozenset({
    "CODEX_INFERENCE_BUSY", "CODEX_INFERENCE_QUEUE_TIMEOUT", "CODEX_INFERENCE_TIMEOUT",
    "CODEX_AUTH_REQUIRED", "CODEX_LOGIN_STALE", "CODEX_AUTH_BINDING_INVALID",
    "CODEX_AUTH_ARCHIVE_UNAVAILABLE", "CODEX_AUTH_NOT_PERSISTED", "CODEX_STORAGE_PATH_UNSAFE",
    "CODEX_CREDENTIAL_STORE_UNAVAILABLE",
    "CODEX_EXECUTABLE_UNAVAILABLE", "CODEX_VERSION_UNSUPPORTED", "CODEX_MODEL_NOT_VERIFIED",
    "CODEX_RPC_FORBIDDEN", "CODEX_RPC_TIMEOUT", "CODEX_RPC_FAILED", "CODEX_RPC_UNAVAILABLE",
    "CODEX_RPC_CLOSED", "CODEX_RPC_TOO_LARGE", "CODEX_RPC_INVALID_MESSAGE",
    "CODEX_RPC_UNSOLICITED_REQUEST", "CODEX_RPC_BACKPRESSURE", "CODEX_RPC_IO_ERROR",
    "CODEX_TURN_MISMATCH", "CODEX_MODEL_REQUEST_FAILED", "CONNECTION_REVISION_CHANGED",
    "MODEL_JOB_CANCELLED_OR_STALE", "CANCELLED", "EVIDENCE_ACCESS_CHANGED",
    "UNSUPPORTED_TOOL_CALL", "PROVIDER_INVALID_RESPONSE", "PROVIDER_RESPONSE_TOO_LARGE",
    "PROVIDER_REQUEST_TOO_LARGE", "PROVIDER_RESPONSE_INCOMPLETE", "PROVIDER_INVALID_JSON",
    "INVALID_MESSAGES", "UNSUPPORTED_MESSAGE_CONTENT", "INVALID_TOKEN_LIMIT", "INVALID_TIMEOUT",
})


def safe_code(error):
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code in SAFE_CODES else "INTERNAL_ERROR"


class RequestBudget:
    def __init__(self, timeout, guard, clock):
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or not math.isfinite(timeout) or timeout <= 0):
            raise ProviderError("INVALID_TIMEOUT")
        self.clock = clock
        self.unlimited = timeout is None
        self.guard = ThrottledCheck(guard, clock) if timeout is None else guard
        self.started = clock()
        # Never enlarge a caller's shorter budget; keep the existing hard cap.
        self.deadline = float("inf") if timeout is None else self.started + min(timeout, 180)

    def remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise ProviderError("CODEX_INFERENCE_TIMEOUT")
        return remaining

    def check(self, *, force=False):
        self.remaining()
        self.guard.force() if force and isinstance(self.guard, ThrottledCheck) else self.guard()
        self.remaining()


class BoundedAdmission:
    """FIFO around the existing single slot; waiting holds no identity/DB lock."""
    def __init__(self, slot, *, max_waiting=4, max_wait_seconds=30):
        if type(max_waiting) is not int or not 0 <= max_waiting <= 16:
            raise ValueError("invalid queue capacity")
        if not math.isfinite(max_wait_seconds) or not 0 < max_wait_seconds <= 180:
            raise ValueError("invalid queue wait budget")
        self.slot = slot
        self.max_waiting, self.max_wait_seconds = max_waiting, max_wait_seconds
        self.condition = threading.Condition()
        self.waiters = deque()
        self.quarantined = False

    def quarantine(self):
        with self.condition:
            self.quarantined = True
            self.condition.notify_all()

    @property
    def waiting(self):
        with self.condition:
            return len(self.waiters)

    @contextmanager
    def take(self, budget, trace):
        ticket, acquired = object(), False
        end = budget.deadline if budget.unlimited else min(budget.deadline, budget.clock() + self.max_wait_seconds)
        try:
            budget.check()
            with self.condition:
                if self.quarantined:
                    raise ProviderError("CODEX_RPC_UNAVAILABLE")
                trace.queue_depth = len(self.waiters)
                if not self.waiters and self.slot.acquire(blocking=False):
                    acquired = True
                else:
                    if len(self.waiters) >= self.max_waiting:
                        raise ProviderError("CODEX_INFERENCE_BUSY")
                    self.waiters.append(ticket)
            while not acquired:
                # A cancelled/revoked waiter leaves before it can materialize an
                # identity. The check runs outside the queue's condition lock.
                budget.guard()
                with self.condition:
                    if self.quarantined:
                        raise ProviderError("CODEX_RPC_UNAVAILABLE")
                    remaining = end - budget.clock()
                    if remaining <= 0:
                        raise ProviderError("CODEX_INFERENCE_QUEUE_TIMEOUT")
                    if self.waiters[0] is ticket and self.slot.acquire(blocking=False):
                        self.waiters.popleft()
                        acquired = True
                    else:
                        self.condition.wait(timeout=min(POLL_SECONDS, remaining))
            yield
        finally:
            with self.condition:
                if ticket in self.waiters:
                    self.waiters.remove(ticket)
                if acquired:
                    self.slot.release()
                self.condition.notify_all()


@contextmanager
def checked_lock(lock, budget):
    """Bound identity/RPC lock waits without running guards under queue locks."""
    acquired = False
    try:
        while not acquired:
            budget.check()
            acquired = lock.acquire(timeout=min(POLL_SECONDS, budget.remaining()))
        budget.check()
        yield
    finally:
        if acquired:
            lock.release()


class InferenceTrace:
    """One bounded structured record per call; only enums/counts/durations."""
    def __init__(self, clock):
        self.clock = clock
        self.started = self.phase_started = clock()
        self.phase = "preflight"
        self.phase_ms = {}
        self.queue_depth = 0
        self.first_event_ms = self.first_output_ms = None
        self.events = self.server_retry_events = 0
        self.cleanup_codes = []
        self.error_phase = None

    def enter(self, phase):
        now = self.clock()
        self.phase_ms[self.phase] = self.phase_ms.get(self.phase, 0) + (now - self.phase_started) * 1000
        self.phase, self.phase_started = phase, now

    def event(self, *, output=False, retry=False):
        self.events += 1
        elapsed = (self.clock() - self.started) * 1000
        if self.first_event_ms is None:
            self.first_event_ms = elapsed
        if output and self.first_output_ms is None:
            self.first_output_ms = elapsed
        self.server_retry_events += int(retry)

    def emit(self, error):
        self.enter("finished")
        record = {
            "schema": 1, "outcome": "FAILED" if error else "COMPLETED",
            "error_code": safe_code(error) if error else None,
            "error_phase": self.error_phase,
            "phase_ms": {key: round(value, 3) for key, value in self.phase_ms.items()},
            "total_ms": round((self.clock() - self.started) * 1000, 3),
            "queue_depth": self.queue_depth, "events": self.events,
            "first_event_ms": self.first_event_ms, "first_output_ms": self.first_output_ms,
            "server_retry_events": self.server_retry_events, "cleanup_codes": self.cleanup_codes,
        }
        # Diagnostics must never replace the primary failure or retain its text.
        try:
            LOGGER.info("codex_inference %s", json.dumps(record, separators=(",", ":")))
        except Exception:  # noqa: BLE001, S110 - logging failures cannot expose secrets or mask the result.
            pass
