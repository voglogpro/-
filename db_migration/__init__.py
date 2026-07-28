"""Offline PostgreSQL migration schema; not imported by the application runtime."""

from .metadata import metadata

ALEMBIC_HEAD = "0008_task_template_versioning"

__all__ = ["ALEMBIC_HEAD", "metadata"]
