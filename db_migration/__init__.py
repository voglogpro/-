"""Offline PostgreSQL migration schema; not imported by the application runtime."""

from .metadata import metadata

ALEMBIC_HEAD = "0001_pg_baseline"

__all__ = ["ALEMBIC_HEAD", "metadata"]
