#!/usr/bin/env python3
"""Upload a build artifact to Tencent COS using environment-provided credentials."""

from __future__ import annotations

import argparse
import mimetypes
import os
from pathlib import Path

from qcloud_cos import CosConfig, CosS3Client

SINGLE_PUT_OBJECT_LIMIT = 5 * 1024 * 1024 * 1024


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _normalize_remote_path(remote_path: str) -> str:
    cleaned = remote_path.strip().lstrip("/")
    if not cleaned:
        raise SystemExit("Remote path must not be empty")
    return cleaned


def upload_file(local_path: Path, remote_path: str) -> str:
    secret_id = _required_env("COS_SECRET_ID")
    secret_key = _required_env("COS_SECRET_KEY")
    bucket = _required_env("COS_BUCKET")
    region = _required_env("COS_REGION")
    base_url = os.getenv("COS_BASE_URL") or f"https://{bucket}.cos.{region}.myqcloud.com"

    if not local_path.is_file():
        raise SystemExit(f"Local path is not a file: {local_path}")

    remote_key = _normalize_remote_path(remote_path)
    content_type, _ = mimetypes.guess_type(str(local_path))

    config = CosConfig(Region=region, SecretId=secret_id, SecretKey=secret_key)
    client = CosS3Client(config)

    if local_path.stat().st_size > SINGLE_PUT_OBJECT_LIMIT:
        client.upload_file(
            Bucket=bucket,
            Key=remote_key,
            LocalFilePath=str(local_path),
            PartSize=10,
            MAXThread=5,
        )
    else:
        with local_path.open("rb") as fh:
            client.put_object(
                Bucket=bucket,
                Key=remote_key,
                Body=fh,
                ContentType=content_type,
            )

    return f"{base_url.rstrip('/')}/{remote_key}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-path", required=True, help="Local artifact file to upload")
    parser.add_argument("--remote-path", required=True, help="Remote COS object key")
    args = parser.parse_args()

    local_path = Path(args.local_path).expanduser().resolve()
    url = upload_file(local_path, args.remote_path)
    print(f"Uploaded {local_path} -> {args.remote_path}")
    print(f"URL: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
