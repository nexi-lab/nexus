"""Factory for Cloud SQL Python Connector connection creators.

Provides sync (pg8000) and async (asyncpg) connection creators for use
with SQLAlchemy's ``create_engine(creator=...)`` /
``create_async_engine(creator=...)``.

Usage::

    sync_creator, async_creator = create_cloud_sql_creators(
        instance_connection_name="project:region:instance",
        db_user="nexus",
        db_name="nexus",
    )

    engine = create_engine("postgresql+pg8000://", creator=sync_creator)
    async_engine = create_async_engine("postgresql+asyncpg://", creator=async_creator)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def create_cloud_sql_creators(
    instance_connection_name: str,
    db_user: str,
    db_name: str,
) -> tuple[Callable[[], Any], Callable[[], Any]]:
    """Return ``(sync_creator, async_creator)`` for Cloud SQL Python Connector.

    Args:
        instance_connection_name: Cloud SQL instance in ``project:region:instance`` format.
        db_user: Database user (IAM or built-in).
        db_name: Target database name.

    Returns:
        A 2-tuple of callables suitable for SQLAlchemy engine ``creator`` kwarg.
    """
    from google.cloud.sql.connector import Connector

    connector = Connector()

    def sync_creator() -> Any:
        """Create a pg8000 connection via Cloud SQL Connector."""
        return connector.connect(instance_connection_name, "pg8000", user=db_user, db=db_name)

    async def async_creator() -> Any:
        """Create an asyncpg connection via Cloud SQL Connector."""
        return await connector.connect_async(
            instance_connection_name, "asyncpg", user=db_user, db=db_name
        )

    return sync_creator, async_creator
