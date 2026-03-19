"""CAS + Local transport backend — full-featured local storage.

Composes CASBackend (addressing) + LocalBlobTransport (I/O) +
MultipartUpload (resumable uploads) using Feature DI for Bloom filter,
content cache, stripe lock, and CDCEngine (chunking).

    CASLocalBackend = CASBackend(LocalBlobTransport)
                    + MultipartUpload     (resumable uploads, ABC)
                    + Feature DI          (Bloom, cache, stripe lock, CDC)

CDC routing is handled by CASBackend base class via Feature DI —
CASLocalBackend only instantiates and passes CDCEngine.

Naming convention: {addressing}_{transport} per Section 5.2 of
docs/architecture/backend-architecture.md.

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from nexus.backends.base.cas_backend import CASBackend
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.base.stripe_lock import _StripeLock
from nexus.backends.engines.cdc import CDCEngine
from nexus.backends.engines.multipart import MultipartUpload
from nexus.backends.transports.local_transport import LocalBlobTransport
from nexus.contracts.capabilities import ConnectorCapability
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# Default Bloom filter settings (tuned for typical CAS workloads)
DEFAULT_CAS_BLOOM_CAPACITY = 100_000
DEFAULT_CAS_BLOOM_FP_RATE = 0.01


def _init_bloom(cas_root: Path, capacity: int, fp_rate: float) -> Any:
    """Initialize Bloom filter, populate from disk. Returns None if unavailable."""
    try:
        from nexus_fast import BloomFilter

        bloom = BloomFilter(capacity, fp_rate)
        # Scan existing CAS entries
        if cas_root.exists():
            keys = [
                f.name
                for f in cas_root.rglob("*")
                if f.is_file() and f.suffix not in (".meta", ".lock")
            ]
            if keys:
                bloom.add_bulk(keys)
                logger.info("CAS Bloom filter populated with %d entries", len(keys))
        return bloom
    except ImportError:
        logger.debug("nexus_fast not available, CAS Bloom filter disabled")
        return None
    except Exception as e:
        logger.warning("Failed to initialize CAS Bloom filter: %s", e)
        return None


@register_connector(
    "cas_local",
    description="Local filesystem with CAS deduplication (new architecture)",
    category="storage",
)
class CASLocalBackend(CASBackend, MultipartUpload):
    """CAS addressing + local filesystem transport.

    CDCEngine is injected via CASBackend Feature DI (``self._cdc``).
    CDC routing (write/read/delete) is handled by CASBackend base class.
    """

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "root_path": ConnectionArg(
            type=ArgType.PATH,
            description="Root directory for storage",
            required=True,
            config_key="data_dir",
        ),
    }

    _CAPABILITIES: ClassVar[frozenset[ConnectorCapability]] = frozenset(
        {
            ConnectorCapability.CAS,
            ConnectorCapability.ROOT_PATH,
            ConnectorCapability.PARALLEL_MMAP,
            ConnectorCapability.MULTIPART_UPLOAD,
            ConnectorCapability.STREAMING,
            ConnectorCapability.BATCH_CONTENT,
            ConnectorCapability.DIRECTORY_LISTING,
        }
    )

    def __init__(
        self,
        root_path: str | Path,
        content_cache: Any | None = None,
        batch_read_workers: int = 8,
        bloom_capacity: int = DEFAULT_CAS_BLOOM_CAPACITY,
        bloom_fp_rate: float = DEFAULT_CAS_BLOOM_FP_RATE,
        on_write_callback: Any | None = None,
    ):
        self.root_path = Path(root_path).resolve()
        self.cas_root = self.root_path / "cas"
        self.dir_root = self.root_path / "dirs"
        self.content_cache = content_cache
        self.batch_read_workers = batch_read_workers

        # Ensure directories exist
        self.cas_root.mkdir(parents=True, exist_ok=True)
        self.dir_root.mkdir(parents=True, exist_ok=True)

        # Build components
        transport = LocalBlobTransport(root_path=self.root_path, fsync=True)
        bloom = _init_bloom(self.cas_root, bloom_capacity, bloom_fp_rate)
        stripe = _StripeLock()

        # Feature DI: LRU metadata cache for hot-path _read_meta()
        import cachetools

        meta_cache: Any = cachetools.LRUCache(maxsize=10_000)

        # Initialize CASBackend with Feature DI (including CDC)
        # CDCEngine requires a reference to the backend, so we create a
        # temporary instance and wire it after super().__init__().
        super().__init__(
            transport,
            backend_name="local",
            bloom_filter=bloom,
            content_cache=content_cache,
            meta_cache=meta_cache,
            stripe_lock=stripe,
            on_write_callback=on_write_callback,
        )

        # CDCEngine needs self (CASBackend internals) — wire after init
        self._cdc = CDCEngine(self)

    @property
    def name(self) -> str:
        return "local"

    @property
    def has_root_path(self) -> bool:
        return True

    @property
    def supports_parallel_mmap_read(self) -> bool:
        return True

    def _hash_to_path(self, content_hash: str) -> Path:
        """Convert content hash to full disk path for parallel mmap reads."""
        return self.root_path / self._blob_key(content_hash)

    def _is_chunked_content(self, content_hash: str) -> bool:
        """Check if content was stored as CDC chunks."""
        if self._cdc is None:
            return False
        return bool(self._cdc.is_chunked(content_hash))

    # Content operations (write_content, read_content, delete_content,
    # get_content_size, read_content_range) — all inherited from CASBackend
    # which handles CDC routing via Feature DI (self._cdc).

    # === Directory Operations (local FS native, not blob markers) ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        full_path = self.dir_root / path.lstrip("/")
        try:
            if parents:
                full_path.mkdir(parents=True, exist_ok=exist_ok)
            else:
                full_path.mkdir(exist_ok=exist_ok)
        except FileExistsError:
            if exist_ok:
                return
            raise BackendError(
                f"Directory already exists: {path}",
                backend="local",
                path=path,
            ) from None
        except FileNotFoundError:
            raise BackendError(
                f"Parent directory not found: {path}",
                backend="local",
                path=path,
            ) from None

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        full_path = self.dir_root / path.lstrip("/")
        if not full_path.exists():
            raise NexusFileNotFoundError(path=path, message=f"Directory not found: {path}")
        if not full_path.is_dir():
            raise BackendError(f"Path is not a directory: {path}", backend="local", path=path)
        try:
            if recursive:
                shutil.rmtree(full_path)
            else:
                full_path.rmdir()
        except OSError as e:
            raise BackendError(f"Directory not empty: {path}", backend="local", path=path) from e

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        full_path = self.dir_root / path.lstrip("/")
        return full_path.exists() and full_path.is_dir()

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        full_path = self.dir_root / path.lstrip("/")
        if not full_path.exists():
            raise FileNotFoundError(f"Directory not found: {path}")
        if not full_path.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")
        entries = []
        for entry in full_path.iterdir():
            name = entry.name
            if entry.is_dir():
                name += "/"
            entries.append(name)
        return sorted(entries)

    # === Multipart Upload Operations ===

    def init_multipart(
        self,
        backend_path: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> str:
        import uuid

        upload_id = str(uuid.uuid4())
        upload_dir = self.root_path / "uploads" / upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)

        meta = {"content_type": content_type, "backend_path": backend_path}
        if metadata:
            meta.update(metadata)

        meta_path = upload_dir / "_meta.json"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        return upload_id

    def upload_part(
        self,
        backend_path: str,
        upload_id: str,
        part_number: int,
        data: bytes,
    ) -> dict[str, Any]:
        upload_dir = self.root_path / "uploads" / upload_id
        if not upload_dir.exists():
            raise BackendError(
                f"Upload directory not found: {upload_id}",
                backend="local",
                path=backend_path,
            )
        part_path = upload_dir / f"part_{part_number:06d}"
        part_path.write_bytes(data)
        part_hash = hash_content(data)
        return {"etag": part_hash, "part_number": part_number}

    def complete_multipart(
        self,
        backend_path: str,
        upload_id: str,
        parts: list[dict[str, Any]],
    ) -> str:
        upload_dir = self.root_path / "uploads" / upload_id
        if not upload_dir.exists():
            raise BackendError(
                f"Upload directory not found: {upload_id}",
                backend="local",
                path=backend_path,
            )

        sorted_parts = sorted(parts, key=lambda p: p["part_number"])
        assembled = bytearray()
        for part_info in sorted_parts:
            part_path = upload_dir / f"part_{part_info['part_number']:06d}"
            if not part_path.exists():
                raise BackendError(
                    f"Part file not found: part_{part_info['part_number']:06d}",
                    backend="local",
                    path=backend_path,
                )
            assembled.extend(part_path.read_bytes())

        content = bytes(assembled)
        result = self.write_content(content)

        shutil.rmtree(upload_dir, ignore_errors=True)
        return result.content_hash

    def abort_multipart(
        self,
        backend_path: str,
        upload_id: str,
    ) -> None:
        upload_dir = self.root_path / "uploads" / upload_id
        if upload_dir.exists():
            shutil.rmtree(upload_dir, ignore_errors=True)
