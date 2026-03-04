"""CAS addressing engine over any BlobTransport.

CASBackend implements ObjectStoreABC (via Backend) using content-addressable
storage semantics: content is stored by hash, automatically deduplicated,
and reference-counted.

    CASBackend(transport: BlobTransport)
        ├── GCSBackend      — thin: creates GCSBlobTransport, registered as "gcs"
        ├── LocalCASBackend  — thin: creates LocalBlobTransport + features
        └── (future S3CAS)  — thin: creates S3BlobTransport

The transport is INTERNAL — callers never see BlobTransport.  They see Backend.
Thin subclasses exist for: registration, CONNECTION_ARGS, connector-specific
features (batch reads, signed URLs, versioning).

Feature DI (optional, local-only optimizations):
    bloom_filter  — Bloom pre-check for fast content_exists() miss
    content_cache — In-memory cache for read_content() hot path
    stripe_lock   — Per-hash threading.Lock for metadata read-modify-write
    on_write_callback — Write notification (e.g. Zoekt reindex)

Storage layout (in transport key-space):
    cas/<hash[0:2]>/<hash[2:4]>/<hash>       # Content blob
    cas/<hash[0:2]>/<hash[2:4]>/<hash>.meta   # JSON metadata sidecar
    dirs/<path>/                               # Directory marker

References:
    - Issue #1323: CAS x Backend orthogonal composition
    - backends/cas_blob_store.py — local CAS engine (_StripeLock source)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, ClassVar

from nexus.backends.base.backend import Backend
from nexus.backends.base.blob_transport import BlobTransport
from nexus.contracts.capabilities import ConnectorCapability
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

CAS_BACKEND_CAPABILITIES: frozenset[ConnectorCapability] = frozenset(
    {
        ConnectorCapability.CAS,
        ConnectorCapability.STREAMING,
        ConnectorCapability.BATCH_CONTENT,
    }
)
"""Common capabilities for CAS-based backends."""


class CASBackend(Backend):
    """CAS addressing over any BlobTransport.  Full ObjectStoreABC implementation.

    Content is stored at ``cas/<h[:2]>/<h[2:4]>/<h>`` with a JSON metadata
    sidecar at ``<path>.meta`` tracking ``ref_count`` and ``size``.

    Directory markers live at ``dirs/<path>/``.

    Attributes:
        _transport: The underlying BlobTransport for raw I/O.
        _backend_name: Human-readable backend identifier.
    """

    _CAPABILITIES: ClassVar[frozenset[ConnectorCapability]] = CAS_BACKEND_CAPABILITIES

    def __init__(
        self,
        transport: BlobTransport,
        *,
        backend_name: str | None = None,
        # Feature DI — local-only optimizations, all None-safe
        bloom_filter: Any | None = None,
        content_cache: Any | None = None,
        stripe_lock: Any | None = None,
        on_write_callback: Any | None = None,
    ) -> None:
        self._transport = transport
        self._backend_name = backend_name or f"cas-{transport.transport_name}"
        # Feature DI: None means feature disabled (cloud backends pass nothing)
        self._bloom = bloom_filter  # nexus_fast.BloomFilter
        self._cache = content_cache  # storage.content_cache.ContentCache
        self._stripe_lock = stripe_lock  # cas_blob_store._StripeLock
        self._on_write_callback = on_write_callback

    @property
    def name(self) -> str:
        return self._backend_name

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

        When stripe_lock is injected, caller must hold the lock before calling.
        """
        key = self._meta_key(content_hash)
        try:
            data, _ = self._transport.get_blob(key)
            meta: dict[str, Any] = json.loads(data)
            return meta
        except (NexusFileNotFoundError, FileNotFoundError):
            return {"ref_count": 0, "size": 0}
        except (json.JSONDecodeError, Exception) as e:
            raise BackendError(
                f"Failed to read CAS metadata: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

    def _write_meta(self, content_hash: str, meta: dict[str, Any]) -> None:
        """Write metadata sidecar (JSON).

        When stripe_lock is injected, caller must hold the lock before calling.
        """
        key = self._meta_key(content_hash)
        try:
            self._transport.put_blob(key, json.dumps(meta).encode(), "application/json")
        except Exception as e:
            raise BackendError(
                f"Failed to write CAS metadata: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

    def _meta_update_locked(
        self,
        content_hash: str,
        updater: "Callable[[dict[str, Any]], dict[str, Any]]",
    ) -> dict[str, Any]:
        """Read-modify-write metadata under stripe lock (if available).

        Args:
            content_hash: Hash identifying the content.
            updater: Callable(meta_dict) -> meta_dict that modifies metadata.

        Returns:
            The updated metadata dict.
        """
        if self._stripe_lock is not None:
            lock = self._stripe_lock.acquire_for(content_hash)
            with lock:
                meta: dict[str, Any] = self._read_meta(content_hash)
                meta = updater(meta)
                self._write_meta(content_hash, meta)
                return meta
        else:
            meta = self._read_meta(content_hash)
            meta = updater(meta)
            self._write_meta(content_hash, meta)
            return meta

    # === Content Operations (ObjectStoreABC) ===

    def write_content(
        self, content: bytes, context: "OperationContext | None" = None
    ) -> WriteResult:
        content_hash = hash_content(content)
        key = self._blob_key(content_hash)

        try:
            # Blob write is idempotent (same content → same key), safe without lock
            self._transport.put_blob(key, content)

            # Metadata update: read-modify-write under stripe lock to avoid
            # TOCTOU race where multiple threads all see ref_count=0 and set 1.
            def _update_meta(meta: dict[str, Any]) -> dict[str, Any]:
                meta["ref_count"] = meta.get("ref_count", 0) + 1
                meta["size"] = len(content)
                return meta

            updated = self._meta_update_locked(content_hash, _update_meta)
            is_new = updated.get("ref_count", 0) == 1

            # Feature DI: Bloom filter
            if self._bloom is not None:
                self._bloom.add(content_hash)

            # Feature DI: Content cache
            if self._cache is not None:
                self._cache.put(content_hash, content)

            # Feature DI: Write callback (e.g. Zoekt reindex)
            if is_new and self._on_write_callback is not None:
                self._on_write_callback(key)

            return WriteResult(content_hash=content_hash, size=len(content))

        except (BackendError, NexusFileNotFoundError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to write content: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

    def read_content(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        # Feature DI: cache hit → skip transport
        if self._cache is not None:
            cached: bytes | None = self._cache.get(content_hash)
            if cached is not None:
                return cached

        key = self._blob_key(content_hash)

        try:
            data, _ = self._transport.get_blob(key)

            # Verify integrity
            actual_hash = hash_content(data)
            if actual_hash != content_hash:
                raise BackendError(
                    f"Content hash mismatch: expected {content_hash}, got {actual_hash}",
                    backend=self.name,
                    path=content_hash,
                )

            content = bytes(data)

            # Feature DI: cache on miss
            if self._cache is not None:
                self._cache.put(content_hash, content)

            return content

        except NexusFileNotFoundError:
            raise
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to read content: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        key = self._blob_key(content_hash)

        try:
            if not self._transport.blob_exists(key):
                raise NexusFileNotFoundError(content_hash)

            def _do_delete() -> None:
                meta = self._read_meta(content_hash)
                ref_count = meta.get("ref_count", 1)

                if ref_count <= 1:
                    # Last reference — delete blob and metadata
                    self._transport.delete_blob(key)
                    meta_key = self._meta_key(content_hash)
                    if self._transport.blob_exists(meta_key):
                        self._transport.delete_blob(meta_key)
                else:
                    meta["ref_count"] = ref_count - 1
                    self._write_meta(content_hash, meta)

            if self._stripe_lock is not None:
                lock = self._stripe_lock.acquire_for(content_hash)
                with lock:
                    _do_delete()
            else:
                _do_delete()

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

    def content_exists(self, content_hash: str, context: "OperationContext | None" = None) -> bool:
        try:
            # Bloom filter fast-miss: definitely not present → skip transport I/O
            if self._bloom is not None and not self._bloom.might_exist(content_hash):
                return False
            key = self._blob_key(content_hash)
            return self._transport.blob_exists(key)
        except Exception:
            return False

    def get_content_size(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        key = self._blob_key(content_hash)

        try:
            return self._transport.get_blob_size(key)
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to get content size: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

    def get_ref_count(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        if not self.content_exists(content_hash, context=context):
            raise NexusFileNotFoundError(content_hash)

        meta = self._read_meta(content_hash)
        return int(meta.get("ref_count", 0))

    def stream_content(
        self,
        content_hash: str,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> Iterator[bytes]:
        key = self._blob_key(content_hash)
        try:
            yield from self._transport.stream_blob(key, chunk_size)
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
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        # Collect chunks for hashing (matches write_content behavior)
        content = b"".join(chunks)
        return self.write_content(content, context=context)

    def batch_read_content(
        self,
        content_hashes: list[str],
        context: "OperationContext | None" = None,
        *,
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, bytes | None]:
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
            if self._transport.blob_exists(dir_key):
                if not exist_ok:
                    raise FileExistsError(f"Directory already exists: {path}")
                return

            if not parents:
                parent = "/".join(path.split("/")[:-1])
                if parent and not self.is_directory(parent):
                    raise FileNotFoundError(f"Parent directory not found: {parent}")

            self._transport.create_directory_marker(dir_key)

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
            if not self._transport.blob_exists(dir_key):
                raise NexusFileNotFoundError(path)

            if not recursive:
                blobs, _ = self._transport.list_blobs(prefix=dir_key, delimiter="/")
                if len(blobs) > 1:
                    raise OSError(f"Directory not empty: {path}")

            self._transport.delete_blob(dir_key)

            if recursive:
                blobs, _ = self._transport.list_blobs(prefix=dir_key, delimiter="")
                for blob_key in blobs:
                    if blob_key != dir_key:
                        try:
                            self._transport.delete_blob(blob_key)
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
            return self._transport.blob_exists(dir_key)

        except Exception:
            return False

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        try:
            path = path.strip("/")

            if path and not self.is_directory(path):
                raise FileNotFoundError(f"Directory not found: {path}")

            prefix = f"dirs/{path}/" if path else "dirs/"
            blobs, prefixes = self._transport.list_blobs(prefix=prefix, delimiter="/")

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
