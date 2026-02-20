"""Record store factory — create_record_store with Cloud SQL support."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


def create_record_store(
    *,
    db_url: str | None = None,
    db_path: str | None = None,
    create_tables: bool = True,
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> RecordStoreABC:
    """Create a RecordStore with Cloud SQL and read replica support auto-detected from env.

    When the ``CLOUD_SQL_INSTANCE`` environment variable is set, the
    Cloud SQL Python Connector is used for IAM-authenticated connections
    (no passwords, no public IP).  Otherwise, the standard URL-based
    connection path is used.

    Read replica support (Issue #725):
    - ``NEXUS_READ_REPLICA_URL``: Standard read replica connection string
    - ``CLOUD_SQL_READ_INSTANCE``: Cloud SQL read replica instance

    Args:
        db_url: Explicit database URL. Falls back to env vars.
        db_path: SQLite path (development only).
        create_tables: If True, run ``create_all`` on init. Set False
            in production when Alembic is the schema SSOT.

    Returns:
        Fully initialized ``SQLAlchemyRecordStore``.
    """
    import os

    from nexus.storage.record_store import SQLAlchemyRecordStore

    read_replica_url = os.getenv("NEXUS_READ_REPLICA_URL")

    cloud_sql_instance = os.getenv("CLOUD_SQL_INSTANCE")
    if cloud_sql_instance:
        from nexus.storage.cloud_sql import create_cloud_sql_creators

        sync_creator, async_creator = create_cloud_sql_creators(
            instance_connection_name=cloud_sql_instance,
            db_user=os.getenv("CLOUD_SQL_USER", "nexus"),
            db_name=os.getenv("CLOUD_SQL_DB", "nexus"),
        )

        # Cloud SQL read replica support (Issue #725)
        read_replica_creator = None
        async_read_replica_creator = None
        cloud_sql_read_instance = os.getenv("CLOUD_SQL_READ_INSTANCE")
        if cloud_sql_read_instance:
            read_sync, read_async = create_cloud_sql_creators(
                instance_connection_name=cloud_sql_read_instance,
                db_user=os.getenv("CLOUD_SQL_USER", "nexus"),
                db_name=os.getenv("CLOUD_SQL_DB", "nexus"),
            )
            read_replica_creator = read_sync
            async_read_replica_creator = read_async
            # Use placeholder URL for read replica engine
            read_replica_url = read_replica_url or "postgresql://"

        return SQLAlchemyRecordStore(
            db_url=db_url or "postgresql://",  # placeholder, creator overrides
            create_tables=create_tables,
            creator=sync_creator,
            async_creator=async_creator,
            read_replica_url=read_replica_url,
            read_replica_creator=read_replica_creator,
            async_read_replica_creator=async_read_replica_creator,
            pool_size=pool_size,
            max_overflow=max_overflow,
        )

    return SQLAlchemyRecordStore(
        db_url=db_url,
        db_path=db_path,
        create_tables=create_tables,
        read_replica_url=read_replica_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )
