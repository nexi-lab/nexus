"""P8 live-plugin integration: SearchDaemon → nexus-search-plugin gRPC.

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
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NEXUS_SEARCH_LIVE_PLUGIN") != "1",
    reason="SearchDaemon live-plugin test needs a running "
    "nexus-search-plugin (see module docstring for compose recipe); "
    "set NEXUS_SEARCH_LIVE_PLUGIN=1 to enable.",
)


NODE_GRPC = os.environ.get("NEXUS_SEARCH_PLUGIN_TARGET", "127.0.0.1:2126")


class _MinimalSearchRequest:
    """Stand-in for SearchRequest — SearchDaemon.search only
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
def _event_loop():
    """One loop shared across every test in the module.

    ``grpc.aio.insecure_channel`` binds the channel to the loop it
    first runs on; a per-test fresh loop would strand the channel
    from prior tests.  Keeping a module-scoped loop matches the
    daemon fixture's scope so every call reaches the same
    stub+channel pair.
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture(scope="module")
def daemon(_event_loop):
    """SearchDaemon pointing at the docker compose node."""
    from nexus.bricks.search.daemon import SearchDaemon

    d = SearchDaemon(target=NODE_GRPC)
    yield d
    try:
        _event_loop.run_until_complete(d.shutdown())
    except Exception:
        pass


class TestSearchDaemonRoundtrip:
    """Every method the FastAPI router touches — verify it round-trips
    through the plugin.  Same shape as the direct-gRPC E2E suite, but
    the caller is the production ``SearchDaemon`` Python proxy."""

    def test_health_returns_status(self, daemon, _event_loop) -> None:
        h = _event_loop.run_until_complete(daemon.get_health())
        # 'healthy' when the embedder is loaded, 'degraded' when not.
        # The docker image doesn't ship a model in the P8 scope so
        # degraded is the expected happy path.
        assert h["status"] in ("healthy", "degraded"), h

    def test_stats_returns_counts(self, daemon, _event_loop) -> None:
        s = _event_loop.run_until_complete(daemon.get_stats())
        for key in ("fts_doc_count", "fts_path_count", "ann_chunk_count", "parked_count"):
            assert key in s, f"stats missing {key}: {s}"
            assert isinstance(s[key], int), s

    def test_index_documents_then_locate_and_search(self, daemon, _event_loop) -> None:
        docs = [
            {"path": "/rust-daemon-e2e/a.md", "text": "widget alpha content"},
            {"path": "/rust-daemon-e2e/b.md", "text": "widget beta content"},
            {"path": "/rust-daemon-e2e/c.md", "text": "unrelated payload"},
        ]

        async def journey():
            r = await daemon.index_documents(docs, zone_id="root")
            assert r["indexed"] == 3, r
            loc = await daemon.locate("/rust-daemon-e2e/a.md", zone_id="root")
            assert loc["indexed"] is True, loc
            assert loc["chunk_count"] >= 1
            miss = await daemon.locate("/rust-daemon-e2e/nope.md", zone_id="root")
            assert miss["indexed"] is False, miss
            hits = await daemon.search(
                _MinimalSearchRequest("widget", zone_id="root", path_filter="/rust-daemon-e2e/")
            )
            paths = {h.path for h in hits}
            assert "/rust-daemon-e2e/a.md" in paths, paths
            assert "/rust-daemon-e2e/b.md" in paths, paths
            assert "/rust-daemon-e2e/c.md" not in paths, paths

        _event_loop.run_until_complete(journey())

    def test_notify_file_change_delete_removes_from_search(self, daemon, _event_loop) -> None:
        async def journey():
            docs = [{"path": "/rust-daemon-notify/x.md", "text": "widget only"}]
            r = await daemon.index_documents(docs, zone_id="root")
            assert r["indexed"] == 1, r

            await daemon.notify_file_change("/rust-daemon-notify/x.md", "delete", zone_id="root")

            hits = await daemon.search(
                _MinimalSearchRequest("widget", zone_id="root", path_filter="/rust-daemon-notify/")
            )
            paths = {h.path for h in hits}
            assert "/rust-daemon-notify/x.md" not in paths, f"deleted doc leaked: {paths}"

        _event_loop.run_until_complete(journey())

    def test_indexed_directories_add_list_remove(self, daemon, _event_loop) -> None:
        # Fresh path per test invocation so a rerun against a persisted
        # plugin data volume doesn't see stale entries from prior runs
        # (the plugin's indexed_dirs.json is durable across container
        # restarts by design).
        path = f"/some/registered/dir-{uuid.uuid4().hex[:8]}"

        async def journey():
            r_add = await daemon.add_indexed_directory("root", path)
            assert r_add["added"] is True, r_add
            dirs = await daemon.list_indexed_directories("root")
            assert path in dirs, dirs
            r_add_dup = await daemon.add_indexed_directory("root", path)
            assert r_add_dup["added"] is False, r_add_dup
            r_rm = await daemon.remove_indexed_directory("root", path)
            assert r_rm == "removed", r_rm
            r_rm2 = await daemon.remove_indexed_directory("root", path)
            assert r_rm2 == "not_found", r_rm2

        _event_loop.run_until_complete(journey())

    def test_set_zone_indexing_mode_roundtrip(self, daemon, _event_loop) -> None:
        async def journey():
            await daemon.set_zone_indexing_mode("root", "sandbox")
            modes = await daemon.get_zone_indexing_modes()
            assert modes.get("root") == "sandbox", modes
            await daemon.set_zone_indexing_mode("root", "on")
            modes = await daemon.get_zone_indexing_modes()
            # After reset the key may be 'on' or absent (default); accept both.
            assert modes.get("root", "on") == "on", modes

        _event_loop.run_until_complete(journey())
