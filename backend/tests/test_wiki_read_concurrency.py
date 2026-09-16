"""File-SQLite reader/writer regressions; real HTTP, SQL and authorization checks.

An independent connection holds an uncommitted BEGIN IMMEDIATE throughout each
read. Only identity/model boundaries are synthetic; no WAL change, database
adapter replacement, real auth file, network, model or platform service is used.
"""
from __future__ import annotations

import copy
import os
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from sqlalchemy import delete, event, insert, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_wiki import env as env  # noqa: PLC0414
from test_wiki import page

from fund_kb import api, api_wiki_reader, codex_bridge, codex_text, providers, wiki
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.db import Base, ReadOnlySession
from fund_kb.ingestion import block_text, text_sha256

# Keep this inventory independent of the implementation: adding a GET or changing
# an operation's capability requires an explicit review here, not a subset check.
EXPECTED_READ_ONLY_OPERATIONS = frozenset({
    "downloadExport", "getAdminReview", "getCase", "getDocumentTaxonomy", "getGuidanceNormalization",
    "getHealth", "getJob", "getLibrary", "getMe", "getModelConnection", "getModelOAuthChallenge",
    "getModelOAuthState", "getModelPolicy", "getPermissions", "getPurgeEligibility", "getRelations",
    "getResource", "getResourcePreservation", "getRetentionPolicy", "getRetrievalProfiles", "getRetrievalStatus", "getReviewQueue", "getRun",
    "getRunProgress", "getThread", "getUpload", "getVersion", "getWikiCompilationSpecs",
    "getWikiEntryMaintenance", "getWikiGraph", "getWikiMaintenanceProposal", "getWikiPageLinks",
    "getWikiReaderCatalog", "getWikiTaxonomy", "getWikiWorkspace", "listCases", "listDocuments", "listJobs",
    "listLibraries", "listLibraryMembers", "listLibraryUsers", "listMembers", "listModelConnections",
    "listModelCredentialReferences", "listModelOptions", "listModelProviders", "listResources", "listSpaces",
    "listThreads", "listVersionReviews", "listVersions", "listWikiMaintenanceProposals", "queryAudit",
    "readVersionContent", "resolveWikiMaintenanceAlias", "resolveWikiTitle", "searchKnowledge", "searchHybridKnowledge",
    "listSourceAuthority", "getSourceAuthority", "listSourceAuthoritySuggestions",
    "listCapabilities", "getCapabilityStarter", "getCapability", "getCapabilityVersion",
    "exportCapabilitySkill", "listCapabilityRuns", "getCapabilityRun", "getCapabilityRunNext",
    "getCapabilityRunSources", "listAgentAccess",
})

# These real-HTTP fixtures exercise the shared read dispatcher under a writer
# reservation. The complete API capability inventory is checked separately.
READ_OPERATIONS = (
    "getWikiGraph", "getWikiWorkspace", "getWikiPageLinks", "resolveWikiTitle",
    "getWikiTaxonomy", "getJob", "getResource", "getVersion", "listResources", "listVersions",
    "listJobs", "getRetrievalProfiles",
)


@pytest.fixture(autouse=True)
def offline_identity_and_network(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("FKB_"):
            monkeypatch.delenv(name)
    attempts = []
    original_connect = socket.socket.connect

    def forbidden(*args, **kwargs):
        attempts.append("blocked")
        raise AssertionError("Concurrency tests must not read credentials or invoke processes/models/network")

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            return forbidden()
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(codex_bridge, "_read_private", forbidden)
    monkeypatch.setattr(codex_text, "_read_private", forbidden)
    monkeypatch.setattr(providers, "_master_key", forbidden)
    monkeypatch.setattr(providers, "complete", forbidden)
    yield
    assert not attempts


@pytest.fixture
def database(env):
    engine = env.app.state.engine
    read_engine = env.app.state.read_engine
    read_factory = env.app.state.read_session_factory
    assert engine.dialect.name == "sqlite"
    assert engine.url.database and engine.url.database != ":memory:"
    assert read_engine.pool is not engine.pool
    assert read_engine.url.query["mode"] == "ro"
    assert read_factory.kw["bind"] is read_engine
    assert issubclass(read_factory.class_, ReadOnlySession)
    assert read_factory.kw["autoflush"] is False
    assert env.db.kw["bind"].get_execution_options()["sqlite_transaction_mode"] == "IMMEDIATE"
    assert env.db.kw["autoflush"] is True

    def short_busy_timeout(connection, record, proxy):
        # Shorten only this test database's lock wait, so an IMMEDIATE regression
        # fails promptly. Do not alter journal mode or transaction implementation.
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=100")
        finally:
            cursor.close()

    event.listen(engine, "checkout", short_busy_timeout)
    try:
        for bound_engine, query_only in ((engine, 0), (read_engine, 1)):
            with bound_engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "delete"
                assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == query_only
                assert connection.exec_driver_sql("PRAGMA read_uncommitted").scalar_one() == 0
        yield env
        for bound_engine, query_only in ((engine, 0), (read_engine, 1)):
            with bound_engine.connect() as connection:
                assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "delete"
                assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == query_only
    finally:
        event.remove(engine, "checkout", short_busy_timeout)


@contextmanager
def observe_sql(env):
    trace = SimpleNamespace(statements=[], statement_pools=[], sessions=[], commits=[])
    # HTTP reads own a separate engine/pool; the model authority guard still
    # derives a DEFERRED binding from the writer factory. Observe both paths.
    engines = (env.app.state.engine, env.app.state.read_engine)
    pools = {engine.pool for engine in engines}

    def statement(connection, cursor, sql, parameters, context, executemany):
        trace.statements.append((id(connection.connection.driver_connection), sql.strip().upper()))
        trace.statement_pools.append(connection.engine.pool)

    def begun(session, transaction, connection):
        if connection.engine.pool in pools:
            trace.sessions.append(session)

    def committing(session):
        if session.get_bind().pool in pools:
            trace.commits.append((len(session.new), len(session.dirty), len(session.deleted)))

    for engine in engines:
        event.listen(engine, "before_cursor_execute", statement)
    event.listen(Session, "after_begin", begun)
    event.listen(Session, "before_commit", committing)
    try:
        yield trace
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", statement)
        event.remove(Session, "after_begin", begun)
        event.remove(Session, "before_commit", committing)


def assert_read_trace(trace, *, writer_id=None, read_pool=None):
    sql = [statement for _connection, statement in trace.statements]
    assert sql and sql[0] == "BEGIN DEFERRED", sql
    allowed = {"BEGIN DEFERRED"}
    if read_pool is not None:
        # projection_read verifies the real SQLite read capability. These exact
        # getter forms cannot change either setting; setters/other PRAGMAs fail.
        allowed |= {"PRAGMA QUERY_ONLY", "PRAGMA READ_UNCOMMITTED"}
    unexpected = [statement for statement in sql if statement not in allowed and not statement.startswith("SELECT ")]
    assert not unexpected, unexpected
    if writer_id is not None:
        assert all(connection != writer_id for connection, _statement in trace.statements)
    assert trace.sessions
    assert all(not session.in_transaction() for session in trace.sessions)
    assert all(counts == (0, 0, 0) for counts in trace.commits)
    if read_pool is not None:
        assert all(pool is read_pool for pool in trace.statement_pools)
        assert all(session.get_bind().pool is read_pool and isinstance(session, ReadOnlySession)
                   and session.autoflush is False for session in trace.sessions)


def database_snapshot(env):
    # Compare actual stored rows in every table, including audit, idempotency,
    # session, policy and outbox state. No file bytes or filesystem timestamps.
    with env.app.state.engine.connect() as connection:
        return {table.name: sorted(repr(tuple(row)) for row in connection.execute(select(table)))
                for table in Base.metadata.sorted_tables}


@contextmanager
def reserved_writer(env):
    with env.db() as writer:
        writer.begin()
        connection = writer.connection()  # Emits BEGIN IMMEDIATE before any read.
        assert connection.get_execution_options()["sqlite_transaction_mode"] == "IMMEDIATE"
        writer.info["test_connection_id"] = id(connection.connection.driver_connection)
        try:
            yield writer
        finally:
            writer.rollback()


@pytest.fixture
def readable(database):
    env = database
    source = page(env, "并发来源", kind="document")
    knowledge = page(env, "并发知识", text="按并发来源核对估值适用条件。", cites=[source])
    jid = svc.uid()
    with env.db.begin() as db:
        db.add(m.Job(id=jid, kind="COMPILE", owner_id=env.owner, resource_id=knowledge[0],
                     version_id=knowledge[1], state="SUCCEEDED", dedupe_key="synthetic:" + jid,
                     payload={"task": "WIKI_BUILD", "space_id": env.space,
                              "source_snapshot": [{"version_id": source[1]}]},
                     result={"created_resource_ids": [knowledge[0]], "created_version_ids": [knowledge[1]]}))
    paths = {
        "getWikiGraph": f"/wiki/graph?space_id={env.space}",
        "getWikiWorkspace": f"/wiki/workspace?space_id={env.space}",
        "getRetrievalProfiles": f"/retrieval/profiles?space_id={env.space}",
        "getWikiPageLinks": f"/wiki/pages/{knowledge[0]}/links",
        "resolveWikiTitle": f"/wiki/resolve?space_id={env.space}&title={quote('并发知识')}",
        "getWikiTaxonomy": f"/wiki/taxonomy?space_id={env.space}",
        "getJob": f"/jobs/{jid}",
        "getResource": f"/resources/{knowledge[0]}",
        "getVersion": f"/versions/{knowledge[1]}",
        "listResources": f"/resources?space_id={env.space}",
        "listVersions": f"/resources/{knowledge[0]}/versions",
        "listJobs": "/jobs",
    }
    assert set(paths) == set(READ_OPERATIONS)
    return env, source, knowledge, paths


def test_only_explicit_read_operations_are_allowlisted(database):
    env = database
    assert api.READ_ONLY_OPERATIONS == EXPECTED_READ_ONLY_OPERATIONS
    assert api.ASYNC_OIDC_OPERATIONS == {"beginLogin", "finishLogin"}
    assert api.READ_ONLY_OPERATIONS.isdisjoint(api.ASYNC_OIDC_OPERATIONS)
    readonly_posts = {"searchKnowledge", "searchHybridKnowledge"}
    assert api.READ_ONLY_POST == readonly_posts
    expected_gets = (EXPECTED_READ_ONLY_OPERATIONS - readonly_posts) | {"beginLogin", "finishLogin"}
    paths = env.app.openapi()["paths"]
    get_operations = [spec["get"]["operationId"] for spec in paths.values() if "get" in spec]
    assert set(get_operations) == expected_gets
    assert len(get_operations) == len(expected_gets), "GET operation IDs must remain unique"
    read_methods = {(operation["operationId"], method) for spec in paths.values()
                    for method, operation in spec.items()
                    if method in {"get", "post", "put", "patch", "delete"}
                    and operation["operationId"] in EXPECTED_READ_ONLY_OPERATIONS}
    assert read_methods == {(operation, "get") for operation in EXPECTED_READ_ONLY_OPERATIONS - readonly_posts} | {
        (operation, "post") for operation in readonly_posts}
    # Include the two non-schema GETs without admitting arbitrary /api/v1 routes.
    get_routes = [route for route in env.app.routes
                  if route.path.startswith(env.settings.api_prefix + "/") and "GET" in route.methods]
    documented_gets = {(env.settings.api_prefix + path, spec["get"]["operationId"])
                       for path, spec in paths.items() if "get" in spec}
    expected_routes = documented_gets | {(env.settings.api_prefix + "/auth/demo", None),
                                        (env.settings.api_prefix + "/system/status", None)}
    assert {(route.path, route.operation_id) for route in get_routes} == expected_routes
    assert len(get_routes) == len(expected_routes)


@pytest.mark.parametrize("operation", READ_OPERATIONS)
def test_safe_get_uses_independent_deferred_transaction_while_writer_holds_reservation(readable, operation):
    env, source, _knowledge, paths = readable
    path = paths[operation]
    baseline = env.call("GET", path)
    assert baseline.status_code == 200, baseline.text
    before = database_snapshot(env)
    with reserved_writer(env) as writer:
        writer.get(m.Resource, source[0]).restricted = True
        writer.flush()
        with observe_sql(env) as trace:
            # Repetition detects a read session inadvertently retained across calls.
            for _ in range(3):
                response = env.call("GET", path)
                assert response.status_code == 200, response.text
                assert response.json() == baseline.json(), "An uncommitted ACL edit leaked into a read"
        assert writer.in_transaction()
        assert_read_trace(trace, writer_id=writer.info["test_connection_id"], read_pool=env.app.state.read_engine.pool)
        assert len(trace.sessions) == 3 and len({id(session) for session in trace.sessions}) == 3
        assert len(trace.commits) == 3
        assert database_snapshot(env) == before
    assert database_snapshot(env) == before


@pytest.mark.parametrize("operation", READ_OPERATIONS)
def test_next_get_rechecks_authority_after_writer_commits(readable, operation):
    env, _source, _knowledge, paths = readable
    path = paths[operation]
    with reserved_writer(env) as writer:
        # Covers authentication on all allowlisted operations, including taxonomy.
        writer.get(m.User, env.owner).active = False
        writer.flush()
        with observe_sql(env) as trace:
            response = env.call("GET", path)
        assert response.status_code == 200, response.text
        assert_read_trace(trace, writer_id=writer.info["test_connection_id"], read_pool=env.app.state.read_engine.pool)
        writer.commit()
    before_denial = database_snapshot(env)
    with observe_sql(env) as trace:
        denied = env.call("GET", path)
    assert denied.status_code == 401 and denied.json()["code"] == "AUTH_REQUIRED"
    assert_read_trace(trace, read_pool=env.app.state.read_engine.pool)
    assert database_snapshot(env) == before_denial


@pytest.mark.parametrize("operation", ["getWikiGraph", "getWikiWorkspace", "getWikiPageLinks",
                                      "resolveWikiTitle", "getJob", "getResource", "getVersion"])
def test_committed_source_acl_revocation_is_not_hidden_by_read_factory(readable, operation):
    env, source, knowledge, paths = readable
    with reserved_writer(env) as writer:
        writer.get(m.Resource, source[0]).restricted = True
        writer.flush()
        assert env.call("GET", paths[operation]).status_code == 200
        writer.commit()
    before = database_snapshot(env)
    with observe_sql(env) as trace:
        response = env.call("GET", paths[operation])
    if operation in {"getWikiGraph", "getWikiWorkspace"}:
        assert response.status_code == 200, response.text
        assert source[0] not in response.text and knowledge[0] not in response.text
    else:
        assert response.status_code == 404, response.text
    assert_read_trace(trace, read_pool=env.app.state.read_engine.pool)
    assert database_snapshot(env) == before


@pytest.fixture
def authority(database):
    env = database
    cid, oid = svc.uid(), svc.uid()
    # These are synthetic identity metadata, not credential material. The real
    # text_snapshot/state_policy/authorize_model_snapshot implementations run.
    with env.db.begin() as db:
        db.add(m.RuntimePolicy(id=cid, name="model-connection:" + cid, updated_by=env.owner,
            config={"owner_user_id": env.owner, "space_id": env.space, "name": "合成订阅连接",
                    "provider_id": "chatgpt-codex", "protocol": "codex_app_server",
                    "credential_mode": "chatgpt_oauth", "enabled": True, "allow_document_transfer": True,
                    "custom_models": [{"id": "synthetic-text", "name": "Synthetic"}]}))
        db.add(m.RuntimePolicy(id=oid, name="model-oauth:" + cid, updated_by=env.owner,
            config={"owner_user_id": env.owner, "connection_id": cid, "state": "AUTHENTICATED",
                    "auth_epoch": 1, "connection_revision": 1}))
    object.__setattr__(env.settings, "_codex_bridge", SimpleNamespace(
        text_engine=SimpleNamespace(models=frozenset({"synthetic-text"}))))
    object.__setattr__(env.settings, "_codex_session_factory", env.db)
    with env.db() as db:
        snapshot = codex_text.text_snapshot(db, db.get(m.User, env.owner), db.get(m.RuntimePolicy, cid),
                                           "synthetic-text", env.settings, env.space)
    return env, snapshot["_authority_check"], cid, oid


@pytest.mark.parametrize("change,code", [
    ("principal", "CODEX_AUTH_REQUIRED"),
    ("oauth_epoch", "CODEX_LOGIN_STALE"),
    ("connection", "CONNECTION_REVISION_CHANGED"),
    ("space_acl", "NOT_FOUND"),
])
def test_guard_is_deferred_without_autoflush_and_rejects_next_committed_revocation(authority, change, code):
    env, guard, cid, oid = authority
    original_factory = dict(env.db.kw)
    guard()
    before = database_snapshot(env)
    with reserved_writer(env) as writer:
        if change == "principal":
            writer.get(m.User, env.owner).active = False
        elif change == "oauth_epoch":
            state = writer.get(m.RuntimePolicy, oid)
            state.config = {**state.config, "auth_epoch": 2}
        elif change == "connection":
            connection = writer.get(m.RuntimePolicy, cid)
            connection.config = {**connection.config, "enabled": False}
        else:
            for member in writer.scalars(select(m.SpaceMember).where(
                    m.SpaceMember.space_id == env.space, m.SpaceMember.user_id == env.owner)):
                writer.delete(member)
        writer.flush()
        with observe_sql(env) as trace:
            for _ in range(10):
                guard()
        assert_read_trace(trace, writer_id=writer.info["test_connection_id"])
        assert len(trace.sessions) == 10 and all(session.autoflush is False for session in trace.sessions)
        assert len({id(session) for session in trace.sessions}) == 10
        assert trace.commits == [], "Authorization reads must not commit identity changes"
        assert database_snapshot(env) == before
        writer.commit()  # Must succeed: guard() released its SHARED read locks.
    committed = database_snapshot(env)
    with observe_sql(env) as denied_trace, pytest.raises((providers.ProviderError, svc.APIError)) as error:
        guard()
    assert error.value.code == code
    assert_read_trace(denied_trace)
    assert database_snapshot(env) == committed
    assert env.db.kw == original_factory, "Guard construction modified the original writer factory"
    assert env.db.kw["bind"].get_execution_options()["sqlite_transaction_mode"] == "IMMEDIATE"
    assert env.db.kw["autoflush"] is True


def test_same_runtime_write_factory_still_contends_for_real_writer_reservation(authority):
    env, guard, _cid, _oid = authority
    with reserved_writer(env):
        # This negative control proves the positive read tests actually hold a
        # writer reservation on a separate connection to the same file.
        with observe_sql(env) as blocked, pytest.raises(OperationalError) as error, env.db() as other_writer:
            other_writer.get(m.User, env.owner)
        assert "locked" in str(error.value.orig).lower()
        assert blocked.statements[0][1] == "BEGIN IMMEDIATE"
        with observe_sql(env) as trace:
            guard()
        assert_read_trace(trace)


def test_mutating_http_request_keeps_immediate_and_commits_only_after_reservation_released(database):
    env = database
    body = {"space_id": env.space, "path": "并发写入分类"}
    before = database_snapshot(env)
    with reserved_writer(env):
        with observe_sql(env) as blocked, pytest.raises(OperationalError) as error:
            env.call("POST", "/wiki/categories", body)
        assert "locked" in str(error.value.orig).lower()
        assert blocked.statements[0][1] == "BEGIN IMMEDIATE"
        assert database_snapshot(env) == before
    with observe_sql(env) as written:
        created = env.call("POST", "/wiki/categories", body)
    assert created.status_code == 201, created.text
    assert written.statements[0][1] == "BEGIN IMMEDIATE"
    assert any(sql.startswith("INSERT ") for _, sql in written.statements)
    taxonomy = env.call("GET", f"/wiki/taxonomy?space_id={env.space}")
    assert taxonomy.status_code == 200
    assert any(item["path"] == body["path"] for item in taxonomy.json()["categories"])


def test_unlisted_get_is_rejected_at_registration(database, monkeypatch):
    # /jobs now explicitly uses read_factory. A genuinely new, unreviewed GET
    # must fail registration, before it can inherit either transaction factory.
    operation = "getUnreviewedWikiRead"
    assert operation not in api.READ_ONLY_OPERATIONS | api.ASYNC_OIDC_OPERATIONS
    spec = copy.deepcopy(api_wiki_reader.PATHS["/wiki/catalog"])
    spec["get"]["operationId"] = operation
    monkeypatch.setitem(api_wiki_reader.PATHS, "/wiki/unreviewed-read", spec)
    monkeypatch.setitem(api_wiki_reader.HANDLERS, operation, api_wiki_reader.get_catalog)
    with pytest.raises(RuntimeError, match=f"^GET operation has no reviewed read/OIDC capability: {operation}$"):
        api.create_app(database.settings)


def test_write_csrf_guard_is_unchanged(database):
    env = database
    with observe_sql(env) as trace:
        response = env.client.post("/api/v1/wiki/categories", json={"space_id": env.space, "path": "禁止创建"},
                                   headers={"Origin": "http://testserver", "Idempotency-Key": svc.uid()})
    assert response.status_code == 403 and response.json()["code"] == "CSRF_REJECTED"
    assert trace.statements[0][1] == "BEGIN IMMEDIATE"
    with env.db() as db:
        assert db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == "wiki-taxonomy:" + env.space)) is None


@pytest.mark.parametrize("etag_case", ["missing", "stale", "current"])
def test_version_write_fence_and_immediate_transaction_are_unchanged(database, etag_case):
    env = database
    record = page(env, "可编辑并发草稿", state="DRAFT")
    current = env.call("GET", f"/versions/{record[1]}")
    assert current.status_code == 200, current.text
    body = {key: copy.deepcopy(current.json()[key]) for key in (
        "title", "knowledge_type", "applicability", "required_facts", "legal_status", "valid_from", "valid_to", "blocks")}
    body["blocks"][0]["data"]["text"] = "经过版本前置条件校验后的修改。"
    etag = None if etag_case == "missing" else '"0"' if etag_case == "stale" else current.headers["etag"]
    with observe_sql(env) as trace:
        response = env.call("PATCH", f"/versions/{record[1]}", body, etag=etag)
    assert trace.statements[0][1] == "BEGIN IMMEDIATE"
    after = env.call("GET", f"/versions/{record[1]}")
    assert after.status_code == 200, after.text
    if etag_case == "current":
        assert response.status_code == 200, response.text
        assert after.headers["etag"] != current.headers["etag"]
        assert after.json()["blocks"][0]["data"]["text"] == body["blocks"][0]["data"]["text"]
    else:
        assert response.status_code == (428 if etag_case == "missing" else 412), response.text
        assert after.json() == current.json()


DEPENDENCY_CHANNELS = ("citation", "relation", "image", "attachment")


def dependency_values(channel, root, target, *, ordinal=1):
    if channel == "citation":
        return m.EvidenceLink, {"id": svc.uid(), "from_version_id": root[1], "from_block_id": root[2],
                               "to_version_id": target[1], "to_block_id": target[2], "purpose": "FACT"}
    if channel == "relation":
        return m.RelationEdge, {"id": svc.uid(), "source_version_id": root[1], "target_resource_id": target[0],
                               "relation_type": "REQUIRES", "conditions": {},
                               "evidence_version_id": target[1], "evidence_block_id": target[2]}
    data = {"version_id": target[1], "caption": "合成依赖附件"}
    text = block_text({"block_type": channel, "data": data})
    return m.ContentBlock, {"version_id": root[1], "block_id": svc.uid(), "ordinal": ordinal,
                           "block_type": channel, "data": data, "locator": {}, "search_text": text,
                           "content_sha256": text_sha256(text)}


def dependency_identity(model, values):
    return (values["version_id"], values["block_id"]) if model is m.ContentBlock else values["id"]


def dependency_where(model, values):
    if model is m.ContentBlock:
        return (model.version_id == values["version_id"], model.block_id == values["block_id"])
    return (model.id == values["id"],)


def retarget_values(channel, target):
    if channel == "citation":
        return {"to_version_id": target[1], "to_block_id": target[2]}
    if channel == "relation":
        return {"target_resource_id": target[0], "evidence_version_id": target[1], "evidence_block_id": target[2]}
    return {"data": {"version_id": target[1], "caption": "合成依赖附件"}}


def seed_dependency(env, channel):
    # Documents avoid mixing adjacency behavior with generated Wiki provenance.
    root = page(env, "结构缓存宿主", kind="document")
    old = page(env, "旧依赖", kind="document")
    new = page(env, "新依赖", kind="document")
    model, values = dependency_values(channel, root, old)
    with env.db.begin() as db:
        db.add(model(**values))
    return root, old, new, model, values


def assert_structure(db, version, *, dependencies, targets):
    # Validate both the public dependency set and the target-only adjacency used
    # by check_dependency_access; a relation target is not itself evidence.
    assert svc.dependency_ids(db, version) == set(dependencies)
    ids, actual_targets = svc._dependency_structure(db, version)
    assert ids == frozenset(dependencies)
    assert actual_targets == frozenset(targets)


@pytest.mark.parametrize("channel", DEPENDENCY_CHANNELS)
@pytest.mark.parametrize("operation", ["insert", "update", "delete"])
@pytest.mark.parametrize("write_path", ["orm_flush", "bulk_dml"])
def test_structure_cache_invalidates_after_mutation_and_rollback_without_parent_revision_change(
        database, channel, operation, write_path):
    env = database
    root, old, new, model, values = seed_dependency(env, channel)
    before = database_snapshot(env)
    with env.db() as db:
        version = db.get(m.ResourceVersion, root[1])
        initial_revision = version.revision
        original_targets = {old[0]} if channel == "relation" else set()
        assert_structure(db, version, dependencies={old[1]}, targets=original_targets)
        if operation == "insert":
            _, added = dependency_values(channel, root, new, ordinal=2)
            if write_path == "orm_flush":
                db.add(model(**added))
            else:
                db.execute(insert(model).values(**added))
            expected_ids = {old[1], new[1]}
            expected_targets = {old[0], new[0]} if channel == "relation" else set()
        elif operation == "update":
            changed = retarget_values(channel, new)
            if write_path == "orm_flush":
                row = db.get(model, dependency_identity(model, values))
                for field, value in changed.items():
                    setattr(row, field, value)
            else:
                db.execute(update(model).where(*dependency_where(model, values)).values(**changed)
                           .execution_options(synchronize_session=False))
            expected_ids = {new[1]}
            expected_targets = {new[0]} if channel == "relation" else set()
        else:
            if write_path == "orm_flush":
                db.delete(db.get(model, dependency_identity(model, values)))
            else:
                db.execute(delete(model).where(*dependency_where(model, values))
                           .execution_options(synchronize_session=False))
            expected_ids, expected_targets = set(), set()
        db.flush()
        # No pending ORM changes or parent revision bump may accidentally mask
        # a missing after_flush / bulk DML invalidation hook.
        assert not (db.new or db.dirty or db.deleted)
        assert version.revision == initial_revision
        assert_structure(db, version, dependencies=expected_ids, targets=expected_targets)
        db.rollback()  # The altered adjacency was cached above; it must expire.
        assert version.revision == initial_revision
        assert_structure(db, version, dependencies={old[1]}, targets=original_targets)
    assert database_snapshot(env) == before


def test_warm_structure_cache_avoids_reloading_paragraph_json_and_returns_independent_sets(database):
    env = database
    root = page(env, "大量正文", kind="document", text="不应为依赖遍历反复解码的正文。" * 3000)
    source = page(env, "真实附件依据", kind="document")
    not_a_dependency = page(env, "普通段落中的字段不是依赖", kind="document")
    with env.db.begin() as db:
        paragraph = db.get(m.ContentBlock, (root[1], root[2]))
        paragraph.data = {**paragraph.data, "version_id": not_a_dependency[1]}
        for ordinal, channel in enumerate(("image", "attachment"), 1):
            model, values = dependency_values(channel, root, source, ordinal=ordinal)
            db.add(model(**values))
    with env.db() as db:
        version = db.get(m.ResourceVersion, root[1])
        with observe_sql(env) as cold:
            ids = svc.dependency_ids(db, version)
        assert ids == {source[1]}
        block_queries = [sql for _, sql in cold.statements if "FROM CONTENT_BLOCKS" in sql]
        assert block_queries and all(sql.split("FROM", 1)[0].strip() == "SELECT CONTENT_BLOCKS.DATA"
                                     and "CONTENT_BLOCKS.BLOCK_TYPE IN" in sql for sql in block_queries)
        assert not any(isinstance(row, m.ContentBlock) for row in db.identity_map.values())
        ids.clear()  # Mutating a caller's returned set must not corrupt the cache.
        with observe_sql(env) as warm:
            for _ in range(100):
                assert svc.dependency_ids(db, version) == {source[1]}
        assert not any(table in sql for _, sql in warm.statements
                       for table in ("CONTENT_BLOCKS", "EVIDENCE_LINKS", "RELATION_EDGES"))


@pytest.mark.parametrize("channel", DEPENDENCY_CHANNELS)
@pytest.mark.parametrize("write_path", ["orm_flush", "bulk_delete"])
def test_same_session_rechecks_acl_after_grant_revocation_even_with_warm_structure(database, channel, write_path):
    env = database
    root, old, _new, _model, _values = seed_dependency(env, channel)
    with env.db.begin() as db:
        db.get(m.Resource, old[0]).restricted = True
        db.add(m.ResourceGrant(resource_id=old[0], user_id=env.owner, permission="read"))
    with env.db() as db:
        version = db.get(m.ResourceVersion, root[1])
        owner, reader = db.get(m.User, env.owner), db.get(m.User, env.reader)
        svc.check_dependency_access(db, owner, version)
        # No mutation between principals: sharing adjacency must not share ACL.
        with pytest.raises(svc.APIError) as denied_reader:
            svc.check_dependency_access(db, reader, version)
        assert denied_reader.value.code == "NOT_FOUND"
        svc.check_dependency_access(db, owner, version)
        grant_filter = (m.ResourceGrant.resource_id == old[0], m.ResourceGrant.user_id == env.owner,
                        m.ResourceGrant.permission == "read")
        if write_path == "orm_flush":
            db.delete(db.scalar(select(m.ResourceGrant).where(*grant_filter)))
            db.flush()
        else:
            db.execute(delete(m.ResourceGrant).where(*grant_filter))
        # Rewarm only structure after the write: authorization must still fail.
        assert svc.dependency_ids(db, version) == {old[1]}
        with pytest.raises(svc.APIError) as revoked:
            svc.check_dependency_access(db, owner, version)
        assert revoked.value.code == "NOT_FOUND"
        db.rollback()
        assert svc.dependency_ids(db, version) == {old[1]}
        svc.check_dependency_access(db, owner, version)


def test_target_only_relation_keeps_acl_checks_without_inventing_evidence_dependencies(database):
    env = database
    root, target, _new, model, values = seed_dependency(env, "relation")
    with env.db.begin() as db:
        relation = db.get(model, values["id"])
        relation.evidence_version_id = relation.evidence_block_id = None
    with env.db() as db:
        version, actor = db.get(m.ResourceVersion, root[1]), db.get(m.User, env.owner)
        assert_structure(db, version, dependencies=set(), targets={target[0]})
        svc.check_dependency_access(db, actor, version)
        db.get(m.Resource, target[0]).restricted = True
        db.flush()
        assert_structure(db, version, dependencies=set(), targets={target[0]})
        with pytest.raises(svc.APIError) as revoked:
            svc.check_dependency_access(db, actor, version)
        assert revoked.value.code == "NOT_FOUND"


@pytest.mark.parametrize("write_path", ["orm_flush", "bulk_insert"])
def test_cycle_introduced_after_cache_warmup_is_detected_and_rollback_restores_access(database, write_path):
    env = database
    root, child, _new, _model, _values = seed_dependency(env, "citation")
    with env.db() as db:
        version, actor = db.get(m.ResourceVersion, root[1]), db.get(m.User, env.owner)
        svc.check_dependency_access(db, actor, version)
        model, values = dependency_values("citation", child, root)
        if write_path == "orm_flush":
            db.add(model(**values))
            db.flush()
        else:
            db.execute(insert(model).values(**values))
        with pytest.raises(svc.APIError) as cycle:
            svc.check_dependency_access(db, actor, version)
        assert cycle.value.code == "DEPENDENCY_CYCLE"
        db.rollback()
        svc.check_dependency_access(db, actor, version)


@pytest.mark.parametrize("operation", ["miss_then_insert", "edit_then_rollback", "delete_then_rollback", "detach"])
def test_policy_row_cache_respects_orm_lifecycle_without_caching_misses(database, operation):
    env = database
    pid, name = svc.uid(), "synthetic-cache-policy:" + svc.uid()
    if operation != "miss_then_insert":
        with env.db.begin() as db:
            db.add(m.RuntimePolicy(id=pid, name=name, config={"value": "original"}, updated_by=env.owner))
    with env.db() as db:
        cached = wiki._policy(db, name)
        if operation == "miss_then_insert":
            assert cached is None
            db.add(m.RuntimePolicy(id=pid, name=name, config={"value": "new"}, updated_by=env.owner))
            db.flush()
            assert wiki._policy(db, name).config == {"value": "new"}
        elif operation == "edit_then_rollback":
            cached.config = {"value": "edited"}
            db.flush()
            assert wiki._policy(db, name).config == {"value": "edited"}
            db.rollback()
            assert wiki._policy(db, name).config == {"value": "original"}
        elif operation == "delete_then_rollback":
            db.delete(cached)
            db.flush()
            assert wiki._policy(db, name) is None
            db.rollback()
            assert wiki._policy(db, name).id == pid
        else:
            db.expunge(cached)
            cached.config = {"value": "detached-only"}
            fresh = wiki._policy(db, name)
            assert fresh is not cached and fresh.config == {"value": "original"}


def test_policy_insert_rollback_does_not_leave_a_cached_nonexistent_row(database):
    env = database
    pid, name = svc.uid(), "synthetic-rolled-back-policy:" + svc.uid()
    with env.db() as db:
        db.add(m.RuntimePolicy(id=pid, name=name, config={"value": "uncommitted"}, updated_by=env.owner))
        db.flush()
        assert wiki._policy(db, name).id == pid
        db.rollback()
        assert db.scalar(select(m.RuntimePolicy).where(m.RuntimePolicy.name == name)) is None
        assert wiki._policy(db, name) is None, "Rolled-back INSERT must not survive in a Session's metadata cache"


@pytest.mark.parametrize("light_path", ["/auth/demo", "/system/status"])
def test_slow_graph_does_not_block_light_request_and_keeps_session_on_one_worker(database, monkeypatch, light_path):
    env = database
    page(env, "轻量并发图节点")
    before = database_snapshot(env)
    entered, release = threading.Event(), threading.Event()
    graph_sessions, phases = [], []
    original_graph = wiki.graph
    pools = {env.app.state.engine.pool, env.app.state.read_engine.pool}
    loop_thread = env.client.portal.call(threading.get_ident)

    def note(session, phase):
        if session.get_bind().pool in pools:
            phases.append((id(session), phase, threading.get_ident()))

    def query(state):
        note(state.session, "query")

    def begun(session, transaction, connection):
        note(session, "begin")

    def committing(session):
        note(session, "commit")

    def ended(session, transaction):
        note(session, "end")

    def slow_graph(db, *args, **kwargs):
        graph_sessions.append(db)
        entered.set()
        assert release.wait(timeout=5), "Test did not release its synthetic slow Graph read"
        return original_graph(db, *args, **kwargs)

    # Delay only the graph work; authentication, SQL, validation and commit stay
    # real. Both requests use one TestClient portal and therefore one ASGI loop.
    monkeypatch.setattr(wiki, "graph", slow_graph)
    listeners = {"do_orm_execute": query, "after_begin": begun,
                 "before_commit": committing, "after_transaction_end": ended}
    for name, callback in listeners.items():
        event.listen(Session, name, callback)
    try:
        with observe_sql(env) as trace, ThreadPoolExecutor(max_workers=2) as requests:
            graph_future = requests.submit(env.call, "GET", f"/wiki/graph?space_id={env.space}")
            try:
                assert entered.wait(timeout=2), "Graph did not reach its synthetic slow phase"
                light_future = requests.submit(env.call, "GET", light_path)
                light = light_future.result(timeout=2)
                assert light.status_code == 200, light.text
                assert not graph_future.done(), "The light request was serialized behind Graph"
            finally:
                release.set()
            response = graph_future.result(timeout=2)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-trace-id"]
        assert_read_trace(trace, read_pool=env.app.state.read_engine.pool)
        assert len(trace.sessions) == 2 and len({id(session) for session in trace.sessions}) == 2
        assert len(trace.commits) == 2
        for read_session in trace.sessions:
            session_phases = [(phase, tid) for sid, phase, tid in phases if sid == id(read_session)]
            assert {phase for phase, _tid in session_phases} >= {"query", "begin", "commit", "end"}
            session_threads = {tid for _phase, tid in session_phases}
            assert len(session_threads) == 1 and loop_thread not in session_threads
        session, = graph_sessions
        graph_phases = [(phase, tid) for sid, phase, tid in phases if sid == id(session)]
        assert {phase for phase, _tid in graph_phases} >= {"query", "begin", "commit", "end"}
        worker_ids = {tid for _phase, tid in graph_phases}
        assert len(worker_ids) == 1, "A single Graph Session crossed threads"
        light_ids = {tid for sid, _phase, tid in phases if sid != id(session)}
        assert light_ids and worker_ids.isdisjoint(light_ids), "Overlapping Graph and light requests shared a worker"
        assert not session.in_transaction()
    finally:
        release.set()
        for name, callback in listeners.items():
            event.remove(Session, name, callback)
    assert database_snapshot(env) == before


@pytest.mark.parametrize("query,status,code", [
    ("", 400, "MISSING_PARAMETER"),
    ("space_id=INVALID_SPACE_CANARY", 422, "SCHEMA_VALIDATION"),
    ("space_id={space}&depth=INVALID_DEPTH_CANARY", 422, "INVALID_PARAMETER"),
    ("space_id={space}&depth=4", 422, "SCHEMA_VALIDATION"),
    ("space_id={space}&node_role=INVALID_ROLE_CANARY", 422, "SCHEMA_VALIDATION"),
    ("space_id={space}&business_date=INVALID_DATE_CANARY", 422, "SCHEMA_VALIDATION"),
])
def test_threaded_read_preserves_parameter_error_codes_and_safe_response(database, query, status, code):
    env = database
    before = database_snapshot(env)
    response = env.call("GET", "/wiki/graph?" + query.format(space=env.space))
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert response.json()["trace_id"]
    assert response.headers["cache-control"] == "private, no-store"
    assert "CANARY" not in response.text
    assert database_snapshot(env) == before
