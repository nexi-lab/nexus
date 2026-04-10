"""S3 backend integration tests using moto.

Tests the full SlimNexusFS lifecycle against a mocked S3 backend.
Uses moto's mock_aws to intercept all boto3 calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("moto", reason="moto required for S3 integration tests")
pytest.importorskip("boto3", reason="boto3 required for S3 integration tests")

import boto3
from moto import mock_aws

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.core.router import PathRouter
from nexus.fs import _make_mount_entry
from nexus.fs._facade import SlimNexusFS
from nexus.fs._sqlite_meta import SQLiteMetastore

BUCKET_NAME = "test-nexus-fs-bucket"


@pytest.fixture
def s3_fs(tmp_path: Path):
    """Boot SlimNexusFS with a moto-mocked S3 backend."""
    with mock_aws():
        # Create the mock bucket
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET_NAME)

        # Import the S3 backend
        from nexus.backends.storage.path_s3 import PathS3Backend

        backend = PathS3Backend(bucket_name=BUCKET_NAME, prefix="")

        # SQLite metastore
        db_path = str(tmp_path / "metadata.db")
        metastore = SQLiteMetastore(db_path)

        # Router with mount
        from nexus.core.mount_table import MountTable

        mount_table = MountTable(metastore)
        router = PathRouter(mount_table)
        mount_point = f"/s3/{BUCKET_NAME}"
        mount_table.add(mount_point, backend)

        # Create DT_MOUNT entry
        metastore.put(_make_mount_entry(mount_point, backend.name))

        # Kernel
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

        yield SlimNexusFS(kernel), mount_point


@pytest.mark.integration
class TestS3BackendLifecycle:
    @pytest.mark.asyncio
    async def test_write_and_read(self, s3_fs):
        fs, mp = s3_fs
        content = b"Hello from S3!"
        fs.write(f"{mp}/test.txt", content)
        result = fs.read(f"{mp}/test.txt")
        assert result == content

    @pytest.mark.asyncio
    async def test_stat(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/meta.txt", b"metadata test")
        stat = await fs.stat(f"{mp}/meta.txt")
        assert stat is not None
        assert stat["size"] == 13
        assert stat["is_directory"] is False

    @pytest.mark.asyncio
    async def test_ls(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/a.txt", b"aaa")
        fs.write(f"{mp}/b.txt", b"bbb")
        entries = await fs.ls(f"{mp}/", detail=False, recursive=True)
        paths = [e for e in entries if e.endswith(".txt")]
        assert f"{mp}/a.txt" in paths
        assert f"{mp}/b.txt" in paths

    @pytest.mark.asyncio
    async def test_exists(self, s3_fs):
        fs, mp = s3_fs
        assert not await fs.exists(f"{mp}/nofile.txt")
        fs.write(f"{mp}/nofile.txt", b"now I exist")
        assert await fs.exists(f"{mp}/nofile.txt")

    @pytest.mark.asyncio
    async def test_delete(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/delete-me.txt", b"bye")
        await fs.delete(f"{mp}/delete-me.txt")
        stat = await fs.stat(f"{mp}/delete-me.txt")
        assert stat is None

    @pytest.mark.asyncio
    async def test_copy(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/src.txt", b"copy me")
        await fs.copy(f"{mp}/src.txt", f"{mp}/dst.txt")
        src = fs.read(f"{mp}/src.txt")
        dst = fs.read(f"{mp}/dst.txt")
        assert src == dst == b"copy me"

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        reason="S3 PathBackend rename is metadata-only; blob key doesn't change. "
        "Native S3 copy+delete rename planned for v0.2.0.",
        strict=False,
    )
    async def test_rename(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/old.txt", b"rename me")
        await fs.rename(f"{mp}/old.txt", f"{mp}/new.txt")
        result = fs.read(f"{mp}/new.txt")
        assert result == b"rename me"

    @pytest.mark.asyncio
    async def test_mkdir(self, s3_fs):
        fs, mp = s3_fs
        fs.mkdir(f"{mp}/subdir")
        stat = await fs.stat(f"{mp}/subdir")
        assert stat is not None
        assert stat["is_directory"] is True

    @pytest.mark.asyncio
    async def test_list_mounts(self, s3_fs):
        fs, mp = s3_fs
        mounts = fs.list_mounts()
        assert mp in mounts

    @pytest.mark.asyncio
    async def test_overwrite(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/ow.txt", b"version 1")
        fs.write(f"{mp}/ow.txt", b"version 2")
        result = fs.read(f"{mp}/ow.txt")
        assert result == b"version 2"

    @pytest.mark.asyncio
    async def test_binary_content(self, s3_fs):
        fs, mp = s3_fs
        content = bytes(range(256))
        fs.write(f"{mp}/binary.bin", content)
        result = fs.read(f"{mp}/binary.bin")
        assert result == content

    @pytest.mark.asyncio
    async def test_empty_file(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/empty.txt", b"")
        result = fs.read(f"{mp}/empty.txt")
        assert result == b""

    @pytest.mark.asyncio
    async def test_edit_simple(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/code.py", b"def foo():\n    return 1\n")
        result = fs.edit(f"{mp}/code.py", [("def foo():", "def bar():")])
        assert result["success"] is True
        assert result["applied_count"] == 1
        content = fs.read(f"{mp}/code.py")
        assert b"def bar():" in content

    @pytest.mark.asyncio
    async def test_edit_preview(self, s3_fs):
        fs, mp = s3_fs
        original = b"keep this\n"
        fs.write(f"{mp}/preview.txt", original)
        result = fs.edit(f"{mp}/preview.txt", [("keep", "change")], preview=True)
        assert result["success"] is True
        content = fs.read(f"{mp}/preview.txt")
        assert content == original  # unchanged

    @pytest.mark.asyncio
    async def test_edit_multiple(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/multi.py", b"x = 1\ny = 2\n")
        result = fs.edit(f"{mp}/multi.py", [("x = 1", "x = 10"), ("y = 2", "y = 20")])
        assert result["success"] is True
        assert result["applied_count"] == 2
        content = fs.read(f"{mp}/multi.py")
        assert content == b"x = 10\ny = 20\n"

    @pytest.mark.asyncio
    async def test_edit_fuzzy(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/fuzzy.py", b"def calculate_total(items):\n    pass\n")
        result = fs.edit(
            f"{mp}/fuzzy.py",
            [("def calcuate_total(items):", "def compute(items):")],
            fuzzy_threshold=0.8,
        )
        assert result["success"] is True
        assert result["matches"][0]["match_type"] == "fuzzy"

    @pytest.mark.asyncio
    async def test_edit_no_match(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/nomatch.txt", b"actual content\n")
        result = fs.edit(
            f"{mp}/nomatch.txt",
            [("nonexistent", "x")],
            fuzzy_threshold=1.0,
        )
        assert result["success"] is False
