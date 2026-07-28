"""Offline PostgreSQL migration schema; not imported by the application runtime."""

from .metadata import metadata

ALEMBIC_HEAD = "0006_join_request_admission"

__all__ = ["ALEMBIC_HEAD", "metadata"]
