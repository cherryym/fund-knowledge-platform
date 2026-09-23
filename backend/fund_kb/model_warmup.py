"""Process-local, single-flight preparation of explicitly configured LOCAL models.

No document reads, provider requests, downloads or hidden retry loops. A status
read never starts work. Native kernels finish before shutdown frees their model.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import threading
import time
from uuid import uuid4

_PREPARATION_SLOT = threading.Semaphore(1)
_SAFE_ERRORS = {"LOCAL_MODEL_FILE_MISSING_OR_INVALID", "LOCAL_MODEL_HASH_MISMATCH",
    "LOCAL_MODEL_IDENTITY_NOT_PINNED", "LOCAL_MODEL_PATH_REQUIRED", "REQUESTED_MPS_UNAVAILABLE",
    "QWEN_MPS_FALLBACK_FORBIDDEN", "QWEN_MODEL_DEVICE_OR_DTYPE_MISMATCH",
    "WARMUP_EMBEDDING_INVALID", "WARMUP_RERANK_INVALID"}


class ModelWarmupError(RuntimeError):
    def __init__(self, code="MODEL_WARMUP_FAILED"):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_error(exc):
    code = str(exc).split(":", 1)[0]
    if code in _SAFE_ERRORS:
        return code
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "LOCAL_DEPENDENCY_UNAVAILABLE"
    if isinstance(exc, MemoryError) or "out of memory" in str(exc).lower():
        return "LOCAL_MEMORY_UNAVAILABLE"
    return "MODEL_WARMUP_FAILED"


class ModelWarmup:
    def __init__(self, settings, probe):
        self.mode = getattr(settings, "retrieval_warmup_mode", "disabled")
        self.supported = (self.mode != "disabled" and settings.embedding_mode == "transformers"
                          and getattr(settings, "reranker_mode", "disabled") == "local")
        self._probe = probe
        self._condition = threading.Condition(threading.RLock())
        self._thread = None
        self._closed = False
        self._value = {"runtime_id": str(uuid4()), "process_id": os.getpid(), "scope": "current_process",
            "policy": self.mode, "supported": self.supported, "state": "NOT_LOADED" if self.supported else "NOT_APPLICABLE",
            "phase": "idle", "attempts": 0, "started_at": None, "completed_at": None,
            "error_code": None, "self_tested": False, "elapsed_ms": None}

    def snapshot(self):
        with self._condition:
            return dict(self._value)

    def start(self, *, retry=False):
        with self._condition:
            if self._closed:
                raise ModelWarmupError("MODEL_WARMUP_CLOSED")
            if not self.supported or self._value["state"] in {"LOADING", "READY"}:
                return self.snapshot()
            if self._value["state"] == "FAILED" and not retry:
                return self.snapshot()
            self._value.update(state="LOADING", phase="waiting_slot", started_at=_now(), completed_at=None,
                error_code=None, self_tested=False, elapsed_ms=None, attempts=self._value["attempts"] + 1)
            self._thread = threading.Thread(target=self._run, name="fund-kb-local-model-warmup", daemon=True)
            try:
                self._thread.start()
            except RuntimeError:
                self._thread = None
                self._value.update(state="FAILED",phase="failed",error_code="MODEL_WARMUP_FAILED",completed_at=_now())
                self._condition.notify_all()
            return self.snapshot()

    def _phase(self, phase):
        with self._condition:
            if self._closed:
                raise ModelWarmupError("MODEL_WARMUP_CLOSED")
            self._value["phase"] = phase
            self._condition.notify_all()

    def _run(self):
        started = time.monotonic()
        try:
            with _PREPARATION_SLOT:
                self._phase("preparing")
                self._probe(self._phase)
            with self._condition:
                if not self._closed:
                    self._value.update(state="READY", phase="ready", self_tested=True)
        except BaseException as exc:
            with self._condition:
                if not self._closed:
                    self._value.update(state="FAILED", phase="failed", error_code=_safe_error(exc), self_tested=False)
            if not isinstance(exc, Exception):
                raise
        finally:
            with self._condition:
                self._value.update(completed_at=_now(), elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                self._condition.notify_all()

    def ensure_ready(self, cancel_check=None):
        if not self.supported:
            return self.snapshot()
        if cancel_check:
            cancel_check()
        self.start()
        while True:
            if cancel_check:
                cancel_check()
            with self._condition:
                state = self._value["state"]
                if state == "READY":
                    return self.snapshot()
                if state == "FAILED":
                    raise ModelWarmupError(self._value["error_code"])
                if self._closed:
                    raise ModelWarmupError("MODEL_WARMUP_CLOSED")
                self._condition.wait(.2)

    def close(self):
        with self._condition:
            self._closed = True
            self._value.update(state="CLOSED", phase="closed", self_tested=False)
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join()


def start_default_warmup(settings, vector):
    if getattr(settings, "retrieval_warmup_mode", "disabled") == "auto" and vector is not None:
        manager = getattr(vector, "model_runtime", None)
        if manager is not None:
            manager.start()
