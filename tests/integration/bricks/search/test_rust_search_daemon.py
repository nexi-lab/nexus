"""P8 live-plugin integration: RustSearchDaemon → nexus-search-plugin gRPC.

Requires a running nexus-search-plugin cdylib inside a
nexusd-cluster reachable at NEXUS_SEARCH_PLUGIN_TARGET (default
127.0.0.1:2126) — the docker-compose.search-plugin-e2e.yml stack
provisions exactly that.  The gate env var
NEXUS_SEARCH_LIVE_PLUGIN=1 opts callers in; without it the whole
module skips so CI runs that don't have the plugin available
don't fail collection.

Lives under tests/integration/bricks/search/ rather than
tests/e2e/docker/ because it imports nexus.bricks.search directly
— the docker E2E runner image (Dockerfile.federation-test) ships
a slim /opt/proto stub tree that deliberately doesn't include
nexus.bricks.  Run against a real nexus Python env alongside the
docker compose:

    docker compose -f dockerfiles/docker-compose.search-plugin-e2e.yml up -d node
    NEXUS_SEARCH_LIVE_PLUGIN=1 NEXUS_SEARCH_PLUGIN_TARGET=127.0.0.1:2126 \\
        pytest tests/integration/bricks/search/test_rust_search_daemon.py

Verifies every SearchBrickProtocol method the P8 roadmap promised:
search, index_documents, notify_file_change, locate, list_parked,
add_indexed_directory / remove_indexed_directory /
list_indexed_directories, set_zone_indexing_mode, get_health,
get_stats.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NEXUS_SEARCH_LIVE_PLUGIN") != "1",
    reason="RustSearchDaemon live-plugin test needs a running "
    "nexus-search-plugin (see module docstring for compose recipe); "
    "set NEXUS_SEARCH_LIVE_PLUGIN=1 to enable.",
)


NODE_GRPC = os.environ.get("NEXUS_SEARCH_PLUGIN_TARGET", "127.0.0.1:2126")


class _MinimalSearchRequest:
    """Stand-in for SearchRequest — RustSearchDaemon.search only
    reads a fixed set of attributes.  Using a small class instead of
    the real SearchRequest keeps this test file free of the wider
    nexus.contracts import chain."""

    def __init__(
        self,
        query: str,
        *,
        search_type: str = "keyword",
        limit: int = 10,
        zone_id: str | None = None,
        path_filter: str | None = None,
        alpha: float = 0.0,
        fusion_method: str = "",
        rrf_k: int = 0,
        expand: str = "",
        recency: str = "",
        recency_weight: float = 0.0,
        recency_half_life_days: float = 0.0,
    ) -> None:
        self.query = query
        self.search_type = search_type
        self.limit = limit
        self.zone_id = zone_id
        self.path_filter = path_filter
        self.alpha = alpha
        self.fusion_method = fusion_method
        self.rrf_k = rrf_k
        self.expand = expand
        self.recency = recency
        self.recency_weight = recency_weight
        self.recency_half_life_days = recency_half_life_days


@pytest.fixture(scope="module")
def daemon():
    """RustSearchDaemon pointing at the docker compose node."""
    from nexus.bricks.search.rust_daemon import RustSearchDaemon

    d = RustSearchDaemon(target=NODE_GRPC)
    yield d
    # Shutdown is best-effort — the module fixture teardown runs
    # after pytest closes its event loop, so a proper close needs
    # its own loop.
    try:
        asyncio.new_event_loop().run_until_complete(d.shutdown())
    except Exception:
        pass


class TestRustSearchDaemonRoundtrip:
    """Every method the FastAPI router touches — verify it round-
    trips through the plugin.  Same shape as the direct-gRPC E2E
    suite, but the caller is RustSearchDaemon (what production
    uses at SEARCH_BACKEND=rust)."""

    def test_health_returns_status(self, daemon) -> None:
        h = daemon.get_health()
        # 'healthy' when the embedder is loaded, 'degraded' when
        # not.  The docker image doesn't ship a model in the P8
        # scope so degraded is the expected happy path.
        assert h["status"] in ("healthy", "degraded"), h

    def test_stats_returns_counts(self, daemon) -> None:
        s = daemon.get_stats()
        for key in ("fts_doc_count", "fts_path_count", "ann_chunk_count", "parked_count"):
            assert key in s, f"stats missing {key}: {s}"
            assert isinstance(s[key], int), s

    def test_index_documents_then_locate_and_search(self, daemon) -> None:
        # Seed 3 docs via IndexDocuments.
        docs = [
            {"path": "/rust-daemon-e2e/a.md", "text": "widget alpha content"},
            {"path": "/rust-daemon-e2e/b.md", "text": "widget beta content"},
            {"path": "/rust-daemon-e2e/c.md", "text": "unrelated payload"},
        ]

        async def _run():
            r = await daemon.index_documents(docs, zone_id="root")
            assert r["indexed"] == 3, r
            # Locate — path exists.
            loc = await daemon.locate("/rust-daemon-e2e/a.md", zone_id="root")
            assert loc["indexed"] is True, loc
            assert loc["chunk_count"] >= 1
            # Locate — missing path.
            miss = await daemon.locate("/rust-daemon-e2e/nope.md", zone_id="root")
            assert miss["indexed"] is False, miss
            # Search — 2 hits for 'widget'.
            hits = await daemon.search(
                _MinimalSearchRequest("widget", zone_id="root", path_filter="/rust-daemon-e2e/")
            )
            paths = {h.path for h in hits}
            assert "/rust-daemon-e2e/a.md" in paths, paths
            assert "/rust-daemon-e2e/b.md" in paths, paths
            assert "/rust-daemon-e2e/c.md" not in paths, paths

        asyncio.new_event_loop().run_until_complete(_run())

    def test_notify_file_change_delete_removes_from_search(self, daemon) -> None:
        async def _run():
            docs = [
                {"path": "/rust-daemon-notify/x.md", "text": "widget only"},
            ]
            r = await daemon.index_documents(docs, zone_id="root")
            assert r["indexed"] == 1, r

            # Delete via NotifyFileChange.
            await daemon.notify_file_change("/rust-daemon-notify/x.md", "delete", zone_id="root")

            # Search must not surface it.
            hits = await daemon.search(
                _MinimalSearchRequest("widget", zone_id="root", path_filter="/rust-daemon-notify/")
            )
            paths = {h.path for h in hits}
            assert "/rust-daemon-notify/x.md" not in paths, f"deleted doc leaked: {paths}"

        asyncio.new_event_loop().run_until_complete(_run())

    def test_indexed_directories_add_list_remove(self, daemon) -> None:
        async def _run():
            r_add = await daemon.add_indexed_directory("root", "/some/registered/dir")
            assert r_add["added"] is True, r_add
            # List surfaces it.
            dirs = daemon.list_indexed_directories("root")
            assert "/some/registered/dir" in dirs, dirs
            # Second add is idempotent.
            r_add_dup = await daemon.add_indexed_directory("root", "/some/registered/dir")
            assert r_add_dup["added"] is False, r_add_dup
            # Remove.
            r_rm = await daemon.remove_indexed_directory("root", "/some/registered/dir")
            assert r_rm == "removed", r_rm
            # Not found after remove.
            r_rm2 = await daemon.remove_indexed_directory("root", "/some/registered/dir")
            assert r_rm2 == "not_found", r_rm2

        asyncio.new_event_loop().run_until_complete(_run())

    def test_set_zone_indexing_mode_roundtrip(self, daemon) -> None:
        async def _run():
            await daemon.set_zone_indexing_mode("root", "sandbox")
            modes = daemon._zone_indexing_modes
            assert modes.get("root") == "sandbox", modes
            # Reset.
            await daemon.set_zone_indexing_mode("root", "on")
            modes = daemon._zone_indexing_modes
            # After reset the key may be 'on' or absent (default);
            # accept both.
            assert modes.get("root", "on") == "on", modes

        asyncio.new_event_loop().run_until_complete(_run())

    def test_parked_queue_list_shape(self, daemon) -> None:
        # Rust plugin doesn't currently populate the parked queue
        # (do_index_documents doesn't park on failure yet — see the
        # step 4 commit body).  Just verify the list surface is
        # dial-able and returns the expected dict shape.
        parked = daemon.list_parked()
        assert isinstance(parked, dict), parked
        for zone, entries in parked.items():
            assert isinstance(zone, str)
            assert isinstance(entries, list)
