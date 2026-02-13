"""Unit tests for MultipartUploadMixin — LocalBackend implementation (Issue #788)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.backends.local import LocalBackend
from nexus.backends.multipart_upload_mixin import MultipartUploadMixin


@pytest.fixture
def local_backend(tmp_path: Path) -> LocalBackend:
    """Create a LocalBackend in a temp directory."""
    return LocalBackend(root_path=tmp_path)


class TestLocalBackendMultipart:
    """Tests for LocalBackend's MultipartUploadMixin implementation."""

    def test_isinstance_multipart(self, local_backend: LocalBackend) -> None:
        assert isinstance(local_backend, MultipartUploadMixin)
        assert local_backend.supports_multipart is True

    def test_init_multipart_creates_temp_dir(self, local_backend: LocalBackend) -> None:
        upload_id = local_backend.init_multipart(
            backend_path="/test/file.txt",
            content_type="text/plain",
        )
        assert upload_id  # non-empty string
        upload_dir = local_backend.root_path / "uploads" / upload_id
        assert upload_dir.exists()

        # Should have metadata file
        meta_path = upload_dir / "_meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["content_type"] == "text/plain"
        assert meta["backend_path"] == "/test/file.txt"

    def test_init_multipart_with_metadata(self, local_backend: LocalBackend) -> None:
        upload_id = local_backend.init_multipart(
            backend_path="/test/file.txt",
            metadata={"custom_key": "custom_value"},
        )
        upload_dir = local_backend.root_path / "uploads" / upload_id
        meta = json.loads((upload_dir / "_meta.json").read_text())
        assert meta["custom_key"] == "custom_value"

    def test_upload_part_writes_file(self, local_backend: LocalBackend) -> None:
        upload_id = local_backend.init_multipart(backend_path="/f")
        data = b"chunk data here"

        result = local_backend.upload_part(
            backend_path="/f",
            upload_id=upload_id,
            part_number=1,
            data=data,
        )

        assert "etag" in result
        assert result["part_number"] == 1

        # Verify part file exists
        upload_dir = local_backend.root_path / "uploads" / upload_id
        part_path = upload_dir / "part_000001"
        assert part_path.exists()
        assert part_path.read_bytes() == data

    def test_upload_part_multiple(self, local_backend: LocalBackend) -> None:
        upload_id = local_backend.init_multipart(backend_path="/f")

        parts = []
        for i in range(1, 4):
            result = local_backend.upload_part(
                backend_path="/f",
                upload_id=upload_id,
                part_number=i,
                data=f"part{i}".encode(),
            )
            parts.append(result)

        assert len(parts) == 3
        upload_dir = local_backend.root_path / "uploads" / upload_id
        assert (upload_dir / "part_000001").exists()
        assert (upload_dir / "part_000002").exists()
        assert (upload_dir / "part_000003").exists()

    def test_upload_part_nonexistent_upload(self, local_backend: LocalBackend) -> None:
        from nexus.core.exceptions import BackendError

        with pytest.raises(BackendError, match="not found"):
            local_backend.upload_part(
                backend_path="/f",
                upload_id="nonexistent-id",
                part_number=1,
                data=b"data",
            )

    def test_complete_multipart_assembles(self, local_backend: LocalBackend) -> None:
        upload_id = local_backend.init_multipart(backend_path="/f")

        parts = []
        for i in range(1, 4):
            result = local_backend.upload_part(
                backend_path="/f",
                upload_id=upload_id,
                part_number=i,
                data=f"chunk_{i}_".encode(),
            )
            parts.append(result)

        content_hash = local_backend.complete_multipart(
            backend_path="/f",
            upload_id=upload_id,
            parts=parts,
        )

        assert content_hash  # non-empty string

        # Verify temp dir is cleaned up
        upload_dir = local_backend.root_path / "uploads" / upload_id
        assert not upload_dir.exists()

        # Verify content can be read back
        response = local_backend.read_content(content_hash)
        assert response.success
        assert response.data == b"chunk_1_chunk_2_chunk_3_"

    def test_complete_multipart_handles_order(self, local_backend: LocalBackend) -> None:
        """Parts should be assembled in part_number order regardless of upload order."""
        upload_id = local_backend.init_multipart(backend_path="/f")

        # Upload parts out of order
        parts = []
        for i in [3, 1, 2]:
            result = local_backend.upload_part(
                backend_path="/f",
                upload_id=upload_id,
                part_number=i,
                data=f"part{i}".encode(),
            )
            parts.append(result)

        content_hash = local_backend.complete_multipart(
            backend_path="/f",
            upload_id=upload_id,
            parts=parts,
        )

        response = local_backend.read_content(content_hash)
        assert response.success
        assert response.data == b"part1part2part3"

    def test_abort_multipart_cleans_up(self, local_backend: LocalBackend) -> None:
        upload_id = local_backend.init_multipart(backend_path="/f")
        local_backend.upload_part(
            backend_path="/f",
            upload_id=upload_id,
            part_number=1,
            data=b"data",
        )

        upload_dir = local_backend.root_path / "uploads" / upload_id
        assert upload_dir.exists()

        local_backend.abort_multipart(backend_path="/f", upload_id=upload_id)
        assert not upload_dir.exists()

    def test_abort_multipart_nonexistent_is_safe(self, local_backend: LocalBackend) -> None:
        # Should not raise
        local_backend.abort_multipart(
            backend_path="/f",
            upload_id="nonexistent-id",
        )
