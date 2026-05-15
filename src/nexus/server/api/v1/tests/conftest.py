"""Shared pytest fixtures for server API v1 tests.

Re-exports the ``pg_engine`` fixture from ``nexus.bricks.auth.tests`` so
tests in this package can request it as a parameter without triggering
ruff's F811 (redefinition) on a direct module-level import.
"""

from __future__ import annotations

from nexus.bricks.auth.tests.test_postgres_profile_store import (
    pg_engine,  # noqa: F401  -- pytest fixture re-export
)
