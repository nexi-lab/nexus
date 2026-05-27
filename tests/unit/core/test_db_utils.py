"""Tests for nexus.core.db_utils (Issue #2195, #4238)."""

from nexus.core.db_utils import (
    normalize_database_url,
    sqlalchemy_url_to_asyncpg_dsn,
)


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


class TestNormalizeDatabaseUrl:
    """Test ``postgres://`` → ``postgresql://`` rewrite (Issue #4238)."""

    def test_rewrites_postgres_scheme(self) -> None:
        assert (
            normalize_database_url("postgres://user:pass@host:5432/db")
            == "postgresql://user:pass@host:5432/db"
        )

    def test_postgresql_scheme_unchanged(self) -> None:
        assert (
            normalize_database_url("postgresql://user:pass@host/db")
            == "postgresql://user:pass@host/db"
        )

    def test_postgresql_asyncpg_unchanged(self) -> None:
        """Driver-suffixed URLs are not touched — they already pass SQLAlchemy."""
        assert (
            normalize_database_url("postgresql+asyncpg://host/db") == "postgresql+asyncpg://host/db"
        )

    def test_sqlite_unchanged(self) -> None:
        assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"

    def test_empty_string_unchanged(self) -> None:
        assert normalize_database_url("") == ""

    def test_none_passthrough(self) -> None:
        """Callers can pipe ``os.getenv(...)`` directly without a None-guard."""
        assert normalize_database_url(None) is None

    def test_only_first_occurrence_rewritten(self) -> None:
        """Password equal to 'postgres://' must not be mangled."""
        url = "postgres://u:postgres://@host/db"
        assert normalize_database_url(url) == "postgresql://u:postgres://@host/db"

    def test_query_params_preserved(self) -> None:
        url = "postgres://u:p@host:5432/db?sslmode=require"
        assert normalize_database_url(url) == "postgresql://u:p@host:5432/db?sslmode=require"
