"""Offline PostgreSQL migration schema; not imported by the application runtime."""

from .metadata import metadata

ALEMBIC_HEAD = "0005_manual_grant_reversals"

__all__ = ["ALEMBIC_HEAD", "metadata"]
