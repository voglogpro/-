"""Offline PostgreSQL migration schema; not imported by the application runtime."""

from .metadata import metadata

ALEMBIC_HEAD = "0004_authority_registry"

__all__ = ["ALEMBIC_HEAD", "metadata"]
