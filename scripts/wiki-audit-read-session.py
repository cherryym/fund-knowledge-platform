"""Authenticated, allowlisted SQLite snapshot reader for Wiki verification.

No login/model/profile work happens on import. Pass the existing authenticated
API() object; only GET /me and graph requests use it. Caller must withhold PASS
until close_and_revalidate() succeeds. An exception requires discarding the read.

resource_ids/version_ids must include the complete dependency and version
metadata closure needed by the ordinary handlers. Missing scope fails closed.
"""
from __future__ import annotations

import copy
import re
import sqlite3
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.orm import sessionmaker, with_loader_criteria
from sqlalchemy.pool import NullPool

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT / "backend") not in sys.path:
    sys.path.insert(0, str(PROJECT / "backend"))

from fund_kb import api_catalog, api_content, api_tasks
from fund_kb import models as m
from fund_kb import services as svc


class AuditReadError(RuntimeError):
    """Safe codes only; do not include SQL, credentials, response bodies or paths."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(UTC).isoformat()


def _uuid(value):
    if not isinstance(value, str):
        raise AuditReadError("AUDIT_INVALID_ID")
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise AuditReadError("AUDIT_INVALID_ID") from None
    if canonical != value:
        raise AuditReadError("AUDIT_INVALID_ID")
    return canonical


def _ids(values):
    if isinstance(values, (str, bytes)):
        raise AuditReadError("AUDIT_INVALID_ALLOWLIST")
    result = frozenset(_uuid(value) for value in values)
    if len(result) > 20000:
        raise AuditReadError("AUDIT_SCOPE_TOO_LARGE")
    return result


READ_TABLES = frozenset({
    "users", "spaces", "space_members", "resources", "resource_versions", "resource_grants",
    "blobs", "content_blocks", "evidence_links", "relation_edges", "releases", "review_decisions",
    "runtime_policies", "jobs",
})


def _authorizer(action, arg1, arg2, database, source):
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if arg1 in READ_TABLES else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        return sqlite3.SQLITE_OK if arg2 is None and arg1 in {
            "read_uncommitted", "query_only", "data_version", "synchronous", "journal_mode",
        } else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_DENY if arg2 in {"load_extension", "readfile", "writefile"} else sqlite3.SQLITE_OK
    return sqlite3.SQLITE_OK if action in {
        sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
    } else sqlite3.SQLITE_DENY


def _row(row):
    return svc.primitive({column.key: getattr(row, column.key) for column in inspect(type(row)).columns})


class _View:
    def __init__(self, owner):
        self.owner = owner
        self.db = owner._factory()
        self.resources, self.versions, self.jobs = set(), set(), set()
        self.policy_reads = set()
        self.bodies = {}
        event.listen(self.db, "do_orm_execute", self._statement)
        event.listen(self.db, "loaded_as_persistent", self._loaded)
        try:
            self.user = self.db.get(m.User, owner.user_id)
            if not self.user or not self.user.active:
                raise AuditReadError("AUDIT_ACCESS_DENIED")
            svc.space_access(self.db, self.user, owner.space_id)
            self.snapshot_at = _now()
        except Exception:
            self.close()
            raise

    def _statement(self, state):
        if not state.is_select or not state.is_orm_statement:
            raise AuditReadError("AUDIT_SQL_NOT_ALLOWED")
        if any(item.get("entity") is m.RuntimePolicy for item in state.statement.column_descriptions):
            for value in state.statement.compile().params.values():
                values = value if isinstance(value, (list, tuple, set, frozenset)) else (value,)
                if any(isinstance(item, str) and item.startswith("model-") for item in values):
                    raise AuditReadError("AUDIT_SECRET_POLICY_FORBIDDEN")
        # Apply BEFORE fetching config: model-* rows never enter the Session.
        # Include space governance, otherwise a private library could incorrectly
        # be treated as legacy when its policy is hidden by the reader.
        names = tuple(self.owner._policy_names)
        state.statement = state.statement.options(with_loader_criteria(
            m.RuntimePolicy, m.RuntimePolicy.name.in_(names), include_aliases=True))

    def _loaded(self, db, row):
        if isinstance(row, m.Resource):
            if row.id not in self.owner.resource_ids or row.space_id != self.owner.space_id:
                raise AuditReadError("AUDIT_RESOURCE_OUT_OF_SCOPE")
            self.resources.add(row.id)
        elif isinstance(row, m.ResourceVersion):
            if row.id not in self.owner.version_ids or row.resource_id not in self.owner.resource_ids:
                raise AuditReadError("AUDIT_VERSION_OUT_OF_SCOPE")
            self.versions.add(row.id)
            resource = db.get(m.Resource, row.resource_id)
            if resource.kind == "knowledge" and row.origin in {"AI_DRAFT", "COPY"}:
                direct = db.scalar(select(m.RuntimePolicy.id).where(
                    m.RuntimePolicy.name == "wiki-provenance:" + row.resource_id))
                if direct is None:
                    receipts = db.scalars(select(m.RuntimePolicy.config).where(m.RuntimePolicy.name.in_(
                        ["wiki-build-receipt:" + jid for jid in self.owner.job_ids])))
                    if not any(row.resource_id in (config.get("result") or {}).get("created_resource_ids", [])
                               for config in receipts):
                        raise AuditReadError("AUDIT_PROVENANCE_SCOPE_REQUIRED")
        elif isinstance(row, m.Job):
            if (row.id not in self.owner.job_ids or row.kind != "COMPILE"
                    or row.payload.get("task") != "WIKI_BUILD"
                    or row.payload.get("space_id") != self.owner.space_id):
                raise AuditReadError("AUDIT_JOB_OUT_OF_SCOPE")
            self.jobs.add(row.id)
        elif isinstance(row, m.Blob) and row.space_id != self.owner.space_id:
            raise AuditReadError("AUDIT_BLOB_OUT_OF_SCOPE")
        elif isinstance(row, m.RuntimePolicy):
            if row.name not in self.owner._policy_names:
                raise AuditReadError("AUDIT_SECRET_POLICY_FORBIDDEN")
            if row.config.get("space_id", self.owner.space_id) != self.owner.space_id:
                raise AuditReadError("AUDIT_POLICY_OUT_OF_SCOPE")
            self.policy_reads.add(row.name)

    def _context(self, operation, oid):
        return svc.Context(SimpleNamespace(path_params={"id": oid}), self.db, self.user,
                           {}, {}, operation, [])

    def read(self, kind, oid):
        key = (kind, oid)
        if key in self.bodies:
            return self.bodies[key]["body"]
        if kind == "jobs":
            # Read only routing metadata before loading a full Job. Never enter
            # model/OAuth/answer/export handlers that can query secret policies.
            row = self.db.execute(select(m.Job.kind, func.json_extract(m.Job.payload, "$.task"),
                func.json_extract(m.Job.payload, "$.space_id")).where(m.Job.id == oid)).first()
            if row != ("COMPILE", "WIKI_BUILD", self.owner.space_id):
                raise AuditReadError("AUDIT_JOB_OUT_OF_SCOPE")
            handler, operation = api_tasks.jobs, "getJob"
        elif kind == "resources":
            handler, operation = api_catalog.get_resource, "getResource"
        else:
            handler, operation = api_content.get_version, "getVersion"
        result = handler(self._context(operation, oid))
        if result.status != 200 or self.db.new or self.db.dirty or self.db.deleted:
            raise AuditReadError("AUDIT_READ_MUTATION_OR_STATUS")
        self.bodies[key] = {"body": copy.deepcopy(result.body), "headers": dict(result.headers)}
        return self.bodies[key]["body"]

    def graph_scope(self, graph):
        if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
            raise AuditReadError("AUDIT_GRAPH_INVALID")
        nodes = set()
        for node in graph["nodes"]:
            rid, vid = node.get("id"), node.get("version_id")
            if rid not in self.owner.resource_ids or vid not in self.owner.version_ids:
                raise AuditReadError("AUDIT_GRAPH_OUT_OF_SCOPE")
            if self.read("versions", vid)["resource_id"] != rid:
                raise AuditReadError("AUDIT_GRAPH_INVALID")
            self.read("resources", rid)
            nodes.add(rid)
        if any(edge.get("source") not in nodes or edge.get("target") not in nodes for edge in graph["edges"]):
            raise AuditReadError("AUDIT_GRAPH_INVALID")

    def seal(self):
        # Include every object actually traversed by authorization, not only
        # the explicitly requested page. A source hash/epoch can change while
        # the derived page's public dictionary stays exactly the same.
        done = set()
        while True:
            pending = ({("resources", rid) for rid in self.resources}
                       | {("versions", vid) for vid in self.versions}
                       | {("jobs", jid) for jid in self.jobs}) - done
            if not pending:
                break
            for kind, oid in sorted(pending):
                self.read(kind, oid)
                done.add((kind, oid))
        metadata = {"resources": {}, "versions": {}, "jobs": {}, "blobs": {}, "blocks": {}, "policies": {}}
        for kind, model, ids in (("resources", m.Resource, self.resources),
                                 ("versions", m.ResourceVersion, self.versions), ("jobs", m.Job, self.jobs)):
            for oid in sorted(ids):
                row = self.db.get(model, oid)
                metadata[kind][oid] = _row(row)
                if model is m.ResourceVersion:
                    metadata[kind][oid]["recomputed_content_sha256"] = svc.check_frozen_hash(self.db, row)
                    if row.source_blob_id:
                        metadata["blobs"][row.source_blob_id] = _row(self.db.get(m.Blob, row.source_blob_id))
                    for block in self.db.scalars(select(m.ContentBlock).where(m.ContentBlock.version_id == oid)):
                        metadata["blocks"][f"{oid}:{block.block_id}"] = _row(block)
        # Compare both presence and absence of provenance/receipt metadata. The
        # names are exact and safe; this query cannot read credential policies.
        names = {"space-governance:" + self.owner.space_id} | self.policy_reads
        names.update("wiki-provenance:" + rid for rid in self.resources)
        # Compare absence too: confirmation can be added/revoked without changing
        # the original manuscript, dates, epoch or public version dictionary.
        names.update("admin-review:" + vid for vid in self.versions)
        names.update("wiki-build-receipt:" + jid for jid in self.jobs)
        names.update(f"wiki-semantic:{self.owner.space_id}:{jid}" for jid in self.jobs)
        for policy in self.db.scalars(select(m.RuntimePolicy).where(m.RuntimePolicy.name.in_(names))):
            metadata["policies"][policy.name] = _row(policy)
        # Missing legacy lineage must not become an authorization bypass when
        # only allowlisted receipts can be read. Require a supplied receipt or
        # explicit provenance for generated/copy knowledge.
        for data in metadata["versions"].values():
            rid = data["resource_id"]
            resource = metadata["resources"][rid]
            if resource["kind"] == "knowledge" and data["origin"] in {"AI_DRAFT", "COPY"}:
                direct = "wiki-provenance:" + rid in metadata["policies"]
                legacy = any(rid in (p["config"].get("result") or {}).get("created_resource_ids", [])
                             for name, p in metadata["policies"].items() if name.startswith("wiki-build-receipt:"))
                if not direct and not legacy:
                    raise AuditReadError("AUDIT_PROVENANCE_SCOPE_REQUIRED")
        if self.db.new or self.db.dirty or self.db.deleted:
            raise AuditReadError("AUDIT_READ_MUTATION_OR_STATUS")
        return {"responses": copy.deepcopy(self.bodies), "metadata": metadata}

    def close(self):
        self.db.rollback()
        self.db.close()


class WikiAuditReadSession:
    def __init__(self, api, *, database_path, space_id, resource_ids, version_ids, job_ids=(),
                 expected_user_id=None, timeout_seconds=5):
        self.api = api
        self.space_id = _uuid(space_id)
        self.resource_ids, self.version_ids, self.job_ids = map(_ids, (resource_ids, version_ids, job_ids))
        self._thread = threading.get_ident()
        self._closed, self._failed = False, False
        self._view = self._engine = None
        self._reads = {}
        self.status = "OPEN_UNVERIFIED"
        self.requested_at_utc = _now()
        identity = self._authenticate()
        self.user_id = identity["id"]
        if expected_user_id is not None and self.user_id != _uuid(expected_user_id):
            raise AuditReadError("AUDIT_IDENTITY_MISMATCH")
        self._identity = identity
        self.authenticated_at_utc = _now()
        self._path = Path(database_path)
        if (not self._path.is_absolute() or not self._path.is_file()
                or any(part.is_symlink() for part in (self._path, *self._path.parents))
                or self._path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}):
            raise AuditReadError("AUDIT_DATABASE_PATH_INVALID")
        self._file_identity = (self._path.stat().st_dev, self._path.stat().st_ino)
        if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 30:
            raise AuditReadError("AUDIT_INVALID_TIMEOUT")
        self._timeout = timeout_seconds
        self._policy_names = frozenset({"space-governance:" + self.space_id}
            | {"wiki-provenance:" + rid for rid in self.resource_ids}
            # admin_review.approve writes version/hash/epoch/provenance-bound
            # attestation metadata only; no credential/provider configuration.
            | {"admin-review:" + vid for vid in self.version_ids}
            | {"wiki-build-receipt:" + jid for jid in self.job_ids}
            | {f"wiki-semantic:{self.space_id}:{jid}" for jid in self.job_ids})
        try:
            self._engine = create_engine("sqlite://", creator=self._connection, poolclass=NullPool,
                                         hide_parameters=True)

            @event.listens_for(self._engine, "begin")
            def begin(connection):
                connection.exec_driver_sql("BEGIN DEFERRED")

            self._factory = sessionmaker(bind=self._engine, autoflush=False, expire_on_commit=False)
            self._view = _View(self)
            self.snapshot_started_at_utc = self._view.snapshot_at
        except Exception:  # noqa: BLE001 - never surface driver configuration or SQL details
            self.close()
            raise AuditReadError("AUDIT_OPEN_FAILED") from None

    def _connection(self):
        info = self._path.stat()
        if (any(part.is_symlink() for part in (self._path, *self._path.parents))
                or (info.st_dev, info.st_ino) != self._file_identity):
            raise AuditReadError("AUDIT_DATABASE_REPLACED")
        connection = sqlite3.connect(self._path.as_uri() + "?mode=ro", uri=True,
                                     isolation_level=None, timeout=self._timeout)
        connection.execute("PRAGMA query_only=ON")
        connection.set_authorizer(_authorizer)
        return connection

    def _authenticate(self):
        try:
            me = self.api.request("GET", "/me")
            uid = _uuid(me["id"])
            space = next(item for item in me["spaces"] if item["id"] == self.space_id)
            if not space.get("roles"):
                raise ValueError()
            return {"id": uid, "space": {key: copy.deepcopy(space.get(key))
                for key in ("id", "roles", "revision", "kind", "owner_id", "governed")}}
        except Exception:  # noqa: BLE001 - API implementations differ; never expose auth responses
            raise AuditReadError("AUDIT_API_AUTH_FAILED") from None

    def _check_open(self):
        if threading.get_ident() != self._thread:
            raise AuditReadError("AUDIT_THREAD_MISMATCH")
        if self._closed:
            raise AuditReadError("AUDIT_CLOSED")
        if self._failed:
            raise AuditReadError("AUDIT_SESSION_FAILED")

    def _route(self, path):
        if not isinstance(path, str) or re.search(r"[\s\\%]", path):
            raise AuditReadError("AUDIT_PATH_NOT_ALLOWED")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise AuditReadError("AUDIT_PATH_NOT_ALLOWED")
        match = re.fullmatch(r"/(resources|versions|jobs)/([0-9a-f-]{36})", parsed.path)
        if match and not parsed.query:
            kind, oid = match.groups()
            oid = _uuid(oid)
            if oid not in getattr(self, {"resources": "resource_ids", "versions": "version_ids", "jobs": "job_ids"}[kind]):
                raise AuditReadError("AUDIT_ID_NOT_ALLOWED")
            return kind, oid, f"/{kind}/{oid}"
        if parsed.path not in {"/wiki/graph", "/graph"}:
            raise AuditReadError("AUDIT_PATH_NOT_ALLOWED")
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        if len(query) != len(pairs):
            raise AuditReadError("AUDIT_PATH_NOT_ALLOWED")
        if parsed.path == "/graph" and "space" in query:
            if "space_id" in query:
                raise AuditReadError("AUDIT_PATH_NOT_ALLOWED")
            query["space_id"] = query.pop("space")
        if set(query) - {"space_id", "focus_id", "depth", "limit"} or query.get("space_id") != self.space_id:
            raise AuditReadError("AUDIT_GRAPH_OUT_OF_SCOPE")
        if "focus_id" in query and _uuid(query["focus_id"]) not in self.resource_ids:
            raise AuditReadError("AUDIT_ID_NOT_ALLOWED")
        for name, low, high in (("depth", 0, 3), ("limit", 1, 200)):
            if name in query and (not query[name].isdigit() or not low <= int(query[name]) <= high):
                raise AuditReadError("AUDIT_PATH_NOT_ALLOWED")
        return "graph", None, "/wiki/graph?" + urlencode(sorted(query.items()))

    def get(self, path):
        self._check_open()
        try:
            kind, oid, canonical = self._route(path)
            if canonical not in self._reads:
                if kind == "graph":
                    body = self.api.request("GET", canonical)
                    self._view.graph_scope(body)
                else:
                    body = self._view.read(kind, oid)
                self._reads[canonical] = copy.deepcopy(body)
            return copy.deepcopy(self._reads[canonical])
        except Exception as exc:
            self._failed = True
            self.status = "FAILED"
            self.close()
            if isinstance(exc, AuditReadError):
                raise
            raise AuditReadError("AUDIT_READ_FAILED") from None

    def close_and_revalidate(self):
        self._check_open()
        fresh = watcher = None
        try:
            if not self._reads:
                raise AuditReadError("AUDIT_EMPTY_READ_SET")
            original = self._view.seal()
            self._view.close()
            self._view = None
            self.snapshot_ended_at_utc = _now()
            self.revalidation_started_at_utc = _now()
            watcher = self._connection()
            data_version = watcher.execute("PRAGMA data_version").fetchone()[0]
            if self._authenticate() != self._identity:
                raise AuditReadError("AUDIT_IDENTITY_CHANGED")
            graphs = {path: self.api.request("GET", path) for path in self._reads if path.startswith("/wiki/graph?")}
            fresh = _View(self)
            fresh_snapshot_at = fresh.snapshot_at
            for path, expected in self._reads.items():
                kind, oid, _ = self._route(path)
                if kind == "graph":
                    actual = graphs[path]
                    fresh.graph_scope(actual)
                else:
                    actual = fresh.read(kind, oid)
                if actual != expected:
                    raise AuditReadError("AUDIT_REVALIDATION_CHANGED")
            if fresh.seal() != original:
                raise AuditReadError("AUDIT_REVALIDATION_CHANGED")
            fresh.close()
            fresh = None
            if self._authenticate() != self._identity:
                raise AuditReadError("AUDIT_IDENTITY_CHANGED")
            if watcher.execute("PRAGMA data_version").fetchone()[0] != data_version:
                raise AuditReadError("AUDIT_REVALIDATION_RACED")
            self.status = "PASS"
            return {"status": "PASS", "user_id": self.user_id, "space_id": self.space_id,
                "authenticated_at_utc": self.authenticated_at_utc,
                "snapshot_started_at_utc": self.snapshot_started_at_utc,
                "snapshot_ended_at_utc": self.snapshot_ended_at_utc,
                "revalidation_started_at_utc": self.revalidation_started_at_utc,
                "revalidation_snapshot_at_utc": fresh_snapshot_at, "revalidation_finished_at_utc": _now(),
                "read_paths": sorted(self._reads),
                "resources_checked": len(original["metadata"]["resources"]),
                "versions_checked": len(original["metadata"]["versions"]),
                "jobs_checked": len(original["metadata"]["jobs"]),
                "professional_accuracy": "NOT_EVALUATED", "formal_evidence_allowed": False}
        except Exception as exc:
            self._failed = True
            self.status = "FAILED"
            if isinstance(exc, AuditReadError):
                raise
            raise AuditReadError("AUDIT_REVALIDATION_FAILED") from None
        finally:
            if fresh is not None:
                fresh.close()
            if watcher is not None:
                watcher.close()
            self.close()

    def close(self):
        if self._view is not None:
            self._view.close()
            self._view = None
        if self._engine is not None:
            self._engine.dispose()
        self._closed = True
        if self.status == "OPEN_UNVERIFIED":
            self.status = "CLOSED_UNVERIFIED"

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *exc):
        self.close()
