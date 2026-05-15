"""GCS Transport — raw key→blob I/O over Google Cloud Storage.

Shared between CASGCSBackend (CAS addressing) and PathGCSBackend (path
addressing).  This is the value of orthogonal composition — one transport
implementation serves both addressing strategies.

Authentication priority:
    1. access_token (OAuth, for user-scoped connectors)
    2. credentials_path (service account JSON)
    3. Application Default Credentials (gcloud auth / GCE service account)

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from typing import Any

from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError

_gcs_retry: Any | None
_gcs_storage: Any | None
_GcsNotFoundError: type[Exception]

try:
    from google.api_core import retry as _gcs_retry_mod
    from google.cloud import storage as _gcs_storage_mod
    from google.cloud.exceptions import NotFound as _GcsNotFoundError_mod
except ImportError:  # pragma: no cover - exercised in slim/cloud-extra installs
    _gcs_retry = None
    _gcs_storage = None

    class _MissingGcsNotFoundError(Exception):
        """Sentinel exception used when google-cloud is not installed."""

    _GcsNotFoundError = _MissingGcsNotFoundError
else:
    _gcs_retry = _gcs_retry_mod
    _gcs_storage = _gcs_storage_mod
    _GcsNotFoundError = _GcsNotFoundError_mod

retry = _gcs_retry
storage = _gcs_storage
GcsNotFoundError = _GcsNotFoundError

logger = logging.getLogger(__name__)


class GCSTransport:
    """Raw key→blob I/O over Google Cloud Storage.

    Implements the Transport protocol (structural typing — no inheritance).
    """

    transport_name: str = "gcs"

    def __init__(
        self,
        bucket_name: str,
        project_id: str | None = None,
        credentials_path: str | None = None,
        access_token: str | None = None,
        operation_timeout: float = 60.0,
        upload_timeout: float = 300.0,
    ) -> None:
        if storage is None or retry is None:
            raise BackendError(
                "google-cloud-storage is required for GCS transport. "
                "Install with: pip install 'nexus-ai-fs[gcs]'",
                backend="gcs",
                path=bucket_name,
            )
        self._retry = retry

        try:
            if access_token:
                from google.oauth2 import credentials as oauth2_credentials

                creds = oauth2_credentials.Credentials(token=access_token)
                self.client = storage.Client(project=project_id, credentials=creds)
            elif credentials_path:
                self.client = storage.Client.from_service_account_json(
                    credentials_path, project=project_id
                )
            else:
                self.client = storage.Client(project=project_id)

            self.bucket = self.client.bucket(bucket_name)
            self.bucket_name = bucket_name
            self._operation_timeout = operation_timeout
            self._upload_timeout = upload_timeout

        except Exception as e:
            raise BackendError(
                f"Failed to initialize GCS transport: {e}",
                backend="gcs",
                path=bucket_name,
            ) from e

    def verify_bucket(self) -> None:
        """Verify the bucket exists.  Called during connector init."""
        if not self.bucket.exists():
            raise BackendError(
                f"Bucket '{self.bucket_name}' does not exist",
                backend="gcs",
                path=self.bucket_name,
            )

    def check_versioning(self) -> bool:
        """Check if bucket has versioning enabled."""
        self.bucket.reload()
        return self.bucket.versioning_enabled or False

    def _retry_kwargs(self, deadline: int) -> dict[str, Any]:
        """Build retry kwargs when retry support is available."""
        retry_mod = getattr(self, "_retry", None) or retry
        if retry_mod is None:
            return {}
        return {"retry": retry_mod.Retry(deadline=deadline)}

    # === Transport Protocol Methods ===

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        try:
            blob = self.bucket.blob(key)
            blob.upload_from_string(
                data,
                content_type=content_type or None,
                timeout=self._operation_timeout,
                **self._retry_kwargs(deadline=120),
            )

            # Return generation number if versioning, else None
            blob.reload()
            return str(blob.generation) if blob.generation else None

        except Exception as e:
            raise BackendError(
                f"Failed to upload blob to {key}: {e}",
                backend="gcs",
                path=key,
            ) from e

    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        try:
            if version_id and version_id.isdigit():
                blob = self.bucket.blob(key, generation=int(version_id))
            else:
                blob = self.bucket.blob(key)

            if not blob.exists():
                raise NexusFileNotFoundError(key)

            content = blob.download_as_bytes(
                timeout=self._operation_timeout,
                **self._retry_kwargs(deadline=120),
            )

            blob.reload()
            generation_str = str(blob.generation) if blob.generation else None

            return bytes(content), generation_str

        except GcsNotFoundError as e:
            raise NexusFileNotFoundError(key) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to download blob from {key}: {e}",
                backend="gcs",
                path=key,
            ) from e

    def remove(self, key: str) -> None:
        try:
            blob = self.bucket.blob(key)

            if not blob.exists():
                raise NexusFileNotFoundError(key)

            blob.delete(
                timeout=self._operation_timeout,
                **self._retry_kwargs(deadline=120),
            )

        except GcsNotFoundError as e:
            raise NexusFileNotFoundError(key) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to delete blob at {key}: {e}",
                backend="gcs",
                path=key,
            ) from e

    def exists(self, key: str) -> bool:
        try:
            blob = self.bucket.blob(key)
            return bool(blob.exists())
        except Exception:
            return False

    def get_size(self, key: str) -> int:
        try:
            blob = self.bucket.blob(key)

            if not blob.exists():
                raise NexusFileNotFoundError(key)

            blob.reload()
            size = blob.size
            if size is None:
                raise BackendError(
                    "Failed to get blob size: size is None",
                    backend="gcs",
                    path=key,
                )
            return int(size)

        except GcsNotFoundError as e:
            raise NexusFileNotFoundError(key) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to get blob size for {key}: {e}",
                backend="gcs",
                path=key,
            ) from e

    def list_keys(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        try:
            blobs = self.bucket.list_keys(prefix=prefix, delimiter=delimiter)
            blob_keys = [blob.name for blob in blobs]
            common_prefixes = list(blobs.prefixes) if blobs.prefixes else []
            return blob_keys, common_prefixes

        except Exception as e:
            raise BackendError(
                f"Failed to list blobs with prefix {prefix}: {e}",
                backend="gcs",
                path=prefix,
            ) from e

    def copy_key(self, src_key: str, dst_key: str) -> None:
        """Server-side copy using GCS rewrite API.

        Uses the rewrite() token-continuation loop which handles
        large objects and cross-location copies reliably.  For
        same-location objects this completes in a single call.
        """
        try:
            source_blob = self.bucket.blob(src_key)
            if not source_blob.exists():
                raise NexusFileNotFoundError(src_key)

            dest_blob = self.bucket.blob(dst_key)
            token = None
            while True:
                token, _bytes_rewritten, _total_bytes = dest_blob.rewrite(
                    source_blob,
                    token=token,
                    timeout=self._operation_timeout,
                )
                if token is None:
                    break

        except GcsNotFoundError as e:
            raise NexusFileNotFoundError(src_key) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to copy blob from {src_key} to {dst_key}: {e}",
                backend="gcs",
                path=src_key,
            ) from e

    def create_dir(self, key: str) -> None:
        try:
            blob = self.bucket.blob(key)
            blob.upload_from_string(
                "",
                content_type="application/x-directory",
                timeout=self._operation_timeout,
                retry=self._retry.Retry(deadline=120),
            )
        except Exception as e:
            raise BackendError(
                f"Failed to create directory marker at {key}: {e}",
                backend="gcs",
                path=key,
            ) from e

    def stream(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        try:
            if version_id and version_id.isdigit():
                blob = self.bucket.blob(key, generation=int(version_id))
            else:
                blob = self.bucket.blob(key)

            if not blob.exists():
                raise NexusFileNotFoundError(key)

            buffer = io.BytesIO()
            blob.download_to_file(
                buffer,
                timeout=self._operation_timeout,
                **self._retry_kwargs(deadline=120),
            )
            buffer.seek(0)

            while True:
                chunk = buffer.read(chunk_size)
                if not chunk:
                    break
                yield chunk

        except GcsNotFoundError as e:
            raise NexusFileNotFoundError(key) from e
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to stream blob from {key}: {e}",
                backend="gcs",
                path=key,
            ) from e

    def store_chunked(
        self,
        key: str,
        chunks: "Iterator[bytes]",
        content_type: str = "",
    ) -> str | None:
        """Stream chunks into GCS via resumable upload.

        Uses ``blob.open('wb')`` which manages a resumable upload session
        under the hood — chunks are uploaded incrementally without
        buffering the full object in memory.
        """
        try:
            blob = self.bucket.blob(key)
            with blob.open(
                "wb",
                content_type=content_type or "application/octet-stream",
                timeout=self._upload_timeout,
                **self._retry_kwargs(deadline=300),
            ) as writer:
                for chunk in chunks:
                    writer.write(chunk)

            blob.reload()
            return str(blob.generation) if blob.generation else None

        except Exception as e:
            raise BackendError(
                f"Failed to write chunked blob to {key}: {e}",
                backend="gcs",
                path=key,
            ) from e

    def get_blob_range(self, key: str, offset: int, size: int) -> bytes:
        """Read a byte range from a GCS object (single HTTP request).

        Uses ``download_as_bytes(start=, end=)`` — GCS end is *inclusive*.

        Args:
            key: Object key.
            offset: Start byte offset (0-based).
            size: Number of bytes to read.

        Returns:
            The requested byte range.

        Issue #3406: Volume-level cold tiering — range reads.
        """
        try:
            blob = self.bucket.blob(key)
            # GCS end parameter is inclusive, so end = offset + size - 1
            data: bytes = blob.download_as_bytes(
                start=offset,
                end=offset + size - 1,
                timeout=self._operation_timeout,
                **self._retry_kwargs(deadline=120),
            )
            return bytes(data)

        except GcsNotFoundError as e:
            raise NexusFileNotFoundError(key) from e
        except Exception as e:
            raise BackendError(
                f"Failed to range-read blob at {key} (offset={offset}, size={size}): {e}",
                backend="gcs",
                path=key,
            ) from e

    def upload_file(
        self, key: str, local_path: str, chunk_size: int = 8 * 1024 * 1024
    ) -> str | None:
        """Upload a local file to GCS via resumable chunked upload.

        Args:
            key: Destination object key.
            local_path: Path to the local file.
            chunk_size: Upload chunk size in bytes (default 8 MB).

        Returns:
            Generation number string if versioning enabled, else None.

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

    # === GCS-Specific Extras (not part of Transport protocol) ===

    def generate_signed_url(self, key: str, expires_in: int = 3600, method: str = "GET") -> str:
        """Generate a V4 signed URL for direct download."""
        from datetime import timedelta

        blob = self.bucket.blob(key)
        url: str = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=min(expires_in, 604800)),
            method=method,
        )
        return url

    def get_generation(self, key: str) -> str | None:
        """Get GCS generation number for a blob."""
        try:
            blob = self.bucket.blob(key)
            if not blob.exists():
                return None
            blob.reload()
            return str(blob.generation) if blob.generation else None
        except Exception:
            return None

    def reload_blob_metadata(self, key: str) -> dict:
        """Get full blob metadata (size, updated, generation, etc.)."""
        blob = self.bucket.blob(key)
        try:
            blob.reload()
        except GcsNotFoundError as e:
            raise NexusFileNotFoundError(key) from e

        return {
            "size": blob.size or 0,
            "updated": blob.updated,
            "generation": str(blob.generation) if blob.generation else None,
        }
