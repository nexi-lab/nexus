"""S3 backend integration tests using moto.

Tests the full kernel lifecycle against a mocked S3 backend.
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
from nexus.contracts.metadata import DT_MOUNT  # noqa: E402
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.fs import _make_mount_entry
from nexus.fs._helpers import LOCAL_CONTEXT, list_mounts
from nexus.fs._sqlite_meta import SQLiteMetastore

BUCKET_NAME = "test-nexus-fs-bucket"


@pytest.fixture
def s3_fs(tmp_path: Path):
    """Boot a NexusFS kernel with a moto-mocked S3 backend."""
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

        mount_point = f"/s3/{BUCKET_NAME}"

        # Kernel (constructs DLC + router internally)
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
        kernel.sys_setattr(mount_point, entry_type=DT_MOUNT, backend=backend)
        metastore.put(_make_mount_entry(mount_point, backend.name))

        yield kernel, mount_point


@pytest.mark.integration
class TestS3BackendLifecycle:
    @pytest.mark.asyncio
    async def test_write_and_read(self, s3_fs):
        fs, mp = s3_fs
        content = b"Hello from S3!"
        fs.write(f"{mp}/test.txt", content, context=LOCAL_CONTEXT)
        result = fs.sys_read(f"{mp}/test.txt", context=LOCAL_CONTEXT)
        assert result == content

    @pytest.mark.asyncio
    async def test_stat(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/meta.txt", b"metadata test", context=LOCAL_CONTEXT)
        stat = fs.sys_stat(f"{mp}/meta.txt", context=LOCAL_CONTEXT)
        assert stat is not None
        assert stat["size"] == 13
        assert stat["is_directory"] is False

    @pytest.mark.asyncio
    async def test_ls(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/a.txt", b"aaa", context=LOCAL_CONTEXT)
        fs.write(f"{mp}/b.txt", b"bbb", context=LOCAL_CONTEXT)
        entries = list(
            fs.sys_readdir(f"{mp}/", recursive=True, details=False, context=LOCAL_CONTEXT)
        )
        paths = [e for e in entries if e.endswith(".txt")]
        assert f"{mp}/a.txt" in paths
        assert f"{mp}/b.txt" in paths

    @pytest.mark.asyncio
    async def test_exists(self, s3_fs):
        fs, mp = s3_fs
        assert not fs.access(f"{mp}/nofile.txt", context=LOCAL_CONTEXT)
        fs.write(f"{mp}/nofile.txt", b"now I exist", context=LOCAL_CONTEXT)
        assert fs.access(f"{mp}/nofile.txt", context=LOCAL_CONTEXT)

    @pytest.mark.asyncio
    async def test_delete(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/delete-me.txt", b"bye", context=LOCAL_CONTEXT)
        fs.sys_unlink(f"{mp}/delete-me.txt", context=LOCAL_CONTEXT)
        stat = fs.sys_stat(f"{mp}/delete-me.txt", context=LOCAL_CONTEXT)
        assert stat is None

    @pytest.mark.asyncio
    async def test_copy(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/src.txt", b"copy me", context=LOCAL_CONTEXT)
        fs.sys_copy(f"{mp}/src.txt", f"{mp}/dst.txt", context=LOCAL_CONTEXT)
        src = fs.sys_read(f"{mp}/src.txt", context=LOCAL_CONTEXT)
        dst = fs.sys_read(f"{mp}/dst.txt", context=LOCAL_CONTEXT)
        assert src == dst == b"copy me"

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        reason="S3 PathBackend rename is metadata-only; blob key doesn't change. "
        "Native S3 copy+delete rename planned for v0.2.0.",
        strict=False,
    )
    async def test_rename(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/old.txt", b"rename me", context=LOCAL_CONTEXT)
        fs.sys_rename(f"{mp}/old.txt", f"{mp}/new.txt", context=LOCAL_CONTEXT)
        result = fs.sys_read(f"{mp}/new.txt", context=LOCAL_CONTEXT)
        assert result == b"rename me"

    @pytest.mark.asyncio
    async def test_mkdir(self, s3_fs):
        fs, mp = s3_fs
        fs.mkdir(f"{mp}/subdir", parents=True, exist_ok=True, context=LOCAL_CONTEXT)
        stat = fs.sys_stat(f"{mp}/subdir", context=LOCAL_CONTEXT)
        assert stat is not None
        assert stat["is_directory"] is True

    @pytest.mark.asyncio
    async def test_list_mounts(self, s3_fs):
        fs, mp = s3_fs
        mounts = list_mounts(fs)
        assert mp in mounts

    @pytest.mark.asyncio
    async def test_overwrite(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/ow.txt", b"version 1", context=LOCAL_CONTEXT)
        fs.write(f"{mp}/ow.txt", b"version 2", context=LOCAL_CONTEXT)
        result = fs.sys_read(f"{mp}/ow.txt", context=LOCAL_CONTEXT)
        assert result == b"version 2"

    @pytest.mark.asyncio
    async def test_binary_content(self, s3_fs):
        fs, mp = s3_fs
        content = bytes(range(256))
        fs.write(f"{mp}/binary.bin", content, context=LOCAL_CONTEXT)
        result = fs.sys_read(f"{mp}/binary.bin", context=LOCAL_CONTEXT)
        assert result == content

    @pytest.mark.asyncio
    async def test_empty_file(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/empty.txt", b"", context=LOCAL_CONTEXT)
        result = fs.sys_read(f"{mp}/empty.txt", context=LOCAL_CONTEXT)
        assert result == b""

    @pytest.mark.asyncio
    async def test_edit_simple(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/code.py", b"def foo():\n    return 1\n", context=LOCAL_CONTEXT)
        result = fs.edit(
            f"{mp}/code.py",
            [("def foo():", "def bar():")],
            context=LOCAL_CONTEXT,
        )
        assert result["success"] is True
        assert result["applied_count"] == 1
        content = fs.sys_read(f"{mp}/code.py", context=LOCAL_CONTEXT)
        assert b"def bar():" in content

    @pytest.mark.asyncio
    async def test_edit_preview(self, s3_fs):
        fs, mp = s3_fs
        original = b"keep this\n"
        fs.write(f"{mp}/preview.txt", original, context=LOCAL_CONTEXT)
        result = fs.edit(
            f"{mp}/preview.txt",
            [("keep", "change")],
            context=LOCAL_CONTEXT,
            preview=True,
        )
        assert result["success"] is True
        content = fs.sys_read(f"{mp}/preview.txt", context=LOCAL_CONTEXT)
        assert content == original  # unchanged

    @pytest.mark.asyncio
    async def test_edit_multiple(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/multi.py", b"x = 1\ny = 2\n", context=LOCAL_CONTEXT)
        result = fs.edit(
            f"{mp}/multi.py",
            [("x = 1", "x = 10"), ("y = 2", "y = 20")],
            context=LOCAL_CONTEXT,
        )
        assert result["success"] is True
        assert result["applied_count"] == 2
        content = fs.sys_read(f"{mp}/multi.py", context=LOCAL_CONTEXT)
        assert content == b"x = 10\ny = 20\n"

    @pytest.mark.asyncio
    async def test_edit_fuzzy(self, s3_fs):
        fs, mp = s3_fs
        fs.write(
            f"{mp}/fuzzy.py",
            b"def calculate_total(items):\n    pass\n",
            context=LOCAL_CONTEXT,
        )
        result = fs.edit(
            f"{mp}/fuzzy.py",
            [("def calcuate_total(items):", "def compute(items):")],
            context=LOCAL_CONTEXT,
            fuzzy_threshold=0.8,
        )
        assert result["success"] is True
        assert result["matches"][0]["match_type"] == "fuzzy"

    @pytest.mark.asyncio
    async def test_edit_no_match(self, s3_fs):
        fs, mp = s3_fs
        fs.write(f"{mp}/nomatch.txt", b"actual content\n", context=LOCAL_CONTEXT)
        result = fs.edit(
            f"{mp}/nomatch.txt",
            [("nonexistent", "x")],
            context=LOCAL_CONTEXT,
            fuzzy_threshold=1.0,
        )
        assert result["success"] is False
