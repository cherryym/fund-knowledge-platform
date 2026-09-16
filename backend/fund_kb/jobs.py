"""Persistent, fenced jobs. Database authority is checked at every delivery boundary.

The queue transports IDs only. ``attempts`` is the monotonic fencing token; a
worker whose lease expired cannot finish over a newer worker's transaction.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import mimetypes
import multiprocessing
import re
import shutil
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from . import models as m
from . import services as svc
from .storage import Storage, safe_filename, validate_key

# v1 is the installed ai.py answer-v2/grounded template, not arbitrary stored text.
SUPPORTED_PROMPT_VERSIONS = frozenset({"v1"})


def now():
    return datetime.now(UTC)


def uid():
    return str(uuid4())


def stable_id(job_id, purpose):
    return str(uuid5(NAMESPACE_URL, f"fund-kb:{job_id}:{purpose}"))


def sha(data: bytes):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return json.dumps(svc.primitive(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode()


class JobError(RuntimeError):
    def __init__(self, code, *, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


class LeaseLost(JobError):
    def __init__(self):
        super().__init__("LEASE_LOST")


class Cancelled(JobError):
    def __init__(self):
        super().__init__("CANCELLED")


def setting(settings, canonical, legacy, default):
    return getattr(settings, canonical, getattr(settings, legacy, default))


def _inspect_file(data, filename, maximum):
    """Bounded basic checks are also mandatory before an external scanner/parser."""
    if not data or len(data) > maximum:
        raise JobError("FILE_SIZE_INVALID")
    if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in data:
        raise JobError("FILE_REJECTED")
    suffix = Path(filename).suffix.lower()
    if suffix in {".docx", ".xlsx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if len(infos) > 10000 or sum(x.file_size for x in infos) > maximum * 5:
                    raise JobError("FILE_REJECTED")
                for info in infos:
                    name = info.filename.rstrip("/")
                    if name:
                        validate_key(name)
                    if (info.flag_bits & 1 or info.file_size > max(1, info.compress_size) * 150
                            or "vbaproject" in name.lower() or "/embeddings/" in name.lower()):
                        raise JobError("FILE_REJECTED")
                names = set(archive.namelist())
                marker = "word/document.xml" if suffix == ".docx" else "xl/workbook.xml"
                if "[Content_Types].xml" not in names or marker not in names:
                    raise JobError("MIME_MISMATCH")
        except (zipfile.BadZipFile, ValueError):
            raise JobError("FILE_REJECTED") from None
        return ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if suffix == ".docx" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise JobError("MIME_MISMATCH")
        if any(marker in data for marker in (b"/JavaScript", b"/JS ", b"/Launch", b"/EmbeddedFile")):
            raise JobError("FILE_REJECTED")
        return "application/pdf"
    if suffix in {".png", ".jpg", ".jpeg"}:
        from PIL import Image, UnidentifiedImageError
        try:
            with Image.open(io.BytesIO(data)) as image:
                actual = image.format
                image.verify()
            if actual != ("PNG" if suffix == ".png" else "JPEG"):
                raise JobError("MIME_MISMATCH")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise JobError("FILE_REJECTED") from None
        return "image/png" if suffix == ".png" else "image/jpeg"
    if suffix in {".txt", ".md", ".html", ".htm"}:
        if b"\0" in data and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise JobError("MIME_MISMATCH")
        if data.startswith((b"MZ", b"\x7fELF", b"PK\x03\x04", b"%PDF-")):
            raise JobError("MIME_MISMATCH")
        return mimetypes.guess_type(filename)[0] or "text/plain"
    raise JobError("UNSUPPORTED_FORMAT")


def scan_file(settings, path, filename):
    data = path.read_bytes()
    mime = _inspect_file(data, filename, settings.max_file_bytes)
    mode = setting(settings, "scan_backend", "scan_mode", "basic")
    if mode == "basic":
        if settings.app_env not in {"development", "test"}:
            raise JobError("SCANNER_UNAVAILABLE")
        return mime, {"scanner": "development-basic-v1", "level": "basic", "clean": True}, [
            "DEVELOPMENT_BASIC_SCAN_ONLY: 仅基础内容/格式/压缩包检查，未完成机构杀毒与DLP验收。"]
    if mode == "clamav":
        command = getattr(settings, "clamav_command", "clamscan")
        executable = shutil.which(command)
        if not executable:
            raise JobError("SCANNER_UNAVAILABLE")
        try:
            result = subprocess.run([executable, "--no-summary", str(path)],
                                    capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise JobError("SCANNER_UNAVAILABLE", retryable=True) from None
        if result.returncode == 1:
            raise JobError("FILE_REJECTED")
        if result.returncode != 0:
            raise JobError("SCANNER_UNAVAILABLE", retryable=True)
        return mime, {"scanner": "clamav", "level": "antivirus", "clean": True}, []
    if mode == "adapter":
        reference = getattr(settings, "scanner_adapter", None)
        if not reference or ":" not in reference:
            raise JobError("SCANNER_UNAVAILABLE")
        try:
            module, name = reference.split(":", 1)
            adapter = getattr(importlib.import_module(module), name)
            report = adapter(path=path, filename=filename, settings=settings)
        except Exception:  # noqa: BLE001 - institution boundary; never expose scanner output/secrets.
            raise JobError("SCANNER_UNAVAILABLE") from None
        if not isinstance(report, dict) or not report.get("scanner") or report.get("clean") is not True:
            raise JobError("FILE_REJECTED")
        return mime, {"scanner": str(report["scanner"])[:100], "level": "institution-adapter", "clean": True}, []
    raise JobError("SCANNER_UNAVAILABLE")


def _parse_child(connection, path, filename):
    """A bounded child isolates native document parsers/OCR from durable workers."""
    from .ingestion import parse_file
    try:
        connection.send(("ok", parse_file(Path(path), filename)))
    except Exception as exc:  # noqa: BLE001 - child protocol never sends arbitrary parser exception text.
        code = getattr(exc, "code", "PARSE_FAILED")
        connection.send(("error", code if isinstance(code, str) and len(code) < 100 else "PARSE_FAILED"))
    finally:
        connection.close()


class JobDispatcher:
    RETRY_DELAYS = (30, 120, 600)

    def __init__(self, settings, session_factory, vector_index, *, codex_bridge=None, retrieval_registry=None):
        self.settings, self.session_factory, self.vector_index = settings, session_factory, vector_index
        self.retrieval_registry = retrieval_registry
        self._owns_retrieval_registry = False
        self._job_retrieval_binding = None
        if retrieval_registry is None and getattr(settings, "retrieval_profiles_file", None) is not None:
            # CLI/Celery construct dispatchers too; they must use the same
            # named profiles as the API, even without an application lifespan.
            from .retrieval_registry import registry_for
            cached = getattr(self.settings, "_retrieval_registry", None)
            self.retrieval_registry = registry_for(self.settings, self.vector_index)
            self._owns_retrieval_registry = self.retrieval_registry is not None and self.retrieval_registry is not cached
        self.read_session_factory = session_factory
        self.codex_bridge = codex_bridge
        binding = getattr(session_factory, "kw", {}).get("bind")
        if binding is not None and binding.dialect.name == "sqlite":
            # Acquire SQLite write intent before any SELECT, avoiding read-to-write
            # promotion deadlocks. Other database isolation/locking is unchanged.
            self.session_factory = sessionmaker(class_=session_factory.class_, **{
                **session_factory.kw,
                "bind": binding.execution_options(sqlite_transaction_mode="IMMEDIATE"),
            })
            self.read_session_factory = sessionmaker(class_=session_factory.class_, **{
                **session_factory.kw, "autoflush": False,
                "bind": binding.execution_options(sqlite_transaction_mode="DEFERRED"),
            })
        self.storage = Storage(settings)
        self.mode = setting(settings, "job_backend", "job_mode", "local")
        if self.mode not in {"local", "celery"}:
            raise ValueError("UNSUPPORTED_JOB_BACKEND")
        self.lease_seconds = max(1, float(getattr(settings, "job_lease_seconds", 120)))
        self.pool = ThreadPoolExecutor(max_workers=getattr(settings, "job_workers", 2),
                                       thread_name_prefix="fund-kb-job") if self.mode == "local" else None
        self._lock, self._stop = threading.RLock(), threading.Event()
        self._futures, self._recovery = {}, None
        self._celery = None

    def _retrieval_job_view(self, job_id, attempt):
        """Bind execution to a frozen request without changing the shared worker."""
        from .vector_indexing import freeze_retrieval_selection
        with self.read_session_factory() as db:
            job = db.get(m.Job, job_id)
            if not job or job.attempts != attempt or job.state != "RUNNING" \
                    or job.lease_until is None or job.lease_until <= now():
                raise LeaseLost()
            if job.cancel_requested:
                raise Cancelled()
            if job.kind == "ANSWER":
                _, run, _ = self._run_context(db, job, read_only=True, validate_attachments=False)
                selection = (run.request or {}).get("retrieval_selection")
                recorded = (run.model_snapshot or {}).get("retrieval_selection")
                if recorded != selection:
                    raise JobError("RETRIEVAL_BINDING_CHANGED")
            elif job.kind == "COMPILE" and (job.payload or {}).get("task") == "VECTOR_INDEX":
                from .vector_indexing import authorize_index_job
                user = self._user(db, job)
                authorize_index_job(db, user, job)
                selection = (job.payload or {}).get("retrieval_selection")
            else:
                return self
        registry = self.retrieval_registry
        if registry is None and selection is None:
            return self  # Explicitly unconfigured, historical single-index behavior.
        try:
            frozen = freeze_retrieval_selection(registry, selection, require_frozen=True)
        except svc.APIError as exc:
            raise JobError(exc.code) from None
        marker = (job_id, attempt, svc.digest(frozen))
        if self._job_retrieval_binding == marker:
            if self.vector_index is None or self.vector_index.embedding.fingerprint != frozen["fingerprint"]:
                raise JobError("RETRIEVAL_RUNTIME_MISMATCH")
            return self
        if self._job_retrieval_binding is not None:
            raise JobError("RETRIEVAL_RUNTIME_MISMATCH")
        if getattr(self, "_retrieval_profile_bound", False):
            if getattr(self, "_retrieval_selection", None) != frozen or self.vector_index is None \
                    or self.vector_index.embedding.fingerprint != frozen["fingerprint"]:
                raise JobError("RETRIEVAL_RUNTIME_MISMATCH")
            self._job_retrieval_binding = marker
            return self
        from .retrieval_registry import RetrievalProfileError
        try:
            scoped = registry.scoped_dispatcher(self, frozen)
        except RetrievalProfileError as exc:
            raise JobError(exc.code) from None
        if scoped is self or scoped.vector_index is None \
                or scoped.vector_index.embedding.fingerprint != frozen["fingerprint"]:
            raise JobError("RETRIEVAL_RUNTIME_MISMATCH")
        scoped._job_retrieval_binding = marker
        scoped._owns_retrieval_registry = False
        return scoped

    def _check_retrieval_fence(self, db, job):
        if self._job_retrieval_binding is None:
            return
        from .vector_indexing import freeze_retrieval_selection
        if job.kind == "ANSWER":
            run = db.get(m.ConsultationRun, job.run_id or job.payload.get("run_id"))
            selection = (run.request or {}).get("retrieval_selection") if run else None
            if run is None or (run.model_snapshot or {}).get("retrieval_selection") != selection:
                raise JobError("RETRIEVAL_BINDING_CHANGED")
        else:
            selection = job.payload.get("retrieval_selection")
        try:
            frozen = freeze_retrieval_selection(self.retrieval_registry, selection, require_frozen=True)
        except svc.APIError as exc:
            raise JobError(exc.code) from None
        if self._job_retrieval_binding != (job.id, job.attempts, svc.digest(frozen)) \
                or self.vector_index is None or self.vector_index.embedding.fingerprint != frozen["fingerprint"]:
            raise JobError("RETRIEVAL_BINDING_CHANGED")

    def _projection_vectors(self):
        """Invalidation includes each registered backend and its old named collections."""
        vectors = [self.vector_index] if self.vector_index is not None else []
        if self.retrieval_registry is not None:
            for profile_id in self.retrieval_registry.enabled_ids():
                vector = self.retrieval_registry.resolve(profile_id).vector
                if vector is not None and all(vector is not previous for previous in vectors):
                    vectors.append(vector)
        return vectors

    def __call__(self, job_id):
        """Dispatch after the API commits job+outbox. Queue failures stay recoverable."""
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("DISPATCHER_CLOSED")
            if self.mode == "local":
                previous = self._futures.get(job_id)
                if previous is not None and not previous.done():
                    return previous
                result = self.pool.submit(self.run, job_id)
                self._futures[job_id] = result
            else:
                if self._celery is None:
                    from .celery_app import create_celery_app
                    self._celery = create_celery_app(self.settings)
                result = self._celery.send_task("fund_kb.run_job", args=[job_id], task_id=job_id)
        # Mark only this job's delivery events, never an unrelated invalidation event.
        with self.session_factory.begin() as db:
            db.execute(update(m.Outbox).where(m.Outbox.aggregate_id == job_id,
                m.Outbox.dispatched_at.is_(None), m.Outbox.event_type.in_(
                    ["JOB_CREATED", "JOB_RETRY", "JOB_RECOVERED"])).values(dispatched_at=now()))
        return result

    def _audit(self, db, job, action, details=None, outcome="SUCCESS"):
        db.add(m.AuditEvent(id=uid(), actor_id=job.owner_id, action=action, object_type="Job",
            object_id=job.id, trace_id=f"job:{job.id}:{job.attempts}", outcome=outcome,
            details={"attempt": job.attempts, "kind": job.kind, **(details or {})}))

    def _claim(self, job_id):
        instant = now()
        with self.session_factory.begin() as db:
            existing = db.scalar(select(m.Job).where(m.Job.id == job_id).with_for_update())
            if existing and (existing.payload or {}).get("manual_retry_only") and existing.state == "RUNNING" \
                    and (existing.lease_until is None or existing.lease_until <= instant):
                existing.state = "CANCELLED" if existing.cancel_requested else "FAILED"
                existing.stage = existing.state
                existing.error_code = "CANCELLED" if existing.cancel_requested else "MANUAL_RETRY_REQUIRED"
                existing.lease_until, existing.updated_at = None, instant
                if existing.run_id:
                    interrupted = db.get(m.ConsultationRun, existing.run_id)
                    if interrupted and interrupted.state not in {"COMPLETED", "CANCELLED"}:
                        interrupted.state, interrupted.error_code, interrupted.completed_at = existing.state, existing.error_code, instant
                self._audit(db, existing, "job.manual_retry_required", {"attempt": existing.attempts}, "FAILED")
                return None
            result = db.execute(update(m.Job).where(m.Job.id == job_id, or_(
                and_(m.Job.state == "QUEUED", or_(m.Job.lease_until.is_(None), m.Job.lease_until <= instant)),
                and_(m.Job.state == "RUNNING", or_(m.Job.lease_until.is_(None), m.Job.lease_until <= instant))
            )).values(state="RUNNING", stage="CLAIMED", attempts=m.Job.attempts + 1,
                      lease_until=instant + timedelta(seconds=self.lease_seconds), updated_at=instant)
                .execution_options(synchronize_session=False))
            if result.rowcount != 1:
                return None
            db.expire_all()
            job = db.get(m.Job, job_id)
            self._audit(db, job, "job.claimed")
            return job.attempts

    def _fence(self, db, job_id, attempt):
        instant = now()
        result = db.execute(update(m.Job).where(m.Job.id == job_id, m.Job.attempts == attempt,
            m.Job.state == "RUNNING", m.Job.lease_until > instant).values(
                lease_until=instant + timedelta(seconds=self.lease_seconds), updated_at=instant)
            .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise LeaseLost()
        db.expire_all()
        job = db.get(m.Job, job_id)
        if job.cancel_requested:
            raise Cancelled()
        self._check_retrieval_fence(db, job)
        return job

    def _checkpoint(self, job_id, attempt, stage, evidence=None):
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            job.stage = stage
            if evidence:
                job.result = {**(job.result or {}), **evidence}
            self._audit(db, job, "job.stage", {"stage": stage, **(evidence or {})})

    def _heartbeat(self, job_id, attempt, stopped):
        while not stopped.wait(max(.2, self.lease_seconds / 3)):
            try:
                with self.session_factory.begin() as db:
                    self._fence(db, job_id, attempt)
            except (LeaseLost, Cancelled):
                return
            except OperationalError:
                # Never extend an expired lease after a database outage.
                continue

    def run(self, job_id):
        attempt = self._claim(job_id)
        if attempt is None:
            return False
        return self.run_claimed(job_id, attempt)

    def run_claimed(self, job_id, attempt):
        """Execute a fenced lease, including leases atomically reserved by an operator."""
        stopped = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(job_id, attempt, stopped), daemon=True)
        heartbeat.start()
        try:
            with self.session_factory() as db:
                job = db.get(m.Job, job_id)
                kind = job.kind
            self._checkpoint(job_id, attempt, "STARTED")
            handler = getattr(self, "_" + kind.lower(), None)
            if handler is None:
                raise JobError("UNSUPPORTED_JOB_KIND")
            handler(job_id, attempt)
            return True
        except LeaseLost:
            return False
        except Exception as exc:  # noqa: BLE001 - every failed task must retain durable evidence.
            self._fail(job_id, attempt, exc)
            return False
        finally:
            stopped.set()
            heartbeat.join(timeout=2)

    def _fail(self, job_id, attempt, exc):
        code = getattr(exc, "code", "INTERNAL_ERROR")
        # Parser/API errors expose stable codes; arbitrary exception text may contain secrets/content.
        if not isinstance(code, str) or len(code) > 100:
            code = "INTERNAL_ERROR"
        from botocore.exceptions import ConnectionClosedError, EndpointConnectionError, ReadTimeoutError
        retryable = getattr(exc, "retryable", False) or isinstance(exc, (
            OperationalError, TimeoutError, ConnectionClosedError, EndpointConnectionError, ReadTimeoutError))
        with self.session_factory.begin() as db:
            result = db.execute(update(m.Job).where(m.Job.id == job_id, m.Job.attempts == attempt,
                m.Job.state == "RUNNING", m.Job.lease_until > now()).values(updated_at=now())
                .execution_options(synchronize_session=False))
            if result.rowcount != 1:
                return
            job = db.get(m.Job, job_id)
            cancelled = job.cancel_requested or isinstance(exc, Cancelled)
            retry = retryable and attempt <= len(self.RETRY_DELAYS) and not cancelled \
                and not (job.payload or {}).get("manual_retry_only", False)
            if retry and job.kind == "ANSWER":
                paid_run = db.get(m.ConsultationRun, job.run_id or job.payload.get("run_id"))
                if paid_run and (paid_run.model_snapshot or {}).get("model_request_count", 0):
                    # A later DB/transport failure must not silently replay a
                    # billable model-first analysis or synthesis request.
                    retry = False
            job.state = "CANCELLED" if cancelled else "QUEUED" if retry else "FAILED"
            job.stage = "RETRY_WAIT" if retry else job.state
            job.error_code = "CANCELLED" if cancelled else code
            job.lease_until = now() + timedelta(seconds=self.RETRY_DELAYS[attempt - 1]) if retry else None
            self._audit(db, job, "job.failed", {"error_code": job.error_code, "will_retry": retry}, "FAILED")
            if retry:
                db.add(m.Outbox(id=uid(), event_type="JOB_RETRY", aggregate_id=job.id,
                               payload={"job_id": job.id, "attempt": attempt}))
            if job.kind == "PUBLISH":
                release = db.get(m.Release, stable_id(job.id, "release"))
                if release and release.state == "PREPARING":
                    release.state = "FAILED"
                    release.manifest = {**(release.manifest or {}), "error_code": code}
            if job.kind == "ANSWER":
                run = db.get(m.ConsultationRun, job.payload.get("run_id") or job.run_id)
                thread = db.get(m.ConsultationThread, run.thread_id) if run else None
                if run and thread and thread.owner_id == job.owner_id and run.state != "COMPLETED":
                    run.state = "CANCELLED" if cancelled else "QUEUED" if retry else "FAILED"
                    run.error_code = job.error_code
                    run.completed_at = None if retry else now()

    def _succeed(self, db, job, result):
        if job.cancel_requested:
            raise Cancelled()
        job.state, job.stage, job.error_code = "SUCCEEDED", "COMPLETED", None
        job.result = {**(job.result or {}), **result}
        job.lease_until, job.updated_at = None, now()
        self._audit(db, job, "job.succeeded", result)
        if self.retrieval_registry is not None or (self.vector_index is not None and self.settings.retrieval_mode == "hybrid" \
                and self.settings.hybrid_index_auto_sync):
            from types import SimpleNamespace
            from .vector_indexing import queue_resource_index
            resource_ids = set()
            if job.kind in {"SCAN_PARSE", "PUBLISH", "INVALIDATE"} and job.resource_id:
                resource_ids.add(job.resource_id)
            if job.kind == "COMPILE" and (job.payload or {}).get("task") != "VECTOR_INDEX":
                resource_ids.update((result or {}).get("created_resource_ids", []))
                if (result or {}).get("resource_id"):
                    resource_ids.add(result["resource_id"])
            for resource_id in sorted(resource_ids):
                try:
                    ctx = SimpleNamespace(db=db,user=self._user(db,job),settings=self.settings,
                        retrieval_registry=self.retrieval_registry, data={},dispatch=[],
                        request=SimpleNamespace(state=SimpleNamespace(trace_id=f"job:{job.id}:{job.attempts}")))
                    queue_resource_index(ctx, resource_id)
                except svc.APIError as exc:
                    self._audit(db, job, "vector.followup_not_authorized", {"code":exc.code})

    def recover(self):
        """Redeliver queued jobs and expired leases; also run periodic recovery locally."""
        count = self._recover_once()
        with self._lock:
            if self._recovery is None and not self._stop.is_set():
                self._recovery = threading.Thread(target=self._recovery_loop, name="fund-kb-recovery", daemon=True)
                self._recovery.start()
        return count

    def _recover_once(self):
        if self._stop.is_set():
            return 0
        with self.session_factory() as db:
            ids = list(db.scalars(select(m.Job.id).where(m.Job.state.in_(["QUEUED", "RUNNING"]),
                or_(m.Job.lease_until.is_(None), m.Job.lease_until <= now()))
                .order_by(m.Job.created_at, m.Job.id).limit(500)))
        dispatched = 0
        for job_id in ids:
            try:
                self(job_id)
                dispatched += 1
            except Exception:  # noqa: BLE001 - dispatch boundary; queue/SDK exceptions differ.
                # Retain pending job/outbox; record metadata without broker URLs/credentials.
                with self.session_factory.begin() as db:
                    job = db.get(m.Job, job_id)
                    self._audit(db, job, "job.dispatch_failed", {"error_code": "QUEUE_UNAVAILABLE"}, "FAILED")
        return dispatched

    def _recovery_loop(self):
        delay = max(.2, float(getattr(self.settings, "job_recovery_interval_seconds", 5)))
        while not self._stop.wait(delay):
            try:
                self._recover_once()
                with self._lock:
                    self._futures = {k: v for k, v in self._futures.items() if not v.done()}
            except OperationalError:
                continue

    def close(self):
        if self._job_retrieval_binding is not None or getattr(self, "_retrieval_profile_bound", False):
            return  # A scoped execution view never owns shared pools/bridges/stores.
        self._stop.set()
        if self._recovery:
            self._recovery.join(timeout=2)
        if self.pool:
            self.pool.shutdown(wait=True, cancel_futures=False)
        if self._celery:
            self._celery.close()
        if self._owns_retrieval_registry:
            self.retrieval_registry.close()
            self._owns_retrieval_registry = False
        self.storage.close()

    def _user(self, db, job):
        user = db.scalar(select(m.User).where(m.User.id == job.owner_id).with_for_update()
                         .execution_options(populate_existing=True))
        if not user or not user.active:
            raise JobError("REQUESTER_INACTIVE")
        return user

    def _version(self, db, user, version_id, action="read"):
        candidate = db.get(m.ResourceVersion, version_id)
        if not candidate:
            raise JobError("NOT_FOUND")
        resource = db.scalar(select(m.Resource).where(m.Resource.id == candidate.resource_id).with_for_update()
                             .execution_options(populate_existing=True))
        db.scalar(select(m.Space).where(m.Space.id == resource.space_id).with_for_update())
        version = svc.version_access(db, user, version_id, action)
        resource = svc.require_resource(db, user, version.resource_id, action)
        return version, resource

    def _write_blocks(self, db, version, blocks):
        from .ingestion import block_text, text_sha256
        for ordinal, block in enumerate(blocks):
            text = block_text(block)
            db.add(m.ContentBlock(version_id=version.id, block_id=block["block_id"], ordinal=ordinal,
                block_type=block["block_type"], data=block["data"], locator=block.get("locator", {}),
                search_text=text, content_sha256=text_sha256(text)))
        db.flush()
        for block in blocks:
            for citation in block.get("citations", []):
                db.add(m.EvidenceLink(id=uid(), from_version_id=version.id, from_block_id=block["block_id"],
                    to_version_id=citation["version_id"], to_block_id=citation["block_id"],
                    purpose=citation["purpose"]))
        db.flush()
        version.content_sha256 = svc.check_frozen_hash(db, version)

    def _scan_parse(self, job_id, attempt):
        with self.session_factory() as db:
            job = db.get(m.Job, job_id)
            user = self._user(db, job)
            upload = db.get(m.Upload, job.payload["upload_id"])
            if not upload or upload.user_id != user.id or upload.state != "SEALED":
                raise JobError("UPLOAD_NOT_SEALED")
            version, resource = self._version(db, user, upload.version_id, "edit")
            if version.state != "DRAFT" or version.source_blob_id:
                raise JobError("ORIGINAL_IMMUTABLE")
            if svc.version_blocks(db, version.id):
                raise JobError("DRAFT_CONTENT_CONFLICT")
            revision = version.revision
            parts = list(db.scalars(select(m.UploadPart).where(m.UploadPart.upload_id == upload.id)
                                   .order_by(m.UploadPart.part_no)))
            if [p.part_no for p in parts] != list(range(1, upload.part_count + 1)):
                raise JobError("UPLOAD_PARTS_MISSING")
        chunks = []
        total = 0
        for part in parts:
            expected_key = f"uploads/{upload.id}/{part.part_no}"
            if part.object_key != expected_key:
                raise JobError("UPLOAD_PART_KEY_INVALID")
            chunk = self.storage.read_bytes(expected_key)
            total += len(chunk)
            if total > self.settings.max_file_bytes or len(chunk) != part.size_bytes or sha(chunk) != part.sha256:
                raise JobError("UPLOAD_INTEGRITY_FAILED")
            chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) != upload.declared_size or (upload.expected_sha256 and sha(data) != upload.expected_sha256):
            raise JobError("UPLOAD_INTEGRITY_FAILED")
        quarantine_key = f"quarantine/{job_id}/{safe_filename(upload.filename)}"
        self.storage.write_bytes(quarantine_key, data)
        self._checkpoint(job_id, attempt, "SCANNING", {"source_sha256": sha(data)})
        mime, report, warnings = scan_file(self.settings, self.storage.local_path(quarantine_key), upload.filename)
        self._checkpoint(job_id, attempt, "PARSING", {"scan": report})
        parsed = self._parse_bounded(job_id, attempt, self.storage.local_path(quarantine_key), upload.filename)
        warnings += parsed.warnings
        blob_id = stable_id(upload.id, "blob")
        blob_key = f"blobs/{blob_id}/{safe_filename(upload.filename)}"
        preview_key = f"previews/{version.id}.html"
        self.storage.write_bytes(blob_key, data)
        self.storage.write_bytes(preview_key, parsed.preview_html.encode())
        self._checkpoint(job_id, attempt, "PARSED", {"preview_sha256": sha(parsed.preview_html.encode())})
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            user = self._user(db, job)
            version, resource = self._version(db, user, upload.version_id, "edit")
            current_upload = db.get(m.Upload, upload.id)
            if current_upload.state != "SEALED" or version.state != "DRAFT" or version.revision != revision:
                raise JobError("DRAFT_CHANGED")
            if version.source_blob_id or svc.version_blocks(db, version.id):
                raise JobError("ORIGINAL_IMMUTABLE")
            db.add(m.Blob(id=blob_id, space_id=resource.space_id, object_key=blob_key, sha256=sha(data),
                          size_bytes=len(data), mime_type=mime, scan_state="CLEAN"))
            db.flush()
            version.source_blob_id, version.origin, version.source_verified = blob_id, "UPLOAD", False
            version.revision += 1
            self._write_blocks(db, version, parsed.blocks)
            self._succeed(db, job, {"version_id": version.id, "source_blob_id": blob_id,
                "preview_available": bool(parsed.preview_html), "block_count": len(parsed.blocks),
                "warnings": warnings})

    def _parse_bounded(self, job_id, attempt, path, filename):
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        child = context.Process(target=_parse_child, args=(sending, str(path), filename), daemon=True)
        child.start()
        sending.close()
        deadline = time.monotonic() + min(600, float(getattr(self.settings, "parse_timeout_seconds", 600)))
        try:
            while time.monotonic() < deadline:
                if receiving.poll(.2):
                    try:
                        status, payload = receiving.recv()
                    except EOFError:
                        raise JobError("PARSER_PROCESS_FAILED") from None
                    if status == "ok":
                        return payload
                    raise JobError(payload)
                if not child.is_alive():
                    raise JobError("PARSER_PROCESS_FAILED")
                with self.session_factory() as db:
                    job = db.get(m.Job, job_id)
                    if job.cancel_requested:
                        raise Cancelled()
                    if job.attempts != attempt or job.state != "RUNNING" or job.lease_until <= now():
                        raise LeaseLost()
            raise JobError("PARSE_TIMEOUT")
        finally:
            receiving.close()
            if child.is_alive():
                child.terminate()
            child.join(timeout=2)
            if child.is_alive():
                child.kill()
                child.join(timeout=2)

    def _compile(self, job_id, attempt):
        with self.session_factory() as db:
            current = db.get(m.Job, job_id)
            subtask = (current.payload or {}).get("task") if current else None
        if subtask == "VECTOR_INDEX":
            scoped = self._retrieval_job_view(job_id, attempt)
            if scoped is not self:
                return scoped._compile(job_id, attempt)
            from .vector_indexing import run_index_job
            run_index_job(self, job_id, attempt)
            return
        if subtask in {"MODEL_SYNC", "MODEL_TEST"}:
            from .providers import run_connection_job
            result = run_connection_job(self.settings, self.session_factory, job_id, attempt,
                lambda stage, details=None: self._checkpoint(job_id, attempt, stage, details or {}), bridge=self.codex_bridge)
        elif subtask in {"CODEX_LOGIN_START", "CODEX_AUTH_REFRESH", "CODEX_LOGIN_CANCEL", "CODEX_LOGOUT"}:
            from .api_oauth import run_oauth_job
            result = run_oauth_job(self.settings, self.session_factory, job_id, attempt,
                lambda stage, details=None: self._checkpoint(job_id, attempt, stage, details or {}), bridge=self.codex_bridge)
        elif subtask == "WIKI_BUILD":
            from .wiki import execute_build
            result = execute_build(self.settings, self.session_factory, job_id, attempt,
                lambda stage, details=None: self._checkpoint(job_id, attempt, stage, details or {}))
        elif subtask == "GUIDANCE_NORMALIZE":
            from .documents import execute_guidance_normalize
            result = execute_guidance_normalize(self.settings, self.session_factory, job_id, attempt,
                lambda stage, details=None: self._checkpoint(job_id, attempt, stage, details or {}))
        if subtask in {"MODEL_SYNC", "MODEL_TEST", "WIKI_BUILD", "GUIDANCE_NORMALIZE", "CODEX_LOGIN_START", "CODEX_AUTH_REFRESH", "CODEX_LOGIN_CANCEL", "CODEX_LOGOUT"}:
            with self.session_factory.begin() as db:
                job = self._fence(db, job_id, attempt)
                user = self._user(db, job)
                if subtask == "WIKI_BUILD":
                    from .wiki import authorize_build_result
                    authorize_build_result(db, user, job)
                elif subtask == "GUIDANCE_NORMALIZE":
                    from .documents import guard_guidance_job
                    guard_guidance_job(db, user, job, self.settings)
                else:
                    from .providers import authorize_model_job
                    authorize_model_job(db, user, job, self.settings, action="complete")
                self._succeed(db, job, result)
            return
        from .ai import compile_knowledge
        with self.session_factory() as db:
            job = db.get(m.Job, job_id)
            user = self._user(db, job)
            source, resource = self._version(db, user, job.payload["version_id"])
            if resource.suspended:
                raise JobError("SOURCE_SUSPENDED")
            target = job.payload["target_space_id"]
            svc.space_access(db, user, target, "editor")
            template_id = job.payload.get("template_version_id")
            if template_id:
                template, template_resource = self._version(db, user, template_id)
                if template_resource.kind != "template" or not svc.released(db, template.id):
                    raise JobError("TEMPLATE_NOT_PUBLISHED")
            blocks = svc.version_blocks(db, source.id)
            source_hash = svc.check_frozen_hash(db, source)
            if not blocks:
                raise JobError("SOURCE_TEXT_UNAVAILABLE")
            if source.source_blob_id and db.get(m.Blob, source.source_blob_id).scan_state != "CLEAN":
                raise JobError("SOURCE_NOT_CLEAN")
        started = time.monotonic()
        content = compile_knowledge(blocks, source.id, source.title, job.payload.get("knowledge_type", "sop"))
        if time.monotonic() - started > 180:
            raise JobError("COMPILE_TIMEOUT")
        new_resource_id, version_id = stable_id(job_id, "resource"), stable_id(job_id, "draft")
        self._checkpoint(job_id, attempt, "COMPILED", {"compiled_sha256": sha(json_bytes(content))})
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            user = self._user(db, job)
            source, resource = self._version(db, user, source.id)
            svc.space_access(db, user, target, "editor")
            if source_hash != svc.check_frozen_hash(db, source) or resource.suspended:
                raise JobError("SOURCE_CHANGED")
            if template_id:
                self._version(db, user, template_id)
            compiled = m.Resource(id=new_resource_id, space_id=target, kind="knowledge", owner_id=user.id,
                name=content["title"], category=resource.category, restricted=True,
                classification=resource.classification)
            db.add(compiled)
            db.flush()
            # Compilation must not widen a private source into an unrestricted target.
            for permission in ("read", "download", "edit", "manage"):
                db.add(m.ResourceGrant(resource_id=compiled.id, user_id=user.id, permission=permission))
            version = m.ResourceVersion(id=version_id, resource_id=compiled.id, version_no=1, author_id=user.id,
                title=content["title"], knowledge_type=content["knowledge_type"], origin="AI_DRAFT", state="DRAFT",
                applicability=content.get("applicability", {}), required_facts=content.get("required_facts", []),
                legal_status=content.get("legal_status", "UNKNOWN"), source_verified=False,
                change_reason="原文提取整理；待人工编辑、独立复核和发布")
            for key in ("valid_from", "valid_to"):
                value = content.get(key)
                setattr(version, key, date.fromisoformat(value) if isinstance(value, str) else value)
            db.add(version)
            db.flush()
            self._write_blocks(db, version, content["blocks"])
            if not any(b.get("citations") for b in content["blocks"]):
                raise JobError("COMPILE_MISSING_CITATIONS")
            self._succeed(db, job, {"resource_id": compiled.id, "draft_version_id": version.id,
                "source_version_ids": [source.id], "warnings": [
                    "EXTRACTIVE_DRAFT: 原文提取整理，不代表LLM推理效果或获批专业规则。",
                    *(["TEMPLATE_REFERENCE_ONLY: 已核验模板访问权；提取整理不自动执行模板规则。"] if template_id else [])]})

    def _publish_checks(self, db, user, version_id):
        version, resource = self._version(db, user, version_id, "publish")
        if resource.suspended:
            raise JobError("RESOURCE_SUSPENDED")
        if version.state != "APPROVED" or not version.content_sha256:
            raise JobError("VERSION_NOT_APPROVED")
        if svc.check_frozen_hash(db, version) != version.content_sha256:
            raise JobError("APPROVED_CONTENT_CHANGED")
        from .admin_review import confirmation, source_dependencies
        from .wiki import assert_formal_wiki
        admin_confirmed = confirmation(db, version)
        review = svc.independent_review(db, version)
        if not review and not admin_confirmed:
            raise JobError("INDEPENDENT_REVIEW_REQUIRED")
        if admin_confirmed:
            source_dependencies(db, user, version)
        else:
            assert_formal_wiki(db, version)
        if version.knowledge_type == "source" and not version.source_verified:
            raise JobError("SOURCE_NOT_VERIFIED")
        if resource.kind == "document" and not version.source_blob_id:
            raise JobError("ORIGINAL_REQUIRED")
        if version.source_blob_id:
            blob = db.get(m.Blob, version.source_blob_id)
            if not blob or blob.scan_state != "CLEAN" or blob.space_id != resource.space_id:
                raise JobError("SOURCE_NOT_CLEAN")
            if not self.storage.exists(blob.object_key) or sha(self.storage.read_bytes(blob.object_key)) != blob.sha256:
                raise JobError("ORIGINAL_INTEGRITY_FAILED")
        current = db.get(m.Release, resource.active_release_id) if resource.active_release_id else None
        if current and current.version_id != version.id and version.base_version_id != current.version_id:
            raise JobError("PUBLISH_BASE_CONFLICT")
        if not admin_confirmed and version.legal_status != "NOT_APPLICABLE" and not version.valid_from:
            raise JobError("EFFECTIVE_DATE_REQUIRED")
        # The effective date belongs to the reviewed snapshot, not a worker mutation.
        if not admin_confirmed and not version.valid_from:
            raise JobError("EFFECTIVE_DATE_REQUIRED")
        dependencies = []

        def visit(candidate, ancestors):
            if candidate.id in ancestors or len(ancestors) >= 8:
                raise JobError("DEPENDENCY_CYCLE")
            ids = set(svc.dependency_ids(db, candidate))
            for edge in db.scalars(select(m.RelationEdge).where(m.RelationEdge.source_version_id == candidate.id)):
                target = svc.require_resource(db, user, edge.target_resource_id)
                if target.suspended:
                    raise JobError("DEPENDENCY_SUSPENDED")
                if edge.relation_type == "DEPENDS_ON" and not edge.evidence_version_id:
                    release = db.get(m.Release, target.active_release_id) if target.active_release_id else None
                    if not release:
                        raise JobError("DEPENDENCY_NOT_PUBLISHED")
                    ids.add(release.version_id)
            for dependency_id in sorted(ids):
                dep, target = self._version(db, user, dependency_id)
                if target.suspended or dep.state != "APPROVED" or not svc.released(db, dep.id):
                    raise JobError("DEPENDENCY_NOT_PUBLISHED")
                if dep.knowledge_type == "source" and not dep.source_verified:
                    raise JobError("DEPENDENCY_NOT_VERIFIED")
                if dep.source_blob_id:
                    blob = db.get(m.Blob, dep.source_blob_id)
                    if not blob or blob.scan_state != "CLEAN":
                        raise JobError("DEPENDENCY_NOT_CLEAN")
                if dep.content_sha256 != svc.check_frozen_hash(db, dep):
                    raise JobError("DEPENDENCY_CHANGED")
                dependencies.append({"resource_id": target.id, "version_id": dep.id,
                                     "content_sha256": dep.content_sha256, "access_epoch": target.access_epoch})
                visit(dep, (*ancestors, candidate.id))
        visit(version, ())
        return version, resource, current, dependencies

    def _publish(self, job_id, attempt):
        from .ingestion import PARSER_VERSION, render_blocks
        release_id = stable_id(job_id, "release")
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            user = self._user(db, job)
            version, resource, current, dependencies = self._publish_checks(db, user, job.payload["version_id"])
            if current and current.version_id == version.id:
                self._succeed(db, job, {"resource_id": resource.id, "version_id": version.id,
                    "release_id": current.id, "activated_at": svc.primitive(current.activated_at)})
                return
            blocks = svc.version_blocks(db, version.id)
            from .admin_review import confirmation
            admin_confirmed = confirmation(db, version)
            original_only = bool(admin_confirmed and resource.kind == "document" and version.source_blob_id and not blocks)
            if not blocks and not original_only:
                raise JobError("EMPTY_CONTENT")
            expected_release, frozen = resource.active_release_id, version.content_sha256
            release = db.get(m.Release, release_id)
            if not release:
                release = m.Release(id=release_id, resource_id=resource.id, version_id=version.id,
                                    state="PREPARING", publisher_id=user.id, manifest={})
                db.add(release)
            else:
                release.state = "PREPARING"
            self._audit(db, job, "release.preparing", {"release_id": release_id, "content_sha256": frozen})
        artifacts = {}
        for format, extension in (("html", "html"), ("markdown", "md")):
            content = render_blocks(blocks, format).encode()
            key = f"releases/{release_id}/content.{extension}"
            self.storage.write_bytes(key, content)
            artifacts[format] = {"object_key": key, "sha256": sha(content), "size_bytes": len(content)}
        records = []
        from .ingestion import block_text, text_sha256
        for block in blocks:
            text = block_text(block)
            records.append({"resource_id": resource.id, "version_id": version.id, "block_id": block["block_id"],
                "release_id": release_id, "title": version.title, "text": text,
                "locator": block.get("locator", {}), "content_sha256": text_sha256(text)})
        index_key = f"releases/{release_id}/index.json"
        index_data = json_bytes(records)
        self.storage.write_bytes(index_key, index_data)
        if self.vector_index is not None and self.settings.embedding_mode == "hashing":
            self.vector_index.upsert(records)
        artifacts["index"] = {"object_key": index_key, "sha256": sha(index_data), "record_count": len(records)}
        manifest = {"release_id": release_id, "version_id": version.id, "content_sha256": frozen,
            "artifacts": artifacts, "dependencies": dependencies, "parser_version": PARSER_VERSION,
            "model_mode": self.settings.llm_provider,
            "embedding": self.vector_index.status() if self.vector_index is not None else {"state": "disabled", "mode": "wiki"},
            "prompt_version": "extractive-v1", "required_artifacts": ["html", "markdown", "index"]}
        if admin_confirmed:
            manifest["review"] = admin_confirmed
            manifest["content_mode"] = "original_only" if original_only else "parsed"
            manifest["legal_effect_confirmed"] = False
        manifest_key = f"releases/{release_id}/manifest.json"
        self.storage.write_bytes(manifest_key, json_bytes(manifest))
        self._checkpoint(job_id, attempt, "RELEASE_PREPARED", {"manifest_sha256": sha(json_bytes(manifest))})
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            user = self._user(db, job)
            version, resource, current, current_dependencies = self._publish_checks(db, user, version.id)
            if (resource.active_release_id != expected_release or version.content_sha256 != frozen
                    or current_dependencies != dependencies):
                raise JobError("PUBLISH_SOURCE_CHANGED")
            for artifact in artifacts.values():
                if sha(self.storage.read_bytes(artifact["object_key"])) != artifact["sha256"]:
                    raise JobError("RELEASE_ARTIFACT_INVALID")
            if sha(self.storage.read_bytes(manifest_key)) != sha(json_bytes(manifest)):
                raise JobError("RELEASE_MANIFEST_INVALID")
            release = db.get(m.Release, release_id)
            if current:
                current.state = "SUPERSEDED"
                db.flush()  # Portable generated uniqueness columns allow only one ACTIVE.
            release.manifest, release.state, release.activated_at = manifest, "ACTIVE", now()
            db.flush()
            resource.active_release_id = release.id
            resource.revision += 1
            db.add(m.Outbox(id=uid(), event_type="RELEASE_ACTIVATED", aggregate_id=resource.id,
                           payload={"resource_id": resource.id, "version_id": version.id, "release_id": release.id}))
            if current:
                invalidation_id = stable_id(job_id, "invalidate")
                db.add(m.Job(id=invalidation_id, kind="INVALIDATE", owner_id=user.id, resource_id=resource.id,
                    state="QUEUED", dedupe_key=f"release:{release.id}:invalidate", payload={
                        "resource_id": resource.id, "reason": "SOURCE_UPDATED", "release_id": release.id}))
                db.flush()
                db.add(m.Outbox(id=uid(), event_type="JOB_CREATED", aggregate_id=invalidation_id,
                               payload={"job_id": invalidation_id}))
            self._succeed(db, job, {"resource_id": resource.id, "version_id": version.id,
                "release_id": release.id, "activated_at": svc.primitive(release.activated_at)})

    def _run_context(self, db, job, *, read_only=False, validate_attachments=True):
        user = db.get(m.User, job.owner_id) if read_only else self._user(db, job)
        if not user or not user.active:
            raise JobError("REQUESTER_INACTIVE")
        run = db.get(m.ConsultationRun, job.payload["run_id"])
        thread = db.get(m.ConsultationThread, run.thread_id) if run else None
        if not thread or thread.owner_id != user.id or thread.deleted_at:
            raise JobError("RUN_NOT_ACCESSIBLE")
        svc.space_access(db, user, thread.space_id)
        parent_id = run.request.get("parent_run_id") or run.parent_run_id
        if parent_id:
            parent = db.get(m.ConsultationRun, parent_id)
            if not parent or parent.thread_id != thread.id:
                raise JobError("PARENT_RUN_NOT_ACCESSIBLE")
            if validate_attachments:
                source_ids = {item["version_id"] for item in parent.evidence_snapshot or []}
                source_ids.update(item["version_id"] for item in (parent.response or {}).get("citations", []))
                for source_id in source_ids:
                    svc.version_access(db, user, source_id)
        for version_id in run.request.get("attachment_version_ids", []) if validate_attachments else []:
            attachment = svc.version_access(db, user, version_id) if read_only else self._version(db, user, version_id)[0]
            blob = db.get(m.Blob, attachment.source_blob_id) if attachment.source_blob_id else None
            if not blob or blob.scan_state != "CLEAN":
                raise JobError("ATTACHMENT_NOT_CLEAN")
        return user, run, thread

    @staticmethod
    def _validate_answer(answer, evidence):
        schema_path = Path(__file__).resolve().parents[2] / "contracts" / "answer.schema.json"
        schema = json.loads(schema_path.read_text())
        if list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(answer)):
            raise JobError("ANSWER_SCHEMA_INVALID")
        records = {(e["version_id"], e["block_id"]): e for e in evidence}
        citations = answer["citations"]
        citation_ids = {c["id"] for c in citations}
        if len(citation_ids) != len(citations):
            raise JobError("ANSWER_CITATIONS_INVALID")
        for citation in citations:
            record = records.get((citation["version_id"], citation["block_id"]))
            if (not record or citation["resource_id"] != record["resource_id"]
                    or citation["content_sha256"] != record["content_sha256"]
                    or citation["excerpt"] not in record["text"]):
                raise JobError("ANSWER_CITATIONS_INVALID")
        for claim in answer["claims"]:
            if not set(claim["evidence_ids"]) <= citation_ids:
                raise JobError("ANSWER_CITATIONS_INVALID")
        steps = (answer.get("solution") or {}).get("steps", [])
        seen = set()
        for step in steps:
            if (step["id"] in seen or not set(step["depends_on"]) <= seen
                    or not set(step["evidence_ids"]) <= citation_ids):
                raise JobError("ANSWER_STEPS_INVALID")
            seen.add(step["id"])

    def _answer_policy(self, db, run):
        selection = (run.request or {}).get("model_selection")
        if selection:
            from .providers import public_snapshot, resolve_connection
            thread = db.get(m.ConsultationThread, run.thread_id)
            user = db.get(m.User, thread.owner_id)
            connection = resolve_connection(db, user, thread.space_id, selection["connection_id"],
                selection["model_id"], self.settings, require_transfer=True,
                expected_revision=(run.model_snapshot or {}).get("revision"))
            retrieval_selection = (run.model_snapshot or {}).get("retrieval_selection")
            run.model_snapshot = {**public_snapshot(connection),
                **({"retrieval_selection": retrieval_selection} if retrieval_selection is not None else {})}
            run.policy_snapshot = {"source": "model-connection", "worker_frozen": True,
                "frozen_at": svc.primitive(now()), "provider_ref": "model-connection",
                "generation_model": connection["model_id"], "evaluation_id": "NOT_EVALUATED",
                "enable_vector": self.vector_index is not None, "model": run.model_snapshot,
                "prompt_version": "v1", "retrieval_mode": self.settings.retrieval_mode}
            return self.settings.model_copy(update={"llm_provider": "http", "llm_model": connection["model_id"],
                "llm_base_url": connection["base_url"]}), self.vector_index
        if (run.policy_snapshot or {}).get("worker_frozen"):
            snapshot = dict(run.policy_snapshot)
        else:
            policy = db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == "model-policy"))
            if policy:
                snapshot = {**policy.config, "runtime_policy_id": policy.id, "revision": policy.revision,
                            "source": "RuntimePolicy"}
            else:
                snapshot = {"provider_ref": "configured-http" if self.settings.llm_provider == "http" else "evidence",
                    "generation_model": self.settings.llm_model if self.settings.llm_provider == "http" else "evidence-only",
                    "extraction_model": "deterministic-extraction", "enable_vector": self.vector_index is not None,
                    "prompt_version": "v1", "source": "environment", "evaluation_id": "NOT_EVALUATED"}
            snapshot.update(worker_frozen=True, frozen_at=svc.primitive(now()))
        if snapshot.get("prompt_version") not in SUPPORTED_PROMPT_VERSIONS:
            raise JobError("POLICY_TEMPLATE_UNAVAILABLE")
        if snapshot.get("extraction_model") != "deterministic-extraction":
            raise JobError("POLICY_EXTRACTION_UNAVAILABLE")
        if not isinstance(snapshot.get("enable_vector"), bool):
            raise JobError("POLICY_INVALID")
        provider = snapshot.get("provider_ref")
        if provider == "evidence":
            effective = self.settings.model_copy(update={"llm_provider": "evidence", "llm_model": None})
        elif provider == "configured-http":
            model = snapshot.get("generation_model")
            if snapshot.get("source") == "RuntimePolicy" and (
                    self.settings.llm_provider != "http" or not self.settings.llm_base_url or not model):
                raise JobError("MODEL_NOT_CONFIGURED")
            effective = self.settings.model_copy(update={"llm_provider": "http", "llm_model": model})
        else:
            raise JobError("POLICY_PROVIDER_UNAVAILABLE")
        run.policy_snapshot = snapshot
        return effective, self.vector_index if snapshot["enable_vector"] else None

    def _record_answer_model_response(self, job_id, attempt, phase, raw, duration, reasoning_requested=None):
        # Only operational metadata; never persist the provider's private
        # reasoning, transport credentials or unvalidated candidate text here.
        from .answer_execution import receipt
        details = receipt(raw, duration)
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            run = db.get(m.ConsultationRun, job.run_id)
            previous = (run.model_snapshot or {}).get("last_request") or {}
            run.model_snapshot = {**(run.model_snapshot or {}), "last_request": {
                **previous, **details, "phase": phase, "state": "received", "attempt": attempt}}
            if phase == "synthesis":
                job.stage = "VALIDATING_ANSWER"
            self._audit(db, job, "answer.model_response_received", {"phase": phase,
                **details, "reasoning_requested": details.get("reasoning_requested", reasoning_requested)})

    def _record_answer_model_failure(self, job_id, attempt, phase, code, duration, diagnostic=None):
        # Transport/normalization failures must not leave a false 'thinking'
        # state. Never attach provider exception text or unvalidated content.
        code = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", code) else "PROVIDER_FAILURE"
        from .provider_contracts import safe_diagnostic
        safe = safe_diagnostic(diagnostic or {})
        returned = safe.get("outcome") == "completed" and safe.get("finish_reason") == "stop"
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            run = db.get(m.ConsultationRun, job.run_id)
            details = {**safe, "phase": phase, "state": "received" if returned else "failed", "attempt": attempt,
                "duration_ms": max(0, round(duration * 1000)), "error_code": code}
            run.model_snapshot = {**(run.model_snapshot or {}), "last_request": {
                **((run.model_snapshot or {}).get("last_request") or {}), **details}}
            self._audit(db, job, "answer.model_request_failed", details)

    def _answer(self, job_id, attempt):
        scoped = self._retrieval_job_view(job_id, attempt)
        if scoped is not self:
            return scoped._answer(job_id, attempt)
        if self.settings.answer_engine == "wiki_reader":
            with self.session_factory() as db:
                current = db.get(m.Job, job_id)
                target = db.get(m.ConsultationRun, current.run_id) if current else None
                use_wiki_reader = bool(target and (target.request or {}).get("reasoning_strategy") == "model_first")
            if use_wiki_reader:
                from .wiki_answer_job import run_wiki_answer
                return run_wiki_answer(self, job_id, attempt)
        from .ai import AnswerValidationError, generate_answer
        from .answer_execution import phase_limits
        from .answer_retrieval import retrieve_answer_context
        from .reference_evidence import evidence_signature, reference_evidence
        from .retrieval import rank_evidence
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            user, run, thread = self._run_context(db, job, validate_attachments=False)
            if run.state == "COMPLETED":
                raise JobError("RUN_ALREADY_COMPLETED")
            context, request = dict(run.request.get("context", {})), dict(run.request)
            answer_scope = request.get("answer_scope", "formal")
            reference = answer_scope == "reference"
            strategy = request.get("reasoning_strategy", "evidence_first")
            previous_calls = int((run.model_snapshot or {}).get("model_request_count", 0))
            previous_analysis = (run.policy_snapshot or {}).get('question_analysis') if attempt > 1 and (run.policy_snapshot or {}).get('reuse_question_analysis') else None
            if previous_analysis:
                from .answer_planning import _validate as validate_plan
                saved_plan = validate_plan(previous_analysis.get('plan'))
                plan_hash = svc.digest(saved_plan)
                receipts = db.scalars(select(m.AuditEvent).where(m.AuditEvent.object_id == job_id,
                    m.AuditEvent.action == 'answer.planning_completed')).all()
                if not any((event.details or {}).get('plan_sha256') == plan_hash for event in receipts):
                    raise JobError('PLANNING_RECEIPT_MISSING')
            generation_settings, policy_index = self._answer_policy(db, run)
            if previous_analysis:
                run.policy_snapshot = {**run.policy_snapshot, 'question_analysis': previous_analysis}
            if (request.get("require_model") or strategy == "model_first") and generation_settings.llm_provider != "http":
                raise JobError("MODEL_REQUIRED")
            run.state, run.error_code, run.completed_at = "RUNNING", None, None
            run.model_snapshot = {**(run.model_snapshot or {}), "model_invoked": False,
                "execution_mode": None, "evidence_count": None, "attempt": attempt,
                "model_request_count": previous_calls, "planning_model_invoked": False,
                "answer_model_invoked": False, "reasoning_strategy": strategy, 'planning_reused': bool(previous_analysis),
                "last_request": None, "validation_status": "pending"}
            job.stage = "PLANNING_QUESTION" if strategy == "model_first" and not previous_analysis else "RETRIEVING"
            self._audit(db, job, "answer.planning_started" if strategy == "model_first" and not previous_analysis else "answer.retrieval_started",
                {"attempt": attempt, "local_sources_loaded": 0, 'reused_analysis': bool(previous_analysis)})
            if previous_analysis:
                self._audit(db, job, 'answer.planning_reused', {'plan_sha256': plan_hash, 'additional_model_calls': 0})
        model_plan = saved_plan if previous_analysis else None
        if strategy == "model_first" and model_plan is None:
            from .answer_planning import PLANNING_SCHEMA, PlanningError, plan_question
            from .ai_transport import ProviderError as LegacyProviderError
            from . import providers
            planning_transport = {}

            def planning_client(payload):
                # This guard deliberately does not resolve any document,
                # attachment, candidate or Wiki title before the first call.
                with self.session_factory.begin() as db:
                    current_job = self._fence(db, job_id, attempt)
                    actor, current, current_thread = self._run_context(db, current_job, validate_attachments=False)
                    choice = request.get("model_selection")
                    connection = providers.resolve_connection(db, actor, current_thread.space_id,
                        choice["connection_id"], choice["model_id"], self.settings, require_transfer=True,
                        expected_revision=(current.model_snapshot or {}).get("revision")) if choice else None
                    limits = phase_limits(self.settings, connection, "planning")
                    current.model_snapshot = {**(current.model_snapshot or {}), "model_invoked": True,
                        "planning_model_invoked": True,
                        "last_request": {"phase": "planning", "state": "waiting", "attempt": attempt,
                            "started_at": svc.primitive(now()), "limits": limits},
                        "model_request_count": int((current.model_snapshot or {}).get("model_request_count", 0)) + 1}
                    self._audit(db, current_job, "answer.planning_model_invocation_started", {
                        "local_sources_loaded": 0, "limits": limits,
                        "input_roles": [item["role"] for item in payload["messages"]]})
                try:
                    model_started = time.monotonic()
                    if connection:
                        connection = {**connection, "read_idle_timeout": limits["read_idle_seconds"]}
                        raw = providers.complete(connection, payload["messages"], max_tokens=limits["max_output_tokens"],
                            json_mode=True, timeout=limits["total_seconds"], output_schema=PLANNING_SCHEMA)
                    else:
                        from .ai_transport import post_json
                        raw = post_json(generation_settings.llm_base_url, "chat/completions", {
                            "model": generation_settings.llm_model, "messages": payload["messages"],
                            "max_tokens": limits["max_output_tokens"], "stream": False, "response_format": {"type": "json_object"}},
                            generation_settings.llm_api_key, limits["total_seconds"],
                            read_idle_timeout=limits["read_idle_seconds"])
                    self._record_answer_model_response(job_id, attempt, "planning", raw,
                        time.monotonic() - model_started)
                    return raw
                except (providers.ProviderError, LegacyProviderError) as exc:
                    self._record_answer_model_failure(job_id, attempt, "planning", exc.code, time.monotonic() - model_started,
                        getattr(exc, "diagnostic", None))
                    planning_transport.update(getattr(exc, "diagnostic", None) or {})
                    if isinstance(exc.code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", exc.code):
                        planning_transport["code"] = exc.code
                    raise PlanningError(exc.code) from None

            try:
                model_plan = plan_question(request["question"], "answer" if run.mode == "auto" else run.mode, context, planning_client)
            except PlanningError as exc:
                with self.session_factory.begin() as db:
                    current_job = self._fence(db, job_id, attempt)
                    _, current, _ = self._run_context(db, current_job, validate_attachments=False)
                    current.policy_snapshot = {**(current.policy_snapshot or {}), "generation_diagnostic": {
                        **(getattr(exc, "diagnostic", None) or {}),
                        **planning_transport, "code": planning_transport.get("code", exc.code), "phase": "planning"}}
                raise JobError(exc.code) from exc
            with self.session_factory.begin() as db:
                current_job = self._fence(db, job_id, attempt)
                _, current, _ = self._run_context(db, current_job, validate_attachments=False)
                current.policy_snapshot = {**(current.policy_snapshot or {}), "question_analysis": {
                    "source": "model_prior_knowledge_unverified", "local_sources_loaded": 0, "plan": model_plan}}
                current_job.stage = "RETRIEVING"
                self._audit(db, current_job, "answer.planning_completed", {"local_sources_loaded": 0,
                    "search_query_count": len(model_plan["search_queries"]), "plan_sha256": svc.digest(model_plan)})
                self._audit(db, current_job, "answer.retrieval_started", {"after_model_analysis": True})
        # Publish RUNNING/RETRIEVING before expensive source checks. Retrieval is
        # read-only and must not hold SQLite writer intent or user row locks.
        with self.read_session_factory() as db:
            current_job = db.get(m.Job, job_id)
            if not current_job or current_job.cancel_requested or current_job.state != "RUNNING" or current_job.attempts != attempt:
                raise JobError("MODEL_JOB_CANCELLED_OR_STALE")
            user, run, thread = self._run_context(db, current_job, read_only=True)
            retrieval_diagnostic = None
            if reference and model_plan is not None:
                from .planned_retrieval import planned_reference_evidence
                candidates, retrieval_diagnostic = planned_reference_evidence(db, user, thread.space_id, context,
                    request["question"], model_plan)
            else:
                candidates = (reference_evidence if reference else svc.eligible_evidence)(db, user, thread.space_id, context)
            all_candidates = candidates
            clarification = [] if reference else svc.clarification_candidates(db, user, thread.space_id, context)
            if self.settings.retrieval_mode == "wiki" and not reference:
                candidates = [e for e in candidates if e.get("kind") == "knowledge"]
                clarification = [e for e in clarification if e.get("kind") == "knowledge"]
        self._checkpoint(job_id, attempt, "SELECTING_SOURCES", {"candidate_blocks": len(candidates),
            **({"planned_retrieval": retrieval_diagnostic} if retrieval_diagnostic else {})})
        # Unknown applicability is used only to ask deterministic questions. The
        # helper still enforces ACL, legal status, approved hashes and CLEAN scans.
        relevant_unknown = rank_evidence(request["question"], clarification, None, limit=12)
        missing = sorted({field for e in relevant_unknown for field in e.get("missing_context_fields", [])})
        source_analysis = None
        if reference and not missing:
            evidence, source_analysis = retrieve_answer_context(request["question"], candidates, context=context)
            if model_plan is not None:
                source_analysis["question_analysis"] = {"unverified": True, "plan": model_plan}
        else:
            evidence = relevant_unknown if missing else rank_evidence(request["question"], candidates, policy_index, limit=12)
        if self.settings.retrieval_mode == "wiki" and not reference and evidence and not missing:
            selected_pairs = {(e["version_id"], e["block_id"]) for e in evidence}
            source_pairs = set()
            with self.session_factory() as db:
                for link in db.scalars(select(m.EvidenceLink).where(
                        m.EvidenceLink.from_version_id.in_({pair[0] for pair in selected_pairs}))):
                    if (link.from_version_id, link.from_block_id) in selected_pairs:
                        source_pairs.add((link.to_version_id, link.to_block_id))
            originals = [e for e in all_candidates if e.get("kind") == "document"
                and (e["version_id"], e["block_id"]) in source_pairs]
            evidence = evidence[:8] + originals[:max(0, 12 - min(8, len(evidence)))]
        def get_eligible(db, user, space_id, context):
            if reference:
                return reference_evidence(db, user, space_id, context, version_ids={e["version_id"] for e in evidence})
            return (svc.clarification_candidates if missing else svc.eligible_evidence)(db, user, space_id, context)
        self._checkpoint(job_id, attempt, "CLARIFYING" if missing else "PREPARING_ANSWER", {"evidence_count": len(evidence)})
        # Recheck after vector/network retrieval and before sending any text to the provider.
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            user, run, thread = self._run_context(db, job)
            allowed = {evidence_signature(e) for e in get_eligible(db, user, thread.space_id, context)}
            if any(evidence_signature(e) not in allowed for e in evidence):
                raise JobError("EVIDENCE_ACCESS_CHANGED")
            snapshots = {(e["version_id"], e["block_id"]): e for e in (run.evidence_snapshot or [])}
            for e in evidence:
                snapshots[(e["version_id"], e["block_id"])] = {
                    k: e[k] for k in ("resource_id", "version_id", "block_id", "content_sha256", "reference_signature") if k in e}
                if not db.get(m.RunEvidence, (run.id, e["version_id"], e["block_id"])):
                    source = db.get(m.Resource, e["resource_id"])
                    db.add(m.RunEvidence(run_id=run.id, version_id=e["version_id"], block_id=e["block_id"],
                                         resource_access_epoch=source.access_epoch))
            run.evidence_snapshot = list(snapshots.values())
            self._audit(db, job, "answer.evidence_frozen", {"evidence_count": len(evidence),
                                                          "snapshot_sha256": sha(json_bytes(run.evidence_snapshot))})
        invocation = {"attempted": False}
        generation_diagnostics = {"phase": "synthesis"}
        answer_timeout = getattr(self.settings, "answer_model_timeout_seconds", self.settings.model_timeout_seconds)
        if missing:
            answer = {"status": "NEEDS_CLARIFICATION", "mode": "solution" if run.mode == "solution" else "answer",
                "summary": "相关已发布资料的适用条件或必需事实尚不完整，请补充后再判断。",
                "scope": {k: str(v) if v is not None else None for k, v in context.items()},
                "facts": [{"name": k, "value": v, "origin": "USER", "certainty": "PROVIDED"}
                          for k, v in context.items()],
                "missing_facts": [{"field": field, "question": f"请补充{field}。",
                    "why_needed": "该字段来自相关已核验资料的适用条件或必需事实。"} for field in missing],
                "claims": [], "citations": [], "solution": None, "required_sources": [],
                "limitations": ["确定性事实澄清，未调用生成模型；尚未形成专业处理结论。"]}
        else:
            completion_client = None
            def before_model(record_invocation=True):
                # Prompt construction can take time. Recheck at the actual adapter
                # boundary, not only before constructing the generation payload.
                with self.session_factory.begin() as db:
                    current_job = self._fence(db, job_id, attempt)
                    current_user, current_run, current_thread = self._run_context(db, current_job)
                    allowed_now = {evidence_signature(e) for e in get_eligible(db, current_user, current_thread.space_id, context)}
                    if any(evidence_signature(e) not in allowed_now for e in evidence):
                        raise JobError("EVIDENCE_ACCESS_CHANGED")
                    current_connection = None
                    if choice := request.get("model_selection"):
                        from .providers import resolve_connection
                        current_connection = resolve_connection(db, current_user, current_thread.space_id,
                            choice["connection_id"], choice["model_id"], self.settings, require_transfer=True,
                            expected_revision=(current_run.model_snapshot or {}).get("revision"))
                    if record_invocation:
                        limits = phase_limits(self.settings, current_connection, "synthesis")
                        current_job.stage = "GENERATING"
                        current_run.model_snapshot = {**(current_run.model_snapshot or {}), "model_invoked": True,
                            "answer_model_invoked": True,
                            "last_request": {"phase": "synthesis", "state": "waiting", "attempt": attempt,
                                "started_at": svc.primitive(now()), "limits": limits},
                            "model_request_count": int((current_run.model_snapshot or {}).get("model_request_count", 0)) + 1}
                        self._audit(db, current_job, "answer.model_invocation_started", {"answer_scope": answer_scope,
                            "evidence_count": len(evidence), "limits": limits})
                if record_invocation:
                    invocation["attempted"] = True
                return current_connection
            if selection := request.get("model_selection"):
                from . import providers
                from .ai_transport import ProviderError as AnswerProviderError
                def completion_client(payload):
                    current_connection = before_model()
                    if current_connection.get("protocol") == "codex_app_server":
                        current_connection["_before_send_check"] = lambda: before_model(record_invocation=False)
                    limits = phase_limits(self.settings, current_connection, "synthesis")
                    current_connection = {**current_connection, "read_idle_timeout": limits["read_idle_seconds"]}
                    try:
                        model_started = time.monotonic()
                        raw = providers.complete(current_connection, payload["messages"],
                            max_tokens=limits["max_output_tokens"], json_mode=True, timeout=limits["total_seconds"],
                            output_schema=json.loads(payload["messages"][1]["content"])["schema"])
                        self._record_answer_model_response(job_id, attempt, "synthesis", raw,
                            time.monotonic() - model_started)
                        return raw
                    except providers.ProviderError as exc:
                        self._record_answer_model_failure(job_id, attempt, "synthesis", exc.code, time.monotonic() - model_started,
                            getattr(exc, "diagnostic", None))
                        error = AnswerProviderError(exc.code)
                        error.diagnostic = exc.diagnostic
                        raise error from None
            elif generation_settings.llm_provider == "http" and generation_settings.llm_base_url:
                from .ai_transport import post_json
                def completion_client(payload):
                    before_model()
                    limits = phase_limits(self.settings, None, "synthesis")
                    model_started = time.monotonic()
                    from .ai_transport import ProviderError as AnswerProviderError
                    try:
                        raw = post_json(generation_settings.llm_base_url, "chat/completions",
                            {**payload, "max_tokens": limits["max_output_tokens"]},
                            generation_settings.llm_api_key, limits["total_seconds"],
                            read_idle_timeout=limits["read_idle_seconds"])
                        self._record_answer_model_response(job_id, attempt, "synthesis", raw, time.monotonic() - model_started)
                        return raw
                    except AnswerProviderError as exc:
                        self._record_answer_model_failure(job_id, attempt, "synthesis", exc.code, time.monotonic() - model_started)
                        raise
            try:
                answer = generate_answer(request["question"], run.mode, context, evidence, generation_settings, run.id,
                    **({"compact_output": True} if generation_settings.llm_provider == "http" else {}),
                    diagnostics=generation_diagnostics,
                    **({"answer_scope": "reference",
                        "source_analysis": source_analysis} if reference else {}),
                    **({"completion_client": completion_client} if completion_client else {}))
            except AnswerValidationError as exc:
                with self.session_factory.begin() as db:
                    current_job = self._fence(db, job_id, attempt)
                    current_user, current_run, current_thread = self._run_context(db, current_job)
                    current_signatures = {evidence_signature(e) for e in get_eligible(db, current_user, current_thread.space_id, context)}
                    if any(evidence_signature(e) not in current_signatures for e in evidence):
                        raise JobError("EVIDENCE_ACCESS_CHANGED") from exc
                    current_run.policy_snapshot = {**current_run.policy_snapshot, "generation_diagnostic": generation_diagnostics}
                    current_run.model_snapshot = {**(current_run.model_snapshot or {}),
                        "execution_mode": "generation_rejected", "evidence_count": 0, "validation_status": "failed",
                        "retrieved_evidence_count": len(evidence)}
                raise JobError(exc.code) from exc
        # The transport owns one explicit per-request wall-clock deadline.
        # Do not discard a completed response using a second timer that also
        # counts prompt construction and permission revalidation.
        self._checkpoint(job_id, attempt, "VALIDATING_ANSWER", {})
        answer["run_id"], answer["generated_at"] = run.id, svc.primitive(now())
        answer["review_status"] = "REQUIRES_EXPERT"
        self._validate_answer(answer, evidence)
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            user, run, thread = self._run_context(db, job)
            # Lock the member-policy parent and all source resources before the final ACL read.
            if selection := request.get("model_selection"):
                from .providers import resolve_connection
                resolve_connection(db, user, thread.space_id, selection["connection_id"], selection["model_id"],
                    self.settings, require_transfer=True, expected_revision=(run.model_snapshot or {}).get("revision"))
            db.scalar(select(m.Space).where(m.Space.id == thread.space_id).with_for_update())
            for version_id in sorted({e["version_id"] for e in evidence}):
                self._version(db, user, version_id)
            fresh = {(e["version_id"], e["block_id"]): e
                     for e in get_eligible(db, user, thread.space_id, context)}
            for e in evidence:
                current = fresh.get((e["version_id"], e["block_id"]))
                if not current or evidence_signature(current) != evidence_signature(e):
                    raise JobError("EVIDENCE_ACCESS_CHANGED")
                if not db.get(m.RunEvidence, (run.id, e["version_id"], e["block_id"])):
                    db.add(m.RunEvidence(run_id=run.id, version_id=e["version_id"], block_id=e["block_id"],
                                         resource_access_epoch=current["resource_access_epoch"]))
            run.response, run.state, run.completed_at = answer, "COMPLETED", now()
            run.policy_snapshot = {**run.policy_snapshot, "schema": "answer-v2",
                "validation": "SCHEMA_AND_CITATION_CHECKED", "professional_accuracy": "NOT_EVALUATED",
                **({"generation_diagnostic": generation_diagnostics} if generation_diagnostics else {})}
            limits = " ".join(answer.get("limitations", []))
            fallback = any(marker in limits for marker in ("未调用生成模型", "MODEL_NOT_CONFIGURED", "已降级为证据提取"))
            run.model_snapshot = {**(run.model_snapshot or {}), "configured_provider": generation_settings.llm_provider,
                "configured_model": generation_settings.llm_model,
                "answer_timeout_seconds": answer_timeout,
                "model_invoked": bool(model_plan is not None or invocation["attempted"]),
                "answer_model_invoked": invocation["attempted"], "evidence_count": len(answer.get("citations", [])),
                "execution_mode": "planning_only" if model_plan is not None and not invocation["attempted"] else
                    "deterministic_clarification" if missing else "extractive"
                    if generation_settings.llm_provider == "evidence" or fallback or not invocation["attempted"] else
                    "reference_grounded" if reference else "http_grounded",
                "validation_status": "passed", "semantic_effectiveness": "NOT_EVALUATED"}
            self._succeed(db, job, {"run_id": run.id})

    def _export(self, job_id, attempt):
        from .ingestion import render_blocks
        with self.session_factory() as db:
            job = db.get(m.Job, job_id)
            user = self._user(db, job)
            ids, format = job.payload["version_ids"], job.payload["format"]
            if format not in {"markdown", "json"} or not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
                raise JobError("EXPORT_INVALID")
            snapshot = []
            for version_id in ids:
                version, _ = self._version(db, user, version_id, "download")
                snapshot.append((svc.version_dict(db, version), svc.check_frozen_hash(db, version)))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for version, _ in snapshot:
                data = json_bytes(version) if format == "json" else render_blocks(version["blocks"], "markdown").encode()
                name = f"{version['id']}.{('json' if format == 'json' else 'md')}"
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, data)
        data = buffer.getvalue()
        key = f"exports/{job_id}.zip"
        self.storage.write_bytes(key, data)
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            user = self._user(db, job)
            for version, frozen in snapshot:
                current, _ = self._version(db, user, version["id"], "download")
                if frozen != svc.check_frozen_hash(db, current):
                    raise JobError("EXPORT_SOURCE_CHANGED")
            self._succeed(db, job, {"format": format, "filename": f"fund-kb-{job_id}.zip",
                "expires_at": svc.primitive(now() + timedelta(hours=24)), "artifact_sha256": sha(data)})

    def _invalidation_scope(self, db, resource_id):
        root_ids = set(db.scalars(select(m.ResourceVersion.id).where(m.ResourceVersion.resource_id == resource_id)))
        affected = set(root_ids)
        resource_ids = {resource_id}
        for _ in range(9):
            found = set(db.scalars(select(m.EvidenceLink.from_version_id)
                                  .where(m.EvidenceLink.to_version_id.in_(affected)))) if affected else set()
            found |= set(db.scalars(select(m.RelationEdge.source_version_id).where(or_(
                m.RelationEdge.target_resource_id.in_(resource_ids), m.RelationEdge.evidence_version_id.in_(affected)))))
            for block in db.scalars(select(m.ContentBlock).where(m.ContentBlock.block_type.in_(["image", "attachment"]))):
                if block.data.get("version_id") in affected:
                    found.add(block.version_id)
            if found <= affected:
                return root_ids, affected, resource_ids
            affected |= found
            resource_ids |= set(db.scalars(select(m.ResourceVersion.resource_id).where(m.ResourceVersion.id.in_(affected))))
        raise JobError("DEPENDENCY_DEPTH_EXCEEDED")

    def _invalidate(self, job_id, attempt):
        from .ingestion import block_text, render_blocks, text_sha256
        rebuild_records, rebuild_previews = [], {}
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            user = self._user(db, job)
            resource = db.get(m.Resource, job.payload["resource_id"])
            if not resource:
                raise JobError("NOT_FOUND")
            reason = job.payload.get("reason", "DELETED")
            action = "publish" if reason in {"SUSPENSION_CHANGED", "SOURCE_UPDATED"} else "manage"
            if resource.owner_id == user.id and not resource.active_release_id and reason != "PERMISSIONS_CHANGED":
                action = "edit"
            svc.require_resource(db, user, resource.id, action, include_deleted=True)
            root_ids, affected, resource_ids = self._invalidation_scope(db, resource.id)
            run_ids = set(db.scalars(select(m.RunEvidence.run_id).where(m.RunEvidence.version_id.in_(affected))))
            if run_ids:
                db.execute(update(m.ConsultationRun).where(m.ConsultationRun.id.in_(run_ids)).values(invalidated_at=now()))
            if resource.deleted_at or resource.suspended or reason == "SOURCE_UPDATED":
                downstream = resource_ids - {resource.id}
                if downstream:
                    db.execute(update(m.Resource).where(m.Resource.id.in_(downstream)).values(suspended=True,
                        revision=m.Resource.revision + 1, access_epoch=m.Resource.access_epoch + 1))
            if not resource.deleted_at and reason in {"RESTORED", "SUSPENSION_CHANGED", "PERMISSIONS_CHANGED"}:
                for version_id in root_ids:
                    version = db.get(m.ResourceVersion, version_id)
                    blocks = svc.version_blocks(db, version_id)
                    if blocks:
                        rebuild_previews[version_id] = render_blocks(blocks, "html").encode()
                    release = svc.released(db, version_id)
                    if release and not resource.suspended:
                        for block in blocks:
                            text = block_text(block)
                            rebuild_records.append({"resource_id": resource.id, "version_id": version_id,
                                "block_id": block["block_id"], "release_id": release.id, "title": version.title,
                                "text": text, "locator": block.get("locator", {}), "content_sha256": text_sha256(text)})
            self._audit(db, job, "resource.projections_invalidating", {"affected_version_count": len(affected)})
        # Source updates preserve the new version's index. Authority filters make the gap fail closed.
        removed = (affected - root_ids if reason == "SOURCE_UPDATED" else affected
                   if resource.deleted_at or resource.suspended else set())
        for vector in self._projection_vectors():
            vector.delete_versions(sorted(removed))
        if rebuild_records and self.retrieval_registry is None and self.vector_index is not None and self.settings.embedding_mode == "hashing":
            self.vector_index.upsert(rebuild_records)
        for version_id, preview in rebuild_previews.items():
            self.storage.write_bytes(f"previews/{version_id}.html", preview)
        if resource.deleted_at:
            for version_id in root_ids:
                self.storage.delete(f"previews/{version_id}.html")
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            self._succeed(db, job, {"affected_resource_count": len(resource_ids), "affected_run_count": len(run_ids)})

    def _purge_checks(self, db, job):
        user = self._user(db, job)
        from .retention import assert_purge_allowed
        assert_purge_allowed(db, user, job.payload["resource_id"], phase="execution", job_id=job.id)
        resource = svc.require_resource(db, user, job.payload["resource_id"], "manage", include_deleted=True)
        db.scalar(select(m.Resource).where(m.Resource.id == resource.id).with_for_update())
        if not resource.deleted_at or not str(job.payload.get("reason", "")).strip():
            raise JobError("PURGE_REQUIRES_TRASH_AND_REASON")
        versions = list(db.scalars(select(m.ResourceVersion).where(m.ResourceVersion.resource_id == resource.id)))
        ids = [v.id for v in versions]
        if ids:
            if db.scalar(select(m.EvidenceLink.id).where(m.EvidenceLink.to_version_id.in_(ids),
                     m.EvidenceLink.from_version_id.not_in(ids)).limit(1)):
                raise JobError("INBOUND_DEPENDENCIES")
            if db.scalar(select(m.RunEvidence.run_id).where(m.RunEvidence.version_id.in_(ids)).limit(1)):
                raise JobError("INBOUND_DEPENDENCIES")
            if db.scalar(select(m.RelationEdge.id).where(or_(m.RelationEdge.target_resource_id == resource.id,
                m.RelationEdge.evidence_version_id.in_(ids)), m.RelationEdge.source_version_id.not_in(ids)).limit(1)):
                raise JobError("INBOUND_DEPENDENCIES")
            for block in db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id.not_in(ids),
                         m.ContentBlock.block_type.in_(["image", "attachment"]))):
                if block.data.get("version_id") in ids:
                    raise JobError("INBOUND_DEPENDENCIES")
            if db.scalar(select(m.Upload.id).where(m.Upload.version_id.in_(ids), m.Upload.state == "OPEN").limit(1)):
                raise JobError("UPLOAD_IN_PROGRESS")
        for pending in db.scalars(select(m.Job).where(m.Job.id != job.id, m.Job.state.in_(["QUEUED", "RUNNING"]))):
            payload = pending.payload or {}
            if (pending.resource_id == resource.id or pending.version_id in ids
                    or payload.get("resource_id") == resource.id or payload.get("version_id") in ids
                    or set(payload.get("version_ids", [])) & set(ids)):
                raise JobError("DEPENDENT_JOB_RUNNING")
        return resource, versions

    def _purge(self, job_id, attempt):
        # Exact-object deletion is performed under the resource and job transaction locks.
        # A crash can leave a partly deleted primary store: retries are idempotent,
        # and the tombstone/failed job remains. No claim about backups is made.
        with self.session_factory.begin() as db:
            job = self._fence(db, job_id, attempt)
            resource, versions = self._purge_checks(db, job)
            ids = [v.id for v in versions]
            uploads = list(db.scalars(select(m.Upload).where(m.Upload.version_id.in_(ids))))
            keys = {f"previews/{v.id}.html" for v in versions}
            blobs = []
            for blob_id in {v.source_blob_id for v in versions if v.source_blob_id}:
                if not db.scalar(select(m.ResourceVersion.id).where(m.ResourceVersion.source_blob_id == blob_id,
                        m.ResourceVersion.id.not_in(ids)).limit(1)):
                    blob = db.get(m.Blob, blob_id)
                    keys.add(blob.object_key)
                    blobs.append(blob)
            for upload in uploads:
                for part in db.scalars(select(m.UploadPart).where(m.UploadPart.upload_id == upload.id)):
                    keys.add(part.object_key)
                for scan in db.scalars(select(m.Job).where(m.Job.kind == "SCAN_PARSE")):
                    if scan.payload.get("upload_id") == upload.id:
                        keys.add(f"quarantine/{scan.id}/{safe_filename(upload.filename)}")
            for release in db.scalars(select(m.Release).where(m.Release.resource_id == resource.id)):
                keys.update({f"releases/{release.id}/content.html", f"releases/{release.id}/content.md",
                             f"releases/{release.id}/index.json", f"releases/{release.id}/manifest.json"})
            for export in db.scalars(select(m.Job).where(m.Job.kind == "EXPORT")):
                if set(export.payload.get("version_ids", [])) & set(ids):
                    keys.add(f"exports/{export.id}.zip")
            for vector in self._projection_vectors():
                vector.delete_versions(ids)
            for key in sorted(keys):
                self.storage.delete(key)
            db.execute(delete(m.EvidenceLink).where(m.EvidenceLink.from_version_id.in_(ids)))
            db.execute(delete(m.RelationEdge).where(m.RelationEdge.source_version_id.in_(ids)))
            db.execute(delete(m.ContentBlock).where(m.ContentBlock.version_id.in_(ids)))
            for version in versions:
                version.source_blob_id, version.source_url = None, None
                version.title, version.change_reason = "[已清除]", ""
                version.applicability, version.required_facts = {}, []
            db.flush()
            for blob in blobs:
                db.delete(blob)
            for upload in uploads:
                db.execute(delete(m.UploadPart).where(m.UploadPart.upload_id == upload.id))
                upload.filename = "[已清除]"
            for review in db.scalars(select(m.ReviewDecision).where(m.ReviewDecision.version_id.in_(ids))):
                review.comment = ""
            resource.name, resource.category, resource.tags = "[已清除]", "已清除", []
            resource.suspended = True
            resource.revision += 1
            resource.access_epoch += 1
            receipt_id = stable_id(job_id, "purge-receipt")
            result = {"resource_id": resource.id, "purged_at": svc.primitive(now()),
                "purge_receipt_id": receipt_id, "backup_expiry_at": None,
                "scope": "primary_objects_and_content_only", "deleted_object_count": len(keys),
                "backups_verified": False}
            db.add(m.AuditEvent(id=receipt_id, actor_id=job.owner_id, action="resource.purged",
                object_type="Resource", object_id=resource.id, trace_id=f"job:{job.id}",
                outcome="SUCCESS", details=result))
            self._succeed(db, job, result)
