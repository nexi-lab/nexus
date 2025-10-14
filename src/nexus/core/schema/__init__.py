"""Database schema definitions for Nexus."""

from nexus.core.schema.postgres import POSTGRES_SCHEMA
from nexus.core.schema.sqlite import SQLITE_SCHEMA

__all__ = ["SQLITE_SCHEMA", "POSTGRES_SCHEMA"]
