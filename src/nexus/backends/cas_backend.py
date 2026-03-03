"""CAS addressing engine over any BlobTransport.

CASBackend implements ObjectStoreABC (via Backend) using content-addressable
storage semantics: content is stored by hash, automatically deduplicated,
and reference-counted.

    CASBackend(transport: BlobTransport)
        ├── GCSBackend      — thin: creates GCSBlobTransport, registered as "gcs"
        └── (future S3CAS)  — thin: creates S3BlobTransport

The transport is INTERNAL — callers never see BlobTransport.  They see Backend.
Thin subclasses exist for: registration, CONNECTION_ARGS, connector-specific
features (batch reads, signed URLs, versioning).

Storage layout (in transport key-space):
    cas/<hash[0:2]>/<hash[2:4]>/<hash>       # Content blob
    cas/<hash[0:2]>/<hash[2:4]>/<hash>.meta   # JSON metadata sidecar
    dirs/<path>/                               # Directory marker

References:
    - Issue #1323: CAS x Backend orthogonal composition
    - backends/cas_blob_store.py — local CAS engine (reference, NOT reused)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, ClassVar

from nexus.backends.backend import Backend
from nexus.backends.blob_transport import BlobTransport
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
    ) -> None:
        self._transport = transport
        self._backend_name = backend_name or f"cas-{transport.transport_name}"

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
        """Read metadata sidecar.  Returns default dict if not found."""
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
        """Write metadata sidecar (JSON)."""
        key = self._meta_key(content_hash)
        try:
            self._transport.put_blob(key, json.dumps(meta).encode(), "application/json")
        except Exception as e:
            raise BackendError(
                f"Failed to write CAS metadata: {e}",
                backend=self.name,
                path=content_hash,
            ) from e

    # === Content Operations (ObjectStoreABC) ===

    def write_content(
        self, content: bytes, context: "OperationContext | None" = None
    ) -> WriteResult:
        content_hash = hash_content(content)
        key = self._blob_key(content_hash)

        try:
            is_new = not self._transport.blob_exists(key)

            if is_new:
                self._transport.put_blob(key, content)
                self._write_meta(content_hash, {"ref_count": 1, "size": len(content)})
            else:
                meta = self._read_meta(content_hash)
                meta["ref_count"] = meta.get("ref_count", 0) + 1
                self._write_meta(content_hash, meta)

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

            return bytes(data)

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
