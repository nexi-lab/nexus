"""In-memory Transport for LLM backends.

Stores CAS blobs in process memory. LLM conversations are ephemeral
during a session — persistence is handled by CAS flush to durable
backends (local/GCS/S3) in Step 2 (DT_STREAM + CAS flush).

Satisfies the 6-method CASAddressingEngine subset of Transport:
    put_blob, get_blob, delete_blob, blob_exists, get_blob_size, stream_blob.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

from nexus.contracts.exceptions import NexusFileNotFoundError


class LLMTransport:
    """In-memory blob transport for LLM request/response storage.

    Thread-safe via a threading.Lock on the internal dict. Suitable for
    concurrent CAS operations from multiple async tasks writing to the
    same conversation store.

    Not a subclass of Transport (Protocol-based structural typing).
    """

    transport_name: str = "llm_memory"

    __slots__ = ("_store", "_lock")

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put_blob(self, key: str, data: bytes, content_type: str = "") -> str | None:
        """Store blob in memory. Returns None (no versioning)."""
        with self._lock:
            self._store[key] = bytes(data) if isinstance(data, memoryview) else data
        return None

    def get_blob(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        """Retrieve blob from memory."""
        with self._lock:
            data = self._store.get(key)
        if data is None:
            raise NexusFileNotFoundError(path=key, message=f"Blob not found: {key}")
        return data, None

    def delete_blob(self, key: str) -> None:
        """Delete blob from memory."""
        with self._lock:
            if key not in self._store:
                raise NexusFileNotFoundError(path=key, message=f"Blob not found: {key}")
            del self._store[key]

    def blob_exists(self, key: str) -> bool:
        """Check if blob exists in memory."""
        with self._lock:
            return key in self._store

    def get_blob_size(self, key: str) -> int:
        """Return blob size in bytes."""
        with self._lock:
            data = self._store.get(key)
        if data is None:
            raise NexusFileNotFoundError(path=key, message=f"Blob not found: {key}")
        return len(data)

    def list_blobs(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        """List blobs under prefix."""
        with self._lock:
            keys = list(self._store.keys())
        blobs = []
        prefixes: set[str] = set()
        for k in keys:
            if not k.startswith(prefix):
                continue
            remainder = k[len(prefix) :]
            if delimiter and delimiter in remainder:
                pfx = prefix + remainder[: remainder.index(delimiter) + len(delimiter)]
                prefixes.add(pfx)
            else:
                blobs.append(k)
        return sorted(blobs), sorted(prefixes)

    def copy_blob(self, src_key: str, dst_key: str) -> None:
        """Copy blob from src to dst."""
        with self._lock:
            data = self._store.get(src_key)
            if data is None:
                raise NexusFileNotFoundError(path=src_key, message=f"Blob not found: {src_key}")
            self._store[dst_key] = data

    def create_directory_marker(self, key: str) -> None:
        """Create empty directory marker."""
        with self._lock:
            self._store[key] = b""

    def stream_blob(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """Stream blob in chunks."""
        data, _ = self.get_blob(key, version_id)
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def put_blob_chunked(
        self,
        key: str,
        chunks: Iterator[bytes],
        content_type: str = "",
    ) -> str | None:
        """Write blob from chunks (in-memory: just concatenate)."""
        data = b"".join(chunks)
        return self.put_blob(key, data, content_type)
