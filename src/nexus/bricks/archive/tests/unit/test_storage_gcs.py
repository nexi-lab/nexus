"""Tests for GCS archive storage backend (uses MagicMock bucket injection)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("google.cloud.storage")

from nexus.bricks.archive.storage.gcs import GCSArchiveStorage  # noqa: E402


def _make_storage(prefix: str = "") -> tuple[GCSArchiveStorage, MagicMock]:
    bucket = MagicMock()
    storage = GCSArchiveStorage(bucket_name="test-bucket", prefix=prefix, _bucket=bucket)
    return storage, bucket


def test_put_uploads_file(tmp_path: Path) -> None:
    storage, bucket = _make_storage(prefix="archives/")
    src = tmp_path / "a.nexus"
    src.write_bytes(b"hello")

    blob = MagicMock()
    bucket.blob.return_value = blob

    storage.put("daily/a.nexus", src)

    bucket.blob.assert_called_once_with("archives/daily/a.nexus")
    blob.upload_from_filename.assert_called_once_with(str(src))


def test_list_returns_entries() -> None:
    storage, bucket = _make_storage(prefix="arc/")

    blob1 = MagicMock()
    blob1.name = "arc/daily/x.nexus"
    blob1.size = 42
    blob1.updated = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)

    blob2 = MagicMock()
    blob2.name = "arc/daily/y.nexus"
    blob2.size = 7
    blob2.updated = datetime(2025, 3, 4, 8, 0, tzinfo=UTC)

    bucket.list_blobs.return_value = [blob1, blob2]

    entries = storage.list("daily/")

    bucket.list_blobs.assert_called_once_with(prefix="arc/daily/")
    assert len(entries) == 2
    keys = {e.key for e in entries}
    assert keys == {"daily/x.nexus", "daily/y.nexus"}
    sizes = {e.size_bytes for e in entries}
    assert sizes == {42, 7}
