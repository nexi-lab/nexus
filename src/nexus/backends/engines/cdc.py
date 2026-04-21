"""Content-Defined Chunking (CDC) for large files (Issue #1074).

CDCEngine provides chunking for any CASAddressingEngine subclass via Feature DI:

    class CASAddressingEngine(Backend):
        def __init__(self, transport, ..., cdc_engine=None):
            self._cdc = cdc_engine  # Optional, None-safe

CDC routing is handled by CASAddressingEngine base class — subclasses do NOT
need to override write_content/read_content for CDC.

ChunkingStrategy protocol allows pluggable chunking algorithms:
    - FastCDCStrategy (default): Rabin fingerprint content-defined chunking
    - Custom strategies: message-boundary chunking for LLM conversations, etc.

Storage structure:
    cas/
    ├── ab/cd/
    │   ├── abcd1234...         # Single-blob OR chunk content
    │   ├── abcd1234...meta     # Metadata: {"is_chunk": true}
    │   ├── 5678efgh...         # Chunked manifest (JSON)
    │   └── 5678efgh...meta     # {"is_chunked_manifest": true, "chunk_count": N}
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from nexus.core.hash_fast import hash_content

if TYPE_CHECKING:
    from nexus.backends.base.cas_addressing_engine import CASAddressingEngine
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# CDC Configuration Constants
CDC_THRESHOLD_BYTES = 16 * 1024 * 1024  # 16MB
CDC_MIN_CHUNK_SIZE = 256 * 1024  # 256KB
CDC_AVG_CHUNK_SIZE = 1 * 1024 * 1024  # 1MB
CDC_MAX_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB
CDC_PARALLEL_WORKERS = 8


# =============================================================================
# ChunkingStrategy Protocol
# =============================================================================


@runtime_checkable
class ChunkingStrategy(Protocol):
    """Pluggable chunking strategy for CAS content.

    Allows custom chunking algorithms beyond fastcdc:
    - Default: ``CDCEngine`` (Rabin fingerprint, 16MB threshold)
    - Custom: message-boundary chunking for LLM conversations, etc.

    All methods receive the parent ``CASAddressingEngine`` instance for transport access.
    """

    def should_chunk(self, content: bytes) -> bool:
        """Return True if content should be stored as chunks."""
        ...

    def write_chunked(self, content: bytes, context: "OperationContext | None" = None) -> str:
        """Write content as chunks + manifest. Returns manifest hash."""
        ...

    def read_chunked(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        """Reassemble chunked content from manifest + chunks."""
        ...

    def read_chunked_range(
        self,
        content_hash: str,
        start: int,
        end: int,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """Read a byte range [start, end) from chunked content."""
        ...

    def is_chunked(self, content_hash: str) -> bool:
        """Check if content_hash refers to a chunked manifest."""
        ...

    def get_size(self, content_hash: str) -> int:
        """Get original file size from manifest metadata."""
        ...

    def delete_chunked(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        """Delete chunked content (manifest + all chunks)."""
        ...

    def write_chunked_partial(
        self,
        old_manifest_hash: str,
        buf: bytes,
        offset: int,
        context: "OperationContext | None" = None,
    ) -> str:
        """Partial write into chunked content. Returns new manifest hash."""
        ...


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
    """CDC chunking engine — composed into CASAddressingEngine subclasses.

    Uses CASAddressingEngine's internal methods directly:
    ``_transport``, ``_blob_key()``, ``_read_meta()``, ``_write_meta()``.
    """

    __slots__ = ("_backend", "threshold", "min_chunk", "avg_chunk", "max_chunk", "workers")

    def __init__(
        self,
        backend: CASAddressingEngine,
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
            dedup_count = 0
            for future in as_completed(futures):
                offset, length = futures[future]
                chunk_hash, was_deduped = future.result()
                if was_deduped:
                    dedup_count += 1
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
        b._transport.store(key, manifest_bytes)

        # Write .meta with manifest flags (for GC to identify manifests)
        manifest_meta: dict[str, Any] = {
            "size": len(content),
            "is_chunked_manifest": True,
            "chunk_count": len(chunk_infos),
        }
        b._write_meta(manifest_hash, manifest_meta)

        written = len(chunk_infos) - dedup_count
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "Wrote chunked: %d bytes -> %d chunks (%d written, %d deduped) in %.1fms",
            len(content),
            len(chunk_infos),
            written,
            dedup_count,
            elapsed_ms,
        )
        return manifest_hash

    def _write_single_chunk(self, chunk_bytes: bytes) -> tuple[str, bool]:
        """Write a single chunk to CAS. Returns (chunk_hash, was_deduped)."""
        b = self._backend
        chunk_hash = hash_content(chunk_bytes)
        key = b._blob_key(chunk_hash)

        # Skip store if chunk already exists in local CAS. Bloom membership
        # pre-check was dropped in R10f — a direct `_transport.exists()` is
        # fast enough on the hot path; the Bloom filter saved a single
        # redb lookup per chunk, not a noticeable fraction of throughput.
        deduped = b._transport.exists(key)

        if not deduped:
            b._transport.store(key, chunk_bytes)

        # Write .meta with is_chunk flag (for GC to identify chunks)
        meta: dict[str, Any] = {"size": len(chunk_bytes), "is_chunk": True}
        b._write_meta(chunk_hash, meta)

        return chunk_hash, deduped

    def write_chunked_partial(
        self,
        old_manifest_hash: str,
        buf: bytes,
        offset: int,
        context: OperationContext | None = None,
    ) -> str:
        """Partial write: splice ``buf`` at ``offset`` within chunked content.

        Only rewrites affected chunks. Unaffected chunks are reused
        (referenced by the new manifest).

        Returns new manifest hash.
        """
        b = self._backend

        # Read old manifest
        key = b._blob_key(old_manifest_hash)
        manifest_data, _ = b._transport.fetch(key)
        old_manifest = ChunkedReference.from_json(manifest_data)

        write_end = offset + len(buf)

        # Classify chunks: prefix (before write), affected (overlap), suffix (after write)
        prefix_chunks: list[ChunkInfo] = []
        affected_chunks: list[ChunkInfo] = []
        suffix_chunks: list[ChunkInfo] = []

        for ci in old_manifest.chunks:
            chunk_end = ci.offset + ci.length
            if chunk_end <= offset:
                prefix_chunks.append(ci)
            elif ci.offset >= write_end:
                suffix_chunks.append(ci)
            else:
                affected_chunks.append(ci)

        if not affected_chunks:
            # Write extends beyond all existing chunks — append scenario
            # Read nothing, just write the new data as new chunks
            affected_data = b"\x00" * max(0, offset - old_manifest.total_size) + buf
            new_chunk_tuples = self._chunk_content(affected_data)
            new_chunk_infos: list[ChunkInfo] = []
            base_offset = old_manifest.total_size if not suffix_chunks else offset
            for _co, _cl, chunk_bytes in new_chunk_tuples:
                chunk_hash, _ = self._write_single_chunk(chunk_bytes)
                new_chunk_infos.append(
                    ChunkInfo(chunk_hash=chunk_hash, offset=base_offset + _co, length=_cl)
                )
        else:
            # Read affected region, splice buf in, re-chunk
            first_affected = affected_chunks[0]
            last_affected = affected_chunks[-1]
            region_start = first_affected.offset
            region_end = last_affected.offset + last_affected.length

            # Extend region to include write that goes beyond
            region_end = max(region_end, write_end)

            # Read affected chunk data
            affected_data_parts: dict[int, bytes] = {}
            for ci in affected_chunks:
                chunk_data = self._read_single_chunk(ci.chunk_hash)
                affected_data_parts[ci.offset] = chunk_data

            # Assemble old data for the affected region
            assembled = b""
            for ci in affected_chunks:
                assembled += affected_data_parts[ci.offset]

            # Splice: replace [offset - region_start, offset - region_start + len(buf))
            splice_start = offset - region_start
            # Zero-fill if offset goes beyond assembled data
            if splice_start > len(assembled):
                assembled = assembled + b"\x00" * (splice_start - len(assembled))
            new_region = assembled[:splice_start] + buf + assembled[splice_start + len(buf) :]

            # Re-chunk the affected region
            new_chunk_tuples = self._chunk_content(new_region)
            new_chunk_infos = []
            for _co, _cl, chunk_bytes in new_chunk_tuples:
                chunk_hash, _ = self._write_single_chunk(chunk_bytes)
                new_chunk_infos.append(
                    ChunkInfo(chunk_hash=chunk_hash, offset=region_start + _co, length=_cl)
                )

        # Reused chunks need no ref_count update — GC uses reachability scan.

        # Build new manifest
        all_chunks = tuple(prefix_chunks) + tuple(new_chunk_infos) + tuple(suffix_chunks)
        total_size = max(
            (ci.offset + ci.length for ci in all_chunks),
            default=0,
        )

        new_manifest = ChunkedReference(
            total_size=total_size,
            chunk_count=len(all_chunks),
            avg_chunk_size=total_size // len(all_chunks) if all_chunks else 0,
            content_hash="",  # Skip full-file hash for partial writes
            chunks=all_chunks,
        )
        manifest_bytes = new_manifest.to_json()
        manifest_hash = hash_content(manifest_bytes)

        # Store manifest blob
        mkey = b._blob_key(manifest_hash)
        b._transport.store(mkey, manifest_bytes)

        # Write .meta with manifest flags
        manifest_meta: dict[str, Any] = {
            "size": total_size,
            "is_chunked_manifest": True,
            "chunk_count": len(all_chunks),
        }
        b._write_meta(manifest_hash, manifest_meta)

        logger.info(
            "Partial write: offset=%d len=%d -> %d chunks (%d reused) total_size=%d",
            offset,
            len(buf),
            len(all_chunks),
            len(prefix_chunks) + len(suffix_chunks),
            total_size,
        )
        return manifest_hash

    # === Read ===

    def read_chunked(self, content_hash: str, context: OperationContext | None = None) -> bytes:
        """Reassemble chunked content from manifest + chunks."""
        start_time = time.perf_counter()
        b = self._backend

        key = b._blob_key(content_hash)
        manifest_data, _ = b._transport.fetch(key)
        manifest = ChunkedReference.from_json(manifest_data)

        chunk_data: dict[int, bytes] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(self._read_and_verify_chunk, ci) for ci in manifest.chunks]
            for future in as_completed(futures):
                offset, data = future.result()
                chunk_data[offset] = data

        content = b"".join(chunk_data[o] for o in sorted(chunk_data.keys()))

        # Verify full-content hash when available.
        # Partial writes (write_chunked_partial) set content_hash="" — skip check,
        # relying on per-chunk hash verification instead (Issue #1395).
        if manifest.content_hash:
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

    def read_chunked_range(
        self,
        content_hash: str,
        start: int,
        end: int,
        context: OperationContext | None = None,
    ) -> bytes:
        """Read a byte range [start, end) from chunked content.

        Only fetches and verifies the overlapping chunks, not all chunks.
        """
        b = self._backend

        key = b._blob_key(content_hash)
        manifest_data, _ = b._transport.fetch(key)
        manifest = ChunkedReference.from_json(manifest_data)

        # Filter to overlapping chunks
        overlapping = [
            ci for ci in manifest.chunks if ci.offset < end and (ci.offset + ci.length) > start
        ]

        if not overlapping:
            return b""

        # Fetch and verify overlapping chunks in parallel
        chunk_data: dict[int, bytes] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(self._read_and_verify_chunk, ci) for ci in overlapping]
            for future in as_completed(futures):
                offset, data = future.result()
                chunk_data[offset] = data

        # Assemble overlapping chunks in order and slice to exact range
        assembled = b"".join(chunk_data[o] for o in sorted(chunk_data.keys()))

        # Calculate the byte offset of the first overlapping chunk
        first_chunk_offset = overlapping[0].offset
        slice_start = start - first_chunk_offset
        slice_end = end - first_chunk_offset

        return assembled[slice_start:slice_end]

    def _read_and_verify_chunk(self, chunk_info: ChunkInfo) -> tuple[int, bytes]:
        """Read a single chunk and verify its hash. Returns (offset, data)."""
        key = self._backend._blob_key(chunk_info.chunk_hash)
        data, _ = self._backend._transport.fetch(key)

        actual_hash = hash_content(data)
        if actual_hash != chunk_info.chunk_hash:
            raise ValueError(
                f"Chunk hash mismatch at offset {chunk_info.offset}: "
                f"expected {chunk_info.chunk_hash}, got {actual_hash}"
            )
        return (chunk_info.offset, data)

    def _read_single_chunk(self, chunk_hash: str) -> bytes:
        key = self._backend._blob_key(chunk_hash)
        data, _ = self._backend._transport.fetch(key)
        return data

    # === Query ===

    def is_chunked(self, content_hash: str) -> bool:
        """Check if content_hash refers to a chunked manifest.

        Fast path: non-CDC content has no .meta file, so exists (~5μs stat)
        short-circuits before the full fetch+json.loads path (~30μs).
        Meta cache also short-circuits on repeated checks.
        """
        b = self._backend
        # Meta cache hit → skip I/O entirely
        if b._meta_cache is not None:
            cached = b._meta_cache.get(content_hash)
            if cached is not None:
                return bool(cached.get("is_chunked_manifest", False))
        # No .meta file → definitely not chunked (cheap stat vs expensive fetch)
        meta_key = b._meta_key(content_hash)
        if not b._transport.exists(meta_key):
            # Cache the negative result to avoid repeated stat
            if b._meta_cache is not None:
                b._meta_cache[content_hash] = {"size": 0}
            return False
        try:
            meta = b._read_meta(content_hash)
            return bool(meta.get("is_chunked_manifest", False))
        except Exception:
            return False

    def get_size(self, content_hash: str) -> int:
        """Get original file size from manifest metadata."""
        return int(self._backend._read_meta(content_hash).get("size", 0))

    # === Delete ===

    def delete_chunked(self, content_hash: str, context: OperationContext | None = None) -> None:
        """Delete chunked content — unconditionally delete manifest + all chunks."""
        b = self._backend

        key = b._blob_key(content_hash)
        manifest_data, _ = b._transport.fetch(key)
        manifest = ChunkedReference.from_json(manifest_data)

        # Delete manifest blob + meta
        with contextlib.suppress(Exception):
            b._transport.remove(key)
        with contextlib.suppress(Exception):
            b._transport.remove(b._meta_key(content_hash))
        if b._meta_cache is not None:
            b._meta_cache.pop(content_hash, None)

        # Delete all chunks in parallel
        def _delete_chunk(ci: ChunkInfo) -> None:
            with contextlib.suppress(Exception):
                b._transport.remove(b._blob_key(ci.chunk_hash))
            with contextlib.suppress(Exception):
                b._transport.remove(b._meta_key(ci.chunk_hash))
            if b._meta_cache is not None:
                b._meta_cache.pop(ci.chunk_hash, None)

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(_delete_chunk, ci) for ci in manifest.chunks]
            for future in as_completed(futures):
                future.result()

        logger.info(
            "Deleted chunked content %s... (%d chunks)",
            content_hash[:16],
            manifest.chunk_count,
        )

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
