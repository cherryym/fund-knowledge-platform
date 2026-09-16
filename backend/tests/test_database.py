"""Database tests require no models, cloud services, Docker or external credentials.

Real servers are opt-in with FKB_TEST_{ORACLE,OCEANBASE}_URL and an explicit
FKB_TEST_ALLOW_DDL=1. They must be EMPTY, dedicated test schemas/databases.
"""
from __future__ import annotations

import copy
import io
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    create_mock_engine,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects import mysql, oracle, sqlite
from sqlalchemy.exc import IntegrityError, OperationalError, StatementError
from sqlalchemy.orm.exc import StaleDataError

from fund_kb import db
from fund_kb import models as m
from fund_kb.settings import APPLICATION_ROOT, Settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]
HASH = "a" * 64
DIALECTS = [sqlite.dialect(), mysql.dialect(), oracle.dialect()]


def uid():
    return str(uuid4())


@pytest.fixture
def engine(tmp_path):
    result = db.build_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    db.Base.metadata.create_all(result)
    yield result
    result.dispose()


def seed_graph(session):
    user = m.User(id=uid(), external_subject=uid(), display_name="合成测试用户")
    space = m.Space(id=uid(), name="合成测试空间")
    session.add_all([user, space])
    session.flush()
    resource = m.Resource(id=uid(), space_id=space.id, kind="document", name="证据", owner_id=user.id)
    session.add(resource)
    session.flush()
    version = m.ResourceVersion(id=uid(), resource_id=resource.id, version_no=1, author_id=user.id,
                                title="测试草稿", origin="HUMAN")
    session.add(version)
    session.flush()
    return user, space, resource, version


@pytest.fixture
def graph(engine):
    factory = db.make_session_factory(engine)
    with factory() as session:
        user, space, resource, version = seed_graph(session)
        session.commit()
        return {"user": user.id, "space": space.id, "resource": resource.id, "version": version.id}


def configuration(url):
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    return config


def test_all_design_tables_and_columns_are_present():
    path = APPLICATION_ROOT / "contracts" / "schema.sql"
    if not path.is_file():
        path = APPLICATION_ROOT.parent / "共同成长" / "projects" / "PRJ-FUNDKB-001-开发设计" / "schema.sql"
    source = path.read_text(encoding="utf-8")
    tables = dict(re.findall(r"CREATE TABLE (\w+) \((.*?)^\);", source, re.DOTALL | re.MULTILINE))
    assert len(tables) == 24
    assert set(db.Base.metadata.tables) == set(tables) | {"login_sessions"}
    for name, definition in tables.items():
        expected = set(re.findall(r"^    (\w+) (?:uuid|text|jsonb|boolean|timestamptz|date|bigint|integer)\b",
                                  definition, re.MULTILINE))
        assert expected <= set(db.Base.metadata.tables[name].columns.keys()), name
    assert {"id", "user_id", "token_hash", "csrf_token", "expires_at", "revoked_at", "created_at", "last_seen_at"} == set(
        m.LoginSession.__table__.columns.keys())


@pytest.mark.parametrize("dialect", DIALECTS, ids=lambda item: item.name)
def test_all_tables_indexes_compile_without_postgres_constructs(dialect):
    statements = []
    mock = create_mock_engine(f"{dialect.name}://", lambda statement, *a, **k: statements.append(
        str(statement.compile(dialect=dialect))))
    # AddConstraint during mock ALTER compilation modifies the constraint's
    # inline create rule. Keep those compiler side effects off runtime metadata.
    copied = MetaData()
    for table in db.Base.metadata.tables.values():
        table.to_metadata(copied)
    copied.create_all(mock)
    assert sum(sql.startswith("\nCREATE TABLE") for sql in statements) == 25
    compiled = "\n".join(statements).upper()
    for forbidden in ("JSONB", "TIMESTAMPTZ", "TEXT[]", "USING GIN", "DEFERRABLE", "SEARCH_PATH"):
        assert forbidden not in compiled
    for statement in statements:
        if "CREATE INDEX" in statement or "CREATE UNIQUE INDEX" in statement:
            assert " WHERE " not in statement.upper()
    assert "ONE_AUTHOR_DRAFT" in compiled and "ONE_ACTIVE_RELEASE" in compiled and "ONE_OPEN_UPLOAD" in compiled
    if dialect.name == "oracle":
        assert "IS JSON STRICT WITH UNIQUE KEYS" in compiled
        assert "CLOB" in compiled and "VARCHAR2" in compiled
        assert "GENERATED ALWAYS AS" in compiled
        assert "DEFERRABLE" not in compiled
    elif dialect.name == "mysql":
        assert "LONGTEXT" in compiled and "DATETIME(6)" in compiled
        assert "JSON_TYPE" in compiled


@pytest.mark.parametrize("dialect", DIALECTS, ids=lambda item: item.name)
def test_portable_type_bind_result_roundtrips(dialect):
    value = {"中文": ["引用", {"深层": True}], "empty": ""}
    datatype = db.JSONDocument("object")
    assert datatype.process_result_value(datatype.process_bind_param(value, dialect), dialect) == value
    original = datetime(2026, 9, 7, 10, 1, 2, 123456, tzinfo=timezone(timedelta(hours=8)))
    dt = db.UTCDateTime()
    stored = dt.process_bind_param(original, dialect)
    assert stored == datetime(2026, 9, 7, 2, 1, 2, 123456, tzinfo=None)  # noqa: DTZ001 - disk format
    assert dt.process_result_value(stored, dialect) == original.astimezone(UTC)
    assert str(db.UUIDString().compile(dialect=dialect)) == "CHAR(36)"
    assert db.UUIDString().process_bind_param(uid().upper(), dialect).islower()
    with pytest.raises(ValueError, match="timezone-aware"):
        dt.process_bind_param(datetime(2026, 9, 7, tzinfo=None), dialect)  # noqa: DTZ001 - rejection test
    with pytest.raises(ValueError):
        db.UUIDString().process_bind_param("not-a-uuid", dialect)
    for invalid in ([1], "{\"bad\": true}", {"nan": float("nan")}):
        with pytest.raises(ValueError):
            datatype.process_bind_param(invalid, dialect)
    exact = db.ExactKey(128)
    for token in ("Case", "case", "case ", "中文标识"):
        assert exact.process_result_value(exact.process_bind_param(token, dialect), dialect) == token
    assert exact.process_bind_param("case ", dialect) != exact.process_bind_param("case", dialect)


def test_sqlite_foreign_keys_enabled_every_connection(engine):
    for _ in range(2):
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        engine.dispose()
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(m.SpaceMember.__table__.insert().values(space_id=uid(), user_id=uid(), role="reader"))


def test_one_draft_per_author_and_unlimited_non_drafts(engine, graph):
    factory = db.make_session_factory(engine)
    with factory() as session:
        session.add(m.ResourceVersion(resource_id=graph["resource"], version_no=2, author_id=graph["user"],
                                      title="重复草稿", origin="HUMAN"))
        with pytest.raises(IntegrityError):
            session.commit()
    with factory() as session:
        existing = session.get(m.ResourceVersion, graph["version"])
        existing.state, existing.content_sha256 = "REJECTED", HASH
        session.flush()
        for number in (2, 3):
            session.add(m.ResourceVersion(resource_id=graph["resource"], version_no=number, author_id=graph["user"],
                                          title="历史版本", origin="HUMAN", state="REJECTED", content_sha256=HASH))
        session.add(m.ResourceVersion(resource_id=graph["resource"], version_no=4, author_id=graph["user"],
                                      title="新的草稿", origin="HUMAN"))
        session.commit()
        history = session.scalars(select(m.ResourceVersion).where(m.ResourceVersion.state == "REJECTED")).all()
        assert len(history) == 3
        assert all(row.draft_resource_id is None and row.draft_author_id is None for row in history)


def test_one_open_upload_and_reopen_after_close(engine, graph):
    factory = db.make_session_factory(engine)
    params = {"version_id": graph["version"], "user_id": graph["user"], "filename": "test.txt", "declared_size": 3,
                  "part_count": 1, "expires_at": db.utcnow() + timedelta(days=1)}
    with factory() as session:
        upload = m.Upload(**params, state="OPEN")
        session.add(upload)
        session.commit()
        first = upload.id
    with factory() as session:
        session.add(m.Upload(**params, state="OPEN"))
        with pytest.raises(IntegrityError):
            session.commit()
    with factory() as session:
        session.get(m.Upload, first).state = "SEALED"
        session.flush()
        session.add_all([m.Upload(**params, state="EXPIRED"), m.Upload(**params, state="OPEN")])
        session.commit()


def test_release_unique_pointer_and_immediate_ordering(engine, graph):
    factory = db.make_session_factory(engine)
    params = {"resource_id": graph["resource"], "version_id": graph["version"], "publisher_id": graph["user"]}
    with factory() as session:
        first = m.Release(**params, state="ACTIVE")
        next_release = m.Release(**params, state="PREPARING")
        session.add_all([first, next_release])
        session.flush()
        resource = session.get(m.Resource, graph["resource"])
        resource.active_release_id = first.id
        session.commit()
        first_id, next_id = first.id, next_release.id
    with factory() as session:
        session.get(m.Release, next_id).state = "ACTIVE"
        with pytest.raises(IntegrityError):
            session.commit()
    with factory.begin() as session:
        resource = session.get(m.Resource, graph["resource"])
        session.get(m.Release, first_id).state = "SUPERSEDED"
        session.flush()  # Must release the unique active key before activating the new row.
        session.get(m.Release, next_id).state = "ACTIVE"
        session.flush()
        resource.active_release_id = next_id
    with factory() as session:
        assert session.get(m.Resource, graph["resource"]).active_release_id == next_id


@pytest.mark.parametrize("field", ["active_release_id", "base_version_id"])
def test_cross_resource_ownership_cannot_be_forged(engine, graph, field):
    factory = db.make_session_factory(engine)
    with factory() as session:
        user, _space, resource, version = seed_graph(session)
        foreign_version_id = version.id
        release = m.Release(resource_id=resource.id, version_id=version.id, publisher_id=user.id, state="ACTIVE")
        session.add(release)
        session.commit()
        foreign_release_id = release.id
    with factory() as session:
        if field == "active_release_id":
            session.get(m.Resource, graph["resource"]).active_release_id = foreign_release_id
        else:
            session.get(m.ResourceVersion, graph["version"]).base_version_id = foreign_version_id
        with pytest.raises(IntegrityError):
            session.commit()


def test_evidence_pair_and_block_ownership(engine, graph):
    factory = db.make_session_factory(engine)
    with factory() as session:
        session.add(m.RelationEdge(source_version_id=graph["version"], target_resource_id=graph["resource"],
                                   relation_type="CITES", evidence_version_id=graph["version"]))
        with pytest.raises(IntegrityError):
            session.commit()
    with factory() as session:
        session.add(m.EvidenceLink(from_version_id=graph["version"], from_block_id=uid(),
                                  to_version_id=graph["version"], to_block_id=uid(), purpose="RULE"))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize("updates", [
    {"state": "UNKNOWN"}, {"state": "IN_REVIEW"}, {"version_no": 0},
    {"content_sha256": "g" * 64}, {"valid_from": date(2026, 9, 8), "valid_to": date(2026, 9, 7)},
])
def test_version_checks_reject_invalid_state_hash_and_dates(engine, graph, updates):
    with db.make_session_factory(engine)() as session:
        row = session.get(m.ResourceVersion, graph["version"])
        for key, value in updates.items():
            setattr(row, key, value)
        with pytest.raises(IntegrityError):
            session.commit()


def test_boolean_domain_is_constrained_even_for_raw_sql(engine, graph):
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(text("UPDATE resources SET restricted = 2 WHERE id = :id"), {"id": graph["resource"]})


def test_json_assignment_and_empty_text_roundtrip(engine, graph):
    factory = db.make_session_factory(engine)
    with factory() as session:
        version = session.get(m.ResourceVersion, graph["version"])
        version.applicability = {"conditions": {"市场": "境内"}}
        version.change_reason = ""
        version.valid_from = date(2026, 9, 7)
        session.commit()
    with factory() as session:
        version = session.get(m.ResourceVersion, graph["version"])
        replacement = copy.deepcopy(version.applicability)
        replacement["conditions"]["市场"] = "沪深"
        version.applicability = replacement
        session.commit()
    with factory() as session:
        version = session.get(m.ResourceVersion, graph["version"])
        assert version.applicability == {"conditions": {"市场": "沪深"}}
        assert version.change_reason == ""
        assert version.created_at.tzinfo is UTC
        assert version.valid_from == date(2026, 9, 7)


@pytest.mark.parametrize("document", ["not json", "[]", '{"value":NaN}'])
def test_raw_json_invalid_or_wrong_root_is_rejected(engine, graph, document):
    with engine.begin() as connection, pytest.raises((IntegrityError, OperationalError)):
        connection.execute(text("UPDATE resource_versions SET applicability=:value WHERE id=:id"),
                           {"value": document, "id": graph["version"]})


def test_json_and_timestamps_reject_invalid_python_input(engine, graph):
    factory = db.make_session_factory(engine)
    for attribute, value in (("applicability", []), ("created_at", datetime(2026, 9, 7, tzinfo=None))):  # noqa: DTZ001
        with factory() as session:
            setattr(session.get(m.ResourceVersion, graph["version"]), attribute, value)
            with pytest.raises(StatementError):
                session.commit()


def test_revision_rejects_stale_detached_update(engine, graph):
    factory = db.make_session_factory(engine)
    with factory() as first:
        stale = first.get(m.Resource, graph["resource"])
        first.expunge(stale)
        first.rollback()
    with factory.begin() as second:
        second.get(m.Resource, graph["resource"]).name = "新版本"
    with factory() as first:
        first.add(stale)
        stale.name = "过期写入"
        with pytest.raises(StaleDataError):
            first.commit()
    with factory() as session:
        current = session.get(m.Resource, graph["resource"])
        assert current.name == "新版本" and current.revision == 2


def test_business_job_outbox_audit_are_atomic(engine, graph):
    factory = db.make_session_factory(engine)
    with pytest.raises(RuntimeError), factory.begin() as session:
        session.get(m.Resource, graph["resource"]).deleted_at = db.utcnow()
        session.add(m.Job(id=uid(), kind="INVALIDATE", owner_id=graph["user"], state="QUEUED", dedupe_key=uid()))
        session.add(m.Outbox(id=uid(), event_type="INVALIDATE", aggregate_id=graph["resource"], payload={}))
        session.add(m.AuditEvent(id=uid(), actor_id=graph["user"], action="delete", object_type="resource",
                                 object_id=graph["resource"], trace_id=uid(), outcome="SUCCESS"))
        session.flush()
        raise RuntimeError("synthetic crash before commit")
    with factory() as session:
        assert session.get(m.Resource, graph["resource"]).deleted_at is None
        assert session.scalars(select(m.Job)).all() == []
        assert session.scalars(select(m.Outbox)).all() == []
        assert session.scalars(select(m.AuditEvent)).all() == []


def test_idempotency_scope_and_request_binding_are_preserved(engine, graph):
    factory = db.make_session_factory(engine)
    args = {"actor_id": graph["user"], "http_method": "POST", "key": "test-key", "state": "STARTED",
                "request_sha256": HASH, "expires_at": db.utcnow() + timedelta(hours=24)}
    with factory.begin() as session:
        session.add_all([m.IdempotencyRecord(**args, route="/a"), m.IdempotencyRecord(**args, route="/b")])
    with factory() as session:
        session.add(m.IdempotencyRecord(**{**args, "request_sha256": "b" * 64}, route="/a"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_sessions_token_hash_unique_and_expiry_roundtrip(engine, graph):
    factory = db.make_session_factory(engine)
    with factory() as session:
        login = m.LoginSession(user_id=graph["user"], token_hash=HASH, expires_at=db.utcnow() + timedelta(hours=8))
        session.add(login)
        session.commit()
        session.refresh(login)
        assert login.csrf_token and login.expires_at.tzinfo is UTC and login.revoked_at is None
    with factory() as session:
        session.add(m.LoginSession(user_id=graph["user"], token_hash=HASH, expires_at=db.utcnow()))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize("url,expected", [
    ("sqlite:///:memory:", "sqlite"), ("mysql://", "mysql"), ("oceanbase://", "mysql"),
    ("oceanbase+mysql://", "mysql"), ("oceanbase+pymysql://", "mysql"), ("oracle://", "oracle"),
])
def test_connection_selection_is_explicit_and_lazy(url, expected):
    engine = db.build_engine(url)
    try:
        caps = db.database_capabilities(engine)
        assert caps["dialect"] == expected
        assert caps["real_database_verified"] is False
        assert caps["verification_status"] == "NOT_EVALUATED"
    finally:
        engine.dispose()


def test_no_silent_fallback_on_bad_url_or_connection_error(tmp_path, monkeypatch):
    for url in ("postgresql://", "oceanbase+oracle://", "oracle+cx_oracle://", "mysql+pymysql://?charset=latin1"):
        with pytest.raises(ValueError):
            db.build_engine(url)
    engine = db.build_engine(f"sqlite:///{tmp_path / 'does-not-exist' / 'db.sqlite3'}")
    with pytest.raises(OperationalError):
        engine.connect()
    assert not (tmp_path / "does-not-exist").exists()
    engine.dispose()
    called = []
    def reject(*args, **kwargs):
        called.append(args)
        raise RuntimeError("driver load failure")
    monkeypatch.setattr(db, "create_engine", reject)
    with pytest.raises(RuntimeError, match="driver load failure"):
        db.build_engine("oracle://")
    assert len(called) == 1


@pytest.mark.parametrize("url", ["sqlite:///:memory:", "oracle+oracledb://", "mysql+pymysql://"])
def test_alembic_offline_upgrade_compiles_all_tables_and_cycle(url):
    config = configuration(url)
    output = io.StringIO()
    config.output_buffer = output
    command.upgrade(config, "head", sql=True)
    ddl = output.getvalue()
    assert len(re.findall(r"CREATE TABLE", ddl, re.IGNORECASE)) == 26
    assert ddl.count("CONSTRAINT fk_resource_release_owner") == 1
    assert "0001_initial" in ddl
    for forbidden in ("jsonb", "timestamptz", "USING gin", "DEFERRABLE"):
        assert forbidden not in ddl


def test_alembic_upgrade_matches_models_and_reruns_safely(tmp_path):
    url = f"sqlite:///{tmp_path / 'migration.sqlite3'}"
    command.upgrade(configuration(url), "head")
    command.upgrade(configuration(url), "head")
    engine = db.build_engine(url)
    try:
        with engine.begin() as connection:
            assert set(inspect(connection).get_table_names()) == set(db.Base.metadata.tables) | {"alembic_version"}
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001_initial"
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, db.Base.metadata) == []
            ownership = inspect(connection).get_foreign_keys("resources")
            assert any(fk["name"] == "fk_resource_release_owner" for fk in ownership)
        with db.make_session_factory(engine).begin() as session:
            seed_graph(session)
    finally:
        engine.dispose()


def test_settings_paths_aliases_and_secret_repr(tmp_path, monkeypatch):
    settings = Settings(storage_dir=tmp_path, job_mode="celery", scan_mode="clamav", rabbitmq_url="amqp://localhost//")
    assert settings.database_url == f"sqlite:///{tmp_path / 'fund_kb.sqlite3'}"
    assert settings.qdrant_path == tmp_path / "qdrant"
    assert settings.job_backend == settings.job_mode == "celery"
    assert settings.scan_backend == settings.scan_mode == "clamav"
    assert settings.celery_broker_url == settings.rabbitmq_url == "amqp://localhost//"
    monkeypatch.setenv("FKB_JOB_MODE", "celery")
    monkeypatch.setenv("FKB_SCAN_BACKEND", "adapter")
    assert Settings().job_backend == "celery" and Settings().scan_backend == "adapter"
    with pytest.raises(ValueError):
        Settings(job_mode="local", job_backend="celery")
    secrets = Settings(llm_api_key="synthetic-secret", qdrant_api_key="synthetic-secret", oidc_client_secret="synthetic-secret")
    assert "synthetic-secret" not in repr(secrets)
    with pytest.raises(ValidationError):
        Settings(database_url="")  # Explicit invalid URL is never replaced with the default.


def test_s3_identity_is_explicit_and_errors_hide_inputs():
    with pytest.raises(ValidationError, match="explicit mode requires"):
        Settings(storage_backend="s3", s3_bucket="synthetic")
    settings = Settings(storage_backend="s3", s3_bucket="synthetic", s3_access_key="test-access",
                        s3_secret_key="sensitive-synthetic-secret")
    assert settings.s3_access_key_id == settings.s3_access_key == "test-access"
    assert settings.s3_secret_access_key == settings.s3_secret_key
    assert "sensitive-synthetic-secret" not in repr(settings)
    assert Settings(storage_backend="s3", s3_bucket="synthetic", s3_credential_mode="workload").s3_credential_mode == "workload"
    with pytest.raises(ValidationError) as error:
        Settings(database_url="invalid", llm_api_key="sensitive-synthetic-secret")
    assert "sensitive-synthetic-secret" not in str(error.value)
    assert "input_value" not in str(error.value)


def test_approved_model_policies_are_structured_and_empty_by_default():
    first, second = Settings(), Settings()
    assert first.approved_model_policies == second.approved_model_policies == {}
    record = {
        "provider_ref": "evidence", "generation_model": None, "extraction_model": None,
        "prompt_version": "engineering-only-v1", "enable_vector": False, "scope": "engineering-only",
    }
    configured = Settings(approved_model_policies={"engineering-only-example": record})
    assert configured.approved_model_policies["engineering-only-example"] == record
    assert "PASS" not in str(configured.approved_model_policies)
    first.approved_model_policies["engineering-only-example"] = record
    assert second.approved_model_policies == {}
    for invalid in (["anything"], {"anything": "PASS"}):
        with pytest.raises(ValidationError):
            Settings(approved_model_policies=invalid)


def test_production_rejects_development_defaults():
    with pytest.raises(ValidationError):
        Settings(app_env="production")
    valid = {"app_env": "production", "retrieval_mode": "hybrid", "database_url": "oracle://", "storage_dir": "/tmp/fkb-test-only",
                 "qdrant_url": "https://qdrant.invalid", "allowed_origins": ["https://fkb.invalid"], "auth_mode": "oidc",
                 "oidc_issuer": "https://id.invalid", "oidc_client_id": "synthetic", "oidc_client_secret": "synthetic",
                 "cookie_secure": True, "auto_create_schema": False, "embedding_mode": "http", "embedding_model": "unselected",
                 "scan_backend": "clamav"}
    assert Settings(**valid).app_env == "production"  # Configuration validation is not service verification.
    for field, value in (("auth_mode", "demo"), ("embedding_mode", "hashing"), ("scan_backend", "basic"),
                          ("auto_create_schema", True), ("database_url", "sqlite:///:memory:"), ("cookie_secure", False)):
        with pytest.raises(ValidationError):
            Settings(**{**valid, field: value})
    wiki = {key: value for key, value in valid.items() if key not in {"qdrant_url", "embedding_mode", "embedding_model"}}
    wiki["retrieval_mode"] = "wiki"
    configured = Settings(**wiki)
    assert configured.retrieval_mode == "wiki" and configured.qdrant_url is None


@pytest.mark.parametrize("backend,variable", [("oracle", "FKB_TEST_ORACLE_URL"), ("mysql", "FKB_TEST_OCEANBASE_URL")])
def test_optional_real_database(backend, variable):
    url = os.environ.get(variable)
    if not url or os.environ.get("FKB_TEST_ALLOW_DDL") != "1":
        pytest.skip(f"NOT_VERIFIED: explicit {variable} and FKB_TEST_ALLOW_DDL=1 are required")
    engine = db.build_engine(url)
    assert engine.dialect.name == backend
    try:
        with engine.connect() as connection:
            # Do not operate in an existing application schema, even if a URL was supplied.
            assert inspect(connection).get_table_names() == [], "Test database/schema must be empty"
            if backend == "mysql":
                version = str(connection.scalar(text("SELECT ob_version()")))
                assert "OceanBase" in version or re.search(r"\d+\.\d+\.\d+", version)
                mode = str(connection.scalar(text("SELECT @@ob_compatibility_mode"))).upper()
                assert mode == "MYSQL"
            else:
                assert tuple(engine.dialect.server_version_info) >= (19,)
        config = configuration(url)
        command.upgrade(config, "head")
        try:
            with db.make_session_factory(engine)() as session:
                user, _space, resource, version = seed_graph(session)
                assert session.get(m.ResourceVersion, version.id).applicability == {}
                session.add(m.ResourceVersion(resource_id=resource.id, version_no=2, author_id=user.id,
                                              title="并发草稿约束", origin="HUMAN"))
                with pytest.raises(IntegrityError):
                    session.flush()
                session.rollback()
            with engine.connect() as connection:
                assert set(inspect(connection).get_table_names()) == set(db.Base.metadata.tables) | {"alembic_version"}
        finally:
            # Exact revision-owned tables only, in the confirmed-empty test schema.
            command.downgrade(config, "base")
            with engine.begin() as connection:
                Table("alembic_version", MetaData(), Column("version_num", Integer)).drop(connection, checkfirst=True)
    finally:
        engine.dispose()


@pytest.mark.parametrize("invalid", ["EXCLUSIVE", "immediate", "IMMEDIATE; PRAGMA foreign_keys=OFF", None])
def test_sqlite_transaction_mode_is_whitelisted(engine, invalid):
    selected = engine.execution_options(sqlite_transaction_mode=invalid)
    with selected.connect() as connection, pytest.raises(ValueError, match="IMMEDIATE or DEFERRED"):
        connection.execute(text("SELECT 1"))
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1


def test_sqlite_default_remains_deferred(engine):
    statements = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
        assert "BEGIN DEFERRED" in statements
        assert db.database_capabilities(engine)["sqlite_transaction_mode"] == "DEFERRED"
    finally:
        event.remove(engine, "before_cursor_execute", observe)


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_sqlite_file_read_then_write_transactions_wait_before_first_read(engine, graph, journal_mode):
    # This is a real file and two independent DBAPI connections. WAL is enabled
    # only on this disposable test file, never on the application's database.
    raw = engine.raw_connection()
    try:
        result = raw.cursor().execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0]
        assert result.upper() == journal_mode
    finally:
        raw.close()
    writes = engine.execution_options(sqlite_transaction_mode="IMMEDIATE")
    factory = db.make_session_factory(writes)
    first_read = threading.Event()
    release_first = threading.Event()
    second_attempted_begin = threading.Event()
    second_read = threading.Event()
    worker_ids = {}

    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE" and threading.get_ident() == worker_ids.get("second"):
            second_attempted_begin.set()

    def first_writer():
        with factory.begin() as session:
            row = session.get(m.Resource, graph["resource"])
            assert session.scalar(text("PRAGMA foreign_keys")) == 1
            first_read.set()
            assert release_first.wait(5), "test did not release first writer"
            row.name = "first committed"

    def second_writer():
        worker_ids["second"] = threading.get_ident()
        with factory.begin() as session:
            row = session.get(m.Resource, graph["resource"])
            second_read.set()
            observed = row.name
            assert session.scalar(text("PRAGMA foreign_keys")) == 1
            row.name = "second committed"
        return observed

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(first_writer)
            try:
                assert first_read.wait(5)
                second = workers.submit(second_writer)
                assert second_attempted_begin.wait(5)
                assert not second_read.is_set(), "second writer must wait BEFORE its first read"
                # A DEFERRED health/read transaction can coexist with the writer's reserved lock.
                with engine.connect() as connection:
                    assert connection.scalar(text("SELECT 1")) == 1
                    assert connection.scalar(select(m.Resource.name).where(m.Resource.id == graph["resource"])) == "证据"
            finally:
                release_first.set()
            first.result(timeout=5)
            assert second.result(timeout=5) == "first committed"
        with factory() as session:
            row = session.get(m.Resource, graph["resource"])
            assert row.name == "second committed" and row.revision == 3
        assert db.database_capabilities(writes)["sqlite_transaction_mode"] == "IMMEDIATE"
    finally:
        event.remove(engine, "before_cursor_execute", observe)
