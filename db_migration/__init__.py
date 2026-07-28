"""Offline PostgreSQL migration package with a dependency-light contract surface."""

ALEMBIC_HEAD = "0008_task_template_versioning"

__all__ = ["ALEMBIC_HEAD", "metadata"]


def __getattr__(name):
    """Load SQLAlchemy metadata only for migration callers that request it."""
    if name == "metadata":
        from .metadata import metadata

        return metadata
    raise AttributeError(name)
