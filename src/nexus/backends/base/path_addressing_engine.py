"""Path-based addressing engine over any Transport.

PathAddressingEngine implements ObjectStoreABC (via Backend) using direct path mapping:
files are stored at their actual paths, with no CAS transformation or
deduplication.

    PathAddressingEngine(transport: Transport)
        ├── PathGCSBackend       — thin: creates GCSTransport + cache
        ├── PathS3Backend        — thin: creates S3Transport + cache + multipart
        └── (future Azure)       — thin: creates AzureTransport

This replaces ``BaseBlobStorageConnector`` which used abstract methods (inheritance)
for cloud-specific I/O.  PathAddressingEngine uses composition (Transport protocol).

References:
    - Issue #1323: CAS x Backend orthogonal composition
    - backends/base_blob_connector.py — predecessor (being replaced)
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import Iterator
from typing import TYPE_CHECKING, ClassVar

from nexus.backends.base.backend import Backend
from nexus.backends.base.transport import Transport
from nexus.contracts.backend_features import BLOB_BACKEND_FEATURES, BackendFeature
from nexus.contracts.exceptions import AuthenticationError, BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


class PathAddressingEngine(Backend):
    """Path-based addressing over any Transport.

    Files are stored at their actual paths (with optional prefix).
    No CAS transformation, no deduplication, no reference counting.
    External tools can browse the bucket normally.

    Attributes:
        _transport: The underlying Transport for raw I/O.
        _backend_name: Human-readable backend identifier.
        bucket_name: Storage bucket/container name.
        prefix: Optional prefix for all paths.
        versioning_enabled: Whether versioning is enabled on the bucket.
    """

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = BLOB_BACKEND_FEATURES

    def __init__(
        self,
        transport: Transport,
        *,
        backend_name: str | None = None,
        bucket_name: str = "",
        prefix: str = "",
        versioning_enabled: bool = False,
    ) -> None:
        super().__init__()
        self._transport = transport
        self._backend_name = backend_name or f"path-{transport.transport_name}"
        self.bucket_name = bucket_name
        self.prefix = prefix.rstrip("/")
        self.versioning_enabled = versioning_enabled

    @property
    def name(self) -> str:
        return self._backend_name

    # === Helper Methods ===

    def _compute_hash(self, content: bytes) -> str:
        """Compute BLAKE3 hash of content (Rust-accelerated)."""
        return hash_content(content)

    def _detect_content_type(self, backend_path: str, content: bytes) -> str:
        """Detect Content-Type from path extension and content."""
        content_type, _ = mimetypes.guess_type(backend_path)

        if not content_type or content_type.startswith("text/"):
            try:
                content.decode("utf-8")
                if content_type and content_type.startswith("text/"):
                    return f"{content_type}; charset=utf-8"
                else:
                    return "text/plain; charset=utf-8"
            except UnicodeDecodeError:
                return content_type or "application/octet-stream"

        return content_type

    def _get_key_path(self, backend_path: str) -> str:
        """Convert backend-relative path to full blob path.

        Raises:
            BackendError: If path contains traversal components (e.g., "..").
        """
        import posixpath

        backend_path = backend_path.lstrip("/")

        # Security: reject path traversal attempts (e.g., "../../etc/passwd")
        normalized = posixpath.normpath(backend_path) if backend_path else ""
        if normalized == ".." or normalized.startswith("../"):
            raise BackendError(
                f"Path traversal detected: {backend_path}",
                backend=getattr(self, "name", "blob"),
                path=backend_path,
            )
        backend_path = normalized

        if self.prefix:
            if backend_path:
                return f"{self.prefix}/{backend_path}"
            else:
                return self.prefix
        return backend_path

    def _is_version_id(self, value: str) -> bool:
        """Check if value looks like a version ID (not a hex hash).

        Subclasses can override for cloud-specific logic (e.g. GCS generation
        numbers are all-digit strings).
        """
        if len(value) == 64:
            try:
                int(value, 16)
                return False  # It's a hex hash
            except ValueError:
                pass
        return True  # Likely a version ID

    # === Content Operations (ObjectStoreABC) ===

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        # Use content_id as blob_path when provided; fall back to context.backend_path
        if content_id:
            backend_path = content_id
        elif context and context.backend_path:
            backend_path = context.backend_path
        else:
            raise BackendError(
                f"{self.name} connector requires content_id or backend_path in OperationContext. "
                "This backend stores files at actual paths, not CAS hashes.",
                backend=self.name,
            )

        # Offset write: read old content, splice, write back (Issue #1395)
        if offset > 0:
            blob_path = self._get_key_path(backend_path)
            try:
                old_data, _ = self._transport.fetch(blob_path)
            except Exception:
                old_data = b""
            # Zero-fill gap if offset > len(old_data)
            if offset > len(old_data):
                old_data = old_data + b"\x00" * (offset - len(old_data))
            content = old_data[:offset] + content + old_data[offset + len(content) :]

        blob_path = self._get_key_path(backend_path)
        content_type = self._detect_content_type(backend_path, content)
        result = self._transport.store(blob_path, content, content_type)

        # If versioning, store returns version_id; otherwise compute hash
        content_hash = result if result is not None else self._compute_hash(content)

        return WriteResult(content_id=content_hash, version=content_hash, size=len(content))

    def read_content(self, content_id: str, context: "OperationContext | None" = None) -> bytes:
        if not context or not context.backend_path:
            raise BackendError(
                f"{self.name} connector requires backend_path in OperationContext. "
                "This backend reads files from actual paths, not CAS hashes.",
                backend=self.name,
            )

        blob_path = self._get_key_path(context.backend_path)

        version_id = None
        if self.versioning_enabled and content_id and self._is_version_id(content_id):
            version_id = content_id

        content, _version_id = self._transport.fetch(blob_path, version_id)
        return content

    def stream_content(
        self,
        content_id: str,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> Iterator[bytes]:
        if not context or not context.backend_path:
            raise ValueError(f"{self.name} connector requires backend_path in OperationContext.")

        blob_path = self._get_key_path(context.backend_path)

        try:
            version_id = None
            if self.versioning_enabled and content_id and self._is_version_id(content_id):
                version_id = content_id

            yield from self._transport.stream(blob_path, chunk_size, version_id)

        except (NexusFileNotFoundError, BackendError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to stream content from {blob_path}: {e}",
                backend=self.name,
                path=blob_path,
            ) from e

    def delete_content(self, content_id: str, context: "OperationContext | None" = None) -> None:
        if not context or not context.backend_path:
            raise BackendError(
                f"{self.name} connector requires backend_path in OperationContext",
                backend=self.name,
            )

        blob_path = self._get_key_path(context.backend_path)
        self._transport.remove(blob_path)

    def content_exists(self, content_id: str, context: "OperationContext | None" = None) -> bool:
        if not context or not context.backend_path:
            return False
        try:
            blob_path = self._get_key_path(context.backend_path)
            return self._transport.exists(blob_path)
        except NexusFileNotFoundError:
            # The only failure mode that legitimately maps to "does not exist".
            return False
        except AuthenticationError:
            # Auth-required is not "missing" — the server needs to surface
            # 401 + recovery_hint so clients can re-auth.  Collapsing this
            # into False lets exists-checks become silent no-ops (Issue #3822).
            raise
        # BackendError and any other failure propagate — a storage outage is
        # not the same as "file not found".

    def get_content_size(self, content_id: str, context: "OperationContext | None" = None) -> int:
        if not context or not context.backend_path:
            raise BackendError(
                f"{self.name} connector requires backend_path in OperationContext",
                backend=self.name,
            )

        blob_path = self._get_key_path(context.backend_path)
        return self._transport.get_size(blob_path)

    # === Public path-based helpers (for kernel copy/rename) ===

    def get_size_by_path(self, backend_path: str) -> int:
        """Get blob size by backend-relative path (public API for kernel)."""
        blob_path = self._get_key_path(backend_path.strip("/"))
        return self._transport.get_size(blob_path)

    def get_version_by_path(self, backend_path: str) -> str | None:
        """Get blob version/generation by backend-relative path.

        Returns None if the transport doesn't support versioning.
        """
        blob_path = self._get_key_path(backend_path.strip("/"))
        get_ver = getattr(self._transport, "get_version_id", None) or getattr(
            self._transport, "get_generation", None
        )
        if get_ver:
            return str(get_ver(blob_path))
        return None

    # === Internal I/O ===

    def _download(self, blob_path: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        """Thin wrapper around transport.fetch."""
        return self._transport.fetch(blob_path, version_id)

    def _bulk_download(
        self,
        blob_paths: list[str],
        version_ids: dict[str, str] | None = None,
        max_workers: int = 20,
    ) -> dict[str, bytes]:
        """Parallel download of multiple blobs via transport."""
        if not blob_paths:
            return {}

        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: dict[str, bytes] = {}

        def download_one(blob_path: str) -> tuple[str, bytes | None]:
            try:
                version_id = version_ids.get(blob_path) if version_ids else None
                content, _ = self._transport.fetch(blob_path, version_id)
                return (blob_path, content)
            except Exception as e:
                logger.warning("Failed to download %s: %s", blob_path, e)
                return (blob_path, None)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(download_one, p): p for p in blob_paths}
            for future in as_completed(futures):
                blob_path, content = future.result()
                if content is not None:
                    results[blob_path] = content

        return results

    # === Batch Operations ===

    def batch_read_content(
        self,
        content_ids: list[str],
        context: "OperationContext | None" = None,
        *,
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, bytes | None]:
        content_hashes = content_ids  # PAS: opaque id, kept as local alias
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

        max_workers = min(8, len(content_hashes))
        result: dict[str, bytes | None] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_read_one, h): h for h in content_hashes}
            for future in as_completed(futures):
                hash_key, content = future.result()
                result[hash_key] = content

        return result

    def batch_get_versions(
        self,
        backend_paths: list[str],
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, str | None]:
        """Get versions for multiple files.  Default: sequential.

        Subclasses should override for cloud-specific batch optimizations.
        """
        if not hasattr(self, "get_version"):
            return dict.fromkeys(backend_paths)

        results: dict[str, str | None] = {}
        for path in backend_paths:
            ctx = contexts.get(path) if contexts else None
            try:
                results[path] = self.get_version(path, ctx)
            except Exception:
                results[path] = None
        return results

    def batch_write_content(
        self,
        items: list[tuple[str, bytes]],
        context: "OperationContext | None" = None,
        *,
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, "WriteResult | None"]:
        if not items:
            return {}

        if len(items) == 1:
            cid, data = items[0]
            try:
                return {cid: self.write_content(data, cid, context=context)}
            except Exception:
                return {cid: None}

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _write_one(cid: str, data: bytes) -> tuple[str, "WriteResult | None"]:
            try:
                ctx = contexts.get(cid, context) if contexts else context
                return (cid, self.write_content(data, cid, context=ctx))
            except Exception:
                return (cid, None)

        max_workers = min(8, len(items))
        result: dict[str, WriteResult | None] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_write_one, cid, data): cid for cid, data in items}
            for future in as_completed(futures):
                cid, wr = future.result()
                result[cid] = wr

        return result

    def batch_delete_content(
        self,
        content_ids: list[str],
        context: "OperationContext | None" = None,
        *,
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, bool]:
        if not content_ids:
            return {}

        if len(content_ids) == 1:
            try:
                self.delete_content(content_ids[0], context=context)
                return {content_ids[0]: True}
            except Exception:
                return {content_ids[0]: False}

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _delete_one(cid: str) -> tuple[str, bool]:
            try:
                ctx = contexts.get(cid, context) if contexts else context
                self.delete_content(cid, context=ctx)
                return (cid, True)
            except Exception:
                return (cid, False)

        max_workers = min(8, len(content_ids))
        result: dict[str, bool] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_delete_one, cid): cid for cid in content_ids}
            for future in as_completed(futures):
                cid, ok = future.result()
                result[cid] = ok

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
            return

        blob_path = self._get_key_path(path) + "/"

        if self._transport.exists(blob_path):
            if not exist_ok:
                raise BackendError(
                    f"Directory already exists: {path}",
                    backend=self.name,
                    path=path,
                )
            return

        if not parents:
            parent = "/".join(path.split("/")[:-1])
            if parent and not self.is_directory(parent):
                raise NexusFileNotFoundError(
                    path=parent,
                    message=f"Parent directory not found: {parent}",
                )

        self._transport.create_dir(blob_path)

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

        blob_path = self._get_key_path(path) + "/"

        if not self._transport.exists(blob_path):
            raise NexusFileNotFoundError(
                path=path,
                message=f"Directory not found: {path}",
            )

        if not recursive:
            blobs, prefixes = self._transport.list_keys(prefix=blob_path, delimiter="/")
            if len(blobs) > 1 or prefixes:
                raise BackendError(
                    f"Directory not empty: {path}",
                    backend=self.name,
                    path=path,
                )

        if recursive:
            blobs, _ = self._transport.list_keys(prefix=blob_path, delimiter="")
            for blob_key in blobs:
                if blob_key != blob_path:
                    try:
                        self._transport.remove(blob_key)
                    except Exception as e:
                        logger.debug("Failed to delete blob during recursive rmdir: %s", e)

        self._transport.remove(blob_path)

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        try:
            path = path.strip("/")
            if not path:
                return True

            blob_path = self._get_key_path(path)

            if self._transport.exists(blob_path + "/"):
                return True

            blobs, prefixes = self._transport.list_keys(prefix=blob_path + "/", delimiter="/")
            return len(blobs) > 0 or len(prefixes) > 0

        except Exception:
            return False

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        try:
            path = path.strip("/")

            if path and not self.is_directory(path):
                raise FileNotFoundError(f"Directory not found: {path}")

            blob_base_path = self._get_key_path(path)
            prefix = blob_base_path + "/" if blob_base_path else ""

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
            if isinstance(e, BackendError):
                raise
            raise BackendError(
                f"Failed to list directory {path}: {e}",
                backend=self.name,
                path=path,
            ) from e

    def copy_file(
        self,
        src_path: str,
        dst_path: str,
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> None:
        """Copy a file using backend-native server-side copy.

        Optimistic — no pre-existence checks. The transport's copy_key
        will raise NexusFileNotFoundError if the source doesn't exist.
        """
        try:
            src_path = src_path.strip("/")
            dst_path = dst_path.strip("/")
            src_blob = self._get_key_path(src_path)
            dst_blob = self._get_key_path(dst_path)
            self._transport.copy_key(src_blob, dst_blob)
        except (FileNotFoundError, NexusFileNotFoundError):
            raise
        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(
                f"Failed to copy file {src_path} -> {dst_path}: {e}",
                backend=self.name,
                path=src_path,
            ) from e

    # -- Streaming I/O (cross-backend copy support, Issue #3329) --

    # Default chunk size for cross-backend streaming (8 MB).
    _STREAM_CHUNK_SIZE = 8 * 1024 * 1024

    def stream_file(
        self,
        path: str,
        chunk_size: int | None = None,
    ) -> "Iterator[bytes]":
        """Stream file content as an iterator of byte chunks.

        Used by the kernel for cross-backend streaming copy.
        """
        path = path.strip("/")
        blob_path = self._get_key_path(path)
        return self._transport.stream(
            blob_path,
            chunk_size=chunk_size or self._STREAM_CHUNK_SIZE,
        )

    def write_file_chunked(
        self,
        path: str,
        chunks: "Iterator[bytes]",
        content_type: str = "",
    ) -> str | None:
        """Write a file from an iterator of byte chunks.

        Delegates to the transport's ``store_chunked()`` for
        memory-efficient streaming writes (S3 multipart, GCS resumable).
        """
        path = path.strip("/")
        blob_path = self._get_key_path(path)
        return self._transport.store_chunked(
            blob_path,
            chunks,
            content_type=content_type,
        )

    def rename_file(
        self,
        old_path: str,
        new_path: str,
        context: "OperationContext | None" = None,
    ) -> None:
        """Rename/move a file (copy + delete)."""
        try:
            old_path = old_path.strip("/")
            new_path = new_path.strip("/")

            old_blob_path = self._get_key_path(old_path)
            new_blob_path = self._get_key_path(new_path)

            # Check existence for both files and directories
            old_exists = self._transport.exists(old_blob_path) or self._transport.exists(
                old_blob_path + "/"
            )
            if not old_exists:
                raise FileNotFoundError(f"Source not found: {old_path}")

            new_exists = self._transport.exists(new_blob_path) or self._transport.exists(
                new_blob_path + "/"
            )
            if new_exists:
                raise FileExistsError(f"Destination already exists: {new_path}")

            if hasattr(self._transport, "move"):
                self._transport.move(old_blob_path, new_blob_path)
            else:
                self._transport.copy_key(old_blob_path, new_blob_path)
                self._transport.remove(old_blob_path)

        except (FileNotFoundError, FileExistsError):
            raise
        except Exception as e:
            if isinstance(e, BackendError):
                raise
            raise BackendError(
                f"Failed to rename file {old_path} -> {new_path}: {e}",
                backend=self.name,
                path=old_path,
            ) from e


# Backward-compat alias (will be removed in a future cleanup)
PathBackend = PathAddressingEngine
