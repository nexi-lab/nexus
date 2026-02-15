"""RecordStore: The "Truth" pillar of the Nexus Quartet.

Provides relational data storage for entities, relationships, logs, and vectors.
This is one of the Four Pillars (Metastore, RecordStore, ObjectStore, CacheStore).

OS Analogy: Windows Registry / Systemd state DB (but more structured).
Backing Tech: PostgreSQL (production) / SQLite (development).

RecordStore is NOT required by the Kernel core (inode CRUD only needs Metastore).
It is consumed by Services that currently live inside NexusFS:
- Identity & Auth: Users, OAuthAccounts
- Security (ReBAC): ReBACTuples, GroupClosures
- AI Memory: MemoryModel (Vectors), Trajectory, Playbook
- History: AuditLogs, VersionHistory, Workflows

Usage:
    # Production (PostgreSQL)
    record_store = SQLAlchemyRecordStore(db_url="postgresql://user:pass@host/db")

    # Development (SQLite)
    record_store = SQLAlchemyRecordStore(db_url="sqlite:///dev.db")

    # Kernel init (optional — only needed when Services are used)
    nx = NexusFS(metastore=metastore, record_store=record_store)
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import DBAPIConnection
    from sqlalchemy.pool import ConnectionPoolEntry

logger = logging.getLogger(__name__)


class RecordStoreABC(ABC):
    """Abstract base class for relational data storage (the "Truth" pillar).

    Provides SQL engine and session factory for relational data:
    Users, ReBAC, Audit, Memory (vectors), Workflows, Versioning, etc.

    The ABC exposes both sync and async interfaces, following the Linux VFS
    pattern where ``struct file_operations`` provides both ``read`` (sync)
    and ``read_iter`` (async-capable).  Concrete drivers implement both;
    callers choose based on context.

    Implementations must provide:
    - engine / session_factory: Synchronous SQLAlchemy access
    - async_session_factory: Asynchronous SQLAlchemy access (lazy default)
    """

    @property
    @abstractmethod
    def engine(self) -> Any:
        """SQLAlchemy engine for database operations."""
        ...

    @property
    @abstractmethod
    def session_factory(self) -> Any:
        """Session factory (sessionmaker) for creating database sessions."""
        ...

    @property
    def async_session_factory(self) -> Any:
        """Async session factory (async_sessionmaker) for async database sessions.

        Default raises ``NotImplementedError``.  Concrete drivers that support
        async access (e.g. SQLAlchemyRecordStore) override this with a lazy
        implementation that creates an async engine from the same DB URL.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support async_session_factory. "
            "Override this property to enable async database access."
        )

    @abstractmethod
    def close(self) -> None:
        """Close the store and release resources."""
        ...


class SQLAlchemyRecordStore(RecordStoreABC):
    """SQLAlchemy-based RecordStore for PostgreSQL and SQLite.

    Extracts the engine/session creation logic. This achieves separation of concerns:
    - MetastoreABC handles file metadata (ordered KV via sled)
    - RecordStoreABC handles relational data (SQL via PostgreSQL/SQLite)
    """

    def __init__(
        self,
        db_url: str | None = None,
        db_path: str | Path | None = None,
        *,
        create_tables: bool = True,
        creator: Any | None = None,
        async_creator: Any | None = None,
    ):
        """Initialize SQLAlchemy record store.

        Args:
            db_url: Database URL (e.g., 'postgresql://user:pass@host/db' or 'sqlite:///path')
                   If not provided, checks NEXUS_DATABASE_URL or POSTGRES_URL env vars,
                   then falls back to db_path parameter.
            db_path: Path to SQLite database file (fallback if db_url not provided).
            create_tables: If True (default), run Base.metadata.create_all on init.
                          Set to False in production when Alembic manages schema.
            creator: Optional callable for custom connection creation (e.g. Cloud SQL
                    Python Connector sync). Passed to ``create_engine(creator=...)``.
            async_creator: Optional callable for custom async connection creation
                          (e.g. Cloud SQL Python Connector async). Passed to
                          ``create_async_engine(async_creator=...)``.
        """
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker

        # Resolve database URL
        self.database_url = self._resolve_db_url(db_url, db_path)

        # Store async_creator for lazy async engine initialization
        self._async_creator = async_creator

        # Create engine with appropriate pool configuration
        engine_kwargs: dict[str, Any] = {}
        if self.database_url.startswith("sqlite"):
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        else:
            # PostgreSQL pool configuration — env vars override defaults (Issue #1299)
            pool_size = int(os.getenv("NEXUS_DB_POOL_SIZE", "20"))
            max_overflow = int(os.getenv("NEXUS_DB_MAX_OVERFLOW", "30"))
            pool_recycle = int(os.getenv("NEXUS_DB_POOL_RECYCLE", "1800"))
            engine_kwargs.update(
                {
                    "pool_size": pool_size,
                    "max_overflow": max_overflow,
                    "pool_pre_ping": True,
                    "pool_recycle": pool_recycle,
                }
            )

        # Pass creator for custom connection factories (e.g. Cloud SQL Connector)
        if creator is not None:
            engine_kwargs["creator"] = creator

        self._engine = create_engine(self.database_url, **engine_kwargs)

        # Enable WAL mode for SQLite (better concurrent read performance)
        if self.database_url.startswith("sqlite"):

            @event.listens_for(self._engine, "connect")
            def set_sqlite_pragma(
                dbapi_connection: DBAPIConnection, _connection_record: ConnectionPoolEntry
            ) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

        # Create session factory
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)

        # Async engine/session are created lazily on first access
        self._async_engine: Any = None
        self._async_session_factory_instance: Any = None

        # Create tables (skip in production when Alembic is SSOT)
        if create_tables:
            from nexus.storage.models import Base

            Base.metadata.create_all(self._engine)

        logger.info("SQLAlchemyRecordStore initialized: %s", self.database_url)

    @staticmethod
    def _resolve_db_url(db_url: str | None, db_path: str | Path | None) -> str:
        """Resolve database URL from parameters and environment."""
        if db_url:
            return db_url

        # Check environment variables
        env_url = os.getenv("NEXUS_DATABASE_URL") or os.getenv("POSTGRES_URL")
        if env_url:
            return env_url

        # Fall back to db_path (SQLite)
        if db_path:
            path = Path(db_path) if not isinstance(db_path, Path) else db_path
            if str(path).startswith("sqlite"):
                return str(path)
            return f"sqlite:///{path}"

        # Default: in-memory SQLite
        return "sqlite:///:memory:"

    @property
    def engine(self) -> Any:
        """SQLAlchemy engine."""
        return self._engine

    @property
    def session_factory(self) -> Any:
        """Session factory (sessionmaker)."""
        return self._session_factory

    # Alias for backward compatibility with existing code that uses SessionLocal
    @property
    def SessionLocal(self) -> Any:
        """Session factory (alias for session_factory, backward compat)."""
        return self._session_factory

    @property
    def async_session_factory(self) -> Any:
        """Lazily create async session factory from the same database URL.

        On first access, creates an async engine (``asyncpg`` for PostgreSQL,
        ``aiosqlite`` for SQLite) and an ``async_sessionmaker`` bound to it.
        Subsequent accesses reuse the same engine and session factory.

        When ``async_creator`` was provided at construction time, it is passed
        to ``create_async_engine`` so that connection creation goes through the
        custom factory (e.g. Cloud SQL Python Connector).
        """
        if self._async_session_factory_instance is None:
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

            async_url = self._to_async_url(self.database_url)

            engine_kwargs: dict[str, Any] = {}
            if "postgresql" in async_url:
                pool_size = int(os.getenv("NEXUS_DB_POOL_SIZE", "20"))
                max_overflow = int(os.getenv("NEXUS_DB_MAX_OVERFLOW", "30"))
                pool_recycle = int(os.getenv("NEXUS_DB_POOL_RECYCLE", "1800"))
                engine_kwargs.update(
                    {
                        "pool_size": pool_size,
                        "max_overflow": max_overflow,
                        "pool_pre_ping": True,
                        "pool_use_lifo": True,
                        "pool_recycle": pool_recycle,
                    }
                )

            # Pass async_creator for custom connection factories (e.g. Cloud SQL)
            if self._async_creator is not None:
                engine_kwargs["async_creator"] = self._async_creator

            self._async_engine = create_async_engine(async_url, **engine_kwargs)
            self._async_session_factory_instance = async_sessionmaker(
                self._async_engine, class_=AsyncSession, expire_on_commit=False
            )
            logger.info("Async session factory initialized: %s", async_url.split("@")[-1])

        return self._async_session_factory_instance

    @staticmethod
    def _to_async_url(sync_url: str) -> str:
        """Convert a synchronous database URL to its async driver variant."""
        if sync_url.startswith("postgresql://"):
            return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if sync_url.startswith("sqlite:///"):
            return sync_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        return sync_url

    def close(self) -> None:
        """Close all engines and release connections."""
        self._engine.dispose()
        if self._async_engine is not None:
            # Async engine dispose is sync-safe in SQLAlchemy 2.0+
            self._async_engine.sync_engine.dispose()
            self._async_engine = None
            self._async_session_factory_instance = None
        logger.info("SQLAlchemyRecordStore closed")
