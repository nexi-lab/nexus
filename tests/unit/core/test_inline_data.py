"""Unit tests for inline data (Issue #1508).

Verifies that small files (≤ INLINE_THRESHOLD) are stored directly in the
metastore (Raft-replicated) instead of the CAS backend, and that the
read/write/unlink lifecycle works correctly for inline files, CAS files,
and transitions between the two.
"""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import CASLocalBackend, NexusFS
from nexus.contracts.constants import INLINE_CONTENT_KEY, INLINE_PREFIX, INLINE_THRESHOLD
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create NexusFS instance for inline data tests."""
    fs = create_nexus_fs(
        backend=CASLocalBackend(temp_dir),
        metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-metadata")),
        record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
        parsing=ParseConfig(auto_parse=False),
        permissions=PermissionConfig(enforce=False),
    )
    yield fs
    fs.close()


# ─── Write + Read ────────────────────────────────────────────────────


class TestInlineWriteRead:
    """Small files go through metastore, not CAS."""

    def test_small_file_round_trip(self, nx: NexusFS) -> None:
        """Write ≤ threshold → read returns same content."""
        content = b"Hello, inline data!"
        nx.sys_write("/test.txt", content)
        assert nx.sys_read("/test.txt") == content

    def test_small_file_metadata_has_inline_prefix(self, nx: NexusFS) -> None:
        """physical_path starts with 'inline://'."""
        nx.sys_write("/test.txt", b"small")
        meta = nx.metadata.get("/test.txt")
        assert meta is not None
        assert meta.physical_path.startswith(INLINE_PREFIX)

    def test_small_file_stored_in_custom_metadata(self, nx: NexusFS) -> None:
        """Content is stored in metastore custom metadata key."""
        content = b"inline content"
        nx.sys_write("/test.txt", content)
        raw = nx.metadata.get_file_metadata("/test.txt", INLINE_CONTENT_KEY)
        assert raw is not None
        import base64

        assert base64.b64decode(raw) == content

    def test_small_file_etag_is_content_hash(self, nx: NexusFS) -> None:
        """etag is still the content hash (for dedup/ETag headers)."""
        from nexus.core.hash_fast import hash_content

        content = b"hash me"
        nx.sys_write("/test.txt", content)
        meta = nx.metadata.get("/test.txt")
        assert meta is not None
        assert meta.etag == hash_content(content)

    def test_empty_file_is_inline(self, nx: NexusFS) -> None:
        """Empty files (0 bytes) go through inline path."""
        nx.sys_write("/empty.txt", b"")
        meta = nx.metadata.get("/empty.txt")
        assert meta is not None
        assert meta.physical_path.startswith(INLINE_PREFIX)
        assert nx.sys_read("/empty.txt") == b""

    def test_exact_threshold_is_inline(self, nx: NexusFS) -> None:
        """File exactly at threshold boundary is inline."""
        content = b"x" * INLINE_THRESHOLD
        nx.sys_write("/exact.bin", content)
        meta = nx.metadata.get("/exact.bin")
        assert meta is not None
        assert meta.physical_path.startswith(INLINE_PREFIX)
        assert nx.sys_read("/exact.bin") == content

    def test_read_with_offset_and_count(self, nx: NexusFS) -> None:
        """Inline read supports POSIX pread offset/count."""
        content = b"0123456789"
        nx.sys_write("/test.txt", content)
        assert nx.sys_read("/test.txt", offset=3, count=4) == b"3456"

    def test_version_increments_on_rewrite(self, nx: NexusFS) -> None:
        """Version bumps work the same for inline files."""
        nx.sys_write("/test.txt", b"v1")
        meta1 = nx.metadata.get("/test.txt")
        assert meta1 is not None and meta1.version == 1

        nx.sys_write("/test.txt", b"v2")
        meta2 = nx.metadata.get("/test.txt")
        assert meta2 is not None and meta2.version == 2


# ─── Large files (CAS path unchanged) ───────────────────────────────


class TestLargeFileUnchanged:
    """Files > threshold still go through CAS backend."""

    def test_large_file_uses_cas(self, nx: NexusFS) -> None:
        """File > threshold has CAS hash as physical_path (no inline prefix)."""
        content = b"x" * (INLINE_THRESHOLD + 1)
        nx.sys_write("/big.bin", content)
        meta = nx.metadata.get("/big.bin")
        assert meta is not None
        assert not meta.physical_path.startswith(INLINE_PREFIX)

    def test_large_file_no_inline_metadata(self, nx: NexusFS) -> None:
        """No custom metadata key stored for large files."""
        content = b"x" * (INLINE_THRESHOLD + 1)
        nx.sys_write("/big.bin", content)
        raw = nx.metadata.get_file_metadata("/big.bin", INLINE_CONTENT_KEY)
        assert raw is None

    def test_large_file_round_trip(self, nx: NexusFS) -> None:
        """Large files still read correctly via CAS."""
        content = b"large content " * 10000
        nx.sys_write("/big.txt", content)
        assert nx.sys_read("/big.txt") == content


# ─── Threshold migration ────────────────────────────────────────────


class TestThresholdMigration:
    """File crosses threshold boundary on rewrite."""

    def test_inline_to_cas_migration(self, nx: NexusFS) -> None:
        """File grows past threshold → migrates from inline to CAS."""
        # Start small (inline)
        small = b"small"
        nx.sys_write("/grow.txt", small)
        meta1 = nx.metadata.get("/grow.txt")
        assert meta1 is not None
        assert meta1.physical_path.startswith(INLINE_PREFIX)

        # Rewrite large (CAS)
        large = b"x" * (INLINE_THRESHOLD + 1)
        nx.sys_write("/grow.txt", large)
        meta2 = nx.metadata.get("/grow.txt")
        assert meta2 is not None
        assert not meta2.physical_path.startswith(INLINE_PREFIX)
        assert nx.sys_read("/grow.txt") == large

        # Inline content key should be cleaned up
        raw = nx.metadata.get_file_metadata("/grow.txt", INLINE_CONTENT_KEY)
        assert raw is None

    def test_cas_to_inline_migration(self, nx: NexusFS) -> None:
        """File shrinks below threshold → migrates from CAS to inline."""
        # Start large (CAS)
        large = b"x" * (INLINE_THRESHOLD + 1)
        nx.sys_write("/shrink.txt", large)
        meta1 = nx.metadata.get("/shrink.txt")
        assert meta1 is not None
        assert not meta1.physical_path.startswith(INLINE_PREFIX)

        # Rewrite small (inline)
        small = b"now small"
        nx.sys_write("/shrink.txt", small)
        meta2 = nx.metadata.get("/shrink.txt")
        assert meta2 is not None
        assert meta2.physical_path.startswith(INLINE_PREFIX)
        assert nx.sys_read("/shrink.txt") == small


# ─── Unlink ──────────────────────────────────────────────────────────


class TestInlineUnlink:
    """Deleting inline files cleans up metastore custom metadata."""

    def test_unlink_inline_file(self, nx: NexusFS) -> None:
        """Unlink removes both metadata and inline content key."""
        nx.sys_write("/del.txt", b"delete me")
        nx.sys_unlink("/del.txt")

        # Metadata gone
        assert nx.metadata.get("/del.txt") is None

        # Custom metadata key gone
        raw = nx.metadata.get_file_metadata("/del.txt", INLINE_CONTENT_KEY)
        assert raw is None

    def test_unlink_then_read_raises(self, nx: NexusFS) -> None:
        """Reading a deleted inline file raises FileNotFound."""
        nx.sys_write("/del.txt", b"gone soon")
        nx.sys_unlink("/del.txt")
        with pytest.raises(NexusFileNotFoundError):
            nx.sys_read("/del.txt")

    def test_unlink_large_file_still_works(self, nx: NexusFS) -> None:
        """Unlink for CAS files is unchanged."""
        content = b"x" * (INLINE_THRESHOLD + 1)
        nx.sys_write("/big.bin", content)
        nx.sys_unlink("/big.bin")
        assert nx.metadata.get("/big.bin") is None


# ─── Binary content ─────────────────────────────────────────────────


class TestBinaryContent:
    """Inline data handles arbitrary binary correctly (base64 encoding)."""

    def test_binary_round_trip(self, nx: NexusFS) -> None:
        """All 256 byte values survive inline storage."""
        content = bytes(range(256))
        nx.sys_write("/binary.bin", content)
        assert nx.sys_read("/binary.bin") == content

    def test_null_bytes(self, nx: NexusFS) -> None:
        """Content with null bytes works."""
        content = b"\x00" * 100
        nx.sys_write("/nulls.bin", content)
        assert nx.sys_read("/nulls.bin") == content
