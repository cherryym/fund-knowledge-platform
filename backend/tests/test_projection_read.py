"""SQLite projection safety: temporary databases, synthetic login, zero models.

Every cache assertion warms the *read-only* scope first.  The imported env/page
helpers create only pytest-owned SQLite files; no runtime database is opened.
"""
from __future__ import annotations

import gc
import weakref
from collections import Counter
from contextlib import contextmanager
from contextvars import copy_context
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_wiki import env as env  # noqa: PLC0414
from test_wiki import grant, page
from test_wiki_read_concurrency import offline_identity_and_network as offline_identity_and_network  # noqa: PLC0414
from test_wiki_unverified import draft_source

from fund_kb import api_wiki
from fund_kb import models as m
from fund_kb import projection_read as projection
from fund_kb import services as svc
from fund_kb import wiki
from fund_kb.db import ReadOnlySession, ReadOnlyViolation, build_engine, make_session_factory
from fund_kb.ingestion import block_text, text_sha256


@pytest.fixture
def read_factory(env):
    # Catch a venv/PYTHONPATH accidentally importing the original application.
    assert Path(wiki.__file__).resolve().parents[1] == Path(__file__).resolve().parents[1]
    factory = env.app.state.read_session_factory
    assert factory.kw["autoflush"] is False
    with factory() as db:
        assert isinstance(db, ReadOnlySession)
        assert db.get_bind().pool is env.app.state.read_engine.pool
        assert db.get_bind().pool is not env.app.state.engine.pool
        connection = db.connection()
        assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA read_uncommitted").scalar_one() == 0
        assert connection.connection.driver_connection.in_transaction
    return factory


@contextmanager
def select_counter(engine):
    """Keep aggregate counts, never SQL parameters or row payloads."""
    counter = Counter()

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["selects"] += 1

    event.listen(engine, "before_cursor_execute", count)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", count)


@pytest.fixture
def unverified(env):
    """Freeze a real draft-source fixture without scheduling any generation."""
    source = draft_source(env)
    target = page(env, "快照派生知识", state="DRAFT", cites=[source])
    with env.db.begin() as db:
        _, snapshots = wiki.choose_build_sources(db, db.get(m.User, env.owner), env.space,
            [source[0]], source_mode=wiki.DRAFT_SOURCE_MODE)
        db.add(m.RuntimePolicy(id=svc.uid(), name=f"wiki-provenance:{target[0]}", updated_by=env.owner,
            config={"space_id": env.space, "resource_id": target[0], "created_version_id": target[1],
                    "source_mode": wiki.DRAFT_SOURCE_MODE, "source_snapshot": snapshots,
                    "source_version_ids": [source[1]], "reference_snapshot": []}))
    return source, target


def test_explicit_read_only_engine_uses_real_file_without_writes(env):
    engine = build_engine(env.settings.database_url, read_only=True)
    factory = make_session_factory(engine, read_only=True)
    try:
        assert engine.url.query["mode"] == "ro"
        with factory() as db, projection.projection_read(db):
            assert isinstance(db, ReadOnlySession) and projection.active(db)
            assert db.get(m.User, env.owner).active
            assert not db.new and not db.dirty and not db.deleted
    finally:
        engine.dispose()


def test_scope_warm_hit_and_namespace_separation(read_factory):
    calls = []

    def compute():
        calls.append(1)
        return len(calls)

    with read_factory() as db:
        assert not projection.active(db)
        with projection.projection_read(db):
            assert projection.active(db)
            assert projection.memo(db, "one", ("key",), compute) == 1
            assert projection.memo(db, "one", ("key",), compute) == 1
            assert projection.memo(db, "two", ("key",), compute) == 2
            assert projection.memo(db, "two", ("key",), compute) == 2
        assert not projection.active(db)
        assert projection.memo(db, "one", ("key",), compute) == 3
        assert projection.memo(db, "one", ("key",), compute) == 4


def test_nested_same_transaction_preserves_outer_scope(read_factory):
    with read_factory() as db, projection.projection_read(db):
        scope = projection._CURRENT.get()
        assert projection.memo(db, "nested", 1, lambda: "outer") == "outer"
        with projection.projection_read(db):
            assert projection._CURRENT.get() is scope
            assert projection.memo(db, "nested", 1, lambda: "wrong") == "outer"
        assert scope.live and projection.active(db)


def test_roles_and_explicit_grants_are_actor_keyed_and_return_copies(env, read_factory, monkeypatch):
    record = page(env, restricted=True)
    grant(env, record[0], env.owner)
    calls = []
    original = svc._roles_uncached

    def counted(db, actor, space):
        calls.append(actor.id)
        return original(db, actor, space)

    monkeypatch.setattr(svc, "_roles_uncached", counted)
    with read_factory() as db, projection.projection_read(db):
        owner, reader = db.get(m.User, env.owner), db.get(m.User, env.reader)
        resource = db.get(m.Resource, record[0])
        first = svc.roles(db, owner, env.space)
        first.clear()
        assert "admin" in svc.roles(db, owner, env.space)
        assert svc.roles(db, reader, env.space) == {"reader"}
        assert svc.roles(db, reader, env.space) == {"reader"}
        assert calls == [env.owner, env.reader]
        permissions = svc.grants(db, owner, resource)
        assert permissions == {"read"}
        permissions.add("edit")
        assert svc.grants(db, owner, resource) == {"read"}
        assert svc.grants(db, reader, resource) == set()
        assert svc.resource_access(db, owner, resource).id == resource.id
        for actor, action in ((owner, "edit"), (owner, "download"), (reader, "read")):
            with pytest.raises(svc.APIError) as denied:
                svc.resource_access(db, actor, resource, action)
            assert denied.value.code == "NOT_FOUND"


def test_same_actor_different_session_cannot_inherit_memo(read_factory):
    with read_factory() as first, read_factory() as second:
        with projection.projection_read(first):
            outer = projection._CURRENT.get()
            assert projection.memo(first, "actor", "same-id", lambda: "first") == "first"
            assert not projection.active(second)
            assert projection.memo(second, "actor", "same-id", lambda: "uncached") == "uncached"
            with projection.projection_read(second):
                assert projection.memo(second, "actor", "same-id", lambda: "second") == "second"
                assert projection.memo(second, "actor", "same-id", lambda: "wrong") == "second"
            assert projection._CURRENT.get() is outer
            assert projection.memo(first, "actor", "same-id", lambda: "wrong") == "first"


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_transaction_end_clears_values_and_does_not_reuse_next_transaction(env, read_factory, finish):
    with read_factory() as db, projection.projection_read(db):
        scope = projection._CURRENT.get()
        assert projection.memo(db, "txn", 1, lambda: "old") == "old"
        db.get(m.User, env.owner)
        assert scope.rows and scope.values
        getattr(db, finish)()
        assert not scope.rows and not scope.values and not projection.active(db)
        db.get(m.User, env.owner)  # Start another transaction on the SAME Session.
        assert not projection.active(db)
        assert projection.memo(db, "txn", 1, lambda: "new") == "new"
        assert projection.memo(db, "txn", 1, lambda: "newer") == "newer"
        with projection.projection_read(db):
            assert projection.active(db)
            assert projection.memo(db, "txn", 1, lambda: "fresh-scope") == "fresh-scope"
            assert projection.memo(db, "txn", 1, lambda: "wrong") == "fresh-scope"


def test_savepoint_rollback_invalidates_warmed_values(read_factory):
    with read_factory() as db, projection.projection_read(db):
        assert projection.memo(db, "savepoint", 1, lambda: "before") == "before"
        nested = db.begin_nested()
        nested.rollback()
        assert projection.memo(db, "savepoint", 1, lambda: "after") == "after"


def test_normal_write_session_never_enables_cache(env):
    with env.db() as db:
        assert isinstance(db, Session) and not isinstance(db, ReadOnlySession)
        assert db.connection().exec_driver_sql("PRAGMA query_only").scalar_one() == 0
        with projection.projection_read(db):
            assert not projection.active(db)
            assert projection.memo(db, "write", 1, lambda: 1) == 1
            assert projection.memo(db, "write", 1, lambda: 2) == 2


@pytest.mark.parametrize("dialect", ["oracle", "mysql"])
def test_other_dialects_do_not_connect_or_memoize(dialect):
    def forbidden():
        pytest.fail("Non-SQLite projection must not acquire a connection")

    db = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name=dialect)),
                         connection=forbidden, new=set(), dirty=set(), deleted=set())
    with projection.projection_read(db):
        assert not projection.active(db)
        assert projection.memo(db, "dialect", 1, lambda: 1) == 1
        assert projection.memo(db, "dialect", 1, lambda: 2) == 2


@pytest.mark.parametrize("operation", ["bulk_update", "bulk_delete", "raw_update", "disable_query_only", "orm_flush"])
def test_read_only_database_rejects_all_write_paths(env, read_factory, operation):
    with read_factory() as db, projection.projection_read(db):
        user = db.get(m.User, env.owner)
        assert projection.memo(db, "write-denial", 1, lambda: user.active) is True
        if operation == "bulk_update":
            with pytest.raises(ReadOnlyViolation):
                db.execute(update(m.User).where(m.User.id == env.owner).values(active=False))
        elif operation == "bulk_delete":
            with pytest.raises(ReadOnlyViolation):
                db.execute(delete(m.User).where(m.User.id == env.owner))
        elif operation == "orm_flush":
            user.active = False
            assert not projection.active(db)
            assert projection.memo(db, "write-denial", 1, lambda: user.active) is False
            with pytest.raises(ReadOnlyViolation):
                db.flush()
        else:
            with pytest.raises(DBAPIError):
                connection = db.connection()
                if operation == "raw_update":
                    connection.exec_driver_sql(f"UPDATE {m.User.__tablename__} SET active=0 WHERE id=?", (env.owner,))
                else:
                    connection.exec_driver_sql("PRAGMA query_only=OFF")
            assert db.connection().exec_driver_sql("PRAGMA query_only").scalar_one() == 1
        db.rollback()
    with read_factory() as check:
        assert check.scalar(select(m.User.active).where(m.User.id == env.owner)) is True


@pytest.mark.parametrize("exception", [False, True])
def test_copied_context_is_unusable_after_normal_or_exceptional_exit(read_factory, exception):
    with read_factory() as db:
        try:
            with projection.projection_read(db):
                scope = projection._CURRENT.get()
                assert projection.memo(db, "copied", 1, lambda: "cached") == "cached"
                inherited = copy_context()
                if exception:
                    raise ValueError("synthetic scope failure")
        except ValueError:
            assert exception
        assert not scope.live and not scope.values and not scope.rows
        assert inherited.run(projection.active, db) is False
        assert inherited.run(projection.memo, db, "copied", 1, lambda: "fresh") == "fresh"
        assert inherited.run(projection.memo, db, "copied", 1, lambda: "fresh-again") == "fresh-again"
        assert not scope.values


@pytest.mark.parametrize("budget", [0, 2])
def test_memo_budget_exhaustion_recomputes_every_result_without_omission(read_factory, monkeypatch, budget):
    monkeypatch.setattr(projection, "_MAX_ENTRIES", budget)
    calls = Counter()

    def compute(key):
        calls[key] += 1
        return {"id": key, "value": key * key}

    with read_factory() as db, projection.projection_read(db):
        for _ in range(2):
            result = [projection.memo(db, "budget", key, lambda key=key: compute(key)) for key in range(17)]
            assert result == [{"id": key, "value": key * key} for key in range(17)]
        assert len(projection._CURRENT.get().values) == budget
    assert calls == Counter({key: 1 if key < budget else 2 for key in range(17)})


def test_exceptions_are_not_cached_as_success(read_factory):
    calls = []

    def denied():
        calls.append(1)
        svc.fail(404, "NOT_FOUND", "合成拒绝")

    with read_factory() as db, projection.projection_read(db):
        for _ in range(2):
            with pytest.raises(svc.APIError):
                projection.memo(db, "failure", 1, denied)
        assert len(calls) == 2 and not projection._CURRENT.get().values


def test_loaded_rows_are_strong_only_inside_scope_and_do_not_issue_extra_reads(env, read_factory):
    record = page(env)
    with read_factory() as db:
        with select_counter(env.app.state.read_engine) as count:
            with projection.projection_read(db):
                scope = projection._CURRENT.get()
                assert not scope.rows
                row = db.get(m.Resource, record[0])
                reference = weakref.ref(row)
                identity = id(row)
                del row
                gc.collect()
                assert reference() is not None
                assert id(db.get(m.Resource, record[0])) == identity
                assert count["selects"] == 1
                assert not db.new and not db.dirty and not db.deleted
            gc.collect()
            assert not scope.rows and reference() is None
            assert db.get(m.Resource, record[0]).id == record[0]
            assert count["selects"] == 2


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_loaded_rows_are_released_on_transaction_end(env, read_factory, finish):
    with read_factory() as db, projection.projection_read(db):
        row = db.get(m.User, env.owner)
        reference = weakref.ref(row)
        del row
        assert reference() is not None
        getattr(db, finish)()
        gc.collect()
        assert not projection._CURRENT.get().rows
        assert reference() is None


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_ended_scope_does_not_retain_rows_loaded_in_later_transaction(env, read_factory, finish):
    with read_factory() as db, projection.projection_read(db):
        scope = projection._CURRENT.get()
        db.get(m.User, env.owner)
        assert scope.rows
        getattr(db, finish)()
        assert not scope.rows
        row = db.get(m.User, env.reader)
        reference = weakref.ref(row)
        del row
        gc.collect()
        assert not projection.active(db)
        assert not scope.rows, "The old snapshot must not retain ORM rows from a new transaction"
        assert reference() is None


@pytest.mark.parametrize("budget", [0, 1, 3])
def test_workspace_and_graph_match_uncached_full_results_when_budget_is_exhausted(env, read_factory, monkeypatch, budget):
    source = page(env, "预算测试原文", kind="document")
    first = page(env, "预算测试甲", cites=[source])
    second = page(env, "预算测试乙", cites=[source])
    hidden = page(env, "预算测试隐藏节点", restricted=True)
    context = {"business_date": "2026-09-08"}
    with read_factory() as db:
        owner = db.get(m.User, env.owner)
        expected_workspace = wiki.workspace(db, owner, env.space, context=context)
        expected_graph = wiki.graph(db, owner, env.space, context=context)
    assert {p["id"] for p in expected_workspace["pages"]} == {first[0], second[0]}
    assert {n["id"] for n in expected_graph["nodes"]} == {source[0], first[0], second[0]}
    assert hidden[0] not in {n["id"] for n in expected_graph["nodes"]}
    assert {(e["source"], e["target"]) for e in expected_graph["edges"]} == {
        (first[0], source[0]), (second[0], source[0])}
    monkeypatch.setattr(projection, "_MAX_ENTRIES", budget)
    with read_factory() as db, projection.projection_read(db):
        owner = db.get(m.User, env.owner)
        for _ in range(2):
            assert wiki.workspace(db, owner, env.space, context=context) == expected_workspace
            assert wiki.graph(db, owner, env.space, context=context) == expected_graph
        assert len(projection._CURRENT.get().values) <= budget
        assert not db.new and not db.dirty and not db.deleted


def test_release_presence_keys_include_resource_and_cutoff(env, read_factory, monkeypatch):
    record = page(env)
    calls = []
    original = svc._is_released_uncached

    def counted(db, version, cutoff=None):
        calls.append((version.resource_id, version.id, cutoff))
        return original(db, version, cutoff)

    monkeypatch.setattr(svc, "_is_released_uncached", counted)
    with read_factory() as db, projection.projection_read(db):
        version = db.get(m.ResourceVersion, record[1])
        activation = svc.aware(db.scalar(select(m.Release.activated_at).where(m.Release.version_id == version.id)))
        early = activation - timedelta(microseconds=1)
        for _ in range(2):
            assert svc.is_released(db, version)
            assert svc.is_released(db, version, activation)
            assert not svc.is_released(db, version, early)
            corrupt = SimpleNamespace(resource_id=svc.uid(), id=version.id)
            assert not svc.is_released(db, corrupt)
        assert Counter(key[:2] for key in calls)[(version.resource_id, version.id)] == 3


@pytest.mark.parametrize("change", ["acl", "delete", "epoch", "body", "blob"])
def test_warm_source_checks_do_not_hide_revocation_on_next_read_request(env, read_factory, unverified, change):
    source, target = unverified
    with read_factory() as db, projection.projection_read(db):
        owner = db.get(m.User, env.owner)
        for _ in range(2):
            assert svc.version_access(db, owner, target[1]).id == target[1]
            assert {row["id"] for row in wiki.workspace(db, owner, env.space)["pages"]} == {target[0]}
        assert any(key[0] == "draft-lineage" for key in projection._CURRENT.get().values)
    before = env.call("GET", f"/wiki/graph?space_id={env.space}")
    assert before.status_code == 200 and target[0] in {node["id"] for node in before.json()["nodes"]}
    with env.db.begin() as db:
        resource, version = db.get(m.Resource, source[0]), db.get(m.ResourceVersion, source[1])
        if change == "acl":
            resource.restricted = True
        elif change == "delete":
            resource.deleted_at = svc.now()
        elif change == "epoch":
            resource.access_epoch += 1
        elif change == "blob":
            db.get(m.Blob, version.source_blob_id).scan_state = "QUARANTINED"
        else:
            block = db.get(m.ContentBlock, (source[1], source[2]))
            block.data = {"text": "合成来源已经改写，旧快照应当失效。", "text_format": "markdown"}
            block.search_text = block_text({"block_type": block.block_type, "data": block.data})
            block.content_sha256 = text_sha256(block.search_text)
            version.revision += 1
    with read_factory() as db, projection.projection_read(db):
        with pytest.raises(svc.APIError):
            svc.version_access(db, db.get(m.User, env.owner), target[1])
    for path, field in (("workspace", "pages"), ("graph", "nodes")):
        response = env.call("GET", f"/wiki/{path}?space_id={env.space}")
        assert response.status_code == 200
        assert target[0] not in {row["id"] for row in response.json()[field]}


def test_next_scope_same_session_rechecks_committed_grant_revocation(env, read_factory):
    record = page(env, restricted=True)
    grant(env, record[0], env.owner)
    with read_factory() as db:
        with projection.projection_read(db):
            owner = db.get(m.User, env.owner)
            for _ in range(2):
                assert svc.resource_access(db, owner, record[0]).id == record[0]
        db.commit()  # End DELETE-journal reader before committing the test writer.
        with env.db.begin() as writer:
            writer.execute(delete(m.ResourceGrant).where(m.ResourceGrant.resource_id == record[0]))
        with projection.projection_read(db):
            with pytest.raises(svc.APIError) as denied:
                svc.resource_access(db, db.get(m.User, env.owner), record[0])
            assert denied.value.code == "NOT_FOUND"


def test_lineage_key_includes_full_path_not_only_depth(env, read_factory, unverified, monkeypatch):
    source, target = unverified
    paths = []
    original = wiki._check_draft_lineage_uncached

    def counted(db, actor, resource):
        paths.append(wiki._PROVENANCE_PATH.get())
        return original(db, actor, resource)

    monkeypatch.setattr(wiki, "_check_draft_lineage_uncached", counted)
    ancestors = [((env.owner, svc.uid()),), ((env.owner, svc.uid()),)]
    with read_factory() as db, projection.projection_read(db):
        owner, resource = db.get(m.User, env.owner), db.get(m.Resource, target[0])
        for ancestor in ancestors:
            token = wiki._PROVENANCE_PATH.set(ancestor)
            try:
                result = wiki._check_draft_lineage(db, owner, resource)
                assert result == [source[1]]
                result.clear()
                assert wiki._check_draft_lineage(db, owner, resource) == [source[1]]
            finally:
                wiki._PROVENANCE_PATH.reset(token)
        assert paths == ancestors


@pytest.mark.parametrize("bad_path", ["ancestor-cycle", "eight-ancestors"])
def test_warm_visible_version_cannot_bypass_provenance_cycle_or_depth(env, read_factory, unverified, bad_path):
    _, target = unverified
    context = {"business_date": "2026-09-08"}
    with read_factory() as db, projection.projection_read(db):
        owner, resource = db.get(m.User, env.owner), db.get(m.Resource, target[0])
        assert wiki._visible_version(db, owner, resource, context).id == target[1]
        legal = tuple((env.owner, svc.uid()) for _ in range(7))
        token = wiki._PROVENANCE_PATH.set(legal)
        try:
            assert wiki._visible_version(db, owner, resource, context).id == target[1]
        finally:
            wiki._PROVENANCE_PATH.reset(token)
        bad = ((*legal[:-1], (env.owner, target[0])) if bad_path == "ancestor-cycle"
               else (*legal, (env.owner, svc.uid())))
        token = wiki._PROVENANCE_PATH.set(bad)
        try:
            with pytest.raises(svc.APIError) as denied:
                svc.resource_access(db, owner, resource)
            assert denied.value.code == "DEPENDENCY_CYCLE"
            assert wiki._visible_version(db, owner, resource, context) is None
        finally:
            wiki._PROVENANCE_PATH.reset(token)
        assert wiki._visible_version(db, owner, resource, context).id == target[1]


@pytest.mark.parametrize("cycle", [False, True])
def test_warmed_dependency_structure_retains_cycle_and_eight_layer_checks(env, read_factory, cycle):
    leaf = page(env, "依赖末端", kind="document")
    root = page(env, "依赖起点", kind="document", cites=[leaf])
    if cycle:
        with env.db.begin() as db:
            db.add(m.EvidenceLink(id=svc.uid(), from_version_id=leaf[1], from_block_id=leaf[2],
                to_version_id=root[1], to_block_id=root[2], purpose="FACT"))
    with read_factory() as db, projection.projection_read(db):
        owner, version = db.get(m.User, env.owner), db.get(m.ResourceVersion, root[1])
        for _ in range(2):
            assert svc.dependency_ids(db, version) == {leaf[1]}
        if cycle:
            for _ in range(2):
                with pytest.raises(svc.APIError) as denied:
                    svc.check_dependency_access(db, owner, version)
                assert denied.value.code == "DEPENDENCY_CYCLE"
        else:
            svc.check_dependency_access(db, owner, version)
            svc.check_dependency_access(db, owner, version, tuple(svc.uid() for _ in range(6)))
            with pytest.raises(svc.APIError) as denied:
                svc.check_dependency_access(db, owner, version, tuple(svc.uid() for _ in range(7)))
            assert denied.value.code == "DEPENDENCY_CYCLE"


def test_visible_version_status_and_business_date_do_not_share_results(env, read_factory):
    record = page(env, valid_from=date(2026, 1, 1), valid_to=date(2027, 1, 1))
    with read_factory() as db, projection.projection_read(db):
        owner, resource = db.get(m.User, env.owner), db.get(m.Resource, record[0])
        for _ in range(2):
            assert wiki._visible_version(db, owner, resource, {"business_date": "2026-09-08"}).id == record[1]
            assert wiki._visible_version(db, owner, resource, {"business_date": "2027-01-01"}) is None
            assert wiki._visible_version(db, owner, resource, {"business_date": "2026-09-08"}, "DRAFT") is None


def test_request_context_freezes_business_date(monkeypatch):
    monkeypatch.setattr(svc, "effective_date", lambda: date(2026, 9, 8))
    context = api_wiki._context(SimpleNamespace(query={}))
    monkeypatch.setattr(svc, "effective_date", lambda: date(2026, 9, 9))
    assert context == {"business_date": "2026-09-08"}
    assert api_wiki._context(SimpleNamespace(query={})) == {"business_date": "2026-09-09"}
    assert api_wiki._context(SimpleNamespace(query={"business_date": "2025-01-01"})) == {"business_date": "2025-01-01"}
