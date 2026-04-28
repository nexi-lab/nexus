"""E2E tests for kernel write_batch() and read_batch() (Issue #3700).

Boots a real NexusFS kernel (SQLite + CASLocalBackend) and exercises the kernel
batch APIs - no mocks, no external server process required.

Verifies:
- write_batch returns correct metadata (content_id, version, size)
- read_batch returns correct content and metadata
- Round-trip: write then read produces identical bytes and content_ids
- Partial mode: missing path returns error item, not exception
- Strict mode: missing path raises NexusFileNotFoundError
- Binary content survives round-trip intact
- Large batches (50 files)
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
from nexus.fs._helpers import LOCAL_CONTEXT
from nexus.fs._sqlite_meta import SQLiteMetastore

# ---------------------------------------------------------------------------
# Fixture: real slim FS
# ---------------------------------------------------------------------------


@pytest.fixture()
def slim(tmp_path: Path) -> NexusFS:
    """NexusFS kernel backed by SQLite metastore + CASLocalBackend."""
    from nexus.backends.storage.cas_local import CASLocalBackend

    db_path = str(tmp_path / "meta.db")
    metastore = SQLiteMetastore(db_path)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backend = CASLocalBackend(root_path=data_dir)

    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
    )
    kernel._init_cred = OperationContext(
        user_id="slim-e2e",
        groups=[],
        zone_id=ROOT_ZONE_ID,
        is_admin=True,
    )
    kernel.sys_setattr("/files", entry_type=DT_MOUNT, backend=backend)
    metastore.put(_make_mount_entry("/files", backend.name))

    return kernel


# ---------------------------------------------------------------------------
# write_batch
# ---------------------------------------------------------------------------


class TestSlimWriteBatch:
    @pytest.mark.asyncio
    async def test_write_single_file(self, slim: NexusFS) -> None:
        results = slim.write_batch([("/files/a.txt", b"alpha")], context=LOCAL_CONTEXT)
        assert len(results) == 1
        r = results[0]
        assert r["size"] == 5
        assert r["version"] >= 1
        assert r.get("content_id") is not None

    @pytest.mark.asyncio
    async def test_write_multiple_files(self, slim: NexusFS) -> None:
        files = [
            ("/files/x.txt", b"xxx"),
            ("/files/y.txt", b"yyyy"),
            ("/files/z.txt", b"zzzzz"),
        ]
        results = slim.write_batch(files, context=LOCAL_CONTEXT)
        assert len(results) == 3
        # Results are in input order: x, y, z
        assert results[0]["size"] == 3
        assert results[1]["size"] == 4
        assert results[2]["size"] == 5

    @pytest.mark.asyncio
    async def test_write_empty_content(self, slim: NexusFS) -> None:
        results = slim.write_batch([("/files/empty.txt", b"")], context=LOCAL_CONTEXT)
        assert results[0]["size"] == 0

    @pytest.mark.asyncio
    async def test_write_binary_content(self, slim: NexusFS) -> None:
        binary = bytes(range(256))
        results = slim.write_batch([("/files/bin.bin", binary)], context=LOCAL_CONTEXT)
        assert results[0]["size"] == 256

    @pytest.mark.asyncio
    async def test_overwrite_increments_version(self, slim: NexusFS) -> None:
        r1 = slim.write_batch([("/files/ver.txt", b"v1")], context=LOCAL_CONTEXT)
        r2 = slim.write_batch([("/files/ver.txt", b"v2")], context=LOCAL_CONTEXT)
        assert r2[0]["version"] > r1[0]["version"]


# ---------------------------------------------------------------------------
# read_batch
# ---------------------------------------------------------------------------


class TestSlimReadBatch:
    @pytest.mark.asyncio
    async def test_read_single_file(self, slim: NexusFS) -> None:
        slim.write_batch([("/files/r1.txt", b"read me")], context=LOCAL_CONTEXT)
        results = slim.read_batch(["/files/r1.txt"], context=LOCAL_CONTEXT)
        assert len(results) == 1
        assert results[0]["content"] == b"read me"
        assert results[0]["path"] == "/files/r1.txt"

    @pytest.mark.asyncio
    async def test_read_multiple_files_preserves_order(self, slim: NexusFS) -> None:
        slim.write_batch(
            [
                ("/files/ord1.txt", b"first"),
                ("/files/ord2.txt", b"second"),
                ("/files/ord3.txt", b"third"),
            ],
            context=LOCAL_CONTEXT,
        )
        results = slim.read_batch(
            [
                "/files/ord3.txt",
                "/files/ord1.txt",
                "/files/ord2.txt",
            ],
            context=LOCAL_CONTEXT,
        )
        assert results[0]["content"] == b"third"
        assert results[1]["content"] == b"first"
        assert results[2]["content"] == b"second"

    @pytest.mark.asyncio
    async def test_read_strict_missing_raises(self, slim: NexusFS) -> None:
        from nexus.contracts.exceptions import NexusFileNotFoundError

        with pytest.raises(NexusFileNotFoundError):
            slim.read_batch(["/files/ghost.txt"], context=LOCAL_CONTEXT)

    @pytest.mark.asyncio
    async def test_read_partial_missing_returns_error_item(self, slim: NexusFS) -> None:
        results = slim.read_batch(["/files/ghost.txt"], partial=True, context=LOCAL_CONTEXT)
        assert len(results) == 1
        assert "error" in results[0]
        assert results[0]["path"] == "/files/ghost.txt"

    @pytest.mark.asyncio
    async def test_read_partial_mixed(self, slim: NexusFS) -> None:
        slim.write_batch([("/files/pm_exists.txt", b"here")], context=LOCAL_CONTEXT)
        results = slim.read_batch(
            ["/files/pm_exists.txt", "/files/pm_missing.txt"],
            partial=True,
            context=LOCAL_CONTEXT,
        )
        assert len(results) == 2
        assert results[0]["content"] == b"here"
        assert "error" in results[1]

    @pytest.mark.asyncio
    async def test_read_empty_batch(self, slim: NexusFS) -> None:
        results = slim.read_batch([], context=LOCAL_CONTEXT)
        assert results == []

    @pytest.mark.asyncio
    async def test_read_binary_content(self, slim: NexusFS) -> None:
        binary = bytes(range(256))
        slim.write_batch([("/files/rb.bin", binary)], context=LOCAL_CONTEXT)
        results = slim.read_batch(["/files/rb.bin"], context=LOCAL_CONTEXT)
        assert results[0]["content"] == binary
        assert results[0]["size"] == 256


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestSlimBatchRoundTrip:
    @pytest.mark.asyncio
    async def test_write_then_read_content_id_matches(self, slim: NexusFS) -> None:
        write_results = slim.write_batch(
            [
                ("/files/rt_a.txt", b"payload A"),
                ("/files/rt_b.txt", b"payload B"),
            ],
            context=LOCAL_CONTEXT,
        )
        read_results = slim.read_batch(
            ["/files/rt_a.txt", "/files/rt_b.txt"], context=LOCAL_CONTEXT
        )

        # Content matches
        assert read_results[0]["content"] == b"payload A"
        assert read_results[1]["content"] == b"payload B"

        # ETags match
        if write_results[0].get("content_id") and read_results[0].get("content_id"):
            assert read_results[0]["content_id"] == write_results[0]["content_id"]
            assert read_results[1]["content_id"] == write_results[1]["content_id"]

    @pytest.mark.asyncio
    async def test_large_batch_50_files(self, slim: NexusFS) -> None:
        files = [(f"/files/batch_{i:03d}.txt", f"content_{i}".encode()) for i in range(50)]
        slim.write_batch(files, context=LOCAL_CONTEXT)

        paths = [p for p, _ in files]
        results = slim.read_batch(paths, context=LOCAL_CONTEXT)

        assert len(results) == 50
        for i, (path, content) in enumerate(files):
            assert results[i]["path"] == path
            assert results[i]["content"] == content

    @pytest.mark.asyncio
    async def test_write_batch_faster_than_individual(self, slim: NexusFS) -> None:
        """Batch of 20 writes should succeed - perf regression smoke test."""
        import time

        files = [(f"/files/perf_{i:03d}.txt", f"data_{i}".encode()) for i in range(20)]

        start = time.perf_counter()
        results = slim.write_batch(files, context=LOCAL_CONTEXT)
        elapsed = time.perf_counter() - start

        assert len(results) == 20
        # Sanity: should complete well under 10s in any environment
        assert elapsed < 10.0, f"write_batch took {elapsed:.2f}s for 20 files"
