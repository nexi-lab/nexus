"""Database URL conversion utilities.

Issue #2195: Extracted from lifespan for testability and DRY.
"""

from typing import overload


@overload
def normalize_database_url(url: str) -> str: ...
@overload
def normalize_database_url(url: None) -> None: ...
def normalize_database_url(url: str | None) -> str | None:
    """Normalize the canonical ``postgres://`` scheme to ``postgresql://``.

    SQLAlchemy dropped the ``postgres://`` dialect alias in 1.4 and only
    accepts ``postgresql://``, but the canonical scheme is what
    ``pg_dump``/``pg_isready`` and most cloud providers (Railway, Render,
    Supabase, Heroku) still emit by default. Operators can rarely rewrite
    the URL platforms inject for them, so we normalize at ingest.

    Issue #4238: ``None`` and empty strings pass through unchanged so
    callers can pipe ``os.getenv(...)`` directly without a guard.

    Examples::

        >>> normalize_database_url("postgres://host/db")
        'postgresql://host/db'
        >>> normalize_database_url("postgresql://host/db")
        'postgresql://host/db'
        >>> normalize_database_url("sqlite:///x.db")
        'sqlite:///x.db'
        >>> normalize_database_url(None) is None
        True
    """
    if not url:
        return url
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def sqlalchemy_url_to_asyncpg_dsn(url: str) -> str:
    """Convert a SQLAlchemy database URL to an asyncpg-compatible DSN.

    Strips ``+asyncpg`` and ``+psycopg2`` driver suffixes so the URL
    can be passed directly to ``asyncpg.create_pool()``.

    Examples::

        >>> sqlalchemy_url_to_asyncpg_dsn("postgresql+asyncpg://host/db")
        'postgresql://host/db'
        >>> sqlalchemy_url_to_asyncpg_dsn("postgresql+psycopg2://host/db")
        'postgresql://host/db'
        >>> sqlalchemy_url_to_asyncpg_dsn("postgresql://host/db")
        'postgresql://host/db'
    """
    return url.replace("+asyncpg", "").replace("+psycopg2", "")
