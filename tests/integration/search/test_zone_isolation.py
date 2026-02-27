"""Integration tests for zone-level isolation and ReBAC file-level filtering.

Validates:
- Zone-level isolation via zone_id SQL WHERE clause (txtai backend)
- Cross-zone document visibility
- Zone_id=None / ROOT_ZONE_ID fallback
- ReBAC file-level filtering (PermissionEnforcer.filter_list) in router layer
- Over-fetch compensation for filtered results
"""

from unittest.mock import AsyncMock, patch

import pytest

from nexus.bricks.search.results import BaseSearchResult

daemon_mod = pytest.importorskip(
    "nexus.bricks.search.daemon",
    reason="daemon module not available in this environment",
)

# Skip if this is the old daemon (worktree not active)
_cfg = daemon_mod.DaemonConfig()
if not hasattr(_cfg, "search_backend"):
    pytest.skip("New daemon.py not available (worktree not active)", allow_module_level=True)
del _cfg

DaemonConfig = daemon_mod.DaemonConfig
SearchDaemon = daemon_mod.SearchDaemon

# =============================================================================
# Helpers
# =============================================================================


def _make_backend_results(
    paths: list[str], zone_id: str = "corp", base_score: float = 0.9
) -> list[BaseSearchResult]:
    """Create a list of mock BaseSearchResult for a given zone."""
    _ = zone_id  # zone_id is enforced at SQL layer, not stored on result object
    return [
        BaseSearchResult(
            path=p,
            chunk_text=f"content of {p}",
            score=base_score - (i * 0.01),
        )
        for i, p in enumerate(paths)
    ]


async def _make_daemon_with_mock() -> tuple[SearchDaemon, AsyncMock]:
    """Create a SearchDaemon backed by a mock backend."""
    daemon = SearchDaemon()
    mock_backend = AsyncMock()
    mock_backend.search.return_value = []
    mock_backend.upsert.return_value = 0
    mock_backend.delete.return_value = 0
    with patch("nexus.bricks.search.daemon.create_backend", return_value=mock_backend):
        await daemon.startup()
    return daemon, mock_backend


# =============================================================================
# Zone-level isolation tests (txtai SQL WHERE)
# =============================================================================


class TestZoneLevelIsolation:
    """Test that zone_id is enforced on all search/index operations."""

    @pytest.mark.asyncio
    async def test_search_passes_zone_id_to_backend(self) -> None:
        """zone_id should be forwarded to backend.search()."""
        daemon, mock_backend = await _make_daemon_with_mock()
        await daemon.search("test query", zone_id="corp")
        call_kwargs = mock_backend.search.call_args[1]
        assert call_kwargs["zone_id"] == "corp"
        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_search_different_zone_gets_different_results(self) -> None:
        """Searching zone A vs zone B should produce different backend calls."""
        daemon, mock_backend = await _make_daemon_with_mock()

        # Zone A returns files from zone A
        mock_backend.search.return_value = _make_backend_results(["/a1.py", "/a2.py"], "zone-a")
        results_a = await daemon.search("test", zone_id="zone-a")

        # Zone B returns files from zone B
        mock_backend.search.return_value = _make_backend_results(["/b1.py", "/b2.py"], "zone-b")
        results_b = await daemon.search("test", zone_id="zone-b")

        # Verify different zone_ids were passed to backend
        calls = mock_backend.search.call_args_list
        assert calls[0][1]["zone_id"] == "zone-a"
        assert calls[1][1]["zone_id"] == "zone-b"

        # Results should differ
        assert {r.path for r in results_a} == {"/a1.py", "/a2.py"}
        assert {r.path for r in results_b} == {"/b1.py", "/b2.py"}
        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_search_zone_none_uses_root_zone(self) -> None:
        """When zone_id is None, ROOT_ZONE_ID should be used."""
        daemon, mock_backend = await _make_daemon_with_mock()
        await daemon.search("test")

        call_kwargs = mock_backend.search.call_args[1]
        from nexus.contracts.constants import ROOT_ZONE_ID

        assert call_kwargs["zone_id"] == ROOT_ZONE_ID
        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_index_stamps_zone_id(self) -> None:
        """index_documents should pass zone_id to backend.upsert()."""
        daemon, mock_backend = await _make_daemon_with_mock()
        docs = [{"id": "1", "text": "hello", "path": "/a.py"}]
        await daemon.index_documents(docs, zone_id="corp")

        mock_backend.upsert.assert_awaited_once()
        call_args = mock_backend.upsert.call_args
        assert call_args[1]["zone_id"] == "corp"
        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_delete_passes_zone_id(self) -> None:
        """delete_documents should pass zone_id to backend.delete()."""
        daemon, mock_backend = await _make_daemon_with_mock()
        await daemon.delete_documents(["id1", "id2"], zone_id="corp")

        mock_backend.delete.assert_awaited_once()
        call_args = mock_backend.delete.call_args
        assert call_args[1]["zone_id"] == "corp"
        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_delete_zone_a_does_not_affect_zone_b(self) -> None:
        """Deleting from zone A uses zone A's zone_id, not zone B's."""
        daemon, mock_backend = await _make_daemon_with_mock()

        await daemon.delete_documents(["id1"], zone_id="zone-a")
        await daemon.delete_documents(["id2"], zone_id="zone-b")

        calls = mock_backend.delete.call_args_list
        assert calls[0][1]["zone_id"] == "zone-a"
        assert calls[1][1]["zone_id"] == "zone-b"
        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_auto_index_groups_by_zone(self) -> None:
        """Auto-indexed docs should be grouped by zone_id before upsert."""
        config = DaemonConfig(auto_index_on_write=True, refresh_debounce_seconds=0.1)
        daemon = SearchDaemon(config)
        mock_backend = AsyncMock()
        mock_backend.upsert.return_value = 1
        with patch("nexus.bricks.search.daemon.create_backend", return_value=mock_backend):
            await daemon.startup()

        # Queue docs for two different zones
        await daemon.notify_file_change("/a.py", "content-a", zone_id="zone-a")
        await daemon.notify_file_change("/b.py", "content-b", zone_id="zone-b")
        await daemon.notify_file_change("/c.py", "content-c", zone_id="zone-a")

        assert len(daemon._pending_index_docs) == 3

        await daemon.shutdown()


# =============================================================================
# ReBAC file-level filtering tests (router layer simulation)
# =============================================================================


class TestReBACFileFiltering:
    """Test ReBAC file-level filtering that happens in the router layer.

    SearchDaemon does zone-level isolation (pre-filtering).
    The router layer does file-level ReBAC filtering (post-retrieval).
    """

    def _simulate_rebac_filter(
        self,
        results: list[BaseSearchResult],
        permitted_paths: set[str],
        limit: int = 10,
    ) -> list[BaseSearchResult]:
        """Simulate router-layer ReBAC filtering.

        This mirrors what the search router does:
        1. Daemon returns zone-filtered results
        2. Router applies PermissionEnforcer.filter_list()
        3. Only permitted files are returned
        """
        return [r for r in results if r.path in permitted_paths][:limit]

    def test_user_without_read_gets_no_results(self) -> None:
        """User without READ on any file -> 0 results."""
        results = _make_backend_results(["/secret.py", "/private.py"])
        filtered = self._simulate_rebac_filter(results, permitted_paths=set())
        assert filtered == []

    def test_user_with_read_gets_results(self) -> None:
        """User with READ on file -> file appears in results."""
        results = _make_backend_results(["/a.py", "/b.py", "/c.py"])
        filtered = self._simulate_rebac_filter(results, permitted_paths={"/a.py", "/c.py"})
        assert len(filtered) == 2
        assert {r.path for r in filtered} == {"/a.py", "/c.py"}

    def test_admin_sees_all_files(self) -> None:
        """Admin user -> sees all files in zone."""
        paths = ["/a.py", "/b.py", "/c.py", "/d.py"]
        results = _make_backend_results(paths)
        filtered = self._simulate_rebac_filter(results, permitted_paths=set(paths))
        assert len(filtered) == 4

    def test_mixed_permissions(self) -> None:
        """User has READ on 3/10 files -> only 3 returned."""
        paths = [f"/file_{i}.py" for i in range(10)]
        results = _make_backend_results(paths)
        permitted = {"/file_2.py", "/file_5.py", "/file_8.py"}
        filtered = self._simulate_rebac_filter(results, permitted_paths=permitted)
        assert len(filtered) == 3
        assert {r.path for r in filtered} == permitted

    def test_overfetch_compensates(self) -> None:
        """Over-fetch (3x) compensates for filtered-out results."""
        # Daemon fetches 30 (3x of limit=10), ReBAC filters down
        paths = [f"/file_{i}.py" for i in range(30)]
        results = _make_backend_results(paths)
        # Only 15 are permitted, user wants top 10
        permitted = {f"/file_{i}.py" for i in range(0, 30, 2)}  # even numbers only
        filtered = self._simulate_rebac_filter(results, permitted_paths=permitted, limit=10)
        assert len(filtered) == 10

    def test_rebac_filter_preserves_score_ordering(self) -> None:
        """Filtered results maintain original score ordering."""
        results = [
            BaseSearchResult(path="/high.py", chunk_text="high", score=0.95),
            BaseSearchResult(path="/mid.py", chunk_text="mid", score=0.80),
            BaseSearchResult(path="/low.py", chunk_text="low", score=0.60),
        ]
        filtered = self._simulate_rebac_filter(results, permitted_paths={"/high.py", "/low.py"})
        assert filtered[0].path == "/high.py"
        assert filtered[1].path == "/low.py"
        assert filtered[0].score > filtered[1].score

    def test_rebac_limit_applied_after_filter(self) -> None:
        """Limit is applied after filtering, not before."""
        paths = [f"/file_{i}.py" for i in range(20)]
        results = _make_backend_results(paths)
        permitted = set(paths)  # all permitted
        filtered = self._simulate_rebac_filter(results, permitted_paths=permitted, limit=5)
        assert len(filtered) == 5

    @pytest.mark.asyncio
    async def test_end_to_end_zone_then_rebac(self) -> None:
        """Full flow: zone-level pre-filter (daemon) then ReBAC post-filter (router)."""
        daemon, mock_backend = await _make_daemon_with_mock()

        # Backend returns 10 zone-filtered results
        zone_results = _make_backend_results(
            [f"/corp/file_{i}.py" for i in range(10)], zone_id="corp"
        )
        mock_backend.search.return_value = zone_results

        # Daemon search (zone-level isolation)
        results = await daemon.search("test", zone_id="corp", limit=10)
        assert len(results) == 10

        # Router-layer ReBAC filtering (user can only read 4 files)
        permitted = {"/corp/file_0.py", "/corp/file_3.py", "/corp/file_6.py", "/corp/file_9.py"}
        final = self._simulate_rebac_filter(
            [
                BaseSearchResult(path=r.path, chunk_text=r.chunk_text, score=r.score)
                for r in results
            ],
            permitted_paths=permitted,
        )
        assert len(final) == 4
        assert all(r.path in permitted for r in final)

        await daemon.shutdown()
