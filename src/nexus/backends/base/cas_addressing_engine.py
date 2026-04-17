"""CAS addressing engine over any Transport.

CASAddressingEngine implements ObjectStoreABC (via Backend) using content-addressable
storage semantics: content is stored by hash, automatically deduplicated.

    CASAddressingEngine(transport: Transport)
        ├── CASGCSBackend   — thin: creates GCSTransport, registered as "cas_gcs"
        ├── CASLocalBackend  — thin: creates LocalTransport + features
        └── (future S3CAS)  — thin: creates S3Transport

The transport is INTERNAL — callers never see Transport.  They see Backend.
Thin subclasses exist for: registration, CONNECTION_ARGS, connector-specific
features (batch reads, signed URLs, versioning).

Feature DI (optional optimizations):
    content_cache — In-memory cache for read_content() hot path
    meta_cache    — LRU cache for _read_meta() hot path (e.g. cachetools.LRUCache)
    on_write_callback — Write notification (e.g. Zoekt reindex)
    cdc_engine    — ChunkingStrategy for large file chunking (CDC)

Storage layout (in transport key-space):
    cas/<hash[0:2]>/<hash[2:4]>/<hash>       # Content blob
    cas/<hash[0:2]>/<hash[2:4]>/<hash>.meta   # JSON metadata sidecar (CDC only)
    dirs/<path>/                               # Directory marker

GC: Reachability-based. No ref_count — GC scans metastore for referenced etags,
sweeps CAS blobs, deletes unreferenced blobs older than grace period.

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, ClassVar

from nexus.backends.base.backend import Backend
from nexus.backends.base.transport import Transport
from nexus.contracts.backend_features import BackendFeature
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import create_hasher, hash_content
from nexus.core.object_store import WriteResult

# CAS-specific: SHA-256 hex pattern for content hash validation.
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_hash(content_hash: str) -> None:
    """Validate that a content hash is a well-formed SHA-256 hex string.

    CAS-specific — only used by CAS addressing engine and its subclasses.

    Args:
        content_hash: Value to validate.

    Raises:
        ValueError: If content_hash is not a 64-character lowercase hex string.
    """
    if not _HASH_PATTERN.match(content_hash):
        raise ValueError(
            f"Invalid SHA-256 content hash: {content_hash!r} "
            f"(expected 64-character lowercase hex string)"
        )


if TYPE_CHECKING:
    from nexus.backends.engines.cdc import ChunkingStrategy
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

CAS_ADDRESSING_BACKEND_FEATURES: frozenset[BackendFeature] = frozenset(
    {
        BackendFeature.CAS,
        BackendFeature.STREAMING,
        BackendFeature.BATCH_CONTENT,
    }
)
"""Common capabilities for CAS-based backends."""


class CASAddressingEngine(Backend):
    """CAS addressing over any Transport.  Full ObjectStoreABC implementation.

    Content is stored at ``cas/<h[:2]>/<h[2:4]>/<h>``.  CDC-chunked content
    has a JSON metadata sidecar at ``<path>.meta`` with chunk/manifest flags.
    Non-CDC content has no .meta sidecar.

    Directory markers live at ``dirs/<path>/``.

    GC uses reachability-based scan (metastore → referenced etags → sweep CAS).
    No ref_count — writes are idempotent direct writes.

    Attributes:
        _transport: The underlying Transport for raw I/O.
        _backend_name: Human-readable backend identifier.
    """

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = CAS_ADDRESSING_BACKEND_FEATURES

    def __init__(
        self,
        transport: Transport,
        *,
        backend_name: str | None = None,
        # Feature DI — optional optimizations, all None-safe
        content_cache: Any | None = None,
        meta_cache: Any | None = None,
        on_write_callback: Any | None = None,
        cdc_engine: "ChunkingStrategy | None" = None,
    ) -> None:
        super().__init__()
        self._transport = transport
        self._backend_name = backend_name or f"cas-{transport.transport_name}"
        # Feature DI: None means feature disabled (cloud backends pass nothing).
        # Bloom filter previously lived here; removed in R10f — Rust stat() is
        # fast enough (5-17μs) that the seeding cost of a Bloom filter doesn't
        # pay back. Tracked under #3799 if benchmarks later justify.
        self._cache = content_cache  # storage.content_cache.ContentCache
        self._meta_cache: Any | None = meta_cache  # cachetools.LRUCache
        self._meta_cache_hits = 0
        self._meta_cache_misses = 0
        self._on_write_callback = on_write_callback
        self._cdc: ChunkingStrategy | None = cdc_engine

    @property
    def name(self) -> str:
        return self._backend_name

    @property
    def cache_stats(self) -> dict[str, int]:
        """Return metadata cache hit/miss statistics."""
        size = len(self._meta_cache) if self._meta_cache is not None else 0
        maxsize = getattr(self._meta_cache, "maxsize", 0) if self._meta_cache is not None else 0
        return {
            "hits": self._meta_cache_hits,
            "misses": self._meta_cache_misses,
            "size": size,
            "maxsize": maxsize,
        }

    # === CAS Path Helpers ===

    @staticmethod
    def _blob_key(content_hash: str) -> str:
        """Convert content hash to CAS blob key."""
        return f"cas/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"

    @staticmethod
    def _meta_key(content_hash: str) -> str:
        """Convert content hash to CAS metadata sidecar key."""
        return f"cas/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.meta"

    def _read_meta(self, content_hash: str) -> dict[str, Any]:
        """Read metadata sidecar.  Returns default dict if not found.

        Used by CDC engine for chunk/manifest flags (is_chunk, is_chunked_manifest).
        Non-CDC content has no .meta file.
        Uses meta_cache (read-through) when injected via Feature DI.
        """
        # Feature DI: meta cache read-through
        if self._meta_cache is not None:
            cached: dict[str, Any] | None = self._meta_cache.get(content_hash)
            if cached is not None:
                self._meta_cache_hits += 1
                return cached

        self._meta_cache_misses += 1 if self._meta_cache is not None else 0

        key = self._meta_key(content_hash)
        try:
            data, _ = self._transport.fetch(key)
            meta: dict[str, Any] = json.loads(data)
        except (NexusFileNotFoundError, FileNotFoundError):
            meta = {"size": 0}
        except (json.JSONDecodeError, Exception) as e:
            raise BackendError(
                f"Failed to read CAS metadata: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

        # Populate cache on miss
        if self._meta_cache is not None:
            self._meta_cache[content_hash] = meta

        return meta

    def _write_meta(self, content_hash: str, meta: dict[str, Any]) -> None:
        """Write metadata sidecar (JSON).

        Only used by CDC engine for chunk/manifest flags.
        Non-CDC writes skip .meta entirely.

        Uses store_nosync when available — meta JSON is reconstructable
        from VFS metastore, so fsync + atomic rename is unnecessary overhead.
        """
        key = self._meta_key(content_hash)
        data = json.dumps(meta).encode()
        try:
            if hasattr(self._transport, "store_nosync"):
                self._transport.store_nosync(key, data)
            else:
                self._transport.store(key, data, "application/json")
        except Exception as e:
            raise BackendError(
                f"Failed to write CAS metadata: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

        # Feature DI: update meta cache after successful write
        if self._meta_cache is not None:
            self._meta_cache[content_hash] = meta

    # === Content Operations (ObjectStoreABC) ===

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        # Offset write: read-modify-write for partial content update (Issue #1395)
        if offset > 0 and content_id:
            return self._write_at_offset(content, content_id, offset, context)

        # Feature DI: CDC routing for large files
        if self._cdc is not None and self._cdc.should_chunk(content):
            content_hash = self._cdc.write_chunked(content, context)

            # Feature DI: content cache (bloom removed in R10f)
            if self._cache is not None:
                self._cache.put(content_hash, content)

            return WriteResult(content_id=content_hash, version=content_hash, size=len(content))

        content_hash = hash_content(content)
        key = self._blob_key(content_hash)

        try:
            # Dedup skip: if blob already exists, skip the content write.
            # CAS is idempotent by design — same content → same key.
            # One stat() (~17μs) is much cheaper than a full store (~760μs).
            is_new = not self._transport.exists(key)
            if is_new:
                # TTL routing (Issue #3405): if context has ttl_seconds,
                # route to a TTL-bucketed volume via store_ttl.
                ttl = getattr(context, "ttl_seconds", None) if context else None
                if ttl and ttl > 0 and hasattr(self._transport, "store_ttl"):
                    self._transport.store_ttl(key, content, ttl)
                else:
                    self._transport.store(key, content)

            # No .meta for non-CDC content — ref_count eliminated.

            # Feature DI: Content cache (bloom removed in R10f)
            if self._cache is not None:
                self._cache.put(content_hash, content)

            # Feature DI: Write callback (e.g. Zoekt reindex)
            if is_new and self._on_write_callback is not None:
                self._on_write_callback(key)

            return WriteResult(content_id=content_hash, version=content_hash, size=len(content))

        except (BackendError, NexusFileNotFoundError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to write content: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

    def _write_at_offset(
        self,
        buf: bytes,
        old_content_id: str,
        offset: int,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """Partial write: splice ``buf`` at ``offset`` within existing content.

        CAS RMW: read old content, splice, write new content as whole-file.
        For CDC-chunked files, delegates to CDCEngine.write_chunked_partial()
        for chunk-level RMW (only affected chunks rewritten).

        Args:
            buf: Bytes to splice in.
            old_content_id: Content hash of the existing file.
            offset: Byte offset within the existing file.
            context: Operation context.

        Returns:
            WriteResult with new content_id, version, and total size.
        """
        # CDC + chunked → chunk-level partial write
        if self._cdc is not None and self._cdc.is_chunked(old_content_id):
            new_hash = self._cdc.write_chunked_partial(old_content_id, buf, offset, context)
            # Read manifest to get total size
            total_size = self._cdc.get_size(new_hash)
            return WriteResult(content_id=new_hash, version=new_hash, size=total_size)

        # Single blob → read old, splice, write new (offset=0)
        try:
            old_data = self.read_content(old_content_id, context=context)
        except Exception:
            old_data = b""

        # Zero-fill gap if offset > len(old_data)
        if offset > len(old_data):
            old_data = old_data + b"\x00" * (offset - len(old_data))

        new_data = old_data[:offset] + buf + old_data[offset + len(buf) :]
        return self.write_content(new_data, context=context)

    def read_content(self, content_id: str, context: "OperationContext | None" = None) -> bytes:
        content_hash = content_id  # CAS: content_id is a SHA-256 hash
        # Feature DI: cache hit → skip transport
        if self._cache is not None:
            cached: bytes | None = self._cache.get(content_hash)
            if cached is not None:
                return cached

        key = self._blob_key(content_hash)

        try:
            data, _ = self._transport.fetch(key)

            # CDC: check if blob IS a chunked manifest (read-then-check).
            # Avoids .meta stat on every non-CDC read — O(1) prefix check on
            # already-read data instead of extra filesystem I/O.
            if self._cdc is not None:
                from nexus.backends.engines.cdc import ChunkedReference

                if ChunkedReference.is_chunked_manifest(data):
                    chunked_content: bytes = self._cdc.read_chunked(content_hash, context)
                    if self._cache is not None:
                        self._cache.put(content_hash, chunked_content)
                    return chunked_content

            # Feature DI: cache on miss
            if self._cache is not None:
                self._cache.put(content_hash, data)

            return data

        except (NexusFileNotFoundError, BackendError, ValueError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to read content: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

    def delete_content(self, content_id: str, context: "OperationContext | None" = None) -> None:
        content_hash = content_id  # CAS: content_id is a SHA-256 hash
        # Feature DI: CDC chunked content
        if self._cdc is not None and self._cdc.is_chunked(content_hash):
            self._cdc.delete_chunked(content_hash, context)
            return

        key = self._blob_key(content_hash)

        try:
            if not self._transport.exists(key):
                raise NexusFileNotFoundError(content_hash)

            # Always physically delete blob + .meta unconditionally.
            # delete_content is never called by kernel (kernel does metadata-only
            # sys_unlink). GC is the safe cleanup path for shared content.
            self._transport.remove(key)
            meta_key = self._meta_key(content_hash)
            if self._transport.exists(meta_key):
                self._transport.remove(meta_key)
            # Feature DI: evict from meta cache
            if self._meta_cache is not None:
                self._meta_cache.pop(content_hash, None)

        except NexusFileNotFoundError:
            raise
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to delete content: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

    def content_exists(self, content_id: str, context: "OperationContext | None" = None) -> bool:
        content_hash = content_id  # CAS: content_id is a SHA-256 hash
        try:
            # Bloom pre-check removed in R10f — Rust stat() is fast enough that
            # the Bloom seeding cost doesn't pay back.
            key = self._blob_key(content_hash)
            return self._transport.exists(key)
        except Exception:
            return False

    def get_content_size(self, content_id: str, context: "OperationContext | None" = None) -> int:
        content_hash = content_id  # CAS: content_id is a SHA-256 hash
        # Feature DI: CDC chunked content
        if self._cdc is not None and self._cdc.is_chunked(content_hash):
            size: int = self._cdc.get_size(content_hash)
            return size

        key = self._blob_key(content_hash)

        try:
            return self._transport.get_size(key)
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to get content size: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

    def read_content_range(
        self,
        content_id: str,
        start: int,
        end: int,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """Read a byte range [start, end) from stored content.

        Optimised path when CDC is enabled:
        - Content cache hit: slice cached content.
        - Chunked content: delegate to CDCEngine.read_chunked_range().
        - Single blob: read full content, verify hash, then slice.
        """
        content_hash = content_id  # CAS: content_id is a SHA-256 hash
        # Feature DI: content cache hit → slice
        if self._cache is not None:
            cached: bytes | None = self._cache.get(content_hash)
            if cached is not None:
                return cached[start:end]

        # Feature DI: CDC chunked content → range-aware chunk read
        if self._cdc is not None and self._cdc.is_chunked(content_hash):
            range_data: bytes = self._cdc.read_chunked_range(content_hash, start, end, context)
            return range_data

        # Single blob: read, verify integrity, then slice
        content = self.read_content(content_id, context=context)
        return content[start:end]

    def stream_content(
        self,
        content_id: str,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> Iterator[bytes]:
        content_hash = content_id  # CAS: content_id is a SHA-256 hash
        key = self._blob_key(content_hash)
        try:
            yield from self._transport.stream(key, chunk_size)
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to stream content: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

    def write_stream(
        self,
        chunks: Iterator[bytes],
        content_id: str = "",
        *,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        import os
        import tempfile

        # Stream chunks to a temp file while computing hash incrementally.
        # This avoids buffering the entire content in memory (Issue #1625).
        hasher = create_hasher()
        total_size = 0

        # Write to a temp file in the transport root (same filesystem → fast rename)
        staging_dir = None
        if hasattr(self._transport, "_root"):
            staging_dir = self._transport._root

        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=staging_dir, delete=False) as tmp:
                tmp_path = tmp.name
                for chunk in chunks:
                    hasher.update(chunk)
                    tmp.write(chunk)
                    total_size += len(chunk)
                tmp.flush()
                if hasattr(self._transport, "_fsync") and self._transport._fsync:
                    os.fsync(tmp.fileno())
        except BaseException:
            # Cleanup temp file on error
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

        content_hash = hasher.hexdigest()
        key = self._blob_key(content_hash)

        try:
            # Move temp file to final blob location if transport supports it;
            # otherwise fall back to reading the temp file.
            if hasattr(self._transport, "store_from_path"):
                self._transport.store_from_path(key, tmp_path)
            else:
                try:
                    with open(tmp_path, "rb") as f:
                        data = f.read()
                    self._transport.store(key, data)
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_path)

            # No .meta for non-CDC streamed content — ref_count eliminated.
            # Bloom filter removed in R10f.

            # Feature DI: Write callback
            if self._on_write_callback is not None:
                self._on_write_callback(key)

            # Skip cache for streamed content (avoid loading into memory)

            return WriteResult(content_id=content_hash, version=content_hash, size=total_size)

        except (BackendError, NexusFileNotFoundError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to write stream: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

    def batch_read_content(
        self,
        content_ids: list[str],
        context: "OperationContext | None" = None,
        *,
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, bytes | None]:
        content_hashes = content_ids  # CAS alias
        if not content_hashes:
            return {}

        if len(content_hashes) == 1:
            try:
                data = self.read_content(content_hashes[0], context=context)
            except Exception:
                data = None
            return {content_hashes[0]: data}

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _read_one(h: str) -> tuple[str, bytes | None]:
            try:
                ctx = contexts.get(h, context) if contexts else context
                return (h, self.read_content(h, context=ctx))
            except Exception:
                return (h, None)

        max_workers = min(10, len(content_hashes))
        result: dict[str, bytes | None] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_read_one, h): h for h in content_hashes}
            for future in as_completed(futures):
                hash_key, content = future.result()
                result[hash_key] = content

        return result

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        path = path.strip("/")
        if not path:
            return  # Root always exists

        dir_key = f"dirs/{path}/"

        try:
            if self._transport.exists(dir_key):
                if not exist_ok:
                    raise FileExistsError(f"Directory already exists: {path}")
                return

            if not parents:
                parent = "/".join(path.split("/")[:-1])
                if parent and not self.is_directory(parent):
                    raise FileNotFoundError(f"Parent directory not found: {parent}")

            self._transport.create_dir(dir_key)

        except (FileExistsError, FileNotFoundError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to create directory: {e}",
                backend=self.name,
                path=path,
            ) from e

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        path = path.strip("/")
        if not path:
            raise BackendError(
                "Cannot remove root directory",
                backend=self.name,
                path="/",
            )

        dir_key = f"dirs/{path}/"

        try:
            if not self._transport.exists(dir_key):
                raise NexusFileNotFoundError(path)

            if not recursive:
                blobs, _ = self._transport.list_keys(prefix=dir_key, delimiter="/")
                if len(blobs) > 1:
                    raise OSError(f"Directory not empty: {path}")

            self._transport.remove(dir_key)

            if recursive:
                blobs, _ = self._transport.list_keys(prefix=dir_key, delimiter="")
                for blob_key in blobs:
                    if blob_key != dir_key:
                        try:
                            self._transport.remove(blob_key)
                        except Exception as e:
                            logger.debug("Failed to delete blob during recursive rmdir: %s", e)

        except (NexusFileNotFoundError, OSError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to remove directory: {e}",
                backend=self.name,
                path=path,
            ) from e

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        try:
            path = path.strip("/")
            if not path:
                return True

            dir_key = f"dirs/{path}/"
            return self._transport.exists(dir_key)

        except Exception:
            return False

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        try:
            path = path.strip("/")

            if path and not self.is_directory(path):
                raise FileNotFoundError(f"Directory not found: {path}")

            prefix = f"dirs/{path}/" if path else "dirs/"
            blobs, prefixes = self._transport.list_keys(prefix=prefix, delimiter="/")

            entries: set[str] = set()

            for blob_key in blobs:
                name = blob_key[len(prefix) :]
                if name:
                    entries.add(name.rstrip("/"))

            for prefix_path in prefixes:
                name = prefix_path[len(prefix) :].rstrip("/")
                if name:
                    entries.add(name + "/")

            return sorted(entries)

        except (FileNotFoundError, NotADirectoryError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to list directory: {e}",
                backend=self.name,
                path=path,
            ) from e
