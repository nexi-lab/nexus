"""Integration tests for document_skeleton rename sync (Issue #3725, 10A).

Verifies the rename pipeline contract:
    - Old path no longer surfaces in locate() results.
    - New path does surface.
    - Title is preserved (not re-extracted as None due to stale hash).

Uses mock collaborators so the test does not require a live DB or file store.
The important invariant is the SkeletonPipeConsumer rename dispatch sequence:
    delete(old_path) → index(new_path)
which must leave the in-memory daemon index in the correct state.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.bricks.search.skeleton_indexer import SkeletonIndexer
from nexus.contracts.constants import ROOT_ZONE_ID

# ---------------------------------------------------------------------------
# Minimal in-memory stubs
# ---------------------------------------------------------------------------


class _StubBM25:
    """Records upsert/delete calls for assertion."""

    def __init__(self) -> None:
        self.upserted: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    async def upsert_skeleton(self, doc_id, virtual_path, title, zone_id, *, path_id=None) -> None:
        self.upserted.append(
            {"doc_id": doc_id, "virtual_path": virtual_path, "title": title, "zone_id": zone_id}
        )

    async def delete_skeleton(self, doc_id, zone_id) -> None:
        self.deleted.append(doc_id)


class _StubFileReader:
    """Returns pre-configured head bytes keyed by path."""

    def __init__(self, content_map: dict[str, bytes]) -> None:
        self._map = content_map

    async def read_head(self, virtual_path: str, max_bytes: int) -> bytes:
        return self._map.get(virtual_path, b"")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_old_path_deleted_new_path_indexed() -> None:
    """After a rename event, old path is removed and new path is indexed."""
    old_path = "/workspace/src/auth/login.py"
    new_path = "/workspace/src/auth/authenticate.py"
    path_id = "pid-001"
    zone_id = ROOT_ZONE_ID

    content = b'"""User authentication module."""\n'
    reader = _StubFileReader({new_path: content})
    bm25 = _StubBM25()
    indexer = SkeletonIndexer(file_reader=reader, bm25=bm25, async_session_factory=None)

    # Simulate: old path was previously indexed
    await indexer.index_file(
        path_id=path_id,
        virtual_path=old_path,
        zone_id=zone_id,
    )
    assert any(u["virtual_path"] == old_path for u in bm25.upserted)

    # Simulate rename: delete old, index new (as SkeletonPipeConsumer does it)
    await indexer.delete_file(path_id=path_id, virtual_path=old_path, zone_id=zone_id)
    await indexer.index_file(path_id=path_id, virtual_path=new_path, zone_id=zone_id)

    # Old path must have been deleted from BM25
    assert old_path in bm25.deleted, f"expected {old_path!r} in deleted: {bm25.deleted}"

    # New path must be in upserted
    new_upserts = [u for u in bm25.upserted if u["virtual_path"] == new_path]
    assert new_upserts, f"expected {new_path!r} in upserted: {bm25.upserted}"


@pytest.mark.asyncio
async def test_rename_title_preserved_via_fresh_extraction() -> None:
    """After rename, the title is freshly extracted from the new path.

    The skeleton_content_hash for the old path is irrelevant — because the
    path_id is the same but the virtual_path changed, hash comparison is on
    the new content read.  Title should match the module docstring.
    """
    old_path = "/workspace/src/old_name.py"
    new_path = "/workspace/src/new_name.py"
    path_id = "pid-002"
    zone_id = ROOT_ZONE_ID
    expected_title = "Core authentication utility."

    reader = _StubFileReader(
        {
            new_path: f'"""{expected_title}"""\n'.encode(),
        }
    )
    from nexus.bricks.catalog.extractors import SKELETON_EXTRACTOR_REGISTRY

    bm25 = _StubBM25()
    indexer = SkeletonIndexer(
        file_reader=reader,
        bm25=bm25,
        async_session_factory=None,
        extractor_registry=SKELETON_EXTRACTOR_REGISTRY,
    )

    await indexer.delete_file(path_id=path_id, virtual_path=old_path, zone_id=zone_id)
    await indexer.index_file(path_id=path_id, virtual_path=new_path, zone_id=zone_id)

    new_upserts = [u for u in bm25.upserted if u["virtual_path"] == new_path]
    assert new_upserts
    assert new_upserts[-1]["title"] == expected_title


@pytest.mark.asyncio
async def test_rename_does_not_leave_old_path_in_daemon_locate() -> None:
    """After rename, daemon.locate() must not return the old path."""
    from nexus.bricks.search.daemon import SearchDaemon

    old_path = "/workspace/src/auth/old_login.py"
    new_path = "/workspace/src/auth/new_login.py"
    zone_id = ROOT_ZONE_ID

    daemon = SearchDaemon()
    # Manually populate daemon skeleton docs as bootstrap would
    daemon.upsert_skeleton_doc(
        path_id="pid-003",
        virtual_path=old_path,
        title="Old login module.",
        zone_id=zone_id,
    )

    # Simulate rename in daemon
    daemon.delete_skeleton_doc(virtual_path=old_path, zone_id=zone_id)
    daemon.upsert_skeleton_doc(
        path_id="pid-003",
        virtual_path=new_path,
        title="Old login module.",  # title unchanged
        zone_id=zone_id,
    )

    results = await daemon.locate("login", zone_id=zone_id, limit=10)
    paths = [r["path"] for r in results]

    assert old_path not in paths, f"old path leaked after rename: {paths}"
    assert new_path in paths, f"new path missing after rename: {paths}"
