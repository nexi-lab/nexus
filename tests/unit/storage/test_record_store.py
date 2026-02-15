"""Unit tests for SQLAlchemyRecordStore (Issue #1299).

Tests cover: URL resolution, pool config via env vars, create_tables flag,
creator/async_creator pass-through, async URL conversion, and lifecycle.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestRecordStoreURLResolution:
    """Tests for database URL resolution logic."""

    def test_creates_sqlite_engine_by_default(self):
        """No URL → in-memory SQLite."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore()
        assert store.database_url == "sqlite:///:memory:"
        store.close()

    def test_creates_postgresql_engine_from_url(self):
        """Explicit postgresql:// URL is used as-is."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        url = "postgresql://user:pass@localhost/testdb"
        # Will fail to connect but the URL should be stored correctly
        store = SQLAlchemyRecordStore.__new__(SQLAlchemyRecordStore)
        resolved = store._resolve_db_url(url, None)
        assert resolved == url

    def test_resolves_url_from_env_var_nexus_database_url(self, monkeypatch):
        """NEXUS_DATABASE_URL takes priority over POSTGRES_URL."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://env-nexus/db")
        monkeypatch.setenv("POSTGRES_URL", "postgresql://env-pg/db")

        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore.__new__(SQLAlchemyRecordStore)
        resolved = store._resolve_db_url(None, None)
        assert resolved == "postgresql://env-nexus/db"

    def test_resolves_url_from_env_var_postgres_url(self, monkeypatch):
        """Fallback to POSTGRES_URL when NEXUS_DATABASE_URL is not set."""
        monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
        monkeypatch.setenv("POSTGRES_URL", "postgresql://env-pg/db")

        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore.__new__(SQLAlchemyRecordStore)
        resolved = store._resolve_db_url(None, None)
        assert resolved == "postgresql://env-pg/db"

    def test_db_path_converts_to_sqlite_url(self):
        """db_path='/tmp/test.db' → 'sqlite:////tmp/test.db'."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore.__new__(SQLAlchemyRecordStore)
        resolved = store._resolve_db_url(None, "/tmp/test.db")
        assert resolved == "sqlite:////tmp/test.db"

    def test_db_url_takes_priority_over_env_vars(self, monkeypatch):
        """Explicit db_url always wins."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://env/db")

        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore.__new__(SQLAlchemyRecordStore)
        resolved = store._resolve_db_url("sqlite:///:memory:", None)
        assert resolved == "sqlite:///:memory:"


class TestRecordStorePoolConfig:
    """Tests for connection pool configuration via environment variables."""

    def test_pool_config_reads_from_env_vars(self, monkeypatch):
        """Pool size/overflow/recycle read from NEXUS_DB_* env vars."""
        monkeypatch.setenv("NEXUS_DB_POOL_SIZE", "10")
        monkeypatch.setenv("NEXUS_DB_MAX_OVERFLOW", "15")
        monkeypatch.setenv("NEXUS_DB_POOL_RECYCLE", "900")

        from nexus.storage.record_store import SQLAlchemyRecordStore

        # Use a PostgreSQL URL so pool config is applied
        with patch("nexus.storage.record_store.SQLAlchemyRecordStore._resolve_db_url") as mock_url:
            mock_url.return_value = "postgresql://test:test@localhost/test"
            with patch("sqlalchemy.create_engine") as mock_create:
                mock_create.return_value = MagicMock()
                with patch("sqlalchemy.orm.sessionmaker") as mock_sm:
                    mock_sm.return_value = MagicMock()
                    SQLAlchemyRecordStore(
                        db_url="postgresql://test:test@localhost/test",
                        create_tables=False,
                    )

                    # Verify pool kwargs passed to create_engine
                    call_kwargs = mock_create.call_args[1]
                    assert call_kwargs["pool_size"] == 10
                    assert call_kwargs["max_overflow"] == 15
                    assert call_kwargs["pool_recycle"] == 900
                    assert call_kwargs["pool_pre_ping"] is True

    def test_pool_defaults_when_no_env_vars(self, monkeypatch):
        """Default pool_size=20, max_overflow=30 when env vars not set."""
        monkeypatch.delenv("NEXUS_DB_POOL_SIZE", raising=False)
        monkeypatch.delenv("NEXUS_DB_MAX_OVERFLOW", raising=False)
        monkeypatch.delenv("NEXUS_DB_POOL_RECYCLE", raising=False)

        from nexus.storage.record_store import SQLAlchemyRecordStore

        with patch("sqlalchemy.create_engine") as mock_create:
            mock_create.return_value = MagicMock()
            with patch("sqlalchemy.orm.sessionmaker") as mock_sm:
                mock_sm.return_value = MagicMock()
                SQLAlchemyRecordStore(
                    db_url="postgresql://test:test@localhost/test",
                    create_tables=False,
                )

                call_kwargs = mock_create.call_args[1]
                assert call_kwargs["pool_size"] == 20
                assert call_kwargs["max_overflow"] == 30
                assert call_kwargs["pool_recycle"] == 1800

    def test_pool_pre_ping_enabled_for_postgresql(self, monkeypatch):
        """pool_pre_ping=True for PostgreSQL connections."""
        monkeypatch.delenv("NEXUS_DB_POOL_SIZE", raising=False)
        monkeypatch.delenv("NEXUS_DB_MAX_OVERFLOW", raising=False)
        monkeypatch.delenv("NEXUS_DB_POOL_RECYCLE", raising=False)

        from nexus.storage.record_store import SQLAlchemyRecordStore

        with patch("sqlalchemy.create_engine") as mock_create:
            mock_create.return_value = MagicMock()
            with patch("sqlalchemy.orm.sessionmaker") as mock_sm:
                mock_sm.return_value = MagicMock()
                SQLAlchemyRecordStore(
                    db_url="postgresql://test:test@localhost/test",
                    create_tables=False,
                )

                call_kwargs = mock_create.call_args[1]
                assert call_kwargs["pool_pre_ping"] is True


class TestRecordStoreAsyncURLConversion:
    """Tests for sync→async URL conversion."""

    def test_to_async_url_postgresql(self):
        """postgresql:// → postgresql+asyncpg://."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        result = SQLAlchemyRecordStore._to_async_url("postgresql://user:pass@host/db")
        assert result == "postgresql+asyncpg://user:pass@host/db"

    def test_to_async_url_sqlite(self):
        """sqlite:/// → sqlite+aiosqlite:///."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        result = SQLAlchemyRecordStore._to_async_url("sqlite:///path/to/db")
        assert result == "sqlite+aiosqlite:///path/to/db"

    def test_to_async_url_unknown_passthrough(self):
        """Unknown URL schemes pass through unchanged."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        result = SQLAlchemyRecordStore._to_async_url("mysql://host/db")
        assert result == "mysql://host/db"


class TestRecordStoreCreateTables:
    """Tests for create_tables flag."""

    def test_create_tables_true_calls_create_all(self):
        """Default behavior: create_tables=True calls create_all."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        with patch("nexus.storage.models.Base") as mock_base:
            store = SQLAlchemyRecordStore(create_tables=True)
            mock_base.metadata.create_all.assert_called_once()
            store.close()

    def test_create_tables_false_skips_create_all(self):
        """Production mode: create_tables=False skips create_all."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        with patch("nexus.storage.models.Base") as mock_base:
            store = SQLAlchemyRecordStore(create_tables=False)
            mock_base.metadata.create_all.assert_not_called()
            store.close()


class TestRecordStoreCreatorParams:
    """Tests for creator/async_creator parameters (Cloud SQL support)."""

    def test_creator_param_passed_to_engine(self, monkeypatch):
        """Sync creator callable passed through to create_engine."""
        monkeypatch.delenv("NEXUS_DB_POOL_SIZE", raising=False)
        monkeypatch.delenv("NEXUS_DB_MAX_OVERFLOW", raising=False)
        monkeypatch.delenv("NEXUS_DB_POOL_RECYCLE", raising=False)

        from nexus.storage.record_store import SQLAlchemyRecordStore

        mock_creator = MagicMock()

        with patch("sqlalchemy.create_engine") as mock_create:
            mock_create.return_value = MagicMock()
            with patch("sqlalchemy.orm.sessionmaker") as mock_sm:
                mock_sm.return_value = MagicMock()
                SQLAlchemyRecordStore(
                    db_url="postgresql://placeholder",
                    create_tables=False,
                    creator=mock_creator,
                )

                call_kwargs = mock_create.call_args[1]
                assert call_kwargs["creator"] is mock_creator

    def test_async_creator_param_stored_for_lazy_init(self, monkeypatch):
        """Async creator is stored and used when async_session_factory is accessed."""
        monkeypatch.delenv("NEXUS_DB_POOL_SIZE", raising=False)
        monkeypatch.delenv("NEXUS_DB_MAX_OVERFLOW", raising=False)
        monkeypatch.delenv("NEXUS_DB_POOL_RECYCLE", raising=False)

        from nexus.storage.record_store import SQLAlchemyRecordStore

        mock_async_creator = MagicMock()

        with patch("sqlalchemy.create_engine") as mock_create:
            mock_create.return_value = MagicMock()
            with patch("sqlalchemy.orm.sessionmaker") as mock_sm:
                mock_sm.return_value = MagicMock()
                store = SQLAlchemyRecordStore(
                    db_url="postgresql://placeholder",
                    create_tables=False,
                    async_creator=mock_async_creator,
                )

                assert store._async_creator is mock_async_creator

    def test_async_creator_param_passed_to_async_engine(self, monkeypatch):
        """Async creator passed to create_async_engine on first access."""
        monkeypatch.delenv("NEXUS_DB_POOL_SIZE", raising=False)
        monkeypatch.delenv("NEXUS_DB_MAX_OVERFLOW", raising=False)
        monkeypatch.delenv("NEXUS_DB_POOL_RECYCLE", raising=False)

        from nexus.storage.record_store import SQLAlchemyRecordStore

        mock_async_creator = MagicMock()

        with patch("sqlalchemy.create_engine") as mock_create:
            mock_create.return_value = MagicMock()
            with patch("sqlalchemy.orm.sessionmaker") as mock_sm:
                mock_sm.return_value = MagicMock()
                store = SQLAlchemyRecordStore(
                    db_url="postgresql://placeholder",
                    create_tables=False,
                    async_creator=mock_async_creator,
                )

        with patch("sqlalchemy.ext.asyncio.create_async_engine") as mock_async_create:
            mock_async_create.return_value = MagicMock()
            with patch("sqlalchemy.ext.asyncio.async_sessionmaker") as mock_asm:
                mock_asm.return_value = MagicMock()
                _ = store.async_session_factory

                call_kwargs = mock_async_create.call_args[1]
                assert call_kwargs["async_creator"] is mock_async_creator


class TestRecordStoreAsyncSessionFactory:
    """Tests for lazy async session factory initialization."""

    def test_async_session_factory_lazy_initialization(self):
        """Async engine not created until async_session_factory is accessed."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore(create_tables=False)
        assert store._async_engine is None
        assert store._async_session_factory_instance is None
        store.close()

    def test_session_factory_returns_sessionmaker(self):
        """session_factory returns a sessionmaker instance."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore(create_tables=False)
        factory = store.session_factory
        assert factory is not None
        assert callable(factory)
        store.close()

    def test_async_session_factory_returns_async_sessionmaker(self):
        """async_session_factory returns an async_sessionmaker on access."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore(create_tables=False)
        factory = store.async_session_factory
        assert factory is not None
        assert callable(factory)
        assert store._async_engine is not None
        store.close()


class TestRecordStoreLifecycle:
    """Tests for store lifecycle management."""

    def test_close_disposes_both_engines(self):
        """close() disposes sync engine and async engine (if initialized)."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore(create_tables=False)
        # Access async factory to initialize async engine
        _ = store.async_session_factory
        assert store._async_engine is not None

        store.close()
        # After close, async engine should be cleaned up
        assert store._async_engine is None
        assert store._async_session_factory_instance is None

    def test_close_without_async_engine(self):
        """close() works even when async engine was never initialized."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore(create_tables=False)
        assert store._async_engine is None
        store.close()  # Should not raise
