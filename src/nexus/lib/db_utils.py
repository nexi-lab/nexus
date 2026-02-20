"""Database URL conversion utilities.

Issue #2195: Extracted from lifespan to support factory-based service construction.
"""

from __future__ import annotations


def sqlalchemy_url_to_asyncpg_dsn(url: str) -> str:
    """Convert a SQLAlchemy database URL to a plain asyncpg DSN.

    Strips dialect-specific ``+driver`` suffixes so the URL can be passed
    directly to ``asyncpg.create_pool()``.

    Examples:
        >>> sqlalchemy_url_to_asyncpg_dsn("postgresql+asyncpg://host/db")
        'postgresql://host/db'
        >>> sqlalchemy_url_to_asyncpg_dsn("postgresql+psycopg2://host/db")
        'postgresql://host/db'
        >>> sqlalchemy_url_to_asyncpg_dsn("postgresql://host/db")
        'postgresql://host/db'
    """
    return url.replace("+asyncpg", "").replace("+psycopg2", "")
