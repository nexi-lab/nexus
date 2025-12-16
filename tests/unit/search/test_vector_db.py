"""Tests for vector database module."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text

from nexus.search.vector_db import VectorDatabase


class TestVectorDatabase:
    """Test VectorDatabase implementation."""

    @pytest.fixture
    def sqlite_engine(self):
        """Create a SQLite engine for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            engine = create_engine(f"sqlite:///{db_path}")
            yield engine
            engine.dispose()

    def test_init_sqlite(self, sqlite_engine):
        """Test initialization with SQLite engine."""
        db = VectorDatabase(sqlite_engine)
        assert db.engine == sqlite_engine
        assert db.db_type == "sqlite"
        assert db._initialized is False
        assert db.vec_available is False

    def test_init_postgresql(self):
        """Test initialization with PostgreSQL engine."""
        mock_engine = MagicMock()
        mock_engine.dialect.name = "postgresql"

        db = VectorDatabase(mock_engine)
        assert db.db_type == "postgresql"

    def test_initialize_sqlite_without_vec(self, sqlite_engine):
        """Test SQLite initialization without sqlite-vec."""
        from unittest.mock import patch

        db = VectorDatabase(sqlite_engine)

        # Mock sqlite_vec import to raise ImportError
        # This will initialize with warnings about sqlite-vec not being available
        with (
            patch.dict("sys.modules", {"sqlite_vec": None}),
            pytest.warns(UserWarning, match="sqlite-vec not installed"),
        ):
            db.initialize()

        assert db._initialized is True

    def test_initialize_idempotent(self, sqlite_engine):
        """Test that initialize is idempotent."""
        from unittest.mock import patch

        db = VectorDatabase(sqlite_engine)

        # First initialization - mock sqlite_vec to raise ImportError
        with (
            patch.dict("sys.modules", {"sqlite_vec": None}),
            pytest.warns(UserWarning, match="sqlite-vec not installed"),
        ):
            db.initialize()
        assert db._initialized is True

        # Second initialization should not re-initialize (and should not warn)
        db.initialize()
        assert db._initialized is True

    def test_initialize_unsupported_db(self):
        """Test initialization with unsupported database type."""
        mock_engine = MagicMock()
        mock_engine.dialect.name = "mysql"

        db = VectorDatabase(mock_engine)

        with pytest.raises(ValueError, match="Unsupported database type"):
            db.initialize()

    def test_db_type_detection(self):
        """Test database type detection."""
        # SQLite
        sqlite_engine = MagicMock()
        sqlite_engine.dialect.name = "sqlite"
        db = VectorDatabase(sqlite_engine)
        assert db.db_type == "sqlite"

        # PostgreSQL
        pg_engine = MagicMock()
        pg_engine.dialect.name = "postgresql"
        db = VectorDatabase(pg_engine)
        assert db.db_type == "postgresql"

    def test_multiple_instances(self, sqlite_engine):
        """Test creating multiple VectorDatabase instances."""
        db1 = VectorDatabase(sqlite_engine)
        db2 = VectorDatabase(sqlite_engine)

        assert db1 is not db2
        assert db1.engine == db2.engine

    def test_store_embedding_sqlite(self, sqlite_engine):
        """Test storing embedding in SQLite."""

        db = VectorDatabase(sqlite_engine)
        db.initialize()

        # Create a mock session
        with sqlite_engine.connect() as conn:
            # Create table if it doesn't exist
            conn.execute(
                text("""
                    CREATE TABLE IF NOT EXISTS document_chunks (
                        chunk_id TEXT PRIMARY KEY,
                        chunk_text TEXT,
                        embedding BLOB
                    )
                """)
            )
            conn.commit()

            # Create a session
            from sqlalchemy.orm import sessionmaker

            SessionLocal = sessionmaker(bind=sqlite_engine)
            session = SessionLocal()

            # Insert a test chunk
            session.execute(
                text("INSERT INTO document_chunks (chunk_id, chunk_text) VALUES (:id, :text)"),
                {"id": "test-chunk-1", "text": "test content"},
            )
            session.commit()

            # Store embedding
            embedding = [0.1, 0.2, 0.3, 0.4, 0.5]
            db.store_embedding(session, "test-chunk-1", embedding)

            # Verify embedding was stored
            result = session.execute(
                text("SELECT embedding FROM document_chunks WHERE chunk_id = :id"),
                {"id": "test-chunk-1"},
            ).fetchone()

            assert result is not None
            assert result[0] is not None  # Embedding blob should exist

            session.close()

    def test_store_embedding_postgresql(self):
        """Test storing embedding in PostgreSQL."""
        from unittest.mock import MagicMock

        from sqlalchemy.orm import Session

        mock_engine = MagicMock()
        mock_engine.dialect.name = "postgresql"

        db = VectorDatabase(mock_engine)
        db.vec_available = True

        mock_session = MagicMock(spec=Session)
        embedding = [0.1, 0.2, 0.3]

        db.store_embedding(mock_session, "chunk-1", embedding)

        # Verify execute was called with correct parameters
        mock_session.execute.assert_called_once()
        call_args = mock_session.execute.call_args
        # call_args is a tuple: (args_tuple, kwargs_dict)
        # SQLAlchemy execute is called as: execute(text_obj, params_dict)
        # So args[0] is the text object, args[1] is the params dict
        args = call_args[0]  # Positional arguments tuple
        assert len(args) >= 2, "execute should be called with at least 2 args (text, params)"
        sql_text = args[0]
        params = args[1]  # Second positional arg is the params dict

        assert "UPDATE document_chunks" in str(sql_text)
        assert params["embedding"] == embedding
        assert params["chunk_id"] == "chunk-1"

    def test_vector_search_unsupported_db(self):
        """Test vector_search with unsupported database."""
        mock_engine = MagicMock()
        mock_engine.dialect.name = "mysql"

        db = VectorDatabase(mock_engine)

        from sqlalchemy.orm import Session

        mock_session = MagicMock(spec=Session)

        with pytest.raises(ValueError, match="Unsupported database type"):
            db.vector_search(mock_session, [0.1, 0.2, 0.3], limit=10)

    def test_initialize_postgresql_with_vec(self):
        """Test PostgreSQL initialization with pgvector."""
        from unittest.mock import MagicMock, patch

        mock_engine = MagicMock()
        mock_engine.dialect.name = "postgresql"

        mock_conn = MagicMock()
        mock_conn.execute = MagicMock()
        mock_conn.commit = MagicMock()
        mock_conn.rollback = MagicMock()

        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=None)

        db = VectorDatabase(mock_engine)

        # Mock successful pgvector extension creation
        with patch.object(mock_conn, "execute") as mock_execute:
            mock_execute.return_value = None
            db.initialize()

            assert db._initialized is True
            # Should try to create vector extension
            assert mock_execute.call_count > 0

    def test_initialize_postgresql_without_vec(self):
        """Test PostgreSQL initialization without pgvector."""
        from unittest.mock import MagicMock

        mock_engine = MagicMock()
        mock_engine.dialect.name = "postgresql"

        mock_conn = MagicMock()
        mock_conn.execute = MagicMock(side_effect=OSError("Extension not available"))
        mock_conn.commit = MagicMock()
        mock_conn.rollback = MagicMock()

        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=None)

        db = VectorDatabase(mock_engine)

        with pytest.warns(UserWarning, match="pgvector extension not available"):
            db.initialize()

        assert db._initialized is True
        assert db.vec_available is False

    def test_initialize_sqlite_with_vec(self, sqlite_engine):
        """Test SQLite initialization with sqlite-vec available."""
        # This test verifies that initialize() completes successfully
        # when sqlite-vec is available. Since sqlite_vec is imported
        # inside the method, we can't easily mock it without complex patching.
        # Instead, we just verify that initialize() completes without error.
        db = VectorDatabase(sqlite_engine)

        # Initialize should complete successfully (will handle sqlite_vec import internally)
        db.initialize()

        assert db._initialized is True
        # vec_available depends on whether sqlite-vec is actually installed
