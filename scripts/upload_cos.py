#!/usr/bin/env python3
"""Upload a build artifact to Tencent COS using the official Python SDK.

Every upload goes through the global acceleration endpoint as a chunked,
retrying transfer. A GitHub runner talking straight to the regional endpoint
(ap-beijing) is slow enough that COS aborts the request itself:

    CosServiceError: {'code': 'UserNetworkTooSlow', ...}

which killed a plugin release mid-upload. `release-nexusd-cluster.yml` hit the
same wall and answered it the same way; this is that answer for every caller
of this script.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
from pathlib import Path

from qcloud_cos import CosConfig, CosS3Client

# Global acceleration: routes the runner to the nearest COS edge instead of
# across the public internet to the bucket's region.
ACCELERATE_ENDPOINT = "cos.accelerate.myqcloud.com"
# Small enough that a stalled part is retried rather than restarting the file.
PART_SIZE_MB = 10
UPLOAD_THREADS = 5
# Per-request retries inside the SDK, on top of the per-part retry above.
REQUEST_RETRIES = 5


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
        config = CosConfig(
            Region=region,
            SecretId=secret_id,
            SecretKey=secret_key,
            Endpoint=ACCELERATE_ENDPOINT,
        )
        self.client = CosS3Client(config, retry=REQUEST_RETRIES)
        self.bucket = bucket
        self.region = region
        # Report the object at the host we uploaded through — the same
        # acceleration domain the published download URLs use.
        self.base_url = base_url or f"https://{bucket}.{ACCELERATE_ENDPOINT}"

    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        content_type: str | None = None,
    ) -> str:
        """Upload one artifact, chunked, whatever its size.

        `upload_file` picks simple-vs-multipart itself from `PartSize`, and
        retries a part rather than the whole transfer. A plain `put_object`
        streams the file in one request, which is what COS was rejecting as
        too slow — so there is no size below which the simple path is worth
        keeping.
        """
        if content_type is None:
            content_type, _ = mimetypes.guess_type(str(local_path))

        self.client.upload_file(
            Bucket=self.bucket,
            Key=remote_path,
            LocalFilePath=str(local_path),
            PartSize=PART_SIZE_MB,
            MAXThread=UPLOAD_THREADS,
            ContentType=content_type,
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
    url = client.upload_file(local_path, remote_path)

    print(f"Uploaded {local_path} -> {remote_path}")
    print(f"URL: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
