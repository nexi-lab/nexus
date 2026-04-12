"""Integration tests for zone isolation in document_skeleton / locate() (Issue #3725, 12A).

Verifies the core invariant: a locate() query in zone A must not return paths
from zone B, even if both zones contain documents matching the query.

Tests cover:
    - daemon.locate() zone filter (unit-level, no DB)
    - SkeletonIndexer zone stamping on upsert
    - Two-zone scenario: zone A query returns only zone A paths

These tests use in-memory daemon state (no DB, no file store) to keep
the invariant verifiable without infrastructure dependencies.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.bricks.search.daemon import SearchDaemon
from nexus.bricks.search.skeleton_indexer import SkeletonIndexer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _populate_daemon(daemon: SearchDaemon, docs: list[dict[str, Any]]) -> None:
    """Directly populate daemon skeleton index for test setup."""
    for doc in docs:
        daemon.upsert_skeleton_doc(
            path_id=doc["path_id"],
            virtual_path=doc["path"],
            title=doc.get("title"),
            zone_id=doc["zone_id"],
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locate_returns_only_zone_a_results() -> None:
    """Zone A query must not surface paths from zone B."""
    daemon = SearchDaemon()

    zone_a_path = "/workspace-a/src/auth/login.py"
    zone_b_path = "/workspace-b/src/auth/login.py"

    _populate_daemon(
        daemon,
        [
            {
                "path_id": "pid-a1",
                "path": zone_a_path,
                "title": "Login module",
                "zone_id": "zone-a",
            },
            {
                "path_id": "pid-b1",
                "path": zone_b_path,
                "title": "Login module",
                "zone_id": "zone-b",
            },
        ],
    )

    results = await daemon.locate("login", zone_id="zone-a", limit=20)
    paths = [r["path"] for r in results]

    assert zone_a_path in paths, f"zone A path missing: {paths}"
    assert zone_b_path not in paths, f"zone B path leaked into zone A query: {paths}"


@pytest.mark.asyncio
async def test_locate_zone_b_does_not_surface_zone_a_paths() -> None:
    """Symmetric: zone B query must not surface zone A paths."""
    daemon = SearchDaemon()

    zone_a_path = "/workspace-a/src/auth/oauth.py"
    zone_b_path = "/workspace-b/src/auth/oauth.py"

    _populate_daemon(
        daemon,
        [
            {
                "path_id": "pid-a2",
                "path": zone_a_path,
                "title": "OAuth middleware",
                "zone_id": "zone-a",
            },
            {
                "path_id": "pid-b2",
                "path": zone_b_path,
                "title": "OAuth middleware",
                "zone_id": "zone-b",
            },
        ],
    )

    results = await daemon.locate("oauth", zone_id="zone-b", limit=20)
    paths = [r["path"] for r in results]

    assert zone_b_path in paths
    assert zone_a_path not in paths, f"zone A path leaked into zone B query: {paths}"


@pytest.mark.asyncio
async def test_locate_empty_zone_returns_no_results() -> None:
    """Query against a zone with no skeleton docs returns empty list."""
    daemon = SearchDaemon()

    _populate_daemon(
        daemon,
        [
            {
                "path_id": "pid-x1",
                "path": "/ws/src/util.py",
                "title": "Utilities",
                "zone_id": "zone-x",
            },
        ],
    )

    results = await daemon.locate("util", zone_id="zone-y", limit=20)
    assert results == [], f"expected empty for zone-y, got {results}"


@pytest.mark.asyncio
async def test_skeleton_indexer_stamps_zone_id_on_upsert() -> None:
    """SkeletonIndexer must stamp the correct zone_id when calling bm25.upsert_skeleton."""

    class _TrackingBM25:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def upsert_skeleton(
            self, doc_id, virtual_path, title, zone_id, *, path_id=None
        ) -> None:
            self.calls.append({"doc_id": doc_id, "zone_id": zone_id})

        async def delete_skeleton(self, doc_id, zone_id) -> None:
            pass

    class _FakeReader:
        async def read_head(self, virtual_path, max_bytes) -> bytes:
            return b"# header\n"

    bm25 = _TrackingBM25()
    indexer = SkeletonIndexer(file_reader=_FakeReader(), bm25=bm25, async_session_factory=None)

    await indexer.index_file(
        path_id="pid-zone",
        virtual_path="/workspace/src/main.py",
        zone_id="expected-zone",
    )

    assert bm25.calls, "no upsert_skeleton call made"
    assert bm25.calls[0]["zone_id"] == "expected-zone"


@pytest.mark.asyncio
async def test_two_zone_full_pipeline_zone_isolation() -> None:
    """Full two-zone scenario: index files in both zones, verify query isolation.

    This is the canonical zone isolation integration test for the skeleton feature.
    """
    daemon = SearchDaemon()

    auth_a = "/zone-a/src/auth/handler.py"
    auth_b = "/zone-b/src/auth/handler.py"
    unrelated = "/zone-a/src/unrelated/other.py"

    _populate_daemon(
        daemon,
        [
            {
                "path_id": "pa1",
                "path": auth_a,
                "title": "Authentication handler",
                "zone_id": "zone-a",
            },
            {
                "path_id": "pb1",
                "path": auth_b,
                "title": "Authentication handler",
                "zone_id": "zone-b",
            },
            {"path_id": "pa2", "path": unrelated, "title": "Unrelated module", "zone_id": "zone-a"},
        ],
    )

    # Zone A query for "authentication" should surface only zone-a paths
    zone_a_results = await daemon.locate("authentication", zone_id="zone-a", limit=20)
    zone_a_paths = {r["path"] for r in zone_a_results}

    assert auth_a in zone_a_paths
    assert auth_b not in zone_a_paths, "Zone B auth handler leaked into zone A query"

    # Zone B query for "authentication" should surface only zone-b paths
    zone_b_results = await daemon.locate("authentication", zone_id="zone-b", limit=20)
    zone_b_paths = {r["path"] for r in zone_b_results}

    assert auth_b in zone_b_paths
    assert auth_a not in zone_b_paths, "Zone A auth handler leaked into zone B query"
    assert unrelated not in zone_b_paths, "Zone A unrelated leaked into zone B query"
