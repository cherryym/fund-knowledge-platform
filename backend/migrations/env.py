"""Alembic entry point; use explicit application configuration, never fallback."""
from alembic import context

from fund_kb import models  # noqa: F401
from fund_kb.db import Base, build_engine, normalize_database_url
from fund_kb.settings import get_settings

config = context.config
target_metadata = Base.metadata


def run_migrations_offline():
    url = config.attributes.get("database_url") or get_settings().database_url
    normalized = normalize_database_url(url)
    context.configure(url=normalized, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, compare_type=True,
                      transactional_ddl=normalized.get_backend_name() == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


def migrate_connection(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                      render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connection = config.attributes.get("connection")
    if connection is not None:
        migrate_connection(connection)
        return
    url = config.attributes.get("database_url") or get_settings().database_url
    engine = build_engine(url)
    try:
        with engine.connect() as connection:
            migrate_connection(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
