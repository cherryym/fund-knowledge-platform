"""Indexed release presence: temporary SQLite only, no runtime/model/network."""
from __future__ import annotations

import socket
import sqlite3
import subprocess
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import IntegrityError

from fund_kb import admin_review, wiki
from fund_kb import db as database
from fund_kb import models as m
from fund_kb import services as svc
from fund_kb.ingestion import block_text, text_sha256

ACTIVATED = datetime(2026, 1, 10, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Release-presence tests must not use network, processes or models")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(wiki, "_provider_module", forbidden)


@pytest.fixture
def fixture(tmp_path):
    path = tmp_path / "release-presence.sqlite3"
    engine = database.build_engine(f"sqlite:///{path}")
    database.Base.metadata.create_all(engine)
    factory = database.make_session_factory(engine)
    owner, reader, space, rid, vid, bid, other = [svc.uid() for _ in range(7)]
    with factory.begin() as db:
        db.add_all([m.User(id=owner, external_subject="synthetic:owner", display_name="合成作者"),
                    m.User(id=reader, external_subject="synthetic:reader", display_name="合成读者"),
                    m.Space(id=space, name="合成发布空间")])
        db.flush()
        for actor, roles in ((owner, ["reader", "editor", "reviewer", "publisher", "admin"]), (reader, ["reader"])):
            for role in roles:
                db.add(m.SpaceMember(space_id=space, user_id=actor, role=role))
        db.add_all([m.Resource(id=rid, space_id=space, kind="knowledge", name="合成知识", owner_id=owner),
                    m.Resource(id=other, space_id=space, kind="knowledge", name="另一资源", owner_id=owner)])
        db.flush()
        version = m.ResourceVersion(id=vid, resource_id=rid, version_no=1, state="DRAFT", author_id=owner,
            title="合成知识", knowledge_type="faq", origin="HUMAN", legal_status="NOT_APPLICABLE",
            valid_from=date(2020, 1, 1), applicability={}, required_facts=[])
        db.add(version)
        db.flush()
        data = {"text": "按授权来源核对适用日期和价格口径。", "text_format": "markdown"}
        text = block_text({"block_type": "paragraph", "data": data})
        db.add(m.ContentBlock(version_id=vid, block_id=bid, ordinal=0, block_type="paragraph", data=data,
                              locator={}, search_text=text, content_sha256=text_sha256(text)))
        db.flush()
        version.content_sha256 = svc.check_frozen_hash(db, version)
        version.state = "APPROVED"
    yield SimpleNamespace(path=path, engine=engine, db=factory, owner=owner, reader=reader, space=space,
                          rid=rid, vid=vid, bid=bid, other=other)
    engine.dispose()


def release(fixture, *, state="ACTIVE", activated=ACTIVATED, version_id=None):
    value = m.Release(id=svc.uid(), resource_id=fixture.rid, version_id=version_id or fixture.vid,
                      state=state, publisher_id=fixture.owner, activated_at=activated,
                      manifest={"synthetic": True, "large_metadata": "not needed for presence;" * 100})
    with fixture.db.begin() as db:
        db.add(value)
        db.flush()
        if state == "ACTIVE":
            db.get(m.Resource, fixture.rid).active_release_id = value.id
    return value.id


@pytest.mark.parametrize("state", ["PREPARING", "ACTIVE", "SUPERSEDED", "FAILED"])
@pytest.mark.parametrize("activated", [None, ACTIVATED])
def test_presence_matches_legacy_state_and_activation_conditions(fixture, state, activated):
    release(fixture, state=state, activated=activated)
    with fixture.db() as db:
        version = db.get(m.ResourceVersion, fixture.vid)
        old = svc.released(db, version.id)
        actual = svc.is_released(db, version)
        assert type(actual) is bool
        assert actual == bool(old) == (state in {"ACTIVE", "SUPERSEDED"} and activated is not None)
        if old is not None:
            assert isinstance(old, m.Release) and old.manifest["synthetic"] is True


@pytest.mark.parametrize("state", ["ACTIVE", "SUPERSEDED"])
@pytest.mark.parametrize("cutoff,expected", [
    (None, True),
    (ACTIVATED - timedelta(microseconds=1), False),
    (ACTIVATED, True),
    (ACTIVATED + timedelta(microseconds=1), True),
    (datetime(2026, 1, 10, 20, tzinfo=timezone(timedelta(hours=8))), True),
    (datetime(2026, 1, 10, 19, 59, 59, 999999, tzinfo=timezone(timedelta(hours=8))), False),
])
def test_cutoff_is_inclusive_and_preserves_utc_normalization(fixture, state, cutoff, expected):
    release(fixture, state=state)
    with fixture.db() as db:
        version = db.get(m.ResourceVersion, fixture.vid)
        assert svc.is_released(db, version, cutoff) is expected
        assert bool(svc.released(db, version.id, cutoff)) is expected


def test_missing_release_and_other_version_release_do_not_match(fixture):
    with fixture.db.begin() as db:
        db.add(m.ResourceVersion(id=svc.uid(), resource_id=fixture.rid, version_no=2, state="IN_REVIEW",
            author_id=fixture.owner, title="另一个版本", origin="HUMAN", content_sha256="a" * 64))
    with fixture.db() as db:
        version = db.get(m.ResourceVersion, fixture.vid)
        assert svc.is_released(db, version) is False and svc.released(db, version.id) is None
        other_vid = db.scalar(select(m.ResourceVersion.id).where(m.ResourceVersion.version_no == 2))
    release(fixture, version_id=other_vid)
    with fixture.db() as db:
        assert svc.is_released(db, db.get(m.ResourceVersion, fixture.vid)) is False
        assert svc.is_released(db, db.get(m.ResourceVersion, other_vid)) is True


def test_any_qualifying_release_is_found_despite_other_nonqualifying_rows(fixture):
    release(fixture, state="FAILED", activated=ACTIVATED - timedelta(days=2))
    release(fixture, state="PREPARING", activated=None)
    release(fixture, state="SUPERSEDED", activated=ACTIVATED + timedelta(days=1))
    expected = release(fixture, state="SUPERSEDED", activated=ACTIVATED)
    with fixture.db() as db:
        version = db.get(m.ResourceVersion, fixture.vid)
        assert svc.is_released(db, version, ACTIVATED) is True
        assert svc.released(db, version.id, ACTIVATED).id == expected


def test_resource_ownership_constraint_still_rejects_invalid_release(fixture):
    with fixture.db() as db, pytest.raises(IntegrityError):
        db.add(m.Release(id=svc.uid(), resource_id=fixture.other, version_id=fixture.vid,
                         state="ACTIVE", activated_at=ACTIVATED, publisher_id=fixture.owner, manifest={}))
        db.flush()


def test_presence_rejects_cross_resource_corruption_even_if_foreign_key_was_bypassed(fixture, tmp_path):
    assert fixture.path.parent.resolve() == tmp_path.resolve()
    # Deliberate corruption of this disposable file only. The application's
    # engine keeps foreign_keys=ON; neither db.py nor runtime pragmas change.
    with sqlite3.connect(fixture.path) as raw:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("INSERT INTO releases (id,resource_id,version_id,state,manifest,publisher_id,activated_at) "
                    "VALUES (?,?,?,'ACTIVE','{}',?,?)",
                    (svc.uid(), fixture.other, fixture.vid, fixture.owner, ACTIVATED.replace(tzinfo=None).isoformat(' ')))
    with fixture.db() as db:
        assert db.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        version = db.get(m.ResourceVersion, fixture.vid)
        assert svc.released(db, version.id) is not None  # The old version-only lookup cannot detect this.
        assert svc.is_released(db, version) is False


def test_real_presence_sql_selects_only_id_limits_one_and_uses_existing_compound_index(fixture):
    release(fixture)
    queries = []

    def observe(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT") and "FROM releases" in statement:
            queries.append((statement, parameters))

    with fixture.db() as db:
        version = db.get(m.ResourceVersion, fixture.vid)
        event.listen(fixture.engine, "before_cursor_execute", observe)
        try:
            assert svc.is_released(db, version)
        finally:
            event.remove(fixture.engine, "before_cursor_execute", observe)
        assert not any(isinstance(row, m.Release) for row in db.identity_map.values())
    sql, params = queries[0]
    assert len(queries) == 1
    assert sql.split("FROM", 1)[0].strip() == "SELECT releases.id"
    assert "manifest" not in sql and "LIMIT" in sql
    with fixture.engine.connect() as connection:
        details = [row[3] for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params)]
    assert any("SEARCH releases USING INDEX ix_release_version" in detail
               and "resource_id=? AND version_id=?" in detail for detail in details), details
    assert not any("SCAN releases" in detail for detail in details)


def test_presence_is_not_cached_across_flush_bulk_delete_or_rollback(fixture):
    rid = release(fixture)
    with fixture.db() as db:
        version = db.get(m.ResourceVersion, fixture.vid)
        assert svc.is_released(db, version)
        db.get(m.Release, rid).state = "FAILED"
        db.flush()
        assert not svc.is_released(db, version)
        db.rollback()
        assert svc.is_released(db, version)
        db.execute(update(m.Resource).where(m.Resource.id == fixture.rid).values(active_release_id=None))
        db.execute(delete(m.Release).where(m.Release.id == rid))
        assert not svc.is_released(db, version)
        db.rollback()
        assert svc.is_released(db, version)


def test_boolean_access_and_evidence_paths_keep_acl_and_do_not_load_full_release(fixture, monkeypatch):
    release(fixture)

    def legacy_forbidden(*args, **kwargs):
        raise AssertionError("Boolean hot paths must use indexed presence, not full Release loading")

    monkeypatch.setattr(svc, "released", legacy_forbidden)
    with fixture.db() as db:
        reader, version = db.get(m.User, fixture.reader), db.get(m.ResourceVersion, fixture.vid)
        assert svc.version_access(db, reader, version) is version
        assert svc.evidence_version_eligible(db, reader, version)
        db.get(m.Resource, fixture.rid).restricted = True
        db.flush()
        assert svc.is_released(db, version)
        with pytest.raises(svc.APIError) as denied:
            svc.version_access(db, reader, version)
        assert denied.value.code == "NOT_FOUND"
        assert not svc.evidence_version_eligible(db, reader, version)


@pytest.mark.parametrize("fault", ["unapproved", "hash", "future", "removed"])
def test_release_presence_never_bypasses_evidence_conditions(fixture, fault):
    release(fixture)
    with fixture.db() as db:
        reader, version = db.get(m.User, fixture.reader), db.get(m.ResourceVersion, fixture.vid)
        assert svc.evidence_version_eligible(db, reader, version)
        if fault == "unapproved":
            version.state = "IN_REVIEW"
        elif fault == "hash":
            version.content_sha256 = "f" * 64
        elif fault == "future":
            version.valid_from = date(2099, 1, 1)
            db.flush()
            version.content_sha256 = svc.check_frozen_hash(db, version)
        else:
            db.get(m.Resource, fixture.rid).deleted_at = svc.now()
        db.flush()
        assert svc.is_released(db, version)
        assert not svc.evidence_version_eligible(db, reader, version)


def test_wiki_draft_visibility_uses_presence_without_full_release_lookup(fixture, monkeypatch):
    def legacy_forbidden(*args, **kwargs):
        raise AssertionError("Draft Wiki visibility must use indexed presence")

    monkeypatch.setattr(svc, "released", legacy_forbidden)
    with fixture.db() as db:
        version = db.get(m.ResourceVersion, fixture.vid)
        version.state = "DRAFT"
        db.flush()
        assert wiki._visible_version(db, db.get(m.User, fixture.owner), db.get(m.Resource, fixture.rid), {}) is version


def test_admin_confirmed_source_selection_uses_presence_without_full_release_lookup(fixture, monkeypatch):
    release(fixture)
    with fixture.db.begin() as db:
        resource, version = db.get(m.Resource, fixture.rid), db.get(m.ResourceVersion, fixture.vid)
        resource.kind = "document"
        blob = m.Blob(id=svc.uid(), space_id=fixture.space, object_key="synthetic/presence.txt",
                      sha256="b" * 64, size_bytes=1, mime_type="text/plain", scan_state="CLEAN")
        db.add(blob)
        db.flush()
        version.source_blob_id, version.knowledge_type, version.source_verified = blob.id, "source", True
        db.flush()
        version.content_sha256 = svc.check_frozen_hash(db, version)
        db.add(m.RuntimePolicy(id=svc.uid(), name="admin-review:" + version.id, updated_by=fixture.owner,
            config={"mode": admin_review.MODE, "version_id": version.id, "content_sha256": version.content_sha256,
                    "resource_access_epoch": resource.access_epoch, "provenance_sha256": None}))

    def legacy_forbidden(*args, **kwargs):
        raise AssertionError("Confirmed-source presence must not load a full Release")

    monkeypatch.setattr(svc, "released", legacy_forbidden)
    with fixture.db() as db:
        sources, snapshots = wiki.choose_build_sources(db, db.get(m.User, fixture.owner), fixture.space,
                                                       [fixture.rid], source_mode="unverified_draft")
        assert sources and snapshots[0]["version_id"] == fixture.vid
