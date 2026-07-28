"""Offline PostgreSQL migration schema; not imported by the application runtime."""

from .metadata import metadata

ALEMBIC_HEAD = "0002_pilot_reliability"

__all__ = ["ALEMBIC_HEAD", "metadata"]
