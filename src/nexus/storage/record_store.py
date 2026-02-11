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

    Implementations must provide:
    - engine: SQLAlchemy engine for creating connections
    - SessionLocal: Session factory for creating database sessions
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
    ):
        """Initialize SQLAlchemy record store.

        Args:
            db_url: Database URL (e.g., 'postgresql://user:pass@host/db' or 'sqlite:///path')
                   If not provided, checks NEXUS_DATABASE_URL or POSTGRES_URL env vars,
                   then falls back to db_path parameter.
            db_path: Path to SQLite database file (fallback if db_url not provided).
        """
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker

        from nexus.storage.models import Base

        # Resolve database URL
        self.database_url = self._resolve_db_url(db_url, db_path)

        # Create engine with appropriate pool configuration
        engine_kwargs: dict[str, Any] = {}
        if self.database_url.startswith("sqlite"):
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        else:
            # PostgreSQL pool configuration (Issue #1246, Decision 16A)
            engine_kwargs.update(
                {
                    "pool_size": 5,  # Baseline connections
                    "max_overflow": 10,  # Burst capacity (total max = 15)
                    "pool_pre_ping": True,  # Detect stale connections
                    "pool_recycle": 1800,  # Recycle connections every 30min
                }
            )

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

        # Create tables
        Base.metadata.create_all(self._engine)

        logger.info(f"SQLAlchemyRecordStore initialized: {self.database_url}")

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

    def close(self) -> None:
        """Close the engine and release connections."""
        self._engine.dispose()
        logger.info("SQLAlchemyRecordStore closed")
