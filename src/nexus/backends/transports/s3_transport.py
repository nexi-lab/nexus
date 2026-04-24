"""S3 Transport — raw key→blob I/O over AWS S3.

Shared between PathS3Backend (path addressing) and potential future
S3CASAddressingEngine (CAS addressing).

Authentication priority:
    1. Explicit credentials (access_key_id + secret_access_key)
    2. Credentials file path
    3. AWS default credentials chain (~/.aws/credentials, env vars, IAM roles)

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError

try:
    import boto3 as _boto3
    from botocore.config import Config as _BotoConfig
    from botocore.exceptions import ClientError as _BotoClientError
except ImportError:  # pragma: no cover - exercised in slim/cloud-extra installs
    _boto3 = None
    _BotoConfig = None
    _BotoClientError = Exception

boto3 = _boto3
Config = _BotoConfig
BotoClientError = _BotoClientError

logger = logging.getLogger(__name__)


def _client_error_code(exc: BaseException) -> str:
    """Extract botocore error code safely when available."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error", {})
    if not isinstance(error, dict):
        return ""
    code = error.get("Code")
    return str(code) if code is not None else ""


class S3Transport:
    """Raw key→blob I/O over AWS S3.

    Implements the Transport protocol (structural typing — no inheritance).
    """

    transport_name: str = "s3"

    def __init__(
        self,
        bucket_name: str,
        region_name: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        credentials_path: str | None = None,
        operation_timeout: float = 60.0,
        upload_timeout: float = 300.0,
    ) -> None:
        self.bucket_name = bucket_name
        self._operation_timeout = operation_timeout
        self._upload_timeout = upload_timeout

        if boto3 is None or Config is None:
            raise BackendError(
                "boto3 is required for S3 transport. Install with: pip install 'nexus-ai-fs[s3]'",
                backend="s3",
                path=bucket_name,
            )

        try:
            boto_config = Config(
                retries={"max_attempts": 3, "mode": "adaptive"},
                max_pool_connections=25,
            )

            session_kwargs: dict[str, Any] = {}
            if region_name:
                session_kwargs["region_name"] = region_name

            # Credential priority
            if access_key_id and secret_access_key:
                session_kwargs["aws_access_key_id"] = access_key_id
                session_kwargs["aws_secret_access_key"] = secret_access_key
                if session_token:
                    session_kwargs["aws_session_token"] = session_token
            elif credentials_path:
                creds = self._load_credentials_file(credentials_path)
                session_kwargs["aws_access_key_id"] = creds.get("aws_access_key_id", "")
                session_kwargs["aws_secret_access_key"] = creds.get("aws_secret_access_key", "")
                if creds.get("aws_session_token"):
                    session_kwargs["aws_session_token"] = creds["aws_session_token"]

            session = boto3.Session(**session_kwargs)
            self.s3_client = session.client("s3", config=boto_config)

        except Exception as e:
            raise BackendError(
                f"Failed to initialize S3 transport: {e}",
                backend="s3",
                path=bucket_name,
            ) from e

    @staticmethod
    def _load_credentials_file(path: str) -> dict[str, str]:
        """Load AWS credentials from a JSON file."""
        try:
            with open(path) as f:
                result: dict[str, str] = json.load(f)
                return result
        except Exception as e:
            raise BackendError(
                f"Failed to load S3 credentials from {path}: {e}",
                backend="s3",
                path=path,
            ) from e

    def verify_bucket(self) -> None:
        """Verify the bucket exists and is accessible."""
        try:
            self.s3_client.head_bucket(Bucket=self.bucket_name)
        except BotoClientError as e:
            error_code = _client_error_code(e)
            if error_code == "404":
                raise BackendError(
                    f"Bucket '{self.bucket_name}' does not exist",
                    backend="s3",
                    path=self.bucket_name,
                ) from e
            elif error_code == "403":
                raise BackendError(
                    f"Access denied to bucket '{self.bucket_name}'",
                    backend="s3",
                    path=self.bucket_name,
                ) from e
            raise BackendError(
                f"Failed to verify S3 bucket: {e}",
                backend="s3",
                path=self.bucket_name,
            ) from e

    def check_versioning(self) -> bool:
        """Check if bucket has versioning enabled."""
        try:
            response = self.s3_client.get_bucket_versioning(Bucket=self.bucket_name)
            return bool(response.get("Status") == "Enabled")
        except Exception:
            return False

    # === Transport Protocol Methods ===

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        try:
            kwargs: dict[str, Any] = {
                "Bucket": self.bucket_name,
                "Key": key,
                "Body": data,
            }
            if content_type:
                kwargs["ContentType"] = content_type

            response = self.s3_client.put_object(**kwargs)

            # Return version ID if versioning is enabled
            version_id: str | None = response.get("VersionId")
            return version_id

        except BotoClientError as e:
            raise BackendError(
                f"Failed to upload blob to {key}: {e}",
                backend="s3",
                path=key,
            ) from e

    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        try:
            kwargs: dict[str, Any] = {
                "Bucket": self.bucket_name,
                "Key": key,
            }
            if version_id:
                kwargs["VersionId"] = version_id

            response = self.s3_client.get_object(**kwargs)
            content = response["Body"].read()
            resp_version_id = response.get("VersionId")

            return bytes(content), resp_version_id

        except BotoClientError as e:
            error_code = _client_error_code(e)
            if error_code in ("NoSuchKey", "404"):
                raise NexusFileNotFoundError(key) from e
            raise BackendError(
                f"Failed to download blob from {key}: {e}",
                backend="s3",
                path=key,
            ) from e

    def remove(self, key: str) -> None:
        try:
            # Check existence first (S3 delete is idempotent)
            self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=key)

        except BotoClientError as e:
            error_code = _client_error_code(e)
            if error_code in ("NoSuchKey", "404"):
                raise NexusFileNotFoundError(key) from e
            raise BackendError(
                f"Failed to delete blob at {key}: {e}",
                backend="s3",
                path=key,
            ) from e

    def exists(self, key: str) -> bool:
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except BotoClientError:
            return False

    def get_size(self, key: str) -> int:
        try:
            response = self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
            return int(response["ContentLength"])

        except BotoClientError as e:
            error_code = _client_error_code(e)
            if error_code in ("NoSuchKey", "404"):
                raise NexusFileNotFoundError(key) from e
            raise BackendError(
                f"Failed to get blob size for {key}: {e}",
                backend="s3",
                path=key,
            ) from e

    def list_keys(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        try:
            kwargs: dict[str, Any] = {
                "Bucket": self.bucket_name,
                "Prefix": prefix,
            }
            if delimiter:
                kwargs["Delimiter"] = delimiter

            blob_keys: list[str] = []
            common_prefixes: list[str] = []

            # Handle pagination
            paginator = self.s3_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents", []):
                    blob_keys.append(obj["Key"])
                for prefix_item in page.get("CommonPrefixes", []):
                    common_prefixes.append(prefix_item["Prefix"])

            return blob_keys, common_prefixes

        except BotoClientError as e:
            raise BackendError(
                f"Failed to list blobs with prefix {prefix}: {e}",
                backend="s3",
                path=prefix,
            ) from e

    def copy_key(self, src_key: str, dst_key: str) -> None:
        """Server-side copy using boto3 managed transfer.

        Automatically handles multipart copy for objects >5 GB via
        boto3's managed copy (CreateMultipartUpload + UploadPartCopy).
        """
        try:
            copy_source = {"Bucket": self.bucket_name, "Key": src_key}
            self.s3_client.copy(copy_source, self.bucket_name, dst_key)

        except BotoClientError as e:
            error_code = _client_error_code(e)
            if error_code in ("NoSuchKey", "404"):
                raise NexusFileNotFoundError(src_key) from e
            raise BackendError(
                f"Failed to copy blob from {src_key} to {dst_key}: {e}",
                backend="s3",
                path=src_key,
            ) from e

    def create_dir(self, key: str) -> None:
        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=b"",
                ContentType="application/x-directory",
            )
        except BotoClientError as e:
            raise BackendError(
                f"Failed to create directory marker at {key}: {e}",
                backend="s3",
                path=key,
            ) from e

    def stream(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        try:
            kwargs: dict[str, Any] = {
                "Bucket": self.bucket_name,
                "Key": key,
            }
            if version_id:
                kwargs["VersionId"] = version_id

            response = self.s3_client.get_object(**kwargs)
            body = response["Body"]

            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield chunk

        except BotoClientError as e:
            error_code = _client_error_code(e)
            if error_code in ("NoSuchKey", "404"):
                raise NexusFileNotFoundError(key) from e
            raise BackendError(
                f"Failed to stream blob from {key}: {e}",
                backend="s3",
                path=key,
            ) from e

    def get_blob_range(self, key: str, offset: int, size: int) -> bytes:
        """Read a byte range from an S3 object (single HTTP request).

        Uses the ``Range`` header — S3 range is *inclusive* on both ends.

        Args:
            key: Object key.
            offset: Start byte offset (0-based).
            size: Number of bytes to read.

        Returns:
            The requested byte range.

        Issue #3406: Volume-level cold tiering — range reads.
        """
        try:
            # S3 Range header: bytes=start-end (inclusive)
            range_header = f"bytes={offset}-{offset + size - 1}"
            response = self.s3_client.get_object(
                Bucket=self.bucket_name,
                Key=key,
                Range=range_header,
            )
            return bytes(response["Body"].read())

        except BotoClientError as e:
            error_code = _client_error_code(e)
            if error_code in ("NoSuchKey", "404"):
                raise NexusFileNotFoundError(key) from e
            if error_code == "InvalidRange":
                raise BackendError(
                    f"Invalid range for {key}: offset={offset}, size={size}",
                    backend="s3",
                    path=key,
                ) from e
            raise BackendError(
                f"Failed to range-read blob at {key} (offset={offset}, size={size}): {e}",
                backend="s3",
                path=key,
            ) from e

    def upload_file(
        self, key: str, local_path: str, chunk_size: int = 8 * 1024 * 1024
    ) -> str | None:
        """Upload a local file to S3 via multipart upload.

        Args:
            key: Destination object key.
            local_path: Path to the local file.
            chunk_size: Upload chunk size in bytes (default 8 MB).

        Returns:
            Version ID string if versioning enabled, else None.

        Issue #3406: Volume-level cold tiering — volume upload.
        """

        def _file_chunks() -> Iterator[bytes]:
            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk

        return self.store_chunked(key, _file_chunks())

    # S3 multipart minimum part size (5 MB) — except for the last part.
    _MIN_PART_SIZE = 5 * 1024 * 1024

    def store_chunked(
        self,
        key: str,
        chunks: Iterator[bytes],
        content_type: str = "",
    ) -> str | None:
        """Stream chunks into S3 via multipart upload.

        Buffers incoming chunks until they reach ``_MIN_PART_SIZE`` (5 MB),
        then uploads each part.  Aborts the upload on any error.
        """
        upload_id: str | None = None
        try:
            upload_id = self.init_multipart(
                key, content_type=content_type or "application/octet-stream"
            )
            parts: list[dict[str, Any]] = []
            part_num = 1
            buf = bytearray()

            for chunk in chunks:
                buf.extend(chunk)
                while len(buf) >= self._MIN_PART_SIZE:
                    part_data = bytes(buf[: self._MIN_PART_SIZE])
                    buf = buf[self._MIN_PART_SIZE :]
                    part = self.upload_part(key, upload_id, part_num, part_data)
                    parts.append(part)
                    part_num += 1

            # Flush remaining bytes as the final part
            if buf or not parts:
                part = self.upload_part(key, upload_id, part_num, bytes(buf))
                parts.append(part)

            return self.complete_multipart(key, upload_id, parts)

        except Exception:
            if upload_id is not None:
                with __import__("contextlib").suppress(Exception):
                    self.abort_multipart(key, upload_id)
            raise

    # === S3-Specific Extras (not part of Transport protocol) ===

    def generate_presigned_url(
        self, key: str, expires_in: int = 3600, method: str = "get_object"
    ) -> str:
        """Generate a presigned URL for direct download/upload."""
        url: str = self.s3_client.generate_presigned_url(
            method,
            Params={"Bucket": self.bucket_name, "Key": key},
            ExpiresIn=expires_in,
        )
        return url

    def init_multipart(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Initialize S3 multipart upload, return upload_id."""
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket_name,
            "Key": key,
            "ContentType": content_type,
        }
        if metadata:
            kwargs["Metadata"] = metadata

        response = self.s3_client.create_multipart_upload(**kwargs)
        upload_id: str = response["UploadId"]
        return upload_id

    def upload_part(
        self, key: str, upload_id: str, part_number: int, data: bytes
    ) -> dict[str, Any]:
        """Upload a single part.  Returns dict with 'ETag'."""
        response = self.s3_client.upload_part(
            Bucket=self.bucket_name,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=data,
        )
        return {"ETag": response["ETag"], "PartNumber": part_number}

    def complete_multipart(
        self, key: str, upload_id: str, parts: list[dict[str, Any]]
    ) -> str | None:
        """Complete multipart upload.  Returns version_id if versioning."""
        response = self.s3_client.complete_multipart_upload(
            Bucket=self.bucket_name,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        vid: str | None = response.get("VersionId")
        return vid

    def abort_multipart(self, key: str, upload_id: str) -> None:
        """Abort a multipart upload."""
        self.s3_client.abort_multipart_upload(
            Bucket=self.bucket_name,
            Key=key,
            UploadId=upload_id,
        )

    def get_version_id(self, key: str) -> str | None:
        """Get S3 version ID for a blob."""
        try:
            response = self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
            vid: str | None = response.get("VersionId")
            return vid
        except BotoClientError:
            return None

    def get_object_metadata(self, key: str) -> dict[str, Any]:
        """Get full object metadata (size, last_modified, version_id, etc.)."""
        try:
            response = self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
            return {
                "size": response.get("ContentLength", 0),
                "last_modified": response.get("LastModified"),
                "version_id": response.get("VersionId"),
                "etag": response.get("ETag", "").strip('"'),
            }
        except BotoClientError as e:
            error_code = _client_error_code(e)
            if error_code in ("NoSuchKey", "404"):
                raise NexusFileNotFoundError(key) from e
            raise BackendError(
                f"Failed to get metadata for {key}: {e}",
                backend="s3",
                path=key,
            ) from e
