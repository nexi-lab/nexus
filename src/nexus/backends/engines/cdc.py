"""Content-Defined Chunking (CDC) for large files (Issue #1074).

CDCEngine provides chunking for any CASBackend subclass via composition:

    class LocalCASBackend(CASBackend):
        def __init__(self, ...):
            super().__init__(transport, ...)
            self._cdc = CDCEngine(self)

        def write_content(self, content, context=None):
            if self._cdc.should_chunk(content):
                hash = self._cdc.write_chunked(content)
                return WriteResult(content_hash=hash, size=len(content))
            return super().write_content(content, context)

Storage structure:
    cas/
    ├── ab/cd/
    │   ├── abcd1234...         # Single-blob OR chunk content
    │   ├── abcd1234...meta     # Metadata: {"ref_count": N, "is_chunk": true}
    │   ├── 5678efgh...         # Chunked manifest (JSON)
    │   └── 5678efgh...meta     # {"ref_count": N, "is_chunked_manifest": true}
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from nexus.core.hash_fast import hash_content

if TYPE_CHECKING:
    from nexus.backends.base.cas_backend import CASBackend
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# CDC Configuration Constants
CDC_THRESHOLD_BYTES = 16 * 1024 * 1024  # 16MB
CDC_MIN_CHUNK_SIZE = 256 * 1024  # 256KB
CDC_AVG_CHUNK_SIZE = 1 * 1024 * 1024  # 1MB
CDC_MAX_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB
CDC_PARALLEL_WORKERS = 8


# =============================================================================
# Data Classes
# =============================================================================


@dataclass(frozen=True, slots=True)
class ChunkInfo:
    """Information about a single chunk in a chunked file."""

    chunk_hash: str
    offset: int
    length: int

    def to_dict(self) -> dict[str, Any]:
        return {"chunk_hash": self.chunk_hash, "offset": self.offset, "length": self.length}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChunkInfo:
        return cls(chunk_hash=data["chunk_hash"], offset=data["offset"], length=data["length"])


@dataclass(frozen=True, slots=True)
class ChunkedReference:
    """Manifest for a file stored as CDC chunks."""

    type: Literal["chunked_manifest_v1"] = "chunked_manifest_v1"
    total_size: int = 0
    chunk_count: int = 0
    avg_chunk_size: int = 0
    content_hash: str = ""
    chunks: tuple[ChunkInfo, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "total_size": self.total_size,
            "chunk_count": self.chunk_count,
            "avg_chunk_size": self.avg_chunk_size,
            "content_hash": self.content_hash,
            "chunks": [c.to_dict() for c in self.chunks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChunkedReference:
        return cls(
            type=data.get("type", "chunked_manifest_v1"),
            total_size=data["total_size"],
            chunk_count=data["chunk_count"],
            avg_chunk_size=data.get("avg_chunk_size", 0),
            content_hash=data["content_hash"],
            chunks=tuple(ChunkInfo.from_dict(c) for c in data["chunks"]),
        )

    def to_json(self) -> bytes:
        return json.dumps(self.to_dict(), separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> ChunkedReference:
        return cls.from_dict(json.loads(data))

    @staticmethod
    def is_chunked_manifest(data: bytes) -> bool:
        """Check if content is a chunked manifest (not raw content)."""
        if len(data) > 500 * 1024:
            return False
        try:
            parsed = json.loads(data)
            return isinstance(parsed, dict) and parsed.get("type") == "chunked_manifest_v1"
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False


# =============================================================================
# CDCEngine
# =============================================================================


class CDCEngine:
    """CDC chunking engine — composed into CASBackend subclasses.

    Uses CASBackend's internal methods directly:
    ``_transport``, ``_blob_key()``, ``_read_meta()``, ``_write_meta()``,
    ``_meta_update_locked()``, ``_stripe_lock``, ``_bloom``.
    """

    __slots__ = ("_backend", "threshold", "min_chunk", "avg_chunk", "max_chunk", "workers")

    def __init__(
        self,
        backend: CASBackend,
        *,
        threshold: int = CDC_THRESHOLD_BYTES,
        min_chunk: int = CDC_MIN_CHUNK_SIZE,
        avg_chunk: int = CDC_AVG_CHUNK_SIZE,
        max_chunk: int = CDC_MAX_CHUNK_SIZE,
        workers: int = CDC_PARALLEL_WORKERS,
    ) -> None:
        self._backend = backend
        self.threshold = threshold
        self.min_chunk = min_chunk
        self.avg_chunk = avg_chunk
        self.max_chunk = max_chunk
        self.workers = workers

    def should_chunk(self, content: bytes) -> bool:
        return len(content) >= self.threshold

    # === Write ===

    def write_chunked(self, content: bytes, context: OperationContext | None = None) -> str:
        """Write content as CDC chunks + manifest. Returns manifest hash."""
        start_time = time.perf_counter()
        b = self._backend

        full_content_hash = hash_content(content)
        chunk_tuples = self._chunk_content(content)

        logger.info(
            f"Chunking {len(content)} bytes -> {len(chunk_tuples)} chunks "
            f"(avg {len(content) // len(chunk_tuples) if chunk_tuples else 0} bytes)"
        )

        # Write chunks in parallel
        chunk_infos: list[ChunkInfo] = []
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures: dict[Any, tuple[int, int]] = {}
            for offset, length, chunk_bytes in chunk_tuples:
                future = executor.submit(self._write_single_chunk, chunk_bytes)
                futures[future] = (offset, length)

            results: dict[int, ChunkInfo] = {}
            for future in as_completed(futures):
                offset, length = futures[future]
                chunk_hash = future.result()
                results[offset] = ChunkInfo(chunk_hash=chunk_hash, offset=offset, length=length)

        for offset in sorted(results.keys()):
            chunk_infos.append(results[offset])

        # Build manifest
        manifest = ChunkedReference(
            total_size=len(content),
            chunk_count=len(chunk_infos),
            avg_chunk_size=len(content) // len(chunk_infos) if chunk_infos else 0,
            content_hash=full_content_hash,
            chunks=tuple(chunk_infos),
        )
        manifest_bytes = manifest.to_json()
        manifest_hash = hash_content(manifest_bytes)

        # Store manifest blob
        key = b._blob_key(manifest_hash)
        b._transport.put_blob(key, manifest_bytes)

        def _update_manifest(meta: dict[str, Any]) -> dict[str, Any]:
            meta["ref_count"] = meta.get("ref_count", 0) + 1
            meta["size"] = len(content)  # original content size
            meta["is_chunked_manifest"] = True
            meta["chunk_count"] = len(chunk_infos)
            return meta

        updated = b._meta_update_locked(manifest_hash, _update_manifest)
        if updated.get("ref_count", 0) == 1 and b._bloom is not None:
            b._bloom.add(manifest_hash)

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            f"Wrote chunked content: {len(content)} bytes -> {len(chunk_infos)} chunks "
            f"in {elapsed_ms:.1f}ms (manifest={manifest_hash[:16]}...)"
        )
        return manifest_hash

    def _write_single_chunk(self, chunk_bytes: bytes) -> str:
        b = self._backend
        chunk_hash = hash_content(chunk_bytes)
        key = b._blob_key(chunk_hash)
        b._transport.put_blob(key, chunk_bytes)

        def _update(meta: dict[str, Any]) -> dict[str, Any]:
            meta["ref_count"] = meta.get("ref_count", 0) + 1
            meta["size"] = len(chunk_bytes)
            meta["is_chunk"] = True
            return meta

        updated = b._meta_update_locked(chunk_hash, _update)
        is_new = updated.get("ref_count", 0) == 1
        if is_new and b._bloom is not None:
            b._bloom.add(chunk_hash)
        return chunk_hash

    # === Read ===

    def read_chunked(self, content_hash: str, context: OperationContext | None = None) -> bytes:
        """Reassemble chunked content from manifest + chunks."""
        start_time = time.perf_counter()
        b = self._backend

        key = b._blob_key(content_hash)
        manifest_data, _ = b._transport.get_blob(key)
        manifest = ChunkedReference.from_json(manifest_data)

        chunk_data: dict[int, bytes] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures: dict[Any, int] = {}
            for ci in manifest.chunks:
                future = executor.submit(self._read_single_chunk, ci.chunk_hash)
                futures[future] = ci.offset
            for future in as_completed(futures):
                chunk_data[futures[future]] = future.result()

        content = b"".join(chunk_data[o] for o in sorted(chunk_data.keys()))

        actual_hash = hash_content(content)
        if actual_hash != manifest.content_hash:
            raise ValueError(
                f"Content hash mismatch: expected {manifest.content_hash}, got {actual_hash}"
            )

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.debug(
            f"Read chunked content: {len(content)} bytes from {manifest.chunk_count} chunks "
            f"in {elapsed_ms:.1f}ms"
        )
        return content

    def _read_single_chunk(self, chunk_hash: str) -> bytes:
        key = self._backend._blob_key(chunk_hash)
        data, _ = self._backend._transport.get_blob(key)
        return data

    # === Query ===

    def is_chunked(self, content_hash: str) -> bool:
        """Check if content_hash refers to a chunked manifest."""
        try:
            meta = self._backend._read_meta(content_hash)
            return bool(meta.get("is_chunked_manifest", False))
        except Exception:
            return False

    def get_size(self, content_hash: str) -> int:
        """Get original file size from manifest metadata."""
        return int(self._backend._read_meta(content_hash).get("size", 0))

    # === Delete ===

    def delete_chunked(self, content_hash: str, context: OperationContext | None = None) -> None:
        """Delete chunked content, handling chunk reference counts."""
        b = self._backend

        # Read manifest BEFORE release — release may delete the blob
        key = b._blob_key(content_hash)
        manifest_data, _ = b._transport.get_blob(key)
        manifest = ChunkedReference.from_json(manifest_data)

        deleted = self._release_blob(content_hash)
        if not deleted:
            logger.debug(f"Decremented manifest {content_hash[:16]}... ref_count")
            return

        # Last reference — parallelize chunk releases
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(self._release_blob, ci.chunk_hash) for ci in manifest.chunks]
            for future in as_completed(futures):
                future.result()

        logger.info(
            f"Deleted chunked content {content_hash[:16]}... "
            f"({manifest.chunk_count} chunks unreferenced)"
        )

    def _release_blob(self, content_hash: str) -> bool:
        """Decrement ref_count; delete blob+meta at zero. Returns True if deleted."""
        b = self._backend
        key = b._blob_key(content_hash)

        def _do_release() -> bool:
            meta = b._read_meta(content_hash)
            if meta.get("ref_count", 1) <= 1:
                with contextlib.suppress(Exception):
                    b._transport.delete_blob(key)
                with contextlib.suppress(Exception):
                    b._transport.delete_blob(b._meta_key(content_hash))
                return True
            meta["ref_count"] = meta["ref_count"] - 1
            b._write_meta(content_hash, meta)
            return False

        if b._stripe_lock is not None:
            lock = b._stripe_lock.acquire_for(content_hash)
            with lock:
                return _do_release()
        return _do_release()

    # === Chunking algorithms ===

    def _chunk_content(self, content: bytes) -> list[tuple[int, int, bytes]]:
        try:
            from fastcdc import fastcdc
        except ImportError:
            logger.warning("fastcdc not installed, falling back to fixed-size chunking")
            return self._chunk_fixed(content)

        chunks = []
        for chunk in fastcdc(
            data=content,
            min_size=self.min_chunk,
            avg_size=self.avg_chunk,
            max_size=self.max_chunk,
        ):
            chunk_bytes = content[chunk.offset : chunk.offset + chunk.length]
            chunks.append((chunk.offset, chunk.length, chunk_bytes))
        return chunks

    def _chunk_fixed(self, content: bytes) -> list[tuple[int, int, bytes]]:
        chunks = []
        offset = 0
        while offset < len(content):
            length = min(self.avg_chunk, len(content) - offset)
            chunk_bytes = content[offset : offset + length]
            chunks.append((offset, length, chunk_bytes))
            offset += length
        return chunks
