"""Tests for the streaming copy implementation.

Validates that copy() works correctly for files that exceed the
STREAMING_COPY_CHUNK_SIZE (64 MB) boundary, exercising the chunked
read_range -> write path in the kernel sys_copy implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import DT_MOUNT  # noqa: E402
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.fs import _make_mount_entry
from nexus.fs._constants import STREAMING_COPY_CHUNK_SIZE
from nexus.fs._helpers import LOCAL_CONTEXT
from nexus.fs._sqlite_meta import SQLiteMetastore


@pytest.fixture
def slim_fs(tmp_path: Path):
    """Boot a slim NexusFS with a local backend."""
    from nexus.backends.storage.cas_local import CASLocalBackend

    db_path = str(tmp_path / "metadata.db")
    metastore = SQLiteMetastore(db_path)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backend = CASLocalBackend(root_path=data_dir)

    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
    )
    kernel._init_cred = OperationContext(
        user_id="test",
        groups=[],
        zone_id=ROOT_ZONE_ID,
        is_admin=True,
    )

    # Mount via the driver coordinator (F2 MountTable migration).
    kernel.sys_setattr("/local", entry_type=DT_MOUNT, backend=backend)
    metastore.put(_make_mount_entry("/local", backend.name))

    return kernel


class TestStreamingCopy:
    """Test copy behavior at and around the chunk boundary."""

    def test_copy_small_file(self, slim_fs: NexusFS):
        """Files under chunk size use single read-write."""
        content = b"small file"
        slim_fs.write("/local/small.txt", content, context=LOCAL_CONTEXT)
        result = slim_fs.sys_copy(
            "/local/small.txt", "/local/small_copy.txt", context=LOCAL_CONTEXT
        )
        assert result["size"] == len(content)
        assert slim_fs.sys_read("/local/small_copy.txt", context=LOCAL_CONTEXT) == content

    def test_copy_at_chunk_boundary(self, slim_fs: NexusFS):
        """File exactly at chunk size boundary."""
        content = b"x" * STREAMING_COPY_CHUNK_SIZE
        slim_fs.write("/local/boundary.bin", content, context=LOCAL_CONTEXT)
        slim_fs.sys_copy("/local/boundary.bin", "/local/boundary_copy.bin", context=LOCAL_CONTEXT)
        result = slim_fs.sys_read("/local/boundary_copy.bin", context=LOCAL_CONTEXT)
        assert len(result) == STREAMING_COPY_CHUNK_SIZE
        assert result == content

    def test_copy_exceeds_chunk_size(self, slim_fs: NexusFS):
        """File larger than one chunk triggers multi-chunk streaming."""
        # 1.5x chunk size to force 2 chunks
        size = STREAMING_COPY_CHUNK_SIZE + STREAMING_COPY_CHUNK_SIZE // 2
        # Use fast byte multiplication instead of slow generator (~100x faster)
        pattern = bytes(range(256))
        content = (pattern * (size // 256 + 1))[:size]
        slim_fs.write("/local/large.bin", content, context=LOCAL_CONTEXT)
        slim_fs.sys_copy("/local/large.bin", "/local/large_copy.bin", context=LOCAL_CONTEXT)
        result = slim_fs.sys_read("/local/large_copy.bin", context=LOCAL_CONTEXT)
        assert len(result) == size
        assert result == content

    def test_copy_exactly_two_chunks(self, slim_fs: NexusFS):
        """File exactly two chunks - no remainder."""
        size = STREAMING_COPY_CHUNK_SIZE * 2
        content = b"\xab" * size
        slim_fs.write("/local/two_chunks.bin", content, context=LOCAL_CONTEXT)
        slim_fs.sys_copy(
            "/local/two_chunks.bin", "/local/two_chunks_copy.bin", context=LOCAL_CONTEXT
        )
        result = slim_fs.sys_read("/local/two_chunks_copy.bin", context=LOCAL_CONTEXT)
        assert len(result) == size
        assert result == content

    def test_copy_preserves_source(self, slim_fs: NexusFS):
        """Copy must not modify or delete the source file."""
        content = b"preserve me" * 1000
        slim_fs.write("/local/src.txt", content, context=LOCAL_CONTEXT)
        slim_fs.sys_copy("/local/src.txt", "/local/dst.txt", context=LOCAL_CONTEXT)
        # Source unchanged
        assert slim_fs.sys_read("/local/src.txt", context=LOCAL_CONTEXT) == content
        # Destination correct
        assert slim_fs.sys_read("/local/dst.txt", context=LOCAL_CONTEXT) == content

    def test_copy_nonexistent_raises(self, slim_fs: NexusFS):
        """Copy of a nonexistent file must raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            slim_fs.sys_copy("/local/nope.txt", "/local/dst.txt", context=LOCAL_CONTEXT)

    def test_copy_empty_file(self, slim_fs: NexusFS):
        """Copy of an empty file."""
        slim_fs.write("/local/empty.txt", b"", context=LOCAL_CONTEXT)
        slim_fs.sys_copy("/local/empty.txt", "/local/empty_copy.txt", context=LOCAL_CONTEXT)
        assert slim_fs.sys_read("/local/empty_copy.txt", context=LOCAL_CONTEXT) == b""
