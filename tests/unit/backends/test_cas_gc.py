"""Unit tests for CasGcService — reachability-based GC (Issue #1772).

Verifies that GC correctly:
- Deletes unreferenced blobs past grace period
- Keeps referenced blobs (content_id in metastore)
- Keeps unreferenced blobs within grace period
- Expands CDC manifests to keep chunk blobs
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from nexus import CASLocalBackend
from nexus.backends.engines.cas_gc import CasGcService


class FakeNexusFs:
    """Tier 1 syscall stub for CAS GC tests.

    CasGcService walks the namespace via ``self._nexus_fs.sys_readdir(
    "/", recursive=True, details=True)`` — the detail-dict shape this
    fake returns matches what the real sys_readdir emits.
    """

    def __init__(self, entries: list[dict] | None = None):
        self._entries = entries or []

    def add(self, path: str, content_id: str) -> None:
        self._entries.append({"path": path, "content_id": content_id})

    def sys_readdir(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        **_kwargs: object,
    ) -> list[dict]:
        assert path == "/"
        assert recursive is True
        assert details is True
        return list(self._entries)


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def engine(temp_dir: Path) -> CASLocalBackend:
    return CASLocalBackend(str(temp_dir / "data"))


class TestGCReachability:
    def test_gc_deletes_unreferenced_past_grace(self, engine: CASLocalBackend) -> None:
        """Unreferenced blob past grace period → deleted."""
        result = engine.write_content(b"orphan data")
        content_id = result.content_id
        assert engine.content_exists(content_id)

        # NexusFS has no reference to this blob
        nexus_fs = FakeNexusFs()

        # Backdate the blob's mtime to exceed grace period
        blob_key = engine._blob_key(content_id)
        blob_path = engine._transport._resolve(blob_key)
        old_time = time.time() - 600  # 10 min ago
        os.utime(str(blob_path), (old_time, old_time))

        gc = CasGcService(engine, nexus_fs, grace_period=300, scan_interval=1)
        gc._collect()

        assert not engine.content_exists(content_id)

    def test_gc_keeps_referenced_blob(self, engine: CASLocalBackend) -> None:
        """Referenced blob (content_id in metastore) → NOT deleted."""
        result = engine.write_content(b"still in use")
        content_id = result.content_id

        nexus_fs = FakeNexusFs()
        nexus_fs.add("/file.txt", content_id)

        gc = CasGcService(engine, nexus_fs, grace_period=0, scan_interval=1)
        gc._collect()

        assert engine.content_exists(content_id)

    def test_gc_keeps_unreferenced_within_grace(self, engine: CASLocalBackend) -> None:
        """Unreferenced but fresh blob (within grace period) → NOT deleted."""
        result = engine.write_content(b"recently written")
        content_id = result.content_id

        nexus_fs = FakeNexusFs()

        gc = CasGcService(engine, nexus_fs, grace_period=300, scan_interval=1)
        gc._collect()

        # Should still exist — within grace period (just written)
        assert engine.content_exists(content_id)

    def test_gc_zero_grace_deletes_immediately(self, engine: CASLocalBackend) -> None:
        """grace_period=0 → delete unreferenced immediately."""
        result = engine.write_content(b"ephemeral")
        content_id = result.content_id

        nexus_fs = FakeNexusFs()

        gc = CasGcService(engine, nexus_fs, grace_period=0, scan_interval=1)
        gc._collect()

        assert not engine.content_exists(content_id)

    def test_gc_no_nexus_fs_skips(self, engine: CASLocalBackend) -> None:
        """No NexusFS injected → skip collection, don't delete anything."""
        result = engine.write_content(b"safe data")
        content_id = result.content_id

        gc = CasGcService(engine, nexus_fs=None, grace_period=0, scan_interval=1)
        gc._collect()

        assert engine.content_exists(content_id)
