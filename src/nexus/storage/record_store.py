"""RecordStore: The "Truth" pillar of the Nexus Quartet.

Provides relational data storage for entities, relationships, logs, and vectors.
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

    # Production with read replica
    record_store = SQLAlchemyRecordStore(
        db_url="postgresql://user:pass@primary/db",
        read_replica_url="postgresql://user:pass@replica/db",
    )

    # Kernel init (optional — only needed when Services are used)
    nx = NexusFS(metastore=metastore, record_store=record_store)
"""

import logging
import os
import threading
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import DBAPIConnection
    from sqlalchemy.orm import Session, sessionmaker
    from sqlalchemy.pool import ConnectionPoolEntry

logger = logging.getLogger(__name__)


def _handle_asyncpg_protocol_error(ctx: Any) -> None:
    """Mark asyncpg.InternalClientError as a disconnect so the pool discards the conn.

    Backports SQLAlchemy #13241 (unreleased as of 2.0.49) and addresses issue #3807.
    asyncpg raises InternalClientError ("got result for unknown protocol state N" or
    "cannot switch to state X; another operation (Y) is in progress") when a query
    races with task cancellation or a server-side session termination. SQLAlchemy's
    asyncpg dialect does not classify these as disconnects, so the poisoned
    connection is returned to the pool and the next checkout cascades into either
    a hang or the same error. Setting ``is_disconnect = True`` forces pool
    invalidation and a fresh connect on next checkout.
    """
    try:
        import asyncpg
    except ImportError:
        return

    if isinstance(ctx.original_exception, asyncpg.InternalClientError):
        ctx.is_disconnect = True


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

    Read replica support (Issue #725):
    - read_engine / read_session_factory: Default to primary engine
    - async_read_session_factory: Default to primary async session factory
    - has_read_replica: Whether a separate read replica is configured
    """

    @property
    @abstractmethod
    def engine(self) -> Any:
        """SQLAlchemy engine for database operations (primary/write)."""
        ...

    @property
    @abstractmethod
    def session_factory(self) -> "sessionmaker[Session]":
        """Session factory (sessionmaker) for creating database sessions (primary/write)."""
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

    @property
    def async_engine(self) -> Any:
        """Async SQLAlchemy engine, or None if async is not supported.

        Concrete drivers that lazily create an async engine (e.g.
        SQLAlchemyRecordStore) should override this property.
        Used by cache brick and other consumers that need non-blocking I/O.
        """
        return None

    # -- Read replica properties (Issue #725) --

    @property
    def read_engine(self) -> Any:
        """Engine for read-only operations. Defaults to primary."""
        return self.engine

    @property
    def read_session_factory(self) -> Any:
        """Session factory for read-only ops. Defaults to primary."""
        return self.session_factory

    @property
    def async_read_session_factory(self) -> Any:
        """Async session factory for read-only ops. Defaults to primary."""
        return self.async_session_factory

    @property
    def has_read_replica(self) -> bool:
        """Whether a separate read replica is configured."""
        return False

    @contextmanager
    def session(self) -> "Generator[Session, None, None]":
        """Transactional session scope with Nexus error translation.

        Delegates to session_scope() for commit/rollback/close + error mapping.
        """
        from nexus.storage.session_scope import session_scope

        with session_scope(self.session_factory) as sess:
            yield sess

    @contextmanager
    def read_session(self) -> "Generator[Session, None, None]":
        """Read-only session using read replica (falls back to primary).

        Uses read_session_factory when a read replica is configured.
        """
        from nexus.storage.session_scope import session_scope

        with session_scope(self.read_session_factory) as sess:
            yield sess

    @abstractmethod
    def close(self) -> None:
        """Close the store and release resources."""
        ...


class SQLAlchemyRecordStore(RecordStoreABC):
    """SQLAlchemy-based RecordStore for PostgreSQL and SQLite.

    Extracts the engine/session creation logic. This achieves separation of concerns:
    - MetastoreABC handles file metadata (ordered KV via sled)
    - RecordStoreABC handles relational data (SQL via PostgreSQL/SQLite)

    Supports optional read replica for PostgreSQL (Issue #725):
    - ~88% of DB traffic is reads (ReBAC permission checks dominate at ~55%)
    - When ``read_replica_url`` is provided, read-only operations use a
      separate connection pool pointed at a PostgreSQL read replica
    - SQLite ignores read_replica_url (single-file DB has no replicas)
    """

    def __init__(
        self,
        db_url: str | None = None,
        db_path: str | Path | None = None,
        *,
        create_tables: bool = True,
        creator: Any | None = None,
        async_creator: Any | None = None,
        read_replica_url: str | None = None,
        read_replica_creator: Any | None = None,
        async_read_replica_creator: Any | None = None,
        pool_size: int | None = None,
        max_overflow: int | None = None,
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
            read_replica_url: Optional read replica database URL (Issue #725).
                            Only used for PostgreSQL. SQLite ignores this parameter.
            read_replica_creator: Optional callable for custom read replica connection
                                creation (e.g. Cloud SQL read instance).
            async_read_replica_creator: Optional callable for custom async read replica
                                      connection creation.
            pool_size: Override default pool size from ProfileTuning.storage.db_pool_size.
                      When None, uses _build_pool_kwargs defaults (20 primary, 10 with replica).
            max_overflow: Override default max overflow from ProfileTuning.storage.db_max_overflow.
                         When None, uses _build_pool_kwargs defaults (30 primary, 10 with replica).
        """
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker

        # Resolve database URL
        _resolved = self._resolve_db_url(db_url, db_path)
        # Auto-convert async driver URL to sync driver.  Many callers pass
        # NEXUS_DATABASE_URL which uses ``postgresql+asyncpg://``, but this
        # store uses a synchronous ``create_engine``.
        self.database_url = (
            _resolved.replace("postgresql+asyncpg://", "postgresql://") if _resolved else _resolved
        )

        # Store creators for lazy async engine initialization
        self._async_creator = async_creator
        self._async_read_replica_creator = async_read_replica_creator

        # Determine if this is a PostgreSQL database
        self._is_postgresql = not self.database_url.startswith("sqlite")

        # Determine if read replica should be used (PostgreSQL only)
        self._read_replica_url = read_replica_url if self._is_postgresql else None
        self._has_read_replica = self._read_replica_url is not None

        # Create primary engine with appropriate pool configuration
        engine_kwargs: dict[str, Any] = {}
        if not self._is_postgresql:
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        else:
            _default_pool = (
                pool_size if pool_size is not None else (10 if self._has_read_replica else 20)
            )
            _default_overflow = (
                max_overflow if max_overflow is not None else (10 if self._has_read_replica else 30)
            )
            engine_kwargs.update(
                self._build_pool_kwargs(
                    prefix="NEXUS_DB",
                    is_async=False,
                    default_pool_size=_default_pool,
                    default_max_overflow=_default_overflow,
                )
            )

        # Pass creator for custom connection factories (e.g. Cloud SQL Connector)
        if creator is not None:
            engine_kwargs["creator"] = creator

        self._engine = create_engine(self.database_url, **engine_kwargs)

        # Set plan_cache_mode at pool level for PostgreSQL (Issue #14, #683)
        if self._is_postgresql:
            self._attach_plan_cache_mode_listener(self._engine)

        # Enable WAL mode for SQLite (better concurrent read performance)
        if not self._is_postgresql:

            @event.listens_for(self._engine, "connect")
            def set_sqlite_pragma(
                dbapi_connection: "DBAPIConnection", _connection_record: "ConnectionPoolEntry"
            ) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

        # Create primary session factory
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)

        # Async engine/session are created lazily on first access
        self._async_engine: Any = None
        self._async_session_factory_instance: Any = None
        self._async_init_lock = threading.Lock()

        # Read replica engine/session (Issue #725)
        self._read_engine: Any = None
        self._read_session_factory_instance: Any = None
        self._async_read_engine: Any = None
        self._async_read_session_factory_instance: Any = None
        self._async_read_init_lock = threading.Lock()

        if self._has_read_replica:
            read_engine_kwargs: dict[str, Any] = self._build_pool_kwargs(
                prefix="NEXUS_READ_REPLICA",
                is_async=False,
                default_pool_size=20,
                default_max_overflow=25,
            )
            if read_replica_creator is not None:
                read_engine_kwargs["creator"] = read_replica_creator

            assert self._read_replica_url is not None  # guarded by _has_read_replica
            self._read_engine = create_engine(self._read_replica_url, **read_engine_kwargs)
            self._attach_plan_cache_mode_listener(self._read_engine)
            self._read_session_factory_instance = sessionmaker(
                bind=self._read_engine, expire_on_commit=False
            )
            _replica_host = (
                self._read_replica_url.split("@")[-1] if "@" in self._read_replica_url else "***"
            )
            logger.info(
                "Read replica engine initialized: %s (pool_size=%s, max_overflow=%s)",
                _replica_host,
                read_engine_kwargs.get("pool_size"),
                read_engine_kwargs.get("max_overflow"),
            )

        # Create tables (skip in production when Alembic is SSOT)
        if create_tables:
            from nexus.storage.models import Base
            from nexus.storage.schema_invariants import ensure_postgres_schema_invariants
            from nexus.storage.zone_bootstrap import ensure_root_zone

            Base.metadata.create_all(self._engine)
            ensure_postgres_schema_invariants(self._engine)
            # Issue #3897: every install must contain zones.root before
            # the first create_api_key call (writes api_key_zones with
            # FK to zones). Alembic's migration handles persistent
            # installs; this covers create_all paths used by CLI tooling,
            # tests, and `nexus hub` flows.
            ensure_root_zone(self.session_factory)

        logger.info(
            "SQLAlchemyRecordStore initialized: %s (read_replica=%s)",
            self.database_url,
            self._has_read_replica,
        )

    @staticmethod
    def _build_pool_kwargs(
        *,
        prefix: str,
        is_async: bool,
        default_pool_size: int = 20,
        default_max_overflow: int = 30,
        default_pool_recycle: int = 1800,
    ) -> dict[str, Any]:
        """Build pool configuration kwargs from environment variables.

        Args:
            prefix: Env var prefix (e.g. 'NEXUS_DB' -> NEXUS_DB_POOL_SIZE).
            is_async: If True, add pool_use_lifo=True for async engines.
            default_pool_size: Default pool size if env var not set.
            default_max_overflow: Default max overflow if env var not set.
            default_pool_recycle: Default pool recycle seconds if env var not set.

        Returns:
            Dict of pool kwargs suitable for create_engine/create_async_engine.
        """
        pool_size = int(os.getenv(f"{prefix}_POOL_SIZE", str(default_pool_size)))
        max_overflow = int(os.getenv(f"{prefix}_MAX_OVERFLOW", str(default_max_overflow)))
        pool_recycle = int(os.getenv(f"{prefix}_POOL_RECYCLE", str(default_pool_recycle)))

        kwargs: dict[str, Any] = {
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "pool_pre_ping": True,
            "pool_recycle": pool_recycle,
        }
        if is_async:
            kwargs["pool_use_lifo"] = True
        return kwargs

    @staticmethod
    def _build_async_pool_kwargs(
        *,
        prefix: str = "NEXUS_DB",
        default_pool_size: int = 20,
        default_max_overflow: int = 30,
    ) -> dict[str, Any]:
        """Pool kwargs for the asyncpg engine.

        Uses ``NullPool`` to eliminate the class of cross-event-loop corruption
        bugs (issues #3807, #3775): with asyncpg, a cached Connection is tied
        to the event loop that opened it. Reusing that connection from a
        different loop (PortalRunner vs uvicorn vs SearchDaemon) triggers
        ``Future attached to a different loop`` errors, which poison the pool.
        NullPool opens a fresh connection on the current loop per checkout and
        closes it on return, so state never leaks across loops.

        Trade-off: without pool_size / max_overflow, bursty async traffic can
        open up to one connection per concurrent request. Operators must
        size PostgreSQL's ``max_connections`` (and any upstream pooler like
        PgBouncer) to at least ``threadpool_size × replicas + headroom``.
        Under-provisioning manifests as ``asyncpg.TooManyConnectionsError``
        — observable in logs/metrics and fixable by raising PG limits or
        opting into the bounded pool below. The previous behaviour (pool
        corruption under cancellation) was silent.

        Override with ``<prefix>_ASYNC_USE_POOL=1`` to restore the pooled
        queue (for single-loop deployments where the overhead matters). The
        primary engine's toggle is ``NEXUS_DB_ASYNC_USE_POOL``; the
        read-replica's is ``NEXUS_READ_REPLICA_ASYNC_USE_POOL``. The primary
        toggle is also honoured as a fallback so existing deployments that
        only set ``NEXUS_DB_ASYNC_USE_POOL`` behave unchanged.

        Pool-size / max-overflow / recycle are read from ``<prefix>_POOL_*``
        when pooled mode is active.
        """
        _truthy = ("1", "true", "yes")
        _falsy = ("0", "false", "no", "")
        # Prefix-specific toggle takes precedence. Only consult the legacy
        # global toggle when the prefix-specific one is genuinely absent,
        # so operators can explicitly disable pooled mode on the replica
        # with ``NEXUS_READ_REPLICA_ASYNC_USE_POOL=0`` even if
        # ``NEXUS_DB_ASYNC_USE_POOL=1`` is set. Whitespace is stripped.
        _prefix_raw = os.environ.get(f"{prefix}_ASYNC_USE_POOL")
        if _prefix_raw is not None:
            _val = _prefix_raw.strip().lower()
            if _val in _truthy:
                _use_pool = True
            elif _val in _falsy:
                _use_pool = False
            else:
                # Present but invalid — fail closed to the safe default.
                logger.warning(
                    "%s_ASYNC_USE_POOL=%r is not a recognized boolean "
                    "(expected 1/0/true/false/yes/no); defaulting to NullPool.",
                    prefix,
                    _prefix_raw,
                )
                _use_pool = False
        else:
            _global_val = os.environ.get("NEXUS_DB_ASYNC_USE_POOL", "").strip().lower()
            _use_pool = _global_val in _truthy
        if _use_pool:
            return SQLAlchemyRecordStore._build_pool_kwargs(
                prefix=prefix,
                is_async=True,
                default_pool_size=default_pool_size,
                default_max_overflow=default_max_overflow,
            )
        from sqlalchemy.pool import NullPool

        return {"poolclass": NullPool}

    @staticmethod
    def _attach_asyncpg_protocol_error_handler(engine: Any) -> None:
        """Attach a handle_error listener that invalidates on asyncpg protocol errors.

        Must be attached to a sync Engine (or an AsyncEngine's ``sync_engine``).
        Paired with :func:`_handle_asyncpg_protocol_error` above.
        """
        from sqlalchemy import event

        event.listen(engine, "handle_error", _handle_asyncpg_protocol_error)

    @staticmethod
    def _attach_plan_cache_mode_listener(engine: Any) -> None:
        """Attach a pool-level listener to SET plan_cache_mode on connect.

        Moved from per-checkout to per-connect for efficiency (Issue #14).
        This fixes PostgreSQL prepared statement performance issues (#683).
        """
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _set_plan_cache_mode(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET plan_cache_mode = 'force_custom_plan'")
            except Exception:
                logger.debug("plan_cache_mode not supported on this connection", exc_info=True)
            finally:
                cursor.close()

    @staticmethod
    def _resolve_db_url(db_url: str | None, db_path: str | Path | None) -> str:
        """Resolve database URL from parameters and environment."""
        if db_url:
            return db_url

        # Check environment variables
        from nexus.lib.env import get_database_url

        env_url = get_database_url()
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
        """SQLAlchemy engine (primary/write)."""
        return self._engine

    @property
    def session_factory(self) -> Any:
        """Session factory (sessionmaker) for primary/write operations."""
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
            with self._async_init_lock:
                # Double-check after acquiring lock
                if self._async_session_factory_instance is None:
                    from sqlalchemy.ext.asyncio import (
                        AsyncSession,
                        async_sessionmaker,
                        create_async_engine,
                    )

                    async_url = self._to_async_url(self.database_url)

                    engine_kwargs: dict[str, Any] = {}
                    if "postgresql" in async_url:
                        engine_kwargs.update(
                            self._build_async_pool_kwargs(
                                prefix="NEXUS_DB",
                                default_pool_size=10 if self._has_read_replica else 20,
                                default_max_overflow=10 if self._has_read_replica else 30,
                            )
                        )

                    # Pass async_creator for custom connection factories (e.g. Cloud SQL)
                    if self._async_creator is not None:
                        engine_kwargs["async_creator"] = self._async_creator

                    self._async_engine = create_async_engine(async_url, **engine_kwargs)

                    # Set plan_cache_mode on async engine for PostgreSQL
                    if self._is_postgresql:
                        self._attach_plan_cache_mode_listener(self._async_engine.sync_engine)
                        # Issue #3807: force pool discard on asyncpg InternalClientError
                        self._attach_asyncpg_protocol_error_handler(self._async_engine.sync_engine)

                    self._async_session_factory_instance = async_sessionmaker(
                        self._async_engine, class_=AsyncSession, expire_on_commit=False
                    )
                    pool_info = {k: v for k, v in engine_kwargs.items() if k.startswith("pool_")}
                    _poolclass = engine_kwargs.get("poolclass")
                    _strategy = (
                        _poolclass.__name__
                        if _poolclass is not None
                        else (str(pool_info) if pool_info else "default")
                    )
                    _is_pg = "postgresql" in async_url
                    if _strategy == "NullPool" and _is_pg:
                        _advisory = (
                            " NullPool opens a fresh connection per checkout to prevent "
                            "cross-event-loop corruption (#3807); ensure PostgreSQL "
                            "max_connections covers peak concurrent async traffic."
                        )
                    elif _is_pg and pool_info:
                        _advisory = (
                            " Pooled mode active (NEXUS_DB_ASYNC_USE_POOL=1); "
                            "connections may be reused across event loops — "
                            "monitor for asyncpg InternalClientError under burst load (#3807)."
                        )
                    else:
                        _advisory = ""
                    logger.info(
                        "Async session factory initialized: %s (strategy=%s, pool_kwargs=%s).%s",
                        async_url.split("@")[-1],
                        _strategy,
                        pool_info or "{}",
                        _advisory,
                    )

        return self._async_session_factory_instance

    @property
    def async_engine(self) -> Any:
        """Async SQLAlchemy engine (lazily created on first async_session_factory access).

        Returns None if async_session_factory has not been accessed yet.
        Accessing async_session_factory triggers lazy creation of this engine.
        """
        return self._async_engine

    # -- Read replica properties (Issue #725) --

    @property
    def read_engine(self) -> Any:
        """Engine for read-only operations. Returns replica if configured, else primary."""
        if self._read_engine is not None:
            return self._read_engine
        return self._engine

    @property
    def read_session_factory(self) -> Any:
        """Session factory for read-only ops. Returns replica if configured, else primary."""
        if self._read_session_factory_instance is not None:
            return self._read_session_factory_instance
        return self._session_factory

    @property
    def async_read_session_factory(self) -> Any:
        """Async session factory for read-only ops.

        Lazily creates an async engine for the read replica on first access.
        Falls back to the primary async session factory if no replica configured.
        """
        if not self._has_read_replica:
            return self.async_session_factory

        if self._async_read_session_factory_instance is None:
            with self._async_read_init_lock:
                # Double-check after acquiring lock
                if self._async_read_session_factory_instance is None:
                    from sqlalchemy.ext.asyncio import (
                        AsyncSession,
                        async_sessionmaker,
                        create_async_engine,
                    )

                    assert self._read_replica_url is not None  # guarded by _has_read_replica
                    async_url = self._to_async_url(self._read_replica_url)
                    engine_kwargs = self._build_async_pool_kwargs(
                        prefix="NEXUS_READ_REPLICA",
                        default_pool_size=20,
                        default_max_overflow=25,
                    )

                    if self._async_read_replica_creator is not None:
                        engine_kwargs["async_creator"] = self._async_read_replica_creator

                    self._async_read_engine = create_async_engine(async_url, **engine_kwargs)

                    # Set plan_cache_mode on async read replica engine for PostgreSQL
                    if self._is_postgresql:
                        self._attach_plan_cache_mode_listener(self._async_read_engine.sync_engine)
                        # Issue #3807: force pool discard on asyncpg InternalClientError
                        self._attach_asyncpg_protocol_error_handler(
                            self._async_read_engine.sync_engine
                        )

                    self._async_read_session_factory_instance = async_sessionmaker(
                        self._async_read_engine, class_=AsyncSession, expire_on_commit=False
                    )
                    pool_info = {k: v for k, v in engine_kwargs.items() if k.startswith("pool_")}
                    _poolclass = engine_kwargs.get("poolclass")
                    _strategy = (
                        _poolclass.__name__
                        if _poolclass is not None
                        else (str(pool_info) if pool_info else "default")
                    )
                    logger.info(
                        "Async read session factory initialized: %s (strategy=%s, pool_kwargs=%s).",
                        async_url.split("@")[-1],
                        _strategy,
                        pool_info or "{}",
                    )

        return self._async_read_session_factory_instance

    @property
    def has_read_replica(self) -> bool:
        """Whether a separate read replica is configured."""
        return self._has_read_replica

    @staticmethod
    def _to_async_url(sync_url: str) -> str:
        """Convert a synchronous database URL to its async driver variant.

        Handles multiple PostgreSQL driver prefixes and SQLite variants.

        Raises:
            ValueError: If the URL scheme is not recognized.
        """
        if sync_url.startswith("postgresql+asyncpg://"):
            return sync_url  # Already async
        if sync_url.startswith("postgresql+psycopg2://"):
            return sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
        if sync_url.startswith("postgresql+pg8000://"):
            return sync_url.replace("postgresql+pg8000://", "postgresql+asyncpg://", 1)
        if sync_url.startswith("postgresql://"):
            return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if sync_url.startswith("sqlite+aiosqlite://"):
            return sync_url  # Already async
        if sync_url.startswith("sqlite:///"):
            return sync_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        if sync_url.startswith("sqlite://"):
            return sync_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        raise ValueError(
            f"Unrecognized database URL scheme: {sync_url.split('://')[0]}://. "
            "Supported: postgresql://, postgresql+psycopg2://, postgresql+pg8000://, "
            "sqlite:///, sqlite://"
        )

    def close(self) -> None:
        """Close all engines and release connections."""
        self._engine.dispose()
        if self._async_engine is not None:
            # Async engine dispose is sync-safe in SQLAlchemy 2.0+
            self._async_engine.sync_engine.dispose()
            self._async_engine = None
            self._async_session_factory_instance = None

        # Dispose read replica engines (Issue #725)
        if self._read_engine is not None:
            self._read_engine.dispose()
            self._read_engine = None
            self._read_session_factory_instance = None
        if self._async_read_engine is not None:
            self._async_read_engine.sync_engine.dispose()
            self._async_read_engine = None
            self._async_read_session_factory_instance = None

        logger.info("SQLAlchemyRecordStore closed")
