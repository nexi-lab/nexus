#!/usr/bin/env python3
"""Upload a build artifact to Tencent COS using the official Python SDK."""

from __future__ import annotations

import argparse
import mimetypes
import os
from pathlib import Path

from qcloud_cos import CosConfig, CosS3Client

LARGE_FILE_THRESHOLD = 128 * 1024 * 1024


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


class COSClient:
    """Tencent COS client wrapper matching the local helper style."""

    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        bucket: str,
        region: str,
        base_url: str | None = None,
    ) -> None:
        config = CosConfig(Region=region, SecretId=secret_id, SecretKey=secret_key)
        self.client = CosS3Client(config)
        self.bucket = bucket
        self.region = region
        self.base_url = base_url or f"https://{bucket}.cos.{region}.myqcloud.com"

    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        content_type: str | None = None,
    ) -> str:
        if content_type is None:
            content_type, _ = mimetypes.guess_type(str(local_path))

        with local_path.open("rb") as fh:
            self.client.put_object(
                Bucket=self.bucket,
                Key=remote_path,
                Body=fh,
                ContentType=content_type,
            )

        return f"{self.base_url.rstrip('/')}/{remote_path}"

    def upload_large_file(self, local_path: Path, remote_path: str) -> str:
        self.client.upload_file(
            Bucket=self.bucket,
            Key=remote_path,
            LocalFilePath=str(local_path),
            PartSize=10,
            MAXThread=5,
        )
        return f"{self.base_url.rstrip('/')}/{remote_path}"


def _normalize_remote_path(remote_path: str) -> str:
    cleaned = remote_path.strip().lstrip("/")
    if not cleaned:
        raise SystemExit("Remote path must not be empty")
    return cleaned


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-path", required=True, help="Local artifact file to upload")
    parser.add_argument("--remote-path", required=True, help="Remote COS object key")
    args = parser.parse_args()

    local_path = Path(args.local_path).expanduser().resolve()
    if not local_path.is_file():
        raise SystemExit(f"Local path is not a file: {local_path}")

    client = COSClient(
        secret_id=_required_env("COS_SECRET_ID"),
        secret_key=_required_env("COS_SECRET_KEY"),
        bucket=_required_env("COS_BUCKET"),
        region=_required_env("COS_REGION"),
        base_url=os.getenv("COS_BASE_URL"),
    )

    remote_path = _normalize_remote_path(args.remote_path)
    if local_path.stat().st_size >= LARGE_FILE_THRESHOLD:
        url = client.upload_large_file(local_path, remote_path)
    else:
        url = client.upload_file(local_path, remote_path)

    print(f"Uploaded {local_path} -> {remote_path}")
    print(f"URL: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
