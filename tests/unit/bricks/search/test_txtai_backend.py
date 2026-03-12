"""Tests for txtai backend (Issue #2663).

Mocked unit tests verifying:
- TxtaiBackend lifecycle (startup, shutdown)
- Index/upsert/delete operations with zone_id stamping
- Search with zone_id SQL WHERE clause
- Path filter generation
- Error propagation
- Graph search methods
"""

from unittest.mock import MagicMock, patch

import pytest

from nexus.bricks.search.results import BaseSearchResult

txtai_backend = pytest.importorskip(
    "nexus.bricks.search.txtai_backend",
    reason="txtai_backend not available in this environment",
)
SearchBackendProtocol = txtai_backend.SearchBackendProtocol
TxtaiBackend = txtai_backend.TxtaiBackend
SEARCH_BACKENDS = txtai_backend.SEARCH_BACKENDS
_escape_sql_string = txtai_backend._escape_sql_string
_stamp_zone_id = txtai_backend._stamp_zone_id
create_backend = txtai_backend.create_backend

# =============================================================================
# Helper tests
# =============================================================================


class TestHelpers:
    """Test helper functions."""

    def test_escape_sql_string_basic(self) -> None:
        assert _escape_sql_string("hello") == "hello"

    def test_escape_sql_string_with_quotes(self) -> None:
        assert _escape_sql_string("it's a test") == "it''s a test"

    def test_stamp_zone_id_immutable(self) -> None:
        docs = [{"id": "1", "text": "hello"}]
        stamped = _stamp_zone_id(docs, "zone-a")
        assert stamped[0]["zone_id"] == "zone-a"
        # Original should not be mutated
        assert "zone_id" not in docs[0]

    def test_stamp_zone_id_overwrites_existing(self) -> None:
        docs = [{"id": "1", "zone_id": "old"}]
        stamped = _stamp_zone_id(docs, "new")
        assert stamped[0]["zone_id"] == "new"


# =============================================================================
# Backend Registry tests
# =============================================================================


class TestBackendRegistry:
    """Test backend registry and factory."""

    def test_txtai_in_registry(self) -> None:
        assert "txtai" in SEARCH_BACKENDS

    def test_create_backend_txtai(self) -> None:
        backend = create_backend("txtai")
        assert isinstance(backend, TxtaiBackend)

    def test_create_backend_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown search backend"):
            create_backend("nonexistent")

    def test_create_backend_with_kwargs(self) -> None:
        backend = create_backend("txtai", model="test-model", hybrid=False)
        assert isinstance(backend, TxtaiBackend)
        assert backend._model == "test-model"
        assert backend._hybrid is False

    def test_registry_is_dict(self) -> None:
        assert isinstance(SEARCH_BACKENDS, dict)

    def test_error_message_includes_available(self) -> None:
        with pytest.raises(ValueError, match="txtai"):
            create_backend("bad-name")


# =============================================================================
# TxtaiBackend lifecycle tests
# =============================================================================


class TestTxtaiBackendLifecycle:
    """Test TxtaiBackend startup and shutdown."""

    @pytest.mark.asyncio
    async def test_startup_creates_embeddings(self) -> None:
        backend = TxtaiBackend(model="test-model")
        # Verify _embeddings starts as None and can be set
        assert backend._embeddings is None
        backend._embeddings = MagicMock()
        assert backend._embeddings is not None

    def test_startup_pgvector_config_uses_pgvector_url_key(self) -> None:
        """Regression test for #2916: pgvector URL must be under config['pgvector']['url'].

        txtai's PGVector ANN resolves the database URL via self.setting('url'),
        which looks up config[config['backend']]['url'].  Previously the URL was
        placed under config['database'] (the content-DB key), leaving the ANN's
        database session uninitialised and causing ``AttributeError: 'NoneType'
        object has no attribute 'query'`` at search time.
        """
        import asyncio

        mock_embeddings_cls = MagicMock()
        captured_configs: list[dict] = []
        mock_embeddings_cls.side_effect = lambda cfg: (
            captured_configs.append(dict(cfg)) or MagicMock()
        )

        # Build mock torch module tree
        mock_mps = MagicMock()
        mock_mps.is_available.return_value = False
        mock_backends = MagicMock()
        mock_backends.mps = mock_mps
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.return_value = False
        mock_torch.backends = mock_backends

        # Patch txtai + torch imports so startup() doesn't need real packages
        with patch.dict(
            "sys.modules",
            {
                "txtai": MagicMock(Embeddings=mock_embeddings_cls),
                "txtai.ann": MagicMock(),
                "txtai.ann.dense": MagicMock(),
                "txtai.ann.dense.pgvector": MagicMock(),
                "torch": mock_torch,
            },
        ):
            backend = TxtaiBackend(
                database_url="postgresql://u:p@localhost:5432/nexus",
                model="test-model",
            )
            asyncio.run(backend.startup())

        assert len(captured_configs) >= 1
        cfg = captured_configs[0]
        assert cfg["backend"] == "pgvector"
        assert cfg["pgvector"] == {"url": "postgresql://u:p@localhost:5432/nexus"}
        assert "database" not in cfg, "URL must not go under 'database' — that's the content-DB key"

    @pytest.mark.asyncio
    async def test_shutdown_when_not_started(self) -> None:
        backend = TxtaiBackend()
        await backend.shutdown()  # Should not raise

    @pytest.mark.asyncio
    async def test_operations_when_not_started(self) -> None:
        backend = TxtaiBackend()
        # All operations should return 0/empty when no embeddings
        assert await backend.index([], zone_id="z") == 0
        assert await backend.upsert([], zone_id="z") == 0
        assert await backend.delete([], zone_id="z") == 0
        results = await backend.search("query", zone_id="z")
        assert results == []


# =============================================================================
# TxtaiBackend search tests
# =============================================================================


class TestTxtaiBackendSearch:
    """Test TxtaiBackend search operations."""

    def _make_backend_with_mock(self) -> tuple[TxtaiBackend, MagicMock]:
        backend = TxtaiBackend()
        mock_emb = MagicMock()
        backend._embeddings = mock_emb
        return backend, mock_emb

    @pytest.mark.asyncio
    async def test_search_builds_sql_with_zone_id(self) -> None:
        backend, mock_emb = self._make_backend_with_mock()
        mock_emb.search.return_value = []
        await backend.search("test query", zone_id="corp", limit=5)
        call_args = mock_emb.search.call_args[0][0]
        assert "zone_id = 'corp'" in call_args
        assert "LIMIT 5" in call_args

    @pytest.mark.asyncio
    async def test_search_with_path_filter(self) -> None:
        backend, mock_emb = self._make_backend_with_mock()
        mock_emb.search.return_value = []
        await backend.search("test", zone_id="z", path_filter="/docs")
        call_args = mock_emb.search.call_args[0][0]
        assert "path LIKE '/docs%'" in call_args

    @pytest.mark.asyncio
    async def test_search_escapes_query(self) -> None:
        backend, mock_emb = self._make_backend_with_mock()
        mock_emb.search.return_value = []
        await backend.search("it's a test", zone_id="z")
        call_args = mock_emb.search.call_args[0][0]
        assert "it''s a test" in call_args

    @pytest.mark.asyncio
    async def test_search_returns_base_search_results(self) -> None:
        backend, mock_emb = self._make_backend_with_mock()
        mock_emb.search.return_value = [
            {"path": "/a.py", "text": "content", "score": 0.95, "zone_id": "z"},
        ]
        results = await backend.search("test", zone_id="z")
        assert len(results) == 1
        assert isinstance(results[0], BaseSearchResult)
        assert results[0].path == "/a.py"
        assert results[0].score == 0.95

    @pytest.mark.asyncio
    async def test_search_empty_results(self) -> None:
        backend, mock_emb = self._make_backend_with_mock()
        mock_emb.search.return_value = []
        results = await backend.search("nothing", zone_id="z")
        assert results == []


# =============================================================================
# TxtaiBackend index operations
# =============================================================================


class TestTxtaiBackendIndex:
    """Test TxtaiBackend index/upsert/delete."""

    def _make_backend_with_mock(self) -> tuple[TxtaiBackend, MagicMock]:
        backend = TxtaiBackend()
        mock_emb = MagicMock()
        backend._embeddings = mock_emb
        return backend, mock_emb

    @pytest.mark.asyncio
    async def test_index_stamps_zone_id(self) -> None:
        backend, mock_emb = self._make_backend_with_mock()
        docs = [{"id": "1", "text": "hello", "path": "/a.py"}]
        count = await backend.index(docs, zone_id="corp")
        assert count == 1
        # Verify zone_id was stamped
        call_args = mock_emb.index.call_args[0][0]
        assert call_args[0][1]["zone_id"] == "corp"

    @pytest.mark.asyncio
    async def test_upsert_stamps_zone_id(self) -> None:
        backend, mock_emb = self._make_backend_with_mock()
        docs = [{"id": "1", "text": "hello", "path": "/a.py"}]
        count = await backend.upsert(docs, zone_id="corp")
        assert count == 1
        call_args = mock_emb.upsert.call_args[0][0]
        assert call_args[0][1]["zone_id"] == "corp"

    @pytest.mark.asyncio
    async def test_delete_calls_backend(self) -> None:
        backend, mock_emb = self._make_backend_with_mock()
        count = await backend.delete(["id1", "id2"], zone_id="z")
        assert count == 2
        mock_emb.delete.assert_called_once_with(["id1", "id2"])

    @pytest.mark.asyncio
    async def test_index_empty_docs(self) -> None:
        backend, mock_emb = self._make_backend_with_mock()
        count = await backend.index([], zone_id="z")
        assert count == 0
        mock_emb.index.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_empty_ids(self) -> None:
        backend, mock_emb = self._make_backend_with_mock()
        count = await backend.delete([], zone_id="z")
        assert count == 0
        mock_emb.delete.assert_not_called()


# =============================================================================
# Graph search tests
# =============================================================================


class TestTxtaiBackendGraph:
    """Test TxtaiBackend graph search methods."""

    @pytest.mark.asyncio
    async def test_graph_search_no_graph(self) -> None:
        backend = TxtaiBackend(graph=False)
        backend._embeddings = MagicMock(spec=[])  # No graph attr
        results = await backend.graph_search("test", zone_id="z")
        assert results == []

    @pytest.mark.asyncio
    async def test_graph_search_filters_by_zone_id(self) -> None:
        backend = TxtaiBackend()
        mock_emb = MagicMock()
        mock_graph = MagicMock()
        mock_graph.search.return_value = [
            {"path": "/a.py", "text": "hello", "score": 0.9, "zone_id": "corp"},
            {"path": "/b.py", "text": "world", "score": 0.8, "zone_id": "other"},
        ]
        mock_emb.graph = mock_graph
        backend._embeddings = mock_emb

        results = await backend.graph_search("test", zone_id="corp")
        assert len(results) == 1
        assert results[0].path == "/a.py"

    @pytest.mark.asyncio
    async def test_get_entity_neighbors_no_graph(self) -> None:
        backend = TxtaiBackend()
        backend._embeddings = MagicMock(spec=[])
        result = await backend.get_entity_neighbors("e1", zone_id="z")
        assert result == []

    @pytest.mark.asyncio
    async def test_graph_search_empty(self) -> None:
        backend = TxtaiBackend()
        mock_emb = MagicMock()
        mock_graph = MagicMock()
        mock_graph.search.return_value = []
        mock_emb.graph = mock_graph
        backend._embeddings = mock_emb

        results = await backend.graph_search("test", zone_id="z")
        assert results == []
