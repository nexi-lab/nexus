"""Volume-packed local Transport — append-only volume files for CAS.

Wraps the Rust VolumeEngine (nexus_kernel.BlobPackEngine) and implements the
Transport protocol. Routes CAS blob keys (cas/...) to the volume engine
and delegates directory operations (dirs/...) to an internal LocalTransport.

Volume engine benefits:
    - Packs thousands of blobs into append-only volume files
    - Reduces inode overhead from ~256-536 bytes/file to ~24 bytes/entry
    - Single pread() per content read (no directory traversal)
    - Batched fdatasync at seal time (not per-blob)
    - redb index for O(1) hash → (volume, offset, size) lookup

TTL bucket routing (Issue #3405):
    - Writes with a TTL are routed to a TTL-bucketed VolumeEngine
    - Each bucket has its own directory, index, and volume lifecycle
    - Expired volumes are deleted wholesale (single unlink, no per-file GC)
    - GC only operates on the permanent engine

Cold tiering (Issue #3406):
    - Sealed volumes can be uploaded to cloud storage (S3/GCS)
    - Local .idx retained for O(1) hash → (volume, offset, size)
    - Reads for tiered volumes use HTTP range requests
    - VolumeLocalTransport intercepts reads and delegates to cloud

Crash recovery:
    - Active volumes are .tmp files — deleted on startup
    - Sealed volumes have TOC at end — can rebuild index from TOCs
    - Startup reconciliation handles all crash scenarios

Issue #3403: CAS volume packing.
Issue #3405: Volume-level TTL.
Issue #3406: Volume-level cold tiering.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from nexus.backends.transports.local_transport import LocalTransport
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError

logger = logging.getLogger(__name__)

# Key prefix that routes to the volume engine (CAS content blobs)
_CAS_PREFIX = "cas/"

# ─── TTL bucket definitions (Issue #3405) ────────────────────────────────────

# (bucket_name, max_ttl_seconds, rotation_interval_seconds)
TTL_BUCKETS: list[tuple[str, float, float]] = [
    ("1m", 5 * 60, 60),  # < 5 minutes, rotate every 1 min
    ("5m", 30 * 60, 5 * 60),  # 5–30 minutes, rotate every 5 min
    ("1h", 4 * 3600, 3600),  # 30min–4 hours, rotate every 1 hour
    ("1d", 48 * 3600, 86400),  # 4–48 hours, rotate every 1 day
    ("1w", 14 * 86400, 7 * 86400),  # 2–14 days, rotate every 1 week
]


def ceil_bucket(ttl_seconds: float) -> str | None:
    """Map a TTL duration to its bucket name.

    Returns the smallest bucket whose max_ttl >= ttl_seconds.
    Returns None if ttl_seconds exceeds all buckets (→ permanent engine).

    Raises ValueError for ttl_seconds <= 0.

    >>> ceil_bucket(30)
    '1m'
    >>> ceil_bucket(300)
    '5m'
    >>> ceil_bucket(86400)
    '1d'
    >>> ceil_bucket(999999)  # exceeds all buckets
    """
    if ttl_seconds <= 0:
        raise ValueError(f"TTL must be positive, got {ttl_seconds}")
    for name, max_ttl, _ in TTL_BUCKETS:
        if ttl_seconds <= max_ttl:
            return name
    return None  # exceeds all buckets → permanent


class BlobPackLocalTransport:
    """Volume-packed Transport for local CAS storage.

    Implements the full Transport protocol (structural typing).
    CAS blob keys (cas/...) are routed to the Rust VolumeEngine.
    All other keys (dirs/..., uploads/...) are handled by an internal
    LocalTransport for filesystem-native directory operations.

    TTL-aware writes (Issue #3405): when `store_ttl()` is called with a
    TTL, the blob is routed to a TTL-bucketed engine. Standard `store()`
    goes to the permanent engine.

    Args:
        root_path: Root directory for all storage.
        fsync: Whether LocalTransport uses fsync (for dirs/uploads).
        target_volume_size: Override volume size in bytes (0 = dynamic).
        compaction_bytes_per_cycle: Max bytes to process per compact() call.
        compaction_sparsity_threshold: Trigger compaction above this (0.0-1.0).
    """

    transport_name: str = "volume_local"

    def __init__(
        self,
        root_path: str | Path,
        *,
        fsync: bool = True,
        target_volume_size: int = 0,
        compaction_bytes_per_cycle: int = 52_428_800,
        compaction_sparsity_threshold: float = 0.3,
    ) -> None:
        self._root = Path(root_path).resolve()
        self._volume_available = False
        self._BlobPackEngine: Any = None  # Class reference for lazy creation

        # VolumeLocalTransport's entire purpose is volume packing — it has no valid
        # degraded mode.  Fail closed immediately if VolumeEngine is unavailable so
        # callers get a clear error (with rebuild instructions) rather than silently
        # writing blobs to flat-file layout that VolumeEngine will never find (Issue #3712).
        from nexus._rust_compat import BlobPackEngine as _BlobPackEngine

        if _BlobPackEngine is None:
            raise RuntimeError(
                "BlobPackEngine is unavailable (stale or absent nexus_kernel). "
                "BlobPackLocalTransport requires a working nexus_kernel binary — "
                "there is no safe degraded mode. "
                "Rebuild the extension: cd rust/kernel && maturin develop --release"
            )
        self._BlobPackEngine = _BlobPackEngine
        self._volume_available = True

        # Permanent engine for non-TTL CAS blobs
        self._engine: Any = None
        self._target_volume_size = target_volume_size
        self._compaction_bytes_per_cycle = compaction_bytes_per_cycle
        self._compaction_sparsity_threshold = compaction_sparsity_threshold

        if self._volume_available:
            volumes_dir = self._root / "cas_volumes"
            self._engine = self._BlobPackEngine(
                str(volumes_dir),
                target_volume_size,
                compaction_bytes_per_cycle,
                compaction_sparsity_threshold,
            )
            logger.info("CAS volume engine (permanent) initialized at %s", volumes_dir)

        # TTL-bucketed engines (Issue #3405): lazily created on first write
        self._ttl_engines: dict[str, Any] = {}
        # Rotation config per bucket: bucket_name → rotation_interval_seconds
        self._ttl_rotation: dict[str, float] = {name: interval for name, _, interval in TTL_BUCKETS}
        # Track last rotation time per bucket
        self._ttl_last_rotation: dict[str, float] = {}

        # Cold tiering delegate (Issue #3406): injected later via set_tiering().
        # When set, get_blob() checks tiering manifest for volumes whose .dat
        # is in cloud storage and reads via HTTP range request.
        self._tiering: Any = None  # VolumeTieringService | None

        # Delegate transport for non-CAS keys (dirs, uploads, etc.)
        # Also serves as fallback if VolumeEngine is unavailable.
        self._delegate = LocalTransport(root_path=root_path, fsync=fsync)

    def _get_ttl_engine(self, bucket: str) -> Any:
        """Get or create a TTL-bucketed VolumeEngine (lazy creation)."""
        engine = self._ttl_engines.get(bucket)
        if engine is not None:
            return engine

        if not self._volume_available:
            raise BackendError(
                "VolumeEngine not available for TTL bucket",
                backend="volume_local",
                path=bucket,
            )

        ttl_dir = self._root / "cas_volumes" / f"ttl_{bucket}"
        engine = self._BlobPackEngine(
            str(ttl_dir),
            self._target_volume_size,
            self._compaction_bytes_per_cycle,
            self._compaction_sparsity_threshold,
        )
        self._ttl_engines[bucket] = engine
        self._ttl_last_rotation[bucket] = time.time()
        logger.info("CAS volume engine (TTL %s) initialized at %s", bucket, ttl_dir)
        return engine

    def _is_cas_key(self, key: str) -> bool:
        """Check if a key should be routed to the volume engine."""
        return self._volume_available and key.startswith(_CAS_PREFIX) and not key.endswith(".meta")

    def _hash_from_key(self, key: str) -> str:
        """Extract content hash from a CAS key like 'cas/ab/cd/abcdef...'."""
        return key.split("/")[-1]

    @contextmanager
    def _cas_op(self, key: str, op_name: str) -> Iterator[tuple[str, Any]]:
        """Context manager for CAS operations — extracts hash and wraps errors.

        Yields (hash_hex, engine) if the key is a CAS key.
        Raises BackendError on failure. Re-raises NexusFileNotFoundError.

        Usage::

            with self._cas_op(key, "get") as (hash_hex, engine):
                return engine.some_method(hash_hex)
        """
        hash_hex = self._hash_from_key(key)
        try:
            yield hash_hex, self._engine
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Volume {op_name} failed: {e}", backend="volume_local", path=key
            ) from e

    # === Transport Protocol Methods ===

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        if self._is_cas_key(key):
            with self._cas_op(key, "put") as (hash_hex, engine):
                engine.put(hash_hex, data)
                return None
        return self._delegate.store(key, data, content_type)

    def store_ttl(
        self, key: str, data: bytes, ttl_seconds: float, content_type: str = ""
    ) -> str | None:
        """Write a CAS blob with TTL-based volume routing (Issue #3405).

        Routes to the appropriate TTL-bucketed engine based on ttl_seconds.
        Non-CAS keys are delegated to the filesystem transport.
        """
        if self._is_cas_key(key) and ttl_seconds > 0:
            bucket = ceil_bucket(ttl_seconds)
            if bucket is not None:
                hash_hex = self._hash_from_key(key)
                engine = self._get_ttl_engine(bucket)
                try:
                    expiry = time.time() + ttl_seconds
                    engine.put_with_expiry(hash_hex, data, expiry)
                    return None
                except Exception as e:
                    raise BackendError(
                        f"Volume TTL put failed: {e}", backend="volume_local", path=key
                    ) from e
            # TTL exceeds all buckets — fall through to permanent
        return self.store(key, data, content_type)

    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        if self._is_cas_key(key):
            hash_hex = self._hash_from_key(key)
            # Check TTL engines first, then permanent
            for engine in self._ttl_engines.values():
                try:
                    data = engine.read_content(hash_hex)
                    if data is not None:
                        return bytes(data), None
                except Exception:
                    pass
            # Permanent engine
            with self._cas_op(key, "get") as (h, engine):
                data = engine.read_content(h)
                if data is not None:
                    return bytes(data), None

                # Not found locally — check if tiered to cloud (Issue #3406).
                # Uses the Python-side blob_index (built from .vol TOC at tier
                # time) instead of engine.locate() which is not exposed via PyO3.
                if self._tiering is not None:
                    tiered_data = self._read_from_tiered(h)
                    if tiered_data is not None:
                        return tiered_data, None

                raise NexusFileNotFoundError(key)
        return self._delegate.fetch(key, version_id)

    def _read_from_tiered(self, hash_hex: str) -> bytes | None:
        """Attempt to read a blob from a tiered (cloud) volume.

        Uses the manifest's O(1) reverse hash lookup to find the volume
        and blob offset, then delegates to the tiering service for an
        HTTP range read.

        Returns None if the blob is not in any tiered volume.
        """
        if self._tiering is None:
            return None

        # O(1) lookup via manifest reverse hash set
        result = self._tiering.manifest.lookup_hash(hash_hex)
        if result is None:
            return None

        entry, offset, size = result
        try:
            data = self._tiering.read_range(
                entry.cloud_key, offset, size, volume_id=entry.volume_id
            )
            return bytes(data)
        except Exception as e:
            logger.warning(
                "Tiered read failed for hash %s in volume %s: %s",
                hash_hex[:16],
                entry.volume_id,
                e,
            )
            return None

    def remove(self, key: str) -> None:
        if self._is_cas_key(key):
            with self._cas_op(key, "delete") as (hash_hex, engine):
                existed = engine.delete(hash_hex)
                if not existed:
                    raise NexusFileNotFoundError(key)
                return
        self._delegate.remove(key)

    def exists(self, key: str) -> bool:
        if self._is_cas_key(key):
            hash_hex = self._hash_from_key(key)
            # Check TTL engines first
            for engine in self._ttl_engines.values():
                try:
                    if engine.exists(hash_hex):
                        return True
                except Exception:
                    pass
            try:
                if self._engine.exists(hash_hex):
                    return True
            except Exception:
                pass
            # O(1) check via manifest reverse hash set (Issue #3406)
            return self._tiering is not None and self._tiering.manifest.is_hash_tiered(hash_hex)
        return self._delegate.exists(key)

    def get_size(self, key: str) -> int:
        if self._is_cas_key(key):
            hash_hex = self._hash_from_key(key)
            # Check TTL engines first
            for engine in self._ttl_engines.values():
                try:
                    size = engine.get_size(hash_hex)
                    if size is not None:
                        return int(size)
                except Exception:
                    pass
            with self._cas_op(key, "get_size") as (h, engine):
                size = engine.get_size(h)
                if size is not None:
                    return int(size)
                # O(1) lookup via manifest reverse hash set (Issue #3406)
                if self._tiering is not None:
                    result = self._tiering.manifest.lookup_hash(h)
                    if result is not None:
                        return int(result[2])  # size
                raise NexusFileNotFoundError(key)
        return self._delegate.get_size(key)

    def list_keys(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        if prefix.startswith(_CAS_PREFIX) and self._volume_available:
            # Aggregate from permanent + all TTL engines
            all_hashes_ts = list(self._engine.list_content_hashes())
            for engine in self._ttl_engines.values():
                with contextlib.suppress(Exception):
                    all_hashes_ts.extend(engine.list_content_hashes())
            blob_keys = [f"cas/{h[:2]}/{h[2:4]}/{h}" for h, _ts in all_hashes_ts]
            if delimiter:
                matching = [k for k in blob_keys if k.startswith(prefix)]
                return sorted(matching), []
            return sorted(blob_keys), []
        return self._delegate.list_keys(prefix, delimiter)

    def copy_key(self, src_key: str, dst_key: str) -> None:
        if self._is_cas_key(src_key) and self._is_cas_key(dst_key):
            data, _ = self.fetch(src_key)
            self.store(dst_key, data)
            return
        if self._is_cas_key(src_key):
            data, _ = self.fetch(src_key)
            self._delegate.store(dst_key, data)
            return
        if self._is_cas_key(dst_key):
            data, _ = self._delegate.fetch(src_key)
            self.store(dst_key, data)
            return
        self._delegate.copy_key(src_key, dst_key)

    def create_dir(self, key: str) -> None:
        self._delegate.create_dir(key)

    def stream(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        if self._is_cas_key(key):
            data, _ = self.fetch(key)
            for i in range(0, len(data), chunk_size):
                yield data[i : i + chunk_size]
            return
        yield from self._delegate.stream(key, chunk_size, version_id)

    def store_chunked(
        self,
        key: str,
        chunks: Iterator[bytes],
        content_type: str = "",
    ) -> str | None:
        data = b"".join(chunks)
        return self.store(key, data, content_type)

    # === Extended Methods (used by CASAddressingEngine via hasattr) ===

    def store_nosync(self, key: str, data: bytes) -> None:
        """Write without fsync — volume engine batches fsync at seal time."""
        if self._is_cas_key(key):
            with self._cas_op(key, "put_nosync") as (hash_hex, engine):
                engine.put(hash_hex, data)
                return
        self._delegate.store_nosync(key, data)

    def store_from_path(self, key: str, src_path: str | Path) -> str | None:
        """Move a file into the volume."""
        if self._is_cas_key(key):
            src = Path(src_path)
            with self._cas_op(key, "put_from_path") as (hash_hex, engine):
                data = src.read_bytes()
                engine.put(hash_hex, data)
                src.unlink(missing_ok=True)
                return None
        return self._delegate.store_from_path(key, src_path)

    def get_mtime(self, key: str) -> float:
        """Blob write timestamp. For GC age threshold."""
        if self._is_cas_key(key):
            with self._cas_op(key, "get_mtime") as (hash_hex, engine):
                ts = engine.get_timestamp(hash_hex)
                if ts is None:
                    raise NexusFileNotFoundError(key)
                return float(ts)
        return self._delegate.get_mtime(key)

    # === New Methods (transport protocol extensions) ===

    def list_content_hashes(self) -> list[tuple[str, float]]:
        """List all content hashes with write timestamps.

        Returns list of (hash_hex, timestamp_secs) tuples.
        Used by GC for reachability scan and by Bloom filter for seeding.
        Only returns hashes from the permanent engine (GC scope).
        """
        if self._volume_available:
            try:
                return list(self._engine.list_content_hashes())
            except Exception as e:
                logger.warning("Volume list_content_hashes failed: %s", e)
                return []
        return self._delegate.list_content_hashes()

    def batch_fetch(self, keys: list[str]) -> dict[str, bytes | None]:
        """Batch read multiple blobs efficiently."""
        result: dict[str, bytes | None] = {}
        cas_keys: dict[str, str] = {}
        other_keys: list[str] = []

        for key in keys:
            if self._is_cas_key(key):
                cas_keys[key] = self._hash_from_key(key)
            else:
                other_keys.append(key)

        if cas_keys and self._volume_available:
            try:
                hash_to_key: dict[str, str] = {h: k for k, h in cas_keys.items()}
                batch_result = self._engine.batch_get(list(cas_keys.values()))
                for hash_hex, data in batch_result.items():
                    matched_key = hash_to_key.get(hash_hex)
                    if matched_key is not None:
                        key = matched_key
                        result[key] = bytes(data)
                for key in cas_keys:
                    if key not in result:
                        result[key] = None
            except Exception:
                for key in cas_keys:
                    try:
                        data, _ = self.fetch(key)
                        result[key] = data
                    except Exception:
                        result[key] = None

        for key in other_keys:
            try:
                data, _ = self._delegate.fetch(key)
                result[key] = data
            except Exception:
                result[key] = None

        return result

    # === Tiering (Issue #3406) ===

    def set_tiering(self, tiering_service: Any) -> None:
        """Inject the VolumeTieringService for cloud-backed reads.

        Called after transport construction when tiering is enabled.
        The tiering service owns the manifest and cloud transport.
        """
        self._tiering = tiering_service

    @property
    def tiering(self) -> Any:
        """Access the tiering service (for GC integration)."""
        return self._tiering

    # === Batch Pre-allocation (Issue #3409) ===

    def store_batch(self, items: list[tuple[str, bytes]]) -> int:
        """Batch-write multiple CAS blobs with pre-allocated volume slots.

        Uses the Rust VolumeEngine's batch pre-allocation API:
        1. filter_known() for dedup (Decision #14A)
        2. preallocate() for slot reservation (Decision #3A)
        3. Parallel pwrite via ThreadPoolExecutor (Decision #16A)
        4. commit_batch() for atomic index update (Decision #4A)

        Args:
            items: List of (cas_key, data) tuples. Keys must be CAS keys.

        Returns:
            Number of new blobs written (excludes duplicates).
        """
        if not self._volume_available or not items:
            return 0

        # Extract hashes and data
        hash_data: list[tuple[str, bytes]] = []
        for key, data in items:
            if self._is_cas_key(key):
                hash_hex = self._hash_from_key(key)
                hash_data.append((hash_hex, data))

        if not hash_data:
            return 0

        try:
            # Use batch_put for optimal single-call bulk write.
            # All I/O happens in Rust with GIL released — no per-entry
            # Python overhead, single index flush at the end.
            return int(self._engine.batch_put(hash_data))

        except BackendError:
            raise
        except Exception as e:
            raise BackendError(
                f"Batch store failed: {e}", backend="volume_local", path="batch"
            ) from e

    # === Volume Management ===

    def seal_active_volume(self) -> bool:
        """Seal the active volume (for testing or explicit flush)."""
        if self._volume_available:
            return bool(self._engine.seal_active())
        return False

    def compact(self) -> tuple[int, int, int]:
        """Run compaction. Returns (volumes_compacted, blobs_moved, bytes_reclaimed)."""
        if self._volume_available:
            return tuple(self._engine.compact())
        return (0, 0, 0)

    def volume_stats(self) -> dict[str, int]:
        """Get volume engine statistics."""
        if self._volume_available:
            return dict(self._engine.stats())
        return {}

    def close(self) -> None:
        """Close the volume engine (seals active volume)."""
        if self._volume_available:
            self._engine.close()
        for engine in self._ttl_engines.values():
            with contextlib.suppress(Exception):
                engine.close()
        self._ttl_engines.clear()

    def migrate_from_files(
        self,
        *,
        batch_size: int = 1000,
        delete_originals: bool = True,
        rate_limit_bytes: int = 0,
    ) -> tuple[int, int, int]:
        """Migrate existing one-file-per-hash CAS blobs into volumes."""
        if not self._volume_available:
            return (0, 0, 0)

        cas_root = str(self._root / "cas")
        return tuple(
            self._engine.migrate_from_files(
                cas_root,
                batch_size,
                delete_originals,
                rate_limit_bytes,
            )
        )

    # === TTL Volume Management (Issue #3405) ===

    def expire_ttl_volumes(self) -> list[tuple[str, int]]:
        """Run TTL expiry across all TTL-bucketed engines.

        Returns list of (bucket_name, total_entries_expired) tuples.
        """
        results: list[tuple[str, int]] = []
        for bucket, engine in self._ttl_engines.items():
            try:
                vol_results = engine.expire_ttl_volumes()
                total = sum(count for _, count in vol_results)
                if total > 0:
                    results.append((bucket, total))
                    logger.info("TTL bucket %s: expired %d entries", bucket, total)
            except Exception as e:
                logger.warning("TTL expiry failed for bucket %s: %s", bucket, e)
        return results

    def flush_expired_index(self) -> int:
        """Deferred redb cleanup for expired TTL entries.

        Called after expire_ttl_volumes() at lower priority. Readers already see
        expired entries as gone (via mem_index), so this is for on-disk consistency.
        """
        total = 0
        for engine in self._ttl_engines.values():
            with contextlib.suppress(Exception):
                total += engine.flush_expired_index()
        return total

    def rotate_ttl_volumes(self) -> int:
        """Seal TTL volumes that have exceeded their rotation interval.

        Returns count of volumes sealed.
        """
        sealed = 0
        now = time.time()
        for bucket, engine in self._ttl_engines.items():
            interval = self._ttl_rotation.get(bucket, 60)
            last = self._ttl_last_rotation.get(bucket, 0.0)
            if now - last >= interval:
                try:
                    if engine.seal_if_nonempty():
                        sealed += 1
                except Exception as e:
                    logger.warning("TTL rotation failed for bucket %s: %s", bucket, e)
                self._ttl_last_rotation[bucket] = now
        return sealed

    @property
    def ttl_engine_count(self) -> int:
        """Number of active TTL-bucketed engines."""
        return len(self._ttl_engines)

    # === Internal Helpers ===

    def move(self, src_key: str, dst_key: str) -> None:
        """Atomic move — delegate to appropriate transport."""
        if self._is_cas_key(src_key) or self._is_cas_key(dst_key):
            data, _ = self.fetch(src_key)
            self.store(dst_key, data)
            self.remove(src_key)
            return
        self._delegate.move(src_key, dst_key)
