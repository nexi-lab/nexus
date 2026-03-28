"""Transport Protocol — raw key→blob I/O abstraction.

Transport captures the *transport* dimension (WHERE data lives) while
CASAddressingEngine / PathAddressingEngine capture the *addressing* dimension (HOW data is
addressed).  These two dimensions are orthogonal:

              Transport (WHERE)
              Local    GCS     S3      Azure(future)
Addressing   +--------+-------+-------+--------------+
(HOW)   CAS  | Local  | GCS   |  S3   |   Azure      |
             | Bkend  | Trans | Trans |   Trans      |
        Path | Pass-  | GCS   | S3    |   Azure      |
             | through| Trans | Trans |   Trans      |
             +--------+-------+-------+--------------+

A single GCSTransport is shared between CASGCSBackend (CAS addressing)
and PathGCSBackend (path addressing) — this is the value of
orthogonal composition.

Design decisions:
    - Protocol (structural typing) rather than ABC — no inheritance required,
      any class with matching methods is a valid transport.
    - 9 methods matching the original BaseBlobStorageConnector abstract methods,
      but renamed to transport-neutral terminology (put/get vs upload/download).
    - CASAddressingEngine uses a subset (6 methods). PathAddressingEngine uses all 9.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """Raw key→blob I/O.  No addressing, no ref-count, no hash logic.

    Implementors provide transport-level operations for a specific storage
    backend (GCS, S3, Azure, local FS, in-memory for tests, etc.).

    Attributes:
        transport_name: Short identifier for the transport (e.g. "gcs", "s3").
    """

    transport_name: str

    def put_blob(self, key: str, data: bytes, content_type: str = "") -> str | None:
        """Store a blob at *key*.

        Args:
            key: Storage key (e.g. "cas/ab/cd/abcd1234…" or "prefix/file.txt").
            data: Raw bytes to store.
            content_type: Optional MIME type hint.

        Returns:
            Version identifier if the transport supports versioning,
            otherwise ``None``.
        """
        ...

    def get_blob(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        """Retrieve a blob.

        Args:
            key: Storage key.
            version_id: Optional version for versioned transports.

        Returns:
            ``(data, version_id)`` — version_id is ``None`` when the
            transport does not support versioning or the blob is unversioned.

        Raises:
            NexusFileNotFoundError: If the blob does not exist.
        """
        ...

    def delete_blob(self, key: str) -> None:
        """Delete a blob.

        Raises:
            NexusFileNotFoundError: If the blob does not exist.
        """
        ...

    def blob_exists(self, key: str) -> bool:
        """Check whether a blob exists at *key*."""
        ...

    def get_blob_size(self, key: str) -> int:
        """Return blob size in bytes.

        Raises:
            NexusFileNotFoundError: If the blob does not exist.
        """
        ...

    def list_blobs(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        """List blobs under *prefix*.

        Returns:
            ``(blob_keys, common_prefixes)`` — mirrors the S3/GCS list
            semantics with delimiter-based virtual directories.
        """
        ...

    def copy_blob(self, src_key: str, dst_key: str) -> None:
        """Copy a blob from *src_key* to *dst_key*.

        Raises:
            NexusFileNotFoundError: If source does not exist.
        """
        ...

    def create_directory_marker(self, key: str) -> None:
        """Create an empty directory marker blob at *key*.

        The key should typically end with ``/``.
        """
        ...

    def stream_blob(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """Stream blob content in chunks.

        Default transports may download the full blob and chunk it;
        advanced transports can implement true streaming.

        Args:
            key: Storage key.
            chunk_size: Bytes per yielded chunk.
            version_id: Optional version identifier.

        Yields:
            Chunks of blob data.
        """
        ...

    def put_blob_chunked(
        self,
        key: str,
        chunks: Iterator[bytes],
        content_type: str = "",
    ) -> str | None:
        """Write a blob from an iterator of byte chunks.

        Enables cross-backend streaming copy without buffering the
        entire file in memory.  Each transport uses its native
        chunked-upload mechanism:
        - S3: multipart upload (min 5 MB per part, except last)
        - GCS: resumable upload via ``blob.open('wb')``
        - Local: write to temp file, then atomic replace

        Args:
            key: Destination storage key.
            chunks: Iterator yielding byte chunks.
            content_type: Optional MIME type hint.

        Returns:
            Version identifier if versioning is enabled, else ``None``.
        """
        ...
