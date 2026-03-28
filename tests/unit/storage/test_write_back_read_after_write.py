"""Tests for write-back consistency guarantee in BufferedMetadataStore.

Verifies that ``consistency="wb"`` writes are immediately visible via
``get()``, ``exists()``, and ``get_batch()`` even before the background
flush commits them to the inner store.

See architecture decision A1 (store-level intercept on get() for buffer
overlay) in Issue #3393.
"""

from datetime import UTC, datetime

from nexus.contracts.metadata import FileMetadata
from nexus.storage.buffered_metadata_store import BufferedMetadataStore
from tests.helpers.dict_metastore import DictMetastore

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_metadata(
    path: str = "/data/file.txt",
    version: int = 1,
    backend_name: str = "default",
    physical_path: str | None = None,
    size: int = 1024,
    etag: str | None = None,
    created_at: datetime | None = None,
    modified_at: datetime | None = None,
    zone_id: str = "zone-1",
    owner_id: str = "user-1",
) -> FileMetadata:
    now = datetime.now(UTC)
    return FileMetadata(
        path=path,
        backend_name=backend_name,
        physical_path=physical_path or f"/phys{path}",
        size=size,
        etag=etag or f"etag-v{version}",
        created_at=created_at or now,
        modified_at=modified_at or now,
        version=version,
        zone_id=zone_id,
        owner_id=owner_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWriteBackReadAfterWrite:
    """Write-back consistency: wb put() is visible on immediate get()."""

    def test_wb_write_then_immediate_get(self) -> None:
        """put(consistency='wb') followed by get() returns the buffered metadata."""
        inner = DictMetastore()
        store = BufferedMetadataStore(inner)

        meta = _make_metadata("/data/a.txt")
        store.put(meta, consistency="wb")

        result = store.get("/data/a.txt")
        assert result is not None
        assert result.path == "/data/a.txt"

    def test_wb_write_then_get_returns_correct_fields(self) -> None:
        """All metadata fields round-trip correctly through the buffer."""
        inner = DictMetastore()
        store = BufferedMetadataStore(inner)

        now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        meta = _make_metadata(
            path="/proj/report.csv",
            version=7,
            backend_name="fast-ssd",
            physical_path="/mnt/ssd/report.csv",
            size=98765,
            etag="sha256-abc123",
            created_at=now,
            modified_at=now,
            zone_id="zone-42",
            owner_id="user-99",
        )
        store.put(meta, consistency="wb")

        result = store.get("/proj/report.csv")
        assert result is not None
        assert result.path == "/proj/report.csv"
        assert result.backend_name == "fast-ssd"
        assert result.physical_path == "/mnt/ssd/report.csv"
        assert result.size == 98765
        assert result.etag == "sha256-abc123"
        assert result.created_at == now
        assert result.modified_at == now
        assert result.version == 7
        assert result.zone_id == "zone-42"
        assert result.owner_id == "user-99"

    def test_wb_overwrite_same_path(self) -> None:
        """Two consecutive wb writes to the same path: get() returns the latest."""
        inner = DictMetastore()
        store = BufferedMetadataStore(inner)

        meta_v1 = _make_metadata("/data/overwrite.txt", version=1, size=100)
        meta_v2 = _make_metadata("/data/overwrite.txt", version=2, size=200)

        store.put(meta_v1, consistency="wb")
        store.put(meta_v2, consistency="wb")

        result = store.get("/data/overwrite.txt")
        assert result is not None
        assert result.version == 2
        assert result.size == 200

    def test_wb_write_does_not_appear_in_inner_store_before_flush(self) -> None:
        """Buffered metadata is NOT visible in the inner store before flush."""
        inner = DictMetastore()
        store = BufferedMetadataStore(inner)

        meta = _make_metadata("/data/buffered.txt")
        store.put(meta, consistency="wb")

        # The inner store should have nothing yet
        assert inner.get("/data/buffered.txt") is None

    def test_wb_write_appears_in_inner_store_after_flush(self) -> None:
        """After flush(), the inner store contains the metadata."""
        inner = DictMetastore()
        store = BufferedMetadataStore(inner)

        meta = _make_metadata("/data/flushed.txt", version=3)
        store.put(meta, consistency="wb")
        store.flush()

        inner_result = inner.get("/data/flushed.txt")
        assert inner_result is not None
        assert inner_result.path == "/data/flushed.txt"
        assert inner_result.version == 3

    def test_sc_write_bypasses_buffer(self) -> None:
        """put(consistency='sc') writes directly to the inner store."""
        inner = DictMetastore()
        store = BufferedMetadataStore(inner)

        meta = _make_metadata("/data/sc.txt")
        store.put(meta, consistency="sc")

        # Immediately visible in the inner store (no flush needed)
        assert inner.get("/data/sc.txt") is not None
        assert inner.get("/data/sc.txt").path == "/data/sc.txt"

    def test_ec_write_bypasses_buffer(self) -> None:
        """put(consistency='ec') writes directly to the inner store."""
        inner = DictMetastore()
        store = BufferedMetadataStore(inner)

        meta = _make_metadata("/data/ec.txt")
        store.put(meta, consistency="ec")

        # Immediately visible in the inner store (no flush needed)
        assert inner.get("/data/ec.txt") is not None
        assert inner.get("/data/ec.txt").path == "/data/ec.txt"

    def test_wb_exists_returns_true(self) -> None:
        """exists() returns True for paths that are only in the buffer."""
        inner = DictMetastore()
        store = BufferedMetadataStore(inner)

        meta = _make_metadata("/data/exists_check.txt")
        store.put(meta, consistency="wb")

        assert store.exists("/data/exists_check.txt") is True
        # Not yet in the inner store
        assert inner.exists("/data/exists_check.txt") is False

    def test_wb_get_batch_includes_buffered(self) -> None:
        """get_batch() merges buffered entries with inner store results."""
        inner = DictMetastore()
        store = BufferedMetadataStore(inner)

        # Put one entry directly in the inner store
        meta_inner = _make_metadata("/data/inner.txt", version=1)
        inner.put(meta_inner)

        # Put another entry through the buffer
        meta_buffered = _make_metadata("/data/buffered.txt", version=2)
        store.put(meta_buffered, consistency="wb")

        results = store.get_batch(["/data/inner.txt", "/data/buffered.txt", "/data/missing.txt"])

        assert results["/data/inner.txt"] is not None
        assert results["/data/inner.txt"].version == 1

        assert results["/data/buffered.txt"] is not None
        assert results["/data/buffered.txt"].version == 2

        assert results["/data/missing.txt"] is None

    def test_wb_version_increment_across_flushes(self) -> None:
        """Write v1, flush, write v2 -- get() returns v2."""
        inner = DictMetastore()
        store = BufferedMetadataStore(inner)

        meta_v1 = _make_metadata("/data/versioned.txt", version=1)
        store.put(meta_v1, consistency="wb")
        store.flush()

        # v1 should now be in the inner store
        assert inner.get("/data/versioned.txt").version == 1

        meta_v2 = _make_metadata("/data/versioned.txt", version=2)
        store.put(meta_v2, consistency="wb")

        # get() should return the buffered v2
        result = store.get("/data/versioned.txt")
        assert result is not None
        assert result.version == 2

        # Inner store still has v1
        assert inner.get("/data/versioned.txt").version == 1

    def test_has_pending_flag_false_when_no_pending(self) -> None:
        """_has_pending tracks buffer state: False -> True -> False after flush."""
        inner = DictMetastore()
        store = BufferedMetadataStore(inner)

        # Initially no pending items
        assert store._has_pending is False

        # After wb write, flag is True
        meta = _make_metadata("/data/pending.txt")
        store.put(meta, consistency="wb")
        assert store._has_pending is True

        # After flush, flag goes back to False
        store.flush()
        assert store._has_pending is False

    def test_flush_clears_buffer(self) -> None:
        """After flush(), buffer is empty and get() falls through to inner store."""
        inner = DictMetastore()
        store = BufferedMetadataStore(inner)

        meta = _make_metadata("/data/cleared.txt", version=5, size=500)
        store.put(meta, consistency="wb")

        # Buffered entry is visible
        assert store.get("/data/cleared.txt") is not None

        store.flush()

        # After flush, the buffer should be empty -- the entry lives only in
        # the inner store now. We can verify by checking the buffer directly.
        assert store._buffer.get_pending("/data/cleared.txt") is None

        # get() still works, falling through to the inner store
        result = store.get("/data/cleared.txt")
        assert result is not None
        assert result.version == 5
        assert result.size == 500
