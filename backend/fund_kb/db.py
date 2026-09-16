"""Portable persistence primitives. Engine failures are never converted to SQLite."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import CHAR, Boolean, DateTime, MetaData, String, Text, create_engine, event
from sqlalchemy.dialects import mysql, oracle
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import QueuePool, StaticPool
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCNow(FunctionElement):
    type = DateTime()
    inherit_cache = True


@compiles(UTCNow)
def _utc_default(element, compiler, **kw):
    return "CURRENT_TIMESTAMP"


@compiles(UTCNow, "sqlite")
def _utc_sqlite(element, compiler, **kw):
    return "(strftime('%Y-%m-%d %H:%M:%f', 'now'))"


@compiles(UTCNow, "mysql")
def _utc_mysql(element, compiler, **kw):
    return "CURRENT_TIMESTAMP(6)"


@compiles(UTCNow, "oracle")
def _utc_oracle(element, compiler, **kw):
    return "SYS_EXTRACT_UTC(SYSTIMESTAMP)"


class UUIDString(TypeDecorator):
    """Python canonical UUID str / CHAR(36), including every foreign key."""
    impl = CHAR(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(UUID(str(value)))

    def process_result_value(self, value, dialect):
        return None if value is None else str(UUID(str(value).strip()))


class BoundedString(TypeDecorator):
    """Reject oversized values before binding; never rely on MySQL truncation."""
    impl = String
    cache_ok = True

    def __init__(self, length: int):
        self.length = length
        super().__init__(length=length)

    def process_bind_param(self, value, dialect):
        if value is not None and (not isinstance(value, str) or len(value) > self.length):
            raise ValueError(f"Expected a string of at most {self.length} characters")
        return value

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.VARCHAR(self.length, collation="utf8mb4_bin"))
        return dialect.type_descriptor(String(self.length))


class ExactKey(BoundedString):
    """Preserve case and trailing spaces for unique identifiers on MySQL.

    utf8mb4_bin still has PAD SPACE comparison rules on some supported servers.
    VARBINARY avoids equating distinct idempotency keys or object keys there.
    """
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.VARBINARY(self.length * 4))
        return dialect.type_descriptor(String(self.length))

    def process_bind_param(self, value, dialect):
        value = super().process_bind_param(value, dialect)
        return value.encode("utf-8") if value is not None and dialect.name == "mysql" else value

    def process_result_value(self, value, dialect):
        return value.decode("utf-8") if isinstance(value, bytes) else value


class JSONShape(ColumnElement):
    """DDL-only check expression, shared by models and frozen migrations."""
    type = Boolean()
    inherit_cache = False

    def __init__(self, column: str, kind: str = "any"):
        self.column_name = column
        self.kind = kind


@compiles(JSONShape)
@compiles(JSONShape, "sqlite")
def _json_shape_sqlite(element, compiler, **kw):
    col = compiler.preparer.quote(element.column_name)
    kind = "IN ('object','array')" if element.kind == "any" else f"= '{element.kind}'"
    return f"({col} IS NULL OR (json_valid({col}) AND json_type({col}) {kind}))"


@compiles(JSONShape, "mysql")
def _json_shape_mysql(element, compiler, **kw):
    col = compiler.preparer.quote(element.column_name)
    kind = "IN ('OBJECT','ARRAY')" if element.kind == "any" else f"= '{element.kind.upper()}'"
    return f"({col} IS NULL OR (JSON_VALID({col}) AND JSON_TYPE({col}) {kind}))"


@compiles(JSONShape, "oracle")
def _json_shape_oracle(element, compiler, **kw):
    col = compiler.preparer.quote(element.column_name)
    shape = ""
    if element.kind != "any":
        shape = f" AND JSON_EXISTS({col}, '$?(@.type() == \"{element.kind}\")')"
    return f"({col} IS NULL OR ({col} IS JSON STRICT WITH UNIQUE KEYS{shape}))"


class UTCDateTime(TypeDecorator):
    """UTC without timezone on disk; UTC-aware datetime at the Python boundary."""
    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        if dialect.name == "oracle":
            return dialect.type_descriptor(oracle.TIMESTAMP(timezone=False))
        return dialect.type_descriptor(DateTime(timezone=False))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Precise timestamps require a timezone-aware datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class JSONDocument(TypeDecorator):
    """JSON text (Oracle CLOB / MySQL LONGTEXT). Reassign the complete value to edit.

    No MutableDict: nested in-place edits cannot be reliably tracked. Use
    ``row.payload = {**row.payload, 'x': new_value}``, or deepcopy then reassign.
    SQL NULL remains None; JSON null is deliberately not a separate application value.
    """
    impl = Text
    cache_ok = True

    def __init__(self, kind: str = "any", string_items: bool = False):
        if kind not in {"any", "object", "array"}:
            raise ValueError("Invalid JSON root kind")
        self.kind = kind
        self.string_items = string_items
        super().__init__()

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.LONGTEXT(charset="utf8mb4", collation="utf8mb4_bin"))
        if dialect.name == "oracle":
            return dialect.type_descriptor(oracle.CLOB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if self.kind == "object" and not isinstance(value, dict):
            raise ValueError("JSON document must be an object")
        if self.kind == "array" and not isinstance(value, list):
            raise ValueError("JSON document must be an array")
        if self.string_items and any(not isinstance(item, str) for item in value):
            raise ValueError("JSON array must contain only strings")
        # Do not allow values that Oracle 19c cannot represent as a JSON document.
        if not isinstance(value, (dict, list)):
            raise TypeError("JSON document root must be an object or array")
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if hasattr(value, "read"):
            value = value.read()
        return json.loads(value)


class EmptyText(TypeDecorator):
    """Allow logical empty text without Oracle's empty-string/NOT NULL conflict.

    These particular columns are physically nullable on every backend. At the
    application boundary both SQL NULL and an empty string represent empty text.
    Required nonempty fields must NOT use this type.
    """
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.LONGTEXT(charset="utf8mb4", collation="utf8mb4_bin"))
        if dialect.name == "oracle":
            return dialect.type_descriptor(oracle.CLOB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        return None if value == "" or value is None else value

    def process_result_value(self, value, dialect):
        if hasattr(value, "read"):
            value = value.read()
        return "" if value is None else value


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })


def normalize_database_url(database_url: str | URL) -> URL:
    try:
        url = make_url(database_url)
    except ArgumentError:
        raise ValueError("Invalid database URL") from None
    if url.drivername in {"oceanbase", "oceanbase+mysql", "oceanbase+pymysql", "mysql"}:
        url = url.set(drivername="mysql+pymysql")
    elif url.drivername == "oracle":
        url = url.set(drivername="oracle+oracledb")
    if url.drivername not in {"sqlite", "sqlite+pysqlite", "mysql+pymysql", "oracle+oracledb"}:
        raise ValueError("Unsupported database driver; choose SQLite, OceanBase MySQL/PyMySQL, or Oracle/oracledb")
    if url.drivername == "mysql+pymysql":
        if url.query.get("charset", "utf8mb4") != "utf8mb4":
            raise ValueError("OceanBase/MySQL connections require charset=utf8mb4")
        url = url.update_query_dict({"charset": "utf8mb4"})
    return url


def build_engine(database_url: str | URL, *, read_only: bool = False) -> Engine:
    """Construct, but do not connect. No credentials discovery or fallback.

    Oracle uses python-oracledb's default Thin mode, without init_oracle_client.
    Oracle schemas and OceanBase tenants/databases must already be provisioned.
    """
    url = normalize_database_url(database_url)
    memory = url.get_backend_name() == "sqlite" and (
        url.database in {None, "", ":memory:"} or url.query.get("mode") == "memory")
    if read_only and url.get_backend_name() == "sqlite" and not memory:
        # A read pool must not create a missing database, or change the writer's
        # pooled connections. SQLite URI mode and query_only apply only here.
        if url.query.get("uri") != "true":
            url = url.set(database="file:" + quote(str(Path(url.database).resolve()), safe="/:"))
        url = url.update_query_dict({"uri": "true", "mode": "ro"})
    options: dict[str, Any] = {"pool_pre_ping": True, "hide_parameters": True}
    if url.get_backend_name() == "sqlite":
        options["connect_args"] = {"check_same_thread": False, "timeout": 15}
        if url.database in {None, "", ":memory:"}:
            options["poolclass"] = StaticPool
        elif memory:
            options["poolclass"] = QueuePool
    else:
        options.update(isolation_level="READ COMMITTED", pool_recycle=1800)
    engine = create_engine(url, **options)
    if engine.dialect.name == "mysql":
        @event.listens_for(engine, "connect")
        def mysql_connect(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET SESSION time_zone = '+00:00'")
            finally:
                cursor.close()
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def sqlite_connect(dbapi_connection, connection_record):
            # Disable sqlite3's legacy autocommit control, so SAVEPOINT/DDL obey
            # the actual SQLAlchemy transaction boundary.
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=15000")
                if read_only:
                    cursor.execute("PRAGMA query_only=ON")
            finally:
                cursor.close()
            if read_only:
                # Also reject attempts to turn query_only off through raw SQL.
                import sqlite3

                def authorize(action, first, second, _database, _trigger):
                    if action == sqlite3.SQLITE_PRAGMA:
                        readable = {"table_info", "table_xinfo", "index_list", "index_info",
                                    "index_xinfo", "foreign_key_list"}
                        return sqlite3.SQLITE_OK if second is None or first.lower() in readable else sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_FUNCTION and (second or first or "").lower() == "load_extension":
                        return sqlite3.SQLITE_DENY
                    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                               sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_RECURSIVE}
                    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

                dbapi_connection.set_authorizer(authorize)

        @event.listens_for(engine, "begin")
        def sqlite_begin(connection):
            mode = "DEFERRED" if read_only else connection.get_execution_options().get("sqlite_transaction_mode", "DEFERRED")
            if mode not in ("IMMEDIATE", "DEFERRED"):
                raise ValueError("sqlite_transaction_mode must be IMMEDIATE or DEFERRED")
            # Choose on the bound Engine BEFORE the first SELECT. IMMEDIATE
            # waits for the writer slot without retaining an old read snapshot.
            connection.exec_driver_sql(f"BEGIN {mode}")
    elif read_only:
        @event.listens_for(engine, "begin")
        def begin_read_only(connection):
            # Server enforcement supplements the ORM guards. Real Oracle and
            # OceanBase interoperability still requires their own integration run.
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
    if read_only:
        @event.listens_for(engine, "before_execute")
        def reject_write_statement(_connection, statement, _multiparams, _params, _options):
            if not getattr(statement, "is_select", False) or getattr(statement, "_for_update_arg", None) is not None:
                raise ReadOnlyViolation("Read-only request attempted a write or locking statement")
    return engine


class ReadOnlyViolation(RuntimeError):
    """A programming error, never an authorization grant or a successful read."""


class ReadOnlySession(Session):
    """Request-owned reads; no permission decisions survive this Session."""


@event.listens_for(ReadOnlySession, "before_flush")
def reject_read_flush(session, _context, _instances):
    if session.new or session.dirty or session.deleted:
        raise ReadOnlyViolation("Read-only request attempted an ORM flush")


@event.listens_for(ReadOnlySession, "before_commit")
def reject_dirty_read_commit(session):
    if session.new or session.dirty or session.deleted:
        raise ReadOnlyViolation("Read-only request attempted to commit changes")


@event.listens_for(ReadOnlySession, "do_orm_execute")
def reject_read_dml(state):
    if not state.is_select or getattr(state.statement, "_for_update_arg", None) is not None:
        raise ReadOnlyViolation("Read-only request attempted DML or locking SQL")


def make_session_factory(engine: Engine, *, read_only: bool = False):
    return sessionmaker(bind=engine, class_=ReadOnlySession if read_only else Session,
                        expire_on_commit=False, autoflush=not read_only)


def database_capabilities(engine: Engine) -> dict[str, Any]:
    """Configuration facts only; this function never connects or certifies a server."""
    dialect = engine.dialect.name
    return {
        "dialect": dialect,
        "driver": engine.dialect.driver,
        "family": {"mysql": "oceanbase_mysql_candidate", "oracle": "oracle", "sqlite": "sqlite"}[dialect],
        "configured": True,
        "real_database_verified": False,
        "verification_status": "NOT_EVALUATED",
        "supports_for_update": dialect != "sqlite",
        "skip_locked": "REQUIRES_SERVER_VERSION_TEST" if dialect != "sqlite" else False,
        "transaction_isolation": "READ COMMITTED" if dialect != "sqlite" else "SQLITE",
        "transactional_ddl": dialect == "sqlite",
        "sqlite_transaction_mode": engine.get_execution_options().get("sqlite_transaction_mode", "DEFERRED")
        if dialect == "sqlite" else None,
        "json_storage": {"mysql": "LONGTEXT", "oracle": "CLOB", "sqlite": "TEXT"}[dialect],
        "timestamps": "UTC, microseconds, timezone-aware Python values",
        "production_eligible": dialect != "sqlite",
        "schema_creation": "migration_required" if dialect != "sqlite" else "development_only",
    }
