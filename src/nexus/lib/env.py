"""Centralized environment variable resolution for infrastructure URLs.

All scattered ``os.getenv("NEXUS_DATABASE_URL")`` (and similar) calls across
CLI commands, services, and factories should use these helpers so that
fallback logic and env-var names live in exactly one place.

Related: Issue #629 (scattered env reads not factory-centralized)
"""

import os


def get_database_url() -> str | None:
    """Resolve database connection URL from environment.

    Checks ``NEXUS_DATABASE_URL`` first, falls back to ``POSTGRES_URL``.
    Returns None when neither is set.

    Issue #4238: rewrites the canonical ``postgres://`` scheme (emitted
    by Railway, Render, Supabase, Heroku) to ``postgresql://``, which is
    the only Postgres dialect SQLAlchemy loads since 1.4. Inlined
    rather than importing from ``nexus.core.db_utils`` because the
    LEGO 5-tier architecture forbids ``lib`` from importing ``core``
    (import-linter contract).
    """
    raw = os.getenv("NEXUS_DATABASE_URL") or os.getenv("POSTGRES_URL")
    if raw and raw.startswith("postgres://"):
        return "postgresql://" + raw[len("postgres://") :]
    return raw


def get_redis_url() -> str | None:
    """Resolve Redis connection URL from environment.

    Reads ``NEXUS_REDIS_URL``. Returns None when not set.
    """
    return os.getenv("NEXUS_REDIS_URL")


def get_dragonfly_url() -> str | None:
    """Resolve Dragonfly cache URL from environment.

    Checks ``NEXUS_DRAGONFLY_URL`` first, falls back to ``DRAGONFLY_URL``.
    Returns None when neither is set.
    """
    return os.getenv("NEXUS_DRAGONFLY_URL") or os.getenv("DRAGONFLY_URL")
