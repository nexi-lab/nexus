"""Tests for vector database module."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine

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
