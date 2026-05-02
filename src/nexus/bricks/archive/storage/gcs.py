"""GCS archive storage backend."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import google.cloud.storage as gcs_lib

from nexus.bricks.archive.storage.base import StorageEntry


class GCSArchiveStorage:
    def __init__(
        self,
        bucket_name: str,
        prefix: str = "",
        *,
        _bucket: Any = None,
    ) -> None:
        self.bucket_name = bucket_name
        self.prefix = prefix
        self._bucket = _bucket if _bucket is not None else gcs_lib.Client().bucket(bucket_name)

    def _full(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def put(self, key: str, source_path: Path) -> None:
        blob = self._bucket.blob(self._full(key))
        blob.upload_from_filename(str(source_path))

    def get(self, key: str, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        blob = self._bucket.blob(self._full(key))
        blob.download_to_filename(str(target_path))

    def delete(self, key: str) -> None:
        blob = self._bucket.blob(self._full(key))
        blob.delete()

    def list(self, prefix: str) -> list[StorageEntry]:
        full_prefix = self._full(prefix)
        blobs = self._bucket.list_blobs(prefix=full_prefix)
        out: list[StorageEntry] = []
        for blob in blobs:
            full_name = blob.name
            key = full_name[len(self.prefix) :] if full_name.startswith(self.prefix) else full_name
            out.append(
                StorageEntry(
                    key=key,
                    size_bytes=blob.size,
                    last_modified=blob.updated,
                )
            )
        return out
