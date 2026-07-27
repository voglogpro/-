"""Strict Alembic environment for the PostgreSQL cutover harness only."""

from __future__ import annotations

import os
import re
from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, pool, text
from sqlalchemy.engine import make_url

from db_migration.metadata import metadata


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata
FORBIDDEN_DSN_QUERY_KEYS = {
    "host", "hostaddr", "port", "service", "dbname", "user", "options",
}
ADVISORY_LOCK_KEY = 4_242_428_675_309


def required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for guarded Alembic execution")
    return value


def target_expectations() -> dict[str, object]:
    schema = required_env("MIGRATION_EXPECTED_SCHEMA")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RuntimeError("MIGRATION_EXPECTED_SCHEMA must be a plain identifier")
    try:
        port = int(required_env("MIGRATION_EXPECTED_SERVER_PORT"))
    except ValueError as exc:
        raise RuntimeError("MIGRATION_EXPECTED_SERVER_PORT must be numeric") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("MIGRATION_EXPECTED_SERVER_PORT is outside 1..65535")
    return {
        "database": required_env("MIGRATION_EXPECTED_DATABASE"),
        "schema": schema,
        "host": required_env("MIGRATION_EXPECTED_SERVER_ADDRESS"),
        "port": port,
        "user": required_env("MIGRATION_EXPECTED_USER"),
    }


def migration_url(expected: dict[str, object]) -> str:
    raw = required_env("MIGRATION_DATABASE_URL")
    url = make_url(raw)
    if not url.drivername.startswith("postgresql") or not url.database:
        raise RuntimeError("MIGRATION_DATABASE_URL must name PostgreSQL explicitly")
    if FORBIDDEN_DSN_QUERY_KEYS.intersection(url.query):
        raise RuntimeError("MIGRATION_DATABASE_URL contains identity/endpoint overrides")
    if not url.host or "," in url.host:
        raise RuntimeError("MIGRATION_DATABASE_URL must name exactly one host")
    if url.host != expected["host"] or int(url.port or 5432) != expected["port"]:
        raise RuntimeError("MIGRATION_DATABASE_URL does not match the expected endpoint")
    if url.database != expected["database"] or url.username != expected["user"]:
        raise RuntimeError("MIGRATION_DATABASE_URL does not match expected identity")
    return raw


def validate_connected_target(connection, expected: dict[str, object]) -> None:
    if connection.dialect.name != "postgresql":
        raise RuntimeError("PostgreSQL is the only supported migration target")
    identity = connection.execute(text(
        "SELECT current_database(),current_schema(),current_user,"
        "COALESCE(inet_server_addr()::text,''),inet_server_port()"
    )).one()
    database, schema, user, _backend_address, _backend_port = identity
    if database != expected["database"] or database in {"postgres", "template0", "template1"}:
        raise RuntimeError("connected database identity is not the expected target")
    if schema != expected["schema"] or user != expected["user"]:
        raise RuntimeError("connected schema/role is not the expected target")
    # inet_server_addr/port describe the backend socket, not necessarily the
    # externally guarded URL endpoint (for example 55432 -> container 5432).
    # Database, schema and role remain connected-target checks; host/port are
    # already matched exactly against MIGRATION_DATABASE_URL before connecting.

    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
    tables = set(inspect(connection).get_table_names(schema=str(expected["schema"])))
    business_tables = tables - {"alembic_version"}
    if "alembic_version" not in tables:
        if business_tables:
            raise RuntimeError("baseline migration requires an empty target schema")
        return

    revisions = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    known = {revision.revision for revision in ScriptDirectory.from_config(config).walk_revisions()}
    if not revisions.issubset(known):
        raise RuntimeError("target contains an unknown Alembic revision")
    if not revisions and business_tables:
        raise RuntimeError("unversioned business tables are not accepted")


def run_migrations_offline() -> None:
    expected = target_expectations()
    url = migration_url(expected)
    if os.getenv("MIGRATION_OFFLINE_ACK") != "unverified-sql-generation-only":
        raise RuntimeError(
            "offline SQL cannot verify a target; set MIGRATION_OFFLINE_ACK="
            "unverified-sql-generation-only only for reviewed SQL generation"
        )
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        transactional_ddl=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    expected = target_expectations()
    url = migration_url(expected)
    connectable = create_engine(url, poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        with connection.begin():
            validate_connected_target(connection, expected)
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
                transactional_ddl=True,
            )
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
