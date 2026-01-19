"""Content-Defined Chunking (CDC) storage for large files (Issue #1074).

Provides chunked storage for large files (>=16MB) using FastCDC algorithm,
enabling efficient deduplication across file versions and parallel I/O.

Features:
- FastCDC content-defined chunking for optimal deduplication
- Parallel chunk writes/reads using ThreadPoolExecutor
- Per-chunk reference counting for safe deletion
- Backward compatible with single-blob storage
- Transparent to callers (same API)

Architecture:
    Small files (<16MB): Single blob in CAS (unchanged)
    Large files (>=16MB): Split into ~1MB chunks using CDC
        - Each chunk stored independently in CAS with ref_count
        - Manifest JSON stored at "content hash" location
        - Manifest tracks chunk hashes and offsets

Storage structure:
    cas/
    ├── ab/cd/
    │   ├── abcd1234...         # Single-blob OR chunk content
    │   ├── abcd1234...meta     # Metadata: {"ref_count": N, "is_chunk": true}
    │   │
    │   ├── 5678efgh...         # Chunked manifest (JSON)
    │   └── 5678efgh...meta     # {"ref_count": N, "is_chunked_manifest": true}

Example:
    >>> backend = LocalBackend("/data", cdc_threshold=16*1024*1024)
    >>> # Large file automatically chunked
    >>> content_hash = backend.write_content(large_50mb_file).unwrap()
    >>> # Read transparently reassembles chunks
    >>> content = backend.read_content(content_hash).unwrap()
    >>> assert content == large_50mb_file
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from nexus.core.hash_fast import hash_content

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)

# =============================================================================
# CDC Configuration Constants
# =============================================================================

# Files below this threshold use single-blob storage (default: 16MB)
CDC_THRESHOLD_BYTES = 16 * 1024 * 1024

# FastCDC chunk size parameters
CDC_MIN_CHUNK_SIZE = 256 * 1024  # 256KB minimum
CDC_AVG_CHUNK_SIZE = 1 * 1024 * 1024  # 1MB average target
CDC_MAX_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB maximum

# Parallel I/O configuration
CDC_PARALLEL_WORKERS = 8  # Optimal for SSD, reduce for HDD


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ChunkInfo:
    """Information about a single chunk in a chunked file.

    Attributes:
        chunk_hash: BLAKE3 hash of the chunk content
        offset: Byte offset in the original file
        length: Size of the chunk in bytes
    """

    chunk_hash: str
    offset: int
    length: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "chunk_hash": self.chunk_hash,
            "offset": self.offset,
            "length": self.length,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChunkInfo:
        """Deserialize from JSON dict."""
        return cls(
            chunk_hash=data["chunk_hash"],
            offset=data["offset"],
            length=data["length"],
        )


@dataclass
class ChunkedReference:
    """Manifest for a file stored as CDC chunks.

    This manifest is stored as JSON in the CAS at the location where
    a single-blob file would normally be stored. The "type" field
    distinguishes it from raw content.

    Attributes:
        type: Always "chunked_manifest_v1" to identify this as a manifest
        total_size: Original file size in bytes
        chunk_count: Number of chunks
        avg_chunk_size: Actual average chunk size (for stats)
        content_hash: BLAKE3 hash of the full original content (for verification)
        chunks: List of ChunkInfo describing each chunk

    Example JSON:
        {
            "type": "chunked_manifest_v1",
            "total_size": 52428800,
            "chunk_count": 50,
            "avg_chunk_size": 1048576,
            "content_hash": "abc123...",
            "chunks": [
                {"chunk_hash": "def456...", "offset": 0, "length": 1024000},
                {"chunk_hash": "ghi789...", "offset": 1024000, "length": 1080000},
                ...
            ]
        }
    """

    type: Literal["chunked_manifest_v1"] = "chunked_manifest_v1"
    total_size: int = 0
    chunk_count: int = 0
    avg_chunk_size: int = 0
    content_hash: str = ""
    chunks: list[ChunkInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
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
        """Deserialize from JSON dict."""
        return cls(
            type=data.get("type", "chunked_manifest_v1"),
            total_size=data["total_size"],
            chunk_count=data["chunk_count"],
            avg_chunk_size=data.get("avg_chunk_size", 0),
            content_hash=data["content_hash"],
            chunks=[ChunkInfo.from_dict(c) for c in data["chunks"]],
        )

    def to_json(self) -> bytes:
        """Serialize to JSON bytes."""
        return json.dumps(self.to_dict(), separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> ChunkedReference:
        """Deserialize from JSON bytes."""
        return cls.from_dict(json.loads(data))

    @staticmethod
    def is_chunked_manifest(data: bytes) -> bool:
        """Check if content is a chunked manifest (not raw content).

        Uses a heuristic: manifests are small JSON with specific type field.

        Args:
            data: Content bytes to check

        Returns:
            True if this appears to be a chunked manifest
        """
        # Manifests are relatively small JSON (<100KB typically)
        if len(data) > 500 * 1024:  # 500KB max manifest size
            return False

        try:
            parsed = json.loads(data)
            return isinstance(parsed, dict) and parsed.get("type") == "chunked_manifest_v1"
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False


# =============================================================================
# ChunkedStorageMixin
# =============================================================================


class ChunkedStorageMixin:
    """Mixin that adds CDC chunking support to storage backends.

    This mixin provides content-defined chunking for large files,
    enabling efficient deduplication across similar files (e.g.,
    different versions of the same document).

    Features:
        - FastCDC algorithm for content-defined boundaries
        - Parallel chunk writes for better throughput
        - Transparent to callers (same API as single-blob storage)
        - Backward compatible (reads old single-blob files)
        - Per-chunk reference counting

    Usage:
        class LocalBackend(Backend, ChunkedStorageMixin):
            def write_content(self, content, context=None):
                if self._should_chunk(content):
                    return self._write_chunked(content, context)
                return self._write_single_blob(content, context)

    Requirements:
        Backend must implement:
        - _hash_to_path(hash) -> Path
        - _read_metadata(hash) -> dict
        - _write_metadata(hash, metadata)
        - _lock_file(path) -> context manager
        - _get_lock_path(hash) -> Path
        - _get_meta_path(hash) -> Path
        - cas_root: Path (attribute)
        - _cas_bloom_add(hash) (optional)
        - content_cache (optional attribute)
    """

    # Configuration (can be overridden in subclass or __init__)
    cdc_threshold: int = CDC_THRESHOLD_BYTES

    # Abstract methods that must be implemented by the backend
    # These stubs satisfy mypy and define the expected interface

    @abstractmethod
    def _hash_to_path(self, content_hash: str) -> Path:
        """Convert content hash to CAS file path."""
        raise NotImplementedError

    @abstractmethod
    def _get_lock_path(self, content_hash: str) -> Path:
        """Get lock file path for a content hash."""
        raise NotImplementedError

    @abstractmethod
    def _get_meta_path(self, content_hash: str) -> Path:
        """Get metadata file path for a content hash."""
        raise NotImplementedError

    @abstractmethod
    def _read_metadata(self, content_hash: str) -> dict[str, Any]:
        """Read metadata for content hash."""
        raise NotImplementedError

    @abstractmethod
    def _write_metadata(self, content_hash: str, metadata: dict[str, Any]) -> None:
        """Write metadata for content hash."""
        raise NotImplementedError

    @abstractmethod
    def _lock_file(self, lock_path: Path) -> Any:
        """Context manager for file locking. Returns a context manager."""
        raise NotImplementedError

    def _cas_bloom_add(self, content_hash: str) -> None:
        """Add hash to Bloom filter. Optional - override in subclass if available."""
        pass  # Default no-op, override in backends with Bloom filter

    cdc_min_chunk: int = CDC_MIN_CHUNK_SIZE
    cdc_avg_chunk: int = CDC_AVG_CHUNK_SIZE
    cdc_max_chunk: int = CDC_MAX_CHUNK_SIZE
    cdc_workers: int = CDC_PARALLEL_WORKERS

    def _should_chunk(self, content: bytes) -> bool:
        """Determine if content should be stored as chunks.

        Args:
            content: File content bytes

        Returns:
            True if content size >= cdc_threshold
        """
        return len(content) >= self.cdc_threshold

    def _chunk_content_cdc(self, content: bytes) -> list[tuple[int, int, bytes]]:
        """Split content into CDC chunks using FastCDC.

        Uses content-defined boundaries for better deduplication
        across similar files (e.g., file versions with insertions).

        Args:
            content: Raw file content

        Returns:
            List of (offset, length, chunk_bytes) tuples
        """
        try:
            from fastcdc import fastcdc
        except ImportError:
            logger.warning(
                "fastcdc not installed, falling back to fixed-size chunking. "
                "Install with: pip install fastcdc"
            )
            return self._chunk_content_fixed(content)

        chunks = []
        for chunk in fastcdc(
            data=content,
            min_size=self.cdc_min_chunk,
            avg_size=self.cdc_avg_chunk,
            max_size=self.cdc_max_chunk,
        ):
            chunk_bytes = content[chunk.offset : chunk.offset + chunk.length]
            chunks.append((chunk.offset, chunk.length, chunk_bytes))

        return chunks

    def _chunk_content_fixed(self, content: bytes) -> list[tuple[int, int, bytes]]:
        """Fallback fixed-size chunking when FastCDC unavailable.

        Args:
            content: Raw file content

        Returns:
            List of (offset, length, chunk_bytes) tuples
        """
        chunks = []
        offset = 0
        while offset < len(content):
            length = min(self.cdc_avg_chunk, len(content) - offset)
            chunk_bytes = content[offset : offset + length]
            chunks.append((offset, length, chunk_bytes))
            offset += length
        return chunks

    def _write_single_chunk(self, chunk_bytes: bytes) -> str:
        """Write a single chunk to CAS, returning its hash.

        Handles deduplication - if chunk already exists,
        just increments ref_count.

        Args:
            chunk_bytes: Chunk content

        Returns:
            BLAKE3 hash of the chunk
        """
        chunk_hash = hash_content(chunk_bytes)
        chunk_path: Path = self._hash_to_path(chunk_hash)
        lock_path: Path = self._get_lock_path(chunk_hash)

        # Ensure directories exist
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock_file(lock_path):
            if chunk_path.exists():
                # Chunk exists - increment ref_count (deduplication!)
                metadata = self._read_metadata(chunk_hash)
                metadata["ref_count"] = metadata.get("ref_count", 0) + 1
                self._write_metadata(chunk_hash, metadata)
                logger.debug(
                    f"Chunk {chunk_hash[:16]}... exists, ref_count={metadata['ref_count']}"
                )
            else:
                # Write new chunk atomically
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=chunk_path.parent, delete=False
                ) as tmp:
                    tmp_path = Path(tmp.name)
                    tmp.write(chunk_bytes)
                    tmp.flush()
                    os.fsync(tmp.fileno())

                os.replace(str(tmp_path), str(chunk_path))

                # Create metadata
                self._write_metadata(
                    chunk_hash,
                    {
                        "ref_count": 1,
                        "size": len(chunk_bytes),
                        "is_chunk": True,
                    },
                )

                # Update Bloom filter if available
                if hasattr(self, "_cas_bloom_add"):
                    self._cas_bloom_add(chunk_hash)

                logger.debug(f"Wrote new chunk {chunk_hash[:16]}... ({len(chunk_bytes)} bytes)")

        return chunk_hash

    def _write_chunked(
        self,
        content: bytes,
        context: OperationContext | None = None,
    ) -> str:
        """Write content as CDC chunks with manifest.

        Steps:
        1. Compute full content hash (for verification)
        2. Split content into CDC chunks
        3. Write chunks in parallel
        4. Create and store manifest
        5. Return manifest hash (used as content_hash)

        Args:
            content: Full file content
            context: Operation context (unused for local backend)

        Returns:
            Manifest hash (functions as the content_hash)
        """
        start_time = time.perf_counter()

        # Compute hash of full content for verification on read
        full_content_hash = hash_content(content)

        # Split into chunks using CDC
        chunk_tuples = self._chunk_content_cdc(content)

        logger.info(
            f"Chunking {len(content)} bytes -> {len(chunk_tuples)} chunks "
            f"(avg {len(content) // len(chunk_tuples) if chunk_tuples else 0} bytes)"
        )

        # Write chunks in parallel for better throughput
        chunk_infos: list[ChunkInfo] = []

        with ThreadPoolExecutor(max_workers=self.cdc_workers) as executor:
            # Submit all chunk writes
            futures: dict[Any, tuple[int, int]] = {}
            for offset, length, chunk_bytes in chunk_tuples:
                future = executor.submit(self._write_single_chunk, chunk_bytes)
                futures[future] = (offset, length)

            # Collect results (may complete out of order)
            results: dict[int, ChunkInfo] = {}
            for future in as_completed(futures):
                offset, length = futures[future]
                chunk_hash = future.result()
                results[offset] = ChunkInfo(
                    chunk_hash=chunk_hash,
                    offset=offset,
                    length=length,
                )

        # Sort by offset to maintain order in manifest
        for offset in sorted(results.keys()):
            chunk_infos.append(results[offset])

        # Create manifest
        manifest = ChunkedReference(
            total_size=len(content),
            chunk_count=len(chunk_infos),
            avg_chunk_size=len(content) // len(chunk_infos) if chunk_infos else 0,
            content_hash=full_content_hash,
            chunks=chunk_infos,
        )

        # Store manifest as JSON
        manifest_bytes = manifest.to_json()
        manifest_hash = hash_content(manifest_bytes)

        manifest_path: Path = self._hash_to_path(manifest_hash)
        lock_path: Path = self._get_lock_path(manifest_hash)

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock_file(lock_path):
            if manifest_path.exists():
                # Same exact chunking already stored (rare but possible)
                # Just increment manifest ref_count
                metadata = self._read_metadata(manifest_hash)
                metadata["ref_count"] = metadata.get("ref_count", 0) + 1
                self._write_metadata(manifest_hash, metadata)
            else:
                # Write manifest atomically
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=manifest_path.parent, delete=False
                ) as tmp:
                    tmp_path = Path(tmp.name)
                    tmp.write(manifest_bytes)
                    tmp.flush()
                    os.fsync(tmp.fileno())

                os.replace(str(tmp_path), str(manifest_path))

                # Create manifest metadata
                self._write_metadata(
                    manifest_hash,
                    {
                        "ref_count": 1,
                        "size": len(content),  # Store ORIGINAL size, not manifest size
                        "is_chunked_manifest": True,
                        "chunk_count": len(chunk_infos),
                    },
                )

                if hasattr(self, "_cas_bloom_add"):
                    self._cas_bloom_add(manifest_hash)

        # Add full content to cache if available
        if hasattr(self, "content_cache") and self.content_cache is not None:
            self.content_cache.put(manifest_hash, content)

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            f"Wrote chunked content: {len(content)} bytes -> {len(chunk_infos)} chunks "
            f"in {elapsed_ms:.1f}ms (manifest={manifest_hash[:16]}...)"
        )

        return manifest_hash

    def _read_single_chunk(self, chunk_hash: str) -> bytes:
        """Read a single chunk from CAS.

        Args:
            chunk_hash: BLAKE3 hash of the chunk

        Returns:
            Chunk content bytes
        """
        chunk_path: Path = self._hash_to_path(chunk_hash)
        return chunk_path.read_bytes()

    def _read_chunked(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> bytes:
        """Read chunked content by reassembling from chunks.

        Args:
            content_hash: Hash of the manifest
            context: Operation context (unused)

        Returns:
            Reassembled file content

        Raises:
            ValueError: If content hash verification fails
        """
        start_time = time.perf_counter()

        # Read manifest
        manifest_path: Path = self._hash_to_path(content_hash)
        manifest = ChunkedReference.from_json(manifest_path.read_bytes())

        # Read chunks in parallel
        chunk_data: dict[int, bytes] = {}

        with ThreadPoolExecutor(max_workers=self.cdc_workers) as executor:
            futures: dict[Any, int] = {}
            for chunk_info in manifest.chunks:
                future = executor.submit(self._read_single_chunk, chunk_info.chunk_hash)
                futures[future] = chunk_info.offset

            for future in as_completed(futures):
                offset = futures[future]
                chunk_data[offset] = future.result()

        # Reassemble in order
        content = b"".join(chunk_data[offset] for offset in sorted(chunk_data.keys()))

        # Verify hash
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

    def _is_chunked_content(self, content_hash: str) -> bool:
        """Check if content_hash refers to a chunked manifest.

        Uses metadata flag for efficiency (avoids reading content).

        Args:
            content_hash: Content hash to check

        Returns:
            True if this is a chunked manifest
        """
        try:
            metadata = self._read_metadata(content_hash)
            return bool(metadata.get("is_chunked_manifest", False))
        except Exception:
            return False

    def _delete_chunk_ref(self, chunk_hash: str) -> None:
        """Decrement chunk ref_count, delete if zero.

        Args:
            chunk_hash: Hash of chunk to unreference
        """
        lock_path: Path = self._get_lock_path(chunk_hash)

        with self._lock_file(lock_path):
            metadata = self._read_metadata(chunk_hash)
            ref_count = metadata.get("ref_count", 1)

            if ref_count <= 1:
                # Delete chunk
                chunk_path: Path = self._hash_to_path(chunk_hash)
                chunk_path.unlink(missing_ok=True)
                self._get_meta_path(chunk_hash).unlink(missing_ok=True)
                logger.debug(f"Deleted chunk {chunk_hash[:16]}... (ref_count=0)")
            else:
                # Decrement ref_count
                metadata["ref_count"] = ref_count - 1
                self._write_metadata(chunk_hash, metadata)
                logger.debug(f"Decremented chunk {chunk_hash[:16]}... ref_count to {ref_count - 1}")

    def _delete_chunked(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> None:
        """Delete chunked content, handling chunk reference counts.

        Decrements ref_count on manifest. If it reaches zero:
        1. Read manifest to get chunk list
        2. Decrement ref_count on each chunk
        3. Delete chunks with ref_count=0
        4. Delete manifest

        Args:
            content_hash: Hash of the manifest
            context: Operation context (unused)
        """
        manifest_path: Path = self._hash_to_path(content_hash)
        lock_path: Path = self._get_lock_path(content_hash)

        with self._lock_file(lock_path):
            metadata = self._read_metadata(content_hash)
            ref_count = metadata.get("ref_count", 1)

            if ref_count > 1:
                # Other references exist - just decrement
                metadata["ref_count"] = ref_count - 1
                self._write_metadata(content_hash, metadata)
                logger.debug(
                    f"Decremented manifest {content_hash[:16]}... ref_count to {ref_count - 1}"
                )
                return

            # Last reference - delete manifest and unreference all chunks
            manifest = ChunkedReference.from_json(manifest_path.read_bytes())

            # Decrement ref_count on each chunk
            for chunk_info in manifest.chunks:
                self._delete_chunk_ref(chunk_info.chunk_hash)

            # Delete manifest
            manifest_path.unlink(missing_ok=True)
            self._get_meta_path(content_hash).unlink(missing_ok=True)

            logger.info(
                f"Deleted chunked content {content_hash[:16]}... "
                f"({manifest.chunk_count} chunks unreferenced)"
            )

        # Clean up lock file
        lock_path.unlink(missing_ok=True)

    def _get_content_size_chunked(self, content_hash: str) -> int:
        """Get the original file size from chunked manifest metadata.

        Args:
            content_hash: Manifest hash

        Returns:
            Original file size in bytes
        """
        metadata = self._read_metadata(content_hash)
        # We store the original size in metadata, not the manifest size
        return int(metadata.get("size", 0))
