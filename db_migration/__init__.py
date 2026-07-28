"""Offline PostgreSQL migration schema; not imported by the application runtime."""

from .metadata import metadata

ALEMBIC_HEAD = "0007_capability_rbac"

__all__ = ["ALEMBIC_HEAD", "metadata"]
