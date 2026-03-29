"""Tests for batch operation fallback when engine lacks batch methods (Issue #3469).

ZoneHandle historically lacked batch_set_metadata, batch_delete_metadata, and
get_metadata_multi.  RaftMetadataStore now has _engine_batch_set() and
_engine_batch_delete() helpers that fall back to individual calls when the
engine does not expose these methods.

These tests verify that the full write path — including BufferedMetadataStore
flush — works correctly with a ZoneHandle-like engine that has no batch API.
"""

from datetime import UTC, datetime
from typing import Any

from nexus.contracts.metadata import FileMetadata
from nexus.storage.buffered_metadata_store import BufferedMetadataStore
from nexus.storage.raft_metadata_store import RaftMetadataStore

# ---------------------------------------------------------------------------
# ZoneHandle-like mock engine (no batch methods)
# ---------------------------------------------------------------------------


class FakeZoneHandle:
    """Mimics ZoneHandle's method surface: NO batch_set_metadata,
    NO batch_delete_metadata, NO get_metadata_multi.

    This is the exact API shape that caused Issue #3469.
    """

    def __init__(self) -> None:
        self._metadata: dict[str, bytes] = {}

    # -- Metadata (individual ops only) ------------------------------------

    def set_metadata(
        self, path: str, value: bytes | list[int], consistency: str = "sc"
    ) -> int | None:
        if isinstance(value, list):
            value = bytes(value)
        self._metadata[path] = value
        return None

    def get_metadata(self, path: str) -> bytes | None:
        return self._metadata.get(path)

    def delete_metadata(self, path: str, consistency: str = "sc") -> int | None:
        self._metadata.pop(path, None)
        return None

    def list_metadata(self, prefix: str) -> list[tuple[str, bytes]]:
        return sorted([(k, v) for k, v in self._metadata.items() if k.startswith(prefix)])

    def is_committed(self, token: int) -> str | None:
        return None

    # -- Leadership (ZoneHandle-specific) ----------------------------------

    def is_leader(self) -> bool:
        return True

    def leader_id(self) -> int | None:
        return 1


class FakeZoneHandleWithBatch(FakeZoneHandle):
    """Extended fake that DOES have batch methods — used as control group."""

    def get_metadata_multi(self, paths: list[str]) -> list[tuple[str, bytes | None]]:
        return [(p, self._metadata.get(p)) for p in paths]

    def batch_set_metadata(self, items: list[tuple[str, bytes]]) -> int:
        for path, value in items:
            self.set_metadata(path, value)
        return len(items)

    def batch_delete_metadata(self, keys: list[str]) -> int:
        for key in keys:
            self.delete_metadata(key)
        return len(keys)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(engine: Any = None) -> RaftMetadataStore:
    """Create a RaftMetadataStore backed by a fake engine."""
    engine = engine or FakeZoneHandle()
    store = object.__new__(RaftMetadataStore)
    store._dcache = {}
    store._dcache_hits = 0
    store._dcache_misses = 0
    store._engine = engine
    store._zone_id = None
    return store


def _make_metadata(
    path: str = "/data/file.txt",
    version: int = 1,
    size: int = 1024,
    etag: str | None = None,
) -> FileMetadata:
    now = datetime.now(UTC)
    return FileMetadata(
        path=path,
        backend_name="default",
        physical_path=f"/phys{path}",
        size=size,
        etag=etag or f"etag-v{version}",
        created_at=now,
        modified_at=now,
        version=version,
    )


# ===========================================================================
# RaftMetadataStore batch fallback tests
# ===========================================================================


class TestEngineBatchSetFallback:
    """_engine_batch_set() falls back to individual set_metadata() calls."""

    def test_put_batch_without_batch_set_metadata(self) -> None:
        """put_batch() succeeds on an engine without batch_set_metadata."""
        engine = FakeZoneHandle()
        store = _make_store(engine)

        items = [_make_metadata(path=f"/batch/{i}.txt") for i in range(5)]
        store.put_batch(items)

        for i in range(5):
            assert store.exists(f"/batch/{i}.txt")

    def test_put_batch_data_round_trips(self) -> None:
        """Metadata written via fallback put_batch is retrievable with correct fields."""
        engine = FakeZoneHandle()
        store = _make_store(engine)

        meta = _make_metadata(path="/rt/doc.txt", size=42, version=7, etag="abc123")
        store.put_batch([meta])

        result = store.get("/rt/doc.txt")
        assert result is not None
        assert result.size == 42
        assert result.version == 7
        assert result.etag == "abc123"

    def test_put_batch_with_batch_set_metadata(self) -> None:
        """Control: put_batch() uses native batch_set_metadata when available."""
        engine = FakeZoneHandleWithBatch()
        store = _make_store(engine)

        items = [_make_metadata(path=f"/ctrl/{i}.txt") for i in range(3)]
        store.put_batch(items)

        for i in range(3):
            assert store.exists(f"/ctrl/{i}.txt")


class TestEngineBatchDeleteFallback:
    """_engine_batch_delete() falls back to individual delete_metadata() calls."""

    def test_delete_batch_without_batch_delete_metadata(self) -> None:
        """delete_batch() succeeds on an engine without batch_delete_metadata."""
        engine = FakeZoneHandle()
        store = _make_store(engine)

        for i in range(3):
            store.put(_make_metadata(path=f"/del/{i}.txt"))

        store.delete_batch(["/del/0.txt", "/del/2.txt"])
        assert store.exists("/del/0.txt") is False
        assert store.exists("/del/1.txt") is True
        assert store.exists("/del/2.txt") is False

    def test_delete_batch_with_batch_delete_metadata(self) -> None:
        """Control: delete_batch() uses native batch_delete_metadata when available."""
        engine = FakeZoneHandleWithBatch()
        store = _make_store(engine)

        for i in range(3):
            store.put(_make_metadata(path=f"/del/{i}.txt"))

        store.delete_batch(["/del/0.txt", "/del/2.txt"])
        assert store.exists("/del/0.txt") is False
        assert store.exists("/del/1.txt") is True
        assert store.exists("/del/2.txt") is False


class TestGetBatchFallback:
    """get_batch() falls back to individual get_metadata() calls."""

    def test_get_batch_without_get_metadata_multi(self) -> None:
        """get_batch() succeeds on an engine without get_metadata_multi."""
        engine = FakeZoneHandle()
        store = _make_store(engine)

        store.put(_make_metadata(path="/gb/a.txt", etag="ea"))
        store.put(_make_metadata(path="/gb/b.txt", etag="eb"))

        result = store.get_batch(["/gb/a.txt", "/gb/b.txt", "/gb/miss.txt"])
        assert result["/gb/a.txt"] is not None
        assert result["/gb/a.txt"].etag == "ea"
        assert result["/gb/b.txt"] is not None
        assert result["/gb/b.txt"].etag == "eb"
        assert result["/gb/miss.txt"] is None


# ===========================================================================
# BufferedMetadataStore flush with ZoneHandle-like engine (Issue #3469)
# ===========================================================================


class TestBufferedFlushWithZoneHandleEngine:
    """End-to-end: BufferedMetadataStore flush cycle with an engine
    that lacks batch methods — the exact scenario from Issue #3469.
    """

    def test_flush_succeeds_without_batch_methods(self) -> None:
        """Flush cycle completes and commits data to the inner store."""
        engine = FakeZoneHandle()
        inner = _make_store(engine)
        store = BufferedMetadataStore(inner)

        # Write via buffer (wb mode)
        meta = _make_metadata(path="/flush/test.txt", size=999)
        store.put(meta, consistency="wb")

        # Before flush: inner store should NOT have it
        assert inner.get("/flush/test.txt") is None

        # Flush the buffer
        store.flush()

        # After flush: inner store SHOULD have it
        result = inner.get("/flush/test.txt")
        assert result is not None
        assert result.path == "/flush/test.txt"
        assert result.size == 999

    def test_list_after_flush_returns_all_files(self) -> None:
        """After flush, list() on the inner store returns all buffered files.

        This is the exact bug scenario from Issue #3469: list() returned
        incomplete results because flush failed with AttributeError.
        """
        engine = FakeZoneHandle()
        inner = _make_store(engine)
        store = BufferedMetadataStore(inner)

        # Write multiple files via buffer
        store.put(_make_metadata(path="/repro/a.json"), consistency="wb")
        store.put(_make_metadata(path="/repro/b.json"), consistency="wb")

        # Before flush: list via BufferedMetadataStore should merge pending
        items = store.list(prefix="/repro/")
        paths = {m.path for m in items}
        assert paths == {"/repro/a.json", "/repro/b.json"}

        # Flush
        store.flush()

        # After flush: list via inner store should also have both files
        inner_items = inner.list(prefix="/repro/")
        inner_paths = {m.path for m in inner_items}
        assert inner_paths == {"/repro/a.json", "/repro/b.json"}

    def test_multiple_flushes_accumulate(self) -> None:
        """Multiple flush cycles correctly accumulate data."""
        engine = FakeZoneHandle()
        inner = _make_store(engine)
        store = BufferedMetadataStore(inner)

        store.put(_make_metadata(path="/multi/1.txt"), consistency="wb")
        store.flush()

        store.put(_make_metadata(path="/multi/2.txt"), consistency="wb")
        store.flush()

        inner_items = inner.list(prefix="/multi/")
        inner_paths = {m.path for m in inner_items}
        assert inner_paths == {"/multi/1.txt", "/multi/2.txt"}

    def test_buffer_stats_after_flush(self) -> None:
        """Buffer stats reflect successful flush."""
        engine = FakeZoneHandle()
        inner = _make_store(engine)
        store = BufferedMetadataStore(inner)

        store.put(_make_metadata(path="/stats/a.txt"), consistency="wb")
        store.put(_make_metadata(path="/stats/b.txt"), consistency="wb")
        store.flush()

        stats = store.get_buffer_stats()
        assert stats["total_flushed"] >= 2
        assert stats["pending_metadata"] == 0

    def test_dead_letter_empty_after_successful_flush(self) -> None:
        """No dead-lettered items after a successful flush."""
        engine = FakeZoneHandle()
        inner = _make_store(engine)
        store = BufferedMetadataStore(inner)

        store.put(_make_metadata(path="/dl/a.txt"), consistency="wb")
        store.flush()

        assert store.get_dead_letter() == []
