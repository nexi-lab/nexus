"""S3 archive storage backend."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import boto3

from nexus.bricks.archive.storage.base import StorageEntry


class S3ArchiveStorage:
    def __init__(self, bucket: str, prefix: str = "", region: str | None = None) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self.client = boto3.client("s3", region_name=region) if region else boto3.client("s3")

    def _full(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def put(self, key: str, source_path: Path) -> None:
        self.client.upload_file(str(source_path), self.bucket, self._full(key))

    def get(self, key: str, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, self._full(key), str(target_path))

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._full(key))

    def list(self, prefix: str) -> list[StorageEntry]:
        full_prefix = self._full(prefix)
        paginator = self.client.get_paginator("list_objects_v2")
        out: list[StorageEntry] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                full_key = obj["Key"]
                key = full_key[len(self.prefix) :] if full_key.startswith(self.prefix) else full_key
                last_mod = obj["LastModified"]
                if isinstance(last_mod, datetime):
                    out.append(
                        StorageEntry(key=key, size_bytes=obj["Size"], last_modified=last_mod)
                    )
        return out
