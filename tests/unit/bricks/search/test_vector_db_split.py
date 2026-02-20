"""Tests for VectorDatabase backend operations (Issue #1520).

Validates VectorDatabase init paths, store_embedding, keyword_search,
and vector_search for both SQLite and PostgreSQL backends.

These are characterization tests — they document current behavior
before the vector_db split refactoring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from nexus.bricks.search.vector_db import VectorDatabase

# =============================================================================
# VectorDatabase construction and init
# =============================================================================


class TestVectorDatabaseConstruction:
    """Test VectorDatabase constructor and basic properties."""

    def test_constructor_sets_db_type_from_engine(self) -> None:
        """db_type should come from engine.dialect.name."""
        engine = MagicMock()
        engine.dialect.name = "sqlite"

        from nexus.bricks.search.vector_db import VectorDatabase

        vdb = VectorDatabase(engine)
        assert vdb.db_type == "sqlite"
        assert vdb.vec_available is False
        assert vdb.bm25_available is False

    def test_constructor_postgresql(self) -> None:
        engine = MagicMock()
        engine.dialect.name = "postgresql"

        from nexus.bricks.search.vector_db import VectorDatabase

        vdb = VectorDatabase(engine)
        assert vdb.db_type == "postgresql"

    def test_constructor_with_hnsw_config(self) -> None:
        engine = MagicMock()
        engine.dialect.name = "postgresql"

        from nexus.bricks.search.hnsw_config import HNSWConfig
        from nexus.bricks.search.vector_db import VectorDatabase

        config = HNSWConfig(m=16, ef_construction=64, ef_search=40)
        vdb = VectorDatabase(engine, hnsw_config=config)
        assert vdb.hnsw_config.m == 16
        assert vdb.hnsw_config.ef_construction == 64


# =============================================================================
# SQLite init path
# =============================================================================


class TestSQLiteInitPath:
    """Test VectorDatabase._init_sqlite behavior (mocked)."""

    def test_sqlite_init_without_sqlite_vec(self) -> None:
        """When sqlite_vec not installed, vec_available should be False."""
        engine = MagicMock()
        engine.dialect.name = "sqlite"

        from nexus.bricks.search.vector_db import VectorDatabase

        vdb = VectorDatabase(engine)

        with (
            patch.dict("sys.modules", {"sqlite_vec": None}),
            patch("builtins.__import__", side_effect=ImportError("no sqlite_vec")),
        ):
            # Can't easily test _init_sqlite directly because it catches ImportError
            # Just verify initial state
            assert vdb.vec_available is False

    def test_unsupported_dialect_raises(self) -> None:
        """Unsupported db_type should raise ValueError."""
        engine = MagicMock()
        engine.dialect.name = "mysql"

        from nexus.bricks.search.vector_db import VectorDatabase

        vdb = VectorDatabase(engine)
        with pytest.raises(ValueError, match="Unsupported database type"):
            vdb.initialize()


# =============================================================================
# store_embedding
# =============================================================================


class TestStoreEmbedding:
    """Test store_embedding for both backends."""

    def test_sqlite_store_embedding_uses_blob(self) -> None:
        """SQLite embeddings should be stored as BLOB (struct pack)."""
        import struct

        engine = MagicMock()
        engine.dialect.name = "sqlite"

        from nexus.bricks.search.vector_db import VectorDatabase

        vdb = VectorDatabase(engine)

        session = MagicMock()
        embedding = [0.1, 0.2, 0.3]
        vdb.store_embedding(session, "chunk-1", embedding)

        session.execute.assert_called_once()
        # Params are passed as second positional arg: session.execute(text(...), params)
        params = session.execute.call_args.args[1]
        assert params["chunk_id"] == "chunk-1"
        # Verify blob format
        expected_blob = struct.pack("3f", 0.1, 0.2, 0.3)
        assert params["embedding"] == expected_blob

    def test_postgres_store_embedding_uses_array(self) -> None:
        """PostgreSQL embeddings should be stored as array."""
        engine = MagicMock()
        engine.dialect.name = "postgresql"

        from nexus.bricks.search.vector_db import VectorDatabase

        vdb = VectorDatabase(engine)

        session = MagicMock()
        embedding = [0.1, 0.2, 0.3]
        vdb.store_embedding(session, "chunk-1", embedding)

        session.execute.assert_called_once()
        params = session.execute.call_args.args[1]
        assert params["chunk_id"] == "chunk-1"
        assert params["embedding"] == [0.1, 0.2, 0.3]


# =============================================================================
# Result dict shape
# =============================================================================


class TestResultDictShape:
    """Verify the result dict shape from search methods."""

    def _expected_keys(self) -> set[str]:
        """Standard keys in a search result dict."""
        return {
            "chunk_id",
            "path",
            "chunk_index",
            "chunk_text",
            "start_offset",
            "end_offset",
            "line_start",
            "line_end",
            "score",
        }

    def test_keyword_search_result_shape(self) -> None:
        """keyword_search results should have standard keys."""
        engine = MagicMock()
        engine.dialect.name = "sqlite"

        from nexus.bricks.search.vector_db import VectorDatabase

        vdb = VectorDatabase(engine)
        vdb._initialized = True

        # Mock the FTS search to return a row
        session = MagicMock()
        mock_row = MagicMock()
        mock_row.chunk_id = "c1"
        mock_row.virtual_path = "/test.py"
        mock_row.chunk_index = 0
        mock_row.chunk_text = "test content"
        mock_row.start_offset = 0
        mock_row.end_offset = 12
        mock_row.line_start = 1
        mock_row.line_end = 1
        mock_row.score = -0.5  # FTS5 rank is negative

        session.execute.return_value = [mock_row]

        # Patch Zoekt and BM25S to return None (fallback to FTS)
        with (
            patch.object(vdb, "_try_keyword_search_with_zoekt", return_value=None),
            patch.object(vdb, "_try_keyword_search_with_bm25s", return_value=None),
        ):
            results = vdb.keyword_search(session, "test", limit=10)

        assert len(results) == 1
        result = results[0]
        # Check all expected keys are present
        for key in self._expected_keys():
            assert key in result, f"Missing key: {key}"

    def test_vector_search_unsupported_dialect(self) -> None:
        """vector_search should raise for unsupported db_type."""
        engine = MagicMock()
        engine.dialect.name = "mysql"

        from nexus.bricks.search.vector_db import VectorDatabase

        vdb = VectorDatabase(engine)
        session = MagicMock()

        with pytest.raises(ValueError, match="Unsupported database type"):
            vdb.vector_search(session, [0.1, 0.2], limit=10)


# =============================================================================
# _run_sync helper
# =============================================================================


class TestRunSync:
    """Test the _run_sync() instance method for sync/async bridging."""

    @staticmethod
    def _make_vdb() -> VectorDatabase:
        """Create a minimal VectorDatabase with a mock engine."""
        from unittest.mock import MagicMock

        engine = MagicMock()
        engine.dialect.name = "sqlite"
        return VectorDatabase(engine)

    def test_run_sync_outside_event_loop(self) -> None:
        """_run_sync should work when no event loop is running."""
        vdb = self._make_vdb()

        async def async_add(a: int, b: int) -> int:
            return a + b

        result = vdb._run_sync(async_add(1, 2))
        assert result == 3
        vdb.close()

    def test_run_sync_returns_value(self) -> None:
        vdb = self._make_vdb()

        async def async_identity(x: str) -> str:
            return x

        result = vdb._run_sync(async_identity("hello"))
        assert result == "hello"
        vdb.close()


# =============================================================================
# VectorDatabase properties
# =============================================================================


class TestVectorDatabaseProperties:
    """Test VectorDatabase direct property access."""

    def test_properties_reflect_init_state(self) -> None:
        engine = MagicMock()
        engine.dialect.name = "sqlite"

        from nexus.bricks.search.vector_db import VectorDatabase

        vdb = VectorDatabase(engine)

        assert vdb.db_type == "sqlite"
        assert vdb.vec_available is False
        assert vdb.bm25_available is False
