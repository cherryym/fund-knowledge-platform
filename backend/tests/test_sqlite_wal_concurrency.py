"""DELETE/WAL lock-chain proof on pytest-owned files, never the runtime DB.

Run with -s to emit safe, reproducible observations. No auth files, model calls,
external network, operational logs, or deployment settings are read or changed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from test_wiki import env as env  # noqa: PLC0414
from test_wiki import page
from test_wiki_read_concurrency import authority as authority  # noqa: PLC0414
from test_wiki_read_concurrency import (
    offline_identity_and_network as offline_identity_and_network,  # noqa: PLC0414
)

from fund_kb import api_catalog, api_content, api_tasks, providers
from fund_kb import models as m
from fund_kb import services as svc


@pytest.fixture
def database(env, tmp_path):
    # authority uses this fixture instead of the DELETE-only fixture in its
    # original module. Resolve the engine's actual database, not a guessed path.
    path = Path(env.app.state.engine.url.database)
    assert path.is_file() and not path.is_symlink()
    assert path.resolve().parent == tmp_path.resolve()
    assert env.db.kw["bind"].get_execution_options()["sqlite_transaction_mode"] == "IMMEDIATE"
    return env


def set_temporary_journal(env, tmp_path, mode):
    assert mode in {"DELETE", "WAL"}
    path = Path(env.app.state.engine.url.database)
    assert path.resolve().parent == tmp_path.resolve() and not path.is_symlink()
    with sqlite3.connect(path, isolation_level=None) as connection:
        assert connection.execute("PRAGMA journal_mode=" + mode).fetchone()[0] == mode.lower()
        connection.execute("PRAGMA synchronous=FULL")
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    return path


def safe_exception(exc):
    result = {"type": type(exc).__name__, "code": exc.code}
    if isinstance(exc, OperationalError):
        frames = traceback.extract_tb(exc.__traceback__)
        result.update(sqlite_errorcode=getattr(exc.orig, "sqlite_errorcode", None),
                      sqlite_errorname=getattr(exc.orig, "sqlite_errorname", None),
                      application_frames=[{"file": Path(frame.filename).name, "line": frame.lineno,
                                           "function": frame.name}
                                          for frame in frames if "fund_kb" in Path(frame.filename).parts],
                      frames=[{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                              for frame in frames[-8:]])
    return result


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_old_reader_writer_commit_and_fresh_guard_get_job(authority, tmp_path, journal_mode):
    env, guard, _connection_id, oauth_id = authority
    path = set_temporary_journal(env, tmp_path, journal_mode)
    engine = env.app.state.engine
    read_engine = env.app.state.read_engine
    assert read_engine.pool is not engine.pool
    with read_engine.connect() as connection:
        api_reader_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
    # The HTTP reader has a separate, unchanged timeout. Its blocked read must
    # finish before this synthetic writer times out, so releasing the old reader
    # still allows a real COMMIT instead of observing the writer's rollback.
    writer_timeout = max(5000, api_reader_timeout + 150 + 5000)
    env.app.state.raise_test_errors = False
    errors = []

    def configure(connection, record, proxy):
        cursor = connection.cursor()
        cursor.execute("PRAGMA busy_timeout=150")
        # Do not query/change synchronous while COMMIT is pending: that PRAGMA
        # can itself block and would obscure the actual application's read.
        cursor.close()

    def capture_error(ctx):
        # No SQL text, bound parameters, exception repr or provider payload.
        errors.append({"is_pre_ping": ctx.is_pre_ping,
                       "sqlite_errorcode": getattr(ctx.original_exception, "sqlite_errorcode", None),
                       "sqlite_errorname": getattr(ctx.original_exception, "sqlite_errorname", None)})

    event.listen(engine, "checkout", configure)
    event.listen(engine, "handle_error", capture_error)
    reader = None
    try:
        jid = svc.uid()
        with env.db.begin() as db:
            assert db.connection().exec_driver_sql("PRAGMA synchronous").scalar_one() == 2
            db.add(m.Job(id=jid, kind="COMPILE", owner_id=env.owner, state="RUNNING", stage="before-commit",
                         dedupe_key="synthetic:" + jid, payload={"task": "SYNTHETIC"}))
        guard()
        reader = sqlite3.connect(path, isolation_level=None, timeout=.15)
        reader.execute("PRAGMA query_only=ON")
        reader.execute("PRAGMA synchronous=FULL")
        assert reader.execute("PRAGMA synchronous").fetchone()[0] == 2
        reader.execute("BEGIN DEFERRED")
        assert reader.execute("SELECT stage FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "before-commit"
        entered = threading.Event()

        def writer():
            with env.db() as db:
                connection = db.connection()
                assert connection.get_execution_options()["sqlite_transaction_mode"] == "IMMEDIATE"
                cursor = connection.connection.driver_connection.cursor()
                cursor.execute(f"PRAGMA busy_timeout={writer_timeout}")
                assert cursor.execute("PRAGMA synchronous").fetchone()[0] == 2
                cursor.close()
                db.get(m.Job, jid).stage = "after-commit"
                oauth = db.get(m.RuntimePolicy, oauth_id)
                oauth.config = {**oauth.config, "auth_epoch": 2}
                db.flush()
                started = time.monotonic()
                entered.set()
                db.commit()
                return round((time.monotonic() - started) * 1000, 3)

        result = {"journal_mode": journal_mode, "synchronous": 2,
                  "reader_busy_timeout_ms": 150, "api_reader_busy_timeout_ms": api_reader_timeout,
                  "writer_busy_timeout_ms": writer_timeout,
                  "observed_at_utc": datetime.now(UTC).isoformat()}
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(writer)
            try:
                assert entered.wait(timeout=2)
                if journal_mode == "WAL":
                    # Completion while the old reader is open is the assertion;
                    # a wall-clock speed comparison is not required for PASS.
                    future.result(timeout=2)
                else:
                    # Observe the real PENDING lock, not a fixed scheduling delay.
                    # A RESERVED writer still permits this new read; PENDING does not.
                    probe = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                            isolation_level=None, timeout=0)
                    try:
                        deadline = time.monotonic() + 2
                        while True:
                            try:
                                probe.execute("SELECT stage FROM jobs WHERE id=?", (jid,)).fetchone()
                            except sqlite3.OperationalError as exc:
                                assert exc.sqlite_errorcode == sqlite3.SQLITE_BUSY
                                break
                            assert not future.done(), "Writer ended before PENDING was observed"
                            assert time.monotonic() < deadline, "Writer did not reach PENDING"
                            time.sleep(.005)
                    finally:
                        probe.close()
                    assert not future.done()
                result["writer_committed_with_old_reader_open"] = future.done()
                started = time.monotonic()
                with pytest.raises((OperationalError, svc.APIError, providers.ProviderError)) as caught:
                    guard()
                result["guard_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
                result["guard"] = safe_exception(caught.value)
                if journal_mode == "DELETE":
                    assert isinstance(caught.value, OperationalError)
                    assert caught.value.code == "e3q8"
                    assert caught.value.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY
                else:
                    assert caught.value.code == "CODEX_LOGIN_STALE"
                started = time.monotonic()
                response = env.call("GET", f"/jobs/{jid}")
                result["get_job"] = {"http_status": response.status_code, "code": response.json().get("code"),
                                     "stage": response.json().get("stage"),
                                     "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
                assert response.status_code == (503 if journal_mode == "DELETE" else 200)
                if journal_mode == "DELETE":
                    assert response.json()["code"] == "BACKEND_UNAVAILABLE"
                    assert not future.done(), "Writer timed out before the blocked HTTP reader returned"
                else:
                    assert response.json()["stage"] == "after-commit"
                result["old_reader_retains_snapshot"] = (
                    reader.execute("SELECT stage FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "before-commit")
                assert result["old_reader_retains_snapshot"]
            finally:
                reader.rollback()
                reader.close()
                reader = None
            result["writer_commit_elapsed_ms"] = future.result(timeout=6)
        with pytest.raises(svc.APIError) as revoked:
            guard()
        assert revoked.value.code == "CODEX_LOGIN_STALE"
        result["guard_after_commit"] = revoked.value.code
        response = env.call("GET", f"/jobs/{jid}")
        assert response.status_code == 200 and response.json()["stage"] == "after-commit"
        result["get_job_after_commit"] = {"http_status": response.status_code, "stage": response.json()["stage"]}
        result["error_contexts"] = errors
        result["outcome"] = "PASS"
        print("SQLITE_WAL_OBSERVATION=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        if reader is not None:
            reader.rollback()
            reader.close()
        event.remove(engine, "checkout", configure)
        event.remove(engine, "handle_error", capture_error)


def test_readonly_shared_session_matches_api_but_final_authority_needs_a_fresh_snapshot(database, tmp_path):
    env = database
    source = page(env, "合成原件", kind="document")
    knowledge = page(env, "合成验收页", cites=[source])
    jid = svc.uid()
    with env.db.begin() as db:
        db.add(m.Job(id=jid, kind="COMPILE", owner_id=env.owner, state="SUCCEEDED", dedupe_key="synthetic:" + jid,
                     payload={"task": "WIKI_BUILD", "space_id": env.space,
                              "source_snapshot": [{"version_id": source[1]}]},
                     result={"created_version_ids": [knowledge[1]], "created_resource_ids": [knowledge[0]]}))
    path = set_temporary_journal(env, tmp_path, "WAL")
    identity = env.call("GET", "/me")
    assert identity.status_code == 200
    actor_id = identity.json()["id"]  # Do not copy/log session or CSRF values.
    local_reads = []

    def readonly_connection():
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        connection.execute("PRAGMA query_only=ON")
        return connection

    local_engine = create_engine("sqlite://", creator=readonly_connection)

    @event.listens_for(local_engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN DEFERRED")

    @event.listens_for(local_engine, "before_cursor_execute")
    def observed(connection, cursor, statement, params, context, executemany):
        local_reads.append(statement.upper())

    factory = sessionmaker(bind=local_engine, autoflush=False, expire_on_commit=False)

    def context(db, operation, object_id):
        return svc.Context(SimpleNamespace(path_params={"id": object_id}), db, db.get(m.User, actor_id),
                           {}, {}, operation, [])

    try:
        with factory() as db:
            pairs = [
                (api_content.get_version, "getVersion", knowledge[1], f"/versions/{knowledge[1]}"),
                (api_catalog.get_resource, "getResource", knowledge[0], f"/resources/{knowledge[0]}"),
                (api_tasks.jobs, "getJob", jid, f"/jobs/{jid}"),
            ]
            for handler, operation, oid, url in pairs:
                local = handler(context(db, operation, oid))
                remote = env.call("GET", url)
                assert remote.status_code == local.status == 200
                assert remote.json() == local.body
                if "ETag" in local.headers:
                    assert remote.headers["etag"] == local.headers["ETag"]
            assert not (db.new or db.dirty or db.deleted)
            assert not any("LOGIN_SESSIONS" in sql for sql in local_reads)
            assert all(sql.startswith("SELECT ") or sql == "BEGIN DEFERRED" for sql in local_reads)
            with env.db.begin() as writer:
                writer.get(m.Resource, source[0]).restricted = True
            # A long verification transaction remains a snapshot, even when
            # the same authorization functions are invoked again inside it.
            old_view = api_content.get_version(context(db, "getVersion", knowledge[1]))
            assert old_view.status == 200
            assert env.call("GET", f"/versions/{knowledge[1]}").status_code == 404
        with factory() as fresh, pytest.raises(svc.APIError) as denied:
            api_content.get_version(context(fresh, "getVersion", knowledge[1]))
        assert denied.value.code == "NOT_FOUND"
    finally:
        local_engine.dispose()
