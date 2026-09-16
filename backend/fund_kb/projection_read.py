"""Reuse work only inside one consistent SQLite GET read transaction.

No cross-request response or permission cache. Other database engines keep the
original path until their snapshot isolation is independently established.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from sqlalchemy import event


@dataclass
class _ReadScope:
    session: object
    transaction: object
    values: dict = field(default_factory=dict)
    rows: dict = field(default_factory=dict)
    live: bool = True


_CURRENT = ContextVar("fundkb_projection_read", default=None)
_MAX_ENTRIES = 20000  # Beyond this memo budget work is recomputed, never omitted.


def active(db):
    scope = _CURRENT.get()
    return bool(scope is not None and scope.live and scope.session is db and scope.transaction is db.get_transaction()
                and not db.new and not db.dirty and not db.deleted)


def memo(db, namespace, key, compute):
    scope = _CURRENT.get()
    if not active(db):
        return compute()
    cache_key = (namespace, key)
    if cache_key in scope.values:
        return scope.values[cache_key]
    result = compute()
    if len(scope.values) < _MAX_ENTRIES:
        scope.values[cache_key] = result
    return result


@contextmanager
def projection_read(db):
    if db.get_bind().dialect.name != "sqlite" or db.new or db.dirty or db.deleted:
        yield
        return
    connection = db.connection()
    raw = connection.connection.driver_connection
    if (not raw.in_transaction or connection.exec_driver_sql("PRAGMA query_only").scalar() != 1
            or connection.exec_driver_sql("PRAGMA read_uncommitted").scalar() != 0):
        yield
        return
    previous = _CURRENT.get()
    if previous is not None and previous.session is db and previous.transaction is db.get_transaction():
        yield
        return
    scope = _ReadScope(db, db.get_transaction())
    token = _CURRENT.set(scope)

    def clear(*_):
        scope.values.clear()
        scope.rows.clear()

    def keep_row(session, instance):
        # SQLAlchemy's identity map normally uses weak references. Retain rows
        # already read in this snapshot to avoid repeatedly hydrating the same
        # source/blob/version. This performs no new reads or authorization.
        if (scope.live and scope.session is session and scope.transaction is session.get_transaction()
                and len(scope.rows) < 100000):
            scope.rows[id(instance)] = instance

    def statement(state):
        if not state.is_select:
            clear()

    hooks = [("before_flush", clear), ("after_flush", clear), ("after_rollback", clear),
             ("after_commit", clear), ("after_transaction_end", clear), ("do_orm_execute", statement),
             ("loaded_as_persistent", keep_row)]
    for name, callback in hooks:
        event.listen(db, name, callback)
    try:
        yield
    finally:
        for name, callback in hooks:
            event.remove(db, name, callback)
        scope.live = False
        clear()
        _CURRENT.reset(token)
