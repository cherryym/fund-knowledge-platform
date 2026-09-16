"""Request/thread/transaction isolation on pytest-owned SQLite files only."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, event, insert, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_wiki import env as env  # noqa: PLC0414
from test_wiki import page

from fund_kb import api, auth, models as m, providers, wiki
from fund_kb import services as svc
from fund_kb.db import Base, ReadOnlySession, ReadOnlyViolation, build_engine, make_session_factory


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("FKB_"):
            monkeypatch.delenv(name)
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("blocked")
        raise AssertionError("Isolation tests must not use models, credentials, processes or network")

    def guarded(original):
        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                return forbidden()
            return original(sock, address)
        return connect

    for name in ("connect", "connect_ex", "bind"):
        monkeypatch.setattr(socket.socket, name, guarded(getattr(socket.socket, name)))
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(providers, "complete", forbidden)
    monkeypatch.setattr(providers, "_master_key", forbidden)
    yield
    assert not attempts


def snapshot(env):
    with env.db() as db:
        return {table.name: sorted(repr(tuple(row)) for row in db.execute(select(table)))
                for table in Base.metadata.sorted_tables}


@contextmanager
def trace(env):
    engines = (env.app.state.engine, env.app.state.read_engine)
    pools = {engine.pool for engine in engines}
    rows, sessions, phases = [], {}, []

    def sql(conn, _cursor, statement, _params, _context, _many):
        rows.append((threading.get_ident(), conn.engine.pool, statement.strip().upper()))

    def phase(session, name):
        if session.get_bind().pool in pools:
            sessions[id(session)] = session
            phases.append((id(session), name, threading.get_ident()))

    def begin(session, _transaction, _connection):
        phase(session, "begin")

    def commit(session):
        phase(session, "commit")

    def end(session, _transaction):
        phase(session, "end")

    for engine in engines:
        event.listen(engine, "before_cursor_execute", sql)
    for name, function in (("after_begin", begin), ("before_commit", commit), ("after_transaction_end", end)):
        event.listen(Session, name, function)
    try:
        yield rows, sessions, phases
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", sql)
        for name, function in (("after_begin", begin), ("before_commit", commit), ("after_transaction_end", end)):
            event.remove(Session, name, function)


def test_every_get_declares_read_or_oidc_and_search_is_read_only(env):
    get_operations = {spec["get"]["operationId"] for spec in env.app.openapi()["paths"].values() if "get" in spec}
    assert get_operations == (api.READ_ONLY_OPERATIONS - api.READ_ONLY_POST) | api.ASYNC_OIDC_OPERATIONS
    assert api.READ_ONLY_OPERATIONS.isdisjoint(api.ASYNC_OIDC_OPERATIONS)
    assert api.ASYNC_OIDC_OPERATIONS == {"beginLogin", "finishLogin"}
    assert env.client.get("/api/v1/health").status_code == 200


def test_pools_and_session_capabilities_are_independent_and_writer_stays_writable(env, tmp_path):
    assert Path(env.app.state.engine.url.database).resolve().parent == tmp_path.resolve()
    read_factory = env.app.state.read_session_factory
    assert env.app.state.engine.pool is not env.app.state.read_engine.pool
    assert issubclass(read_factory.class_, ReadOnlySession) and read_factory.kw["autoflush"] is False
    assert env.db.kw["autoflush"] is True
    assert env.db.kw["bind"].get_execution_options()["sqlite_transaction_mode"] == "IMMEDIATE"
    for _ in range(3):
        with read_factory() as db:
            assert db.scalar(select(m.User.id).where(m.User.id == env.owner)) == env.owner
            assert db.connection().exec_driver_sql("PRAGMA query_only").scalar_one() == 1
        with env.db.begin() as db:
            assert db.connection().exec_driver_sql("PRAGMA query_only").scalar_one() == 0
            db.get(m.User, env.owner).display_name = "仍可写入临时测试库"


def test_read_engine_does_not_create_missing_database_and_handles_escaped_paths(tmp_path):
    missing = tmp_path / "不存在 #只读.sqlite"
    reader = build_engine(f"sqlite:///{missing}", read_only=True)
    try:
        with pytest.raises(DBAPIError), reader.connect():
            pass
        assert not missing.exists()
    finally:
        reader.dispose()
    writer = build_engine(f"sqlite:///{missing}")
    reader = build_engine(f"sqlite:///{missing}", read_only=True)
    try:
        Base.metadata.create_all(writer)
        with reader.connect() as conn:
            assert conn.scalar(select(m.User.id)) is None
    finally:
        reader.dispose()
        writer.dispose()


@pytest.mark.parametrize("path", [
    "/spaces", "/libraries", "/documents?space_id={space}", "/documents/taxonomy?space_id={space}",
    "/wiki/workspace?space_id={space}", "/wiki/taxonomy?space_id={space}", "/system/status", "/auth/demo",
])
def test_sync_get_owns_session_and_serialization_on_one_worker(env, monkeypatch, path):
    loop_thread = env.client.portal.call(threading.get_ident)
    rendered = []
    original_render = api.JSONResponse.render

    def render(response, content):
        rendered.append(threading.get_ident())
        return original_render(response, content)

    monkeypatch.setattr(api.JSONResponse, "render", render)
    with trace(env) as (rows, sessions, phases):
        response = env.call("GET", path.format(space=env.space))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store" and response.headers["x-trace-id"]
    assert rows and all(pool is env.app.state.read_engine.pool for _tid, pool, _sql in rows)
    assert rows[0][2] == "BEGIN DEFERRED"
    assert all(sql in {"BEGIN DEFERRED", "PRAGMA QUERY_ONLY", "PRAGMA READ_UNCOMMITTED"}
        or sql.startswith("SELECT ") for _tid, _pool, sql in rows)
    assert len(sessions) == 1 and all(isinstance(session, ReadOnlySession) for session in sessions.values())
    tids = {tid for _sid, _phase, tid in phases} | set(rendered)
    assert len(tids) == 1 and loop_thread not in tids
    assert {phase for _sid, phase, _tid in phases} >= {"begin", "commit", "end"}
    assert all(not session.in_transaction() for session in sessions.values())


@pytest.mark.parametrize("mutation", [
    "dirty", "flush", "orm_update", "orm_insert", "orm_delete", "bulk", "core", "raw", "ddl", "disable", "lock",
])
def test_get_rejects_every_write_path_without_audit_or_business_mutation(env, monkeypatch, mutation):
    before = snapshot(env)
    env.app.state.raise_test_errors = False
    original = wiki.taxonomy

    def faulty(db, user, space_id, **kwargs):
        result = original(db, user, space_id, **kwargs)
        if mutation in {"dirty", "flush"}:
            db.get(m.User, user.id).display_name = "禁止写入"
            if mutation == "flush":
                db.flush()
        elif mutation == "orm_update":
            db.execute(update(m.User).where(m.User.id == user.id).values(display_name="禁止写入"))
        elif mutation == "orm_insert":
            db.execute(insert(m.User).values(id=svc.uid(), external_subject="test:forbidden", display_name="禁止写入"))
        elif mutation == "orm_delete":
            db.execute(delete(m.User).where(m.User.id == "missing"))
        elif mutation == "bulk":
            db.bulk_update_mappings(m.User, [{"id": user.id, "display_name": "禁止写入"}])
        elif mutation == "core":
            db.connection().execute(update(m.User).values(display_name="禁止写入"))
        elif mutation == "raw":
            db.connection().exec_driver_sql("UPDATE users SET display_name='FORBIDDEN'")
        elif mutation == "ddl":
            db.connection().exec_driver_sql("CREATE TABLE forbidden (id INTEGER)")
        elif mutation == "disable":
            db.connection().exec_driver_sql("PRAGMA query_only=OFF")
        else:
            db.execute(select(m.User).with_for_update())
        return result

    monkeypatch.setattr(wiki, "taxonomy", faulty)
    response = env.call("GET", f"/wiki/taxonomy?space_id={env.space}")
    assert response.status_code == 500, response.text
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert "FORBIDDEN" not in response.text and "users" not in response.text
    assert snapshot(env) == before


def test_readonly_session_guards_apply_without_http_wrapper(env):
    with env.app.state.read_session_factory() as db:
        db.get(m.User, env.owner).display_name = "禁止提交"
        with pytest.raises(ReadOnlyViolation):
            db.commit()
        db.rollback()
        with pytest.raises(ReadOnlyViolation):
            db.execute(update(m.User).values(display_name="禁止更新"))


def test_writer_wait_and_saturated_write_budget_do_not_block_reads_or_asgi(env):
    # Saturate only the test's write admission budget. No production setting changes.
    env.client.portal.call(setattr, env.app.state.request_write_limiter, "total_tokens", 1)
    entered = threading.Event()
    fixture_thread = threading.get_ident()

    def waiting(_conn, _cursor, statement, _params, _context, _many):
        if statement.upper().startswith("BEGIN IMMEDIATE") and threading.get_ident() != fixture_thread:
            entered.set()

    event.listen(env.app.state.engine, "before_cursor_execute", waiting)
    try:
        with env.db() as writer, ThreadPoolExecutor(max_workers=3) as requests:
            writer.connection()  # Keep a real writer reservation on the same temporary file.
            first = requests.submit(env.call, "POST", "/threads", {"space_id": env.space, "title": "等待写锁"})
            try:
                assert entered.wait(3)
                second = requests.submit(env.call, "POST", "/threads", {"space_id": env.space, "title": "等待写预算"})
                light = requests.submit(env.call, "GET", "/libraries").result(timeout=3)
                assert light.status_code == 200, light.text
                assert not first.done() and not second.done()
                assert env.client.portal.call(threading.get_ident) != fixture_thread
            finally:
                writer.rollback()
            assert first.result(timeout=3).status_code == second.result(timeout=3).status_code == 201
    finally:
        event.remove(env.app.state.engine, "before_cursor_execute", waiting)


def test_slow_read_does_not_block_light_get_or_share_its_session(env, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = wiki.workspace

    def slow(db, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(db, *args, **kwargs)

    monkeypatch.setattr(wiki, "workspace", slow)
    with ThreadPoolExecutor(max_workers=2) as requests:
        slow_request = requests.submit(env.call, "GET", f"/wiki/workspace?space_id={env.space}")
        try:
            assert entered.wait(3)
            light = requests.submit(env.call, "GET", "/system/status").result(timeout=3)
            assert light.status_code == 200 and not slow_request.done()
        finally:
            release.set()
        assert slow_request.result(timeout=3).status_code == 200


@pytest.mark.parametrize("dispatch_failure", [False, True])
def test_write_commit_idempotency_audit_outbox_and_post_commit_dispatch(env, dispatch_failure):
    thread = env.call("POST", "/threads", {"space_id": env.space, "title": "合成提交"}).json()
    loop_thread = env.client.portal.call(threading.get_ident)
    calls = []

    def dispatch(job_id):
        assert threading.get_ident() != loop_thread
        with env.db() as db:
            assert db.get(m.Job, job_id) is not None
            assert db.scalar(select(m.Outbox).where(m.Outbox.aggregate_id == job_id)) is not None
            assert db.scalar(select(m.IdempotencyRecord).where(m.IdempotencyRecord.key == "isolation-once")) is not None
        calls.append(job_id)
        if dispatch_failure:
            raise RuntimeError("synthetic dispatch failure")

    env.app.state.job_dispatcher = dispatch
    path = f"/threads/{thread['id']}/runs"
    data = {"question": "合成请求，不执行模型", "mode": "answer", "context": {}, "answer_scope": "formal"}
    with trace(env) as (_rows, sessions, phases):
        response = env.call("POST", path, data, key="isolation-once")
    assert response.status_code == 202, response.text
    assert calls == [response.json()["job_id"]]
    for sid in sessions:
        tids = {tid for session_id, _phase, tid in phases if session_id == sid}
        assert len(tids) == 1 and loop_thread not in tids
        assert not sessions[sid].in_transaction()
    again = env.call("POST", path, data, key="isolation-once")
    assert again.status_code == 202 and again.json() == response.json()
    assert len(calls) == 1
    with env.db() as db:
        assert len(list(db.scalars(select(m.Job)))) == 1
        assert len(list(db.scalars(select(m.Outbox)))) == 1
        assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "run.created")) is not None
    mismatch = env.call("POST", path, {**data, "question": "不同请求"}, key="isolation-once")
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_serialization_failure_rolls_back_business_audit_and_idempotency(env, monkeypatch):
    before = snapshot(env)
    env.app.state.raise_test_errors = False

    def invalid(_result):
        raise RuntimeError("PRIVATE_RESPONSE_CANARY")

    with monkeypatch.context() as patch:
        patch.setattr(api, "response_for", invalid)
        response = env.call("POST", "/threads", {"space_id": env.space, "title": "不应提交"}, key="rollback-key")
    assert response.status_code == 500 and "PRIVATE_RESPONSE_CANARY" not in response.text
    assert snapshot(env) == before
    retry = env.call("POST", "/threads", {"space_id": env.space, "title": "不应提交"}, key="rollback-key")
    assert retry.status_code == 201


def test_csrf_etag_errors_and_denied_audit_stay_intact(env):
    resource = page(env, "合成原名")
    denied = env.client.patch(f"/api/v1/resources/{resource[0]}", json={"name": "不应修改"},
        headers={"Origin": "http://testserver", "Idempotency-Key": "csrf-denied", "If-Match": '"1"'})
    assert denied.status_code == 403 and denied.json()["code"] == "CSRF_REJECTED"
    missing = env.call("PATCH", f"/resources/{resource[0]}", {"name": "不应修改"})
    assert missing.status_code == 428 and missing.json()["code"] == "PRECONDITION_REQUIRED"
    stale = env.call("PATCH", f"/resources/{resource[0]}", {"name": "不应修改"}, etag='"999999"')
    assert stale.status_code == 412
    with env.db() as db:
        assert db.get(m.Resource, resource[0]).name == "合成原名"
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.object_type == "request")))
        assert len(events) == 3
        assert {event.details["code"] for event in events} >= {"CSRF_REJECTED", "PRECONDITION_REQUIRED"}


def test_fresh_read_rechecks_identity_and_hidden_source_lineage(env):
    source = page(env, "私有来源标记", kind="document")
    derived = page(env, "派生知识标记", cites=[source])
    path = f"/wiki/workspace?space_id={env.space}"
    assert derived[0] in env.call("GET", path).text
    with env.db.begin() as db:
        db.get(m.Resource, source[0]).restricted = True
    hidden = env.call("GET", path)
    assert hidden.status_code == 200 and hidden.json()["stats"]["total_pages"] == 0
    assert all(value not in hidden.text for value in (source[0], derived[0], "派生知识标记", "私有来源标记"))
    with env.db.begin() as db:
        db.get(m.User, env.owner).active = False
    revoked = env.call("GET", path)
    assert revoked.status_code == 401 and revoked.json()["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize("operation", ["beginLogin", "finishLogin"])
def test_oidc_has_dedicated_worker_loop_and_no_transaction_during_external_wait(tmp_path, monkeypatch, operation):
    entered, release = threading.Event(), threading.Event()
    observed = []

    async def synthetic_oidc(ctx):
        observed.append((threading.get_ident(), id(asyncio.get_running_loop()), ctx.db.in_transaction()))
        entered.set()
        for _ in range(500):
            if release.is_set():
                break
            await asyncio.sleep(.01)
        assert release.is_set()
        response = RedirectResponse("/", status_code=302)
        if operation == "finishLogin":
            actor = ctx.db.scalar(select(m.User).limit(1))
            auth.set_session(ctx.request, ctx.db, actor, response)
        return response

    monkeypatch.setitem(auth.HANDLERS, operation, synthetic_oidc)
    generator = env.__wrapped__(tmp_path)
    client_env = next(generator)
    try:
        loop_thread = client_env.client.portal.call(threading.get_ident)
        loop_id = client_env.client.portal.call(lambda: id(asyncio.get_running_loop()))
        path = next(path for path, value in client_env.app.openapi()["paths"].items()
                    if value.get("get", {}).get("operationId") == operation)
        url = "/api/v1" + path + "?" + urlencode({"state": "synthetic-state", "code": "synthetic-code"})
        with ThreadPoolExecutor(max_workers=2) as requests:
            pending = requests.submit(client_env.client.get, url, follow_redirects=False)
            try:
                assert entered.wait(3)
                light = requests.submit(client_env.call, "GET", "/libraries").result(timeout=3)
                assert light.status_code == 200 and not pending.done()
            finally:
                release.set()
            result = pending.result(timeout=3)
        assert result.status_code == 302
        assert observed == [(observed[0][0], observed[0][1], False)]
        assert observed[0][0] != loop_thread and observed[0][1] != loop_id
        if operation == "finishLogin":
            assert "kb_session=" in result.headers["set-cookie"]
    finally:
        release.set()
        generator.close()


def test_named_memory_database_supports_distinct_read_and_write_pools(tmp_path):
    from fastapi.testclient import TestClient
    from fund_kb.settings import Settings

    app = api.create_app(Settings(database_url="sqlite:///:memory:", storage_dir=tmp_path,
                                 retrieval_mode="wiki", app_env="development", auth_mode="demo"))
    with TestClient(app):
        assert app.state.engine.pool is not app.state.read_engine.pool
        with app.state.session_factory.begin() as db:
            db.add(m.User(id=svc.uid(), external_subject="test:memory", display_name="合成内存库"))
        with app.state.read_session_factory() as db:
            assert db.scalar(select(m.User.display_name)) == "合成内存库"
