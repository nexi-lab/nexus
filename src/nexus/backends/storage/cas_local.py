"""CAS + Local transport backend — full-featured local storage.

Composes CASAddressingEngine (addressing) + VolumeLocalTransport (I/O) +
MultipartUpload (resumable uploads) using Feature DI for content cache,
VFSSemaphore, and CDCEngine (chunking).

    CASLocalBackend = CASAddressingEngine(VolumeLocalTransport)
                    + MultipartUpload     (resumable uploads, ABC)
                    + Feature DI          (cache, VFSSemaphore, CDC)

Bloom filter was dropped in R10f — Rust stat() is fast enough that the Bloom
seeding cost on startup doesn't pay back. Tracked under #3799 if benchmarks
later justify reintroducing it.

VolumeLocalTransport packs CAS blobs into append-only volume files with a
redb index, reducing inode overhead and enabling batched fsync. Falls back
to LocalTransport if the Rust VolumeEngine is unavailable.

CDC routing is handled by CASAddressingEngine base class via Feature DI —
CASLocalBackend only instantiates and passes CDCEngine.

Naming convention: {addressing}_{transport} per Section 5.2 of
docs/architecture/backend-architecture.md.

References:
    - Issue #1323: CAS x Backend orthogonal composition
    - Issue #3403: CAS volume packing
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from nexus.backends.base.cas_addressing_engine import CASAddressingEngine
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.engines.cas_gc import CASGarbageCollector
from nexus.backends.engines.cdc import CDCEngine
from nexus.backends.engines.multipart import MultipartUpload
from nexus.backends.transports.local_transport import LocalTransport
from nexus.backends.transports.volume_local_transport import VolumeLocalTransport
from nexus.contracts.backend_features import BackendFeature
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


@register_connector("cas_local")
class CASLocalBackend(CASAddressingEngine, MultipartUpload):
    """CAS addressing + local filesystem transport.

    CDCEngine is injected via CASAddressingEngine Feature DI (``self._cdc``).
    CDC routing (write/read/delete) is handled by CASAddressingEngine base class.
    """

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "root_path": ConnectionArg(
            type=ArgType.PATH,
            description="Root directory for storage",
            required=True,
            config_key="data_dir",
        ),
    }

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = frozenset(
        {
            BackendFeature.CAS,
            BackendFeature.ROOT_PATH,
            BackendFeature.MULTIPART_UPLOAD,
            BackendFeature.STREAMING,
            BackendFeature.BATCH_CONTENT,
            BackendFeature.DIRECTORY_LISTING,
        }
    )

    def __init__(
        self,
        root_path: str | Path,
        content_cache: Any | None = None,
        batch_read_workers: int = 8,
        on_write_callback: Any | None = None,
        *,
        use_volume_packing: bool = False,
        tiering_config: Any | None = None,
    ):
        self.root_path = Path(root_path).resolve()
        self.cas_root = self.root_path / "cas"
        self.dir_root = self.root_path / "dirs"
        self.content_cache = content_cache
        self.batch_read_workers = batch_read_workers

        # Ensure directories exist
        self.cas_root.mkdir(parents=True, exist_ok=True)
        self.dir_root.mkdir(parents=True, exist_ok=True)

        # Build transport — VolumeLocalTransport with fallback to LocalTransport
        # VolumeLocalTransport packs CAS blobs into volumes; falls back internally
        # if VolumeEngine is unavailable (Issue #3403).
        # Both VolumeLocalTransport and LocalTransport implement Transport
        # structurally (Protocol), but mypy can't verify VolumeLocalTransport against
        # the Protocol since it uses dynamic PyO3 dispatch. Using Transport annotation
        # directly would fail for VolumeLocalTransport.
        transport: Any
        if use_volume_packing:
            transport = VolumeLocalTransport(root_path=self.root_path, fsync=True)
        else:
            transport = LocalTransport(root_path=self.root_path, fsync=True)

        # Feature DI: LRU metadata cache for hot-path _read_meta()
        import cachetools

        meta_cache: Any = cachetools.LRUCache(maxsize=10_000)

        # Initialize CASAddressingEngine with Feature DI (including CDC)
        # CDCEngine requires a reference to the backend, so we create a
        # temporary instance and wire it after super().__init__().
        super().__init__(
            transport,
            backend_name="local",
            content_cache=content_cache,
            meta_cache=meta_cache,
            on_write_callback=on_write_callback,
        )

        # CDCEngine needs self (CASAddressingEngine internals) — wire after init
        self._cdc = CDCEngine(self)

        # GC: metastore injected later via set_metastore() — not available at construction.
        self._gc = CASGarbageCollector(self)

        # Volume compaction (Issue #3408): background scheduler.
        # Requires volume packing (VolumeLocalTransport).
        self._compactor: Any | None = None
        if isinstance(transport, VolumeLocalTransport):
            from nexus.services.volume_compactor import VolumeCompactor

            self._compactor = VolumeCompactor(transport)

        # Cold tiering (Issue #3406): wire VolumeTieringService if enabled.
        # Requires volume packing (VolumeLocalTransport).
        self._tiering_service: Any | None = None
        if (
            tiering_config is not None
            and tiering_config.enabled
            and isinstance(transport, VolumeLocalTransport)
        ):
            self._tiering_service = self._init_tiering(transport, tiering_config)

    @staticmethod
    def _init_tiering(transport: VolumeLocalTransport, config: Any) -> Any:
        """Create and wire VolumeTieringService into the transport.

        Creates the appropriate cloud transport (S3 or GCS) based on config,
        then creates VolumeTieringService and injects it into the transport.

        Issue #3406: Volume-level cold tiering.
        """
        # NOTE: Layering violation — backend importing from services.
        # VolumeTieringService creation should be lifted to the factory/orchestrator.
        # Kept as lazy import to avoid circular dependency at module load time.
        from nexus.services.volume_tiering import VolumeTieringService

        cloud_transport: Any
        if config.cloud_backend == "s3":
            from nexus.backends.transports.s3_transport import S3Transport

            cloud_transport = S3Transport(bucket_name=config.cloud_bucket)
        elif config.cloud_backend == "gcs":
            from nexus.backends.transports.gcs_transport import GCSTransport

            cloud_transport = GCSTransport(bucket_name=config.cloud_bucket)
        else:
            logger.warning("Unknown tiering cloud_backend: %s, skipping", config.cloud_backend)
            return None

        volumes_dir = transport._root / "cas_volumes"
        service = VolumeTieringService(
            volumes_dir=volumes_dir,
            cloud_transport=cloud_transport,
            config=config,
        )
        transport.set_tiering(service)
        logger.info(
            "Cold tiering configured: backend=%s, bucket=%s",
            config.cloud_backend,
            config.cloud_bucket,
        )
        return service

    def _on_mount(self, mount_point: str) -> None:
        """Start background services when the backend is mounted."""
        import asyncio

        logger.info("CAS engine mounted at %s (backend=%s)", mount_point, self._backend_name)
        if self._compactor is not None:
            asyncio.ensure_future(self._compactor.start())
            logger.info("Volume compactor scheduled to start on mount")
        if self._tiering_service is not None:
            asyncio.ensure_future(self._tiering_service.start())
            logger.info("Cold tiering service scheduled to start on mount")

    def _on_unmount(self) -> None:
        """Stop background services when the backend is unmounted."""
        import asyncio

        if self._compactor is not None:
            asyncio.ensure_future(self._compactor.stop())
            logger.info("Volume compactor scheduled to stop on unmount")
        if self._tiering_service is not None:
            asyncio.ensure_future(self._tiering_service.stop())
            logger.info("Cold tiering service scheduled to stop on unmount")

    def set_metastore(self, metastore: Any) -> None:
        """Inject metastore reference for GC reachability scan."""
        self._gc.set_metastore(metastore)

    @property
    def name(self) -> str:
        return "local"

    @property
    def has_root_path(self) -> bool:
        return True

    def _hash_to_path(self, content_hash: str) -> Path:
        """Convert content hash to full disk path."""
        return self.root_path / self._blob_key(content_hash)

    def batch_read_content(
        self,
        content_ids: list[str],
        context: OperationContext | None = None,
        *,
        contexts: dict[str, OperationContext] | None = None,
    ) -> dict[str, bytes | None]:
        """Read multiple content items via transport batch_fetch.

        Uses transport.batch_fetch() for efficient bulk reads — works for
        both volume-packed storage (batch pread) and file-per-blob (mmap bulk).
        Falls back to sequential reads if batch_fetch is unavailable.
        """
        if len(content_ids) <= 1:
            return super().batch_read_content(content_ids, context, contexts=contexts)

        if hasattr(self._transport, "batch_fetch"):
            keys = [self._blob_key(cid) for cid in content_ids]
            key_results = self._transport.batch_fetch(keys)
            result: dict[str, bytes | None] = {}
            for cid, key in zip(content_ids, keys, strict=True):
                result[cid] = key_results.get(key)
            return result

        return super().batch_read_content(content_ids, context, contexts=contexts)

    def _is_chunked_content(self, content_hash: str) -> bool:
        """Check if content was stored as CDC chunks."""
        if self._cdc is None:
            return False
        return bool(self._cdc.is_chunked(content_hash))

    # Content operations (write_content, read_content, delete_content,
    # get_content_size, read_content_range) — all inherited from CASAddressingEngine
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
        except FileNotFoundError:
            pass  # Already gone — metastore/backend out of sync, that's fine
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
        return result.content_id

    def abort_multipart(
        self,
        backend_path: str,
        upload_id: str,
    ) -> None:
        upload_dir = self.root_path / "uploads" / upload_id
        if upload_dir.exists():
            shutil.rmtree(upload_dir, ignore_errors=True)
