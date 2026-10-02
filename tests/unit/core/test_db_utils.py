"""Native PostgreSQL clients must receive a DSN they can parse without data loss."""

import pytest
from psycopg2.extensions import parse_dsn
from sqlalchemy import create_engine

from nexus.core.db_utils import normalize_database_url, sqlalchemy_url_to_postgres_dsn


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg2"])
def test_default_postgres_engine_uses_installed_driver(scheme: str) -> None:
    url = (
        f"{scheme}://demo:p%40ss+psycopg2@localhost:15488/nexus"
        "?application_name=demo+psycopg2&sslmode=require"
    )
    normalized = normalize_database_url(url)
    engine = create_engine(normalized)
    try:
        assert engine.dialect.driver == "psycopg2"
        assert engine.dialect.dbapi.__name__ == "psycopg2"
        assert engine.url.password == "p@ss+psycopg2"
        assert engine.url.query == {"application_name": "demo psycopg2", "sslmode": "require"}
        assert parse_dsn(sqlalchemy_url_to_postgres_dsn(normalized)) == parse_dsn(
            sqlalchemy_url_to_postgres_dsn(url)
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "sqlite:///cache+psycopg2.db",
        "postgresql+asyncpg://host/db",
        "postgresql+psycopg://host/db",
        "postgresql+pg8000://host/db",
    ],
)
def test_normalize_preserves_explicit_driver_and_non_postgres_urls(url: str | None) -> None:
    assert normalize_database_url(url) == url


@pytest.mark.parametrize(
    "scheme", ["postgres", "postgresql", "postgresql+psycopg2", "postgresql+asyncpg"]
)
def test_native_postgres_dsn_preserves_connection_parameters(scheme: str) -> None:
    url = (
        f"{scheme}://demo:p%40ss+psycopg2+asyncpg@localhost:15488/nexus"
        "?application_name=demo+psycopg2&sslmode=require"
    )
    parsed = parse_dsn(sqlalchemy_url_to_postgres_dsn(url))

    assert parsed == {
        "user": "demo",
        "password": "p@ss+psycopg2+asyncpg",
        "host": "localhost",
        "port": "15488",
        "dbname": "nexus",
        "application_name": "demo+psycopg2",
        "sslmode": "require",
    }


def test_non_postgres_url_is_unchanged() -> None:
    url = "sqlite:///cache+psycopg2.db"
    assert sqlalchemy_url_to_postgres_dsn(url) == url
