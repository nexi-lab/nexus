"""Unit tests for CASAddressingEngine.release_content() (Issue #1320).

Verifies ref_count decrement without physical delete, and released_at timestamp.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nexus import CASLocalBackend


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def engine(temp_dir: Path) -> CASLocalBackend:
    return CASLocalBackend(str(temp_dir / "data"))


class TestReleaseContent:
    def test_release_decrements_ref_count(self, engine: CASLocalBackend) -> None:
        result = engine.write_content(b"hello world")
        content_id = result.content_id
        assert engine.get_ref_count(content_id) == 1

        engine.release_content(content_id)
        # ref_count should be 0 but blob still exists (GC hasn't run)
        meta = engine._read_meta(content_id)
        assert meta["ref_count"] == 0
        assert engine.content_exists(content_id)

    def test_release_sets_released_at(self, engine: CASLocalBackend) -> None:
        result = engine.write_content(b"timestamp test")
        content_id = result.content_id

        engine.release_content(content_id)
        meta = engine._read_meta(content_id)
        assert "released_at" in meta
        assert meta["released_at"] > 0

    def test_release_idempotent_on_missing(self, engine: CASLocalBackend) -> None:
        # Should not raise on nonexistent content
        engine.release_content("0000000000000000000000000000000000000000000000000000000000000000")

    def test_release_does_not_go_negative(self, engine: CASLocalBackend) -> None:
        result = engine.write_content(b"once")
        content_id = result.content_id

        engine.release_content(content_id)
        engine.release_content(content_id)  # second release
        meta = engine._read_meta(content_id)
        assert meta["ref_count"] == 0  # not -1

    def test_dedup_release_keeps_other_refs(self, engine: CASLocalBackend) -> None:
        """Two writes of same content → ref_count=2. Release once → ref_count=1."""
        r1 = engine.write_content(b"shared content")
        r2 = engine.write_content(b"shared content")
        assert r1.content_id == r2.content_id
        assert engine.get_ref_count(r1.content_id) == 2

        engine.release_content(r1.content_id)
        assert engine.get_ref_count(r1.content_id) == 1
        # Content still readable
        data = engine.read_content(r1.content_id)
        assert data == b"shared content"
