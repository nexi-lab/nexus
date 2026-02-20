"""Tests for nexus.core.db_utils (Issue #2195)."""

from __future__ import annotations

from nexus.core.db_utils import sqlalchemy_url_to_asyncpg_dsn


class TestSqlalchemyUrlToAsyncpgDsn:
    """Test DSN conversion utility."""

    def test_strips_asyncpg_driver(self) -> None:
        assert (
            sqlalchemy_url_to_asyncpg_dsn("postgresql+asyncpg://host/db") == "postgresql://host/db"
        )

    def test_strips_psycopg2_driver(self) -> None:
        assert (
            sqlalchemy_url_to_asyncpg_dsn("postgresql+psycopg2://host/db") == "postgresql://host/db"
        )

    def test_plain_url_unchanged(self) -> None:
        assert sqlalchemy_url_to_asyncpg_dsn("postgresql://host/db") == "postgresql://host/db"

    def test_sqlite_url_unchanged(self) -> None:
        assert sqlalchemy_url_to_asyncpg_dsn("sqlite:///test.db") == "sqlite:///test.db"

    def test_empty_string(self) -> None:
        assert sqlalchemy_url_to_asyncpg_dsn("") == ""

    def test_url_with_query_params(self) -> None:
        url = "postgresql+asyncpg://user:pass@host:5432/db?sslmode=require"
        expected = "postgresql://user:pass@host:5432/db?sslmode=require"
        assert sqlalchemy_url_to_asyncpg_dsn(url) == expected
