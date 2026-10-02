"""Database URL conversion utilities.

Issue #2195: Extracted from lifespan for testability and DRY.
"""

from typing import overload


@overload
def normalize_database_url(url: str) -> str: ...
@overload
def normalize_database_url(url: None) -> None: ...
def normalize_database_url(url: str | None) -> str | None:
    """Normalize PostgreSQL URLs for the project's synchronous SQLAlchemy driver.

    SQLAlchemy dropped the ``postgres://`` dialect alias in 1.4, but that is what
    ``pg_dump``/``pg_isready`` and most cloud providers (Railway, Render,
    Supabase, Heroku) still emit by default. Operators can rarely rewrite
    the URL platforms inject for them, so we normalize at ingest. Select the
    installed psycopg2 driver explicitly: SQLAlchemy 2.1 defaults unqualified
    PostgreSQL URLs to psycopg (v3), which Nexus does not install. Explicit
    driver choices and all connection parameters are preserved.

    Issue #4238: ``None`` and empty strings pass through unchanged so
    callers can pipe ``os.getenv(...)`` directly without a guard.

    Examples::

        >>> normalize_database_url("postgres://host/db")
        'postgresql+psycopg2://host/db'
        >>> normalize_database_url("postgresql://host/db")
        'postgresql+psycopg2://host/db'
        >>> normalize_database_url("sqlite:///x.db")
        'sqlite:///x.db'
        >>> normalize_database_url(None) is None
        True
    """
    if not url:
        return url
    scheme, separator, rest = url.partition("://")
    if separator and scheme in ("postgres", "postgresql"):
        return f"postgresql+psycopg2://{rest}"
    return url


def sqlalchemy_url_to_postgres_dsn(url: str) -> str:
    """Convert a SQLAlchemy PostgreSQL URL for a native database client.

    psycopg2 and asyncpg accept PostgreSQL URIs, not SQLAlchemy's
    ``postgresql+driver`` dialect names. Only change the scheme: credentials
    and query parameters may themselves contain a driver's name.

    Examples::

        >>> sqlalchemy_url_to_postgres_dsn("postgresql+asyncpg://host/db")
        'postgresql://host/db'
        >>> sqlalchemy_url_to_postgres_dsn("postgresql+psycopg2://host/db")
        'postgresql://host/db'
        >>> sqlalchemy_url_to_postgres_dsn("postgresql://host/db")
        'postgresql://host/db'
    """
    scheme, separator, rest = url.partition("://")
    if separator and scheme.split("+", 1)[0] in ("postgres", "postgresql"):
        return f"postgresql://{rest}"
    return url
