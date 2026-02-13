"""Multipart upload mixin for backends (Issue #788).

Provides an opt-in ABC for backends that support native multipart/chunked
upload operations. Backends that inherit this mixin can participate in
tus.io resumable uploads with optimal performance.

- LocalBackend: assembles parts from temp directory
- S3ConnectorBackend: uses native S3 multipart upload API
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MultipartUploadMixin(ABC):
    """Opt-in mixin for backends that support multipart uploads.

    Backends implementing this mixin can efficiently handle chunked
    uploads by using their native multipart mechanisms (e.g. S3
    multipart upload, or local temp directory assembly).

    Backends that do NOT implement this mixin will fall back to
    buffering chunks in a temp directory and assembling them on
    completion via the ChunkedUploadService.
    """

    @property
    def supports_multipart(self) -> bool:
        """Whether this backend supports multipart uploads."""
        return True

    @abstractmethod
    def init_multipart(
        self,
        backend_path: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Initialize a multipart upload.

        Args:
            backend_path: Backend-specific path for the upload target.
            content_type: MIME type of the content.
            metadata: Optional key-value metadata for the upload.

        Returns:
            A backend-specific upload ID string.
        """
        ...

    @abstractmethod
    def upload_part(
        self,
        backend_path: str,
        upload_id: str,
        part_number: int,
        data: bytes,
    ) -> dict[str, Any]:
        """Upload a single part/chunk.

        Args:
            backend_path: Backend-specific path for the upload target.
            upload_id: The upload ID from init_multipart().
            part_number: 1-based part number.
            data: Raw bytes for this chunk.

        Returns:
            Dict with at least an "etag" key for part verification.
        """
        ...

    @abstractmethod
    def complete_multipart(
        self,
        backend_path: str,
        upload_id: str,
        parts: list[dict[str, Any]],
    ) -> str:
        """Complete a multipart upload by assembling all parts.

        Args:
            backend_path: Backend-specific path for the upload target.
            upload_id: The upload ID from init_multipart().
            parts: Ordered list of part dicts (from upload_part responses).

        Returns:
            Content hash of the assembled file.
        """
        ...

    @abstractmethod
    def abort_multipart(
        self,
        backend_path: str,
        upload_id: str,
    ) -> None:
        """Abort a multipart upload and clean up resources.

        Args:
            backend_path: Backend-specific path for the upload target.
            upload_id: The upload ID from init_multipart().
        """
        ...
