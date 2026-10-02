"""Native PostgreSQL clients must receive a DSN they can parse without data loss."""

import pytest
from psycopg2.extensions import parse_dsn

from nexus.core.db_utils import sqlalchemy_url_to_postgres_dsn


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
