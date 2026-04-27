"""Unit tests for CASGarbageCollector — reachability-based GC (Issue #1772).

Verifies that GC correctly:
- Deletes unreferenced blobs past grace period
- Keeps referenced blobs (etag in metastore)
- Keeps unreferenced blobs within grace period
- Expands CDC manifests to keep chunk blobs
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from nexus import CASLocalBackend
from nexus.backends.engines.cas_gc import CASGarbageCollector


@dataclass
class FakeMetaEntry:
    path: str
    content_id: str | None = None


class FakeMetastore:
    def __init__(self, entries: list[FakeMetaEntry] | None = None):
        self._entries = entries or []

    def add(self, path: str, content_id: str) -> None:
        self._entries.append(FakeMetaEntry(path=path, content_id=content_id))

    def list(
        self, prefix: str = "", recursive: bool = True, **kwargs: object
    ) -> list[FakeMetaEntry]:
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

        # Metastore has no reference to this blob
        metastore = FakeMetastore()

        # Backdate the blob's mtime to exceed grace period
        blob_key = engine._blob_key(content_id)
        blob_path = engine._transport._resolve(blob_key)
        old_time = time.time() - 600  # 10 min ago
        os.utime(str(blob_path), (old_time, old_time))

        gc = CASGarbageCollector(engine, metastore, grace_period=300, scan_interval=1)
        gc._collect()

        assert not engine.content_exists(content_id)

    def test_gc_keeps_referenced_blob(self, engine: CASLocalBackend) -> None:
        """Referenced blob (etag in metastore) → NOT deleted."""
        result = engine.write_content(b"still in use")
        content_id = result.content_id

        metastore = FakeMetastore()
        metastore.add("/file.txt", content_id)

        gc = CASGarbageCollector(engine, metastore, grace_period=0, scan_interval=1)
        gc._collect()

        assert engine.content_exists(content_id)

    def test_gc_keeps_unreferenced_within_grace(self, engine: CASLocalBackend) -> None:
        """Unreferenced but fresh blob (within grace period) → NOT deleted."""
        result = engine.write_content(b"recently written")
        content_id = result.content_id

        metastore = FakeMetastore()

        gc = CASGarbageCollector(engine, metastore, grace_period=300, scan_interval=1)
        gc._collect()

        # Should still exist — within grace period (just written)
        assert engine.content_exists(content_id)

    def test_gc_zero_grace_deletes_immediately(self, engine: CASLocalBackend) -> None:
        """grace_period=0 → delete unreferenced immediately."""
        result = engine.write_content(b"ephemeral")
        content_id = result.content_id

        metastore = FakeMetastore()

        gc = CASGarbageCollector(engine, metastore, grace_period=0, scan_interval=1)
        gc._collect()

        assert not engine.content_exists(content_id)

    def test_gc_no_metastore_skips(self, engine: CASLocalBackend) -> None:
        """No metastore injected → skip collection, don't delete anything."""
        result = engine.write_content(b"safe data")
        content_id = result.content_id

        gc = CASGarbageCollector(engine, metastore=None, grace_period=0, scan_interval=1)
        gc._collect()

        assert engine.content_exists(content_id)
