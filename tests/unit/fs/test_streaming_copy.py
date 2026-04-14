"""Tests for the streaming copy implementation.

Validates that copy() works correctly for files that exceed the
STREAMING_COPY_CHUNK_SIZE (64 MB) boundary, exercising the chunked
read_range → write path in SlimNexusFS._copy().
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.core.router import PathRouter
from nexus.fs import _make_mount_entry
from nexus.fs._constants import STREAMING_COPY_CHUNK_SIZE
from nexus.fs._facade import SlimNexusFS
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

    from nexus.core.mount_table import MountTable

    mount_table = MountTable(metastore)
    router = PathRouter(mount_table)

    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        router=router,
    )
    kernel._init_cred = OperationContext(
        user_id="test",
        groups=[],
        zone_id=ROOT_ZONE_ID,
        is_admin=True,
    )

    # Add mount AFTER NexusFS init so it registers in the Rust Kernel
    # that NexusFS wired to mount_table._kernel.
    mount_table.add("/local", backend)
    metastore.put(_make_mount_entry("/local", backend.name))

    return SlimNexusFS(kernel)


class TestStreamingCopy:
    """Test copy behavior at and around the chunk boundary."""

    def test_copy_small_file(self, slim_fs: SlimNexusFS):
        """Files under chunk size use single read-write."""
        content = b"small file"
        slim_fs.write("/local/small.txt", content)
        result = slim_fs.copy("/local/small.txt", "/local/small_copy.txt")
        assert result["size"] == len(content)
        assert slim_fs.read("/local/small_copy.txt") == content

    def test_copy_at_chunk_boundary(self, slim_fs: SlimNexusFS):
        """File exactly at chunk size boundary."""
        content = b"x" * STREAMING_COPY_CHUNK_SIZE
        slim_fs.write("/local/boundary.bin", content)
        slim_fs.copy("/local/boundary.bin", "/local/boundary_copy.bin")
        result = slim_fs.read("/local/boundary_copy.bin")
        assert len(result) == STREAMING_COPY_CHUNK_SIZE
        assert result == content

    def test_copy_exceeds_chunk_size(self, slim_fs: SlimNexusFS):
        """File larger than one chunk triggers multi-chunk streaming."""
        # 1.5x chunk size to force 2 chunks
        size = STREAMING_COPY_CHUNK_SIZE + STREAMING_COPY_CHUNK_SIZE // 2
        content = bytes(i % 256 for i in range(size))
        slim_fs.write("/local/large.bin", content)
        slim_fs.copy("/local/large.bin", "/local/large_copy.bin")
        result = slim_fs.read("/local/large_copy.bin")
        assert len(result) == size
        assert result == content

    def test_copy_exactly_two_chunks(self, slim_fs: SlimNexusFS):
        """File exactly two chunks — no remainder."""
        size = STREAMING_COPY_CHUNK_SIZE * 2
        content = b"\xab" * size
        slim_fs.write("/local/two_chunks.bin", content)
        slim_fs.copy("/local/two_chunks.bin", "/local/two_chunks_copy.bin")
        result = slim_fs.read("/local/two_chunks_copy.bin")
        assert len(result) == size
        assert result == content

    def test_copy_preserves_source(self, slim_fs: SlimNexusFS):
        """Copy must not modify or delete the source file."""
        content = b"preserve me" * 1000
        slim_fs.write("/local/src.txt", content)
        slim_fs.copy("/local/src.txt", "/local/dst.txt")
        # Source unchanged
        assert slim_fs.read("/local/src.txt") == content
        # Destination correct
        assert slim_fs.read("/local/dst.txt") == content

    def test_copy_nonexistent_raises(self, slim_fs: SlimNexusFS):
        """Copy of a nonexistent file must raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            slim_fs.copy("/local/nope.txt", "/local/dst.txt")

    def test_copy_empty_file(self, slim_fs: SlimNexusFS):
        """Copy of an empty file."""
        slim_fs.write("/local/empty.txt", b"")
        slim_fs.copy("/local/empty.txt", "/local/empty_copy.txt")
        assert slim_fs.read("/local/empty_copy.txt") == b""
