"""Unit tests for CASGarbageCollector (Issue #1320).

Verifies that GC correctly:
- Deletes ref_count=0 blobs past grace period
- Skips ref_count>0 blobs
- Skips ref_count=0 blobs within grace period
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from nexus import CASLocalBackend
from nexus.backends.engines.cas_gc import CASGarbageCollector


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def engine(temp_dir: Path) -> CASLocalBackend:
    return CASLocalBackend(str(temp_dir / "data"))


class TestGCCollect:
    def test_gc_deletes_zero_ref_past_grace(self, engine: CASLocalBackend) -> None:
        """ref_count=0 + released_at past grace → blob deleted."""
        result = engine.write_content(b"garbage")
        content_id = result.content_id
        assert engine.content_exists(content_id)

        # Simulate release + expired grace period
        engine.release_content(content_id)
        meta = engine._read_meta(content_id)
        meta["released_at"] = time.time() - 600  # 10 min ago
        engine._write_meta(content_id, meta)

        gc = CASGarbageCollector(engine, grace_period=300, scan_interval=1)
        gc._collect()

        assert not engine.content_exists(content_id)

    def test_gc_skips_nonzero_ref(self, engine: CASLocalBackend) -> None:
        """ref_count>0 → blob NOT deleted."""
        result = engine.write_content(b"still in use")
        content_id = result.content_id

        gc = CASGarbageCollector(engine, grace_period=0, scan_interval=1)
        gc._collect()

        assert engine.content_exists(content_id)
        assert engine.get_ref_count(content_id) == 1

    def test_gc_skips_within_grace_period(self, engine: CASLocalBackend) -> None:
        """ref_count=0 but released_at within grace → blob NOT deleted yet."""
        result = engine.write_content(b"recently released")
        content_id = result.content_id

        engine.release_content(content_id)
        # released_at is fresh (just set by release_content)

        gc = CASGarbageCollector(engine, grace_period=300, scan_interval=1)
        gc._collect()

        # Should still exist — within grace period
        assert engine.content_exists(content_id)

    def test_gc_zero_grace_deletes_immediately(self, engine: CASLocalBackend) -> None:
        """grace_period=0 → delete immediately after release."""
        result = engine.write_content(b"ephemeral")
        content_id = result.content_id

        engine.release_content(content_id)

        gc = CASGarbageCollector(engine, grace_period=0, scan_interval=1)
        gc._collect()

        assert not engine.content_exists(content_id)
