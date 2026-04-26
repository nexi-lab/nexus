"""Tests for txtai backend (Issue #2663).

Mocked unit tests verifying:
- TxtaiBackend lifecycle (startup, shutdown)
- Index/upsert/delete operations with zone_id stamping
- Search with zone_id SQL WHERE clause
- Path filter generation
- Error propagation
- Graph search methods
- Thread-safety lock serialisation (#2919)
"""

import asyncio
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
_escape_like_string = txtai_backend._escape_like_string
_build_search_sql = txtai_backend._build_search_sql
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

    def test_escape_like_string_percent(self) -> None:
        """Issue #3062: % in path filter must be escaped for LIKE."""
        assert _escape_like_string("foo%bar") == "foo\\%bar"

    def test_escape_like_string_underscore(self) -> None:
        """Issue #3062: _ in path filter must be escaped for LIKE."""
        assert _escape_like_string("file_name") == "file\\_name"

    def test_escape_like_string_backslash(self) -> None:
        """Backslashes must be escaped before % and _."""
        assert _escape_like_string("a\\b") == "a\\\\b"

    def test_escape_like_string_combined(self) -> None:
        """Combined escaping: quotes + wildcards."""
        assert _escape_like_string("it's 100% done_1") == "it''s 100\\% done\\_1"

    def test_build_search_sql_path_filter_escapes_wildcards(self) -> None:
        """Issue #3062: _build_search_sql escapes LIKE wildcards in path_filter."""
        sql = _build_search_sql("query", zone_id="z1", path_filter="/data/100%_files")
        assert "\\%" in sql
        assert "\\_" in sql
        assert "ESCAPE" in sql

    def test_build_search_sql_no_path_filter(self) -> None:
        sql = _build_search_sql("query", zone_id="z1")
        assert "LIKE" not in sql

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
        assert cfg["content"] == "postgresql://u:p@localhost:5432/nexus"
        assert "database" not in cfg, "URL must not go under 'database' — that's the content-DB key"

    def test_startup_with_database_url_does_not_default_content_to_sqlite(self) -> None:
        """Regression test for SQLite sidecar crashes during txtai indexing.

        txtai treats ``content=True`` as "use SQLite". When Nexus already has a
        PostgreSQL database URL, the content/object store must use that same URL
        so the backend stays fully Postgres/pgvector-backed.
        """
        import asyncio

        mock_embeddings_cls = MagicMock()
        captured_configs: list[dict] = []
        mock_embeddings_cls.side_effect = lambda cfg: (
            captured_configs.append(dict(cfg)) or MagicMock()
        )

        mock_mps = MagicMock()
        mock_mps.is_available.return_value = False
        mock_backends = MagicMock()
        mock_backends.mps = mock_mps
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.return_value = False
        mock_torch.backends = mock_backends

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

        cfg = captured_configs[0]
        assert cfg["content"] == "postgresql://u:p@localhost:5432/nexus"
        assert cfg["content"] is not True

    def test_startup_passes_optional_vectors_config(self) -> None:
        import asyncio

        mock_embeddings_cls = MagicMock()
        captured_configs: list[dict] = []
        mock_embeddings_cls.side_effect = lambda cfg: (
            captured_configs.append(dict(cfg)) or MagicMock()
        )

        mock_mps = MagicMock()
        mock_mps.is_available.return_value = False
        mock_backends = MagicMock()
        mock_backends.mps = mock_mps
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.return_value = False
        mock_torch.backends = mock_backends

        with patch.dict(
            "sys.modules",
            {
                "txtai": MagicMock(Embeddings=mock_embeddings_cls),
                "torch": mock_torch,
            },
        ):
            backend = TxtaiBackend(
                model="openai/text-embedding-3-small",
                vectors={"api_key": "sk-test", "api_base": "https://api.openai.example/v1"},
            )
            asyncio.run(backend.startup())

        cfg = captured_configs[0]
        assert cfg["path"] == "openai/text-embedding-3-small"
        assert cfg["vectors"] == {
            "api_key": "sk-test",
            "api_base": "https://api.openai.example/v1",
        }

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
        # search_type="keyword" so the hybrid over-fetch (Issue #3900) doesn't
        # mask the limit assertion.
        await backend.search("test query", zone_id="corp", limit=5, search_type="keyword")
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

    @pytest.mark.asyncio
    async def test_search_dedupes_hybrid_duplicates(self) -> None:
        """Issue #3900: hybrid mode must not surface the same id twice.

        txtai's hybrid scorer returns one row per scorer (BM25 + dense), so
        the same chunk can appear in `raw` more than once. The backend must
        collapse rows by id and keep the best score.
        """
        backend, mock_emb = self._make_backend_with_mock()
        mock_emb.search.return_value = [
            {"id": "demo-1", "path": "/p/demo-1", "text": "t", "score": 0.55, "zone_id": "z"},
            {"id": "demo-1", "path": "/p/demo-1", "text": "t", "score": 0.40, "zone_id": "z"},
            {"id": "demo-2", "path": "/p/demo-2", "text": "u", "score": 0.30, "zone_id": "z"},
        ]
        results = await backend.search("q", zone_id="z", search_type="hybrid")
        assert [r.path for r in results] == ["/p/demo-1", "/p/demo-2"]
        assert results[0].score == 0.55  # higher of the two demo-1 scores

    @pytest.mark.asyncio
    async def test_search_hybrid_overfetches_to_avoid_underfill(self) -> None:
        """Issue #3900: hybrid SQL must over-fetch so dedupe doesn't underfill.

        If we ask txtai for exactly `limit` rows and every row pair is the
        same id, post-dedupe we'd return < limit unique results when more
        unique matches existed beyond the SQL LIMIT. The backend should
        over-fetch in hybrid mode and slice to limit *after* dedupe.
        """
        backend, mock_emb = self._make_backend_with_mock()

        # Simulate txtai returning 2*limit rows (the over-fetch), where
        # ids 1-3 are duplicated by both BM25 and dense scorers and id 4
        # appears only once. With limit=3 and naive limit-then-dedupe,
        # we'd see [1, 1, 2] → 2 unique. With over-fetch + dedupe-then-slice,
        # we should see all three unique ids.
        mock_emb.search.return_value = [
            {"id": "1", "path": "/a", "text": "a", "score": 0.90, "zone_id": "z"},
            {"id": "1", "path": "/a", "text": "a", "score": 0.85, "zone_id": "z"},
            {"id": "2", "path": "/b", "text": "b", "score": 0.80, "zone_id": "z"},
            {"id": "2", "path": "/b", "text": "b", "score": 0.75, "zone_id": "z"},
            {"id": "3", "path": "/c", "text": "c", "score": 0.70, "zone_id": "z"},
            {"id": "3", "path": "/c", "text": "c", "score": 0.65, "zone_id": "z"},
        ]
        results = await backend.search("q", zone_id="z", search_type="hybrid", limit=3)
        # Backend asked for an over-fetched LIMIT in the SQL.
        sql = mock_emb.search.call_args[0][0]
        assert "LIMIT 6" in sql
        # All three unique ids surface, ordered by score, sliced to limit.
        assert len(results) == 3
        assert [r.path for r in results] == ["/a", "/b", "/c"]

    @pytest.mark.asyncio
    async def test_search_keyword_does_not_overfetch(self) -> None:
        """Keyword-only search has no dedupe risk and should not over-fetch."""
        backend, mock_emb = self._make_backend_with_mock()
        mock_emb.search.return_value = []
        await backend.search("q", zone_id="z", search_type="keyword", limit=5)
        sql = mock_emb.search.call_args[0][0]
        assert "LIMIT 5" in sql


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


# =============================================================================
# Thread-safety lock tests (#2919)
# =============================================================================


class TestTxtaiBackendConcurrency:
    """Verify that the asyncio.Lock serialises access to _embeddings.

    faiss is NOT thread-safe for concurrent search+write operations.
    asyncio.to_thread() dispatches to a thread pool, so without the lock
    multiple coroutines can hit the C++ layer concurrently → segfault.
    """

    @pytest.mark.asyncio
    async def test_concurrent_search_and_upsert_are_serialised(self) -> None:
        """Search and upsert must not overlap in the thread pool."""
        backend = TxtaiBackend()
        mock_emb = MagicMock()
        mock_emb.ann = MagicMock()  # non-None so upsert path is taken
        mock_emb.search.return_value = []
        backend._embeddings = mock_emb

        # Track whether operations overlap
        active = 0
        max_active = 0

        original_to_thread = asyncio.to_thread

        async def tracking_to_thread(func, *args, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                return await original_to_thread(func, *args, **kwargs)
            finally:
                active -= 1

        with patch("nexus.bricks.search.txtai_backend.asyncio.to_thread", tracking_to_thread):
            await asyncio.gather(
                backend.search("q1", zone_id="z"),
                backend.search("q2", zone_id="z"),
                backend.upsert([{"id": "1", "text": "t", "path": "/a"}], zone_id="z"),
            )

        # With the lock, at most 1 to_thread call should be active at a time
        assert max_active == 1, f"Operations overlapped: max_active={max_active}"

    @pytest.mark.asyncio
    async def test_lock_exists_on_backend(self) -> None:
        backend = TxtaiBackend()
        assert isinstance(backend._get_lock(), asyncio.Lock)
        assert backend._get_lock() is backend._get_lock()

    def test_lock_is_distinct_per_event_loop(self) -> None:
        # Issue #3894: a single TxtaiBackend instance reused across event loops
        # must not raise "bound to a different event loop" on lock acquire.
        import threading

        backend = TxtaiBackend()
        seen: list[asyncio.Lock] = []
        errors: list[BaseException] = []

        async def acquire_once() -> None:
            async with backend._get_lock():
                seen.append(backend._get_lock())

        def runner() -> None:
            try:
                asyncio.run(acquire_once())
            except BaseException as exc:
                errors.append(exc)

        t1 = threading.Thread(target=runner)
        t2 = threading.Thread(target=runner)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert errors == [], f"acquire raised across loops: {errors!r}"
        assert len(seen) == 2
        assert seen[0] is not seen[1]

    def test_native_section_serialises_across_loops(self) -> None:
        """Issue #3894 review: cross-loop native work must not overlap.

        With the round-3 design the cross-loop lock lives inside the worker
        thread that ``_run_native`` dispatches onto, so this test exercises
        ``_run_native`` (the new public surface for native execution) rather
        than ``_exclusive`` (which is now per-loop fairness only).
        """
        import threading
        import time as _time

        backend = TxtaiBackend()
        active = 0
        max_active = 0
        active_lock = threading.Lock()
        errors: list[BaseException] = []

        def native_op() -> None:
            nonlocal active, max_active
            with active_lock:
                active += 1
                if active > max_active:
                    max_active = active
            # Hold long enough that an overlapping caller would be observable.
            _time.sleep(0.05)
            with active_lock:
                active -= 1

        async def critical() -> None:
            await backend._run_native(native_op)

        def runner() -> None:
            try:
                asyncio.run(critical())
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=runner) for _ in range(4)]
        t0 = _time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = _time.perf_counter() - t0

        assert errors == [], f"native section raised: {errors!r}"
        assert max_active == 1, f"cross-loop critical sections overlapped: max={max_active}"
        # Four 0.05s native ops, fully serialised, must take at least ~0.18s.
        assert elapsed >= 0.18, f"sections did not serialise: elapsed={elapsed:.3f}s"

    def test_run_native_cancellation_holds_until_worker_returns(self) -> None:
        """Issue #3894 round 3: cancelling the awaiting coroutine must NOT let
        a second native op enter the section while the first worker thread is
        still executing ``fn``. The lock is acquired/released inside the worker
        so cancellation cannot strand it.
        """
        import threading
        import time as _time

        backend = TxtaiBackend()
        running = threading.Event()
        finish = threading.Event()
        second_started_at: list[float] = []
        second_observed_first_running: list[bool] = []

        def slow_fn() -> None:
            running.set()
            finish.wait(timeout=5)

        def fast_fn() -> None:
            second_started_at.append(_time.perf_counter())
            # If serialisation is intact, this only runs after slow_fn returns.
            second_observed_first_running.append(running.is_set() and not finish.is_set())

        async def cancelled_caller() -> None:
            await backend._run_native(slow_fn)

        async def driver() -> None:
            t = asyncio.create_task(cancelled_caller())
            # Give the worker a chance to acquire the lock and start slow_fn.
            await asyncio.to_thread(running.wait, 5)
            assert running.is_set()
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

        import contextlib

        # Caller path: cancel the slow operation while it is mid-flight.
        async def submit_second_after_cancel() -> None:
            await driver()
            # The slow worker is still holding the native lock; submit a second
            # native call and verify it is serialised behind the first.
            second = asyncio.create_task(backend._run_native(fast_fn))
            # Sleep briefly to let the second op queue up. It should NOT run
            # until ``finish`` is set, so the time delta will reveal overlap.
            await asyncio.sleep(0.05)
            assert not second.done(), "second native op ran while first still in flight"
            # Release the slow op, then wait for the second to finish.
            finish.set()
            await second

        asyncio.run(submit_second_after_cancel())
        assert second_started_at, "fast_fn never ran"
        # If the second op overlapped with the first, this would be True.
        assert second_observed_first_running == [False], (
            f"second op overlapped first: {second_observed_first_running!r}"
        )

    def test_startup_owner_failure_propagates_to_waiters(self) -> None:
        """Issue #3894 round 2: if the owning loop's startup raises, every
        waiting loop must wake and re-raise the same error instead of polling
        forever on ``_started``.
        """
        import threading

        backend = TxtaiBackend()
        boom = RuntimeError("startup boom")

        async def failing_impl() -> None:
            await asyncio.sleep(0.05)
            raise boom

        backend._startup_impl = failing_impl

        owner_error: list[BaseException] = []
        waiter_errors: list[BaseException] = []

        def owner() -> None:
            try:
                asyncio.run(backend.startup())
            except BaseException as exc:
                owner_error.append(exc)

        def waiter() -> None:
            try:
                # Give the owner time to enter _startup_running.
                import time as _t

                _t.sleep(0.005)
                asyncio.run(backend.startup())
            except BaseException as exc:
                waiter_errors.append(exc)

        t_owner = threading.Thread(target=owner)
        t_waiter1 = threading.Thread(target=waiter)
        t_waiter2 = threading.Thread(target=waiter)
        t_owner.start()
        t_waiter1.start()
        t_waiter2.start()
        t_owner.join(timeout=5)
        t_waiter1.join(timeout=5)
        t_waiter2.join(timeout=5)

        assert not t_owner.is_alive(), "owner deadlocked"
        assert not t_waiter1.is_alive(), "waiter 1 deadlocked"
        assert not t_waiter2.is_alive(), "waiter 2 deadlocked"
        assert owner_error and owner_error[0] is boom
        # At least one waiter saw the same error (race: both might also race
        # to acquire ownership and re-attempt, but neither may hang).
        assert all(isinstance(e, RuntimeError) for e in waiter_errors)

    def test_startup_retry_after_failure_isolates_generations(self) -> None:
        """Issue #3894 round 3: a retry that follows a startup failure must
        not stomp on the completion signal that earlier waiters are blocked
        on. Waiters from generation N see generation N's error; later
        generations have their own per-generation Event.
        """
        import threading

        backend = TxtaiBackend()
        first_boom = RuntimeError("gen 1 boom")
        second_boom = RuntimeError("gen 2 boom")
        gen2_can_start = threading.Event()

        call_count = 0
        call_count_lock = threading.Lock()

        async def impl() -> None:
            nonlocal call_count
            with call_count_lock:
                call_count += 1
                attempt = call_count
            if attempt == 1:
                # Hold long enough that the generation-1 waiter is parked and
                # the retry can start during this window.
                await asyncio.sleep(0.1)
                raise first_boom
            # Generation 2: wait for the test to release us, then fail with a
            # distinct error. If the implementation conflates generations,
            # generation-1 waiters would observe second_boom.
            await asyncio.to_thread(gen2_can_start.wait, 5)
            raise second_boom

        backend._startup_impl = impl

        gen1_owner_err: list[BaseException] = []
        gen1_waiter_err: list[BaseException] = []
        gen2_owner_err: list[BaseException] = []

        def gen1_owner() -> None:
            try:
                asyncio.run(backend.startup())
            except BaseException as exc:
                gen1_owner_err.append(exc)

        def gen1_waiter() -> None:
            # Enter while gen1 owner is still inside _startup_impl.
            import time as _t

            _t.sleep(0.005)
            try:
                asyncio.run(backend.startup())
            except BaseException as exc:
                gen1_waiter_err.append(exc)

        def gen2_owner() -> None:
            # Enter AFTER gen1 owner has finished raising — i.e. after gen1
            # waiter has unblocked. We deliberately race start of gen2 with
            # the tail of gen1 to exercise the generation-isolation contract.
            import time as _t

            _t.sleep(0.15)
            try:
                asyncio.run(backend.startup())
            except BaseException as exc:
                gen2_owner_err.append(exc)

        threads = [
            threading.Thread(target=gen1_owner),
            threading.Thread(target=gen1_waiter),
            threading.Thread(target=gen2_owner),
        ]
        for t in threads:
            t.start()
        # Let gen2 actually fail rather than block forever.
        gen2_can_start.set()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), "thread deadlocked"

        # Gen1 owner must see gen1's error.
        assert gen1_owner_err and gen1_owner_err[0] is first_boom
        # Gen1 waiter must see gen1's error too (NOT gen2's error). This is
        # the contract the per-generation Event protects.
        assert gen1_waiter_err
        assert gen1_waiter_err[0] is first_boom, (
            f"gen1 waiter saw wrong error: {gen1_waiter_err[0]!r}"
        )
        # Gen2 owner sees its own error.
        assert gen2_owner_err and gen2_owner_err[0] is second_boom

    def test_startup_runs_once_across_loops(self) -> None:
        """Issue #3894 review: only one loop should run _startup_impl, even when
        startup() is called concurrently from multiple loops.
        """
        import threading

        backend = TxtaiBackend()
        impl_calls = 0
        impl_lock = threading.Lock()
        errors: list[BaseException] = []

        async def fake_impl() -> None:
            nonlocal impl_calls
            with impl_lock:
                impl_calls += 1
            # Hold long enough for the other loop to enter startup() and see
            # _startup_running.
            await asyncio.sleep(0.1)
            backend._started = True

        backend._startup_impl = fake_impl

        def runner() -> None:
            try:
                asyncio.run(backend.startup())
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=runner) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"startup raised: {errors!r}"
        assert impl_calls == 1, f"_startup_impl ran {impl_calls} times, expected 1"
        assert backend._started is True

    @pytest.mark.asyncio
    async def test_search_during_shutdown_returns_empty(self) -> None:
        """search() must not dereference None if shutdown() clears _embeddings."""
        backend = TxtaiBackend()
        mock_emb = MagicMock()
        mock_emb.search.return_value = []
        backend._embeddings = mock_emb

        # Simulate: shutdown runs while search is queued on the lock.
        await backend.shutdown()
        assert backend._embeddings is None

        # search() after shutdown must return [] without raising
        results = await backend.search("test", zone_id="z")
        assert results == []

    @pytest.mark.asyncio
    async def test_upsert_during_shutdown_returns_zero(self) -> None:
        """upsert() must not dereference None if shutdown() clears _embeddings."""
        backend = TxtaiBackend()
        mock_emb = MagicMock()
        mock_emb.ann = MagicMock()
        backend._embeddings = mock_emb

        await backend.shutdown()

        count = await backend.upsert([{"id": "1", "text": "t", "path": "/a"}], zone_id="z")
        assert count == 0
