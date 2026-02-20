"""Unit tests for nexus.lib.db_utils.

Issue #2195: DSN conversion utility extracted from lifespan.
"""

from __future__ import annotations

from nexus.lib.db_utils import sqlalchemy_url_to_asyncpg_dsn


class TestSqlalchemyUrlToAsyncpgDsn:
    """Test sqlalchemy_url_to_asyncpg_dsn()."""

    def test_strips_asyncpg_driver(self) -> None:
        url = "postgresql+asyncpg://user:pass@host:5432/db"
        assert sqlalchemy_url_to_asyncpg_dsn(url) == "postgresql://user:pass@host:5432/db"

    def test_strips_psycopg2_driver(self) -> None:
        url = "postgresql+psycopg2://user:pass@host:5432/db"
        assert sqlalchemy_url_to_asyncpg_dsn(url) == "postgresql://user:pass@host:5432/db"

    def test_plain_url_unchanged(self) -> None:
        url = "postgresql://user:pass@host:5432/db"
        assert sqlalchemy_url_to_asyncpg_dsn(url) == url

    def test_sqlite_url_unchanged(self) -> None:
        url = "sqlite:///path/to/db.sqlite"
        assert sqlalchemy_url_to_asyncpg_dsn(url) == url

    def test_empty_string(self) -> None:
        assert sqlalchemy_url_to_asyncpg_dsn("") == ""

    def test_url_with_query_params(self) -> None:
        url = "postgresql+asyncpg://host/db?sslmode=require"
        assert sqlalchemy_url_to_asyncpg_dsn(url) == "postgresql://host/db?sslmode=require"
