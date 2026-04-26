"""GCS backend integration tests using a mock transport.

Tests the full kernel lifecycle against a mocked GCS backend.
Uses unittest.mock to intercept the GCS transport layer, providing
an in-memory blob store that exercises the real CAS addressing engine.

This validates:
- The mount("gcs://...") -> CASGCSBackend wiring
- CAS addressing with GCS-style key layout
- Full kernel API surface against the GCS backend
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Guard: skip if google-cloud-storage not installed
gcs_storage = pytest.importorskip(
    "google.cloud.storage",
    reason="google-cloud-storage required for GCS integration tests",
)

from nexus.contracts.constants import ROOT_ZONE_ID  # noqa: E402
from nexus.contracts.metadata import DT_MOUNT  # noqa: E402
from nexus.contracts.types import OperationContext  # noqa: E402
from nexus.core.config import PermissionConfig  # noqa: E402
from nexus.core.nexus_fs import NexusFS  # noqa: E402
from nexus.fs import _make_mount_entry  # noqa: E402
from nexus.fs._helpers import LOCAL_CONTEXT, list_mounts  # noqa: E402
from nexus.fs._sqlite_meta import SQLiteMetastore  # noqa: E402


class InMemoryBlobStore:
    """In-memory blob store that mimics GCS bucket behavior.

    Used to replace the real GCS client/bucket/blob objects in tests.
    Stores blobs as {key: bytes} and provides the minimal interface
    needed by GCSTransport.
    """

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def blob(self, key: str) -> MagicMock:
        """Return a mock Blob that reads/writes from the in-memory store."""
        mock_blob = MagicMock()
        mock_blob.name = key

        def upload_from_string(data: bytes, **kwargs: Any) -> None:
            self._blobs[key] = data

        def upload_from_file(file_obj: io.BytesIO, **kwargs: Any) -> None:
            self._blobs[key] = file_obj.read()

        def download_as_bytes(**kwargs: Any) -> bytes:
            if key not in self._blobs:
                from google.cloud.exceptions import NotFound

                raise NotFound(f"Blob {key} not found")
            return self._blobs[key]

        def download_to_file(file_obj: io.BytesIO, **kwargs: Any) -> None:
            if key not in self._blobs:
                from google.cloud.exceptions import NotFound

                raise NotFound(f"Blob {key} not found")
            file_obj.write(self._blobs[key])

        def exists_fn(**kwargs: Any) -> bool:
            return key in self._blobs

        def delete_fn(**kwargs: Any) -> None:
            self._blobs.pop(key, None)

        mock_blob.upload_from_string = upload_from_string
        mock_blob.upload_from_file = upload_from_file
        mock_blob.download_as_bytes = download_as_bytes
        mock_blob.download_to_file = download_to_file
        mock_blob.exists.side_effect = exists_fn
        mock_blob.delete.side_effect = delete_fn
        mock_blob.size = len(self._blobs.get(key, b""))
        mock_blob.reload = MagicMock()
        mock_blob.generation = 1
        return mock_blob

    def list_keys(self, prefix: str = "", **kwargs: Any) -> list[MagicMock]:
        results = []
        for key, data in self._blobs.items():
            if key.startswith(prefix):
                mock_blob = MagicMock()
                mock_blob.name = key
                mock_blob.size = len(data)
                results.append(mock_blob)
        return results

    def exists(self, **kwargs: Any) -> bool:
        return True  # Bucket always exists in tests


def _build_gcs_fs(tmp_path: Path) -> tuple[NexusFS, str]:
    """Build NexusFS kernel with a mock GCS backend."""
    blob_store = InMemoryBlobStore()

    # Mock the GCS client so CASGCSBackend doesn't need real credentials
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_bucket.blob = blob_store.blob
    mock_bucket.list_blobs = blob_store.list_keys
    mock_bucket.exists.return_value = True
    mock_bucket.name = "test-gcs-bucket"
    mock_client.bucket.return_value = mock_bucket

    with patch("google.cloud.storage.Client", return_value=mock_client):
        from nexus.backends.storage.cas_gcs import CASGCSBackend

        backend = CASGCSBackend(
            bucket_name="test-gcs-bucket",
            project_id="test-project",
        )

    # SQLite metastore
    db_path = str(tmp_path / "metadata.db")
    metastore = SQLiteMetastore(db_path)

    mount_point = "/gcs/test-project/test-gcs-bucket"

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

    # Mount via coordinator (registers in backend pool + routing table + hooks)
    kernel.sys_setattr(mount_point, entry_type=DT_MOUNT, backend=backend)

    # Create DT_MOUNT entry
    metastore.put(_make_mount_entry(mount_point, backend.name))

    return kernel, mount_point


@pytest.fixture
def gcs_fs(tmp_path: Path):
    """Provide a NexusFS kernel with a mocked GCS backend."""
    return _build_gcs_fs(tmp_path)


@pytest.mark.skip(
    reason="GCS lifecycle now handled by Rust GcsBackend - Python mock transport "
    "is bypassed. These tests need rewriting as Rust-level GCS integration tests."
)
@pytest.mark.integration
class TestGCSBackendLifecycle:
    @pytest.mark.asyncio
    async def test_write_and_read(self, gcs_fs):
        fs, mp = gcs_fs
        content = b"Hello from GCS!"
        fs.write(f"{mp}/test.txt", content, context=LOCAL_CONTEXT)
        result = fs.sys_read(f"{mp}/test.txt", context=LOCAL_CONTEXT)
        assert result == content

    @pytest.mark.asyncio
    async def test_stat(self, gcs_fs):
        fs, mp = gcs_fs
        fs.write(f"{mp}/meta.txt", b"metadata test", context=LOCAL_CONTEXT)
        stat = fs.sys_stat(f"{mp}/meta.txt", context=LOCAL_CONTEXT)
        assert stat is not None
        assert stat["size"] == 13
        assert stat["is_directory"] is False

    @pytest.mark.asyncio
    async def test_ls(self, gcs_fs):
        fs, mp = gcs_fs
        fs.write(f"{mp}/a.txt", b"aaa", context=LOCAL_CONTEXT)
        fs.write(f"{mp}/b.txt", b"bbb", context=LOCAL_CONTEXT)
        entries = list(
            fs.sys_readdir(f"{mp}/", recursive=True, details=False, context=LOCAL_CONTEXT)
        )
        paths = [e for e in entries if e.endswith(".txt")]
        assert f"{mp}/a.txt" in paths
        assert f"{mp}/b.txt" in paths

    @pytest.mark.asyncio
    async def test_exists(self, gcs_fs):
        fs, mp = gcs_fs
        assert not fs.access(f"{mp}/nofile.txt", context=LOCAL_CONTEXT)
        fs.write(f"{mp}/nofile.txt", b"now I exist", context=LOCAL_CONTEXT)
        assert fs.access(f"{mp}/nofile.txt", context=LOCAL_CONTEXT)

    @pytest.mark.asyncio
    async def test_delete(self, gcs_fs):
        fs, mp = gcs_fs
        fs.write(f"{mp}/delete-me.txt", b"bye", context=LOCAL_CONTEXT)
        fs.sys_unlink(f"{mp}/delete-me.txt", context=LOCAL_CONTEXT)
        stat = fs.sys_stat(f"{mp}/delete-me.txt", context=LOCAL_CONTEXT)
        assert stat is None

    @pytest.mark.asyncio
    async def test_copy(self, gcs_fs):
        fs, mp = gcs_fs
        fs.write(f"{mp}/src.txt", b"copy me", context=LOCAL_CONTEXT)
        fs.sys_copy(f"{mp}/src.txt", f"{mp}/dst.txt", context=LOCAL_CONTEXT)
        src = fs.sys_read(f"{mp}/src.txt", context=LOCAL_CONTEXT)
        dst = fs.sys_read(f"{mp}/dst.txt", context=LOCAL_CONTEXT)
        assert src == dst == b"copy me"

    @pytest.mark.asyncio
    async def test_mkdir(self, gcs_fs):
        fs, mp = gcs_fs
        fs.mkdir(f"{mp}/subdir", parents=True, exist_ok=True, context=LOCAL_CONTEXT)
        stat = fs.sys_stat(f"{mp}/subdir", context=LOCAL_CONTEXT)
        assert stat is not None
        assert stat["is_directory"] is True

    @pytest.mark.asyncio
    async def test_list_mounts(self, gcs_fs):
        fs, mp = gcs_fs
        mounts = list_mounts(fs)
        assert mp in mounts

    @pytest.mark.asyncio
    async def test_overwrite(self, gcs_fs):
        fs, mp = gcs_fs
        fs.write(f"{mp}/ow.txt", b"version 1", context=LOCAL_CONTEXT)
        fs.write(f"{mp}/ow.txt", b"version 2", context=LOCAL_CONTEXT)
        result = fs.sys_read(f"{mp}/ow.txt", context=LOCAL_CONTEXT)
        assert result == b"version 2"

    @pytest.mark.asyncio
    async def test_binary_content(self, gcs_fs):
        fs, mp = gcs_fs
        content = bytes(range(256))
        fs.write(f"{mp}/binary.bin", content, context=LOCAL_CONTEXT)
        result = fs.sys_read(f"{mp}/binary.bin", context=LOCAL_CONTEXT)
        assert result == content

    @pytest.mark.asyncio
    async def test_empty_file(self, gcs_fs):
        fs, mp = gcs_fs
        fs.write(f"{mp}/empty.txt", b"", context=LOCAL_CONTEXT)
        result = fs.sys_read(f"{mp}/empty.txt", context=LOCAL_CONTEXT)
        assert result == b""
